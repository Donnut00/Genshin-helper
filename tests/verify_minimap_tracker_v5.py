"""
Offline verification suite for Track 2B:
  - src/tile_cache.py (LruTerrainTileCache + HoYoLAB underground diag(1,-1,1) Y-flip guard)
  - src/minimap_tracker.py (MinimapGate, HybridMinimapTracker, 4-state FSM, 2-zoom hysteresis,
    Follow-View ORB rotation recovery, bounded phaseCorrelate fallback, floor comparison, LocalizationWorker)

Run via:
    .\\.venv\\Scripts\\python.exe tests/verify_minimap_tracker_v5.py
"""
from __future__ import annotations

import math
from pathlib import Path
import random
import sys
import tempfile
import time
from typing import Optional, Tuple

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.heading360 import shortest_angle_delta, wrap_angle_360
from src.image_io import write_image
from src.minimap_tracker import (
    HybridMinimapTracker,
    LocalizationScheduler,
    LocalizationWorker,
    MODE_FOLLOW_VIEW,
    MODE_LOCK_DIRECTION,
    MinimapGate,
    MinimapTrackerConfig,
    PoseMeasurement,
    STATE_ACQUIRING,
    STATE_LOST,
    STATE_RELOCATING,
    STATE_TRACKING,
    ZOOM_CITY,
    ZOOM_OPEN_WORLD,
    visible_snapshot,
)
from src.tile_cache import (
    LruTerrainTileCache,
    compose_underground_local_to_surface,
    extract_underground_patch,
)


# ---------------------------------------------------------------------------
# Synthetic Realistic Genshin Minimap Renderer (over real terrain_n2_v4.npz)
# ---------------------------------------------------------------------------
def _draw_cyan_player_arrow(img_bgr: np.ndarray, heading_cw_deg: float) -> None:
    """Draws a crisp Genshin-style 3-tip cyan player arrow at minimap center."""
    h, w = img_bgr.shape[:2]
    cx, cy = w * 0.5, h * 0.5
    s = min(h, w) / 218.0
    # Arrow polygon in local coords pointing North (-Y): nose, right wing, tail notch, left wing
    pts_local = np.array(
        [
            [0.0, -14.0 * s],
            [10.0 * s, 9.0 * s],
            [0.0, 4.0 * s],
            [-10.0 * s, 9.0 * s],
        ],
        dtype=np.float64,
    )
    rad = math.radians(heading_cw_deg)
    c, sn = math.cos(rad), math.sin(rad)
    rot = np.array([[c, -sn], [sn, c]], dtype=np.float64)
    pts_rot = (pts_local @ rot.T) + np.array([cx, cy])
    poly = np.round(pts_rot).astype(np.int32)
    cv2.fillPoly(img_bgr, [poly], (252, 235, 10), lineType=cv2.LINE_AA)


