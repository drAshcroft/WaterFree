"""`waterfree animgen ...` -- text to humanoid motion clips (BVH/FBX) with NVIDIA Kimodo."""

from __future__ import annotations

from argparse import Namespace, _SubParsersAction

from backend.animgen import engine as kimodo
from backend.animgen import fbx as fbx_lib
from backend.animgen.config import (
    FBX_MODES,
    MAX_VARIANTS,
    TPOSES,
    apply_overrides,
    load_config,
    output_dir_for,
    write_default_config,
)
from backend.animgen.generate import make, parse_clips_file
from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
    resolve_workspace,
)
from backend.cli._gpu import add_gpu_args, policy_from, report_from, run_gpu_action
from backend.gpu import ram


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("animgen", help="Text to humanoid motion clips (BVH + FBX) with Kimodo")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_make = actions.add_parser("make", help="Generate motion clip(s) from text prompts")
    p_make.add_argument("prompts", nargs="*", metavar="PROMPT",
                        help="One clip per prompt, e.g. \"a person jogs forward\". Use --prompts for a batch file.")
    p_make.add_argument("--prompts", dest="prompts_file", default=None,
                        help="A .txt (one prompt per line) or .json list of prompts / "
                             "{name, text|segments, seconds, seed, inPlace} objects.")
    p_make.add_argument("-n", "--count", type=int, default=1,
                        help=f"Variants per prompt (1-{MAX_VARIANTS}), consecutive seeds.")
    p_make.add_argument("--seconds", type=float, default=None, help="Clip length, 0.5-10 (default 4).")
    p_make.add_argument("--steps", type=int, default=None, help="Diffusion steps (default 100).")
    p_make.add_argument("--seed", type=int, default=None, help="First seed; clip k uses seed+k. Omit for random.")
    p_make.add_argument("--in-place", dest="in_place", action="store_true", default=None,
                        help="Remove the travel across the floor (for engine-driven locomotion).")
    p_make.add_argument("--loop", action="store_true", default=None,
                        help="Make a seamless cycle of about --seconds (max 6): cut at the best-matching frames "
                             "of a longer generation and blend the seam.")
    p_make.add_argument("--path", nargs="+", metavar="X,Z", default=None,
                        help="Waypoints in metres for the PROMPT clips to walk through, e.g. --path 0,3 2,5. "
                             "The clip starts at 0,0 facing +z; +x is the character's left.")
    p_make.add_argument("--constraints", default=None, metavar="FILE",
                        help="A Kimodo constraints JSON (as its demo exports) for the PROMPT clips.")
    p_make.add_argument("--no-preview", dest="preview", action="store_false", default=None,
                        help="Skip the stick-figure contact sheet PNG.")
    p_make.add_argument("--no-postprocess", dest="postprocess", action="store_false", default=None,
                        help="Skip Kimodo's foot-skate clean-up.")
    p_make.add_argument("--tpose", choices=TPOSES, default=None,
                        help="BVH rest pose: standard T-pose (default; what retargeting expects) or the "
                             "BONES-SEED dataset's own.")
    p_make.add_argument("--fbx", choices=FBX_MODES, default=None,
                        help="FBX via Blender: auto (default, when Blender is found), always, never.")
    p_make.add_argument("--skip-ram-check", action="store_true",
                        help="Load the CPU text encoder even when free RAM looks too low. Windows will page "
                             "other programs out and the machine will stutter.")
    p_make.add_argument("--output-dir", default="",
                        help="Where to write. Default: <workspace>/.waterfree/generated/animations")
    add_gpu_args(p_make)
    add_workspace_arg(p_make)

    p_status = actions.add_parser("status", help="Is Kimodo installed; is Blender available for FBX?")
    add_workspace_arg(p_status)

    actions.add_parser("install", help="Build the Kimodo venv and fetch its weights (~5 GB + ~16.5 GB)")

    p_init = actions.add_parser("init", help="Write .waterfree/animgen.json with every setting")
    add_workspace_arg(p_init)

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    action = args.action
    if action == "install":
        return run_gpu_action(kimodo.install)
    workspace = resolve_workspace(args)
    if action == "status":
        config = load_config(workspace)
        payload = status_payload(config)
        payload["outputDir"] = str(output_dir_for(config, workspace))
        emit_json(payload)
        return EXIT_OK if payload["usable"] else EXIT_DEP_MISSING
    if action == "init":
        path = write_default_config(workspace)
        emit_json({"config": str(path), "message": "Edit this file to change the defaults."})
        return EXIT_OK
    if action == "make":
        if not args.prompts and not args.prompts_file:
            return emit_error("Give PROMPT(s) or --prompts FILE.", exit_code=EXIT_USAGE)
        config = apply_overrides(load_config(workspace), seconds=args.seconds, steps=args.steps, seed=args.seed,
                                 in_place=args.in_place, loop=args.loop, preview=args.preview,
                                 postprocess=args.postprocess, tpose=args.tpose, fbx=args.fbx)

        def action_make() -> dict:
            extra: dict = {}
            if args.path:
                extra["path"] = [_point(text) for text in args.path]
            if args.constraints:
                extra["constraints"] = args.constraints
            clips: list = [{"text": prompt, **extra} for prompt in args.prompts]
            if args.prompts_file:
                clips += parse_clips_file(args.prompts_file)
            return make(clips, count=args.count, config=config, workspace=workspace,
                        output_dir=args.output_dir, policy=policy_from(args),
                        check_vram=not args.skip_vram_check, check_ram=not args.skip_ram_check,
                        report=report_from(args))

        return run_gpu_action(action_make)
    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _point(text: str) -> list[float]:
    try:
        x, z = (float(part) for part in text.split(","))
    except ValueError:
        raise ValueError(f"--path point {text!r} is not X,Z (e.g. 0,3)") from None
    return [x, z]


def status_payload(config) -> dict:
    engine = kimodo.status()
    blender = fbx_lib.find_blender(config.blender_path)
    memory = ram.query()
    cache = kimodo.embedding_cache_dir()
    payload = {
        "usable": engine["installed"],
        "engine": engine,
        "fbx": {"mode": config.fbx, "blender": str(blender) if blender else None},
        "textEncoder": {
            "device": "cpu",
            "ramNeededGb": config.ram_gb,
            "ram": memory.public() if memory else None,
            "cachedPrompts": sum(1 for _ in cache.glob("*/*.npy")) if cache.is_dir() else 0,
            "cacheDir": str(cache),
        },
    }
    if not engine["installed"]:
        payload["fix"] = kimodo.INSTALL_HINT
    return payload
