"""Output naming and sidecar metadata shared by meshgen and audiogen.

Every generated file gets a ``.json`` sidecar with its inputs, seed, settings,
engine version, model revision and licence: without it a promising result is
unreproducible five minutes later, and its licence is unknowable a year later.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from pathlib import Path

_SLUG_MAX_CHARS = 40


def slug(text: str, max_chars: int = _SLUG_MAX_CHARS) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_chars].strip("-")


def stem(text: str, *, timestamp: float | None = None) -> str:
    """``YYYYmmdd-HHMMSS_<slug>_<8 hex>`` -- unique even for concurrent same-second runs."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp or time.time()))
    tag = uuid.uuid4().hex[:8]
    name = slug(text)
    return f"{stamp}_{name}_{tag}" if name else f"{stamp}_{tag}"


def file_sha256(path: str | Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sidecar(artifact: Path, data: dict) -> Path:
    sidecar = artifact.with_suffix(".json")
    sidecar.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return sidecar


def resolve_seed(seed: int) -> int:
    """A negative seed means "pick one" -- but always a concrete, recorded one."""
    return seed if seed >= 0 else random.randrange(0, 2 ** 31)


def engine_provenance(release, model_set) -> dict:
    return {
        "engine": {"project": release.project, "version": release.version},
        "model": {"set": model_set.key, "label": model_set.label, "repo": model_set.repo,
                  "revision": model_set.revision},
        "licence": model_set.licence,
    }
