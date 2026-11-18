"""Small 16-bit PCM WAV helpers (stdlib ``wave``; numpy when present for speed)."""

from __future__ import annotations

import io
import wave
from pathlib import Path


def info(source: Path | bytes) -> dict:
    with _open(source) as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        return {
            "sampleRate": rate,
            "channels": handle.getnchannels(),
            "sampleWidth": handle.getsampwidth(),
            "frames": frames,
            "seconds": frames / rate if rate else 0.0,
        }


def convert_channels(data: bytes, mode: str) -> bytes:
    """Return ``data`` (a WAV file's bytes) as mono, stereo, or unchanged ("native")."""
    if mode == "native":
        return data
    with _open(data) as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        pcm = handle.readframes(handle.getnframes())
    want = 1 if mode == "mono" else 2
    if channels == want:
        return data
    if width != 2:
        raise ValueError(f"Only 16-bit PCM can be converted; this WAV is {8 * width}-bit.")
    if channels == 2 and want == 1:
        pcm = _downmix(pcm)
    elif channels == 1 and want == 2:
        pcm = _duplicate(pcm)
    else:
        raise ValueError(f"Cannot convert {channels} channels to {want}.")
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(want)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return out.getvalue()


def _downmix(pcm: bytes) -> bytes:
    try:
        import numpy as np
        samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, 2).astype(np.int32)
        return ((samples[:, 0] + samples[:, 1]) // 2).astype("<i2").tobytes()
    except ImportError:
        from array import array
        stereo = array("h", pcm)
        return array("h", ((stereo[i] + stereo[i + 1]) // 2 for i in range(0, len(stereo), 2))).tobytes()


def _duplicate(pcm: bytes) -> bytes:
    try:
        import numpy as np
        samples = np.frombuffer(pcm, dtype="<i2")
        return np.repeat(samples, 2).astype("<i2").tobytes()
    except ImportError:
        from array import array
        mono = array("h", pcm)
        return array("h", (s for sample in mono for s in (sample, sample))).tobytes()


def _open(source: Path | bytes) -> wave.Wave_read:
    if isinstance(source, (bytes, bytearray)):
        return wave.open(io.BytesIO(source), "rb")
    return wave.open(str(source), "rb")
