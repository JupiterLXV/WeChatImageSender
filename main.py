#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信图片顺序发送助手 v3.6.3 入口程序
支持：
- GUI 模式：`python main.py`
- 演示模式：`python main.py --demo`（或 `--demo --headless` 在无显示器环境自动跑完演示队列）
- 诊断模式：`python main.py --diagnose [--diagnose-output diagnostic.json]`
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from wechat_sender.image_utils import scan_image_folder
from wechat_sender.job_store import SequentialSendEngine
from wechat_sender.wechat_adapter import DemoWeChatAdapter, WindowsWeChatAdapter


def run_diagnose(output_path: str | None, demo: bool = False) -> int:
    adapter = DemoWeChatAdapter() if demo else WindowsWeChatAdapter(enable_accessibility=False)
    info = adapter.diagnose()
    payload = asdict(info)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
    return 0


def run_headless_demo() -> int:
    print("=== 微信图片顺序发送助手 v3.6.3 演示队列（Headless 模式）===")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        for idx, color in [(1, (220, 60, 60)), (2, (60, 180, 90)), (10, (60, 100, 220))]:
            img = Image.new("RGB", (320, 200), color)
            img.save(tmp_path / f"{idx}_测试样张.jpg", format="JPEG")

        items, _ = scan_image_folder(tmp_path, include_extension=False)
        adapter = DemoWeChatAdapter(simulate_fast_ms=30)
        engine = SequentialSendEngine(
            adapter,
            jobs_dir=tmp_path / "jobs",
            on_progress=lambda it, msg: print(f"  [进度 #{it.index + 1}] {it.display_name} -> {it.stage}: {msg}"),
        )
        job = engine.create_job(tmp_path, items, target_chat="文件传输助手", clipboard_mode="CF_HDROP")
        ok, summary = engine.run_job(job, verify_timeout_sec=3.0, stable_duration_sec=0.05)
        print(f"结果: {'成功' if ok else '暂停'} - {summary}")
        return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="微信图片顺序发送助手 v3.6.3")
    parser.add_argument("--demo", action="store_true", help="以离线演示模式启动，不连接真实微信")
    parser.add_argument("--headless", action="store_true", help="配合 --demo 在命令行直接演示完整队列")
    parser.add_argument("--diagnose", action="store_true", help="只读检测微信版本与窗口状态，不修改内存、不发消息")
    parser.add_argument("--diagnose-output", type=str, default=None, help="将诊断结果保存为指定 JSON 文件")
    args = parser.parse_args()

    if args.diagnose:
        return run_diagnose(args.diagnose_output, demo=args.demo)

    if args.demo and args.headless:
        return run_headless_demo()

    import tkinter as tk
    from wechat_sender.gui import SenderAppGUI

    from wechat_sender.wechat_adapter import enable_windows_dpi_awareness
    enable_windows_dpi_awareness()
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WeChatImageSender.Desktop.3.6.3")
        except OSError:
            pass
    root = tk.Tk()
    _app = SenderAppGUI(root, demo_mode=args.demo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
