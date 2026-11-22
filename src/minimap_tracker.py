# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/tracker.py (MIT License)
# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/live_worker.py (MIT License)
# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/performance.py (MIT License)
# Copyright (c) 2026 MaxWay12
# MinimapGate, 2-zoom regime hysteresis, annulus TM_CCOEFF_NORMED + 1D parabolic sub-pixel refinement,
# Follow-View ORB + estimateAffinePartial2D rotation recovery, bounded cv2.phaseCorrelate fallback,
# and multi-layer surface-vs-underground floor comparison are clean-room original implementations.
"""Hybrid minimap local tracker, 4-state FSM, 2-zoom/floor hysteresis, and daemon worker."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace
import logging
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from src.game_settings import compute_ui_layout, detect_hdr_clipping
from src.heading360 import (
    CameraHeadingFuser,
    CircularAngleSmoother,
    extract_camera_view_cone,
    extract_player_arrow_360,
    shortest_angle_delta,
    wrap_angle_360,
)
from src.paths import get_asset_path
from src.tile_cache import LruTerrainTileCache

logger = logging.getLogger("GenshinHelper.MinimapTracker")

STATE_LOST = "LOST"
STATE_ACQUIRING = "ACQUIRING"
STATE_TRACKING = "TRACKING"
STATE_RELOCATING = "RELOCATING"

MODE_LOCK_DIRECTION = "lock_direction"
MODE_FOLLOW_VIEW = "follow_view"

ZOOM_OPEN_WORLD = "open_world"
ZOOM_CITY = "city"

GlobalLocatorFn = Callable[[np.ndarray, Optional[int]], Optional[Tuple[int, float, float, float]]]


# ---------------------------------------------------------------------------
# Data Models & Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MinimapTrackerConfig:
    """Configuration for 4-state FSM, annulus matching, 2-zoom hysteresis, and fallbacks."""

    # 4-State FSM parameters (ported from Genshin-Navigator TrackerConfig, in HoYoLAB world units)
    min_confidence: float = 0.45
    acquire_hits: int = 2
    relocate_hits: int = 3
    candidate_radius_world: float = 35.0
    max_speed_world_per_second: float = 120.0
    jump_margin_world: float = 25.0
    smoothing_alpha: float = 0.4
    lost_timeout_seconds: float = 1.5

    # Minimap geometry & 2-zoom world span (diameter across minimap crop, resolution-independent)
    open_world_span_world: float = 360.0
    city_span_world: float = 120.0  # ~3x zoom-in inside cities
    open_world_patch_px_per_unit: float = 0.5  # downscaled 0.5x -> 0.25 px/unit (native terrain_n2_v4)
    city_patch_px_per_unit: float = 1.0  # downscaled 0.5x -> 0.50 px/unit
    search_radius_open_world: float = 96.0
    search_radius_city: float = 48.0
    match_downscale: float = 0.5

    # Annulus radii relative to minimap radius R = 0.5 * min(H, W)
    annulus_inner_ratio: float = 0.20
    annulus_outer_ratio: float = 0.85

    # 2-Zoom regime switch hysteresis (anti-jitter)
    zoom_switch_min_corr: float = 0.72
    zoom_switch_margin: float = 0.08
    zoom_switch_consecutive_wins: int = 2

    # Follow-View ORB + estimateAffinePartial2D rotation invariance
    follow_view_cam_pin_deg: float = 12.0
    follow_view_unlock_deg: float = 16.0
    follow_view_min_map_rot_deg: float = 8.0
    follow_view_min_inliers: int = 8

    # Bounded sparse-terrain phaseCorrelate fallback
    template_min_corr: float = 0.45
    phase_min_response: float = 0.15
    phase_max_shift_frac: float = 0.25
    max_phase_fallback_frames: int = 5

    # Multi-layer surface vs underground floor comparison hysteresis
    floor_switch_margin: float = 0.08
    floor_switch_min_score: float = 0.25
    floor_switch_consecutive_wins: int = 3

    # Gate parameters
    gate_min_std: float = 12.0
    gate_stationary_diff: float = 1.2
    gate_cone_change_diff: float = 0.15
    gate_border_coherence: float = 0.42

    # v5.3 HD SIFT + Fast Zero-Mean Sobel + Paimon Pause Gate + Auto-Layer + Global Relocate
    sift_min_inliers: int = 6
    sobel_min_corr: float = 0.34
    paimon_thresh: float = 0.56
    broadcast_min_sift_inliers: int = 6
    broadcast_min_sobel_corr: float = 0.38
    enable_z0_city: bool = True
    enable_auto_layer: bool = True
    enable_relocating_guard: bool = True
    enable_interframe_sobel: bool = True
    interframe_sobel_k: int = 3
    interframe_sobel_min_corr: float = 0.38
    enable_global_relocate: bool = True
    hold_on_phase_exhausted: bool = False
    arrow_gate_change_diff: float = 1.8

    def __post_init__(self) -> None:
        if not 0.0 < self.smoothing_alpha <= 1.0:
            raise ValueError(f"smoothing_alpha must be in (0, 1], got {self.smoothing_alpha}")
        if self.acquire_hits < 1 or self.relocate_hits < 1:
            raise ValueError("acquire_hits and relocate_hits must be >= 1")
        if self.lost_timeout_seconds <= 0.0:
            raise ValueError("lost_timeout_seconds must be > 0")
        if self.max_phase_fallback_frames < 1:
            raise ValueError("max_phase_fallback_frames must be >= 1")


@dataclass(frozen=True)
class MinimapGateResult:
    """Output of `MinimapGate.check()`."""

    valid: bool
    stationary: bool
    frame_diff: float
    alpha_diff: float
    center_diff: float
    inner_std: float
    border_coherence: float
    hdr_clipped: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class PoseMeasurement:
    """Raw localization observation fed into the 4-state FSM."""

    found: bool
    map_id: Optional[int] = None
    world_x: Optional[float] = None
    world_y: Optional[float] = None
    confidence: float = 0.0
    is_underground: bool = False
    floor_id: Optional[int] = None
    floor_name: str = "G"
    match_method: str = "none"
    reason: Optional[str] = None


@dataclass(frozen=True)
class MinimapPose:
    """Unified pose snapshot emitted by `HybridMinimapTracker` and `LocalizationWorker`."""

    state: str
    map_id: Optional[int]
    world_x: Optional[float]
    world_y: Optional[float]
    lat: Optional[float]
    lng: Optional[float]
    camera_heading: Optional[float]
    body_heading: Optional[float]
    view_heading: Optional[float]
    minimap_mode: str
    zoom_regime: str
    is_underground: bool
    floor_id: Optional[int]
    confidence: float
    timestamp: float
    hdr_clipped: bool
    floor_name: str = "G"
    stationary: bool = False
    match_method: str = "none"
    map_rotation_deg: float = 0.0
    reason: Optional[str] = None
    should_broadcast_web: bool = True
    span_world: float = 360.0
    paimon_score: float = 1.0
    sift_inliers: int = 0
    zoom_tier: int = -1
    cal_dx_px: int = 0
    cal_dy_px: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# MinimapGate: Blank/Flash Gate, Border Check (Lock & Follow View), Frame-Diff
# ---------------------------------------------------------------------------
class MinimapGate:
    """
    Fast pre-filter for minimap crops (< 0.25 ms).
    - Rejects black, loading, white-flash, and flat screens (`gray.std() < 12.0`).
    - Verifies circular minimap border + interior structure via net directional radial gradient
      WITHOUT hardcoding top-center `"N"` (passes both Lock Direction and Follow View).
    - Computes keyframe annulus `cv2.absdiff` mean (`< 1.2` -> `stationary=True`).
    """

    _SZ = 64

    def __init__(
        self,
        *,
        min_std: float = 12.0,
        stationary_diff: float = 1.2,
        border_coherence: float = 0.42,
        require_circular_border: bool = True,
    ):
        self.min_std = float(min_std)
        self.stationary_diff = float(stationary_diff)
        self.border_coherence = float(border_coherence)
        self.require_circular_border = bool(require_circular_border)

        yy, xx = np.mgrid[0 : self._SZ, 0 : self._SZ]
        c = (self._SZ - 1) * 0.5
        rr = np.hypot(xx - c, yy - c)
        r_max = self._SZ * 0.5
        self._disk_mask = rr <= (0.85 * r_max)
        self._center_mask = rr <= (0.24 * r_max)
        self._annulus_mask = (rr > (0.20 * r_max)) & (rr <= (0.85 * r_max))
        # Precomputed CW-from-North angles (deg in [0, 360)) for each annulus pixel
        self._annulus_angles_deg = (
            np.degrees(np.arctan2(xx - c, -(yy - c))) % 360.0
        )[self._annulus_mask].astype(np.float32)

        # Precompute polar ring coordinates around r in [0.75*R, 0.99*R] (64 angles x 13 radii)
        thetas = np.linspace(0.0, 2.0 * math.pi, 64, endpoint=False)
        radii = np.linspace(0.75 * r_max, 0.99 * r_max, 13)
        self._ring_xs = np.clip(np.round(c + np.outer(np.cos(thetas), radii)).astype(np.intp), 0, self._SZ - 1)
        self._ring_ys = np.clip(np.round(c + np.outer(np.sin(thetas), radii)).astype(np.intp), 0, self._SZ - 1)

        self._keyframe_annulus: Optional[np.ndarray] = None
        self._keyframe_alpha: Optional[np.ndarray] = None
        self._keyframe_center: Optional[np.ndarray] = None
        self._keyframe_cam_deg: Optional[float] = None
        self._last_annulus: Optional[np.ndarray] = None

    def reset_keyframe(self) -> None:
        """Clears the stationary frame-diff reference keyframe."""
        self._keyframe_annulus = None
        self._keyframe_alpha = None
        self._keyframe_center = None
        self._keyframe_cam_deg = None
        self._last_annulus = None

    def commit_keyframe(self, minimap_img: Optional[np.ndarray], cam_deg: Optional[float] = None) -> None:
        """Records `minimap_img` as the reference keyframe for subsequent stationary checks."""
        if minimap_img is None or not isinstance(minimap_img, np.ndarray) or minimap_img.size == 0:
            self.reset_keyframe()
            return
        gray64, alpha64 = self._downscale_64(minimap_img)
        if gray64 is None:
            self.reset_keyframe()
            return
        self._keyframe_annulus = gray64[self._annulus_mask].copy()
        self._keyframe_center = gray64[self._center_mask].copy()
        self._keyframe_alpha = alpha64[self._annulus_mask].copy() if alpha64 is not None else None
        self._keyframe_cam_deg = float(cam_deg) % 360.0 if cam_deg is not None else None

    def is_terrain_stationary_excluding_cones(
        self,
        curr_cam_deg: Optional[float],
        *,
        half_fov_deg: float = 56.0,
        diff_thresh: Optional[float] = None,
    ) -> bool:
        """
        Checks whether the terrain outside both the previous and current camera view cones
        remained stationary (so standing still while rotating the camera stays locked with zero drift).
        """
        if self._keyframe_annulus is None or self._last_annulus is None:
            return False
        if self._keyframe_annulus.shape != self._last_annulus.shape:
            return False
        keep = np.ones(self._annulus_angles_deg.shape, dtype=bool)
        for ang in (curr_cam_deg, self._keyframe_cam_deg):
            if ang is None:
                continue
            delta = ((self._annulus_angles_deg - float(ang) + 180.0) % 360.0) - 180.0
            keep &= np.abs(delta) > half_fov_deg
        if int(np.sum(keep)) < max(64, int(0.22 * keep.size)):
            return False
        mean_diff = float(np.mean(cv2.absdiff(self._last_annulus[keep], self._keyframe_annulus[keep])))
        lim = float(diff_thresh) if diff_thresh is not None else max(1.6, self.stationary_diff * 1.4)
        return mean_diff < lim

    def commit_heading_keyframe(self, minimap_img: Optional[np.ndarray]) -> None:
        """Refreshes only center-arrow and alpha-cone keyframes without shifting terrain annulus keyframe."""
        if minimap_img is None or not isinstance(minimap_img, np.ndarray) or minimap_img.size == 0:
            return
        gray64, alpha64 = self._downscale_64(minimap_img)
        if gray64 is None:
            return
        self._keyframe_center = gray64[self._center_mask].copy()
        if alpha64 is not None:
            self._keyframe_alpha = alpha64[self._annulus_mask].copy()

    def _downscale_64(self, img: np.ndarray) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        # Resize first (4096 px vs ~47k px) so BGRA/BGR->Gray runs >20x faster with zero full-res copy
        if img.ndim == 2:
            small = cv2.resize(img, (self._SZ, self._SZ), interpolation=cv2.INTER_LINEAR)
            gray64 = small if small.dtype == np.uint8 else np.clip(small, 0, 255).astype(np.uint8)
            return gray64, None
        if img.ndim == 3 and img.shape[2] == 4:
            small = cv2.resize(img, (self._SZ, self._SZ), interpolation=cv2.INTER_LINEAR)
            if small.dtype != np.uint8:
                small = np.clip(small, 0, 255).astype(np.uint8)
            gray64 = cv2.cvtColor(small, cv2.COLOR_BGRA2GRAY)
            alpha64 = small[:, :, 3]
            return gray64, alpha64
        if img.ndim == 3 and img.shape[2] == 3:
            small = cv2.resize(img, (self._SZ, self._SZ), interpolation=cv2.INTER_LINEAR)
            if small.dtype != np.uint8:
                small = np.clip(small, 0, 255).astype(np.uint8)
            gray64 = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            return gray64, None
        return None, None

    def _circular_border_coherence(self, gray64: np.ndarray, inner_std: float) -> float:
        samples = gray64[self._ring_ys, self._ring_xs].astype(np.float32)  # (64, 13)
        dr = samples[:, 2:] - samples[:, :-2]  # (64, 11)
        thresh = max(10.0, 0.35 * inner_std)
        pos = np.mean(dr >= thresh, axis=0)
        neg = np.mean(dr <= -thresh, axis=0)
        net_dir = np.abs(pos - neg)
        return float(np.max(net_dir))

    def check(self, minimap_img: Optional[np.ndarray], *, update_keyframe: bool = False) -> MinimapGateResult:
        """Validates a minimap frame and computes fast annulus frame-diff stationarity."""
        if minimap_img is None or not isinstance(minimap_img, np.ndarray) or minimap_img.size == 0:
            self.reset_keyframe()
            return MinimapGateResult(
                valid=False,
                stationary=False,
                frame_diff=255.0,
                alpha_diff=255.0,
                center_diff=255.0,
                inner_std=0.0,
                border_coherence=0.0,
                hdr_clipped=False,
                reason="empty_frame",
            )

        h, w = minimap_img.shape[:2]
        if min(h, w) < 40 or not (0.75 <= (float(w) / float(h)) <= 1.33):
            self.reset_keyframe()
            return MinimapGateResult(
                valid=False,
                stationary=False,
                frame_diff=255.0,
                alpha_diff=255.0,
                center_diff=255.0,
                inner_std=0.0,
                border_coherence=0.0,
                hdr_clipped=False,
                reason="invalid_dimensions",
            )

        gray64, alpha64 = self._downscale_64(minimap_img)
        if gray64 is None:
            self.reset_keyframe()
            return MinimapGateResult(
                valid=False,
                stationary=False,
                frame_diff=255.0,
                alpha_diff=255.0,
                center_diff=255.0,
                inner_std=0.0,
                border_coherence=0.0,
                hdr_clipped=False,
                reason="invalid_channels",
            )

        hdr_info = detect_hdr_clipping(gray64)
        hdr_clipped = bool(hdr_info["clipped"])

        full_std = float(np.std(gray64))
        inner_vals = gray64[self._disk_mask]
        inner_std = float(np.std(inner_vals))
        inner_mean = float(np.mean(inner_vals))

        if full_std < self.min_std or inner_std < self.min_std or inner_mean < 6.0 or inner_mean > 249.0:
            self.reset_keyframe()
            return MinimapGateResult(
                valid=False,
                stationary=False,
                frame_diff=255.0,
                alpha_diff=255.0,
                center_diff=255.0,
                inner_std=inner_std,
                border_coherence=0.0,
                hdr_clipped=hdr_clipped,
                reason="blank_or_flash_frame",
            )

        coherence = self._circular_border_coherence(gray64, inner_std)
        if self.require_circular_border and coherence < self.border_coherence:
            self.reset_keyframe()
            return MinimapGateResult(
                valid=False,
                stationary=False,
                frame_diff=255.0,
                alpha_diff=255.0,
                center_diff=255.0,
                inner_std=inner_std,
                border_coherence=coherence,
                hdr_clipped=hdr_clipped,
                reason="no_circular_minimap_border",
            )

        curr_annulus = gray64[self._annulus_mask]
        self._last_annulus = curr_annulus
        if self._keyframe_annulus is not None and self._keyframe_annulus.shape == curr_annulus.shape:
            frame_diff = float(np.mean(cv2.absdiff(curr_annulus, self._keyframe_annulus)))
        else:
            frame_diff = 255.0

        curr_center = gray64[self._center_mask]
        if self._keyframe_center is not None and self._keyframe_center.shape == curr_center.shape:
            center_diff = float(np.mean(cv2.absdiff(curr_center, self._keyframe_center)))
        else:
            center_diff = 255.0

        alpha_diff = 0.0
        if alpha64 is not None:
            curr_alpha = alpha64[self._annulus_mask]
            if self._keyframe_alpha is not None and self._keyframe_alpha.shape == curr_alpha.shape:
                alpha_diff = float(np.mean(cv2.absdiff(curr_alpha, self._keyframe_alpha)))
            else:
                alpha_diff = 255.0
        elif self._keyframe_alpha is not None:
            alpha_diff = 255.0

        stationary = bool(frame_diff < self.stationary_diff)
        if update_keyframe and not stationary:
            self._keyframe_annulus = curr_annulus.copy()
            self._keyframe_center = curr_center.copy()
            self._keyframe_alpha = alpha64[self._annulus_mask].copy() if alpha64 is not None else None

        return MinimapGateResult(
            valid=True,
            stationary=stationary,
            frame_diff=frame_diff,
            alpha_diff=alpha_diff,
            center_diff=center_diff,
            inner_std=inner_std,
            border_coherence=coherence,
            hdr_clipped=hdr_clipped,
            reason=None,
        )


# ---------------------------------------------------------------------------
# 4-State FSM (Ported from MaxWay12/Genshin-Navigator@99e1e13:tracker.py MIT)
# ---------------------------------------------------------------------------
class TrackerFSM:
    """
    4-state localization stabilizer (LOST -> ACQUIRING(2 hits) -> TRACKING(EMA alpha=0.4
    + velocity gate) -> RELOCATING(3 hits) -> LOST(timeout=1.5s)) with `pause()` timestamp freeze.

    Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/tracker.py (MIT License).
    """

    def __init__(self, config: Optional[MinimapTrackerConfig] = None):
        self.config = config or MinimapTrackerConfig()
        self.state: str = STATE_LOST
        self._map_id: Optional[int] = None
        self._x: Optional[float] = None
        self._y: Optional[float] = None
        self._is_underground: bool = False
        self._floor_id: Optional[int] = None
        self._floor_name: str = "G"
        self._confidence: float = 0.0
        self._last_timestamp: Optional[float] = None
        self._last_accepted_timestamp: Optional[float] = None
        self._last_absolute_fix_timestamp: Optional[float] = None

        self._candidate_x: Optional[float] = None
        self._candidate_y: Optional[float] = None
        self._candidate_map_id: Optional[int] = None
        self._candidate_is_underground: bool = False
        self._candidate_floor_id: Optional[int] = None
        self._candidate_floor_name: str = "G"
        self._candidate_confidence: float = 0.0
        self._candidate_match_method: str = "none"
        self._candidate_hits: int = 0

    @staticmethod
    def _distance(ax: float, ay: float, bx: float, by: float) -> float:
        return math.hypot(ax - bx, ay - by)

    def _clear_candidate(self) -> None:
        self._candidate_x = None
        self._candidate_y = None
        self._candidate_map_id = None
        self._candidate_is_underground = False
        self._candidate_floor_id = None
        self._candidate_floor_name = "G"
        self._candidate_confidence = 0.0
        self._candidate_match_method = "none"
        self._candidate_hits = 0

    def _valid(self, m: PoseMeasurement) -> bool:
        if not (m.found and m.map_id is not None and m.world_x is not None and m.world_y is not None):
            return False
        if m.match_method == "motion":
            return m.confidence >= 0.30
        return m.confidence >= self.config.min_confidence

    def _set_candidate(self, m: PoseMeasurement) -> None:
        assert m.world_x is not None and m.world_y is not None and m.map_id is not None
        self._candidate_x = float(m.world_x)
        self._candidate_y = float(m.world_y)
        self._candidate_map_id = int(m.map_id)
        self._candidate_is_underground = bool(m.is_underground)
        self._candidate_floor_id = int(m.floor_id) if m.floor_id is not None else None
        self._candidate_floor_name = str(m.floor_name or ("G" if not m.is_underground else "B1"))
        self._candidate_confidence = float(m.confidence)
        self._candidate_match_method = str(m.match_method)
        self._candidate_hits = 1

    def _update_candidate(self, m: PoseMeasurement) -> bool:
        assert m.world_x is not None and m.world_y is not None and m.map_id is not None
        x, y = float(m.world_x), float(m.world_y)
        if self._candidate_x is None or self._candidate_y is None:
            self._set_candidate(m)
            return False
        if (
            int(m.map_id) != self._candidate_map_id
            or bool(m.is_underground) != self._candidate_is_underground
            or (int(m.floor_id) if m.floor_id is not None else None) != self._candidate_floor_id
            or self._distance(x, y, self._candidate_x, self._candidate_y) > self.config.candidate_radius_world
        ):
            self._set_candidate(m)
            return False
        count = self._candidate_hits + 1
        self._candidate_x += (x - self._candidate_x) / count
        self._candidate_y += (y - self._candidate_y) / count
        self._candidate_confidence = float(m.confidence)
        self._candidate_match_method = str(m.match_method)
        self._candidate_hits = count
        return True

    def _accept_candidate(self, timestamp: float) -> None:
        assert self._candidate_x is not None and self._candidate_y is not None
        self._x = self._candidate_x
        self._y = self._candidate_y
        self._map_id = self._candidate_map_id
        self._is_underground = self._candidate_is_underground
        self._floor_id = self._candidate_floor_id
        self._floor_name = self._candidate_floor_name
        self._confidence = self._candidate_confidence
        self._last_accepted_timestamp = timestamp
        if self._candidate_match_method != "motion":
            self._last_absolute_fix_timestamp = timestamp
        self._clear_candidate()
        self.state = STATE_TRACKING

    def set_anchor(
        self,
        map_id: int,
        world_x: float,
        world_y: float,
        timestamp: float,
        *,
        is_underground: bool = False,
        floor_id: Optional[int] = None,
        floor_name: str = "G",
    ) -> None:
        """Immediately locks FSM to TRACKING at the exact Map M anchor (zero search latency)."""
        ts = float(timestamp)
        if self._last_timestamp is not None and ts < self._last_timestamp:
            raise ValueError("Tracker timestamps must be monotonic")
        self._last_timestamp = ts
        self._last_accepted_timestamp = ts
        self._last_absolute_fix_timestamp = ts
        self._map_id = int(map_id)
        self._x = float(world_x)
        self._y = float(world_y)
        self._is_underground = bool(is_underground)
        self._floor_id = int(floor_id) if floor_id is not None else None
        self._floor_name = str(floor_name or ("G" if not is_underground else "B1"))
        self._confidence = 1.0
        self._clear_candidate()
        self.state = STATE_TRACKING

    def set_floor(self, is_underground: bool, floor_id: Optional[int], floor_name: str) -> None:
        """Updates confirmed floor layer after multi-layer floor comparison hysteresis passes."""
        self._is_underground = bool(is_underground)
        self._floor_id = int(floor_id) if floor_id is not None else None
        self._floor_name = str(floor_name or ("G" if not is_underground else "B1"))

    def force_lost(self, timestamp: float) -> None:
        """Immediately fails safe to LOST (e.g., when phaseCorrelate 5-frame budget is exhausted)."""
        ts = float(timestamp)
        if self._last_timestamp is not None and ts < self._last_timestamp:
            raise ValueError("Tracker timestamps must be monotonic")
        self._last_timestamp = ts
        self._last_accepted_timestamp = None
        self.state = STATE_LOST
        self._x = None
        self._y = None
        self._confidence = 0.0
        self._clear_candidate()

    def touch_stationary(self, timestamp: float) -> None:
        """Refreshes accepted timestamp on valid stationary frames without modifying position."""
        ts = float(timestamp)
        if self._last_timestamp is not None and ts < self._last_timestamp:
            raise ValueError("Tracker timestamps must be monotonic")
        self._last_timestamp = ts
        if self.state == STATE_TRACKING and self._x is not None and self._y is not None:
            self._last_accepted_timestamp = ts

    def pause(self, timestamp: float) -> None:
        """Freezes `_last_accepted_timestamp` while Map M / menu is open so closing never spuriously drops to LOST."""
        ts = float(timestamp)
        if self._last_timestamp is not None and ts < self._last_timestamp:
            raise ValueError("Tracker timestamps must be monotonic")
        if self._last_timestamp is not None and self._last_accepted_timestamp is not None:
            self._last_accepted_timestamp += ts - self._last_timestamp
        self._last_timestamp = ts

    def update(self, m: PoseMeasurement, timestamp: float) -> Tuple[bool, bool, Optional[str]]:
        """
        Ingests one localization measurement at monotonic `timestamp`.
        Returns `(accepted, stale, reason)`.
        """
        ts = float(timestamp)
        if self._last_timestamp is not None and ts < self._last_timestamp:
            raise ValueError("Tracker timestamps must be monotonic")
        delta = 0.0 if self._last_timestamp is None else (ts - self._last_timestamp)
        self._last_timestamp = ts

        if not self._valid(m):
            expired = bool(
                self._last_accepted_timestamp is None
                or (ts - self._last_accepted_timestamp) > self.config.lost_timeout_seconds
            )
            if expired:
                self.state = STATE_LOST
                self._x = None
                self._y = None
                self._confidence = 0.0
                self._clear_candidate()
            elif self.state in (STATE_ACQUIRING, STATE_RELOCATING):
                self.state = STATE_TRACKING
            return False, (not expired), (m.reason or "localization_below_threshold")

        assert m.world_x is not None and m.world_y is not None and m.map_id is not None
        x, y = float(m.world_x), float(m.world_y)

        if self.state == STATE_LOST:
            self._set_candidate(m)
            if self.config.acquire_hits <= 1:
                self._accept_candidate(ts)
                return True, False, None
            self.state = STATE_ACQUIRING
            return False, False, "awaiting_confirmation"

        if self.state == STATE_ACQUIRING:
            self._update_candidate(m)
            if self._candidate_hits >= self.config.acquire_hits:
                self._accept_candidate(ts)
                return True, False, None
            return False, False, "awaiting_confirmation"

        if self._x is None or self._y is None:
            self._set_candidate(m)
            self.state = STATE_ACQUIRING
            return False, False, "awaiting_confirmation"

        allowed_distance = self.config.max_speed_world_per_second * delta + self.config.jump_margin_world
        distance_from_track = self._distance(x, y, self._x, self._y)
        m_floor = int(m.floor_id) if m.floor_id is not None else None
        same_layer = bool(
            int(m.map_id) == self._map_id
            and bool(m.is_underground) == self._is_underground
            and m_floor == self._floor_id
        )

        if same_layer and distance_from_track <= allowed_distance:
            alpha = self.config.smoothing_alpha
            self._x += alpha * (x - self._x)
            self._y += alpha * (y - self._y)
            self._last_accepted_timestamp = ts
            self._clear_candidate()
            self.state = STATE_TRACKING
            self._confidence = float(m.confidence)
            if m.match_method != "motion":
                self._last_absolute_fix_timestamp = ts
            return True, False, None

        # Outside velocity gate or layer changed -> RELOCATING (requires relocate_hits consistent confirmations)
        if self.state != STATE_RELOCATING:
            self._set_candidate(m)
            self.state = STATE_RELOCATING
        else:
            self._update_candidate(m)

        if self._candidate_hits >= self.config.relocate_hits:
            self._accept_candidate(ts)
            return True, False, "relocation_confirmed"
        return False, True, "possible_relocation"


# ---------------------------------------------------------------------------
# HybridMinimapTracker
# ---------------------------------------------------------------------------
class HybridMinimapTracker:
    """
    Hybrid minimap local tracker combining:
    - Map M zero-latency anchoring (`set_map_m_anchor`) + `pause()` timestamp freeze
    - `MinimapGate` (blank/flash rejection, circular border check, fast stationary frame-diff skip)
    - 360° camera view cone & player arrow extraction on every valid frame
    - Annulus `TM_CCOEFF_NORMED` (0.5x downscale + 1D parabolic sub-pixel refinement) across
      2 zoom regimes (`open_world` vs `city ~3x`) with 2-win anti-jitter hysteresis
    - Follow-View rotation recovery via ORB + `estimateAffinePartial2D` (`view_heading = wrap(-theta_map)`)
    - Bounded `cv2.phaseCorrelate` sparse-terrain fallback (max 5 consecutive frames -> fail safe to LOST)
    - Multi-layer surface vs underground floor comparison (`compare_surface_vs_underground`, 3-win hysteresis)
    """

    def __init__(
        self,
        tile_cache: Optional[LruTerrainTileCache] = None,
        config: Optional[MinimapTrackerConfig] = None,
        gate: Optional[MinimapGate] = None,
        *,
        global_locator: Optional[GlobalLocatorFn] = None,
        ug_provider: Optional[Any] = None,
        global_relocator: Optional[Any] = None,
        paimon_tpl: Optional[np.ndarray] = None,
        paimon_mask: Optional[np.ndarray] = None,
        paimon_cache_path: Optional[Path] = None,
    ):
        self.config = config or MinimapTrackerConfig()
        self.tile_cache = tile_cache if tile_cache is not None else LruTerrainTileCache(max_tiles=16)
        self.gate = gate if gate is not None else MinimapGate(
            min_std=self.config.gate_min_std,
            stationary_diff=self.config.gate_stationary_diff,
            border_coherence=self.config.gate_border_coherence,
        )
        self.global_locator = global_locator
        self.ug_provider = ug_provider
        self.global_relocator = global_relocator
        self.fsm = TrackerFSM(self.config)

        # Heading state & smoothers
        self._camera_heading: Optional[float] = None
        self._body_heading: Optional[float] = None
        self._view_heading: Optional[float] = None
        self._camera_conf: float = 0.0
        self._body_conf: float = 0.0
        self._body_smoother = CircularAngleSmoother(alpha=0.75, deadband_deg=0.6)
        self._camera_smoother = CircularAngleSmoother(alpha=0.82, deadband_deg=0.5)
        # Temporal gate: rejects false tier-3 cone guesses / single-frame flips, body fallback
        self._camera_fuser = CameraHeadingFuser()
        self._camera_method: str = "none"
        # Where the cone is drawn in the CURRENT frame (raw detector output): used only to mask
        # the cone out of stationary / template diffs. Navigation uses the gated `_camera_heading`.
        self._cone_raw_heading: Optional[float] = None

        # Minimap mode & Follow-View rotation state
        self._minimap_mode: str = MODE_LOCK_DIRECTION
        self._map_rotation_deg: float = 0.0

        # 2-Zoom regime & anti-jitter hysteresis + continuous observed span
        self._zoom_regime: str = ZOOM_OPEN_WORLD
        self._zoom_alt_streak: int = 0
        self._confirmed_span_world: float = self.config.open_world_span_world
        self._span_confirmed: bool = False
        self._last_zoom_tier: int = -1

        # Multi-layer floor comparison hysteresis (leaky-bucket score + legacy compare)
        self._floor_switch_streak: int = 0
        self._floor_switch_candidate_id: Optional[int] = None
        self._layer_probe_tick: int = 0
        self._pending_layer_key: Optional[Tuple[bool, Optional[int], str]] = None
        self._pending_layer_score: int = 0

        # Post-pause / loading relocation & hybrid SIFT/Sobel inter-frame state
        self._pause_start_ts: Optional[float] = None
        self._long_pause_pending: bool = False
        self._just_resumed_from_pause: bool = False
        self._resume_frames_left: int = 0
        self._relocate_tier2_left: int = 0
        self._consecutive_sift_misses: int = 0
        self._sift_interframe_budget: int = 0
        self._global_vote_bins: Dict[Tuple[int, int], List[Tuple[float, float, float]]] = {}
        self._global_query_count: int = 0
        self._last_sift_inliers: int = 0

        # Paimon pause gate + runtime HUD offset auto-calibration
        self._paimon_cache_path: Optional[Path] = (
            Path(paimon_cache_path) if paimon_cache_path is not None else None
        )
        self._has_paimon_tpl: bool = False
        self.paimon_tpl: Optional[np.ndarray] = None
        self.paimon_mask: Optional[np.ndarray] = None
        self._paimon_mask_bool: Optional[np.ndarray] = None
        self._paimon_t_zm: Optional[np.ndarray] = None
        self._paimon_t_norm: float = 1.0
        self._paimon_tpl_half: Optional[np.ndarray] = None
        self._paimon_mask_half: Optional[np.ndarray] = None
        self._paimon_last_xy_1080: Optional[Tuple[int, int]] = None
        self._paimon_check_tick: int = 0
        self._paimon_bootstrap_streak: int = 0
        self._layout_cache: Dict[Tuple[int, int, bool], Any] = {}
        self._hud_frame_size: Optional[Tuple[int, int, bool]] = None
        self._cal_offset_samples: List[Tuple[int, int]] = []
        self._cal_dx_px: int = 0
        self._cal_dy_px: int = 0
        self._cal_locked: bool = False

        if paimon_tpl is not None and paimon_mask is not None:
            self._init_paimon_matcher(paimon_tpl, paimon_mask)
        else:
            self._try_load_cached_paimon()

        # Bounded phaseCorrelate sparse-terrain fallback
        self._phase_size: int = 96
        self._hann_window: np.ndarray = cv2.createHanningWindow((self._phase_size, self._phase_size), cv2.CV_32F)
        self._prev_phase_gray: Optional[np.ndarray] = None
        self._prev_mm_gray_for_phase: Optional[np.ndarray] = None
        self._prev_cone_excl_for_phase: Optional[float] = None
        self._motion_fallback_frames: int = 0

        # Cached masks, SIFT, ORB & HD cell LRU cache
        self._annulus_masks: Dict[int, np.ndarray] = {}
        self._sift_mm = cv2.SIFT_create(nfeatures=300, contrastThreshold=0.012, edgeThreshold=15)
        self._sift_ref = cv2.SIFT_create(nfeatures=950, contrastThreshold=0.011, edgeThreshold=15)
        self._bf_sift = cv2.BFMatcher(cv2.NORM_L2)
        self._cell_cache: "OrderedDict[Tuple[Any, ...], Dict[str, Any]]" = OrderedDict()
        self._max_cells: int = 18
        self._orb = cv2.ORB_create(
            nfeatures=800,
            scaleFactor=1.15,
            nlevels=6,
            edgeThreshold=11,
            patchSize=15,
            fastThreshold=7,
        )
        self._bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

        # Telemetry counters (useful for diagnostics & verification)
        self.matcher_calls: int = 0
        self.stationary_skips: int = 0
        self.last_match_ms: float = 0.0
        self._last_hdr_clipped: bool = False
        self._last_timestamp: float = 0.0

    @property
    def state(self) -> str:
        return self.fsm.state

    @property
    def zoom_regime(self) -> str:
        return self._zoom_regime

    @property
    def minimap_mode(self) -> str:
        return self._minimap_mode

    @property
    def calibrated_offset_px(self) -> Tuple[int, int]:
        return (self._cal_dx_px, self._cal_dy_px)

    def _init_paimon_matcher(self, tpl_gray: np.ndarray, tpl_mask: np.ndarray) -> None:
        g = tpl_gray if tpl_gray.ndim == 2 else cv2.cvtColor(tpl_gray[:, :, :3], cv2.COLOR_BGR2GRAY)
        m = (tpl_mask > 0).astype(np.uint8) * np.uint8(255)
        self.paimon_tpl = np.ascontiguousarray(g, dtype=np.uint8)
        self.paimon_mask = np.ascontiguousarray(m, dtype=np.uint8)
        self._paimon_mask_bool = self.paimon_mask > 0
        if not np.any(self._paimon_mask_bool):
            self._has_paimon_tpl = False
            return
        t_vals = self.paimon_tpl[self._paimon_mask_bool].astype(np.float32)
        self._paimon_t_zm = t_vals - float(np.mean(t_vals))
        self._paimon_t_norm = float(np.linalg.norm(self._paimon_t_zm)) + 1e-6
        ht, wt = self.paimon_tpl.shape[:2]
        hw_h, hh_h = max(12, wt // 2), max(12, ht // 2)
        self._paimon_tpl_half = cv2.resize(self.paimon_tpl, (hw_h, hh_h), interpolation=cv2.INTER_AREA)
        self._paimon_mask_half = (
            cv2.resize(self.paimon_mask, (hw_h, hh_h), interpolation=cv2.INTER_NEAREST) > 0
        ).astype(np.uint8) * np.uint8(255)
        self._has_paimon_tpl = True

    def _try_load_cached_paimon(self) -> None:
        candidates: List[Path] = []
        if self._paimon_cache_path is not None:
            candidates.append(self._paimon_cache_path)
        bundled_paimon = Path(get_asset_path("src", "data", "paimon_tpl.npz"))
        candidates.append(bundled_paimon)
        for p in candidates:
            if not p.is_file():
                continue
            try:
                with np.load(p) as zf:
                    if "tpl_gray" in zf.files and "mask" in zf.files:
                        self._init_paimon_matcher(np.array(zf["tpl_gray"], copy=True), np.array(zf["mask"], copy=True))
                        if self._has_paimon_tpl:
                            return
            except Exception as exc:
                logger.debug(f"Failed loading Paimon template from {p}: {exc}")

    def _try_bootstrap_paimon(self, hud_tl_bgr: np.ndarray, lay: Any) -> None:
        if self._has_paimon_tpl or self._paimon_cache_path is None:
            return
        px, py, pw, ph = lay.paimon_rect
        x1 = min(hud_tl_bgr.shape[1], px + pw)
        y1 = min(hud_tl_bgr.shape[0], py + ph)
        if x1 - px < 16 or y1 - py < 16:
            return
        crop = hud_tl_bgr[py:y1, px:x1]
        gray = crop if crop.ndim == 2 else cv2.cvtColor(crop[:, :, :3], cv2.COLOR_BGR2GRAY)
        if float(np.std(gray)) < 12.0:
            return
        tpl_gray = (
            gray
            if gray.shape == (77, 68)
            else cv2.resize(gray, (68, 77), interpolation=cv2.INTER_AREA if gray.shape[1] > 68 else cv2.INTER_LINEAR)
        )
        yy, xx = np.mgrid[0:77, 0:68]
        ell = (((xx - 33.5) / 30.0) ** 2 + ((yy - 38.0) / 35.0) ** 2) <= 1.0
        tpl_mask = np.where(ell, np.uint8(255), np.uint8(0))
        try:
            self._paimon_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                suffix=".npz",
                prefix=".paimon-",
                dir=str(self._paimon_cache_path.parent),
                delete=False,
            ) as tf:
                tmp_name = tf.name
            np.savez_compressed(tmp_name, tpl_gray=tpl_gray, mask=tpl_mask)
            os.replace(tmp_name, self._paimon_cache_path)
        except Exception as exc:
            logger.debug(f"Paimon template cache write skipped: {exc}")
        self._init_paimon_matcher(tpl_gray, tpl_mask)

    def _check_paimon(self, paimon_maybe_img: Optional[np.ndarray], ui_scale: float) -> float:
        """1-point locked zero-mean masked NCC (~0.04ms) + 0.5x half-res scan + +-4px 1080p refine."""
        if (
            not self._has_paimon_tpl
            or self.paimon_tpl is None
            or self.paimon_mask is None
            or self._paimon_mask_bool is None
            or self._paimon_t_zm is None
            or self._paimon_tpl_half is None
            or self._paimon_mask_half is None
            or paimon_maybe_img is None
            or paimon_maybe_img.size == 0
        ):
            return 1.0
        mh, mw = paimon_maybe_img.shape[:2]
        th, tw = self.paimon_tpl.shape[:2]
        sc = max(1e-6, float(ui_scale))
        inv_s = 1.0 / sc
        rw = max(tw, int(round(mw * inv_s)))
        rh = max(th, int(round(mh * inv_s)))

        if self._paimon_last_xy_1080 is not None:
            lx, ly = self._paimon_last_xy_1080
            if 0 <= lx and lx + tw <= rw and 0 <= ly and ly + th <= rh:
                px0 = max(0, min(mw - 1, int(round(lx * sc))))
                py0 = max(0, min(mh - 1, int(round(ly * sc))))
                px1 = min(mw, max(px0 + 1, int(round((lx + tw) * sc))))
                py1 = min(mh, max(py0 + 1, int(round((ly + th) * sc))))
                sub = paimon_maybe_img[py0:py1, px0:px1]
                g_sub = sub if sub.ndim == 2 else cv2.cvtColor(sub[:, :, :3], cv2.COLOR_BGR2GRAY)
                if g_sub.shape != (th, tw):
                    interp = cv2.INTER_AREA if g_sub.shape[1] > tw else cv2.INTER_LINEAR
                    g_sub = cv2.resize(g_sub, (tw, th), interpolation=interp)
                g_vals = g_sub[self._paimon_mask_bool].astype(np.float32)
                g_zm = g_vals - float(np.mean(g_vals))
                g_norm = float(np.linalg.norm(g_zm)) + 1e-6
                ncc = float(np.dot(g_zm, self._paimon_t_zm) / (g_norm * self._paimon_t_norm))
                if math.isfinite(ncc) and ncc >= self.config.paimon_thresh:
                    self._paimon_check_tick += 1
                    return ncc

        self._paimon_check_tick += 1
        ht_h, wt_h = self._paimon_tpl_half.shape[:2]
        rw_h = max(wt_h, rw // 2)
        rh_h = max(ht_h, rh // 2)
        scaled_h = cv2.resize(paimon_maybe_img, (rw_h, rh_h), interpolation=cv2.INTER_AREA)
        gray_h = scaled_h if scaled_h.ndim == 2 else cv2.cvtColor(scaled_h[:, :, :3], cv2.COLOR_BGR2GRAY)
        res_h = cv2.matchTemplate(
            gray_h, self._paimon_tpl_half, cv2.TM_CCOEFF_NORMED, mask=self._paimon_mask_half
        )
        if not np.isfinite(res_h).all():
            res_h = np.nan_to_num(res_h, nan=0.0, posinf=0.0, neginf=0.0)
        _, mv_h, _, ml_h = cv2.minMaxLoc(res_h)
        if mv_h < self.config.paimon_thresh - 0.12:
            return float(max(0.0, mv_h))

        cx_1080 = int(ml_h[0] * 2)
        cy_1080 = int(ml_h[1] * 2)
        x0 = max(0, cx_1080 - 4)
        y0 = max(0, cy_1080 - 4)
        x1 = min(rw, x0 + tw + 8)
        y1 = min(rh, y0 + th + 8)
        tw_1080 = x1 - x0
        th_1080 = y1 - y0
        if tw_1080 >= tw and th_1080 >= th:
            px0 = max(0, min(mw - 1, int(round(x0 * sc))))
            py0 = max(0, min(mh - 1, int(round(y0 * sc))))
            px1 = min(mw, max(px0 + 1, int(round(x1 * sc))))
            py1 = min(mh, max(py0 + 1, int(round(y1 * sc))))
            sub = paimon_maybe_img[py0:py1, px0:px1]
            if sub.shape[1] != tw_1080 or sub.shape[0] != th_1080:
                interp = cv2.INTER_AREA if sub.shape[1] > tw_1080 else cv2.INTER_LINEAR
                sub = cv2.resize(sub, (tw_1080, th_1080), interpolation=interp)
            gray_sub = sub if sub.ndim == 2 else cv2.cvtColor(sub[:, :, :3], cv2.COLOR_BGR2GRAY)
            res = cv2.matchTemplate(gray_sub, self.paimon_tpl, cv2.TM_CCOEFF_NORMED, mask=self.paimon_mask)
            if not np.isfinite(res).all():
                res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv >= self.config.paimon_thresh:
                self._paimon_last_xy_1080 = (x0 + ml[0], y0 + ml[1])
            return float(mv)

        return float(max(0.0, mv_h))

    def _get_layout(self, frame_w: int, frame_h: int, gamepad_mode: bool = False) -> Any:
        key = (int(frame_w), int(frame_h), bool(gamepad_mode))
        lay = self._layout_cache.get(key)
        if lay is None:
            lay = compute_ui_layout(key[0], key[1], gamepad_mode=key[2])
            self._layout_cache[key] = lay
        return lay

    def _resolve_timestamp(self, now: Optional[float]) -> float:
        if now is not None:
            return float(now)
        ts = time.perf_counter()
        if self.fsm._last_timestamp is not None and ts < self.fsm._last_timestamp:
            return self.fsm._last_timestamp
        return ts

    def _get_annulus_mask(self, size: int) -> np.ndarray:
        sz = int(size)
        mask = self._annulus_masks.get(sz)
        if mask is not None:
            return mask
        yy, xx = np.mgrid[0:sz, 0:sz]
        c = (sz - 1) * 0.5
        rr = np.hypot(xx - c, yy - c)
        r_max = sz * 0.5
        inner_r = self.config.annulus_inner_ratio * r_max
        outer_r = self.config.annulus_outer_ratio * r_max
        mask = np.where((rr > inner_r) & (rr <= outer_r), np.uint8(255), np.uint8(0))
        self._annulus_masks[sz] = mask
        return mask

    def _span_for_regime(self, regime: str) -> float:
        return self.config.city_span_world if regime == ZOOM_CITY else self.config.open_world_span_world

    def _patch_params_for_regime(self, regime: str) -> Tuple[float, float, float]:
        """Returns `(span_world, search_radius_world, target_px_per_unit)`."""
        if regime == ZOOM_CITY:
            return (
                self.config.city_span_world,
                self.config.search_radius_city,
                self.config.city_patch_px_per_unit,
            )
        return (
            self.config.open_world_span_world,
            self.config.search_radius_open_world,
            self.config.open_world_patch_px_per_unit,
        )

    def _build_pose(
        self,
        timestamp: float,
        *,
        stationary: bool = False,
        match_method: str = "none",
        reason: Optional[str] = None,
        should_broadcast_web: bool = False,
        paimon_score: float = 1.0,
        sift_inliers: int = 0,
    ) -> MinimapPose:
        wx = self.fsm._x
        wy = self.fsm._y
        return MinimapPose(
            state=self.fsm.state,
            map_id=self.fsm._map_id,
            world_x=wx,
            world_y=wy,
            lat=wy,  # Genshin-Helper HoYoLAB CRS: lat == world_y
            lng=wx,  # Genshin-Helper HoYoLAB CRS: lng == world_x
            camera_heading=self._camera_heading,
            body_heading=self._body_heading,
            view_heading=self._view_heading,
            minimap_mode=self._minimap_mode,
            zoom_regime=self._zoom_regime,
            is_underground=self.fsm._is_underground,
            floor_id=self.fsm._floor_id,
            confidence=self.fsm._confidence,
            timestamp=float(timestamp),
            hdr_clipped=self._last_hdr_clipped,
            floor_name=self.fsm._floor_name,
            stationary=bool(stationary),
            match_method=str(match_method),
            map_rotation_deg=float(self._map_rotation_deg),
            reason=reason,
            should_broadcast_web=bool(should_broadcast_web),
            span_world=float(self._confirmed_span_world),
            paimon_score=float(paimon_score),
            sift_inliers=int(sift_inliers),
            zoom_tier=int(self._last_zoom_tier),
            cal_dx_px=int(self._cal_dx_px),
            cal_dy_px=int(self._cal_dy_px),
        )

    def set_map_m_anchor(
        self,
        map_id: int,
        world_x: float,
        world_y: float,
        is_underground: bool = False,
        floor_id: Optional[int] = None,
        floor_name: str = "G",
        timestamp: Optional[float] = None,
    ) -> MinimapPose:
        """Immediately locks tracker to exact Map M anchor (`TRACKING` state, zero search latency)."""
        ts = self._resolve_timestamp(timestamp)
        self._last_timestamp = ts
        self.fsm.set_anchor(
            map_id=int(map_id),
            world_x=float(world_x),
            world_y=float(world_y),
            timestamp=ts,
            is_underground=bool(is_underground),
            floor_id=floor_id,
            floor_name=floor_name,
        )
        self._motion_fallback_frames = 0
        self._prev_phase_gray = None
        self._prev_mm_gray_for_phase = None
        self._prev_cone_excl_for_phase = None
        self._zoom_regime = ZOOM_OPEN_WORLD
        self._zoom_alt_streak = 0
        self._confirmed_span_world = self.config.open_world_span_world
        self._span_confirmed = False
        self._last_zoom_tier = -1
        self._floor_switch_streak = 0
        self._floor_switch_candidate_id = None
        self._pending_layer_key = None
        self._pending_layer_score = 0
        self._pause_start_ts = None
        self._long_pause_pending = False
        self._just_resumed_from_pause = False
        self._resume_frames_left = 0
        self._relocate_tier2_left = 0
        self._consecutive_sift_misses = 0
        self._sift_interframe_budget = 0
        self._global_vote_bins.clear()
        self._global_query_count = 0
        self._last_sift_inliers = 0
        self.gate.reset_keyframe()
        return self._build_pose(
            ts,
            stationary=False,
            match_method="map_m_anchor",
            reason=None,
            should_broadcast_web=True,
        )

    def pause(
        self,
        now: Optional[float] = None,
        reason: str = "paused",
        paimon_score: float = 0.0,
    ) -> MinimapPose:
        """Freezes `_last_accepted_timestamp` while Map M / menu is open to prevent spurious LOST timeout."""
        ts = self._resolve_timestamp(now)
        self._last_timestamp = ts
        self.fsm.pause(ts)
        if self._pause_start_ts is None:
            self._pause_start_ts = ts
            self._global_vote_bins.clear()
            self._global_query_count = 0
        if (ts - self._pause_start_ts) >= 1.2:
            self._long_pause_pending = True
            self._relocate_tier2_left = 2
        self._just_resumed_from_pause = True
        self._resume_frames_left = 8
        self._sift_interframe_budget = 0
        self._prev_phase_gray = None
        self._prev_mm_gray_for_phase = None
        self.gate.reset_keyframe()
        method = "paimon_pause" if "paimon" in reason else "paused"
        return self._build_pose(
            ts,
            stationary=False,
            match_method=method,
            reason=reason,
            should_broadcast_web=False,
            paimon_score=paimon_score,
        )

    def on_loading_screen(self, now: Optional[float] = None) -> MinimapPose:
        """Handles a full-screen loading transition safely without dropping to LOST or corrupting web map."""
        ts = self._resolve_timestamp(now)
        self._last_timestamp = ts
        if self._pause_start_ts is None:
            self._pause_start_ts = ts
        self._long_pause_pending = True
        self._relocate_tier2_left = 2
        self._just_resumed_from_pause = True
        self._resume_frames_left = 10
        self._sift_interframe_budget = 0
        self._pending_layer_key = None
        self._pending_layer_score = 0
        self._global_vote_bins.clear()
        self._global_query_count = 0
        self._prev_phase_gray = None
        self._prev_mm_gray_for_phase = None
        self.gate.reset_keyframe()
        self.fsm.pause(ts)
        return self._build_pose(
            ts,
            stationary=False,
            match_method="loading_pause",
            reason="loading_screen",
            should_broadcast_web=False,
            paimon_score=0.0,
        )

    def ingest_measurement(
        self,
        measurement: PoseMeasurement,
        now: Optional[float] = None,
    ) -> MinimapPose:
        """Feeds an external or synthetic `PoseMeasurement` directly into the 4-state FSM."""
        ts = self._resolve_timestamp(now)
        self._last_timestamp = ts
        acc, _, reason = self.fsm.update(measurement, ts)
        if measurement.found and measurement.match_method != "motion":
            self._motion_fallback_frames = 0
        return self._build_pose(
            ts,
            stationary=False,
            match_method=measurement.match_method,
            reason=reason,
            should_broadcast_web=bool(acc),
        )

    @staticmethod
    def _parabolic_peak_offset(v_prev: float, v_curr: float, v_next: float) -> float:
        denom = v_prev - 2.0 * v_curr + v_next
        if abs(denom) > 1e-6:
            return float(np.clip(0.5 * (v_prev - v_next) / denom, -0.5, 0.5))
        return 0.0

    def _get_cone_masked_annulus(
        self,
        size: int,
        exclude_cone_cw_deg: Optional[float],
        half_fov_deg: float = 52.0,
    ) -> np.ndarray:
        """Returns annulus mask with the translucent 90 deg camera view cone zeroed out."""
        base = self._get_annulus_mask(size)
        if exclude_cone_cw_deg is None or not math.isfinite(float(exclude_cone_cw_deg)):
            return base
        sz = int(size)
        mask = base.copy()
        c = sz // 2
        cv_ang = wrap_angle_360(exclude_cone_cw_deg) - 90.0
        cv2.ellipse(
            mask,
            (c, c),
            (sz, sz),
            0.0,
            cv_ang - half_fov_deg,
            cv_ang + half_fov_deg,
            0,
            -1,
        )
        return mask

    def _match_annulus_template(
        self,
        minimap_gray: np.ndarray,
        map_id: int,
        anchor_x: float,
        anchor_y: float,
        regime: str,
        *,
        pre_rotate_cw_deg: float = 0.0,
        exclude_cone_cw_deg: Optional[float] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """
        Annulus `TM_CCOEFF_NORMED` local match (0.5x downscaled + 1D parabolic sub-pixel refinement).
        Masks out center circle `r <= 0.20*R`, outer ring `r > 0.85*R`, and optional camera view cone.
        Returns `(world_x, world_y, corr)` or `None`.
        """
        span_world, search_rad, target_ppu = self._patch_params_for_regime(regime)
        half_extent = 0.5 * span_world + search_rad

        ref_patch, (origin_wx, origin_wy, ppu) = self.tile_cache.extract_local_patch(
            map_id, anchor_x, anchor_y, half_extent, target_px_per_unit=target_ppu
        )
        if ref_patch is None or ref_patch.size == 0 or float(np.std(ref_patch)) < 3.0:
            return None

        # Downscale reference patch by 0.5x (e.g. 0.5 -> 0.25 px/unit for open_world, 1.0 -> 0.5 for city)
        ds = float(self.config.match_downscale)
        ref_h, ref_w = ref_patch.shape[:2]
        ds_w = max(24, int(round(ref_w * ds)))
        ds_h = max(24, int(round(ref_h * ds)))
        eff_ppu_x = ppu * (ds_w / float(ref_w))
        eff_ppu_y = ppu * (ds_h / float(ref_h))
        ref_ds = cv2.resize(ref_patch, (ds_w, ds_h), interpolation=cv2.INTER_AREA)

        # Build matching downscaled minimap template (optionally de-rotated for Follow View)
        mm_work = minimap_gray
        if abs(pre_rotate_cw_deg) > 0.25:
            mh, mw = mm_work.shape[:2]
            # OpenCV getRotationMatrix2D positive angle = CCW, so clockwise pre_rotate_cw_deg -> negative angle
            M_rot = cv2.getRotationMatrix2D(((mw - 1) * 0.5, (mh - 1) * 0.5), -float(pre_rotate_cw_deg), 1.0)
            mm_work = cv2.warpAffine(mm_work, M_rot, (mw, mh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        tpl_sz = max(20, int(round(span_world * eff_ppu_x)))
        if tpl_sz >= ds_w - 2 or tpl_sz >= ds_h - 2:
            return None

        tpl_ds = cv2.resize(mm_work, (tpl_sz, tpl_sz), interpolation=cv2.INTER_AREA)
        eff_cone = (
            wrap_angle_360(exclude_cone_cw_deg + pre_rotate_cw_deg)
            if exclude_cone_cw_deg is not None
            else None
        )
        mask = self._get_cone_masked_annulus(tpl_sz, eff_cone)
        # Also suppress saturated white UI overlays (waypoints/quest pins) if enough terrain remains
        non_bright = mask & ((tpl_ds < 235).astype(np.uint8) * np.uint8(255))
        if int(cv2.countNonZero(non_bright)) >= int(0.65 * max(1, cv2.countNonZero(mask))):
            mask = non_bright

        # Guard against flat/zero-variance template
        if float(np.std(tpl_ds[mask > 0])) < 3.0:
            return None

        res = cv2.matchTemplate(ref_ds, tpl_ds, cv2.TM_CCOEFF_NORMED, mask=mask)
        if not np.isfinite(res).all():
            res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)

        _, max_val, _, (mx, my) = cv2.minMaxLoc(res)
        corr = float(max_val)
        if not math.isfinite(corr):
            return None

        rh, rw = res.shape[:2]
        sx_off = 0.0
        if 0 < mx < rw - 1:
            sx_off = self._parabolic_peak_offset(
                float(res[my, mx - 1]), float(res[my, mx]), float(res[my, mx + 1])
            )
        sy_off = 0.0
        if 0 < my < rh - 1:
            sy_off = self._parabolic_peak_offset(
                float(res[my - 1, mx]), float(res[my, mx]), float(res[my + 1, mx])
            )

        # Exact pixel-center mapping back to world coordinates:
        # center pixel of tpl_ds is (tpl_sz - 1) / 2.0, pixel center in ref_ds is rx + 0.5 => (tpl_sz / 2.0)
        pred_wx = origin_wx + (mx + sx_off + 0.5 * tpl_sz) / eff_ppu_x
        pred_wy = origin_wy + (my + sy_off + 0.5 * tpl_sz) / eff_ppu_y
        return float(pred_wx), float(pred_wy), corr

    def _estimate_follow_view_orb(
        self,
        minimap_gray: np.ndarray,
        map_id: int,
        anchor_x: float,
        anchor_y: float,
        regime: str,
    ) -> Optional[Tuple[float, float, float, float, int]]:
        """
        Recovers Follow-View map rotation `theta_map` and `(world_x, world_y)` using
        ORB + Lowe ratio test + RANSAC `cv2.estimateAffinePartial2D`.

        Sign convention:
            `M` maps reference patch (North-up) -> minimap (rotated).
            `theta_map = atan2(M[1, 0], M[0, 0])` in degrees (positive = CW in image coords).
            `view_heading = wrap_angle_360(-theta_map)` (CW from North).

        Returns `(world_x, world_y, theta_map_deg, confidence, inliers)` or `None`.
        """
        mh, mw = minimap_gray.shape[:2]
        mm_sz = min(mh, mw)
        span_world = self._span_for_regime(regime)
        search_rad = self.config.search_radius_city if regime == ZOOM_CITY else self.config.search_radius_open_world

        # Match reference patch resolution to minimap pixel resolution for crisp ORB descriptors
        ppu = float(mm_sz) / float(span_world)
        half_extent = 0.5 * span_world + search_rad
        ref_patch, (origin_wx, origin_wy, eff_ppu) = self.tile_cache.extract_local_patch(
            map_id, anchor_x, anchor_y, half_extent, target_px_per_unit=ppu
        )
        if ref_patch is None or ref_patch.size == 0 or float(np.std(ref_patch)) < 5.0:
            return None

        ref_eq = self._clahe.apply(ref_patch)
        mm_sq = minimap_gray if mh == mw else cv2.resize(minimap_gray, (mm_sz, mm_sz), interpolation=cv2.INTER_AREA)
        mm_eq = self._clahe.apply(mm_sq)
        mm_mask = self._get_annulus_mask(mm_sz)

        kp_r, des_r = self._orb.detectAndCompute(ref_eq, None)
        kp_m, des_m = self._orb.detectAndCompute(mm_eq, mm_mask)
        if des_r is None or des_m is None or len(kp_r) < 8 or len(kp_m) < 8:
            return None

        raw_matches = self._bf.knnMatch(des_r, des_m, k=2)
        good = []
        for pair in raw_matches:
            if len(pair) == 2:
                m0, m1 = pair
                if m0.distance < 0.78 * m1.distance:
                    good.append(m0)
        if len(good) < max(4, self.config.follow_view_min_inliers):
            return None

        pts_r = np.float32([kp_r[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        pts_m = np.float32([kp_m[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        M, inliers = cv2.estimateAffinePartial2D(
            pts_r,
            pts_m,
            method=cv2.RANSAC,
            ransacReprojThreshold=4.0,
            maxIters=2000,
            confidence=0.99,
        )
        if M is None or inliers is None:
            return None

        inlier_count = int(inliers.sum())
        if inlier_count < self.config.follow_view_min_inliers:
            return None

        scale_est = math.hypot(float(M[0, 0]), float(M[1, 0]))
        if not (0.72 <= scale_est <= 1.38):
            return None

        theta_map_deg = math.degrees(math.atan2(float(M[1, 0]), float(M[0, 0])))

        # Invert M to map minimap center -> reference patch coordinates -> world coordinates
        M_inv = cv2.invertAffineTransform(M)
        mm_cx = (mm_sz - 1) * 0.5
        mm_cy = (mm_sz - 1) * 0.5
        ref_cx = float(M_inv[0, 0] * mm_cx + M_inv[0, 1] * mm_cy + M_inv[0, 2])
        ref_cy = float(M_inv[1, 0] * mm_cx + M_inv[1, 1] * mm_cy + M_inv[1, 2])
        pred_wx = origin_wx + (ref_cx + 0.5) / eff_ppu
        pred_wy = origin_wy + (ref_cy + 0.5) / eff_ppu

        inlier_ratio = float(inlier_count) / float(max(1, len(good)))
        orb_conf = float(min(0.95, max(0.50, 0.45 + 0.35 * min(1.0, inlier_count / 40.0) + 0.15 * inlier_ratio)))
        return pred_wx, pred_wy, theta_map_deg, orb_conf, inlier_count

    def _prepare_phase_patch(
        self,
        minimap_gray: np.ndarray,
        exclude_cone_cw_deg: Optional[float] = None,
    ) -> np.ndarray:
        """Extracts center- and view-cone-masked inner square inside the minimap for phaseCorrelate."""
        h, w = minimap_gray.shape[:2]
        # Inscribed square inside r = 0.85 * R -> half-side = 0.85 / sqrt(2) * R ≈ 0.60 * R -> [0.20, 0.80]
        y0, y1 = int(round(0.20 * h)), int(round(0.80 * h))
        x0, x1 = int(round(0.20 * w)), int(round(0.80 * w))
        crop = minimap_gray[y0:y1, x0:x1]
        resized = cv2.resize(crop, (self._phase_size, self._phase_size), interpolation=cv2.INTER_AREA).astype(
            np.float32
        )
        # Neutralize center player arrow circle (r <= 0.20 * R -> ~0.33 of cropped half-side) with mean intensity
        mean_val = float(np.mean(resized))
        c = self._phase_size // 2
        cv2.circle(resized, (c, c), int(round(0.26 * c)), mean_val, -1)
        if exclude_cone_cw_deg is not None and math.isfinite(float(exclude_cone_cw_deg)):
            cv_ang = wrap_angle_360(exclude_cone_cw_deg) - 90.0
            cv2.ellipse(resized, (c, c), (self._phase_size, self._phase_size), 0.0, cv_ang - 48.0, cv_ang + 48.0, mean_val, -1)
        return resized

    def _run_phase_correlate_fallback(
        self,
        curr_phase: np.ndarray,
    ) -> Optional[Tuple[float, float, float]]:
        """
        Bounded `cv2.phaseCorrelate` relative motion estimate when template correlation < 0.45.
        Returns `(pred_wx, pred_wy, confidence)` or `None`.
        """
        if self._prev_phase_gray is None or self.fsm._x is None or self.fsm._y is None:
            return None
        if curr_phase.shape != self._prev_phase_gray.shape:
            return None

        (dx, dy), response = cv2.phaseCorrelate(self._prev_phase_gray, curr_phase, self._hann_window)
        resp = float(response)
        if not (math.isfinite(dx) and math.isfinite(dy) and math.isfinite(resp)):
            return None
        if resp < self.config.phase_min_response:
            return None

        shift_mag = math.hypot(dx, dy)
        if shift_mag > self.config.phase_max_shift_frac * self._phase_size:
            return None

        # phase_size covers 0.60 * minimap_width = 0.60 * span_world
        span_world = (
            self._confirmed_span_world
            if self._span_confirmed
            else self._span_for_regime(self._zoom_regime)
        )
        world_units_per_phase_px = (0.60 * span_world) / float(self._phase_size)

        # Feature shift (+dx, +dy) in minimap means player moved (-dx, -dy)
        step_mx = -dx * world_units_per_phase_px
        step_my = -dy * world_units_per_phase_px

        if self._minimap_mode == MODE_FOLLOW_VIEW and abs(self._map_rotation_deg) > 0.5:
            # Rotate minimap-frame motion back to North-up world axes by -theta_map
            rad = math.radians(-self._map_rotation_deg)
            c, s = math.cos(rad), math.sin(rad)
            step_wx = c * step_mx - s * step_my
            step_wy = s * step_mx + c * step_my
        else:
            step_wx, step_wy = step_mx, step_my

        conf = float(min(0.58, max(0.36, 0.34 + 0.30 * resp)))
        return float(self.fsm._x + step_wx), float(self.fsm._y + step_wy), conf

    @property
    def _cone_mask_heading(self) -> Optional[float]:
        return self._cone_raw_heading if self._cone_raw_heading is not None else self._camera_heading

    def _update_headings(
        self,
        minimap_img: np.ndarray,
        gate_res: MinimapGateResult,
        ts: Optional[float] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Extracts 360° `camera_heading` and `body_heading` on every valid frame where the
        corresponding pixels changed (even when stationary, e.g. rotating camera/character while
        standing still), while keeping unchanged stationary frames < 0.3 ms.
        """
        arrow_diff_thresh = (
            self.config.arrow_gate_change_diff
            if self._body_heading is not None
            else self.config.gate_cone_change_diff
        )
        arrow_may_have_changed = (
            self._body_heading is None
            or gate_res.center_diff >= arrow_diff_thresh
        )
        if arrow_may_have_changed:
            body_deg, body_conf = extract_player_arrow_360(minimap_img)
            if body_deg is not None and body_conf > 0.0:
                self._body_heading = self._body_smoother.update(body_deg)
                self._body_conf = float(body_conf)

        cone_may_have_changed = (
            self._camera_heading is None
            or gate_res.frame_diff >= self.config.gate_cone_change_diff
            or gate_res.alpha_diff >= self.config.gate_cone_change_diff
        )
        cam_deg: Optional[float] = None
        cam_conf = 0.0
        cam_method = "none"
        if cone_may_have_changed:
            cam_deg, cam_conf, cam_method = extract_camera_view_cone(minimap_img)
            if cam_deg is not None and cam_conf > 0.0:
                self._cone_raw_heading = float(cam_deg)
        now_ts = float(ts) if ts is not None else time.monotonic()
        prev_source = self._camera_fuser.source
        fused = self._camera_fuser.update(
            cam_deg if cone_may_have_changed else None,
            cam_conf,
            cam_method,
            self._body_heading,
            self._body_conf,
            now_ts,
        )
        if fused is not None and (
            self._camera_heading is None
            or self._camera_fuser.source != prev_source
            or abs(shortest_angle_delta(self._camera_heading, fused)) > 0.05
        ):
            if self._camera_fuser.source == "body" and prev_source != "body":
                self._camera_smoother.reset()
            self._camera_heading = self._camera_smoother.update(fused)
            self._camera_conf = float(self._camera_fuser.confidence)
            self._camera_method = self._camera_fuser.source

        if self._minimap_mode == MODE_FOLLOW_VIEW:
            # If camera view cone clearly leaves North on screen, user switched back to Lock Direction
            if (
                self._camera_heading is not None
                and self._camera_conf >= 0.45
                and abs(shortest_angle_delta(0.0, self._camera_heading)) >= self.config.follow_view_unlock_deg
            ):
                self._minimap_mode = MODE_LOCK_DIRECTION
                self._map_rotation_deg = 0.0
                self._view_heading = self._camera_heading
            else:
                self._view_heading = wrap_angle_360(-self._map_rotation_deg)
        else:
            if self._camera_heading is not None and self._camera_conf > 0.0:
                self._view_heading = self._camera_heading
            elif self._body_heading is not None and self._body_conf > 0.0:
                self._view_heading = self._body_heading

        return self._camera_heading, self._body_heading

    def compare_surface_vs_underground(
        self,
        minimap_img: np.ndarray,
        surface_patch: Optional[np.ndarray],
        underground_patch: Optional[np.ndarray],
        *,
        candidate_floor_id: Optional[int] = None,
        candidate_floor_name: str = "B1",
        patch_px_per_unit: Optional[float] = None,
        apply_hysteresis: bool = True,
    ) -> Dict[str, Any]:
        """
        Compares annulus Sobel edge-correlation score of surface vs underground tile at `(world_x, world_y)`.
        When `apply_hysteresis=True`, requires 3 consecutive wins with margin `>= 0.08` before switching floor.
        """
        if minimap_img is None or not isinstance(minimap_img, np.ndarray) or minimap_img.size == 0:
            return {
                "surface_score": 0.0,
                "underground_score": 0.0,
                "margin": 0.0,
                "is_underground": self.fsm._is_underground,
                "floor_id": self.fsm._floor_id,
                "floor_name": self.fsm._floor_name,
                "streak": self._floor_switch_streak,
                "switched": False,
            }

        mm_gray = (
            minimap_img
            if minimap_img.ndim == 2
            else cv2.cvtColor(minimap_img[:, :, :3], cv2.COLOR_BGR2GRAY)
        )
        if self._minimap_mode == MODE_FOLLOW_VIEW and abs(self._map_rotation_deg) > 0.5:
            mh, mw = mm_gray.shape[:2]
            M_rot = cv2.getRotationMatrix2D(((mw - 1) * 0.5, (mh - 1) * 0.5), -float(self._map_rotation_deg), 1.0)
            mm_gray = cv2.warpAffine(mm_gray, M_rot, (mw, mh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        def _score_patch(patch: Optional[np.ndarray]) -> float:
            if patch is None or not isinstance(patch, np.ndarray) or patch.size == 0:
                return 0.0
            p_gray = patch if patch.ndim == 2 else cv2.cvtColor(patch[:, :, :3], cv2.COLOR_BGR2GRAY)
            if p_gray.dtype != np.uint8:
                p_gray = np.clip(p_gray, 0, 255).astype(np.uint8)
            ph, pw = p_gray.shape[:2]
            if min(ph, pw) < 20 or float(np.std(p_gray)) < 3.0:
                return 0.0

            if patch_px_per_unit is not None and patch_px_per_unit > 0.0:
                tpl_sz = min(min(ph, pw), max(20, int(round(self._span_for_regime(self._zoom_regime) * patch_px_per_unit))))
            else:
                tpl_sz = min(ph, pw, min(mm_gray.shape[:2]))
            tpl_sz = max(20, tpl_sz)

            mm_tpl = cv2.resize(mm_gray, (tpl_sz, tpl_sz), interpolation=cv2.INTER_AREA)
            mask = self._get_annulus_mask(tpl_sz)

            mm_edge = cv2.magnitude(
                cv2.Sobel(mm_tpl, cv2.CV_32F, 1, 0, ksize=3),
                cv2.Sobel(mm_tpl, cv2.CV_32F, 0, 1, ksize=3),
            )
            p_edge = cv2.magnitude(
                cv2.Sobel(p_gray, cv2.CV_32F, 1, 0, ksize=3),
                cv2.Sobel(p_gray, cv2.CV_32F, 0, 1, ksize=3),
            )

            active = mask > 0
            if float(np.std(mm_edge[active])) < 1e-3:
                return 0.0
            if ph == tpl_sz and pw == tpl_sz and float(np.std(p_edge[active])) < 1e-3:
                return 0.0

            res = cv2.matchTemplate(p_edge, mm_edge, cv2.TM_CCOEFF_NORMED, mask=mask)
            if not np.isfinite(res).all():
                res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
            _, max_val, _, _ = cv2.minMaxLoc(res)
            return float(max(0.0, min(1.0, max_val)))

        surf_score = _score_patch(surface_patch)
        ug_score = _score_patch(underground_patch)
        switched = False

        if apply_hysteresis:
            margin_req = self.config.floor_switch_margin
            min_score = self.config.floor_switch_min_score
            wins_req = self.config.floor_switch_consecutive_wins

            if not self.fsm._is_underground:
                if ug_score >= min_score and (ug_score - surf_score) >= margin_req:
                    if candidate_floor_id == self._floor_switch_candidate_id:
                        self._floor_switch_streak += 1
                    else:
                        self._floor_switch_candidate_id = candidate_floor_id
                        self._floor_switch_streak = 1
                    if self._floor_switch_streak >= wins_req:
                        self.fsm.set_floor(True, candidate_floor_id, candidate_floor_name)
                        self._floor_switch_streak = 0
                        self._floor_switch_candidate_id = None
                        switched = True
                else:
                    self._floor_switch_streak = 0
                    self._floor_switch_candidate_id = None
            else:
                if surf_score >= min_score and (surf_score - ug_score) >= margin_req:
                    self._floor_switch_streak += 1
                    if self._floor_switch_streak >= wins_req:
                        self.fsm.set_floor(False, None, "G")
                        self._floor_switch_streak = 0
                        self._floor_switch_candidate_id = None
                        switched = True
                else:
                    self._floor_switch_streak = 0

        return {
            "surface_score": float(surf_score),
            "underground_score": float(ug_score),
            "margin": float(ug_score - surf_score),
            "is_underground": self.fsm._is_underground,
            "floor_id": self.fsm._floor_id,
            "floor_name": self.fsm._floor_name,
            "streak": int(self._floor_switch_streak),
            "switched": bool(switched),
        }

    def _get_ref_cell(
        self,
        map_id: int,
        anchor_x: float,
        anchor_y: float,
        *,
        is_underground: bool,
        floor_id: Optional[int],
        zoom_tier: int = -1,
    ) -> Optional[Dict[str, Any]]:
        z_eff = 0 if (not is_underground and int(zoom_tier) == 0) else -1
        if z_eff == 0:
            step_w = 96.0
            ppu = 1.0
            half_ext = 176.0
        else:
            step_w = 128.0
            ppu = 0.5
            half_ext = 280.0

        cx_cell = int(round(anchor_x / step_w)) * step_w
        cy_cell = int(round(anchor_y / step_w)) * step_w
        fid_key = int(floor_id) if (is_underground and floor_id is not None) else None
        ckey = (int(map_id), bool(is_underground), fid_key, z_eff, int(cx_cell), int(cy_cell))
        dl_seq = int(getattr(self.tile_cache.tile_provider, "downloads", 0))
        cached = self._cell_cache.get(ckey)
        if cached is not None:
            if cached.get("dl_seq", dl_seq) == dl_seq:
                self._cell_cache.move_to_end(ckey)
                return cached
            self._cell_cache.pop(ckey, None)

        if is_underground:
            if self.ug_provider is None or floor_id is None:
                return None
            patch, meta = self.ug_provider.extract_patch(
                map_id, int(floor_id), cx_cell, cy_cell, half_ext, target_px_per_unit=ppu
            )
            if patch is None or float(np.std(patch)) < 4.0:
                return None
        else:
            patch, meta = self.tile_cache.extract_local_patch(
                map_id, cx_cell, cy_cell, half_ext, target_px_per_unit=ppu
            )
            if patch is None or float(np.std(patch)) < 4.0:
                return None

        eq = self._clahe.apply(patch)
        kp_r, des_r = self._sift_ref.detectAndCompute(eq, None)
        sobel_r = cv2.magnitude(
            cv2.Sobel(patch, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(patch, cv2.CV_32F, 0, 1, ksize=3),
        )
        rec = {
            "patch": patch,
            "sobel": sobel_r,
            "kp": tuple(kp_r or ()),
            "des": des_r,
            "ox": float(meta[0]),
            "oy": float(meta[1]),
            "ppu": float(meta[2]),
            "cx": cx_cell,
            "cy": cy_cell,
            "zoom_tier": z_eff,
            "dl_seq": dl_seq,
        }
        self._cell_cache[ckey] = rec
        while len(self._cell_cache) > self._max_cells:
            self._cell_cache.popitem(last=False)
        return rec

    def _extract_mm_sift(
        self,
        mm_img: np.ndarray,
        sz: int = 128,
    ) -> Tuple[Tuple[cv2.KeyPoint, ...], Optional[np.ndarray], int, np.ndarray]:
        """Extracts single-pass 128x128 minimap SIFT descriptors once per frame (reusable across cells/layers)."""
        mm_gray = mm_img if mm_img.ndim == 2 else cv2.cvtColor(mm_img[:, :, :3], cv2.COLOR_BGR2GRAY)
        s_sz = min(mm_gray.shape[:2])
        use_sz = sz if s_sz > sz else s_sz
        mm_sq_g = (
            mm_gray
            if (mm_gray.shape[0] == use_sz and mm_gray.shape[1] == use_sz)
            else cv2.resize(mm_gray, (use_sz, use_sz), interpolation=cv2.INTER_AREA)
        )
        mask = self._get_annulus_mask(use_sz).copy()
        if mm_img.ndim == 3 and mm_img.shape[2] >= 3:
            mm_sq = (
                mm_img[:, :, :3]
                if (mm_img.shape[0] == use_sz and mm_img.shape[1] == use_sz)
                else cv2.resize(mm_img[:, :, :3], (use_sz, use_sz), interpolation=cv2.INTER_AREA)
            )
            hsv = cv2.cvtColor(mm_sq, cv2.COLOR_BGR2HSV)
            white_pins = (mm_sq_g >= 238) & (hsv[:, :, 1] <= 38)
            active_px = int(cv2.countNonZero(mask))
            if active_px - int(np.sum(white_pins)) >= 0.4 * active_px:
                mask[white_pins] = 0
        kp_m, des_m = self._sift_mm.detectAndCompute(self._clahe.apply(mm_sq_g), mask)
        return tuple(kp_m or ()), des_m, use_sz, mm_gray

    def _match_sift_feats(
        self,
        kp_m: Tuple[cv2.KeyPoint, ...],
        des_m: Optional[np.ndarray],
        sz: int,
        cell: Dict[str, Any],
        anchor_x: float,
        anchor_y: float,
        max_dist_w: float,
    ) -> Optional[Tuple[float, float, int, float, float]]:
        """Matches pre-extracted minimap SIFT features against a cached cell (`wx, wy, ninl, span_w, theta_map_deg`)."""
        des_r = cell.get("des")
        min_inl = self.config.sift_min_inliers
        if des_r is None or len(cell["kp"]) < 10 or des_m is None or len(kp_m) < min_inl:
            return None
        pairs = self._bf_sift.knnMatch(des_m, des_r, k=2)
        good = []
        for pair in pairs:
            if len(pair) == 2 and pair[0].distance < 0.78 * pair[1].distance:
                good.append(pair[0])
        if len(good) < min_inl:
            return None
        pts_m = np.float32([kp_m[m.queryIdx].pt for m in good])
        pts_r = np.float32([cell["kp"][m.trainIdx].pt for m in good])
        M, inl = cv2.estimateAffinePartial2D(
            pts_m,
            pts_r,
            method=cv2.RANSAC,
            ransacReprojThreshold=3.5,
            maxIters=1200,
            confidence=0.99,
        )
        if M is None or inl is None:
            return None
        ninl = int(inl.sum())
        if ninl < min_inl:
            return None
        sc = math.hypot(float(M[0, 0]), float(M[1, 0]))
        # M maps minimap -> North-up reference; negate so theta_map_deg matches ORB ref -> minimap sign.
        theta_mm_to_ref = math.degrees(math.atan2(float(M[1, 0]), float(M[0, 0])))
        theta_map_deg = ((-theta_mm_to_ref) + 180.0) % 360.0 - 180.0
        span_w = (sz * sc) / cell["ppu"]
        if not (95.0 <= span_w <= 430.0):
            return None
        c = (sz - 1) * 0.5
        rx = float(M[0, 0] * c + M[0, 1] * c + M[0, 2])
        ry = float(M[1, 0] * c + M[1, 1] * c + M[1, 2])
        wx = cell["ox"] + (rx + 0.5) / cell["ppu"]
        wy = cell["oy"] + (ry + 0.5) / cell["ppu"]
        if math.hypot(wx - anchor_x, wy - anchor_y) > max_dist_w:
            return None
        return float(wx), float(wy), ninl, float(span_w), float(theta_map_deg)

    def _match_sobel_fast(
        self,
        mm_img: np.ndarray,
        cell: Dict[str, Any],
        anchor_x: float,
        anchor_y: float,
        span_world: float,
        *,
        search_rad_w: float = 42.0,
        pre_rotate_cw_deg: float = 0.0,
        exclude_cone_cw_deg: Optional[float] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """Unmasked zero-mean orthogonal Sobel TM_CCOEFF_NORMED (with 0.5x half-res ref) + parabolic peak."""
        ppu = float(cell["ppu"])
        tpl_sz_native = max(24, int(round(span_world * ppu)))
        sobel_full = cell["sobel"]
        if tpl_sz_native > 96:
            sobel_ref = cell.get("sobel_half")
            if sobel_ref is None:
                hw = max(16, sobel_full.shape[1] // 2)
                hh = max(16, sobel_full.shape[0] // 2)
                sobel_ref = cv2.resize(sobel_full, (hw, hh), interpolation=cv2.INTER_AREA)
                cell["sobel_half"] = sobel_ref
            eff_ppu = ppu * (sobel_ref.shape[1] / float(sobel_full.shape[1]))
        else:
            sobel_ref = sobel_full
            eff_ppu = ppu

        tpl_sz = max(24, int(round(span_world * eff_ppu)))
        ax_px = (anchor_x - cell["ox"]) * eff_ppu
        ay_px = (anchor_y - cell["oy"]) * eff_ppu
        half_need = int(round(0.5 * tpl_sz + search_rad_w * eff_ppu)) + 2
        x0 = max(0, int(math.floor(ax_px - half_need)))
        y0 = max(0, int(math.floor(ay_px - half_need)))
        x1 = min(sobel_ref.shape[1], int(math.ceil(ax_px + half_need)))
        y1 = min(sobel_ref.shape[0], int(math.ceil(ay_px + half_need)))
        if (x1 - x0) <= tpl_sz + 2 or (y1 - y0) <= tpl_sz + 2:
            return None

        roi_edge = sobel_ref[y0:y1, x0:x1]
        mm_gray = mm_img if mm_img.ndim == 2 else cv2.cvtColor(mm_img[:, :, :3], cv2.COLOR_BGR2GRAY)
        if abs(pre_rotate_cw_deg) > 0.25:
            mh, mw = mm_gray.shape[:2]
            m_rot = cv2.getRotationMatrix2D(((mw - 1) * 0.5, (mh - 1) * 0.5), -float(pre_rotate_cw_deg), 1.0)
            mm_gray = cv2.warpAffine(mm_gray, m_rot, (mw, mh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        tpl_mm = (
            mm_gray
            if (mm_gray.shape[0] == tpl_sz and mm_gray.shape[1] == tpl_sz)
            else cv2.resize(mm_gray, (tpl_sz, tpl_sz), interpolation=cv2.INTER_AREA)
        )
        tpl_edge = cv2.magnitude(
            cv2.Sobel(tpl_mm, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(tpl_mm, cv2.CV_32F, 0, 1, ksize=3),
        )

        mask = self._get_cone_masked_annulus(tpl_sz, (wrap_angle_360(exclude_cone_cw_deg + pre_rotate_cw_deg) if exclude_cone_cw_deg is not None else None), half_fov_deg=50.0)
        if mask is self._get_annulus_mask(tpl_sz):
            mask = mask.copy()
        non_bright = mask & ((tpl_mm < 236).astype(np.uint8) * np.uint8(255))
        if int(cv2.countNonZero(non_bright)) >= int(0.55 * max(1, cv2.countNonZero(mask))):
            mask = non_bright

        act = mask > 0
        acnt = int(np.count_nonzero(act))
        if acnt < 16 or float(np.std(tpl_edge[act])) < 1.0:
            return None

        tpl_zm = np.zeros_like(tpl_edge)
        tpl_zm[act] = tpl_edge[act] - float(np.mean(tpl_edge[act]))
        res = cv2.matchTemplate(roi_edge, tpl_zm, cv2.TM_CCOEFF_NORMED)
        if not np.isfinite(res).all():
            res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, mv, _, (mx, my) = cv2.minMaxLoc(res)
        raw_mv = float(mv)
        if not math.isfinite(raw_mv):
            return None
        scale_c = math.sqrt(acnt / float(tpl_sz * tpl_sz))
        corr = min(1.0, raw_mv / max(0.25, scale_c))

        sub_x = self._parabolic_peak_offset(float(res[my, mx - 1]), raw_mv, float(res[my, mx + 1])) if 0 < mx < res.shape[1] - 1 else 0.0
        sub_y = self._parabolic_peak_offset(float(res[my - 1, mx]), raw_mv, float(res[my + 1, mx])) if 0 < my < res.shape[0] - 1 else 0.0

        wx = cell["ox"] + (x0 + mx + sub_x + 0.5 * tpl_sz) / eff_ppu
        wy = cell["oy"] + (y0 + my + sub_y + 0.5 * tpl_sz) / eff_ppu
        return float(wx), float(wy), corr

    def _apply_rotation_mode(self, theta_map_deg: float, ninl: int) -> None:
        if abs(theta_map_deg) <= max(self.config.follow_view_min_map_rot_deg, 10.0):
            self._minimap_mode = MODE_LOCK_DIRECTION
            self._map_rotation_deg = 0.0
            self._view_heading = self._camera_heading if self._camera_heading is not None else self._body_heading
        elif ninl >= max(8, self.config.follow_view_min_inliers):
            self._minimap_mode = MODE_FOLLOW_VIEW
            self._map_rotation_deg = float(theta_map_deg)
            self._view_heading = wrap_angle_360(-theta_map_deg)

    def _update_zoom_from_span(self, span_w: float) -> None:
        if not self._span_confirmed:
            self._confirmed_span_world = float(span_w)
            self._span_confirmed = True
        else:
            self._confirmed_span_world = 0.65 * self._confirmed_span_world + 0.35 * float(span_w)
        want_regime = ZOOM_CITY if span_w < 230.0 else ZOOM_OPEN_WORLD
        if want_regime != self._zoom_regime:
            self._zoom_alt_streak += 1
            if self._zoom_alt_streak >= self.config.zoom_switch_consecutive_wins:
                self._zoom_regime = want_regime
                self._zoom_alt_streak = 0
        else:
            self._zoom_alt_streak = 0

    def _probe_candidate_layers(
        self,
        kp_m: Tuple[cv2.KeyPoint, ...],
        des_m: Optional[np.ndarray],
        mm_sz: int,
        mm_gray: np.ndarray,
        cone_excl: Optional[float],
        map_id: int,
        anchor_x: float,
        anchor_y: float,
        cur_is_ug: bool,
        cur_floor_id: Optional[int],
        cur_ninl: int,
        max_dist: float,
        was_resumed: bool,
        ts: float,
        p_score: float,
    ) -> Optional[MinimapPose]:
        """Evaluates overlapping surface/cave floor candidates with leaky-bucket hysteresis (+2/-1)."""
        if self.ug_provider is None:
            return None
        overlapping = self.ug_provider.find_overlapping_floors(map_id, anchor_x, anchor_y, pad=30.0)
        if not overlapping:
            self._pending_layer_key = None
            self._pending_layer_score = 0
            return None

        def _sort_key(rec: Dict[str, Any]) -> Tuple[int, float]:
            inside = (rec["lx"] <= anchor_x <= rec["rx"]) and (rec["ly"] <= anchor_y <= rec["ry"])
            area = (rec["rx"] - rec["lx"]) * (rec["ry"] - rec["ly"])
            return (0 if inside else 1, area)

        candidates: List[Tuple[bool, Optional[int], str]] = []
        if not cur_is_ug:
            for rec in sorted(overlapping, key=_sort_key)[:3]:
                candidates.append((True, int(rec["floor_id"]), str(rec["floor_name"])))
        else:
            candidates.append((False, None, "G"))
            cur_gid = None
            for rec in overlapping:
                if int(rec["floor_id"]) == int(cur_floor_id or -1):
                    cur_gid = rec["group_id"]
                    break
            siblings = [
                r
                for r in overlapping
                if int(r["floor_id"]) != int(cur_floor_id or -1)
                and (cur_gid is None or r["group_id"] == cur_gid)
            ]
            for rec in sorted(siblings, key=_sort_key)[:2]:
                candidates.append((True, int(rec["floor_id"]), str(rec["floor_name"])))

        min_cand_ninl = 8 if cur_ninl == 0 else max(10, cur_ninl + 4)
        best_cand = None
        for c_ug, c_fid, c_fname in candidates:
            cell_c = self._get_ref_cell(
                map_id, anchor_x, anchor_y, is_underground=c_ug, floor_id=c_fid, zoom_tier=-1
            )
            if cell_c is None:
                continue
            cand = self._match_sift_feats(kp_m, des_m, mm_sz, cell_c, anchor_x, anchor_y, max_dist_w=max_dist)
            if cand is None or cand[2] < min_cand_ninl:
                continue
            if best_cand is None or cand[2] > best_cand[3]:
                wx, wy, ninl, span_w, theta_map = cand
                best_cand = (c_ug, c_fid, c_fname, ninl, wx, wy, span_w, theta_map)

        if best_cand is None:
            if cur_ninl >= 10:
                self._pending_layer_key = None
                self._pending_layer_score = 0
            else:
                self._pending_layer_score = max(0, self._pending_layer_score - 1)
                if self._pending_layer_score == 0:
                    self._pending_layer_key = None
            return None

        c_ug, c_fid, c_fname, ninl, wx, wy, span_w, theta_map = best_cand
        lkey = (c_ug, c_fid, c_fname)
        if self._pending_layer_key == lkey:
            self._pending_layer_score += 2
        else:
            self._pending_layer_key = lkey
            self._pending_layer_score = 2

        immediate_resume_switch = bool(was_resumed and cur_ninl == 0 and ninl >= 13)
        if not (self._pending_layer_score >= 3 or immediate_resume_switch):
            return None

        self._pending_layer_key = None
        self._pending_layer_score = 0
        self._motion_fallback_frames = 0
        self._consecutive_sift_misses = 0
        self._sift_interframe_budget = 0
        self._just_resumed_from_pause = False
        self._resume_frames_left = 0
        self._long_pause_pending = False
        self._relocate_tier2_left = 0
        self._global_vote_bins.clear()
        self._global_query_count = 0
        self._last_zoom_tier = -1
        self._last_sift_inliers = ninl
        self._update_zoom_from_span(span_w)
        self._apply_rotation_mode(theta_map, ninl)
        self._prev_mm_gray_for_phase = mm_gray
        self._prev_cone_excl_for_phase = cone_excl
        self._prev_phase_gray = None
        self.fsm.set_anchor(
            map_id=map_id,
            world_x=wx,
            world_y=wy,
            timestamp=ts,
            is_underground=c_ug,
            floor_id=c_fid,
            floor_name=c_fname,
        )
        return self._build_pose(
            ts,
            stationary=False,
            match_method="layer_switch_sift",
            reason=f"layer_switch_{c_fname}",
            should_broadcast_web=True,
            paimon_score=p_score,
            sift_inliers=ninl,
        )

    def _probe_tier2_neighbors(
        self,
        kp_m: Tuple[cv2.KeyPoint, ...],
        des_m: Optional[np.ndarray],
        mm_sz: int,
        map_id: int,
        anchor_x: float,
        anchor_y: float,
        is_ug: bool,
        floor_id: Optional[int],
    ) -> Optional[Tuple[float, float, int, float, float]]:
        """Probes 4 neighboring z=-1 cells shifted by +-380w (covers local teleports up to ~680w)."""
        best = None
        for dx, dy in ((380.0, 0.0), (-380.0, 0.0), (0.0, 380.0), (0.0, -380.0)):
            nx, ny = anchor_x + dx, anchor_y + dy
            cell_n = self._get_ref_cell(
                map_id, nx, ny, is_underground=is_ug, floor_id=floor_id, zoom_tier=-1
            )
            if cell_n is None:
                continue
            cand = self._match_sift_feats(kp_m, des_m, mm_sz, cell_n, nx, ny, max_dist_w=300.0)
            if cand is not None and cand[2] >= 12 and (best is None or cand[2] > best[2]):
                best = cand
        return best

    def _legacy_annulus_or_orb_match(
        self,
        minimap_gray: np.ndarray,
        map_id: int,
        anchor_x: float,
        anchor_y: float,
    ) -> Tuple[Optional[float], Optional[float], float, str, bool]:
        """Fallback legacy z=-2 annulus template & Follow-View ORB path (handles blurred synthetic/sparse patches)."""
        cam_pinned_north = bool(
            self._camera_heading is not None
            and abs(shortest_angle_delta(0.0, self._camera_heading)) < self.config.follow_view_cam_pin_deg
        )
        best_wx: Optional[float] = None
        best_wy: Optional[float] = None
        best_corr: float = -1.0
        match_method = "none"
        curr_regime = self._zoom_regime
        alt_regime = ZOOM_CITY if curr_regime == ZOOM_OPEN_WORLD else ZOOM_OPEN_WORLD

        if self._minimap_mode == MODE_FOLLOW_VIEW:
            orb_res = self._estimate_follow_view_orb(minimap_gray, map_id, anchor_x, anchor_y, curr_regime)
            if orb_res is not None:
                o_wx, o_wy, theta_map, o_conf, _ = orb_res
                self._map_rotation_deg = float(theta_map)
                self._view_heading = wrap_angle_360(-self._map_rotation_deg)
                derot = self._match_annulus_template(
                    minimap_gray,
                    map_id,
                    anchor_x,
                    anchor_y,
                    curr_regime,
                    pre_rotate_cw_deg=-theta_map,
                    exclude_cone_cw_deg=0.0,
                )
                if derot is not None and derot[2] >= self.config.template_min_corr:
                    best_wx, best_wy, best_corr = derot
                    match_method = "follow_view_annulus"
                else:
                    best_wx, best_wy, best_corr = o_wx, o_wy, o_conf
                    match_method = "follow_view_orb"
            return best_wx, best_wy, best_corr, match_method, False

        curr_res = self._match_annulus_template(
            minimap_gray,
            map_id,
            anchor_x,
            anchor_y,
            curr_regime,
            exclude_cone_cw_deg=self._cone_mask_heading,
        )
        curr_corr = curr_res[2] if curr_res is not None else -1.0

        alt_res = None
        if curr_corr <= (1.0 - self.config.zoom_switch_margin + 1e-6):
            alt_res = self._match_annulus_template(
                minimap_gray,
                map_id,
                anchor_x,
                anchor_y,
                alt_regime,
                exclude_cone_cw_deg=self._cone_mask_heading,
            )
        alt_corr = alt_res[2] if alt_res is not None else -1.0
        alt_near_anchor = (
            alt_res is not None
            and math.hypot(alt_res[0] - anchor_x, alt_res[1] - anchor_y) <= 45.0
        )
        alt_clear_win = (
            alt_near_anchor
            and alt_corr >= self.config.zoom_switch_min_corr
            and (alt_corr - max(0.0, curr_corr)) >= self.config.zoom_switch_margin
        )

        zoom_pending_switch = False
        if alt_clear_win:
            self._zoom_alt_streak += 1
            if self._zoom_alt_streak >= self.config.zoom_switch_consecutive_wins:
                self._zoom_regime = alt_regime
                self._zoom_alt_streak = 0
                assert alt_res is not None
                best_wx, best_wy, best_corr = alt_res
                match_method = "annulus_template_zoom_switch"
            else:
                zoom_pending_switch = True
                if curr_res is not None and curr_corr >= self.config.template_min_corr:
                    best_wx, best_wy, best_corr = curr_res
                    match_method = "annulus_template"
        else:
            self._zoom_alt_streak = 0
            if curr_res is not None:
                best_wx, best_wy, best_corr = curr_res
                match_method = "annulus_template"

        if (not zoom_pending_switch) and (cam_pinned_north or self._camera_heading is None) and best_corr < 0.65:
            orb_res = self._estimate_follow_view_orb(minimap_gray, map_id, anchor_x, anchor_y, self._zoom_regime)
            if orb_res is not None:
                o_wx, o_wy, theta_map, o_conf, _ = orb_res
                rot_mag = abs(shortest_angle_delta(0.0, theta_map))
                if rot_mag >= self.config.follow_view_min_map_rot_deg:
                    self._minimap_mode = MODE_FOLLOW_VIEW
                    self._map_rotation_deg = float(theta_map)
                    self._view_heading = wrap_angle_360(-self._map_rotation_deg)
                    derot = self._match_annulus_template(
                        minimap_gray,
                        map_id,
                        anchor_x,
                        anchor_y,
                        self._zoom_regime,
                        pre_rotate_cw_deg=-theta_map,
                        exclude_cone_cw_deg=0.0,
                    )
                    if derot is not None and derot[2] >= self.config.template_min_corr:
                        best_wx, best_wy, best_corr = derot
                        match_method = "follow_view_annulus"
                    else:
                        best_wx, best_wy, best_corr = o_wx, o_wy, o_conf
                        match_method = "follow_view_orb"

        return best_wx, best_wy, best_corr, match_method, zoom_pending_switch

    def _try_global_teyvat_relocate(
        self,
        minimap_img: np.ndarray,
        mm_gray: np.ndarray,
        kp_m: Optional[Any],
        des_m: Optional[np.ndarray],
        mm_sz: int,
        ts: float,
        p_score: float,
        cone_excl: float,
        t_match_start: float,
    ) -> Optional[MinimapPose]:
        """Attempts blind Map 2 Teyvat relocation only after `global_relocator.is_ready`."""
        if not (self.config.enable_global_relocate and self.global_relocator is not None):
            return None
        if not self.global_relocator.is_ready:
            return None

        q_idx = self._global_query_count
        self._global_query_count += 1
        if not (q_idx < 6 or (q_idx % 4 == 0)):
            return None

        cands = self.global_relocator.query_candidates(
            minimap_img, vote_bins=self._global_vote_bins, top_k=3
        )
        if not cands:
            return None

        if kp_m is None or des_m is None:
            kp_m, des_m, mm_sz, _ = self._extract_mm_sift(minimap_img, sz=128)
        if kp_m is None or des_m is None or len(des_m) == 0:
            return None

        best_global = None
        for cwx, cwy, _vscore in cands:
            cell_g = self._get_ref_cell(
                2, cwx, cwy, is_underground=False, floor_id=None, zoom_tier=-1
            )
            if cell_g is None:
                continue
            g_res = self._match_sift_feats(
                kp_m, des_m, mm_sz, cell_g, cwx, cwy, max_dist_w=260.0
            )
            if g_res is None:
                continue
            gwx, gwy, gninl, gspan, g_theta_map = g_res
            verified = gninl >= 16
            if not verified and gninl >= 10:
                is_fv = abs(g_theta_map) > 10.0
                s_chk = self._match_sobel_fast(
                    mm_gray,
                    cell_g,
                    gwx,
                    gwy,
                    gspan,
                    search_rad_w=16.0,
                    pre_rotate_cw_deg=(-g_theta_map if is_fv else 0.0),
                    exclude_cone_cw_deg=(0.0 if is_fv else self._cone_mask_heading),
                )
                if (
                    s_chk is not None
                    and s_chk[2] >= 0.30
                    and math.hypot(s_chk[0] - gwx, s_chk[1] - gwy) <= 12.0
                ):
                    verified = True
            if verified and (best_global is None or gninl > best_global[2]):
                best_global = (gwx, gwy, gninl, gspan, g_theta_map)

        if best_global is None:
            return None

        gwx, gwy, gninl, gspan, g_theta_map = best_global
        self._long_pause_pending = False
        self._relocate_tier2_left = 0
        self._just_resumed_from_pause = False
        self._resume_frames_left = 0
        self._motion_fallback_frames = 0
        self._consecutive_sift_misses = 0
        self._sift_interframe_budget = 0
        self._pending_layer_key = None
        self._pending_layer_score = 0
        self._global_vote_bins.clear()
        self._global_query_count = 0
        self._last_zoom_tier = -1
        self._last_sift_inliers = gninl
        self._update_zoom_from_span(gspan)
        self._apply_rotation_mode(g_theta_map, gninl)
        self._prev_mm_gray_for_phase = mm_gray
        self._prev_cone_excl_for_phase = cone_excl
        self._prev_phase_gray = None
        self.fsm.set_anchor(
            map_id=2,
            world_x=gwx,
            world_y=gwy,
            timestamp=ts,
            is_underground=False,
            floor_id=None,
            floor_name="G",
        )
        self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
        return self._build_pose(
            ts,
            stationary=False,
            match_method="global_teyvat_relocate",
            reason="global_teyvat_relocate",
            should_broadcast_web=True,
            paimon_score=p_score,
            sift_inliers=gninl,
        )

    def step(
        self,
        minimap_img: Optional[np.ndarray],
        now: Optional[float] = None,
        *,
        underground_overlay: Optional[Tuple[np.ndarray, Dict[str, Any], Optional[int], str]] = None,
    ) -> MinimapPose:
        """Processes one live minimap frame end-to-end using the V5.3 hybrid local/global pipeline."""
        return self.step_with_paimon(
            minimap_img,
            None,
            now=now,
            ui_scale=1.0,
            underground_overlay=underground_overlay,
        )

    def step_with_paimon(
        self,
        minimap_img: Optional[np.ndarray],
        paimon_maybe_img: Optional[np.ndarray] = None,
        *,
        now: Optional[float] = None,
        ui_scale: float = 1.0,
        underground_overlay: Optional[Tuple[np.ndarray, Dict[str, Any], Optional[int], str]] = None,
    ) -> MinimapPose:
        """Runs V5.3 with optional Paimon pause gate crop before delegating to `_step_core`."""
        ts = self._resolve_timestamp(now)
        self._last_timestamp = ts

        p_score = self._check_paimon(paimon_maybe_img, ui_scale)
        if p_score < self.config.paimon_thresh:
            if p_score < 0.25 and minimap_img is not None and minimap_img.size > 0:
                sample = minimap_img[::4, ::4]
                if float(np.std(sample)) < 12.0 or float(np.mean(sample)) < 22.0 or float(np.mean(sample)) > 238.0:
                    self._long_pause_pending = True
                    self._relocate_tier2_left = 2
            return self.pause(now=ts, reason="paimon_hidden", paimon_score=p_score)

        return self._step_core(minimap_img, p_score, ts, underground_overlay=underground_overlay)

    def step_from_hud(
        self,
        hud_tl_bgr: Optional[np.ndarray],
        frame_w: int,
        frame_h: int,
        *,
        now: Optional[float] = None,
        gamepad_mode: bool = False,
        underground_overlay: Optional[Tuple[np.ndarray, Dict[str, Any], Optional[int], str]] = None,
    ) -> MinimapPose:
        """End-to-end runtime HUD Paimon auto-calibration, pause gate, and minimap tracking."""
        ts = self._resolve_timestamp(now)
        self._last_timestamp = ts

        if hud_tl_bgr is None or hud_tl_bgr.size == 0:
            return self.pause(now=ts, reason="empty_hud", paimon_score=0.0)

        fsize = (int(frame_w), int(frame_h), bool(gamepad_mode))
        if self._hud_frame_size != fsize:
            self._hud_frame_size = fsize
            self._cal_offset_samples.clear()
            self._cal_dx_px = 0
            self._cal_dy_px = 0
            self._cal_locked = False
            self._paimon_last_xy_1080 = None

        lay = self._get_layout(fsize[0], fsize[1], gamepad_mode=fsize[2])
        pmx, pmy, pmw, pmh = lay.paimon_maybe_rect
        pm_crop = hud_tl_bgr[pmy : min(hud_tl_bgr.shape[0], pmy + pmh), pmx : min(hud_tl_bgr.shape[1], pmx + pmw)]
        p_score = self._check_paimon(pm_crop, lay.ui_scale)

        if self._has_paimon_tpl and p_score >= 0.72 and not self._cal_locked and self._paimon_last_xy_1080 is not None:
            px_phys = pmx + int(round(self._paimon_last_xy_1080[0] * lay.ui_scale))
            py_phys = pmy + int(round(self._paimon_last_xy_1080[1] * lay.ui_scale))
            raw_dx = px_phys - lay.paimon_rect[0]
            raw_dy = py_phys - lay.paimon_rect[1]
            if abs(raw_dx) <= int(round(45.0 * lay.ui_scale)) and abs(raw_dy) <= int(round(25.0 * lay.ui_scale)):
                self._cal_offset_samples.append((raw_dx, raw_dy))
                self._cal_dx_px = int(round(float(np.median([s[0] for s in self._cal_offset_samples]))))
                self._cal_dy_px = int(round(float(np.median([s[1] for s in self._cal_offset_samples]))))
                if len(self._cal_offset_samples) >= 3:
                    self._cal_locked = True

        mx, my, mw, mh = lay.minimap_rect
        cx = max(0, min(max(0, hud_tl_bgr.shape[1] - mw), mx + self._cal_dx_px))
        cy = max(0, min(max(0, hud_tl_bgr.shape[0] - mh), my + self._cal_dy_px))
        mm_crop = hud_tl_bgr[cy : min(hud_tl_bgr.shape[0], cy + mh), cx : min(hud_tl_bgr.shape[1], cx + mw)]

        if self._has_paimon_tpl and p_score < self.config.paimon_thresh:
            if mm_crop.size > 0:
                sample = mm_crop[::4, ::4]
                if float(np.std(sample)) < 12.0 or float(np.mean(sample)) < 22.0 or float(np.mean(sample)) > 238.0:
                    self._long_pause_pending = True
                    self._relocate_tier2_left = 2
            return self.pause(now=ts, reason="paimon_hidden", paimon_score=p_score)

        pose = self._step_core(mm_crop, p_score, ts, underground_overlay=underground_overlay)
        if not self._has_paimon_tpl and self._paimon_cache_path is not None:
            if pose.state == STATE_TRACKING and pose.sift_inliers >= 12:
                self._paimon_bootstrap_streak += 1
                if self._paimon_bootstrap_streak >= 3:
                    self._try_bootstrap_paimon(hud_tl_bgr, lay)
            else:
                self._paimon_bootstrap_streak = 0
        return pose

    # Gate stand-in for the heading fast path: always re-extract cone & arrow.
    _FORCE_HEADINGS_GATE = SimpleNamespace(center_diff=float("inf"), frame_diff=float("inf"), alpha_diff=float("inf"))

    def minimap_capture_rect(
        self, frame_w: int, frame_h: int, *, gamepad_mode: bool = False
    ) -> Tuple[int, int, int, int]:
        """Client-area rect `(x, y, w, h)` of the minimap, including the Paimon HUD calibration offset."""
        lay = self._get_layout(int(frame_w), int(frame_h), gamepad_mode=bool(gamepad_mode))
        mx, my, mw, mh = lay.minimap_rect
        return (max(0, mx + self._cal_dx_px), max(0, my + self._cal_dy_px), mw, mh)

    def step_headings_only(
        self, minimap_img: Optional[np.ndarray], *, now: Optional[float] = None
    ) -> Optional[MinimapPose]:
        """
        Heading fast path (~3 ms): refreshes camera / body / view heading from a minimap crop
        without position matching, so the camera can update at 60 Hz while position matching
        runs at a lower rate. Only valid while TRACKING and not paused; returns None otherwise
        (the caller then runs the full `step_from_hud`).
        """
        if minimap_img is None or getattr(minimap_img, "size", 0) == 0 or minimap_img.ndim != 3:
            return None
        if self.fsm.state != STATE_TRACKING or self._pause_start_ts is not None or self._long_pause_pending:
            return None
        sample = minimap_img[::6, ::6, :3]
        mean = float(np.mean(sample))
        if float(np.std(sample)) < 8.0 or mean < 20.0 or mean > 240.0:
            return None  # menu / loading / white-out: let the full step's gate decide
        ts = self._resolve_timestamp(now)
        self._last_timestamp = ts
        self._update_headings(minimap_img, self._FORCE_HEADINGS_GATE, ts)
        return self._build_pose(ts, stationary=True, match_method="heading_fast", should_broadcast_web=False)

    def _step_core(
        self,
        minimap_img: Optional[np.ndarray],
        p_score: float,
        ts: float,
        *,
        underground_overlay: Optional[Tuple[np.ndarray, Dict[str, Any], Optional[int], str]] = None,
    ) -> MinimapPose:
        self.last_match_ms = 0.0
        if self._pause_start_ts is not None:
            if (ts - self._pause_start_ts) >= 1.2:
                self._long_pause_pending = True
                self._relocate_tier2_left = 2
            self._pause_start_ts = None

        # 1. Fast Gate + Cyan Arrow Rescue
        gate_res = self.gate.check(minimap_img, update_keyframe=False)
        self._last_hdr_clipped = gate_res.hdr_clipped
        if (
            not gate_res.valid
            and gate_res.reason == "no_circular_minimap_border"
            and minimap_img is not None
            and gate_res.inner_std >= self.config.gate_min_std
        ):
            _rescue_deg, _rescue_conf = extract_player_arrow_360(minimap_img)
            if _rescue_deg is not None and _rescue_conf >= 0.55:
                gate_res = replace(gate_res, valid=True, reason=None)

        if not gate_res.valid:
            self._prev_phase_gray = None
            self._prev_mm_gray_for_phase = None
            self._sift_interframe_budget = 0
            self._zoom_alt_streak = 0
            if self.config.hold_on_phase_exhausted and self.fsm.state == STATE_TRACKING and self.fsm._x is not None:
                self.fsm.touch_stationary(ts)
                return self._build_pose(
                    ts,
                    stationary=True,
                    match_method="gate_hold",
                    reason=gate_res.reason,
                    should_broadcast_web=False,
                    paimon_score=p_score,
                )
            self._motion_fallback_frames = 0
            _, _, reason = self.fsm.update(PoseMeasurement(found=False, reason=gate_res.reason), ts)
            return self._build_pose(
                ts,
                stationary=False,
                match_method="gate_rejected",
                reason=reason,
                should_broadcast_web=False,
                paimon_score=p_score,
            )

        assert minimap_img is not None
        # 2. Extract 360 headings (preserves <1ms skip when identical frame)
        self._update_headings(minimap_img, gate_res, ts)

        # 3. Stationary fast-path skip (zero drift on 360 camera spin; skip when pending relocation)
        terrain_stationary = gate_res.stationary
        if (
            not terrain_stationary
            and not self._long_pause_pending
            and self.fsm.state == STATE_TRACKING
            and self.fsm._x is not None
            and self._minimap_mode == MODE_LOCK_DIRECTION
            and self._cone_mask_heading is not None
        ):
            terrain_stationary = self.gate.is_terrain_stationary_excluding_cones(self._cone_mask_heading)

        if (
            terrain_stationary
            and not self._long_pause_pending
            and self.fsm.state == STATE_TRACKING
            and self.fsm._x is not None
        ):
            if (
                gate_res.center_diff >= self.config.gate_cone_change_diff
                or gate_res.alpha_diff >= self.config.gate_cone_change_diff
            ):
                self.gate.commit_heading_keyframe(minimap_img)
            self.stationary_skips += 1
            self.fsm.touch_stationary(ts)
            return self._build_pose(
                ts,
                stationary=True,
                match_method="stationary_skip",
                reason=None,
                should_broadcast_web=False,
                paimon_score=p_score,
            )

        self.gate.commit_keyframe(minimap_img, cam_deg=self._cone_mask_heading)

        mm_gray = (
            minimap_img
            if minimap_img.ndim == 2
            else cv2.cvtColor(minimap_img[:, :, :3], cv2.COLOR_BGR2GRAY)
        )
        cone_excl = self._cone_mask_heading if self._minimap_mode == MODE_LOCK_DIRECTION else 0.0

        # If unanchored yet (LOST / ACQUIRING), consult optional global_locator or TeyvatGlobalRelocator
        if self.fsm._x is None or self.fsm._y is None or self.fsm._map_id is None:
            if self.global_locator is not None:
                glob = self.global_locator(minimap_img, self.fsm._map_id)
                if glob is not None:
                    g_mid, g_wx, g_wy, g_conf = glob
                    meas = PoseMeasurement(
                        found=True,
                        map_id=int(g_mid),
                        world_x=float(g_wx),
                        world_y=float(g_wy),
                        confidence=float(g_conf),
                        is_underground=self.fsm._is_underground,
                        floor_id=self.fsm._floor_id,
                        floor_name=self.fsm._floor_name,
                        match_method="global",
                    )
                    acc, _, reason = self.fsm.update(meas, ts)
                    self._prev_mm_gray_for_phase = mm_gray
                    self._prev_cone_excl_for_phase = cone_excl
                    self._prev_phase_gray = None
                    return self._build_pose(
                        ts,
                        stationary=False,
                        match_method="global",
                        reason=reason,
                        should_broadcast_web=bool(acc),
                        paimon_score=p_score,
                    )
            if self.fsm._map_id in (None, 2):
                cold_pose = self._try_global_teyvat_relocate(
                    minimap_img,
                    mm_gray,
                    None,
                    None,
                    min(mm_gray.shape[:2]),
                    ts,
                    p_score,
                    cone_excl,
                    time.perf_counter(),
                )
                if cold_pose is not None:
                    self.matcher_calls += 1
                    return cold_pose
            self._prev_mm_gray_for_phase = mm_gray
            self._prev_cone_excl_for_phase = cone_excl
            self._prev_phase_gray = None
            _, _, reason = self.fsm.update(PoseMeasurement(found=False, reason="no_anchor"), ts)
            return self._build_pose(
                ts,
                stationary=False,
                match_method="none",
                reason=reason,
                should_broadcast_web=False,
                paimon_score=p_score,
            )

        t_match_start = time.perf_counter()
        self.matcher_calls += 1
        map_id = int(self.fsm._map_id)
        anchor_x = float(self.fsm._x)
        anchor_y = float(self.fsm._y)
        is_ug = bool(self.fsm._is_underground)
        floor_id = self.fsm._floor_id

        # Underground without overlay provider (unit-test & offline safety): bounded odometry hold
        if is_ug and self.ug_provider is None and underground_overlay is None:
            curr_phase = self._prepare_phase_patch(mm_gray, exclude_cone_cw_deg=cone_excl)
            phase_res = self._run_phase_correlate_fallback(curr_phase)
            self._prev_phase_gray = curr_phase
            if phase_res is not None:
                p_wx, p_wy, p_conf = phase_res
                step_d = math.hypot(p_wx - anchor_x, p_wy - anchor_y)
                if step_d <= 18.0:
                    meas = PoseMeasurement(
                        found=True,
                        map_id=map_id,
                        world_x=p_wx,
                        world_y=p_wy,
                        confidence=p_conf,
                        is_underground=True,
                        floor_id=floor_id,
                        floor_name=self.fsm._floor_name,
                        match_method="motion",
                    )
                    _, _, reason = self.fsm.update(meas, ts)
                    self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
                    return self._build_pose(
                        ts,
                        stationary=False,
                        match_method="underground_odometry",
                        reason=reason,
                        should_broadcast_web=False,
                        paimon_score=p_score,
                    )
            self.fsm.touch_stationary(ts)
            self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
            return self._build_pose(
                ts,
                stationary=True,
                match_method="underground_hold",
                reason=None,
                should_broadcast_web=False,
                paimon_score=p_score,
            )

        self._layer_probe_tick += 1

        # 4B. Hybrid Inter-Frame Zero-Mean Sobel Fast Path (skips SIFT for K=3 frames after strong lock)
        if (
            self.config.enable_interframe_sobel
            and self._sift_interframe_budget > 0
            and (self._has_paimon_tpl or self._paimon_cache_path is None)
            and self._span_confirmed
            and self._minimap_mode == MODE_LOCK_DIRECTION
            and not self._just_resumed_from_pause
            and not self._long_pause_pending
            and self._consecutive_sift_misses == 0
            and self._pending_layer_score == 0
        ):
            want_z0_fast = bool(self.config.enable_z0_city and not is_ug and self._confirmed_span_world < 195.0)
            z_tier_fast = 0 if want_z0_fast else -1
            cell_fast = self._get_ref_cell(
                map_id, anchor_x, anchor_y, is_underground=is_ug, floor_id=floor_id, zoom_tier=z_tier_fast
            )
            if cell_fast is None and z_tier_fast == 0:
                z_tier_fast = -1
                cell_fast = self._get_ref_cell(
                    map_id, anchor_x, anchor_y, is_underground=is_ug, floor_id=floor_id, zoom_tier=-1
                )
            self._sift_interframe_budget -= 1
            if cell_fast is not None:
                fast_res = self._match_sobel_fast(
                    mm_gray,
                    cell_fast,
                    anchor_x,
                    anchor_y,
                    self._confirmed_span_world,
                    search_rad_w=18.0,
                    pre_rotate_cw_deg=0.0,
                    exclude_cone_cw_deg=cone_excl,
                )
                if (
                    fast_res is not None
                    and fast_res[2] >= self.config.interframe_sobel_min_corr
                    and math.hypot(fast_res[0] - anchor_x, fast_res[1] - anchor_y) <= 24.0
                ):
                    wx, wy, sc = fast_res
                    self._last_zoom_tier = z_tier_fast
                    self._motion_fallback_frames = 0
                    self._view_heading = self._camera_heading
                    meas = PoseMeasurement(
                        found=True,
                        map_id=map_id,
                        world_x=wx,
                        world_y=wy,
                        confidence=float(max(0.0, min(1.0, sc))),
                        is_underground=is_ug,
                        floor_id=floor_id,
                        floor_name=self.fsm._floor_name,
                        match_method="hd_sobel_fast",
                    )
                    acc, _, reason = self.fsm.update(meas, ts)
                    self._prev_mm_gray_for_phase = mm_gray
                    self._prev_cone_excl_for_phase = cone_excl
                    self._prev_phase_gray = None
                    self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
                    return self._build_pose(
                        ts,
                        stationary=False,
                        match_method="hd_sobel_fast",
                        reason=reason,
                        should_broadcast_web=bool(acc),
                        paimon_score=p_score,
                    )
            self._sift_interframe_budget = 0

        # 5. Primary: Single-pass Local-Cell SIFT (128x128)
        kp_m, des_m, mm_sz, _ = self._extract_mm_sift(minimap_img, sz=128)
        wide_search = self._just_resumed_from_pause or self._consecutive_sift_misses >= 2
        max_dist = 320.0 if wide_search else 85.0
        want_z0 = bool(
            self.config.enable_z0_city
            and not is_ug
            and not wide_search
            and not self._long_pause_pending
            and self._span_confirmed
            and self._confirmed_span_world < 195.0
        )
        z_tier = 0 if want_z0 else -1
        cell = self._get_ref_cell(
            map_id, anchor_x, anchor_y, is_underground=is_ug, floor_id=floor_id, zoom_tier=z_tier
        )
        if cell is None and z_tier == 0:
            z_tier = -1
            cell = self._get_ref_cell(
                map_id, anchor_x, anchor_y, is_underground=is_ug, floor_id=floor_id, zoom_tier=-1
            )

        sift_res = None
        if cell is not None:
            cand = self._match_sift_feats(kp_m, des_m, mm_sz, cell, anchor_x, anchor_y, max_dist_w=max_dist)
            if cand is not None:
                c_dist = math.hypot(cand[0] - anchor_x, cand[1] - anchor_y)
                if c_dist <= 85.0 or (self._just_resumed_from_pause and cand[2] >= 7) or cand[2] >= 9:
                    sift_res = cand

        if sift_res is None and z_tier == 0:
            z_tier = -1
            cell_n1 = self._get_ref_cell(
                map_id, anchor_x, anchor_y, is_underground=is_ug, floor_id=floor_id, zoom_tier=-1
            )
            if cell_n1 is not None:
                cell = cell_n1
                cand = self._match_sift_feats(kp_m, des_m, mm_sz, cell_n1, anchor_x, anchor_y, max_dist_w=max_dist)
                if cand is not None:
                    c_dist = math.hypot(cand[0] - anchor_x, cand[1] - anchor_y)
                    if c_dist <= 85.0 or cand[2] >= 9:
                        sift_res = cand

        self._last_zoom_tier = z_tier

        # 5B. Automatic Surface <-> Underground floor switch probe (leaky-bucket hysteresis)
        cur_ninl = sift_res[2] if sift_res is not None else 0
        self._last_sift_inliers = cur_ninl
        if sift_res is not None and cur_ninl >= 10:
            self._pending_layer_key = None
            self._pending_layer_score = 0
        elif (
            self.ug_provider is not None
            and self.config.enable_auto_layer
            and (
                (
                    sift_res is None
                    and (
                        self._just_resumed_from_pause
                        or self._consecutive_sift_misses >= 1
                        or self._pending_layer_score > 0
                    )
                )
                or (self._layer_probe_tick % 12 == 0 and cur_ninl < 10)
            )
        ):
            layer_pose = self._probe_candidate_layers(
                kp_m,
                des_m,
                mm_sz,
                mm_gray,
                cone_excl,
                map_id,
                anchor_x,
                anchor_y,
                is_ug,
                floor_id,
                cur_ninl,
                max_dist,
                self._just_resumed_from_pause,
                ts,
                p_score,
            )
            if layer_pose is not None:
                self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
                return layer_pose

        # 5C. Tier-2 Post-Loading Neighbor Probing (+-380w)
        tier2_reacquire = False
        if sift_res is None and self._long_pause_pending and self._relocate_tier2_left > 0:
            self._relocate_tier2_left -= 1
            cand_t2 = self._probe_tier2_neighbors(kp_m, des_m, mm_sz, map_id, anchor_x, anchor_y, is_ug, floor_id)
            if cand_t2 is not None:
                sift_res = cand_t2
                tier2_reacquire = True

        if sift_res is not None:
            wx, wy, ninl, span_w, theta_map = sift_res
            dist_from_anchor = math.hypot(wx - anchor_x, wy - anchor_y)
            was_resumed = self._just_resumed_from_pause
            self._motion_fallback_frames = 0
            self._consecutive_sift_misses = 0
            self._just_resumed_from_pause = False
            self._resume_frames_left = 0
            self._long_pause_pending = False
            self._relocate_tier2_left = 0
            self._global_vote_bins.clear()
            self._global_query_count = 0

            self._update_zoom_from_span(span_w)
            self._apply_rotation_mode(theta_map, ninl)
            self._prev_mm_gray_for_phase = mm_gray
            self._prev_cone_excl_for_phase = cone_excl
            self._prev_phase_gray = None

            if (was_resumed and dist_from_anchor > 55.0 and ninl >= 7) or tier2_reacquire:
                self._sift_interframe_budget = 0
                self.fsm.set_anchor(
                    map_id=map_id,
                    world_x=wx,
                    world_y=wy,
                    timestamp=ts,
                    is_underground=is_ug,
                    floor_id=floor_id,
                    floor_name=self.fsm._floor_name,
                )
                self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
                return self._build_pose(
                    ts,
                    stationary=False,
                    match_method="hd_sift_tier2" if tier2_reacquire else "hd_sift_reacquire",
                    reason="tier2_reacquire" if tier2_reacquire else "post_pause_reacquire",
                    should_broadcast_web=True,
                    paimon_score=p_score,
                    sift_inliers=ninl,
                )

            conf = float(min(0.98, 0.45 + 0.04 * ninl))
            meas = PoseMeasurement(
                found=True,
                map_id=map_id,
                world_x=wx,
                world_y=wy,
                confidence=conf,
                is_underground=is_ug,
                floor_id=floor_id,
                floor_name=self.fsm._floor_name,
                match_method="hd_sift",
            )
            acc, _, reason = self.fsm.update(meas, ts)
            should_bc = bool(acc and ninl >= self.config.broadcast_min_sift_inliers)
            if (
                acc
                and ninl >= 10
                and self._minimap_mode == MODE_LOCK_DIRECTION
                and self._span_confirmed
                and self.config.enable_interframe_sobel
            ):
                self._sift_interframe_budget = self.config.interframe_sobel_k
            else:
                self._sift_interframe_budget = 0
            self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
            return self._build_pose(
                ts,
                stationary=False,
                match_method="hd_sift",
                reason=reason,
                should_broadcast_web=should_bc,
                paimon_score=p_score,
                sift_inliers=ninl,
            )

        self._sift_interframe_budget = 0
        self._consecutive_sift_misses += 1
        if self._resume_frames_left > 0:
            self._resume_frames_left -= 1
            if self._resume_frames_left == 0:
                self._just_resumed_from_pause = False

        # 5D. Safe Relocating Guard + Tier-3 Full-Teyvat Global Auto-Relocate (no Map M)
        if self.config.enable_relocating_guard and self._long_pause_pending:
            strict_sobel = None
            if cell is not None and self._span_confirmed:
                strict_sobel = self._match_sobel_fast(
                    mm_gray,
                    cell,
                    anchor_x,
                    anchor_y,
                    self._confirmed_span_world,
                    search_rad_w=24.0,
                    pre_rotate_cw_deg=(-self._map_rotation_deg if self._minimap_mode == MODE_FOLLOW_VIEW else 0.0),
                    exclude_cone_cw_deg=(0.0 if self._minimap_mode == MODE_FOLLOW_VIEW else self._cone_mask_heading),
                )
            if strict_sobel is not None and strict_sobel[2] >= 0.58:
                wx, wy, sc = strict_sobel
                self._long_pause_pending = False
                self._relocate_tier2_left = 0
                self._just_resumed_from_pause = False
                self._motion_fallback_frames = 0
                self._global_vote_bins.clear()
                self._global_query_count = 0
                meas = PoseMeasurement(
                    found=True,
                    map_id=map_id,
                    world_x=wx,
                    world_y=wy,
                    confidence=float(min(1.0, sc)),
                    is_underground=is_ug,
                    floor_id=floor_id,
                    floor_name=self.fsm._floor_name,
                    match_method="hd_sobel_resume",
                )
                acc, _, reason = self.fsm.update(meas, ts)
                self._prev_mm_gray_for_phase = mm_gray
                self._prev_cone_excl_for_phase = cone_excl
                self._prev_phase_gray = None
                self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
                return self._build_pose(
                    ts,
                    stationary=False,
                    match_method="hd_sobel",
                    reason=reason,
                    should_broadcast_web=bool(acc),
                    paimon_score=p_score,
                )

            if map_id == 2:
                glob_pose = self._try_global_teyvat_relocate(
                    minimap_img,
                    mm_gray,
                    kp_m,
                    des_m,
                    mm_sz,
                    ts,
                    p_score,
                    cone_excl,
                    t_match_start,
                )
                if glob_pose is not None:
                    return glob_pose

            self._prev_phase_gray = None
            self._prev_mm_gray_for_phase = None
            self.fsm.touch_stationary(ts)
            self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
            return self._build_pose(
                ts,
                stationary=False,
                match_method="relocating_hold",
                reason="teleport_relocating_hold",
                should_broadcast_web=False,
                paimon_score=p_score,
            )

        # 6. Secondary Fallback: Fast Zero-Mean Sobel on HD / Cave Overlay
        sobel_res = None
        if cell is not None:
            probe_spans = (self._confirmed_span_world,) if self._span_confirmed else (180.0, 360.0)
            for p_span in probe_spans:
                cand_s = self._match_sobel_fast(
                    mm_gray,
                    cell,
                    anchor_x,
                    anchor_y,
                    p_span,
                    search_rad_w=42.0,
                    pre_rotate_cw_deg=(-self._map_rotation_deg if self._minimap_mode == MODE_FOLLOW_VIEW else 0.0),
                    exclude_cone_cw_deg=(0.0 if self._minimap_mode == MODE_FOLLOW_VIEW else self._cone_mask_heading),
                )
                if cand_s is not None and (sobel_res is None or cand_s[2] > sobel_res[2]):
                    sobel_res = cand_s
                    if not self._span_confirmed and cand_s[2] >= self.config.sobel_min_corr:
                        self._confirmed_span_world = p_span
        if sobel_res is not None and sobel_res[2] >= self.config.sobel_min_corr:
            wx, wy, sc = sobel_res
            self._motion_fallback_frames = 0
            self._view_heading = (
                wrap_angle_360(-self._map_rotation_deg)
                if self._minimap_mode == MODE_FOLLOW_VIEW
                else self._camera_heading
            )
            meas = PoseMeasurement(
                found=True,
                map_id=map_id,
                world_x=wx,
                world_y=wy,
                confidence=float(max(0.0, min(1.0, sc))),
                is_underground=is_ug,
                floor_id=floor_id,
                floor_name=self.fsm._floor_name,
                match_method="hd_sobel",
            )
            acc, _, reason = self.fsm.update(meas, ts)
            self._prev_mm_gray_for_phase = mm_gray
            self._prev_cone_excl_for_phase = cone_excl
            self._prev_phase_gray = None
            should_bc = bool(acc and sc >= self.config.broadcast_min_sobel_corr)
            self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
            return self._build_pose(
                ts,
                stationary=False,
                match_method="hd_sobel",
                reason=reason,
                should_broadcast_web=should_bc,
                paimon_score=p_score,
            )

        # 6B. Legacy z=-2 Annulus / ORB fallback when HD Sobel misses (covers synthetic blurred/city regimes)
        if not is_ug:
            l_wx, l_wy, l_corr, l_method, zoom_pending = self._legacy_annulus_or_orb_match(
                mm_gray, map_id, anchor_x, anchor_y
            )
            template_plausible = (
                l_wx is not None
                and l_wy is not None
                and l_corr >= self.config.template_min_corr
                and (l_corr >= 0.65 or math.hypot(l_wx - anchor_x, l_wy - anchor_y) <= 45.0)
            )
            if template_plausible:
                self._motion_fallback_frames = 0
                meas = PoseMeasurement(
                    found=True,
                    map_id=map_id,
                    world_x=l_wx,
                    world_y=l_wy,
                    confidence=float(max(0.0, min(1.0, l_corr))),
                    is_underground=is_ug,
                    floor_id=floor_id,
                    floor_name=self.fsm._floor_name,
                    match_method=l_method,
                )
                acc, _, reason = self.fsm.update(meas, ts)
                self._prev_mm_gray_for_phase = mm_gray
                self._prev_cone_excl_for_phase = cone_excl
                self._prev_phase_gray = None
                self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
                return self._build_pose(
                    ts,
                    stationary=False,
                    match_method=l_method,
                    reason=reason,
                    should_broadcast_web=bool(acc),
                    paimon_score=p_score,
                )
            if zoom_pending and l_corr < self.config.template_min_corr:
                self._prev_mm_gray_for_phase = mm_gray
                self._prev_cone_excl_for_phase = cone_excl
                self._prev_phase_gray = None
                _, _, reason = self.fsm.update(PoseMeasurement(found=False, reason="zoom_switch_pending"), ts)
                self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
                return self._build_pose(
                    ts,
                    stationary=False,
                    match_method="zoom_switch_pending",
                    reason=reason,
                    should_broadcast_web=False,
                    paimon_score=p_score,
                )

        # 7. Bounded phaseCorrelate fallback (freeze while layer switch pending!)
        if self._pending_layer_score > 0 and cur_ninl == 0:
            self._prev_phase_gray = None
            self._prev_mm_gray_for_phase = None
            self.fsm.touch_stationary(ts)
            self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
            return self._build_pose(
                ts,
                stationary=False,
                match_method="anchor_hold",
                reason="pending_layer_hold",
                should_broadcast_web=False,
                paimon_score=p_score,
            )

        if self._prev_phase_gray is None and self._prev_mm_gray_for_phase is not None:
            self._prev_phase_gray = self._prepare_phase_patch(
                self._prev_mm_gray_for_phase, exclude_cone_cw_deg=self._prev_cone_excl_for_phase
            )
        curr_phase = self._prepare_phase_patch(mm_gray, exclude_cone_cw_deg=cone_excl)
        self._prev_mm_gray_for_phase = mm_gray
        self._prev_cone_excl_for_phase = cone_excl

        if self._motion_fallback_frames >= self.config.max_phase_fallback_frames:
            self._prev_phase_gray = curr_phase
            self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
            if self.config.hold_on_phase_exhausted:
                self.fsm.touch_stationary(ts)
                return self._build_pose(
                    ts,
                    stationary=False,
                    match_method="anchor_hold",
                    reason="low_texture_hold",
                    should_broadcast_web=False,
                    paimon_score=p_score,
                )
            self.fsm.force_lost(ts)
            return self._build_pose(
                ts,
                stationary=False,
                match_method="phase_budget_exhausted",
                reason="motion_fallback_budget_exceeded",
                should_broadcast_web=False,
                paimon_score=p_score,
            )

        phase_res = self._run_phase_correlate_fallback(curr_phase)
        self._prev_phase_gray = curr_phase
        if phase_res is not None:
            p_wx, p_wy, p_conf = phase_res
            self._motion_fallback_frames += 1
            match_method = "motion"
            measurement = PoseMeasurement(
                found=True,
                map_id=map_id,
                world_x=p_wx,
                world_y=p_wy,
                confidence=p_conf,
                is_underground=is_ug,
                floor_id=floor_id,
                floor_name=self.fsm._floor_name,
                match_method="motion",
            )
            _, _, reason = self.fsm.update(measurement, ts)
        elif self.config.hold_on_phase_exhausted and self.fsm.state == STATE_TRACKING and self.fsm._x is not None:
            self.fsm.touch_stationary(ts)
            match_method = "anchor_hold"
            reason = "low_texture_hold"
        else:
            match_method = "none"
            _, _, reason = self.fsm.update(
                PoseMeasurement(found=False, reason="template_and_phase_failed"), ts
            )

        # 8. Optional explicit multi-layer floor comparison when caller provides an overlay tuple
        if (
            underground_overlay is not None
            and self.fsm.state == STATE_TRACKING
            and self.fsm._x is not None
            and self.fsm._y is not None
            and self.fsm._map_id is not None
        ):
            ug_img, ug_bounds, ug_fid, ug_fname = underground_overlay
            span_w = self._confirmed_span_world if self._span_confirmed else self._span_for_regime(self._zoom_regime)
            half_e = 0.5 * span_w
            cmp_ppu = 0.5
            surf_p, _ = self.tile_cache.extract_local_patch(
                int(self.fsm._map_id), self.fsm._x, self.fsm._y, half_e, target_px_per_unit=cmp_ppu
            )
            ug_p, _ = self.tile_cache.extract_underground_patch(
                ug_img, ug_bounds, self.fsm._x, self.fsm._y, half_e, target_px_per_unit=cmp_ppu
            )
            if surf_p is not None and ug_p is not None:
                self.compare_surface_vs_underground(
                    minimap_img,
                    surf_p,
                    ug_p,
                    candidate_floor_id=ug_fid,
                    candidate_floor_name=ug_fname,
                    patch_px_per_unit=cmp_ppu,
                    apply_hysteresis=True,
                )

        self.last_match_ms = (time.perf_counter() - t_match_start) * 1000.0
        return self._build_pose(
            ts,
            stationary=False,
            match_method=match_method,
            reason=reason,
            should_broadcast_web=False,
            paimon_score=p_score,
        )


# ---------------------------------------------------------------------------
# Exponential Backoff Scheduler & Single-Owner Daemon Worker
# (Ported from MaxWay12/Genshin-Navigator@99e1e13:performance.py & live_worker.py MIT)
# ---------------------------------------------------------------------------
class LocalizationScheduler:
    """
    Throttles localization cadence with exponential backoff when LOST.
    Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/performance.py (MIT License).
    """

    def __init__(
        self,
        *,
        tracking_interval: float = 0.10,
        lost_base_interval: float = 0.25,
        lost_max_interval: float = 2.0,
        backoff_factor: float = 1.45,
    ):
        self.tracking_interval = float(tracking_interval)
        self.lost_base_interval = float(lost_base_interval)
        self.lost_max_interval = float(lost_max_interval)
        self.backoff_factor = float(backoff_factor)
        self._last_run_at: Optional[float] = None
        self._consecutive_misses: int = 0

    @property
    def consecutive_misses(self) -> int:
        return self._consecutive_misses

    def interval_for(self, state: str) -> float:
        if state == STATE_TRACKING:
            return self.tracking_interval
        backoff = self.lost_base_interval * (self.backoff_factor ** min(self._consecutive_misses, 8))
        return min(backoff, self.lost_max_interval)

    def due(self, now: float, state: str) -> bool:
        return bool(
            self._last_run_at is None
            or (now - self._last_run_at + 1e-9) >= self.interval_for(state)
        )

    def mark_run(self, now: float) -> None:
        self._last_run_at = float(now)

    def observe_result(self, *, found: bool, state: str) -> None:
        if found or state == STATE_TRACKING:
            self._consecutive_misses = 0
        else:
            self._consecutive_misses += 1

    def force_next(self) -> None:
        self._last_run_at = None
        self._consecutive_misses = 0


@dataclass(frozen=True)
class LiveResult:
    """Latest-result mailbox payload published by `LocalizationWorker`."""

    pose: MinimapPose
    captured_at: float
    processing_ms: float
    generation: int
    search_mode: str


def visible_snapshot(
    result: Optional[LiveResult],
    now: Optional[float] = None,
    max_age: float = 1.5,
) -> Optional[MinimapPose]:
    """
    Returns the pose if fresh (`now - captured_at <= max_age`), else clamps stale results to LOST.
    Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/live_worker.py (MIT License).
    """
    if result is None:
        return None
    ts = time.perf_counter() if now is None else float(now)
    if (ts - result.captured_at) > float(max_age):
        return replace(
            result.pose,
            state=STATE_LOST,
            world_x=None,
            world_y=None,
            lat=None,
            lng=None,
            confidence=0.0,
            reason="capture_expired",
        )
    return result.pose


class LocalizationWorker:
    """
    Single-owner background daemon thread with latest-result mailbox, `_generation` cancellation
    on pause/stop, `visible_snapshot(max_age)` staleness clamping, and LOST exponential backoff.

    Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/live_worker.py (MIT License).
    """

    def __init__(
        self,
        tracker: HybridMinimapTracker,
        capture_fn: Callable[[], Optional[np.ndarray]],
        *,
        scheduler: Optional[LocalizationScheduler] = None,
        max_age: float = 1.5,
        clock: Callable[[], float] = time.perf_counter,
        poll_sleep: float = 0.005,
    ):
        self.tracker = tracker
        self.capture_fn = capture_fn
        self.scheduler = scheduler or LocalizationScheduler()
        self.max_age = float(max_age)
        self._clock = clock
        self._poll_sleep = max(0.001, float(poll_sleep))

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._generation: int = 0
        self._paused: bool = False
        self._latest: Optional[LiveResult] = None
        self._error: Optional[BaseException] = None
        self._pending_anchor: Optional[Tuple[int, float, float, bool, Optional[int], str]] = None
        self._samples_ms: deque[float] = deque(maxlen=60)
        self._thread = threading.Thread(
            target=self._run,
            name="genshin-helper-minimap-worker",
            daemon=True,
        )

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            p = bool(paused)
            if p != self._paused:
                self._paused = p
                self._generation += 1
                self._latest = None

    def anchor(
        self,
        map_id: int,
        world_x: float,
        world_y: float,
        *,
        is_underground: bool = False,
        floor_id: Optional[int] = None,
        floor_name: str = "G",
    ) -> None:
        """Queues a Map M anchor to be applied safely on the single-owner worker thread."""
        with self._lock:
            self._pending_anchor = (
                int(map_id),
                float(world_x),
                float(world_y),
                bool(is_underground),
                floor_id,
                str(floor_name),
            )
            self._generation += 1

    def latest(self) -> Optional[LiveResult]:
        with self._lock:
            if self._error is not None:
                raise RuntimeError("Localization worker failed") from self._error
            return self._latest

    def visible_snapshot(
        self,
        now: Optional[float] = None,
        max_age: Optional[float] = None,
    ) -> Optional[MinimapPose]:
        res = self.latest()
        ts = self._clock() if now is None else float(now)
        age = self.max_age if max_age is None else float(max_age)
        return visible_snapshot(res, now=ts, max_age=age)

    def stop(self, timeout: float = 0.5) -> None:
        """Signals daemon thread to stop, discards in-flight results, and joins briefly."""
        self._stop.set()
        with self._lock:
            self._generation += 1
            self._latest = None
        if self._thread.is_alive():
            self._thread.join(timeout=float(timeout))

    def close(self, timeout: float = 0.5) -> None:
        self.stop(timeout=timeout)

    def _run(self) -> None:
        seen_generation = -1
        was_paused = False
        try:
            while not self._stop.is_set():
                with self._lock:
                    current = self._generation
                    paused = self._paused
                    pending_anchor = self._pending_anchor
                    self._pending_anchor = None

                if pending_anchor is not None:
                    now_ts = self._clock()
                    mid, wx, wy, is_ug, fid, fname = pending_anchor
                    pose = self.tracker.set_map_m_anchor(
                        mid, wx, wy, is_underground=is_ug, floor_id=fid, floor_name=fname, timestamp=now_ts
                    )
                    self.scheduler.force_next()
                    seen_generation = current
                    with self._lock:
                        if not self._stop.is_set() and current == self._generation and not self._paused:
                            self._latest = LiveResult(
                                pose=pose,
                                captured_at=now_ts,
                                processing_ms=0.0,
                                generation=current,
                                search_mode="anchor",
                            )

                if paused:
                    if not was_paused:
                        self.tracker.pause(self._clock(), reason="worker_paused")
                        was_paused = True
                    self._stop.wait(0.02)
                    continue

                if was_paused:
                    self.tracker.pause(self._clock(), reason="worker_resumed")
                    self.scheduler.force_next()
                    was_paused = False

                if current != seen_generation:
                    self.scheduler.force_next()
                    seen_generation = current

                now_due = self._clock()
                if not self.scheduler.due(now_due, self.tracker.state):
                    self._stop.wait(self._poll_sleep)
                    continue

                started = time.perf_counter()
                frame = self.capture_fn()
                captured_at = self._clock()
                self.scheduler.mark_run(captured_at)

                if frame is None:
                    pose = self.tracker.pause(captured_at, reason="no_capture_frame")
                    search_mode = "paused"
                    found = False
                else:
                    # If minimap is hidden (menu / Map M), freeze tracker time via pause() (Genshin-Navigator pattern)
                    gate_peek = self.tracker.gate.check(frame, update_keyframe=False)
                    if not gate_peek.valid:
                        pose = self.tracker.pause(captured_at, reason=gate_peek.reason or "minimap_not_visible")
                        search_mode = "gate_pause"
                        found = False
                    else:
                        pose = self.tracker.step(frame, now=captured_at)
                        search_mode = pose.match_method
                        found = pose.state == STATE_TRACKING and pose.world_x is not None

                self.scheduler.observe_result(found=found, state=pose.state)
                proc_ms = (time.perf_counter() - started) * 1000.0
                self._samples_ms.append(proc_ms)

                with self._lock:
                    if self._stop.is_set() or current != self._generation or self._paused:
                        continue
                    self._latest = LiveResult(
                        pose=pose,
                        captured_at=captured_at,
                        processing_ms=round(proc_ms, 3),
                        generation=current,
                        search_mode=search_mode,
                    )
        except BaseException as exc:
            with self._lock:
                self._error = exc
