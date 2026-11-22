"""
Verification for the v5.4 in-game navigation UX pass:

1. Calm arrow: locked target survives nearest-N feed churn (coverage rule), collected/vanished
   targets are dropped, auto-advance picks the nearest pin, floor flicker is debounced, the
   spring needle ignores position jitter near a pin, and close range switches to the radar dot.
2. Readable target label: category (HoYoLAB label) name replaces generic "Map Pin", no lone "G".
3. HUD placement presets + resolution-independent custom anchor.
4. In-game Guide Card / flash / edit-mode rendering, undo re-insertion, guide fetcher parsing.
5. Web map: player marker lives in a dedicated pane above HoYoLAB's z-index 9999 icon canvas.
6. v5.4.1: proximity takeover (pin at hand beats a far lock), camera-cone temporal gate
   (false tier-3 guesses rejected, body fallback), web feed = individual enabled pins only.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time

import cv2
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from src.heading360 import CameraHeadingFuser  # noqa: E402
from src.nav_controller import NavGuidance, NavigationController, NavPin  # noqa: E402
from src.nav_hud import (  # noqa: E402
    POSITION_MODES,
    NavHudRenderer,
    NavMiniHud,
    SpringAngleSmoother,
    noise_deadband_deg,
)
from src.pin_guide import PinGuideFetcher, clean_guide_text  # noqa: E402


def _ring(n: int, radius: float, start: int = 0):
    return [
        NavPin(id=str(start + i), name="Map Pin", category="Rương Thường",
               lat=-math.cos(2 * math.pi * i / n) * radius, lng=math.sin(2 * math.pi * i / n) * radius)
        for i in range(n)
    ]


def verify_target_lock_under_feed_churn() -> None:
    near = NavPin(id="near", name="Map Pin", category="Rương Tinh Xảo", lat=-30.0, lng=0.0)
    others = _ring(30, 120.0, start=100)
    ctrl = NavigationController()
    ctrl.set_pins([near] + others[:24], coverage=(0.0, 0.0, 120.0))
    g = ctrl.compute_guidance(0.0, 0.0, 0.0, 2)
    assert g.target is not None and g.target.id == "near"

    # Player walks away (15 m steps, between ring pins, frozen dwell clock); browser's nearest-25
    # window no longer includes the locked pin (OUTSIDE the searched radius) -> lock must hold.
    ctrl._clock = lambda: 0.0
    for step_lng in range(15, 301, 15):
        g_walk = ctrl.compute_guidance(0.0, float(step_lng), 0.0, 2)
        assert g_walk.target is not None and g_walk.target.id == "near", f"flip at {step_lng}: {g_walk.target}"
    ctrl.set_pins(others[:25], coverage=(0.0, 300.0, 200.0))
    g2 = ctrl.compute_guidance(0.0, 300.0, 0.0, 2)
    assert g2.target is not None and g2.target.id == "near", f"target flipped to {g2.target}"

    # Many alternating feeds with different subsets: still locked
    rng = random.Random(7)
    for _ in range(40):
        subset = rng.sample(others, 25)
        ctrl.set_pins(subset, coverage=(0.0, 300.0, 150.0))
        g3 = ctrl.compute_guidance(rng.uniform(-2, 2), 300.0 + rng.uniform(-2, 2), 0.0, 2)
        assert g3.target is not None and g3.target.id == "near"

    # Now the browser searched AROUND the pin and no longer lists it -> collected on web -> dropped
    ctrl.set_pins(others[:25], coverage=(-30.0, 0.0, 200.0))
    g4 = ctrl.compute_guidance(-28.0, 0.0, 0.0, 2)
    assert g4.target is not None and g4.target.id != "near"

    # Legacy calls without coverage keep old behaviour (no retention of plain targets)
    legacy = NavigationController([near])
    legacy.compute_guidance(0.0, 0.0, 0.0, 2)
    legacy.set_pins(others[:3])
    assert legacy.current_target is not None and legacy.current_target.id != "near"
    print("  [OK] Locked target survives nearest-N churn; web-collected target dropped")


def verify_nearest_after_collect_and_debounce() -> None:
    a = NavPin(id="a", name="A", lat=-10.0, lng=0.0)
    b = NavPin(id="b", name="B", lat=-200.0, lng=0.0)   # next in frozen order
    c = NavPin(id="c", name="C", lat=-10.0, lng=40.0)   # nearest after walking east
    ctrl = NavigationController([a, b, c])
    ctrl.compute_guidance(0.0, 0.0, 0.0, 2)
    g_walk = ctrl.compute_guidance(-10.0, 20.0, 0.0, 2)  # walk toward c (20m: outside takeover)
    assert g_walk.target is not None and g_walk.target.id == "a"
    nxt = ctrl.mark_collected()
    assert nxt is not None and nxt.id == "c", f"expected nearest 'c', got {nxt.id if nxt else None}"

    # Marking another pin explicitly must not move the lock
    ctrl2 = NavigationController([a, b, c])
    ctrl2.compute_guidance(0.0, 0.0, 0.0, 2)
    assert ctrl2.mark_collected("b").id == "a"

    # Floor flicker debounce (opt-in)
    sf = NavPin(id="sf", name="Surface", lat=-50.0, lng=0.0)
    ug = NavPin(id="ug", name="Cave", lat=-20.0, lng=0.0, floor_id="7", is_underground=True)
    deb = NavigationController([sf, ug], space_debounce_frames=4)
    assert deb.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "sf"
    for _ in range(3):  # 3 flicker frames underground -> must NOT switch
        assert deb.compute_guidance(0.0, 0.0, 0.0, 2, player_floor_id="7", player_is_underground=True).target.id == "sf"
        assert deb.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "sf"
    for _ in range(4):  # stable underground -> switches
        g = deb.compute_guidance(0.0, 0.0, 0.0, 2, player_floor_id="7", player_is_underground=True)
    assert g.target.id == "ug"
    print("  [OK] Nearest-after-collect, explicit-mark lock & floor flicker debounce passed")


def verify_undo_reinserts_web_dropped_pin() -> None:
    p = NavPin(id="p", name="P", category="Thần Đồng", lat=-5.0, lng=0.0)
    q = NavPin(id="q", name="Q", lat=-80.0, lng=0.0)
    ctrl = NavigationController()
    ctrl.set_pins([p, q], coverage=(0.0, 0.0, 200.0))
    ctrl.compute_guidance(0.0, 0.0, 0.0, 2)
    ctrl.mark_collected("p")
    ctrl.set_pins([q], coverage=(0.0, 0.0, 200.0))       # web marked it -> no longer listed
    assert ctrl.get_pin("p") is not None                 # still resolvable for undo
    assert ctrl.peek_history() == ("collected", "p")
    restored = ctrl.undo()
    assert restored is not None and restored.id == "p"
    ctrl.set_pins([q], coverage=(0.0, 0.0, 200.0))       # web has not unmarked yet -> protected
    assert ctrl.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "p"
    print("  [OK] Undo re-inserts web-dropped pin and protects it during web unmark")


def verify_calm_needle_and_radar() -> None:
    assert noise_deadband_deg(100.0) < 1.5 and noise_deadband_deg(8.0) > 8.0

    # Near a pin (10 m) with ±1.4 m position noise the raw bearing swings ~±8°;
    # the spring needle output must stay within a few degrees.
    sm = SpringAngleSmoother()
    rng = random.Random(3)
    outs = []
    t = 0.0
    for i in range(240):
        dx = rng.uniform(-1.4, 1.4)
        dy = rng.uniform(-1.4, 1.4)
        raw = math.degrees(math.atan2(0.0 + dx, 10.0 + dy))
        sm.set_distance(10.0)
        sm.set_target(raw)
        t += 1 / 30
        v = sm.step(now=t)
        if i > 30:
            outs.append(v)
    spread = max(outs) - min(outs)
    assert spread < 6.0, f"needle jitter spread {spread:.1f}° too large"

    # Big retarget glides (no snap), wrap-safe across ±180°
    sm2 = SpringAngleSmoother()
    sm2.set_distance(80.0)
    sm2.set_target(170.0)
    sm2.step(now=0.0)
    sm2.set_target(-170.0)
    v1 = sm2.step(now=1 / 30)
    assert abs(v1) > 165.0, f"wrapped the long way: {v1}"
    for k in range(2, 60):
        v = sm2.step(now=k / 30)
    assert abs(((v + 170.0 + 180.0) % 360.0) - 180.0) < 2.0

    r = NavHudRenderer()
    pin = NavPin(id="t", name="Map Pin", category="Rương Quý Giá", lat=-18.0, lng=0.0)
    g = NavigationController([pin]).compute_guidance(0.0, 0.0, 0.0, 2)
    r.render(g, delta_deg=30.0, distance_m=18.0, lang="vi")
    assert r.last_render_meta["radar"] is True
    xy1 = r.last_render_meta["radar_xy"]
    r.render(g, delta_deg=33.0, distance_m=18.3, lang="vi")
    xy2 = r.last_render_meta["radar_xy"]
    assert abs(xy1[0] - xy2[0]) <= 1 and abs(xy1[1] - xy2[1]) <= 1, "radar dot should barely move"
    r.render(g, delta_deg=30.0, distance_m=60.0, lang="vi")
    assert r.last_render_meta["radar"] is False
    print(f"  [OK] Spring needle spread={spread:.2f}° at 10 m, wrap-safe glide & close-range radar passed")


def verify_labels_and_cards() -> None:
    r = NavHudRenderer()
    pin = NavPin(id="1", name="Điểm đánh dấu", category="Rương Tinh Xảo", lat=-60.0, lng=0.0)
    g = NavigationController([pin]).compute_guidance(0.0, 0.0, 0.0, 2)
    r.render(g, lang="vi", hotkey_label="Alt + G", guide_hotkey_label="Alt + H")
    assert r.last_render_meta["title"].startswith("Rương Tinh Xảo"), r.last_render_meta["title"]
    assert NavHudRenderer._format_floor_tag(g, "vi") == ""
    numeric = NavPin(id="2", name="Map Pin", category="123", lat=0.0, lng=0.0)
    assert numeric.display_name == "Map Pin"

    near = NavigationController([pin]).compute_guidance(-50.0, 0.0, 0.0, 2)
    r.render(near, lang="vi", hotkey_label="Alt + G", guide_hotkey_label="Alt + H")
    assert "Alt + G" in r.last_render_meta["subtitle"] and "Alt + H" in r.last_render_meta["subtitle"]

    base = r.render(g, lang="vi")
    flash = r.render(g, lang="vi", flash_text="Đã ghim: Rương Tinh Xảo", flash_kind="success")
    assert r.last_render_meta["subtitle"].startswith("Đã ghim") and flash.height > base.height
    r.render(g, lang="vi", edit_mode=True, edit_hint="drag")
    assert r.last_render_meta["palette"] == "edit"

    # Guide card: placeholder when empty, image + text when loaded
    empty = r.render(g, lang="vi", guide_open=True, guide_hotkey_label="Alt + H")
    assert r.last_render_meta["guide_open"] is True and empty.height > base.height
    pin.image_url = "https://example.invalid/guide.png"
    pin.description = "Leo lên mỏm đá phía sau tượng.\nRương nằm sau bụi cây."
    assert r.put_guide_image(pin.image_url, Image.new("RGB", (640, 400), (60, 120, 200)))
    full = r.render(g, lang="vi", guide_open=True, guide_hotkey_label="Alt + H")
    assert full.height > empty.height + 100 and full.height < 520, full.size
    print(f"  [OK] Category title, hotkey hints, flash/edit palettes & Guide Card {full.size} passed")


def verify_positions() -> None:
    assert set(POSITION_MODES) == {"under_minimap", "right_of_minimap", "top_center", "bottom_center", "custom"}
    win = (0, 0, 1920, 1080)
    wa = (0, 0, 1920, 1040)
    ux, uy = NavMiniHud.compute_hud_position(300, 60, win, position_mode="under_minimap", work_area=wa)
    rx, ry = NavMiniHud.compute_hud_position(300, 60, win, position_mode="right_of_minimap", work_area=wa)
    assert rx > ux and ry < uy, "right_of_minimap must sit beside (not below) the minimap"
    bx, by = NavMiniHud.compute_hud_position(300, 60, win, position_mode="bottom_center", work_area=wa)
    assert abs(bx + 150 - 960) <= 1 and by > 700
    cx, cy = NavMiniHud.compute_hud_position(300, 60, win, position_mode="custom", work_area=wa, custom_pos=(0.5, 0.25))
    assert (cx, cy) == (960, 270)
    # Same fractions at 1440p scale with the window
    cx2, cy2 = NavMiniHud.compute_hud_position(
        300, 60, (0, 0, 2560, 1440), position_mode="custom", work_area=(0, 0, 2560, 1400), custom_pos=(0.5, 0.25)
    )
    assert (cx2, cy2) == (1280, 360)
    # custom without anchor falls back to under_minimap; unknown modes normalise
    assert NavMiniHud.compute_hud_position(300, 60, win, position_mode="custom", work_area=wa) == (ux, uy)
    assert NavMiniHud._norm_position_mode("top_right") == "under_minimap"
    assert NavMiniHud._norm_custom_pos((1.7, -2)) == (0.98, 0.0)
    print("  [OK] HUD presets (right_of_minimap / bottom_center) & resolution-independent custom anchor passed")


def verify_guide_fetcher() -> None:
    assert clean_guide_text("Đi <b>qua</b> cầu<br/>rẽ trái&nbsp;sau đá") == "Đi qua cầu\nrẽ trái sau đá"
    calls = []

    def fake_json(url: str):
        calls.append(url)
        return {"retcode": 0, "data": {"info": {"content": "<p>Sau thác nước</p>", "img": "https://act-upload.hoyoverse.com/x.jpg"}}}

    def fake_bytes(url: str) -> bytes:
        calls.append(url)
        return b"img"

    f = PinGuideFetcher(lambda: "vi", fetch_json=fake_json, fetch_bytes=fake_bytes)
    got = []
    assert f.request("107596", 2, got.append) is True
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        time.sleep(0.02)
    assert got and got[0].description == "Sau thác nước" and got[0].image_bytes == b"img"
    assert "point_id=107596" in calls[0] and "lang=vi-vn" in calls[0]
    assert "x-oss-process=image/resize" in calls[1]
    assert f.get_cached("107596") is not None and f.request("107596", 2, got.append) is False
    assert f.request("pin_1.0_2.0", 2, got.append) is False  # non-HoYoLAB ids are never fetched
    f.stop()
    print("  [OK] PinGuideFetcher parsing, resized image URL, cache & id filtering passed")


def verify_web_player_pane_and_feed() -> None:
    js = open(os.path.join(ROOT, "extension", "page_main.js"), encoding="utf-8").read()
    assert "PLAYER_PANE_Z = 690" in js and "createPane(PLAYER_PANE_NAME)" in js
    assert "markerOpts.pane = PLAYER_PANE_NAME" in js
    assert "pin_${idx}_" not in js, "index-based fallback ids make the HUD target unstable"
    assert "coverage_radius" in js and "label/tree" in js and "NAV_MARK_PIN" in js
    for lang in ("vi", "en"):
        ext = json.load(open(os.path.join(ROOT, "extension", "locales", f"{lang}.json"), encoding="utf-8"))
        for k in ("popup.nav_hud.pos_right_of_minimap", "popup.hotkeys.nav_mark_title", "page.nav.marked"):
            assert ext.get(k), f"missing {lang}:{k}"
    print("  [OK] Web player pane above icon canvas, stable feed ids, coverage & label names passed")


def verify_proximity_takeover() -> None:
    puzzle = NavPin(id="pz", name="Puzzle", lat=-60.0, lng=0.0)
    chest = NavPin(id="ch", name="Chest", lat=-5.0, lng=30.0)
    ctrl = NavigationController([puzzle, chest])
    ctrl.select_target("pz")  # user picks the puzzle by hand
    ctrl.compute_guidance(0.0, 0.0, 0.0, 2)
    g = ctrl.compute_guidance(-5.0, 26.0, 0.0, 2)  # standing 4m from the chest
    assert g.target.id == "pz", "a hand-picked target must not be taken over"

    auto = NavigationController([puzzle, chest])
    auto.compute_guidance(-30.0, 0.0, 0.0, 2)      # auto-locks the puzzle (30m vs ~39m)
    assert auto.current_target.id == "pz"
    assert auto.compute_guidance(-10.0, 20.0, 0.0, 2).target.id == "pz"   # chest 11m: not yet
    g2 = auto.compute_guidance(-5.0, 26.0, 0.0, 2)  # chest 4m, puzzle ~62m -> takeover
    assert g2.target.id == "ch" and g2.state == "arrived"
    assert auto.compute_guidance(-12.0, 26.0, 0.0, 2).target.id == "ch", "no ping-pong back"
    nxt = auto.mark_collected()
    assert nxt is not None and nxt.id == "pz"

    route = NavigationController([puzzle, chest])
    route.set_route_queue([puzzle, chest])
    assert route.compute_guidance(-5.0, 26.0, 0.0, 2).target.id == "pz", "route order wins"
    print("  [OK] Proximity takeover (hand-pick & route respected, hysteresis) passed")


def verify_teleport_and_clearly_closer_relock() -> None:
    puzzle = NavPin(id="pz", name="Puzzle", lat=-250.0, lng=0.0)
    oculus = NavPin(id="oc", name="Oculus", lat=-5.0, lng=20.0)
    far = NavPin(id="far", name="Far", lat=900.0, lng=900.0)
    # Report case: locked on a puzzle 250 m away, Oculus 20 m away (outside the 10 m takeover)
    clock = [0.0]
    ctrl = NavigationController([puzzle, oculus])
    ctrl._clock = lambda: clock[0]
    ctrl.select_target("pz")
    ctrl._user_locked_id = None  # auto lock (e.g. nearest at an earlier spot)
    assert ctrl.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "pz"
    clock[0] = 0.5
    assert ctrl.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "pz", "dwell not reached yet"
    clock[0] = 0.9
    assert ctrl.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "oc", "clearly closer pin must win after 0.8 s"
    clock[0] = 5.0
    assert ctrl.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "oc", "no flip back"

    # Similar distances never flip (ratio rule): 60 m vs 40 m
    a, b = NavPin(id="a", name="A", lat=-60.0, lng=0.0), NavPin(id="b", name="B", lat=0.0, lng=40.0)
    c2 = NavigationController([a, b])
    t2 = [0.0]
    c2._clock = lambda: t2[0]
    c2.select_target("a")
    c2._user_locked_id = None
    for k in range(10):
        t2[0] = k * 0.5
        assert c2.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "a"

    # Hand-picked target is kept even when something is clearly closer
    c3 = NavigationController([puzzle, oculus])
    t3 = [0.0]
    c3._clock = lambda: t3[0]
    c3.select_target("pz")
    for k in range(5):
        t3[0] = k * 1.0
        assert c3.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "pz"

    # Teleport (> 50 m jump) drops the auto lock -> nearest pin at the new spot, immediately
    c4 = NavigationController([puzzle, oculus, far])
    c4._clock = lambda: 0.0
    assert c4.compute_guidance(-240.0, 0.0, 0.0, 2).target.id == "pz"
    assert c4.compute_guidance(0.0, 0.0, 0.0, 2).target.id == "oc"
    # ...but a hand-picked one survives a teleport
    c4.select_target("far")
    assert c4.compute_guidance(-240.0, 0.0, 0.0, 2).target.id == "far"
    print("  [OK] Teleport re-lock, clearly-closer (x0.5, 30 m, 0.8 s dwell), no flip-flop, hand-pick kept passed")


def verify_camera_heading_fuser() -> None:
    f = CameraHeadingFuser()
    t = 100.0
    for _ in range(5):
        f.update(42.0, 0.9, "bgra_alpha", 40.0, 0.9, t)
        t += 0.05
    assert abs(f.estimate - 42.0) < 1e-6 and f.source == "bgra_alpha"
    # Report case: hsv_adaptive false positive at 177 deg while the true cone is ~42 deg
    for _ in range(12):
        f.update(177.0, 0.6, "hsv_adaptive", 40.0, 0.9, t)
        f.update(42.5, 0.9, "bgra_alpha", 40.0, 0.9, t + 0.02)
        t += 0.05
    assert abs(f.estimate - 42.5) < 1e-6, f"false guess leaked: {f.estimate}"
    # Single-frame trusted flip is ignored, a real fast turn is confirmed within 3 frames
    f.update(250.0, 0.9, "bgra_alpha", 40.0, 0.9, t)
    assert abs(f.estimate - 42.5) < 1e-6
    for i in range(3):
        t += 0.03
        f.update(252.0 + i, 0.9, "bgra_alpha", 40.0, 0.9, t)
    assert abs(f.estimate - 254.0) < 1e-6
    # Only guesses that disagree for a while -> stale -> falls back to the body arrow
    for _ in range(20):
        t += 0.1
        f.update(10.0, 0.5, "hsv_adaptive", 300.0, 0.9, t)
    assert f.source == "body" and abs(f.estimate - 300.0) < 1e-6
    # Agreeing guess (within 50 deg of body) is accepted again
    t += 0.05
    f.update(310.0, 0.6, "hsv_adaptive", 300.0, 0.9, t)
    assert f.source == "hsv_adaptive" and abs(f.estimate - 310.0) < 1e-6
    print("  [OK] Camera cone gate: false tier-3 rejected, flips confirmed, body fallback passed")


def verify_web_feed_individual_pins() -> None:
    js = open(os.path.join(ROOT, "extension", "page_main.js"), encoding="utf-8").read()
    assert "function _collectIndividualMarkers(ctx)" in js
    assert "_topClusterLevel.getAllChildMarkers()" in js
    assert "NAV_EXCLUDED_PARENT_LABELS = new Set([1, 426])" in js
    assert "markerObj.pointId ?? markerObj.id" in js
    q = js.split("function queryNearbyUncollectedPins", 1)[1].split("\n    function ", 1)[0]
    assert "_collectIndividualMarkers(ctx)" in q and "ctx.markerList" not in q, \
        "nearby feed must use individual enabled pins, never cluster centroids / landmark list"
    f = js.split("function _findMarkerForPin", 1)[1].split("\n    function ", 1)[0]
    assert "_collectIndividualMarkers(ctx)" in f and "ctx.markerList" not in f
    o = js.split("async function _openPinPopup", 1)[1].split("\n    async function ", 1)[0]
    assert "!marker._map" in o and "onMarkerClick" in o and "setView" in o
    assert "restoreView" in js.split("async function handleNavMarkPin", 1)[1]
    print("  [OK] Web feed: individual enabled pins, landmark categories excluded, clustered mark passed")


def verify_hud_background_visibility_and_report() -> None:
    from src.capture import WindowFinder
    game = (0, 0, 1920, 1080)
    # 2 monitors: browser maximized on the right monitor (incl. -8px maximize border) -> visible
    assert not WindowFinder.foreground_covers_game(game, (1912, -8, 3848, 1048))
    # 1 monitor: browser maximized over the game -> covered -> HUD hides
    assert WindowFinder.foreground_covers_game(game, (-8, -8, 1928, 1048))
    # small popup (calculator) in a corner of the game -> still visible
    assert not WindowFinder.foreground_covers_game(game, (1600, 700, 1900, 1050))
    assert not WindowFinder.foreground_covers_game(game, None)
    assert WindowFinder.is_game_visible_unfocused(0, game) is False
    main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "is_game_visible_unfocused(" in main_src and "nav_state=dict(current_nav_state)" in main_src
    diag_src = open(os.path.join(ROOT, "src", "diagnostics.py"), encoding="utf-8").read()
    assert '"nav_state": nav_state or {}' in diag_src
    print("  [OK] HUD stays on a 2nd-monitor Alt+Tab, hides when covered; nav state in reports passed")


def verify_minimap_recorder() -> None:
    import tempfile

    from src.minimap_recorder import MinimapRecorder

    with tempfile.TemporaryDirectory() as tmp:
        done = {}
        rec = MinimapRecorder(1.0, tmp, fps=10.0, on_done=lambda d, n: done.update(dir=d, n=n))
        img = np.zeros((120, 160, 4), np.uint8)
        img[:, :, 3] = 200
        t = 50.0
        for _ in range(40):  # 25 Hz ticks -> throttled to 10 fps, stops after 10 frames
            rec.add(img, 1920, 1080, t, {"state": "TRACKING", "camera": 42.0})
            t += 0.04
        rec.join(5.0)
        assert not rec.active and rec.frames == 10 and done.get("n") == 10
        frames = sorted(os.listdir(os.path.join(rec.out_dir, "frames")))
        assert len(frames) == 10 and frames[0] == "000001.png"
        back = cv2.imread(os.path.join(rec.out_dir, "frames", frames[0]), cv2.IMREAD_UNCHANGED)
        assert back.shape == (120, 160, 4) and int(back[0, 0, 3]) == 200, "alpha must survive (tier-1 cone)"
        lines = open(os.path.join(rec.out_dir, "meta.jsonl"), encoding="utf-8").read().splitlines()
        assert len(lines) == 10 and json.loads(lines[0])["camera"] == 42.0
        rec.add(img, 1920, 1080, t + 1.0)
        assert rec.frames == 10, "no frames after the budget"
    main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert '"--record-minimap"' in main_src and "minimap_recorder.add(hud_bgra" in main_src
    print("  [OK] Minimap recorder: 30fps throttle, BGRA lossless PNG + meta, budget stop passed")


def verify_heading_fast_path_and_pacing() -> None:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from verify_minimap_tracker_v5 import render_synthetic_minimap  # noqa: E402

    from src.minimap_tracker import STATE_TRACKING, HybridMinimapTracker
    from src.tile_cache import LruTerrainTileCache

    cache = LruTerrainTileCache(max_tiles=16)
    tr = HybridMinimapTracker(tile_cache=cache)
    wx, wy, t = -500.0, -1266.0, 100.0
    tr.set_map_m_anchor(2, wx, wy, timestamp=t)
    t += 0.05
    full = tr.step(render_synthetic_minimap(cache, 2, wx, wy, camera_heading_cw=40.0, body_heading_cw=40.0), now=t)
    assert full.state == STATE_TRACKING
    # Camera turns 40 -> 70 deg over 6 fast ticks (16 ms apart); position must not move
    times = []
    pose = None
    for k, cam in enumerate((45.0, 50.0, 55.0, 60.0, 65.0, 70.0)):
        t += 1.0 / 60.0
        mm = render_synthetic_minimap(cache, 2, wx, wy, camera_heading_cw=cam, body_heading_cw=40.0)
        t0 = time.perf_counter()
        pose = tr.step_headings_only(mm, now=t)
        times.append((time.perf_counter() - t0) * 1000.0)
        assert pose is not None and pose.match_method == "heading_fast" and pose.state == STATE_TRACKING
        assert pose.world_x == full.world_x and pose.world_y == full.world_y
    assert abs(((pose.camera_heading - 70.0 + 180.0) % 360.0) - 180.0) < 4.0, pose.camera_heading
    assert float(np.median(times)) < 8.0, f"fast path too slow: {np.median(times):.1f} ms"
    # Refuses flat / black frames and paused trackers (caller then runs the full step)
    assert tr.step_headings_only(np.zeros((218, 218, 3), np.uint8), now=t + 0.02) is None
    tr.pause(now=t + 0.03, reason="map_m")
    assert tr.step_headings_only(mm, now=t + 0.04) is None
    rx, ry, rw, rh = tr.minimap_capture_rect(1920, 1080)
    assert (rw, rh) == (218, 218) and rx >= 0 and ry >= 0
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "MINIMAP_TICK_S = 1.0 / 60.0" in src and "step_headings_only(mm_fast" in src
    assert "MINIMAP_TICK_S - (time.perf_counter() - tick_start)" in src, "deadline pacing"
    assert '"heading_fast")' in src
    assert "timeBeginPeriod" in src and "_hires_timer.set(live_minimap)" in src and "_hires_timer.set(False)" in src
    import main as app_main
    t = app_main._HighResTimer()
    t.set(True)
    assert t.active is (sys.platform == "win32")
    t.set(False)
    assert t.active is False
    print(f"  [OK] 60 Hz heading fast path: {np.median(times):.1f} ms/tick, position frozen, safe refusals passed")


def main() -> None:
    print("Running Genshin-Helper v5.4 navigation UX verification...")
    verify_target_lock_under_feed_churn()
    verify_nearest_after_collect_and_debounce()
    verify_undo_reinserts_web_dropped_pin()
    verify_calm_needle_and_radar()
    verify_labels_and_cards()
    verify_positions()
    verify_guide_fetcher()
    verify_web_player_pane_and_feed()
    verify_proximity_takeover()
    verify_teleport_and_clearly_closer_relock()
    verify_camera_heading_fuser()
    verify_web_feed_individual_pins()
    verify_hud_background_visibility_and_report()
    verify_minimap_recorder()
    verify_heading_fast_path_and_pacing()
    print("ALL V5.4 NAV UX CHECKS PASSED")


if __name__ == "__main__":
    main()
