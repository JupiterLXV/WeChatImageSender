"""Small, private-by-default timing summaries for a single queue item.

No image, name, source path, chat title or exception text is recorded. Timings
are buffered in memory and appended once per item; a slow/unwritable log must
never change whether a message is submitted or confirmed.
"""
from __future__ import annotations

import contextlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional


class ItemPerformanceTrace:
    def __init__(self, index: int, clock=None) -> None:
        self.index = int(index)
        self.clock = clock or time.perf_counter
        self.started = self.clock()
        self.stages: Dict[str, dict] = {}
        self.counters: Dict[str, float] = {}
        self._stack = []

    @contextlib.contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        frame = None
        try:
            allowed = {"capture", "source_decode", "analysis", "wait", "clipboard", "source_check",
                       "locate_chat", "save_state", "image_submit", "image_verify", "final_image_check",
                       "name_submit", "prepare_templates"}
            frame = [stage if stage in allowed else "other", self.clock(), 0.0]
            self._stack.append(frame)
        except Exception:
            frame = None
        try:
            yield
        finally:
            if frame is not None:
                try:
                    elapsed = max(0.0, self.clock() - frame[1])
                    self._stack.pop()
                    if self._stack:
                        self._stack[-1][2] += elapsed
                    value = self.stages.setdefault(frame[0], {
                        "count": 0, "total_ms": 0.0, "own_ms": 0.0, "max_ms": 0.0,
                    })
                    value["count"] += 1
                    value["total_ms"] += elapsed * 1000.0
                    value["own_ms"] += max(0.0, elapsed - frame[2]) * 1000.0
                    value["max_ms"] = max(value["max_ms"], elapsed * 1000.0)
                except Exception:
                    # Only bookkeeping fails here. Exceptions from the body
                    # still propagate through this generator unchanged.
                    try:
                        if self._stack and self._stack[-1] is frame:
                            self._stack.pop()
                    except Exception:
                        self._stack = []

    def add_counters(self, values: dict) -> None:
        # Only known numerical counters can cross the diagnostic boundary.
        allowed = {"cache_hits", "cache_misses", "full_searches", "local_searches",
                   "template_resizes", "template_cache_hits", "source_cache_hits", "source_cache_misses",
                   "roi_hits", "roi_misses", "match_calls",
                   "analysis_pixels", "capture_frames", "prepare_retries"}
        for key, value in values.items():
            if key in allowed and isinstance(value, (int, float)) and math.isfinite(value):
                self.counters[key] = float(value)

    def snapshot(self, outcome: str) -> dict:
        allowed_outcomes = {"PENDING", "IMAGE_SUBMITTED", "IMAGE_CONFIRMED",
                            "NAME_SUBMITTED", "COMPLETED", "NEEDS_MANUAL_VERIFY"}
        return {
            "schema": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "item_index": self.index,
            "outcome": outcome if outcome in allowed_outcomes else "UNKNOWN",
            "elapsed_ms": round(max(0.0, self.clock() - self.started) * 1000.0, 3),
            "stages": {name: {key: round(number, 3) if isinstance(number, float) else number
                              for key, number in values.items()}
                       for name, values in self.stages.items()},
            "counters": dict(self.counters),
        }


class PerformanceRecorder:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.last_path: Optional[Path] = None
        self.last_summary: Optional[dict] = None

    def finish(self, trace: ItemPerformanceTrace, outcome: str) -> Optional[Path]:
        # Logging is observational: no diagnostics exception can cause a retry.
        try:
            summary = trace.snapshot(outcome)
            self.last_summary = summary
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / ("timings-" + datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(summary, ensure_ascii=True, allow_nan=False) + "\n")
            self.last_path = path
            return path
        except Exception:
            return None
