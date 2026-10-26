"""Identify the submitted occurrence of an image in caller-supplied frames.

This module only examines in-memory images. It never captures a window, reads
message text, presses keys, or accesses the clipboard. A matching historical
photo is allowed; a changed coordinate alone is not proof of a new message.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from .image_utils import VisualBubbleVerifier

Box = Tuple[int, int, int, int]


def _overlap(first: Box, second: Box) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    area = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    union = aw * ah + bw * bh - area
    return area / float(union) if union > 0 else 0.0


class ImageSubmissionTracker:
    """Track a new outgoing occurrence without banning repeated pictures.

    ``observe`` returns whether the candidate belongs to this submission, not
    whether its upload has completed. The caller must still enforce its upload
    indicator, motion, quiet-time and final pre-name checks.
    """

    def __init__(
        self, pre_frame: Image.Image, historical_boxes: Sequence[Box],
        pre_uploading_boxes: Sequence[Box] = (),
    ) -> None:
        self.pre_frame = pre_frame.convert("RGB").copy()
        self.historical_boxes = self._boxes(historical_boxes)
        self.pre_uploading_boxes = self._boxes(pre_uploading_boxes)
        self._pre = np.asarray(self.pre_frame, dtype=np.int16)
        self._indicator = VisualBubbleVerifier()
        self._observed_upload_box: Optional[Box] = None
        self._scroll_cache: dict = {}

    def _outgoing(self, box: Box) -> bool:
        x, y, width, height = box
        frame_width, frame_height = self.pre_frame.size
        return (width >= 24 and height >= 24 and x >= 0 and y >= 0
                and x + width <= frame_width and y + height <= frame_height
                and x + width * .5 >= frame_width * .55
                and x + width >= frame_width * .75)

    def _boxes(self, boxes: Sequence[Box]) -> List[Box]:
        result: List[Box] = []
        for value in boxes:
            try:
                box = tuple(map(int, value))
            except (TypeError, ValueError):
                continue
            if len(box) != 4 or not self._outgoing(box):
                continue
            if not any(_overlap(box, prior) >= .65 for prior in result):
                result.append(box)
        return sorted(result, key=lambda box: box[1] + box[3])

    def _raw_changed(self, frame: Image.Image, box: Box) -> bool:
        x, y, width, height = box
        before = self._pre[y:y + height, x:x + width]
        after = np.asarray(frame.convert("RGB").crop((x, y, x + width, y + height)), dtype=np.int16)
        if before.shape != after.shape or not before.size:
            return False
        delta = np.abs(after - before)
        return float(np.mean(delta)) > 2.0 and float(np.mean(delta > 12)) > .06

    def _alignment_score(self, current: np.ndarray, shift: int) -> Optional[float]:
        """Check old textured pixels rather than letting blank backgrounds win.

        A negative shift means old messages moved upward. New pixels at the
        bottom are intentionally outside the comparison. At zero shift, only
        pre-existing content supplies support; a new photo over blank space
        does not invalidate an otherwise unchanged history.
        """
        height, width = self._pre.shape[:2]
        overlap_height = height + shift
        if shift > 0 or overlap_height < max(80, int(height * .25)):
            return None
        left = int(width * .32)
        before = self._pre[-shift:height, left:]
        after = current[:overlap_height, left:]
        if before.shape != after.shape or min(before.shape[:2]) < 8:
            return None
        # Bound the cost on high-DPI windows while retaining character/photo
        # edges. These are samples from exact client pixels, not template fits.
        step = max(1, int(max(before.shape[:2]) / 480))
        before = before[::step, ::step]
        after = after[::step, ::step]
        gray = np.mean(before, axis=2)
        dx = np.abs(gray[:, 1:] - gray[:, :-1])
        dy = np.abs(gray[1:, :] - gray[:-1, :])
        textured = np.zeros(gray.shape, dtype=bool)
        textured[:, 1:] |= dx > 7
        textured[1:, :] |= dy > 7
        # Also retain non-background color patches, so gradients/flat-colored
        # photographs have enough support without counting an empty viewport.
        background = np.median(before.reshape(-1, 3), axis=0)
        textured |= np.max(np.abs(before - background), axis=2) > 22
        support = int(np.count_nonzero(textured))
        if support < max(120, int(gray.size * .004)):
            return None
        delta = np.max(np.abs(after - before), axis=2)[textured]
        return float(np.mean(delta <= 10))

    def _scroll_shift(self, frame: Image.Image, current_boxes: List[Box]) -> Optional[int]:
        # Include a cheap visual signature: identical box coordinates do not
        # imply that the rest of the conversation still has the same content.
        visual = hash(frame.resize((64, 64), Image.Resampling.BILINEAR).tobytes())
        key = (tuple(current_boxes), visual)
        if key in self._scroll_cache:
            return self._scroll_cache[key]
        height = self.pre_frame.height
        proposals = {0}
        for old in self.historical_boxes:
            for now in current_boxes:
                if abs(old[2] - now[2]) <= max(6, old[2] * .08):
                    delta = now[1] - old[1]
                    if -height * .75 <= delta <= 0:
                        for offset in (-2, -1, 0, 1, 2):
                            if -height * .75 <= delta + offset <= 0:
                                proposals.add(delta + offset)
        # Excessive periodic matches are ambiguous, not a reason to perform a
        # large search and choose an arbitrary repeated copy.
        if len(proposals) > 100:
            self._scroll_cache[key] = None
            return None
        current = np.asarray(frame.convert("RGB"), dtype=np.int16)
        scores = [(score, shift) for shift in proposals
                  if (score := self._alignment_score(current, shift)) is not None]
        scores.sort(reverse=True)
        selected = None
        if scores and scores[0][0] >= .96:
            best, shift = scores[0]
            alternatives = [score for score, other in scores if abs(other - shift) > 4]
            if not alternatives or best - max(alternatives) >= .035:
                selected = shift
        self._scroll_cache[key] = selected
        return selected

    def _new_upload_indicator(self, frame: Image.Image, box: Box) -> bool:
        # A pending historical copy may simply finish after scrolling. It must
        # not establish the new submission's lifecycle on its own.
        if self.pre_uploading_boxes:
            return False
        if self._indicator.detect_static_spinner(frame, box):
            if not any(self._indicator.detect_static_spinner(self.pre_frame, old)
                       for old in self.historical_boxes):
                return True
        # A central overlay can replace identical photo pixels at the exact
        # same viewport position. Compare with a pre-existing photo as source,
        # rather than requiring a large whole-photo difference.
        for x, y, width, height in self.historical_boxes:
            reference = self.pre_frame.crop((x, y, x + width, y + height))
            if self._indicator.detect_center_upload_overlay(frame, reference, box):
                return True
        return False

    def observe(
        self, curr_frame: Image.Image, bbox: Optional[Box],
        current_boxes: Sequence[Box], uploading: bool,
    ) -> Tuple[bool, str]:
        if curr_frame.size != self.pre_frame.size or bbox is None:
            self._observed_upload_box = None
            return False, "缺少同尺寸的新图片画面"
        try:
            candidate = tuple(map(int, bbox))
        except (TypeError, ValueError):
            self._observed_upload_box = None
            return False, "图片位置无法识别"
        if len(candidate) != 4 or not self._outgoing(candidate):
            self._observed_upload_box = None
            return False, "图片尚未定位到右侧消息区"
        if (self._observed_upload_box is not None
                and _overlap(candidate, self._observed_upload_box) < .80):
            self._observed_upload_box = None
        boxes = self._boxes(current_boxes)
        if boxes and candidate[1] + candidate[3] < boxes[-1][1] + boxes[-1][3] - 4:
            self._observed_upload_box = None
            return False, "等待本次最下方图片，未使用上方历史图片"
        changed = self._raw_changed(curr_frame, candidate)
        if not self.historical_boxes:
            return (True, "发送前没有同图，已发现本次新图片") if changed else (False, "等待本次新图片上屏")
        if len(boxes) > len(self.historical_boxes) and changed:
            return True, "历史同图之外新增了底部图片"
        shift = self._scroll_shift(curr_frame, boxes)
        if shift is not None:
            projected = [(x, y + shift, width, height)
                         for x, y, width, height in self.historical_boxes]
            mapped_old = any(_overlap(candidate, old) >= .60 for old in projected)
            beyond_history = candidate[1] >= max(y + height for _, y, _, height in projected) - 2
            new_area = shift == 0 or candidate[1] >= self.pre_frame.height + shift - 4
            if not mapped_old and beyond_history and new_area and (shift < 0 or changed):
                return True, "聊天记录对齐后确认底部新图片"
        if uploading and self._new_upload_indicator(curr_frame, candidate):
            self._observed_upload_box = candidate
        if self._observed_upload_box is not None and _overlap(candidate, self._observed_upload_box) >= .80:
            return True, "已跟踪到本次底部图片的上传过程"
        return False, "正在区分本次图片与历史同图，等待新增消息或上传过程证据"
