import argparse
import asyncio
import ctypes
from dataclasses import asdict, replace
import inspect
import logging
import math
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Optional, Dict, Any, List, Sequence, Tuple
from src.config import config, CONFIG_FILE_PATH
from src.capture import WindowFinder, ScreenCapturer
from src.locator import MapLocator
from src.map_state import MapScreenStateMachine
from src.paths import get_diagnostics_dir
from src.server import SyncServer
from src.hotkey import HotkeyManager
from src.diagnostics import DiagnosticManager
from src.single_monitor import AudioFeedback, HudToastOverlay, WindowSwitcher
from src.tray import SystemTrayApp
from src.i18n import get_language, set_language, t
from src.game_settings import check_uipi_status, compute_ui_layout, launch_genshin_borderless
from src.tile_cache import LruTerrainTileCache
from src.hd_assets import (
    HoyolabHdTileProvider,
    UndergroundOverlayProvider,
    default_paimon_cache_path,
    load_multimap_db,
)
from src.global_relocator import TeyvatGlobalRelocator
from src.minimap_tracker import HybridMinimapTracker, MinimapTrackerConfig, STATE_TRACKING
from src.nav_controller import NavPin, NavigationController
from src.nav_hud import NavMiniHud
from src.tsp_route import load_waypoints_for_map, plan_route
from src.lan_companion import LanCompanionServer
from src.map_bridge import (
    AppsampleAdapter,
    AxisAffine,
    CanonicalJsonAdapter,
    CanonicalPin,
    GenshinMapGithubAdapter,
    KongyingAdapter,
    PerMapAffineRegistry,
    TargetPin,
    fit_axis_affine,
    match_pins_one_to_one,
    parse_hoyolab_export,
    parse_hoyolab_points,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("GenshinHelper.Main")

MUTEX_NAME = "Local\\GenshinHelper_SingleInstance_v4"

# Global state to share between tracking loop, hotkey triggers, and status telemetry
current_game_state: Dict[str, Any] = {
    "hwnd": None,
    "rect": None,
    "last_pos": None,
    "uipi_status": None,
}

# Thread-safe monotonic clock guard for HybridMinimapTracker across tracking_loop and hotkeys
_tracker_lock = threading.RLock()
_tracker_last_ts: float = 0.0


def _next_tracker_ts() -> float:
    global _tracker_last_ts
    now = time.monotonic()
    if now < _tracker_last_ts:
        now = _tracker_last_ts
    _tracker_last_ts = now
    return now


def _safe_int_or_none(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _angle_diff_deg(a: Optional[float], b: Optional[float]) -> float:
    if a is None and b is None:
        return 0.0
    if a is None or b is None:
        return 180.0
    return abs(((float(a) - float(b) + 180.0) % 360.0) - 180.0)


def _normalize_raw_nav_pin(raw: Any, default_map_id: int = 2) -> Optional[NavPin]:
    if isinstance(raw, NavPin):
        return raw
    if not isinstance(raw, dict):
        return None
    lat_raw = raw.get("lat", raw.get("y_pos"))
    lng_raw = raw.get("lng", raw.get("x_pos"))
    if lat_raw is None or lng_raw is None:
        return None
    try:
        lat_val = float(lat_raw)
        lng_val = float(lng_raw)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_val) and math.isfinite(lng_val)):
        return None

    pin_id = raw.get("id", raw.get("point_id"))
    if pin_id is None or str(pin_id).strip() == "":
        pin_id = f"{lat_val:.2f},{lng_val:.2f}"

    floor_id = raw.get("floor_id", raw.get("floorId"))
    floor_name = str(raw.get("floor_name", raw.get("floorName", "")) or "").strip()
    if "is_underground" in raw and raw["is_underground"] is not None:
        is_ug = bool(raw["is_underground"])
    elif "isUnderground" in raw and raw["isUnderground"] is not None:
        is_ug = bool(raw["isUnderground"])
    else:
        is_ug = bool(
            (floor_id is not None and str(floor_id).strip() not in ("", "0"))
            or (floor_name and floor_name.upper() not in ("G", "SURFACE", "GROUND"))
        )

    map_id_raw = raw.get("map_id", raw.get("mapId", default_map_id))
    try:
        map_id = int(map_id_raw) if map_id_raw is not None else int(default_map_id)
    except (TypeError, ValueError):
        map_id = int(default_map_id)

    cave = raw.get("cave_entrance", raw.get("caveEntrance"))
    return NavPin(
        id=str(pin_id),
        name=str(raw.get("name") or f"Pin {pin_id}"),
        lat=lat_val,
        lng=lng_val,
        map_id=map_id,
        floor_id=str(floor_id).strip() if floor_id is not None and str(floor_id).strip() else None,
        floor_name=floor_name,
        is_underground=is_ug,
        category=str(raw.get("category", raw.get("label_id", raw.get("labelId", ""))) or ""),
        image_url=str(raw.get("image_url", raw.get("imageUrl", "")) or ""),
        description=str(raw.get("description", raw.get("content", "")) or ""),
        cave_entrance=dict(cave) if isinstance(cave, dict) else None,
    )


