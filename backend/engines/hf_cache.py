"""Hugging Face cache helpers shared by the areas that download through ``huggingface_hub``."""

from __future__ import annotations

import sys


def avoid_symlink_race() -> bool:
    """Force copy mode for HF downloads on Windows machines that cannot create symlinks.

    huggingface_hub's ``are_symlinks_supported(dir)`` stores a provisional
    ``True`` for a directory *before* testing ``os.symlink`` there, and
    ``snapshot_download`` (behind ``DiffusionPipeline.download``) runs 8 worker
    threads. On a fresh repo folder a second thread can read that provisional
    value, try a symlink, and kill the whole download with
    ``[WinError 1314] A required privilege is not held by the client`` -- an
    error the hub does not catch. Seen 2026-11-18 (huggingface_hub 1.28) on a
    machine without Developer Mode, whose cache was already in copy mode.

    So: probe once, single-threaded, on the cache root; if symlinks do not
    work there, set the hub's own ``HF_HUB_DISABLE_SYMLINKS`` so no per-repo
    probe runs at all. Returns True when copy mode is in force. (todo GPU-010)
    """
    if sys.platform != "win32":
        return False
    try:
        from huggingface_hub import constants
        from huggingface_hub.file_download import are_symlinks_supported
    except ImportError:
        return False
    if constants.HF_HUB_DISABLE_SYMLINKS:
        return True
    if are_symlinks_supported(constants.HF_HUB_CACHE):
        return False
    constants.HF_HUB_DISABLE_SYMLINKS = True
    return True
