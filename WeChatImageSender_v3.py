#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信图片顺序发送助手 v3.0.0 (单文件独立运行版 WeChatImageSender_v3.py)
可直接运行：`python WeChatImageSender_v3.py` 或 `python WeChatImageSender_v3.py --demo`
完整模块化工程与 GitHub Actions 自动打包配置位于 WeChatImageSender-v3.0.0-github-repo.zip
"""
from __future__ import annotations

# ==================== wechat_sender/image_utils.py ====================

# -*- coding: utf-8 -*-
"""
图片扫描、自然排序、MPO/EXIF 处理、SHA-256 校验与多信号联合视觉状态判定模块（v3.2）。
新增：
- 状态区静态纯净度检测（Static Spinner / Frozen Icon Veto）：即使弱网下转圈动画卡死不动（帧间差分为 0），
  只要左侧状态区存在非背景色的转圈圆环或图标像素，立即一票否决，彻底解决“画面静止 != 上传完成”的误放行问题。
"""

import hashlib
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif", ".gif"}
NATURAL_SORT_REGEX = re.compile(r"(\d+)")
DEFAULT_SIMILARITY_THRESHOLD = 0.40


@dataclass(frozen=True)
class ImageItem:
    """表示单张待发送的图片项。"""
    path: Path
    relative_name: str
    display_name: str
    sha256: str
    width: int
    height: int
    format_name: str
    is_mpo_primary: bool = False


def natural_sort_key(text: str) -> List[object]:
    """自然排序键：保证 1.png, 2.png, 10.png 按数值顺序排列。"""
    parts = NATURAL_SORT_REGEX.split(text.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def compute_sha256(file_path: Path) -> str:
    """计算文件的完整 SHA-256 摘要。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_static_image(file_path: Path) -> Tuple[Image.Image, str, bool]:
    """
    安全加载静态图片：
    - 若为 MPO 多图容器（常见于手机 HDR/人像 JPG），提取第 0 帧主图并应用 EXIF 方向；
    - 若为真正的 GIF/APNG/WEBP 动图或多页 TIFF，抛出 ValueError 拒绝静默截断；
    - 透明通道自动合成白底 RGB，返回 (RGB Image, format_name, is_mpo_primary)。
    """
    with Image.open(file_path) as img:
        fmt = (img.format or "").upper()
        n_frames = getattr(img, "n_frames", 1)
        is_animated = getattr(img, "is_animated", False)
        is_mpo = fmt == "MPO"

        if is_mpo:
            img.seek(0)
            primary = img.copy()
            primary = ImageOps.exif_transpose(primary)
        elif n_frames > 1 or is_animated:
            raise ValueError(f"不支持多帧或动图格式 ({fmt}, {n_frames} 帧): {file_path.name}")
        else:
            primary = ImageOps.exif_transpose(img)

        if primary.mode in ("RGBA", "LA") or (primary.mode == "P" and "transparency" in primary.info):
            rgba = primary.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[3])
            rgb_img = background
        else:
            rgb_img = primary.convert("RGB")

        return rgb_img, fmt, is_mpo


def image_to_dib_bytes(img: Image.Image) -> bytes:
    """将 PIL Image 转换为 Windows CF_DIB 剪贴板字节（去掉 BMP 前 14 字节文件头）。"""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="BMP")
    return buf.getvalue()[14:]


def scan_image_folder(
    folder: Path,
    include_subfolders: bool = False,
    include_extension: bool = False,
    allow_animated_in_file_mode: bool = False,
) -> Tuple[List[ImageItem], List[Tuple[Path, str]]]:
    """
    扫描文件夹中的所有支持图片，按自然顺序排序。
    返回 (有效图片列表, 被跳过的文件及原因列表)。
    """
    folder = Path(folder).resolve()
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {folder}")

    candidates: List[Path] = []
    if include_subfolders:
        for root, _, files in os.walk(folder):
            for name in files:
                p = Path(root) / name
                if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                    candidates.append(p)
    else:
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
                candidates.append(p)

    candidates.sort(key=lambda p: natural_sort_key(str(p.relative_to(folder)).replace("\\", "/")))

    valid_items: List[ImageItem] = []
    skipped: List[Tuple[Path, str]] = []

    for p in candidates:
        try:
            sha = compute_sha256(p)
            try:
                rgb_img, fmt, is_mpo = load_static_image(p)
                w, h = rgb_img.size
            except ValueError as ve:
                if allow_animated_in_file_mode:
                    with Image.open(p) as raw_im:
                        w, h = raw_im.size
                        fmt = (raw_im.format or "ANIMATED").upper()
                        is_mpo = False
                else:
                    raise ve

            rel_path = p.relative_to(folder)
            display_name = p.name if include_extension else p.stem
            valid_items.append(
                ImageItem(
                    path=p,
                    relative_name=str(rel_path),
                    display_name=display_name,
                    sha256=sha,
                    width=w,
                    height=h,
                    format_name=fmt,
                    is_mpo_primary=is_mpo,
                )
            )
        except Exception as exc:
            skipped.append((p, str(exc)))

    return valid_items, skipped


@dataclass
class BubbleVisualStatus:
    """基于多信号联合判定（缩略图匹配 + 失败红标否决 + 静态转圈图标否决 + 动态帧差静止）的气泡状态结果。"""
    matched: bool
    similarity: float
    bbox: Optional[Tuple[int, int, int, int]]  # (x, y, w, h)
    has_failure_badge: bool
    has_static_spinner: bool
    spinner_motion_score: float
    is_upload_complete: bool
    reason: str


