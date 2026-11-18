"""Settings for voice design, speech and sound effects with openmoss.

Three layers, later winning: the defaults below, ``.waterfree/audiogen.json``
(`waterfree audiogen init` writes it), then CLI flags.

Three model sets, each loaded by its own openmoss server (one model per
process, and only one fits the card at a time):

* ``voice``  -- MOSS-VoiceGenerator: invents a voice from a text description.
* ``speech`` -- MOSS-TTS-Local v1.5: speaks lines, cloning a saved voice.
* ``sfx``    -- MOSS-SoundEffect v2.0: text to sound effect.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

CONFIG_PATH = (".waterfree", "audiogen.json")
DEFAULT_OUTPUT_DIR = (".waterfree", "generated", "audio")
DEFAULT_VOICES_DIR = (".waterfree", "voices")

ROLES = ("voice", "speech", "sfx")
CHANNEL_MODES = ("native", "mono", "stereo")
SFX_MAX_SECONDS = 30.0


@dataclass(frozen=True)
class AudiogenConfig:
    voice_model: str = "moss-voicegen"
    speech_model: str = "moss-tts-local-q8"
    sfx_model: str = "moss-sfx"
    # Game engines spatialise mono sources; "native" keeps the model's own layout
    # (24 kHz mono for voice design, 48 kHz stereo for speech, 48 kHz mono for SFX).
    channels: str = "native"
    language: str = ""          # optional hint, e.g. "en"; empty lets the model decide
    # libllama context for the speech/voice backbones. The engine default (8192)
    # spends 1.15 GB of KV cache on MOSS-TTS-Local -- room a 12 GB card does not
    # have -- for utterances no game line comes near. 0 = engine default.
    n_ctx: int = 4096
    max_new_tokens: int = 0     # 0 = half of n_ctx (2048 frames = ~2.7 min of audio)
    sfx_seconds: float = 4.0
    sfx_steps: int = 100
    sfx_cfg: float = 4.0
    sfx_negative_prompt: str = ""
    output_dir: str = ""
    voices_dir: str = ""

    def model_for(self, role: str) -> str:
        return {"voice": self.voice_model, "speech": self.speech_model, "sfx": self.sfx_model}[role]

    def validate(self) -> AudiogenConfig:
        if self.channels not in CHANNEL_MODES:
            raise ValueError(f"channels must be one of {CHANNEL_MODES}")
        if not 0 < float(self.sfx_seconds) <= SFX_MAX_SECONDS:
            raise ValueError(f"SFX seconds must be greater than 0 and at most {SFX_MAX_SECONDS:g}")
        if not 1 <= int(self.sfx_steps) <= 500:
            raise ValueError("SFX steps must be between 1 and 500")
        if not 1.0 <= float(self.sfx_cfg) <= 20.0:
            raise ValueError("SFX cfg must be between 1 and 20")
        if self.max_new_tokens < 0:
            raise ValueError("max_new_tokens must be 0 (half of n_ctx) or positive")
        if self.n_ctx and not 1024 <= self.n_ctx <= 32768:
            raise ValueError("n_ctx must be 0 (engine default) or between 1024 and 32768")
        if self.n_ctx and self.max_new_tokens >= self.n_ctx:
            raise ValueError("max_new_tokens must stay below n_ctx, or a long take overruns the context")
        return self

    def effective_max_new_tokens(self) -> int:
        """Frames a single take may generate; 0 = leave it to the engine."""
        if self.max_new_tokens:
            return self.max_new_tokens
        return self.n_ctx // 2 if self.n_ctx else 0


def load_config(workspace_path: str = "") -> AudiogenConfig:
    if not workspace_path:
        return AudiogenConfig()
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AudiogenConfig()
    if not isinstance(raw, dict):
        return AudiogenConfig()
    known = set(AudiogenConfig.__dataclass_fields__)
    payload = {_snake(k): v for k, v in raw.items() if _snake(k) in known and v is not None}
    try:
        return AudiogenConfig(**payload)
    except TypeError:
        return AudiogenConfig()


def apply_overrides(config: AudiogenConfig, **overrides: Any) -> AudiogenConfig:
    supplied = {k: v for k, v in overrides.items() if v is not None}
    return replace(config, **supplied) if supplied else config


def write_default_config(workspace_path: str) -> Path:
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {_camel(k): v for k, v in asdict(AudiogenConfig()).items()}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def output_dir_for(config: AudiogenConfig, workspace_path: str, kind: str) -> Path:
    root = Path(config.output_dir).expanduser() if config.output_dir.strip() \
        else Path(workspace_path or ".").joinpath(*DEFAULT_OUTPUT_DIR)
    return root / kind


def voices_dir_for(config: AudiogenConfig, workspace_path: str) -> Path:
    if config.voices_dir.strip():
        return Path(config.voices_dir).expanduser()
    return Path(workspace_path or ".").joinpath(*DEFAULT_VOICES_DIR)


def _snake(key: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in str(key))


def _camel(key: str) -> str:
    head, *rest = str(key).split("_")
    return head + "".join(part.title() for part in rest)
