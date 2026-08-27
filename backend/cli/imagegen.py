"""`waterfree imagegen ...` — local GPU image generation."""

from __future__ import annotations

from argparse import Namespace, _SubParsersAction

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
    add_workspace_arg(p_make)

    p_models = actions.add_parser("models", help="List presets and whether they fit this GPU")
    add_workspace_arg(p_models)

    p_status = actions.add_parser("status", help="Check that the backend is usable")
    add_workspace_arg(p_status)

    p_init = actions.add_parser("init", help="Write .waterfree/imagegen.json with every setting")
    add_workspace_arg(p_init)

    p_unload = actions.add_parser("unload", help="Release the cached pipeline and free VRAM")
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
        usable, reason = backend.is_available()
        config = load_config(workspace)
        emit_json({
            "usable": usable,
            "reason": reason,
            "gpuVramGb": round(available_vram_gb(), 1),
            "preset": config.preset,
            "outputDir": str(output_dir_for(config, workspace)),
        })
        return EXIT_OK if usable else EXIT_DEP_MISSING

    if action == "init":
        path = write_default_config(workspace)
        emit_json({"config": str(path), "message": "Edit this file to change the defaults."})
        return EXIT_OK

    if action == "unload":
        backend.unload()
        emit_json({"unloaded": True})
        return EXIT_OK

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
    )
    try:
        result = generate(
            args.prompt,
            config=config,
            workspace_path=workspace,
            count=args.count,
            output_dir=args.output_dir,
            hf_token=args.hf_token,
        )
    except backend.BackendUnavailable as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except backend.GenerationFailed as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)

    emit_json(result)
    return EXIT_OK
