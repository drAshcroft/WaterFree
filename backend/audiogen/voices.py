"""The workspace's voice library: named reference clips that speech clones.

A voice is a short WAV of clean speech plus a JSON record of where it came from
(the description it was designed from, the words spoken in it, the seed). It
lives in the *project* -- ``.waterfree/voices/<name>.wav`` -- because a
character's voice is part of the game, like its sprite, and has to survive a
machine change. Speaking every line of a character from the same clip is what
keeps the character sounding like one person.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from backend.audiogen import wav
from backend.engines.outputs import file_sha256

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MIN_SECONDS = 2.0
MAX_SECONDS = 60.0   # the server's payload cap fits ~60 s of reference audio


class VoiceError(ValueError):
    pass


@dataclass(frozen=True)
class Voice:
    name: str
    wav_path: Path
    record: dict

    def public(self) -> dict:
        return {"name": self.name, "wav": str(self.wav_path), **{
            k: self.record.get(k) for k in ("description", "transcript", "seconds", "sampleRate",
                                            "channels", "createdAt", "source")}}


def check_name(name: str) -> str:
    cleaned = name.strip().lower()
    if not _NAME.match(cleaned):
        raise VoiceError("Voice names are 1-64 characters: lowercase letters, digits, '-' and '_', "
                         "starting with a letter or digit (e.g. 'blacksmith', 'old-sailor').")
    return cleaned


def save(directory: Path, name: str, source_wav: Path, *, description: str = "", transcript: str = "",
         source: dict | None = None, overwrite: bool = False) -> Voice:
    name = check_name(name)
    source_wav = Path(source_wav).expanduser().resolve()
    if not source_wav.is_file():
        raise VoiceError(f"No such WAV file: {source_wav}")
    info = wav.info(source_wav)
    if not MIN_SECONDS <= info["seconds"] <= MAX_SECONDS:
        raise VoiceError(f"A reference voice should be {MIN_SECONDS:g}-{MAX_SECONDS:g} s of clean speech; "
                         f"{source_wav.name} is {info['seconds']:.1f} s.")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.wav"
    if target.exists() and not overwrite:
        raise VoiceError(f"Voice '{name}' already exists. Pass --force to replace it.")
    shutil.copyfile(source_wav, target)
    record = {
        "name": name,
        "description": description,
        "transcript": transcript,
        "seconds": round(info["seconds"], 2),
        "sampleRate": info["sampleRate"],
        "channels": info["channels"],
        "sha256": file_sha256(target),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source or {"path": str(source_wav)},
    }
    (directory / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return Voice(name=name, wav_path=target, record=record)


def load(directory: Path, name: str) -> Voice:
    name = check_name(name)
    target = directory / f"{name}.wav"
    if not target.is_file():
        known = ", ".join(v.name for v in list_voices(directory)) or "none yet"
        raise VoiceError(f"No voice named '{name}' in {directory}. Known voices: {known}. "
                         "Design one with `waterfree audiogen design`, then `audiogen save-voice`.")
    try:
        record = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {"name": name}
    return Voice(name=name, wav_path=target, record=record)


def list_voices(directory: Path) -> list[Voice]:
    if not directory.is_dir():
        return []
    voices = []
    for path in sorted(directory.glob("*.wav")):
        if _NAME.match(path.stem):
            try:
                voices.append(load(directory, path.stem))
            except VoiceError:
                continue
    return voices


def remove(directory: Path, name: str) -> Voice:
    voice = load(directory, name)
    voice.wav_path.unlink(missing_ok=True)
    (directory / f"{voice.name}.json").unlink(missing_ok=True)
    return voice
