"""Conservative visual fallback for the verified 4.1.15.53 image layout.

The image must match its source at the expected outgoing location. Only a
positively observed upload ring can arm the separate completion gate; an empty
status area by itself never means sent. Unsupported layouts return UNKNOWN.
Screenshots are processed in memory and are not saved by the application.
"""
from dataclasses import dataclass
import math

from PIL import Image, ImageChops, ImageStat

from .core import State
from .images import load_rgb


@dataclass(frozen=True)
class VisualResult:
    state: State
    matched: bool = False
    detail: str = ''
    thumbnail_bounds: tuple[int, int, int, int] | None = None


def prepare_source(path):
    return load_rgb(path)


def _difference(first, second):
    return sum(ImageStat.Stat(ImageChops.difference(first, second)).mean) / 3


def _samples(center, radius, step):
    return sorted(set(range(center - radius, center + radius + 1, step)) | {center})


def _match_thumbnail(frame, source, expected_size, scale):
    """Calibrate only the expected outgoing bubble, never search other rows.

    Qt can round/layout the same image a few pixels differently. Preserve the
    source aspect ratio, cap size changes at 5%, and keep the original pixel
    error limit. The resulting bounds are shared by status and menu checks.
    """
    width, height = expected_size
    longest = max(width, height)
    right0, top0 = round(frame.width - 70 * scale), round(8 * scale)
    size_radius = max(1, round(longest * .05))
    right_radius, top_radius = round(6 * scale), round(4 * scale)
    reference = source.resize((96, 96), Image.Resampling.LANCZOS).crop((5, 5, 91, 91))
    small_reference = source.resize((32, 32), Image.Resampling.BILINEAR).crop((2, 2, 30, 30))

    def bounds_for(size, right, top):
        if (abs(size - longest) > size_radius or abs(right - right0) > right_radius
                or abs(top - top0) > top_radius):
            return None
        if width >= height:
            w, h = size, round(size * source.height / source.width)
        else:
            w, h = round(size * source.width / source.height), size
        left, bottom = right - w, top + h
        if (min(w, h) < 40 * scale or left < 40 * scale or top < 0
                or right > frame.width or bottom > frame.height
                or frame.height - bottom > 80 * scale):
            return None
        return left, top, right, bottom

    def full_error(bounds):
        sample = frame.crop(bounds).resize((96, 96), Image.Resampling.LANCZOS).crop((5, 5, 91, 91))
        return _difference(reference, sample)

    nominal = bounds_for(longest, right0, top0)
    # An almost exact match needs no search. Borderline matches are calibrated.
    if nominal and full_error(nominal) <= 2:
        return nominal
    candidates = {}

    def score(size, right, top):
        key = (size, right, top)
        if key in candidates:
            return
        bounds = bounds_for(*key)
        if bounds is not None:
            sample = frame.crop(bounds).resize((32, 32), Image.Resampling.BILINEAR).crop((2, 2, 30, 30))
            candidates[key] = (_difference(small_reference, sample), bounds)

    for size in _samples(longest, size_radius, 3):
        for right in _samples(right0, right_radius, 3):
            for top in _samples(top0, top_radius, 2):
                score(size, right, top)
    coarse = sorted(candidates, key=lambda key: candidates[key][0])[:3]
    for size, right, top in coarse:
        for ds in range(-2, 3):
            for dx in range(-2, 3):
                for dy in range(-1, 2):
                    score(size + ds, right + dx, top + dy)
    finalists = sorted(candidates.values(), key=lambda item: item[0])[:8]
    if not finalists:
        return None
    error, bounds = min((full_error(bounds), bounds) for _, bounds in finalists)
    return bounds if error <= 10 else None


def image_state(frame: Image.Image, source: Image.Image, dpi: int, version: str) -> VisualResult:
    if version != '4.1.15.53' or dpi not in {96, 120, 144, 168, 192}:
        return VisualResult(State.UNKNOWN, detail='未验证的构建或显示缩放')
    scale = dpi / 96
    # Nominal Qt layout, calibrated within a tightly bounded neighborhood.
    expected = source.copy()
    expected.thumbnail((round(240 * scale), round(240 * scale)), Image.Resampling.LANCZOS)
    width, height = expected.size
    if min(width, height) < 40 * scale:
        return VisualResult(State.UNKNOWN, detail='图片缩略图太小，无法可靠检查上传指示')
    frame = frame.convert('RGB')
    thumbnail = _match_thumbnail(frame, source, (width, height), scale)
    if thumbnail is None:
        return VisualResult(State.UNKNOWN, detail='当前缩略图与待发送图片不匹配，或图片布局超出可校准范围')
    left, top, right, bottom = thumbnail
    height = bottom - top

    def result(state, detail):
        return VisualResult(state, True, detail, thumbnail)

    center_x, center_y = left - 18 * scale, top + height / 2
    radius = 16 * scale
    bounds = (round(center_x - radius), round(center_y - radius),
              round(center_x + radius), round(center_y + radius))
    if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > frame.width or bounds[3] > frame.height:
        return result(State.UNKNOWN, '状态区域不完整')
    area = frame.crop(bounds)
    bg = frame.getpixel((max(1, left - round(40 * scale)), round(center_y)))
    points = []
    for y in range(area.height):
        for x in range(area.width):
            pixel = area.getpixel((x, y))
            if max(abs(pixel[i] - bg[i]) for i in range(3)) > 18:
                points.append((x + bounds[0] - center_x, y + bounds[1] - center_y, pixel))
    # A clipped row, tooltip or hover overlay is not a clear status area.
    border = [area.getpixel((x, y)) for x in range(area.width) for y in (0, area.height - 1)]
    border += [area.getpixel((x, y)) for y in range(area.height) for x in (0, area.width - 1)]
    if any(max(abs(p[i] - bg[i]) for i in range(3)) > 18 for p in border):
        return result(State.UNKNOWN, '状态区域被遮挡或布局发生变化')
    if not points:
        return result(State.CLEAR, '本条图片的上传指示区域已清空')
    red = sum(p[0] > 150 and p[0] > p[1] + 45 and p[0] > p[2] + 45 for _, _, p in points)
    if red >= 8 * scale:
        return result(State.FAILED, '本条图片旁出现红色发送失败标记')
    ring = sum(6 * scale <= math.hypot(x, y) <= 10 * scale for x, y, p in points)
    neutral = sum(max(p) - min(p) < 30 for _, _, p in points)
    span_x = max(x for x, _, _ in points) - min(x for x, _, _ in points)
    span_y = max(y for _, y, _ in points) - min(y for _, y, _ in points)
    if (len(points) >= 20 * scale and ring / len(points) > .9
            and neutral / len(points) > .95 and span_x >= 12 * scale and span_y >= 12 * scale):
        return result(State.SENDING, '本条图片旁显示上传转圈，继续等待')
    return result(State.UNKNOWN, '图片状态区域含未识别的指示，不能确认完成')
