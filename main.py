import asyncio
import logging
import os
import sys
import time
from src.config import config, CONFIG_FILE_PATH
from src.capture import WindowFinder, ScreenCapturer
from src.locator import MapLocator
from src.server import SyncServer
from src.hotkey import HotkeyManager

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
    Called immediately when Alt+F11 is pressed:
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

def main():
    capturer = ScreenCapturer(mode=config.CAPTURE_MODE)
    locator = MapLocator()
    server = SyncServer(host=config.WS_HOST, port=config.WS_PORT)

    def on_hotkey():
        handle_hotkey_action(server, locator, capturer)

    hotkey_mgr = HotkeyManager(config.HOTKEY_MARK, on_hotkey)
    hotkey_mgr.start()

    def on_hotkey_change(new_key: str):
        success = hotkey_mgr.update_hotkey(new_key)
        if success:
            config.HOTKEY_MARK = new_key
            config.save_to_file()
            display_name = hotkey_mgr.get_display_name()
            logger.info(f"==> Global hotkey updated & saved: [{display_name}]")
            return True, display_name
        return False, hotkey_mgr.get_display_name()

    def get_hotkey_info():
        return {
            "hotkey": config.HOTKEY_MARK,
            "display_name": hotkey_mgr.get_display_name()
        }

    server.on_hotkey_change = on_hotkey_change
    server.get_hotkey_info = get_hotkey_info

    print("=" * 65)
    print("   GENSHIN IMPACT EXPLORATION & HOYOLAB AUTO-SYNC TOOL v3.0")
    print("   - Ultra-fast Landmark Matching Engine (<50ms)")
    print("   - Surface & Underground Multilayer Map Support")
    print(f"   - Current Hotkey: [ {hotkey_mgr.get_display_name()} ]")
    print("   -> Open Map (M) in game and trigger hotkey to Auto-Sync & Mark!")
    print("   -> You can change hotkey anytime via Web Map or config.json")
    print("=" * 65)

    async def config_file_watcher():
        last_mtime = os.path.getmtime(CONFIG_FILE_PATH) if os.path.exists(CONFIG_FILE_PATH) else 0
        while True:
            await asyncio.sleep(1.0)
            try:
                if os.path.exists(CONFIG_FILE_PATH):
                    current_mtime = os.path.getmtime(CONFIG_FILE_PATH)
                    if current_mtime > last_mtime:
                        last_mtime = current_mtime
                        old_key = config.HOTKEY_MARK
                        config.load_from_file()
                        if config.HOTKEY_MARK != old_key:
                            logger.info(f"[Config Watcher] Detected config.json update: hotkey='{config.HOTKEY_MARK}'")
                            on_hotkey_change(config.HOTKEY_MARK)
                            server.broadcast({
                                "type": "HOTKEY_UPDATED",
                                "hotkey": config.HOTKEY_MARK,
                                "display_name": hotkey_mgr.get_display_name()
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
