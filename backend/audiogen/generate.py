"""Voice design, speech and sound effects with openmoss, under the exclusive GPU lease.

Each command takes the GPU once, starts one ``moss-tts-server`` with the model
for its role, runs the whole batch through it, then stops it -- so the model
load is paid once per batch, not once per line.

* design -- MOSS-VoiceGenerator turns a description ("gruff, warm tavern
  keeper") into speech. Each candidate uses a different seed, so ``-n 3``
  gives three different people; pick one and `save-voice` it.
* speak  -- MOSS-TTS-Local clones a saved voice for every line, which is what
  keeps a character consistent across hundreds of lines.
* sfx    -- MOSS-SoundEffect, text to sound effect.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.audiogen import qa as audio_qa
from backend.audiogen import voices as voice_lib
from backend.audiogen import wav
from backend.audiogen.config import AudiogenConfig, output_dir_for, voices_dir_for
from backend.engines import install
from backend.engines.catalog import MODEL_SETS, ModelSet
from backend.engines.outputs import (
    engine_provenance,
    resolve_seed,
    stem,
    write_sidecar,
)
from backend.gpu import lease as gpu_lease
from backend.gpu.engine import EngineError, EngineServer
from backend.gpu.preflight import exclusive_gpu

MAX_ITEMS = 200
MAX_CANDIDATES = 8
INSTALL_HINTS = {
    "voice": "waterfree audiogen install voice",
    "speech": "waterfree audiogen install speech",
    "sfx": "waterfree audiogen install sfx",
}
_TTS_TIMEOUT = 900.0
_SFX_TIMEOUT = 1800.0

Report = Callable[[str], None]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def backbone(model_set: ModelSet) -> Path:
    """The GGUF passed to ``--model``; openmoss finds the ``.extras.gguf`` sidecar beside it."""
    for remote in model_set.files:
        if not remote.name.endswith(".extras.gguf"):
            return model_set.path(remote.name)
    raise ValueError(f"{model_set.key} has no backbone GGUF")


@contextmanager
def _session(role: str, config: AudiogenConfig, *, label: str, detail: str,
             policy: gpu_lease.WaitPolicy | None, check_vram: bool, report: Report,
             voice_dir: Path | None = None,
             qa_items: list | None = None) -> Iterator[tuple[EngineServer, object, ModelSet, object]]:
    wanted = MODEL_SETS[config.model_for(role)]
    # Each model set names its engine build: SFX runs on openmoss's Vulkan build
    # (its DAC-VAE decode is ~66x slower on the CUDA one), speech/voice on CUDA.
    release, model_set = install.require(wanted.engine, wanted.key, install_hint=INSTALL_HINTS[role])
    argv = [str(release.executable("server")), "--model", str(backbone(model_set)),
            "--no-webui", "--main-gpu", "0"]
    if role == "voice":
        argv += ["--template", "voicegen"]
    if role in ("voice", "speech") and config.n_ctx:
        argv += ["--n-ctx", str(config.n_ctx)]
    if voice_dir is not None:
        # The server's registry reads <id>.wav + <id>.json -- the same layout as
        # the workspace voice library -- and caches each voice's encoded codes, so
        # a 100-line batch encodes every reference once instead of 100 times.
        argv += ["--voice-dir", str(voice_dir)]
    with exclusive_gpu(label=label, detail=f"{model_set.key}, {detail}", vram_gb=model_set.vram_gb,
                       policy=policy, check_vram=check_vram) as grant:
        with EngineServer(name=f"openmoss-{role}", argv=argv, cwd=release.install_dir(),
                          env={**os.environ, **release.env}, report=report) as server:
            report(f"{model_set.label} ready in {server.load_seconds:.0f}s")
            yield server, release, model_set, grant
        if qa_items:
            # openmoss has stopped and given the card back; the lease is still ours (GPU-007).
            audio_qa.run(qa_items, report=report)


# ---------------------------------------------------------------------------
# design
# ---------------------------------------------------------------------------

def design(description: str, text: str, *, count: int, seed: int, config: AudiogenConfig,
           workspace: str, output_dir: str = "", policy: gpu_lease.WaitPolicy | None = None,
           check_vram: bool = True, qa: bool = False, report: Report = _stderr) -> dict:
    description, text = description.strip(), text.strip()
    if not description:
        raise ValueError("Describe the voice, e.g. \"gruff, warm middle-aged tavern keeper, slight rasp\".")
    if not text:
        raise ValueError("Give the sample line the candidates should speak (--text).")
    if not 1 <= count <= MAX_CANDIDATES:
        raise ValueError(f"count must be between 1 and {MAX_CANDIDATES}")
    config.validate()
    base_seed = resolve_seed(seed)
    destination = _destination(output_dir, config, workspace, "voice-design")
    qa_items = _qa_items(qa)
    files, failures = [], []
    started = time.monotonic()
    with _session("voice", config, label="audiogen design", detail=f"{count} candidate(s)",
                  policy=policy, check_vram=check_vram, report=report,
                  qa_items=qa_items) as (server, release, model_set, grant):
        for index in range(count):
            candidate_seed = base_seed + index
            payload = {"text": text, "instruction": description, "response_format": "wav",
                       "sampling": {"seed": candidate_seed}}
            _common_tts_fields(payload, config)
            report(f"[{index + 1}/{count}] designing voice (seed {candidate_seed})")
            try:
                audio, headers = server.post_json("/tts", payload, timeout=_TTS_TIMEOUT)
                files.append(_write(audio, headers, destination, f"{description} {index + 1}", config, {
                    "kind": "voice-design",
                    "description": description,
                    "transcript": text,
                    "seed": candidate_seed,
                    "request": payload,
                    **engine_provenance(release, model_set),
                }))
                _queue_qa(qa_items, files[-1], "speech", text=text, config=config, group="design")
            except EngineError as exc:
                failures.append({"seed": candidate_seed, "error": str(exc)})
                if not server.running():
                    break
        load, spill = server.load_seconds, server.spill_report()
    return _result(files, failures, destination, grant, load, started, model_set, spill=spill,
                   qa=qa_items is not None,
                   next_step="Listen, then keep one: waterfree audiogen save-voice <name> --from <wav>")


# ---------------------------------------------------------------------------
# speak
# ---------------------------------------------------------------------------

def speak(lines: list[dict], *, voice: str | None, seed: int, instruction: str = "",
          config: AudiogenConfig, workspace: str, output_dir: str = "",
          policy: gpu_lease.WaitPolicy | None = None, check_vram: bool = True,
          qa: bool = False, report: Report = _stderr) -> dict:
    if not lines:
        raise ValueError("Nothing to say: pass TEXT or --lines FILE.")
    if len(lines) > MAX_ITEMS:
        raise ValueError(f"At most {MAX_ITEMS} lines per command; split the file.")
    config.validate()
    library = voices_dir_for(config, workspace)
    # Resolve every voice before taking the GPU: a typo should fail in milliseconds.
    references: dict[str, voice_lib.Voice] = {}
    for line in lines:
        name = line.get("voice") or voice
        if name and name not in references:
            references[name] = voice_lib.load(library, name)
    base_seed = resolve_seed(seed)
    destination = _destination(output_dir, config, workspace, "speech")
    qa_items = _qa_items(qa)
    files, failures = [], []
    started = time.monotonic()
    with _session("speech", config, label="audiogen speak", detail=f"{len(lines)} line(s)",
                  policy=policy, check_vram=check_vram, report=report,
                  voice_dir=library if references else None,
                  qa_items=qa_items) as (server, release, model_set, grant):
        for index, line in enumerate(lines):
            name = line.get("voice") or voice
            line_seed = int(line["seed"]) if line.get("seed") is not None else base_seed + index
            payload = {"text": line["text"], "response_format": "wav", "sampling": {"seed": line_seed}}
            if name:
                payload["voice"] = references[name].name
            if line.get("instruction") or instruction:
                payload["instruction"] = line.get("instruction") or instruction
            _common_tts_fields(payload, config)
            label = line.get("name") or f"{name or 'speech'} {index + 1:03d} {line['text']}"
            report(f"[{index + 1}/{len(lines)}] {name or 'default voice'}: {line['text'][:60]}")
            try:
                audio, headers = server.post_json("/tts", payload, timeout=_TTS_TIMEOUT)
                files.append(_write(audio, headers, destination, label, config, {
                    "kind": "speech",
                    "text": line["text"],
                    "voice": {**references[name].public(), "sha256": references[name].record.get("sha256")}
                    if name else None,
                    "seed": line_seed,
                    "request": payload,
                    **engine_provenance(release, model_set),
                }, exact_name=line.get("name")))
                _queue_qa(qa_items, files[-1], "speech", text=line["text"], config=config)
            except EngineError as exc:
                failures.append({"line": index + 1, "text": line["text"], "error": str(exc)})
                if not server.running():
                    break
        load, spill = server.load_seconds, server.spill_report()
    return _result(files, failures, destination, grant, load, started, model_set, spill=spill,
                   qa=qa_items is not None)


# ---------------------------------------------------------------------------
# sfx
# ---------------------------------------------------------------------------

def sfx(prompts: list[str], *, count: int, seed: int, config: AudiogenConfig, workspace: str,
        output_dir: str = "", policy: gpu_lease.WaitPolicy | None = None, check_vram: bool = True,
        qa: bool = False, report: Report = _stderr) -> dict:
    prompts = [p.strip() for p in prompts if p and p.strip()]
    if not prompts:
        raise ValueError("Describe the sound, e.g. \"heavy wooden door creaks open, stone hall reverb\".")
    if not 1 <= count <= MAX_CANDIDATES:
        raise ValueError(f"count must be between 1 and {MAX_CANDIDATES}")
    if len(prompts) * count > MAX_ITEMS:
        raise ValueError(f"At most {MAX_ITEMS} clips per command.")
    config.validate()
    base_seed = resolve_seed(seed)
    destination = _destination(output_dir, config, workspace, "sfx")
    qa_items = _qa_items(qa)
    files, failures = [], []
    started = time.monotonic()
    total = len(prompts) * count
    with _session("sfx", config, label="audiogen sfx", detail=f"{total} clip(s)",
                  policy=policy, check_vram=check_vram, report=report,
                  qa_items=qa_items) as (server, release, model_set, grant):
        done = 0
        for prompt in prompts:
            for variant in range(count):
                done += 1
                clip_seed = base_seed + done - 1
                payload = {"prompt": prompt, "seconds": float(config.sfx_seconds),
                           "steps": int(config.sfx_steps), "cfg_scale": float(config.sfx_cfg),
                           "seed": clip_seed, "response_format": "wav"}
                if config.sfx_negative_prompt.strip():
                    payload["negative_prompt"] = config.sfx_negative_prompt.strip()
                report(f"[{done}/{total}] {prompt[:60]} (seed {clip_seed})")
                clip_started = time.monotonic()
                label = prompt if count == 1 else f"{prompt} {variant + 1}"
                try:
                    audio, headers = server.post_json("/sfx", payload, timeout=_SFX_TIMEOUT)
                    files.append(_write(audio, headers, destination, label, config, {
                        "kind": "sfx",
                        "prompt": prompt,
                        "promptSent": headers.get("x-moss-prompt"),
                        "seed": clip_seed,
                        "request": payload,
                        "seconds": round(time.monotonic() - clip_started, 1),
                        **engine_provenance(release, model_set),
                    }))
                    _queue_qa(qa_items, files[-1], "sfx", prompt=prompt, config=config, group=prompt)
                except EngineError as exc:
                    failures.append({"prompt": prompt, "seed": clip_seed, "error": str(exc)})
                    if not server.running():
                        break
            if not server.running():
                break
        load, spill = server.load_seconds, server.spill_report()
    return _result(files, failures, destination, grant, load, started, model_set, spill=spill,
                   qa=qa_items is not None)


# ---------------------------------------------------------------------------
# Batch files
# ---------------------------------------------------------------------------

def parse_lines_file(path: str) -> list[dict]:
    """Lines to speak from a .txt (one line each) or .json file.

    JSON: a list of strings, or of objects ``{"text", "voice"?, "name"?, "seed"?,
    "instruction"?}``; ``name`` becomes the output file stem, so a dialogue file
    can produce ``blacksmith_greeting.wav`` directly.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"Lines file does not exist: {path}")
    text = source.read_text(encoding="utf-8-sig")
    if source.suffix.lower() == ".json":
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"{path} must hold a JSON list of lines")
        lines = []
        for index, item in enumerate(raw, start=1):
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                raise ValueError(f"{path}: entry {index} needs a non-empty \"text\"")
            line = {"text": str(item["text"]).strip()}
            for key in ("voice", "name", "instruction"):
                if item.get(key):
                    line[key] = str(item[key]).strip()
            if item.get("seed") is not None:
                line["seed"] = int(item["seed"])
            if line.get("name"):
                line["name"] = _file_stem(line["name"])
                if not line["name"]:
                    raise ValueError(f"{path}: entry {index} has a \"name\" with no usable characters")
            lines.append(line)
        names = [line["name"] for line in lines if line.get("name")]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"{path}: these names are used more than once and would overwrite each other: "
                             f"{', '.join(duplicates)}")
        return lines
    return [{"text": line.strip()} for line in text.splitlines() if line.strip()]


