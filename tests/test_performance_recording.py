"""Offline timing instrumentation must not expose content or alter recovery."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_sender.performance import ItemPerformanceTrace, PerformanceRecorder


class Clock:
    now = 0.0
    def __call__(self):
        return self.now


class TestTimingRecords(unittest.TestCase):
    def test_nested_stages_separate_own_work_from_wait(self):
        clock = Clock()
        trace = ItemPerformanceTrace(3, clock)
        with trace.measure("analysis"):
            clock.now += .1
            with trace.measure("wait"):
                clock.now += .3
            clock.now += .1
        summary = trace.snapshot("COMPLETED")
        self.assertAlmostEqual(summary["stages"]["analysis"]["total_ms"], 500)
        self.assertAlmostEqual(summary["stages"]["analysis"]["own_ms"], 200)
        self.assertAlmostEqual(summary["stages"]["wait"]["own_ms"], 300)

    def test_exception_keeps_timing_and_propagates_business_error(self):
        clock = Clock()
        trace = ItemPerformanceTrace(0, clock)
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with trace.measure("capture"):
                clock.now += .02
                raise RuntimeError("synthetic")
        self.assertEqual(trace.snapshot("NEEDS_MANUAL_VERIFY")["stages"]["capture"]["count"], 1)

    def test_only_safe_numeric_counters_and_known_outcomes_are_written(self):
        trace = ItemPerformanceTrace(2)
        trace.add_counters({"cache_hits": 4, "cache_misses": float("nan"),
                            "chat_title": "private", "file_path": "private", "roi_hits": "private"})
        with tempfile.TemporaryDirectory() as folder:
            path = PerformanceRecorder(Path(folder)).finish(trace, "private name")
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["outcome"], "UNKNOWN")
        self.assertEqual(data["counters"], {"cache_hits": 4.0})
        self.assertNotIn("private", json.dumps(data))

    def test_unwritable_record_does_not_raise(self):
        recorder = PerformanceRecorder(Path("unused"))
        with patch.object(Path, "mkdir", side_effect=PermissionError("synthetic")):
            self.assertIsNone(recorder.finish(ItemPerformanceTrace(0), "COMPLETED"))
        self.assertEqual(recorder.last_summary["outcome"], "COMPLETED")

    def test_each_finished_item_is_one_json_line(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = PerformanceRecorder(Path(folder))
            recorder.finish(ItemPerformanceTrace(0), "COMPLETED")
            recorder.finish(ItemPerformanceTrace(1), "NEEDS_MANUAL_VERIFY")
            rows = [json.loads(line) for line in recorder.last_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["item_index"] for row in rows], [0, 1])
            self.assertEqual(rows[1]["outcome"], "NEEDS_MANUAL_VERIFY")
