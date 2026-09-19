import ctypes
from ctypes import wintypes
import logging
import threading
from typing import Callable, Optional, Set, Tuple
import keyboard

logger = logging.getLogger("GenshinHelper.Hotkey")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_XBUTTONDOWN = 0x020B

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt

MOUSE_ALIASES = {
    # Mouse 4
    "mouse4": "mouse4",
    "mouse_4": "mouse4",
    "xbutton1": "mouse4",
    "xbtn1": "mouse4",
    "mouse_back": "mouse4",
    "btn4": "mouse4",
    # Mouse 5
    "mouse5": "mouse5",
    "mouse_5": "mouse5",
    "xbutton2": "mouse5",
    "xbtn2": "mouse5",
    "mouse_forward": "mouse5",
    "btn5": "mouse5",
    # Middle
    "middle": "middle",
    "mouse_middle": "middle",
    "mbutton": "middle",
    "wheel": "middle",
    "wheel_click": "middle",
    "btn3": "middle",
    # Right
    "right": "right",
    "mouse_right": "right",
    "rbutton": "right",
}

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),
    ]

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(MSLLHOOKSTRUCT))


class UnifiedHotkeyManager:
    """
    Unified Hotkey Engine supporting:
    1. All standard keyboard keys & combinations (F1-F12, Alt+F11, Ctrl+Q, etc.)
    2. All mouse buttons (Mouse 4, Mouse 5, Middle / Wheel click, Right click)
    3. Keyboard modifier + Mouse combinations (Alt+Mouse4, Ctrl+Middle, etc.)
    4. Zero-restart live hotkey re-binding
    """
    def __init__(self, hotkey_str: str, callback: Callable[[], None]):
        self.hotkey_str = hotkey_str.strip()
        self.callback = callback

        # Keyboard hook reference
        self._kb_hook = None

        # Mouse hook references
        self._mouse_hook = None
        self._mouse_thread = None
        self._mouse_thread_id = None
        self._mouse_proc = None

        # Parsed state
        self.is_mouse: bool = False
        self.mouse_btn: Optional[str] = None
        self.modifiers: Set[str] = set()

    def _parse_hotkey(self, hotkey: str) -> Tuple[bool, Optional[str], Set[str], str]:
        parts = [p.strip().lower() for p in hotkey.replace("+", " ").replace("-", " ").split() if p.strip()]
        modifiers = set()
        main_key = ""
        is_mouse = False
        mouse_btn = None

        for p in parts:
            if p in ("alt", "menu"):
                modifiers.add("alt")
            elif p in ("ctrl", "control"):
                modifiers.add("ctrl")
            elif p in ("shift",):
                modifiers.add("shift")
            elif p in MOUSE_ALIASES:
                is_mouse = True
                mouse_btn = MOUSE_ALIASES[p]
                main_key = mouse_btn
            else:
                main_key = p

        clean_str = "+".join(sorted(list(modifiers)) + ([main_key] if main_key else []))
        return is_mouse, mouse_btn, modifiers, clean_str

    def get_display_name(self) -> str:
        """Returns human-friendly display name (e.g. 'Alt + Mouse 4' or 'F10')."""
        is_mouse, mouse_btn, modifiers, _ = self._parse_hotkey(self.hotkey_str)
        mod_prefix = ""
        if "ctrl" in modifiers:
            mod_prefix += "Ctrl + "
        if "alt" in modifiers:
            mod_prefix += "Alt + "
        if "shift" in modifiers:
            mod_prefix += "Shift + "

        if is_mouse:
            if mouse_btn == "mouse4":
                return f"{mod_prefix}Mouse 4 (Chuột hông)"
            elif mouse_btn == "mouse5":
                return f"{mod_prefix}Mouse 5 (Chuột hông)"
            elif mouse_btn == "middle":
                return f"{mod_prefix}Chuột giữa (Con lăn)"
            elif mouse_btn == "right":
                return f"{mod_prefix}Chuột phải"
            return f"{mod_prefix}{mouse_btn.upper()}"
        return self.hotkey_str.upper()

    def start(self) -> bool:
        self.is_mouse, self.mouse_btn, self.modifiers, clean_str = self._parse_hotkey(self.hotkey_str)

        if self.is_mouse:
            return self._start_mouse_hook()
        else:
            return self._start_keyboard_hook()

    def _start_keyboard_hook(self) -> bool:
        try:
            self._kb_hook = keyboard.add_hotkey(self.hotkey_str, self._on_triggered)
            logger.info(f"Global keyboard hotkey registered: [{self.hotkey_str.upper()}]")
            return True
        except Exception as e:
            logger.warning(f"Failed to register keyboard hotkey [{self.hotkey_str}]: {e}")
            return False

    def _start_mouse_hook(self) -> bool:
        started_event = threading.Event()

        def _run():
            self._mouse_thread_id = kernel32.GetCurrentThreadId()

            def hook_callback(nCode, wParam, lParam):
                if nCode >= 0:
                    data = lParam.contents
                    btn_pressed = None

                    if wParam == WM_MBUTTONDOWN:
                        btn_pressed = "middle"
                    elif wParam == WM_RBUTTONDOWN:
                        btn_pressed = "right"
                    elif wParam == WM_XBUTTONDOWN:
                        xbtn = (data.mouseData >> 16) & 0xFFFF
                        if xbtn == 1:
                            btn_pressed = "mouse4"
                        elif xbtn == 2:
                            btn_pressed = "mouse5"

                    if btn_pressed == self.mouse_btn:
                        # Check modifiers
                        alt_pressed = bool(user32.GetKeyState(VK_MENU) < 0)
                        ctrl_pressed = bool(user32.GetKeyState(VK_CONTROL) < 0)
                        shift_pressed = bool(user32.GetKeyState(VK_SHIFT) < 0)

                        match = True
                        if ("alt" in self.modifiers) != alt_pressed:
                            match = False
                        if ("ctrl" in self.modifiers) != ctrl_pressed:
                            match = False
                        if ("shift" in self.modifiers) != shift_pressed:
                            match = False

                        if match:
                            self._on_triggered()

                return user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam)

            self._mouse_proc = HOOKPROC(hook_callback)
            self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, 0, 0)
            if not self._mouse_hook:
                logger.error("Failed to install Windows Low-Level Mouse Hook.")
                started_event.set()
                return

            started_event.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        self._mouse_thread = threading.Thread(target=_run, daemon=True)
        self._mouse_thread.start()
        started_event.wait(timeout=1.0)

        if self._mouse_hook:
            logger.info(f"Global mouse hotkey registered: [{self.get_display_name()}]")
            return True
        return False

    def _on_triggered(self):
        logger.info(f"Hotkey [{self.get_display_name()}] triggered! Processing action...")
        try:
            self.callback()
        except Exception as e:
            logger.error(f"Error in hotkey callback: {e}")

    def stop(self):
        # Stop keyboard hook
        if self._kb_hook is not None:
            try:
                keyboard.remove_hotkey(self._kb_hook)
            except Exception:
                pass
            self._kb_hook = None

        # Stop mouse hook
        if self._mouse_hook:
            try:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            except Exception:
                pass
            self._mouse_hook = None

        if self._mouse_thread_id:
            user32.PostThreadMessageW(self._mouse_thread_id, 0x0012, 0, 0)  # WM_QUIT
            self._mouse_thread_id = None

        self._mouse_thread = None
        self._mouse_proc = None
        logger.info("Previous hotkey unregistered.")

    def update_hotkey(self, new_hotkey_str: str) -> bool:
        new_key = new_hotkey_str.strip().lower()
        if not new_key:
            return False

        logger.info(f"Updating hotkey to: [{new_key}]...")
        self.stop()
        self.hotkey_str = new_key
        success = self.start()
        if not success:
            logger.error(f"Failed to re-bind hotkey to [{new_key}]. Falling back to alt+f11.")
            self.hotkey_str = "alt+f11"
            self.start()
            return False
        return True


# Backward compatibility alias
HotkeyManager = UnifiedHotkeyManager
