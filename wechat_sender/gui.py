# -*- coding: utf-8 -*-
"""
微信图片顺序发送助手 v3.4.0 现代视觉工作台界面（Modern Studio UI）。
核心 UI 升级特性：
1. 现代扁平化配色与微信绿（#07C160）视觉规范，支持「☀️ 浅色 / 🌙 深色」主题一键切换；
2. 顶部实时仪表盘（总张数、已完成、待核实、平均耗时）+ 实时可视化进度条；
3. 左右分栏工作台：左侧彩色状态标签队列（待发送/发送中/已完成/待核实自动高亮染色）+
   右侧实时图片缩略图预览卡片、文件元信息、单张发送名称在线修改、队列上移/下移/移除微调；
4. 底部带时间戳的滚动运行日志抽屉，运行脉络一目了然；
5. 窗口关闭自动复位系统无障碍标志，全局 F8 紧急停止。
"""

from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover - 兼容无 GUI 的 Headless CI 环境
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    ImageTk = None  # type: ignore[assignment]

from .image_utils import DEFAULT_SIMILARITY_THRESHOLD, ImageItem, load_static_image, scan_image_folder
from .job_store import (
    ItemStage,
    JobRecordItem,
    JobState,
    ManualDecision,
    SequentialSendEngine,
)
from .wechat_adapter import (
    DEFAULT_QUIET_CONFIRM_SEC,
    BaseWeChatAdapter,
    DemoWeChatAdapter,
    WindowsWeChatAdapter,
)


THEMES: Dict[str, Dict[str, str]] = {
    "light": {
        "bg": "#F3F5F8",
        "card_bg": "#FFFFFF",
        "card_border": "#E2E8F0",
        "header_bg": "#111827",
        "header_fg": "#FFFFFF",
        "header_sub": "#9CA3AF",
        "text": "#1E293B",
        "text_muted": "#64748B",
        "primary": "#07C160",
        "primary_hover": "#06AD56",
        "danger": "#EF4444",
        "danger_hover": "#DC2626",
        "warning": "#F59E0B",
        "secondary_btn": "#E2E8F0",
        "secondary_fg": "#1E293B",
        "tree_bg": "#FFFFFF",
        "tree_fg": "#1E293B",
        "tree_sel_bg": "#DCFCE7",
        "tree_sel_fg": "#065F46",
        "tag_pending_bg": "#FFFFFF",
        "tag_pending_fg": "#475569",
        "tag_active_bg": "#EFF6FF",
        "tag_active_fg": "#1D4ED8",
        "tag_done_bg": "#ECFDF5",
        "tag_done_fg": "#047857",
        "tag_verify_bg": "#FEF3C7",
        "tag_verify_fg": "#B45309",
        "log_bg": "#0F172A",
        "log_fg": "#E2E8F0",
        "preview_bg": "#F8FAFC",
    },
    "dark": {
        "bg": "#0F172A",
        "card_bg": "#1E293B",
        "card_border": "#334155",
        "header_bg": "#020617",
        "header_fg": "#F8FAFC",
        "header_sub": "#94A3B8",
        "text": "#F1F5F9",
        "text_muted": "#94A3B8",
        "primary": "#07C160",
        "primary_hover": "#059669",
        "danger": "#EF4444",
        "danger_hover": "#DC2626",
        "warning": "#F59E0B",
        "secondary_btn": "#334155",
        "secondary_fg": "#F1F5F9",
        "tree_bg": "#1E293B",
        "tree_fg": "#F1F5F9",
        "tree_sel_bg": "#064E3B",
        "tree_sel_fg": "#A7F3D0",
        "tag_pending_bg": "#1E293B",
        "tag_pending_fg": "#CBD5E1",
        "tag_active_bg": "#1E3A8A",
        "tag_active_fg": "#BFDBFE",
        "tag_done_bg": "#064E3B",
        "tag_done_fg": "#6EE7B7",
        "tag_verify_bg": "#78350F",
        "tag_verify_fg": "#FDE68A",
        "log_bg": "#020617",
        "log_fg": "#CBD5E1",
        "preview_bg": "#0F172A",
    },
}

STAGE_BADGES: Dict[str, str] = {
    ItemStage.PENDING.value: "⏳ 待发送",
    ItemStage.IMAGE_SUBMITTED.value: "📤 图片上传中",
    ItemStage.IMAGE_CONFIRMED.value: "🖼️ 图片已确认",
    ItemStage.NAME_SUBMITTED.value: "🔤 名称发送中",
    ItemStage.COMPLETED.value: "✅ 已完成",
    ItemStage.NEEDS_MANUAL_VERIFY.value: "⚠️ 待人工核实",
}


