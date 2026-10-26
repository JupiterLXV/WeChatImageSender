# -*- coding: utf-8 -*-
"""
发送队列状态机与断点续传持久化模块（v3.5 Pro 带系统剪贴板无感备份与恢复）：
严格按照「待发送 -> 图片已提交待核实 -> 图片已确认待发名称 -> 名称已提交待核实 -> 已完成」顺序推进。
任何阶段不明时保持 NEEDS_MANUAL_VERIFY 暂停状态，绝不盲目自动重发。
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .image_utils import ImageItem, compute_sha256
from .wechat_adapter import BaseWeChatAdapter


class ItemStage(str, Enum):
    PENDING = "PENDING"                         # 尚未发送
    IMAGE_SUBMITTED = "IMAGE_SUBMITTED"         # 图片已粘贴提交，等待上传完成确认
    IMAGE_CONFIRMED = "IMAGE_CONFIRMED"         # 图片上传已确认完成，待发送名称
    NAME_SUBMITTED = "NAME_SUBMITTED"           # 名称已提交，待最终确认
    COMPLETED = "COMPLETED"                     # 图片与名称均已确认完成
    NEEDS_MANUAL_VERIFY = "NEEDS_MANUAL_VERIFY" # 状态不明，暂停等待人工核实


class ManualDecision(str, Enum):
    SEND_NAME_ONLY = "SEND_NAME_ONLY"           # 图片已成功，仅补发名称
    MARK_COMPLETED = "MARK_COMPLETED"           # 文件名实际上已发出，标记本张完成
    RETRY_FULL_ITEM = "RETRY_FULL_ITEM"         # 确认未发出，重新发送图片+名称


@dataclass
class JobRecordItem:
    index: int
    file_path: str
    relative_name: str
    display_name: str
    sha256: str
    stage: str = ItemStage.PENDING.value
    last_message: str = ""
    updated_at: float = field(default_factory=time.time)
    image_confirmation: str = ""


@dataclass
class JobState:
    job_id: str
    folder_path: str
    target_chat: str
    clipboard_mode: str  # "CF_HDROP" or "CF_DIB"
    send_shortcut: str   # "ENTER" or "CTRL+ENTER"
    include_extension: bool
    include_subfolders: bool
    created_at: float
    items: List[JobRecordItem]

    def to_dict(self) -> Dict[str, object]:
        return {
            "job_id": self.job_id,
            "folder_path": self.folder_path,
            "target_chat": self.target_chat,
            "clipboard_mode": self.clipboard_mode,
            "send_shortcut": self.send_shortcut,
            "include_extension": self.include_extension,
            "include_subfolders": self.include_subfolders,
            "created_at": self.created_at,
            "items": [asdict(it) for it in self.items],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "JobState":
        raw_items = data.get("items", [])
        items = [JobRecordItem(**it) for it in raw_items]  # type: ignore[arg-type]
        return cls(
            job_id=str(data["job_id"]),
            folder_path=str(data["folder_path"]),
            target_chat=str(data["target_chat"]),
            clipboard_mode=str(data.get("clipboard_mode", "CF_HDROP")),
            send_shortcut=str(data.get("send_shortcut", "ENTER")),
            include_extension=bool(data.get("include_extension", False)),
            include_subfolders=bool(data.get("include_subfolders", False)),
            created_at=float(data.get("created_at", time.time())),
            items=items,
        )


def get_default_jobs_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data) / "WeChatImageSender" / "jobs"
    else:
        base = Path.home() / ".wechat_image_sender" / "jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base


class SequentialSendEngine:
    """
    核心顺序发送引擎：
    支持先写日志后执行操作、SHA-256 发送前复核、F8 停止信号、剪贴板自动备份还原、以及待核实项人工恢复决策。
    """

    def __init__(
        self,
        adapter: BaseWeChatAdapter,
        jobs_dir: Optional[Path] = None,
        on_progress: Optional[Callable[[JobRecordItem, str], None]] = None,
        restore_clipboard_on_finish: bool = True,
    ) -> None:
        self.adapter = adapter
        self.jobs_dir = Path(jobs_dir) if jobs_dir else get_default_jobs_dir()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.on_progress = on_progress
        self.stop_requested = False
        self.restore_clipboard_on_finish = restore_clipboard_on_finish

    def create_job(
        self,
        folder_path: Path,
        image_items: List[ImageItem],
        target_chat: str,
        clipboard_mode: str = "CF_HDROP",
        send_shortcut: str = "ENTER",
        include_extension: bool = False,
        include_subfolders: bool = False,
    ) -> JobState:
        job_id = f"job_{int(time.time() * 1000)}"
        records = [
            JobRecordItem(
                index=idx,
                file_path=str(item.path),
                relative_name=item.relative_name,
                display_name=item.display_name,
                sha256=item.sha256,
            )
            for idx, item in enumerate(image_items)
        ]
        job = JobState(
            job_id=job_id,
            folder_path=str(folder_path),
            target_chat=target_chat,
            clipboard_mode=clipboard_mode,
            send_shortcut=send_shortcut,
            include_extension=include_extension,
            include_subfolders=include_subfolders,
            created_at=time.time(),
            items=records,
        )
        self.save_job(job)
        return job

    def save_job(self, job: JobState) -> Path:
        out_path = self.jobs_dir / f"{job.job_id}.json"
        tmp_path = self.jobs_dir / f"{job.job_id}.json.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(job.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(out_path)
        return out_path

    def load_job(self, job_file: Path) -> JobState:
        with open(job_file, "r", encoding="utf-8") as f:
            return JobState.from_dict(json.load(f))

    def apply_manual_decision(
        self,
        job: JobState,
        item_index: int,
        decision: ManualDecision,
    ) -> JobRecordItem:
        """处理人工核实决定，更新状态并立即落盘。"""
        item = job.items[item_index]
        if item.stage != ItemStage.NEEDS_MANUAL_VERIFY.value:
            raise ValueError(f"条目 #{item_index} 当前不是待核实状态 ({item.stage})")

        if decision == ManualDecision.SEND_NAME_ONLY:
            item.stage = ItemStage.IMAGE_CONFIRMED.value
            item.image_confirmation = "manual"
            item.last_message = "人工确认图片已发出，准备仅补发名称"
        elif decision == ManualDecision.MARK_COMPLETED:
            item.stage = ItemStage.COMPLETED.value
            item.last_message = "人工确认图片与名称均已发出，标记完成"
        elif decision == ManualDecision.RETRY_FULL_ITEM:
            item.stage = ItemStage.PENDING.value
            item.image_confirmation = ""
            item.last_message = "人工确认未发出，重置为待发送"
        else:
            raise ValueError(f"未知的人工决策: {decision}")

        item.updated_at = time.time()
        self.save_job(job)
        return item

    def _notify(self, item: JobRecordItem, message: str) -> None:
        item.last_message = message
        item.updated_at = time.time()
        if self.on_progress:
            self.on_progress(item, message)

    def run_job(
        self,
        job: JobState,
        verify_timeout_sec: float = 60.0,
        stable_duration_sec: float = 1.5,
        reset_stop: bool = True,
    ) -> Tuple[bool, str]:
        """
        顺序执行队列任务（进入前备份系统剪贴板，退出时自动还原）：
        返回 (是否全部完成, 停止或完成原因说明)。
        """
        if reset_stop:
            self.stop_requested = False

        for item in job.items:
            if item.stage == ItemStage.IMAGE_CONFIRMED.value and item.image_confirmation not in ("visual-v2", "manual"):
                item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                self._notify(item, "旧版图片确认记录需要重新核实；未自动补发名称或重发图片")
                self.save_job(job)
            if item.stage in (ItemStage.IMAGE_SUBMITTED.value, ItemStage.NAME_SUBMITTED.value):
                phase = "图片" if item.stage == ItemStage.IMAGE_SUBMITTED.value else "名称"
                item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                self._notify(item, f"上次{phase}提交后状态未确认，请先在微信核实是否已发出")
                self.save_job(job)
            if item.stage == ItemStage.NEEDS_MANUAL_VERIFY.value:
                return False, f"第 {item.index + 1} 张（{item.display_name}）处于待人工核实状态，请先核实后再继续"
            if item.stage not in {stage.value for stage in ItemStage}:
                item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                self._notify(item, "任务阶段无法识别，请先人工核实")
                self.save_job(job)
                return False, item.last_message

        clip_mgr = getattr(self.adapter, "clipboard", None)
        saved_clipboard = None
        active_item: Optional[JobRecordItem] = None
        previous_stop_callback = self.adapter.stop_callback
        self.adapter.set_stop_callback(lambda: self.stop_requested)

        try:
            if self.restore_clipboard_on_finish and clip_mgr is not None:
                saved_clipboard = clip_mgr.snapshot()
            for item in job.items:
                if item.stage == ItemStage.COMPLETED.value:
                    continue
                active_item = item

                if self.stop_requested:
                    self.save_job(job)
                    return False, "用户已按停止键 (F8) 暂停队列"

                ok_fg, fg_msg = self.adapter.ensure_foreground_and_target(job.target_chat)
                if not ok_fg:
                    self.save_job(job)
                    return False, fg_msg

                img_path = Path(item.file_path)
                if not img_path.exists():
                    item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                    self._notify(item, f"源图片文件不存在: {img_path}")
                    self.save_job(job)
                    return False, item.last_message

                current_sha = compute_sha256(img_path, fast_sample=True)
                if current_sha != item.sha256:
                    # 兼容早期完整 SHA-256 记录
                    full_sha = compute_sha256(img_path, fast_sample=False)
                    if full_sha != item.sha256:
                        item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                        self._notify(item, "发送前发现源图片内容发生变化，已暂停以防错发")
                        self.save_job(job)
                        return False, item.last_message

                # 阶段 1：如果尚未确认图片发送成功，则发送图片并等待视觉确认
                if item.stage in (ItemStage.PENDING.value, ItemStage.IMAGE_SUBMITTED.value):
                    if item.stage == ItemStage.IMAGE_SUBMITTED.value:
                        item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                        self._notify(item, "上次图片提交后状态未确认，请先在微信核实是否已发出")
                        self.save_job(job)
                        return False, item.last_message

                    item.stage = ItemStage.IMAGE_SUBMITTED.value
                    self._notify(item, f"正在发送图片：{item.relative_name} ({job.clipboard_mode})")
                    self.save_job(job)

                    self.adapter.send_image(img_path, job.clipboard_mode, job.send_shortcut)

                    status = self.adapter.verify_image_sent(
                        img_path,
                        timeout_sec=verify_timeout_sec,
                        stable_duration_sec=stable_duration_sec,
                    )
                    if not self.adapter.image_status_allows_name(status):
                        item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                        self._notify(item, f"图片状态未确认：{status.reason}")
                        self.save_job(job)
                        return False, item.last_message

                    item.stage = ItemStage.IMAGE_CONFIRMED.value
                    item.image_confirmation = "visual-v2"
                    self._notify(item, f"图片上传已确认完成：{status.reason}")
                    self.save_job(job)

                if self.stop_requested:
                    self.save_job(job)
                    return False, "用户已在图片确认后暂停，下次可仅补发名称"

                # 阶段 2：图片已确认完成，发送图片名称
                if item.stage == ItemStage.IMAGE_CONFIRMED.value:
                    ok_fg, fg_msg = self.adapter.ensure_foreground_and_target(job.target_chat)
                    if not ok_fg:
                        self.save_job(job)
                        return False, fg_msg

                    if item.image_confirmation != "manual":
                        ready, reason = self.adapter.confirm_image_before_name(img_path)
                        if not ready:
                            item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                            self._notify(item, reason)
                            self.save_job(job)
                            return False, reason

                    if self.stop_requested:
                        self.save_job(job)
                        return False, "用户已在发送名称前暂停"

                    item.stage = ItemStage.NAME_SUBMITTED.value
                    self._notify(item, f"正在发送名称：{item.display_name}")
                    self.save_job(job)

                    ok_txt, txt_msg = self.adapter.send_text_and_verify(item.display_name, job.send_shortcut)
                    if not ok_txt:
                        item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                        self._notify(item, f"名称发送未确认：{txt_msg}")
                        self.save_job(job)
                        return False, item.last_message

                    item.stage = ItemStage.COMPLETED.value
                    self._notify(item, f"已完成：{item.display_name}")
                    self.save_job(job)

            return True, f"全部 {len(job.items)} 张图片及名称已按顺序发送并确认完成！"
        except Exception as exc:
            message = f"发送发生异常，队列已暂停：{exc}"
            if active_item is not None:
                # 已提交的操作可能确实发出，保留人工核实状态，绝不自动重发。
                if active_item.stage in (ItemStage.IMAGE_SUBMITTED.value, ItemStage.NAME_SUBMITTED.value):
                    active_item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                active_item.last_message = message
                active_item.updated_at = time.time()
            self.save_job(job)
            if active_item is not None and self.on_progress:
                try:
                    self.on_progress(active_item, message)
                except Exception:
                    pass
            return False, message
        finally:
            self.adapter.set_stop_callback(previous_stop_callback)
            if saved_clipboard is not None and clip_mgr is not None:
                try:
                    clip_mgr.restore(saved_clipboard)
                except Exception:
                    # 剪贴板恢复失败不能覆盖已经落盘的发送结果。
                    pass
