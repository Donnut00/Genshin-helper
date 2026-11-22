"""
360-Degree Camera View Cone & Player Arrow Heading Extractor for Genshin-Helper v5.

Ported from GengGode/cvAutoTrack@336a4a6:source/algorithms/algorithms.rotation.h & algorithms.direction.h (MIT License)
Copyright (c) GengGode / TianLi cvAutoTrack contributors.
See THIRD_PARTY_NOTICES for full MIT License text.

Unified Angle Convention across Genshin-Helper:
    - 0.0 deg   = North (Up, -Y in image and HoYoLAB world coordinates)
    - 90.0 deg  = East  (Right, +X)
    - 180.0 deg = South (Down, +Y)
    - 270.0 deg = West  (Left, -X)
    - Clockwise in [0.0, 360.0), matching src/locator.py:562 and Leaflet CSS rotate().
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np

# Polar annulus dimensions (512 angular bins x 76 radial bins; ~4x smaller warp than 512x512)
_POLAR_BINS = 512
_RAD_BINS = 128
_RAD_R0 = 26   # round(0.20 * 128)
_RAD_R1 = 102  # round(0.80 * 128)

# Genshin minimap white camera view cone alpha-blend factor & uint8 SIMD helpers
_CONE_BLEND_ALPHA = 0.25
_CONE_ROLL_SHIFT = 5
_CONE_TOL_SQ = 0.001
_MORPH_RAD_2 = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1))
_LUT_PRED_U8 = np.clip(np.round(np.arange(256) * 0.75 + 63.75), 0, 255).astype(np.uint8)
_ANG_IDXS = np.arange(_POLAR_BINS)

# Cyan player arrow HSV fallback bounds (resilient to FSR2 / brightness / HDR)
_CYAN_HSV_LOWER = np.array([85, 160, 180], dtype=np.uint8)
_CYAN_HSV_UPPER = np.array([105, 255, 255], dtype=np.uint8)


def wrap_angle_360(deg: float) -> float:
    """Normalizes any angle in degrees into [0.0, 360.0)."""
    val = float(deg) % 360.0
    if val == 360.0 or val == 0.0:
        return 0.0
    return val


def shortest_angle_delta(from_deg: float, to_deg: float) -> float:
    """
    Returns signed shortest angular delta (to_deg - from_deg) in [-180.0, 180.0].
    Positive = clockwise (turn right), negative = counter-clockwise (turn left).
    """
    delta = (float(to_deg) - float(from_deg) + 180.0) % 360.0 - 180.0
    if delta == -180.0:
        return 180.0
    return 0.0 if delta == 0.0 else float(delta)


def _circular_boxcar_5(vec: np.ndarray) -> np.ndarray:
    """Wrap-safe 5-tap boxcar smoothing over a 1D circular array (length 512) without tile/roll."""
    v = np.asarray(vec, dtype=np.float32).ravel()
    p = np.concatenate((v[-2:], v, v[:2]))
    return (
        p[0:_POLAR_BINS]
        + p[1 : _POLAR_BINS + 1]
        + p[2 : _POLAR_BINS + 2]
        + p[3 : _POLAR_BINS + 3]
        + p[4 : _POLAR_BINS + 4]
    ) * np.float32(0.2)


def _circular_subpixel_peak(arr: np.ndarray, idx: int) -> float:
    """Parabolic sub-bin refinement around a circular 1D maximum."""
    n = arr.shape[0]
    y_prev = float(arr[(idx - 1) % n])
    y_curr = float(arr[idx])
    y_next = float(arr[(idx + 1) % n])
    denom = y_prev - 2.0 * y_curr + y_next
    if abs(denom) > 1e-9:
        offset = 0.5 * (y_prev - y_next) / denom
        offset = max(-0.5, min(0.5, offset))
    else:
        offset = 0.0
    return float((idx + offset) % n)


def _extract_cone_bgra_alpha_polar(alpha_channel: np.ndarray) -> Optional[Tuple[float, float]]:
    """Tier 1: Fast BGRA alpha-channel Scharr Y path on 512x76 polar annulus."""
    a_min = int(alpha_channel.min())
    a_max = int(alpha_channel.max())
    if a_min >= 255 or a_max <= a_min:
        return None

    h, w = alpha_channel.shape[:2]
    center = (w * 0.5, h * 0.5)
    max_rad = min(h, w) * 0.5
    polar_a = cv2.warpPolar(
        alpha_channel,
        (_RAD_BINS, _POLAR_BINS),
        center,
        max_rad,
        cv2.WARP_POLAR_LINEAR | cv2.INTER_LINEAR,
    )
    ann_a = polar_a[:, _RAD_R0:_RAD_R1]
    h_rad = ann_a.shape[1]
    padded = cv2.copyMakeBorder(ann_a, 2, 2, 0, 0, cv2.BORDER_WRAP)
    grad_y = cv2.Scharr(padded, cv2.CV_32FC1, 0, 1)[2:-2, :]
    col_sum = _circular_boxcar_5(np.sum(grad_y, axis=1, dtype=np.float32))
    min_idx = int(np.argmin(col_sum))
    max_idx = int(np.argmax(col_sum))
    min_val = float(col_sum[min_idx])
    max_val = float(col_sum[max_idx])

    min_edge = 16.0 * h_rad * 4.0
    if abs(min_val) < min_edge or abs(max_val) < min_edge:
        return None

    ang_min = (_circular_subpixel_peak(-col_sum, min_idx) / _POLAR_BINS) * 360.0
    ang_max = (_circular_subpixel_peak(col_sum, max_idx) / _POLAR_BINS) * 360.0
    span_low = (ang_max - ang_min) % 360.0
    span_high = (ang_min - ang_max) % 360.0

    if abs(span_low - 90.0) <= 15.0:
        mid_polar = ang_min + span_low * 0.5
        span_err = abs(span_low - 90.0)
    elif abs(span_high - 90.0) <= 15.0:
        mid_polar = ang_max + span_high * 0.5
        span_err = abs(span_high - 90.0)
    else:
        return None

    heading_cw = wrap_angle_360(mid_polar + 90.0)
    strength = min(abs(min_val), abs(max_val)) / (16.0 * h_rad * 40.0)
    fov_sc = max(0.0, 1.0 - span_err / 20.0)
    conf = float(min(0.99, max(0.55, 0.55 + 0.30 * min(1.0, strength) + 0.14 * fov_sc)))
    return heading_cw, conf


def _extract_cone_bgr_blend_simd(
    ann: np.ndarray,
    v_prof: np.ndarray,
) -> Optional[Tuple[float, float]]:
    """Tier 2: Vectorized uint8 LUT + cv2.absdiff alpha=0.25 blend edge detector + wedge brightness guard."""
    pred_u8 = cv2.LUT(ann, _LUT_PRED_U8)
    pad_u8 = np.concatenate((ann[-_CONE_ROLL_SHIFT:], ann, ann[:_CONE_ROLL_SHIFT]), axis=0)
    ann_gray = cv2.cvtColor(ann, cv2.COLOR_BGR2GRAY)
    pad_gray = np.concatenate((ann_gray[-_CONE_ROLL_SHIFT:], ann_gray, ann_gray[:_CONE_ROLL_SHIFT]), axis=0)
    ann_bright = cv2.add(ann_gray, 3)
    h_rad = ann.shape[1]

    angles: list[float] = []
    pvals: list[float] = []
    snrs: list[float] = []
    for off in (2 * _CONE_ROLL_SHIFT, 0):  # 10 == roll(-5), 0 == roll(+5)
        sh = pad_u8[off : off + _POLAR_BINS]
        sh_g = pad_gray[off : off + _POLAR_BINS]
        ad = cv2.absdiff(pred_u8, sh)
        m_bgr = (ad[:, :, 0] <= 8) & (ad[:, :, 1] <= 8) & (ad[:, :, 2] <= 8) & (sh_g > ann_bright)
        opened = cv2.morphologyEx(m_bgr.view(np.uint8) * np.uint8(255), cv2.MORPH_OPEN, _MORPH_RAD_2)
        cs = _circular_boxcar_5(np.sum(opened, axis=1, dtype=np.float32))
        pk_i = int(np.argmax(cs))
        pk = float(cs[pk_i])
        mn = float(np.mean(cs))
        if pk < 0.04 * h_rad * 255.0 or pk < 2.2 * (mn + 1e-3):
            return None
        angles.append((_circular_subpixel_peak(cs, pk_i) / _POLAR_BINS) * 360.0)
        pvals.append(pk / (h_rad * 255.0))
        snrs.append(pk / (mn + 1e-3))

    span = (angles[1] - angles[0]) % 360.0
    if abs(span - 90.0) > 20.0:
        return None

    mid_polar = angles[0] + span * 0.5
    mid_bin = int(round((mid_polar % 360.0) / 360.0 * _POLAR_BINS)) % _POLAR_BINS
    cd = np.minimum((_ANG_IDXS - mid_bin) % _POLAR_BINS, (mid_bin - _ANG_IDXS) % _POLAR_BINS)
    inside_v = float(np.mean(v_prof[cd <= 56]))
    outside_v = float(np.mean(v_prof[(cd >= 72) & (cd <= 112)]))
    if inside_v <= outside_v + 0.01:
        return None

    heading_cw = wrap_angle_360(mid_polar + 90.0)
    fill_sc = min(1.0, min(pvals) / 0.35)
    snr_sc = min(1.0, (min(snrs) - 2.0) / 10.0)
    fov_sc = max(0.0, 1.0 - abs(span - 90.0) / 25.0)
    conf = float(min(0.95, max(0.45, 0.45 + 0.25 * fill_sc + 0.15 * snr_sc + 0.10 * fov_sc)))
    return heading_cw, conf


def _extract_cone_hsv_adaptive_polar(
    hsv: np.ndarray,
    v: np.ndarray,
    v_prof: np.ndarray,
) -> Optional[Tuple[float, float]]:
    """Tier 3: Adaptive HSV/Value paired radial-gradient fallback (zero np.roll allocation)."""
    contrast = v - hsv[:, :, 1].astype(np.float32) * np.float32(0.25 / 255.0)
    padded = cv2.copyMakeBorder(contrast, 2, 2, 0, 0, cv2.BORDER_WRAP)
    gy = cv2.Scharr(padded, cv2.CV_32FC1, 0, 1)[2:-2, :] * np.float32(1.0 / 16.0)
    g = _circular_boxcar_5(np.mean(np.clip(gy, -0.35, 0.35), axis=1, dtype=np.float32))
    g_pad = np.concatenate((g[-68:], g, g[:68]))

    best_score = np.zeros(_POLAR_BINS, dtype=np.float32)
    for half_fov in (60, 64, 68):
        pos_enter = np.maximum(0.0, g_pad[68 - half_fov : 68 - half_fov + _POLAR_BINS])
        pos_exit = np.maximum(0.0, -g_pad[68 + half_fov : 68 + half_fov + _POLAR_BINS])
        np.maximum(best_score, np.sqrt(pos_enter * pos_exit), out=best_score)

    best_idx = int(np.argmax(best_score))
    peak = float(best_score[best_idx])
    if peak < 0.015:
        return None

    cd = np.minimum((_ANG_IDXS - best_idx) % _POLAR_BINS, (best_idx - _ANG_IDXS) % _POLAR_BINS)
    bg = best_score[cd > 24]
    bg_med = float(np.median(bg))
    bg_std = float(np.std(bg))
    bg_p98 = float(np.percentile(bg, 98))
    if peak <= bg_med + 3.5 * max(bg_std, 1e-4) or peak <= 1.2 * bg_p98:
        return None

    inside_v = float(np.mean(v_prof[cd <= 56]))
    outside_v = float(np.mean(v_prof[(cd >= 72) & (cd <= 112)]))
    if inside_v <= outside_v + 0.01:
        return None

    c_sub = _circular_subpixel_peak(best_score, best_idx)
    heading_cw = wrap_angle_360((c_sub / _POLAR_BINS) * 360.0 + 90.0)
    prom = (peak - bg_med) / (peak + 0.05)
    conf = float(min(0.82, max(0.35, 0.35 + 0.47 * prom)))
    return heading_cw, conf


def extract_camera_view_cone(minimap_img: np.ndarray) -> Tuple[Optional[float], float, str]:
    """
    Extracts the 360-degree camera view cone heading from a Genshin Impact minimap crop
    (~1.8-2.9 ms/frame via 512x76 polar annulus & uint8 SIMD Tier-2 detector).

    Returns:
        (camera_deg_cw_north, confidence, method)
        - camera_deg_cw_north: angle in [0.0, 360.0) clockwise from North, or None on failure.
        - confidence: float in [0.0, 1.0].
        - method: "bgra_alpha" | "bgr_blend" | "hsv_adaptive" | "none".
    """
    if minimap_img is None or not isinstance(minimap_img, np.ndarray) or minimap_img.size == 0:
        return (None, 0.0, "none")
    if minimap_img.ndim != 3 or minimap_img.shape[2] not in (3, 4):
        return (None, 0.0, "none")
    h, w = minimap_img.shape[:2]
    if min(h, w) < 24:
        return (None, 0.0, "none")

    img_u8 = minimap_img if minimap_img.dtype == np.uint8 else np.clip(minimap_img, 0, 255).astype(np.uint8)

    # Tier 1: BGRA alpha-channel fast path (when capture preserves DX11 minimap alpha)
    if img_u8.shape[2] == 4:
        res = _extract_cone_bgra_alpha_polar(img_u8[:, :, 3])
        if res is not None:
            return (res[0], res[1], "bgra_alpha")

    center = (w * 0.5, h * 0.5)
    max_rad = min(h, w) * 0.5
    polar = cv2.warpPolar(
        img_u8[:, :, :3],
        (_RAD_BINS, _POLAR_BINS),
        center,
        max_rad,
        cv2.WARP_POLAR_LINEAR | cv2.INTER_LINEAR,
    )
    ann = np.ascontiguousarray(polar[:, _RAD_R0:_RAD_R1, :])  # (512, 76, 3) uint8
    hsv = cv2.cvtColor(ann, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2].astype(np.float32) * np.float32(1.0 / 255.0)
    v_prof = np.mean(v, axis=1)

    # Tier 2: Vectorized uint8 LUT + cv2.absdiff alpha=0.25 blend detector
    res = _extract_cone_bgr_blend_simd(ann, v_prof)
    if res is not None:
        return (res[0], res[1], "bgr_blend")

    # Tier 3: Adaptive HSV/Value radial-gradient fallback (HDR / bright snow)
    res = _extract_cone_hsv_adaptive_polar(hsv, v, v_prof)
    if res is not None:
        return (res[0], res[1], "hsv_adaptive")

    return (None, 0.0, "none")


def _clean_arrow_mask(mask2: np.ndarray) -> np.ndarray:
    """Removes stray peripheral cyan specks far from the minimap center."""
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask2, connectivity=8)
    if num_labels <= 2:
        return mask2

    mh, mw = mask2.shape[:2]
    cx0, cy0 = mw / 2.0, mh / 2.0
    max_dist = 0.42 * min(mh, mw)

    # Pick the largest component reasonably near the center as the arrow body
    best_label = -1
    best_area = 0
    for lbl in range(1, num_labels):
        area = int(stats[lbl, cv2.CC_STAT_AREA])
        dist = math.hypot(float(centroids[lbl][0]) - cx0, float(centroids[lbl][1]) - cy0)
        if dist <= max_dist and area > best_area:
            best_area = area
            best_label = lbl

    if best_label < 0:
        return mask2

    cleaned = np.zeros_like(mask2)
    min_keep_area = max(4, int(0.12 * best_area))
    for lbl in range(1, num_labels):
        area = int(stats[lbl, cv2.CC_STAT_AREA])
        dist = math.hypot(float(centroids[lbl][0]) - cx0, float(centroids[lbl][1]) - cy0)
        if lbl == best_label or (dist <= max_dist and area >= min_keep_area):
            cleaned[labels == lbl] = 255
    return cleaned


def _erase_core_and_find_contours(
    edges_base: np.ndarray,
    cx: float,
    cy: float,
    r_erase: float,
) -> list:
    """Erases the central core circle of the 4x Canny edge map and returns tip contours."""
    work = edges_base.copy()
    cv2.circle(
        work,
        (int(math.ceil(cx)), int(math.ceil(cy))),
        max(1, int(math.ceil(r_erase))),
        0,
        -1,
    )
    dilate_el = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    erode_el = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    work = cv2.dilate(work, dilate_el)
    work = cv2.erode(work, erode_el)
    contours, _ = cv2.findContours(work, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    # Filter degenerate 1-2px fragments if extra contours exist
    if len(contours) > 3:
        filtered = [c for c in contours if len(c) >= 6]
        if filtered:
            contours = filtered
    return list(contours)


def _solve_arrow_from_mask(mask2: np.ndarray, conf_scale: float) -> Optional[Tuple[float, float]]:
    """Runs cvAutoTrack 4x Canny + center-core erasure + 3-tip (or 2-wing) angle solver."""
    if cv2.countNonZero(mask2) < 16:
        return None

    clean2 = _clean_arrow_mask(mask2)
    if cv2.countNonZero(clean2) < 16:
        return None

    # Upscale 2x again (4x total) matching cvAutoTrack (INTER_AREA == 3)
    mask4 = cv2.resize(clean2, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_AREA)
    edges_base = cv2.Canny(mask4, 20, 60, apertureSize=3)
    rows, cols = mask4.shape[:2]
    img_center = np.array([cols / 2.0, rows / 2.0], dtype=np.float64)

    # First attempt: exact cvAutoTrack center and r = cols / 4.5
    contours = _erase_core_and_find_contours(edges_base, cols / 2.0, rows / 2.0, cols / 4.5)

    # Check if 2-contour result looks like a genuine symmetric 2-wing occluded-nose case
    # Two rear wings subtend ~65-110 deg from center, whereas wing+nose subtend ~130-155 deg.
    def _is_symmetric_two_wings(cnts: list) -> bool:
        if len(cnts) != 2:
            return False
        pts2 = []
        for c in cnts:
            x, y, bw, bh = cv2.boundingRect(c)
            pts2.append(np.array([x + bw / 2.0, y + bh / 2.0], dtype=np.float64))
        v0 = pts2[0] - img_center
        v1 = pts2[1] - img_center
        d0 = float(np.linalg.norm(v0))
        d1 = float(np.linalg.norm(v1))
        if min(d0, d1) < 2.0:
            return False
        ratio = max(d0, d1) / min(d0, d1)
        cos_ang = float(np.clip(np.dot(v0, v1) / (d0 * d1), -1.0, 1.0))
        subtended_deg = math.degrees(math.acos(cos_ang))
        mid = 0.5 * (pts2[0] + pts2[1])
        return ratio <= 1.35 and 40.0 <= subtended_deg <= 120.0 and float(np.linalg.norm(img_center - mid)) >= 2.0

    # Adaptive centroid/radius sweep if fixed r = cols/4.5 did not isolate 3 tips
    # (and did not produce a clean symmetric 2-wing occluded-nose fallback)
    if len(contours) != 3 and not _is_symmetric_two_wings(contours):
        ys, xs = np.nonzero(mask4)
        if xs.size >= 16:
            mcx = float(np.mean(xs))
            mcy = float(np.mean(ys))
            r_max = float(np.max(np.hypot(xs - mcx, ys - mcy)))
            if r_max >= 6.0:
                fallback_two: Optional[list] = None
                for frac in (0.62, 0.56, 0.68, 0.50, 0.74, 0.44):
                    cand = _erase_core_and_find_contours(edges_base, mcx, mcy, frac * r_max)
                    if len(cand) == 3:
                        contours = cand
                        break
                    if len(cand) == 2 and fallback_two is None:
                        fallback_two = cand
                else:
                    if len(contours) != 2 and fallback_two is not None:
                        contours = fallback_two

    if len(contours) == 3:
        pts = []
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            pts.append(np.array([x + bw / 2.0, y + bh / 2.0], dtype=np.float64))

        d01 = float(np.linalg.norm(pts[1] - pts[0]))
        d02 = float(np.linalg.norm(pts[2] - pts[0]))
        d12 = float(np.linalg.norm(pts[2] - pts[1]))

        # Nose is the vertex connected to the 2 longest edges (opposite the shortest wing-wing base)
        if d01 <= d02 and d01 <= d12:
            nose, w0, w1 = pts[2], pts[0], pts[1]
            short_d, l0, l1 = d01, d02, d12
        elif d02 <= d01 and d02 <= d12:
            nose, w0, w1 = pts[1], pts[0], pts[2]
            short_d, l0, l1 = d02, d01, d12
        else:
            nose, w0, w1 = pts[0], pts[1], pts[2]
            short_d, l0, l1 = d12, d01, d02

        if l0 < 1e-3 or l1 < 1e-3:
            return None

        u0 = (nose - w0) / l0
        u1 = (nose - w1) / l1
        key_vec = u0 + u1
        if float(np.linalg.norm(key_vec)) < 1e-3:
            return None

        # Unified convention: 0 deg = North (-Y), CW [0, 360)
        angle_cw = wrap_angle_360(math.degrees(math.atan2(float(key_vec[0]), -float(key_vec[1]))))
        elongation = min(l0, l1) / max(short_d, 1e-3)
        symmetry = max(0.0, 1.0 - abs(l0 - l1) / max(l0, l1))
        elong_score = min(1.0, max(0.0, (elongation - 1.0) / 0.35))
        conf = float(min(0.98, max(0.65, (0.65 + 0.18 * symmetry + 0.15 * elong_score) * conf_scale)))
        return angle_cw, conf

    if len(contours) == 2:
        pts = []
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            pts.append(np.array([x + bw / 2.0, y + bh / 2.0], dtype=np.float64))
        mid = 0.5 * (pts[0] + pts[1])
        vec = img_center - mid
        if float(np.linalg.norm(vec)) < 2.0:
            return None
        v0 = pts[0] - img_center
        v1 = pts[1] - img_center
        d0, d1 = float(np.linalg.norm(v0)), float(np.linalg.norm(v1))
        subtended_ok = False
        if d0 >= 2.0 and d1 >= 2.0:
            cos_ang = float(np.clip(np.dot(v0, v1) / (d0 * d1), -1.0, 1.0))
            subtended_ok = 35.0 <= math.degrees(math.acos(cos_ang)) <= 125.0
        angle_cw = wrap_angle_360(math.degrees(math.atan2(float(vec[0]), -float(vec[1]))))
        base_conf = 0.60 if subtended_ok else 0.35
        return angle_cw, float(base_conf * conf_scale)

    return None


def extract_player_arrow_360(minimap_bgr: np.ndarray) -> Tuple[Optional[float], float]:
    """
    Extracts exact 360-degree player facing direction from the cyan arrow at minimap center.
    Eliminates 180-degree ellipse ambiguity via 3-tip contour triangle geometry.

    Returns:
        (arrow_deg_cw_north, confidence)
        - arrow_deg_cw_north: angle in [0.0, 360.0) clockwise from North, or None on failure.
        - confidence: float in [0.0, 1.0].
    """
    if minimap_bgr is None or not isinstance(minimap_bgr, np.ndarray) or minimap_bgr.size == 0:
        return (None, 0.0)
    if minimap_bgr.ndim != 3 or minimap_bgr.shape[2] not in (3, 4):
        return (None, 0.0)
    h, w = minimap_bgr.shape[:2]
    if min(h, w) < 20:
        return (None, 0.0)

    bgr = minimap_bgr[:, :, :3]
    y0, y1 = int(round(0.4 * h)), int(round(0.6 * h))
    x0, x1 = int(round(0.4 * w)), int(round(0.6 * w))
    crop = bgr[y0:y1, x0:x1]
    if crop.size == 0 or min(crop.shape[:2]) < 4:
        return (None, 0.0)

    up2 = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LINEAR)

    # Primary: cvAutoTrack strict BGR cyan threshold (B>240, G>212, R<25)
    b, g, r = cv2.split(up2)
    _, gray0 = cv2.threshold(b, 240, 255, cv2.THRESH_BINARY)
    _, gray1 = cv2.threshold(g, 212, 255, cv2.THRESH_BINARY)
    _, gray2 = cv2.threshold(r, 25, 255, cv2.THRESH_BINARY_INV)
    mask_bgr = cv2.bitwise_and(gray1, gray2, mask=gray0)

    res_bgr = _solve_arrow_from_mask(mask_bgr, conf_scale=1.0)
    if res_bgr is not None and res_bgr[1] >= 0.65:
        return res_bgr

    # Fallback: HSV cyan range for FSR2 / compression / brightness / HDR resilience
    # Also overrides a degraded 2-contour BGR match when HSV achieves a clean 3-tip lock.
    hsv = cv2.cvtColor(up2, cv2.COLOR_BGR2HSV)
    mask_hsv = cv2.inRange(hsv, _CYAN_HSV_LOWER, _CYAN_HSV_UPPER)
    res_hsv = _solve_arrow_from_mask(mask_hsv, conf_scale=0.92)
    if res_hsv is not None and (res_bgr is None or res_hsv[1] > res_bgr[1]):
        return res_hsv
    if res_bgr is not None:
        return res_bgr

    return (None, 0.0)


class CircularAngleSmoother:
    """
    Circular exponential moving average (EMA) on (sin theta, cos theta) with deadband
    and continuous angle unwrapping across the 0/360 degree boundary.
    """

    def __init__(self, alpha: float = 0.35, deadband_deg: float = 1.5):
        if not (0.0 < alpha <= 1.0):
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        if deadband_deg < 0.0:
            raise ValueError(f"deadband_deg must be >= 0, got {deadband_deg}")
        self.alpha = float(alpha)
        self.deadband_deg = float(deadband_deg)
        self._sin: Optional[float] = None
        self._cos: Optional[float] = None
        self._wrapped_deg: Optional[float] = None
        self._unwrapped_deg: Optional[float] = None

    def reset(self) -> None:
        """Resets filter state."""
        self._sin = None
        self._cos = None
        self._wrapped_deg = None
        self._unwrapped_deg = None

    @property
    def angle(self) -> Optional[float]:
        """Smoothed heading in [0.0, 360.0), or None before first sample."""
        return self._wrapped_deg

    @property
    def unwrapped_angle(self) -> Optional[float]:
        """Continuous unwrapped heading for jitter-free CSS rotation transitions."""
        return self._unwrapped_deg

    def update(self, angle_deg: Optional[float]) -> Optional[float]:
        """Ingests a new raw heading measurement (degrees CW from North) and returns smoothed [0, 360)."""
        if angle_deg is None:
            return self._wrapped_deg

        raw = wrap_angle_360(angle_deg)
        if self._wrapped_deg is None or self._sin is None or self._cos is None or self._unwrapped_deg is None:
            rad = math.radians(raw)
            self._sin = math.sin(rad)
            self._cos = math.cos(rad)
            self._wrapped_deg = raw
            self._unwrapped_deg = raw
            return self._wrapped_deg

        delta = shortest_angle_delta(self._wrapped_deg, raw)
        if abs(delta) < self.deadband_deg:
            return self._wrapped_deg

        rad = math.radians(raw)
        s_in = math.sin(rad)
        c_in = math.cos(rad)
        new_sin = (1.0 - self.alpha) * self._sin + self.alpha * s_in
        new_cos = (1.0 - self.alpha) * self._cos + self.alpha * c_in
        norm = math.hypot(new_sin, new_cos)
        if norm < 1e-6:
            # Exact 180-deg cancellation guard: advance smoothly via signed delta
            step_rad = math.radians(wrap_angle_360(self._wrapped_deg + self.alpha * delta))
            self._sin = math.sin(step_rad)
            self._cos = math.cos(step_rad)
        else:
            self._sin = new_sin / norm
            self._cos = new_cos / norm

        new_wrapped = wrap_angle_360(math.degrees(math.atan2(self._sin, self._cos)))
        step = shortest_angle_delta(self._wrapped_deg, new_wrapped)
        self._unwrapped_deg += step
        self._wrapped_deg = new_wrapped
        return self._wrapped_deg


def compute_nav_relative_angle(
    player_pos: Sequence[float],
    target_pos: Sequence[float],
    view_deg_cw_north: float,
) -> Tuple[float, float]:
    """
    Computes navigation bearing relative to current player/camera heading.

    Uses HoYoLAB / Genshin-Helper world coordinate axes (+X = East, -Y = North, +Y = South).

    Returns:
        (delta_deg_in_minus180_to_180, distance_world_units)
        - delta_deg == 0.0 means target is straight ahead (press W).
        - delta_deg > 0.0 means target is to the right (turn clockwise).
        - delta_deg < 0.0 means target is to the left (turn counter-clockwise).
    """
    dx = float(target_pos[0]) - float(player_pos[0])
    dy = float(target_pos[1]) - float(player_pos[1])
    dist = float(math.hypot(dx, dy))
    if dist < 1e-9:
        return (0.0, 0.0)

    target_bearing_deg = wrap_angle_360(math.degrees(math.atan2(dx, -dy)))
    delta_deg = shortest_angle_delta(view_deg_cw_north, target_bearing_deg)
    return (delta_deg, dist)


# Cone extractor tiers that are trustworthy on their own. Tier 3 ("hsv_adaptive") is a
# last-resort radial-gradient guess: on busy / bright minimaps (Snezhnaya ice, map screens) it
# locks onto fixed false angles (~37° / ~225°) or the opposite side, which made the web FOV cone
# spin and the in-game nav arrow swing while the real camera was still.
TRUSTED_CONE_METHODS = frozenset({"bgra_alpha", "bgr_blend"})


class CameraHeadingFuser:
    """
    Temporal gate for per-frame camera view-cone measurements.

    - Trusted tiers (alpha / blend) are accepted directly when continuous with the estimate.
    - Tier-3 guesses are accepted only when they agree with the current estimate (±28°) or with
      the cyan body arrow (±50°: the camera trails the character while walking).
    - A jump larger than `jump_deg` must be confirmed by `confirm_frames` consecutive mutually
      consistent measurements before the estimate moves (fast mouse flicks still pass in ~0.1 s).
    - Without an accepted measurement for `stale_s`, the estimate falls back to the body arrow,
      so navigation never keeps pointing along a stale/false camera angle.
    """

    def __init__(
        self,
        *,
        jump_deg: float = 50.0,
        confirm_frames: int = 3,
        confirm_window_s: float = 0.6,
        stale_s: float = 0.8,
    ) -> None:
        self.jump_deg = float(jump_deg)
        self.confirm_frames = max(1, int(confirm_frames))
        self.confirm_window_s = float(confirm_window_s)
        self.stale_s = float(stale_s)
        self._estimate: Optional[float] = None
        self._conf: float = 0.0
        self._source: str = "none"
        self._last_accept_ts: Optional[float] = None
        self._pending: list[Tuple[float, float]] = []  # (angle, ts)

    @property
    def estimate(self) -> Optional[float]:
        return self._estimate

    @property
    def confidence(self) -> float:
        return self._conf

    @property
    def source(self) -> str:
        return self._source

    def reset(self) -> None:
        self._estimate = None
        self._conf = 0.0
        self._source = "none"
        self._last_accept_ts = None
        self._pending.clear()

    def _accept(self, angle: float, conf: float, ts: float, source: str) -> None:
        self._estimate = wrap_angle_360(angle)
        self._conf = float(conf)
        self._source = source
        self._last_accept_ts = ts
        self._pending.clear()

    def update(
        self,
        cam_deg: Optional[float],
        cam_conf: float,
        method: str,
        body_deg: Optional[float],
        body_conf: float,
        now: float,
    ) -> Optional[float]:
        """Feeds one frame; returns the accepted camera heading estimate (or None)."""
        ts = float(now)
        trusted = cam_deg is not None and cam_conf > 0.0 and method in TRUSTED_CONE_METHODS
        guess = cam_deg is not None and cam_conf > 0.0 and not trusted
        body_ok = body_deg is not None and body_conf >= 0.5

        if cam_deg is not None and (trusted or guess):
            meas = wrap_angle_360(cam_deg)
            if self._estimate is None:
                if trusted:
                    self._accept(meas, cam_conf, ts, method)
                elif body_ok and abs(shortest_angle_delta(body_deg, meas)) <= 50.0:
                    self._accept(meas, cam_conf * 0.8, ts, method)
                elif cam_conf >= 0.8:
                    self._accept(meas, cam_conf * 0.7, ts, method)
            else:
                jump = abs(shortest_angle_delta(self._estimate, meas))
                if jump <= (self.jump_deg if trusted else 28.0):
                    self._accept(meas, cam_conf, ts, method)
                elif guess and body_ok and abs(shortest_angle_delta(body_deg, meas)) <= 50.0 and jump <= 90.0:
                    self._accept(meas, cam_conf * 0.8, ts, method)
                elif guess and (not body_ok or abs(shortest_angle_delta(body_deg, meas)) > 50.0):
                    # A tier-3 guess can be wrong *consistently* (sky/UI colours): a big jump it
                    # reports is never confirmed by itself unless the body arrow agrees with it.
                    pass
                else:
                    # Large jump: require N consecutive, mutually consistent measurements
                    self._pending = [
                        (a, t) for (a, t) in self._pending
                        if ts - t <= self.confirm_window_s and abs(shortest_angle_delta(a, meas)) <= 20.0
                    ]
                    self._pending.append((meas, ts))
                    needed = self.confirm_frames if trusted else self.confirm_frames + 2
                    if len(self._pending) >= needed:
                        self._accept(meas, cam_conf, ts, method)

        stale = self._last_accept_ts is None or (ts - self._last_accept_ts) > self.stale_s
        if stale and body_ok:
            # No trustworthy cone lately: the camera trails the body when navigating, so use it.
            if self._estimate is None or abs(shortest_angle_delta(self._estimate, body_deg)) > 1.0 or self._source != "body":
                self._estimate = wrap_angle_360(body_deg)
                self._conf = float(min(0.5, body_conf * 0.5))
                self._source = "body"
        return self._estimate
