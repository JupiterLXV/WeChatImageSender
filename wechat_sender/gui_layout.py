# -*- coding: utf-8 -*-
"""Blue desktop layout, compact task controls, and nonblocking image previews."""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageOps, ImageTk

from .job_store import ItemStage


THEMES = {
    "light": {
        "bg": "#F1F3F6", "card_bg": "#FFFFFF", "card_border": "#D6DCE5",
        "header_bg": "#FFFFFF", "header_fg": "#202124", "header_sub": "#626B77",
        "text": "#202124", "text_muted": "#657080",
        "primary": "#1A73E8", "primary_hover": "#1765CC", "primary_fg": "#FFFFFF",
        "danger": "#C43C35", "danger_hover": "#A9302A",
        "warning": "#FFF4DC", "warning_hover": "#F9E6BC", "warning_fg": "#855B11",
        "secondary_btn": "#F5F7FA", "secondary_hover": "#E9EEF5", "secondary_fg": "#344154",
        "input_bg": "#FFFFFF", "input_fg": "#202124", "input_border": "#BCC6D2",
        "tree_bg": "#FFFFFF", "tree_fg": "#303744", "tree_heading": "#F3F6FA",
        "tree_sel_bg": "#DCEBFF", "tree_sel_fg": "#174EA6",
        "tag_pending_bg": "#FFFFFF", "tag_pending_fg": "#657080",
        "tag_active_bg": "#EDF4FF", "tag_active_fg": "#185ABC",
        "tag_done_bg": "#FFFFFF", "tag_done_fg": "#1967D2",
        "tag_verify_bg": "#FFF4DC", "tag_verify_fg": "#855B11",
        "log_bg": "#F8FAFC", "log_fg": "#3D4755", "preview_bg": "#F3F5F8",
        "badge_bg": "#EDF3FC", "badge_fg": "#245CA8",
        "connected_bg": "#EAF2FF", "connected_fg": "#185ABC",
        "disconnected_bg": "#F0F2F5", "disconnected_fg": "#697382",
        "kpi_total": "#344154", "kpi_done": "#1967D2", "kpi_verify": "#855B11", "kpi_speed": "#657080",
    },
    "dark": {
        "bg": "#20242B", "card_bg": "#292E37", "card_border": "#414A58",
        "header_bg": "#292E37", "header_fg": "#EDF1F7", "header_sub": "#A9B3C2",
        "text": "#EDF1F7", "text_muted": "#A9B3C2",
        "primary": "#8AB4F8", "primary_hover": "#A3C4FA", "primary_fg": "#142E54",
        "danger": "#E18B86", "danger_hover": "#EDA29D",
        "warning": "#493E28", "warning_hover": "#584A30", "warning_fg": "#F0CF8F",
        "secondary_btn": "#353D49", "secondary_hover": "#424D5C", "secondary_fg": "#E2E8F0",
        "input_bg": "#242A32", "input_fg": "#EDF1F7", "input_border": "#586578",
        "tree_bg": "#292E37", "tree_fg": "#DFE5EF", "tree_heading": "#323A47",
        "tree_sel_bg": "#344F75", "tree_sel_fg": "#E6F0FF",
        "tag_pending_bg": "#292E37", "tag_pending_fg": "#ABB6C7",
        "tag_active_bg": "#2C3C53", "tag_active_fg": "#B4D1FF",
        "tag_done_bg": "#292E37", "tag_done_fg": "#A7C8FA",
        "tag_verify_bg": "#493E28", "tag_verify_fg": "#F0CF8F",
        "log_bg": "#242A32", "log_fg": "#CFD7E5", "preview_bg": "#242A32",
        "badge_bg": "#334662", "badge_fg": "#BAD6FF",
        "connected_bg": "#334662", "connected_fg": "#BAD6FF",
        "disconnected_bg": "#383E48", "disconnected_fg": "#B2BDCD",
        "kpi_total": "#DFE5EF", "kpi_done": "#A7C8FA", "kpi_verify": "#F0CF8F", "kpi_speed": "#A9B3C2",
    },
}


def assets_directory() -> Path:
    candidates = [
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "assets",
        Path(__file__).resolve().parent.parent / "assets",
        Path(__file__).resolve().parent / "assets",
    ]
    return next((p for p in candidates if (p / "app-icon.png").is_file()), candidates[0])


