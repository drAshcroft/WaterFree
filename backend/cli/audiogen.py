"""`waterfree audiogen ...` -- NPC voices, spoken lines and sound effects (openmoss)."""

from __future__ import annotations

import json
import sys
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from backend.audiogen import qa as audio_qa
from backend.audiogen import voices as voice_lib
from backend.audiogen.config import (
    CHANNEL_MODES,
    ROLES,
    apply_overrides,
    load_config,
    output_dir_for,
    voices_dir_for,
    write_default_config,
)
from backend.audiogen.generate import (
    MAX_CANDIDATES,
    design,
    parse_lines_file,
    parse_prompts_file,
    sfx,
    speak,
)
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


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("audiogen", help="NPC voices, spoken lines and sound effects on the local GPU")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_design = actions.add_parser("design", help="Invent a voice from a description; writes candidates")
    p_design.add_argument("description", help="Who is speaking, e.g. \"gruff, warm tavern keeper, slight rasp\".")
    p_design.add_argument("--text", required=True,
                          help="The sample line each candidate speaks. 6-15 s of speech makes a good reference.")
    p_design.add_argument("-n", "--count", type=int, default=3, help=f"Candidates (1-{MAX_CANDIDATES}). Default 3.")
    p_design.add_argument("--seed", type=int, default=None, help="First seed; candidate k uses seed+k.")
    _add_common(p_design)

    p_save = actions.add_parser("save-voice", help="Keep a WAV as a named voice in this workspace")
    p_save.add_argument("name", help="Voice name, e.g. blacksmith")
    p_save.add_argument("--from", dest="source", required=True,
                        help="A WAV of clean speech (a design candidate, or a recording).")
    p_save.add_argument("--description", default=None, help="Defaults to the candidate's description.")
    p_save.add_argument("--transcript", default=None, help="Words spoken in the WAV. Defaults to the candidate's text.")
    p_save.add_argument("--force", action="store_true", help="Replace an existing voice of that name.")
    add_workspace_arg(p_save)

    p_voices = actions.add_parser("voices", help="List the workspace's saved voices")
    add_workspace_arg(p_voices)

    p_remove = actions.add_parser("remove-voice", help="Delete a saved voice")
    p_remove.add_argument("name")
    add_workspace_arg(p_remove)

    p_speak = actions.add_parser("speak", help="Speak line(s), cloning a saved voice")
    p_speak.add_argument("text", nargs="?", default=None, help="One line. Use --lines for a batch.")
    p_speak.add_argument("--voice", default=None, help="Saved voice to clone. Omit for the model's default voice.")
    p_speak.add_argument("--lines", default=None,
                         help="A .txt (one line each) or .json file: [\"text\", ...] or "
                              "[{\"text\", \"voice\", \"name\", \"seed\"}, ...].")
    p_speak.add_argument("--instruction", default=None, help="Style hint, e.g. \"whispering, urgent\".")
    p_speak.add_argument("--seed", type=int, default=None, help="First seed; line k uses seed+k.")
    _add_common(p_speak)

    p_sfx = actions.add_parser("sfx", help="Generate sound effect(s) from a description")
    p_sfx.add_argument("prompt", nargs="?", default=None, help="Describe the sound. Use --prompts for a batch.")
    p_sfx.add_argument("--prompts", default=None, help="A .txt (one prompt per line) or .json list of prompts.")
    p_sfx.add_argument("-n", "--count", type=int, default=1, help=f"Variants per prompt (1-{MAX_CANDIDATES}).")
    p_sfx.add_argument("--seconds", type=float, default=None, help="Clip length (default 4, max 30).")
    p_sfx.add_argument("--steps", type=int, default=None, help="Solver steps (default 100).")
    p_sfx.add_argument("--cfg", type=float, default=None,
                       help="Guidance (default 4.0). 1.0 halves the time at some cost in adherence.")
    p_sfx.add_argument("--negative-prompt", default=None)
    p_sfx.add_argument("--seed", type=int, default=None, help="First seed; clip k uses seed+k.")
    _add_common(p_sfx)

    p_status = actions.add_parser("status", help="Which audio models are installed")
    add_workspace_arg(p_status)

    p_install = actions.add_parser("install", help="Download openmoss and audio model weights")
    p_install.add_argument("roles", nargs="*", choices=[*ROLES, "qa", "all"], default=["all"],
                           help="voice (7.8 GB), speech (9.1 GB), sfx (7.1 GB), all three (default), or qa "
                                "(the opt-in Whisper + CLAP checker for --qa: ~4.5 GB venv + 3.5 GB weights).")

    p_init = actions.add_parser("init", help="Write .waterfree/audiogen.json with every setting")
    add_workspace_arg(p_init)

    p.set_defaults(_runner=run)


def _add_common(parser) -> None:
    parser.add_argument("--channels", choices=CHANNEL_MODES, default=None,
                        help="native (default) keeps the model's layout; mono suits positional game audio.")
    parser.add_argument("--language", default=None, help="Language hint, e.g. en.")
    parser.add_argument("--output-dir", default="",
                        help="Where to write. Default: <workspace>/.waterfree/generated/audio/<kind>")
    parser.add_argument("--qa", action="store_true",
                        help="Afterwards, check every take: Whisper word error rate for speech (and rank voice "
                             "candidates), CLAP prompt match for SFX (and rank variants). Needs "
                             "`waterfree audiogen install qa`.")
    add_gpu_args(parser)
    add_workspace_arg(parser)


