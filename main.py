import asyncio
import logging
import os
import sys
import time
from typing import Optional, Dict, Any
from src.config import config, CONFIG_FILE_PATH
from src.capture import WindowFinder, ScreenCapturer
from src.locator import MapLocator
from src.server import SyncServer
from src.hotkey import HotkeyManager
from src.diagnostics import DiagnosticManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("GenshinHelper.Main")

# Global state to share between tracking loop and hotkey trigger
current_game_state = {
    "hwnd": None,
    "rect": None,
    "last_pos": None,
}

async def tracking_loop(server: SyncServer, capturer: ScreenCapturer):
    logger.info("Tracking loop running. Waiting for Genshin Impact window...")
    window_attached = False

    last_res = None
    while server.is_running:
        win_info = WindowFinder.get_game_window(config.WINDOW_TITLES)
        if not win_info:
            if window_attached:
                logger.warning("Genshin Impact window lost. Waiting for window...")
                window_attached = False
                current_game_state["hwnd"] = None
                current_game_state["rect"] = None
                last_res = None
            await asyncio.sleep(1.0)
            continue

        hwnd, left, top, width, height = win_info
        current_game_state["hwnd"] = hwnd
        current_game_state["rect"] = (left, top, width, height)

        if not window_attached or last_res != (width, height):
            logger.info(f"Attached to Genshin Impact window (Resolution: {width}x{height})")
            window_attached = True
            last_res = (width, height)

        await asyncio.sleep(config.TRACK_INTERVAL)

def handle_hotkey_action(server: SyncServer, locator: MapLocator, capturer: ScreenCapturer):
    """
    Called immediately when Sync Hotkey (e.g. Alt+F11) is pressed:
    1. Captures full game window (Map M).
    2. Runs MapLocator to find player arrow & landmark waypoints.
    3. Calculates exact Teyvat world coordinates (x_pos, y_pos).
    4. Detects underground status and broadcasts to HoYoLAB Web Map!
    """
    try:
        logger.info(f"[{config.HOTKEY_MARK} Pressed] Capturing game screen for Map Sync...")
        rect = current_game_state.get("rect")
        if not rect or rect[2] < 1000 or rect[3] < 600:
            win_info = WindowFinder.get_game_window(config.WINDOW_TITLES)
            if win_info:
                rect = win_info[1:]
                current_game_state["rect"] = rect

        if not rect:
            logger.warning("Genshin window not detected. Broadcasting generic MARK request...")
            server.broadcast({"type": "SYNC_AND_MARK", "detected": False})
            return

        left, top, width, height = rect
        full_frame = capturer.capture_region(left, top, width, height)
        if full_frame is None:
            logger.warning("Failed to grab screen frame.")
            server.broadcast({"type": "SYNC_AND_MARK", "detected": False})
            return

        res = locator.locate_player(full_frame)
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
            server.broadcast({"type": "SYNC_AND_MARK", "detected": False})
    except Exception as e:
        logger.exception(f"Exception in handle_hotkey_action: {e}")

def handle_debug_action(server: SyncServer, locator: MapLocator, capturer: ScreenCapturer,
                        browser_state: Optional[dict] = None, trigger_source: str = "hotkey_debug") -> dict:
    """
    Called when Debug Hotkey (e.g. Alt+F12) or Web UI diagnostic button is triggered:
    1. Simultaneously captures Game + Browser windows.
    2. Runs Computer Vision analysis with deep telemetry.
    3. Produces annotated visual & JSON report bundle in scratch/diagnostics/.
    4. Auto-opens directory in Windows Explorer.
    """
    try:
        logger.info(f"📸 [{trigger_source.upper()}] Triggered Diagnostic Capture & Debug Bundle...")
        rect = current_game_state.get("rect")
        bundle = DiagnosticManager.capture_and_create_bundle(
            capturer=capturer,
            locator=locator,
            game_rect=rect,
            browser_state=browser_state or server.last_browser_state,
            trigger_source=trigger_source
        )
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

