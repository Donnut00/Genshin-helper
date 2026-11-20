"""
Genshin Helper v4.3.0 Offline & Integration Verification Suite.
Validates real diagnostic & Test-images screenshots, terrain-refined alignment,
extreme 0%/100% zoom consistency, off-screen compass badge rejection, 1-WP & 0-WP
synthetic fallbacks, MapScreenStateMachine drawer hysteresis / close debounce /
2-frame settling-drag abort FSM, warm latency budgets, DiagnosticManager user notes,
and DualSyncServer multi-tab lossless /poll?since=<seq> broadcast.
"""
import asyncio
import glob
import json
import math
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.diagnostics import DiagnosticManager
from src.locator import MapLocator
from src.map_state import MapScreenStateMachine
from src.server import DualSyncServer


DIAG_ASSERTIONS = {
    "diag_20260920_214102": {"map_id": 36},
    "diag_20260920_214145": {"map_id": 36},
    "diag_20260920_220416": {"map_id": 40, "is_underground": True, "group_id": 212, "floor_id": 306},
    "diag_20260920_220447": {"map_id": 40, "is_underground": True, "group_id": 207, "floor_id": 301},
    "diag_20260920_220225": {"map_id": 2, "area_id": 16, "group_id": 234, "floor_id": 347},
    "diag_20260920_220106": {"map_id": 2, "group_id": 91, "floor_id": 81},
    "diag_20260920_214910": {"map_id": 2, "group_id": 182, "floor_id": 249},
    "diag_20260920_220317": {"map_id": 2, "group_id": 224, "floor_id": 325},
    "diag_20260920_220745": {"map_id": 2, "is_underground": True, "group_id": 119, "floor_id": 150},
    "diag_20260920_221245": {"map_id": 2, "is_underground": False},
    "diag_20260920_222052": {"map_id": 2, "group_id": 146, "floor_id": 189},
    "diag_20261120_204445": {"map_id": 2, "area_id": 11, "is_underground": False},
    "diag_20261120_204655": {"map_id": 2, "area_id": 8, "is_underground": True, "group_id": 90, "floor_id": 78},
    "diag_20261120_205011": {"map_id": 2, "area_id": 16, "is_underground": True, "group_id": 235, "floor_id": 349},
    "diag_20261120_213109": {"map_id": 2, "area_id": 8, "is_underground": True, "group_id": 90, "floor_id": 78},
    "diag_20261120_231651": {"map_id": 2, "area_id": 16, "is_underground": False},
    "diag_20261120_232016": {"map_id": 2, "area_id": 16, "is_underground": False},
    "diag_20261120_232232": {"map_id": 2, "area_id": 16, "is_underground": False, "group_id": None},
    "diag_20261120_232623": {"map_id": 2, "area_id": 4, "is_underground": True, "group_id": 26, "floor_id": 46},
    "diag_20261121_011215": {"map_id": 2, "area_id": 4, "is_underground": True, "group_id": 26, "floor_id": 46},
    "diag_20261121_011322": {"map_id": 2, "area_id": 8, "is_underground": True, "group_id": 90, "floor_id": 78},
}

OFFSCREEN_DIAGS = {"diag_20261121_011520"}