class SenderAppGUI:
    def __init__(self, root: Any, demo_mode: bool = False) -> None:
        if tk is None:
            raise RuntimeError("当前 Python 环境未安装 tkinter，请使用 --demo --headless 或在 Windows 标准 Python 下运行")
        self.root = root
        self.demo_mode = demo_mode
        self.theme_name = "light"
        self.colors = THEMES[self.theme_name]

        title_suffix = " [演示模式]" if demo_mode else " v3.4.0 Pro"
        self.root.title(f"微信图片顺序发送助手{title_suffix}")
        self.root.geometry("1160x780")
        self.root.minsize(1020, 680)

        self.adapter: BaseWeChatAdapter = (
            DemoWeChatAdapter(similarity_threshold=DEFAULT_SIMILARITY_THRESHOLD)
            if demo_mode
            else WindowsWeChatAdapter(enable_accessibility=True, similarity_threshold=DEFAULT_SIMILARITY_THRESHOLD)
        )
        atexit.register(self.adapter.close)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.engine = SequentialSendEngine(self.adapter, on_progress=self._on_engine_progress)
        self.scanned_items: List[ImageItem] = []
        self.custom_names: Dict[int, str] = {}
        self.current_job: Optional[JobState] = None
        self.worker_thread: Optional[threading.Thread] = None
        self._preview_photo: Any = None
        self._run_start_ts: Optional[float] = None

        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self._build_ui()
        self._apply_theme()
        self.root.bind("<F8>", lambda _e: self.request_stop())
        self.refresh_chats()
        self._append_log("系统就绪：已启用零内存补丁 UIA、CF_HDROP 原图句柄通道与 60ms 极速锁定识别。")

    def _on_close(self) -> None:
        try:
            self.engine.stop_requested = True
            self.adapter.close()
        finally:
            self.root.destroy()

    def _configure_styles(self) -> None:
        c = self.colors
        font_family = "Microsoft YaHei UI"
        self.root.configure(bg=c["bg"])

        self.style.configure(".", font=(font_family, 9), background=c["bg"], foreground=c["text"])
        self.style.configure("Card.TFrame", background=c["card_bg"], relief=tk.FLAT)
        self.style.configure("Bg.TFrame", background=c["bg"])
        self.style.configure(
            "CardTitle.TLabel",
            background=c["card_bg"],
            foreground=c["text"],
            font=(font_family, 10, "bold"),
        )
        self.style.configure(
            "CardBody.TLabel",
            background=c["card_bg"],
            foreground=c["text"],
            font=(font_family, 9),
        )
        self.style.configure(
            "CardMuted.TLabel",
            background=c["card_bg"],
            foreground=c["text_muted"],
            font=(font_family, 8),
        )
        self.style.configure(
            "Card.TCheckbutton",
            background=c["card_bg"],
            foreground=c["text"],
            font=(font_family, 9),
        )
        self.style.configure(
            "Card.TRadiobutton",
            background=c["card_bg"],
            foreground=c["text"],
            font=(font_family, 9),
        )

        # 按钮样式：主按钮（微信绿）、危险按钮（珊瑚红）、警告按钮（琥珀黄）、普通按钮
        self.style.configure(
            "Primary.TButton",
            background=c["primary"],
            foreground="#FFFFFF",
            font=(font_family, 10, "bold"),
            padding=(16, 7),
            borderwidth=0,
        )
        self.style.map(
            "Primary.TButton",
            background=[("active", c["primary_hover"]), ("disabled", "#94A3B8")],
            foreground=[("disabled", "#E2E8F0")],
        )

        self.style.configure(
            "Danger.TButton",
            background=c["danger"],
            foreground="#FFFFFF",
            font=(font_family, 10, "bold"),
            padding=(14, 7),
            borderwidth=0,
        )
        self.style.map("Danger.TButton", background=[("active", c["danger_hover"])])

        self.style.configure(
            "Warning.TButton",
            background=c["warning"],
            foreground="#FFFFFF",
            font=(font_family, 9, "bold"),
            padding=(12, 7),
            borderwidth=0,
        )
        self.style.map("Warning.TButton", background=[("active", "#D97706")])

        self.style.configure(
            "Secondary.TButton",
            background=c["secondary_btn"],
            foreground=c["secondary_fg"],
            font=(font_family, 9),
            padding=(10, 5),
            borderwidth=0,
        )
        self.style.map("Secondary.TButton", background=[("active", c["card_border"])])

        # Treeview 现代高行距表格样式
        self.style.configure(
            "Queue.Treeview",
            background=c["tree_bg"],
            foreground=c["tree_fg"],
            fieldbackground=c["tree_bg"],
            rowheight=30,
            font=(font_family, 9),
            borderwidth=0,
        )
        self.style.configure(
            "Queue.Treeview.Heading",
            background=c["secondary_btn"],
            foreground=c["text"],
            font=(font_family, 9, "bold"),
            padding=(6, 6),
            relief=tk.FLAT,
        )
        self.style.map(
            "Queue.Treeview",
            background=[("selected", c["tree_sel_bg"])],
            foreground=[("selected", c["tree_sel_fg"])],
        )

        # 绿色进度条样式
        self.style.configure(
            "Emerald.Horizontal.TProgressbar",
            troughcolor=c["secondary_btn"],
            background=c["primary"],
            thickness=10,
            borderwidth=0,
        )

    def _build_ui(self) -> None:
        font_family = "Microsoft YaHei UI"

        # ==================== 1. 顶部深色品牌导航栏 ====================
        self.header_frame = tk.Frame(self.root, height=58, padx=18, pady=10)
        self.header_frame.pack(fill=tk.X, side=tk.TOP)

        title_box = tk.Frame(self.header_frame)
        title_box.pack(side=tk.LEFT)
        self.header_title = tk.Label(
            title_box,
            text="微信图片顺序发送助手",
            font=(font_family, 15, "bold"),
        )
        self.header_title.pack(side=tk.LEFT)

        self.version_badge = tk.Label(
            title_box,
            text=" v3.4 Pro · 60ms 极速锁定引擎 ",
            font=(font_family, 8, "bold"),
            bg="#07C160",
            fg="#FFFFFF",
            padx=6,
            pady=2,
        )
        self.version_badge.pack(side=tk.LEFT, padx=10)

        self.header_sub = tk.Label(
            title_box,
            text="图片 → 确认上传完成 → 自动发送名称（零内存补丁 · 支持 CF_HDROP 原图秒发）",
            font=(font_family, 9),
        )
        self.header_sub.pack(side=tk.LEFT, padx=6)

        right_header = tk.Frame(self.header_frame)
        right_header.pack(side=tk.RIGHT)

        self.conn_pill = tk.Label(
            right_header,
            text="● 正在检测微信状态...",
            font=(font_family, 9, "bold"),
            padx=10,
            pady=3,
        )
        self.conn_pill.pack(side=tk.LEFT, padx=8)

        self.theme_btn = tk.Button(
            right_header,
            text="🌙 深色模式",
            font=(font_family, 9),
            relief=tk.FLAT,
            padx=10,
            pady=3,
            cursor="hand2",
            command=self.toggle_theme,
        )
        self.theme_btn.pack(side=tk.LEFT)

        # ==================== 2. 顶部 KPI 实时统计仪表盘 + 进度条 ====================
        self.kpi_container = ttk.Frame(self.root, style="Bg.TFrame", padding=(14, 10, 14, 4))
        self.kpi_container.pack(fill=tk.X)

        self.kpi_cards: List[tk.Frame] = []
        self.kpi_val_labels: Dict[str, tk.Label] = {}
        self.kpi_title_labels: List[tk.Label] = []

        kpi_defs = [
            ("total", "队列总图片", "0 张", "#3B82F6"),
            ("done", "已确认完成", "0 张", "#07C160"),
            ("verify", "待人工核实", "0 张", "#F59E0B"),
            ("speed", "完成进度 / 速率", "0% (0.0s/张)", "#8B5CF6"),
        ]
        for idx, (key, title, init_val, accent_color) in enumerate(kpi_defs):
            card = tk.Frame(self.kpi_container, padx=14, pady=8, highlightthickness=1)
            card.grid(row=0, column=idx, sticky=tk.EW, padx=(0 if idx == 0 else 8, 0))
            self.kpi_container.columnconfigure(idx, weight=1)

            lbl_t = tk.Label(card, text=title, font=(font_family, 8))
            lbl_t.pack(anchor=tk.W)
            lbl_v = tk.Label(card, text=init_val, font=(font_family, 14, "bold"), fg=accent_color)
            lbl_v.pack(anchor=tk.W, pady=(2, 0))

            self.kpi_cards.append(card)
            self.kpi_title_labels.append(lbl_t)
            self.kpi_val_labels[key] = lbl_v

        prog_frame = ttk.Frame(self.root, style="Bg.TFrame", padding=(14, 4, 14, 4))
        prog_frame.pack(fill=tk.X)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(
            prog_frame,
            style="Emerald.Horizontal.TProgressbar",
            variable=self.progress_var,
            maximum=100.0,
        )
        self.progress_bar.pack(fill=tk.X)

        # ==================== 3. 主工作台：左右分栏 ====================
        self.workspace = ttk.Frame(self.root, style="Bg.TFrame", padding=(14, 4, 14, 6))
        self.workspace.pack(fill=tk.BOTH, expand=True)
        self.workspace.columnconfigure(0, weight=7)
        self.workspace.columnconfigure(1, weight=3)
        self.workspace.rowconfigure(1, weight=1)

        # ---- 左侧上部：双卡片并排配置区（文件夹源 + 接收会话与参数） ----
        config_row = ttk.Frame(self.workspace, style="Bg.TFrame")
        config_row.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        config_row.columnconfigure(0, weight=1)
        config_row.columnconfigure(1, weight=1)

        # 卡片 A：发送源与命名规则
        self.src_card = tk.Frame(config_row, padx=14, pady=10, highlightthickness=1)
        self.src_card.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 6))
        self.src_card.columnconfigure(1, weight=1)

        ttk.Label(self.src_card, text="📁 1. 图片文件夹与排序规则", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 6)
        )
        ttk.Label(self.src_card, text="路径:", style="CardBody.TLabel").grid(row=1, column=0, sticky=tk.W)
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(self.src_card, textvariable=self.folder_var)
        self.folder_entry.grid(row=1, column=1, columnspan=2, sticky=tk.EW, padx=6)
        ttk.Button(self.src_card, text="选择文件夹...", style="Secondary.TButton", command=self.choose_folder).grid(
            row=1, column=3, padx=2
        )

        self.subfolders_var = tk.BooleanVar(value=False)
        self.ext_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.src_card,
            text="包含子文件夹",
            variable=self.subfolders_var,
            style="Card.TCheckbutton",
            command=self.rescan_folder,
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(6, 2))
        ttk.Checkbutton(
            self.src_card,
            text="名称保留文件扩展名 (.jpg/.png)",
            variable=self.ext_var,
            style="Card.TCheckbutton",
            command=self.rescan_folder,
        ).grid(row=2, column=2, columnspan=2, sticky=tk.W, pady=(6, 2))

        ttk.Label(self.src_card, text="剪贴板引擎:", style="CardBody.TLabel").grid(row=3, column=0, sticky=tk.W, pady=(4, 0))
        self.clip_mode_var = tk.StringVar(value="CF_HDROP")
        mode_combo = ttk.Combobox(
            self.src_card,
            textvariable=self.clip_mode_var,
            values=["CF_HDROP", "CF_DIB"],
            state="readonly",
            width=13,
        )
        mode_combo.grid(row=3, column=1, sticky=tk.W, padx=6, pady=(4, 0))
        ttk.Label(
            self.src_card,
            text="推荐 CF_HDROP：原文件直发、零重压缩、支持动图",
            style="CardMuted.TLabel",
        ).grid(row=3, column=2, columnspan=2, sticky=tk.W, pady=(4, 0))

        # 卡片 B：目标微信会话与极速确认参数
        self.chat_card = tk.Frame(config_row, padx=14, pady=10, highlightthickness=1)
        self.chat_card.grid(row=0, column=1, sticky=tk.NSEW, padx=(6, 0))
        self.chat_card.columnconfigure(1, weight=1)

        ttk.Label(self.chat_card, text="💬 2. 接收对象与极速确认参数", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 6)
        )
        ttk.Label(self.chat_card, text="接收会话:", style="CardBody.TLabel").grid(row=1, column=0, sticky=tk.W)
        self.chat_combo = ttk.Combobox(self.chat_card, width=22)
        self.chat_combo.grid(row=1, column=1, sticky=tk.EW, padx=6)
        ttk.Button(self.chat_card, text="刷新会话", style="Secondary.TButton", command=self.refresh_chats).grid(
            row=1, column=2, padx=2
        )
        ttk.Button(self.chat_card, text="锁定当前聊天", style="Secondary.TButton", command=self.use_current_chat).grid(
            row=1, column=3, padx=2
        )

        ttk.Label(self.chat_card, text="发送热键:", style="CardBody.TLabel").grid(row=2, column=0, sticky=tk.W, pady=(6, 2))
        self.shortcut_var = tk.StringVar(value="ENTER")
        hotkey_box = ttk.Frame(self.chat_card, style="Card.TFrame")
        hotkey_box.grid(row=2, column=1, columnspan=3, sticky=tk.W, padx=6, pady=(6, 2))
        ttk.Radiobutton(
            hotkey_box, text="Enter 发送", variable=self.shortcut_var, value="ENTER", style="Card.TRadiobutton"
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            hotkey_box, text="Ctrl + Enter 发送", variable=self.shortcut_var, value="CTRL+ENTER", style="Card.TRadiobutton"
        ).pack(side=tk.LEFT)

        param_box = ttk.Frame(self.chat_card, style="Card.TFrame")
        param_box.grid(row=3, column=0, columnspan=4, sticky=tk.EW, pady=(4, 0))
        ttk.Label(param_box, text="相似度阈值:", style="CardBody.TLabel").pack(side=tk.LEFT)
        self.threshold_var = tk.DoubleVar(value=DEFAULT_SIMILARITY_THRESHOLD)
        ttk.Spinbox(
            param_box,
            from_=0.10,
            to=0.90,
            increment=0.05,
            textvariable=self.threshold_var,
            width=6,
            command=self._on_params_change,
        ).pack(side=tk.LEFT, padx=(4, 14))

        ttk.Label(param_box, text="静默确认(秒):", style="CardBody.TLabel").pack(side=tk.LEFT)
        self.quiet_sec_var = tk.DoubleVar(value=DEFAULT_QUIET_CONFIRM_SEC)
        ttk.Spinbox(
            param_box,
            from_=0.15,
            to=3.00,
            increment=0.05,
            textvariable=self.quiet_sec_var,
            width=6,
            command=self._on_params_change,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Label(param_box, text="(转圈消失 0.12s 秒放)", style="CardMuted.TLabel").pack(side=tk.LEFT, padx=4)

        # ---- 左侧中部：彩色状态队列表格 + 操控栏 ----
        self.queue_card = tk.Frame(self.workspace, padx=14, pady=10, highlightthickness=1)
        self.queue_card.grid(row=1, column=0, sticky=tk.NSEW, padx=(0, 6))
        self.queue_card.rowconfigure(1, weight=1)
        self.queue_card.columnconfigure(0, weight=1)

        q_header = ttk.Frame(self.queue_card, style="Card.TFrame")
        q_header.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 6))
        ttk.Label(
            q_header,
            text="📋 3. 顺序发送队列（点击任意行可在右侧预览图片并修改发送名称）",
            style="CardTitle.TLabel",
        ).pack(side=tk.LEFT)

        cols = ("idx", "rel_name", "send_name", "stage", "detail")
        self.tree = ttk.Treeview(
            self.queue_card,
            columns=cols,
            show="headings",
            style="Queue.Treeview",
            selectmode="browse",
        )
        self.tree.heading("idx", text="#")
        self.tree.heading("rel_name", text="原文件相对路径")
        self.tree.heading("send_name", text="待发送名称")
        self.tree.heading("stage", text="当前状态")
        self.tree.heading("detail", text="实时检测详情")
        self.tree.column("idx", width=48, anchor=tk.CENTER)
        self.tree.column("rel_name", width=185)
        self.tree.column("send_name", width=155)
        self.tree.column("stage", width=125, anchor=tk.CENTER)
        self.tree.column("detail", width=265)
        self.tree.grid(row=1, column=0, sticky=tk.NSEW)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        scroll = ttk.Scrollbar(self.queue_card, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky=tk.NS)

        # 底部主控按钮栏
        action_bar = ttk.Frame(self.queue_card, style="Card.TFrame")
        action_bar.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))

        self.start_btn = ttk.Button(
            action_bar,
            text="▶ 开始 / 继续发送",
            style="Primary.TButton",
            command=self.start_or_resume,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            action_bar,
            text="⏹ 紧急停止 (F8)",
            style="Danger.TButton",
            command=self.request_stop,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            action_bar,
            text="⚠ 处理待核实项...",
            style="Warning.TButton",
            command=self.handle_needs_verify,
        ).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            action_bar,
            text="📂 恢复历史任务...",
            style="Secondary.TButton",
            command=self.load_history_job,
        ).pack(side=tk.RIGHT)

        # ---- 右侧分栏：图片实时缩略图预览 + 单条名称编辑与微调 ----
        self.inspector_card = tk.Frame(self.workspace, padx=14, pady=10, highlightthickness=1)
        self.inspector_card.grid(row=1, column=1, sticky=tk.NSEW, padx=(6, 0))
        self.inspector_card.columnconfigure(0, weight=1)

        ttk.Label(self.inspector_card, text="🖼️ 图片预览与单条微调", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 6)
        )

        self.preview_canvas = tk.Canvas(
            self.inspector_card,
            width=260,
            height=185,
            highlightthickness=1,
            bd=0,
        )
        self.preview_canvas.grid(row=1, column=0, sticky=tk.EW, pady=4)

        self.meta_var = tk.StringVar(value="在左侧列表选择图片以预览缩略图与分辨率")
        ttk.Label(self.inspector_card, textvariable=self.meta_var, style="CardMuted.TLabel", wraplength=260).grid(
            row=2, column=0, sticky=tk.W, pady=(2, 8)
        )

        ttk.Label(self.inspector_card, text="自定义本张发送名称:", style="CardBody.TLabel").grid(
            row=3, column=0, sticky=tk.W
        )
        name_edit_row = ttk.Frame(self.inspector_card, style="Card.TFrame")
        name_edit_row.grid(row=4, column=0, sticky=tk.EW, pady=(4, 8))
        name_edit_row.columnconfigure(0, weight=1)
        self.edit_name_var = tk.StringVar()
        self.edit_name_entry = ttk.Entry(name_edit_row, textvariable=self.edit_name_var)
        self.edit_name_entry.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))
        self.edit_name_entry.bind("<Return>", lambda _e: self.apply_custom_name())
        ttk.Button(name_edit_row, text="保存名称", style="Secondary.TButton", command=self.apply_custom_name).grid(
            row=0, column=1
        )

        reorder_row = ttk.Frame(self.inspector_card, style="Card.TFrame")
        reorder_row.grid(row=5, column=0, sticky=tk.EW, pady=(2, 8))
        ttk.Button(reorder_row, text="⬆️ 上移", style="Secondary.TButton", command=lambda: self.move_selected(-1)).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(reorder_row, text="⬇️ 下移", style="Secondary.TButton", command=lambda: self.move_selected(1)).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(reorder_row, text="🗑️ 移除此项", style="Secondary.TButton", command=self.remove_selected).pack(
            side=tk.LEFT
        )

        # 右下：实时运行日志小窗
        ttk.Label(self.inspector_card, text="📡 实时运行日志", style="CardTitle.TLabel").grid(
            row=6, column=0, sticky=tk.W, pady=(6, 4)
        )
        self.log_text = tk.Text(
            self.inspector_card,
            height=7,
            font=("Consolas", 8),
            relief=tk.FLAT,
            padx=8,
            pady=6,
            state=tk.DISABLED,
        )
        self.log_text.grid(row=7, column=0, sticky=tk.NSEW)
        self.inspector_card.rowconfigure(7, weight=1)

        # ==================== 4. 底部状态栏 ====================
        self.status_var = tk.StringVar(
            value="就绪。v3.4 Pro 已启用 60ms 首帧尺度锁定、静态转圈否决与 WAL 脉冲检测。"
        )
        self.status_bar = tk.Label(
            self.root,
            textvariable=self.status_var,
            anchor=tk.W,
            padx=14,
            pady=5,
            font=(font_family, 9),
        )
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _apply_theme(self) -> None:
        self.colors = THEMES[self.theme_name]
        c = self.colors
        self._configure_styles()

        self.header_frame.configure(bg=c["header_bg"])
        self.header_title.configure(bg=c["header_bg"], fg=c["header_fg"])
        self.header_sub.configure(bg=c["header_bg"], fg=c["header_sub"])
        self.theme_btn.configure(
            bg=c["secondary_btn"],
            fg=c["secondary_fg"],
            text="☀️ 浅色模式" if self.theme_name == "dark" else "🌙 深色模式",
        )

        for card in self.kpi_cards + [self.src_card, self.chat_card, self.queue_card, self.inspector_card]:
            card.configure(bg=c["card_bg"], highlightbackground=c["card_border"])
        for lbl in self.kpi_title_labels:
            lbl.configure(bg=c["card_bg"], fg=c["text_muted"])
        for lbl in self.kpi_val_labels.values():
            lbl.configure(bg=c["card_bg"])

        self.preview_canvas.configure(bg=c["preview_bg"], highlightbackground=c["card_border"])
        self.log_text.configure(bg=c["log_bg"], fg=c["log_fg"])
        self.status_bar.configure(bg=c["card_bg"], fg=c["text_muted"])

        # 配置表格彩色标签行
        self.tree.tag_configure(
            "pending", background=c["tag_pending_bg"], foreground=c["tag_pending_fg"]
        )
        self.tree.tag_configure(
            "active", background=c["tag_active_bg"], foreground=c["tag_active_fg"]
        )
        self.tree.tag_configure(
            "done", background=c["tag_done_bg"], foreground=c["tag_done_fg"]
        )
        self.tree.tag_configure(
            "verify", background=c["tag_verify_bg"], foreground=c["tag_verify_fg"]
        )

    def toggle_theme(self) -> None:
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self._apply_theme()

    def _append_log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    @staticmethod
    def _stage_to_tag(stage: str) -> str:
        if stage == ItemStage.COMPLETED.value:
            return "done"
        if stage == ItemStage.NEEDS_MANUAL_VERIFY.value:
            return "verify"
        if stage in (
            ItemStage.IMAGE_SUBMITTED.value,
            ItemStage.IMAGE_CONFIRMED.value,
            ItemStage.NAME_SUBMITTED.value,
        ):
            return "active"
        return "pending"

    def _update_kpi_dashboard(self) -> None:
        if self.current_job:
            total = len(self.current_job.items)
            done = sum(1 for it in self.current_job.items if it.stage == ItemStage.COMPLETED.value)
            verify = sum(1 for it in self.current_job.items if it.stage == ItemStage.NEEDS_MANUAL_VERIFY.value)
        else:
            total = len(self.scanned_items)
            done = 0
            verify = 0

        pct = (done * 100.0 / total) if total > 0 else 0.0
        self.progress_var.set(pct)
        self.kpi_val_labels["total"].configure(text=f"{total} 张")
        self.kpi_val_labels["done"].configure(text=f"{done} 张")
        self.kpi_val_labels["verify"].configure(text=f"{verify} 张")

        if self._run_start_ts and done > 0:
            elapsed = max(0.1, time.monotonic() - self._run_start_ts)
            avg_s = elapsed / done
            self.kpi_val_labels["speed"].configure(text=f"{pct:.0f}% ({avg_s:.2f}s/张)")
        else:
            self.kpi_val_labels["speed"].configure(text=f"{pct:.0f}%")

    def _on_params_change(self) -> None:
        try:
            val = float(self.threshold_var.get())
            self.adapter.set_similarity_threshold(max(0.05, min(0.95, val)))
        except Exception:
            pass
        try:
            q_sec = float(self.quiet_sec_var.get())
            self.adapter.set_quiet_confirm_sec(max(0.10, min(5.0, q_sec)))
        except Exception:
            pass

    def refresh_chats(self) -> None:
        diag = self.adapter.diagnose()
        if diag.wechat_running:
            mode_txt = "演示模式" if self.demo_mode else f"已连接微信 ({diag.dpi_scale_percent}% DPI)"
            self.conn_pill.configure(text=f"● {mode_txt}", bg="#064E3B", fg="#6EE7B7")
        else:
            self.conn_pill.configure(text="○ 未检测到微信窗口", bg="#7F1D1D", fg="#FCA5A5")

        chats = self.adapter.list_recent_chats()
        self.chat_combo["values"] = chats
        if chats and not self.chat_combo.get():
            self.chat_combo.set(chats[0])

    def use_current_chat(self) -> None:
        curr = self.adapter.get_current_chat_title()
        if curr:
            self.chat_combo.set(curr)
            self.status_var.set(f"已锁定当前微信会话：{curr}")
            self._append_log(f"已锁定微信当前聊天窗口：{curr}")

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="选择图片文件夹")
        if chosen:
            self.folder_var.set(chosen)
            self.rescan_folder()

    def rescan_folder(self) -> None:
        folder_str = self.folder_var.get().strip()
        if not folder_str:
            return
        try:
            items, skipped = scan_image_folder(
                Path(folder_str),
                include_subfolders=self.subfolders_var.get(),
                include_extension=self.ext_var.get(),
                allow_animated_in_file_mode=(self.clip_mode_var.get() == "CF_HDROP"),
            )
            self.scanned_items = items
            self.custom_names.clear()
            self.current_job = None
            self._refresh_tree_from_scan()
            self._update_kpi_dashboard()
            msg = f"已扫描 {len(items)} 张有效图片"
            if skipped:
                msg += f"（已跳过 {len(skipped)} 个不支持的文件）"
            self.status_var.set(msg)
            self._append_log(msg)
            if items:
                self.tree.selection_set("0")
                self._show_preview_for_index(0)
        except Exception as exc:
            messagebox.showerror("扫描失败", str(exc))

    def _get_effective_display_name(self, idx: int, item: ImageItem) -> str:
        return self.custom_names.get(idx, item.display_name)

    def _refresh_tree_from_scan(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for idx, it in enumerate(self.scanned_items):
            send_name = self._get_effective_display_name(idx, it)
            badge = STAGE_BADGES.get(ItemStage.PENDING.value, ItemStage.PENDING.value)
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(idx + 1, it.relative_name, send_name, badge, f"{it.width}×{it.height} ({it.format_name})"),
                tags=("pending",),
            )

    def _refresh_tree_from_job(self) -> None:
        if not self.current_job:
            return
        self.tree.delete(*self.tree.get_children())
        for it in self.current_job.items:
            badge = STAGE_BADGES.get(it.stage, it.stage)
            tag = self._stage_to_tag(it.stage)
            self.tree.insert(
                "",
                tk.END,
                iid=str(it.index),
                values=(it.index + 1, it.relative_name, it.display_name, badge, it.last_message),
                tags=(tag,),
            )
        self._update_kpi_dashboard()

    def _on_tree_select(self, _event: Any = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            self._show_preview_for_index(idx)
        except Exception:
            pass

    def _show_preview_for_index(self, idx: int) -> None:
        file_path: Optional[Path] = None
        display_name = ""
        meta_str = ""

        if self.current_job and 0 <= idx < len(self.current_job.items):
            rec = self.current_job.items[idx]
            file_path = Path(rec.file_path)
            display_name = rec.display_name
            meta_str = f"第 {idx + 1} 张 | 状态: {STAGE_BADGES.get(rec.stage, rec.stage)}\nSHA-256: {rec.sha256[:16]}..."
        elif 0 <= idx < len(self.scanned_items):
            it = self.scanned_items[idx]
            file_path = it.path
            display_name = self._get_effective_display_name(idx, it)
            try:
                kb = file_path.stat().st_size / 1024.0
            except Exception:
                kb = 0.0
            meta_str = f"尺寸: {it.width} × {it.height} | 格式: {it.format_name} | 大小: {kb:.1f} KB"

        self.edit_name_var.set(display_name)
        self.meta_var.set(meta_str)

        self.preview_canvas.delete("all")
        cw = max(240, self.preview_canvas.winfo_width())
        ch = max(175, self.preview_canvas.winfo_height())

        if file_path and file_path.exists() and ImageTk is not None:
            try:
                rgb_img, _, _ = load_static_image(file_path)
                rgb_img.thumbnail((cw - 16, ch - 16), Image.Resampling.LANCZOS)
                self._preview_photo = ImageTk.PhotoImage(rgb_img)
                self.preview_canvas.create_image(cw // 2, ch // 2, image=self._preview_photo, anchor=tk.CENTER)
                return
            except Exception as exc:
                self.preview_canvas.create_text(
                    cw // 2, ch // 2, text=f"预览加载失败:\n{exc}", fill="#EF4444", justify=tk.CENTER
                )
                return

        self.preview_canvas.create_text(
            cw // 2, ch // 2, text="暂无图片预览", fill=self.colors["text_muted"], justify=tk.CENTER
        )

    def apply_custom_name(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        new_name = self.edit_name_var.get().strip()
        if not new_name:
            messagebox.showwarning("名称为空", "发送的图片名称不能为空。")
            return

        if self.current_job and 0 <= idx < len(self.current_job.items):
            self.current_job.items[idx].display_name = new_name
            self.engine.save_job(self.current_job)
            self._refresh_tree_from_job()
        elif 0 <= idx < len(self.scanned_items):
            self.custom_names[idx] = new_name
            self._refresh_tree_from_scan()

        self.tree.selection_set(str(idx))
        self._append_log(f"已将第 {idx + 1} 张图片的待发送名称修改为：“{new_name}”")

    def move_selected(self, delta: int) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "发送进行中无法调整顺序，请先按 F8 停止。")
            return
        sel = self.tree.selection()
        if not sel or not self.scanned_items or self.current_job is not None:
            return
        idx = int(sel[0])
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.scanned_items)):
            return
        self.scanned_items[idx], self.scanned_items[new_idx] = (
            self.scanned_items[new_idx],
            self.scanned_items[idx],
        )
        # 同步交换自定义名称
        n1 = self.custom_names.get(idx)
        n2 = self.custom_names.get(new_idx)
        if n2 is not None:
            self.custom_names[idx] = n2
        else:
            self.custom_names.pop(idx, None)
        if n1 is not None:
            self.custom_names[new_idx] = n1
        else:
            self.custom_names.pop(new_idx, None)

        self._refresh_tree_from_scan()
        self.tree.selection_set(str(new_idx))
        self.tree.see(str(new_idx))

    def remove_selected(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "发送进行中无法移除条目，请先按 F8 停止。")
            return
        sel = self.tree.selection()
        if not sel or not self.scanned_items or self.current_job is not None:
            return
        idx = int(sel[0])
        removed = self.scanned_items.pop(idx)
        new_custom: Dict[int, str] = {}
        for k, v in self.custom_names.items():
            if k < idx:
                new_custom[k] = v
            elif k > idx:
                new_custom[k - 1] = v
        self.custom_names = new_custom
        self._refresh_tree_from_scan()
        self._update_kpi_dashboard()
        self._append_log(f"已从队列移除：{removed.relative_name}")

    def _on_engine_progress(self, item: JobRecordItem, message: str) -> None:
        def _update() -> None:
            iid = str(item.index)
            badge = STAGE_BADGES.get(item.stage, item.stage)
            tag = self._stage_to_tag(item.stage)
            if self.tree.exists(iid):
                self.tree.item(
                    iid,
                    values=(item.index + 1, item.relative_name, item.display_name, badge, message),
                    tags=(tag,),
                )
                self.tree.see(iid)
                self.tree.selection_set(iid)
            self._show_preview_for_index(item.index)
            self._update_kpi_dashboard()
            self.status_var.set(f"[{item.index + 1}] {item.display_name}: {message}")
            self._append_log(f"[#{item.index + 1} {item.display_name}] {message}")

        self.root.after(0, _update)

    def start_or_resume(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("提示", "发送任务正在运行中。")
            return

        self._on_params_change()
        target_chat = self.chat_combo.get().strip()
        if not target_chat:
            messagebox.showwarning("缺少目标会话", "请先选择或填写微信接收对象。")
            return

        if self.current_job is None:
            if not self.scanned_items:
                messagebox.showwarning("列表为空", "请先选择包含有效图片的文件夹。")
                return
            # 应用用户在右侧面板自定义的发送名称
            final_items = [
                ImageItem(
                    path=it.path,
                    relative_name=it.relative_name,
                    display_name=self._get_effective_display_name(i, it),
                    sha256=it.sha256,
                    width=it.width,
                    height=it.height,
                    format_name=it.format_name,
                    is_mpo_primary=it.is_mpo_primary,
                )
                for i, it in enumerate(self.scanned_items)
            ]
            self.current_job = self.engine.create_job(
                folder_path=Path(self.folder_var.get().strip()),
                image_items=final_items,
                target_chat=target_chat,
                clipboard_mode=self.clip_mode_var.get(),
                send_shortcut=self.shortcut_var.get(),
                include_extension=self.ext_var.get(),
                include_subfolders=self.subfolders_var.get(),
            )
            self._refresh_tree_from_job()

        self._run_start_ts = time.monotonic()
        self.adapter.activate_chat(target_chat)
        self._append_log(
            f"开始执行发送队列 → 目标会话：“{target_chat}” | 阈值={self.adapter.similarity_threshold:.2f} | 静默={self.adapter.quiet_confirm_sec:.2f}s"
        )

        def _run() -> None:
            assert self.current_job is not None
            ok, summary = self.engine.run_job(
                self.current_job,
                stable_duration_sec=self.adapter.quiet_confirm_sec,
            )
            self.root.after(0, self._update_kpi_dashboard)
            self.root.after(0, lambda: self.status_var.set(summary))
            self.root.after(0, lambda: self._append_log(summary))
            if not ok:
                self.root.after(0, lambda: messagebox.showwarning("队列已暂停", summary))
            else:
                self.root.after(0, lambda: messagebox.showinfo("发送完成", summary))

        self.worker_thread = threading.Thread(target=_run, daemon=True)
        self.worker_thread.start()

    def request_stop(self) -> None:
        self.engine.stop_requested = True
        self.status_var.set("已发出停止指令 (F8)，正在安全暂停当前步骤...")
        self._append_log("收到停止指令 (F8)，正在安全暂停...")

    def handle_needs_verify(self) -> None:
        if not self.current_job:
            messagebox.showinfo("提示", "当前没有活动的任务记录。")
            return
        pending_items = [it for it in self.current_job.items if it.stage == ItemStage.NEEDS_MANUAL_VERIFY.value]
        if not pending_items:
            messagebox.showinfo("提示", "当前没有需要人工核实的条目。")
            return

        target = pending_items[0]
        dlg = tk.Toplevel(self.root)
        dlg.title(f"处理待核实项 - 第 {target.index + 1} 张 ({target.display_name})")
        dlg.geometry("500x260")
        dlg.configure(bg=self.colors["card_bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(
            dlg,
            text=(
                f"文件：{target.relative_name}\n"
                f"暂停原因：{target.last_message}\n\n"
                "请到微信窗口核实该图片实际状态，并选择处理方式："
            ),
            style="CardBody.TLabel",
            wraplength=460,
        ).pack(padx=18, pady=14, anchor=tk.W)

        def _apply(decision: ManualDecision) -> None:
            assert self.current_job is not None
            self.engine.apply_manual_decision(self.current_job, target.index, decision)
            self._refresh_tree_from_job()
            self._append_log(f"人工核实处理第 {target.index + 1} 张 ({target.display_name}) -> {decision.value}")
            dlg.destroy()

        ttk.Button(
            dlg,
            text="1. 图片已成功发出 → 仅补发图片名称",
            style="Primary.TButton",
            command=lambda: _apply(ManualDecision.SEND_NAME_ONLY),
        ).pack(fill=tk.X, padx=24, pady=4)
        ttk.Button(
            dlg,
            text="2. 图片与名称均已发出 → 标记本张完成",
            style="Secondary.TButton",
            command=lambda: _apply(ManualDecision.MARK_COMPLETED),
        ).pack(fill=tk.X, padx=24, pady=4)
        ttk.Button(
            dlg,
            text="3. 确认未发出（且已清理草稿）→ 重新发送本张",
            style="Secondary.TButton",
            command=lambda: _apply(ManualDecision.RETRY_FULL_ITEM),
        ).pack(fill=tk.X, padx=24, pady=4)

    def load_history_job(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择历史任务 JSON 记录",
            initialdir=str(self.engine.jobs_dir),
            filetypes=[("Job JSON", "*.json")],
        )
        if not chosen:
            return
        try:
            job = self.engine.load_job(Path(chosen))
            self.current_job = job
            self.folder_var.set(job.folder_path)
            self.chat_combo.set(job.target_chat)
            self.clip_mode_var.set(job.clipboard_mode)
            self.shortcut_var.set(job.send_shortcut)
            self._refresh_tree_from_job()
            msg = f"已加载历史任务：{job.job_id}（共 {len(job.items)} 项）"
            self.status_var.set(msg)
            self._append_log(msg)
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))
