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


def prepare_source(path):
    return load_rgb(path)


def image_state(frame: Image.Image, source: Image.Image, dpi: int, version: str) -> VisualResult:
    if version != '4.1.15.53' or dpi not in {96, 120, 144, 168, 192}:
        return VisualResult(State.UNKNOWN, detail='未验证的构建或显示缩放')
    scale = dpi / 96
    # Measured Qt layout: 240 DIP thumbnail box, 70 DIP right inset, 8 DIP top.
    expected = source.copy()
    expected.thumbnail((round(240 * scale), round(240 * scale)), Image.Resampling.LANCZOS)
    width, height = expected.size
    if min(width, height) < 40 * scale:
        return VisualResult(State.UNKNOWN, detail='图片缩略图太小，无法可靠检查上传指示')
    right = round(frame.width - 70 * scale)
    left, top = right - width, round(8 * scale)
    bottom = top + height
    if left < 40 * scale or bottom > frame.height or frame.height - bottom > 80 * scale:
        return VisualResult(State.UNKNOWN, detail='图片布局不在已验证范围内')
    frame = frame.convert('RGB')
    actual = frame.crop((left, top, right, bottom))
    # Ignore Qt's rounded corners while comparing the source and thumbnail.
    a = expected.resize((96, 96)).crop((5, 5, 91, 91))
    b = actual.resize((96, 96)).crop((5, 5, 91, 91))
    error = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
    if error > 10:
        return VisualResult(State.UNKNOWN, detail='当前缩略图与待发送图片不匹配')
    center_x, center_y = left - 18 * scale, top + height / 2
    radius = 16 * scale
    bounds = (round(center_x - radius), round(center_y - radius),
              round(center_x + radius), round(center_y + radius))
    if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > frame.width or bounds[3] > frame.height:
        return VisualResult(State.UNKNOWN, detail='状态区域不完整')
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
        return VisualResult(State.UNKNOWN, True, '状态区域被遮挡或布局发生变化')
    if not points:
        return VisualResult(State.CLEAR, True, '本条图片的上传指示区域已清空')
    red = sum(p[0] > 150 and p[0] > p[1] + 45 and p[0] > p[2] + 45 for _, _, p in points)
    if red >= 8 * scale:
        return VisualResult(State.FAILED, True, '本条图片旁出现红色发送失败标记')
    ring = sum(6 * scale <= math.hypot(x, y) <= 10 * scale for x, y, p in points)
    neutral = sum(max(p) - min(p) < 30 for _, _, p in points)
    span_x = max(x for x, _, _ in points) - min(x for x, _, _ in points)
    span_y = max(y for _, y, _ in points) - min(y for _, y, _ in points)
    if (len(points) >= 20 * scale and ring / len(points) > .9
            and neutral / len(points) > .95 and span_x >= 12 * scale and span_y >= 12 * scale):
        return VisualResult(State.SENDING, True, '本条图片旁显示上传转圈，继续等待')
    return VisualResult(State.UNKNOWN, True, '图片状态区域含未识别的指示，不能确认完成')
