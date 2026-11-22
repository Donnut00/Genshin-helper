"""
Verification suite for Genshin-Helper v5 Track 3A:
  a) NavigationController (HoYoLAB CRS bearings, Sticky Target, Next/Prev/Skip/Blacklist/Mark/Undo,
     TSP Route Queue, Floor-Mismatch direct arrow warning & cross-floor cycling, Spot Card trigger)
     + CollectedHoldController (1s hold & cancel guards).
  b) CircularAngleSmoother (wrap-safe 0°/360° & ±180°, deadband) + NavHudRenderer
     (< 1.0 ms/frame steady-state benchmark, < 0.5 MB badge cache, Compact vs Expanded heights,
      lock-on & behind visual cues, Spot Card LRU 30 thumbs, alpha >= 220 zero sky hole-punching,
      bilingual vi & en).
  c) NavMiniHud positioning under minimap (1920x1080, 2560x1440, 3440x1440, Gamepad, top_center,
     work_area clamping) + live Win32 layered window lifecycle with zero GDI/USER handle leak.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import math
import os
import sys
import time
from typing import Tuple

import numpy as np
from PIL import Image

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from main import _normalize_raw_nav_pin
from src.game_settings import compute_ui_layout
from src.locator import MapLocator
from src.nav_controller import (
    CollectedHoldController,
    NavGuidance,
    NavPin,
    NavigationController,
    compute_hoyolab_bearing,
    normalize_angle_delta,
)
from src.nav_hud import (
    CircularAngleSmoother,
    NavHudRenderer,
    NavMiniHud,
    rgba_to_premultiplied_bgra,
)


def _get_gui_resource_counts() -> Tuple[int, int]:
    """Returns `(gdi_objects, user_objects)` for the current process via Win32 GetGuiResources."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.GetGuiResources.restype = wintypes.DWORD

    hproc = kernel32.GetCurrentProcess()
    gdi = int(user32.GetGuiResources(hproc, 0))   # GR_GDIOBJECTS
    user = int(user32.GetGuiResources(hproc, 1))  # GR_USEROBJECTS
    return gdi, user


def verify_cardinal_bearings_and_deltas() -> None:
    # HoYoLAB CRS: +lng = East, +lat = South, -lat = North
    cases = [
        ("North", -100.0, 0.0, 0.0),
        ("East", 0.0, 100.0, 90.0),
        ("South", 100.0, 0.0, 180.0),
        ("West", 0.0, -100.0, 270.0),
        ("NE", -100.0, 100.0, 45.0),
        ("SE", 100.0, 100.0, 135.0),
        ("SW", 100.0, -100.0, 225.0),
        ("NW", -100.0, -100.0, 315.0),
    ]
    for label, tlat, tlng, expected_deg in cases:
        dist, bearing = compute_hoyolab_bearing(0.0, 0.0, tlat, tlng)
        assert abs(bearing - expected_deg) < 1e-6, f"{label}: expected {expected_deg}°, got {bearing}°"
        assert dist > 99.0

    # Relative delta_deg checks
    assert abs(normalize_angle_delta(0.0, 0.0) - 0.0) < 1e-6
    assert abs(normalize_angle_delta(90.0, 0.0) - 90.0) < 1e-6
    assert abs(normalize_angle_delta(270.0, 0.0) - (-90.0)) < 1e-6
    assert abs(abs(normalize_angle_delta(180.0, 0.0)) - 180.0) < 1e-6
    assert abs(normalize_angle_delta(0.0, 270.0) - 90.0) < 1e-6
    assert abs(normalize_angle_delta(355.0, 5.0) - (-10.0)) < 1e-6
    assert abs(normalize_angle_delta(5.0, 355.0) - 10.0) < 1e-6

    # End-to-end via NavigationController.compute_guidance
    for label, tlat, tlng, expected_deg in cases[:4]:
        ctrl = NavigationController([NavPin(id=label, name=label, lat=tlat, lng=tlng, map_id=2)])
        g = ctrl.compute_guidance(0.0, 0.0, view_heading_deg=expected_deg, player_map_id=2)
        assert g.state == "tracking"
        assert g.bearing_deg is not None and abs(g.bearing_deg - expected_deg) < 1e-6
        assert g.delta_deg is not None and abs(g.delta_deg) < 1e-6
        assert g.is_locked_on is True
        assert g.is_behind is False

    print("  [OK] HoYoLAB CRS cardinal bearings (N=0°, E=90°, S=180°, W=270°) & relative delta_deg passed")