def render_synthetic_minimap(
    tile_cache: LruTerrainTileCache,
    map_id: int,
    world_x: float,
    world_y: float,
    *,
    span_world: float = 360.0,
    size: int = 218,
    camera_heading_cw: float = 45.0,
    body_heading_cw: float = 45.0,
    follow_view: bool = False,
    with_bgra_alpha: bool = False,
    custom_terrain_gray: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    Renders a realistic circular Genshin minimap (BGR or BGRA) centered on `(world_x, world_y)`
    from real `terrain_n2_v4.npz` tiles (or `custom_terrain_gray`), including:
      - Follow-View terrain rotation (CCW by +camera_heading_cw so top = front, camera cone pinned at 0°)
      - 90° translucent white camera view cone (alpha = 0.25)
      - Circular Genshin bezel ring + dark exterior corners (no hardcoded top "N" required)
      - 3-tip cyan player arrow at center
    """
    if custom_terrain_gray is not None:
        base_gray = cv2.resize(custom_terrain_gray, (size, size), interpolation=cv2.INTER_LINEAR)
    else:
        ppu = float(size) / float(span_world)
        # Extract extra margin when rotating for Follow View so corners never clip
        half_ext = 0.5 * span_world * (1.48 if follow_view else 1.02)
        patch, _ = tile_cache.extract_local_patch(map_id, world_x, world_y, half_ext, target_px_per_unit=ppu)
        assert patch is not None, f"Failed to extract patch at ({world_x}, {world_y})"

        if follow_view:
            ph, pw = patch.shape[:2]
            # Facing +H CW from North -> terrain rotates CCW by +H so front is at top (0°)
            M_rot = cv2.getRotationMatrix2D(((pw - 1) * 0.5, (ph - 1) * 0.5), float(camera_heading_cw), 1.0)
            rot = cv2.warpAffine(patch, M_rot, (pw, ph), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            ox = (pw - size) // 2
            oy = (ph - size) // 2
            base_gray = rot[oy : oy + size, ox : ox + size].copy()
        else:
            ph, pw = patch.shape[:2]
            ox = max(0, (pw - size) // 2)
            oy = max(0, (ph - size) // 2)
            base_gray = patch[oy : oy + size, ox : ox + size].copy()
            if base_gray.shape != (size, size):
                base_gray = cv2.resize(base_gray, (size, size), interpolation=cv2.INTER_LINEAR)

    # Convert terrain grayscale into muted natural map colors (slight warm parchment tint)
    bgr = np.stack(
        [
            np.clip(base_gray.astype(np.float32) * 0.88, 0, 255).astype(np.uint8),
            np.clip(base_gray.astype(np.float32) * 0.96, 0, 255).astype(np.uint8),
            np.clip(base_gray.astype(np.float32) * 0.92, 0, 255).astype(np.uint8),
        ],
        axis=2,
    )

    center = (size // 2, size // 2)
    outer_r = int(round(0.46 * size))

    # Camera view cone: pinned at 0° (North) in Follow View, else at camera_heading_cw
    cone_ang = 0.0 if follow_view else wrap_angle_360(camera_heading_cw)
    # OpenCV ellipse 0° = East, CW positive -> North (0° CW) = -90° OpenCV
    cv_center_ang = cone_ang - 90.0
    cone_mask = np.zeros((size, size), dtype=np.uint8)
    cv2.ellipse(
        cone_mask,
        center,
        (int(round(0.43 * size)), int(round(0.43 * size))),
        0.0,
        cv_center_ang - 45.0,
        cv_center_ang + 45.0,
        255,
        -1,
    )
    bgr_f = bgr.astype(np.float32)
    bgr_f[cone_mask > 0] = bgr_f[cone_mask > 0] * 0.75 + 255.0 * 0.25
    bgr = np.clip(bgr_f, 0, 255).astype(np.uint8)

    # Circular minimap mask + realistic dark exterior + cream/dark bezel ring
    circle_mask = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(circle_mask, center, outer_r, 255, -1)
    bgr[circle_mask == 0] = (18, 20, 24)
    cv2.circle(bgr, center, outer_r + 2, (45, 48, 52), 2, lineType=cv2.LINE_AA)
    cv2.circle(bgr, center, outer_r, (215, 224, 232), 2, lineType=cv2.LINE_AA)

    # Player cyan arrow: relative to screen top (in Follow View, body_heading - camera_heading)
    arrow_screen_ang = wrap_angle_360(body_heading_cw - camera_heading_cw) if follow_view else wrap_angle_360(body_heading_cw)
    _draw_cyan_player_arrow(bgr, arrow_screen_ang)

    if with_bgra_alpha:
        alpha = np.full((size, size), 255, dtype=np.uint8)
        alpha[cone_mask > 0] = 191  # Real DX11 Genshin alpha drop inside camera cone
        bgra = np.dstack([bgr, alpha])
        return np.ascontiguousarray(bgra)
    return np.ascontiguousarray(bgr)


# ---------------------------------------------------------------------------
# Test (a): LruTerrainTileCache Bound, Real NPZ Slicing, Disk Cache & Y-Flip
# ---------------------------------------------------------------------------
def test_a_tile_cache_and_underground_y_flip() -> None:
    print("[Test a] LruTerrainTileCache max_tiles=16 bound, real NPZ slice, disk cache, underground Y-flip...")
    cache = LruTerrainTileCache(max_tiles=16, tile_size=256)

    # 1. Strictly respect max_tiles=16 under 60 random tile queries across Map 2
    rng = random.Random(20261121)
    loaded_count = 0
    for i in range(60):
        tx = rng.randint(7, 28)
        ty = rng.randint(6, 25)
        tile = cache.get_tile(2, -2, tx, ty)
        assert cache.tile_count <= 16, f"Cache exceeded max_tiles=16 at query {i}: {cache.tile_count}"
        if tile is not None:
            assert tile.shape == (256, 256) and tile.dtype == np.uint8
            loaded_count += 1
    assert loaded_count >= 50, f"Expected >= 50 valid Map 2 tiles, got {loaded_count}"
    assert cache.evictions > 0, "Expected LRU evictions under 60 random tile queries"
    assert cache.approx_ram_bytes <= 16 * 256 * 256, f"Unexpected RAM usage: {cache.approx_ram_bytes}"

    # 2. Verify accurate sub-pixel slice from real terrain_n2_v4.npz against raw canvas
    with np.load(ROOT / "src" / "data" / "terrain_n2_v4.npz") as zf:
        raw_canvas = zf["map_2_gray"].copy()
        raw_meta = zf["map_2_meta"].copy()
    tx0, ty0, z_native, orig_x, orig_y = [float(v) for v in raw_meta]
    pow2z = 2.0 ** z_native  # 0.25 px/unit

    for label, wx, wy in [("MondstadtCity", -503.0, -1267.0), ("LiyueHarbor", -1103.0, 2101.0)]:
        patch, (ox, oy, ppu) = cache.extract_local_patch(2, wx, wy, half_extent_world=200.0, target_px_per_unit=0.25)
        assert patch is not None and patch.shape == (100, 100), f"{label} bad patch shape"
        assert abs(ppu - 0.25) < 1e-6
        # Center pixel (49.5) must map to exact (wx, wy)
        cx_w = ox + 50.0 / ppu
        cy_w = oy + 50.0 / ppu
        assert abs(cx_w - wx) < 1e-6 and abs(cy_w - wy) < 1e-6

        # Compare directly against raw_canvas crop at exact integer pixel offset
        rx_f = (ox + orig_x) * pow2z - tx0 * 256.0
        ry_f = (oy + orig_y) * pow2z - ty0 * 256.0
        assert abs(rx_f - round(rx_f)) < 1e-6 and abs(ry_f - round(ry_f)) < 1e-6
        rx, ry = int(round(rx_f)), int(round(ry_f))
        ref_crop = raw_canvas[ry : ry + 100, rx : rx + 100]
        corr = float(cv2.matchTemplate(ref_crop, patch, cv2.TM_CCOEFF_NORMED)[0, 0])
        assert corr > 0.999, f"{label} NPZ slice correlation too low: {corr:.4f}"

    # 3. Verify lazy disk tile cache takes precedence over NPZ (Unicode-safe image_io)
    with tempfile.TemporaryDirectory(prefix="gh_tiles_test_") as tmp_dir:
        disk_cache = LruTerrainTileCache(max_tiles=16, tile_size=256, disk_cache_dir=tmp_dir)
        synthetic_tile = np.full((256, 256), 173, dtype=np.uint8)
        tile_path = Path(tmp_dir) / "2" / "-1" / "45_30.png"
        assert write_image(tile_path, synthetic_tile)
        got = disk_cache.get_tile(2, -1, 45, 30)
        assert got is not None and got.shape == (256, 256) and int(got[128, 128]) == 173

    # 4. Verify HoYoLAB underground Y-flip guard (`diag(1, -1, 1)` + `l_y/r_y` ordering)
    # Create an overlay PNG with a bright marker strictly in the NORTH (top 20%) and dark SOUTH (bottom 20%)
    ug_north_up = np.full((200, 200), 40, dtype=np.uint8)
    ug_north_up[20:55, 70:130] = 235  # North landmark (top)
    ug_north_up[150:180, 70:130] = 95  # South landmark (bottom)

    # Case 4a: Standard HoYoLAB multimap_v4.json bounds (l_y < r_y, surface Y-down)
    bounds_std = {"l_x": -4000.0, "l_y": -7400.0, "r_x": -3200.0, "r_y": -6600.0}
    p_std, _ = cache.extract_underground_patch(ug_north_up, bounds_std, -3600.0, -7000.0, 400.0, target_px_per_unit=0.25)
    assert p_std is not None
    top_mean = float(np.mean(p_std[:50, :]))
    bot_mean = float(np.mean(p_std[50:, :]))
    assert top_mean > bot_mean + 8.0, f"Standard HoYoLAB overlay inverted! top={top_mean:.1f}, bot={bot_mean:.1f}"

    # Case 4b: Opposite-sign HoYoLAB underground metadata (north-up +Y: l_y=+7400, r_y=+6600 vs surface world_y=-7000)
    # Must auto-apply diag(1, -1, 1) so North stays at top!
    bounds_opp = {"l_x": -4000.0, "l_y": 7400.0, "r_x": -3200.0, "r_y": 6600.0}
    mat_opp, applied_reflect = compose_underground_local_to_surface(
        bounds_opp, 200, 200, world_y=-7000.0, half_extent_world=400.0
    )
    assert applied_reflect is True, "Expected diag(1, -1, 1) Y-reflection to trigger on opposite-sign HoYoLAB bounds"
    assert mat_opp[1, 1] > 0.0, f"Expected positive surface Y scale after diag(1,-1,1), got {mat_opp[1, 1]}"
    p_opp, _ = extract_underground_patch(ug_north_up, bounds_opp, -3600.0, -7000.0, 400.0, target_px_per_unit=0.25)
    assert p_opp is not None
    diff_std_opp = float(np.mean(cv2.absdiff(p_std, p_opp)))
    assert diff_std_opp < 1.0, f"diag(1,-1,1) underground patch diverged from standard: diff={diff_std_opp:.2f}"

    # Case 4c: South-up upside-down overlay (row 0 = South -6600, row H = North -7400, l_y > r_y)
    ug_south_up = cv2.flip(ug_north_up, 0)
    bounds_flipped = {"l_x": -4000.0, "l_y": -6600.0, "r_x": -3200.0, "r_y": -7400.0}
    p_flip, _ = cache.extract_underground_patch(
        ug_south_up, bounds_flipped, -3600.0, -7000.0, 400.0, target_px_per_unit=0.25
    )
    assert p_flip is not None
    diff_std_flip = float(np.mean(cv2.absdiff(p_std, p_flip)))
    assert diff_std_flip < 1.5, f"Flipped l_y>r_y overlay failed to un-invert: diff={diff_std_flip:.2f}"

    print(
        f"  PASS (cache_tiles={cache.tile_count}/16, evictions={cache.evictions}, "
        f"ram={cache.approx_ram_bytes // 1024}KB, underground_y_diff={diff_std_opp:.2f})"
    )


# ---------------------------------------------------------------------------
# Test (b): 25-Step Moving Minimap, Stationary Skip (< 1 ms), 2-Zoom Hysteresis
# ---------------------------------------------------------------------------
def test_b_moving_trajectory_stationary_and_2zoom() -> None:
    print("[Test b] 25-step trajectory on real Mondstadt terrain, stationary skip (< 1ms), 2-zoom hysteresis...")
    cache = LruTerrainTileCache(max_tiles=16)
    tracker = HybridMinimapTracker(tile_cache=cache)

    wx0, wy0 = -500.0, -1266.0  # Mondstadt City / Starfell region
    t = 100.0
    anchor_pose = tracker.set_map_m_anchor(2, wx0, wy0, timestamp=t)
    assert anchor_pose.state == STATE_TRACKING
    assert anchor_pose.world_x == wx0 and anchor_pose.world_y == wy0
    assert anchor_pose.lat == wy0 and anchor_pose.lng == wx0

    # 1. Translate 25 steps smoothly across real terrain (realistic 10 Hz sprint speed: ~1.35 world units/step)
    max_err = 0.0
    errs = []
    last_frame = None
    for step_idx in range(1, 26):
        t += 0.1
        true_x = wx0 + step_idx * 1.1
        true_y = wy0 + step_idx * 0.8
        cam_ang = wrap_angle_360(35.0 + step_idx * 6.0)
        body_ang = wrap_angle_360(125.0 + step_idx * 4.0)
        mm = render_synthetic_minimap(
            cache,
            2,
            true_x,
            true_y,
            span_world=360.0,
            camera_heading_cw=cam_ang,
            body_heading_cw=body_ang,
            follow_view=False,
            with_bgra_alpha=True,
        )
        pose = tracker.step(mm, now=t)
        assert pose.state == STATE_TRACKING, f"Lost track at step {step_idx}: {pose}"
        assert pose.world_x is not None and pose.world_y is not None
        assert pose.lat == pose.world_y and pose.lng == pose.world_x
        assert pose.minimap_mode == MODE_LOCK_DIRECTION
        assert pose.zoom_regime == ZOOM_OPEN_WORLD
        err = math.hypot(pose.world_x - true_x, pose.world_y - true_y)
        errs.append(err)
        max_err = max(max_err, err)
        last_frame = mm

    assert max_err < 3.0, f"Tracking error exceeded 3.0 world units: max_err={max_err:.2f}, mean={np.mean(errs):.2f}"

    # 2. Stationary frame skips matcher and executes in < 1 ms
    assert last_frame is not None
    # Warmup one stationary call so JIT/caches settle
    t += 0.1
    calls_before = tracker.matcher_calls
    pose_stat_0 = tracker.step(last_frame, now=t)
    assert pose_stat_0.stationary is True, "Expected stationary=True on identical frame"
    assert tracker.matcher_calls == calls_before, "Matcher must NOT run on stationary frame"

    stat_times_ms = []
    for _ in range(15):
        t += 0.1
        t0 = time.perf_counter()
        p_s = tracker.step(last_frame, now=t)
        stat_times_ms.append((time.perf_counter() - t0) * 1000.0)
        assert p_s.stationary is True
        assert p_s.state == STATE_TRACKING
    assert tracker.matcher_calls == calls_before, "Matcher ran during stationary frames"
    median_stat_ms = float(np.median(stat_times_ms))
    assert median_stat_ms < 1.0, f"Stationary step too slow: median={median_stat_ms:.3f} ms (expected < 1.0 ms)"

    # 2b. Verify rotating ONLY the player arrow while standing still stays stationary (skips matcher!)
    #     AND updates body_heading immediately!
    t += 0.1
    mm_rot_body = render_synthetic_minimap(
        cache,
        2,
        wx0 + 25 * 1.1,
        wy0 + 25 * 0.8,
        span_world=360.0,
        camera_heading_cw=wrap_angle_360(35.0 + 25 * 6.0),
        body_heading_cw=15.0,
        follow_view=False,
        with_bgra_alpha=True,
    )
    calls_before_body_rot = tracker.matcher_calls
    pose_rot_body = tracker.step(mm_rot_body, now=t)
    assert pose_rot_body.stationary is True, "Rotating only center player arrow should stay stationary"
    assert tracker.matcher_calls == calls_before_body_rot, "Matcher should be skipped when only player arrow rotated"
    assert pose_rot_body.body_heading is not None

    # 2c. Verify rotating camera while standing still STILL updates camera_heading!
    t += 0.1
    mm_rot_cam = render_synthetic_minimap(
        cache,
        2,
        wx0 + 25 * 1.1,
        wy0 + 25 * 0.8,
        span_world=360.0,
        camera_heading_cw=260.0,
        body_heading_cw=10.0,
        follow_view=False,
        with_bgra_alpha=True,
    )
    pose_rot_cam = tracker.step(mm_rot_cam, now=t)
    assert pose_rot_cam.camera_heading is not None
    assert abs(shortest_angle_delta(260.0, pose_rot_cam.camera_heading)) < 45.0, (
        f"Camera heading failed to move toward 260° while standing still: {pose_rot_cam.camera_heading}"
    )

    # 2d. Verify 3-channel BGR (no alpha) trajectory over real Liyue Harbor terrain
    liyue_x, liyue_y = -1100.0, 2100.0
    t += 0.1
    tracker.set_map_m_anchor(2, liyue_x, liyue_y, timestamp=t)
    liyue_max_err = 0.0
    for step_idx in range(1, 11):
        t += 0.1
        tx = liyue_x + step_idx * 1.0
        ty = liyue_y - step_idx * 0.9
        mm_bgr = render_synthetic_minimap(
            cache,
            2,
            tx,
            ty,
            span_world=360.0,
            camera_heading_cw=120.0,
            body_heading_cw=120.0,
            follow_view=False,
            with_bgra_alpha=False,  # 3-channel BGR path
        )
        p_ly = tracker.step(mm_bgr, now=t)
        assert p_ly.state == STATE_TRACKING and p_ly.world_x is not None and p_ly.world_y is not None
        liyue_max_err = max(liyue_max_err, math.hypot(p_ly.world_x - tx, p_ly.world_y - ty))
    assert liyue_max_err < 3.0, f"Liyue 3-channel BGR error >= 3.0: {liyue_max_err:.2f}"

    # 3. 2-Zoom city transition requires 2 consecutive clear wins (anti-jitter!)
    city_x, city_y = -480.0, -1245.0  # Rich Mondstadt City terrain
    t += 0.1
    tracker.set_map_m_anchor(2, city_x, city_y, timestamp=t)
    assert tracker.zoom_regime == ZOOM_OPEN_WORLD

    mm_open = render_synthetic_minimap(
        cache, 2, city_x, city_y, span_world=360.0, camera_heading_cw=60.0, with_bgra_alpha=True
    )
    mm_city_1 = render_synthetic_minimap(
        cache, 2, city_x + 1.0, city_y + 1.0, span_world=120.0, camera_heading_cw=60.0, with_bgra_alpha=True
    )
    mm_city_2 = render_synthetic_minimap(
        cache, 2, city_x + 2.0, city_y + 2.0, span_world=120.0, camera_heading_cw=60.0, with_bgra_alpha=True
    )

    # Establish open_world baseline
    t += 0.1
    p0 = tracker.step(mm_open, now=t)
    assert p0.zoom_regime == ZOOM_OPEN_WORLD

    # Single city frame -> must NOT switch yet (anti-jitter)
    t += 0.1
    p_jitter = tracker.step(mm_city_1, now=t)
    assert p_jitter.zoom_regime == ZOOM_OPEN_WORLD, "Single city frame must NOT immediately switch zoom regime!"

    # Back to open_world -> streak resets cleanly
    t += 0.1
    p_back = tracker.step(mm_open, now=t)
    assert p_back.zoom_regime == ZOOM_OPEN_WORLD

    # Now 2 CONSECUTIVE city frames -> frame 1 stays open_world, frame 2 switches to city!
    t += 0.1
    p_c1 = tracker.step(mm_city_1, now=t)
    assert p_c1.zoom_regime == ZOOM_OPEN_WORLD, "Zoom switched on 1st consecutive win (expected 2)!"
    t += 0.1
    p_c2 = tracker.step(mm_city_2, now=t)
    assert p_c2.zoom_regime == ZOOM_CITY, f"Expected switch to city on 2nd consecutive win, got {p_c2.zoom_regime}"

    print(
        f"  PASS (25-step max_err={max_err:.2f}w mean={np.mean(errs):.2f}w, "
        f"stationary_median={median_stat_ms:.3f}ms, 2-zoom hysteresis verified)"
    )


# ---------------------------------------------------------------------------
# Test (c): Follow-View Rotating Minimap (ORB + estimateAffinePartial2D)
# ---------------------------------------------------------------------------
def test_c_follow_view_rotation_recovery() -> None:
    print("[Test c] Follow-View rotating minimap ORB + estimateAffinePartial2D heading recovery (< 6 deg)...")
    cache = LruTerrainTileCache(max_tiles=16)
    tracker = HybridMinimapTracker(tile_cache=cache)

    wx, wy = -500.0, -1266.0
    t = 200.0
    tracker.set_map_m_anchor(2, wx, wy, timestamp=t)

    test_headings = [35.0, 90.0, 145.0, 225.0, 270.0, 315.0]
    max_ang_err = 0.0
    for idx, true_heading in enumerate(test_headings):
        t += 0.1
        # Slight position walk while rotating camera in Follow View
        px = wx + idx * 1.5
        py = wy + idx * 1.0
        mm_fv = render_synthetic_minimap(
            cache,
            2,
            px,
            py,
            span_world=360.0,
            camera_heading_cw=true_heading,
            body_heading_cw=true_heading,
            follow_view=True,
            with_bgra_alpha=True,
        )
        pose = tracker.step(mm_fv, now=t)
        assert pose.state == STATE_TRACKING, f"Lost track in Follow View at H={true_heading}: {pose}"
        assert pose.minimap_mode == MODE_FOLLOW_VIEW, f"Failed to detect follow_view at H={true_heading}: {pose}"
        assert pose.view_heading is not None
        ang_err = abs(shortest_angle_delta(true_heading, pose.view_heading))
        max_ang_err = max(max_ang_err, ang_err)
        assert ang_err < 6.0, f"Follow-View heading error too large at {true_heading}°: got {pose.view_heading:.2f}° (err={ang_err:.2f}°)"

    # Switch back to Lock Direction (camera cone leaves North -> 130° East-South-East)
    t += 0.1
    mm_lock = render_synthetic_minimap(
        cache,
        2,
        wx + 9.0,
        wy + 6.0,
        span_world=360.0,
        camera_heading_cw=130.0,
        body_heading_cw=130.0,
        follow_view=False,
        with_bgra_alpha=True,
    )
    pose_lock = tracker.step(mm_lock, now=t)
    assert pose_lock.minimap_mode == MODE_LOCK_DIRECTION, f"Expected return to lock_direction, got {pose_lock.minimap_mode}"
    print(f"  PASS (6 headings 35..315 deg, max_heading_err={max_ang_err:.2f} deg, lock_direction return OK)")


# ---------------------------------------------------------------------------
# Test (d): 4-State FSM, pause(), Relocation, MinimapGate & phaseCorrelate Budget
# ---------------------------------------------------------------------------
def test_d_fsm_pause_gate_and_phase_budget() -> None:
    print("[Test d] 4-State FSM transitions, pause() freeze, MinimapGate, and phaseCorrelate 5-frame budget...")
    cfg = MinimapTrackerConfig(
        acquire_hits=2,
        relocate_hits=3,
        smoothing_alpha=0.4,
        lost_timeout_seconds=1.5,
        max_phase_fallback_frames=5,
    )
    cache = LruTerrainTileCache(max_tiles=16)
    tracker = HybridMinimapTracker(tile_cache=cache, config=cfg)

    # 1. LOST -> ACQUIRING (hit 1) -> TRACKING (hit 2)
    assert tracker.state == STATE_LOST
    p1 = tracker.ingest_measurement(PoseMeasurement(found=True, map_id=2, world_x=100.0, world_y=200.0, confidence=0.85, match_method="global"), now=10.0)
    assert p1.state == STATE_ACQUIRING and p1.world_x is None

    p2 = tracker.ingest_measurement(PoseMeasurement(found=True, map_id=2, world_x=104.0, world_y=204.0, confidence=0.88, match_method="global"), now=10.1)
    assert p2.state == STATE_TRACKING and p2.world_x is not None
    assert abs(p2.world_x - 102.0) < 1e-6 and abs(p2.world_y - 202.0) < 1e-6

    # 2. EMA alpha = 0.4 verification
    p3 = tracker.ingest_measurement(PoseMeasurement(found=True, map_id=2, world_x=112.0, world_y=202.0, confidence=0.90, match_method="annulus_template"), now=10.2)
    assert p3.state == STATE_TRACKING
    assert abs(p3.world_x - (102.0 + 0.4 * 10.0)) < 1e-6  # 106.0

    # 3. pause() freezes timeout across a 12-second Map M pause (>> 1.5s timeout!)
    tracker.pause(now=10.5, reason="map_m_open")
    tracker.pause(now=22.5, reason="map_m_open")
    p_resume = tracker.ingest_measurement(PoseMeasurement(found=True, map_id=2, world_x=106.0, world_y=202.0, confidence=0.85, match_method="annulus_template"), now=22.6)
    assert p_resume.state == STATE_TRACKING, f"pause() failed to prevent LOST timeout: {p_resume}"

    # 4. Teleport outside velocity gate -> RELOCATING (hits 1, 2) -> TRACKING (hit 3)
    p_r1 = tracker.ingest_measurement(PoseMeasurement(found=True, map_id=2, world_x=900.0, world_y=900.0, confidence=0.90, match_method="global"), now=22.7)
    assert p_r1.state == STATE_RELOCATING and abs(p_r1.world_x - 106.0) < 1e-6  # Keeps old confirmed pos

    p_r2 = tracker.ingest_measurement(PoseMeasurement(found=True, map_id=2, world_x=902.0, world_y=901.0, confidence=0.90, match_method="global"), now=22.8)
    assert p_r2.state == STATE_RELOCATING and abs(p_r2.world_x - 106.0) < 1e-6

    p_r3 = tracker.ingest_measurement(PoseMeasurement(found=True, map_id=2, world_x=901.0, world_y=899.0, confidence=0.90, match_method="global"), now=22.9)
    assert p_r3.state == STATE_TRACKING and abs(p_r3.world_x - 901.0) < 2.0 and p_r3.reason == "relocation_confirmed"

    # 5. Timeout (> 1.5s invalid frames) -> LOST
    p_stale = tracker.ingest_measurement(PoseMeasurement(found=False, reason="missed"), now=23.5)
    assert p_stale.state == STATE_TRACKING  # Still within 1.5s
    p_lost = tracker.ingest_measurement(PoseMeasurement(found=False, reason="missed"), now=24.6)
    assert p_lost.state == STATE_LOST and p_lost.world_x is None

    # 6. MinimapGate rejections & HDR clipping flag
    gate = MinimapGate()
    assert not gate.check(np.zeros((218, 218, 3), dtype=np.uint8)).valid  # Black screen
    assert not gate.check(np.full((218, 218, 3), 255, dtype=np.uint8)).valid  # White flash
    assert not gate.check(np.full((218, 218, 3), 120, dtype=np.uint8)).valid  # Flat gray
    raw_patch, _ = cache.extract_local_patch(2, -500.0, -1266.0, 180.0, target_px_per_unit=0.6)
    assert raw_patch is not None
    unbordered_bgr = cv2.cvtColor(cv2.resize(raw_patch, (218, 218)), cv2.COLOR_GRAY2BGR)
    assert not gate.check(unbordered_bgr).valid, "Unbordered square terrain should fail circular border check"
    # HDR clipped minimap (washed out high white ratio) still flags hdr_clipped=True
    mm_hdr = render_synthetic_minimap(cache, 2, -500.0, -1266.0)
    mm_hdr = np.clip(mm_hdr.astype(np.int32) + 145, 0, 255).astype(np.uint8)
    assert gate.check(mm_hdr).hdr_clipped is True

    # 7. phaseCorrelate 5-frame sparse-terrain budget -> frame 6 fails safe to LOST!
    t = 300.0
    tracker.set_map_m_anchor(2, -500.0, -1266.0, timestamp=t)
    # Build a synthetic high-frequency periodic wave pattern that does NOT correlate with Mondstadt (< 0.45)
    # yet shifts smoothly frame-to-frame for phaseCorrelate!
    big_wave = np.zeros((420, 420), dtype=np.float32)
    yy, xx = np.mgrid[0:420, 0:420]
    big_wave = (
        120.0
        + 32.0 * np.sin(xx * 0.17 + yy * 0.09)
        + 26.0 * np.cos(xx * 0.11 - yy * 0.21)
        + 18.0 * np.sin(np.hypot(xx - 210.0, yy - 210.0) * 0.23)
    )
    big_wave_u8 = np.clip(big_wave, 20, 220).astype(np.uint8)

    # Seed prev_phase_gray on an initial matched frame (or frame 0)
    t += 0.1
    mm_seed = render_synthetic_minimap(cache, 2, -500.0, -1266.0, with_bgra_alpha=True)
    p_seed = tracker.step(mm_seed, now=t)
    assert p_seed.state == STATE_TRACKING

    # Feed first wave frame to establish wave phase reference, then shift smoothly
    # Wait: frame 1 vs Mondstadt seed won't phase-correlate well, so seed with wave frame 0 first by setting _prev_phase_gray?
    # Even better: test realistic transition where player walks off known tiles onto sparse ocean/desert!
    # Frame 1..5 use consecutive shifted wave frames; first let's align prev_phase on wave frame 0 via a controlled anchor:
    mm_w0 = render_synthetic_minimap(cache, 2, -500.0, -1266.0, custom_terrain_gray=big_wave_u8[100:318, 100:318])
    tracker._prev_phase_gray = tracker._prepare_phase_patch(cv2.cvtColor(mm_w0, cv2.COLOR_BGR2GRAY))

    prev_x = tracker.fsm._x
    assert prev_x is not None
    for k in range(1, 6):
        t += 0.1
        # Player moves East -> terrain window shifts East in big_wave (+3 px per frame) -> features scroll West (-3 px)
        ox = 100 + k * 4
        oy = 100
        mm_wk = render_synthetic_minimap(
            cache, 2, -500.0, -1266.0, custom_terrain_gray=big_wave_u8[oy : oy + 218, ox : ox + 218]
        )
        pk = tracker.step(mm_wk, now=t)
        assert pk.state == STATE_TRACKING, f"Expected TRACKING on phaseCorrelate frame {k}/5, got {pk}"
        assert pk.match_method == "motion", f"Expected motion fallback on frame {k}/5, got {pk.match_method}"
        assert pk.world_x is not None and pk.world_x > prev_x, f"Expected Eastward motion on frame {k}: {pk.world_x} <= {prev_x}"
        prev_x = pk.world_x

    # Frame 6: 5-frame phaseCorrelate budget exhausted -> MUST fail safe immediately to LOST!
    t += 0.1
    ox = 100 + 6 * 4
    mm_w6 = render_synthetic_minimap(
        cache, 2, -500.0, -1266.0, custom_terrain_gray=big_wave_u8[100:318, ox : ox + 218]
    )
    p6 = tracker.step(mm_w6, now=t)
    assert p6.state == STATE_LOST, f"Expected LOST after 5-frame phase budget exhausted, got {p6}"
    assert p6.world_x is None and p6.world_y is None
    assert p6.reason == "motion_fallback_budget_exceeded"
    print("  PASS (LOST->ACQUIRING->TRACKING->pause->RELOCATING->TRACKING->LOST & 5-frame phase budget -> LOST)")


# ---------------------------------------------------------------------------
# Test (e): Multi-Layer Floor Comparison (3 consecutive wins, margin >= 0.08)
# ---------------------------------------------------------------------------
def test_e_multilayer_floor_comparison() -> None:
    print("[Test e] Multi-layer floor comparison (3 consecutive wins with margin >= 0.08 hysteresis)...")
    cache = LruTerrainTileCache(max_tiles=16)
    tracker = HybridMinimapTracker(tile_cache=cache)
    tracker.set_map_m_anchor(2, -500.0, -1266.0, is_underground=False, timestamp=400.0)

    # Real surface patch from terrain_n2_v4.npz
    surf_patch, _ = cache.extract_local_patch(2, -500.0, -1266.0, 180.0, target_px_per_unit=0.5)
    assert surf_patch is not None

    # Synthetic underground cavern patch with sharp distinctive tunnel corridors
    ug_patch = np.full(surf_patch.shape, 28, dtype=np.uint8)
    h, w = ug_patch.shape
    cv2.line(ug_patch, (15, 25), (w - 15, h - 25), 210, 8)
    cv2.line(ug_patch, (w - 20, 20), (20, h - 20), 195, 6)
    cv2.circle(ug_patch, (w // 2, h // 3), 24, 230, 5)
    cv2.rectangle(ug_patch, (w // 4, h // 2), (3 * w // 4, 3 * h // 4), 180, 4)

    mm_surf = render_synthetic_minimap(cache, 2, -500.0, -1266.0, custom_terrain_gray=surf_patch)
    mm_ug = render_synthetic_minimap(cache, 2, -500.0, -1266.0, custom_terrain_gray=ug_patch)

    # 1. Two consecutive underground wins followed by a surface frame -> must NOT switch (streak resets!)
    r1 = tracker.compare_surface_vs_underground(mm_ug, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    assert r1["margin"] >= 0.08 and r1["is_underground"] is False and r1["streak"] == 1
    r2 = tracker.compare_surface_vs_underground(mm_ug, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    assert r2["margin"] >= 0.08 and r2["is_underground"] is False and r2["streak"] == 2
    r_reset = tracker.compare_surface_vs_underground(mm_surf, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    assert r_reset["is_underground"] is False and r_reset["streak"] == 0

    # 2. Ambiguous blended frame with margin < 0.08 -> must NEVER switch even after 5 frames
    blended = cv2.addWeighted(surf_patch, 0.5, ug_patch, 0.5, 0)
    mm_ambig = render_synthetic_minimap(cache, 2, -500.0, -1266.0, custom_terrain_gray=blended)
    for _ in range(5):
        ra = tracker.compare_surface_vs_underground(mm_ambig, blended, blended, candidate_floor_id=78, candidate_floor_name="B1")
        assert abs(ra["margin"]) < 0.01 and ra["is_underground"] is False

    # 3. Three consecutive clear underground wins (margin >= 0.08) -> switches on #3!
    u1 = tracker.compare_surface_vs_underground(mm_ug, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    u2 = tracker.compare_surface_vs_underground(mm_ug, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    assert u1["is_underground"] is False and u2["is_underground"] is False
    u3 = tracker.compare_surface_vs_underground(mm_ug, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    assert u3["switched"] is True and u3["is_underground"] is True and u3["floor_id"] == 78 and u3["floor_name"] == "B1"

    # 4. Three consecutive clear surface wins -> switches back to surface on #3!
    s1 = tracker.compare_surface_vs_underground(mm_surf, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    s2 = tracker.compare_surface_vs_underground(mm_surf, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    assert s1["is_underground"] is True and s2["is_underground"] is True
    s3 = tracker.compare_surface_vs_underground(mm_surf, surf_patch, ug_patch, candidate_floor_id=78, candidate_floor_name="B1")
    assert s3["switched"] is True and s3["is_underground"] is False and s3["floor_id"] is None and s3["floor_name"] == "G"

    print(
        f"  PASS (ug_margin={u3['margin']:+.3f}, surf_margin={s3['margin']:+.3f}, "
        "3-win hysteresis verified both directions)"
    )


# ---------------------------------------------------------------------------
# Test (f): LocalizationWorker Mailbox, visible_snapshot(max_age), Backoff & Shutdown
# ---------------------------------------------------------------------------
def test_f_localization_worker_and_backoff() -> None:
    print("[Test f] LocalizationWorker mailbox, visible_snapshot(max_age), LOST backoff, and clean shutdown...")

    # 1. Exponential backoff verification when LOST
    sched = LocalizationScheduler(tracking_interval=0.10, lost_base_interval=0.25, lost_max_interval=2.0)
    assert abs(sched.interval_for(STATE_TRACKING) - 0.10) < 1e-6
    prev_iv = sched.interval_for(STATE_LOST)
    assert abs(prev_iv - 0.25) < 1e-6
    for _ in range(5):
        sched.observe_result(found=False, state=STATE_LOST)
        nxt = sched.interval_for(STATE_LOST)
        assert nxt > prev_iv or abs(nxt - 2.0) < 1e-6
        prev_iv = nxt
    assert prev_iv <= 2.0 + 1e-6
    sched.observe_result(found=True, state=STATE_TRACKING)
    assert sched.consecutive_misses == 0 and abs(sched.interval_for(STATE_LOST) - 0.25) < 1e-6

    # 2. Live worker mailbox, generation cancellation, pause/resume, visible_snapshot(max_age), clean shutdown
    cache = LruTerrainTileCache(max_tiles=16)
    tracker = HybridMinimapTracker(tile_cache=cache)
    mm_frame = render_synthetic_minimap(cache, 2, -500.0, -1266.0, camera_heading_cw=90.0, with_bgra_alpha=True)
    capture_count = {"n": 0}

    def fake_capture() -> np.ndarray:
        capture_count["n"] += 1
        return mm_frame

    worker = LocalizationWorker(
        tracker,
        fake_capture,
        scheduler=LocalizationScheduler(tracking_interval=0.02, lost_base_interval=0.05, lost_max_interval=0.5),
        max_age=1.5,
        poll_sleep=0.005,
    )
    worker.start()
    try:
        assert worker.is_running
        worker.anchor(2, -500.0, -1266.0)

        # Wait up to 1.0s for worker mailbox to publish a TRACKING result
        deadline = time.perf_counter() + 1.0
        latest = None
        while time.perf_counter() < deadline:
            latest = worker.latest()
            if latest is not None and latest.pose.state == STATE_TRACKING and capture_count["n"] >= 1:
                break
            time.sleep(0.01)

        assert latest is not None, "Worker failed to publish result to mailbox"
        assert latest.pose.state == STATE_TRACKING
        assert latest.pose.world_x is not None

        # Fresh visible_snapshot -> TRACKING
        fresh = visible_snapshot(latest, now=latest.captured_at + 0.3, max_age=1.5)
        assert fresh is not None and fresh.state == STATE_TRACKING

        # Expired visible_snapshot -> clamped to LOST with reason="capture_expired"
        expired = visible_snapshot(latest, now=latest.captured_at + 2.0, max_age=1.5)
        assert expired is not None and expired.state == STATE_LOST
        assert expired.world_x is None and expired.world_y is None
        assert expired.reason == "capture_expired"

        # Pause cancels in-flight generation and clears mailbox
        gen_before = worker.generation
        worker.set_paused(True)
        assert worker.generation == gen_before + 1
        assert worker.latest() is None
        time.sleep(0.05)
        assert worker.latest() is None, "Mailbox must stay empty while paused"

        # Resume increments generation and publishes fresh results again
        worker.set_paused(False)
        assert worker.generation == gen_before + 2
        deadline = time.perf_counter() + 1.0
        resumed = None
        while time.perf_counter() < deadline:
            resumed = worker.latest()
            if resumed is not None and resumed.generation == worker.generation:
                break
            time.sleep(0.01)
        assert resumed is not None and resumed.pose.state == STATE_TRACKING
    finally:
        worker.close(timeout=0.5)

    assert not worker.is_running, "Worker thread did not exit cleanly!"
    assert worker.latest() is None
    print(f"  PASS (captures={capture_count['n']}, backoff/pause/visible_snapshot/shutdown verified)")


def test_g_live_camera_rotation_and_underground_odometry() -> None:
    print("[Test g] Stationary 360-deg camera spin stability + underground no-teleport odometry...")
    cache = LruTerrainTileCache(max_tiles=16)
    tracker = HybridMinimapTracker(tile_cache=cache)
    wx0, wy0 = -500.0, -1266.0
    t = 500.0
    tracker.set_map_m_anchor(2, wx0, wy0, timestamp=t)

    # Seed initial frame at camera=0 deg
    t += 0.1
    mm0 = render_synthetic_minimap(cache, 2, wx0, wy0, camera_heading_cw=0.0, body_heading_cw=0.0, with_bgra_alpha=False)
    p0 = tracker.step(mm0, now=t)
    assert p0.state == STATE_TRACKING

    # Spin camera 360 deg across 8 directions while standing completely still -> must stay TRACKING with ~0 drift!
    for cam in (45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0, 0.0):
        t += 0.1
        mm_spin = render_synthetic_minimap(cache, 2, wx0, wy0, camera_heading_cw=cam, body_heading_cw=0.0, with_bgra_alpha=False)
        ps = tracker.step(mm_spin, now=t)
        assert ps.state == STATE_TRACKING, f"Lost tracking during stationary camera spin at {cam} deg: {ps}"
        assert ps.stationary is True, f"Expected cone-masked stationary_skip at {cam} deg, got {ps.match_method}"
        assert math.hypot(ps.world_x - wx0, ps.world_y - wy0) < 3.0, f"Drifted during stationary spin: ({ps.world_x}, {ps.world_y})"

    # Underground B1 without overlay must NEVER teleport onto surface shorelines or drop to LOST!
    ug_fixture = ROOT / "scratch" / "diagnostics" / "diag_20261121_205434" / "01_game.png"
    if ug_fixture.is_file():
        from src.game_settings import compute_ui_layout
        game_img = cv2.imread(str(ug_fixture))
        assert game_img is not None
        mx, my, mw, mh = compute_ui_layout(game_img.shape[1], game_img.shape[0]).minimap_rect
        mm_real = game_img[my : my + mh, mx : mx + mw]
        ug_x, ug_y = -5071.2, -1638.5
        t += 0.1
        tracker.set_map_m_anchor(2, ug_x, ug_y, is_underground=True, floor_id=78, floor_name="B1", timestamp=t)
        for k in range(10):
            t += 0.1
            pu = tracker.step(mm_real, now=t)
            assert pu.state == STATE_TRACKING, f"Underground B1 dropped to {pu.state} at step {k}: {pu}"
            assert pu.is_underground is True and pu.floor_id == 78
            drift = math.hypot(pu.world_x - ug_x, pu.world_y - ug_y)
            assert drift < 5.0, f"Underground B1 teleported by {drift:.1f}w to ({pu.world_x:.1f}, {pu.world_y:.1f})"

    print("  PASS (360-deg stationary spin zero-drift + real underground B1 zero-teleport verified)")


def main() -> int:
    t0 = time.perf_counter()
    test_a_tile_cache_and_underground_y_flip()
    test_b_moving_trajectory_stationary_and_2zoom()
    test_c_follow_view_rotation_recovery()
    test_d_fsm_pause_gate_and_phase_budget()
    test_e_multilayer_floor_comparison()
    test_f_localization_worker_and_backoff()
    test_g_live_camera_rotation_and_underground_odometry()
    elapsed = time.perf_counter() - t0
    print(f"\nALL TRACK 2B VERIFICATION TESTS PASSED in {elapsed:.2f}s!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
