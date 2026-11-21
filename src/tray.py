import logging
import os
import threading
from typing import Callable, Dict, List, Optional, Tuple

import win32api
import win32con
import win32gui

from src.paths import get_asset_path

logger = logging.getLogger("GenshinHelper.Tray")


class SystemTrayApp:
    """
    Pure pywin32 System Tray controller for Genshin Helper.
    Displays live Paimon status icons (connected / idle / paused / offline)
    without requiring external GUI tray dependencies.
    """

    WM_TRAYICON = win32con.WM_USER + 20
    WM_APP_UPDATE_STATE = win32con.WM_APP + 10
    WM_APP_CLOSE = win32con.WM_APP + 11

    ID_TOGGLE_ENGINE = 1001
    ID_OPEN_MAP = 1002
    ID_OPEN_DIAG = 1003
    ID_OPEN_CONFIG = 1004
    ID_QUIT = 1005

    STATE_ICON_FILES: Dict[str, str] = {
        "connected": "tray_connected.ico",
        "idle": "tray_default.ico",
        "paused": "tray_paused.ico",
        "offline": "tray_disconnected.ico",
    }

    DEFAULT_TOOLTIPS: Dict[str, str] = {
        "connected": "Genshin Helper v4.5 — Đang kết nối Web Map",
        "idle": "Genshin Helper v4.5 — Đang chạy (Chờ mở HoYoLAB Map)",
        "paused": "Genshin Helper v4.5 — Tạm dừng (Standby 0% CPU)",
        "offline": "Genshin Helper v4.5 — Offline",
    }

    def __init__(
        self,
        on_toggle_engine: Optional[Callable[[], None]] = None,
        on_open_map: Optional[Callable[[], None]] = None,
        on_open_diagnostics: Optional[Callable[[], None]] = None,
        on_open_config_dir: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
        initial_active: bool = True,
    ) -> None:
        self.on_toggle_engine = on_toggle_engine
        self.on_open_map = on_open_map
        self.on_open_diagnostics = on_open_diagnostics
        self.on_open_config_dir = on_open_config_dir
        self.on_quit = on_quit

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._hwnd: Optional[int] = None
        self._closed = False
        self._class_name = f"GenshinHelperTrayWnd_{id(self)}"
        self._state: str = "idle" if initial_active else "paused"
        self._engine_active: bool = bool(initial_active)
        self._tooltip: str = self.DEFAULT_TOOLTIPS.get(self._state, "Genshin Helper")
        self._icons: Dict[str, Tuple[int, bool]] = {}
        self._nid_added = False

    @classmethod
    def build_menu_items(cls, engine_active: bool) -> List[Tuple[int, str, bool]]:
        """Returns tray menu entries as (command_id, label, is_separator)."""
        toggle_label = "Tạm dừng đồng bộ (Standby)" if engine_active else "Bật lại đồng bộ (Active)"
        return [
            (cls.ID_TOGGLE_ENGINE, toggle_label, False),
            (cls.ID_OPEN_MAP, "Mở HoYoLAB Web Map", False),
            (0, "", True),
            (cls.ID_OPEN_DIAG, "Mở thư mục Diagnostics", False),
            (cls.ID_OPEN_CONFIG, "Mở thư mục cấu hình", False),
            (0, "", True),
            (cls.ID_QUIT, "Thoát hẳn Genshin Helper", False),
        ]

    def _load_icons(self) -> None:
        cx = max(16, int(win32api.GetSystemMetrics(win32con.SM_CXSMICON) or 16))
        cy = max(16, int(win32api.GetSystemMetrics(win32con.SM_CYSMICON) or 16))
        fallback = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

        for state, fname in self.STATE_ICON_FILES.items():
            ico_path = get_asset_path("assets", "icons", fname)
            loaded = 0
            owned = False
            if os.path.exists(ico_path):
                try:
                    loaded = int(
                        win32gui.LoadImage(
                            0,
                            ico_path,
                            win32con.IMAGE_ICON,
                            cx,
                            cy,
                            win32con.LR_LOADFROMFILE,
                        )
                    )
                    owned = bool(loaded)
                except Exception as e:
                    logger.debug(f"Could not load tray icon {ico_path}: {e}")
                    loaded = 0
            self._icons[state] = (loaded or fallback, owned)

    def _destroy_icons(self) -> None:
        for hicon, owned in self._icons.values():
            if owned and hicon:
                try:
                    win32gui.DestroyIcon(hicon)
                except Exception:
                    pass
        self._icons.clear()

    def _apply_notify_icon(self, hwnd: int, action: int) -> None:
        with self._lock:
            state = self._state
            tip = (self._tooltip or self.DEFAULT_TOOLTIPS.get(state, "Genshin Helper"))[:127]
        hicon = self._icons.get(state, self._icons.get("idle", (0, False)))[0]
        if not hicon:
            hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (hwnd, 0, flags, self.WM_TRAYICON, hicon, tip)
        try:
            win32gui.Shell_NotifyIcon(action, nid)
            if action == win32gui.NIM_ADD:
                self._nid_added = True
        except Exception as e:
            logger.debug(f"Shell_NotifyIcon({action}) warning: {e}")

    def _dispatch_cb(self, cb: Optional[Callable[[], None]]) -> None:
        if cb:
            threading.Thread(target=self._run_safe, args=(cb,), daemon=True).start()

    @staticmethod
    def _run_safe(cb: Callable[[], None]) -> None:
        try:
            cb()
        except Exception as e:
            logger.error(f"Tray callback error: {e}")

    def _show_context_menu(self, hwnd: int) -> None:
        with self._lock:
            active = self._engine_active
        menu = win32gui.CreatePopupMenu()
        try:
            for cmd_id, label, is_sep in self.build_menu_items(active):
                if is_sep:
                    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                else:
                    win32gui.AppendMenu(menu, win32con.MF_STRING, cmd_id, label)
            pos = win32gui.GetCursorPos()
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN | win32con.TPM_RIGHTBUTTON,
                pos[0],
                pos[1],
                0,
                hwnd,
                None,
            )
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            try:
                win32gui.DestroyMenu(menu)
            except Exception:
                pass

    def _ui_thread_main(self) -> None:
        taskbar_created_msg = win32gui.RegisterWindowMessage("TaskbarCreated")
        hinst = win32api.GetModuleHandle(None)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == taskbar_created_msg:
                self._apply_notify_icon(hwnd, win32gui.NIM_ADD)
                return 0
            if msg == self.WM_APP_UPDATE_STATE:
                action = win32gui.NIM_MODIFY if self._nid_added else win32gui.NIM_ADD
                self._apply_notify_icon(hwnd, action)
                return 0
            if msg == self.WM_TRAYICON:
                if lparam == win32con.WM_RBUTTONUP or lparam == win32con.WM_CONTEXTMENU:
                    self._show_context_menu(hwnd)
                elif lparam == win32con.WM_LBUTTONDBLCLK:
                    self._dispatch_cb(self.on_open_map)
                return 0
            if msg == win32con.WM_COMMAND:
                cmd_id = win32api.LOWORD(wparam)
                if cmd_id == self.ID_TOGGLE_ENGINE:
                    self._dispatch_cb(self.on_toggle_engine)
                elif cmd_id == self.ID_OPEN_MAP:
                    self._dispatch_cb(self.on_open_map)
                elif cmd_id == self.ID_OPEN_DIAG:
                    self._dispatch_cb(self.on_open_diagnostics)
                elif cmd_id == self.ID_OPEN_CONFIG:
                    self._dispatch_cb(self.on_open_config_dir)
                elif cmd_id == self.ID_QUIT:
                    self._dispatch_cb(self.on_quit)
                return 0
            if msg == self.WM_APP_CLOSE:
                if self._nid_added:
                    try:
                        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
                    except Exception:
                        pass
                    self._nid_added = False
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_DESTROY:
                self._destroy_icons()
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        wc = win32gui.WNDCLASS()
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        wc.lpfnWndProc = wndproc

        try:
            win32gui.RegisterClass(wc)
        except Exception as e:
            logger.warning(f"Tray RegisterClass failed: {e}")
            self._ready_event.set()
            return

        hwnd = 0
        try:
            hwnd = win32gui.CreateWindow(
                self._class_name,
                "GenshinHelperTray",
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                hinst,
                None,
            )
            self._hwnd = int(hwnd)
            self._load_icons()
            self._apply_notify_icon(hwnd, win32gui.NIM_ADD)
        except Exception as e:
            logger.warning(f"Tray window creation failed: {e}")
            self._ready_event.set()
            return

        self._ready_event.set()
        try:
            win32gui.PumpMessages()
        finally:
            self._hwnd = None
            try:
                win32gui.UnregisterClass(self._class_name, hinst)
            except Exception:
                pass

    def start(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._thread is not None and self._thread.is_alive():
                return True
            self._ready_event.clear()
            self._thread = threading.Thread(target=self._ui_thread_main, name="GH_SystemTray", daemon=True)
            self._thread.start()
        self._ready_event.wait(timeout=2.0)
        return self._hwnd is not None

    def set_state(self, state: str, tooltip: str = "", engine_active: Optional[bool] = None) -> None:
        norm = state if state in self.STATE_ICON_FILES else "idle"
        with self._lock:
            if engine_active is not None:
                self._engine_active = bool(engine_active)
            else:
                self._engine_active = norm != "paused"
            tip = tooltip or self.DEFAULT_TOOLTIPS.get(norm, "Genshin Helper")
            if self._state == norm and self._tooltip == tip and self._hwnd is not None:
                return
            self._state = norm
            self._tooltip = tip
            hwnd = self._hwnd
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, self.WM_APP_UPDATE_STATE, 0, 0)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            hwnd = self._hwnd
            t = self._thread
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, self.WM_APP_CLOSE, 0, 0)
            except Exception:
                pass
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