def verify_sticky_target_commands_and_undo() -> None:
    p1 = NavPin(id="p1", name="Rương Thường 1", lat=-30.0, lng=0.0, map_id=2)
    p2 = NavPin(id="p2", name="Rương Hiếm 2", lat=-60.0, lng=0.0, map_id=2)
    p3 = NavPin(id="p3", name="Thần Đồng 3", lat=-90.0, lng=0.0, map_id=2)
    ctrl = NavigationController([p1, p2, p3])

    # Initial lock picks nearest (p1 at 30m)
    g0 = ctrl.compute_guidance(0.0, 0.0, 0.0, player_map_id=2)
    assert g0.target is not None and g0.target.id == "p1"
    assert g0.state == "tracking"

    # Sticky Target stability: walking on while another pin is nearer but not at hand
    # (p2 at 25/20/15/12m, outside the 10m takeover radius) MUST keep p1.
    for walk_lat in (-35.0, -40.0, -45.0, -48.0):
        g_move = ctrl.compute_guidance(walk_lat, 0.0, 0.0, player_map_id=2)
        assert g_move.target is not None and g_move.target.id == "p1", (
            f"Sticky target flipped unexpectedly to {g_move.target.id if g_move.target else None} at {walk_lat}"
        )

    # Proximity takeover: standing right next to p3 (5m) while p1 is 55m away re-locks to p3
    g_take = ctrl.compute_guidance(-85.0, 0.0, 0.0, player_map_id=2)
    assert g_take.target is not None and g_take.target.id == "p3", (
        f"Expected proximity takeover to p3, got {g_take.target.id if g_take.target else None}"
    )

    # Explicit refresh_nearest() switches to p3 (now 5m away -> arrived!)
    refreshed = ctrl.refresh_nearest()
    assert refreshed is not None and refreshed.id == "p3"
    g_ref = ctrl.compute_guidance(-85.0, 0.0, 0.0, player_map_id=2)
    assert g_ref.target is not None and g_ref.target.id == "p3"
    assert g_ref.state == "arrived" and g_ref.distance_m is not None and g_ref.distance_m <= 12.0

    # Next / Prev cycling (order from (-85, 0) is p3, p2, p1)
    nxt = ctrl.next_target()
    assert nxt is not None and nxt.id == "p2"
    nxt2 = ctrl.next_target()
    assert nxt2 is not None and nxt2.id == "p1"
    prv = ctrl.prev_target()
    assert prv is not None and prv.id == "p2"

    # Skip (session-only), Blacklist, Mark Collected -> full Undo stack reversal
    after_skip = ctrl.skip_current()  # skips p2 -> moves to p1
    assert "p2" in ctrl.skipped_ids and "p2" not in ctrl.collected_ids
    assert after_skip is not None and after_skip.id == "p1"

    after_bl = ctrl.blacklist_current()  # blacklists p1 -> moves to p3
    assert "p1" in ctrl.blacklisted_ids
    assert after_bl is not None and after_bl.id == "p3"

    after_mark = ctrl.mark_collected()  # marks p3 -> no targets left
    assert "p3" in ctrl.collected_ids
    assert after_mark is None
    g_idle = ctrl.compute_guidance(-85.0, 0.0, 0.0, player_map_id=2)
    assert g_idle.state == "idle" and g_idle.target is None

    # Undo 1: restores p3 (collected)
    u1 = ctrl.undo()
    assert u1 is not None and u1.id == "p3" and "p3" not in ctrl.collected_ids
    # Undo 2: restores p1 (blacklisted)
    u2 = ctrl.undo()
    assert u2 is not None and u2.id == "p1" and "p1" not in ctrl.blacklisted_ids
    # Undo 3: restores p2 (skipped)
    u3 = ctrl.undo()
    assert u3 is not None and u3.id == "p2" and "p2" not in ctrl.skipped_ids

    # Tracking lost state
    g_lost = ctrl.compute_guidance(-85.0, 0.0, 0.0, player_map_id=2, tracking_state="LOST")
    assert g_lost.state == "lost" and g_lost.show_spot_card is False

    print("  [OK] Sticky Target stability, refresh_nearest, next/prev/skip/blacklist/mark & full undo passed")


def verify_tsp_route_queue() -> None:
    p1 = NavPin(id="r1", name="Route Stop 1", lat=-100.0, lng=0.0, map_id=2)
    p2 = NavPin(id="r2", name="Route Stop 2", lat=-200.0, lng=0.0, map_id=2)
    p3 = NavPin(id="r3", name="Route Stop 3", lat=-5.0, lng=0.0, map_id=2)  # Closest, but last in TSP queue
    ctrl = NavigationController([p1, p2, p3])
    ctrl.set_route_queue([p1, p2, p3])

    # Must follow TSP order r1 -> r2 -> r3 despite r3 being closest
    g1 = ctrl.compute_guidance(0.0, 0.0, 0.0, player_map_id=2)
    assert g1.target is not None and g1.target.id == "r1"
    assert g1.route_step == 1 and g1.route_total == 3

    # Marking r1 auto-advances to r2
    nxt = ctrl.mark_collected()
    assert nxt is not None and nxt.id == "r2"
    g2 = ctrl.compute_guidance(0.0, 0.0, 0.0, player_map_id=2)
    assert g2.target is not None and g2.target.id == "r2"
    assert g2.route_step == 2 and g2.route_total == 3

    # Marking r2 auto-advances to r3
    nxt2 = ctrl.mark_collected()
    assert nxt2 is not None and nxt2.id == "r3"
    assert ctrl.route_progress == (3, 3)

    # Undo restores r2 and step 2
    restored = ctrl.undo()
    assert restored is not None and restored.id == "r2"
    assert ctrl.route_progress == (2, 3)

    print("  [OK] TSP Route Queue auto-advance and undo step restoration passed")


