# -*- coding: utf-8 -*-
"""
图片扫描、自然排序、MPO/EXIF 处理、SHA-256 校验与多信号联合视觉状态判定模块（v3.2）。
新增：
- 状态区静态纯净度检测（Static Spinner / Frozen Icon Veto）：即使弱网下转圈动画卡死不动（帧间差分为 0），
  只要左侧状态区存在非背景色的转圈圆环或图标像素，立即一票否决，彻底解决“画面静止 != 上传完成”的误放行问题。
"""

from __future__ import annotations

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
