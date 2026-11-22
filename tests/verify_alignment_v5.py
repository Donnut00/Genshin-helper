"""
Genshin Helper v5 Track 1A Alignment Verification Suite.
Validates:
  1. Screen-space icon-anchor compensation (scaled by frame_h / 1440.0) & resolution invariance.
  2. Underground Sobel guard & tightened residual Sobel thresholds (max_val >= 0.30, margin >= 0.06, |dw| <= 8.0).
  3. Quantitative residual accuracy on diag_20261121_011215, diag_20261121_011322,
     diag_20261120_231651, and diag_20260920_214102.
  4. Zoom-invariance across 0%..100% zoom (scratch/0zoom.png vs scratch/100zoom.png) both with
     and without terrain_n2 refinement.
  5. Player arrow rotation-pivot refinement (< 0.75 px @1440p from true center) and nose heading
     consistency, plus backwards-compatible locate_player schema.
  6. Pre-Sobel residual accuracy across all high-confidence surface fixtures.
"""
import glob
import math
import os
import sys

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.locator import MapLocator


REQUIRED_LOCATE_KEYS = {
    "detected",
    "has_world_coords",
    "map_id",
    "map_name",
    "area_id",
    "area_name",
    "world_x",
    "world_y",
    "lat",
    "lng",
    "pixel_x",
    "pixel_y",
    "heading",
    "scale",
    "inliers_count",
    "rmse",
    "is_underground",
    "in_game_is_underground",
    "floor_idx",
    "total_floors",
    "active_floor_idx",
    "surface_idx",
    "floor_name",
    "floor_short",
    "hoyolab_floor_name",
    "group_id",
    "group_name",
    "floor_id",
    "has_layers",
    "zoom_ratio_t",
}


def _read_img(rel_path: str) -> np.ndarray:
    fp = os.path.join(PROJECT_ROOT, rel_path)
    img = cv2.imread(fp)
    assert img is not None, f"Failed to read fixture: {fp}"
    return img


def _locate_cold(loc: MapLocator, img: np.ndarray, debug: bool = True) -> dict:
    loc.last_known_pos = None
    loc.last_map_id = None
    res = loc.locate_player(img, debug=debug)
    assert res and res.get("has_world_coords"), f"Localization failed: {res}"
    missing = REQUIRED_LOCATE_KEYS - set(res.keys())
    assert not missing, f"locate_player schema regression, missing keys: {sorted(missing)}"
    return res


def verify_screen_anchor_scaling(loc: MapLocator) -> None:
    print("[1/6] Verifying resolution-scaled screen-space icon anchor compensation...")
    wp_tp = {"id": 1, "label_id": 3, "x_pos": 100.0, "y_pos": 200.0}
    wp_st = {"id": 2, "label_id": 2, "x_pos": 100.0, "y_pos": 200.0}
    wp_dm = {"id": 3, "label_id": 154, "x_pos": 100.0, "y_pos": 200.0}

    for h, su in ((1080, 0.75), (1440, 1.0), (2160, 1.5)):
        ox_tp, oy_tp = MapLocator._icon_anchor_offset_px(wp_tp, h / 1440.0)
        ox_st, oy_st = MapLocator._icon_anchor_offset_px(wp_st, h / 1440.0)
        ox_dm, oy_dm = MapLocator._icon_anchor_offset_px(wp_dm, h / 1440.0)
        assert abs(ox_tp) < 1e-9 and abs(oy_tp - 5.0 * su) < 1e-6
        assert abs(ox_st) < 1e-9 and abs(oy_st - 3.5 * su) < 1e-6
        assert abs(ox_dm - 2.5 * su) < 1e-6 and abs(oy_dm) < 1e-9

    # Cross-resolution invariance: native 1440p vs downscaled 1080p on surface + underground
    for rel in (
        "scratch/diagnostics/diag_20260920_214102/01_game.png",
        "scratch/diagnostics/diag_20261121_011322/01_game.png",
    ):
        img_1440 = _read_img(rel)
        img_1080 = cv2.resize(img_1440, (1920, 1080), interpolation=cv2.INTER_AREA)
        r1440 = _locate_cold(loc, img_1440)
        r1080 = _locate_cold(loc, img_1080)
        assert r1440["map_id"] == r1080["map_id"], f"Map mismatch 1440p vs 1080p on {rel}"
        d_res = math.hypot(r1440["world_x"] - r1080["world_x"], r1440["world_y"] - r1080["world_y"])
        assert d_res <= 3.0, f"1440p vs 1080p world drift too large on {rel}: {d_res:.2f}w"
        print(f"  [OK] {os.path.basename(os.path.dirname(rel))}: 1440p vs 1080p drift = {d_res:.2f}w")