def run(args: Namespace) -> int:
    action = args.action
    if action == "install":
        return run_gpu_action(lambda: _install(args.roles))
    if action == "status":
        workspace = resolve_workspace(args)
        config = load_config(workspace)
        payload = status_payload(config)
        payload["voicesDir"] = str(voices_dir_for(config, workspace))
        payload["outputDir"] = str(output_dir_for(config, workspace, ""))
        emit_json(payload)
        return EXIT_OK if payload["usable"] else EXIT_DEP_MISSING
    workspace = resolve_workspace(args)
    if action == "init":
        path = write_default_config(workspace)
        emit_json({"config": str(path), "message": "Edit this file to change the defaults."})
        return EXIT_OK
    config = load_config(workspace)
    library = voices_dir_for(config, workspace)
    if action == "voices":
        emit_json({"voicesDir": str(library), "voices": [v.public() for v in voice_lib.list_voices(library)]})
        return EXIT_OK
    if action == "remove-voice":
        return run_gpu_action(lambda: {"removed": voice_lib.remove(library, args.name).public()})
    if action == "save-voice":
        return run_gpu_action(lambda: _save_voice(args, library))

    config = apply_overrides(config, channels=args.channels, language=args.language)
    common = {"config": config, "workspace": workspace, "output_dir": args.output_dir, "qa": args.qa,
              "policy": policy_from(args), "check_vram": not args.skip_vram_check,
              "report": report_from(args)}
    seed = args.seed if args.seed is not None else -1
    if action == "design":
        return run_gpu_action(lambda: design(args.description, args.text, count=args.count, seed=seed, **common))
    if action == "speak":
        if bool(args.text) == bool(args.lines):
            return emit_error("Give either TEXT or --lines FILE.", exit_code=EXIT_USAGE)
        return run_gpu_action(lambda: speak(
            parse_lines_file(args.lines) if args.lines else [{"text": args.text}],
            voice=args.voice, seed=seed, instruction=args.instruction or "", **common))
    if action == "sfx":
        if bool(args.prompt) == bool(args.prompts):
            return emit_error("Give either PROMPT or --prompts FILE.", exit_code=EXIT_USAGE)
        common["config"] = apply_overrides(config, sfx_seconds=args.seconds, sfx_steps=args.steps,
                                           sfx_cfg=args.cfg, sfx_negative_prompt=args.negative_prompt)
        return run_gpu_action(lambda: sfx(
            parse_prompts_file(args.prompts) if args.prompts else [args.prompt],
            count=args.count, seed=seed, **common))
    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _install(roles: list[str]) -> dict:
    # "all" means the three generator roles; QA is opt-in and installed only when named.
    wanted = list(ROLES) if "all" in roles else [r for r in dict.fromkeys(roles) if r in ROLES]
    defaults = load_config("")
    sets = {role: MODEL_SETS[defaults.model_for(role)] for role in wanted}
    out: dict = {"engines": {}, "models": {}}
    # Each role's model set names its engine build (SFX uses the Vulkan one).
    for engine_key in dict.fromkeys(model_set.engine for model_set in sets.values()):
        out["engines"][engine_key] = install.install_engine(ENGINES[engine_key])
    for role, model_set in sets.items():
        out["models"][role] = install.install_models(model_set)
    if "qa" in roles:
        out["qa"] = audio_qa.install(report=lambda message: print(message, file=sys.stderr, flush=True))
    return out


def _save_voice(args: Namespace, library: Path) -> dict:
    source = Path(args.source).expanduser()
    candidate = {}
    try:
        candidate = json.loads(source.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    description = args.description if args.description is not None else candidate.get("description", "")
    transcript = args.transcript if args.transcript is not None else \
        candidate.get("transcript") or candidate.get("text", "")
    origin = {"path": str(source.resolve())}
    for key in ("seed", "model", "engine"):
        if key in candidate:
            origin[key] = candidate[key]
    voice = voice_lib.save(library, args.name, source, description=description, transcript=transcript,
                           source=origin, overwrite=args.force)
    return {"saved": voice.public(), "use": f"waterfree audiogen speak --voice {voice.name} \"...\""}


def status_payload(config) -> dict:
    roles = {}
    for role in ROLES:
        model_set = MODEL_SETS[config.model_for(role)]
        engine = install.engine_status(ENGINES[model_set.engine])
        models = install.model_status(model_set)
        roles[role] = {
            "ready": engine["installed"] and models["installed"],
            "engine": {k: engine[k] for k in ("key", "version", "installed", "path")},
            "models": models,
        }
    payload = {"usable": any(r["ready"] for r in roles.values()), "roles": roles}
    missing = [role for role, r in roles.items() if not r["ready"]]
    if missing:
        payload["fix"] = f"waterfree audiogen install {' '.join(missing) if len(missing) < len(ROLES) else 'all'}"
    checker = audio_qa.status()
    payload["qa"] = {"installed": checker["installed"], "optional": True,
                     **({} if checker["installed"] else {"fix": audio_qa.INSTALL_HINT})}
    return payload