def verify_diagnostics_and_test_images(loc: MapLocator) -> tuple[list[str], list[str]]:
    diag_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_*", "01_game.png")))
    test_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "scratch", "Test-images", "*.png")))
    assert len(diag_files) >= 27, f"Expected >= 27 diag screenshots, found {len(diag_files)}"
    assert len(test_files) == 23, f"Expected 23 Test-images screenshots, found {len(test_files)}"

    print(f"[1/6] Verifying {len(diag_files)} diagnostics + {len(test_files)} Test-images (cold-start)...")
    for fp in diag_files:
        dname = os.path.basename(os.path.dirname(fp))
        img = cv2.imread(fp)
        loc.last_known_pos = None
        loc.last_map_id = None
        if dname in OFFSCREEN_DIAGS:
            assert loc.detect_player_arrow(img) is None, f"{dname} should reject off-screen edge compass button"
            assert loc.locate_player(img) is None, f"{dname} locate_player must return None when arrow is off-screen"
            print(f"  [OK] {dname}: Rejected off-screen edge compass button (returned None)")
            continue

        res = loc.locate_player(img, debug=True)
        assert res and res.get("has_world_coords"), f"{dname} failed world localization: {res}"
        expected = DIAG_ASSERTIONS.get(dname, {})
        for k, v in expected.items():
            actual = res.get(k)
            assert actual == v, f"{dname} mismatch on {k}: expected {v!r}, got {actual!r} (world=({res.get('world_x')},{res.get('world_y')}), floor={res.get('hoyolab_floor_name')})"
        if dname == "diag_20261121_011322":
            assert abs(res["world_y"] - (-1478.50)) <= 4.0 and abs(res["world_x"] - (-4942.00)) <= 6.0, (
                f"{dname} world position misaligned: ({res['world_x']:.2f}, {res['world_y']:.2f})"
            )
        elif dname == "diag_20261121_011215":
            assert abs(res["world_y"] - 2208.50) <= 5.0, (
                f"{dname} world_y misaligned: {res['world_y']:.2f}"
            )
        print(
            f"  [OK] {dname}: Map={res['map_id']} Area={res.get('area_id')} "
            f"ug={res['is_underground']} G={res.get('group_id')} Fl={res.get('floor_id')} inl={res['inliers_count']}"
        )

    for fp in test_files:
        fname = os.path.basename(fp)
        img = cv2.imread(fp)
        loc.last_known_pos = None
        loc.last_map_id = None
        res = loc.locate_player(img, debug=True)
        assert res and res.get("has_world_coords"), f"{fname} failed world localization: {res}"
        print(
            f"  [OK] {fname}: Map={res['map_id']} Area={res.get('area_id')} "
            f"ug={res['is_underground']} G={res.get('group_id')} Fl={res.get('floor_id')} inl={res['inliers_count']}"
        )

    return diag_files, test_files


def verify_extreme_zooms(loc: MapLocator) -> None:
    print("[2/5] Verifying extreme 0% & 100% zoom screenshots...")
    p0 = os.path.join(PROJECT_ROOT, "scratch", "0zoom.png")
    loc.last_known_pos = None
    loc.last_map_id = None
    r0 = loc.locate_player(cv2.imread(p0), debug=True)
    assert r0 and r0.get("has_world_coords"), f"0zoom failed: {r0}"
    assert r0["map_id"] == 2 and r0.get("area_id") == 16, f"0zoom wrong map/area: {r0}"
    assert 1.80 <= r0["scale"] <= 2.05, f"0zoom scale out of bounds: {r0['scale']}"
    print(f"  [OK] 0zoom.png: Map={r0['map_id']} Area={r0['area_id']} scale={r0['scale']:.4f} inl={r0['inliers_count']}")

    p100 = os.path.join(PROJECT_ROOT, "scratch", "100zoom.png")
    loc.last_known_pos = None
    loc.last_map_id = None
    r100 = loc.locate_player(cv2.imread(p100), debug=True)
    assert r100 and r100.get("has_world_coords"), f"100zoom failed: {r100}"
    assert r100["map_id"] == 2 and r100.get("area_id") == 16, f"100zoom wrong map/area: {r100}"
    assert 0.29 <= r100["scale"] <= 0.35, f"100zoom scale out of bounds: {r100['scale']}"
    dy_z = abs(r0["world_y"] - r100["world_y"])
    d_z = math.hypot(r0["world_x"] - r100["world_x"], r0["world_y"] - r100["world_y"])
    assert dy_z <= 4.0 and d_z <= 10.0, f"0zoom vs 100zoom cross-zoom mismatch: dy={dy_z:.2f}, d={d_z:.2f}"
    print(f"  [OK] 100zoom.png: Map={r100['map_id']} Area={r100['area_id']} scale={r100['scale']:.4f} inl={r100['inliers_count']} | 0vs100 diff={d_z:.2f}w")


