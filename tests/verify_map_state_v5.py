"""
Genshin Helper v5.0 MapScreenStateMachine Verification Suite (Track 1D).
Validates:
  1. get_close_probe_rect exact ROI geometry matching frame[0:int(0.11*h), int(0.90*w):w]
  2. is_close_x_probe on real Map M frames vs browser/drawer/synthetic non-map frames
  3. 64-bin (4x4x4) RGB histogram & Bhattacharyya distance properties and thresholds
  4. Instant 1-frame scene-cut reset (bhatt >= 0.40) vs preserved 3-frame debounce (bhatt < 0.28)
  5. Backwards-compatible 4-state FSM transitions (CLOSED -> SETTLING -> SYNCED_IDLE <-> PANNING -> CLOSED)
"""
import glob
import os
import sys
import time

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.map_state import MapScreenStateMachine

OFFSCREEN_DRAWER_DIAGS = {"diag_20261121_011520"}
OPEN_WORLD_DIAGS = {"diag_20261121_205434", "diag_20261122_032842", "diag_20261122_115106", "diag_20261122_121803", "diag_20261122_161651"}


def _make_transient_anchor_drop(frame: np.ndarray) -> np.ndarray:
    """Shifts Map M slightly (~6%) and masks all 3 anchors so is_open=False & hysteresis=False while bhatt < 0.28."""
    h, w = frame.shape[:2]
    dy, dx = int(0.06 * h), int(0.06 * w)
    shifted = cv2.resize(frame[dy:, dx:], (w, h), interpolation=cv2.INTER_LINEAR)
    # Mask top-right Close-X, bottom-left Gear, and left ZoomSlider track
    shifted[0:int(0.11 * h), int(0.90 * w):w] = (32, 32, 32)
    shifted[int(0.88 * h):h, 0:int(0.09 * w)] = (32, 32, 32)
    shifted[int(0.20 * h):int(0.80 * h), 0:int(0.08 * w)] = (32, 32, 32)
    return shifted


