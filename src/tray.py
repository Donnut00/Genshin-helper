import logging
import os
import threading
from typing import Callable, Dict, List, Optional, Tuple

import win32api
import win32con
import win32gui

from src.i18n import t
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
    ID_LAUNCH_BORDERLESS = 1006
    ID_SUPPORT = 1007

    STATE_ICON_FILES: Dict[str, str] = {
        "connected": "tray_connected.ico",
        "idle": "tray_default.ico",
        "paused": "tray_paused.ico",
        "offline": "tray_disconnected.ico",
    }

    TOOLTIP_KEYS: Dict[str, str] = {
        "connected": "tray.tooltip.connected",
        "idle": "tray.tooltip.idle",
        "paused": "tray.tooltip.paused",
        "offline": "tray.tooltip.offline",
    }

    DEFAULT_TOOLTIPS: Dict[str, str] = {
        "connected": "Genshin Helper v5.0 — Đang kết nối Web Map",
        "idle": "Genshin Helper v5.0 — Đang chạy (Chờ mở HoYoLAB Map)",
        "paused": "Genshin Helper v5.0 — Tạm dừng (Standby 0% CPU)",
        "offline": "Genshin Helper v5.0 — Offline",
    }

    def __init__(
        self,
        on_toggle_engine: Optional[Callable[[], None]] = None,
        on_open_map: Optional[Callable[[], None]] = None,
        on_open_diagnostics: Optional[Callable[[], None]] = None,
        on_open_config_dir: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
        initial_active: bool = True,
        on_launch_borderless: Optional[Callable[[], None]] = None,
        on_open_support: Optional[Callable[[], None]] = None,
        lang: Optional[str] = None,
    ) -> None:
        self.on_toggle_engine = on_toggle_engine
        self.on_open_map = on_open_map
        self.on_open_diagnostics = on_open_diagnostics
        self.on_open_config_dir = on_open_config_dir
        self.on_quit = on_quit
        self.on_launch_borderless = on_launch_borderless
        self.on_open_support = on_open_support

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._hwnd: Optional[int] = None
        self._closed = False
        self._class_name = f"GenshinHelperTrayWnd_{id(self)}"
        self._state: str = "idle" if initial_active else "paused"
        self._last_applied_state: Optional[str] = None
        self._engine_active: bool = bool(initial_active)
        self._lang: Optional[str] = lang
        self._uipi_warning: bool = False
        self._custom_tooltip: str = ""
        self._tooltip: str = self.get_tooltip(self._state, lang=self._lang, uipi_warning=self._uipi_warning)
        self._icons: Dict[str, Tuple[int, bool]] = {}
        self._nid_added = False

    @classmethod
    def get_tooltip(
        cls,
        state: str,
        lang: Optional[str] = None,
        uipi_warning: bool = False,
    ) -> str:
        """Returns localized tooltip string for `state`, optionally appending UIPI Admin warning."""
        norm = state if state in cls.STATE_ICON_FILES else "idle"
        key = cls.TOOLTIP_KEYS.get(norm, "tray.tooltip.idle")
        base = t(key, lang=lang)
        if not base or base == key:
            base = cls.DEFAULT_TOOLTIPS.get(norm, "Genshin Helper")
        if uipi_warning:
            warn_key = "uipi.warning.short"
            warn = t(warn_key, lang=lang)
            if warn and warn != warn_key:
                return f"{base}\n⚠ {warn}"
        return base

    @classmethod
    def build_menu_items(
        cls,
        engine_active: bool,
        lang: Optional[str] = None,
    ) -> List[Tuple[int, str, bool]]:
        """Returns localized tray menu entries as (command_id, label, is_separator)."""
        toggle_label = t("tray.menu.pause", lang=lang) if engine_active else t("tray.menu.resume", lang=lang)
        return [
            (cls.ID_TOGGLE_ENGINE, toggle_label, False),
            (cls.ID_OPEN_MAP, t("tray.menu.open_map", lang=lang), False),
            (cls.ID_LAUNCH_BORDERLESS, t("tray.menu.launch_borderless", lang=lang), False),
            (0, "", True),
            (cls.ID_OPEN_DIAG, t("tray.menu.open_diag", lang=lang), False),
            (cls.ID_OPEN_CONFIG, t("tray.menu.open_config", lang=lang), False),
            (0, "", True),
            (cls.ID_SUPPORT, t("tray.menu.support", lang=lang), False),
            (0, "", True),
            (cls.ID_QUIT, t("tray.menu.quit", lang=lang), False),
        ]

    def _compose_tooltip_locked(self) -> str:
        if self._custom_tooltip:
            base = self._custom_tooltip
            if self._uipi_warning:
                warn = t("uipi.warning.short", lang=self._lang)
                if warn and warn != "uipi.warning.short":
                    return f"{base}\n⚠ {warn}"
            return base
        return self.get_tooltip(self._state, lang=self._lang, uipi_warning=self._uipi_warning)

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
            tip = (self._tooltip or self._compose_tooltip_locked())[:127]
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
            lang = self._lang
        menu = win32gui.CreatePopupMenu()
        try:
            for cmd_id, label, is_sep in self.build_menu_items(active, lang=lang):
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
                elif cmd_id == self.ID_LAUNCH_BORDERLESS:
                    self._dispatch_cb(self.on_launch_borderless)
                elif cmd_id == self.ID_OPEN_DIAG:
                    self._dispatch_cb(self.on_open_diagnostics)
                elif cmd_id == self.ID_OPEN_CONFIG:
                    self._dispatch_cb(self.on_open_config_dir)
                elif cmd_id == self.ID_SUPPORT:
                    self._dispatch_cb(self.on_open_support)
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
            self._custom_tooltip = tooltip or ""
            self._state = norm
            tip = self._compose_tooltip_locked()
            if self._last_applied_state == norm and self._tooltip == tip and self._hwnd is not None:
                return
            self._tooltip = tip
            self._last_applied_state = norm
            hwnd = self._hwnd
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, self.WM_APP_UPDATE_STATE, 0, 0)
            except Exception:
                pass

    def set_uipi_warning(self, mismatch: bool) -> None:
        """Appends or clears the localized Windows UIPI Admin warning on the tray tooltip."""
        flag = bool(mismatch)
        with self._lock:
            if self._uipi_warning == flag:
                return
            self._uipi_warning = flag
            self._tooltip = self._compose_tooltip_locked()
            hwnd = self._hwnd
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, self.WM_APP_UPDATE_STATE, 0, 0)
            except Exception:
                pass

    def set_language(self, lang: Optional[str] = None) -> None:
        """Updates tray language override (`None` follows active `src.i18n` language) and refreshes tooltip."""
        with self._lock:
            self._lang = lang
            tip = self._compose_tooltip_locked()
            if self._tooltip == tip and self._hwnd is not None:
                return
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
            thread = self._thread
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, self.WM_APP_CLOSE, 0, 0)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None