def verify_floor_mismatch_and_spot_card() -> None:
    # Target is underground just 5m horizontally North of player: arrow still points straight at pin (direct)
    # with clear floor-mismatch warning instead of redirecting to waypoints / cave entrances.
    ug_pin = NavPin(
        id="ug1",
        name="Rương Ngầm Hang Động",
        lat=-5.0,
        lng=0.0,
        map_id=2,
        floor_id="F_B1",
        floor_name="B1",
        is_underground=True,
        image_url="https://example.invalid/spot1.jpg",
        description="Đi qua cửa hang phía Đông rồi rẽ trái sau tảng đá lớn.",
        cave_entrance={"lat": 0.0, "lng": 40.0, "name": "Cửa hang phía Đông"},
    )
    ctrl = NavigationController([ug_pin])

    # Player is on surface -> floor_mismatch (NOT arrived even though horizontal dist == 5m!)
    g_mis = ctrl.compute_guidance(
        player_lat=0.0,
        player_lng=0.0,
        view_heading_deg=0.0,
        player_map_id=2,
        player_floor_id=None,
        player_is_underground=False,
    )
    assert g_mis.state == "floor_mismatch", f"Expected floor_mismatch, got {g_mis.state}"
    assert g_mis.floor_mismatch is True
    assert g_mis.route_mode == "direct"
    assert g_mis.route_target_name == ""
    assert g_mis.bearing_deg is not None and abs(g_mis.bearing_deg - 0.0) < 1e-6, (
        f"Expected direct North (0°) arrow to target pin, got {g_mis.bearing_deg}°"
    )
    assert g_mis.delta_deg is not None and abs(g_mis.delta_deg - 0.0) < 1e-6
    assert g_mis.distance_m is not None and abs(g_mis.distance_m - 5.0) < 1e-6
    assert g_mis.target_distance_m is not None and abs(g_mis.target_distance_m - 5.0) < 1e-6

    # Passing waypoints no longer redirects the arrow away from the pin
    ug_no_cave = NavPin(
        id="ug2",
        name="Underground Chest B2",
        lat=-50.0,
        lng=0.0,
        map_id=2,
        floor_id="F_B2",
        floor_name="B2",
        is_underground=True,
    )
    waypoints = [
        {"lat": 0.0, "lng": -25.0, "map_id": 2, "is_underground": False, "floor_id": None, "name": "Surface WP West"},
    ]
    ctrl_wp = NavigationController([ug_no_cave], waypoints=waypoints)
    g_wp = ctrl_wp.compute_guidance(
        0.0, 0.0, 0.0, player_map_id=2, player_floor_id=None, player_is_underground=False, waypoints=waypoints
    )
    assert g_wp.state == "floor_mismatch"
    assert g_wp.route_mode == "direct"
    assert g_wp.route_target_name == ""
    assert g_wp.bearing_deg is not None and abs(g_wp.bearing_deg - 0.0) < 1e-6
    assert g_wp.distance_m is not None and abs(g_wp.distance_m - 50.0) < 1e-6

    # Both underground but different floor_id ("F_B1" vs "F_B2") -> floor_mismatch
    g_sub = ctrl_wp.compute_guidance(
        0.0, 0.0, 0.0, player_map_id=2, player_floor_id="F_B1", player_is_underground=True, player_floor_name="B1"
    )
    assert g_sub.state == "floor_mismatch" and g_sub.floor_mismatch is True
    assert g_sub.player_floor_name == "B1"

    # Fix 1 & 2: Human-readable floor labels (never raw numeric IDs) + directional floor badges (↑/↓/↕)
    g_num_ug_to_sf = NavGuidance(
        state="floor_mismatch",
        target=NavPin(id="sf_top", name="Rương Mặt Đất", lat=-15.0, lng=0.0, map_id=2, is_underground=False),
        distance_m=15.0,
        target_distance_m=15.0,
        bearing_deg=0.0,
        delta_deg=0.0,
        floor_mismatch=True,
        player_floor_id="184",
        player_floor_name="B1",
        player_is_underground=True,
        target_floor_id=None,
        target_floor_name="",
        target_is_underground=False,
    )
    _, sub_vi, tag_vi, pal_vi, exp_vi, _, _ = NavHudRenderer._resolve_texts_and_mode(
        g_num_ug_to_sf, lang="vi", hotkey_label="Alt+K", guide_hotkey_label="Alt+H"
    )
    assert pal_vi == "warning" and exp_vi is True
    assert tag_vi == "↑ Mặt đất", f"Expected '↑ Mặt đất', got {tag_vi!r}"
    assert sub_vi == "Khác tầng! (Ngầm (B1) → Mặt đất (G)) • Alt+H hướng dẫn", f"Unexpected sub_vi: {sub_vi!r}"

    _, sub_en, tag_en, _, _, _, _ = NavHudRenderer._resolve_texts_and_mode(
        g_num_ug_to_sf, lang="en", hotkey_label="Alt+K", guide_hotkey_label="Alt+H"
    )
    assert tag_en == "↑ Surface", f"Expected '↑ Surface', got {tag_en!r}"
    assert sub_en == "Floor mismatch! (Underground (B1) → Surface (G)) • Alt+H guide", f"Unexpected sub_en: {sub_en!r}"

    # Numeric floor_id without floor_name NEVER leaks raw digits like "184"
    g_raw_id_only = NavGuidance(
        state="floor_mismatch",
        target=NavPin(id="ug_num", name="Rương Ngầm", lat=-10.0, lng=0.0, map_id=2, floor_id="184", floor_name="", is_underground=True),
        distance_m=10.0,
        target_distance_m=10.0,
        bearing_deg=0.0,
        delta_deg=0.0,
        floor_mismatch=True,
        player_floor_id="999",
        player_floor_name="",
        player_is_underground=False,
        target_floor_id="184",
        target_floor_name="",
        target_is_underground=True,
    )
    _, sub_nonum_vi, tag_nonum_vi, _, _, _, _ = NavHudRenderer._resolve_texts_and_mode(
        g_raw_id_only, lang="vi", hotkey_label="Alt+K"
    )
    assert "184" not in sub_nonum_vi and "184" not in tag_nonum_vi
    assert tag_nonum_vi == "↓ Ngầm"
    assert "Mặt đất (G) → Ngầm" in sub_nonum_vi

    _, sub_nonum_en, tag_nonum_en, _, _, _, _ = NavHudRenderer._resolve_texts_and_mode(
        g_raw_id_only, lang="en", hotkey_label="Alt+K"
    )
    assert "184" not in sub_nonum_en and tag_nonum_en == "↓ UG"
    assert "Surface (G) → Underground" in sub_nonum_en

    # Directional tag checks: surface->B1, B1->B2, B2->B1
    assert NavHudRenderer._format_floor_tag(g_mis, "vi") == "↓ B1"
    assert NavHudRenderer._format_floor_tag(g_sub, "vi") == "↓ B2"
    g_b2_to_b1 = NavGuidance(
        state="floor_mismatch",
        target=NavPin(id="b1", name="P", lat=-5.0, lng=0.0, map_id=2, floor_id="184", floor_name="B1", is_underground=True),
        distance_m=5.0,
        target_distance_m=5.0,
        bearing_deg=0.0,
        delta_deg=0.0,
        floor_mismatch=True,
        player_floor_id="185",
        player_floor_name="B2",
        player_is_underground=True,
        target_floor_id="184",
        target_floor_name="B1",
        target_is_underground=True,
    )
    assert NavHudRenderer._format_floor_tag(g_b2_to_b1, "vi") == "↑ B1"

    # Fix 3: MapLocator.resolve_floor_label & unique-overlay inference + _normalize_raw_nav_pin wiring
    loc = MapLocator.__new__(MapLocator)
    loc._floor_label_index = {}
    loc.map_point_groups = {
        2: [
            {
                "id": 10,
                "floors": [
                    {
                        "id": 184,
                        "floor_short": "B1",
                        "floor_name": "Tầng B1",
                        "overlay": {"minLng": 10.0, "maxLng": 30.0, "minLat": -20.0, "maxLat": 0.0},
                    },
                    {
                        "id": 185,
                        "floor_short": "B2",
                        "floor_name": "Tầng B2",
                        "overlay": {"minLng": 40.0, "maxLng": 70.0, "minLat": 40.0, "maxLat": 70.0},
                    },
                    {
                        "id": 186,
                        "floor_short": "B3",
                        "floor_name": "Tầng B3",
                        "overlay": {"minLng": 45.0, "maxLng": 75.0, "minLat": 45.0, "maxLat": 75.0},
                    },
                ],
            }
        ]
    }
    assert loc.resolve_floor_label(2, "184") == "B1"
    assert loc.resolve_floor_label(2, 185) == "B2"
    assert loc.resolve_floor_label(2, "999999") == ""
    assert loc.infer_unique_underground_floor(2, -10.0, 20.0) == ("184", "B1")
    assert loc.infer_unique_underground_floor(2, 55.0, 60.0) is None  # Ambiguous overlapping B2 & B3
    norm_unique = _normalize_raw_nav_pin({"id": "nu1", "lat": -10.0, "lng": 20.0, "is_underground": True}, locator=loc)
    assert norm_unique is not None and norm_unique.floor_id == "184" and norm_unique.floor_name == "B1"
    norm_ambig = _normalize_raw_nav_pin({"id": "na1", "lat": 55.0, "lng": 60.0, "is_underground": True}, locator=loc)
    assert norm_ambig is not None and norm_ambig.floor_id is None and norm_ambig.is_underground is True

    # Matching floor + Spot Card distance threshold (<= 20m with content)
    spot_pin = NavPin(
        id="sp1",
        name="Luxurious Chest",
        lat=-18.0,
        lng=0.0,
        map_id=2,
        description="Hidden behind the waterfall",
    )
    bare_pin = NavPin(id="bare1", name="Bare Chest", lat=-10.0, lng=0.0, map_id=2)
    ctrl_sp = NavigationController([spot_pin])
    assert ctrl_sp.compute_guidance(0.0, 0.0, 0.0, 2).show_spot_card is True   # 18m <= 20m
    assert ctrl_sp.compute_guidance(5.0, 0.0, 0.0, 2).show_spot_card is False  # 23m > 20m
    ctrl_bare = NavigationController([bare_pin])
    assert ctrl_bare.compute_guidance(0.0, 0.0, 0.0, 2).show_spot_card is False  # no image/desc

    # Fix 4: Scoping by (map_id, floor_id), unified same-floor match for untagged underground pins, & cross-floor cycling
    sf_pin = NavPin(id="sf1", name="Surface Pin", lat=-40.0, lng=0.0, map_id=2, is_underground=False)
    b1_pin = NavPin(id="b1_1", name="B1 Pin", lat=-15.0, lng=0.0, map_id=2, floor_id="F_B1", floor_name="B1", is_underground=True)
    ug_untagged = NavPin(id="ug_nt", name="Untagged UG", lat=-12.0, lng=0.0, map_id=2, floor_id=None, is_underground=True)
    other_map = NavPin(id="m9", name="Enkanomiya Pin", lat=-2.0, lng=0.0, map_id=9, is_underground=False)
    ctrl_scope = NavigationController([sf_pin, b1_pin, other_map])
    g_sf = ctrl_scope.compute_guidance(0.0, 0.0, 0.0, player_map_id=2, player_floor_id=None, player_is_underground=False)
    assert g_sf.target is not None and g_sf.target.id == "sf1" and g_sf.floor_mismatch is False

    # next_target() cycles from surface pin onto cross-floor B1 pin and back cleanly
    nxt_cross = ctrl_scope.next_target()
    assert nxt_cross is not None and nxt_cross.id == "b1_1"
    g_cross = ctrl_scope.compute_guidance(0.0, 0.0, 0.0, player_map_id=2, player_floor_id=None, player_is_underground=False)
    assert g_cross.target is not None and g_cross.target.id == "b1_1"
    assert g_cross.state == "floor_mismatch" and g_cross.floor_mismatch is True
    nxt_back = ctrl_scope.next_target()
    assert nxt_back is not None and nxt_back.id == "sf1"

    g_b1 = ctrl_scope.compute_guidance(0.0, 0.0, 0.0, player_map_id=2, player_floor_id="F_B1", player_is_underground=True, player_floor_name="B1")
    assert g_b1.target is not None and g_b1.target.id == "b1_1" and g_b1.floor_mismatch is False
    g_m9 = ctrl_scope.compute_guidance(0.0, 0.0, 0.0, player_map_id=9, player_floor_id=None, player_is_underground=False)
    assert g_m9.target is not None and g_m9.target.id == "m9"

    # Underground player with floor_id="184" treats untagged underground pin (floor_id=None) as same-floor
    ctrl_untagged = NavigationController([sf_pin, ug_untagged])
    g_ut = ctrl_untagged.compute_guidance(0.0, 0.0, 0.0, player_map_id=2, player_floor_id="184", player_is_underground=True, player_floor_name="B1")
    assert g_ut.target is not None and g_ut.target.id == "ug_nt" and g_ut.floor_mismatch is False

    print("  [OK] Floor-mismatch direct warning, directional tags, unique-floor inference & cross-floor cycling passed")


