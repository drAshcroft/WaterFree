"""`waterfree gpu ...` -- who holds the GPU, who is waiting, and making room."""

from __future__ import annotations

import argparse
import subprocess
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from backend.cli._common import EXIT_OK, EXIT_USAGE, emit_error, emit_json
from backend.cli._gpu import (
    add_gpu_args,
    policy_from,
    run_gpu_action,
    run_gpu_action_quiet,
)
from backend.gpu import lease as gpu_lease
from backend.gpu import ollama as gpu_ollama
from backend.gpu import vram
from backend.gpu.preflight import exclusive_gpu


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("gpu", help="GPU lease, queue and free memory")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    actions.add_parser("status", help="Lease holders, the queue, free VRAM and resident Ollama models")

    p_evict = actions.add_parser(
        "evict", help="Wait for the GPU, unload resident Ollama models, report free VRAM")
    add_gpu_args(p_evict, vram_check=False)

    p_run = actions.add_parser(
        "run", help="Run any command while holding the GPU exclusively (e.g. a Whisper or ComfyUI script)")
    p_run.add_argument("--vram-gb", type=float, default=None,
                       help="Free VRAM the command needs; checked after Ollama is unloaded.")
    p_run.add_argument("--label", default="", help="Name shown in `gpu status` (default: the command).")
    add_gpu_args(p_run)
    p_run.add_argument("command", nargs=argparse.REMAINDER,
                       help="The command, after `--`: waterfree gpu run -- python transcribe.py a.m4a")

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    if args.action == "status":
        emit_json(status())
        return EXIT_OK
    if args.action == "evict":
        return run_gpu_action(lambda: evict(args))
    if args.action == "run":
        return run_command(args)
    return emit_error(f"unknown action: {args.action}", exit_code=EXIT_USAGE)


def run_command(args: Namespace) -> int:
    """Hold the GPU for an external program; its exit code and output pass straight through.

    The program inherits the lease (WATERFREE_GPU_LEASE), so any WaterFree command
    it runs reuses it instead of queueing behind its own parent.
    """
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        return emit_error("Give the command after `--`.", exit_code=EXIT_USAGE)
    label = args.label or f"gpu run {Path(command[0]).name}"
    holder: dict = {}

    def action() -> dict:
        with exclusive_gpu(label=label, detail=" ".join(command)[:120], vram_gb=args.vram_gb,
                           policy=policy_from(args), check_vram=not args.skip_vram_check):
            try:
                holder["code"] = subprocess.call(command, env=gpu_lease.child_env())
            except OSError as exc:
                raise ValueError(f"Could not run {command[0]}: {exc}") from exc
        return {}

    # Errors (GPU busy, too little VRAM) are reported as JSON like every GPU action;
    # on success the command's own output is the output, and its exit code ours.
    code = run_gpu_action_quiet(action)
    return holder.get("code", code)


def status() -> dict:
    gpu = vram.query()
    resident = gpu_ollama.resident_models()
    others = vram.gpu_processes()
    from backend.animgen import engine as kimodo
    from backend.audiogen import qa as audio_qa
    from backend.engines.catalog import ENGINES, MODEL_SETS
    from backend.engines.install import engine_status, model_status
    engines = {key: engine_status(release)["installed"] for key, release in ENGINES.items()}
    # Python engines: a venv + HF-cache weights rather than a catalog release.
    engines[kimodo.KIMODO.key] = kimodo.status()["installed"]
    engines[audio_qa.AUDIOQA.key] = audio_qa.status()["installed"]
    return {
        "lease": gpu_lease.snapshot(),
        "gpu": gpu.public() if gpu else None,
        "ollama": {"base": gpu_ollama.local_base(), "reachable": resident is not None,
                   "resident": resident or []},
        "otherGpuProcesses": vram.significant_process_names(others),
        "engines": engines,
        "models": {key: model_status(model_set)["installed"] for key, model_set in MODEL_SETS.items()},
        "note": "Entries whose process has exited are removed automatically on every read.",
    }


def evict(args: Namespace) -> dict:
    with gpu_lease.hold(gpu_lease.EXCLUSIVE, label="gpu evict", detail="unloading Ollama models",
                        policy=policy_from(args)):
        evicted = gpu_ollama.evict_all()
        gpu = vram.query()
    return {"evictedOllamaModels": evicted, "gpu": gpu.public() if gpu else None}