def verify_synthetic_fallbacks(loc: MapLocator) -> None:
    print("[3/5] Verifying synthetic 1-waypoint, 0-waypoint & anti-false-positive checks...")
    ref_path = os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_20260920_214102", "01_game.png")
    base_img = cv2.imread(ref_path)
    loc.last_known_pos = None
    loc.last_map_id = None
    truth = loc.locate_player(base_img, debug=True)
    assert truth and truth.get("has_world_coords") and len(truth.get("inliers", [])) >= 2

    # Build a synthetic 1-waypoint image by painting surrounding background color over all other waypoints
    one_wp_img = base_img.copy()
    keep_px, keep_py = int(truth["inliers"][0]["pixel_x"]), int(truth["inliers"][0]["pixel_y"])
    hsv = cv2.cvtColor(base_img, cv2.COLOR_BGR2HSV)
    for c in loc.detect_waypoint_candidates(base_img, arrow_pos=(truth["pixel_x"], truth["pixel_y"]), hsv=hsv):
        cx, cy = int(round(c[0])), int(round(c[1]))
        if math.hypot(cx - keep_px, cy - keep_py) > 15:
            sample = base_img[max(0, cy - 28), max(0, cx - 28)].tolist()
            cv2.circle(one_wp_img, (cx, cy), 22, sample, -1)

    loc.last_known_pos = None
    loc.last_map_id = None
    r_1wp = loc.locate_player(one_wp_img, debug=True)
    assert r_1wp and r_1wp.get("has_world_coords"), f"Synthetic 1-WP failed: {r_1wp}"
    assert r_1wp["map_id"] == truth["map_id"], f"1-WP wrong map: {r_1wp['map_id']}"
    err_1wp = math.hypot(r_1wp["world_x"] - truth["world_x"], r_1wp["world_y"] - truth["world_y"])
    assert err_1wp <= 40.0, f"1-WP world error too large: {err_1wp:.1f}m"
    print(f"  [OK] Synthetic 1-WP (Map 36): world error={err_1wp:.2f}m")

    # Build a synthetic 0-waypoint image by masking ALL waypoints on Map 36
    zero_wp_img = one_wp_img.copy()
    sample0 = base_img[max(0, keep_py - 28), max(0, keep_px - 28)].tolist()
    cv2.circle(zero_wp_img, (keep_px, keep_py), 22, sample0, -1)
    loc.last_known_pos = None
    loc.last_map_id = None
    r_0wp = loc.locate_player(zero_wp_img, debug=True)
    assert r_0wp and r_0wp.get("has_world_coords"), f"Synthetic 0-WP failed: {r_0wp}"
    assert r_0wp["map_id"] == truth["map_id"], f"0-WP wrong map: {r_0wp['map_id']}"
    err_0wp = math.hypot(r_0wp["world_x"] - truth["world_x"], r_0wp["world_y"] - truth["world_y"])
    assert err_0wp <= 50.0, f"0-WP world error too large: {err_0wp:.1f}m"
    print(f"  [OK] Synthetic 0-WP terrain fallback (Map 36): world error={err_0wp:.2f}m")

    # Anti-false-positive on blank/noise images
    for label, synth in [
        ("black", np.zeros((1440, 2560, 3), dtype=np.uint8)),
        ("white", np.full((1440, 2560, 3), 255, dtype=np.uint8)),
        ("noise", np.random.default_rng(42).integers(0, 256, size=(1440, 2560, 3), dtype=np.uint8)),
    ]:
        loc.last_known_pos = None
        loc.last_map_id = None
        rn = loc.locate_player(synth)
        assert not (rn and rn.get("has_world_coords")), f"False positive on {label}: {rn}"
    print("  [OK] Synthetic negative frames produced 0 false positives")


def verify_offscreen_arrows(loc: MapLocator, sample_files: list[str]) -> None:
    filtered = [fp for fp in sample_files if os.path.basename(os.path.dirname(fp)) not in OFFSCREEN_DIAGS]
    print(f"[3b/6] Verifying off-screen arrow rejection on {len(filtered)} real maps...")
    for fp in filtered:
        img = cv2.imread(fp)
        assert img is not None, f"Failed to read {fp}"
        arrow = loc.detect_player_arrow(img)
        assert arrow is not None, f"Arrow missed before masking on {fp}"
        px, py, heading = arrow
        assert 0.0 <= heading < 360.0, f"Heading out of range [0, 360): {heading}"

        masked = img.copy()
        h, w = masked.shape[:2]
        pad = int(32 * (h / 1440.0))
        x1, y1 = max(0, int(px) - pad), max(0, int(py) - pad)
        x2, y2 = min(w, int(px) + pad), min(h, int(py) + pad)
        # Fill with bright ocean blue (210, 175, 85) to confirm no water/waypoint false positive
        masked[y1:y2, x1:x2] = (210, 175, 85)
        assert loc.detect_player_arrow(masked) is None, f"False positive arrow after masking on {fp}"
        assert loc.locate_player(masked) is None, f"locate_player did not return None when off-screen on {fp}"
    print(f"  [OK] {len(sample_files)}/{len(sample_files)} off-screen arrow rejection (0 false positives)")