def verify_collected_hold_controller() -> None:
    hold = CollectedHoldController(duration_seconds=1.0)
    # Rejects unavailable or None target
    assert hold.begin(None, available=True, now=10.0) is False
    assert hold.begin("p1", available=False, now=10.0) is False

    # Normal 1.0s hold progression -> confirmed
    assert hold.begin("p1", available=True, now=10.0) is True
    s1 = hold.update(key_down=True, target_id="p1", available=True, now=10.4)
    assert s1.active is True and s1.confirmed is False and s1.cancelled is False and abs(s1.progress - 0.4) < 1e-6
    s2 = hold.update(key_down=True, target_id="p1", available=True, now=11.05)
    assert s2.confirmed is True and s2.active is False and s2.progress == 1.0
    assert hold.is_active is False

    # Cancel on key release
    assert hold.begin("p1", available=True, now=20.0) is True
    sr = hold.update(key_down=False, target_id="p1", available=True, now=20.5)
    assert sr.cancelled is True and sr.confirmed is False and hold.is_active is False

    # Cancel on target switch
    assert hold.begin("p1", available=True, now=30.0) is True
    ss = hold.update(key_down=True, target_id="p2", available=True, now=30.5)
    assert ss.cancelled is True and ss.confirmed is False

    # Cancel on lost tracking
    assert hold.begin("p1", available=True, now=40.0) is True
    sl = hold.update(key_down=True, target_id="p1", available=False, now=40.5)
    assert sl.cancelled is True and sl.confirmed is False

    print("  [OK] CollectedHoldController 1.0s hold & release/switch/lost-cancel passed")


