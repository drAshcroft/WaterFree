"""Take the GPU for a heavy job: exclusive lease, evict Ollama, check free VRAM.

Every heavy area (imagegen, meshgen, audiogen) goes through
:func:`exclusive_gpu` so the order is always the same:

1. queue for the exclusive lease (waits, with progress, per the WaitPolicy);
2. unload whatever Ollama still has resident from earlier calls;
3. confirm the card has the free memory the job needs.

Step 3 matters more on Windows than it looks. When a CUDA allocation exceeds
VRAM, the NVIDIA Windows driver's default "sysmem fallback" policy quietly
places it in system RAM instead of failing, and the job runs many times slower
with no error. Refusing up front is kinder than a mesh that takes two hours.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from backend.gpu import lease as lease_mod
from backend.gpu import ollama, vram


class InsufficientVram(RuntimeError):
    def __init__(self, message: str, *, need_gb: float, gpu: vram.GpuMemory, others: list[dict]) -> None:
        super().__init__(message)
        self.need_gb = need_gb
        self.gpu = gpu
        self.others = others

    def to_json(self) -> dict:
        return {
            "insufficientVram": True,
            "message": str(self),
            "needGb": round(self.need_gb, 2),
            "gpu": self.gpu.public(),
            "otherGpuProcesses": self.others,
        }


@dataclass
class GpuGrant:
    lease: lease_mod.Lease
    evicted: list[str]
    gpu: vram.GpuMemory | None
    waited_seconds: float

    def public(self) -> dict:
        return {
            "leaseId": self.lease.id,
            "evictedOllamaModels": self.evicted,
            "gpu": self.gpu.public() if self.gpu else None,
            "waitedSeconds": round(self.waited_seconds, 1),
        }


@contextmanager
def exclusive_gpu(
    *,
    label: str,
    detail: str = "",
    vram_gb: float | None = None,
    policy: lease_mod.WaitPolicy | None = None,
    check_vram: bool = True,
    gpu_index: int = 0,
) -> Iterator[GpuGrant]:
    """Hold the GPU exclusively for the body of the ``with`` block."""
    policy = policy or lease_mod.WaitPolicy()
    started = time.monotonic()
    held = lease_mod.acquire(lease_mod.EXCLUSIVE, label=label, detail=detail,
                             vram_gb=vram_gb, policy=policy)
    try:
        waited = time.monotonic() - started
        evicted: list[str] = []
        gpu = None
        # With the scheduler off (WATERFREE_GPU_SCHEDULER=0) nothing is coordinated:
        # no eviction of someone else's Ollama models, no VRAM gate.
        if lease_mod.scheduler_enabled():
            evicted = ollama.evict_all()
            if evicted:
                policy.emit(f"Unloaded Ollama models to free the GPU: {', '.join(evicted)}")
            gpu = _check_free_vram(vram_gb, gpu_index, policy) if check_vram else vram.query(gpu_index)
        yield GpuGrant(lease=held, evicted=evicted, gpu=gpu, waited_seconds=waited)
    finally:
        held.release()


def _check_free_vram(need_gb: float | None, index: int, policy: lease_mod.WaitPolicy,
                     settle_seconds: float = 10.0) -> vram.GpuMemory | None:
    gpu = vram.query(index)
    if gpu is None:
        policy.emit("nvidia-smi is not available; skipping the free-VRAM check.")
        return None
    if not need_gb:
        return gpu
    deadline = time.monotonic() + settle_seconds
    # Freed memory shows up a moment after a process unloads, so give a just-
    # evicted model (or a just-finished previous job) a few seconds to let go.
    while gpu.free_gb < need_gb and time.monotonic() < deadline:
        time.sleep(1.0)
        gpu = vram.query(index) or gpu
    if gpu.free_gb >= need_gb:
        return gpu
    others = vram.gpu_processes()
    names = vram.significant_process_names(others)
    raise InsufficientVram(
        f"This job needs about {need_gb:.1f} GB of free VRAM on the {gpu.name}, but only "
        f"{gpu.free_gb:.1f} of {gpu.total_gb:.1f} GB is free. Other programs using the GPU: "
        f"{', '.join(names) or 'none reported'} (Windows does not report per-program usage). "
        "Close GPU-heavy programs (browsers with many tabs, games, other AI tools) and retry, "
        "or pass --skip-vram-check to run anyway: on Windows an over-budget job spills into "
        "system RAM and runs many times slower instead of failing.",
        need_gb=need_gb, gpu=gpu, others=others,
    )
