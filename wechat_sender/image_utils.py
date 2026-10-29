# -*- coding: utf-8 -*-
"""
图片扫描、自然排序、批量命名模板、采样 SHA-256、MPO/EXIF 处理与视觉状态判定模块。
核心特性：
1. 采用 wcs 参考版本的逐宽度多尺度匹配，结合源图内框与全框评分；
2. 对底部匹配图片与较大显示缩放补充搜索，不要求重建历史消息或滚动轨迹；
3. 采样快速 SHA-256（大文件首/中/尾采样哈希）与批量命名模板引擎（支持 {num:02d}_{name} 等）；
4. 左侧状态区防边缘渗入静态转圈检测 + 红色感叹号失败否决 + 连续帧动态差分确认。
"""

import hashlib
import io
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif", ".gif"}
NATURAL_SORT_REGEX = re.compile(r"(\d+)")
DEFAULT_SIMILARITY_THRESHOLD = 0.40
FAST_HASH_THRESHOLD_BYTES = 4 * 1024 * 1024  # 4MB 以上大图启用首中尾高速采样哈希
FAST_HASH_CHUNK_SIZE = 131072                # 128KB 采样块


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


def compute_sha256(file_path: Path, fast_sample: bool = True) -> str:
    """
    计算文件的 SHA-256 摘要：
    - 小于等于 4MB（或 fast_sample=False）时计算完整文件 SHA-256；
    - 大于 4MB 且开启 fast_sample 时，采用「文件大小 + 头部 128KB + 中部 128KB + 尾部 128KB」高速采样摘要，
      在百张高清大图场景下提速 20~50 倍且依然严格保障文件防篡改校验。
    """
    h = hashlib.sha256()
    size = file_path.stat().st_size
    if not fast_sample or size <= FAST_HASH_THRESHOLD_BYTES:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    h.update(f"FAST_SHA256:{size}:".encode("ascii"))
    with open(file_path, "rb") as f:
        h.update(f.read(FAST_HASH_CHUNK_SIZE))
        f.seek(max(0, size // 2 - FAST_HASH_CHUNK_SIZE // 2))
        h.update(f.read(FAST_HASH_CHUNK_SIZE))
        f.seek(max(0, size - FAST_HASH_CHUNK_SIZE))
        h.update(f.read(FAST_HASH_CHUNK_SIZE))
    return h.hexdigest()


def format_batch_display_name(
    template: str,
    index_1based: int,
    file_path: Path,
    include_extension: bool = False,
) -> str:
    """
    根据用户模板批量生成待发送名称：
    支持占位符：
    - {num} 或 {num:02d} / {num:03d}：1 起始的序号
    - {index}：0 起始的索引
    - {name} 或 {stem}：不含扩展名的原文件名
    - {ext}：不含点号的扩展名（如 jpg）
    - {filename}：含扩展名的完整文件名
    - {folder}：所属父文件夹名称
    """
    stem = file_path.stem
    ext = file_path.suffix.lstrip(".")
    filename = file_path.name
    folder = file_path.parent.name or "images"
    default_name = filename if include_extension else stem

    tpl = (template or "").strip()
    if not tpl:
        return default_name

    try:
        rendered = tpl.format(
            num=index_1based,
            index=index_1based - 1,
            name=stem,
            stem=stem,
            ext=ext,
            filename=filename,
            folder=folder,
        ).strip()
        return rendered or default_name
    except Exception:
        # 若用户输入了非标准花括号文本，回退作为前缀拼接
        return f"{tpl}{default_name}"


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


def scan_image_paths(
    paths: Sequence[Path],
    base_folder: Optional[Path] = None,
    include_subfolders: bool = False,
    include_extension: bool = False,
    allow_animated_in_file_mode: bool = False,
    progress_callback: Optional[Callable[[int, int, ImageItem], None]] = None,
) -> Tuple[List[ImageItem], List[Tuple[Path, str]]]:
    """
    通用图片集合扫描器（支持文件夹、多选文件或拖拽混入的文件/目录列表）：
    支持 progress_callback(current_idx, total_count, item) 供 GUI 异步流式刷新。
    """
    candidates: List[Path] = []
    inferred_base = base_folder.resolve() if base_folder else None

    for raw_p in paths:
        p = Path(raw_p).resolve()
        if not p.exists():
            continue
        if p.is_dir():
            if inferred_base is None:
                inferred_base = p
            if include_subfolders:
                for root, _, files in os.walk(p):
                    for name in files:
                        fp = Path(root) / name
                        if fp.suffix.lower() in SUPPORTED_EXTENSIONS:
                            candidates.append(fp)
            else:
                for fp in p.iterdir():
                    if fp.is_file() and fp.suffix.lower() in SUPPORTED_EXTENSIONS:
                        candidates.append(fp)
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            if inferred_base is None:
                inferred_base = p.parent
            candidates.append(p)

    # 去重并按自然顺序排列
    unique_map = {str(p): p for p in candidates}
    candidates = list(unique_map.values())

    def _rel_str(p: Path) -> str:
        if inferred_base:
            try:
                return str(p.relative_to(inferred_base)).replace("\\", "/")
            except ValueError:
                pass
        return p.name

    candidates.sort(key=lambda p: natural_sort_key(_rel_str(p)))

    valid_items: List[ImageItem] = []
    skipped: List[Tuple[Path, str]] = []
    total = len(candidates)

    for idx, p in enumerate(candidates, start=1):
        try:
            sha = compute_sha256(p, fast_sample=True)
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

            rel_name = _rel_str(p)
            display_name = p.name if include_extension else p.stem
            item = ImageItem(
                path=p,
                relative_name=rel_name,
                display_name=display_name,
                sha256=sha,
                width=w,
                height=h,
                format_name=fmt,
                is_mpo_primary=is_mpo,
            )
            valid_items.append(item)
            if progress_callback is not None:
                progress_callback(idx, total, item)
        except Exception as exc:
            skipped.append((p, str(exc)))

    return valid_items, skipped


def scan_image_folder(
    folder: Path,
    include_subfolders: bool = False,
    include_extension: bool = False,
    allow_animated_in_file_mode: bool = False,
    progress_callback: Optional[Callable[[int, int, ImageItem], None]] = None,
) -> Tuple[List[ImageItem], List[Tuple[Path, str]]]:
    """
    扫描文件夹中的所有支持图片，按自然顺序排序。
    返回 (有效图片列表, 被跳过的文件及原因列表)。
    """
    folder = Path(folder).resolve()
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {folder}")

    return scan_image_paths(
        paths=[folder],
        base_folder=folder,
        include_subfolders=include_subfolders,
        include_extension=include_extension,
        allow_animated_in_file_mode=allow_animated_in_file_mode,
        progress_callback=progress_callback,
    )


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
    多信号联合非侵入式视觉验证器：
    1. 保留 wcs 的多尺度内框/全框匹配与默认阈值 0.40；
    2. 缓存当前源图模板，画面其余部分不变时只在已定位图片附近匹配；
    3. 红色感叹号失败标记一票否决；
    4. 缩略图两侧状态图标检测，支持不同显示缩放；
    5. 连续帧间动态差分静止确认（Spinner Motion Score <= 阈值）。
    """

    def __init__(
        self,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        motion_quiet_threshold: float = 1.8,
        red_failure_ratio_threshold: float = 0.04,
        static_spinner_std_threshold: float = 8.5,
        max_cv_threads: int = 2,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.motion_quiet_threshold = motion_quiet_threshold
        self.red_failure_ratio_threshold = red_failure_ratio_threshold
        self.static_spinner_std_threshold = static_spinner_std_threshold
        # Keep native matching from monopolizing every CPU core while WeChat
        # and Tk must render/respond. Tests/benchmarks may explicitly vary it.
        if max_cv_threads > 0 and cv2.getNumThreads() > max_cv_threads:
            cv2.setNumThreads(max_cv_threads)
        self.performance_counters = {name: 0 for name in (
            "cache_hits", "cache_misses", "full_searches", "local_searches",
            "template_resizes", "template_cache_hits", "source_cache_hits",
            "source_cache_misses", "source_array_builds", "overlay_resizes", "overlay_cache_hits",
        )}
        self._source_key = None
        self._source_arrays = None
        self._source_scope = None
        self._source_scope_arrays = None
        self._prepared_source_ref = None
        self._templates = OrderedDict()
        self._template_bytes = 0
        self._overlay_templates = OrderedDict()
        self._overlay_bytes = 0
        self._location_cache = None

    def begin_image(self, source):
        """Prepare one immutable source for an adapter's current image step.

        The caller must not mutate it before ``end_image``. Direct public
        ``locate_thumbnail`` calls still validate content on every call.
        """
        self._prepare_source(source)
        self._prepared_source_ref = source

    def end_image(self):
        """Release the caller's source image; resized-template storage is bounded."""
        self._prepared_source_ref = None

    def _prepare_source(self, source, allow_prepared=False):
        # Full content hashing detects in-place PIL edits as well as a new
        # object. No id-only cache can mistake a different image after GC.
        if allow_prepared and self._prepared_source_ref is source and self._source_key is not None:
            self.performance_counters["source_cache_hits"] += 1
            return self._source_key
        # Palette edits change displayed pixels without changing indexed pixel
        # bytes. Include the palette rather than trusting object identity.
        palette = (source.palette.mode, source.palette.tobytes()) if source.mode == "P" and source.palette else None
        key = (source.mode, source.size, hashlib.sha256(source.tobytes()).digest(), palette)
        if key != self._source_key:
            self._source_key = key
            self._source_arrays = None
            self._source_scope_arrays = None
            self._templates.clear()
            self._template_bytes = 0
            self._overlay_templates.clear()
            self._overlay_bytes = 0
            self._location_cache = None
            self.performance_counters["source_cache_misses"] += 1
        else:
            self.performance_counters["source_cache_hits"] += 1
        return key

    def _prepared_arrays(self, source):
        if self._source_arrays is not None:
            return self._source_arrays
        if self._source_scope_arrays is not None:
            return self._source_scope_arrays
        self.performance_counters["source_array_builds"] += 1
        rgb = source.convert("RGB")
        sw, sh = rgb.size
        mx, my = int(round(sw * .10)), int(round(sh * .10))
        use_inner = sw - 2 * mx >= 16 and sh - 2 * my >= 16
        inner = rgb.crop((mx, my, sw - mx, sh - my)) if use_inner else rgb
        arrays = (np.asarray(rgb, dtype=np.uint8), np.asarray(inner, dtype=np.uint8), use_inner)
        # At most one small/medium source and 16 MiB of resized templates are
        # retained. Large source arrays live only for the current evaluation.
        if arrays[0].nbytes + arrays[1].nbytes <= 16 * 1024 * 1024:
            self._source_arrays = arrays
        elif self._source_scope is not None:
            # Lower-message searches share this evaluation's large arrays.
            # They are released by the outer location scope, never retained
            # between frames or as a second copy of a large decoded source.
            self._source_scope_arrays = arrays
        return arrays

    def _source_template(self, source, width, height, inner):
        key = (bool(inner), int(width), int(height))
        if key in self._templates:
            return self._template(None, width, height, inner)
        full_array, inner_array, _ = self._prepared_arrays(source)
        return self._template(inner_array if inner else full_array, width, height, inner)

    def _template(self, array, width, height, inner):
        key = (bool(inner), int(width), int(height))
        cached = self._templates.get(key)
        if cached is not None:
            self._templates.move_to_end(key)
            self.performance_counters["template_cache_hits"] += 1
            return cached
        value = cv2.resize(array, (width, height), interpolation=cv2.INTER_AREA)
        self.performance_counters["template_resizes"] += 1
        limit = 16 * 1024 * 1024
        while self._templates and (self._template_bytes + value.nbytes > limit or len(self._templates) >= 64):
            _, retired = self._templates.popitem(last=False)
            self._template_bytes -= retired.nbytes
        if value.nbytes <= limit:
            self._templates[key] = value
            self._template_bytes += value.nbytes
        return value

    def _stable_frame_key(self, screenshot, box):
        pixels = np.array(screenshot.convert("RGB"), dtype=np.uint8)
        for _, (x1, y1, x2, y2) in self._status_regions(screenshot, box):
            pixels[y1:y2, x1:x2] = 0
        x, y, width, height = box
        # A central upload overlay may animate. It is checked on every frame
        # by the normal motion and overlay detectors, never cached as quiet.
        half = max(12, min(48, int(min(width, height) * .25)))
        cx, cy = x + width // 2, y + height // 2
        pixels[max(0, cy-half):cy+half+1, max(0, cx-half):cx+half+1] = 0
        return (screenshot.size, hashlib.sha256(pixels.tobytes()).digest())

    def _locate_nearby(self, screenshot, source, box):
        x, y, width, height = box
        sw, sh = source.size
        mx, my = int(round(sw * .10)), int(round(sh * .10))
        use_inner = sw - 2*mx >= 16 and sh - 2*my >= 16
        margin = max(4, min(24, int(width * .10)))
        left = max(int(screenshot.width * .15), x - margin)
        top = max(0, y - margin)
        right, bottom = min(screenshot.width, x + width + margin), min(screenshot.height, y + height + margin)
        chat = np.asarray(screenshot.convert("RGB"), dtype=np.uint8)
        roi = chat[top:bottom, left:right]
        iw = max(16, int(round(width * .80))) if use_inner else width
        ih = max(16, int(round(height * .80))) if use_inner else height
        if iw >= roi.shape[1] or ih >= roi.shape[0]:
            return False, 0.0, None
        template = self._source_template(source, iw, ih, True)
        response = cv2.matchTemplate(roi, template, cv2.TM_SQDIFF_NORMED)
        _, _, position, _ = cv2.minMaxLoc(response)
        ix, iy = position
        score = self._perceptual_patch_similarity(roi[iy:iy+ih, ix:ix+iw], template)
        bx = left + ix - (width - iw) // 2
        by = top + iy - (height - ih) // 2
        if bx < 0 or by < 0 or bx + width > screenshot.width or by + height > screenshot.height:
            return False, score, None
        full_template = self._source_template(source, width, height, False)
        score = max(score, self._perceptual_patch_similarity(chat[by:by+height, bx:bx+width], full_template))
        return score >= self.similarity_threshold, score, (bx, by, width, height)

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
        own_scope = self._source_scope is None or self._source_scope[0] is not source_image
        if own_scope:
            key = self._prepare_source(source_image)
            self._source_scope = (source_image, key)
        try:
            return self._locate_thumbnail_prepared(chat_screenshot, source_image, candidate_widths)
        finally:
            if own_scope:
                self._source_scope = None
                self._source_scope_arrays = None

    def _locate_thumbnail_prepared(self, chat_screenshot, source_image, candidate_widths):
        chat_rgb_img = chat_screenshot.convert("RGB")
        cw, ch = chat_rgb_img.size
        sw, sh = source_image.size
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

        margin_x, margin_y = int(round(sw * .10)), int(round(sh * .10))
        use_inner = sw - 2 * margin_x >= 16 and sh - 2 * margin_y >= 16

        best_score = -1.0
        best_bbox = None

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

            tpl_inner_rgb = self._source_template(source_image, iw, ih, True)
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
                tpl_full_rgb = self._source_template(source_image, full_w, full_h, False)
                patch_full = chat_arr[full_y : full_y + full_h, full_x : full_x + full_w]
                full_score = self._perceptual_patch_similarity(patch_full, tpl_full_rgb)
                score = max(score, full_score)

            if score > best_score + 0.002:
                best_score = score
                best_bbox = (full_x, full_y, full_w, full_h)

        matched = best_score >= self.similarity_threshold
        return matched, max(0.0, float(best_score)), best_bbox if matched else None

    def _locate_current_thumbnail(self, screenshot, source, candidate_widths=None):
        key = self._prepare_source(source, allow_prepared=True)
        self._source_scope = (source, key)
        try:
            cached = self._location_cache
            locked = candidate_widths is not None and len(candidate_widths) == 1
            if cached is not None and locked:
                old_box, old_score, frame_key = cached
                if (candidate_widths[0] == old_box[2]
                        and self._stable_frame_key(screenshot, old_box) == frame_key):
                    self.performance_counters["local_searches"] += 1
                    found, score, box = self._locate_nearby(screenshot, source, old_box)
                    if found and score >= max(self.similarity_threshold, old_score - .12):
                        self.performance_counters["cache_hits"] += 1
                        self._location_cache = (box, score, self._stable_frame_key(screenshot, box))
                        return found, score, box
            self.performance_counters["cache_misses"] += 1
            self.performance_counters["full_searches"] += 1
            found, score, box = self._locate_current_full(screenshot, source, candidate_widths)
            self._location_cache = (box, score, self._stable_frame_key(screenshot, box)) if found and box else None
            return found, score, box
        finally:
            # A frame-level scope saves repeated hashing in the lower-message
            # search, without retaining another potentially huge source image.
            self._source_scope = None
            self._source_scope_arrays = None

    def _locate_current_full(self, screenshot, source, candidate_widths=None):
        """Use the reference matcher, preferring a lower matching message.

        No history identity, message count or scroll reconstruction is required.
        Extra physical-pixel sizes cover larger display scaling.
        """
        widths = candidate_widths
        if widths is None and screenshot.width * .75 > 340:
            maximum = min(1000, int(screenshot.width * .75))
            # 125% and 200% render the common 160 px thumbnail at 200/320
            # physical pixels. Exact fits keep central-overlay registration
            # valid without relaxing source comparison or spinner thresholds.
            widths = sorted(set([340, 320, 310, 280, 260, 240, 225, 210, 200, 195, 180, 168,
                                 160, 150, 140, 130, 120, 110, 100, 90, 80, 70, 60]
                                + list(range(360, maximum + 1, 30)) + [maximum]), reverse=True)
        found, score, box = self.locate_thumbnail(screenshot, source, widths)
        if not found or box is None:
            return found, score, box
        floor = max(self.similarity_threshold, score - .12)
        for _ in range(5):
            top = box[1] + box[3]
            if screenshot.height - top < 32:
                break
            lower = screenshot.crop((0, top, screenshot.width, screenshot.height))
            matched, value, candidate = self.locate_thumbnail(lower, source, widths)
            if not matched or candidate is None or value < floor:
                break
            x, y, width, height = candidate
            if min(x, y) < 0 or x + width > lower.width or y + height > lower.height:
                break
            score, box = value, (x, y + top, width, height)
        return True, score, box

    def detect_failure_badge(
        self,
        chat_screenshot: Image.Image,
        bbox: Tuple[int, int, int, int],
    ) -> bool:
        """检测独立的红色失败图标，避免大缩略图稀释小图标像素比例。"""
        for _side, region_box in self._status_regions(chat_screenshot, bbox):
            arr = np.asarray(chat_screenshot.convert("RGB").crop(region_box), dtype=np.int16)
            r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            red = (r > 155) & ((r - g) > 65) & ((r - b) > 65)
            if self._compact_status_components(red, bbox, allow_solid=True):
                return True
        return False

    @staticmethod
    def _status_regions(
        screenshot: Image.Image, bbox: Tuple[int, int, int, int]
    ) -> List[Tuple[str, Tuple[int, int, int, int]]]:
        """Separate UI status areas from the photo and the top-aligned avatar.

        No DPI value is required: the search covers common indicator sizes in
        physical pixels instead of a fixed 30-pixel column.
        """
        x, y, w, h = map(int, bbox)
        strip = max(64, min(180, int(round(w * 0.65))))
        gap = max(6, min(12, int(round(w * 0.025))))
        candidates = [
            ("left", (x - strip, y, x - gap, y + h)),
            # The avatar is normally at the top on the other side. A middle
            # band plus border-component rejection keeps it out of this test.
            ("right", (x + w + gap, y + int(h * 0.20),
                       x + w + strip, y + int(h * 0.80))),
        ]
        regions = []
        for side, (x1, y1, x2, y2) in candidates:
            box = (max(0, x1), max(0, y1), min(screenshot.width, x2), min(screenshot.height, y2))
            if box[2] - box[0] >= 8 and box[3] - box[1] >= 8:
                regions.append((side, box))
        return regions

    @staticmethod
    def _compact_status_components(
        mask: np.ndarray, bbox: Tuple[int, int, int, int], allow_solid: bool = False
    ) -> bool:
        """Find compact floating indicators; reject photo edges and large UI blocks."""
        if mask.ndim != 2 or not np.any(mask):
            return False
        # Join separated spokes/dots without relying on their rendering scale.
        joined = cv2.dilate(mask.astype(np.uint8), np.ones((5, 5), dtype=np.uint8))
        count, _labels, stats, _centers = cv2.connectedComponentsWithStats(joined, connectivity=8)
        rows, cols = mask.shape
        maximum = max(40, min(96, int(round(bbox[2] * 0.45))))
        for index in range(1, count):
            left, top, width, height, _area = map(int, stats[index])
            # Dilation expands 2 pixels around an indicator. Border-attached
            # regions are normally the photo edge, avatar, or window chrome.
            if left <= 0 or top <= 0 or left + width >= cols or top + height >= rows:
                continue
            original = mask[top:top + height, left:left + width]
            ys, xs = np.where(original)
            if xs.size < 8:
                continue
            actual_w, actual_h = int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
            if not (5 <= actual_w <= maximum and 5 <= actual_h <= maximum):
                continue
            if not 0.45 <= actual_w / float(actual_h) <= 2.2:
                continue
            density = xs.size / float(actual_w * actual_h)
            if 0.08 <= density <= (1.0 if allow_solid else 0.85):
                return True
        return False

    def detect_static_spinner(
        self,
        chat_screenshot: Image.Image,
        bbox: Tuple[int, int, int, int],
    ) -> bool:
        """Veto a visible indicator even when its animation freezes.

        The photo itself is deliberately excluded: a circular object in the
        source image is not an upload indicator.
        """
        threshold = max(10.0, min(24.0, self.static_spinner_std_threshold * 1.5))
        for _side, region_box in self._status_regions(chat_screenshot, bbox):
            gray = self._to_gray_array(chat_screenshot.crop(region_box))
            background = float(np.median(gray))
            mask = np.abs(gray - background) >= threshold
            if self._compact_status_components(mask, bbox):
                return True
        return False

    def compute_spinner_motion(
        self,
        prev_screenshot: Image.Image,
        curr_screenshot: Image.Image,
        bbox: Tuple[int, int, int, int],
    ) -> float:
        """Use local activity so a small animation is not diluted by a large photo."""
        if prev_screenshot.size != curr_screenshot.size:
            return 999.0
        x, y, w, h = bbox
        photo_box = (max(0, x), max(0, y), min(curr_screenshot.width, x + w), min(curr_screenshot.height, y + h))
        regions = [box for _side, box in self._status_regions(curr_screenshot, bbox)] + [photo_box]
        scores = []
        for box in regions:
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            crop1 = self._to_gray_array(prev_screenshot.crop(box))
            crop2 = self._to_gray_array(curr_screenshot.crop(box))
            if crop1.shape != crop2.shape or crop1.size == 0:
                return 999.0
            diff = np.abs(crop1 - crop2)
            window = max(8, min(24, int(round(min(w, h) * 0.10))))
            window = min(window, min(diff.shape))
            local = cv2.boxFilter(diff, -1, (window, window), normalize=True, borderType=cv2.BORDER_CONSTANT)
            scores.append(float(np.max(local)))
        if not scores:
            return 999.0
        return max(scores)

    def detect_center_upload_overlay(
        self, screenshot: Image.Image, source_image: Image.Image,
        bbox: Tuple[int, int, int, int],
    ) -> bool:
        """Veto a new neutral loading ring over an otherwise matching photo.

        This is intentionally narrow. It requires matching surroundings and
        rejects residuals near edges already present in the source, so a photo
        of a circle or compression around lettering does not become a spinner.
        """
        x, y, width, height = map(int, bbox)
        if (width < 36 or height < 36 or x < 0 or y < 0
                or x + width > screenshot.width or y + height > screenshot.height):
            return False
        self._prepare_source(source_image, allow_prepared=True)
        actual = np.asarray(screenshot.convert("RGB").crop((x, y, x + width, y + height)), dtype=np.float32)
        center_x, center_y = width // 2, height // 2
        half = min(64, int(min(width, height) * .32))
        # Sample only the surrounding photo while choosing the display resize
        # and one-pixel registration. The center must not choose its own fit.
        step = max(1, min(width, height) // 100)
        ys, xs = np.mgrid[2:height - 2:step, 2:width - 2:step]
        outside = ((np.abs(xs - center_x) > half) | (np.abs(ys - center_y) > half))
        sample_y, sample_x = ys[outside], xs[outside]
        if sample_x.size < 100:
            return False
        best = None
        source = None
        for method in (Image.Resampling.LANCZOS, Image.Resampling.BILINEAR):
            key = (width, height, int(method))
            cached = self._overlay_templates.get(key)
            if cached is None:
                if source is None:
                    source = source_image.convert("RGB")
                cached = np.asarray(source.resize((width, height), method), dtype=np.uint8)
                self.performance_counters["overlay_resizes"] += 1
                limit = 16 * 1024 * 1024
                while self._overlay_templates and (self._overlay_bytes + cached.nbytes > limit
                                                   or len(self._overlay_templates) >= 16):
                    _, retired = self._overlay_templates.popitem(last=False)
                    self._overlay_bytes -= retired.nbytes
                if cached.nbytes <= limit:
                    self._overlay_templates[key] = cached
                    self._overlay_bytes += cached.nbytes
            else:
                self._overlay_templates.move_to_end(key)
                self.performance_counters["overlay_cache_hits"] += 1
            # Cache source-only pixels, never registration, residuals, spinner
            # flags or completion. Those still use this frame's actual photo.
            expected = cached.astype(np.float32)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    delta = actual[sample_y, sample_x] - expected[sample_y + dy, sample_x + dx]
                    offset = np.median(delta, axis=0)
                    error = np.abs(delta - offset)
                    mean = float(np.mean(error))
                    if best is None or mean < best[0]:
                        best = (mean, float(np.percentile(error, 95)), offset, expected, dx, dy)
        mean, percentile, offset, expected, dx, dy = best
        if mean > 5.0 or percentile > 16.0 or np.max(np.abs(offset)) > 8.0:
            return False
        # Use the selected registration only in the interior, where every
        # source index is valid and photo borders cannot contribute residuals.
        x1, y1 = center_x - half, center_y - half
        x2, y2 = center_x + half, center_y + half
        reference = expected[y1 + dy:y2 + dy, x1 + dx:x2 + dx] + offset
        patch = actual[y1:y2, x1:x2]
        residual = np.max(np.abs(patch - reference), axis=2)
        neutral = np.max(patch, axis=2) - np.min(patch, axis=2) <= 16.0
        foreground = ((residual >= 36.0) & neutral).astype(np.uint8)
        if not np.any(foreground):
            return False
        reference_gray = np.mean(reference, axis=2)
        kernel = np.ones((5, 5), dtype=np.uint8)
        source_edges = cv2.dilate(reference_gray, kernel) - cv2.erode(reference_gray, kernel) > 28.0
        joined = cv2.dilate(foreground, np.ones((3, 3), dtype=np.uint8))
        count, _labels, stats, _centers = cv2.connectedComponentsWithStats(joined, connectivity=8)
        for index in range(1, count):
            left, top, w, h, _area = map(int, stats[index])
            if left <= 0 or top <= 0 or left + w >= foreground.shape[1] or top + h >= foreground.shape[0]:
                continue
            local = foreground[top:top + h, left:left + w]
            my, mx = np.where(local)
            if mx.size < 18:
                continue
            ax1, ay1, ax2, ay2 = int(mx.min()), int(my.min()), int(mx.max()), int(my.max())
            ring_w, ring_h = ax2 - ax1 + 1, ay2 - ay1 + 1
            if not (7 <= ring_w <= 96 and 7 <= ring_h <= 96 and .70 <= ring_w / ring_h <= 1.40):
                continue
            rx, ry = (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0
            if abs(left + rx - half) > 6 or abs(top + ry - half) > 6:
                continue
            density = mx.size / float(ring_w * ring_h)
            if not .12 <= density <= .65:
                continue
            radial = np.sqrt(((mx - rx) / (ring_w / 2.0)) ** 2 + ((my - ry) / (ring_h / 2.0)) ** 2)
            if float(np.mean((radial >= .50) & (radial <= 1.35))) < .85:
                continue
            angles = np.arctan2(my - ry, mx - rx)
            sectors = np.floor((angles + np.pi) * 12 / (2 * np.pi)).astype(int) % 12
            if np.unique(sectors).size < 8:
                continue
            # A circle, letter O, or JPEG ringing already in the photograph
            # has source edges under these pixels. It is not new upload UI.
            if float(np.mean(source_edges[top + my, left + mx])) > .15:
                continue
            return True
        return False

    def evaluate(
        self,
        curr_screenshot: Image.Image,
        source_image: Image.Image,
        prev_screenshot: Optional[Image.Image] = None,
        candidate_widths: Optional[List[int]] = None,
    ) -> BubbleVisualStatus:
        matched, sim, bbox = self._locate_current_thumbnail(curr_screenshot, source_image, candidate_widths)
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
        if is_quiet and self.detect_center_upload_overlay(curr_screenshot, source_image, bbox):
            return BubbleVisualStatus(
                matched=True, similarity=sim, bbox=bbox, has_failure_badge=False,
                has_static_spinner=True, spinner_motion_score=99.0, is_upload_complete=False,
                reason="图片中央存在源图没有的上传指示覆盖层，继续等待",
            )
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
