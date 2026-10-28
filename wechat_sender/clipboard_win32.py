# -*- coding: utf-8 -*-
"""
双模剪贴板控制器，带文本或位图快照备份与恢复：
1. CF_HDROP（文件路径模式）：传递源文件路径，不在本程序中重新编码图片；微信如何传输由客户端决定；
2. CF_DIB（静态位图模式，兼容）：将解码后的 RGB 位图写入剪贴板；
3. CF_UNICODETEXT：写入待发送的图片名称文本；
4. snapshot() / restore()：在队列启动前备份用户剪贴板中的文本或位图，队列暂停/完成后无感恢复。
"""

import ctypes
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

from .image_utils import image_to_dib_bytes, load_static_image


class ClipboardBusyError(RuntimeError):
    """The clipboard could not be opened; no paste or send was attempted."""


CF_TEXT = 1
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
GHND = GMEM_MOVEABLE | GMEM_ZEROINIT


@dataclass(frozen=True)
class ClipboardSnapshot:
    """保存发送队列启动前的系统剪贴板数据快照。"""
    u_format: int
    raw_bytes: bytes
    mode_name: str
    payload_preview: object = None


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
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    utf16_path = abs_path.encode("utf-16le") + b"\x00\x00\x00\x00"
    return header + utf16_path


class ClipboardManager:
    """跨平台抽象 + Windows 原生 Win32 API 剪贴板管理器（含快照备份与自动恢复）。"""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run or (sys.platform != "win32")
        self._last_mode: Optional[str] = None
        self._last_payload: Optional[object] = None
        self._last_raw_format: int = 0
        self._last_raw_bytes: bytes = b""

    @property
    def last_mode(self) -> Optional[str]:
        return self._last_mode

    @property
    def last_payload(self) -> Optional[object]:
        return self._last_payload

    def _open_clipboard_with_retry(self, retries: int = 5, delay_sec: float = 0.02) -> bool:
        user32 = ctypes.windll.user32
        for _ in range(retries):
            if user32.OpenClipboard(0):
                return True
            time.sleep(delay_sec)
        return False

    def _write_win32_clipboard(self, u_format: int, raw_bytes: bytes) -> None:
        self._last_raw_format = u_format
        self._last_raw_bytes = raw_bytes
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

        if not self._open_clipboard_with_retry():
            raise ClipboardBusyError("无法打开系统剪贴板，可能正被其他程序占用")
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

    def snapshot(self) -> Optional[ClipboardSnapshot]:
        """在发送队列开始前备份当前系统剪贴板（优先保存 CF_UNICODETEXT 文本或 CF_DIB 图像）。"""
        if self.dry_run:
            if not self._last_mode or not self._last_raw_bytes:
                return None
            return ClipboardSnapshot(
                u_format=self._last_raw_format,
                raw_bytes=self._last_raw_bytes,
                mode_name=self._last_mode,
                payload_preview=self._last_payload,
            )

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        user32.IsClipboardFormatAvailable.restype = ctypes.c_int
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        if not self._open_clipboard_with_retry():
            return None
        try:
            for fmt, mode_name in (
                (CF_UNICODETEXT, "CF_UNICODETEXT"),
                (CF_DIB, "CF_DIB"),
                (CF_HDROP, "CF_HDROP"),
            ):
                if user32.IsClipboardFormatAvailable(fmt):
                    h_data = user32.GetClipboardData(fmt)
                    if not h_data:
                        continue
                    size = int(kernel32.GlobalSize(h_data) or 0)
                    if size <= 0 or size > 64 * 1024 * 1024:
                        continue
                    ptr = kernel32.GlobalLock(h_data)
                    if not ptr:
                        continue
                    try:
                        raw = ctypes.string_at(ptr, size)
                        preview: object = len(raw)
                        if fmt == CF_UNICODETEXT:
                            preview = raw.decode("utf-16le", errors="ignore").rstrip("\x00")
                        return ClipboardSnapshot(
                            u_format=fmt,
                            raw_bytes=raw,
                            mode_name=mode_name,
                            payload_preview=preview,
                        )
                    finally:
                        kernel32.GlobalUnlock(h_data)
        except Exception:
            return None
        finally:
            user32.CloseClipboard()
        return None

    def restore(self, snap: Optional[ClipboardSnapshot]) -> bool:
        """将快照恢复回系统剪贴板。"""
        if snap is None or not snap.raw_bytes:
            return False
        try:
            self._write_win32_clipboard(snap.u_format, snap.raw_bytes)
            self._last_mode = snap.mode_name
            self._last_payload = snap.payload_preview
            return True
        except Exception:
            return False

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
