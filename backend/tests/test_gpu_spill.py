"""GPU-005: notice a job spilling out of VRAM into system RAM while it runs."""

import os
import sys
import unittest
from unittest import mock

from backend.gpu import spill

MB = 1024 * 1024


class FakeReader:
    def __init__(self, readings):
        self.readings = list(readings)
        self.closed = False

    def sample(self):
        return self.readings.pop(0) if self.readings else None

    def close(self):
        self.closed = True


class SpillWatchTests(unittest.TestCase):
    def watch_with(self, readings, messages):
        watch = spill.SpillWatch(1234, label="trellis", report=messages.append, interval=60)
        watch._reader = FakeReader(readings)
        return watch

    def test_a_flat_job_is_quiet(self) -> None:
        messages: list[str] = []
        watch = self.watch_with([(70 * MB, 4000 * MB), (75 * MB, 4500 * MB), (74 * MB, 4400 * MB)], messages)
        for _ in range(3):
            watch.sample_once()
        self.assertEqual(messages, [])
        self.assertEqual(watch.report_json(), {"vramSpillMb": 5, "sharedPeakMb": 75, "dedicatedPeakMb": 4500,
                                               "spilled": False})

    def test_growth_past_the_threshold_warns_once(self) -> None:
        messages: list[str] = []
        watch = self.watch_with([(70 * MB, 9000 * MB), (600 * MB, 9800 * MB), (900 * MB, 9800 * MB)], messages)
        for _ in range(3):
            watch.sample_once()
        self.assertEqual(len(messages), 1)
        self.assertIn("spilling out of VRAM", messages[0])
        self.assertEqual(watch.report_json()["vramSpillMb"], 830)
        self.assertTrue(watch.report_json()["spilled"])

    def test_no_counters_means_no_report(self) -> None:
        watch = spill.SpillWatch(1234, label="x", report=lambda _m: None)
        with mock.patch.object(spill.sys, "platform", "linux"):
            watch.start()
        watch.stop()
        self.assertIsNone(watch.report_json())

    @unittest.skipUnless(sys.platform == "win32", "PDH counters are Windows-only")
    def test_real_counters_read_without_error_for_a_process_with_no_gpu_context(self) -> None:
        reader = spill._PdhProcessMemory(os.getpid())
        try:
            reading = reader.sample()
        finally:
            reader.close()
        # The test process may or may not have touched the GPU; either way it must not raise.
        self.assertTrue(reading is None or (reading[0] >= 0 and reading[1] >= 0))


if __name__ == "__main__":
    unittest.main()
