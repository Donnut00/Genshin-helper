import argparse
import asyncio
import ctypes
import logging
import math
import os
import sys
import time
import urllib.request
import webbrowser
from typing import Optional, Dict, Any
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("GenshinHelper.Main")

MUTEX_NAME = "Local\\GenshinHelper_SingleInstance_v4"

# Global state to share between tracking loop and hotkey trigger
current_game_state = {
    "hwnd": None,
    "rect": None,
    "last_pos": None,
}


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
):
    logger.info("Tracking loop running (v4.5 Map M FSM enabled). Waiting for Genshin Impact window...")
    window_attached = False
    last_res = None
    pending_verify = None

    while server.is_running and not stop_event.is_set():
        if engine_state is not None and not engine_state.get("active", True):
            if window_attached:
                window_attached = False
                last_res = None
            state_machine.reset()
            pending_verify = None
            await asyncio.sleep(0.35)
            continue

        win_info = WindowFinder.get_game_window(config.WINDOW_TITLES)
        if not win_info:
            if window_attached:
                logger.warning("Genshin Impact window lost. Waiting for window...")
                window_attached = False
                current_game_state["hwnd"] = None
                current_game_state["rect"] = None
                last_res = None
            state_machine.reset()
            pending_verify = None
            await asyncio.sleep(0.8)
            continue

        hwnd, left, top, width, height = win_info
        current_game_state["hwnd"] = hwnd
        current_game_state["rect"] = (left, top, width, height)

        if width < 800 or height < 500:
            state_machine.reset()
            pending_verify = None
            await asyncio.sleep(0.5)
            continue

        if not window_attached or last_res != (width, height):
            logger.info(f"Attached to Genshin Impact window (Resolution: {width}x{height})")
            window_attached = True
            last_res = (width, height)

        if not config.AUTO_SYNC_MAP_M:
            state_machine.reset()
            pending_verify = None
            await asyncio.sleep(0.5)
            continue

        is_fg = WindowFinder.is_window_foreground(hwnd)
        if not is_fg and state_machine.state != state_machine.STATE_SETTLING and pending_verify is None:
            await asyncio.sleep(0.25)
            continue

        def _capture_and_step(cl: int, ct: int, cw: int, ch: int):
            frame = capturer.capture_region(cl, ct, cw, ch)
            if frame is None:
                return None, None
            return frame, state_machine.step(frame)

        full_frame, step_res = await asyncio.to_thread(_capture_and_step, left, top, width, height)
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
                                    logger.info(
                                        f"[Auto-Sync M Verify] Refined Map {v_res.get('map_id')} -> "
                                        f"({v_res['lat']:.1f}, {v_res['lng']:.1f}) inl={v_res.get('inliers_count')}"
                                    )

        cur_state = state_machine.state
        sleep_s = 0.045 if cur_state == state_machine.STATE_SETTLING else (
            0.075 if cur_state == state_machine.STATE_CLOSED else 0.11
        )
        await asyncio.sleep(sleep_s)