def _make_synthetic_gameplay_frames(h: int = 1440, w: int = 2560) -> list[tuple[str, np.ndarray]]:
    """Builds realistic color-distributed synthetic non-map gameplay/loading frames."""
    rng = np.random.default_rng(12345)

    # Daytime overworld (bright sky blue top, vibrant green grass bottom)
    gp_day = np.empty((h, w, 3), dtype=np.uint8)
    gp_day[: h // 2] = (235, 190, 120)
    gp_day[h // 2 :] = (55, 145, 65)

    # Night overworld (deep navy sky + dark forest)
    gp_night = np.empty((h, w, 3), dtype=np.uint8)
    gp_night[: h // 2] = (50, 25, 18)
    gp_night[h // 2 :] = (28, 52, 30)

    # Bright white loading screen & pure black transition screen
    loading_white = np.full((h, w, 3), 245, dtype=np.uint8)
    loading_black = np.zeros((h, w, 3), dtype=np.uint8)

    # Uniform random noise
    noise = rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)

    return [
        ("gp_day", gp_day),
        ("gp_night", gp_night),
        ("loading_white", loading_white),
        ("loading_black", loading_black),
        ("noise", noise),
    ]


def test_get_close_probe_rect() -> None:
    print("[1/5] Testing get_close_probe_rect geometry...")
    for win_left, win_top, win_w, win_h in [
        (0, 0, 2560, 1440),
        (100, 50, 1920, 1080),
        (-1920, 120, 1366, 768),
        (2560, 0, 3840, 2160),
    ]:
        rx, ry, rw, rh = MapScreenStateMachine.get_close_probe_rect(win_left, win_top, win_w, win_h)
        assert rx == win_left + int(0.90 * win_w), f"Unexpected rx={rx}"
        assert ry == win_top, f"Unexpected ry={ry}"
        assert rw == win_w - int(0.90 * win_w), f"Unexpected rw={rw}"
        assert rh == int(0.11 * win_h), f"Unexpected rh={rh}"

        # Verify pixel-for-pixel slice equivalence with frame[0:int(0.11*h), int(0.90*w):w]
        dummy = np.arange(win_h * win_w, dtype=np.int32).reshape(win_h, win_w)
        slice_expected = dummy[0:int(0.11 * win_h), int(0.90 * win_w):win_w]
        rel_x, rel_y = rx - win_left, ry - win_top
        slice_probe = dummy[rel_y : rel_y + rh, rel_x : rel_x + rw]
        assert slice_probe.shape == slice_expected.shape
        assert np.array_equal(slice_probe, slice_expected)

    # Guard invalid dimensions
    assert MapScreenStateMachine.get_close_probe_rect(10, 20, 0, 1440) == (10, 20, 0, 0)
    assert MapScreenStateMachine.get_close_probe_rect(10, 20, 2560, -10) == (10, 20, 0, 0)
    print("  [OK] get_close_probe_rect exact slice alignment verified")


def test_is_close_x_probe(diag_files: list[str], browser_files: list[str]) -> None:
    print(f"[2/5] Testing is_close_x_probe on {len(diag_files)} diag + {len(browser_files)} browser + synthetic frames...")
    sm = MapScreenStateMachine()
    pos_checked = 0
    probe_times_ms: list[float] = []

    for fp in diag_files:
        dname = os.path.basename(os.path.dirname(fp))
        img = cv2.imread(fp)
        assert img is not None, f"Failed to read {fp}"
        h, w = img.shape[:2]
        rx, ry, rw, rh = sm.get_close_probe_rect(0, 0, w, h)
        roi = img[ry : ry + rh, rx : rx + rw]

        t0 = time.perf_counter()
        hit, score = sm.is_close_x_probe(roi, frame_h=h)
        probe_times_ms.append((time.perf_counter() - t0) * 1000.0)

        full_chk = sm.is_map_m_open(img)
        assert abs(score - full_chk["close_score"]) < 1e-6, (
            f"Probe score mismatch on {dname}: probe={score} vs full={full_chk['close_score']}"
        )

        if dname in OFFSCREEN_DRAWER_DIAGS or dname in OPEN_WORLD_DIAGS:
            assert not hit, f"Non-open/drawer Close-X should not hit on {dname}: score={score:.4f}"
        else:
            assert hit and score >= 0.78, f"Missed Close-X probe on {dname}: hit={hit}, score={score:.4f}"
            pos_checked += 1

    assert pos_checked >= 25, f"Expected >= 25 positive Map M diag frames, checked {pos_checked}"

    # Verify 1080p downscaled Map M frame also hits
    ref_img = cv2.imread(diag_files[0])
    assert ref_img is not None
    img_1080 = cv2.resize(ref_img, (1920, 1080), interpolation=cv2.INTER_AREA)
    rx, ry, rw, rh = sm.get_close_probe_rect(0, 0, 1920, 1080)
    hit_1080, score_1080 = sm.is_close_x_probe(img_1080[ry : ry + rh, rx : rx + rw], frame_h=1080)
    assert hit_1080 and score_1080 >= 0.78, f"1080p Close-X probe missed: score={score_1080:.4f}"

    # Verify BGRA 4-channel ROI compatibility
    rx0, ry0, rw0, rh0 = sm.get_close_probe_rect(0, 0, ref_img.shape[1], ref_img.shape[0])
    roi_bgra = cv2.cvtColor(ref_img[ry0 : ry0 + rh0, rx0 : rx0 + rw0], cv2.COLOR_BGR2BGRA)
    hit_bgra, score_bgra = sm.is_close_x_probe(roi_bgra, frame_h=ref_img.shape[0])
    assert hit_bgra and score_bgra >= 0.78, f"BGRA Close-X probe failed: score={score_bgra:.4f}"

    # Verify non-map browser frames return False
    for fp in browser_files:
        bimg = cv2.imread(fp)
        assert bimg is not None
        bh, bw = bimg.shape[:2]
        rx, ry, rw, rh = sm.get_close_probe_rect(0, 0, bw, bh)
        hit_b, score_b = sm.is_close_x_probe(bimg[ry : ry + rh, rx : rx + rw], frame_h=bh)
        assert not hit_b, f"False positive Close-X probe on browser {fp}: score={score_b:.4f}"

    # Verify synthetic gameplay/non-map frames & invalid inputs return False
    for label, synth in _make_synthetic_gameplay_frames():
        sh, sw = synth.shape[:2]
        rx, ry, rw, rh = sm.get_close_probe_rect(0, 0, sw, sh)
        hit_s, score_s = sm.is_close_x_probe(synth[ry : ry + rh, rx : rx + rw], frame_h=sh)
        assert not hit_s, f"False positive Close-X probe on synthetic {label}: score={score_s:.4f}"

    assert sm.is_close_x_probe(None, 1440) == (False, 0.0)
    assert sm.is_close_x_probe(np.zeros((0, 0, 3), dtype=np.uint8), 1440) == (False, 0.0)
    assert sm.is_close_x_probe(np.zeros((158, 256, 3), dtype=np.uint8), 400) == (False, 0.0)
    assert sm.is_close_x_probe(np.zeros((2, 2, 3), dtype=np.uint8), 1440) == (False, 0.0)
    assert sm.is_close_x_probe(np.zeros((158, 256), dtype=np.uint8), 1440) == (False, 0.0)

    med_ms = sorted(probe_times_ms)[len(probe_times_ms) // 2]
    assert med_ms < 5.0, f"is_close_x_probe too slow: median={med_ms:.3f}ms"
    print(f"  [OK] {pos_checked}/{pos_checked} Map M hit, 0/{len(browser_files)} browser FP, 0 synthetic FP, median={med_ms:.3f}ms")


def test_hist64_and_bhattacharyya(diag_files: list[str], browser_files: list[str]) -> None:
    print("[3/5] Testing 64-bin RGB histogram & Bhattacharyya distance...")
    fa = cv2.imread(diag_files[0])
    fb_brow = cv2.imread(browser_files[0])
    assert fa is not None and fb_brow is not None

    ha = MapScreenStateMachine._compute_rgb_hist64(fa)
    assert ha.shape == (64,) and ha.dtype == np.float32, f"Bad hist shape/dtype: {ha.shape}, {ha.dtype}"
    assert np.all(ha >= 0.0) and abs(float(ha.sum()) - 1.0) < 1e-5, f"Hist not L1-normalized: sum={ha.sum()}"

    # Cross-check exact equivalence with OpenCV 4x4x4 calcHist on frame[::8, ::8]
    sub = np.ascontiguousarray(fa[::8, ::8])
    ref_hist = cv2.calcHist([sub], [2, 1, 0], None, [4, 4, 4], [0, 256, 0, 256, 0, 256]).ravel().astype(np.float32)
    ref_hist /= float(ref_hist.sum())
    assert float(np.max(np.abs(ha - ref_hist))) < 1e-6, "hist64 diverges from cv2.calcHist 4x4x4 RGB reference"

    # Metric identities (float32 L1 rounding yields sqrt(~1e-7) <= ~3e-4; exact power-of-2 uniform is 0.0)
    assert MapScreenStateMachine._bhattacharyya_distance(ha, ha) < 1e-3
    h_uniform = np.full(64, 1.0 / 64.0, dtype=np.float32)
    assert MapScreenStateMachine._bhattacharyya_distance(h_uniform, h_uniform) == 0.0
    h_black = MapScreenStateMachine._compute_rgb_hist64(np.zeros((64, 64, 3), dtype=np.uint8))
    h_white = MapScreenStateMachine._compute_rgb_hist64(np.full((64, 64, 3), 255, dtype=np.uint8))
    assert abs(MapScreenStateMachine._bhattacharyya_distance(h_black, h_white) - 1.0) < 1e-6
    d_ab = MapScreenStateMachine._bhattacharyya_distance(ha, h_black)
    d_ba = MapScreenStateMachine._bhattacharyya_distance(h_black, ha)
    assert abs(d_ab - d_ba) < 1e-9

    # Shifted / panned / anchor-masked Map M frames must be < 0.28
    max_pan_bhatt = 0.0
    for fp in diag_files[:10]:
        if os.path.basename(os.path.dirname(fp)) in OFFSCREEN_DRAWER_DIAGS:
            continue
        img = cv2.imread(fp)
        assert img is not None
        h0 = MapScreenStateMachine._compute_rgb_hist64(img)
        hh, ww = img.shape[:2]
        panned = cv2.resize(img[int(0.10 * hh) :, int(0.10 * ww) :], (ww, hh), interpolation=cv2.INTER_LINEAR)
        d_pan = MapScreenStateMachine._bhattacharyya_distance(h0, MapScreenStateMachine._compute_rgb_hist64(panned))
        max_pan_bhatt = max(max_pan_bhatt, d_pan)
        assert d_pan < 0.28, f"Panned Map M bhatt too high on {fp}: {d_pan:.4f}"

        transient = _make_transient_anchor_drop(img)
        d_tr = MapScreenStateMachine._bhattacharyya_distance(h0, MapScreenStateMachine._compute_rgb_hist64(transient))
        assert d_tr < 0.28, f"Transient anchor-drop bhatt too high on {fp}: {d_tr:.4f}"

    # Map M -> synthetic gameplay / loading / different scene must be >= 0.40
    min_cut_bhatt = 1.0
    for label, synth in _make_synthetic_gameplay_frames(fa.shape[0], fa.shape[1]):
        d_cut = MapScreenStateMachine._bhattacharyya_distance(ha, MapScreenStateMachine._compute_rgb_hist64(synth))
        min_cut_bhatt = min(min_cut_bhatt, d_cut)
        assert d_cut >= MapScreenStateMachine.SCENE_CUT_THRESHOLD, (
            f"Scene cut bhatt too low for {label}: {d_cut:.4f} < {MapScreenStateMachine.SCENE_CUT_THRESHOLD}"
        )

    d_brow = MapScreenStateMachine._bhattacharyya_distance(ha, MapScreenStateMachine._compute_rgb_hist64(fb_brow))
    assert d_brow >= MapScreenStateMachine.SCENE_CUT_THRESHOLD, f"Map->browser bhatt too low: {d_brow:.4f}"
    print(f"  [OK] max panned bhatt={max_pan_bhatt:.4f} (<0.28), min scene-cut bhatt={min_cut_bhatt:.4f} (>=0.40), map->browser={d_brow:.4f}")


def test_scene_cut_vs_debounce(diag_files: list[str], browser_files: list[str]) -> None:
    print("[4/5] Testing 1-frame scene-cut reset vs 3-frame transient debounce...")
    f_open = cv2.imread(diag_files[0])
    f_browser = cv2.imread(browser_files[0])
    assert f_open is not None and f_browser is not None
    f_transient = _make_transient_anchor_drop(f_open)
    gameplay_frames = _make_synthetic_gameplay_frames(f_open.shape[0], f_open.shape[1])

    sm = MapScreenStateMachine()
    chk_tr = sm.is_map_m_open(f_transient)
    assert not chk_tr["is_open"] and not chk_tr["slider_strict_ok"], f"Transient frame should drop all anchors: {chk_tr}"

    # Helper to bring FSM cleanly to SYNCED_IDLE
    def _enter_synced() -> None:
        sm.reset()
        assert sm.step(f_open)["state"] == sm.STATE_SETTLING
        assert sm.step(f_open)["state"] == sm.STATE_SETTLING
        r = sm.step(f_open)
        assert r["state"] == sm.STATE_SYNCED_IDLE and r["should_auto_sync"] is True
        assert sm._prev_hist64 is not None

    # A. Preserves 3-frame debounce when bhatt < SCENE_CUT_THRESHOLD (0.40)
    _enter_synced()
    r1 = sm.step(f_transient)
    assert r1["state"] == sm.STATE_SYNCED_IDLE and not r1["should_auto_sync"]
    assert r1.get("scene_cut") is False and float(r1.get("bhatt_dist", 1.0)) < 0.28, r1

    r2 = sm.step(f_transient)
    assert r2["state"] == sm.STATE_SYNCED_IDLE and not r2["should_auto_sync"]
    assert r2.get("scene_cut") is False and float(r2.get("bhatt_dist", 1.0)) < 0.28, r2

    r3 = sm.step(f_transient)
    assert r3["state"] == sm.STATE_CLOSED and not r3["should_auto_sync"]
    assert r3.get("scene_cut") is False and sm._prev_hist64 is None, r3

    # B. Recovers cleanly if Map M returns before 3rd debounced frame
    _enter_synced()
    assert sm.step(f_transient)["state"] == sm.STATE_SYNCED_IDLE
    assert sm.step(f_transient)["state"] == sm.STATE_SYNCED_IDLE
    rec = sm.step(f_open)
    assert rec["state"] in (sm.STATE_SYNCED_IDLE, sm.STATE_PANNING) and not rec["should_auto_sync"], rec

    # C. Instant 1-frame reset on scene cut (bhatt >= 0.40) from SYNCED_IDLE
    for label, gp in gameplay_frames + [("browser", f_browser)]:
        _enter_synced()
        cut = sm.step(gp)
        assert cut["state"] == sm.STATE_CLOSED, f"Scene cut {label} did not close on frame 1: {cut}"
        assert cut["should_auto_sync"] is False
        assert cut.get("scene_cut") is True, f"Expected scene_cut=True for {label}: {cut}"
        assert float(cut.get("bhatt_dist", 0.0)) >= sm.SCENE_CUT_THRESHOLD, cut
        assert sm._prev_hist64 is None, "reset() must clear _prev_hist64"

    # D. Instant 1-frame reset from SETTLING (user taps M then immediately closes Map)
    sm.reset()
    assert sm.step(f_open)["state"] == sm.STATE_SETTLING
    cut_settle = sm.step(gameplay_frames[0][1])
    assert cut_settle["state"] == sm.STATE_CLOSED and cut_settle.get("scene_cut") is True, cut_settle

    # E. Instant 1-frame reset after manual mark_synced(frame)
    sm.reset()
    sm.mark_synced(f_open)
    assert sm._prev_hist64 is not None
    cut_manual = sm.step(gameplay_frames[0][1])
    assert cut_manual["state"] == sm.STATE_CLOSED and cut_manual.get("scene_cut") is True, cut_manual

    # F. Drawer hysteresis still keeps Map M alive even when right 'X' is covered
    _enter_synced()
    f_drawer = f_open.copy()
    fh, fw = f_drawer.shape[:2]
    f_drawer[0:int(0.11 * fh), int(0.90 * fw):fw] = (32, 32, 32)
    for _ in range(4):
        hyst = sm.step(f_drawer)
        assert hyst["state"] == sm.STATE_SYNCED_IDLE and not hyst["should_auto_sync"], hyst

    f_011520 = cv2.imread(os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_20261121_011520", "01_game.png"))
    assert f_011520 is not None
    for _ in range(3):
        hyst15 = sm.step(f_011520)
        assert hyst15["state"] in (sm.STATE_SYNCED_IDLE, sm.STATE_PANNING) and not hyst15["should_auto_sync"], hyst15

    # Closing straight from drawer to gameplay still resets on frame 1
    cut_drawer = sm.step(gameplay_frames[0][1])
    assert cut_drawer["state"] == sm.STATE_CLOSED and cut_drawer.get("scene_cut") is True, cut_drawer

    print("  [OK] 3-frame transient debounce, 1-frame scene-cut reset & drawer hysteresis verified")


def test_four_state_fsm_transitions(diag_files: list[str], browser_files: list[str]) -> None:
    print("[5/5] Testing full 4-state FSM transitions (CLOSED -> SETTLING -> SYNCED_IDLE <-> PANNING -> CLOSED)...")
    f_open_a = cv2.imread(diag_files[0])
    f_open_b = cv2.imread(diag_files[4])
    f_closed = cv2.imread(browser_files[0])
    assert f_open_a is not None and f_open_b is not None and f_closed is not None

    sm = MapScreenStateMachine()
    assert sm.state == sm.STATE_CLOSED

    # 1. Stay CLOSED on non-map frames
    for _ in range(2):
        res = sm.step(f_closed)
        assert res["state"] == sm.STATE_CLOSED and not res["should_auto_sync"]

    # 2. CLOSED -> SETTLING -> SYNCED_IDLE (fires exactly once)
    r_open1 = sm.step(f_open_a)
    assert r_open1["state"] == sm.STATE_SETTLING and not r_open1["should_auto_sync"]
    r_open2 = sm.step(f_open_a)
    assert r_open2["state"] == sm.STATE_SETTLING and not r_open2["should_auto_sync"]
    r_open3 = sm.step(f_open_a)
    assert r_open3["state"] == sm.STATE_SYNCED_IDLE and r_open3["should_auto_sync"] is True

    # 3. Stay SYNCED_IDLE while static (never re-fires)
    r_idle = sm.step(f_open_a)
    assert r_idle["state"] == sm.STATE_SYNCED_IDLE and not r_idle["should_auto_sync"]

    # 4. SYNCED_IDLE -> PANNING when center changes (diff > 4.5)
    f_pan = f_open_a.copy()
    fh, fw = f_pan.shape[:2]
    f_pan[int(0.25 * fh) : int(0.75 * fh), int(0.25 * fw) : int(0.75 * fw)] = 210
    r_pan = sm.step(f_pan)
    assert r_pan["state"] == sm.STATE_PANNING and not r_pan["should_auto_sync"]

    # 5. PANNING -> SYNCED_IDLE when static again (diff < 3.0, never re-fires)
    r_settled_again = sm.step(f_pan)
    assert r_settled_again["state"] == sm.STATE_SYNCED_IDLE and not r_settled_again["should_auto_sync"]

    # 6. SYNCED_IDLE -> CLOSED on scene cut
    r_close = sm.step(f_closed)
    assert r_close["state"] == sm.STATE_CLOSED and not r_close["should_auto_sync"] and r_close.get("scene_cut") is True

    # 7. Re-opening Map M fires auto-sync cleanly on the new session
    sm.step(f_open_b)
    sm.step(f_open_b)
    r_reopen = sm.step(f_open_b)
    assert r_reopen["state"] == sm.STATE_SYNCED_IDLE and r_reopen["should_auto_sync"] is True

    # 8. request_retry bounded at MAX_RETRIES = 2
    assert sm.request_retry() is True and sm.state == sm.STATE_SETTLING
    assert sm.request_retry() is True and sm.state == sm.STATE_SETTLING
    assert sm.request_retry() is False and sm.state == sm.STATE_SYNCED_IDLE

    # 9. None frame resets cleanly
    r_none = sm.step(None)
    assert r_none["state"] == sm.STATE_CLOSED and sm._prev_hist64 is None

    print("  [OK] Full 4-state FSM lifecycle & retry bounds verified")


def main() -> None:
    t0 = time.perf_counter()
    diag_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_*", "01_game.png")))
    browser_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_*", "02_browser.png")))
    assert len(diag_files) >= 26, f"Expected >= 26 diagnostic 01_game.png fixtures, found {len(diag_files)}"
    assert len(browser_files) >= 26, f"Expected >= 26 diagnostic 02_browser.png fixtures, found {len(browser_files)}"

    test_get_close_probe_rect()
    test_is_close_x_probe(diag_files, browser_files)
    test_hist64_and_bhattacharyya(diag_files, browser_files)
    test_scene_cut_vs_debounce(diag_files, browser_files)
    test_four_state_fsm_transitions(diag_files, browser_files)

    elapsed = time.perf_counter() - t0
    print(f"\nALL TRACK 1D MAP_STATE V5 CHECKS PASSED in {elapsed:.2f}s!")


if __name__ == "__main__":
    main()