def verify_underground_sobel_guard(loc: MapLocator) -> None:
    print("[2/6] Verifying underground Sobel guard & tightened residual thresholds...")
    # 1. Surface high-NCC frame (diag_20260920_214102, Map 36): Sobel succeeds when is_underground=False,
    #    MUST short-circuit to False when is_underground=True, and MUST reject shifts > 8.0 world units.
    img_m36 = _read_img("scratch/diagnostics/diag_20260920_214102/01_game.png")
    gray_m36 = cv2.cvtColor(img_m36, cv2.COLOR_BGR2GRAY)
    r_m36 = _locate_cold(loc, img_m36)
    wps36 = {w["id"]: w for w in loc.maps[36]["waypoints"]}
    inl36 = [((i["pixel_x"], i["pixel_y"], 100), wps36[i["waypoint_id"]]) for i in r_m36["inliers"]]
    adj36 = loc._compensate_inliers_screen_anchors(inl36, img_m36.shape[0] / 1440.0)
    sc36, tx36, ty36, _ = loc._fit_isometric_lsq(adj36)
    ty36 += loc.WORLD_ANCHOR_Y_OFFSET

    tx_ok, ty_ok, ok_surf = loc._refine_transform_with_terrain(
        gray_m36, 36, sc36, tx36, ty36, is_underground=False
    )
    assert ok_surf, "Expected surface Sobel refinement to succeed on diag_20260920_214102"
    assert abs(tx_ok - tx36) <= 2.0 and abs(ty_ok - ty36) <= 2.0, (
        f"Pre-Sobel residual unexpectedly large on Map 36: ({tx_ok - tx36:+.2f}, {ty_ok - ty36:+.2f})"
    )

    tx_ug, ty_ug, ok_ug = loc._refine_transform_with_terrain(
        gray_m36, 36, sc36, tx36, ty36, is_underground=True
    )
    assert not ok_ug and tx_ug == tx36 and ty_ug == ty36, (
        "Underground guard failed: _refine_transform_with_terrain modified transform with is_underground=True"
    )

    # Injecting +12.0 world shift exceeds tightened +/-8.0 residual clamp -> must reject
    tx_far, ty_far, ok_far = loc._refine_transform_with_terrain(
        gray_m36, 36, sc36, tx36, ty36 + 12.0, is_underground=False
    )
    assert not ok_far and tx_far == tx36 and ty_far == ty36 + 12.0, (
        "Tightened residual clamp failed: accepted a >8.0w shift"
    )

    # 2. Low-NCC / foggy frame rejection & true-position recovery on diag_20261120_231651:
    #    diag_20261120_231651 (Ga Byaroza, Snezhnaya; user note "[Sai vị trí]") previously had
    #    low NCC (0.137) only because 3 false waypoints mislocated it to (-7522.6, -5781.1).
    #    The Head-to-Head Terrain Guard now recovers the true Ga Byaroza coords (~(-5541.9, -7014.9)),
    #    while genuinely foggy/blurred or mismatched inputs must still be rejected (max_val < 0.30).
    img_fog = _read_img("scratch/diagnostics/diag_20261120_231651/01_game.png")
    gray_fog = cv2.cvtColor(img_fog, cv2.COLOR_BGR2GRAY)
    r_fog = _locate_cold(loc, img_fog)
    assert abs(r_fog["world_x"] - (-5541.9)) <= 15.0 and abs(r_fog["world_y"] - (-7014.9)) <= 15.0, (
        f"diag_20261120_231651 Ga Byaroza recovery misaligned: ({r_fog['world_x']:.2f}, {r_fog['world_y']:.2f})"
    )
    tx_f = r_fog["world_x"] - r_fog["scale"] * r_fog["pixel_x"]
    ty_f = r_fog["world_y"] - r_fog["scale"] * r_fog["pixel_y"]
    gray_heavy_fog = cv2.GaussianBlur(gray_fog, (51, 51), 15.0)
    _, _, ok_heavy_fog = loc._refine_transform_with_terrain(
        gray_heavy_fog, 2, r_fog["scale"], tx_f, ty_f, is_underground=False
    )
    assert not ok_heavy_fog, "Heavily blurred foggy frame must be rejected by tightened Sobel (< 0.30)"
    tx_wrong = -7522.6 - r_fog["scale"] * r_fog["pixel_x"]
    ty_wrong = -5781.1 - r_fog["scale"] * r_fog["pixel_y"]
    _, _, ok_wrong = loc._refine_transform_with_terrain(
        gray_fog, 2, r_fog["scale"], tx_wrong, ty_wrong, is_underground=False
    )
    assert not ok_wrong, "Mismatched terrain coordinates must be rejected by tightened Sobel (< 0.30)"
    print(
        f"  [OK] Surface Map 36 pre-Sobel residual=({tx_ok - tx36:+.2f}, {ty_ok - ty36:+.2f})w, "
        f"underground guard=blocked, >8w shift=rejected, low-NCC fog/mismatch=rejected, 231651=recovered"
    )