def verify_state_machine(diag_files: list[str], test_files: list[str]) -> None:
    print("[4/5] Verifying MapScreenStateMachine 3-anchor detector, drawer hysteresis & 4-state FSM...")
    sm = MapScreenStateMachine()
    all_pos = [fp for fp in diag_files if os.path.basename(os.path.dirname(fp)) not in OFFSCREEN_DIAGS] + test_files
    for fp in all_pos:
        chk = sm.is_map_m_open(cv2.imread(fp))
        assert chk["is_open"], f"Map M missed on {fp}: {chk}"

    browser_files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_*", "02_browser.png")))
    assert len(browser_files) >= 27, f"Expected >= 27 browser screenshots, found {len(browser_files)}"
    for fp in browser_files:
        chk = sm.is_map_m_open(cv2.imread(fp))
        assert not chk["is_open"] and not chk.get("slider_strict_ok"), f"False positive on browser image {fp}: {chk}"

    for synth in (
        np.zeros((1440, 2560, 3), dtype=np.uint8),
        np.full((1440, 2560, 3), 255, dtype=np.uint8),
        np.random.default_rng(7).integers(0, 256, size=(1440, 2560, 3), dtype=np.uint8),
    ):
        chk_s = sm.is_map_m_open(synth)
        assert not chk_s["is_open"] and not chk_s.get("slider_strict_ok"), f"Synth false positive: {chk_s}"

    f_closed = cv2.imread(browser_files[0])
    f_open_a = cv2.imread(diag_files[0])
    f_open_b = cv2.imread(diag_files[4])
    seq = [f_closed] * 3 + [f_open_a] * 6 + [f_closed] * 4 + [f_open_b] * 5 + [f_closed] * 4

    sm.reset()
    fire_indices = []
    states = []
    for idx, frame in enumerate(seq):
        out = sm.step(frame)
        states.append(out["state"])
        if out["should_auto_sync"]:
            fire_indices.append(idx)

    assert len(fire_indices) == 2, f"Expected exact 2 triggers, got {fire_indices} ({states})"
    assert fire_indices[0] in range(3, 9) and fire_indices[1] in range(13, 18), f"Fired on closed frame: {fire_indices}"

    # Verify HUD fade-in settling guard: changing bottom-right HUD blocks premature SETTLING trigger
    f_hud_fade = f_open_a.copy()
    fh_hud, fw_hud = f_hud_fade.shape[:2]
    f_hud_fade[int(0.86 * fh_hud):int(0.98 * fh_hud), int(0.75 * fw_hud):int(0.98 * fw_hud)] = 0
    sm.reset()
    assert not sm.step(f_open_a)["should_auto_sync"]  # CLOSED -> SETTLING
    assert not sm.step(f_hud_fade)["should_auto_sync"], "HUD fade-in frame should not pass settling!"
    assert not sm.step(f_open_a)["should_auto_sync"], "Frame where final HUD appears resets stability to 0"
    assert not sm.step(f_open_a)["should_auto_sync"], "Single settled frame after HUD change must still wait 1 more frame!"
    assert sm.step(f_open_a)["should_auto_sync"], "Expected trigger after 2 consecutive settled center+HUD frames"

    # Verify 2-step monotonic open fade-in during SETTLING does NOT false-abort teleport sync
    f_fade_a = f_open_a.copy()
    f_fade_a[int(0.25 * fh_hud):int(0.75 * fh_hud), int(0.25 * fw_hud):int(0.75 * fw_hud)] = 16
    f_fade_b = f_open_a.copy()
    f_fade_b[int(0.25 * fh_hud):int(0.75 * fh_hud), int(0.25 * fw_hud):int(0.75 * fw_hud)] = 180
    sm.reset()
    assert sm.step(f_fade_a)["state"] == sm.STATE_SETTLING
    s1 = sm.step(f_fade_b)
    assert s1["state"] == sm.STATE_SETTLING and not s1["should_auto_sync"], f"Fade step 1 aborted: {s1}"
    s2 = sm.step(f_open_a)
    assert s2["state"] == sm.STATE_SETTLING and not s2["should_auto_sync"], f"Fade step 2 aborted: {s2}"
    s3 = sm.step(f_open_a)
    assert s3["state"] == sm.STATE_SETTLING and not s3["should_auto_sync"], f"Expected 2 stable frames after fade: {s3}"
    s4 = sm.step(f_open_a)
    assert s4["should_auto_sync"], f"Expected auto-sync after monotonic fade settled: {s4}"

    # Verify dragging during SETTLING (2 consecutive center moves after fade window while HUD stays static) aborts auto-sync
    f_drag_a = f_open_a.copy()
    f_drag_a[int(0.25 * fh_hud):int(0.75 * fh_hud), int(0.25 * fw_hud):int(0.75 * fw_hud)] = 200
    f_drag_b = f_open_a.copy()
    f_drag_b[int(0.25 * fh_hud):int(0.75 * fh_hud), int(0.25 * fw_hud):int(0.75 * fw_hud)] = 40
    sm.reset()
    assert sm.step(f_open_a)["state"] == sm.STATE_SETTLING
    for pan_frame in (f_drag_a, f_drag_b, f_drag_a, f_drag_b):
        st_pan = sm.step(pan_frame)
        assert not st_pan["should_auto_sync"]
    drag_step = sm.step(f_drag_a)
    assert drag_step["state"] == sm.STATE_PANNING and not drag_step["should_auto_sync"], f"Drag did not abort settling: {drag_step}"
    for _ in range(4):
        after_drag = sm.step(f_open_a)
        assert not after_drag["should_auto_sync"], f"Re-fired auto-sync after settling drag: {after_drag}"

    # Verify manual mark_synced suppresses pending SETTLING auto-sync
    sm.reset()
    assert not sm.step(f_closed)["should_auto_sync"]
    assert not sm.step(f_open_a)["should_auto_sync"]
    sm.mark_synced(f_open_a)
    for _ in range(4):
        assert not sm.step(f_open_a)["should_auto_sync"]

    # Verify drawer/pin-popup hysteresis when right-side drawer hides top-right 'X' or Gear
    f_drawer = f_open_a.copy()
    fh, fw = f_drawer.shape[:2]
    f_drawer[0:int(0.11 * fh), int(0.90 * fw):fw] = (32, 32, 32)
    chk_drawer = sm.is_map_m_open(f_drawer)
    assert not chk_drawer["is_open"], "Masked close X should drop strict is_open"
    assert chk_drawer["anchor_count"] == 2 and chk_drawer["gear_score"] >= sm.ANCHOR_THRESHOLD, chk_drawer

    f_011520_path = os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_20261121_011520", "01_game.png")
    f_011520 = cv2.imread(f_011520_path)
    chk15 = sm.is_map_m_open(f_011520)
    assert not chk15["is_open"] and chk15["slider_strict_ok"], f"Expected slider_strict_ok on 011520 drawer: {chk15}"

    sm.reset()
    assert sm.step(f_drawer)["state"] == sm.STATE_CLOSED, "Drawer frame must never open from CLOSED"
    sm.step(f_open_a)
    sm.step(f_open_a)
    sm.step(f_open_a)
    res_settled = sm.step(f_open_a)
    assert res_settled["state"] == sm.STATE_SYNCED_IDLE
    for _ in range(3):
        hyst = sm.step(f_drawer)
        assert hyst["state"] == sm.STATE_SYNCED_IDLE and not hyst["should_auto_sync"], f"Drawer reset FSM: {hyst}"
    for _ in range(4):
        hyst15 = sm.step(f_011520)
        assert hyst15["state"] in (sm.STATE_SYNCED_IDLE, sm.STATE_PANNING) and not hyst15["should_auto_sync"], f"011520 reset FSM: {hyst15}"
    uncover = sm.step(f_open_a)
    assert uncover["state"] in (sm.STATE_SYNCED_IDLE, sm.STATE_PANNING) and not uncover["should_auto_sync"], "Closing drawer re-triggered sync!"

    # Verify close debounce: 2 transient closed frames do not reset, 3rd consecutive closed frame resets
    assert sm.step(f_closed)["state"] != sm.STATE_CLOSED
    assert sm.step(f_closed)["state"] != sm.STATE_CLOSED
    assert sm.step(f_closed)["state"] == sm.STATE_CLOSED

    print(f"  [OK] {len(all_pos)}/{len(all_pos)} open, 0/{len(browser_files)} false positives, HUD fade-in + hysteresis + debounce + drag abort verified")


