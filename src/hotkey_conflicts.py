"""Smart Hotkey Conflict Detection, Safe Alternatives & NVIDIA Overlay Remapper."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
import struct
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from src.image_io import write_json_atomic
from src.paths import get_data_dir

if sys.platform == "win32":
    import winreg
else:  # pragma: no cover
    winreg = None

logger = logging.getLogger("GenshinHelper.HotkeyConflicts")

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

MODIFIER_VKS: Set[int] = {
    VK_SHIFT, VK_CONTROL, VK_MENU,
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,  # L/R Shift, Ctrl, Alt
    0x5B, 0x5C,  # L/R Win
}

MOUSE_BUTTON_MAP: Dict[str, str] = {
    "mouse4": "mouse4",
    "mouse_4": "mouse4",
    "xbutton1": "mouse4",
    "xbtn1": "mouse4",
    "mouse_back": "mouse4",
    "btn4": "mouse4",
    "mouse5": "mouse5",
    "mouse_5": "mouse5",
    "xbutton2": "mouse5",
    "xbtn2": "mouse5",
    "mouse_forward": "mouse5",
    "btn5": "mouse5",
    "middle": "middle",
    "mouse_middle": "middle",
    "mbutton": "middle",
    "wheel": "middle",
    "wheel_click": "middle",
    "btn3": "middle",
    "right": "right",
    "mouse_right": "right",
    "rbutton": "right",
}

KEY_TO_VK: Dict[str, int] = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "arrowup": 0x26,
    "down": 0x28,
    "arrowdown": 0x28,
    "left": 0x25,
    "arrowleft": 0x25,
    "arrowright": 0x27,
    "capslock": 0x14,
    "pause": 0x13,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "`": 0xC0,
    "grave": 0xC0,
    "tilde": 0xC0,
    "backquote": 0xC0,
    "-": 0xBD,
    "minus": 0xBD,
    "=": 0xBB,
    "equal": 0xBB,
    "plus": 0xBB,
    "[": 0xDB,
    "bracketleft": 0xDB,
    "]": 0xDD,
    "bracketright": 0xDD,
    "\\": 0xDC,
    "backslash": 0xDC,
    ";": 0xBA,
    "semicolon": 0xBA,
    "'": 0xDE,
    "quote": 0xDE,
    ",": 0xBC,
    "comma": 0xBC,
    ".": 0xBE,
    "period": 0xBE,
    "/": 0xBF,
    "slash": 0xBF,
    "numpadmultiply": 0x6A,
    "numpadadd": 0x6B,
    "numpadsubtract": 0x6D,
    "numpaddecimal": 0x6E,
    "numpaddivide": 0x6F,
}
for _c in range(ord("a"), ord("z") + 1):
    KEY_TO_VK[chr(_c)] = _c - 32
for _d in range(0, 10):
    KEY_TO_VK[str(_d)] = 0x30 + _d
    KEY_TO_VK[f"numpad{_d}"] = 0x60 + _d
for _fn in range(1, 25):
    KEY_TO_VK[f"f{_fn}"] = 0x70 + (_fn - 1)

VK_TO_KEY: Dict[int, str] = {
    0x20: "space",
    0x09: "tab",
    0x0D: "enter",
    0x1B: "esc",
    0x08: "backspace",
    0x2D: "insert",
    0x2E: "delete",
    0x24: "home",
    0x23: "end",
    0x21: "pageup",
    0x22: "pagedown",
    0x26: "up",
    0x28: "down",
    0x25: "left",
    0x27: "arrowright",
    0x14: "capslock",
    0x13: "pause",
    0x2C: "printscreen",
    0xC0: "`",
    0xBD: "-",
    0xBB: "=",
    0xDB: "[",
    0xDD: "]",
    0xDC: "\\",
    0xBA: ";",
    0xDE: "'",
    0xBC: ",",
    0xBE: ".",
    0xBF: "/",
    0x6A: "numpadmultiply",
    0x6B: "numpadadd",
    0x6D: "numpadsubtract",
    0x6E: "numpaddecimal",
    0x6F: "numpaddivide",
}
for _c in range(ord("A"), ord("Z") + 1):
    VK_TO_KEY[_c] = chr(_c).lower()
for _d in range(0, 10):
    VK_TO_KEY[0x30 + _d] = str(_d)
    VK_TO_KEY[0x60 + _d] = f"numpad{_d}"
for _fn in range(1, 25):
    VK_TO_KEY[0x70 + (_fn - 1)] = f"f{_fn}"


# Win32 explicit 64-bit prototypes for hotkey probing & process inspection
if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL

    TH32CS_SNAPPROCESS = 0x00000002

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover
    _user32 = None
    _kernel32 = None


def parse_hotkey_parts(hotkey_str: str) -> Tuple[bool, Optional[str], Set[str], str]:
    """Parses a hotkey string into `(is_mouse, mouse_btn, modifiers, main_key)`."""
    raw = str(hotkey_str or "").strip().lower()
    if not raw:
        return False, None, set(), ""
    tokens = [p.strip() for p in raw.replace("+", " ").replace("-", " ").split() if p.strip()]
    modifiers: Set[str] = set()
    main_key = ""
    is_mouse = False
    mouse_btn: Optional[str] = None
    for tok in tokens:
        if tok in ("ctrl", "control"):
            modifiers.add("ctrl")
        elif tok in ("alt", "menu"):
            modifiers.add("alt")
        elif tok == "shift":
            modifiers.add("shift")
        elif tok in MOUSE_BUTTON_MAP:
            is_mouse = True
            mouse_btn = MOUSE_BUTTON_MAP[tok]
            main_key = mouse_btn
        else:
            main_key = tok
    return is_mouse, mouse_btn, modifiers, main_key


def normalize_hotkey(hotkey_str: str) -> str:
    """Returns canonical modifier order `ctrl+alt+shift+<key>`."""
    is_mouse, mouse_btn, modifiers, main_key = parse_hotkey_parts(hotkey_str)
    parts: List[str] = []
    for mod in ("ctrl", "alt", "shift"):
        if mod in modifiers:
            parts.append(mod)
    key = mouse_btn if is_mouse and mouse_btn else main_key
    if key:
        parts.append(key)
    return "+".join(parts)


def is_mouse_hotkey(hotkey_str: str) -> bool:
    return parse_hotkey_parts(hotkey_str)[0]


def format_display_name(hotkey_str: str) -> str:
    """Formats a human-readable hotkey label."""
    is_mouse, mouse_btn, modifiers, main_key = parse_hotkey_parts(hotkey_str)
    parts: List[str] = []
    if "ctrl" in modifiers:
        parts.append("Ctrl")
    if "alt" in modifiers:
        parts.append("Alt")
    if "shift" in modifiers:
        parts.append("Shift")
    if is_mouse:
        mouse_labels = {
            "mouse4": "Mouse 4",
            "mouse5": "Mouse 5",
            "middle": "Middle",
            "right": "Right",
        }
        parts.append(mouse_labels.get(mouse_btn or "", (mouse_btn or "").upper()))
    elif main_key:
        parts.append(main_key.upper() if len(main_key) <= 4 else main_key.title())
    return " + ".join(parts) if parts else str(hotkey_str or "").upper()


def hotkey_to_vk_combo(hotkey_str: str) -> Optional[Tuple[int, Set[str]]]:
    """Converts a keyboard hotkey into `(vk_code, modifiers)` or None if mouse/unknown."""
    is_mouse, _, modifiers, main_key = parse_hotkey_parts(hotkey_str)
    if is_mouse or not main_key:
        return None
    vk = KEY_TO_VK.get(main_key)
    if vk is None and len(main_key) == 1 and main_key.isascii():
        vk = ord(main_key.upper())
    if vk is None or vk in MODIFIER_VKS:
        return None
    return vk, set(modifiers)


def hotkey_to_vk_sequence(hotkey_str: str) -> Optional[List[int]]:
    """Converts a keyboard hotkey into an ordered VK list (`[17?, 18?, 16?, main_vk]`)."""
    combo = hotkey_to_vk_combo(hotkey_str)
    if combo is None:
        return None
    vk, mods = combo
    seq: List[int] = []
    if "ctrl" in mods:
        seq.append(VK_CONTROL)
    if "alt" in mods:
        seq.append(VK_MENU)
    if "shift" in mods:
        seq.append(VK_SHIFT)
    seq.append(int(vk))
    return seq


def vk_sequence_to_hotkey(vks: Sequence[int]) -> str:
    """Converts a Windows VK list (e.g. `[18, 90]`) into canonical hotkey string (`alt+z`)."""
    mods: Set[str] = set()
    main_key = ""
    for raw in vks:
        try:
            vk = int(raw)
        except (TypeError, ValueError):
            continue
        if vk in (VK_CONTROL, 0xA2, 0xA3):
            mods.add("ctrl")
        elif vk in (VK_MENU, 0xA4, 0xA5):
            mods.add("alt")
        elif vk in (VK_SHIFT, 0xA0, 0xA1):
            mods.add("shift")
        elif vk in VK_TO_KEY:
            main_key = VK_TO_KEY[vk]
    parts: List[str] = [m for m in ("ctrl", "alt", "shift") if m in mods]
    if main_key:
        parts.append(main_key)
    return "+".join(parts)


# Genshin PC controls triggered even with Alt/Shift/Ctrl modifiers held (user confirmed G & Alt+Shift+G)
_GENSHIN_ALWAYS_TRIGGERED_KEYS: Dict[str, Tuple[str, str]] = {
    "g": ("Giáo trình / Hướng dẫn", "Tutorials / Archive"),
    "m": ("Bản đồ (Map)", "World Map"),
    "j": ("Nhật ký Nhiệm vụ", "Quest Menu"),
    "b": ("Túi đồ", "Inventory"),
    "c": ("Nhân vật", "Character Menu"),
    "l": ("Thiết lập Đội hình", "Party Setup"),
    "o": ("Bạn bè / Co-op", "Friends / Co-op"),
    "p": ("Thoát Khiêu chiến / Bí cảnh", "Leave Challenge / Domain"),
    "u": ("Sổ Thám Hiểm / Bí Cảnh", "Adventurer Handbook / Domain"),
    "y": ("Thông báo / Thư", "Notices / Mail"),
    "v": ("Điều hướng Nhiệm vụ", "Navigate Objective"),
    "z": ("Đạo cụ nhỏ (Gadget)", "Quick Gadget"),
    "f1": ("Bằng Chứng Mạo Hiểm", "Adventurer Handbook"),
    "f2": ("Chế độ Nhiều Người Chơi", "Co-Op Mode"),
    "f3": ("Cầu Nguyện (Wish)", "Wish Menu"),
    "f4": ("Nhật Ký Hành Trình", "Battle Pass"),
    "f5": ("Sự Kiện", "Events Overview"),
    "1": ("Đổi nhân vật #1 (Alt+1 = Kỹ năng Nộ)", "Party Slot 1 (Alt+1 = Burst)"),
    "2": ("Đổi nhân vật #2 (Alt+2 = Kỹ năng Nộ)", "Party Slot 2 (Alt+2 = Burst)"),
    "3": ("Đổi nhân vật #3 (Alt+3 = Kỹ năng Nộ)", "Party Slot 3 (Alt+3 = Burst)"),
    "4": ("Đổi nhân vật #4 (Alt+4 = Kỹ năng Nộ)", "Party Slot 4 (Alt+4 = Burst)"),
    "5": ("Đổi nhân vật #5 (Alt+5 = Kỹ năng Nộ)", "Party Slot 5 (Alt+5 = Burst)"),
}

# Genshin bare combat/interaction keys (safe with Alt/Ctrl unless bare or Shift-only)
_GENSHIN_COMBAT_KEYS: Dict[str, Tuple[str, str]] = {
    "w": ("Di chuyển Tiến", "Move Forward"),
    "a": ("Di chuyển Trái", "Move Left"),
    "s": ("Di chuyển Lùi", "Move Backward"),
    "d": ("Di chuyển Phải", "Move Right"),
    "e": ("Kỹ năng Nguyên Tố", "Elemental Skill"),
    "q": ("Kỹ năng Nộ", "Elemental Burst"),
    "r": ("Ngắm bắn", "Aim Mode"),
    "f": ("Tương tác / Nhặt đồ", "Interact / Pick Up"),
    "t": ("Tương tác Đặc biệt", "Special Interaction"),
    "x": ("Thả cánh bay", "Drop / Cancel Glide"),
    "space": ("Nhảy / Bung cánh", "Jump / Glide"),
    "tab": ("Bánh xe Phím tắt", "Shortcut Wheel"),
    "esc": ("Menu Paimon", "Paimon Menu"),
}

_SAFE_SUGGESTION_POOL: Tuple[str, ...] = (
    "alt+k",
    "alt+shift+k",
    "alt+h",
    "alt+n",
    "alt+shift+h",
    "alt+x",
    "alt+shift+n",
    "ctrl+shift+k",
    "ctrl+shift+h",
    "ctrl+shift+n",
    "f6",
    "f7",
    "f8",
    "f9",
    "f10",
    "alt+f11",
    "alt+f12",
    "ctrl+shift+f6",
    "ctrl+shift+f7",
    "ctrl+shift+f8",
    "mouse4",
    "mouse5",
    "alt+mouse4",
    "alt+mouse5",
)

_PROBE_HOTKEY_ID = 0x7A4F


def probe_system_hotkey_registered(hotkey_str: str) -> bool:
    """
    Probes Windows via `RegisterHotKey` + immediate `UnregisterHotKey`.
    Returns True only when another application already holds this global hotkey (`1409`).
    """
    if sys.platform != "win32" or _user32 is None:
        return False
    combo = hotkey_to_vk_combo(hotkey_str)
    if combo is None:
        return False
    vk, mods = combo
    mod_flags = MOD_NOREPEAT
    if "alt" in mods:
        mod_flags |= MOD_ALT
    if "ctrl" in mods:
        mod_flags |= MOD_CONTROL
    if "shift" in mods:
        mod_flags |= MOD_SHIFT

    ctypes.set_last_error(0)
    ok = bool(_user32.RegisterHotKey(None, _PROBE_HOTKEY_ID, mod_flags, vk))
    if ok:
        _user32.UnregisterHotKey(None, _PROBE_HOTKEY_ID)
        return False
    return int(ctypes.get_last_error()) == ERROR_HOTKEY_ALREADY_REGISTERED


def get_running_process_names() -> Set[str]:
    """Returns lowercase running executable basenames (`nvidia overlay.exe`, `discord.exe`, etc.)."""
    out: Set[str] = set()
    if sys.platform != "win32" or _kernel32 is None:
        return out
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                name = str(entry.szExeFile or "").strip().lower()
                if name:
                    out.add(name)
                if not _kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    except Exception:
        pass
    finally:
        _kernel32.CloseHandle(snap)
    return out


_NVIDIA_SHORTCUT_META: Dict[str, Tuple[str, str, str]] = {
    "OpenIGO": ("GFEOverlayHKeyV2", "Mở NVIDIA Overlay", "Open NVIDIA Overlay"),
    "Screenshot": ("ScreenshotHKey", "Chụp ảnh (NVIDIA)", "NVIDIA Screenshot"),
    "PMOCOverlay": ("PMOCOverlayHKey", "Bảng thông số FPS (NVIDIA)", "NVIDIA Statistics Overlay"),
    "OpenFreestyle": ("FSUIHKeyV1", "Bộ lọc game Freestyle", "NVIDIA Game Filter"),
    "RecordToggle": ("ManualHKey", "Bật/Tắt quay màn hình (NVIDIA)", "Toggle Video Recording"),
    "OpenAnsel": ("AnselUIHKey", "Chế độ ảnh Ansel", "NVIDIA Photo Mode"),
    "DVRSave": ("DVRHKey", "Lưu Instant Replay", "Save Instant Replay"),
    "DVRToggle": ("IRToggleHKey", "Bật/Tắt Instant Replay", "Toggle Instant Replay"),
    "MicToggle": ("MicToggleHKey", "Bật/Tắt Micro (NVIDIA)", "Toggle Microphone (NVIDIA)"),
    "PMOCOverlayCycle": ("PMOCOverlayCycleHKey", "Đổi chế độ thông số", "Cycle Statistics View"),
    "ChatUIToggle": ("ChatUIToggleHKey", "Khung Chat Broadcast", "Broadcast Chat UI"),
    "ChatPTT": ("ChatPTTHKey", "Nhấn để nói (NVIDIA)", "Push-To-Talk (NVIDIA)"),
    "FreestylePresentCycle": ("FSPresetHKey", "Đổi Preset Freestyle", "Cycle Filter Preset"),
}

_KNOWN_OVERLAY_DEFAULTS: List[Tuple[str, str, str, Tuple[str, ...], str, str]] = [
    ("amd_adrenalin", "AMD Software Overlay", "alt+r", ("amddow.exe", "radeonsoftware.exe"), "Bật/Tắt Overlay AMD", "Toggle AMD Overlay"),
    ("amd_adrenalin", "AMD Sidebar Overlay", "alt+z", ("amddow.exe", "radeonsoftware.exe"), "Mở thanh bên AMD", "Open AMD Sidebar"),
    ("amd_adrenalin", "AMD Performance Metrics", "ctrl+shift+o", ("amddow.exe", "radeonsoftware.exe"), "Bảng thông số AMD", "AMD Metrics Overlay"),
    ("discord", "Discord Overlay", "shift+`", ("discord.exe",), "Mở Discord Game Overlay", "Toggle Discord Overlay"),
    ("steam", "Steam Overlay", "shift+tab", ("steam.exe", "gameoverlayui.exe"), "Mở Steam Overlay", "Open Steam Overlay"),
]


class NvidiaOverlayAdapter:
    """
    Reads, safely remaps (with automatic atomic backup), and restores NVIDIA Overlay shortcuts
    in `%LOCALAPPDATA%\\NVIDIA Corporation\\NVIDIA Overlay\\ShareSettings.json` and
    `HKCU\\Software\\NVIDIA Corporation\\Global\\ShadowPlay\\NVSPCAPS`.
    """

    NVSPCAPS_SUBKEY = r"Software\NVIDIA Corporation\Global\ShadowPlay\NVSPCAPS"

    def __init__(
        self,
        *,
        share_settings_path: Optional[str] = None,
        backup_path: Optional[str] = None,
        write_registry: bool = True,
    ) -> None:
        local_app = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        self.share_settings_path = share_settings_path or os.path.join(
            local_app, "NVIDIA Corporation", "NVIDIA Overlay", "ShareSettings.json"
        )
        self.backup_path = backup_path or os.path.join(
            get_data_dir(), "external_hotkey_backups.json"
        )
        self.write_registry = bool(write_registry and sys.platform == "win32" and winreg is not None)

    def _read_share_json(self) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.share_settings_path):
            return None
        try:
            with open(self.share_settings_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"Failed to read NVIDIA ShareSettings.json: {e}")
            return None

    def _read_registry_vk(self, reg_prefix: str) -> Optional[List[int]]:
        if not self.write_registry or not reg_prefix or winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.NVSPCAPS_SUBKEY, 0, winreg.KEY_READ) as key:
                raw_cnt, _ = winreg.QueryValueEx(key, f"{reg_prefix}Count")
                if not isinstance(raw_cnt, (bytes, bytearray)) or len(raw_cnt) < 4:
                    return None
                count = struct.unpack("<I", raw_cnt[:4])[0]
                if count == 0:
                    return []
                vks: List[int] = []
                for idx in range(min(int(count), 6)):
                    raw_vk, _ = winreg.QueryValueEx(key, f"{reg_prefix}{idx}")
                    if isinstance(raw_vk, (bytes, bytearray)) and len(raw_vk) >= 4:
                        vks.append(struct.unpack("<I", raw_vk[:4])[0])
                return vks
        except OSError:
            return None

    def _write_registry_vk(self, reg_prefix: str, vks: Sequence[int]) -> bool:
        if not self.write_registry or not reg_prefix or winreg is None:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                self.NVSPCAPS_SUBKEY,
                0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            ) as key:
                winreg.SetValueEx(key, f"{reg_prefix}Count", 0, winreg.REG_BINARY, struct.pack("<I", len(vks)))
                for idx, vk in enumerate(vks):
                    winreg.SetValueEx(key, f"{reg_prefix}{idx}", 0, winreg.REG_BINARY, struct.pack("<I", int(vk)))
                for extra_idx in range(len(vks), 8):
                    try:
                        winreg.DeleteValue(key, f"{reg_prefix}{extra_idx}")
                    except OSError:
                        pass
            return True
        except OSError as e:
            logger.debug(f"Failed to update NVIDIA NVSPCAPS {reg_prefix}: {e}")
            return False

    def read_shortcuts(self) -> List[Dict[str, Any]]:
        """Returns active NVIDIA Overlay shortcuts as normalized metadata dicts."""
        by_id: Dict[str, List[int]] = {}
        data = self._read_share_json()
        raw_sc = (data or {}).get("settings", {}).get("shortcuts", {}) if isinstance(data, dict) else {}
        if isinstance(raw_sc, dict):
            for sid, vks in raw_sc.items():
                if isinstance(vks, list) and vks:
                    by_id[sid] = [int(v) for v in vks]

        if self.write_registry:
            for sid, (reg_prefix, _vi, _en) in _NVIDIA_SHORTCUT_META.items():
                if sid not in by_id:
                    reg_vks = self._read_registry_vk(reg_prefix)
                    if reg_vks:
                        by_id[sid] = reg_vks

        items: List[Dict[str, Any]] = []
        for sid, vks in by_id.items():
            hk = vk_sequence_to_hotkey(vks)
            if not hk:
                continue
            reg_prefix, desc_vi, desc_en = _NVIDIA_SHORTCUT_META.get(sid, ("", sid, sid))
            items.append({
                "app_id": "nvidia_overlay",
                "app_name": "NVIDIA Overlay",
                "shortcut_id": sid,
                "reg_prefix": reg_prefix,
                "hotkey": hk,
                "display_name": format_display_name(hk),
                "vks": list(vks),
                "desc_vi": desc_vi,
                "desc_en": desc_en,
            })
        return items

    def _load_backups(self) -> Dict[str, Any]:
        if not os.path.isfile(self.backup_path):
            return {"version": 1, "items": {}}
        try:
            with open(self.backup_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("items"), dict):
                return data
        except Exception:
            pass
        return {"version": 1, "items": {}}

    def _save_backups(self, store: Dict[str, Any]) -> None:
        parent = os.path.dirname(os.path.abspath(self.backup_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        write_json_atomic(self.backup_path, store, indent=2, ensure_ascii=False)

    def list_backups(self) -> List[Dict[str, Any]]:
        store = self._load_backups()
        out: List[Dict[str, Any]] = []
        for key, entry in store.get("items", {}).items():
            if not isinstance(entry, dict):
                continue
            orig_hk = str(entry.get("original_hotkey") or "")
            new_hk = str(entry.get("current_hotkey") or "")
            out.append({
                "key": key,
                "app_id": entry.get("app_id", "nvidia_overlay"),
                "app_name": entry.get("app_name", "NVIDIA Overlay"),
                "shortcut_id": entry.get("shortcut_id", ""),
                "desc_vi": entry.get("desc_vi", entry.get("shortcut_id", "")),
                "desc_en": entry.get("desc_en", entry.get("shortcut_id", "")),
                "original_hotkey": orig_hk,
                "original_display": format_display_name(orig_hk),
                "current_hotkey": new_hk,
                "current_display": format_display_name(new_hk),
            })
        return out

    def remap_shortcut(self, shortcut_id: str, new_hotkey: str) -> Dict[str, Any]:
        """Safely remaps an NVIDIA Overlay shortcut with backup for 1-click undo."""
        vks = hotkey_to_vk_sequence(new_hotkey)
        if not vks:
            return {"ok": False, "error": f"Unsupported keyboard hotkey: {new_hotkey!r}"}
        norm_new = normalize_hotkey(new_hotkey)

        data = self._read_share_json()
        if data is None and not self.write_registry:
            return {"ok": False, "error": "NVIDIA Overlay ShareSettings.json not found."}

        reg_prefix, desc_vi, desc_en = _NVIDIA_SHORTCUT_META.get(shortcut_id, ("", shortcut_id, shortcut_id))
        old_vks: Optional[List[int]] = None
        if isinstance(data, dict):
            sc = data.get("settings", {}).get("shortcuts", {})
            if isinstance(sc, dict) and isinstance(sc.get(shortcut_id), list):
                old_vks = [int(v) for v in sc[shortcut_id]]
        if old_vks is None and reg_prefix and self.write_registry:
            old_vks = self._read_registry_vk(reg_prefix)
        if not old_vks:
            return {"ok": False, "error": f"NVIDIA shortcut {shortcut_id!r} not found."}

        backup_store = self._load_backups()
        backup_key = f"nvidia_overlay:{shortcut_id}"
        items = backup_store.setdefault("items", {})
        if backup_key not in items:
            old_hk = vk_sequence_to_hotkey(old_vks)
            items[backup_key] = {
                "app_id": "nvidia_overlay",
                "app_name": "NVIDIA Overlay",
                "shortcut_id": shortcut_id,
                "desc_vi": desc_vi,
                "desc_en": desc_en,
                "original_vks": list(old_vks),
                "original_hotkey": old_hk,
                "current_vks": list(vks),
                "current_hotkey": norm_new,
            }
        else:
            items[backup_key]["current_vks"] = list(vks)
            items[backup_key]["current_hotkey"] = norm_new
        self._save_backups(backup_store)

        json_written = False
        if isinstance(data, dict):
            settings = data.setdefault("settings", {})
            shortcuts = settings.setdefault("shortcuts", {})
            shortcuts[shortcut_id] = list(vks)
            write_json_atomic(self.share_settings_path, data, indent=2, ensure_ascii=False)
            json_written = True

        reg_written = self._write_registry_vk(reg_prefix, vks) if reg_prefix else False
        if not json_written and not reg_written:
            return {"ok": False, "error": "Failed to write NVIDIA Overlay configuration."}

        return {
            "ok": True,
            "app_id": "nvidia_overlay",
            "shortcut_id": shortcut_id,
            "new_hotkey": norm_new,
            "new_display": format_display_name(norm_new),
            "original_hotkey": items[backup_key]["original_hotkey"],
            "original_display": format_display_name(items[backup_key]["original_hotkey"]),
            "restart_required": True,
        }

    def restore_shortcut(self, shortcut_id: str) -> Dict[str, Any]:
        """Restores a previously backed-up NVIDIA Overlay shortcut."""
        backup_store = self._load_backups()
        backup_key = f"nvidia_overlay:{shortcut_id}"
        items = backup_store.get("items", {})
        entry = items.get(backup_key)
        if not isinstance(entry, dict) or not isinstance(entry.get("original_vks"), list):
            return {"ok": False, "error": f"No backup recorded for {shortcut_id!r}."}

        orig_vks = [int(v) for v in entry["original_vks"]]
        orig_hk = str(entry.get("original_hotkey") or vk_sequence_to_hotkey(orig_vks))
        reg_prefix = _NVIDIA_SHORTCUT_META.get(shortcut_id, ("", "", ""))[0]

        data = self._read_share_json()
        json_written = False
        if isinstance(data, dict):
            shortcuts = data.setdefault("settings", {}).setdefault("shortcuts", {})
            shortcuts[shortcut_id] = list(orig_vks)
            write_json_atomic(self.share_settings_path, data, indent=2, ensure_ascii=False)
            json_written = True

        reg_written = self._write_registry_vk(reg_prefix, orig_vks) if reg_prefix else False
        if not json_written and not reg_written:
            return {"ok": False, "error": "Failed to restore NVIDIA Overlay configuration."}

        items.pop(backup_key, None)
        self._save_backups(backup_store)
        return {
            "ok": True,
            "app_id": "nvidia_overlay",
            "shortcut_id": shortcut_id,
            "restored_hotkey": orig_hk,
            "restored_display": format_display_name(orig_hk),
            "restart_required": True,
        }


class HotkeyConflictAdvisor:
    """
    3-layer conflict engine:
      1. Internal duplicate check across Genshin-Helper actions.
      2. Genshin Impact PC keybind conflict check (catches modifier+menu keys like Alt+G / Alt+Shift+G).
      3. External app / OS global hotkey conflict check (`RegisterHotKey` + NVIDIA Overlay + running overlays),
         plus verified-clean suggestions and 1-click NVIDIA remap/restore.
    """

    def __init__(
        self,
        *,
        nvidia_adapter: Optional[NvidiaOverlayAdapter] = None,
        probe_fn: Optional[Callable[[str], bool]] = None,
        proc_fn: Optional[Callable[[], Set[str]]] = None,
    ) -> None:
        self.nvidia = nvidia_adapter if nvidia_adapter is not None else NvidiaOverlayAdapter()
        self._probe_fn = probe_fn if probe_fn is not None else probe_system_hotkey_registered
        self._proc_fn = proc_fn if proc_fn is not None else get_running_process_names

    @staticmethod
    def _norm_lang(lang: Optional[str]) -> str:
        low = str(lang or "vi").strip().lower()
        return "en" if low.startswith("en") else "vi"

    def _detect_genshin_conflict(
        self,
        norm_hk: str,
        lang: str,
        *,
        suppress_enabled: bool = False,
    ) -> Optional[Dict[str, Any]]:
        is_mouse, _, mods, main_key = parse_hotkey_parts(norm_hk)
        if is_mouse or not main_key:
            return None
        is_en = lang == "en"
        if main_key in _GENSHIN_ALWAYS_TRIGGERED_KEYS:
            vi_name, en_name = _GENSHIN_ALWAYS_TRIGGERED_KEYS[main_key]
            feat = en_name if is_en else vi_name
            if mods and suppress_enabled:
                mod_txt = "+".join(m.title() for m in sorted(mods))
                msg = (
                    f"Trigger-key suppression intercepts {main_key.upper()} ({feat}) while the game is focused so it won't open the in-game menu."
                    if is_en
                    else f"Đã tự chặn phím {main_key.upper()} ({feat}) khi chơi game (tổ hợp {mod_txt}+{main_key.upper()}) nên sẽ không làm bật menu game."
                )
                return {
                    "kind": "genshin",
                    "severity": "info",
                    "suppressed": True,
                    "app_id": "genshin_impact",
                    "app_name": "Genshin Impact",
                    "shortcut_id": main_key,
                    "can_remap": False,
                    "message": msg,
                }
            msg = (
                f"Genshin Impact reads {main_key.upper()} ({feat}) even when {'/'.join(m.title() for m in sorted(mods))} is held."
                if mods and is_en
                else (
                    f"Genshin Impact opens {main_key.upper()} ({feat})."
                    if is_en
                    else (
                        f"Genshin vẫn nhận phím {main_key.upper()} ({feat}) kể cả khi giữ {'+'.join(m.title() for m in sorted(mods))}!"
                        if mods
                        else f"Trùng phím mặc định của Genshin: {main_key.upper()} ({feat})."
                    )
                )
            )
            return {
                "kind": "genshin",
                "severity": "warn",
                "app_id": "genshin_impact",
                "app_name": "Genshin Impact",
                "shortcut_id": main_key,
                "can_remap": False,
                "message": msg,
            }
        if main_key in _GENSHIN_COMBAT_KEYS and "alt" not in mods:
            vi_name, en_name = _GENSHIN_COMBAT_KEYS[main_key]
            feat = en_name if is_en else vi_name
            msg = (
                f"Conflicts with Genshin Impact combat/movement key {main_key.upper()} ({feat})."
                if is_en
                else f"Trùng phím điều khiển trong Genshin: {main_key.upper()} ({feat})."
            )
            return {
                "kind": "genshin",
                "severity": "warn",
                "app_id": "genshin_impact",
                "app_name": "Genshin Impact",
                "shortcut_id": main_key,
                "can_remap": False,
                "message": msg,
            }
        return None

    def _detect_external_conflicts(
        self,
        norm_hk: str,
        lang: str,
        nv_shortcuts: Sequence[Dict[str, Any]],
        running_procs: Set[str],
    ) -> List[Dict[str, Any]]:
        if is_mouse_hotkey(norm_hk):
            return []
        is_en = lang == "en"
        out: List[Dict[str, Any]] = []
        matched_apps: Set[str] = set()

        for item in nv_shortcuts:
            if normalize_hotkey(item["hotkey"]) != norm_hk:
                continue
            matched_apps.add("nvidia_overlay")
            label = item["desc_en"] if is_en else item["desc_vi"]
            msg = (
                f"Used by NVIDIA Overlay ({label}). You can switch keys or remap NVIDIA's shortcut."
                if is_en
                else f"Đang trùng NVIDIA Overlay ({label}). Bạn có thể đổi phím khác hoặc chiếm & đổi phím NVIDIA."
            )
            out.append({
                "kind": "external_config",
                "severity": "error",
                "app_id": "nvidia_overlay",
                "app_name": "NVIDIA Overlay",
                "shortcut_id": item["shortcut_id"],
                "shortcut_label": label,
                "can_remap": True,
                "current_external_hotkey": item["hotkey"],
                "current_external_display": item["display_name"],
                "message": msg,
            })

        nv_running = bool({"nvidia overlay.exe", "nvsphelper64.exe", "nvcontainer.exe"} & running_procs)
        probed_taken = self._probe_fn(norm_hk)
        if probed_taken and not out:
            matched_known = False
            for app_id, app_name, def_hk, procs, vi_desc, en_desc in _KNOWN_OVERLAY_DEFAULTS:
                if normalize_hotkey(def_hk) != norm_hk:
                    continue
                if any(p in running_procs for p in procs):
                    matched_known = True
                    desc = en_desc if is_en else vi_desc
                    msg = (
                        f"Active hotkey held by {app_name} ({desc})."
                        if is_en
                        else f"Phím này đang bị {app_name} ({desc}) đăng ký toàn cục."
                    )
                    out.append({
                        "kind": "external_live",
                        "severity": "error",
                        "app_id": app_id,
                        "app_name": app_name,
                        "shortcut_id": def_hk,
                        "can_remap": False,
                        "message": msg,
                    })
                    break
            if not matched_known:
                owner = "NVIDIA Overlay / Windows" if nv_running else ("another app / Windows" if is_en else "ứng dụng khác / Windows")
                msg = (
                    f"Already registered system-wide by {owner} (may conflict unless taken over)."
                    if is_en
                    else f"Phím này đang bị {owner} đăng ký toàn cục trên Windows."
                )
                out.append({
                    "kind": "external_live",
                    "severity": "error",
                    "app_id": "system",
                    "app_name": owner,
                    "shortcut_id": "",
                    "can_remap": False,
                    "message": msg,
                })
        return out

    def _is_hotkey_clean(
        self,
        cand_hk: str,
        used_norms: Set[str],
        nv_norms: Set[str],
    ) -> bool:
        norm = normalize_hotkey(cand_hk)
        if not norm or norm in used_norms or norm in nv_norms:
            return False
        if self._detect_genshin_conflict(norm, "vi") is not None:
            return False
        if self._probe_fn(norm):
            return False
        return True

    def suggest_safe_hotkeys(
        self,
        action: Optional[str] = None,
        current_bindings: Optional[Dict[str, str]] = None,
        *,
        count: int = 3,
        nv_shortcuts: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[Dict[str, str]]:
        """Returns up to `count` clean, Genshin-safe hotkeys not held internally or by Windows/NVIDIA."""
        used_norms: Set[str] = set()
        if isinstance(current_bindings, dict):
            for act_name, hk in current_bindings.items():
                if action and act_name == action:
                    continue
                if hk:
                    used_norms.add(normalize_hotkey(hk))
        if nv_shortcuts is None:
            nv_shortcuts = self.nvidia.read_shortcuts()
        nv_norms = {normalize_hotkey(it["hotkey"]) for it in nv_shortcuts if it.get("hotkey")}

        suggestions: List[Dict[str, str]] = []
        for cand in _SAFE_SUGGESTION_POOL:
            if len(suggestions) >= count:
                break
            if action == "nav_undo" and "shift" not in cand and "ctrl" not in cand:
                continue
            if self._is_hotkey_clean(cand, used_norms, nv_norms):
                norm = normalize_hotkey(cand)
                suggestions.append({"hotkey": norm, "display_name": format_display_name(norm)})
        if len(suggestions) < count:
            for cand in _SAFE_SUGGESTION_POOL:
                if len(suggestions) >= count:
                    break
                norm = normalize_hotkey(cand)
                if any(s["hotkey"] == norm for s in suggestions):
                    continue
                if self._is_hotkey_clean(cand, used_norms, nv_norms):
                    suggestions.append({"hotkey": norm, "display_name": format_display_name(norm)})
        return suggestions

    def analyze_hotkey(
        self,
        hotkey_str: str,
        *,
        action: Optional[str] = None,
        current_bindings: Optional[Dict[str, str]] = None,
        lang: str = "vi",
        nv_shortcuts: Optional[Sequence[Dict[str, Any]]] = None,
        running_procs: Optional[Set[str]] = None,
        suppress_enabled: bool = False,
    ) -> Dict[str, Any]:
        norm_lang = self._norm_lang(lang)
        norm_hk = normalize_hotkey(hotkey_str)
        nv_list = list(nv_shortcuts) if nv_shortcuts is not None else self.nvidia.read_shortcuts()
        procs = running_procs if running_procs is not None else self._proc_fn()

        conflicts: List[Dict[str, Any]] = []
        if isinstance(current_bindings, dict):
            for other_act, other_hk in current_bindings.items():
                if action and other_act == action:
                    continue
                if other_hk and normalize_hotkey(other_hk) == norm_hk:
                    msg = (
                        f"Already assigned to [{other_act}] in Genshin Helper."
                        if norm_lang == "en"
                        else f"Trùng với chức năng [{other_act}] trong Genshin Helper."
                    )
                    conflicts.append({
                        "kind": "internal",
                        "severity": "error",
                        "app_id": "genshin_helper",
                        "app_name": "Genshin Helper",
                        "shortcut_id": other_act,
                        "can_remap": False,
                        "message": msg,
                    })

        g_conf = self._detect_genshin_conflict(norm_hk, norm_lang, suppress_enabled=suppress_enabled)
        if g_conf is not None:
            conflicts.append(g_conf)

        conflicts.extend(self._detect_external_conflicts(norm_hk, norm_lang, nv_list, procs))

        severity = "ok"
        if any(c.get("severity") == "error" for c in conflicts):
            severity = "error"
        elif any(c.get("severity") == "warn" for c in conflicts):
            severity = "warn"
        elif conflicts:
            severity = "info"

        suggestions = (
            self.suggest_safe_hotkeys(action, current_bindings, count=3, nv_shortcuts=nv_list)
            if severity in ("warn", "error")
            else []
        )
        return {
            "hotkey": norm_hk,
            "display_name": format_display_name(norm_hk),
            "ok": severity not in ("warn", "error"),
            "severity": severity,
            "conflicts": conflicts,
            "suggestions": suggestions,
        }

    def analyze_all(
        self,
        bindings: Dict[str, str],
        *,
        lang: str = "vi",
        suppress_enabled: bool = False,
    ) -> Dict[str, Any]:
        nv_list = self.nvidia.read_shortcuts()
        procs = self._proc_fn()
        results: Dict[str, Any] = {}
        for act, hk in (bindings or {}).items():
            if not hk:
                continue
            results[act] = self.analyze_hotkey(
                hk,
                action=act,
                current_bindings=bindings,
                lang=lang,
                nv_shortcuts=nv_list,
                running_procs=procs,
                suppress_enabled=suppress_enabled,
            )
        return {
            "ok": True,
            "actions": results,
            "active_backups": self.nvidia.list_backups(),
            "nvidia_detected": bool(nv_list),
        }

    def remap_external_hotkey(
        self,
        app_id: str,
        shortcut_id: str,
        new_hotkey: str,
        *,
        current_bindings: Optional[Dict[str, str]] = None,
        lang: str = "vi",
    ) -> Dict[str, Any]:
        norm_lang = self._norm_lang(lang)
        if app_id != "nvidia_overlay":
            return {
                "ok": False,
                "error": (
                    f"Automatic remapping is not supported for {app_id!r}."
                    if norm_lang == "en"
                    else f"Chưa hỗ trợ tự đổi phím trực tiếp cho {app_id!r}."
                ),
            }
        norm_new = normalize_hotkey(new_hotkey)
        if not hotkey_to_vk_sequence(norm_new):
            return {
                "ok": False,
                "error": (
                    "Please enter a valid keyboard combination (not mouse)."
                    if norm_lang == "en"
                    else "Vui lòng chọn tổ hợp phím bàn phím hợp lệ cho app kia."
                ),
            }
        if isinstance(current_bindings, dict):
            for act, hk in current_bindings.items():
                if hk and normalize_hotkey(hk) == norm_new:
                    return {
                        "ok": False,
                        "error": (
                            f"Replacement hotkey [{format_display_name(norm_new)}] is already used by [{act}]."
                            if norm_lang == "en"
                            else f"Phím mới [{format_display_name(norm_new)}] đang trùng với [{act}] của app!"
                        ),
                    }
        for existing in self.nvidia.read_shortcuts():
            if existing["shortcut_id"] != shortcut_id and normalize_hotkey(existing["hotkey"]) == norm_new:
                return {
                    "ok": False,
                    "error": (
                        f"NVIDIA Overlay already uses [{format_display_name(norm_new)}] for {existing['desc_en']}."
                        if norm_lang == "en"
                        else f"NVIDIA Overlay đang dùng [{format_display_name(norm_new)}] cho {existing['desc_vi']}!"
                    ),
                }
        if self._probe_fn(norm_new):
            return {
                "ok": False,
                "error": (
                    f"[{format_display_name(norm_new)}] is currently held by another Windows hotkey."
                    if norm_lang == "en"
                    else f"Phím [{format_display_name(norm_new)}] đang bị một phần mềm khác trên Windows giữ."
                ),
            }

        res = self.nvidia.remap_shortcut(shortcut_id, norm_new)
        if res.get("ok"):
            res["active_backups"] = self.nvidia.list_backups()
        return res

    def restore_external_hotkey(self, app_id: str, shortcut_id: str) -> Dict[str, Any]:
        if app_id != "nvidia_overlay":
            return {"ok": False, "error": f"Unsupported app_id {app_id!r}"}
        res = self.nvidia.restore_shortcut(shortcut_id)
        if res.get("ok"):
            res["active_backups"] = self.nvidia.list_backups()
        return res
