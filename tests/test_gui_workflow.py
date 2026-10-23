"""Offline regressions for queue identity, resume targeting and worker races."""

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from wechat_sender.gui_workflow import SenderWorkflowMixin
from wechat_sender.image_utils import ImageItem
from wechat_sender.job_store import ItemStage, JobRecordItem, JobState
from wechat_sender.wechat_adapter import BaseWeChatAdapter


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def configure(self, **options):
        self.__dict__.update(options)

    def __setitem__(self, key, value):
        setattr(self, key, value)


class UIQueue:
    def __init__(self):
        self.callbacks = []

    def after(self, _delay, callback):
        self.callbacks.append(callback)

    def flush(self):
        while self.callbacks:
            self.callbacks.pop(0)()


class ControlledThread:
    created = []

    def __init__(self, target, **_options):
        self.target = target
        self.alive = False
        self.created.append(self)

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def run(self):
        try:
            self.target()
        finally:
            self.alive = False


def image(name):
    return ImageItem(Path(name), name, Path(name).stem, "digest", 12, 8, "PNG")


def job(target="Saved recipient", stages=None):
    stages = stages or [ItemStage.PENDING.value]
    return JobState("offline-test", ".", target, "CF_HDROP", "ENTER", False, False, 0,
                    [JobRecordItem(i, f"{i}.png", f"{i}.png", str(i), "digest", stage)
                     for i, stage in enumerate(stages)])


class FakeEngine:
    jobs_dir = Path(".")

    def __init__(self):
        self.received = []
        self.created = []
        self.stop_requested = False
        self.error = None

    def create_job(self, **options):
        self.created.append(options)
        result = job(options["target_chat"])
        result.items = [JobRecordItem(i, str(it.path), it.relative_name, it.display_name, it.sha256)
                        for i, it in enumerate(options["image_items"])]
        return result

    def run_job(self, record, **_options):
        self.received.append(record)
        self.run_options = _options
        if self.error:
            raise self.error
        return True, "Offline complete"

    def load_job(self, _path):
        return self.loaded


class LegacyStub:
    def _get_effective_display_name(self, index, item):
        return self.custom_names.get(index, item.display_name)

    def _refresh_tree_view(self):
        self.refreshes += 1

    _refresh_tree_from_job = _refresh_tree_view

    def _show_preview_for_index(self, index):
        self.previews.append(index)

    def _finish_scan_results(self, items, skipped):
        self.scanned_items = items
        self.skipped = skipped

    def choose_folder(self):
        self.edits.append("choose_folder")

    def choose_files(self):
        self.edits.append("choose_files")

    def _on_files_dropped(self, _paths):
        self.edits.append("drop")

    def rescan_folder(self, sync=False):
        self.edits.append("rescan")

    def apply_custom_name(self):
        self.edits.append("rename")

    def apply_batch_template(self):
        self.edits.append("batch")

    def move_selected(self, _delta):
        self.edits.append("move")

    def remove_selected(self):
        self.edits.append("remove")

    def handle_needs_verify(self):
        self.edits.append("verify")


class Harness(SenderWorkflowMixin, LegacyStub):
    def __init__(self):
        self.root = UIQueue()
        self.worker_thread = None
        self._closing = False
        self.current_job = None
        self.scanned_items = []
        self.custom_names = {}
        self._sort_col, self._sort_reverse = "idx", False
        self._run_start_ts = None
        self.status_var = Value()
        self.chat_combo = Value("Draft recipient")
        for key, initial in {
            "folder_var": ".", "ext_var": False, "subfolders_var": False,
            "clip_mode_var": "CF_HDROP", "shortcut_var": "ENTER", "hud_enable_var": False,
            "filter_stage_var": "ALL", "search_query_var": "", "edit_name_var": "", "meta_var": "",
        }.items():
            setattr(self, key, Value(initial))
        self.kpi_val_labels = {key: Value() for key in ("total", "done", "verify", "speed")}
        self.progress_var = Value()
        self.engine = FakeEngine()
        self.adapter = SimpleNamespace(quiet_confirm_sec=1.5)
        self.demo_mode = True
        self.refreshes = 0
        self.previews, self.edits, self.logs = [], [], []
        self.tree = SimpleNamespace(selection_set=lambda _index: None, selection=lambda: (), exists=lambda _index: True)
        self._workflow_state()

    def _append_log(self, text):
        self.logs.append(text)

    def _on_params_change(self):
        pass

    def _hide_mini_hud(self):
        pass

    def _set_connection_pill(self, connected, text):
        self.connected = connected
        self.connection_text = text


