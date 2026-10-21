# -*- coding: utf-8 -*-
"""
发送队列状态机与断点续传持久化模块：
严格按照「待发送 -> 图片已提交待核实 -> 图片已确认待发名称 -> 名称已提交待核实 -> 已完成」顺序推进。
任何阶段不明时保持 PENDING_VERIFY 暂停状态，绝不盲目自动重发。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

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
    支持先写日志后执行操作、SHA-256 发送前复核、F8 停止信号、以及待核实项人工恢复决策。
    """

    def __init__(
        self,
        adapter: BaseWeChatAdapter,
        jobs_dir: Optional[Path] = None,
        on_progress: Optional[Callable[[JobRecordItem, str], None]] = None,
    ) -> None:
        self.adapter = adapter
        self.jobs_dir = Path(jobs_dir) if jobs_dir else get_default_jobs_dir()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.on_progress = on_progress
        self.stop_requested = False

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
            item.last_message = "人工确认图片已发出，准备仅补发名称"
        elif decision == ManualDecision.MARK_COMPLETED:
            item.stage = ItemStage.COMPLETED.value
            item.last_message = "人工确认图片与名称均已发出，标记完成"
        elif decision == ManualDecision.RETRY_FULL_ITEM:
            item.stage = ItemStage.PENDING.value
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
        verify_timeout_sec: float = 25.0,
        stable_duration_sec: float = 1.5,
    ) -> Tuple[bool, str]:
        """
        顺序执行队列任务：
        返回 (是否全部完成, 停止或完成原因说明)。
        """
        self.stop_requested = False

        # 若队列中存在尚未处理的 NEEDS_MANUAL_VERIFY 项，拒绝越过它继续发送
        for item in job.items:
            if item.stage == ItemStage.NEEDS_MANUAL_VERIFY.value:
                return False, f"第 {item.index + 1} 张（{item.display_name}）处于待人工核实状态，请先核实后再继续"

        for item in job.items:
            if item.stage == ItemStage.COMPLETED.value:
                continue

            if self.stop_requested:
                self.save_job(job)
                return False, "用户已按停止键暂停队列"

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

            current_sha = compute_sha256(img_path)
            if current_sha != item.sha256:
                item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                self._notify(item, "发送前发现源图片 SHA-256 发生变化，已暂停以防错发")
                self.save_job(job)
                return False, item.last_message

            # 阶段 1：如果尚未确认图片发送成功，则发送图片并等待视觉确认
            if item.stage in (ItemStage.PENDING.value, ItemStage.IMAGE_SUBMITTED.value):
                if item.stage == ItemStage.IMAGE_SUBMITTED.value:
                    # 上次运行在提交图片后中断，不能自动重发，转为待人工核实
                    item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                    self._notify(item, "上次图片提交后状态未确认，请先在微信核实是否已发出")
                    self.save_job(job)
                    return False, item.last_message

                # 先写入 IMAGE_SUBMITTED 状态再执行粘贴发送
                item.stage = ItemStage.IMAGE_SUBMITTED.value
                self._notify(item, f"正在发送图片：{item.relative_name} ({job.clipboard_mode})")
                self.save_job(job)

                self.adapter.send_image(img_path, job.clipboard_mode, job.send_shortcut)

                status = self.adapter.verify_image_sent(
                    img_path,
                    timeout_sec=verify_timeout_sec,
                    stable_duration_sec=stable_duration_sec,
                )
                if not status.matched or status.has_failure_badge or not status.is_upload_complete:
                    item.stage = ItemStage.NEEDS_MANUAL_VERIFY.value
                    self._notify(item, f"图片状态未确认：{status.reason}")
                    self.save_job(job)
                    return False, item.last_message

                item.stage = ItemStage.IMAGE_CONFIRMED.value
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