class DesktopLayoutMixin:
    def _configure_styles(self) -> None:
        c = self.colors
        self.root.configure(bg=c["bg"])
        font = ("Microsoft YaHei UI", 9)
        self.style.configure(".", font=font, background=c["bg"], foreground=c["text"])
        for key in ("Bg.TFrame", "Card.TFrame"):
            self.style.configure(key, background=c["bg"] if key.startswith("Bg") else c["card_bg"])
        for key, size, color in [("CardTitle.TLabel", 10, "text"), ("CardBody.TLabel", 9, "text"), ("CardMuted.TLabel", 9, "text_muted")]:
            self.style.configure(key, background=c["card_bg"], foreground=c[color], font=(font[0], size))
        self.style.configure("Secondary.TButton", background=c["secondary_btn"], foreground=c["secondary_fg"], bordercolor=c["input_border"], lightcolor=c["card_bg"], darkcolor=c["input_border"], padding=(10, 6), borderwidth=1)
        self.style.map("Secondary.TButton", background=[("active", c["secondary_hover"])], foreground=[("disabled", c["text_muted"])])
        self.style.configure("Primary.TButton", background=c["primary"], foreground=c["primary_fg"], bordercolor=c["primary"], lightcolor=c["primary"], darkcolor=c["primary"], padding=(20, 8), borderwidth=1, font=(font[0], 10))
        self.style.map("Primary.TButton", background=[("disabled", c["secondary_btn"]), ("active", c["primary_hover"])], foreground=[("disabled", c["text_muted"])])
        self.style.configure("Danger.TButton", background=c["secondary_btn"], foreground=c["danger"], padding=(12, 8), borderwidth=1, bordercolor=c["input_border"])
        self.style.map("Danger.TButton", background=[("active", c["secondary_hover"])], foreground=[("disabled", c["text_muted"])])
        self.style.configure("Warning.TButton", background=c["warning"], foreground=c["warning_fg"], padding=(10, 6), borderwidth=1, bordercolor=c["card_border"])
        self.style.map("Warning.TButton", background=[("active", c["warning_hover"])])
        for key in ("TEntry", "TCombobox", "TSpinbox"):
            self.style.configure(key, fieldbackground=c["input_bg"], foreground=c["input_fg"], background=c["secondary_btn"], bordercolor=c["input_border"], lightcolor=c["input_border"], darkcolor=c["input_border"], arrowcolor=c["text_muted"], padding=5)
            self.style.map(key, fieldbackground=[("readonly", c["input_bg"]), ("disabled", c["secondary_btn"])], foreground=[("disabled", c["text_muted"]), ("readonly", c["input_fg"])])
        for key in ("Card.TCheckbutton", "Card.TRadiobutton"):
            self.style.configure(key, background=c["card_bg"], foreground=c["text"], indicatorcolor=c["input_bg"])
            self.style.map(key, background=[("active", c["card_bg"])])
        self.style.configure("Queue.Treeview", background=c["tree_bg"], fieldbackground=c["tree_bg"], foreground=c["tree_fg"], rowheight=35, borderwidth=0, font=font)
        self.style.configure("Queue.Treeview.Heading", background=c["tree_heading"], foreground=c["text_muted"], padding=(6, 9), relief=tk.FLAT, font=font)
        self.style.map("Queue.Treeview", background=[("selected", c["tree_sel_bg"])], foreground=[("selected", c["tree_sel_fg"])])
        self.style.map("Queue.Treeview.Heading", background=[("active", c["secondary_hover"])])
        self.style.configure("Blue.Horizontal.TProgressbar", troughcolor=c["secondary_btn"], background=c["primary"], borderwidth=0, thickness=6)
        self.style.configure("TPanedwindow", background=c["bg"])

    def _build_ui(self) -> None:
        self.colors = THEMES[self.theme_name]
        self._configure_styles()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{min(1160, max(840, sw - 100))}x{min(780, max(580, sh - 110))}")
        self.root.minsize(900, 700)
        self.root.title("微信图片顺序发送助手" + (" · 离线演示" if self.demo_mode else ""))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        self._theme_frames: list[Any] = []
        self._theme_labels: list[Any] = []
        self._edit_controls: list[Any] = []
        self._dialogs: list[Any] = []
        self._preview_cache: OrderedDict = OrderedDict()
        self._preview_token = 0
        self._preview_loading_key = None
        self._preview_after = None
        self._footer_after = None

        self.header_frame = tk.Frame(self.root, padx=18, pady=12)
        self.header_frame.grid(row=0, column=0, sticky="ew")
        self.header_frame.columnconfigure(1, weight=1)
        self.logo_label = tk.Label(self.header_frame)
        self.logo_label.grid(row=0, column=0, rowspan=2, padx=(0, 11))
        self.header_title = tk.Label(self.header_frame, text="微信图片顺序发送助手", font=("Microsoft YaHei UI", 15))
        self.header_title.grid(row=0, column=1, sticky="w")
        self.header_sub = tk.Label(self.header_frame, text="按队列顺序发送图片，确认后再发送名称", font=("Microsoft YaHei UI", 9))
        self.header_sub.grid(row=1, column=1, sticky="w", pady=(2, 0))
        self.conn_pill = tk.Label(self.header_frame, text="离线演示" if self.demo_mode else "正在检测微信…", padx=10, pady=5, font=("Microsoft YaHei UI", 9), cursor="hand2")
        self.conn_pill.grid(row=0, column=2, rowspan=2, padx=(10, 0))
        self.conn_pill.bind("<Button-1>", lambda _e: self.refresh_chats())
        self.version_badge = tk.Label(self.header_frame, text="3.6.3", font=("Segoe UI", 8), padx=5)
        self.version_badge.grid(row=0, column=3, rowspan=2, padx=(10, 0))
        self._load_brand_icon()

        self.toolbar = ttk.Frame(self.root, style="Bg.TFrame", padding=(14, 9))
        self.toolbar.grid(row=1, column=0, sticky="ew")
        self.toolbar.columnconfigure(6, weight=1)
        for col, text, command in [(0, "添加文件夹…", self.choose_folder), (1, "添加图片…", self.choose_files), (2, "恢复任务…", self.load_history_job)]:
            button = ttk.Button(self.toolbar, text=text, style="Secondary.TButton", command=command)
            button.grid(row=0, column=col, padx=(0, 7))
            self._edit_controls.append(button)
            if col == 2:
                self.history_btn = button
        self.new_task_btn = ttk.Button(self.toolbar, text="新建任务", style="Secondary.TButton", command=self.new_task)
        self.new_task_btn.grid(row=0, column=3, padx=(0, 7))
        self._edit_controls.append(self.new_task_btn)
        self.preview_visible_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.toolbar, text="图片预览", variable=self.preview_visible_var, command=self._toggle_preview).grid(row=0, column=7, padx=6)
        self.settings_btn = ttk.Button(self.toolbar, text="高级设置", style="Secondary.TButton", command=self._open_settings)
        self.settings_btn.grid(row=0, column=8, padx=(5, 0))

        self.source_band = tk.Frame(self.root, padx=15, pady=10, highlightthickness=1)
        self.source_band.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
        self.source_band.columnconfigure(1, weight=1)
        self._theme_frames.append(self.source_band)
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(self.source_band, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=(8, 12))
        self.folder_entry.bind("<Return>", lambda _e: self.rescan_folder())
        self._edit_controls.append(self.folder_entry)
        self._label(self.source_band, "图片来源").grid(row=0, column=0, sticky="w")
        self._label(self.source_band, "发送到").grid(row=0, column=2, sticky="w")
        self.chat_combo = ttk.Combobox(self.source_band, width=21)
        self.chat_combo.grid(row=0, column=3, padx=(8, 7), sticky="ew")
        self.current_chat_btn = ttk.Button(self.source_band, text="刷新/定位聊天", style="Secondary.TButton", command=self.refresh_chats)
        self.current_chat_btn.grid(row=0, column=4)
        self._edit_controls.append(self.current_chat_btn)

        self.subfolders_var = tk.BooleanVar(value=False)
        self.ext_var = tk.BooleanVar(value=False)
        self.clip_mode_var = tk.StringVar(value="CF_HDROP")
        self.shortcut_var = tk.StringVar(value="ENTER")
        self.auto_search_var = tk.BooleanVar(value=False)
        self.print_win_var = tk.BooleanVar(value=False)
        self.threshold_var = tk.DoubleVar(value=0.40)
        self.quiet_sec_var = tk.DoubleVar(value=1.5)
        self.hud_enable_var = tk.BooleanVar(value=True)
        self.filter_stage_var = tk.StringVar(value="ALL")
        self.search_query_var = tk.StringVar()
        self.batch_tpl_var = tk.StringVar(value="{num:02d}_{name}")
        self.edit_name_var = tk.StringVar()
        self.meta_var = tk.StringVar(value="选择一张图片，查看预览并调整发送名称")

        self.workspace = ttk.Frame(self.root, style="Bg.TFrame", padding=(14, 0, 14, 10))
        self.workspace.grid(row=3, column=0, sticky="nsew")
        self.workspace.columnconfigure(0, weight=1)
        self.workspace.rowconfigure(0, weight=1)
        self.panes = ttk.Panedwindow(self.workspace, orient=tk.HORIZONTAL)
        self.panes.grid(row=0, column=0, sticky="nsew")
        self.queue_card = tk.Frame(self.panes, padx=12, pady=10, highlightthickness=1, width=690)
        self.queue_card.rowconfigure(2, weight=1)
        self.queue_card.columnconfigure(0, weight=1)
        self.inspector_card = tk.Frame(self.panes, padx=12, pady=10, highlightthickness=1, width=292)
        self.inspector_card.columnconfigure(0, weight=1)
        self.inspector_card.rowconfigure(1, weight=1)
        self.panes.add(self.queue_card, weight=7)
        self.panes.add(self.inspector_card, weight=3)
        self._theme_frames.extend([self.queue_card, self.inspector_card])
        self.src_card = self.source_band
        self.chat_card = self.source_band
        q_head = ttk.Frame(self.queue_card, style="Card.TFrame")
        q_head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        q_head.columnconfigure(0, weight=1)
        ttk.Label(q_head, text="发送队列", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(q_head, text="列表序号即发送顺序", style="CardMuted.TLabel").grid(row=0, column=1, sticky="e")
        filters = ttk.Frame(self.queue_card, style="Card.TFrame")
        filters.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        filters.columnconfigure(4, weight=1)
        for i, (text, value) in enumerate([("全部", "ALL"), ("待发", "PENDING"), ("已完成", "COMPLETED"), ("待核实", "VERIFY")]):
            ttk.Radiobutton(filters, text=text, value=value, variable=self.filter_stage_var, command=self._refresh_tree_view, style="Card.TRadiobutton").grid(row=0, column=i, padx=(0, 8))
        search = ttk.Entry(filters, textvariable=self.search_query_var, width=13)
        search.grid(row=0, column=4, sticky="e", padx=(8, 0))
        self.search_query_var.trace_add("write", lambda *_: self._refresh_tree_view())
        self.tree = ttk.Treeview(self.queue_card, columns=("idx", "rel_name", "send_name", "stage", "detail"), displaycolumns=("idx", "rel_name", "send_name", "stage"), show="headings", style="Queue.Treeview", selectmode="extended")
        for col, title, width, minimum in [("idx", "序号", 50, 45), ("rel_name", "图片文件", 216, 105), ("send_name", "发送名称", 185, 100), ("stage", "状态", 105, 95), ("detail", "确认详情", 240, 100)]:
            self.tree.heading(col, text=title, command=lambda key=col: self._sort_tree_by_column(key))
            self.tree.column(col, width=width, minwidth=minimum, stretch=col not in ("idx", "stage"), anchor=tk.CENTER if col in ("idx", "stage") else tk.W)
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Delete>", lambda _e: self.remove_selected())
        v_scroll = ttk.Scrollbar(self.queue_card, orient=tk.VERTICAL, command=self.tree.yview)
        v_scroll.grid(row=2, column=1, sticky="ns")
        h_scroll = ttk.Scrollbar(self.queue_card, orient=tk.HORIZONTAL, command=self.tree.xview)
        h_scroll.grid(row=3, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
        order_tools = ttk.Frame(self.queue_card, style="Card.TFrame")
        order_tools.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for i, (text, fn) in enumerate([("上移", lambda: self.move_selected(-1)), ("下移", lambda: self.move_selected(1)), ("移除", self.remove_selected)]):
            button = ttk.Button(order_tools, text=text, style="Secondary.TButton", command=fn, width=6)
            button.grid(row=0, column=i, padx=(0, 6))
            self._edit_controls.append(button)
        order_tools.columnconfigure(3, weight=1)
        batch_button = ttk.Button(order_tools, text="批量命名…", style="Secondary.TButton", command=self._open_batch_names)
        batch_button.grid(row=0, column=4, sticky="e")
        self._edit_controls.append(batch_button)

        ttk.Label(self.inspector_card, text="图片预览", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 9))
        self.preview_canvas = tk.Canvas(self.inspector_card, width=250, height=180, highlightthickness=1, bd=0, cursor="hand2")
        self.preview_canvas.grid(row=1, column=0, sticky="nsew")
        self.preview_canvas.bind("<Configure>", self._on_preview_canvas_configure)
        self.preview_canvas.bind("<Double-Button-1>", lambda _e: self._open_lightbox_viewer())
        self.meta_label = ttk.Label(self.inspector_card, textvariable=self.meta_var, style="CardMuted.TLabel", wraplength=270)
        self.meta_label.grid(row=2, column=0, sticky="ew", pady=(8, 11))
        ttk.Label(self.inspector_card, text="发送名称", style="CardBody.TLabel").grid(row=3, column=0, sticky="w")
        self.edit_name_entry = ttk.Entry(self.inspector_card, textvariable=self.edit_name_var)
        self.edit_name_entry.grid(row=4, column=0, sticky="ew", pady=(4, 8))
        self.edit_name_entry.bind("<Return>", lambda _e: self.apply_custom_name())
        self._edit_controls.append(self.edit_name_entry)
        name_button = ttk.Button(self.inspector_card, text="保存名称", style="Secondary.TButton", command=self.apply_custom_name)
        name_button.grid(row=5, column=0, sticky="e")
        self._edit_controls.append(name_button)

        self.footer = tk.Frame(self.root, padx=18, pady=12, highlightthickness=1)
        self.footer.grid(row=4, column=0, sticky="ew")
        self.footer.columnconfigure(0, weight=1)
        self._theme_frames.append(self.footer)
        self.kpi_cards = []
        self.kpi_title_labels = []
        self.kpi_accent_keys = {"total": "kpi_total", "done": "kpi_done", "verify": "kpi_verify", "speed": "kpi_speed"}
        stats = ttk.Frame(self.footer, style="Card.TFrame")
        self.stats_frame = stats
        stats.grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.kpi_val_labels = {}
        for i, (key, label) in enumerate([("total", "共"), ("done", "完成"), ("verify", "待核实")]):
            ttk.Label(stats, text=label, style="CardMuted.TLabel").grid(row=0, column=i * 2)
            value = tk.Label(stats, text="0 张", font=("Microsoft YaHei UI", 9))
            value.grid(row=0, column=i * 2 + 1, padx=(4, 16))
            self.kpi_val_labels[key] = value
        self.kpi_val_labels["speed"] = tk.Label(self.footer, text="0%", font=("Microsoft YaHei UI", 9))
        self.kpi_val_labels["speed"].grid(row=0, column=1, sticky="e", padx=(0, 14))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(self.footer, style="Blue.Horizontal.TProgressbar", maximum=100, variable=self.progress_var)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(9, 1), padx=(0, 14))
        self.footer_actions = ttk.Frame(self.footer, style="Card.TFrame")
        self.footer_actions.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.verify_btn = ttk.Button(self.footer_actions, text="核实当前图片…", style="Warning.TButton", command=self.handle_needs_verify)
        self.verify_btn.grid(row=0, column=0, padx=(0, 9))
        self.verify_btn.grid_remove()
        self.stop_btn = ttk.Button(self.footer_actions, text="停止后续发送  F8", style="Danger.TButton", command=self.request_stop)
        self.stop_btn.grid(row=0, column=1, padx=(0, 9))
        self.start_btn = ttk.Button(self.footer_actions, text="开始发送", style="Primary.TButton", command=self.start_or_resume)
        self.start_btn.grid(row=0, column=2)

        self.bottom_band = tk.Frame(self.root)
        self.bottom_band.grid(row=5, column=0, sticky="ew")
        self.bottom_band.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪。添加图片或拖入文件夹，确认接收对象后开始发送。")
        self.status_bar = tk.Label(self.bottom_band, textvariable=self.status_var, anchor="w", padx=15, pady=6, font=("Microsoft YaHei UI", 9))
        self.status_bar.grid(row=0, column=0, sticky="ew")
        self.status_bar.bind("<Configure>", lambda e: self.status_bar.configure(wraplength=max(300, e.width - 30)))
        ttk.Button(self.bottom_band, text="运行记录…", style="Secondary.TButton", command=self._open_log).grid(row=0, column=1, padx=(6, 14), pady=4)
        self.theme_btn = ttk.Button(self.bottom_band, text="深色", style="Secondary.TButton", command=self.toggle_theme, width=5)
        self.theme_btn.grid(row=0, column=2, padx=(0, 14))
        self._build_log_window()
        self.root.after(100, self._initial_sash)
        self.root.after(150, lambda: self._show_preview_for_index(0))
        self._refresh_task_controls()

    def _label(self, parent: Any, text: str) -> tk.Label:
        label = tk.Label(parent, text=text, font=("Microsoft YaHei UI", 9))
        self._theme_labels.append(label)
        return label

    def _load_brand_icon(self) -> None:
        directory = assets_directory()
        try:
            self._icon_photo = ImageTk.PhotoImage(Image.open(directory / "app-icon.png").resize((32, 32), Image.Resampling.LANCZOS))
            self.logo_label.configure(image=self._icon_photo)
            self.root.iconphoto(True, self._icon_photo)
            if sys.platform == "win32" and (directory / "app-icon.ico").is_file():
                self.root.iconbitmap(str(directory / "app-icon.ico"))
        except (OSError, tk.TclError):
            self.logo_label.configure(text="▣", font=("Segoe UI", 24), width=2)

    def _initial_sash(self) -> None:
        if self._closing:
            return
        try:
            self.panes.sashpos(0, max(440, self.panes.winfo_width() - 310))
        except tk.TclError:
            pass

    def _toggle_preview(self) -> None:
        if self.preview_visible_var.get():
            self.panes.add(self.inspector_card, weight=3)
            self.root.after_idle(self._initial_sash)
            self.root.after_idle(lambda: self._show_preview_for_index(self._current_preview_idx))
        else:
            self._preview_token += 1
            self._preview_loading_key = None
            try:
                self.panes.forget(self.inspector_card)
            except tk.TclError:
                pass

    def _apply_theme(self) -> None:
        self.colors = THEMES[self.theme_name]
        c = self.colors
        self._configure_styles()
        self.header_frame.configure(bg=c["header_bg"])
        self.logo_label.configure(bg=c["header_bg"], fg=c["primary"])
        self.header_title.configure(bg=c["header_bg"], fg=c["header_fg"])
        self.header_sub.configure(bg=c["header_bg"], fg=c["header_sub"])
        self.version_badge.configure(bg=c["badge_bg"], fg=c["badge_fg"])
        for frame in self._theme_frames:
            frame.configure(bg=c["card_bg"], highlightbackground=c["card_border"])
        for label in self._theme_labels:
            label.configure(bg=c["card_bg"], fg=c["text_muted"])
        for key, label in self.kpi_val_labels.items():
            label.configure(bg=c["card_bg"], fg=c[self.kpi_accent_keys[key]])
        self.preview_canvas.configure(bg=c["preview_bg"], highlightbackground=c["card_border"])
        self.log_text.configure(bg=c["log_bg"], fg=c["log_fg"], insertbackground=c["text"], selectbackground=c["tree_sel_bg"], selectforeground=c["tree_sel_fg"])
        self.bottom_band.configure(bg=c["bg"])
        self.status_bar.configure(bg=c["bg"], fg=c["text_muted"])
        self.theme_btn.configure(text="浅色" if self.theme_name == "dark" else "深色")
        for tag in ("pending", "active", "done", "verify"):
            self.tree.tag_configure(tag, background=c[f"tag_{tag}_bg"], foreground=c[f"tag_{tag}_fg"])
        self._set_connection_pill(getattr(self, "_wechat_connected", False), getattr(self, "_wechat_connection_text", "正在检测微信…"))
        for window in self._dialogs:
            try:
                window.configure(bg=c["card_bg"])
            except tk.TclError:
                pass

    def _build_log_window(self) -> None:
        self.log_window = tk.Toplevel(self.root)
        self.log_window.withdraw()
        self.log_window.title("运行记录 · 微信图片顺序发送助手")
        self.log_window.geometry("760x430")
        self.log_window.minsize(520, 300)
        self.log_window.transient(self.root)
        self.log_window.protocol("WM_DELETE_WINDOW", self.log_window.withdraw)
        self._dialogs.append(self.log_window)
        area = ttk.Frame(self.log_window, style="Card.TFrame", padding=12)
        area.pack(fill="both", expand=True)
        self.log_text = tk.Text(area, font=("Consolas", 10), wrap="word", relief=tk.FLAT, state=tk.DISABLED, padx=10, pady=8)
        scroll = ttk.Scrollbar(area, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        ttk.Button(self.log_window, text="保存日志…", style="Secondary.TButton", command=self._save_log).pack(anchor="e", padx=12, pady=(0, 12))

    def _open_log(self) -> None:
        self.log_window.deiconify()
        self.log_window.lift()

    def _save_log(self) -> None:
        chosen = filedialog.asksaveasfilename(parent=self.log_window, title="保存运行记录", defaultextension=".txt", initialfile="WeChatImageSender-log.txt", filetypes=[("文本文件", "*.txt")])
        if chosen:
            try:
                Path(chosen).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc), parent=self.log_window)

    def _append_log(self, msg: str) -> None:
        if self._closing:
            return
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 2500:
            self.log_text.delete("1.0", "501.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _open_settings(self) -> None:
        if self._task_running():
            self.status_var.set("发送中已锁定参数，暂停后可调整。")
            return
        if getattr(self, "settings_window", None) is not None:
            try:
                self.settings_window.lift()
                return
            except tk.TclError:
                pass
        top = tk.Toplevel(self.root)
        self.settings_window = top
        top.title("高级设置")
        top.resizable(False, False)
        top.transient(self.root)
        self._dialogs.append(top)
        frame = ttk.Frame(top, style="Card.TFrame", padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="文件与发送", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        for row, text, variable in [(1, "包含子文件夹", self.subfolders_var), (2, "发送名称保留文件扩展名", self.ext_var)]:
            ttk.Checkbutton(frame, text=text, variable=variable, style="Card.TCheckbutton", command=self.rescan_folder).grid(row=row, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(frame, text="发送方式", style="CardBody.TLabel").grid(row=3, column=0, sticky="w", pady=8)
        mode_label = tk.StringVar(value="原文件（推荐）" if self.clip_mode_var.get() == "CF_HDROP" else "兼容位图")
        mode = ttk.Combobox(frame, state="readonly", textvariable=mode_label, values=["原文件（推荐）", "兼容位图"], width=22)
        mode.grid(row=3, column=1, sticky="ew", padx=(20, 0))
        mode.bind("<<ComboboxSelected>>", lambda _e: self._change_clipboard_mode(mode_label.get()))
        ttk.Label(frame, text="微信发送按键", style="CardBody.TLabel").grid(row=4, column=0, sticky="w", pady=8)
        keys = ttk.Frame(frame, style="Card.TFrame")
        keys.grid(row=4, column=1, sticky="w", padx=(20, 0))
        for text, value in [("Enter", "ENTER"), ("Ctrl+Enter", "CTRL+ENTER")]:
            ttk.Radiobutton(keys, text=text, value=value, variable=self.shortcut_var, style="Card.TRadiobutton").pack(side="left", padx=(0, 12))
        ttk.Separator(frame).grid(row=5, column=0, columnspan=2, sticky="ew", pady=14)
        ttk.Label(frame, text="状态确认", style="CardTitle.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 6))
        for row, text, var, lo, hi, step in [(7, "图片相似度", self.threshold_var, .1, .9, .05), (8, "稳定确认时间（秒）", self.quiet_sec_var, 1.5, 5.0, .25)]:
            ttk.Label(frame, text=text, style="CardBody.TLabel").grid(row=row, column=0, sticky="w", pady=6)
            spin = ttk.Spinbox(frame, textvariable=var, from_=lo, to=hi, increment=step, width=10, command=self._on_params_change)
            spin.grid(row=row, column=1, sticky="e", padx=(20, 0))
            spin.bind("<FocusOut>", lambda _e: self._on_params_change())
        for row, text, variable in [(9, "自动搜索并切换到目标会话", self.auto_search_var), (10, "使用兼容截图方式", self.print_win_var), (11, "发送时显示悬浮进度条", self.hud_enable_var)]:
            ttk.Checkbutton(frame, text=text, variable=variable, command=self._on_params_change, style="Card.TCheckbutton").grid(row=row, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(frame, text="状态不明时暂停核实，避免重复或错序发送。", style="CardMuted.TLabel").grid(row=12, column=0, columnspan=2, sticky="w", pady=(10, 14))
        ttk.Button(frame, text="完成", style="Primary.TButton", command=lambda: (self._on_params_change(), top.destroy())).grid(row=13, column=1, sticky="e")
        top.grab_set()

    def _layout_footer(self) -> None:
        # Two reserved rows keep recovery and stop actions independent of stats.
        pass

    def _change_clipboard_mode(self, label: str) -> None:
        self.clip_mode_var.set("CF_HDROP" if label.startswith("原文件") else "CF_DIB")
        if self.folder_var.get():
            self.rescan_folder()

    def _open_batch_names(self) -> None:
        if self._task_running():
            return
        top = tk.Toplevel(self.root)
        top.title("批量命名")
        top.transient(self.root)
        top.resizable(False, False)
        self._dialogs.append(top)
        frame = ttk.Frame(top, style="Card.TFrame", padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="修改发送名称，不改动磁盘上的原文件。", style="CardBody.TLabel").pack(anchor="w")
        self.batch_tpl_combo = ttk.Combobox(frame, textvariable=self.batch_tpl_var, values=["{num:02d}_{name}", "{num:03d}_{name}", "图{num}_{name}", "{folder}_{num:02d}", "{name}"], width=39)
        self.batch_tpl_combo.pack(fill="x", pady=(12, 8))
        ttk.Label(frame, text="{num:02d} 两位序号    {name} 原名称    {folder} 文件夹", style="CardMuted.TLabel").pack(anchor="w")
        ttk.Button(frame, text="应用到选中项 / 全部", style="Primary.TButton", command=self.apply_batch_template).pack(anchor="e", pady=(18, 0))

    def _task_running(self) -> bool:
        return bool(getattr(self, "_run_active", False) or (self.worker_thread and self.worker_thread.is_alive()))

    def _refresh_task_controls(self) -> None:
        if self._closing or not hasattr(self, "start_btn"):
            return
        running = self._task_running()
        scanning = bool(getattr(self, "_scan_busy", False))
        connecting = bool(getattr(self, "_connection_busy", False))
        locked = running or scanning
        draft_locked = locked or self.current_job is not None
        records = self.current_job.items if self.current_job else []
        needs_verify = any(it.stage == ItemStage.NEEDS_MANUAL_VERIFY.value for it in records)
        done = bool(records) and all(it.stage == ItemStage.COMPLETED.value for it in records)
        has_images = bool(records or self.scanned_items)
        for widget in self._edit_controls:
            try:
                widget.configure(state="disabled" if draft_locked else "normal")
            except tk.TclError:
                pass
        self.new_task_btn.configure(state="disabled" if running else "normal")
        self.history_btn.configure(state="disabled" if locked else "normal")
        self.settings_btn.configure(state="disabled" if draft_locked else "normal")
        self.chat_combo.configure(state="disabled" if running or self.current_job else "normal")
        if self.current_job:
            self.chat_combo.set(self.current_job.target_chat)
            self.current_chat_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if needs_verify:
            self.verify_btn.grid()
        else:
            self.verify_btn.grid_remove()
        self.verify_btn.configure(state="disabled" if running else "normal")
        self.start_btn.configure(text="正在发送…" if running else "正在扫描…" if scanning else "全部完成" if done else "先核实再继续" if needs_verify else "继续发送" if self.current_job else "开始发送", state="disabled" if locked or connecting or not has_images or needs_verify or done else "normal")
        self._layout_footer()

    def _show_preview_for_index(self, idx: int) -> None:
        if self._closing or not self.preview_visible_var.get():
            return
        self._current_preview_idx = idx
        path, name, meta = self._get_item_path_and_name(idx)
        self.edit_name_var.set(name)
        self.meta_var.set(meta if meta else "选择一张图片，查看预览并调整发送名称")
        cw, ch = max(100, self.preview_canvas.winfo_width()), max(80, self.preview_canvas.winfo_height())
        self.meta_label.configure(wraplength=max(170, cw))
        if not path or not path.is_file():
            self._preview_token += 1
            self._preview_loading_key = None
            self.preview_canvas.delete("all")
            self.preview_canvas.create_text(cw // 2, ch // 2, text="添加图片或拖入文件夹\n双击预览可查看大图", fill=self.colors["text_muted"], font=("Microsoft YaHei UI", 9), justify="center", width=max(100, cw - 20))
            return
        try:
            key = (str(path), path.stat().st_mtime_ns, cw, ch)
        except OSError:
            return
        if key in self._preview_cache:
            self._preview_token += 1
            self._preview_loading_key = None
            self._preview_cache.move_to_end(key)
            self._draw_preview(self._preview_cache[key], cw, ch)
            return
        if self._preview_loading_key == key:
            return
        self._preview_token += 1
        token = self._preview_token
        self._preview_loading_key = key

        def worker() -> None:
            try:
                with Image.open(path) as source:
                    source.seek(0)
                    source.draft("RGB", (cw * 2, ch * 2))
                    image = ImageOps.exif_transpose(source)
                    image.thumbnail((max(60, cw - 20), max(50, ch - 20)), Image.Resampling.LANCZOS)
                    if image.mode == "RGBA" or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        thumbnail = Image.new("RGB", rgba.size, "white")
                        thumbnail.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        thumbnail = image.convert("RGB").copy()
                def apply() -> None:
                    if self._closing or token != self._preview_token:
                        return
                    self._preview_loading_key = None
                    photo = ImageTk.PhotoImage(thumbnail)
                    self._preview_cache[key] = photo
                    while len(self._preview_cache) > 12:
                        self._preview_cache.popitem(last=False)
                    self._draw_preview(photo, cw, ch)
                if not self._closing:
                    self.root.after(0, apply)
            except Exception as exc:
                def fail(message=str(exc)) -> None:
                    if self._closing or token != self._preview_token:
                        return
                    self._preview_loading_key = None
                    self.preview_canvas.delete("all")
                    self.preview_canvas.create_text(cw // 2, ch // 2, text=f"预览不可用\n{message}", fill=self.colors["text_muted"], width=max(100, cw - 20), justify="center")
                if not self._closing:
                    try:
                        self.root.after(0, fail)
                    except RuntimeError:
                        pass
        threading.Thread(target=worker, daemon=True, name="ImagePreview").start()

    def _draw_preview(self, photo: Any, cw: int, ch: int) -> None:
        self._preview_photo = photo
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(cw // 2, ch // 2, image=photo)

    def _get_item_path_and_name(self, idx: int):
        if self.current_job and 0 <= idx < len(self.current_job.items):
            rec = self.current_job.items[idx]
            label = {ItemStage.PENDING.value: "待发送", ItemStage.IMAGE_SUBMITTED.value: "正在确认图片", ItemStage.IMAGE_CONFIRMED.value: "图片已确认", ItemStage.NAME_SUBMITTED.value: "正在确认名称", ItemStage.COMPLETED.value: "已完成", ItemStage.NEEDS_MANUAL_VERIFY.value: "待人工核实"}.get(rec.stage, rec.stage)
            return Path(rec.file_path), rec.display_name, f"第 {idx + 1} 张 · {label}\n{rec.relative_name}"
        if 0 <= idx < len(self.scanned_items):
            item = self.scanned_items[idx]
            try:
                size = f" · {item.path.stat().st_size / 1024:.0f} KB"
            except OSError:
                size = ""
            return item.path, self._get_effective_display_name(idx, item), f"{item.width} × {item.height} · {item.format_name}{size}\n双击预览可查看大图"
        return None, "", ""

    def _show_mini_hud(self) -> None:
        if self._mini_hud is not None:
            return
        c = self.colors
        hud = tk.Toplevel(self.root)
        hud.overrideredirect(True)
        hud.attributes("-topmost", True)
        hud.geometry(f"430x64+{max(10, self.root.winfo_screenwidth() - 450)}+20")
        hud.configure(bg=c["card_border"])
        frame = tk.Frame(hud, bg=c["card_bg"], padx=12, pady=10)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        self._mini_hud_label = tk.Label(frame, text="正在准备发送…", bg=c["card_bg"], fg=c["text"], font=("Microsoft YaHei UI", 9), anchor="w")
        self._mini_hud_label.pack(side="left", fill="both", expand=True)
        tk.Button(frame, text="停止 F8", command=self.request_stop, bg=c["secondary_btn"], fg=c["danger"], relief=tk.FLAT, padx=10, pady=5).pack(side="right")
        drag = {}
        def begin(event):
            drag.update(x=event.x_root - hud.winfo_x(), y=event.y_root - hud.winfo_y())
        def move(event):
            if "x" in drag:
                hud.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")
        self._mini_hud_label.bind("<ButtonPress-1>", begin)
        self._mini_hud_label.bind("<B1-Motion>", move)
        self._mini_hud = hud

    def _on_close(self) -> None:
        if getattr(self, "_shutdown_pending", False):
            return
        self._shutdown_pending = True
        self.engine.stop_requested = True
        if self._task_running():
            self.status_var.set("正在停止发送并保存任务，请稍候…")
            self.start_btn.configure(state="disabled")
            self.root.after(80, self._finish_close)
        else:
            self._finish_close()

    def _finish_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.root.after(80, self._finish_close)
            return
        self._closing = True
        self._preview_token += 1
        self._stop_global_hotkey_listener()
        self._hide_mini_hud()
        self.adapter.close()
        self.root.destroy()
