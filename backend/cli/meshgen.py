"""`waterfree meshgen ...` -- image to UV-textured GLB on the local GPU (trellis.cpp)."""

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
from backend.cli._gpu import add_gpu_args, policy_from, report_from, run_gpu_action
from backend.engines import install
from backend.engines.catalog import ENGINES, MODEL_SETS
from backend.meshgen import preview
from backend.meshgen.config import (
    BG_REMOVAL_MODES,
    RESOLUTIONS,
    UV_MODES,
    apply_overrides,
    load_config,
    output_dir_for,
    write_default_config,
)
from backend.meshgen.generate import MAX_IMAGES, make

_MODEL_SETS = sorted(k for k, v in MODEL_SETS.items() if v.engine == "trellis")


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("meshgen", help="Image to textured 3D model (GLB) on the local GPU")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_make = actions.add_parser("make", help="Turn image(s) into UV-textured GLB meshes")
    p_make.add_argument("images", nargs="+", metavar="IMAGE",
                        help=f"Input image(s), one object each on a plain background (max {MAX_IMAGES}).")
    p_make.add_argument("--res", dest="resolution", type=int, choices=RESOLUTIONS, default=None,
                        help="Geometry resolution. 1024 = sharper (cascade), 512 = lighter and faster.")
    p_make.add_argument("--faces", type=int, default=None,
                        help="Triangle budget. Not available with trellis.cpp v0.8.1, which has a fixed "
                             "target (~150K at --res 512, ~300K at 1024); any value but 0 is refused.")
    p_make.add_argument("--atlas", type=int, default=None, help="Texture atlas size in px (default: engine's).")
    p_make.add_argument("--no-texture", dest="texture", action="store_false", default=None,
                        help="Geometry only.")
    p_make.add_argument("--bg-removal", choices=BG_REMOVAL_MODES, default=None,
                        help="auto (default): keep an existing alpha, else BiRefNet.")
    p_make.add_argument("--uv", choices=UV_MODES, default=None,
                        help="xatlas (default, tight packing) or box (faster, looser).")
    p_make.add_argument("--steps", type=int, default=None, help="Flow sampler steps (default 12).")
    p_make.add_argument("--seed", type=int, default=None, help="Fix the seed. Omit for random (recorded).")
    p_make.add_argument("--model-set", choices=_MODEL_SETS, default=None)
    p_make.add_argument("--copyright", default=None, help="glTF asset.copyright metadata.")
    p_make.add_argument("--preview", action="store_true",
                        help="Afterwards, render front/three-quarter/side/back PNGs + a 2x2 grid of each mesh.")
    p_make.add_argument("--output-dir", default="",
                        help="Where to write. Default: <workspace>/.waterfree/generated/meshes")
    add_gpu_args(p_make)
    add_workspace_arg(p_make)

    p_preview = actions.add_parser("preview", help="Render GLB(s) to PNG views + a 2x2 grid for checking")
    p_preview.add_argument("glbs", nargs="+", metavar="GLB")
    p_preview.add_argument("--size", type=int, default=512, help="Pixels per view (default 512).")
    p_preview.add_argument("--quiet", action="store_true", help="No progress lines on stderr.")

    p_status = actions.add_parser("status", help="Is trellis.cpp and its model set installed?")
    add_workspace_arg(p_status)

    p_install = actions.add_parser("install", help="Download trellis.cpp and its weights (~10.7 GB)")
    p_install.add_argument("--model-set", choices=_MODEL_SETS, default="trellis2-q8")

    p_init = actions.add_parser("init", help="Write .waterfree/meshgen.json with every setting")
    add_workspace_arg(p_init)

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    action = args.action
    if action == "status":
        workspace = resolve_workspace(args)
        config = load_config(workspace)
        payload = status_payload(config.model_set)
        payload["outputDir"] = str(output_dir_for(config, workspace))
        emit_json(payload)
        return EXIT_OK if payload["usable"] else EXIT_DEP_MISSING
    if action == "install":
        return run_gpu_action(lambda: {
            "engine": install.install_engine(ENGINES["trellis"]),
            "models": install.install_models(MODEL_SETS[args.model_set]),
        })
    if action == "init":
        path = write_default_config(resolve_workspace(args))
        emit_json({"config": str(path), "message": "Edit this file to change the defaults."})
        return EXIT_OK
    if action == "preview":
        return run_gpu_action(lambda: _previews(preview.render(args.glbs, size=args.size,
                                                              report=report_from(args))))
    if action == "make":
        workspace = resolve_workspace(args)
        config = apply_overrides(
            load_config(workspace),
            resolution=args.resolution, faces=args.faces, atlas=args.atlas, texture=args.texture,
            bg_removal=args.bg_removal, uv=args.uv, steps=args.steps, seed=args.seed,
            model_set=args.model_set, copyright=args.copyright,
        )

        def action_make() -> dict:
            result = make(args.images, config=config, workspace=workspace, output_dir=args.output_dir,
                          policy=policy_from(args), check_vram=not args.skip_vram_check, report=report_from(args))
            if args.preview and result["files"]:
                # After the GPU lease is released: Chromium's WebGL needs little, and nothing waits on it.
                result["previews"] = preview.render([f["mesh"] for f in result["files"]],
                                                    report=report_from(args))
            return result

        return run_gpu_action(action_make)
    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _previews(rendered: list[dict]) -> dict:
    done = [r for r in rendered if "grid" in r]
    return {
        "count": len(done),
        "files": done,
        "failed": [r for r in rendered if "error" in r],
        "next": "Check a grid with: waterfree vision look <grid.png> --purpose describe" if done else None,
    }


def status_payload(model_key: str) -> dict:
    engine = install.engine_status(ENGINES["trellis"])
    models = install.model_status(MODEL_SETS[model_key])
    usable = engine["installed"] and models["installed"]
    payload = {"usable": usable, "engine": engine, "models": models}
    if not usable:
        payload["fix"] = "waterfree meshgen install"
    return payload