def main():
    capturer = ScreenCapturer(mode=config.CAPTURE_MODE)
    locator = MapLocator()
    server = SyncServer(host=config.WS_HOST, port=config.WS_PORT)

    def on_sync_hotkey():
        handle_hotkey_action(server, locator, capturer)

    def on_debug_hotkey():
        handle_debug_action(server, locator, capturer, trigger_source="hotkey_debug")

    # Unified Multi-Hotkey Manager for both Sync and Debug actions
    hotkey_mgr = HotkeyManager()
    hotkey_mgr.register("sync", config.HOTKEY_MARK, on_sync_hotkey)
    hotkey_mgr.register("debug", config.HOTKEY_DEBUG, on_debug_hotkey)
    hotkey_mgr.start()

    def on_hotkey_change(action: str, new_key: str):
        action = action.strip().lower()
        success = hotkey_mgr.update_binding(action, new_key)
        if success:
            if action == "sync":
                config.HOTKEY_MARK = new_key
            elif action == "debug":
                config.HOTKEY_DEBUG = new_key
            config.save_to_file()
            display_name = hotkey_mgr.get_display_name(action)
            logger.info(f"==> Hotkey [{action.upper()}] updated & saved: [{display_name}]")
            return True, display_name
        return False, hotkey_mgr.get_display_name(action)

    def get_hotkey_info():
        return {
            "hotkey": config.HOTKEY_MARK,
            "display_name": hotkey_mgr.get_display_name("sync"),
            "hotkey_sync": config.HOTKEY_MARK,
            "display_name_sync": hotkey_mgr.get_display_name("sync"),
            "hotkey_debug": config.HOTKEY_DEBUG,
            "display_name_debug": hotkey_mgr.get_display_name("debug")
        }

    def on_trigger_diag(browser_state: dict):
        return handle_debug_action(server, locator, capturer, browser_state, trigger_source="web_ui")

    server.on_hotkey_change = on_hotkey_change
    server.get_hotkey_info = get_hotkey_info
    server.on_trigger_diagnostic = on_trigger_diag

    print("=" * 68)
    print("   GENSHIN IMPACT EXPLORATION & HOYOLAB AUTO-SYNC TOOL v3.3")
    print("   - Ultra-fast Landmark Matching Engine (<50ms)")
    print("   - Surface & Underground Multilayer Map Support")
    print(f"   - [Sync & Mark Hotkey]   : [ {hotkey_mgr.get_display_name('sync')} ]")
    print(f"   - [Diagnostic Hotkey]    : [ {hotkey_mgr.get_display_name('debug')} ]")
    print("   -> Open Map (M) in game and trigger Sync hotkey to Auto-Sync & Mark!")
    print("   -> Press Diagnostic hotkey or click Settings ⚙️ to capture debug bundle.")
    print("=" * 68)

    async def config_file_watcher():
        last_mtime = os.path.getmtime(CONFIG_FILE_PATH) if os.path.exists(CONFIG_FILE_PATH) else 0
        while True:
            await asyncio.sleep(1.0)
            try:
                if os.path.exists(CONFIG_FILE_PATH):
                    current_mtime = os.path.getmtime(CONFIG_FILE_PATH)
                    if current_mtime > last_mtime:
                        last_mtime = current_mtime
                        old_sync = config.HOTKEY_MARK
                        old_debug = config.HOTKEY_DEBUG
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
            except Exception as e:
                logger.debug(f"Config watcher error: {e}")

    async def run_app():
        await server.start()
        watcher_task = asyncio.create_task(config_file_watcher())
        try:
            await tracking_loop(server, capturer)
        except Exception as e:
            logger.exception(f"Exception in tracking loop: {e}")
        finally:
            watcher_task.cancel()
            await server.stop()
            hotkey_mgr.stop()
            capturer.close()

    try:
        asyncio.run(run_app())
    except KeyboardInterrupt:
        print("\nShutting down Genshin Helper...")
    except Exception as e:
        logger.exception(f"Fatal exception in main: {e}")

if __name__ == "__main__":
    main()