def _build_lan_state_payload(
    pos_info: Optional[Dict[str, Any]],
    nav_controller: Optional[NavigationController] = None,
    route_legs: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if isinstance(pos_info, dict):
        if pos_info.get("lat") is not None and pos_info.get("lng") is not None:
            payload["lat"] = float(pos_info["lat"])
            payload["lng"] = float(pos_info["lng"])
        if pos_info.get("map_id") is not None:
            payload["map_id"] = int(pos_info["map_id"])
        if pos_info.get("heading") is not None:
            payload["heading"] = float(pos_info["heading"])
        if pos_info.get("camera_heading") is not None:
            payload["camera_heading"] = float(pos_info["camera_heading"])
        payload["is_underground"] = bool(pos_info.get("is_underground", False))
        payload["floor_name"] = str(pos_info.get("floor_name") or ("B1" if payload["is_underground"] else "G"))

    if nav_controller is not None:
        cur_target = nav_controller.current_target
        payload["target_pin"] = cur_target.to_dict() if cur_target is not None else None
        excluded = nav_controller._excluded_ids()
        payload["nearby_pins"] = [
            p.to_dict() for p in nav_controller.pins if p.id not in excluded
        ][:50]
    else:
        payload["target_pin"] = None
        payload["nearby_pins"] = []

    payload["route_legs"] = list(route_legs) if route_legs else []
    return payload


def _pause_minimap_tracker(
    minimap_tracker: Optional[HybridMinimapTracker],
    reason: str = "paused",
) -> None:
    if minimap_tracker is None:
        return
    try:
        with _tracker_lock:
            minimap_tracker.pause(_next_tracker_ts(), reason=reason)
    except Exception as e:
        logger.debug(f"minimap_tracker.pause({reason}) error: {e}")


def _apply_map_m_anchor(
    res: Dict[str, Any],
    minimap_tracker: Optional[HybridMinimapTracker] = None,
    lan_companion: Optional[LanCompanionServer] = None,
    nav_controller: Optional[NavigationController] = None,
    route_legs: Optional[Sequence[Dict[str, Any]]] = None,
) -> None:
    if not res or not res.get("has_world_coords"):
        return
    try:
        map_id = int(res.get("map_id", 2))
        world_x = float(res["world_x"])
        world_y = float(res["world_y"])
        is_ug = bool(res.get("is_underground", False))
        floor_id = _safe_int_or_none(res.get("floor_id"))
        floor_name = str(
            res.get("floor_short")
            or res.get("hoyolab_floor_name")
            or res.get("floor_name")
            or ("B1" if is_ug else "G")
        )
        heading_raw = res.get("heading")
        heading_val = float(heading_raw) if heading_raw is not None else None

        pos_info = {
            "map_id": map_id,
            "lat": world_y,
            "lng": world_x,
            "world_x": world_x,
            "world_y": world_y,
            "heading": heading_val,
            "camera_heading": heading_val,
            "view_heading": heading_val,
            "is_underground": is_ug,
            "floor_id": floor_id,
            "floor_name": floor_name,
        }
        current_game_state["last_pos"] = pos_info

        if minimap_tracker is not None:
            with _tracker_lock:
                minimap_tracker.set_map_m_anchor(
                    map_id=map_id,
                    world_x=world_x,
                    world_y=world_y,
                    is_underground=is_ug,
                    floor_id=floor_id,
                    floor_name=floor_name,
                    timestamp=_next_tracker_ts(),
                )

        if lan_companion is not None and lan_companion.is_running:
            lan_companion.broadcast_state(
                _build_lan_state_payload(pos_info, nav_controller, route_legs),
                force=True,
            )
    except Exception as e:
        logger.debug(f"_apply_map_m_anchor error: {e}")


def _acquire_single_instance_mutex(port: int) -> Optional[int]:
    """Ensures single instance; gracefully asks any older instance to /shutdown first."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    ERROR_ALREADY_EXISTS = 183
    for attempt in range(4):
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        last_err = kernel32.GetLastError()
        if handle and last_err != ERROR_ALREADY_EXISTS:
            return int(handle)
        if handle:
            kernel32.CloseHandle(handle)
        if attempt == 0:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/shutdown",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=1.5):
                    logger.info("Requested graceful shutdown of previous Genshin Helper instance...")
            except Exception:
                pass
        time.sleep(0.45)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return int(handle) if handle else None


async def tracking_loop(
    server: SyncServer,
    capturer: ScreenCapturer,
    locator: MapLocator,
    state_machine: MapScreenStateMachine,
    stop_event: asyncio.Event,
    engine_state: Optional[Dict[str, bool]] = None,
    minimap_tracker: Optional[HybridMinimapTracker] = None,
    nav_controller: Optional[NavigationController] = None,
    nav_hud: Optional[NavMiniHud] = None,
    lan_companion: Optional[LanCompanionServer] = None,
    hud: Optional[HudToastOverlay] = None,
    tray: Optional[SystemTrayApp] = None,
    hotkey_mgr: Optional[HotkeyManager] = None,
    route_state: Optional[Dict[str, Any]] = None,
):
    logger.info("Tracking loop running (v5 Hybrid Minimap + 3-Anchor Map M FSM enabled). Waiting for Genshin Impact window...")
    window_attached = False
    last_res = None
    pending_verify = None
    uipi_checked_hwnd: Optional[int] = None
    nav_hud_visible = False
    last_hud_sig: Optional[Tuple[Any, ...]] = None
    last_mm_bcast_ts = 0.0
    last_mm_bcast_state: Optional[Tuple[Any, ...]] = None
    gate_reject_streak = 0
    hdr_clip_streak = 0
    hdr_warned = False

    def _hide_nav_hud() -> None:
        nonlocal nav_hud_visible, last_hud_sig
        if nav_hud is not None and (nav_hud_visible or nav_hud.is_visible):
            nav_hud.hide()
            nav_hud_visible = False
        last_hud_sig = None

    def _active_route_legs() -> Optional[List[Dict[str, Any]]]:
        if isinstance(route_state, dict):
            legs = route_state.get("legs")
            if isinstance(legs, list):
                return legs
        return None

    while server.is_running and not stop_event.is_set():
        # 0% CPU Standby gate: ZERO screen captures or Win32 polls when inactive
        if engine_state is not None and not engine_state.get("active", True):
            if window_attached:
                window_attached = False
                last_res = None
            state_machine.reset()
            pending_verify = None
            uipi_checked_hwnd = None
            last_mm_bcast_state = None
            _pause_minimap_tracker(minimap_tracker, reason="standby")
            _hide_nav_hud()
            await asyncio.sleep(0.35)
            continue

        win_info = WindowFinder.get_game_window(config.WINDOW_TITLES)
        if not win_info:
            if window_attached:
                logger.warning("Genshin Impact window lost. Waiting for window...")
                window_attached = False
                current_game_state["hwnd"] = None
                current_game_state["rect"] = None
                current_game_state["uipi_status"] = None
                last_res = None
                uipi_checked_hwnd = None
                if tray is not None and hasattr(tray, "set_uipi_warning"):
                    try:
                        tray.set_uipi_warning(False)
                    except Exception:
                        pass
            state_machine.reset()
            pending_verify = None
            last_mm_bcast_state = None
            _pause_minimap_tracker(minimap_tracker, reason="window_lost")
            _hide_nav_hud()
            await asyncio.sleep(0.8)
            continue

        hwnd, left, top, width, height = win_info
        current_game_state["hwnd"] = hwnd
        current_game_state["rect"] = (left, top, width, height)

        if width < 800 or height < 500:
            state_machine.reset()
            pending_verify = None
            _pause_minimap_tracker(minimap_tracker, reason="window_small")
            _hide_nav_hud()
            await asyncio.sleep(0.5)
            continue

        if not window_attached or last_res != (width, height):
            logger.info(f"Attached to Genshin Impact window (Resolution: {width}x{height})")
            window_attached = True
            last_res = (width, height)

        # UIPI Admin elevation mismatch check (once per attached hwnd)
        if hwnd and hwnd != uipi_checked_hwnd:
            uipi_checked_hwnd = hwnd
            try:
                raw_uipi = await asyncio.to_thread(check_uipi_status, hwnd)
                mismatch = bool(
                    raw_uipi.get("uipi_blocked")
                    or raw_uipi.get("needs_elevation")
                    or raw_uipi.get("mismatch")
                )
                uipi_info = {**raw_uipi, "mismatch": mismatch}
                current_game_state["uipi_status"] = uipi_info
                if tray is not None and hasattr(tray, "set_uipi_warning"):
                    try:
                        tray.set_uipi_warning(mismatch)
                    except Exception:
                        pass
                if mismatch:
                    logger.warning(f"[UIPI] {uipi_info.get('reason', 'Game process is elevated')}")
                    if hud is not None:
                        hud.show_toast(
                            t("uipi.warning.title"),
                            t("uipi.warning.short"),
                            kind="warning",
                            duration_ms=5500,
                            anchor_rect=(left, top, width, height),
                        )
            except Exception as e:
                logger.debug(f"UIPI check error: {e}")

        minimap_active = bool(minimap_tracker is not None and config.MINIMAP_TRACKING)
        if not config.AUTO_SYNC_MAP_M and not minimap_active:
            state_machine.reset()
            pending_verify = None
            _pause_minimap_tracker(minimap_tracker, reason="disabled")
            _hide_nav_hud()
            await asyncio.sleep(0.5)
            continue

        is_fg = WindowFinder.is_window_foreground(hwnd)
        if not is_fg and state_machine.state != state_machine.STATE_SETTLING and pending_verify is None:
            _pause_minimap_tracker(minimap_tracker, reason="background")
            _hide_nav_hud()
            await asyncio.sleep(0.25)
            continue

        if not config.AUTO_SYNC_MAP_M and state_machine.state != state_machine.STATE_CLOSED:
            state_machine.reset()
            pending_verify = None

        in_closed_state = state_machine.state == state_machine.STATE_CLOSED

        def _tick_worker(
            cl: int,
            ct: int,
            cw: int,
            ch: int,
            is_closed: bool,
            do_auto_sync: bool,
            do_minimap: bool,
        ):
            if is_closed:
                # Performance Revolution in STATE_CLOSED: capture ONLY tiny Close-X probe ROI
                px, py, pw, ph = state_machine.get_close_probe_rect(cl, ct, cw, ch)
                probe_hit = False
                if pw > 0 and ph > 0:
                    probe_roi = capturer.capture_region(px, py, pw, ph)
                    probe_hit, _ = state_machine.is_close_x_probe(probe_roi, ch)

                if probe_hit:
                    _pause_minimap_tracker(minimap_tracker, reason="map_m_probe")
                    if do_auto_sync:
                        frame = capturer.capture_region(cl, ct, cw, ch)
                        if frame is None:
                            return "map_m", None, None, None
                        return "map_m", frame, state_machine.step(frame), None
                    return "map_m_paused", None, None, None

                if do_minimap and minimap_tracker is not None:
                    layout = compute_ui_layout(cw, ch)
                    mx, my, mw, mh = layout.minimap_rect
                    pmx, pmy, pmw, pmh = layout.paimon_maybe_rect
                    cal_pad_x = int(round(48.0 * layout.ui_scale))
                    cal_pad_y = int(round(28.0 * layout.ui_scale))
                    hud_w = min(cw, max(mx + mw + cal_pad_x, pmx + pmw))
                    hud_h = min(ch, max(my + mh + cal_pad_y, pmy + pmh))
                    cap_bgra = getattr(capturer, "capture_region_bgra", capturer.capture_region)
                    hud_bgra = cap_bgra(cl, ct, hud_w, hud_h) if (hud_w > 0 and hud_h > 0) else None
                    with _tracker_lock:
                        pose = minimap_tracker.step_from_hud(hud_bgra, cw, ch, now=_next_tracker_ts())
                    return "minimap", None, None, pose

                return "idle_closed", None, None, None

            # Map (M) is currently open -> pause minimap tracker and capture full screen for FSM
            _pause_minimap_tracker(minimap_tracker, reason="map_m_open")
            frame = capturer.capture_region(cl, ct, cw, ch)
            if frame is None:
                return "map_m", None, None, None
            return "map_m", frame, state_machine.step(frame), None

        tick_mode, full_frame, step_res, pose = await asyncio.to_thread(
            _tick_worker,
            left,
            top,
            width,
            height,
            in_closed_state,
            bool(config.AUTO_SYNC_MAP_M),
            minimap_active,
        )

        if tick_mode == "map_m_paused":
            _hide_nav_hud()
        elif tick_mode == "map_m":
            if state_machine.state != state_machine.STATE_CLOSED:
                _hide_nav_hud()
            if full_frame is not None and step_res is not None:
                slider_hint = step_res.get("slider_res")
                if step_res.get("should_auto_sync"):
                    pending_verify = None
                    res = await asyncio.to_thread(locator.locate_player, full_frame, False, slider_hint)
                    if res is None:
                        # Only 1 transient retry on missing arrow in case player icon faded in late
                        if state_machine._retries == 0:
                            state_machine.request_retry()
                    elif res.get("has_world_coords"):
                        dx_norm = abs(float(res["pixel_x"]) - width * 0.5) / max(1.0, width * 0.5)
                        dy_norm = abs(float(res["pixel_y"]) - height * 0.5) / max(1.0, height * 0.5)
                        if dx_norm > 0.65 or dy_norm > 0.65:
                            logger.info(
                                f"[Auto-Sync M] Skipped: player arrow off-center "
                                f"({dx_norm:.2f}, {dy_norm:.2f}) — user is panning."
                            )
                        elif (
                            res.get("map_id") == 2
                            and res.get("inliers_count", 0) < 2
                            and state_machine._retries < state_machine.MAX_RETRIES
                        ):
                            state_machine.request_retry()
                        else:
                            server.broadcast({"type": "SYNC_POSITION_ONLY", **res})
                            _apply_map_m_anchor(
                                res,
                                minimap_tracker=minimap_tracker,
                                lan_companion=lan_companion,
                                nav_controller=nav_controller,
                                route_legs=_active_route_legs(),
                            )
                            ug_str = (
                                f" [Floor {res.get('floor_short') or res.get('floor_name')}]"
                                if res.get("is_underground")
                                else " [Surface]"
                            )
                            logger.info(
                                f"[Auto-Sync M] Map {res.get('map_id')} -> "
                                f"({res['lat']:.1f}, {res['lng']:.1f}){ug_str}"
                            )
                            if res.get("inliers_count", 0) <= 3:
                                pending_verify = {"ticks": 4, "res": res}
                    else:
                        state_machine.request_retry()
                elif pending_verify is not None:
                    if step_res.get("state") != state_machine.STATE_SYNCED_IDLE:
                        pending_verify = None
                    else:
                        pending_verify["ticks"] -= 1
                        if pending_verify["ticks"] <= 0:
                            prev = pending_verify["res"]
                            pending_verify = None
                            v_res = await asyncio.to_thread(locator.locate_player, full_frame, False, slider_hint)
                            if v_res and v_res.get("has_world_coords"):
                                v_dx_norm = abs(float(v_res["pixel_x"]) - width * 0.5) / max(1.0, width * 0.5)
                                v_dy_norm = abs(float(v_res["pixel_y"]) - height * 0.5) / max(1.0, height * 0.5)
                                if v_dx_norm <= 0.65 and v_dy_norm <= 0.65:
                                    dist = math.hypot(
                                        v_res["world_x"] - prev["world_x"],
                                        v_res["world_y"] - prev["world_y"],
                                    )
                                    same_layer = (
                                        v_res.get("is_underground") == prev.get("is_underground")
                                        and v_res.get("group_id") == prev.get("group_id")
                                    )
                                    if (
                                        (dist > 60.0 and v_res.get("inliers_count", 0) > prev.get("inliers_count", 0))
                                        or (not same_layer and v_res.get("inliers_count", 0) >= prev.get("inliers_count", 0))
                                    ):
                                        server.broadcast({"type": "SYNC_POSITION_ONLY", **v_res})
                                        _apply_map_m_anchor(
                                            v_res,
                                            minimap_tracker=minimap_tracker,
                                            lan_companion=lan_companion,
                                            nav_controller=nav_controller,
                                            route_legs=_active_route_legs(),
                                        )
                                        logger.info(
                                            f"[Auto-Sync M Verify] Refined Map {v_res.get('map_id')} -> "
                                            f"({v_res['lat']:.1f}, {v_res['lng']:.1f}) inl={v_res.get('inliers_count')}"
                                        )
        elif tick_mode == "minimap" and pose is not None:
            if pose.hdr_clipped:
                hdr_clip_streak += 1
                if hdr_clip_streak >= 8 and not hdr_warned:
                    hdr_warned = True
                    logger.warning(f"[HDR] {t('hdr.warning.short')}")
            else:
                hdr_clip_streak = 0

            pose_valid = bool(
                pose.state == STATE_TRACKING
                and pose.map_id is not None
                and pose.lat is not None
                and pose.lng is not None
            )

            should_bc_pos = bool(getattr(pose, "should_broadcast_web", True))
            allow_heading_only = bool(pose.stationary and pose.match_method == "stationary_skip")

            if pose_valid and (should_bc_pos or allow_heading_only):
                if should_bc_pos:
                    if pose.match_method == "global_teyvat_relocate":
                        logger.info(
                            f"[Minimap V5.3] Global Teyvat relocate -> Map {pose.map_id} "
                            f"({pose.lat:.1f}, {pose.lng:.1f}) inl={pose.sift_inliers} conf={pose.confidence:.2f}"
                        )
                    elif pose.match_method == "layer_switch_sift":
                        logger.info(
                            f"[Minimap V5.3] Auto-layer switch -> Map {pose.map_id} "
                            f"layer={'UG' if pose.is_underground else 'Surf'} {pose.floor_name} "
                            f"(fid={pose.floor_id}) ({pose.lat:.1f}, {pose.lng:.1f})"
                        )

                heading_val = pose.body_heading if pose.body_heading is not None else pose.view_heading
                cam_val = pose.camera_heading if pose.camera_heading is not None else pose.view_heading
                prev_last = current_game_state.get("last_pos") or {}
                if not should_bc_pos and allow_heading_only and prev_last.get("lat") is not None:
                    eff_mid = int(prev_last.get("map_id") if prev_last.get("map_id") is not None else pose.map_id)
                    eff_pos_lat = float(prev_last["lat"])
                    eff_pos_lng = float(prev_last["lng"])
                    eff_wx = float(prev_last.get("world_x") if prev_last.get("world_x") is not None else eff_pos_lng)
                    eff_wy = float(prev_last.get("world_y") if prev_last.get("world_y") is not None else eff_pos_lat)
                    eff_ug = bool(prev_last.get("is_underground") if "is_underground" in prev_last else pose.is_underground)
                    eff_fid = prev_last.get("floor_id", pose.floor_id)
                    eff_fname = prev_last.get("floor_name", pose.floor_name)
                else:
                    eff_mid = int(pose.map_id)
                    eff_pos_lat = float(pose.lat)
                    eff_pos_lng = float(pose.lng)
                    eff_wx = float(pose.world_x)
                    eff_wy = float(pose.world_y)
                    eff_ug = bool(pose.is_underground)
                    eff_fid = pose.floor_id
                    eff_fname = pose.floor_name

                pos_info = {
                    "map_id": eff_mid,
                    "lat": eff_pos_lat,
                    "lng": eff_pos_lng,
                    "world_x": eff_wx,
                    "world_y": eff_wy,
                    "heading": float(heading_val) if heading_val is not None else None,
                    "camera_heading": float(cam_val) if cam_val is not None else None,
                    "view_heading": float(pose.view_heading) if pose.view_heading is not None else None,
                    "is_underground": eff_ug,
                    "floor_id": eff_fid,
                    "floor_name": eff_fname,
                }
                current_game_state["last_pos"] = pos_info

                # Throttled (~10 Hz) MINIMAP_POSITION broadcast only on meaningful pos/angle delta
                now_mono = time.monotonic()
                if (now_mono - last_mm_bcast_ts) >= 0.10:
                    should_send = False
                    if not should_bc_pos and allow_heading_only:
                        if last_mm_bcast_state is not None:
                            _, _, _, p_hdg, p_cam, _, _ = last_mm_bcast_state
                            if (
                                _angle_diff_deg(pos_info["heading"], p_hdg) >= 2.0
                                or _angle_diff_deg(pos_info["camera_heading"], p_cam) >= 2.0
                            ):
                                should_send = True
                    elif last_mm_bcast_state is None:
                        should_send = True
                    else:
                        p_mid, p_lat, p_lng, p_hdg, p_cam, p_ug, p_fid = last_mm_bcast_state
                        if (
                            p_mid != pos_info["map_id"]
                            or p_ug != pos_info["is_underground"]
                            or p_fid != pos_info["floor_id"]
                            or math.hypot(pos_info["lat"] - p_lat, pos_info["lng"] - p_lng) >= 0.8
                            or _angle_diff_deg(pos_info["heading"], p_hdg) >= 2.0
                            or _angle_diff_deg(pos_info["camera_heading"], p_cam) >= 2.0
                        ):
                            should_send = True

                    if should_send:
                        last_mm_bcast_ts = now_mono
                        last_mm_bcast_state = (
                            pos_info["map_id"],
                            pos_info["lat"],
                            pos_info["lng"],
                            pos_info["heading"],
                            pos_info["camera_heading"],
                            pos_info["is_underground"],
                            pos_info["floor_id"],
                        )
                        bcast_ephemeral = getattr(server, "broadcast_ephemeral", server.broadcast)
                        bcast_ephemeral({
                            "type": "MINIMAP_POSITION",
                            "source": "minimap",
                            "has_world_coords": True,
                            "map_id": pos_info["map_id"],
                            "world_x": pos_info["world_x"],
                            "world_y": pos_info["world_y"],
                            "lat": pos_info["lat"],
                            "lng": pos_info["lng"],
                            "heading": pos_info["heading"],
                            "camera_heading": pos_info["camera_heading"],
                            "body_heading": pose.body_heading,
                            "view_heading": pose.view_heading,
                            "minimap_mode": pose.minimap_mode,
                            "zoom_regime": pose.zoom_regime,
                            "is_underground": pos_info["is_underground"],
                            "floor_id": eff_fid,
                            "floor_short": eff_fname,
                            "floor_name": eff_fname,
                            "confidence": round(float(pose.confidence), 3),
                            "hdr_clipped": bool(pose.hdr_clipped),
                            "state": pose.state,
                        })

                # Push 15 Hz state to LAN Companion (internally coalesced to max_hz=15.0)
                if lan_companion is not None and lan_companion.is_running:
                    lan_companion.broadcast_state(
                        _build_lan_state_payload(pos_info, nav_controller, _active_route_legs())
                    )

            # Navigation Mini-HUD guidance update (strictly hidden on Map M / menus / when disabled)
            if not config.NAV_HUD_ENABLED or nav_hud is None or nav_controller is None:
                _hide_nav_hud()
            elif pose.match_method in ("gate_rejected", "gate_hold", "paimon_pause", "loading_pause", "paused"):
                gate_reject_streak += 1
                if gate_reject_streak >= 3:
                    if nav_hud_visible:
                        logger.info(f"[NavHUD] Hidden (minimap pause/gate: {pose.reason or pose.match_method})")
                    _hide_nav_hud()
            else:
                gate_reject_streak = 0
                fallback_pos = current_game_state.get("last_pos") or {}
                eff_map_id = int(pose.map_id if pose.map_id is not None else fallback_pos.get("map_id") or 2)
                eff_lat = float(pose.lat if pose.lat is not None else fallback_pos.get("lat") or 0.0)
                eff_lng = float(pose.lng if pose.lng is not None else fallback_pos.get("lng") or 0.0)
                eff_heading = pose.view_heading if pose.view_heading is not None else pose.camera_heading
                map_wps = (
                    locator.maps.get(eff_map_id, {}).get("waypoints")
                    if hasattr(locator, "maps") and isinstance(locator.maps, dict)
                    else None
                )
                guidance = nav_controller.compute_guidance(
                    player_lat=eff_lat,
                    player_lng=eff_lng,
                    view_heading_deg=eff_heading,
                    player_map_id=eff_map_id,
                    player_floor_id=(str(pose.floor_id) if pose.floor_id is not None else None),
                    player_is_underground=bool(pose.is_underground),
                    tracking_state=pose.state,
                    waypoints=map_wps,
                )
                cur_lang = get_language()
                if guidance.state == "idle" and guidance.target is None:
                    # Show calm status pill under minimap when no uncollected target pins are loaded yet
                    # (avoid spinning a North-only arrow that looks inverted vs the minimap above it)
                    if not server.has_active_client():
                        idle_hint = (
                            "Mở / F5 HoYoLAB Map để kết nối"
                            if cur_lang == "vi"
                            else "Open / F5 HoYoLAB Map to connect"
                        )
                    else:
                        idle_hint = (
                            "Bật bộ lọc rương/thần đồng trên HoYoLAB"
                            if cur_lang == "vi"
                            else "Enable pin filters on HoYoLAB Map"
                        )
                    guidance = replace(
                        guidance,
                        delta_deg=None,
                        route_target_name=idle_hint,
                    )
                sync_label = (
                    hotkey_mgr.get_display_name("sync")
                    if hotkey_mgr is not None
                    else config.HOTKEY_MARK.upper()
                )
                hud_sig = (
                    guidance.state,
                    guidance.target.id if guidance.target is not None else None,
                    round(guidance.distance_m, 1) if guidance.distance_m is not None else None,
                    round(guidance.delta_deg, 1) if guidance.delta_deg is not None else None,
                    guidance.floor_mismatch,
                    guidance.route_target_name,
                    guidance.route_step,
                    guidance.route_total,
                    guidance.remaining_count,
                    guidance.show_spot_card,
                    (left, top, width, height),
                    config.NAV_HUD_POSITION,
                    cur_lang,
                    sync_label,
                )
                if hud_sig != last_hud_sig:
                    was_visible = nav_hud_visible
                    nav_hud.update(
                        guidance,
                        window_rect=(left, top, width, height),
                        position_mode=config.NAV_HUD_POSITION,
                        lang=cur_lang,
                        hotkey_label=sync_label,
                    )
                    nav_hud_visible = True
                    last_hud_sig = hud_sig
                    if not was_visible:
                        logger.info(
                            f"[NavHUD] Shown (state={guidance.state}, pins={len(nav_controller.pins)}, "
                            f"pos={config.NAV_HUD_POSITION}, win={width}x{height})"
                        )
        else:
            _hide_nav_hud()

        cur_state = state_machine.state
        if cur_state == state_machine.STATE_SETTLING:
            sleep_s = 0.045
        elif cur_state == state_machine.STATE_CLOSED:
            sleep_s = 0.033 if minimap_active else 0.075
        else:
            sleep_s = 0.11
        await asyncio.sleep(sleep_s)


def handle_hotkey_action(
    server: SyncServer,
    locator: MapLocator,
    capturer: ScreenCapturer,
    state_machine: Optional[MapScreenStateMachine] = None,
    hud: Optional[HudToastOverlay] = None,
    audio: Optional[AudioFeedback] = None,
    minimap_tracker: Optional[HybridMinimapTracker] = None,
    lan_companion: Optional[LanCompanionServer] = None,
    nav_controller: Optional[NavigationController] = None,
    route_state: Optional[Dict[str, Any]] = None,
):
    """
    Called immediately when Sync Hotkey (e.g. Alt+F11) is pressed:
    1. Captures full game window (Map M).
    2. Runs MapLocator to find player arrow, waypoints, and cave layer.
    3. Marks current open-Map state synced so FSM won't double-fire.
    4. Anchors HybridMinimapTracker & pushes to LAN Companion.
    5. Broadcasts SYNC_AND_MARK to HoYoLAB Web Map!
    """
    try:
        logger.info(f"[{config.HOTKEY_MARK} Pressed] Capturing game screen for Map Sync...")
        rect = current_game_state.get("rect")
        if not rect or rect[2] < 1000 or rect[3] < 600:
            win_info = WindowFinder.get_game_window(config.WINDOW_TITLES, allow_screen_fallback=True)
            if win_info:
                rect = win_info[1:]
                current_game_state["rect"] = rect

        if config.SINGLE_MONITOR_MODE and not server.has_active_client():
            logger.warning("Single-Monitor Mode: No active HoYoLAB Web Map client connected.")
            if hud:
                hud.show_toast(
                    t("toast.map_not_ready.title"),
                    t("toast.map_not_ready.detail"),
                    kind="error",
                    duration_ms=2800,
                    anchor_rect=rect,
                )
            if audio:
                audio.play("error")
            return

        if not rect:
            logger.warning("Genshin window not detected. Broadcasting generic MARK request...")
            if config.SINGLE_MONITOR_MODE:
                if hud:
                    hud.show_toast(
                        t("toast.not_detected.title"),
                        t("toast.not_detected.detail"),
                        kind="warning",
                        duration_ms=2500,
                        anchor_rect=rect,
                    )
                if audio:
                    audio.play("warning")
            server.broadcast({"type": "SYNC_AND_MARK", "detected": False}, feedback_handled=config.SINGLE_MONITOR_MODE)
            return

        left, top, width, height = rect
        full_frame = capturer.capture_region(left, top, width, height)
        if full_frame is None:
            logger.warning("Failed to grab screen frame.")
            if config.SINGLE_MONITOR_MODE:
                if hud:
                    hud.show_toast(
                        t("toast.not_detected.title"),
                        t("borderless.tip"),
                        kind="warning",
                        duration_ms=2500,
                        anchor_rect=rect,
                    )
                if audio:
                    audio.play("warning")
            server.broadcast({"type": "SYNC_AND_MARK", "detected": False}, feedback_handled=config.SINGLE_MONITOR_MODE)
            return

        res = locator.locate_player(full_frame)
        if state_machine is not None:
            state_machine.mark_synced(full_frame)

        if res and res.get("detected"):
            payload = {"type": "SYNC_AND_MARK", **res}
            server.broadcast(payload)
            if res.get("has_world_coords"):
                legs = route_state.get("legs") if isinstance(route_state, dict) else None
                _apply_map_m_anchor(
                    res,
                    minimap_tracker=minimap_tracker,
                    lan_companion=lan_companion,
                    nav_controller=nav_controller,
                    route_legs=legs,
                )
                ug_str = f" [Underground Floor {res['floor_idx']}]" if res.get('is_underground') else " [Surface]"
                map_str = f" [{res.get('map_name', 'Teyvat')}]"
                logger.info(f"[Map Sync Action] Success!{map_str} World: Lat={res['lat']:.1f}, Lng={res['lng']:.1f}{ug_str}")
            else:
                logger.info(f"[Map Sync Action] Detected player arrow at pixel ({res['pixel_x']:.0f}, {res['pixel_y']:.0f})")
        else:
            logger.info("Player arrow not detected on screen.")
            if config.SINGLE_MONITOR_MODE:
                if hud:
                    hud.show_toast(
                        t("toast.not_detected.title"),
                        t("toast.not_detected.detail"),
                        kind="warning",
                        duration_ms=2500,
                        anchor_rect=rect,
                    )
                if audio:
                    audio.play("warning")
            server.broadcast({"type": "SYNC_AND_MARK", "detected": False}, feedback_handled=config.SINGLE_MONITOR_MODE)
    except Exception as e:
        logger.exception(f"Exception in handle_hotkey_action: {e}")

def handle_debug_action(server: SyncServer, locator: MapLocator, capturer: ScreenCapturer,
                        browser_state: Optional[dict] = None, trigger_source: str = "hotkey_debug",
                        user_note: str = "", broadcast: bool = True) -> dict:
    """
    Called when Debug Hotkey (e.g. Alt+F12) or Web UI diagnostic button is triggered:
    1. Simultaneously captures Game + Browser windows.
    2. Runs Computer Vision analysis with deep telemetry.
    3. Produces annotated visual & JSON report bundle in scratch/diagnostics/.
    4. Returns bundle metadata for browser bug-note modal.
    """
    try:
        logger.info(f"📸 [{trigger_source.upper()}] Triggered Diagnostic Capture & Debug Bundle...")
        rect = current_game_state.get("rect")
        bundle = DiagnosticManager.capture_and_create_bundle(
            capturer=capturer,
            locator=locator,
            game_rect=rect,
            browser_state=browser_state or server.last_browser_state,
            trigger_source=trigger_source,
            user_note=user_note,
        )
        if broadcast:
            server.broadcast({
                "type": "DIAGNOSTIC_CAPTURED",
                "folder_name": bundle.get("folder_name", ""),
                "bundle_dir": bundle.get("bundle_dir", ""),
                "has_world_coords": bundle.get("has_world_coords", False),
                "reason": bundle.get("reason", "")
            })
        return bundle
    except Exception as e:
        logger.exception(f"Exception in handle_debug_action: {e}")
        return {"success": False, "error": str(e)}

def main(argv=None):
    parser = argparse.ArgumentParser(description="Genshin Impact Exploration & HoYoLAB Auto-Sync Tool")
    parser.add_argument("--no-tray", action="store_true", help="Disable Windows System Tray icon")
    parser.add_argument("--standby", action="store_true", help="Start in Standby mode (0%% CPU until awakened)")
    parser.add_argument("--minimized", action="store_true", help="Start silently in background tray")
    args, _ = parser.parse_known_args(argv)

    set_language(config.LANGUAGE)
    mutex_handle = _acquire_single_instance_mutex(config.WS_PORT)
    capturer = ScreenCapturer(mode=config.CAPTURE_MODE)
    locator = MapLocator()
    state_machine = MapScreenStateMachine()
    server = SyncServer(host=config.WS_HOST, port=config.WS_PORT)
    audio = AudioFeedback()
    hud = HudToastOverlay()

    # v5.3 core subsystems (tile_cache borrows locator.terrain_data + non-blocking async HD/UG providers)
    maps_db = load_multimap_db()
    hd_provider = HoyolabHdTileProvider(maps_db=maps_db, async_fetch=True)
    ug_provider = UndergroundOverlayProvider(maps_db=maps_db, async_fetch=True)
    tile_cache = LruTerrainTileCache(
        max_tiles=72,
        terrain_data=locator.terrain_data,
        tile_provider=hd_provider,
    )
    global_relocator = TeyvatGlobalRelocator(terrain_data=locator.terrain_data, lazy=True)
    global_relocator.warmup_async()
    minimap_tracker = HybridMinimapTracker(
        tile_cache=tile_cache,
        ug_provider=ug_provider,
        global_relocator=global_relocator,
        paimon_cache_path=default_paimon_cache_path(),
        config=MinimapTrackerConfig(
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
        ),
    )
    nav_controller = NavigationController(waypoints=locator.waypoints)
    route_state: Dict[str, Any] = {"legs": [], "last_route": None}
    lan_companion = LanCompanionServer(host="0.0.0.0", port=config.LAN_COMPANION_PORT)
    affine_registry = PerMapAffineRegistry()

    engine_state = {"active": not args.standby}
    tray: Optional[SystemTrayApp] = None

    def _refresh_tray_state():
        if tray is None:
            return
        if not engine_state["active"]:
            tray.set_state("paused", engine_active=False)
        elif server.has_active_client():
            tray.set_state("connected", engine_active=True)
        else:
            tray.set_state("idle", engine_active=True)

    def _push_lan_snapshot(force: bool = False):
        if not lan_companion.is_running:
            return
        lan_companion.broadcast_state(
            _build_lan_state_payload(
                current_game_state.get("last_pos"),
                nav_controller,
                route_state.get("legs"),
            ),
            force=force,
        )

    def on_sync_hotkey():
        if not engine_state["active"]:
            return
        handle_hotkey_action(
            server,
            locator,
            capturer,
            state_machine=state_machine,
            hud=hud,
            audio=audio,
            minimap_tracker=minimap_tracker,
            lan_companion=lan_companion,
            nav_controller=nav_controller,
            route_state=route_state,
        )

    def on_debug_hotkey():
        if not engine_state["active"]:
            return None
        handle_hotkey_action_res = handle_debug_action(server, locator, capturer, trigger_source="hotkey_debug")
        return handle_hotkey_action_res

    def on_switch_hotkey():
        if not engine_state["active"] or not config.SINGLE_MONITOR_MODE:
            return
        target = WindowSwitcher.toggle_game_and_browser(config.WINDOW_TITLES)
        if target == "no_browser":
            rect = current_game_state.get("rect")
            hud.show_toast(
                t("toast.map_not_ready.title"),
                t("toast.map_not_ready.detail"),
                kind="warning",
                duration_ms=2400,
                anchor_rect=rect,
            )
            audio.play("warning")

    # Unified Multi-Hotkey Manager for Sync, Debug, and Single-Monitor Switch actions
    hotkey_mgr = HotkeyManager()
    hotkey_mgr.register("sync", config.HOTKEY_MARK, on_sync_hotkey, enabled=engine_state["active"])
    hotkey_mgr.register("debug", config.HOTKEY_DEBUG, on_debug_hotkey, enabled=engine_state["active"])
    hotkey_mgr.register(
        "switch",
        config.HOTKEY_SWITCH,
        on_switch_hotkey,
        enabled=bool(engine_state["active"] and config.SINGLE_MONITOR_MODE),
    )
    hotkey_mgr.start()

    nav_hud = NavMiniHud(
        position_mode=config.NAV_HUD_POSITION,
        lang=get_language(),
        hotkey_label=hotkey_mgr.get_display_name("sync"),
    )

    # Strictly opt-in LAN Companion: only binds socket if explicitly enabled in config
    if config.LAN_COMPANION_ENABLED:
        try:
            lan_info = lan_companion.start_in_background()
            logger.info(
                f"==> LAN Companion started (opt-in): {lan_info.get('url')} (PIN: {lan_info.get('pin')})"
            )
        except Exception as e:
            logger.error(f"Failed to start opt-in LAN Companion on port {config.LAN_COMPANION_PORT}: {e}")

    def set_engine_active(active: bool) -> bool:
        want = bool(active)
        engine_state["active"] = want
        hotkey_mgr.set_enabled("sync", want)
        hotkey_mgr.set_enabled("debug", want)
        hotkey_mgr.set_enabled("switch", bool(want and config.SINGLE_MONITOR_MODE))
        if not want:
            state_machine.reset()
            _pause_minimap_tracker(minimap_tracker, reason="standby")
            nav_hud.hide()
            locator.ocr.close()
        _refresh_tray_state()
        logger.info(f"==> Engine State changed: [{'ACTIVE' if want else 'STANDBY (0% CPU)'}]")
        return engine_state["active"]

    def on_hotkey_change(action: str, new_key: str):
        action = action.strip().lower()
        success = hotkey_mgr.update_binding(action, new_key)
        if success:
            if action == "sync":
                config.HOTKEY_MARK = new_key
            elif action == "debug":
                config.HOTKEY_DEBUG = new_key
            elif action == "switch":
                config.HOTKEY_SWITCH = new_key
            config.save_to_file()
            display_name = hotkey_mgr.get_display_name(action)
            logger.info(f"==> Hotkey [{action.upper()}] updated & saved: [{display_name}]")
            return True, display_name
        return False, hotkey_mgr.get_display_name(action)

    def on_auto_sync_change(enabled: bool) -> bool:
        config.AUTO_SYNC_MAP_M = bool(enabled)
        if not config.AUTO_SYNC_MAP_M:
            state_machine.reset()
        config.save_to_file()
        logger.info(f"==> Auto-Sync Map (M) updated & saved: {config.AUTO_SYNC_MAP_M}")
        return config.AUTO_SYNC_MAP_M

    def on_single_monitor_change(enabled: bool) -> bool:
        config.SINGLE_MONITOR_MODE = bool(enabled)
        hotkey_mgr.set_enabled("switch", bool(config.SINGLE_MONITOR_MODE and engine_state["active"]))
        config.save_to_file()
        logger.info(f"==> Single-Monitor Mode updated & saved: {config.SINGLE_MONITOR_MODE}")
        if config.SINGLE_MONITOR_MODE:
            rect = current_game_state.get("rect")
            hud.show_toast(
                t("app.name"),
                t("hud.hint.mark", hotkey=hotkey_mgr.get_display_name("switch")),
                kind="info",
                duration_ms=2400,
                anchor_rect=rect,
            )
        return config.SINGLE_MONITOR_MODE

    def on_language_change(lang: str) -> Dict[str, str]:
        raw = str(lang or "auto").strip().lower()
        if raw not in ("auto", "vi", "en"):
            raw = "auto"
        config.LANGUAGE = raw
        resolved = set_language(raw)
        config.save_to_file()
        nav_hud.set_language(resolved)
        if tray is not None and hasattr(tray, "set_language"):
            try:
                tray.set_language(resolved)
            except Exception:
                pass
        _refresh_tray_state()
        logger.info(f"==> Language updated & saved: setting='{raw}', resolved='{resolved}'")
        return {"language": raw, "resolved": resolved}

    def on_minimap_tracking_change(enabled: bool) -> bool:
        config.MINIMAP_TRACKING = bool(enabled)
        if not config.MINIMAP_TRACKING:
            _pause_minimap_tracker(minimap_tracker, reason="disabled")
            nav_hud.hide()
        config.save_to_file()
        logger.info(f"==> Minimap Tracking updated & saved: {config.MINIMAP_TRACKING}")
        return config.MINIMAP_TRACKING

    def on_nav_hud_change(
        enabled: Optional[bool] = None,
        position: Optional[str] = None,
    ) -> Dict[str, Any]:
        if enabled is not None:
            config.NAV_HUD_ENABLED = bool(enabled)
        if position is not None and str(position).strip():
            norm_pos = NavMiniHud._norm_position_mode(str(position))
            config.NAV_HUD_POSITION = norm_pos
            nav_hud.set_position_mode(norm_pos)
        if not config.NAV_HUD_ENABLED:
            nav_hud.hide()
        config.save_to_file()
        logger.info(
            f"==> Nav Mini-HUD updated & saved: enabled={config.NAV_HUD_ENABLED}, "
            f"position='{config.NAV_HUD_POSITION}'"
        )
        return {
            "enabled": config.NAV_HUD_ENABLED,
            "position": config.NAV_HUD_POSITION,
            "nav_hud_enabled": config.NAV_HUD_ENABLED,
            "nav_hud_position": config.NAV_HUD_POSITION,
        }

    def on_nearby_pins(
        pins: Sequence[Any],
        map_id: int = 2,
        floor_id: Any = None,
    ) -> Dict[str, Any]:
        norm_pins: List[NavPin] = []
        for item in pins or ():
            pin = _normalize_raw_nav_pin(item, default_map_id=map_id)
            if pin is not None:
                norm_pins.append(pin)
        nav_controller.set_pins(norm_pins, reset_order=False)
        _push_lan_snapshot(force=False)
        cur = nav_controller.current_target
        return {
            "ok": True,
            "count": len(norm_pins),
            "target": cur.to_dict() if cur is not None else None,
        }

    def on_nav_command(action: str, req_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        act = str(action or "").strip().lower()
        data = req_data if isinstance(req_data, dict) else {}
        pin_id = data.get("pin_id")

        if act in ("next", "next_target"):
            target = nav_controller.next_target()
        elif act in ("prev", "previous", "prev_target"):
            target = nav_controller.prev_target()
        elif act in ("skip", "skip_current"):
            target = nav_controller.skip_current()
        elif act in ("blacklist", "blacklist_current"):
            target = nav_controller.blacklist_current()
        elif act in ("mark", "collect", "collected", "mark_collected"):
            target = nav_controller.mark_collected(str(pin_id) if pin_id is not None else None)
        elif act == "undo":
            target = nav_controller.undo()
        elif act in ("refresh", "refresh_nearest"):
            last_pos = current_game_state.get("last_pos") or {}
            target = nav_controller.refresh_nearest(
                player_lat=last_pos.get("lat"),
                player_lng=last_pos.get("lng"),
                player_map_id=last_pos.get("map_id"),
                player_floor_id=(
                    str(last_pos["floor_id"]) if last_pos.get("floor_id") is not None else None
                ),
                player_is_underground=last_pos.get("is_underground"),
            )
        elif act in ("select", "select_target") and pin_id is not None:
            target = nav_controller.select_target(str(pin_id))
        elif act in ("clear_route", "clear_route_queue"):
            nav_controller.clear_route_queue()
            route_state["legs"] = []
            route_state["last_route"] = None
            target = nav_controller.current_target
        else:
            return {"ok": False, "action": act, "error": f"Unsupported nav action: {act!r}"}

        _push_lan_snapshot(force=True)
        return {
            "ok": True,
            "action": act,
            "target": target.to_dict() if target is not None else None,
            "summary": asdict(nav_controller.summary),
        }

    def on_plan_tsp_route(
        pins: Sequence[Any],
        start_pos: Any = None,
        map_id: int = 2,
        teleport_penalty: float = 180.0,
    ) -> Dict[str, Any]:
        eff_map_id = int(map_id) if map_id is not None else 2
        eff_start = start_pos
        if eff_start is None:
            last_pos = current_game_state.get("last_pos")
            if isinstance(last_pos, dict) and last_pos.get("lat") is not None and last_pos.get("lng") is not None:
                eff_start = (float(last_pos["lat"]), float(last_pos["lng"]))

        map_entry = locator.maps.get(eff_map_id) if hasattr(locator, "maps") else None
        waypoints = (
            map_entry.get("waypoints")
            if isinstance(map_entry, dict) and map_entry.get("waypoints")
            else load_waypoints_for_map(eff_map_id)
        )
        route = plan_route(
            pins,
            start_pos=eff_start,
            waypoints=waypoints,
            teleport_penalty=float(teleport_penalty),
        )
        route_state["legs"] = route.get("legs", [])
        route_state["last_route"] = route

        ordered_nav_pins: List[NavPin] = []
        for item in route.get("ordered_pins", []):
            npin = _normalize_raw_nav_pin(item, default_map_id=eff_map_id)
            if npin is not None:
                ordered_nav_pins.append(npin)
        if ordered_nav_pins:
            nav_controller.set_route_queue(ordered_nav_pins)
        else:
            nav_controller.clear_route_queue()

        _push_lan_snapshot(force=True)
        return route

    def on_lan_companion_action(action: str) -> Dict[str, Any]:
        act = str(action or "status").strip().lower()
        try:
            if act == "start":
                if not lan_companion.is_running:
                    lan_companion.port = int(config.LAN_COMPANION_PORT)
                    lan_companion.start_in_background()
                config.LAN_COMPANION_ENABLED = True
                config.save_to_file()
                _push_lan_snapshot(force=True)
            elif act == "stop":
                if lan_companion.is_running:
                    lan_companion.stop_background()
                config.LAN_COMPANION_ENABLED = False
                config.save_to_file()
            elif act in ("rotate", "rotate_pin"):
                lan_companion.rotate_credentials()
            elif act == "toggle":
                if lan_companion.is_running:
                    lan_companion.stop_background()
                    config.LAN_COMPANION_ENABLED = False
                else:
                    lan_companion.port = int(config.LAN_COMPANION_PORT)
                    lan_companion.start_in_background()
                    config.LAN_COMPANION_ENABLED = True
                    _push_lan_snapshot(force=True)
                config.save_to_file()

            info = lan_companion.get_connection_info()
            running = bool(info.get("is_running", False))
            return {
                "ok": True,
                "running": running,
                **info,
            }
        except Exception as e:
            logger.error(f"LAN Companion action '{act}' failed: {e}")
            return {
                "ok": False,
                "running": bool(lan_companion.is_running),
                "url": "",
                "pin": "",
                "qr_svg": "",
                "error": str(e),
            }

    def on_launch_borderless(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        req = payload if isinstance(payload, dict) else {}
        dry_run = bool(req.get("dry_run", False))
        force = bool(req.get("force", False))
        width_val = _safe_int_or_none(req.get("width"))
        height_val = _safe_int_or_none(req.get("height"))
        exe_path = req.get("exe_path")
        rect = current_game_state.get("rect")

        if not dry_run and not force and WindowFinder.get_game_window(config.WINDOW_TITLES):
            msg = t("borderless.already_running")
            hud.show_toast(t("borderless.title"), msg, kind="info", duration_ms=3000, anchor_rect=rect)
            return {"ok": False, "launched": False, "already_running": True, "message": msg}

        res = launch_genshin_borderless(
            width=width_val,
            height=height_val,
            exe_path=str(exe_path) if exe_path else None,
            dry_run=dry_run,
        )
        ok_val = bool(res.get("ok", False))
        launched_val = bool(ok_val and not dry_run)
        if ok_val:
            msg = t("borderless.success")
            if not dry_run:
                hud.show_toast(t("borderless.title"), msg, kind="success", duration_ms=3000, anchor_rect=rect)
        elif res.get("exe_path") is None:
            msg = t("borderless.not_found")
            if not dry_run:
                hud.show_toast(t("borderless.title"), msg, kind="warning", duration_ms=3500, anchor_rect=rect)
        else:
            msg = t("borderless.failed", error=res.get("error") or "Unknown")
            if not dry_run:
                hud.show_toast(t("borderless.title"), msg, kind="error", duration_ms=3500, anchor_rect=rect)

        return {**res, "ok": ok_val, "launched": launched_val, "message": msg}

    def on_cross_map_export(payload: Dict[str, Any]) -> Dict[str, Any]:
        req = payload if isinstance(payload, dict) else {}
        map_id = _safe_int_or_none(req.get("map_id"))
        if map_id is None:
            map_id = 2
        target_format = str(req.get("target_format") or req.get("format") or "canonical").strip().lower()
        point_list = req.get("point_list")
        marked_list = req.get("marked_list")
        raw_pins = req.get("pins")

        parsed_export = None
        missing_ids: List[int] = []
        if point_list is not None and marked_list is not None:
            parsed_export = parse_hoyolab_export(point_list, marked_list, map_id=map_id)
            source_pins = parsed_export.pins
            missing_ids = parsed_export.missing_from_point_list
        elif point_list is not None:
            source_pins = parse_hoyolab_points(point_list, map_id=map_id)
        elif isinstance(raw_pins, list):
            source_pins = [
                p if isinstance(p, CanonicalPin) else CanonicalPin.from_dict(p)
                for p in raw_pins
                if isinstance(p, (CanonicalPin, dict))
            ]
        else:
            source_pins = []

        if target_format == "canonical":
            exported = CanonicalJsonAdapter.export(
                parsed_export if parsed_export is not None else source_pins,
                map_id=map_id,
            )
            unmapped_cats = sorted(
                {p.canonical_category for p in source_pins if p.canonical_category.startswith("unmapped_")}
            )
            total_considered = len(source_pins) + len(missing_ids)
            report = {
                "total_source_pins": len(source_pins),
                "matched_count": len(source_pins),
                "confident_count": len(source_pins),
                "ambiguous_count": 0,
                "unmatched_count": 0,
                "unmapped_categories": unmapped_cats,
                "missing_from_point_list_count": len(missing_ids),
                "match_rate_pct": round((100.0 * len(source_pins) / total_considered), 2) if total_considered > 0 else 0.0,
            }
            return {"ok": True, "format": target_format, "payload": exported, "report": report}

        raw_targets = req.get("target_pins")
        if raw_targets is None:
            raw_targets = req.get("target_markers") or []

        if isinstance(raw_targets, list) and all(isinstance(tp, TargetPin) for tp in raw_targets):
            parsed_targets = list(raw_targets)
        elif target_format == "kongying":
            parsed_targets = KongyingAdapter.parse_markers(raw_targets, map_id=map_id)
        elif target_format == "appsample":
            parsed_targets = AppsampleAdapter.parse_markers(raw_targets, map_id=map_id)
        elif target_format == "genshinmap":
            parsed_targets = GenshinMapGithubAdapter.parse_markers(raw_targets, map_id=map_id)
        else:
            return {
                "ok": False,
                "format": target_format,
                "payload": None,
                "report": None,
                "error": t("export.unsupported_map", map_id=map_id, format=target_format),
            }

        affine: Optional[AxisAffine] = None
        if isinstance(req.get("affine"), dict):
            affine = AxisAffine.from_dict(req["affine"])
            if affine.map_id is None:
                affine = AxisAffine(
                    ax=affine.ax,
                    bx=affine.bx,
                    cy=affine.cy,
                    dy=affine.dy,
                    map_id=map_id,
                    rmse=affine.rmse,
                    max_residual=affine.max_residual,
                    anchor_count=affine.anchor_count,
                    source=affine.source,
                )
        elif isinstance(req.get("src_anchors"), list) and isinstance(req.get("dst_anchors"), list):
            affine = fit_axis_affine(req["src_anchors"], req["dst_anchors"], map_id=map_id)
        else:
            affine = affine_registry.get(target_format, map_id, allow_default_seed=True)
            if affine is None and req.get("use_identity_affine"):
                affine = AxisAffine(ax=1.0, bx=0.0, cy=1.0, dy=0.0, map_id=map_id, source="identity")

        if affine is None:
            return {
                "ok": False,
                "format": target_format,
                "payload": None,
                "report": None,
                "error": t("export.unsupported_map", map_id=map_id, format=target_format),
            }

        try:
            max_dist = float(req.get("max_dist", 15.0))
        except (TypeError, ValueError):
            max_dist = 15.0
        include_ambiguous = bool(req.get("include_ambiguous", True))

        transfer = match_pins_one_to_one(
            source_pins,
            parsed_targets,
            affine=affine,
            max_dist=max_dist,
            missing_from_point_list=missing_ids,
        )

        existing_payload = req.get("existing_payload")
        if target_format == "kongying":
            exported_out: Any = KongyingAdapter.export(transfer, include_ambiguous=include_ambiguous)
            if existing_payload:
                exported_out = KongyingAdapter.merge_existing(existing_payload, exported_out)
        elif target_format == "appsample":
            exported_out = AppsampleAdapter.export(transfer, include_ambiguous=include_ambiguous)
            if existing_payload:
                exported_out = AppsampleAdapter.merge_existing(existing_payload, exported_out)
        else:
            if isinstance(existing_payload, str) and existing_payload.strip():
                exported_out = GenshinMapGithubAdapter.merge_into_encoded(
                    existing_payload,
                    transfer,
                    include_ambiguous=include_ambiguous,
                )
            else:
                exported_out = GenshinMapGithubAdapter.export(transfer, include_ambiguous=include_ambiguous)

        return {
            "ok": True,
            "format": target_format,
            "payload": exported_out,
            "report": transfer.summary_dict(),
        }

    def on_action_feedback(payload: dict) -> bool:
        if not config.SINGLE_MONITOR_MODE or not isinstance(payload, dict):
            return False
        status = str(payload.get("status", "") or "").strip().lower()
        title = str(payload.get("title", "") or "").strip()
        detail = str(payload.get("detail", "") or "").strip()
        rect = current_game_state.get("rect")

        if status == "marked":
            hud.show_toast(title or t("toast.marked.title", name="Pin"), detail, kind="success", duration_ms=2600, anchor_rect=rect)
            audio.play("success")
            return True
        if status == "already_marked":
            hud.show_toast(title or t("toast.already_marked.title"), detail, kind="info", duration_ms=2200, anchor_rect=rect)
            audio.play("info")
            return True
        if status == "synced":
            hud.show_toast(title or t("toast.synced.title"), detail, kind="info", duration_ms=1800, anchor_rect=rect)
            return True
        if status in ("no_nearby", "no_coords"):
            hud.show_toast(title or t("toast.no_nearby.title", radius=60), detail, kind="warning", duration_ms=2500, anchor_rect=rect)
            audio.play("warning")
            return True
        if status == "error":
            hud.show_toast(title or t("toast.error.generic", error=detail or "?"), detail, kind="error", duration_ms=2800, anchor_rect=rect)
            audio.play("error")
            return True
        return False

    def on_quick_switch() -> str:
        return WindowSwitcher.toggle_game_and_browser(config.WINDOW_TITLES)

    def get_hotkey_info():
        uipi_status = current_game_state.get("uipi_status")
        uipi_mismatch = bool(
            isinstance(uipi_status, dict)
            and (uipi_status.get("mismatch") or uipi_status.get("uipi_blocked"))
        )
        return {
            "hotkey": config.HOTKEY_MARK,
            "display_name": hotkey_mgr.get_display_name("sync"),
            "hotkey_sync": config.HOTKEY_MARK,
            "display_name_sync": hotkey_mgr.get_display_name("sync"),
            "hotkey_debug": config.HOTKEY_DEBUG,
            "display_name_debug": hotkey_mgr.get_display_name("debug"),
            "hotkey_switch": config.HOTKEY_SWITCH,
            "display_name_switch": hotkey_mgr.get_display_name("switch"),
            "auto_sync_m": config.AUTO_SYNC_MAP_M,
            "single_monitor_mode": config.SINGLE_MONITOR_MODE,
            "monitor_count": WindowFinder.get_monitor_count(),
            "engine_active": bool(engine_state["active"]),
            "language": get_language(),
            "language_setting": config.LANGUAGE,
            "minimap_tracking": bool(config.MINIMAP_TRACKING),
            "nav_hud_enabled": bool(config.NAV_HUD_ENABLED),
            "nav_hud_position": config.NAV_HUD_POSITION,
            "lan_companion_running": bool(lan_companion.is_running),
            "uipi_mismatch": uipi_mismatch,
            "uipi_status": uipi_status,
            "last_sync": server.last_sync_info,
        }

    def on_trigger_diag(browser_state: dict, note: str = ""):
        return handle_debug_action(
            server, locator, capturer, browser_state,
            trigger_source="web_ui", user_note=note, broadcast=False,
        )

    server.on_hotkey_change = on_hotkey_change
    server.get_hotkey_info = get_hotkey_info
    server.on_trigger_diagnostic = on_trigger_diag
    server.on_auto_sync_change = on_auto_sync_change
    server.on_single_monitor_change = on_single_monitor_change
    server.on_engine_state_change = set_engine_active
    server.on_action_feedback = on_action_feedback
    server.on_quick_switch = on_quick_switch
    server.on_language_change = on_language_change
    server.on_minimap_tracking_change = on_minimap_tracking_change
    server.on_nav_hud_change = on_nav_hud_change
    server.on_nearby_pins = on_nearby_pins
    server.on_nav_command = on_nav_command
    server.on_plan_tsp_route = on_plan_tsp_route
    server.on_lan_companion_action = on_lan_companion_action
    server.on_launch_borderless = on_launch_borderless
    server.on_cross_map_export = on_cross_map_export

    if not args.minimized:
        print("=" * 68)
        print("   GENSHIN IMPACT EXPLORATION & HOYOLAB AUTO-SYNC TOOL v5.3.0")
        print("   - Hybrid Minimap Live Tracker + 3-Anchor Map (M) State Machine")
        print("   - Mini-HUD Navigator + TSP Route Planner + Opt-In LAN Companion")
        print(f"   - [Engine State]         : [ {'ACTIVE' if engine_state['active'] else 'STANDBY'} ]")
        print(f"   - [Language]             : [ {get_language().upper()} ({config.LANGUAGE}) ]")
        print(f"   - [Auto-Sync Map (M)]    : [ {'ON' if config.AUTO_SYNC_MAP_M else 'OFF'} ]")
        print(f"   - [Minimap Tracking]     : [ {'ON' if config.MINIMAP_TRACKING else 'OFF'} ]")
        print(f"   - [Navigation Mini-HUD]  : [ {'ON' if config.NAV_HUD_ENABLED else 'OFF'} ({config.NAV_HUD_POSITION}) ]")
        print(f"   - [Single-Monitor Mode]  : [ {'ON' if config.SINGLE_MONITOR_MODE else 'OFF'} ]")
        print(f"   - [LAN Companion]        : [ {'ON' if lan_companion.is_running else 'OFF (opt-in)'} ]")
        print(f"   - [Sync & Mark Hotkey]   : [ {hotkey_mgr.get_display_name('sync')} ]")
        print(f"   - [Quick Switch Hotkey]  : [ {hotkey_mgr.get_display_name('switch')} ]")
        print(f"   - [Diagnostic Hotkey]    : [ {hotkey_mgr.get_display_name('debug')} ]")
        print("   -> Control settings, hotkeys & server power from the Extension Popup!")
        print("=" * 68)

    async def config_file_watcher(stop_event: asyncio.Event):
        last_mtime = os.path.getmtime(CONFIG_FILE_PATH) if os.path.exists(CONFIG_FILE_PATH) else 0
        tick_count = 0
        while not stop_event.is_set():
            await asyncio.sleep(1.0)
            tick_count += 1
            if tick_count % 2 == 0:
                _refresh_tray_state()
            try:
                if os.path.exists(CONFIG_FILE_PATH):
                    current_mtime = os.path.getmtime(CONFIG_FILE_PATH)
                    if current_mtime > last_mtime:
                        last_mtime = current_mtime
                        old_sync = config.HOTKEY_MARK
                        old_debug = config.HOTKEY_DEBUG
                        old_switch = config.HOTKEY_SWITCH
                        old_auto = config.AUTO_SYNC_MAP_M
                        old_single = config.SINGLE_MONITOR_MODE
                        old_lang = config.LANGUAGE
                        old_mm = config.MINIMAP_TRACKING
                        old_hud_en = config.NAV_HUD_ENABLED
                        old_hud_pos = config.NAV_HUD_POSITION
                        config.load_from_file()
                        if config.HOTKEY_MARK != old_sync:
                            logger.info(f"[Config Watcher] Detected config.json update: hotkey_sync='{config.HOTKEY_MARK}'")
                            on_hotkey_change("sync", config.HOTKEY_MARK)
                            server.broadcast({
                                "type": "HOTKEY_UPDATED",
                                "action": "sync",
                                "hotkey": config.HOTKEY_MARK,
                                "display_name": hotkey_mgr.get_display_name("sync")
                            })
                        if config.HOTKEY_DEBUG != old_debug:
                            logger.info(f"[Config Watcher] Detected config.json update: hotkey_debug='{config.HOTKEY_DEBUG}'")
                            on_hotkey_change("debug", config.HOTKEY_DEBUG)
                            server.broadcast({
                                "type": "HOTKEY_UPDATED",
                                "action": "debug",
                                "hotkey": config.HOTKEY_DEBUG,
                                "display_name": hotkey_mgr.get_display_name("debug")
                            })
                        if config.HOTKEY_SWITCH != old_switch:
                            logger.info(f"[Config Watcher] Detected config.json update: hotkey_switch='{config.HOTKEY_SWITCH}'")
                            on_hotkey_change("switch", config.HOTKEY_SWITCH)
                            server.broadcast({
                                "type": "HOTKEY_UPDATED",
                                "action": "switch",
                                "hotkey": config.HOTKEY_SWITCH,
                                "display_name": hotkey_mgr.get_display_name("switch")
                            })
                        if config.AUTO_SYNC_MAP_M != old_auto:
                            logger.info(f"[Config Watcher] Detected config.json update: auto_sync_m={config.AUTO_SYNC_MAP_M}")
                            if not config.AUTO_SYNC_MAP_M:
                                state_machine.reset()
                            server.broadcast({"type": "AUTO_SYNC_UPDATED", "auto_sync_m": config.AUTO_SYNC_MAP_M})
                        if config.SINGLE_MONITOR_MODE != old_single:
                            logger.info(f"[Config Watcher] Detected config.json update: single_monitor_mode={config.SINGLE_MONITOR_MODE}")
                            hotkey_mgr.set_enabled("switch", bool(config.SINGLE_MONITOR_MODE and engine_state["active"]))
                            server.broadcast({"type": "SINGLE_MONITOR_UPDATED", "single_monitor_mode": config.SINGLE_MONITOR_MODE})
                        if config.LANGUAGE != old_lang:
                            resolved_lang = set_language(config.LANGUAGE)
                            nav_hud.set_language(resolved_lang)
                            if tray is not None and hasattr(tray, "set_language"):
                                tray.set_language(resolved_lang)
                            server.broadcast({"type": "LANGUAGE_UPDATED", "language": config.LANGUAGE, "resolved": resolved_lang})
                        if config.MINIMAP_TRACKING != old_mm:
                            if not config.MINIMAP_TRACKING:
                                _pause_minimap_tracker(minimap_tracker, reason="disabled")
                                nav_hud.hide()
                            server.broadcast({
                                "type": "MINIMAP_TRACKING_UPDATED",
                                "enabled": config.MINIMAP_TRACKING,
                                "minimap_tracking": config.MINIMAP_TRACKING,
                            })
                        if config.NAV_HUD_ENABLED != old_hud_en or config.NAV_HUD_POSITION != old_hud_pos:
                            nav_hud.set_position_mode(config.NAV_HUD_POSITION)
                            if not config.NAV_HUD_ENABLED:
                                nav_hud.hide()
                            server.broadcast({
                                "type": "NAV_HUD_UPDATED",
                                "enabled": config.NAV_HUD_ENABLED,
                                "position": config.NAV_HUD_POSITION,
                                "nav_hud_enabled": config.NAV_HUD_ENABLED,
                                "nav_hud_position": config.NAV_HUD_POSITION,
                            })
            except Exception as e:
                logger.debug(f"Config watcher error: {e}")

    async def run_app():
        nonlocal tray
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        server.on_shutdown_request = stop_event.set
        await server.start()

        if not args.no_tray and sys.platform == "win32":
            def _tray_toggle():
                new_active = not engine_state["active"]
                set_engine_active(new_active)
                server.broadcast({"type": "ENGINE_STATE_UPDATED", "engine_active": new_active})

            def _tray_open_map():
                try:
                    webbrowser.open("https://act.hoyolab.com/ys/app/interactive-map/index.html#/map/2")
                except Exception:
                    pass

            def _tray_launch_borderless():
                on_launch_borderless({})

            def _tray_open_diag():
                try:
                    d = get_diagnostics_dir()
                    os.makedirs(d, exist_ok=True)
                    os.startfile(d)
                except Exception:
                    pass

            def _tray_open_cfg():
                try:
                    d = os.path.dirname(os.path.abspath(CONFIG_FILE_PATH))
                    os.makedirs(d, exist_ok=True)
                    os.startfile(d)
                except Exception:
                    pass

            def _tray_quit():
                try:
                    loop.call_soon_threadsafe(stop_event.set)
                except Exception:
                    pass

            tray_kwargs: Dict[str, Any] = {
                "on_toggle_engine": _tray_toggle,
                "on_open_map": _tray_open_map,
                "on_open_diagnostics": _tray_open_diag,
                "on_open_config_dir": _tray_open_cfg,
                "on_quit": _tray_quit,
                "initial_active": engine_state["active"],
            }
            tray_sig = inspect.signature(SystemTrayApp.__init__)
            if "on_launch_borderless" in tray_sig.parameters:
                tray_kwargs["on_launch_borderless"] = _tray_launch_borderless
            if "lang" in tray_sig.parameters:
                tray_kwargs["lang"] = get_language()

            tray = SystemTrayApp(**tray_kwargs)
            tray.on_launch_borderless = _tray_launch_borderless
            tray.start()
            _refresh_tray_state()

        watcher_task = asyncio.create_task(config_file_watcher(stop_event))
        try:
            await tracking_loop(
                server,
                capturer,
                locator,
                state_machine,
                stop_event,
                engine_state=engine_state,
                minimap_tracker=minimap_tracker,
                nav_controller=nav_controller,
                nav_hud=nav_hud,
                lan_companion=lan_companion,
                hud=hud,
                tray=tray,
                hotkey_mgr=hotkey_mgr,
                route_state=route_state,
            )
        except Exception as e:
            logger.exception(f"Exception in tracking loop: {e}")
        finally:
            stop_event.set()
            watcher_task.cancel()
            if tray is not None:
                tray.close()
                tray = None
            lan_companion.stop_background()
            nav_hud.close()
            await server.stop()
            hotkey_mgr.stop()
            audio.close()
            hud.close()
            capturer.close()
            for _prov in (hd_provider, ug_provider):
                try:
                    _prov.close()
                except Exception:
                    pass
            locator.close()

    try:
        asyncio.run(run_app())
    except KeyboardInterrupt:
        print("\nShutting down Genshin Helper...")
    except Exception as e:
        logger.exception(f"Fatal exception in main: {e}")
    finally:
        if mutex_handle and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(mutex_handle)
            except Exception:
                pass

if __name__ == "__main__":
    main()
