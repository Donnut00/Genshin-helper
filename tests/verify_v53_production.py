"""
Production verification suite for V5.3 live minimap engine promotion:
  (a) src/heading360.py vectorized polar view-cone accuracy & latency (< 4.0 ms/frame)
  (b) src/hd_assets.py non-blocking HoYoLAB HD tiles & underground floor overlays
  (c) src/global_relocator.py async RootSIFT FLANN warmup, blind Teyvat retrieval & negative guard
  (d) src/minimap_tracker.py V5.3 step_from_hud calibration, IP-clean Paimon auto-bootstrap,
      pause/broadcast gates, bidirectional Auto-Layer-Switch, cold-start & Tier-3 global relocate,
      and domain/negative hold
  (e) Optional real-footage parity smoke check on local dev_snezhnaya & holdout_natlan (if present)

Run via:
    .\\.venv\\Scripts\\python.exe tests/verify_v53_production.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.asset_setup import get_user_data_cache_dir
from src.game_settings import compute_ui_layout
from src.global_relocator import INDEX_VERSION, TeyvatGlobalRelocator
from src.hd_assets import (
    HoyolabHdTileProvider,
    UndergroundOverlayProvider,
    load_multimap_db,
)
from src.heading360 import extract_camera_view_cone, shortest_angle_delta, wrap_angle_360
from src.image_io import write_image
from src.minimap_tracker import (
    HybridMinimapTracker,
    MinimapTrackerConfig,
    STATE_LOST,
    STATE_TRACKING,
)
from src.tile_cache import LruTerrainTileCache


def _load_terrain_n2() -> Dict[int, Tuple[np.ndarray, np.ndarray]]:
    candidates = [
        ROOT / "src" / "data" / "terrain_n2_v4.npz",
        get_user_data_cache_dir() / "terrain_n2_v4.npz",
    ]
    for p in candidates:
        if p.is_file():
            out: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
            with np.load(p) as zf:
                for k in zf.files:
                    if k.endswith("_gray"):
                        mid = int(k.split("_")[1])
                        mk = f"map_{mid}_meta"
                        if mk in zf.files:
                            out[mid] = (np.array(zf[k], copy=True), np.array(zf[mk], copy=True))
            if 2 in out:
                return out
    raise FileNotFoundError("terrain_n2_v4.npz not found in src/data or user cache")


def _draw_cyan_arrow(img_bgr: np.ndarray, heading_cw_deg: float) -> None:
    h, w = img_bgr.shape[:2]
    cx, cy = w * 0.5, h * 0.5
    s = min(h, w) / 218.0
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
    poly = np.round((pts_local @ rot.T) + np.array([cx, cy])).astype(np.int32)
    cv2.fillPoly(img_bgr, [poly], (252, 235, 10), lineType=cv2.LINE_AA)


def _render_minimap_from_gray(
    base_gray: np.ndarray,
    *,
    size: int = 218,
    camera_heading_cw: float = 45.0,
    body_heading_cw: float = 45.0,
    with_bgra_alpha: bool = False,
    hdr_boost: bool = False,
) -> np.ndarray:
    if base_gray.shape != (size, size):
        ph, pw = base_gray.shape[:2]
        if ph >= size and pw >= size:
            ox = (pw - size) // 2
            oy = (ph - size) // 2
            base_gray = base_gray[oy : oy + size, ox : ox + size].copy()
        else:
            base_gray = cv2.resize(base_gray, (size, size), interpolation=cv2.INTER_LINEAR)

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
    cone_ang = wrap_angle_360(camera_heading_cw)
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
    if hdr_boost:
        bgr_f = np.clip(bgr_f * 1.25 + 22.0, 0, 255)
    bgr_f[cone_mask > 0] = bgr_f[cone_mask > 0] * 0.72 + 255.0 * 0.28
    bgr = np.clip(bgr_f, 0, 255).astype(np.uint8)

    circle_mask = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(circle_mask, center, outer_r, 255, -1)
    bgr[circle_mask == 0] = (18, 20, 24)
    cv2.circle(bgr, center, outer_r + 2, (45, 48, 52), 2, lineType=cv2.LINE_AA)
    cv2.circle(bgr, center, outer_r, (215, 224, 232), 2, lineType=cv2.LINE_AA)
    _draw_cyan_arrow(bgr, wrap_angle_360(body_heading_cw))

    if with_bgra_alpha:
        alpha = np.full((size, size), 255, dtype=np.uint8)
        alpha[cone_mask > 0] = 191
        return np.ascontiguousarray(np.dstack([bgr, alpha]))
    return np.ascontiguousarray(bgr)


def _render_surface_minimap(
    tile_cache: LruTerrainTileCache,
    map_id: int,
    world_x: float,
    world_y: float,
    *,
    span_world: float = 360.0,
    size: int = 218,
    camera_heading_cw: float = 45.0,
    body_heading_cw: float = 45.0,
    with_bgra_alpha: bool = False,
) -> np.ndarray:
    ppu = float(size) / float(span_world)
    patch, _ = tile_cache.extract_local_patch(map_id, world_x, world_y, 0.5 * span_world * 1.02, target_px_per_unit=ppu)
    assert patch is not None, f"Surface patch missing at ({world_x}, {world_y})"
    return _render_minimap_from_gray(
        patch,
        size=size,
        camera_heading_cw=camera_heading_cw,
        body_heading_cw=body_heading_cw,
        with_bgra_alpha=with_bgra_alpha,
    )


def _make_synthetic_badge(w: int = 68, h: int = 77) -> Tuple[np.ndarray, np.ndarray]:
    """Builds a high-contrast geometric synthetic HUD badge & mask (no game artwork)."""
    rng = np.random.default_rng(20261122)
    bgr = np.full((h, w, 3), 24, dtype=np.uint8)
    bgr += rng.integers(0, 12, size=(h, w, 3), dtype=np.uint8)
    cv2.circle(bgr, (34, 38), 28, (215, 225, 240), 2, lineType=cv2.LINE_AA)
    cv2.circle(bgr, (34, 38), 22, (70, 95, 165), -1, lineType=cv2.LINE_AA)
    pts = np.array([[34, 15], [47, 36], [34, 58], [21, 36]], dtype=np.int32)
    cv2.fillPoly(bgr, [pts], (242, 232, 195), lineType=cv2.LINE_AA)
    cv2.line(bgr, (20, 25), (48, 52), (40, 48, 88), 3, lineType=cv2.LINE_AA)
    cv2.circle(bgr, (34, 34), 6, (250, 210, 90), -1, lineType=cv2.LINE_AA)
    yy, xx = np.mgrid[0:h, 0:w]
    ell = (((xx - 33.5) / 30.0) ** 2 + ((yy - 38.0) / 35.0) ** 2) <= 1.0
    mask = np.where(ell, np.uint8(255), np.uint8(0))
    return bgr, mask


def _compose_hud_frame(
    minimap_bgr: np.ndarray,
    badge_bgr: Optional[np.ndarray],
    *,
    frame_w: int = 1920,
    frame_h: int = 1080,
    dx_px: int = 0,
    dy_px: int = 0,
) -> np.ndarray:
    lay = compute_ui_layout(frame_w, frame_h)
    px, py, pw, ph = lay.paimon_rect
    mx, my, mw, mh = lay.minimap_rect
    pmx, pmy, pmw, pmh = lay.paimon_maybe_rect
    pad_x = int(round(48.0 * lay.ui_scale))
    pad_y = int(round(28.0 * lay.ui_scale))
    hud_w = max(mx + mw + pad_x, pmx + pmw)
    hud_h = max(my + mh + pad_y, pmy + pmh)

    rng = np.random.default_rng(77)
    hud = rng.integers(14, 34, size=(hud_h, hud_w, 3), dtype=np.uint8)

    # Place minimap shifted by runtime HUD offset (dx_px, dy_px)
    mm_r = minimap_bgr[:, :, :3]
    if mm_r.shape[:2] != (mh, mw):
        mm_r = cv2.resize(mm_r, (mw, mh), interpolation=cv2.INTER_AREA)
    mx0 = max(0, min(hud_w - mw, mx + dx_px))
    my0 = max(0, min(hud_h - mh, my + dy_px))
    hud[my0 : my0 + mh, mx0 : mx0 + mw] = mm_r

    if badge_bgr is not None:
        br = badge_bgr if badge_bgr.shape[:2] == (ph, pw) else cv2.resize(badge_bgr, (pw, ph))
        px0 = max(0, min(hud_w - pw, px + dx_px))
        py0 = max(0, min(hud_h - ph, py + dy_px))
        hud[py0 : py0 + ph, px0 : px0 + pw] = br
    return hud


def _make_synthetic_ug_overlay_image(width: int = 512, height: int = 512) -> np.ndarray:
    """High-texture asymmetric synthetic cave floor map for SIFT auto-layer testing."""
    rng = np.random.default_rng(31415)
    img = np.full((height, width, 4), (36, 40, 48, 255), dtype=np.uint8)
    noise = rng.integers(-6, 7, size=(height, width, 3), dtype=np.int16)
    img[:, :, :3] = np.clip(img[:, :, :3].astype(np.int16) + noise, 0, 255).astype(np.uint8)
    for idx in range(28):
        x0 = int(rng.integers(40, width - 80))
        y0 = int(rng.integers(40, height - 80))
        r0 = int(rng.integers(14, 38))
        col = int(125 + (idx % 5) * 24)
        if idx % 3 == 0:
            cv2.rectangle(img, (x0, y0), (x0 + r0 + 18, y0 + r0), (col, col - 10, col - 20, 255), 2)
            cv2.circle(img, (x0 + r0 // 2, y0 + r0 // 2), max(4, r0 // 4), (210, 200, 175, 255), -1)
        elif idx % 3 == 1:
            tri = np.array([[x0, y0], [x0 + r0, y0 + 8], [x0 + 10, y0 + r0]], dtype=np.int32)
            cv2.fillPoly(img, [tri], (col, col, col - 15, 255))
        else:
            cv2.line(img, (x0, y0), (x0 + r0, y0 - r0 // 2), (220, 210, 190, 255), 3)
            cv2.circle(img, (x0 + r0 // 2, y0), 6, (185, 195, 225, 255), -1)
    return img


# ---------------------------------------------------------------------------
# (a) heading360 Vectorized Polar View Cone Speed & Accuracy
# ---------------------------------------------------------------------------
def test_a_heading360_speed_and_accuracy() -> None:
    print("[Test a] heading360 vectorized polar view-cone accuracy + latency (< 4.0 ms/f)...")
    yy, xx = np.mgrid[0:218, 0:218]
    base_gray = np.clip(95 + 24 * np.sin(xx * 0.14) * np.cos(yy * 0.11), 25, 165).astype(np.uint8)
    angles = [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]

    bgra_errs: List[float] = []
    bgr_errs: List[float] = []
    hdr_errs: List[float] = []
    latencies_ms: List[float] = []

    for ang in angles:
        mm_bgra = _render_minimap_from_gray(base_gray, camera_heading_cw=ang, with_bgra_alpha=True)
        got_bgra, conf_bgra, _ = extract_camera_view_cone(mm_bgra)
        assert got_bgra is not None and conf_bgra > 0.5
        err_bgra = abs(shortest_angle_delta(ang, got_bgra))
        bgra_errs.append(err_bgra)
        assert err_bgra <= 1.0, f"BGRA cone error too high at {ang}: {err_bgra:.2f}"

        mm_bgr = _render_minimap_from_gray(base_gray, camera_heading_cw=ang, with_bgra_alpha=False)
        got_bgr, conf_bgr, _ = extract_camera_view_cone(mm_bgr)
        assert got_bgr is not None and conf_bgr > 0.15
        err_bgr = abs(shortest_angle_delta(ang, got_bgr))
        bgr_errs.append(err_bgr)
        assert err_bgr <= 2.0, f"BGR cone error too high at {ang}: {err_bgr:.2f}"

        mm_hdr = _render_minimap_from_gray(base_gray, camera_heading_cw=ang, hdr_boost=True)
        got_hdr, conf_hdr, _ = extract_camera_view_cone(mm_hdr)
        assert got_hdr is not None and conf_hdr > 0.10
        err_hdr = abs(shortest_angle_delta(ang, got_hdr))
        hdr_errs.append(err_hdr)
        assert err_hdr <= 2.5, f"HDR cone error too high at {ang}: {err_hdr:.2f}"

    flat_neg = np.full((218, 218, 3), 24, dtype=np.uint8)
    neg_ang, _, _ = extract_camera_view_cone(flat_neg)
    assert neg_ang is None, f"Flat dark frame falsely returned heading={neg_ang}"

    # 20-frame timing check across BGRA + BGR
    sample_bgr = _render_minimap_from_gray(base_gray, camera_heading_cw=135.0, with_bgra_alpha=False)
    sample_bgra = _render_minimap_from_gray(base_gray, camera_heading_cw=135.0, with_bgra_alpha=True)
    extract_camera_view_cone(sample_bgr)  # warm caches
    for i in range(20):
        img = sample_bgra if (i % 2 == 0) else sample_bgr
        t0 = time.perf_counter()
        extract_camera_view_cone(img)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    med_ms = float(np.median(latencies_ms))
    assert med_ms < 4.0, f"View cone median latency too slow: {med_ms:.3f} ms"
    print(
        f"  PASS (BGRA max={max(bgra_errs):.2f} deg, BGR max={max(bgr_errs):.2f} deg, "
        f"HDR max={max(hdr_errs):.2f} deg, med={med_ms:.3f} ms/f)"
    )


# ---------------------------------------------------------------------------
# (b) hd_assets Non-Blocking Providers & Floor Metadata
# ---------------------------------------------------------------------------
def test_b_hd_assets_providers(terrain_data: Dict[int, Tuple[np.ndarray, np.ndarray]]) -> Dict[int, Dict[str, Any]]:
    print("[Test b] src/hd_assets.py load_multimap_db, offline/async HD tiles, UG overlays...")
    maps_db = load_multimap_db()
    assert 2 in maps_db, "Map 2 missing from load_multimap_db()"
    assert "detail_v2" in maps_db[2] and "point_groups" in maps_db[2], "Map 2 metadata incomplete"

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        hd_dir = tmp / "hd_tiles"
        ug_dir = tmp / "ug_overlays"

        # 1. Offline non-blocking miss + cached disk hit
        hd_off = HoyolabHdTileProvider(disk_dir=hd_dir, maps_db=maps_db, offline=True)
        t0 = time.perf_counter()
        miss = hd_off(2, -1, 12, 12)
        miss_ms = (time.perf_counter() - t0) * 1000.0
        assert miss is None and miss_ms < 5.0, f"Offline miss blocked or unexpected hit: {miss_ms:.2f}ms"

        png_path = hd_dir / "2" / "-1" / "12_12.png"
        syn_tile = np.full((256, 256), 142, dtype=np.uint8)
        assert write_image(png_path, syn_tile)
        hit = hd_off(2, -1, 12, 12)
        assert hit is not None and hit.shape == (256, 256) and hd_off.disk_hits >= 1
        hd_off.close()

        # 2. Async provider returns None immediately on uncached miss and closes cleanly
        hd_async = HoyolabHdTileProvider(disk_dir=tmp / "hd_async", maps_db=maps_db, async_fetch=True, timeout=0.2)
        t0 = time.perf_counter()
        async_miss = hd_async(2, -1, 13, 13)
        async_ms = (time.perf_counter() - t0) * 1000.0
        assert async_miss is None and async_ms < 25.0, f"Async miss blocked caller: {async_ms:.2f}ms"
        hd_async.close()

        # 3. UndergroundOverlayProvider overlap query + disk overlay + patch extraction
        ug_off = UndergroundOverlayProvider(disk_dir=ug_dir, maps_db=maps_db, offline=True)
        fls = ug_off._floors_by_map.get(2, [])
        assert len(fls) > 0, "Expected Map 2 underground floors in multimap_v4.json"
        rec = fls[0]
        cx = 0.5 * (rec["lx"] + rec["rx"])
        cy = 0.5 * (rec["ly"] + rec["ry"])
        hits = ug_off.find_overlapping_floors(2, cx, cy)
        assert any(int(h["floor_id"]) == int(rec["floor_id"]) for h in hits)

        syn_ug = _make_synthetic_ug_overlay_image(256, 256)
        assert write_image(ug_dir / f"2_g{rec['group_id']}_f{rec['floor_id']}.png", syn_ug)
        patch, _ = ug_off.extract_patch(
            2, rec["floor_id"], cx, cy, 120.0, target_px_per_unit=0.5, group_id=rec["group_id"]
        )
        assert patch is not None and patch.shape[:2] == (120, 120)
        ug_off.close()

    tc = LruTerrainTileCache(max_tiles=16, terrain_data=terrain_data)
    assert tc.extract_local_patch(2, -500.0, -1266.0, 180.0, target_px_per_unit=0.5)[0] is not None
    print(f"  PASS (maps_db={len(maps_db)}, map2_floors={len(fls)}, miss={miss_ms:.2f}ms)")
    return maps_db


# ---------------------------------------------------------------------------
# (c) global_relocator Async Warmup, Blind Retrieval & Negative Rejection
# ---------------------------------------------------------------------------
def test_c_global_relocator(
    terrain_data: Dict[int, Tuple[np.ndarray, np.ndarray]],
    idx_cache_path: Path,
) -> TeyvatGlobalRelocator:
    print("[Test c] src/global_relocator.py async warmup, blind Teyvat retrieval & negative guard...")
    reloc = TeyvatGlobalRelocator(terrain_data=terrain_data, cache_path=idx_cache_path, lazy=True)
    assert not reloc.is_ready, "lazy=True must not block-build at __init__"

    reloc.warmup_async()
    deadline = time.monotonic() + 25.0
    while not reloc.is_ready and time.monotonic() < deadline:
        time.sleep(0.05)
    assert reloc.is_ready, "warmup_async() timed out waiting for FLANN index"
    assert idx_cache_path.is_file(), "Global index cache file not created"
    with np.load(idx_cache_path) as zf:
        assert int(zf["index_version"]) == INDEX_VERSION

    tc = LruTerrainTileCache(max_tiles=16, terrain_data=terrain_data)
    wx, wy = -500.0, -1266.0
    mm = _render_surface_minimap(tc, 2, wx, wy, span_world=360.0)
    vbins: Dict[Tuple[int, int], List[Tuple[float, float, float]]] = {}
    cands = reloc.query_candidates(mm, vote_bins=vbins, top_k=4, min_bin_score=1.5)
    assert len(cands) > 0, "Global relocator returned 0 candidates for Mondstadt minimap"
    best_dist = min(math.hypot(cx - wx, cy - wy) for cx, cy, _ in cands)
    assert best_dist <= 250.0, f"Top candidates missed Mondstadt anchor: best_dist={best_dist:.1f}w"

    # Negative guard: blank, flat white, and empty frames produce 0 hits
    for neg in (
        np.zeros((218, 218, 3), dtype=np.uint8),
        np.full((218, 218, 3), 248, dtype=np.uint8),
        np.full((218, 218, 3), 90, dtype=np.uint8),
    ):
        assert reloc.query_candidates(neg) == [], "Negative frame produced global FLANN hit"

    # Warm reload from temp index must complete < 2.0s
    t0 = time.perf_counter()
    reloc_warm = TeyvatGlobalRelocator(terrain_data=terrain_data, cache_path=idx_cache_path, lazy=False)
    warm_s = time.perf_counter() - t0
    assert reloc_warm.is_ready and warm_s < 2.0, f"Warm reload too slow: {warm_s:.2f}s"

    print(
        f"  PASS (landmarks={reloc.num_descriptors}, mondstadt_err={best_dist:.1f}w, "
        f"warm_reload={warm_s:.3f}s)"
    )
    return reloc_warm


# ---------------------------------------------------------------------------
# (d) HybridMinimapTracker V5.3 Production Contracts
# ---------------------------------------------------------------------------
def _tracker_cfg(**overrides: Any) -> MinimapTrackerConfig:
    params: Dict[str, Any] = dict(
        gate_border_coherence=0.22,
        template_min_corr=0.34,
        min_confidence=0.34,
        relocate_hits=2,
        candidate_radius_world=40.0,
        lost_timeout_seconds=12.0,
        max_phase_fallback_frames=12,
        max_speed_world_per_second=135.0,
        jump_margin_world=30.0,
        smoothing_alpha=0.55,
        hold_on_phase_exhausted=True,
    )
    params.update(overrides)
    return MinimapTrackerConfig(**params)


def test_d_tracker_v53_contracts(
    terrain_data: Dict[int, Tuple[np.ndarray, np.ndarray]],
    maps_db: Dict[int, Dict[str, Any]],
    relocator: TeyvatGlobalRelocator,
) -> None:
    print("[Test d] HybridMinimapTracker V5.3 (HUD cal, Paimon bootstrap/gate, auto-layer, Tier-3 global)...")
    tc = LruTerrainTileCache(max_tiles=72, terrain_data=terrain_data)
    badge_bgr, badge_mask = _make_synthetic_badge()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        paimon_cache = tmp / "paimon_tpl.npz"

        # 1. IP-clean Paimon auto-bootstrap + pause gate via step_from_hud
        tracker = HybridMinimapTracker(
            tile_cache=tc,
            global_relocator=relocator,
            paimon_cache_path=paimon_cache,
            config=_tracker_cfg(),
        )
        t = 10.0
        wx, wy = -500.0, -1266.0
        tracker.set_map_m_anchor(2, wx, wy, timestamp=t)
        assert not paimon_cache.is_file()

        for i in range(1, 5):
            t += 0.2
            mm = _render_surface_minimap(tc, 2, wx + i * 0.9, wy + i * 0.6, with_bgra_alpha=True)
            hud = _compose_hud_frame(mm, badge_bgr)
            pose = tracker.step_from_hud(hud, 1920, 1080, now=t)
            assert pose.state == STATE_TRACKING

        assert paimon_cache.is_file() and tracker._has_paimon_tpl, "Paimon auto-bootstrap did not cache template"
        with np.load(paimon_cache) as zf:
            assert "tpl_gray" in zf.files and "mask" in zf.files

        # Pause when Paimon icon is hidden -> must freeze cleanly & suppress broadcast
        t += 0.2
        mm_now = _render_surface_minimap(tc, 2, wx + 4.0, wy + 2.5, with_bgra_alpha=True)
        hud_no_paimon = _compose_hud_frame(mm_now, None)
        p_pause = tracker.step_from_hud(hud_no_paimon, 1920, 1080, now=t)
        assert p_pause.match_method == "paimon_pause", f"Expected paimon_pause, got {p_pause.match_method}"
        assert p_pause.should_broadcast_web is False

        # Stationary skip vs active broadcast vs loading pause gate
        tracker2 = HybridMinimapTracker(
            tile_cache=tc,
            paimon_tpl=badge_bgr,
            paimon_mask=badge_mask,
            config=_tracker_cfg(),
        )
        t2 = 50.0
        tracker2.set_map_m_anchor(2, wx, wy, timestamp=t2)
        hud_last = None
        for k in range(1, 5):
            t2 += 0.2
            mm_shift = _render_surface_minimap(tc, 2, wx + k * 0.9, wy + k * 0.6, with_bgra_alpha=True)
            hud_last = _compose_hud_frame(mm_shift, badge_bgr, dx_px=-12, dy_px=6)
            pose_cal = tracker2.step_from_hud(hud_last, 1920, 1080, now=t2)
        cal_dx, cal_dy = tracker2.calibrated_offset_px
        assert abs(cal_dx - (-12)) <= 2 and abs(cal_dy - 6) <= 2, f"Bad HUD cal offset: {(cal_dx, cal_dy)}"
        assert pose_cal.state == STATE_TRACKING and pose_cal.should_broadcast_web is True, (
            f"Unexpected cal pose: {pose_cal.match_method}, bc={pose_cal.should_broadcast_web}"
        )
        err_cal = math.hypot(float(pose_cal.world_x) - (wx + 3.6), float(pose_cal.world_y) - (wy + 2.4))
        assert err_cal < 6.0, f"Calibrated tracking error too large: {err_cal:.2f}w"

        # Identical next frame triggers stationary_skip with should_broadcast_web == False
        t2 += 0.2
        pose_stat = tracker2.step_from_hud(hud_last, 1920, 1080, now=t2)
        assert pose_stat.match_method == "stationary_skip" and pose_stat.should_broadcast_web is False

        # Loading screen transitions cleanly without corrupting broadcast
        t2 += 0.2
        pose_load = tracker2.on_loading_screen(t2)
        assert pose_load.match_method == "loading_pause" and pose_load.should_broadcast_web is False

        # 2. Bidirectional leaky-bucket Auto-Layer-Switch (Surf -> UG -> Surf)
        ug_dir = tmp / "ug_auto"
        ug_prov = UndergroundOverlayProvider(disk_dir=ug_dir, maps_db=maps_db, offline=True)
        chosen_rec = None
        for rec in ug_prov._floors_by_map.get(2, []):
            if (rec["rx"] - rec["lx"]) >= 360.0 and (rec["ry"] - rec["ly"]) >= 360.0:
                cx_r = 0.5 * (rec["lx"] + rec["rx"])
                cy_r = 0.5 * (rec["ly"] + rec["ry"])
                sp_r, _ = tc.extract_local_patch(2, cx_r, cy_r, 180.0, target_px_per_unit=0.5)
                if sp_r is not None and float(np.std(sp_r)) >= 20.0:
                    chosen_rec = rec
                    break
        assert chosen_rec is not None, "No suitable Map 2 floor with textured surface found"
        ug_img = _make_synthetic_ug_overlay_image(512, 512)
        assert write_image(ug_dir / f"2_g{chosen_rec['group_id']}_f{chosen_rec['floor_id']}.png", ug_img)

        fl_cx = 0.5 * (chosen_rec["lx"] + chosen_rec["rx"])
        fl_cy = 0.5 * (chosen_rec["ly"] + chosen_rec["ry"])

        tracker_layer = HybridMinimapTracker(
            tile_cache=tc,
            ug_provider=ug_prov,
            config=_tracker_cfg(),
        )
        t3 = 100.0
        tracker_layer.set_map_m_anchor(2, fl_cx, fl_cy, is_underground=False, floor_id=None, floor_name="G", timestamp=t3)
        switched_to_ug = False
        cur_wx, cur_wy = fl_cx, fl_cy
        for k in range(1, 9):
            t3 += 0.2
            cur_wx = fl_cx + k * 2.2
            cur_wy = fl_cy + k * 1.6
            ug_patch_raw, _ = ug_prov.extract_patch(
                2,
                chosen_rec["floor_id"],
                cur_wx,
                cur_wy,
                180.0 * 1.02,
                target_px_per_unit=218.0 / 360.0,
                group_id=chosen_rec["group_id"],
            )
            assert ug_patch_raw is not None
            ug_gray = (
                ug_patch_raw
                if ug_patch_raw.ndim == 2
                else cv2.cvtColor(ug_patch_raw[:, :, :3], cv2.COLOR_BGR2GRAY)
            )
            mm_ug = _render_minimap_from_gray(ug_gray, camera_heading_cw=60.0 + k * 5.0, with_bgra_alpha=True)
            p_l = tracker_layer.step(mm_ug, now=t3)
            if p_l.match_method == "layer_switch_sift" and p_l.is_underground:
                switched_to_ug = True
                assert int(p_l.floor_id or -1) == int(chosen_rec["floor_id"])
                break
        assert switched_to_ug, "Auto-layer switch Surf -> UG did not trigger"

        switched_to_surf = False
        for k in range(1, 9):
            t3 += 0.2
            mm_surf = _render_surface_minimap(
                tc,
                2,
                cur_wx + k * 2.2,
                cur_wy + k * 1.6,
                camera_heading_cw=120.0 + k * 5.0,
                with_bgra_alpha=True,
            )
            p_s = tracker_layer.step(mm_surf, now=t3)
            if p_s.match_method == "layer_switch_sift" and not p_s.is_underground:
                switched_to_surf = True
                break
        assert switched_to_surf, "Auto-layer switch UG -> Surf did not trigger"
        ug_prov.close()

        # 3. Cold-start global_teyvat_relocate (fsm._x is None)
        tracker_cold = HybridMinimapTracker(
            tile_cache=tc,
            global_relocator=relocator,
            config=_tracker_cfg(),
        )
        assert tracker_cold.state == STATE_LOST
        mm_cold = _render_surface_minimap(tc, 2, wx, wy, with_bgra_alpha=True)
        pose_cold = tracker_cold.step(mm_cold, now=1.0)
        assert pose_cold.match_method == "global_teyvat_relocate", f"Cold start failed: {pose_cold.match_method}"
        assert pose_cold.state == STATE_TRACKING and pose_cold.should_broadcast_web is True
        cold_err = math.hypot(float(pose_cold.world_x) - wx, float(pose_cold.world_y) - wy)
        assert cold_err < 20.0, f"Cold-start relocate error too high: {cold_err:.2f}w"

        # 4. Post-loading Tier-3 global_teyvat_relocate across Teyvat + Domain negative rejection
        tracker_tp = HybridMinimapTracker(
            tile_cache=tc,
            global_relocator=relocator,
            config=_tracker_cfg(),
        )
        t4 = 200.0
        # Anchor far south (outside +-380w Tier-2 window from wx=-500, wy=-1266)
        tracker_tp.set_map_m_anchor(2, -1800.0, 450.0, timestamp=t4)
        t4 += 0.2
        tracker_tp.on_loading_screen(t4)

        # Feed non-Teyvat checkerboard domain minimap frames first -> must hold safely, never jump
        cb = np.zeros((218, 218), dtype=np.uint8)
        for by in range(0, 218, 18):
            for bx in range(0, 218, 18):
                if ((bx // 18) + (by // 18)) % 2 == 0:
                    cb[by : by + 18, bx : bx + 18] = 195
        mm_domain = _render_minimap_from_gray(cb, camera_heading_cw=45.0, with_bgra_alpha=True)
        for _ in range(4):
            t4 += 0.2
            p_dom = tracker_tp.step(mm_domain, now=t4)
            assert p_dom.match_method in ("relocating_hold", "gate_rejected"), (
                f"Domain checkerboard escaped hold: {p_dom.match_method}"
            )
            assert p_dom.should_broadcast_web is False
            assert math.hypot(float(p_dom.world_x) - (-1800.0), float(p_dom.world_y) - 450.0) < 1.0

        # Now feed real Mondstadt minimap after teleport -> recovers cleanly
        t4 += 0.2
        mm_tp = _render_surface_minimap(tc, 2, wx, wy, with_bgra_alpha=True)
        p_tp = tracker_tp.step(mm_tp, now=t4)
        assert p_tp.match_method == "global_teyvat_relocate", f"Tier-3 teleport failed: {p_tp.match_method}"
        assert p_tp.should_broadcast_web is True
        tp_err = math.hypot(float(p_tp.world_x) - wx, float(p_tp.world_y) - wy)
        assert tp_err < 20.0, f"Tier-3 teleport error too high: {tp_err:.2f}w"

    print(
        f"  PASS (cal_err={err_cal:.2f}w, cold_err={cold_err:.2f}w, "
        f"tier3_err={tp_err:.2f}w, auto_layer=Surf<->UG OK)"
    )


# ---------------------------------------------------------------------------
# (e) Optional Real-Footage Parity Smoke Check (dev_snezhnaya & holdout_natlan)
# ---------------------------------------------------------------------------
def test_e_optional_real_footage_parity(
    terrain_data: Dict[int, Tuple[np.ndarray, np.ndarray]],
    maps_db: Dict[int, Dict[str, Any]],
    relocator: TeyvatGlobalRelocator,
) -> None:
    bench_dir = ROOT / "scratch" / "yt_bench"
    ds_dir = bench_dir / "datasets"
    hd_dir = bench_dir / "hd_tiles"
    ug_dir = bench_dir / "ug_overlays"
    paimon_npz = bench_dir / "paimon_tpl.npz"
    slices = ["dev_snezhnaya", "holdout_natlan"]

    if not hd_dir.is_dir() or not all((ds_dir / s / "timeline.jsonl").is_file() for s in slices):
        print("[Test e] Optional real-footage parity smoke check... SKIP (local footage not present)")
        return

    print("[Test e] Optional real-footage parity smoke check (dev_snezhnaya & holdout_natlan)...")
    hd_prov = HoyolabHdTileProvider(disk_dir=hd_dir, maps_db=maps_db, offline=True)
    ug_prov = UndergroundOverlayProvider(disk_dir=ug_dir, maps_db=maps_db, offline=True) if ug_dir.is_dir() else None
    p_tpl_arr = None
    p_mask_arr = None
    if paimon_npz.is_file():
        with np.load(paimon_npz) as zf:
            p_tpl_arr = np.array(zf["tpl_gray"], copy=True)
            p_mask_arr = np.array(zf["mask"], copy=True)

    try:
        for slice_name in slices:
            sdir = ds_dir / slice_name
            meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (sdir / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            gt_rows = [r for r in rows if ("sift_gt" in r or "mapm_gt" in r)]
            gt_by_seq = {r["seq"]: (r.get("sift_gt") or r.get("mapm_gt")) for r in gt_rows}

            anchor_seqs = {gt_rows[0]["seq"]}
            prev_seq = gt_rows[0]["seq"]
            prev_gt = gt_by_seq[prev_seq]
            for r in gt_rows[1:]:
                s = r["seq"]
                g = gt_by_seq[s]
                had_load = any(rows[k]["is_loading"] for k in range(prev_seq + 1, s))
                dist = math.hypot(g["world_x"] - prev_gt["world_x"], g["world_y"] - prev_gt["world_y"])
                layer_chg = (
                    bool(g.get("is_underground", False)) != bool(prev_gt.get("is_underground", False))
                    or g.get("group_id") != prev_gt.get("group_id")
                    or g.get("floor_id") != prev_gt.get("floor_id")
                )
                if had_load or dist > 260.0 or layer_chg:
                    anchor_seqs.add(s)
                prev_seq = s
                prev_gt = g

            tc = LruTerrainTileCache(max_tiles=72, terrain_data=terrain_data, tile_provider=hd_prov)
            tracker = HybridMinimapTracker(
                tile_cache=tc,
                ug_provider=ug_prov,
                global_relocator=relocator,
                paimon_tpl=p_tpl_arr,
                paimon_mask=p_mask_arr,
                config=_tracker_cfg(),
            )

            fw, fh = int(meta["frame_size"][0]), int(meta["frame_size"][1])
            preloaded = {
                r["seq"]: cv2.imread(str(sdir / r["hud_tl_path"]), cv2.IMREAD_COLOR)
                for r in rows[gt_rows[0]["seq"] :]
                if "hud_tl_path" in r
            }

            gameplay_total = 0
            gameplay_tracking = 0
            corrupt = 0
            gt_errors: List[float] = []
            lat_ms: List[float] = []
            anchored = False
            anchor_t = 0.0
            last_bc: Optional[Tuple[int, float, float, float]] = None

            for r in rows[gt_rows[0]["seq"] :]:
                seq = r["seq"]
                ts = float(r["t"]) + 1.0
                if seq in anchor_seqs:
                    gt = gt_by_seq[seq]
                    tracker.set_map_m_anchor(
                        map_id=int(meta["map_id"]),
                        world_x=float(gt["world_x"]),
                        world_y=float(gt["world_y"]),
                        is_underground=bool(gt.get("is_underground", False)),
                        floor_id=gt.get("floor_id"),
                        floor_name=str(gt.get("floor_name") or ("B1" if gt.get("is_underground") else "G")),
                        timestamp=ts,
                    )
                    anchored = True
                    anchor_t = ts
                    last_bc = (seq, ts, float(gt["world_x"]), float(gt["world_y"]))
                    continue
                if not anchored:
                    continue
                if r["is_map_m"]:
                    tracker.pause(now=ts, reason="map_m_open")
                    if "mapm_gt" in r:
                        mgt = r["mapm_gt"]
                        tracker.set_map_m_anchor(
                            map_id=int(meta["map_id"]),
                            world_x=float(mgt["world_x"]),
                            world_y=float(mgt["world_y"]),
                            is_underground=bool(mgt.get("is_underground", False)),
                            floor_id=mgt.get("floor_id"),
                            floor_name=str(mgt.get("floor_name") or "G"),
                            timestamp=ts,
                        )
                        last_bc = (seq, ts, float(mgt["world_x"]), float(mgt["world_y"]))
                    continue
                if r["is_loading"]:
                    tracker.on_loading_screen(ts)
                    continue

                t0 = time.perf_counter()
                pose = tracker.step_from_hud(preloaded.get(seq), fw, fh, now=ts)
                dt_ms = (time.perf_counter() - t0) * 1000.0

                if seq in gt_by_seq and (ts - anchor_t) >= 4.0:
                    gt = gt_by_seq[seq]
                    ewx, ewy = pose.world_x, pose.world_y
                    if (ewx is None or ewy is None or pose.state != STATE_TRACKING) and last_bc is not None:
                        ewx, ewy = last_bc[2], last_bc[3]
                    if ewx is not None and ewy is not None:
                        err = math.hypot(float(ewx) - float(gt["world_x"]), float(ewy) - float(gt["world_y"]))
                        gt_errors.append(err)
                        if pose.should_broadcast_web and err > 35.0:
                            corrupt += 1

                if r["is_gameplay"]:
                    gameplay_total += 1
                    lat_ms.append(dt_ms)
                    if pose.state == STATE_TRACKING:
                        gameplay_tracking += 1
                    if pose.should_broadcast_web and pose.world_x is not None and pose.world_y is not None:
                        if last_bc is not None:
                            dt_prev = max(0.05, ts - last_bc[1])
                            step_d = math.hypot(float(pose.world_x) - last_bc[2], float(pose.world_y) - last_bc[3])
                            inter_load = any(rows[k]["is_loading"] or (k in anchor_seqs) for k in range(last_bc[0] + 1, seq))
                            if not inter_load and step_d > max(50.0, 75.0 * dt_prev):
                                corrupt += 1
                        last_bc = (seq, ts, float(pose.world_x), float(pose.world_y))

            uptime = (100.0 * gameplay_tracking / gameplay_total) if gameplay_total > 0 else 0.0
            gt_med = float(np.median(gt_errors)) if gt_errors else 0.0
            lat_med = float(np.median(lat_ms)) if lat_ms else 0.0
            print(
                f"    {slice_name}: Uptime={uptime:.1f}% Corrupt={corrupt} "
                f"GT_Med={gt_med:.2f}w Lat_Med={lat_med:.2f}ms"
            )
            assert uptime == 100.0, f"{slice_name} uptime dropped below 100%: {uptime:.1f}%"
            assert corrupt == 0, f"{slice_name} had {corrupt} web corruptions"
            assert gt_med < 2.0, f"{slice_name} GT median error >= 2.0w: {gt_med:.2f}w"
            assert lat_med < 8.5, f"{slice_name} median latency >= 8.5ms: {lat_med:.2f}ms"
    finally:
        hd_prov.close()
        if ug_prov is not None:
            ug_prov.close()
    print("  PASS (production parity verified on dev_snezhnaya + holdout_natlan)")


def main() -> int:
    t0 = time.perf_counter()
    terrain_data = _load_terrain_n2()
    test_a_heading360_speed_and_accuracy()
    maps_db = test_b_hd_assets_providers(terrain_data)
    with tempfile.TemporaryDirectory() as reloc_tmp:
        idx_path = Path(reloc_tmp) / "teyvat_global_index_v2.npz"
        relocator = test_c_global_relocator(terrain_data, idx_path)
        test_d_tracker_v53_contracts(terrain_data, maps_db, relocator)
        test_e_optional_real_footage_parity(terrain_data, maps_db, relocator)
    print(f"\nALL V5.3 PRODUCTION VERIFICATION TESTS PASSED in {time.perf_counter() - t0:.2f}s!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