def verify_key_diagnostic_accuracy(loc: MapLocator) -> None:
    print("[3/6] Verifying quantitative accuracy on 4 key diagnostic bundles...")
    # A. diag_20261121_011322 (Map 2, Area 8, UG B1, WP 50068 at (-4942.0, -1478.5))
    img_1322 = _read_img("scratch/diagnostics/diag_20261121_011322/01_game.png")
    r_1322 = _locate_cold(loc, img_1322)
    assert r_1322["map_id"] == 2 and r_1322["area_id"] == 8 and r_1322["is_underground"] is True
    assert r_1322["group_id"] == 90 and r_1322["floor_id"] == 78
    assert abs(r_1322["world_x"] - (-4942.0)) <= 4.0 and abs(r_1322["world_y"] - (-1478.5)) <= 2.5, (
        f"diag_20261121_011322 misaligned: ({r_1322['world_x']:.2f}, {r_1322['world_y']:.2f})"
    )

    # B. diag_20261121_011215 (Map 2, Area 4, UG B1, WP 28750 at (-2819.0, 2208.5))
    img_1215 = _read_img("scratch/diagnostics/diag_20261121_011215/01_game.png")
    r_1215 = _locate_cold(loc, img_1215)
    assert r_1215["map_id"] == 2 and r_1215["area_id"] == 4 and r_1215["is_underground"] is True
    assert r_1215["group_id"] == 26 and r_1215["floor_id"] == 46
    assert abs(r_1215["world_y"] - 2208.5) <= 2.5, (
        f"diag_20261121_011215 world_y misaligned: {r_1215['world_y']:.2f} vs 2208.50"
    )

    # Confirm both underground diagnostics are 100% invariant to removing surface terrain_data
    saved_terrain = loc.terrain_data
    try:
        loc.terrain_data = {}
        r_1322_noterr = _locate_cold(loc, img_1322)
        r_1215_noterr = _locate_cold(loc, img_1215)
    finally:
        loc.terrain_data = saved_terrain

    assert abs(r_1322["world_x"] - r_1322_noterr["world_x"]) < 1e-6
    assert abs(r_1322["world_y"] - r_1322_noterr["world_y"]) < 1e-6
    assert abs(r_1215["world_x"] - r_1215_noterr["world_x"]) < 1e-6
    assert abs(r_1215["world_y"] - r_1215_noterr["world_y"]) < 1e-6

    # C. diag_20261120_231651 (Map 2, Area 16, Ga Byaroza — recovered by Head-to-Head Terrain Guard)
    img_1651 = _read_img("scratch/diagnostics/diag_20261120_231651/01_game.png")
    r_1651 = _locate_cold(loc, img_1651)
    assert r_1651["map_id"] == 2 and r_1651["area_id"] == 16 and r_1651["is_underground"] is False
    assert math.isfinite(r_1651["world_x"]) and math.isfinite(r_1651["world_y"])
    assert abs(r_1651["world_x"] - (-5541.9)) <= 15.0 and abs(r_1651["world_y"] - (-7014.9)) <= 15.0, (
        f"diag_20261120_231651 Ga Byaroza recovery misaligned: ({r_1651['world_x']:.2f}, {r_1651['world_y']:.2f})"
    )

    # C2. Verify screen-anchor + WORLD_ANCHOR_Y_OFFSET shift without terrain refinement on Map 2 surface
    img_2044 = _read_img("scratch/diagnostics/diag_20261120_204445/01_game.png")
    saved_terrain = loc.terrain_data
    try:
        loc.terrain_data = {}
        r_2044 = _locate_cold(loc, img_2044)
    finally:
        loc.terrain_data = saved_terrain
    wps2 = {w["id"]: w for w in loc.maps[2]["waypoints"]}
    inl_2044 = [((i["pixel_x"], i["pixel_y"], 100), wps2[i["waypoint_id"]]) for i in r_2044["inliers"]]
    sc_raw, _, ty_raw, _ = loc._fit_isometric_lsq(inl_2044)
    wy_raw = sc_raw * r_2044["pixel_y"] + ty_raw
    applied_dy = r_2044["world_y"] - wy_raw
    expected_dy = loc.WORLD_ANCHOR_Y_OFFSET - 5.0 * (img_2044.shape[0] / 1440.0) * sc_raw
    assert abs(applied_dy - expected_dy) <= 0.5, (
        f"diag_20261120_204445 anchor shift mismatch: got {applied_dy:+.2f}w, expected {expected_dy:+.2f}w"
    )

    # D. diag_20260920_214102 (Map 36, surface)
    img_4102 = _read_img("scratch/diagnostics/diag_20260920_214102/01_game.png")
    r_4102 = _locate_cold(loc, img_4102)
    assert r_4102["map_id"] == 36 and r_4102["is_underground"] is False
    saved_terrain = loc.terrain_data
    try:
        loc.terrain_data = {}
        r_4102_noterr = _locate_cold(loc, img_4102)
    finally:
        loc.terrain_data = saved_terrain
    err_4102_pre = math.hypot(
        r_4102["world_x"] - r_4102_noterr["world_x"],
        r_4102["world_y"] - r_4102_noterr["world_y"],
    )
    assert err_4102_pre <= 2.0, f"diag_20260920_214102 pre-Sobel drift too large: {err_4102_pre:.2f}w"

    print(
        f"  [OK] 011322=({r_1322['world_x']:.2f}, {r_1322['world_y']:.2f}) | "
        f"011215=({r_1215['world_x']:.2f}, {r_1215['world_y']:.2f}) | "
        f"231651=({r_1651['world_x']:.1f}, {r_1651['world_y']:.1f}) | "
        f"204445_dwy={applied_dy:+.2f}w | 214102_pre_sobel={err_4102_pre:.2f}w"
    )