def verify_circular_smoother() -> None:
    sm = CircularAngleSmoother(alpha=0.35, deadband_deg=1.5)
    # Initial lock
    v0 = sm.update(178.0)
    assert v0 is not None and abs(v0 - 178.0) < 1e-6

    # Wrap-safe across +178° -> -178° (must cross ±180°, never swing near 0°)
    for _ in range(15):
        v = sm.update(-178.0)
        assert v is not None and abs(v) > 170.0, f"Smoother wrapped the wrong way: {v}°"
    assert abs(((sm.value - (-178.0) + 180.0) % 360.0) - 180.0) < 1.6

    # Deadband check (< 1.5° ignored)
    sm.reset()
    sm.update(10.0)
    held = sm.update(11.1)
    assert held == 10.0, f"Expected deadband hold at 10.0°, got {held}°"
    moved = sm.update(20.0)
    assert moved is not None and 10.0 < moved < 20.0

    print("  [OK] CircularAngleSmoother wrap-safe ±180°/0° & 1.5° deadband passed")


def _assert_alpha_integrity(img: Image.Image, badge_rect: Tuple[int, int, int, int], label: str) -> None:
    arr = np.asarray(img)
    assert img.mode == "RGBA"
    bx, by, bw, bh = badge_rect
    cx = bx + bw // 2
    cy = by + bh // 2

    # 1. Center of compass badge MUST have alpha >= 220
    center_alpha = int(arr[cy, cx, 3])
    assert center_alpha >= 220, f"[{label}] Badge center alpha={center_alpha} < 220 (hole detected!)"

    # 2. Inner disc of compass badge (radius 0.35 * bw) must have ZERO sky hole-punching
    yy, xx = np.ogrid[: arr.shape[0], : arr.shape[1]]
    dist_sq = (xx - cx) ** 2 + (yy - cy) ** 2
    inner_mask = dist_sq <= (bw * 0.35) ** 2
    min_badge_alpha = int(arr[inner_mask, 3].min())
    assert min_badge_alpha >= 220, f"[{label}] Badge inner disc min alpha={min_badge_alpha} < 220"

    # 3. Glass card interior strip (mid-height across text region) must stay >= 220 alpha
    strip_x0 = min(img.width - 12, bx + bw + 10)
    strip_x1 = max(strip_x0 + 4, img.width - 14)
    strip_y = cy
    min_card_alpha = int(arr[strip_y, strip_x0:strip_x1, 3].min())
    assert min_card_alpha >= 220, f"[{label}] Glass card interior min alpha={min_card_alpha} < 220"

    # 4. Premultiplied BGRA invariant
    raw = rgba_to_premultiplied_bgra(img)
    assert len(raw) == img.width * img.height * 4
    bgra = np.frombuffer(raw, dtype=np.uint8).reshape((img.height, img.width, 4))
    assert np.all(bgra[:, :, 0] <= bgra[:, :, 3]), f"[{label}] Premultiplied B > A"
    assert np.all(bgra[:, :, 1] <= bgra[:, :, 3]), f"[{label}] Premultiplied G > A"
    assert np.all(bgra[:, :, 2] <= bgra[:, :, 3]), f"[{label}] Premultiplied R > A"


