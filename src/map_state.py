import math
import os
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

from src.asset_setup import resolve_data_asset
from src.zoom_slider import ZoomSliderDetector


class MapScreenStateMachine:
    """
    Visual 3-Anchor Map (M) detector and 4-state FSM (CLOSED -> SETTLING -> SYNCED_IDLE <-> PANNING).
    Fires a single auto-sync trigger once Map (M) finishes opening and settles, and never fires on close.
    """

    STATE_CLOSED = "CLOSED"
    STATE_SETTLING = "SETTLING"
    STATE_SYNCED_IDLE = "SYNCED_IDLE"
    STATE_PANNING = "PANNING"

    ANCHOR_THRESHOLD = 0.82
    SLIDER_THRESHOLD = 0.88
    SLIDER_STRICT_MATCH = 0.975
    SLIDER_CCOEFF_THRESHOLD = 0.65
    CLOSE_DEBOUNCE_FRAMES = 3
    SCENE_CUT_THRESHOLD = 0.40
    MAX_SETTLING_FRAMES = 20
    MAX_RETRIES = 2

    def __init__(self, anchors_path: Optional[str] = None):
        if anchors_path is None:
            anchors_path = str(resolve_data_asset("ui_anchors_v4.npz"))

        self._tpl_gear: Optional[np.ndarray] = None
        self._tpl_close_x: Optional[np.ndarray] = None
        self._scaled_cache: Dict[int, Dict[str, np.ndarray]] = {}

        if os.path.exists(anchors_path):
            with np.load(anchors_path) as data:
                if "tpl_gear" in data:
                    self._tpl_gear = data["tpl_gear"].copy()
                if "tpl_close_x" in data:
                    self._tpl_close_x = data["tpl_close_x"].copy()

        self.state: str = self.STATE_CLOSED
        self._prev_thumb: Optional[np.ndarray] = None
        self._prev_hud: Optional[np.ndarray] = None
        self._synced_thumb: Optional[np.ndarray] = None
        self._prev_hist64: Optional[np.ndarray] = None
        self._stable_frames: int = 0
        self._closed_frames: int = 0
        self._settling_frames: int = 0
        self._settling_drag_frames: int = 0
        self._retries: int = 0

    def reset(self) -> None:
        """Resets the FSM cleanly to CLOSED."""
        self.state = self.STATE_CLOSED
        self._prev_thumb = None
        self._prev_hud = None
        self._synced_thumb = None
        self._prev_hist64 = None
        self._stable_frames = 0
        self._closed_frames = 0
        self._settling_frames = 0
        self._settling_drag_frames = 0
        self._retries = 0

    def _get_scaled_anchors(self, scale_ui: float) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        sz = max(12, int(round(28.0 * scale_ui)))
        if sz in self._scaled_cache:
            cached = self._scaled_cache[sz]
            return cached.get("gear"), cached.get("close")

        entry: Dict[str, np.ndarray] = {}
        interp = cv2.INTER_AREA if sz < 56 else cv2.INTER_LINEAR
        if self._tpl_gear is not None:
            entry["gear"] = self._tpl_gear if sz == self._tpl_gear.shape[0] else cv2.resize(
                self._tpl_gear, (sz, sz), interpolation=interp
            )
        if self._tpl_close_x is not None:
            entry["close"] = self._tpl_close_x if sz == self._tpl_close_x.shape[0] else cv2.resize(
                self._tpl_close_x, (sz, sz), interpolation=interp
            )
        self._scaled_cache[sz] = entry
        return entry.get("gear"), entry.get("close")

    @property
    def has_anchor_templates(self) -> bool:
        """True when bundled/cached Close-X + Gear anchor templates are loaded."""
        return bool(self._tpl_gear is not None and self._tpl_close_x is not None)

    @staticmethod
    def get_slider_probe_rect(
        win_left: int, win_top: int, win_w: int, win_h: int
    ) -> Tuple[int, int, int, int]:
        """Returns left-edge zoom-slider strip `(x, y, w, h)` for template-free Map (M) probing."""
        if win_w <= 0 or win_h <= 0:
            return (int(win_left), int(win_top), 0, 0)
        x0 = max(0, int(win_w * 0.012))
        x1 = min(int(win_w), int(win_w * 0.062))
        y0 = int(win_h * 0.22)
        y1 = int(win_h * 0.68)
        return (int(win_left) + x0, int(win_top) + y0, max(0, x1 - x0), max(0, y1 - y0))

    def is_slider_probe(
        self,
        slider_roi_bgr: Optional[np.ndarray],
        frame_w: int,
        frame_h: int,
    ) -> Tuple[bool, float]:
        """Dual-gated (`ccorr` + `ccoeff`) procedural Zoom-Slider check on the left-edge strip."""
        if frame_h < 500 or frame_w < 800:
            return (False, 0.0)
        x_start = max(0, int(frame_w * 0.012))
        y_start = int(frame_h * 0.22)
        res = ZoomSliderDetector._detect_in_roi(slider_roi_bgr, frame_w, frame_h, x_start, y_start)
        if not res:
            return (False, 0.0)
        score = float(res.get("match_score", 0.0))
        ccoeff = float(res.get("ccoeff", 0.0))
        ok = bool(score >= self.SLIDER_STRICT_MATCH and ccoeff >= self.SLIDER_CCOEFF_THRESHOLD)
        return (ok, score)

    @staticmethod
    def get_close_probe_rect(
        win_left: int, win_top: int, win_w: int, win_h: int
    ) -> Tuple[int, int, int, int]:
        """Returns exact screen-space (x, y, w, h) matching the top-right Close-X ROI."""
        if win_w <= 0 or win_h <= 0:
            return (int(win_left), int(win_top), 0, 0)
        x_off = int(0.90 * win_w)
        return (int(win_left) + x_off, int(win_top), int(win_w) - x_off, int(0.11 * win_h))

    def is_close_x_probe(
        self,
        close_roi_bgr: Optional[np.ndarray],
        frame_h: int,
        threshold: float = 0.78,
    ) -> Tuple[bool, float]:
        """Runs scaled Close-X TM_CCOEFF_NORMED check on the top-right ROI only (no full frame needed)."""
        if (
            close_roi_bgr is None
            or close_roi_bgr.size == 0
            or close_roi_bgr.ndim != 3
            or close_roi_bgr.shape[2] < 3
            or frame_h < 500
        ):
            return (False, 0.0)
        if close_roi_bgr.shape[0] < 4 or close_roi_bgr.shape[1] < 4:
            return (False, 0.0)

        scale_ui = frame_h / 1440.0
        _, tpl_close = self._get_scaled_anchors(scale_ui)
        if tpl_close is None:
            return (False, 0.0)

        roi = close_roi_bgr[:, :, :3] if close_roi_bgr.shape[2] > 3 else close_roi_bgr
        ch = cv2.resize(
            roi,
            (
                max(tpl_close.shape[1], roi.shape[1] // 2),
                max(tpl_close.shape[0], roi.shape[0] // 2),
            ),
            interpolation=cv2.INTER_AREA,
        )
        res_c = cv2.matchTemplate(ch, tpl_close, cv2.TM_CCOEFF_NORMED)
        close_score = float(cv2.minMaxLoc(res_c)[1])
        return (bool(close_score >= threshold), close_score)

    @staticmethod
    def _compute_rgb_hist64(frame: np.ndarray) -> np.ndarray:
        """Fast 4x4x4 RGB histogram sampling every 8th pixel, normalized L1 float32 vector of length 64."""
        if frame is None or frame.size == 0 or frame.ndim < 2:
            return np.zeros(64, dtype=np.float32)
        sub = frame[::8, ::8]
        if sub.size == 0:
            return np.zeros(64, dtype=np.float32)
        if sub.dtype != np.uint8:
            sub = np.clip(sub, 0, 255).astype(np.uint8)
        if sub.ndim == 2:
            q = sub >> 6
            idx = (q << 4) | (q << 2) | q
        elif sub.shape[2] >= 3:
            idx = ((sub[:, :, 2] & 0xC0) >> 2) | ((sub[:, :, 1] & 0xC0) >> 4) | (sub[:, :, 0] >> 6)
        else:
            q = sub[:, :, 0] >> 6
            idx = (q << 4) | (q << 2) | q
        hist = np.bincount(idx.ravel(), minlength=64).astype(np.float32)
        total = float(hist.sum())
        if total > 0.0:
            hist /= total
        return hist

    @staticmethod
    def _bhattacharyya_distance(h1: np.ndarray, h2: np.ndarray) -> float:
        """Computes Bhattacharyya distance sqrt(max(0.0, 1.0 - sum(sqrt(h1 * h2)))) in [0.0, 1.0]."""
        if h1 is None or h2 is None or h1.size == 0 or h2.size == 0:
            return 0.0
        a = np.asarray(h1, dtype=np.float64)
        b = np.asarray(h2, dtype=np.float64)
        bc = float(np.sum(np.sqrt(np.maximum(0.0, a * b))))
        return float(math.sqrt(max(0.0, 1.0 - bc)))

    @staticmethod
    def _compute_center_thumb(frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        crop = frame[int(0.25 * h):int(0.75 * h), int(0.25 * w):int(0.75 * w)]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _compute_hud_thumb(frame: np.ndarray) -> np.ndarray:
        """Captures bottom-right region title and right-side layer bar to detect Map (M) fade-in."""
        h, w = frame.shape[:2]
        br = cv2.resize(
            cv2.cvtColor(frame[int(0.86 * h):int(0.98 * h), int(0.75 * w):int(0.98 * w)], cv2.COLOR_BGR2GRAY),
            (48, 16), interpolation=cv2.INTER_AREA,
        )
        rt = cv2.resize(
            cv2.cvtColor(frame[int(0.32 * h):int(0.68 * h), int(0.91 * w):int(0.99 * w)], cv2.COLOR_BGR2GRAY),
            (16, 48), interpolation=cv2.INTER_AREA,
        )
        return np.concatenate([br.ravel(), rt.ravel()])

    @staticmethod
    def _compute_slider_ccoeff(frame: np.ndarray, slider_res: Optional[Dict[str, Any]]) -> float:
        """Zero-mean diamond NCC at the detected slider thumb to separate Map M from browser/blank frames."""
        if not slider_res or frame is None or frame.size == 0:
            return 0.0
        h, w = frame.shape[:2]
        scale_ui = h / 1440.0
        tw = max(15, int(round(31.0 * scale_ui)))
        th = max(15, int(round(31.0 * scale_ui)))
        tpl, mask = ZoomSliderDetector._get_diamond_template_and_mask(th, tw)
        cx = int(round(slider_res.get("x_track", -1)))
        cy = int(round(slider_res.get("y_thumb_global", -1)))
        x0, y0 = cx - tw // 2, cy - th // 2
        x1, y1 = x0 + tw, y0 + th
        if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
            return 0.0
        patch = frame[y0:y1, x0:x1]
        if patch.shape != tpl.shape:
            return 0.0
        m = mask > 0
        a = patch[m].astype(np.float32)
        b = tpl[m].astype(np.float32)
        a -= a.mean()
        b -= b.mean()
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-3 or nb < 1e-3:
            return 0.0
        return float(np.dot(a.ravel(), b.ravel()) / (na * nb))

    def is_map_m_open(self, frame: Optional[np.ndarray]) -> Dict[str, Any]:
        """Checks all 3 invariant visual anchors (ZoomSlider, bottom-left Gear, top-right Close X)."""
        if frame is None or frame.size == 0 or frame.ndim < 2:
            return {
                "is_open": False,
                "slider_strict_ok": False,
                "slider_ccoeff": 0.0,
                "slider_res": None,
                "gear_score": 0.0,
                "close_score": 0.0,
            }

        h, w = frame.shape[:2]
        if h < 500 or w < 800:
            return {
                "is_open": False,
                "slider_strict_ok": False,
                "slider_ccoeff": 0.0,
                "slider_res": None,
                "gear_score": 0.0,
                "close_score": 0.0,
            }

        slider_res = ZoomSliderDetector.detect_slider(frame)
        match_score = slider_res.get("match_score", 0.0) if slider_res is not None else 0.0
        slider_ok = bool(slider_res is not None and match_score >= self.SLIDER_THRESHOLD)
        if slider_ok:
            cc_cached = slider_res.get("ccoeff")
            slider_ccoeff = float(cc_cached) if cc_cached is not None else self._compute_slider_ccoeff(frame, slider_res)
        else:
            slider_ccoeff = 0.0
        slider_strict_ok = bool(
            slider_res is not None
            and match_score >= self.SLIDER_STRICT_MATCH
            and slider_ccoeff >= self.SLIDER_CCOEFF_THRESHOLD
        )

        scale_ui = h / 1440.0
        tpl_gear, _ = self._get_scaled_anchors(scale_ui)

        gear_score = 0.0
        if tpl_gear is not None:
            gear_roi = frame[int(0.88 * h):h, 0:int(0.09 * w)]
            if gear_roi.shape[0] >= 4 and gear_roi.shape[1] >= 4:
                gh = cv2.resize(
                    gear_roi,
                    (max(tpl_gear.shape[1], gear_roi.shape[1] // 2), max(tpl_gear.shape[0], gear_roi.shape[0] // 2)),
                    interpolation=cv2.INTER_AREA,
                )
                res_g = cv2.matchTemplate(gh, tpl_gear, cv2.TM_CCOEFF_NORMED)
                gear_score = float(cv2.minMaxLoc(res_g)[1])

        close_roi = frame[0:int(0.11 * h), int(0.90 * w):w]
        _, close_score = self.is_close_x_probe(close_roi, h, threshold=self.ANCHOR_THRESHOLD)

        gear_ok = bool(gear_score >= self.ANCHOR_THRESHOLD)
        close_ok = bool(close_score >= self.ANCHOR_THRESHOLD)
        anchor_count = int(slider_ok) + int(gear_ok) + int(close_ok)
        is_open = (
            bool(slider_ok and gear_ok and close_ok)
            if self.has_anchor_templates
            else bool(slider_strict_ok)
        )
        return {
            "is_open": is_open,
            "anchor_count": anchor_count,
            "slider_strict_ok": slider_strict_ok,
            "slider_ccoeff": slider_ccoeff,
            "slider_res": slider_res,
            "gear_score": gear_score,
            "close_score": close_score,
        }

    def step(self, frame: Optional[np.ndarray]) -> Dict[str, Any]:
        """Advances the Map (M) state machine by one captured frame."""
        check = self.is_map_m_open(frame)
        if frame is None:
            self.reset()
            return {"state": self.STATE_CLOSED, "should_auto_sync": False, **check}

        if not check["is_open"]:
            if self.state != self.STATE_CLOSED:
                # Hysteresis: when Map (M) is already open and a drawer/popup covers right 'X' or
                # bottom-left Gear, keep the open session alive via strict slider correlation.
                if check.get("slider_strict_ok") or (
                    check.get("anchor_count", 0) == 2
                    and check.get("gear_score", 0.0) >= self.ANCHOR_THRESHOLD
                ):
                    self._closed_frames = 0
                    if self.state == self.STATE_SETTLING:
                        self.state = self.STATE_PANNING
                    self._prev_thumb = self._compute_center_thumb(frame)
                    self._prev_hud = self._compute_hud_thumb(frame)
                    self._prev_hist64 = self._compute_rgb_hist64(frame)
                    return {"state": self.state, "should_auto_sync": False, **check}

                # Instant scene-cut map-close detection via 64-bin RGB histogram Bhattacharyya distance
                bhatt = 0.0
                if self._prev_hist64 is not None and frame.size > 0:
                    cur_hist64 = self._compute_rgb_hist64(frame)
                    bhatt = self._bhattacharyya_distance(self._prev_hist64, cur_hist64)
                    if bhatt >= self.SCENE_CUT_THRESHOLD:
                        self.reset()
                        return {
                            "state": self.STATE_CLOSED,
                            "should_auto_sync": False,
                            "scene_cut": True,
                            "bhatt_dist": bhatt,
                            **check,
                        }

                # Debounce transient 1-2 frame drag/zoom anchor drops before resetting to CLOSED
                self._closed_frames += 1
                if self._closed_frames < self.CLOSE_DEBOUNCE_FRAMES:
                    return {
                        "state": self.state,
                        "should_auto_sync": False,
                        "scene_cut": False,
                        "bhatt_dist": bhatt,
                        **check,
                    }

                self.reset()
                return {
                    "state": self.STATE_CLOSED,
                    "should_auto_sync": False,
                    "scene_cut": False,
                    "bhatt_dist": bhatt,
                    **check,
                }

            self.reset()
            return {"state": self.STATE_CLOSED, "should_auto_sync": False, **check}

        self._closed_frames = 0
        thumb = self._compute_center_thumb(frame)
        hud_thumb = self._compute_hud_thumb(frame)
        self._prev_hist64 = self._compute_rgb_hist64(frame)

        if self.state == self.STATE_CLOSED:
            self.state = self.STATE_SETTLING
            self._prev_thumb = thumb
            self._prev_hud = hud_thumb
            self._synced_thumb = None
            self._stable_frames = 0
            self._settling_frames = 0
            self._settling_drag_frames = 0
            self._retries = 0
            return {"state": self.state, "should_auto_sync": False, **check}

        diff = float(np.mean(cv2.absdiff(thumb, self._prev_thumb))) if self._prev_thumb is not None else 0.0
        hud_diff = float(np.mean(cv2.absdiff(hud_thumb, self._prev_hud))) if self._prev_hud is not None else 0.0
        self._prev_thumb = thumb
        self._prev_hud = hud_thumb

        if self.state == self.STATE_SETTLING:
            self._settling_frames += 1
            # Allow ~180ms (first 3 settling frames at 45ms) for Map M center/cave fade-in,
            # then abort on 2 consecutive panning frames with static HUD.
            if self._settling_frames >= 4 and diff > 8.0 and hud_diff < 2.5:
                self._settling_drag_frames += 1
            else:
                self._settling_drag_frames = 0

            if self._settling_drag_frames >= 2 or self._settling_frames > self.MAX_SETTLING_FRAMES:
                self.state = self.STATE_PANNING
                return {"state": self.state, "should_auto_sync": False, **check}

            if diff < 3.5 and hud_diff < 3.5:
                self._stable_frames += 1
            else:
                self._stable_frames = 0

            if self._stable_frames >= 2:
                self.state = self.STATE_SYNCED_IDLE
                self._synced_thumb = thumb
                return {"state": self.state, "should_auto_sync": True, **check}

            return {"state": self.state, "should_auto_sync": False, **check}

        # SYNCED_IDLE <-> PANNING while user inspects the map (never re-fires auto-sync)
        if diff > 4.5:
            self.state = self.STATE_PANNING
        elif diff < 3.0 and self.state == self.STATE_PANNING:
            self.state = self.STATE_SYNCED_IDLE

        return {"state": self.state, "should_auto_sync": False, **check}

    def mark_synced(self, frame: Optional[np.ndarray] = None) -> None:
        """Marks the current open-map session as manually/already synced to prevent double firing."""
        if frame is not None and frame.size > 0 and frame.shape[0] >= 500 and frame.shape[1] >= 800:
            thumb = self._compute_center_thumb(frame)
            hud_thumb = self._compute_hud_thumb(frame)
            self._prev_thumb = thumb
            self._prev_hud = hud_thumb
            self._synced_thumb = thumb
            self._prev_hist64 = self._compute_rgb_hist64(frame)
        self.state = self.STATE_SYNCED_IDLE
        self._stable_frames = 2
        self._closed_frames = 0
        self._settling_frames = 0
        self._settling_drag_frames = 0

    def request_retry(self) -> bool:
        """Allows bounded settling retries (max 2) when locator transiently missed world coords."""
        if self._retries < self.MAX_RETRIES:
            self._retries += 1
            self.state = self.STATE_SETTLING
            self._stable_frames = 0
            self._settling_frames = 0
            self._settling_drag_frames = 0
            return True
        self.state = self.STATE_SYNCED_IDLE
        return False