def verify_zoom_invariance_0_to_100(loc: MapLocator) -> None:
    print("[4/6] Verifying 0%..100% zoom invariance (with and without terrain_n2)...")
    img0 = _read_img("scratch/0zoom.png")
    img100 = _read_img("scratch/100zoom.png")

    r0 = _locate_cold(loc, img0)
    r100 = _locate_cold(loc, img100)
    assert r0["map_id"] == 2 and r0["area_id"] == 16 and 1.80 <= r0["scale"] <= 2.05
    assert r100["map_id"] == 2 and r100["area_id"] == 16 and 0.29 <= r100["scale"] <= 0.35

    dx = abs(r0["world_x"] - r100["world_x"])
    dy = abs(r0["world_y"] - r100["world_y"])
    dist = math.hypot(dx, dy)
    assert dx <= 2.0 and dy <= 2.5 and dist <= 3.0, (
        f"0zoom vs 100zoom (with Sobel) drift too large: dx={dx:.2f}, dy={dy:.2f}, d={dist:.2f}"
    )

    # Also verify pure anchor-compensated zoom invariance WITHOUT terrain_n2 refinement
    saved_terrain = loc.terrain_data
    try:
        loc.terrain_data = {}
        r0_nt = _locate_cold(loc, img0)
        r100_nt = _locate_cold(loc, img100)
    finally:
        loc.terrain_data = saved_terrain

    dx_nt = abs(r0_nt["world_x"] - r100_nt["world_x"])
    dy_nt = abs(r0_nt["world_y"] - r100_nt["world_y"])
    dist_nt = math.hypot(dx_nt, dy_nt)
    assert dx_nt <= 3.0 and dy_nt <= 3.5 and dist_nt <= 4.5, (
        f"0zoom vs 100zoom (NO terrain_n2) drift too large: dx={dx_nt:.2f}, dy={dy_nt:.2f}, d={dist_nt:.2f}"
    )
    print(
        f"  [OK] 0zoom (sc={r0['scale']:.3f}) vs 100zoom (sc={r100['scale']:.3f}): "
        f"with_sobel={dist:.2f}w (dx={dx:.2f}, dy={dy:.2f}) | without_terrain={dist_nt:.2f}w (dx={dx_nt:.2f}, dy={dy_nt:.2f})"
    )