def verify_renderer_benchmark_and_visuals() -> float:
    renderer = NavHudRenderer()
    pin_track = NavPin(
        id="t1",
        name="Rương Hiếm Đồng Bằng Phong Khởi",
        lat=-28.0,
        lng=0.0,
        map_id=2,
        image_url="https://example.invalid/chest.png",
        description="Ẩn sau bụi cây ngay bên cạnh bức tượng cũ trên mỏm đá hướng Bắc.",
    )
    ctrl = NavigationController([pin_track])
    g_compact = ctrl.compute_guidance(0.0, 0.0, 45.0, player_map_id=2)
    assert g_compact.state == "tracking" and g_compact.distance_m == 28.0

    # Warm up all 90 compass angles (4° steps) in tracking mode (covers lock_on, info, behind!)
    for deg in range(0, 360, 4):
        rel = ((deg + 180) % 360) - 180
        renderer.render(g_compact, delta_deg=float(rel), lang="vi", scale=1.0)

    # Verify badge cache memory strictly < 0.5 MB
    assert renderer.badge_cache_bytes < 512 * 1024, (
        f"Badge cache exceeded 0.5 MB: {renderer.badge_cache_bytes} bytes"
    )

    # Benchmark 500 steady-state compass rotation frames
    n_frames = 500
    t0 = time.perf_counter()
    for i in range(n_frames):
        rel = (((i * 4) + 180) % 360) - 180
        frame = renderer.render(g_compact, delta_deg=float(rel), lang="vi", scale=1.0)
        assert renderer.last_render_meta["card_cache_hit"] is True
        assert renderer.last_render_meta["badge_cache_hit"] is True
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    avg_ms = elapsed_ms / n_frames
    assert avg_ms < 1.0, f"Steady-state compass render too slow: {avg_ms:.3f} ms/frame (limit < 1.0 ms)"

    # Compact vs Expanded heights + Alpha Integrity across all states and both languages
    compact_vi = renderer.render(g_compact, delta_deg=40.0, lang="vi", scale=1.0)
    assert renderer.last_render_meta["expanded"] is False
    _assert_alpha_integrity(compact_vi, renderer.last_render_meta["badge_rect"], "compact_vi")

    compact_en = renderer.render(g_compact, delta_deg=40.0, lang="en", scale=1.0)
    assert renderer.last_render_meta["expanded"] is False
    _assert_alpha_integrity(compact_en, renderer.last_render_meta["badge_rect"], "compact_en")

    # Lock-on cue (|delta| <= 12°) & Behind cue (|delta| > 135°)
    lock_img = renderer.render(g_compact, delta_deg=4.0, lang="vi", scale=1.0)
    assert renderer.last_render_meta["lock_on"] is True
    assert renderer.last_render_meta["palette"] == "lock_on"
    _assert_alpha_integrity(lock_img, renderer.last_render_meta["badge_rect"], "lock_on")

    behind_img = renderer.render(g_compact, delta_deg=165.0, lang="vi", scale=1.0)
    assert renderer.last_render_meta["behind"] is True
    _assert_alpha_integrity(behind_img, renderer.last_render_meta["badge_rect"], "behind")
    # Verify amber pixel exists in badge ring for behind cue
    bx, by, bw, bh = renderer.last_render_meta["badge_rect"]
    badge_crop = np.asarray(behind_img)[by : by + bh, bx : bx + bw]
    amber_pixels = (badge_crop[:, :, 0] > 180) & (badge_crop[:, :, 1] > 100) & (badge_crop[:, :, 2] < 80)
    assert int(amber_pixels.sum()) >= 12, "Expected visible amber ring cue for |delta_deg| > 135°"

    # Expanded states: Arrived (<= 12m), Floor Mismatch, Lost
    g_arrived = ctrl.compute_guidance(-20.0, 0.0, 0.0, player_map_id=2)  # 8m -> arrived, show_spot_card=True
    assert g_arrived.state == "arrived" and g_arrived.show_spot_card is True

    # Test Arrived without spot content first to compare pure pill Compact vs Expanded height
    g_arrived_no_spot = NavGuidance(
        state="arrived",
        target=NavPin(id="ns", name="Rương Hiếm", lat=-8.0, lng=0.0, map_id=2),
        distance_m=8.0,
        target_distance_m=8.0,
        bearing_deg=0.0,
        delta_deg=0.0,
        floor_mismatch=False,
        show_spot_card=False,
    )
    for lang in ("vi", "en"):
        exp_arr = renderer.render(g_arrived_no_spot, lang=lang, scale=1.0)
        assert renderer.last_render_meta["expanded"] is True
        assert exp_arr.height > compact_vi.height, (
            f"Expanded arrived height {exp_arr.height} <= compact height {compact_vi.height}"
        )
        _assert_alpha_integrity(exp_arr, renderer.last_render_meta["badge_rect"], f"arrived_{lang}")

    g_mismatch = NavGuidance(
        state="floor_mismatch",
        target=NavPin(id="fm", name="Rương Hang Động", lat=-5.0, lng=0.0, map_id=2, floor_id="B1", floor_name="B1", is_underground=True),
        distance_m=5.0,
        target_distance_m=5.0,
        bearing_deg=0.0,
        delta_deg=0.0,
        floor_mismatch=True,
        player_is_underground=False,
        target_floor_id="B1",
        target_floor_name="B1",
        target_is_underground=True,
        route_mode="direct",
        route_target_name="",
        show_spot_card=False,
    )
    for lang in ("vi", "en"):
        exp_fm = renderer.render(g_mismatch, lang=lang, scale=1.0)
        assert renderer.last_render_meta["expanded"] is True
        assert exp_fm.height > compact_vi.height
        _assert_alpha_integrity(exp_fm, renderer.last_render_meta["badge_rect"], f"mismatch_{lang}")

    g_lost = ctrl.compute_guidance(0.0, 0.0, 0.0, player_map_id=2, tracking_state="LOST")
    for lang in ("vi", "en"):
        exp_lost = renderer.render(g_lost, lang=lang, scale=1.0)
        assert renderer.last_render_meta["expanded"] is True
        assert exp_lost.height > compact_vi.height
        _assert_alpha_integrity(exp_lost, renderer.last_render_meta["badge_rect"], f"lost_{lang}")

    # Proximity Spot Card + LRU thumbnail cache (max 30)
    sample_thumb = Image.new("RGB", (160, 120), (68, 130, 196))
    buf = io.BytesIO()
    sample_thumb.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    for idx in range(35):
        renderer.put_thumbnail(f"https://example.invalid/thumb_{idx}.png", png_bytes)
    assert renderer.thumb_cache_count == 30, f"Expected LRU cap 30, got {renderer.thumb_cache_count}"

    renderer.put_thumbnail(pin_track.image_url, png_bytes)
    spot_img_vi = renderer.render(g_arrived, delta_deg=10.0, lang="vi", scale=1.0)
    assert renderer.last_render_meta["has_spot_card"] is True
    assert spot_img_vi.height > exp_arr.height + 40, (
        f"Spot card image height {spot_img_vi.height} did not expand below pill ({exp_arr.height})"
    )
    _assert_alpha_integrity(spot_img_vi, renderer.last_render_meta["badge_rect"], "spot_card_vi")

    spot_img_en = renderer.render(g_arrived, delta_deg=10.0, lang="en", scale=1.0)
    assert renderer.last_render_meta["has_spot_card"] is True
    _assert_alpha_integrity(spot_img_en, renderer.last_render_meta["badge_rect"], "spot_card_en")

    # Verify Spot Card lower panel interior also has alpha >= 220
    spot_arr = np.asarray(spot_img_vi)
    spot_mid_y = spot_img_vi.height - 24
    assert int(spot_arr[spot_mid_y, 24 : spot_img_vi.width - 24, 3].min()) >= 220

    # Verify VI and EN renders actually produce distinct localized glyphs
    assert not np.array_equal(np.asarray(spot_img_vi), np.asarray(spot_img_en)), (
        "Expected VI and EN renders to differ!"
    )

    # Verify strict < 0.5 MB RAM ceiling even when sweeping all 4 palettes across all 90 angles
    for pal_name in ("info", "success", "warning", "lock_on"):
        for deg in range(0, 360, 4):
            renderer.get_compass_badge(pal_name, float(deg), scale=1.0, directional=True, lock_on=(pal_name == "lock_on"))
    assert renderer.badge_cache_bytes <= NavHudRenderer.MAX_BADGE_CACHE_BYTES < 512 * 1024, (
        f"Badge LRU exceeded 0.5 MB cap: {renderer.badge_cache_bytes} bytes"
    )

    print(
        f"  [OK] NavHudRenderer steady-state={avg_ms:.3f} ms/frame (<1.0ms), "
        f"badge_cache={renderer.badge_cache_bytes / 1024:.1f} KB (<512 KB), "
        f"Compact={compact_vi.size}, Expanded={exp_arr.size}, SpotCard={spot_img_vi.size}, "
        f"alpha>=220 & vi/en passed"
    )
    return avg_ms


