"""Report observed Hugging Face cache bytes on stderr during model loading.

Totals are unknown until Hub metadata is fetched; never invent a percentage.
This does not change Hub download behavior or global progress-bar classes.
"""
from __future__ import annotations
import json
import os
import sys
import threading
from pathlib import Path


class DownloadProgress:
    def __init__(self, repo_id: str, *, cache_root=None, emit=None, interval=2.0):
        if cache_root is None:
            from huggingface_hub.constants import HF_HUB_CACHE
            cache_root = HF_HUB_CACHE
        self.blobs = Path(cache_root) / ("models--" + repo_id.replace("/", "--")) / "blobs"
        self.emit = emit or (lambda value: print(json.dumps(value), file=sys.stderr, flush=True))
        self.interval = interval
        self.stop = threading.Event()
        self.thread = None
        self.initial = self._bytes()[0]
        self.previous = self.initial

    def _bytes(self):
        total = active = 0
        try:
            for path in self.blobs.iterdir():
                if path.is_file():
                    size = path.stat().st_size
                    total += size
                    if path.name.endswith(".incomplete"):
                        active += size
        except OSError:
            pass
        return total, active

    def sample(self, *, finished=False):
        total, active = self._bytes()
        growing = total > self.previous
        self.previous = total
        self.emit({"phase": "loaded" if finished else "downloading" if growing else "loading",
                   "newCacheBytes": max(0, total - self.initial), "incompleteCacheBytes": active,
                   "totalBytes": None})

    def _watch(self):
        while not self.stop.wait(self.interval):
            self.sample()

    def __enter__(self):
        if os.environ.get("WATERFREE_IMAGEGEN_PROGRESS", "1") != "0":
            self.sample()
            self.thread = threading.Thread(target=self._watch, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, exc_type, *_):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
            if exc_type is None:
                self.sample(finished=True)