def verify_player_arrow_pivot_and_heading(loc: MapLocator) -> None:
    print("[5/6] Verifying player arrow rotation-pivot accuracy & nose heading consistency...")
    centered_paths = [
        "scratch/diagnostics/diag_20261121_011322/01_game.png",
        "scratch/diagnostics/diag_20261121_011215/01_game.png",
        "scratch/diagnostics/diag_20261120_231651/01_game.png",
        "scratch/diagnostics/diag_20260920_214102/01_game.png",
    ]
    errs_1440 = []
    for rel in centered_paths:
        img = _read_img(rel)
        h, w = img.shape[:2]
        su = h / 1440.0
        arrow = loc.detect_player_arrow(img)
        assert arrow is not None, f"Arrow missed on {rel}"
        px, py, hdg = arrow
        err = math.hypot(px - (w - 1) / 2.0, py - (h - 1) / 2.0) / su
        errs_1440.append(err)
        assert err <= 0.75, f"Arrow pivot error too large on {rel}: {err:.2f}px @1440p"
        assert 0.0 <= hdg < 360.0

        # Check 1080p downscale preserves sub-pixel pivot precision
        img_1080 = cv2.resize(img, (1920, 1080), interpolation=cv2.INTER_AREA)
        a1080 = loc.detect_player_arrow(img_1080)
        assert a1080 is not None
        err_1080 = math.hypot(a1080[0] - 959.5, a1080[1] - 539.5) / (1080.0 / 1440.0)
        assert err_1080 <= 0.85, f"Arrow pivot error at 1080p too large on {rel}: {err_1080:.2f}px @1440p"

    # Cross-zoom heading consistency on 0zoom vs 100zoom (visually pointing right ~88 deg)
    a0 = loc.detect_player_arrow(_read_img("scratch/0zoom.png"))
    a100 = loc.detect_player_arrow(_read_img("scratch/100zoom.png"))
    assert a0 is not None and a100 is not None
    hdg_diff = abs(((a0[2] - a100[2] + 180.0) % 360.0) - 180.0)
    assert hdg_diff <= 2.0, f"0zoom vs 100zoom heading mismatch: {a0[2]:.1f} vs {a100[2]:.1f}"
    assert 75.0 <= a0[2] <= 105.0, f"Expected rightward nose heading (~88 deg) on 0zoom, got {a0[2]:.1f}"
    print(
        f"  [OK] Arrow pivot err @1440p: max={max(errs_1440):.2f}px (<=0.75px) | "
        f"0zoom/100zoom heading={a0[2]:.1f}/{a100[2]:.1f} deg (diff={hdg_diff:.2f} deg)"
    )