def verify_hud_positions_and_win32_lifecycle() -> None:
    expected_ex = (
        NavMiniHud.WS_EX_LAYERED
        | NavMiniHud.WS_EX_TRANSPARENT
        | NavMiniHud.WS_EX_TOPMOST
        | NavMiniHud.WS_EX_NOACTIVATE
        | NavMiniHud.WS_EX_TOOLWINDOW
    )
    assert NavMiniHud.EX_STYLE == expected_ex

    # Verify under_minimap placement across 1080p, 1440p, 21:9 ultrawide, and Gamepad mode
    resolutions = [
        ("1920x1080_desktop", (0, 0, 1920, 1080), (0, 0, 1920, 1040), False),
        ("2560x1440_desktop", (0, 0, 2560, 1440), (0, 0, 2560, 1400), False),
        ("3440x1440_ultrawide", (0, 0, 3440, 1440), (0, 0, 3440, 1400), False),
        ("1920x1080_gamepad", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True),
    ]
    hud_w, hud_h = 260, 64
    gp_x_desktop = None
    gp_x_gamepad = None

    for label, win_rect, work_area, gp_mode in resolutions:
        wx, wy, ww, wh = win_rect
        wa_l, wa_t, wa_r, wa_b = work_area
        layout = compute_ui_layout(ww, wh, gamepad_mode=gp_mode)
        mm_x, mm_y, mm_w, mm_h = layout.minimap_rect

        px, py = NavMiniHud.compute_hud_position(
            hud_w,
            hud_h,
            window_rect=win_rect,
            position_mode="under_minimap",
            gamepad_mode=gp_mode,
            work_area=work_area,
        )
        # Strictly inside work_area
        assert px >= wa_l + 8 and (px + hud_w) <= wa_r - 8, f"[{label}] X out of work_area: {px}"
        assert py >= wa_t + 8 and (py + hud_h) <= wa_b - 8, f"[{label}] Y out of work_area: {py}"
        # Directly below minimap (no overlap with minimap!)
        assert py >= wy + mm_y + mm_h, f"[{label}] HUD y={py} overlaps minimap bottom {wy + mm_y + mm_h}"
        assert py <= wy + mm_y + mm_h + int(round(24 * layout.ui_scale)), f"[{label}] HUD too far below minimap"
        # Strictly clear of top-center Boss HP bar (center 35%..65% of screen width)
        assert (px + hud_w) < (wx + ww * 0.35), f"[{label}] HUD intrudes toward top-center Boss HP bar"

        if label == "1920x1080_desktop":
            gp_x_desktop = px
        elif label == "1920x1080_gamepad":
            gp_x_gamepad = px

    # Gamepad minimap is shifted right relative to desktop minimap -> HUD follows it right
    assert gp_x_desktop is not None and gp_x_gamepad is not None and gp_x_gamepad > gp_x_desktop

    # Verify top_center mode & tight work_area clamping
    tc_x, tc_y = NavMiniHud.compute_hud_position(
        hud_w,
        hud_h,
        window_rect=(0, 0, 1920, 1080),
        position_mode="top_center",
        work_area=(0, 0, 1920, 1040),
    )
    assert abs((tc_x + hud_w / 2.0) - 960.0) <= 1.5
    assert tc_y >= 18

    clamped_x, clamped_y = NavMiniHud.compute_hud_position(
        300,
        140,
        window_rect=(0, 0, 1920, 1080),
        position_mode="under_minimap",
        work_area=(50, 20, 380, 320),
    )
    assert 58 <= clamped_x <= 380 - 300 - 8
    assert 28 <= clamped_y <= 320 - 140 - 8

    # Live Win32 layered window open / update / resize / hide / close lifecycle + zero handle leak
    NavMiniHud.SCREEN_OFFSET = (-20000, -20000)  # real window, never visible on the desktop
    hud = NavMiniHud(position_mode="under_minimap", lang="vi")
    assert hud._thread is None, "NavMiniHud must start its Win32 thread lazily"

    gdi_before, user_before = _get_gui_resource_counts()

    pin = NavPin(
        id="live1",
        name="Rương Hiếm Kiểm Tra",
        lat=-25.0,
        lng=0.0,
        map_id=2,
        image_url="https://example.invalid/live.png",
        description="Mô tả vị trí ngắn gọn để kiểm thử Proximity Spot Card.",
    )
    ctrl = NavigationController([pin])
    g_far = ctrl.compute_guidance(0.0, 0.0, 30.0, player_map_id=2)
    g_near = ctrl.compute_guidance(-18.0, 0.0, 0.0, player_map_id=2)

    # Open + Compact update + rotation update (DIBSection reuse) + Spot Card size change + Hide + Close
    assert hud.update(g_far, window_rect=(0, 0, 1920, 1080), work_area=(0, 0, 1920, 1040)) is True
    time.sleep(0.12)
    assert hud.hwnd is not None
    assert hud.wda_refresh_count >= 1, "Expected initial WDA_EXCLUDEFROMCAPTURE refresh"

    # Rotate angle -> same DIB dimensions
    g_rot = ctrl.compute_guidance(0.0, 0.0, 120.0, player_map_id=2)
    assert hud.update(g_rot, window_rect=(0, 0, 1920, 1080), work_area=(0, 0, 1920, 1040)) is True
    time.sleep(0.08)

    # Expand + Spot Card -> triggers DIBSection recreation cleanly
    hud.put_thumbnail(pin.image_url, Image.new("RGB", (96, 72), (80, 160, 220)))
    assert hud.update(g_near, window_rect=(0, 0, 1920, 1080), work_area=(0, 0, 1920, 1040)) is True
    time.sleep(0.08)

    # Hide then Close
    assert hud.hide() is True
    time.sleep(0.06)
    hud.close()
    assert hud.hwnd is None
    assert hud.is_visible is False
    assert hud._memdc is None and hud._hbmp is None

    # Windows releases per-thread USER objects (default IME window, queue) shortly after the UI
    # thread exits: re-measure until settled (a real leak never settles, so this stays strict).
    gdi_after, user_after = _get_gui_resource_counts()
    settle_deadline = time.time() + 1.0
    while (gdi_after > gdi_before or user_after > user_before) and time.time() < settle_deadline:
        time.sleep(0.05)
        gdi_after, user_after = _get_gui_resource_counts()
    gdi_delta = gdi_after - gdi_before
    user_delta = user_after - user_before
    assert gdi_delta <= 0, f"GDI handle leak detected: before={gdi_before}, after={gdi_after} (delta=+{gdi_delta})"
    assert user_delta <= 0, f"USER handle leak detected: before={user_before}, after={user_after} (delta=+{user_delta})"

    print(
        f"  [OK] NavMiniHud positioning (1080p/1440p/21:9/Gamepad/top_center/clamp) & "
        f"Win32 lifecycle (WDA={hud.wda_refresh_count}, GDI delta={gdi_delta}, USER delta={user_delta}) passed"
    )


def main() -> None:
    print("Running Genshin-Helper v5 Track 3A verification suite...")
    verify_cardinal_bearings_and_deltas()
    verify_sticky_target_commands_and_undo()
    verify_tsp_route_queue()
    verify_floor_mismatch_and_spot_card()
    verify_collected_hold_controller()
    verify_circular_smoother()
    verify_renderer_benchmark_and_visuals()
    verify_hud_positions_and_win32_lifecycle()
    print("ALL TRACK 3A CHECKS PASSED (100%)")


if __name__ == "__main__":
    main()