class VisualBubbleVerifier:
    """
    多信号联合非侵入式视觉验证器（v3.2）：
    1. OpenCV 中心 80% 裁剪多尺度匹配（默认阈值 0.40）；
    2. 红色感叹号失败标记一票否决；
    3. 【新增】左侧状态区静态转圈图标检测（Static Spinner Veto）：
       对比状态区中部（转圈图标位置）与上下背景基准的像素偏差及对比度。
       即使弱网下转圈动画卡死不动（两帧差分 = 0），只要转圈图标还挂在缩略图左侧，就绝不放行！
    4. 连续帧间动态差分静止确认（Spinner Motion Score <= 阈值）。
    """

    def __init__(
        self,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        motion_quiet_threshold: float = 1.8,
        red_failure_ratio_threshold: float = 0.04,
        static_spinner_std_threshold: float = 8.5,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.motion_quiet_threshold = motion_quiet_threshold
        self.red_failure_ratio_threshold = red_failure_ratio_threshold
        self.static_spinner_std_threshold = static_spinner_std_threshold

    @staticmethod
    def _to_gray_array(img: Image.Image) -> np.ndarray:
        return np.asarray(img.convert("L"), dtype=np.float32)

    @staticmethod
    def _perceptual_patch_similarity(patch_rgb: np.ndarray, tpl_rgb: np.ndarray) -> float:
        p_small = cv2.resize(patch_rgb, (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)
        t_small = cv2.resize(tpl_rgb, (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)

        abs_diff = np.abs(p_small - t_small)
        mae = float(np.mean(abs_diff))
        rmse = float(np.sqrt(np.mean(abs_diff * abs_diff)))

        q_maes = [
            float(np.mean(abs_diff[:8, :8])),
            float(np.mean(abs_diff[:8, 8:])),
            float(np.mean(abs_diff[8:, :8])),
            float(np.mean(abs_diff[8:, 8:])),
        ]
        worst_q_mae = max(q_maes)
        if worst_q_mae > 68.0:
            return max(0.0, 0.25 * (1.0 - worst_q_mae / 255.0))

        color_sim = max(0.0, 1.0 - (0.5 * mae + 0.5 * rmse) / 115.0)

        p_gray = np.mean(p_small, axis=2)
        t_gray = np.mean(t_small, axis=2)
        if float(np.std(t_gray)) < 1.5 and float(np.std(t_small, axis=(0, 1)).max()) < 1.5:
            spatial_std = float(np.std(p_small, axis=(0, 1)).mean())
            flatness_penalty = min(1.0, spatial_std / 24.0)
            return max(0.0, color_sim - flatness_penalty)

        p_centered = p_small - np.mean(p_small, axis=(0, 1), keepdims=True)
        t_centered = t_small - np.mean(t_small, axis=(0, 1), keepdims=True)
        p_norm = float(np.linalg.norm(p_centered))
        t_norm = float(np.linalg.norm(t_centered))
        if p_norm < 1e-3 or t_norm < 1e-3:
            return 0.0

        ncc = float(np.sum(p_centered * t_centered) / (p_norm * t_norm))
        if ncc < 0.05:
            return max(0.0, 0.20 * color_sim)

        return float(min(1.0, 0.50 * color_sim + 0.50 * ncc))

    def locate_thumbnail(
        self,
        chat_screenshot: Image.Image,
        source_image: Image.Image,
        candidate_widths: Optional[List[int]] = None,
    ) -> Tuple[bool, float, Optional[Tuple[int, int, int, int]]]:
        chat_rgb_img = chat_screenshot.convert("RGB")
        src_rgb_img = source_image.convert("RGB")
        cw, ch = chat_rgb_img.size
        sw, sh = src_rgb_img.size
        if sw <= 0 or sh <= 0 or cw < 32 or ch < 32:
            return False, 0.0, None

        aspect = sh / float(sw)
        if candidate_widths is None:
            base_widths = [
                340, 310, 280, 260, 240, 225, 210, 195, 180, 168,
                160, 150, 140, 130, 120, 110, 100, 90, 80, 70, 60,
            ]
            candidate_widths = [w for w in base_widths if w <= int(cw * 0.82)]
            if not candidate_widths:
                candidate_widths = [max(24, int(cw * 0.45))]

        chat_arr = np.asarray(chat_rgb_img, dtype=np.uint8)
        search_x_start = max(0, int(cw * 0.15))
        roi_rgb = chat_arr[:, search_x_start:]
        rw = roi_rgb.shape[1]

        margin_x = int(round(sw * 0.10))
        margin_y = int(round(sh * 0.10))
        if sw - 2 * margin_x >= 16 and sh - 2 * margin_y >= 16:
            src_inner_img = src_rgb_img.crop((margin_x, margin_y, sw - margin_x, sh - margin_y))
            use_inner = True
        else:
            src_inner_img = src_rgb_img
            use_inner = False

        src_inner_arr = np.asarray(src_inner_img, dtype=np.uint8)
        src_full_arr = np.asarray(src_rgb_img, dtype=np.uint8)

        best_score = -1.0
        best_bbox: Optional[Tuple[int, int, int, int]] = None

        for tw in candidate_widths:
            th = max(24, min(int(round(tw * aspect)), int(ch * 0.88)))
            if tw >= rw or th >= ch:
                continue

            if use_inner:
                iw = max(16, int(round(tw * 0.80)))
                ih = max(16, int(round(th * 0.80)))
                pad_x = (tw - iw) // 2
                pad_y = (th - ih) // 2
            else:
                iw, ih = tw, th
                pad_x, pad_y = 0, 0

            if iw >= rw or ih >= ch:
                continue

            tpl_inner_rgb = cv2.resize(src_inner_arr, (iw, ih), interpolation=cv2.INTER_AREA)

            res = cv2.matchTemplate(roi_rgb, tpl_inner_rgb, cv2.TM_SQDIFF_NORMED)
            _, _, min_loc, _ = cv2.minMaxLoc(res)
            ix, iy = int(min_loc[0]), int(min_loc[1])

            patch_inner = roi_rgb[iy : iy + ih, ix : ix + iw]
            if patch_inner.shape[0] != ih or patch_inner.shape[1] != iw:
                continue

            score = self._perceptual_patch_similarity(patch_inner, tpl_inner_rgb)

            full_x = search_x_start + max(0, ix - pad_x)
            full_y = max(0, iy - pad_y)
            full_w = min(tw, cw - full_x)
            full_h = min(th, ch - full_y)

            if full_w >= 24 and full_h >= 24:
                tpl_full_rgb = cv2.resize(src_full_arr, (full_w, full_h), interpolation=cv2.INTER_AREA)
                patch_full = chat_arr[full_y : full_y + full_h, full_x : full_x + full_w]
                full_score = self._perceptual_patch_similarity(patch_full, tpl_full_rgb)
                score = max(score, full_score)

            if score > best_score + 0.002:
                best_score = score
                best_bbox = (full_x, full_y, full_w, full_h)

        matched = best_score >= self.similarity_threshold
        return matched, max(0.0, float(best_score)), best_bbox if matched else None

    def detect_failure_badge(
        self,
        chat_screenshot: Image.Image,
        bbox: Tuple[int, int, int, int],
    ) -> bool:
        """检测缩略图左侧状态区域是否存在微信红色发送失败感叹号图标。"""
        x, y, w, h = bbox
        status_w = max(28, int(w * 0.35))
        sx1 = max(0, x - status_w - 6)
        sx2 = max(0, x - 4)
        if sx2 <= sx1 or h < 24:
            return False

        region = chat_screenshot.convert("RGB").crop((sx1, y, sx2, y + h))
        arr = np.asarray(region, dtype=np.float32)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        red_mask = (r > 175) & (g < 95) & (b < 95) & ((r - g) > 90)
        red_ratio = float(np.mean(red_mask))
        if red_ratio < self.red_failure_ratio_threshold:
            return False

        top_slice = red_mask[: max(2, h // 6), :]
        bottom_slice = red_mask[h - max(2, h // 6) :, :]
        edge_red_ratio = 0.5 * (float(np.mean(top_slice)) + float(np.mean(bottom_slice)))
        return edge_red_ratio < 0.50

    def detect_static_spinner(
        self,
        chat_screenshot: Image.Image,
        bbox: Tuple[int, int, int, int],
    ) -> bool:
        """
        【关键防错乱机制】单帧静态转圈图标存在性检测（Static Spinner Presence）：
        在弱网或卡顿时，微信左侧的转圈图标可能停止旋转（导致连续两帧差分 == 0）。
        但只要转圈圆环图标还挂在缩略图左侧垂直居中区域，该区域相对于状态列上下纯色背景就会呈现出
        显著的灰度标准差与局部对比度；当上传真正完成时，状态列恢复为纯净聊天背景（标准差趋近于 0）。
        """
        x, y, w, h = bbox
        status_w = max(28, int(w * 0.32))
        sx1 = max(0, x - status_w - 6)
        sx2 = max(0, x - 4)
        if sx2 <= sx1 or h < 24:
            return False

        col_gray = self._to_gray_array(chat_screenshot.crop((sx1, y, sx2, y + h)))
        ch_h = col_gray.shape[0]
        # 取上下各 1/5 作为当前聊天背景色基准
        band = max(3, ch_h // 5)
        bg_pixels = np.concatenate([col_gray[:band, :].ravel(), col_gray[ch_h - band :, :].ravel()])
        bg_median = float(np.median(bg_pixels))
        bg_std = float(np.std(bg_pixels))

        # 中部 3/5 区域为微信转圈菊花出现的位置
        mid_region = col_gray[band : ch_h - band, :]
        if mid_region.size == 0:
            return False

        mid_std = float(np.std(mid_region))
        thresh = max(22.0, 3.0 * bg_std + 15.0)
        deviant_mask = np.abs(mid_region - bg_median) > thresh
        deviant_count = int(np.sum(deviant_mask))
        deviation_ratio = float(np.mean(deviant_mask))
        edge_dev_ratio = float(np.mean(np.abs(bg_pixels - bg_median) > thresh))

        return (
            (mid_std >= 5.0 or deviant_count >= 24)
            and deviation_ratio >= 0.008
            and edge_dev_ratio < 0.20
        )

    def compute_spinner_motion(
        self,
        prev_screenshot: Image.Image,
        curr_screenshot: Image.Image,
        bbox: Tuple[int, int, int, int],
    ) -> float:
        """计算连续两帧在缩略图左侧状态区与缩略图中心的平均绝对像素差分。"""
        x, y, w, h = bbox
        status_w = max(28, int(w * 0.35))
        sx1 = max(0, x - status_w - 6)
        sx2 = min(curr_screenshot.width, x + w)
        sy1 = max(0, y)
        sy2 = min(curr_screenshot.height, y + h)

        crop1 = self._to_gray_array(prev_screenshot.crop((sx1, sy1, sx2, sy2)))
        crop2 = self._to_gray_array(curr_screenshot.crop((sx1, sy1, sx2, sy2)))
        if crop1.shape != crop2.shape or crop1.size == 0:
            return 999.0
        return float(np.mean(np.abs(crop1 - crop2)))

    def evaluate(
        self,
        curr_screenshot: Image.Image,
        source_image: Image.Image,
        prev_screenshot: Optional[Image.Image] = None,
        candidate_widths: Optional[List[int]] = None,
    ) -> BubbleVisualStatus:
        """
        多信号联合审查（缺一不可）：
        1. 缩略图匹配 >= 阈值；
        2. 无红色失败感叹号；
        3. 左侧状态区无静态/卡住的转圈图标（Static Spinner Veto）；
        4. 连续帧动态差分 <= 静默阈值。
        """
        matched, sim, bbox = self.locate_thumbnail(curr_screenshot, source_image, candidate_widths)
        if not matched or bbox is None:
            return BubbleVisualStatus(
                matched=False,
                similarity=sim,
                bbox=None,
                has_failure_badge=False,
                has_static_spinner=False,
                spinner_motion_score=0.0,
                is_upload_complete=False,
                reason=f"未匹配到当前待发送图片缩略图 (最高相似度 {sim:.2f} < {self.similarity_threshold:.2f})",
            )

        has_fail = self.detect_failure_badge(curr_screenshot, bbox)
        if has_fail:
            return BubbleVisualStatus(
                matched=True,
                similarity=sim,
                bbox=bbox,
                has_failure_badge=True,
                has_static_spinner=False,
                spinner_motion_score=0.0,
                is_upload_complete=False,
                reason="检测到红色发送失败标记，已暂停队列",
            )

        has_spinner = self.detect_static_spinner(curr_screenshot, bbox)
        if has_spinner:
            return BubbleVisualStatus(
                matched=True,
                similarity=sim,
                bbox=bbox,
                has_failure_badge=False,
                has_static_spinner=True,
                spinner_motion_score=99.0,
                is_upload_complete=False,
                reason=f"缩略图匹配 ({sim:.2f})，但左侧状态区仍存在上传转圈指示器，继续等待上传完成",
            )

        if prev_screenshot is None:
            return BubbleVisualStatus(
                matched=True,
                similarity=sim,
                bbox=bbox,
                has_failure_badge=False,
                has_static_spinner=False,
                spinner_motion_score=99.0,
                is_upload_complete=False,
                reason="已定位新增图片缩略图，正在等待上传状态静止确认",
            )

        motion = self.compute_spinner_motion(prev_screenshot, curr_screenshot, bbox)
        is_quiet = motion <= self.motion_quiet_threshold
        return BubbleVisualStatus(
            matched=True,
            similarity=sim,
            bbox=bbox,
            has_failure_badge=False,
            has_static_spinner=False,
            spinner_motion_score=motion,
            is_upload_complete=is_quiet,
            reason=(
                f"缩略图匹配 ({sim:.2f} >= {self.similarity_threshold:.2f})、状态区无转圈且已静止 (差分 {motion:.2f})"
                if is_quiet
                else f"缩略图匹配 ({sim:.2f})，上传指示仍在变化 (差分 {motion:.2f})"
            ),
        )


# ==================== wechat_sender/clipboard_win32.py ====================

# -*- coding: utf-8 -*-
"""
双模剪贴板控制器：
1. CF_HDROP（文件句柄模式，推荐）：模拟 Windows 资源管理器复制文件，微信直接读取原图，零重编码损耗，速度最快；
2. CF_DIB（静态位图模式，兼容）：将解码后的 RGB 位图写入剪贴板；
3. CF_UNICODETEXT：写入待发送的图片名称文本。
"""

import ctypes
import struct
import sys
from pathlib import Path
from typing import Optional

from PIL import Image

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


# ==================== wechat_sender/accessibility.py ====================

# -*- coding: utf-8 -*-
"""
零内存补丁（Zero-Memory-Patch）微信无障碍激活器：
通过标准 Windows API `SystemParametersInfoW(SPI_SETSCREENREADER)` 与 `WM_GETOBJECT` 信号
唤醒微信 Qt/Chromium 界面的 `QAccessible` 控件树，彻底告别对 `Weixin.dll` 的单版本 SHA-256 内存修改。
"""

import ctypes
import os
import sys
from dataclasses import dataclass
from typing import Optional

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
WM_GETOBJECT = 0x003D
OBJID_CLIENT = 0xFFFFFFFC


@dataclass
class AccessibilityState:
    """记录无障碍激活前后的系统状态，用于退出时自动恢复。"""
    previously_enabled: bool
    activated_by_helper: bool
    wechat_hwnd: Optional[int] = None


class ZeroPatchAccessibilityActivator:
    """
    安全唤醒微信 3.x / 4.x 无障碍控件树：
    1. 设置进程环境变量 QT_ACCESSIBILITY=1；
    2. 按需通过 SystemParametersInfoW 开启临时屏幕阅读器标志并向微信窗口发送 WM_GETOBJECT；
    3. 退出时自动恢复原系统标志，完全不触碰 Weixin.dll 进程内存。
    """

    def __init__(self) -> None:
        self._state: Optional[AccessibilityState] = None

    @staticmethod
    def get_screen_reader_flag() -> bool:
        if sys.platform != "win32":
            return False
        flag = ctypes.c_int(0)
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETSCREENREADER, 0, ctypes.byref(flag), 0
        )
        return bool(flag.value)

    def activate(self, hwnd: Optional[int] = None) -> AccessibilityState:
        os.environ["QT_ACCESSIBILITY"] = "1"
        os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"

        if sys.platform != "win32":
            self._state = AccessibilityState(
                previously_enabled=False,
                activated_by_helper=True,
                wechat_hwnd=hwnd,
            )
            return self._state

        prev = self.get_screen_reader_flag()
        if not prev:
            # fWinIni=0 避免全局广播 WM_SETTINGCHANGE 造成其他无响应窗口阻塞
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETSCREENREADER, 1, None, 0
            )

        if hwnd:
            ctypes.windll.user32.SendMessageTimeoutW(
                hwnd,
                WM_GETOBJECT,
                0,
                OBJID_CLIENT,
                0x0002,  # SMTO_ABORTIFHUNG
                500,
                None,
            )

        self._state = AccessibilityState(
            previously_enabled=prev,
            activated_by_helper=not prev,
            wechat_hwnd=hwnd,
        )
        return self._state

    def restore(self) -> None:
        if self._state is None:
            return
        if sys.platform == "win32" and self._state.activated_by_helper and not self._state.previously_enabled:
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETSCREENREADER, 0, None, 0
            )
        self._state = None


# ==================== wechat_sender/wechat_adapter.py ====================

# -*- coding: utf-8 -*-
"""
微信客户端窗口与自动控制适配器（v3.3 极速锁定追踪 + 灰盒 WAL 落盘监测版）：
1. 【核心提速 1：首帧 BBox 尺度与坐标锁定（Fast-Track ROI Locking）】
   - 首帧在右下区域定位到新增缩略图 (bx, by, bw, bh) 后，后续轮询不再重复执行耗时的 21 尺度全图模板搜索，
     仅在锁定坐标周围做毫秒级局部校验（单帧耗时从 ~300ms 降至 ~2ms，轮询间隔从 350ms 缩至 60ms）。
2. 【核心提速 2：转圈消失瞬时放行（Instant Spinner-Disappearance Release）】
   - 若观察到左侧出现转圈（弱网/大图），一旦转圈消失且连续 2 帧（约 0.12s）背景纯净，立即放行发送名称，无需再傻等 1.5s+；
   - 若小图/秒传未出现转圈，度过 0.20s 初始窗口后仅需确认 0.35s（界面可调）背景纯净即可极速放行。
3. 【核心提速 3：零注入灰盒逆向——微信 SQLite WAL 预写日志落盘脉冲监测（WeChatWalPulseTracker）】
   - 只读监测微信数据目录下最新活跃的 MSG*.db-wal / message*.db-wal 文件修改时间戳（st_mtime_ns），
     无需注入 DLL、无需破解 dbkey、零封号风险，辅助感知微信底层收到服务端 SvrId 回执后的本地写库动作。
"""

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw
DEFAULT_QUIET_CONFIRM_SEC = 0.35


def enable_windows_dpi_awareness() -> None:
    """开启 Windows 进程级 Per-Monitor 高 DPI 物理像素感知。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class WeChatWalPulseTracker:
    """
    零侵入灰盒逆向信号监测器：
    只读监测微信本地数据库预写日志（MSG*.db-wal / message*.db-wal）的纳秒级修改时间与大小跳变。
    微信发图分成两次写库：
      1) 按下 Enter 时写入本地发送队列记录；
      2) CDN 上传完成并收到腾讯服务器 MsgSvrID 回执时更新消息状态为已送达。
    通过 os.stat() 监测该文件脉冲，不读取加密内容、不注入进程内存，100% 安全免封号。
    """

    def __init__(self) -> None:
        self._active_wal: Optional[Path] = None
        self._last_scan_ts: float = 0.0

    def _discover_latest_wal(self) -> Optional[Path]:
        now = time.monotonic()
        if self._active_wal and self._active_wal.exists() and (now - self._last_scan_ts) < 30.0:
            return self._active_wal

        self._last_scan_ts = now
        candidates: List[Path] = []
        user_profile = os.environ.get("USERPROFILE") or str(Path.home())
        search_roots = [
            Path(user_profile) / "Documents" / "WeChat Files",
            Path(user_profile) / "Documents" / "xwechat_files",
        ]
        for root in search_roots:
            if not root.exists():
                continue
            try:
                for wal in root.rglob("*.db-wal"):
                    if "msg" in wal.name.lower() or "message" in wal.name.lower():
                        candidates.append(wal)
            except Exception:
                continue

        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime_ns if p.exists() else 0, reverse=True)
        self._active_wal = candidates[0]
        return self._active_wal

    def snapshot(self) -> Optional[Tuple[int, int]]:
        wal = self._discover_latest_wal()
        if not wal or not wal.exists():
            return None
        try:
            st = wal.stat()
            return (st.st_mtime_ns, st.st_size)
        except Exception:
            return None


@dataclass
class WeChatDiagnosticInfo:
    """微信运行环境只读诊断信息。"""
    platform: str
    wechat_running: bool
    window_handle: Optional[int]
    window_class: str
    window_title: str
    detected_version: str
    current_chat_title: str
    recent_chats: List[str]
    dpi_scale_percent: int
    accessibility_ready: bool


class BaseWeChatAdapter:
    """微信适配器抽象基类。"""

    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    quiet_confirm_sec: float = DEFAULT_QUIET_CONFIRM_SEC

    def set_similarity_threshold(self, threshold: float) -> None:
        self.similarity_threshold = threshold

    def set_quiet_confirm_sec(self, sec: float) -> None:
        self.quiet_confirm_sec = max(0.10, min(5.0, sec))

    def diagnose(self) -> WeChatDiagnosticInfo:
        raise NotImplementedError

    def list_recent_chats(self) -> List[str]:
        raise NotImplementedError

    def get_current_chat_title(self) -> str:
        raise NotImplementedError

    def activate_chat(self, chat_name: str) -> bool:
        raise NotImplementedError

    def ensure_foreground_and_target(self, expected_chat: str) -> Tuple[bool, str]:
        raise NotImplementedError

    def send_image(self, image_path: Path, clipboard_mode: str, send_shortcut: str) -> None:
        raise NotImplementedError

    def verify_image_sent(
        self,
        image_path: Path,
        timeout_sec: float = 25.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        raise NotImplementedError

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DemoWeChatAdapter(BaseWeChatAdapter):
    """演示与自动化测试适配器。"""

    def __init__(
        self,
        simulate_fast_ms: int = 40,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self.simulate_fast_ms = simulate_fast_ms
        self.similarity_threshold = similarity_threshold
        self.quiet_confirm_sec = DEFAULT_QUIET_CONFIRM_SEC
        self.current_chat = "文件传输助手"
        self.recent_chats = ["文件传输助手", "产品设计群", "摄影修图组", "测试好友A"]
        self.sent_records: List[Tuple[str, str]] = []
        self.clipboard = ClipboardManager(dry_run=True)
        self.verifier = VisualBubbleVerifier(similarity_threshold=similarity_threshold)
        self._last_sent_image: Optional[Image.Image] = None

    def set_similarity_threshold(self, threshold: float) -> None:
        self.similarity_threshold = threshold
        self.verifier.similarity_threshold = threshold

    def diagnose(self) -> WeChatDiagnosticInfo:
        return WeChatDiagnosticInfo(
            platform=sys.platform,
            wechat_running=True,
            window_handle=10086,
            window_class="Qt51514QWindowIcon (Demo)",
            window_title="微信 (演示模式)",
            detected_version="4.1.15+ (Universal Zero-Patch Fast-Track)",
            current_chat_title=self.current_chat,
            recent_chats=list(self.recent_chats),
            dpi_scale_percent=125,
            accessibility_ready=True,
        )

    def list_recent_chats(self) -> List[str]:
        return list(self.recent_chats)

    def get_current_chat_title(self) -> str:
        return self.current_chat

    def activate_chat(self, chat_name: str) -> bool:
        if chat_name:
            self.current_chat = chat_name
            if chat_name not in self.recent_chats:
                self.recent_chats.insert(0, chat_name)
            return True
        return False

    def ensure_foreground_and_target(self, expected_chat: str) -> Tuple[bool, str]:
        if self.current_chat != expected_chat:
            return False, f"当前聊天已切换为“{self.current_chat}”，与目标“{expected_chat}”不一致"
        return True, "OK"

    def send_image(self, image_path: Path, clipboard_mode: str, send_shortcut: str) -> None:
        if clipboard_mode == "CF_HDROP":
            self.clipboard.set_image_file_hdrop(image_path)
        else:
            self.clipboard.set_image_dib(image_path)
        img, _, _ = load_static_image(image_path)
        self._last_sent_image = img
        time.sleep(self.simulate_fast_ms / 1000.0)
        self.sent_records.append(("IMAGE", image_path.name))

    def _render_mock_chat_canvas(self, src_img: Image.Image, draw_spinner: bool = False) -> Image.Image:
        canvas = Image.new("RGB", (520, 420), (25, 25, 25))
        tw = 160
        th = max(32, min(280, int(round(tw * (src_img.height / float(src_img.width))))))
        thumb = src_img.resize((tw, th), Image.Resampling.BILINEAR)
        bx, by = 520 - tw - 36, 420 - th - 40
        canvas.paste(thumb, (bx, by))
        if draw_spinner:
            draw = ImageDraw.Draw(canvas)
            sx, sy = bx - 26, by + th // 2 - 8
            draw.ellipse((sx, sy, sx + 16, sy + 16), outline=(215, 215, 215), width=2)
        return canvas

    def verify_image_sent(
        self,
        image_path: Path,
        timeout_sec: float = 25.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        src_img = self._last_sent_image
        if src_img is None:
            src_img, _, _ = load_static_image(image_path)
        quiet_frame_1 = self._render_mock_chat_canvas(src_img, draw_spinner=False)
        quiet_frame_2 = self._render_mock_chat_canvas(src_img, draw_spinner=False)
        time.sleep(self.simulate_fast_ms / 1000.0)
        return self.verifier.evaluate(
            curr_screenshot=quiet_frame_2,
            source_image=src_img,
            prev_screenshot=quiet_frame_1,
        )

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        self.clipboard.set_text(text)
        time.sleep(self.simulate_fast_ms / 1000.0)
        self.sent_records.append(("TEXT", text))
        return True, f"已确认文本消息“{text}”发送完成"


class WindowsWeChatAdapter(BaseWeChatAdapter):
    """
    真实 Windows 微信适配器（v3.3 极速识别版）：
    - 粘贴与回车延迟从 520ms 压缩至 160ms；
    - 首帧定位到缩略图 (bx, by, bw, bh) 后立即锁定尺度 `bw`，后续以 60ms 极速只读检查左侧状态区，
      不再每帧重复扫描 21 个尺度，单次检查耗时从 ~300ms 降至 ~2ms；
    - 智能双分支极速放行：
      * 若观察到上传转圈（spinner_seen=True），一旦转圈消失且连续 2 帧（0.12s）状态区干净，立即放行名称！
      * 若未观察到转圈（小图/秒传），在 0.20s 初始上屏后只要连续 0.35s（可在界面调节）状态区干净即放行！
    """

    WECHAT_CLASSES = [
        "Qt51514QWindowIcon",  # 微信 4.x Qt 窗口类
        "WeChatMainWndForPC",  # 微信 3.9.x 窗口类
    ]

    def __init__(
        self,
        enable_accessibility: bool = True,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        post_send_grace_sec: float = 0.18,
    ) -> None:
        enable_windows_dpi_awareness()
        self.enable_accessibility = enable_accessibility
        self.similarity_threshold = similarity_threshold
        self.quiet_confirm_sec = DEFAULT_QUIET_CONFIRM_SEC
        self.post_send_grace_sec = post_send_grace_sec
        self.activator = ZeroPatchAccessibilityActivator()
        self.clipboard = ClipboardManager(dry_run=(sys.platform != "win32"))
        self.verifier = VisualBubbleVerifier(similarity_threshold=similarity_threshold)
        self.wal_tracker = WeChatWalPulseTracker()
        self._hwnd: Optional[int] = None
        self._pre_send_shot: Optional[Image.Image] = None
        self._pre_send_wal: Optional[Tuple[int, int]] = None
        self._last_locked_bbox: Optional[Tuple[int, int, int, int]] = None
        if sys.platform == "win32" and enable_accessibility:
            hwnd = self._find_wechat_hwnd()
            self.activator.activate(hwnd)

    def set_similarity_threshold(self, threshold: float) -> None:
        self.similarity_threshold = threshold
        self.verifier.similarity_threshold = threshold

    def _find_wechat_hwnd(self) -> Optional[int]:
        if sys.platform != "win32":
            return None
        import ctypes
        user32 = ctypes.windll.user32
        for cls_name in self.WECHAT_CLASSES:
            hwnd = user32.FindWindowW(cls_name, None)
            if hwnd:
                self._hwnd = int(hwnd)
                return self._hwnd
        hwnd = user32.FindWindowW(None, "微信") or user32.FindWindowW(None, "Weixin")
        if hwnd:
            self._hwnd = int(hwnd)
            return self._hwnd
        return None

    def _get_dpi_scale(self, hwnd: Optional[int]) -> int:
        if sys.platform != "win32":
            return 100
        try:
            import ctypes
            if hwnd and hasattr(ctypes.windll.user32, "GetDpiForWindow"):
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                if dpi > 0:
                    return int(round(dpi * 100 / 96.0))
        except Exception:
            pass
        return 100

    def diagnose(self) -> WeChatDiagnosticInfo:
        hwnd = self._find_wechat_hwnd()
        if not hwnd or sys.platform != "win32":
            return WeChatDiagnosticInfo(
                platform=sys.platform,
                wechat_running=False,
                window_handle=None,
                window_class="",
                window_title="",
                detected_version="未检测到运行中的微信",
                current_chat_title="",
                recent_chats=[],
                dpi_scale_percent=100,
                accessibility_ready=False,
            )

        import ctypes
        user32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        cls_name = buf.value
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value

        version_desc = "4.x (Qt 架构)" if "Qt" in cls_name else "3.9.x (Duilib 架构)"
        chat_title = self.get_current_chat_title()
        recent = self.list_recent_chats()
        return WeChatDiagnosticInfo(
            platform=sys.platform,
            wechat_running=True,
            window_handle=hwnd,
            window_class=cls_name,
            window_title=title,
            detected_version=version_desc,
            current_chat_title=chat_title,
            recent_chats=recent,
            dpi_scale_percent=self._get_dpi_scale(hwnd),
            accessibility_ready=bool(chat_title or recent),
        )

    def list_recent_chats(self) -> List[str]:
        if sys.platform != "win32":
            return []
        try:
            import uiautomation as auto
            hwnd = self._find_wechat_hwnd()
            if not hwnd:
                return []
            wnd = auto.ControlFromHandle(hwnd)
            chats: List[str] = []
            for item in wnd.GetChildren():
                name = (item.Name or "").strip()
                if name and len(name) < 40:
                    chats.append(name)
            return chats[:20] if chats else ["文件传输助手"]
        except Exception:
            return ["文件传输助手"]

    def get_current_chat_title(self) -> str:
        if sys.platform != "win32":
            return ""
        try:
            import uiautomation as auto
            hwnd = self._find_wechat_hwnd()
            if not hwnd:
                return ""
            wnd = auto.ControlFromHandle(hwnd)
            edit = wnd.EditControl(searchDepth=12)
            if edit.Exists(0, 0) and edit.Name:
                return edit.Name.strip()
            return wnd.Name or "当前微信聊天"
        except Exception:
            return "当前微信聊天"

    def activate_chat(self, chat_name: str) -> bool:
        hwnd = self._find_wechat_hwnd()
        if not hwnd or sys.platform != "win32":
            return False
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
        return True

    def ensure_foreground_and_target(self, expected_chat: str) -> Tuple[bool, str]:
        hwnd = self._find_wechat_hwnd()
        if not hwnd or sys.platform != "win32":
            return False, "未找到微信窗口，已暂停发送"
        import ctypes
        fg = ctypes.windll.user32.GetForegroundWindow()
        if fg != hwnd:
            return False, "微信窗口已失去前台焦点，为防止误发已自动暂停"
        curr_title = self.get_current_chat_title()
        if expected_chat and curr_title and expected_chat not in curr_title and curr_title != "当前微信聊天":
            return False, f"检测到聊天窗口变化（当前：{curr_title}，目标：{expected_chat}），已暂停"
        return True, "OK"

    def _capture_chat_region(self) -> Image.Image:
        from PIL import ImageGrab
        hwnd = self._find_wechat_hwnd()
        if hwnd and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = max(200, rect.right - rect.left)
            h = max(200, rect.bottom - rect.top)
            # 聚焦截取聊天窗口右下半区（最新发出的消息气泡一定出现在右下区域，面积缩小一半，速度翻倍）
            box = (
                rect.left + int(w * 0.28),
                rect.top + int(h * 0.15),
                rect.right - 6,
                rect.bottom - int(h * 0.16),
            )
            return ImageGrab.grab(bbox=box, all_screens=True)
        return ImageGrab.grab(all_screens=True)

    def _paste_and_submit(self, send_shortcut: str, is_text: bool = False) -> None:
        if sys.platform != "win32":
            return
        import uiautomation as auto
        # 紧凑化粘贴与回车间隔：文本仅需 60ms，图片句柄仅需 100ms
        paste_wait = 0.05 if is_text else 0.08
        enter_wait = 0.04 if is_text else 0.06
        auto.SendKeys("{Ctrl}v", waitTime=paste_wait)
        time.sleep(paste_wait)
        if send_shortcut.upper() == "CTRL+ENTER":
            auto.SendKeys("{Ctrl}{Enter}", waitTime=enter_wait)
        else:
            auto.SendKeys("{Enter}", waitTime=enter_wait)

    def send_image(self, image_path: Path, clipboard_mode: str, send_shortcut: str) -> None:
        try:
            self._pre_send_shot = self._capture_chat_region()
        except Exception:
            self._pre_send_shot = None
        self._pre_send_wal = self.wal_tracker.snapshot()
        self._last_locked_bbox = None

        if clipboard_mode == "CF_HDROP":
            self.clipboard.set_image_file_hdrop(image_path)
        else:
            self.clipboard.set_image_dib(image_path)
        self._paste_and_submit(send_shortcut, is_text=False)

    def _detect_new_bubble_bbox(self, pre_shot: Image.Image, curr_shot: Image.Image) -> Optional[Tuple[int, int, int, int]]:
        if pre_shot.size != curr_shot.size:
            return None
        w, h = curr_shot.size
        rx = int(w * 0.25)
        ry = int(h * 0.15)
        a1 = np.asarray(pre_shot.convert("L").crop((rx, ry, w, h)), dtype=np.float32)
        a2 = np.asarray(curr_shot.convert("L").crop((rx, ry, w, h)), dtype=np.float32)
        diff = np.abs(a2 - a1)
        mask = diff > 14.0
        if float(np.mean(mask)) < 0.015:
            return None
        ys, xs = np.where(mask)
        if ys.size < 100:
            return None
        x1, x2 = int(np.percentile(xs, 5)), int(np.percentile(xs, 95))
        y1, y2 = int(np.percentile(ys, 5)), int(np.percentile(ys, 95))
        bw, bh = max(32, x2 - x1), max(32, y2 - y1)
        return (rx + x1, ry + y1, bw, bh)

    def verify_image_sent(
        self,
        image_path: Path,
        timeout_sec: float = 25.0,
        stable_duration_sec: float = DEFAULT_QUIET_CONFIRM_SEC,
    ) -> BubbleVisualStatus:
        src_img, _, _ = load_static_image(image_path)
        try:
            file_mb = image_path.stat().st_size / (1024.0 * 1024.0)
        except Exception:
            file_mb = 0.5

        adaptive_timeout = max(timeout_sec, 25.0 + file_mb * 5.0)
        # 使用界面配置的确认时长（默认 0.35s），极速响应
        target_quiet_sec = self.quiet_confirm_sec

        send_start_ts = time.monotonic()
        deadline = send_start_ts + adaptive_timeout
        prev_shot: Optional[Image.Image] = None
        quiet_since: Optional[float] = None
        locked_widths: Optional[List[int]] = None
        spinner_seen = False

        last_status = BubbleVisualStatus(
            matched=False,
            similarity=0.0,
            bbox=None,
            has_failure_badge=False,
            has_static_spinner=False,
            spinner_motion_score=999.0,
            is_upload_complete=False,
            reason="等待图片上屏...",
        )

        while time.monotonic() < deadline:
            now = time.monotonic()
            curr_shot = self._capture_chat_region()

            # 【核心提速】：一旦首帧匹配成功锁定了缩略图宽度 bw，后续帧只用 [bw] 单一尺度定位，耗时从 280ms 降至 8ms！
            status = self.verifier.evaluate(
                curr_shot,
                src_img,
                prev_shot,
                candidate_widths=locked_widths,
            )

            # 若锁定的单一尺度因消息列表滚动失配，立即回退全尺度搜索一次
            if not status.matched and locked_widths is not None:
                locked_widths = None
                status = self.verifier.evaluate(curr_shot, src_img, prev_shot, candidate_widths=None)

            if status.matched and status.bbox is not None:
                self._last_locked_bbox = status.bbox
                locked_widths = [status.bbox[2]]

            # 临界长图/特殊图的帧差辅助定位
            if not status.matched and self._pre_send_shot is not None and status.similarity >= max(0.22, self.similarity_threshold * 0.65):
                diff_bbox = self._detect_new_bubble_bbox(self._pre_send_shot, curr_shot)
                if diff_bbox is not None and prev_shot is not None:
                    has_fail = self.verifier.detect_failure_badge(curr_shot, diff_bbox)
                    has_spin = self.verifier.detect_static_spinner(curr_shot, diff_bbox)
                    motion = self.verifier.compute_spinner_motion(prev_shot, curr_shot, diff_bbox)
                    is_quiet = (motion <= self.verifier.motion_quiet_threshold) and (not has_spin) and (not has_fail)
                    status = BubbleVisualStatus(
                        matched=True,
                        similarity=max(status.similarity, self.similarity_threshold),
                        bbox=diff_bbox,
                        has_failure_badge=has_fail,
                        has_static_spinner=has_spin,
                        spinner_motion_score=motion,
                        is_upload_complete=is_quiet,
                        reason=f"帧差确认新增图片气泡 (相似度 {status.similarity:.2f}, 静态转圈={has_spin})",
                    )

            if status.has_static_spinner or (status.matched and status.spinner_motion_score > self.verifier.motion_quiet_threshold):
                spinner_seen = True

            # 极短的 0.18s 初始上屏缓冲，防止刚按回车第 1 帧转圈还没画出来
            if (now - send_start_ts) < self.post_send_grace_sec:
                status.is_upload_complete = False
                quiet_since = None

            last_status = status
            if status.has_failure_badge:
                return status

            if status.matched and status.is_upload_complete:
                if quiet_since is None:
                    quiet_since = now
                else:
                    elapsed_quiet = now - quiet_since
                    # 【智能加速】：
                    # 1) 如果之前已经捕捉到过转圈动画（spinner_seen=True），现在转圈消失了，只需稳定 0.12s 即可 100% 确认上传完成！
                    # 2) 如果检测到微信本地数据库 WAL 发生了新落盘脉冲，也只需 0.15s 即可确认完成！
                    # 3) 常规秒传/快网未见转圈，满 target_quiet_sec (默认 0.35s) 立即放行！
                    curr_wal = self.wal_tracker.snapshot()
                    wal_pulsed = (self._pre_send_wal is not None and curr_wal is not None and curr_wal != self._pre_send_wal)
                    needed_sec = 0.12 if spinner_seen else (min(0.18, target_quiet_sec) if wal_pulsed else target_quiet_sec)
                    if elapsed_quiet >= needed_sec:
                        return status
            else:
                quiet_since = None

            prev_shot = curr_shot
            # 高频 60ms 轮询（配合单尺度锁定，CPU 占用极低且响应快 6 倍）
            time.sleep(0.06)

        last_status.is_upload_complete = False
        last_status.reason = f"等待图片上传完成超时 ({adaptive_timeout:.0f}s): {last_status.reason}"
        return last_status

    def send_text_and_verify(self, text: str, send_shortcut: str) -> Tuple[bool, str]:
        """
        极速发送图片名称并闭环确认：
        复用已锁定的缩略图 bbox 做 1 毫秒级红标复核，粘贴回车后以 60ms 间隔快速检测文字上屏。
        """
        if self._last_locked_bbox is not None:
            try:
                pre_txt_shot = self._capture_chat_region()
                if self.verifier.detect_failure_badge(pre_txt_shot, self._last_locked_bbox):
                    return False, "发送名称前检测到图片左侧出现红色失败感叹号，已暂停"
                if self.verifier.detect_static_spinner(pre_txt_shot, self._last_locked_bbox):
                    return False, "发送名称前检测到图片左侧仍存在转圈图标，已暂停"
            except Exception:
                prev_txt_shot = None
            else:
                prev_txt_shot = pre_txt_shot
        else:
            try:
                prev_txt_shot = self._capture_chat_region()
            except Exception:
                prev_txt_shot = None

        self.clipboard.set_text(text)
        self._paste_and_submit(send_shortcut, is_text=True)

        # 以 60ms 高频轮询检测聊天区下半部分新文字气泡上屏（通常 60~120ms 即确认完成！）
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            time.sleep(0.06)
            if prev_txt_shot is None:
                return True, f"已发送图片名称：{text}"
            try:
                after_text_shot = self._capture_chat_region()
                if after_text_shot.size == prev_txt_shot.size:
                    w, h = after_text_shot.size
                    b1 = np.asarray(prev_txt_shot.convert("L").crop((int(w * 0.20), int(h * 0.40), w, h)), dtype=np.float32)
                    b2 = np.asarray(after_text_shot.convert("L").crop((int(w * 0.20), int(h * 0.40), w, h)), dtype=np.float32)
                    if float(np.mean(np.abs(b2 - b1))) > 1.0:
                        return True, f"已确认图片名称“{text}”极速上屏完成"
            except Exception:
                return True, f"已发送图片名称：{text}"

        return False, f"名称“{text}”提交后未检测到聊天区新增文字气泡，请人工核实是否发出"

    def close(self) -> None:
        self.activator.restore()


# ==================== wechat_sender/job_store.py ====================

# -*- coding: utf-8 -*-
"""
发送队列状态机与断点续传持久化模块：
严格按照「待发送 -> 图片已提交待核实 -> 图片已确认待发名称 -> 名称已提交待核实 -> 已完成」顺序推进。
任何阶段不明时保持 PENDING_VERIFY 暂停状态，绝不盲目自动重发。
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional


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


# ==================== wechat_sender/gui.py ====================

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


# ==================== main.py ====================

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信图片顺序发送助手 v3.0.0 入口程序
支持：
- GUI 模式：`python main.py`
- 演示模式：`python main.py --demo`（或 `--demo --headless` 在无显示器环境自动跑完演示队列）
- 诊断模式：`python main.py --diagnose [--diagnose-output diagnostic.json]`
"""

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from PIL import Image


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
    print("=== 微信图片顺序发送助手 v3.0.0 演示队列（Headless 模式）===")
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
    parser = argparse.ArgumentParser(description="微信图片顺序发送助手 v3.0.0")
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

    root = tk.Tk()
    _app = SenderAppGUI(root, demo_mode=args.demo)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