def handle_hotkey_action(
    server: SyncServer,
    locator: MapLocator,
    capturer: ScreenCapturer,
    state_machine: Optional[MapScreenStateMachine] = None,
    hud: Optional[HudToastOverlay] = None,
    audio: Optional[AudioFeedback] = None,
):
    """
    Called immediately when Sync Hotkey (e.g. Alt+F11) is pressed:
    1. Captures full game window (Map M).
    2. Runs MapLocator to find player arrow, waypoints, and cave layer.
    3. Marks current open-Map state synced so FSM won't double-fire.
    4. Broadcasts SYNC_AND_MARK to HoYoLAB Web Map!
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
                    "Web Map chưa kết nối!",
                    "Hãy mở HoYoLAB Map trên trình duyệt",
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
                        "Chưa thấy cửa sổ Genshin",
                        "Mở game rồi nhấn lại phím đồng bộ",
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
                        "Không chụp được màn hình game",
                        "Hãy đưa game về Borderless Windowed",
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
                        "Chưa thấy mũi tên trên Map (M)",
                        "Mở Map (M) trong game rồi thử lại",
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

    mutex_handle = _acquire_single_instance_mutex(config.WS_PORT)
    capturer = ScreenCapturer(mode=config.CAPTURE_MODE)
    locator = MapLocator()
    state_machine = MapScreenStateMachine()
    server = SyncServer(host=config.WS_HOST, port=config.WS_PORT)
    audio = AudioFeedback()
    hud = HudToastOverlay()
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

    def on_sync_hotkey():
        if not engine_state["active"]:
            return
        handle_hotkey_action(server, locator, capturer, state_machine=state_machine, hud=hud, audio=audio)

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
                "Không tìm thấy trình duyệt HoYoLAB Map",
                "Hãy mở HoYoLAB Interactive Map trước",
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

    def set_engine_active(active: bool) -> bool:
        want = bool(active)
        engine_state["active"] = want
        hotkey_mgr.set_enabled("sync", want)
        hotkey_mgr.set_enabled("debug", want)
        hotkey_mgr.set_enabled("switch", bool(want and config.SINGLE_MONITOR_MODE))
        if not want:
            state_machine.reset()
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
                "Chế độ 1 Màn Hình: ĐÃ BẬT",
                f"Phím chuyển nhanh: {hotkey_mgr.get_display_name('switch')}",
                kind="info",
                duration_ms=2400,
                anchor_rect=rect,
            )
        return config.SINGLE_MONITOR_MODE

    def on_action_feedback(payload: dict) -> bool:
        if not config.SINGLE_MONITOR_MODE or not isinstance(payload, dict):
            return False
        status = str(payload.get("status", "") or "").strip().lower()
        title = str(payload.get("title", "") or "").strip()
        detail = str(payload.get("detail", "") or "").strip()
        rect = current_game_state.get("rect")

        if status == "marked":
            hud.show_toast(title or "Đã ghim điểm!", detail, kind="success", duration_ms=2600, anchor_rect=rect)
            audio.play("success")
            return True
        if status == "already_marked":
            hud.show_toast(title or "Điểm này đã được ghim", detail, kind="info", duration_ms=2200, anchor_rect=rect)
            audio.play("info")
            return True
        if status == "synced":
            hud.show_toast(title or "Đã đồng bộ vị trí", detail, kind="info", duration_ms=1800, anchor_rect=rect)
            return True
        if status in ("no_nearby", "no_coords"):
            hud.show_toast(title or "Không có điểm gần (< 60m)", detail, kind="warning", duration_ms=2500, anchor_rect=rect)
            audio.play("warning")
            return True
        if status == "error":
            hud.show_toast(title or "Lỗi đồng bộ", detail, kind="error", duration_ms=2800, anchor_rect=rect)
            audio.play("error")
            return True
        return False

    def on_quick_switch() -> str:
        return WindowSwitcher.toggle_game_and_browser(config.WINDOW_TITLES)

    def get_hotkey_info():
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

    if not args.minimized:
        print("=" * 68)
        print("   GENSHIN IMPACT EXPLORATION & HOYOLAB AUTO-SYNC TOOL v4.5.0")
        print("   - Visual 3-Anchor Map (M) State Machine + Terrain Alignment")
        print("   - Live Player Marker + Exact Cave & Sub-Map Floor Sync")
        print(f"   - [Engine State]         : [ {'ACTIVE' if engine_state['active'] else 'STANDBY'} ]")
        print(f"   - [Auto-Sync Map (M)]    : [ {'ON' if config.AUTO_SYNC_MAP_M else 'OFF'} ]")
        print(f"   - [Single-Monitor Mode]  : [ {'ON' if config.SINGLE_MONITOR_MODE else 'OFF'} ]")
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

            tray = SystemTrayApp(
                on_toggle_engine=_tray_toggle,
                on_open_map=_tray_open_map,
                on_open_diagnostics=_tray_open_diag,
                on_open_config_dir=_tray_open_cfg,
                on_quit=_tray_quit,
                initial_active=engine_state["active"],
            )
            tray.start()
            _refresh_tray_state()

        watcher_task = asyncio.create_task(config_file_watcher(stop_event))
        try:
            await tracking_loop(server, capturer, locator, state_machine, stop_event, engine_state=engine_state)
        except Exception as e:
            logger.exception(f"Exception in tracking loop: {e}")
        finally:
            stop_event.set()
            watcher_task.cancel()
            if tray is not None:
                tray.close()
                tray = None
            await server.stop()
            hotkey_mgr.stop()
            audio.close()
            hud.close()
            capturer.close()
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
