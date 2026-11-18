"""`waterfree imagegen ...` — local GPU image generation."""

from __future__ import annotations

import sys
from argparse import Namespace, _SubParsersAction

from backend.cli._gpu import add_gpu_args, policy_from, run_gpu_action
from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
    resolve_workspace,
)
from backend.imagegen import backend_diffusers as backend
from backend.imagegen.config import (
    OFFLOAD_MODES,
    PRESETS,
    apply_overrides,
    available_vram_gb,
    fits_vram,
    load_config,
    output_dir_for,
    write_default_config,
)
from backend.imagegen.generate import MAX_BATCH, generate


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("imagegen", help="Generate images on the local GPU")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_make = actions.add_parser("make", help="Generate image(s) from a prompt")
    p_make.add_argument("prompt")
    p_make.add_argument("-n", "--count", type=int, default=1,
                        help=f"How many images (1-{MAX_BATCH}). Default: 1.")
    p_make.add_argument("--preset", default=None, choices=sorted(PRESETS),
                        help="Model preset. See `waterfree imagegen models`.")
    p_make.add_argument("--steps", type=int, default=None,
                        help="Denoising steps. Overrides the preset.")
    p_make.add_argument("--guidance", type=float, default=None,
                        help="Guidance scale. Use 0 for distilled models (turbo, schnell).")
    p_make.add_argument("--width", type=int, default=None)
    p_make.add_argument("--height", type=int, default=None)
    p_make.add_argument("--negative-prompt", default=None,
                        help="Ignored by distilled presets, which run without guidance.")
    p_make.add_argument("--seed", type=int, default=None,
                        help="Fix the seed for a reproducible image. Omit for random.")
    p_make.add_argument("--offload", default=None, choices=sorted(OFFLOAD_MODES),
                        help="VRAM strategy. 'sequential' saves the most and is slowest.")
    p_make.add_argument("--output-dir", default="",
                        help="Where to write. Default: <workspace>/.waterfree/generated")
    p_make.add_argument("--hf-token", default="",
                        help="Hugging Face token for gated repos. Falls back to $HF_TOKEN.")
    p_make.add_argument('--backend', choices=('diffusers', 'comfy'), default=None)
    p_make.add_argument('--image', default=None, help='Input image for img2img (path relative to current directory).')
    p_make.add_argument('--mask', default=None, help='Inpainting mask: white edits, black preserves. Requires --image.')
    p_make.add_argument('--strength', type=float, default=None, help='Editing strength, greater than 0 and at most 1.')
    p_make.add_argument('--lora', action='append', default=None, help='Repeat PATH=WEIGHT; local Diffusers file/repo or registered ComfyUI LoRA.')
    p_make.add_argument('--comfy-url', default=None)
    p_make.add_argument('--checkpoint', default=None, help='Registered ComfyUI checkpoint; required if more than one is available.')
    p_make.add_argument('--timeout', type=float, default=None, help='ComfyUI job timeout in seconds.')
    p_make.add_argument('--comfy-free', dest='comfy_free_after', action='store_true', default=None,
                        help='After the job, ask a ComfyUI on this machine to unload its models (POST /free) so the '
                             'next GPU job finds the VRAM. ComfyUI keeps them loaded otherwise.')
    add_gpu_args(p_make)
    add_workspace_arg(p_make)

    p_models = actions.add_parser("models", help="List presets and whether they fit this GPU")
    add_workspace_arg(p_models)

    p_status = actions.add_parser("status", help="Check that the backend is usable")
    p_status.add_argument('--backend', choices=('diffusers', 'comfy'), default=None)
    p_status.add_argument('--comfy-url', default=None)
    add_workspace_arg(p_status)

    p_init = actions.add_parser("init", help="Write .waterfree/imagegen.json with every setting")
    add_workspace_arg(p_init)

    p_install = actions.add_parser(
        "install", help="Download preset weights into the Hugging Face cache (skips what is cached)")
    p_install.add_argument("presets", nargs="*", default=None, metavar="PRESET",
                           help=f"Preset(s) to install: {', '.join(sorted(PRESETS))}, or 'all'. "
                                "Default: the workspace's preset.")
    p_install.add_argument("--hf-token", default="", help="Token for gated repos. Falls back to $HF_TOKEN.")
    add_workspace_arg(p_install)

    p_unload = actions.add_parser(
        "unload", help="Make room on the GPU: wait for it, unload resident Ollama models (= `gpu evict`)")
    add_gpu_args(p_unload, vram_check=False)
    add_workspace_arg(p_unload)

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    action = args.action
    workspace = resolve_workspace(args)

    if action == "models":
        vram = available_vram_gb()
        emit_json({
            "gpuVramGb": round(vram, 1),
            "presets": [
                {
                    "preset": preset.key,
                    "label": preset.label,
                    "repoId": preset.repo_id,
                    "diskGb": preset.disk_gb,
                    "vramGb": preset.vram_gb,
                    "fitsGpu": fits_vram(preset, vram),
                    "defaults": {
                        "steps": preset.steps,
                        "guidance": preset.guidance,
                        "size": f"{preset.width}x{preset.height}",
                        "dtype": preset.dtype,
                    },
                    "notes": preset.notes,
                }
                for preset in sorted(PRESETS.values(), key=lambda p: p.disk_gb)
            ],
        })
        return EXIT_OK

    if action == "status":
        config = apply_overrides(load_config(workspace), backend=args.backend, comfy_url=args.comfy_url)
        if config.backend == 'comfy':
            from backend.imagegen import backend_comfy
            usable, reason = backend_comfy.is_available(config)
        else:
            usable, reason = backend.is_available()
        payload = {
            "usable": usable,
            "reason": reason,
            "gpuVramGb": round(available_vram_gb(), 1),
            "preset": config.preset,
            "backend": config.backend,
            "outputDir": str(output_dir_for(config, workspace)),
        }
        if usable and config.backend == "diffusers" and config.preset in PRESETS:
            # `make` never downloads, so a preset that is not cached is not usable yet.
            installed = backend.is_installed(PRESETS[config.preset].repo_id)
            payload["installed"] = installed
            if not installed:
                payload["usable"] = usable = False
                payload["reason"] = f"Preset weights are not installed. Run: waterfree imagegen install {config.preset}"
        from backend.runtime_paths import find_shadowing  # noqa: PLC0415
        shadowing = find_shadowing()
        if shadowing:
            # Loose scripts named like installed packages (IMG-SYSPATH-001).
            payload["shadowing"] = shadowing
        emit_json(payload)
        return EXIT_OK if usable else EXIT_DEP_MISSING

    if action == "init":
        path = write_default_config(workspace)
        emit_json({"config": str(path), "message": "Edit this file to change the defaults."})
        return EXIT_OK

    if action == "unload":
        # Each `imagegen make` is its own process and frees its pipeline when it
        # finishes, so there is no cached pipeline to drop here. What callers
        # want from "unload" is room on the GPU, which is `gpu evict`.
        from backend.cli import gpu as area_gpu  # noqa: PLC0415
        return run_gpu_action(lambda: {**area_gpu.evict(args), "unloaded": True,
                                       "note": "Same as `waterfree gpu evict`."})

    if action == "install":
        return run_gpu_action(lambda: _install(args, workspace))

    if action == "make":
        return _make(args, workspace)

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _make(args: Namespace, workspace: str) -> int:
    config = apply_overrides(
        load_config(workspace),
        preset=args.preset,
        steps=args.steps,
        guidance=args.guidance,
        width=args.width,
        height=args.height,
        negative_prompt=args.negative_prompt,
        seed=args.seed,
        offload=args.offload,
        backend=args.backend, image=args.image, mask=args.mask, strength=args.strength,
        loras=args.lora, comfy_url=args.comfy_url, comfy_checkpoint=args.checkpoint,
        comfy_timeout=args.timeout, comfy_free_after=args.comfy_free_after,
    )
    return run_gpu_action(lambda: generate(
        args.prompt,
        config=config,
        workspace_path=workspace,
        count=args.count,
        output_dir=args.output_dir,
        hf_token=args.hf_token,
        policy=policy_from(args),
        check_vram=not args.skip_vram_check,
    ))


def _install(args: Namespace, workspace: str) -> dict:
    requested = args.presets or [load_config(workspace).preset]
    keys = sorted(PRESETS) if "all" in requested else list(dict.fromkeys(requested))
    unknown = [k for k in keys if k not in PRESETS]
    if unknown:
        raise ValueError(f"Unknown preset(s): {', '.join(unknown)}. Choose from: {', '.join(sorted(PRESETS))}")
    results = {}
    for key in keys:
        preset = PRESETS[key]
        if backend.is_installed(preset.repo_id):
            results[key] = {"repoId": preset.repo_id, "action": "present"}
            continue
        sys.stderr.write(f"imagegen: installing {key} ({preset.repo_id}, ~{preset.disk_gb:.0f} GB)\n")
        path = backend.install(preset.repo_id, hf_token=args.hf_token)
        results[key] = {"repoId": preset.repo_id, "action": "installed", "path": path}
    return {"presets": results}