def _file_stem(name: str) -> str:
    """A caller-chosen output name, kept as written apart from characters a file name cannot hold.

    Unlike the slug used for generated names, underscores and case survive:
    a dialogue file asking for ``Smith_Greeting`` gets ``Smith_Greeting.wav``.
    """
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip(".-")[:80]


def parse_prompts_file(path: str) -> list[str]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"Prompts file does not exist: {path}")
    text = source.read_text(encoding="utf-8-sig")
    if source.suffix.lower() == ".json":
        raw = json.loads(text)
        if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
            raise ValueError(f"{path} must hold a JSON list of prompt strings")
        return raw
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _qa_items(qa: bool) -> list | None:
    """A list to collect takes for QA, or None. QA must be installed: fail before the GPU is taken."""
    if not qa:
        return None
    audio_qa.require()
    return []


def _queue_qa(items: list | None, entry: dict, kind: str, *, config: AudiogenConfig, text: str = "",
              prompt: str = "", group: str | None = None) -> None:
    if items is not None:
        items.append({"entry": entry, "kind": kind, "text": text, "prompt": prompt, "group": group,
                      "language": config.language.strip() or None})


def _common_tts_fields(payload: dict, config: AudiogenConfig) -> None:
    if config.language.strip():
        payload["language"] = config.language.strip()
    if config.effective_max_new_tokens():
        payload["max_new_tokens"] = config.effective_max_new_tokens()


