"""Offline UI work-count regressions; no Tk window or real WeChat is opened."""

import json
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_sender.gui_workflow import SenderWorkflowMixin
from wechat_sender.job_store import ItemStage, JobRecordItem, JobState
from tests.test_gui_workflow import ControlledThread, Harness as WorkflowHarness, Value


class CountedRows(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.visits = 0

    def __iter__(self):
        for item in super().__iter__():
            self.visits += 1
            yield item


class Queue:
    def __init__(self):
        self.callbacks = []

    def after(self, _delay, callback):
        self.callbacks.append(callback)

    def flush(self):
        while self.callbacks:
            self.callbacks.pop(0)()


class Tree:
    def __init__(self):
        self.selected = ()
        self.updates = []
        self.selection_updates = 0

    def exists(self, _iid):
        return True

    def item(self, iid, **options):
        self.updates.append((iid, options))

    def selection(self):
        return self.selected

    def selection_set(self, iid):
        self.selected = (iid,)
        self.selection_updates += 1

    def see(self, _iid):
        pass


def record(count):
    rows = CountedRows(JobRecordItem(i, f"{i}.png", f"{i}.png", str(i), "digest")
                       for i in range(count))
    return JobState("offline-ui", ".", "Offline recipient", "CF_HDROP", "ENTER", False, False, 0, rows)


class Harness(SenderWorkflowMixin):
    def __init__(self, count):
        self.root, self.tree = Queue(), Tree()
        self.current_job = record(count)
        self.worker_thread = None
        self._run_active, self._closing = True, False
        self._run_start_ts, self._run_end_ts = 0.0, None
        self.chat_combo = Value()
        self.status_var, self.progress_var = Value(), Value()
        self.kpi_val_labels = {key: Value() for key in ("total", "done", "verify", "speed")}
        self.scanned_items = []
        self.logs, self.previews = [], []
        self.controls_refreshes = 0
        self._workflow_state()

    def _refresh_task_controls(self):
        # The real layout also reads verification/completion state when its
        # buttons are refreshed; include that cost in the work-count probe.
        self.controls_refreshes += 1
        any(row.stage == ItemStage.NEEDS_MANUAL_VERIFY.value for row in self.current_job.items)
        all(row.stage == ItemStage.COMPLETED.value for row in self.current_job.items)

    def _row_matches_filter(self, *_args):
        return True

    def _stage_to_tag(self, stage):
        return stage

    def _show_preview_for_index(self, index):
        self.previews.append(index)

    def _append_log(self, message):
        self.logs.append(message)

    def _update_mini_hud(self, _message):
        pass

    def notify(self, index, stage):
        item = self.current_job.items[index]
        item.stage = stage
        self._on_engine_progress(item, stage)
        self.root.flush()


def workload(count=10000, completed=20):
    app = Harness(count)
    started = time.perf_counter()
    stages = [ItemStage.IMAGE_SUBMITTED.value, ItemStage.IMAGE_CONFIRMED.value,
              ItemStage.NAME_SUBMITTED.value, ItemStage.NAME_SUBMITTED.value,
              ItemStage.COMPLETED.value]
    for index in range(completed):
        for stage in stages:
            app.notify(index, stage)
    return app, {"queue_rows": count, "progress_events": completed * len(stages),
                 "record_visits": app.current_job.items.visits,
                 "control_refreshes": app.controls_refreshes,
                 "row_updates": len(app.tree.updates),
                 "selection_updates": app.tree.selection_updates,
                 "preview_requests": len(app.previews),
                 "seconds": time.perf_counter() - started}


class TestGuiProgressPerformance(unittest.TestCase):
    def test_large_queue_updates_preserve_rows_and_logs_without_rescanning_per_event(self):
        app, measured = workload()
        self.assertEqual(measured["row_updates"], 100)
        self.assertEqual(len(app.logs), 100)
        self.assertEqual(measured["preview_requests"], 20)
        self.assertEqual(measured["selection_updates"], 20)
        self.assertLessEqual(measured["record_visits"], 50000)
        self.assertEqual(measured["control_refreshes"], 1)
        self.assertEqual(app.kpi_val_labels["done"].text, "20 张")

    def test_duplicate_stage_notifications_do_not_double_count_completion(self):
        app = Harness(3)
        for stage in (ItemStage.COMPLETED.value, ItemStage.COMPLETED.value,
                      ItemStage.NEEDS_MANUAL_VERIFY.value, ItemStage.COMPLETED.value):
            app.notify(0, stage)
        self.assertEqual(app.kpi_val_labels["done"].text, "1 张")
        self.assertEqual(app.kpi_val_labels["verify"].text, "0 张")

    def test_paused_manual_changes_are_recounted(self):
        app = Harness(3)
        app.notify(0, ItemStage.COMPLETED.value)
        app._run_active = False
        app.current_job.items[1].stage = ItemStage.NEEDS_MANUAL_VERIFY.value
        app._update_kpi_dashboard()
        self.assertEqual(app.kpi_val_labels["done"].text, "1 张")
        self.assertEqual(app.kpi_val_labels["verify"].text, "1 张")

    def test_new_job_does_not_reuse_old_stage_counts(self):
        app = Harness(3)
        app.notify(0, ItemStage.COMPLETED.value)
        app.current_job = record(4)
        app._update_kpi_dashboard()
        self.assertEqual(app.kpi_val_labels["done"].text, "0 张")
        app.notify(2, ItemStage.COMPLETED.value)
        self.assertEqual(app.kpi_val_labels["done"].text, "1 张")

    def test_stale_queued_progress_does_not_change_a_new_job(self):
        app = Harness(2)
        old = app.current_job.items[0]
        old.stage = ItemStage.COMPLETED.value
        app._on_engine_progress(old, "old callback")
        app.current_job = record(4)
        app.root.flush()
        self.assertEqual(app.logs, [])
        self.assertEqual(app.tree.updates, [])

    def test_clear_evidence_releases_the_last_confirmed_source(self):
        app = WorkflowHarness()
        app.adapter._last_confirmed_source = object()
        app._clear_task_evidence()
        self.assertIsNone(app.adapter._last_confirmed_source)

    def test_timing_log_path_is_reported_once_at_run_end(self):
        app = WorkflowHarness()
        app.current_job = record(2)
        app.engine.last_performance_log = Path("old-run/timings.jsonl")
        original_run = app.engine.run_job

        def run_with_timings(*args, **kwargs):
            result = original_run(*args, **kwargs)
            app.engine.last_performance_log = Path("offline-performance/timings.jsonl")
            return result

        app.engine.run_job = run_with_timings
        ControlledThread.created = []
        with patch("wechat_sender.gui_workflow.threading.Thread", ControlledThread), patch(
            "wechat_sender.gui_workflow.messagebox"
        ), patch("wechat_sender.gui._uia_thread_scope", return_value=nullcontext()):
            app.start_or_resume()
            ControlledThread.created[-1].run()
            app.root.flush()
        lines = [line for line in app.logs if "耗时记录：" in line]
        self.assertEqual(lines, ["耗时记录：offline-performance\\timings.jsonl"])

    def test_failure_before_job_entry_does_not_report_old_timings(self):
        app = WorkflowHarness()
        app.current_job = record(2)
        app.engine.last_performance_log = Path("old-run/timings.jsonl")
        ControlledThread.created = []
        with patch("wechat_sender.gui_workflow.threading.Thread", ControlledThread), patch(
            "wechat_sender.gui_workflow.messagebox"
        ), patch("wechat_sender.gui._uia_thread_scope", side_effect=RuntimeError("offline COM failure")):
            app.start_or_resume()
            ControlledThread.created[-1].run()
            app.root.flush()
        self.assertIsNone(app.engine.last_performance_log)
        self.assertEqual(app.engine.received, [])
        self.assertFalse(any("耗时记录：" in line for line in app.logs))


if __name__ == "__main__":
    print(json.dumps(workload()[1], ensure_ascii=False))
    unittest.main()
