"""
Lossless minimap / HUD recorder for offline benchmarking (developer tool).

Started with `main.py --record-minimap SECONDS`. Every in-game tracker tick (throttled to `fps`)
the top-left HUD capture the minimap tracker consumed is written as a lossless PNG (BGRA kept:
the camera-cone tier-1 detector reads the alpha channel), together with one JSON line of tracker
telemetry. Recording time only advances while frames arrive (Alt+Tab / Map M pause it).

Output: <diagnostics root>/../minimap_rec/rec_YYYYMMDD_HHMMSS/{frames/000001.png, meta.jsonl, info.json}
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import queue
import threading
from typing import Any, Callable, Dict, Optional

import numpy as np

from src.image_io import write_image
from src.paths import get_diagnostics_dir

logger = logging.getLogger("GenshinHelper.MinimapRecorder")


def default_recording_root() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(get_diagnostics_dir())), "minimap_rec")


class MinimapRecorder:
    def __init__(
        self,
        seconds: float,
        out_root: Optional[str] = None,
        *,
        fps: float = 30.0,
        max_queue: int = 180,
        on_done: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self.seconds = max(1.0, float(seconds))
        self.fps = max(1.0, float(fps))
        self.target_frames = int(round(self.seconds * self.fps))
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out_dir = os.path.join(out_root or default_recording_root(), f"rec_{stamp}")
        self.frames_dir = os.path.join(self.out_dir, "frames")
        self.on_done = on_done
        self.frames = 0
        self.dropped = 0
        self.write_errors = 0
        self._last_ts: Optional[float] = None
        self._finished = False
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=max(8, int(max_queue)))
        self._meta_fh = None
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return not self._finished

    def _start_worker(self) -> None:
        os.makedirs(self.frames_dir, exist_ok=True)
        self._meta_fh = open(os.path.join(self.out_dir, "meta.jsonl"), "w", encoding="utf-8")
        self._worker = threading.Thread(target=self._run, name="MinimapRecorder", daemon=True)
        self._worker.start()
        logger.info(f"[Recorder] Recording {self.seconds:.0f}s of minimap frames -> {self.out_dir}")

    def add(self, hud_img: Optional[np.ndarray], frame_w: int, frame_h: int, ts: float,
            meta: Optional[Dict[str, Any]] = None) -> None:
        """Queues one HUD capture (called from the tracking thread; never blocks)."""
        if self._finished or hud_img is None or getattr(hud_img, "size", 0) == 0:
            return
        with self._lock:
            if self._last_ts is not None and (ts - self._last_ts) < (1.0 / self.fps) * 0.9:
                return
            if self._worker is None:
                self._start_worker()
            self._last_ts = ts
            self.frames += 1
            idx = self.frames
            last = idx >= self.target_frames
        rec = {"i": idx, "ts": round(float(ts), 4), "frame_w": int(frame_w), "frame_h": int(frame_h)}
        if meta:
            rec.update(meta)
        try:
            self._q.put_nowait((idx, hud_img, rec))
        except queue.Full:
            self.dropped += 1
        if last:
            self.finish()

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        if self._worker is None:
            return
        self._q.put(None)

    def join(self, timeout: Optional[float] = None) -> None:
        if self._worker is not None:
            self._worker.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            idx, img, rec = item
            path = os.path.join(self.frames_dir, f"{idx:06d}.png")
            try:
                if not write_image(path, img):
                    self.write_errors += 1
                self._meta_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:  # pragma: no cover - disk full etc.
                self.write_errors += 1
                logger.debug(f"[Recorder] write error: {e}")
        try:
            self._meta_fh.close()
            info = {
                "frames": self.frames,
                "dropped": self.dropped,
                "write_errors": self.write_errors,
                "fps": self.fps,
                "seconds": self.seconds,
            }
            with open(os.path.join(self.out_dir, "info.json"), "w", encoding="utf-8") as fh:
                json.dump(info, fh, indent=1)
        except Exception as e:  # pragma: no cover
            logger.debug(f"[Recorder] finalize error: {e}")
        logger.info(
            f"[Recorder] Done: {self.frames} frames ({self.dropped} dropped) -> {self.out_dir}"
        )
        if self.on_done is not None:
            try:
                self.on_done(self.out_dir, self.frames)
            except Exception as e:  # pragma: no cover
                logger.debug(f"[Recorder] on_done error: {e}")