def verify_ephemeral_server() -> None:
    print("[5/5] Verifying DualSyncServer endpoints (/status, /set_auto_sync, /trigger_diagnostic, /save_diagnostic_note, /poll, /shutdown) on port 18765...")
    shutdown_triggered = threading.Event()
    server_ready = threading.Event()
    state_holder = {"auto_sync_m": True}

    def run_srv():
        async def _main():
            srv = DualSyncServer(host="127.0.0.1", port=18765)
            srv.get_hotkey_info = lambda: {"auto_sync_m": state_holder["auto_sync_m"]}

            def _on_auto(val: bool) -> bool:
                state_holder["auto_sync_m"] = bool(val)
                return state_holder["auto_sync_m"]

            srv.on_auto_sync_change = _on_auto
            srv.on_trigger_diagnostic = lambda bs, note="": {
                "success": True,
                "folder_name": "diag_mock_web",
                "has_world_coords": True,
                "reason": "ok",
                "user_note": note,
            }
            stop_ev = asyncio.Event()
            srv.on_shutdown_request = lambda: (shutdown_triggered.set(), stop_ev.set())
            await srv.start()
            server_ready.set()
            try:
                await asyncio.wait_for(stop_ev.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            await srv.stop()

        asyncio.run(_main())

    th = threading.Thread(target=run_srv, daemon=True)
    th.start()
    assert server_ready.wait(timeout=5.0), "Ephemeral server failed to start"

    def req_json(path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        method = "POST" if payload is not None else "GET"
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        req = urllib.request.Request(f"http://127.0.0.1:18765{path}", data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            return json.loads(resp.read().decode("utf-8"))

    diag_root = DiagnosticManager._ensure_scratch_dir()
    tmp_folder = "diag_test_note_suite"
    tmp_dir = os.path.join(diag_root, tmp_folder)
    os.makedirs(tmp_dir, exist_ok=True)
    with open(os.path.join(tmp_dir, "03_report.json"), "w", encoding="utf-8") as rf:
        json.dump({"timestamp": "test"}, rf)

    try:
        st = req_json("/status")
        assert st["version"] == "4.0.0" and st.get("script_version") == "v4.3.0" and st["auto_sync_m"] is True, f"Unexpected /status: {st}"
        assert isinstance(st.get("latest_seq"), int), f"Missing latest_seq in /status: {st}"

        # /trigger_diagnostic from web UI should not duplicate DIAGNOSTIC_CAPTURED onto _event_queue
        trig_res = req_json("/trigger_diagnostic", {"source": "web_settings_modal", "note": "web note"})
        assert trig_res["ok"] and trig_res["bundle"]["user_note"] == "web note"
        assert req_json("/status")["queue_len"] == 0, "Unexpected duplicate broadcast on web UI trigger"

        # /save_diagnostic_note saves UTF-8 05_user_note.txt and updates 03_report.json
        note_text = "[Sai vị trí] Kiểm tra note tiếng Việt UTF-8"
        note_res = req_json("/save_diagnostic_note", {"folder_name": tmp_folder, "note": note_text})
        assert note_res["ok"] and note_res["folder_name"] == tmp_folder
        with open(os.path.join(tmp_dir, "05_user_note.txt"), "r", encoding="utf-8") as nf:
            assert nf.read().strip() == note_text
        with open(os.path.join(tmp_dir, "03_report.json"), "r", encoding="utf-8") as rf:
            assert json.load(rf)["user_note"] == note_text

        # Path traversal must be rejected cleanly with HTTP 400
        try:
            req_json("/save_diagnostic_note", {"folder_name": "../bad", "note": "x"})
            raise AssertionError("Expected HTTP 400 for invalid folder_name")
        except urllib.error.HTTPError as he:
            assert he.code == 400, f"Expected 400, got {he.code}"

        toggle_res = req_json("/set_auto_sync", {"auto_sync_m": False})
        assert toggle_res["ok"] and toggle_res["auto_sync_m"] is False
        poll_msg = req_json("/poll")
        assert poll_msg["type"] == "AUTO_SYNC_UPDATED" and poll_msg["auto_sync_m"] is False

        # Verify multi-tab lossless cursor delivery via /poll?since=<seq>
        hb1 = req_json("/poll?since=-1")
        hb2 = req_json("/poll?since=-1")
        assert hb1["type"] == "HEARTBEAT" and hb2["type"] == "HEARTBEAT"
        seq0 = int(hb1["_seq"])

        tab1_res = {}
        tab1_done = threading.Event()

        def poll_tab1():
            tab1_res["data"] = req_json(f"/poll?since={seq0}")
            tab1_done.set()

        t_tab1 = threading.Thread(target=poll_tab1, daemon=True)
        t_tab1.start()
        time.sleep(0.08)

        # Broadcast while Tab 1 is long-polling and Tab 2 is temporarily disconnected between polls
        req_json("/trigger_sync", {"lat": 123.0, "lng": 456.0})
        assert tab1_done.wait(timeout=3.0), "Tab 1 long-poll did not wake on broadcast"
        t_tab1.join(timeout=2.0)

        msg_tab1 = tab1_res.get("data", {})
        assert msg_tab1.get("type") == "SYNC_AND_MARK" and msg_tab1.get("_seq") == seq0 + 1, f"Tab 1 bad event: {msg_tab1}"

        # Tab 2 reconnects afterwards with since=seq0 and must STILL receive the exact same event!
        msg_tab2 = req_json(f"/poll?since={seq0}")
        assert msg_tab2.get("type") == "SYNC_AND_MARK" and msg_tab2.get("_seq") == seq0 + 1 and msg_tab2.get("lat") == 123.0, (
            f"Tab 2 missed broadcast during reconnect gap: {msg_tab2}"
        )

        sd_res = req_json("/shutdown", {})
        assert sd_res["ok"] and sd_res["shutting_down"] is True
        assert shutdown_triggered.wait(timeout=3.0), "Shutdown callback was not invoked"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        th.join(timeout=3.0)
    print("  [OK] Ephemeral server, multi-tab cursor /poll?since=<seq> & diagnostic note API passed cleanly")


def verify_latency_budget(loc: MapLocator) -> None:
    print("[6/6] Verifying warm latency budget (fsm.step < 25ms, locate_player < 180ms)...")
    fp = os.path.join(PROJECT_ROOT, "scratch", "diagnostics", "diag_20261121_011322", "01_game.png")
    img = cv2.imread(fp)
    sm = MapScreenStateMachine()
    # Warm up caches & OCR worker
    sr = sm.step(img).get("slider_res")
    loc.locate_player(img, slider_res=sr)

    t0 = time.perf_counter()
    step_res = sm.step(img)
    t1 = time.perf_counter()
    res = loc.locate_player(img, slider_res=step_res.get("slider_res"))
    t2 = time.perf_counter()

    fsm_ms = (t1 - t0) * 1000.0
    loc_ms = (t2 - t1) * 1000.0
    assert res and res.get("has_world_coords"), f"Warm locate failed: {res}"
    assert fsm_ms < 25.0, f"fsm.step too slow: {fsm_ms:.1f}ms >= 25ms"
    assert loc_ms < 180.0, f"locate_player too slow: {loc_ms:.1f}ms >= 180ms"
    print(f"  [OK] Warm latency: fsm.step={fsm_ms:.1f}ms (<25ms), locate_player={loc_ms:.1f}ms (<180ms)")


def main() -> None:
    loc = MapLocator()
    diag_files, test_files = verify_diagnostics_and_test_images(loc)
    verify_extreme_zooms(loc)
    verify_synthetic_fallbacks(loc)
    verify_offscreen_arrows(loc, diag_files[:5] + test_files[:5])
    verify_state_machine(diag_files, test_files)
    verify_ephemeral_server()
    verify_latency_budget(loc)
    print("\nALL VERIFICATION SUITES PASSED (49/49 active real + 011520 compass reject + 0zoom/100zoom + 1WP/0WP + off-screen + FSM + Multi-Tab Server + Latency)!")


if __name__ == "__main__":
    main()
