import json
import os
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from src.image_io import write_json_atomic
from src.paths import get_config_path

APP_VERSION: str = "5.3.0"
SCRIPT_VERSION: str = f"v{APP_VERSION}"

CONFIG_FILE_PATH = get_config_path()

_VALID_LANGUAGES = frozenset({"auto", "vi", "en"})

# Named in-game navigation hotkey actions -> AppConfig attribute
NAV_HOTKEY_ATTRS: Dict[str, str] = {
    "nav_mark": "HOTKEY_NAV_MARK",
    "nav_undo": "HOTKEY_NAV_UNDO",
    "nav_guide": "HOTKEY_NAV_GUIDE",
    "nav_next": "HOTKEY_NAV_NEXT",
    "nav_layout": "HOTKEY_NAV_LAYOUT",
}


def _parse_custom_pos(raw) -> Optional[Tuple[float, float]]:
    try:
        if raw is None:
            return None
        fx, fy = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    return (fx, fy)

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

    # In-game navigation hotkeys (Mini-HUD target actions, no Alt+Tab needed)
    HOTKEY_NAV_MARK: str = "alt+g"          # Mark current HUD target as done (app + HoYoLAB web)
    HOTKEY_NAV_UNDO: str = "alt+shift+g"    # Undo the last in-game mark
    HOTKEY_NAV_GUIDE: str = "alt+h"         # Toggle the in-game Guide Card (image + notes)
    HOTKEY_NAV_NEXT: str = "alt+n"          # Switch to the next nearby target
    HOTKEY_NAV_LAYOUT: str = "alt+shift+h"  # Toggle Mini-HUD drag-to-move layout mode

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

    # v5 settings (backwards-compatible defaults)
    LANGUAGE: str = "auto"  # "auto" | "vi" | "en"
    MINIMAP_TRACKING: bool = True
    NAV_HUD_ENABLED: bool = True
    NAV_HUD_POSITION: str = "under_minimap"
    NAV_HUD_CUSTOM_POS: Optional[Tuple[float, float]] = None  # (fx, fy) of game window, "custom" mode
    LAN_COMPANION_ENABLED: bool = False
    LAN_COMPANION_PORT: int = 8766

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
                for action, attr in NAV_HOTKEY_ATTRS.items():
                    key = f"hotkey_{action}"
                    if key in data and data[key]:
                        setattr(self, attr, str(data[key]).strip().lower())
                if "chest_search_radius" in data:
                    self.CHEST_SEARCH_RADIUS = float(data["chest_search_radius"])
                if "ws_port" in data:
                    self.WS_PORT = int(data["ws_port"])
                if "auto_sync_m" in data:
                    self.AUTO_SYNC_MAP_M = bool(data["auto_sync_m"])
                if "single_monitor_mode" in data:
                    self.SINGLE_MONITOR_MODE = bool(data["single_monitor_mode"])
                if "language" in data and data["language"]:
                    lang = str(data["language"]).strip().lower()
                    self.LANGUAGE = lang if lang in _VALID_LANGUAGES else "auto"
                if "minimap_tracking" in data:
                    self.MINIMAP_TRACKING = bool(data["minimap_tracking"])
                if "nav_hud_enabled" in data:
                    self.NAV_HUD_ENABLED = bool(data["nav_hud_enabled"])
                if "nav_hud_position" in data and data["nav_hud_position"]:
                    self.NAV_HUD_POSITION = str(data["nav_hud_position"]).strip().lower()
                if "nav_hud_custom_pos" in data:
                    self.NAV_HUD_CUSTOM_POS = _parse_custom_pos(data["nav_hud_custom_pos"])
                if "lan_companion_enabled" in data:
                    self.LAN_COMPANION_ENABLED = bool(data["lan_companion_enabled"])
                if "lan_companion_port" in data:
                    port_val = int(data["lan_companion_port"])
                    if 1 <= port_val <= 65535:
                        self.LAN_COMPANION_PORT = port_val
            except Exception as e:
                print(f"[Config] Warning loading config.json: {e}")
        else:
            self.save_to_file(filepath)

    def save_to_file(self, filepath: str = CONFIG_FILE_PATH):
        try:
            lang = str(self.LANGUAGE).strip().lower() if self.LANGUAGE else "auto"
            if lang not in _VALID_LANGUAGES:
                lang = "auto"
            data = {
                "hotkey": self.HOTKEY_MARK,
                "hotkey_sync": self.HOTKEY_MARK,
                "hotkey_debug": self.HOTKEY_DEBUG,
                "hotkey_switch": self.HOTKEY_SWITCH,
                **{f"hotkey_{action}": getattr(self, attr) for action, attr in NAV_HOTKEY_ATTRS.items()},
                "chest_search_radius": self.CHEST_SEARCH_RADIUS,
                "ws_port": self.WS_PORT,
                "auto_sync_m": self.AUTO_SYNC_MAP_M,
                "single_monitor_mode": self.SINGLE_MONITOR_MODE,
                "language": lang,
                "minimap_tracking": bool(self.MINIMAP_TRACKING),
                "nav_hud_enabled": bool(self.NAV_HUD_ENABLED),
                "nav_hud_position": str(self.NAV_HUD_POSITION).strip().lower() or "under_minimap",
                "nav_hud_custom_pos": (
                    [round(float(self.NAV_HUD_CUSTOM_POS[0]), 4), round(float(self.NAV_HUD_CUSTOM_POS[1]), 4)]
                    if self.NAV_HUD_CUSTOM_POS is not None
                    else None
                ),
                "lan_companion_enabled": bool(self.LAN_COMPANION_ENABLED),
                "lan_companion_port": int(self.LAN_COMPANION_PORT),
                "instructions": "Cấu hình phím tắt cho Genshin Helper. Hỗ trợ: mouse4, mouse5, middle, alt+f11, alt+f12, alt+q, f10, alt+mouse4, ctrl+q, v.v. Bạn có thể sửa trực tiếp tại đây hoặc click vào biểu tượng bánh răng trên Web Map."
            }
            write_json_atomic(filepath, data, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[Config] Error saving config.json: {e}")

config = AppConfig()
config.load_from_file()
