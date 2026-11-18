"""Flags and error mapping shared by every CLI action that uses the GPU.

Exit codes follow docs/cli-surface.md: a busy GPU, too little free VRAM, or a
missing install are all "external dependency unavailable" (4) and print a JSON
body on stdout that says exactly what is in the way.
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Callable

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_INTERNAL,
    EXIT_USAGE,
    emit_error,
    emit_json,
)
from backend.gpu import lease as gpu_lease


def add_gpu_args(parser: ArgumentParser, *, vram_check: bool = True) -> None:
    group = parser.add_argument_group("GPU queue")
    group.add_argument("--wait-timeout", type=float, default=None, metavar="SECONDS",
                       help="How long to queue for the GPU if another job holds it "
                            "(default 1800, or $WATERFREE_GPU_WAIT_TIMEOUT).")
    group.add_argument("--no-wait", action="store_true",
                       help="Fail at once with exit 4 if the GPU is busy, instead of queueing.")
    if vram_check:
        group.add_argument("--skip-vram-check", action="store_true",
                           help="Start even if free VRAM looks too low. On Windows an over-budget job "
                                "spills into system RAM and runs many times slower.")
    group.add_argument("--quiet", action="store_true", help="No progress lines on stderr.")


def policy_from(args: Namespace) -> gpu_lease.WaitPolicy:
    if getattr(args, "no_wait", False):
        timeout = 0.0
    elif getattr(args, "wait_timeout", None) is not None:
        timeout = max(0.0, float(args.wait_timeout))
    else:
        timeout = gpu_lease.default_wait_timeout()
    return gpu_lease.WaitPolicy(timeout=timeout, progress=not getattr(args, "quiet", False))


def report_from(args: Namespace) -> Callable[[str], None]:
    if getattr(args, "quiet", False):
        return lambda _message: None
    return lambda message: print(message, file=sys.stderr, flush=True)


def run_gpu_action_quiet(action: Callable[[], dict]) -> int:
    """Like :func:`run_gpu_action`, but print nothing on success (the action owns stdout)."""
    return run_gpu_action(action, emit_result=False)


def run_gpu_action(action: Callable[[], dict], *, emit_result: bool = True) -> int:
    """Run ``action``; print its result, or map a known failure to its exit code."""
    from backend.audiogen.voices import VoiceError
    from backend.engines.install import InstallError
    from backend.gpu.engine import EngineError
    from backend.gpu.preflight import InsufficientVram
    from backend.gpu.ram import InsufficientRam
    from backend.imagegen.backend_diffusers import BackendUnavailable, GenerationFailed

    try:
        result = action()
    except gpu_lease.GpuBusy as exc:
        emit_json(exc.to_json())
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except (InsufficientVram, InsufficientRam) as exc:
        emit_json(exc.to_json())
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except (InstallError, BackendUnavailable, GenerationFailed) as exc:
        # GenerationFailed stays exit 4, as imagegen has always reported it.
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except EngineError as exc:
        where = f"\nEngine log: {exc.log_path}" if exc.log_path else ""
        tail = f"\n{exc.log_tail}" if exc.log_tail and exc.log_tail not in str(exc) else ""
        return emit_error(f"{exc}{where}{tail}", exit_code=EXIT_INTERNAL)
    except (VoiceError, ValueError) as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    if not emit_result:
        return 0
    emit_json(result)
    if result.get("failed") and not result.get("count"):
        return EXIT_INTERNAL
    return 0
