"""
Verification suite for Track 2A: 360-degree Camera View Cone & Player Arrow Heading Extractor.
Run via: .\\.venv\\Scripts\\python.exe tests/verify_heading360_v5.py
"""

from __future__ import annotations

import math
import os
import sys

import cv2
import numpy as np

# Ensure project root is on sys.path when executed directly
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.heading360 import (
    CircularAngleSmoother,
    compute_nav_relative_angle,
    extract_camera_view_cone,
    extract_player_arrow_360,
    shortest_angle_delta,
    wrap_angle_360,
)
from src.tracker import CvAutoTrackBridge, HybridTracker, PureOpenCVTracker

COMPASS_8 = (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0)


def _make_textured_terrain(size: int = 212, seed: int = 42, base_level: float = 110.0) -> np.ndarray:
    """
    Generates realistic multi-frequency textured Genshin-like minimap terrain
    (rolling green/ochre terrain, roads, water patch, gentle micro-texture).
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    nx = xx / float(size)
    ny = yy / float(size)

    # Smooth multi-frequency terrain elevation & biome colors
    wave1 = np.sin(2.0 * math.pi * (nx * 2.3 + ny * 1.4)) * 22.0
    wave2 = np.cos(2.0 * math.pi * (nx * 4.1 - ny * 3.7)) * 14.0
    wave3 = np.sin(2.0 * math.pi * (nx * 7.5 + ny * 6.2)) * 7.0

    # Smooth Gaussian-blurred noise (terrain foliage / rocks)
    noise_raw = rng.normal(0.0, 14.0, (size, size, 3)).astype(np.float32)
    noise = cv2.GaussianBlur(noise_raw, (5, 5), 1.4)

    # Subtle winding ochre road across the minimap
    road_dist = np.abs(ny - (0.52 + 0.14 * np.sin(nx * 5.0)))
    road_mask = np.exp(-((road_dist / 0.035) ** 2))

    # Build BGR terrain (lush green/earth palette, away from cyan arrow color)
    b = base_level * 0.68 + wave1 * 0.6 + wave2 * 0.5 + wave3 + noise[:, :, 0]
    g = base_level * 1.05 + wave1 * 0.9 + wave2 * 0.7 + wave3 + noise[:, :, 1]
    r = base_level * 0.82 + wave1 * 0.7 - wave2 * 0.4 + wave3 + noise[:, :, 2]

    # Blend ochre road
    b = b * (1.0 - 0.5 * road_mask) + 125.0 * (0.5 * road_mask)
    g = g * (1.0 - 0.5 * road_mask) + 162.0 * (0.5 * road_mask)
    r = r * (1.0 - 0.5 * road_mask) + 184.0 * (0.5 * road_mask)

    bgr = np.stack([b, g, r], axis=-1)
    return np.clip(np.round(bgr), 15, 225).astype(np.uint8)


def _cone_sector_mask(size: int, heading_deg_cw_north: float, fov_deg: float = 90.0) -> np.ndarray:
    """Boolean mask of a 90-degree camera view cone centered at heading_deg_cw_north."""
    cx = size / 2.0
    cy = size / 2.0
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    dx = xx - cx
    dy = yy - cy
    radius = np.hypot(dx, dy)

    # Pixel compass bearing CW from North (0=Up, 90=Right, 180=Down, 270=Left)
    ang_cw = np.degrees(np.arctan2(dx, -dy)) % 360.0
    diff = (ang_cw - heading_deg_cw_north + 180.0) % 360.0 - 180.0

    r_min = 0.08 * size
    r_max = 0.45 * size
    return (np.abs(diff) <= (fov_deg * 0.5)) & (radius >= r_min) & (radius <= r_max)


def _render_bgra_cone_minimap(
    heading_deg_cw_north: float,
    size: int = 212,
    cone_alpha: int = 191,
    bg_alpha: int = 255,
    seed: int = 42,
) -> np.ndarray:
    """Renders synthetic BGRA minimap with view cone encoded in both alpha channel and BGR."""
    terrain = _make_textured_terrain(size=size, seed=seed)
    mask = _cone_sector_mask(size, heading_deg_cw_north, fov_deg=90.0)

    bgr = terrain.astype(np.float32)
    bgr[mask] = bgr[mask] * 0.75 + 255.0 * 0.25
    bgr_u8 = np.clip(np.round(bgr), 0, 255).astype(np.uint8)

    alpha = np.full((size, size), bg_alpha, dtype=np.uint8)
    alpha[mask] = cone_alpha
    return np.dstack([bgr_u8, alpha])


def _render_bgr_cone_minimap(
    heading_deg_cw_north: float,
    size: int = 212,
    blend_alpha: float = 0.25,
    seed: int = 101,
    base_level: float = 110.0,
) -> np.ndarray:
    """Renders synthetic 3-channel BGR minimap with white alpha-blended view cone over textured terrain."""
    terrain = _make_textured_terrain(size=size, seed=seed, base_level=base_level)
    mask = _cone_sector_mask(size, heading_deg_cw_north, fov_deg=90.0)

    bgr = terrain.astype(np.float32)
    bgr[mask] = bgr[mask] * (1.0 - blend_alpha) + 255.0 * blend_alpha
    return np.clip(np.round(bgr), 0, 255).astype(np.uint8)


def _render_player_arrow_minimap(
    heading_deg_cw_north: float,
    size: int = 212,
    arrow_bgr: tuple = (255, 232, 8),
    seed: int = 77,
    occlude_nose: bool = False,
) -> np.ndarray:
    """
    Renders a realistic Genshin Impact cyan 3-tip dart/chevron player arrow at minimap center.
    Geometry in local North-up coordinates (scale proportional to minimap width):
      - Nose tip at (0, -0.070 * size)
      - Right wing tip at (+0.044 * size, +0.056 * size)
      - Rear concave notch at (0, +0.022 * size)
      - Left wing tip at (-0.044 * size, +0.056 * size)
    """
    img = _make_textured_terrain(size=size, seed=seed)
    cx = size / 2.0
    cy = size / 2.0

    # Local North-facing (0 deg) chevron polygon
    local_pts = np.array(
        [
            [0.0, -0.072 * size],          # Nose tip (farthest)
            [0.046 * size, 0.056 * size],  # Right rear wing
            [0.0, 0.020 * size],           # Inner rear notch
            [-0.046 * size, 0.056 * size], # Left rear wing
        ],
        dtype=np.float64,
    )

    # Rotate clockwise in image coordinates (+X right, +Y down):
    # x' = x*cos(t) - y*sin(t), y' = x*sin(t) + y*cos(t)
    # Check: (0, -1) at t=+90 deg -> (+1, 0) = East! Correct!
    rad = math.radians(heading_deg_cw_north)
    c, s = math.cos(rad), math.sin(rad)
    rot_mat = np.array([[c, -s], [s, c]], dtype=np.float64)
    world_pts = (local_pts @ rot_mat.T) + np.array([cx, cy])
    poly = np.round(world_pts).astype(np.int32).reshape((-1, 1, 2))

    # Draw subtle dark border like Genshin UI, then crisp cyan fill
    cv2.polylines(img, [poly], isClosed=True, color=(30, 30, 30), thickness=max(1, size // 120), lineType=cv2.LINE_AA)
    cv2.fillPoly(img, [poly], color=arrow_bgr, lineType=cv2.LINE_8)

    if occlude_nose:
        # Occlude the nose tip with a dark marker dot (tests 2-wing fallback)
        nose_pt = tuple(np.round(world_pts[0]).astype(int))
        cv2.circle(img, nose_pt, max(4, int(round(0.032 * size))), (45, 55, 50), -1)

    return img


def test_bgra_view_cone() -> None:
    print("[a] Testing BGRA alpha-channel View Cone across 8 compass angles...")
    max_err = 0.0
    for idx, true_deg in enumerate(COMPASS_8):
        img = _render_bgra_cone_minimap(true_deg, size=212, cone_alpha=191, bg_alpha=255, seed=10 + idx)
        pred, conf, method = extract_camera_view_cone(img)
        assert pred is not None, f"BGRA cone failed at {true_deg} deg"
        assert method == "bgra_alpha", f"Expected bgra_alpha, got {method} at {true_deg} deg"
        assert conf >= 0.75, f"Low confidence {conf:.3f} at {true_deg} deg"
        err = abs(shortest_angle_delta(true_deg, pred))
        max_err = max(max_err, err)
        assert err < 5.0, f"BGRA cone err={err:.2f} deg at true={true_deg}, pred={pred:.2f}"
        print(f"    true={true_deg:5.1f}  pred={pred:6.2f}  err={err:4.2f}  conf={conf:.2f}  method={method}")

    # Also verify inverted high-alpha overlay cone (bg=160, cone=255) has zero 180-deg flip
    for true_deg in (0.0, 90.0, 180.0, 270.0):
        img = _render_bgra_cone_minimap(true_deg, size=212, cone_alpha=255, bg_alpha=160, seed=99)
        pred, conf, method = extract_camera_view_cone(img)
        assert pred is not None and method == "bgra_alpha"
        err = abs(shortest_angle_delta(true_deg, pred))
        assert err < 5.0, f"High-alpha BGRA cone err={err:.2f} at {true_deg} (pred={pred:.2f})"

    print(f"    PASS (max_err={max_err:.2f} deg < 5.0 deg)")


def test_bgr_blend_view_cone() -> None:
    print("[b] Testing BGR alpha=0.25 View Cone over textured terrain across 8 compass angles...")
    max_err = 0.0
    for idx, true_deg in enumerate(COMPASS_8):
        img = _render_bgr_cone_minimap(true_deg, size=212, blend_alpha=0.25, seed=200 + idx)
        pred, conf, method = extract_camera_view_cone(img)
        assert pred is not None, f"BGR blend cone failed at {true_deg} deg"
        assert method == "bgr_blend", f"Expected bgr_blend, got {method} at {true_deg} deg"
        assert conf >= 0.50, f"Low confidence {conf:.3f} at {true_deg} deg"
        err = abs(shortest_angle_delta(true_deg, pred))
        max_err = max(max_err, err)
        assert err < 6.0, f"BGR blend cone err={err:.2f} deg at true={true_deg}, pred={pred:.2f}"
        print(f"    true={true_deg:5.1f}  pred={pred:6.2f}  err={err:4.2f}  conf={conf:.2f}  method={method}")

    # Adaptive HDR/snow fallback check (alpha=0.14 breaks strict 0.25 tolerance -> hsv_adaptive)
    print("[b2] Testing adaptive HSV/HDR & bright snow fallback + negative rejection...")
    for idx, true_deg in enumerate((0.0, 135.0, 225.0, 315.0)):
        img_hdr = _render_bgr_cone_minimap(true_deg, size=212, blend_alpha=0.14, seed=300 + idx)
        pred, conf, method = extract_camera_view_cone(img_hdr)
        assert pred is not None, f"HDR fallback failed at {true_deg} deg"
        err = abs(shortest_angle_delta(true_deg, pred))
        assert err < 6.0, f"HDR fallback err={err:.2f} at true={true_deg}, pred={pred:.2f} ({method})"
        print(f"    HDR  true={true_deg:5.1f}  pred={pred:6.2f}  err={err:4.2f}  conf={conf:.2f}  method={method}")

    # Flat white snow with NO cone must return (None, 0.0, "none")
    flat_snow = np.full((212, 212, 3), 248, dtype=np.uint8)
    pred_none, conf_none, method_none = extract_camera_view_cone(flat_snow)
    assert pred_none is None and conf_none == 0.0 and method_none == "none", (
        f"False positive on flat white snow: {(pred_none, conf_none, method_none)}"
    )

    # Pure textured terrain with NO cone must return (None, 0.0, "none")
    no_cone = _make_textured_terrain(size=212, seed=555)
    pred_nc, conf_nc, method_nc = extract_camera_view_cone(no_cone)
    assert pred_nc is None and method_nc == "none", (
        f"False positive on cone-free terrain: {(pred_nc, conf_nc, method_nc)}"
    )
    print(f"    PASS (BGR max_err={max_err:.2f} deg < 6.0 deg, HDR fallback & negative rejection OK)")


def test_player_arrow_360() -> None:
    print("[c] Testing Cyan 3-tip Player Arrow across 8 compass angles (ZERO 180-deg ambiguity)...")
    preds = {}
    max_err = 0.0
    for idx, true_deg in enumerate(COMPASS_8):
        img = _render_player_arrow_minimap(true_deg, size=212, arrow_bgr=(255, 232, 8), seed=400 + idx)
        pred, conf = extract_player_arrow_360(img)
        assert pred is not None, f"Player arrow failed at {true_deg} deg"
        assert conf >= 0.70, f"Low arrow confidence {conf:.3f} at {true_deg} deg"
        err = abs(shortest_angle_delta(true_deg, pred))
        max_err = max(max_err, err)
        preds[true_deg] = pred
        assert err < 6.0, f"Arrow err={err:.2f} deg at true={true_deg}, pred={pred:.2f}"
        print(f"    true={true_deg:5.1f}  pred={pred:6.2f}  err={err:4.2f}  conf={conf:.2f}")

    # Explicit opposite-heading separation checks (proving 0 != 180 and 90 != 270)
    sep_ns = abs(shortest_angle_delta(preds[0.0], preds[180.0]))
    sep_ew = abs(shortest_angle_delta(preds[90.0], preds[270.0]))
    assert sep_ns > 170.0, f"0 vs 180 collapsed! sep={sep_ns:.2f}"
    assert sep_ew > 170.0, f"90 vs 270 collapsed! sep={sep_ew:.2f}"

    # FSR2 / dimmed cyan arrow triggering HSV fallback (B=222 < 240, G=198 < 212, R=30 > 25)
    print("[c2] Testing FSR2/dimmed HSV fallback, JPEG75 compression, 2-wing occluded nose, & 720p/4K scales...")
    for true_deg in (0.0, 90.0, 180.0, 270.0):
        img_fsr = _render_player_arrow_minimap(true_deg, size=212, arrow_bgr=(222, 198, 30), seed=500)
        pred, conf = extract_player_arrow_360(img_fsr)
        assert pred is not None, f"HSV fallback failed at {true_deg}"
        err = abs(shortest_angle_delta(true_deg, pred))
        assert err < 6.0, f"HSV fallback err={err:.2f} at true={true_deg}, pred={pred:.2f}"

    # JPEG-75 compressed arrow (erodes strict BGR wing pixels -> HSV 3-tip lock overrides 2-contour BGR)
    for idx, true_deg in enumerate(COMPASS_8):
        img_raw = _render_player_arrow_minimap(true_deg, size=212, seed=550 + idx)
        ok, buf = cv2.imencode(".jpg", img_raw, [cv2.IMWRITE_JPEG_QUALITY, 75])
        assert ok
        dec = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        pred, conf = extract_player_arrow_360(dec)
        assert pred is not None, f"JPEG75 arrow failed at {true_deg}"
        err = abs(shortest_angle_delta(true_deg, pred))
        assert err < 6.0, f"JPEG75 arrow err={err:.2f} at true={true_deg}, pred={pred:.2f}"

    # 2-contour occluded-nose fallback
    for true_deg in (0.0, 90.0, 180.0, 270.0):
        img_occ = _render_player_arrow_minimap(true_deg, size=212, occlude_nose=True, seed=600)
        pred, conf = extract_player_arrow_360(img_occ)
        assert pred is not None, f"Occluded-nose 2-wing fallback failed at {true_deg}"
        err = abs(shortest_angle_delta(true_deg, pred))
        assert err < 12.0, f"Occluded-nose err={err:.2f} at true={true_deg}, pred={pred:.2f}"

    # Scale invariance: 720p (140px) and 4K (424px)
    for sz in (140, 424):
        for true_deg in (45.0, 225.0):
            img_sz = _render_player_arrow_minimap(true_deg, size=sz, seed=700)
            pred, _ = extract_player_arrow_360(img_sz)
            assert pred is not None, f"Arrow failed at size={sz}, deg={true_deg}"
            err = abs(shortest_angle_delta(true_deg, pred))
            assert err < 6.0, f"Arrow size={sz} err={err:.2f} at true={true_deg}, pred={pred:.2f}"

    # Negative check: terrain without cyan arrow
    no_arrow = _make_textured_terrain(size=212, seed=800)
    pred_na, conf_na = extract_player_arrow_360(no_arrow)
    assert pred_na is None and conf_na == 0.0, f"False positive on arrow-free frame: {(pred_na, conf_na)}"

    print(f"    PASS (max_err={max_err:.2f} deg < 6.0 deg, sep_NS={sep_ns:.1f}, sep_EW={sep_ew:.1f})")


def test_circular_angle_smoother() -> None:
    print("[d] Testing CircularAngleSmoother across 355 -> 5 deg boundary...")
    smoother = CircularAngleSmoother(alpha=0.35, deadband_deg=1.5)
    v0 = smoother.update(355.0)
    assert v0 == 355.0 and smoother.unwrapped_angle == 355.0

    # Deadband check: +-0.9 deg jitter must be ignored
    v_jit = smoother.update(355.9)
    assert v_jit == 355.0, f"Deadband failed: moved to {v_jit}"

    # Cross 355 -> 5 degrees clockwise through 0 deg (NEVER through 180 deg!)
    prev_unwrapped = smoother.unwrapped_angle
    assert prev_unwrapped is not None
    trajectory = [v0]
    for _ in range(15):
        v = smoother.update(5.0)
        assert v is not None
        trajectory.append(v)
        # Must remain within 5.5 deg of North (0 deg), never swing toward 180 deg
        dist_from_north = abs(shortest_angle_delta(0.0, v))
        assert dist_from_north <= 5.5, f"Smoother reversed away from North! v={v:.2f}"
        curr_unwrapped = smoother.unwrapped_angle
        assert curr_unwrapped is not None
        assert curr_unwrapped >= prev_unwrapped - 1e-9, (
            f"Unwrapped angle went backwards: {prev_unwrapped:.2f} -> {curr_unwrapped:.2f}"
        )
        prev_unwrapped = curr_unwrapped

    # After convergence (modulo deadband=1.5 deg), must be within deadband of 5.0 deg
    final_err = abs(shortest_angle_delta(5.0, trajectory[-1]))
    assert final_err <= 1.6, f"Did not converge to 5 deg: final={trajectory[-1]:.2f}"
    assert smoother.unwrapped_angle is not None and smoother.unwrapped_angle > 363.0, (
        f"Expected unwrapped_angle near 365, got {smoother.unwrapped_angle}"
    )

    # Reverse crossing: 5 -> 355 deg counter-clockwise
    smoother.reset()
    smoother.update(5.0)
    prev_u = smoother.unwrapped_angle
    assert prev_u is not None
    for _ in range(15):
        v = smoother.update(355.0)
        assert v is not None
        assert abs(shortest_angle_delta(0.0, v)) <= 5.5, f"Reverse wrap failed: v={v:.2f}"
        assert smoother.unwrapped_angle is not None and smoother.unwrapped_angle <= prev_u + 1e-9
        prev_u = smoother.unwrapped_angle
    assert smoother.unwrapped_angle is not None and smoother.unwrapped_angle < -3.0

    # None input holds last valid angle
    held = smoother.update(None)
    assert held == smoother.angle

    print(
        f"    PASS (355->5 traj={[round(x, 1) for x in trajectory[:6]]}..., "
        f"final_err={final_err:.2f} deg)"
    )


def test_nav_relative_angle() -> None:
    print("[e] Testing compute_nav_relative_angle cardinal & diagonal checks...")
    # HoYoLAB / screen coords: +X = East, -Y = North, +Y = South
    cases = [
        # (player, target, view_deg, expected_delta, expected_dist)
        ((0.0, 0.0), (0.0, -100.0), 0.0, 0.0, 100.0),          # North ahead (press W)
        ((0.0, 0.0), (100.0, 0.0), 0.0, 90.0, 100.0),          # East is right when facing N
        ((0.0, 0.0), (-100.0, 0.0), 0.0, -90.0, 100.0),        # West is left when facing N
        ((0.0, 0.0), (0.0, 100.0), 0.0, 180.0, 100.0),         # South is behind when facing N
        ((0.0, 0.0), (0.0, -100.0), 90.0, -90.0, 100.0),       # North is left when facing E
        ((0.0, 0.0), (0.0, -100.0), 270.0, 90.0, 100.0),       # North is right when facing W
        ((0.0, 0.0), (100.0, 0.0), 90.0, 0.0, 100.0),          # East ahead when facing E
        ((0.0, 0.0), (0.0, 100.0), 180.0, 0.0, 100.0),         # South ahead when facing S
        ((0.0, 0.0), (-100.0, 0.0), 270.0, 0.0, 100.0),        # West ahead when facing W
        ((0.0, 0.0), (100.0, -100.0), 315.0, 90.0, math.hypot(100.0, 100.0)),  # NE from NW wrap
        ((0.0, 0.0), (-100.0, -100.0), 45.0, -90.0, math.hypot(100.0, 100.0)), # NW from NE wrap
        ((50.0, 50.0), (50.0, 50.0), 123.0, 0.0, 0.0),         # Zero distance
    ]
    for player, target, view, exp_delta, exp_dist in cases:
        delta, dist = compute_nav_relative_angle(player, target, view)
        assert abs(delta - exp_delta) < 1e-6, (
            f"Nav angle mismatch for p={player}, t={target}, view={view}: got {delta}, expected {exp_delta}"
        )
        assert abs(dist - exp_dist) < 1e-6, f"Nav dist mismatch: got {dist}, expected {exp_dist}"
    print(f"    PASS ({len(cases)} cardinal/diagonal cases exact)")


def test_tracker_integration() -> None:
    print("[f] Testing src/tracker.py PureOpenCVTracker & HybridTracker integration...")
    py_tracker = PureOpenCVTracker()
    hybrid = HybridTracker()
    assert isinstance(hybrid.c_tracker, CvAutoTrackBridge)
    assert isinstance(hybrid.py_tracker, PureOpenCVTracker)

    # Verify 0 vs 180 arrow heading via PureOpenCVTracker.get_minimap_heading
    img_0 = _render_player_arrow_minimap(0.0, size=212, seed=901)
    img_180 = _render_player_arrow_minimap(180.0, size=212, seed=902)
    h0 = py_tracker.get_minimap_heading(img_0)
    h180 = py_tracker.get_minimap_heading(img_180)
    assert h0 is not None and h180 is not None
    assert abs(shortest_angle_delta(0.0, h0)) < 6.0, f"tracker h0={h0}"
    assert abs(shortest_angle_delta(180.0, h180)) < 6.0, f"tracker h180={h180}"
    assert abs(shortest_angle_delta(h0, h180)) > 170.0, "tracker get_minimap_heading 180-deg ambiguity!"

    # Verify camera heading via PureOpenCVTracker.get_minimap_camera_heading
    cone_225 = _render_bgra_cone_minimap(225.0, size=212, seed=903)
    cam_225 = py_tracker.get_minimap_camera_heading(cone_225)
    assert cam_225 is not None
    assert abs(shortest_angle_delta(225.0, cam_225)) < 5.0, f"tracker cam_225={cam_225}"
    assert py_tracker.last_camera_method == "bgra_alpha"

    # HybridTracker delegates cleanly
    assert hybrid.get_minimap_camera_heading(cone_225) == cam_225

    # Angle helper edge checks
    assert wrap_angle_360(-90.0) == 270.0
    assert wrap_angle_360(360.0) == 0.0
    assert wrap_angle_360(-0.0) == 0.0
    assert shortest_angle_delta(350.0, 10.0) == 20.0
    assert shortest_angle_delta(10.0, 350.0) == -20.0

    print("    PASS (PureOpenCVTracker & HybridTracker verified)")


def main() -> None:
    test_bgra_view_cone()
    test_bgr_blend_view_cone()
    test_player_arrow_360()
    test_circular_angle_smoother()
    test_nav_relative_angle()
    test_tracker_integration()
    print("\nALL TRACK 2A HEADING360 VERIFICATION CHECKS PASSED 100%!")


if __name__ == "__main__":
    main()
