"""The machine-wide GPU lease: modes, FIFO fairness, crash safety, inheritance."""

import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.gpu import lease
from backend.test_support import make_temp_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


class LeaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = make_temp_dir(self, prefix="gpu-lease-")
        patcher = mock.patch.dict(os.environ, {
            "WATERFREE_GPU_SCHEDULER": "1",
            "WATERFREE_GPU_DIR": str(self.state_dir),
            "WATERFREE_GPU_PROGRESS": "0",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(lease.LEASE_ENV, None)
        lease.release_all()
        self.addCleanup(lease.release_all)

    def child(self, body: str, *, env: dict | None = None) -> subprocess.Popen:
        """A separate process running ``body`` with the same lease directory."""
        script = textwrap.dedent(body)
        child_env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), **(env or {})}
        proc = subprocess.Popen([sys.executable, "-c", script], env=child_env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)

    @staticmethod
    def wait_for_line(proc: subprocess.Popen, expected: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line.strip() == expected:
                return
            if not line and proc.poll() is not None:
                raise AssertionError(f"child exited: {proc.stderr.read()}")
        raise AssertionError(f"child never printed {expected!r}")


class InProcessTests(LeaseTestCase):
    def test_exclusive_is_recorded_and_released(self) -> None:
        with lease.hold(lease.EXCLUSIVE, label="meshgen make", detail="test"):
            holders = lease.snapshot()["holders"]
            self.assertEqual([h["label"] for h in holders], ["meshgen make"])
            self.assertEqual(holders[0]["mode"], "exclusive")
        self.assertEqual(lease.snapshot()["holders"], [])

    def test_nested_requests_the_held_lease_covers_share_one_entry(self) -> None:
        with lease.hold(lease.EXCLUSIVE, label="outer"):
            with lease.hold(lease.SHARED, label="inner"):
                with lease.hold(lease.EXCLUSIVE, label="inner2"):
                    self.assertEqual(len(lease.snapshot()["holders"]), 1)
            self.assertEqual(len(lease.snapshot()["holders"]), 1)
        self.assertEqual(lease.snapshot()["holders"], [])

    def test_process_scoped_shared_lease_survives_until_upgrade(self) -> None:
        lease.ensure_process_shared(label="vision look")
        self.assertEqual(lease.snapshot()["holders"][0]["mode"], "shared")
        lease.ensure_process_shared(label="vision look")  # idempotent
        self.assertEqual(len(lease.snapshot()["holders"]), 1)
        with lease.hold(lease.EXCLUSIVE, label="imagegen make"):
            modes = [h["mode"] for h in lease.snapshot()["holders"]]
            self.assertEqual(modes, ["exclusive"])
        self.assertEqual(lease.snapshot()["holders"], [])

    def test_disabled_scheduler_hands_out_no_op_leases(self) -> None:
        with mock.patch.dict(os.environ, {"WATERFREE_GPU_SCHEDULER": "0"}):
            with lease.hold(lease.EXCLUSIVE, label="x") as held:
                self.assertIsNone(held.id)
            self.assertFalse(lease.snapshot()["enabled"])

    def test_grant_rules_are_fifo_with_shared_batching(self) -> None:
        def entry(entry_id: str, mode: str) -> lease.Entry:
            return lease.Entry(id=entry_id, pid=1, token=None, mode=mode, label=entry_id)

        shared_a, shared_b, excl = entry("a", "shared"), entry("b", "shared"), entry("x", "exclusive")
        state = lease._State(holders=[], queue=[shared_a, excl, shared_b])
        self.assertTrue(lease._grantable(state, shared_a))
        self.assertFalse(lease._grantable(state, excl), "exclusive must wait for the shared ticket ahead")
        self.assertFalse(lease._grantable(state, shared_b), "shared must not jump a queued exclusive")
        state = lease._State(holders=[shared_a], queue=[shared_b])
        self.assertTrue(lease._grantable(state, shared_b), "shared requests run together")
        state = lease._State(holders=[shared_a], queue=[excl])
        self.assertFalse(lease._grantable(state, excl))

    def test_a_torn_state_file_does_not_wedge_the_gpu(self) -> None:
        (self.state_dir / "state.json").write_text("{not json", encoding="utf-8")
        with lease.hold(lease.EXCLUSIVE, label="after-corruption"):
            self.assertEqual(len(lease.snapshot()["holders"]), 1)

    def test_dead_entries_are_pruned_on_read(self) -> None:
        with lease._locked_state() as state:
            state.holders.append(lease.Entry(id="ghost", pid=999_999_999, token=123, mode="exclusive",
                                             label="crashed job", granted_at=time.time()))
        self.assertEqual(lease.snapshot()["holders"], [])


class CrossProcessTests(LeaseTestCase):
    HOLD_EXCLUSIVE = """
        import sys, time
        from backend.gpu import lease
        held = lease.acquire(lease.EXCLUSIVE, label="child job", detail="holding")
        print("held", flush=True)
        time.sleep(60)
    """

    def test_a_second_process_cannot_take_a_held_gpu(self) -> None:
        holder = self.child(self.HOLD_EXCLUSIVE)
        self.wait_for_line(holder, "held")
        with self.assertRaises(lease.GpuBusy) as ctx:
            lease.acquire(lease.SHARED, label="vision look", policy=lease.WaitPolicy(timeout=0))
        self.assertEqual(ctx.exception.holders[0]["label"], "child job")
        self.assertEqual(lease.snapshot()["queue"], [], "a refused request must withdraw its ticket")

    def test_a_killed_holder_frees_the_gpu(self) -> None:
        holder = self.child(self.HOLD_EXCLUSIVE)
        self.wait_for_line(holder, "held")
        holder.kill()
        holder.wait(timeout=10)
        with lease.hold(lease.EXCLUSIVE, label="next job", policy=lease.WaitPolicy(timeout=10)):
            self.assertEqual([h["label"] for h in lease.snapshot()["holders"]], ["next job"])

    def test_waiters_are_granted_when_the_holder_finishes(self) -> None:
        holder = self.child("""
            import time
            from backend.gpu import lease
            with lease.hold(lease.EXCLUSIVE, label="short job"):
                print("held", flush=True)
                time.sleep(1.5)
        """)
        self.wait_for_line(holder, "held")
        started = time.monotonic()
        with lease.hold(lease.EXCLUSIVE, label="queued", policy=lease.WaitPolicy(timeout=30)):
            waited = time.monotonic() - started
        self.assertGreater(waited, 0.5)

    def test_child_process_inherits_the_lease_instead_of_deadlocking(self) -> None:
        with lease.hold(lease.EXCLUSIVE, label="parent"):
            child = self.child("""
                from backend.gpu import lease
                with lease.hold(lease.EXCLUSIVE, label="child", policy=lease.WaitPolicy(timeout=0)):
                    print("inside", flush=True)
            """, env=lease.child_env({}))
            self.wait_for_line(child, "inside")
            child.wait(timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr.read())
            self.assertEqual([h["label"] for h in lease.snapshot()["holders"]], ["parent"],
                             "the child's release must not drop the parent's lease")


if __name__ == "__main__":
    unittest.main()
