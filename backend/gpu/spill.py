"""Notice when a running GPU job spills out of VRAM into system RAM (Windows).

The preflight (:mod:`backend.gpu.preflight`) checks free VRAM before a job
starts. It cannot see a job that grows past the card *after* starting: under
the NVIDIA Windows driver's default "sysmem fallback" policy the overflow is
silently placed in system RAM and the job keeps running, many times slower,
with no error anywhere. Measured 2026-11-17: MOSS-TTS-Local at n_ctx 8192
peaked at 11.9 of 12.3 GB and its codec decode went from 0.17 s to 3.7 s a line.

``nvidia-smi`` cannot attribute memory to a process under WDDM, but Windows'
own performance counters can: ``\\GPU Process Memory(pid_<pid>_*)\\Shared Usage``
is the GPU memory a process has in *system* RAM. A CUDA job that fits its card
keeps that small and flat; when it climbs, the job is spilling. Read through
PDH via ctypes, with no subprocesses and no packages; elsewhere this is a no-op.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Self

SPILL_WARN_MB = 256           # growth of Shared Usage over the job's own baseline
_SAMPLE_SECONDS = 3.0
_MB = 1024 * 1024


class _PdhProcessMemory:
    """Shared and dedicated GPU memory of one process, summed over its adapters."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._pdh = ctypes.WinDLL("pdh")
        self._query = wintypes.HANDLE()
        if self._pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)) != 0:
            raise OSError("PdhOpenQuery failed")
        self._counters = {}
        for name in ("Shared Usage", "Dedicated Usage"):
            handle = wintypes.HANDLE()
            path = f"\\GPU Process Memory(pid_{pid}_*)\\{name}"
            status = self._pdh.PdhAddEnglishCounterW(self._query, path, None, ctypes.byref(handle))
            if status != 0:
                self.close()
                raise OSError(f"GPU Process Memory counters are not available (PDH status 0x{status & 0xFFFFFFFF:08X})")
            self._counters[name] = handle

    def sample(self) -> tuple[int, int] | None:
        """(shared bytes, dedicated bytes), or None before the process has a GPU context."""
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return None
        values = [self._sum(self._counters[name]) for name in ("Shared Usage", "Dedicated Usage")]
        if values[0] is None and values[1] is None:
            return None
        return values[0] or 0, values[1] or 0

    def _sum(self, counter) -> int | None:
        ctypes = self._ctypes
        from ctypes import wintypes

        class Value(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("largeValue", ctypes.c_longlong)]  # noqa: RUF012 - ctypes layout

        class Item(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", Value)]  # noqa: RUF012 - ctypes layout

        pdh_fmt_large, pdh_more_data = 0x00000400, 0x800007D2
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = self._pdh.PdhGetFormattedCounterArrayW(counter, pdh_fmt_large, ctypes.byref(size),
                                                        ctypes.byref(count), None)
        if (status & 0xFFFFFFFF) != pdh_more_data or not size.value:
            return None
        buffer = (ctypes.c_byte * size.value)()
        status = self._pdh.PdhGetFormattedCounterArrayW(counter, pdh_fmt_large, ctypes.byref(size),
                                                        ctypes.byref(count), buffer)
        if status != 0:
            return None
        items = ctypes.cast(buffer, ctypes.POINTER(Item))
        return sum(int(items[k].FmtValue.largeValue) for k in range(count.value) if items[k].FmtValue.CStatus in (0, 1))

    def close(self) -> None:
        if self._query:
            self._pdh.PdhCloseQuery(self._query)
            self._query = None


class SpillWatch:
    """Sample a process's GPU memory in the background; warn once if it starts spilling.

    Use as a context manager around the job; :meth:`report_json` afterwards
    gives ``{"vramSpillMb", "sharedPeakMb", "dedicatedPeakMb"}`` for the
    result, or None where the counters do not exist.
    """

    def __init__(self, pid: int, *, label: str, report: Callable[[str], None] | None = None,
                 warn_mb: int = SPILL_WARN_MB, interval: float = _SAMPLE_SECONDS) -> None:
        self.pid = pid
        self.label = label
        self.report = report or (lambda message: print(message, file=sys.stderr, flush=True))
        self.warn_mb = warn_mb
        self.interval = interval
        self.baseline: int | None = None
        self.shared_peak = 0
        self.dedicated_peak = 0
        self.warned = False
        self._reader: _PdhProcessMemory | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        if sys.platform != "win32":
            return
        try:
            self._reader = _PdhProcessMemory(self.pid)
        except OSError:
            self._reader = None
            return
        self._thread = threading.Thread(target=self._run, name=f"spill-watch-{self.pid}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)
            self._thread = None
        if self._reader is not None:
            self.sample_once()
            self._reader.close()

    def _run(self) -> None:
        while True:
            self.sample_once()
            if self._stop.wait(self.interval):
                return

    def sample_once(self) -> None:
        reading = self._reader.sample() if self._reader is not None else None
        if reading is None:
            return
        shared, dedicated = reading
        if self.baseline is None:
            self.baseline = shared
        self.shared_peak = max(self.shared_peak, shared)
        self.dedicated_peak = max(self.dedicated_peak, dedicated)
        if not self.warned and self.spill_mb() >= self.warn_mb:
            self.warned = True
            self.report(
                f"WARNING: {self.label} is spilling out of VRAM: {self.spill_mb()} MB of its GPU memory is now "
                f"in system RAM (dedicated {dedicated // _MB} MB). The NVIDIA Windows driver does this silently "
                "and the job runs many times slower. Close other GPU programs, or lower the job's size; "
                "its measured VRAM need may be too low.")

    def spill_mb(self) -> int:
        if self.baseline is None:
            return 0
        return max(0, (self.shared_peak - self.baseline) // _MB)

    def report_json(self) -> dict | None:
        if self.baseline is None:
            return None
        return {"vramSpillMb": self.spill_mb(), "sharedPeakMb": self.shared_peak // _MB,
                "dedicatedPeakMb": self.dedicated_peak // _MB, "spilled": self.spill_mb() >= self.warn_mb}