class TestWorkflowInvariants(unittest.TestCase):
    def setUp(self):
        ControlledThread.created = []
        self.app = Harness()
        self.thread_patch = patch("wechat_sender.gui_workflow.threading.Thread", ControlledThread)
        self.thread_patch.start()
        self.dialog_patch = patch("wechat_sender.gui_workflow.messagebox")
        self.dialog_patch.start()

    def tearDown(self):
        self.thread_patch.stop()
        self.dialog_patch.stop()

    def test_header_sort_changes_actual_job_order_and_keeps_names_with_files(self):
        self.app.scanned_items = [image("10.png"), image("2.png"), image("1.png")]
        self.app.custom_names = {0: "Ten custom", 2: "One custom"}
        self.app._sort_tree_by_column("rel_name")
        self.assertEqual([it.path.name for it in self.app.scanned_items], ["1.png", "2.png", "10.png"])
        self.app.start_or_resume()
        saved = self.app.engine.created[0]["image_items"]
        self.assertEqual([(it.path.name, it.display_name) for it in saved],
                         [("1.png", "One custom"), ("2.png", "2"), ("10.png", "Ten custom")])

    def test_name_sort_and_reverse_reorder_all_items_including_filtered_rows(self):
        self.app.scanned_items = [image("a.png"), image("b.png"), image("c.png")]
        self.app.custom_names = {0: "Label 10", 1: "Label 2", 2: "Label 1"}
        self.app.search_query_var.set("b")
        self.app._sort_tree_by_column("send_name")
        self.assertEqual([it.path.name for it in self.app.scanned_items], ["c.png", "b.png", "a.png"])
        self.app._sort_tree_by_column("send_name")
        self.assertEqual([it.path.name for it in self.app.scanned_items], ["a.png", "b.png", "c.png"])

    def test_sort_keeps_preview_attached_to_the_selected_file(self):
        self.app.scanned_items = [image("10.png"), image("2.png"), image("1.png")]
        selected = []
        self.app.tree.selection = lambda: ("0",)
        self.app.tree.selection_set = selected.append
        self.app._sort_tree_by_column("rel_name")
        self.assertEqual(selected, ["2"])
        self.assertEqual(self.app.scanned_items[self.app.previews[-1]].path.name, "10.png")

    def test_resuming_uses_saved_target_even_if_combobox_text_was_changed(self):
        saved = job("Correct saved chat")
        self.app.current_job = saved
        self.app.chat_combo.set("Wrong draft chat")
        self.app.start_or_resume()
        self.assertEqual(self.app.chat_combo.get(), "Correct saved chat")
        self.assertEqual(self.app.chat_combo.state, "disabled")
        ControlledThread.created[-1].run()
        self.app.root.flush()
        self.assertIs(self.app.engine.received[0], saved)
        self.assertEqual(self.app.engine.received[0].target_chat, "Correct saved chat")
        self.assertFalse(self.app.engine.run_options["reset_stop"])

    def test_all_queue_mutations_are_blocked_during_a_run(self):
        self.app._run_active = True
        self.app.current_job = job()
        for action, args in [("choose_folder", ()), ("choose_files", ()), ("rescan_folder", ()),
                             ("_on_files_dropped", ([Path("one.png")],)), ("apply_custom_name", ()),
                             ("apply_batch_template", ()), ("move_selected", (1,)),
                             ("remove_selected", ()), ("handle_needs_verify", ())]:
            getattr(self.app, action)(*args)
        self.assertEqual(self.app.edits, [])
        original = self.app.current_job
        self.app.new_task()
        self.app._finish_scan_results([image("late.png")], [])
        self.assertIs(self.app.current_job, original)

    def test_persisted_order_and_names_stay_locked_after_pause(self):
        saved = job()
        self.app.current_job = saved
        self.app.scanned_items = [image("2.png"), image("1.png")]
        self.app._sort_tree_by_column("rel_name")
        self.app.apply_custom_name()
        self.assertIs(self.app.current_job, saved)
        self.assertEqual([it.path.name for it in self.app.scanned_items], ["2.png", "1.png"])
        self.assertEqual(self.app.edits, [])

    def test_slow_older_scan_cannot_replace_newer_result(self):
        def scan(**options):
            return [image(options["paths"][0].name)], []
        with patch("wechat_sender.gui_workflow.scan_image_paths", side_effect=scan):
            self.app._run_scan_from_paths([Path("old.png")], Path("."))
            old_worker = ControlledThread.created[-1]
            self.app._run_scan_from_paths([Path("new.png")], Path("."))
            ControlledThread.created[-1].run()
            self.app.root.flush()
            old_worker.run()
            self.app.root.flush()
        self.assertEqual([it.path.name for it in self.app.scanned_items], ["new.png"])
        self.assertFalse(self.app._scan_busy)

    def test_settings_rescan_supersedes_inflight_options_and_keeps_other_edits_locked(self):
        seen_options = []

        def scan(**options):
            seen_options.append((options["include_extension"], options["allow_animated_in_file_mode"]))
            name = "new.png" if options["include_extension"] else "old-animation.gif"
            return [image(name)], []

        def legacy_rescan(app, sync=False):
            folder = Path(app.folder_var.get())
            app._run_scan_from_paths([folder], folder, sync=sync)

        with patch.object(LegacyStub, "rescan_folder", legacy_rescan), \
                patch("wechat_sender.gui_workflow.scan_image_paths", side_effect=scan):
            self.app.rescan_folder()
            old_worker = ControlledThread.created[-1]
            self.app.ext_var.set(True)
            self.app.clip_mode_var.set("CF_DIB")
            self.app.rescan_folder()
            new_worker = ControlledThread.created[-1]
            self.assertIsNot(old_worker, new_worker)
            self.app.apply_custom_name()
            self.app.choose_files()
            self.assertEqual(self.app.edits, [])
            new_worker.run()
            self.app.root.flush()
            old_worker.run()
            self.app.root.flush()

        self.assertEqual(seen_options, [(True, False), (False, True)])
        self.assertEqual([it.path.name for it in self.app.scanned_items], ["new.png"])
        self.assertFalse(self.app._scan_busy)

    def test_settings_rescan_preserves_selected_files_and_filters_animation_without_importing_neighbors(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            selected = folder / "selected.png"
            private_neighbor = folder / "not-selected.png"
            animated = folder / "selected-animation.gif"
            Image.new("RGB", (12, 8), "blue").save(selected)
            Image.new("RGB", (12, 8), "red").save(private_neighbor)
            Image.new("RGB", (12, 8), "blue").save(
                animated, save_all=True, append_images=[Image.new("RGB", (12, 8), "white")], duration=100,
            )
            self.app.folder_var.set(str(folder))
            self.app._run_scan_from_paths([selected, animated], folder, sync=True)
            self.assertEqual({item.path.name for item in self.app.scanned_items}, {selected.name, animated.name})
            self.app.clip_mode_var.set("CF_DIB")
            self.app.ext_var.set(True)
            self.app.rescan_folder(sync=True)
            self.assertEqual([item.path.name for item in self.app.scanned_items], [selected.name])
            self.assertEqual(self.app.scanned_items[0].display_name, selected.name)
            self.assertEqual(self.app._scan_source_mode, "selection")
            self.assertEqual(self.app._scan_source_paths, (selected, animated))

    def test_explicit_folder_import_switches_from_selection_to_whole_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            selected = folder / "selected.png"
            neighbor = folder / "neighbor.png"
            Image.new("RGB", (12, 8), "blue").save(selected)
            Image.new("RGB", (12, 8), "red").save(neighbor)
            self.app.folder_var.set(str(folder))
            self.app._run_scan_from_paths([selected], folder, sync=True)
            with patch("wechat_sender.gui_workflow.filedialog.askdirectory", return_value=str(folder)):
                self.app.choose_folder()
            ControlledThread.created[-1].run()
            self.app.root.flush()
            self.assertEqual(self.app._scan_source_mode, "folder")
            self.assertEqual({item.path.name for item in self.app.scanned_items}, {selected.name, neighbor.name})

    def test_new_and_loaded_jobs_clear_import_scope(self):
        self.app._scan_source_mode = "selection"
        self.app._scan_source_paths = (Path("old.png"),)
        self.app._scan_source_base = Path(".")
        self.app.folder_var.set("old-parent")
        self.app.new_task()
        self.assertIsNone(self.app._scan_source_mode)
        self.assertEqual(self.app._scan_source_paths, ())
        self.assertEqual(self.app.folder_var.get(), "")
        self.app._scan_source_mode = "selection"
        self.app._scan_source_paths = (Path("old.png"),)
        self.app._scan_source_base = Path(".")
        self.app.engine.loaded = job()
        with patch("wechat_sender.gui_workflow.filedialog.askopenfilename", return_value="history.json"):
            self.app.load_history_job()
        self.assertIsNone(self.app._scan_source_mode)
        self.assertEqual(self.app._scan_source_paths, ())
        self.assertIsNone(self.app._scan_source_base)

    def test_loading_history_invalidates_inflight_scan_and_restores_options(self):
        with patch("wechat_sender.gui_workflow.scan_image_paths", return_value=([image("stale.png")], [])):
            self.app._run_scan_from_paths([Path("stale.png")], Path("."))
            old_worker = ControlledThread.created[-1]
            saved = job("Historical chat")
            saved.include_extension, saved.include_subfolders = True, True
            self.app.engine.loaded = saved
            with patch("wechat_sender.gui_workflow.filedialog.askopenfilename", return_value="history.json"):
                self.app.load_history_job()
            old_worker.run()
            self.app.root.flush()
        self.assertIs(self.app.current_job, saved)
        self.assertEqual(self.app.chat_combo.get(), "Historical chat")
        self.assertTrue(self.app.ext_var.get())
        self.assertTrue(self.app.subfolders_var.get())

    def test_loading_another_job_discards_previous_tasks_verification_evidence(self):
        self.app.current_job = job("Task A")
        for name in ("last_verify_screenshot", "last_verify_bbox", "last_verify_status",
                     "_pre_send_shot", "_pre_send_wal", "_last_locked_bbox", "_last_sent_image"):
            setattr(self.app.adapter, name, object())
        self.app.adapter._expected_chat = "Task A"
        self.app._verify_live_photo = object()
        saved = job("Task B", [ItemStage.NEEDS_MANUAL_VERIFY.value])
        self.app.engine.loaded = saved
        with patch("wechat_sender.gui_workflow.filedialog.askopenfilename", return_value="history.json"):
            self.app.load_history_job()
        self.assertIs(self.app.current_job, saved)
        self.assertIsNone(BaseWeChatAdapter.get_annotated_verify_screenshot(self.app.adapter))
        self.assertIsNone(self.app.adapter.last_verify_bbox)
        self.assertIsNone(self.app.adapter.last_verify_status)
        self.assertIsNone(self.app.adapter._pre_send_shot)
        self.assertIsNone(self.app.adapter._last_locked_bbox)
        self.assertEqual(self.app.adapter._expected_chat, "")
        self.assertIsNone(self.app._verify_live_photo)

    def test_new_task_clears_evidence_but_rejected_new_task_during_run_keeps_it(self):
        scene = object()
        self.app.current_job = job()
        self.app.adapter.last_verify_screenshot = scene
        self.app.adapter.last_verify_bbox = (1, 2, 3, 4)
        self.app._run_active = True
        self.app.new_task()
        self.assertIs(self.app.adapter.last_verify_screenshot, scene)
        self.app._run_active = False
        self.app.new_task()
        self.assertIsNone(BaseWeChatAdapter.get_annotated_verify_screenshot(self.app.adapter))
        self.assertIsNone(self.app.adapter.last_verify_bbox)

    def test_sending_cannot_start_with_unfinished_scan(self):
        self.app.scanned_items = [image("ready.png")]
        self.app._scan_busy = True
        self.app.start_or_resume()
        self.assertEqual(self.app.engine.created, [])
        self.assertEqual(ControlledThread.created, [])

    def test_manual_verification_required_before_worker_can_start(self):
        self.app.current_job = job(stages=[ItemStage.NEEDS_MANUAL_VERIFY.value])
        self.app.start_or_resume()
        self.assertEqual(ControlledThread.created, [])
        self.assertIn("核实", self.app.status_var.get())

    def test_speed_counts_only_this_runs_completions_and_freezes_on_pause(self):
        self.app.current_job = job(stages=[ItemStage.COMPLETED.value, ItemStage.COMPLETED.value])
        self.app._run_completed_baseline = 1
        self.app._run_start_ts, self.app._run_end_ts = 10.0, 12.0
        with patch("wechat_sender.gui_workflow.time.monotonic", return_value=99):
            self.app._update_kpi_dashboard()
        self.assertIn("2.00 秒/张", self.app.kpi_val_labels["speed"].text)
        self.assertEqual(self.app.progress_var.get(), 100)

    def test_worker_exception_unlocks_interface_and_preserves_saved_job(self):
        saved = job()
        self.app.current_job = saved
        self.app.engine.error = RuntimeError("Offline fault")
        self.app.start_or_resume()
        ControlledThread.created[-1].run()
        self.app.root.flush()
        self.assertFalse(self.app._sender_running())
        self.assertIs(self.app.current_job, saved)
        self.assertIn("Offline fault", self.app.status_var.get())

    def test_connection_check_runs_in_worker_and_never_overwrites_saved_target(self):
        calls = []
        self.app.adapter = SimpleNamespace(
            diagnose=lambda: calls.append("diagnose") or SimpleNamespace(wechat_running=True, dpi_scale_percent=100),
            list_recent_chats=lambda: ["Wrong recent"],
        )
        self.app.refresh_chats()
        self.assertEqual(calls, [])
        self.app.current_job = job("Locked saved chat")
        ControlledThread.created[-1].run()
        self.app.root.flush()
        self.assertEqual(calls, ["diagnose"])
        self.assertEqual(self.app.chat_combo.get(), "Locked saved chat")
        self.assertFalse(self.app._connection_busy)

    def test_new_task_invalidates_scan_and_preserves_engine_history(self):
        self.app.current_job = job()
        self.app.scanned_items = [image("old.png")]
        self.app.custom_names = {0: "Custom"}
        self.app._scan_busy = True
        before = self.app._scan_generation
        engine = self.app.engine
        self.app.new_task()
        self.assertIsNone(self.app.current_job)
        self.assertEqual(self.app.scanned_items, [])
        self.assertEqual(self.app.custom_names, {})
        self.assertEqual(self.app._scan_generation, before + 1)
        self.assertIs(self.app.engine, engine)
        self.assertEqual(self.app.chat_combo.state, "normal")


if __name__ == "__main__":
    unittest.main()