def _destination(output_dir: str, config: AudiogenConfig, workspace: str, kind: str) -> Path:
    destination = Path(output_dir).expanduser() if output_dir else output_dir_for(config, workspace, kind)
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _write(audio: bytes, headers: dict, destination: Path, label: str, config: AudiogenConfig,
           metadata: dict, *, exact_name: str | None = None) -> dict:
    if audio[:4] != b"RIFF":
        raise EngineError(f"openmoss returned {len(audio)} bytes that are not a WAV")
    try:
        native = wav.info(audio)
        audio = wav.convert_channels(audio, config.channels)
        written = wav.info(audio)
    except (ValueError, EOFError, wave.Error) as exc:
        raise EngineError(f"openmoss returned an unusable WAV: {exc}") from exc
    path = destination / f"{exact_name}.wav" if exact_name else destination / f"{stem(label)}.wav"
    path.write_bytes(audio)
    metadata = {
        **metadata,
        "audio": {"seconds": round(written["seconds"], 2), "sampleRate": written["sampleRate"],
                  "channels": written["channels"], "nativeChannels": native["channels"]},
        "engineTiming": {k: v for k, v in headers.items() if k.startswith("x-moss-")},
    }
    sidecar = write_sidecar(path, metadata)
    return {"audio": str(path), "metadata": str(sidecar), "seconds": round(written["seconds"], 2),
            "seed": metadata.get("seed")}


def _result(files: list[dict], failures: list[dict], destination: Path, grant, load_seconds,
            started: float, model_set: ModelSet, *, spill: dict | None = None, qa: bool = False,
            next_step: str = "") -> dict:
    result = {
        "count": len(files),
        "outputDir": str(destination),
        "files": files,
        "failed": failures,
        "model": model_set.label,
        "gpu": grant.public(),
        "vramSpill": spill,
        "timing": {"loadSeconds": round(load_seconds or 0, 1),
                   "totalSeconds": round(time.monotonic() - started, 1)},
        "licence": model_set.licence,
    }
    if qa:
        result["qa"] = audio_qa.summary(files)
    if next_step and files:
        result["next"] = next_step
    return result