def verify_surface_corpus_pre_sobel_residuals(loc: MapLocator) -> None:
    print("[6/6] Verifying pre-Sobel residuals across all high-confidence surface fixtures...")
    files = (
        sorted(glob.glob(os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_*", "01_game.png")))
        + sorted(glob.glob(os.path.join(PROJECT_ROOT, "scratch", "Test-images", "*.png")))
        + [
            os.path.join(PROJECT_ROOT, "scratch", "0zoom.png"),
            os.path.join(PROJECT_ROOT, "scratch", "100zoom.png"),
        ]
    )
    pre_ey_signed = []
    accepted_count = 0
    for fp in files:
        dname = os.path.basename(os.path.dirname(fp)) if fp.endswith("01_game.png") else os.path.basename(fp)
        # In-game (minimap) reports, not Map (M) screens -> nothing to localize
        if dname in ("diag_20261121_011520", "diag_20261121_205434", "diag_20261122_032842", "diag_20261122_115106"):
            continue
        img = cv2.imread(fp)
        assert img is not None
        res = _locate_cold(loc, img)
        if res["in_game_is_underground"]:
            continue
        mid = res["map_id"]
        wps = {w["id"]: w for w in loc.maps[mid]["waypoints"]}
        inl = [((i["pixel_x"], i["pixel_y"], 100), wps[i["waypoint_id"]]) for i in res["inliers"] if i["waypoint_id"] in wps]
        if len(inl) < 2:
            continue
        adj = loc._compensate_inliers_screen_anchors(inl, img.shape[0] / 1440.0)
        sc, tx, ty, _ = loc._fit_isometric_lsq(adj)
        if mid in loc.WORLD_ANCHOR_MAP_IDS:
            ty += loc.WORLD_ANCHOR_Y_OFFSET
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        tx_r, ty_r, ok = loc._refine_transform_with_terrain(gray, mid, sc, tx, ty, is_underground=False)
        if ok:
            accepted_count += 1
            pre_ey_signed.append(ty_r - ty)

    assert accepted_count >= 17, f"Expected >= 17 high-confidence surface refinements, got {accepted_count}"
    signed_mean = float(np.mean(pre_ey_signed))
    pre_ey_abs = np.abs(pre_ey_signed)
    med_ey = float(np.median(pre_ey_abs))
    max_ey = float(np.max(pre_ey_abs))
    assert abs(signed_mean) <= 1.0, f"Systematic pre-Sobel Y bias too large: {signed_mean:+.2f}w"
    assert med_ey <= 2.5, f"Median pre-Sobel |dwy| too large: {med_ey:.2f}w"
    assert max_ey <= 8.0, f"Max pre-Sobel |dwy| exceeded 8.0w clamp: {max_ey:.2f}w"
    print(
        f"  [OK] {accepted_count} high-confidence surface fixtures refined within +/-8.0w: "
        f"signed_mean={signed_mean:+.2f}w (|<=1.0w|), |dwy| med={med_ey:.2f}w (<=2.5w), max={max_ey:.2f}w (<=8.0w)"
    )


def main() -> None:
    loc = MapLocator()
    try:
        verify_screen_anchor_scaling(loc)
        verify_underground_sobel_guard(loc)
        verify_key_diagnostic_accuracy(loc)
        verify_zoom_invariance_0_to_100(loc)
        verify_player_arrow_pivot_and_heading(loc)
        verify_surface_corpus_pre_sobel_residuals(loc)
    finally:
        loc.close()
    print("\nALL V5 ALIGNMENT CHECKS PASSED (6/6)!")


if __name__ == "__main__":
    main()
