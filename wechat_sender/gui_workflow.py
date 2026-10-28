"""Queue invariants and asynchronous work shared by the desktop interface.

The displayed row number is always the actual sending position. Once a job has
been persisted, its recipient, order and names stay fixed until a new draft is
created; this also keeps interrupted jobs safe to resume.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Sequence, Tuple
from tkinter import filedialog, messagebox

from .image_utils import ImageItem, natural_sort_key, scan_image_paths
from .job_store import ItemStage, JobRecordItem


class SenderWorkflowMixin:
    def _workflow_state(self) -> None:
        defaults = {
            "_scan_generation": 0,
            "_scan_busy": False,
            "_run_active": False,
            "_run_generation": 0,
            "_connection_busy": False,
            "_connection_generation": 0,
            "_connection_manual_pending": False,
            "_run_completed_baseline": 0,
            "_run_end_ts": None,
            "_last_progress_preview_index": None,
            "_scan_source_mode": None,
            "_scan_source_paths": (),
            "_scan_source_base": None,
            "_progress_counts_job": None,
            "_progress_stages": (),
            "_progress_done": 0,
            "_progress_verify": 0,
            "_dashboard_controls_key": None,
        }
        for key, value in defaults.items():
            if not hasattr(self, key):
                setattr(self, key, value)

    def _sender_running(self) -> bool:
        return bool(getattr(self, "_run_active", False) or (
            self.worker_thread and self.worker_thread.is_alive()
        ))

    def _post_ui(self, callback: Any) -> None:
        if not self._closing:
            try:
                self.root.after(0, callback)
            except Exception:
                # Closing a Tk interpreter can race the final worker callback.
                pass

    def _sync_task_controls(self) -> None:
        self._workflow_state()
        if self.current_job is not None:
            self.chat_combo.set(self.current_job.target_chat)
        try:
            self.chat_combo.configure(state="disabled" if self.current_job or self._sender_running() else "normal")
        except Exception:
            pass
        hook = getattr(self, "_refresh_task_controls", None)
        if hook:
            hook()

    def _allow_draft_edit(self) -> bool:
        self._workflow_state()
        if self._sender_running():
            self.status_var.set("发送进行中，图片队列已锁定。按 F8 可安全暂停。")
            return False
        if self._scan_busy:
            self.status_var.set("正在读取图片，请等待扫描完成。")
            return False
        if self.current_job is not None:
            self.status_var.set("当前任务的接收对象、顺序和名称已保存。需要修改时请先新建任务。")
            return False
        return True

    def _clear_task_evidence(self) -> None:
        """Never present a previous job's screenshot as the loaded job's scene."""
        for name in (
            "last_verify_screenshot", "last_verify_bbox", "last_verify_status",
            "_pre_send_shot", "_pre_send_wal", "_last_locked_bbox", "_last_sent_image",
            "_capture_signature", "_capture_backend", "_image_verify_hwnd",
            "_last_confirmed_path", "_last_confirmed_frame",
            "_last_confirmed_source",
        ):
            if hasattr(self.adapter, name):
                setattr(self.adapter, name, None)
        if hasattr(self.adapter, "_expected_chat"):
            self.adapter._expected_chat = ""
        self._verify_orig_photo = None
        self._verify_live_photo = None
        self._last_progress_preview_index = None
        self._progress_counts_job = None
        self._progress_stages = ()
        self._dashboard_controls_key = None

    def _clear_scan_source(self) -> None:
        self._scan_source_mode = None
        self._scan_source_paths = ()
        self._scan_source_base = None

    def new_task(self) -> None:
        self._workflow_state()
        if self._sender_running():
            self.status_var.set("请先按 F8 暂停，等待当前发送步骤安全结束。")
            return
        self._scan_generation += 1
        self._scan_busy = False
        self._connection_generation += 1
        self._connection_manual_pending = False
        self._clear_task_evidence()
        self._clear_scan_source()
        self.current_job = None
        self.folder_var.set("")
        self.scanned_items = []
        self.custom_names.clear()
        self._sort_col, self._sort_reverse = "idx", False
        self._run_start_ts, self._run_end_ts = None, None
        self._run_completed_baseline = 0
        self.filter_stage_var.set("ALL")
        self.search_query_var.set("")
        self.edit_name_var.set("")
        self.meta_var.set("")
        self._refresh_tree_view()
        self._show_preview_for_index(0)
        self._update_kpi_dashboard()
        self._sync_task_controls()
        self.status_var.set("已新建任务。导入图片后，确认接收对象并开始发送。历史任务记录仍保留。")
        self._append_log("已新建空白任务，原任务记录仍可从历史记录恢复。")

    def choose_folder(self) -> None:
        if not self._allow_draft_edit():
            return
        chosen = filedialog.askdirectory(title="选择图片文件夹")
        if chosen:
            folder = Path(chosen)
            self.folder_var.set(chosen)
            self._run_scan_from_paths([folder], folder)

    def choose_files(self) -> None:
        if self._allow_draft_edit():
            super().choose_files()

    def _on_files_dropped(self, paths: Sequence[Path]) -> None:
        if not self._allow_draft_edit() or not paths:
            return
        source_paths = [Path(path) for path in paths]
        base = source_paths[0] if len(source_paths) == 1 and source_paths[0].is_dir() else source_paths[0].parent
        self.folder_var.set(str(base))
        self._run_scan_from_paths(source_paths, base)

    def rescan_folder(self, sync: bool = False) -> None:
        self._workflow_state()
        # Settings can change again while a previous scan is still running.
        # The next scan owns a new generation; only its options may reach the
        # draft. Other draft mutations remain blocked until scanning finishes.
        superseding_scan = self._scan_busy and self.current_job is None and not self._sender_running()
        if superseding_scan or self._allow_draft_edit():
            folder_text = self.folder_var.get().strip()
            if (self._scan_source_mode == "selection" and self._scan_source_base is not None
                    and folder_text and Path(folder_text).absolute() == self._scan_source_base.absolute()):
                self._run_scan_from_paths(self._scan_source_paths, self._scan_source_base, sync=sync)
            else:
                super().rescan_folder(sync=sync)

    def apply_custom_name(self) -> None:
        if self._allow_draft_edit():
            super().apply_custom_name()

    def apply_batch_template(self) -> None:
        if self._allow_draft_edit():
            super().apply_batch_template()

    def move_selected(self, delta: int) -> None:
        if self._allow_draft_edit():
            self._sort_col, self._sort_reverse = "idx", False
            super().move_selected(delta)

    def remove_selected(self) -> None:
        if self._allow_draft_edit():
            super().remove_selected()

    def _run_scan_from_paths(self, paths: Sequence[Path], base_folder: Path, sync: bool = False) -> None:
        self._workflow_state()
        if self._sender_running() or self.current_job is not None:
            self._allow_draft_edit()
            return
        source_paths = tuple(Path(path) for path in paths)
        self._scan_source_paths = source_paths
        self._scan_source_base = Path(base_folder)
        self._scan_source_mode = (
            "folder" if len(source_paths) == 1 and source_paths[0].absolute() == self._scan_source_base.absolute()
            else "selection"
        )
        # Every request owns a generation. A slow earlier scan may finish after
        # a later request or after loading a historical job; its result is ignored.
        self._scan_generation += 1
        generation = self._scan_generation
        self._scan_busy = True
        self._sync_task_controls()
        options = dict(
            paths=list(source_paths), base_folder=base_folder,
            include_subfolders=self.subfolders_var.get(),
            include_extension=self.ext_var.get(),
            allow_animated_in_file_mode=self.clip_mode_var.get() == "CF_HDROP",
        )
        self.status_var.set("正在读取图片并核对文件摘要…")

        def finish(items: List[ImageItem], skipped: List[Tuple[Path, str]]) -> None:
            if generation != self._scan_generation or self._closing or self._sender_running() or self.current_job:
                return
            self._scan_busy = False
            self._finish_scan_results(items, skipped)

        def failed(reason: str) -> None:
            if generation != self._scan_generation or self._closing:
                return
            self._scan_busy = False
            self._sync_task_controls()
            self.status_var.set(f"图片读取失败：{reason}")
            messagebox.showerror("扫描失败", reason)

        def progress(cur: int, total: int, _item: ImageItem) -> None:
            if cur % 25 == 0 or cur == total:
                def update() -> None:
                    if generation == self._scan_generation and self._scan_busy:
                        self.status_var.set(f"正在读取图片（{cur}/{total}）…")
                self._post_ui(update)

        def worker() -> None:
            try:
                items, skipped = scan_image_paths(**options, progress_callback=None if sync else progress)
                if sync:
                    finish(items, skipped)
                else:
                    self._post_ui(lambda: finish(items, skipped))
            except Exception as exc:
                reason = str(exc)
                if sync:
                    failed(reason)
                else:
                    self._post_ui(lambda: failed(reason))

        if sync:
            worker()
        else:
            self._scan_thread = threading.Thread(target=worker, daemon=True, name="ImageScan")
            self._scan_thread.start()

    def _finish_scan_results(self, items: List[ImageItem], skipped: List[Tuple[Path, str]]) -> None:
        if self._sender_running() or self.current_job is not None:
            return
        self._scan_busy = False
        self._sort_col, self._sort_reverse = "idx", False
        self._run_start_ts, self._run_end_ts = None, None
        super()._finish_scan_results(items, skipped)
        self._sync_task_controls()

    def _sort_tree_by_column(self, col: str) -> None:
        if not self._allow_draft_edit():
            return
        if col not in ("idx", "rel_name", "send_name"):
            self.status_var.set("可以按文件名或发送名称调整实际发送顺序。")
            return
        reverse = not self._sort_reverse if self._sort_col == col else False
        if col == "idx":
            self._sort_col, self._sort_reverse = "idx", False
            self._refresh_tree_view()
            self.status_var.set("序号就是实际发送顺序。可按文件名、发送名称或上下移动来调整。")
            return
        else:
            def key(idx: int) -> Any:
                item = self.scanned_items[idx]
                value = item.relative_name if col == "rel_name" else self._get_effective_display_name(idx, item)
                return natural_sort_key(value)
            indices = sorted(range(len(self.scanned_items)), key=key, reverse=reverse)
        selection = self.tree.selection()
        selected_index = int(selection[0]) if selection else None
        old_items, old_names = self.scanned_items, self.custom_names
        self.scanned_items = [old_items[idx] for idx in indices]
        self.custom_names = {new: old_names[old] for new, old in enumerate(indices) if old in old_names}
        self._sort_col, self._sort_reverse = col, reverse
        self._refresh_tree_view()
        if selected_index is not None and selected_index in indices:
            new_index = indices.index(selected_index)
            if self.tree.exists(str(new_index)):
                self.tree.selection_set(str(new_index))
                self._show_preview_for_index(new_index)
        self.status_var.set("发送顺序已更新，将按列表中的序号逐张发送。")

    def _apply_row_sorting(self, rows: list) -> list:
        # Sorting changes the underlying draft, never just the visible rows.
        return sorted(rows, key=lambda row: row[0])

    def _prime_progress_counts(self, job) -> None:
        # Queue edits are locked during a run. Seed once before its worker
        # starts, then apply the immutable stage snapshots received by the UI.
        self._progress_counts_job = job
        self._progress_stages = [item.stage for item in job.items]
        self._progress_done = self._progress_stages.count(ItemStage.COMPLETED.value)
        self._progress_verify = self._progress_stages.count(ItemStage.NEEDS_MANUAL_VERIFY.value)

    def _record_progress_stage(self, job, index: int, stage: str) -> None:
        if not self._run_active or job is None:
            return
        if self._progress_counts_job is not job or len(self._progress_stages) != len(job.items):
            self._prime_progress_counts(job)
        if not 0 <= index < len(self._progress_stages):
            self._progress_counts_job = None
            return
        previous = self._progress_stages[index]
        if previous != stage:
            self._progress_done += int(stage == ItemStage.COMPLETED.value) - int(previous == ItemStage.COMPLETED.value)
            self._progress_verify += int(stage == ItemStage.NEEDS_MANUAL_VERIFY.value) - int(previous == ItemStage.NEEDS_MANUAL_VERIFY.value)
            self._progress_stages[index] = stage

    def _update_kpi_dashboard(self) -> None:
        self._workflow_state()
        records = self.current_job.items if self.current_job else []
        total = len(records) if self.current_job else len(self.scanned_items)
        if self._run_active and self.current_job is not None:
            if self._progress_counts_job is not self.current_job or len(self._progress_stages) != total:
                self._prime_progress_counts(self.current_job)
            done, verify = self._progress_done, self._progress_verify
        else:
            # Paused/manual actions can change records outside engine callbacks.
            # Recount then; the running hot path never scans the whole queue.
            done = sum(item.stage == ItemStage.COMPLETED.value for item in records)
            verify = sum(item.stage == ItemStage.NEEDS_MANUAL_VERIFY.value for item in records)
        pct = done * 100.0 / total if total else 0.0
        self.progress_var.set(pct)
        self.kpi_val_labels["total"].configure(text=f"{total} 张")
        self.kpi_val_labels["done"].configure(text=f"{done} 张")
        self.kpi_val_labels["verify"].configure(text=f"{verify} 张")
        current_done = max(0, done - self._run_completed_baseline)
        speed = f"{pct:.0f}%"
        if self._run_start_ts is not None and current_done:
            end = self._run_end_ts if self._run_end_ts is not None else time.monotonic()
            elapsed = max(0.01, end - self._run_start_ts)
            speed += f" · {elapsed / current_done:.2f} 秒/张"
        self.kpi_val_labels["speed"].configure(text=speed)
        controls_key = (id(self.current_job), self._sender_running(), self._scan_busy,
                        self._connection_busy, total, bool(total and done == total), bool(verify))
        if controls_key != self._dashboard_controls_key:
            self._dashboard_controls_key = controls_key
            self._sync_task_controls()

    def start_or_resume(self) -> None:
        self._workflow_state()
        if self._sender_running():
            return
        if self._scan_busy or self._connection_busy:
            self.status_var.set("正在读取图片或检查微信连接，请稍候再开始。")
            return
        if self.current_job and any(it.stage == ItemStage.NEEDS_MANUAL_VERIFY.value for it in self.current_job.items):
            self.status_var.set("请先处理待核实图片，再继续发送，以免重复发送。")
            return
        self._on_params_change()
        if self.current_job is None:
            target = self.chat_combo.get().strip()
            if not target:
                messagebox.showwarning("缺少接收对象", "请先选择或填写微信接收对象。")
                return
            if not self.scanned_items:
                messagebox.showwarning("列表为空", "请先导入要发送的图片。")
                return
            try:
                final_items = [replace(item, display_name=self._get_effective_display_name(i, item))
                               for i, item in enumerate(self.scanned_items)]
                self.current_job = self.engine.create_job(
                    folder_path=Path(self.folder_var.get().strip() or "."), image_items=final_items,
                    target_chat=target, clipboard_mode=self.clip_mode_var.get(),
                    send_shortcut=self.shortcut_var.get(), include_extension=self.ext_var.get(),
                    include_subfolders=self.subfolders_var.get(),
                )
            except Exception as exc:
                messagebox.showerror("无法保存发送任务", str(exc))
                return
            self._refresh_tree_from_job()
        job = self.current_job
        # A loaded job always uses its persisted recipient, independent of text
        # previously entered into the editable combobox.
        self.chat_combo.set(job.target_chat)
        if job.items and all(item.stage == ItemStage.COMPLETED.value for item in job.items):
            self.status_var.set("此任务已全部完成。需要再次发送时请新建任务。")
            self._sync_task_controls()
            return
        self._run_generation += 1
        generation = self._run_generation
        self._run_completed_baseline = sum(item.stage == ItemStage.COMPLETED.value for item in job.items)
        self._run_start_ts, self._run_end_ts = time.monotonic(), None
        self._last_progress_preview_index = None
        self._prime_progress_counts(job)
        self._dashboard_controls_key = None
        self._run_active = True
        self.engine.stop_requested = False
        if hasattr(self.engine, "last_performance_log"):
            # COM initialization can fail before run_job gets a chance to
            # clear its own record; never present a previous run's timings.
            self.engine.last_performance_log = None
        self._sync_task_controls()
        self.status_var.set(f"正在发送至：{job.target_chat}")
        self._append_log(f"开始发送 → {job.target_chat}（按照列表序号逐张核实）")
        if self.hud_enable_var.get():
            self._show_mini_hud()

        def finish(ok: bool, summary: str, ended: float) -> None:
            if generation != self._run_generation or self._closing:
                return
            self._run_active = False
            self.worker_thread = None
            self._run_end_ts = ended
            self._progress_counts_job = None
            self._progress_stages = ()
            self._hide_mini_hud()
            self._update_kpi_dashboard()
            self.status_var.set(summary)
            self._append_log(summary)
            timing_log = getattr(self.engine, "last_performance_log", None)
            if timing_log is not None:
                self._append_log(f"耗时记录：{timing_log}")
            if getattr(self, "_shutdown_pending", False):
                return
            if not ok:
                messagebox.showwarning("队列已暂停", summary)
            else:
                messagebox.showinfo("发送完成", summary)

        def run() -> None:
            try:
                from .gui import _uia_thread_scope
                with _uia_thread_scope():
                    ok, summary = self.engine.run_job(
                        job, stable_duration_sec=self.adapter.quiet_confirm_sec, reset_stop=False,
                    )
            except Exception as exc:
                # The engine persists submitted steps before each side effect;
                # resuming therefore preserves its uncertainty checks.
                ok, summary = False, f"发送已暂停：{exc}。请查看任务状态后再继续。"
            ended = time.monotonic()
            self._post_ui(lambda: finish(ok, summary, ended))

        self.worker_thread = threading.Thread(target=run, daemon=True, name="ImageSender")
        self.worker_thread.start()

    def _on_engine_progress(self, item: JobRecordItem, message: str) -> None:
        self._workflow_state()
        job = self.current_job
        index, stage, relative, name = item.index, item.stage, item.relative_name, item.display_name

        def update() -> None:
            if self._closing or self.current_job is not job:
                return
            from .gui import STAGE_BADGES
            self._record_progress_stage(job, index, stage)
            iid = str(index)
            badge = STAGE_BADGES.get(stage, stage)
            matches = self._row_matches_filter(relative, name, stage)
            if not matches and self.tree.exists(iid):
                self.tree.delete(iid)
            elif matches:
                values = (index + 1, relative, name, badge, message)
                if self.tree.exists(iid):
                    self.tree.item(iid, values=values, tags=(self._stage_to_tag(stage),))
                else:
                    position = sum(int(child) < index for child in self.tree.get_children())
                    self.tree.insert("", position, iid=iid, values=values, tags=(self._stage_to_tag(stage),))
                if self.tree.selection() != (iid,):
                    self.tree.see(iid)
                    self.tree.selection_set(iid)
            if self._last_progress_preview_index != index:
                self._show_preview_for_index(index)
                self._last_progress_preview_index = index
            self._update_kpi_dashboard()
            self._update_mini_hud(f"[{index + 1}/{len(job.items) if job else 0}] {name} · {badge}")
            self.status_var.set(f"[{index + 1}] {name}：{message}")
            self._append_log(f"[#{index + 1} {name}] {message}")

        self._post_ui(update)

    def handle_needs_verify(self) -> None:
        if self._sender_running():
            self.status_var.set("请等待发送队列安全暂停后，再处理待核实图片。")
            return
        super().handle_needs_verify()

    def load_history_job(self) -> None:
        self._workflow_state()
        if self._sender_running():
            self.status_var.set("请先按 F8 暂停，等待当前步骤安全结束后再恢复历史任务。")
            return
        chosen = filedialog.askopenfilename(
            title="恢复历史发送任务", initialdir=str(self.engine.jobs_dir), filetypes=[("任务记录", "*.json")],
        )
        if not chosen:
            return
        try:
            job = self.engine.load_job(Path(chosen))
            self._scan_generation += 1
            self._scan_busy = False
            self._connection_generation += 1
            self._clear_task_evidence()
            self._clear_scan_source()
            self.current_job = job
            self.scanned_items = []
            self.custom_names.clear()
            self._run_start_ts, self._run_end_ts = None, None
            self._run_completed_baseline = 0
            self.folder_var.set(job.folder_path)
            self.chat_combo.set(job.target_chat)
            self.clip_mode_var.set(job.clipboard_mode)
            self.shortcut_var.set(job.send_shortcut)
            self.ext_var.set(job.include_extension)
            self.subfolders_var.set(job.include_subfolders)
            self.filter_stage_var.set("ALL")
            self.search_query_var.set("")
            self._refresh_tree_from_job()
            self._sync_task_controls()
            if job.items:
                self.tree.selection_set("0")
                self._show_preview_for_index(0)
            msg = f"已恢复 {len(job.items)} 张图片的任务。接收对象已锁定：{job.target_chat}"
            self.status_var.set(msg)
            self._append_log(msg)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    def refresh_chats(self) -> None:
        self.use_current_chat()

    def use_current_chat(self) -> None:
        if self._allow_draft_edit():
            self._refresh_connection_async(use_current=True)

    def _refresh_connection_async(self, use_current: bool = False) -> None:
        self._workflow_state()
        if self._closing or getattr(self, "_shutdown_pending", False) or self._sender_running():
            return
        if self._connection_busy:
            if use_current:
                self._connection_manual_pending = True
                self.status_var.set("正在检查微信连接，完成后将刷新并定位你手动打开的聊天。")
            return
        self._connection_busy = True
        self._sync_task_controls()
        generation = self._connection_generation

        def apply(diag: Any, chats: list, current: str, error: str) -> None:
            self._connection_busy = False
            if self._closing or getattr(self, "_shutdown_pending", False) or self._sender_running():
                self._connection_manual_pending = False
                return
            if error:
                self._set_connection_pill(False, "未检测到微信 · 点击重试")
                if use_current:
                    self.status_var.set(f"微信连接检查失败：{error}")
            else:
                connected = bool(diag.wechat_running)
                label = "离线演示模式" if self.demo_mode else (
                    f"微信已连接 · {diag.dpi_scale_percent}% DPI" if connected else "未检测到微信 · 点击重试"
                )
                self._set_connection_pill(connected, label)
                # Polling only reports whether WeChat is running. The user
                # chooses a conversation in WeChat, then explicitly locates it.
                if use_current and generation == self._connection_generation and self.current_job is None:
                    if connected and current:
                        self.chat_combo["values"] = list(dict.fromkeys([current, *chats]))
                        self.chat_combo.set(current)
                        self.status_var.set(f"已定位你手动打开的微信聊天：{current}")
                        self._append_log(f"已定位当前微信聊天：{current}")
                    else:
                        self.status_var.set("请先在微信中手动打开接收对象的聊天，再点击“刷新/定位聊天”。")
            self._sync_task_controls()
            if self._connection_manual_pending:
                self._connection_manual_pending = False
                if self.current_job is None:
                    self._refresh_connection_async(use_current=True)

        def work() -> None:
            try:
                from .gui import _uia_thread_scope
                with _uia_thread_scope():
                    diag = self.adapter.diagnose()
                    chats = self.adapter.list_recent_chats() if use_current and diag.wechat_running else []
                    current = (self.adapter.get_current_chat_title() or "当前微信聊天") if use_current and diag.wechat_running else ""
                self._post_ui(lambda: apply(diag, chats, current, ""))
            except Exception as exc:
                reason = str(exc)
                self._post_ui(lambda: apply(None, [], "", reason))

        threading.Thread(target=work, daemon=True, name="WeChatConnectionCheck").start()

    def _schedule_auto_detect(self) -> None:
        if self._closing:
            return
        self._refresh_connection_async()
        self.root.after(5000, self._schedule_auto_detect)
