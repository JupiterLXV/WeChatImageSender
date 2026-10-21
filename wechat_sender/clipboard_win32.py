# -*- coding: utf-8 -*-
"""
双模剪贴板控制器：
1. CF_HDROP（文件句柄模式，推荐）：模拟 Windows 资源管理器复制文件，微信直接读取原图，零重编码损耗，速度最快；
2. CF_DIB（静态位图模式，兼容）：将解码后的 RGB 位图写入剪贴板；
3. CF_UNICODETEXT：写入待发送的图片名称文本。
"""

from __future__ import annotations

import ctypes
import struct
import sys
from pathlib import Path
from typing import Optional

from PIL import Image
from .image_utils import image_to_dib_bytes, load_static_image

CF_TEXT = 1
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
GHND = GMEM_MOVEABLE | GMEM_ZEROINIT


def build_cf_hdrop_payload(file_path: Path) -> bytes:
    """
    构造标准的 Windows DROPFILES (CF_HDROP) 二进制结构体：
    struct DROPFILES {
        DWORD pFiles; // 偏移量 = 20 字节
        POINT pt;     // (0, 0)
        BOOL  fNC;    // 0
        BOOL  fWide;  // 1 (Unicode UTF-16LE)
    };
    随后紧跟以双 NULL 结尾的 UTF-16LE 完整文件路径。
    """
    abs_path = str(Path(file_path).resolve())
    # DROPFILES 头部共 20 字节: pFiles=20, pt.x=0, pt.y=0, fNC=0, fWide=1
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    utf16_path = abs_path.encode("utf-16le") + b"\x00\x00\x00\x00"
    return header + utf16_path


class ClipboardManager:
    """跨平台抽象 + Windows 原生 Win32 API 剪贴板管理器。"""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run or (sys.platform != "win32")
        self._last_mode: Optional[str] = None
        self._last_payload: Optional[object] = None

    @property
    def last_mode(self) -> Optional[str]:
        return self._last_mode

    @property
    def last_payload(self) -> Optional[object]:
        return self._last_payload

    def _write_win32_clipboard(self, u_format: int, raw_bytes: bytes) -> None:
        if self.dry_run:
            return

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p

        if not user32.OpenClipboard(0):
            raise RuntimeError("无法打开系统剪贴板，可能正被其他程序占用")
        try:
            user32.EmptyClipboard()
            h_mem = kernel32.GlobalAlloc(GHND, len(raw_bytes))
            if not h_mem:
                raise MemoryError("GlobalAlloc 分配剪贴板内存失败")
            locked_ptr = kernel32.GlobalLock(h_mem)
            if not locked_ptr:
                raise MemoryError("GlobalLock 锁定剪贴板内存失败")
            try:
                ctypes.memmove(locked_ptr, raw_bytes, len(raw_bytes))
            finally:
                kernel32.GlobalUnlock(h_mem)
            if not user32.SetClipboardData(u_format, h_mem):
                raise RuntimeError("SetClipboardData 写入剪贴板失败")
        finally:
            user32.CloseClipboard()

    def set_image_file_hdrop(self, file_path: Path) -> bytes:
        """以 CF_HDROP 文件引用方式写入剪贴板（推荐：零失真、免解码、速度极快）。"""
        payload = build_cf_hdrop_payload(file_path)
        self._write_win32_clipboard(CF_HDROP, payload)
        self._last_mode = "CF_HDROP"
        self._last_payload = str(Path(file_path).resolve())
        return payload

    def set_image_dib(self, file_path_or_img: Path | Image.Image) -> bytes:
        """以 CF_DIB 静态位图方式写入剪贴板（兼容模式）。"""
        if isinstance(file_path_or_img, Image.Image):
            img = file_path_or_img
        else:
            img, _, _ = load_static_image(Path(file_path_or_img))
        dib_bytes = image_to_dib_bytes(img)
        self._write_win32_clipboard(CF_DIB, dib_bytes)
        self._last_mode = "CF_DIB"
        self._last_payload = len(dib_bytes)
        return dib_bytes

    def set_text(self, text: str) -> bytes:
        """以 CF_UNICODETEXT 写入文本（用于粘贴发送图片名称）。"""
        raw = text.encode("utf-16le") + b"\x00\x00"
        self._write_win32_clipboard(CF_UNICODETEXT, raw)
        self._last_mode = "CF_UNICODETEXT"
        self._last_payload = text
        return raw
