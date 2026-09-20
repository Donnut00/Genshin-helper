import ctypes
from ctypes import wintypes
import logging
import threading
from typing import Callable, Optional, Set, Tuple, Dict, Any
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
    Unified Multi-Hotkey Engine supporting:
    1. Multiple named bindings (e.g. 'sync' and 'debug') concurrently.
    2. All standard keyboard keys & combinations (F1-F12, Alt+F11, Ctrl+Q, etc.)
    3. All mouse buttons (Mouse 4, Mouse 5, Middle / Wheel click, Right click)
    4. Keyboard modifier + Mouse combinations (Alt+Mouse4, Ctrl+Middle, etc.)
    5. Single low-level mouse hook shared across all mouse bindings (zero conflict)
    6. Zero-restart live hotkey re-binding per action
    7. 100% backward compatible with single-hotkey initialization
    """
    def __init__(self, hotkey_str: Optional[str] = None, callback: Optional[Callable[[], None]] = None):
        self.bindings: Dict[str, Dict[str, Any]] = {}
        self.is_running: bool = False

        # Shared Mouse hook references
        self._mouse_hook = None
        self._mouse_thread = None
        self._mouse_thread_id = None
        self._mouse_proc = None

        # Backward compatibility for single binding
        if hotkey_str and callback:
            self.register("sync", hotkey_str, callback)

    @staticmethod
    def _parse_hotkey(hotkey: str) -> Tuple[bool, Optional[str], Set[str], str]:
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

    @staticmethod
    def format_display_name(hotkey_str: str) -> str:
        """Returns human-friendly display name (e.g. 'Alt + Mouse 4' or 'Ctrl + F9')."""
        is_mouse, mouse_btn, modifiers, _ = UnifiedHotkeyManager._parse_hotkey(hotkey_str)
        mod_parts = []
        if "ctrl" in modifiers:
            mod_parts.append("Ctrl")
        if "alt" in modifiers:
            mod_parts.append("Alt")
        if "shift" in modifiers:
            mod_parts.append("Shift")

        if is_mouse:
            if mouse_btn == "mouse4":
                main_name = "Mouse 4 (Chuột hông)"
            elif mouse_btn == "mouse5":
                main_name = "Mouse 5 (Chuột hông)"
            elif mouse_btn == "middle":
                main_name = "Chuột giữa (Con lăn)"
            elif mouse_btn == "right":
                main_name = "Chuột phải"
            else:
                main_name = (mouse_btn or "").upper()
            return " + ".join(mod_parts + [main_name])

        # Keyboard formatting
        parts = [p.strip() for p in hotkey_str.replace("+", " ").replace("-", " ").split() if p.strip()]
        keys = []
        for p in parts:
            pl = p.lower()
            if pl in ("ctrl", "control"):
                if "Ctrl" not in keys: keys.append("Ctrl")
            elif pl in ("alt", "menu"):
                if "Alt" not in keys: keys.append("Alt")
            elif pl in ("shift",):
                if "Shift" not in keys: keys.append("Shift")
            else:
                keys.append(p.upper())
        return " + ".join(keys) if keys else hotkey_str.upper()

    def get_display_name(self, name: str = "sync") -> str:
        b = self.bindings.get(name)
        if not b:
            # Fallback to first binding if any
            if self.bindings:
                b = next(iter(self.bindings.values()))
            else:
                return "NONE"
        return self.format_display_name(b["hotkey_str"])

    def get_hotkey_str(self, name: str = "sync") -> str:
        b = self.bindings.get(name)
        return b["hotkey_str"] if b else ""

    def register(self, name: str, hotkey_str: str, callback: Callable[[], None]):
        """Registers or replaces a named hotkey binding."""
        clean_key = hotkey_str.strip().lower()
        is_mouse, mouse_btn, modifiers, clean_str = self._parse_hotkey(clean_key)

        # If binding already exists, stop its old hook
        if name in self.bindings:
            old_b = self.bindings[name]
            if old_b.get("kb_hook"):
                try:
                    keyboard.remove_hotkey(old_b["kb_hook"])
                except Exception:
                    pass

        self.bindings[name] = {
            "name": name,
            "hotkey_str": clean_key,
            "callback": callback,
            "is_mouse": is_mouse,
            "mouse_btn": mouse_btn,
            "modifiers": modifiers,
            "clean_str": clean_str,
            "kb_hook": None,
        }

        # If already running, activate the new binding immediately
        if self.is_running:
            self._activate_binding(name)

    def _activate_binding(self, name: str) -> bool:
        b = self.bindings.get(name)
        if not b:
            return False

        if b["is_mouse"]:
            # Ensure mouse hook is running
            if not self._mouse_hook:
                return self._start_mouse_hook()
            return True
        else:
            try:
                def make_cb(target_b):
                    return lambda: self._trigger_binding(target_b)

                b["kb_hook"] = keyboard.add_hotkey(b["hotkey_str"], make_cb(b))
                logger.info(f"Keyboard hotkey [{b['name'].upper()}] registered: [{b['hotkey_str'].upper()}]")
                return True
            except Exception as e:
                logger.warning(f"Failed to register keyboard hotkey [{b['name']}]: {e}")
                return False

    def start(self) -> bool:
        self.is_running = True
        has_mouse = False
        all_ok = True

        for name, b in list(self.bindings.items()):
            if b["is_mouse"]:
                has_mouse = True
            else:
                ok = self._activate_binding(name)
                if not ok:
                    all_ok = False

        if has_mouse and not self._mouse_hook:
            if not self._start_mouse_hook():
                all_ok = False

        return all_ok

    def _start_mouse_hook(self) -> bool:
        if self._mouse_hook:
            return True

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

                    if btn_pressed:
                        alt_pressed = bool(user32.GetKeyState(VK_MENU) < 0)
                        ctrl_pressed = bool(user32.GetKeyState(VK_CONTROL) < 0)
                        shift_pressed = bool(user32.GetKeyState(VK_SHIFT) < 0)

                        # Match against all registered mouse bindings
                        for b in list(self.bindings.values()):
                            if not b["is_mouse"]:
                                continue
                            if b["mouse_btn"] == btn_pressed:
                                match = True
                                if ("alt" in b["modifiers"]) != alt_pressed:
                                    match = False
                                if ("ctrl" in b["modifiers"]) != ctrl_pressed:
                                    match = False
                                if ("shift" in b["modifiers"]) != shift_pressed:
                                    match = False

                                if match:
                                    self._trigger_binding(b)

                return user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam)

            h_mod = kernel32.GetModuleHandleW(None)
            self._mouse_proc = HOOKPROC(hook_callback)
            self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, h_mod, 0)
            if not self._mouse_hook:
                logger.error(f"Failed to install Windows Low-Level Mouse Hook (LastError={kernel32.GetLastError()}).")
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
            logger.info("Shared Low-Level Mouse Hook initialized successfully.")
            return True
        return False

    def _trigger_binding(self, binding: Dict[str, Any]):
        name = binding.get("name", "action")
        display = self.format_display_name(binding["hotkey_str"])
        logger.info(f"Hotkey [{name.upper()}]: [{display}] triggered! Processing action...")
        cb = binding.get("callback")
        if cb:
            threading.Thread(target=self._run_callback_safe, args=(cb,), daemon=True).start()

    def _run_callback_safe(self, cb: Callable[[], None]):
        try:
            cb()
        except Exception as e:
            logger.error(f"Error in hotkey callback: {e}")

    def stop(self):
        self.is_running = False

        # Stop keyboard hooks
        for b in self.bindings.values():
            if b.get("kb_hook"):
                try:
                    keyboard.remove_hotkey(b["kb_hook"])
                except Exception:
                    pass
                b["kb_hook"] = None

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
        logger.info("All hotkeys unregistered.")

    def update_binding(self, name: str, new_hotkey_str: str) -> bool:
        """Updates hotkey for a specific binding on-the-fly without restarting the app."""
        new_key = new_hotkey_str.strip().lower()
        if not new_key or name not in self.bindings:
            return False

        b = self.bindings[name]
        logger.info(f"Updating [{name.upper()}] hotkey from [{b['hotkey_str']}] to [{new_key}]...")

        # Remove old kb hook if any
        if b.get("kb_hook"):
            try:
                keyboard.remove_hotkey(b["kb_hook"])
            except Exception:
                pass
            b["kb_hook"] = None

        is_mouse, mouse_btn, modifiers, clean_str = self._parse_hotkey(new_key)
        b["hotkey_str"] = new_key
        b["is_mouse"] = is_mouse
        b["mouse_btn"] = mouse_btn
        b["modifiers"] = modifiers
        b["clean_str"] = clean_str

        if self.is_running:
            success = self._activate_binding(name)
            # Check if mouse hook needs to be stopped if no mouse bindings remain
            has_mouse = any(x["is_mouse"] for x in self.bindings.values())
            if not has_mouse and self._mouse_hook:
                try:
                    user32.UnhookWindowsHookEx(self._mouse_hook)
                except Exception:
                    pass
                self._mouse_hook = None
            return success
        return True

    def update_hotkey(self, new_hotkey_str: str) -> bool:
        """Backward compatibility for single hotkey."""
        target_name = "sync" if "sync" in self.bindings else (next(iter(self.bindings.keys())) if self.bindings else "sync")
        return self.update_binding(target_name, new_hotkey_str)


# Backward compatibility alias
HotkeyManager = UnifiedHotkeyManager
