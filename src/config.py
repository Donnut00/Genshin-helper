import json
import os
from dataclasses import dataclass
from typing import Tuple

from src.paths import get_config_path

CONFIG_FILE_PATH = get_config_path()

@dataclass
class AppConfig:
    # Server configuration
    WS_HOST: str = "127.0.0.1"
    WS_PORT: int = 8765

    # Target window titles for Genshin Impact (global, VN, CN)
    WINDOW_TITLES: Tuple[str, ...] = (
        "Genshin Impact",
        "原神",
    )

    # Hotkey for marking nearest chest (Keyboard, Mouse, or Combos)
    # Examples: "mouse4", "mouse5", "middle", "alt+f11", "f10", "alt+mouse4"
    HOTKEY_MARK: str = "alt+f11"

    # Hotkey for diagnostic capture & debug bundle (captures game + browser + logs)
    HOTKEY_DEBUG: str = "alt+f12"

    # Hotkey for quick switching between Genshin Impact and HoYoLAB Web Map (Single-Monitor Mode)
    HOTKEY_SWITCH: str = "alt+q"

    # Capture and tracking settings
    TRACK_INTERVAL: float = 0.05  # 20 updates per second (50ms)
    CAPTURE_MODE: str = "auto"    # "bettercam", "mss", or "auto"

    # Minimap default relative ratios for 16:9 displays
    MINIMAP_CROP_RATIO: Tuple[float, float, float, float] = (
        0.018,  # left / width
        0.023,  # top / height
        0.110,  # box_width / width
        0.195   # box_height / height
    )

    # Nearest chest search radius (in Teyvat world units/meters)
    CHEST_SEARCH_RADIUS: float = 30.0

    # Auto-sync HoYoLAB web map once Map (M) opens & settles
    AUTO_SYNC_MAP_M: bool = True

    # Single-Monitor Mode optimizations (HUD toast, 3-tone audio, Alt+Q switch, anti-throttle)
    SINGLE_MONITOR_MODE: bool = False

    def load_from_file(self, filepath: str = CONFIG_FILE_PATH):
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "hotkey" in data and data["hotkey"]:
                    self.HOTKEY_MARK = str(data["hotkey"]).strip().lower()
                elif "hotkey_sync" in data and data["hotkey_sync"]:
                    self.HOTKEY_MARK = str(data["hotkey_sync"]).strip().lower()
                if "hotkey_debug" in data and data["hotkey_debug"]:
                    self.HOTKEY_DEBUG = str(data["hotkey_debug"]).strip().lower()
                if "hotkey_switch" in data and data["hotkey_switch"]:
                    self.HOTKEY_SWITCH = str(data["hotkey_switch"]).strip().lower()
                if "chest_search_radius" in data:
                    self.CHEST_SEARCH_RADIUS = float(data["chest_search_radius"])
                if "ws_port" in data:
                    self.WS_PORT = int(data["ws_port"])
                if "auto_sync_m" in data:
                    self.AUTO_SYNC_MAP_M = bool(data["auto_sync_m"])
                if "single_monitor_mode" in data:
                    self.SINGLE_MONITOR_MODE = bool(data["single_monitor_mode"])
            except Exception as e:
                print(f"[Config] Warning loading config.json: {e}")
        else:
            self.save_to_file(filepath)

    def save_to_file(self, filepath: str = CONFIG_FILE_PATH):
        try:
            data = {
                "hotkey": self.HOTKEY_MARK,
                "hotkey_sync": self.HOTKEY_MARK,
                "hotkey_debug": self.HOTKEY_DEBUG,
                "hotkey_switch": self.HOTKEY_SWITCH,
                "chest_search_radius": self.CHEST_SEARCH_RADIUS,
                "ws_port": self.WS_PORT,
                "auto_sync_m": self.AUTO_SYNC_MAP_M,
                "single_monitor_mode": self.SINGLE_MONITOR_MODE,
                "instructions": "Cấu hình phím tắt cho Genshin Helper. Hỗ trợ: mouse4, mouse5, middle, alt+f11, alt+f12, alt+q, f10, alt+mouse4, ctrl+q, v.v. Bạn có thể sửa trực tiếp tại đây hoặc click vào biểu tượng bánh răng trên Web Map."
            }
            parent_dir = os.path.dirname(os.path.abspath(filepath))
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[Config] Error saving config.json: {e}")

config = AppConfig()
config.load_from_file()
