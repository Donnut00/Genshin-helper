from collections import OrderedDict
import ctypes
from ctypes import wintypes
import logging
import threading
from typing import Any, List, Optional, Tuple
import cv2
import numpy as np
import win32gui
import win32con
import win32api
import win32process

logger = logging.getLogger("GenshinHelper.Capture")

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
UOI_NAME = 2
DESKTOP_ALL_ACCESS = 0x01FF

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.BYTE * 4),
    ]


_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

_user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
_user32.GetThreadDesktop.restype = wintypes.HANDLE
_user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetUserObjectInformationW.restype = wintypes.BOOL
_user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.OpenDesktopW.restype = wintypes.HANDLE
_user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
_user32.SetThreadDesktop.restype = wintypes.BOOL
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
_user32.CloseDesktop.restype = wintypes.BOOL
_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.GetWindowDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype = wintypes.BOOL

_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL
_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
_gdi32.BitBlt.restype = wintypes.BOOL
_gdi32.GdiFlush.argtypes = []
_gdi32.GdiFlush.restype = wintypes.BOOL
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL

_dpi_initialized = False
_dpi_lock = threading.Lock()
_desktop_local = threading.local()


def _ensure_dpi_awareness() -> None:
    """Enables per-monitor DPI awareness once so physical pixel rects match GDI BitBlt."""
    global _dpi_initialized
    if _dpi_initialized:
        return
    with _dpi_lock:
        if _dpi_initialized:
            return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                _user32.SetProcessDPIAware()
            except Exception:
                pass
        _dpi_initialized = True


def _ensure_default_desktop() -> None:
    """Attaches the calling thread to the interactive 'Default' desktop at most once."""
    if getattr(_desktop_local, "attached", False):
        return
    try:
        h_cur = _user32.GetThreadDesktop(_kernel32.GetCurrentThreadId())
        if h_cur:
            name_buf = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD(0)
            if _user32.GetUserObjectInformationW(
                h_cur, UOI_NAME, name_buf, ctypes.sizeof(name_buf), ctypes.byref(needed)
            ):
                if name_buf.value.strip().lower() == "default":
                    _desktop_local.attached = True
                    return

        h_def = _user32.OpenDesktopW("Default", 0, False, DESKTOP_ALL_ACCESS)
        if h_def:
            if _user32.SetThreadDesktop(h_def):
                # Active thread desktop handles cannot be closed until the thread switches/exits.
                _desktop_local.h_def = h_def
                _desktop_local.attached = True
            else:
                _user32.CloseDesktop(h_def)
    except Exception:
        pass


class WindowFinder:
    @staticmethod
    def get_game_window(
        titles: Tuple[str, ...],
        allow_screen_fallback: bool = False,
    ) -> Optional[Tuple[int, int, int, int, int]]:
        """
        Robust Genshin Impact window finder:
        1. Checks active foreground window (when player is playing)
        2. Enumerates visible windows to find UnityWndClass / Game Titles (picking the largest area)
        3. Optional fallback: Full primary screen dimensions only when allow_screen_fallback=True
        """
        try:
            _ensure_default_desktop()

            def is_ignored(title_str: str, class_str: str) -> bool:
                tl = title_str.lower()
                if any(x in tl for x in ("helper", "hoyolab sync", "visual studio", "code", "cmd.exe", "powershell")):
                    return True
                if class_str in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
                    return True
                return False

            # 1. Check active foreground window
            fg_hwnd = win32gui.GetForegroundWindow()
            if fg_hwnd and win32gui.IsWindowVisible(fg_hwnd) and not win32gui.IsIconic(fg_hwnd):
                class_name = win32gui.GetClassName(fg_hwnd)
                fg_title = win32gui.GetWindowText(fg_hwnd).strip()
                if not is_ignored(fg_title, class_name):
                    if class_name == "UnityWndClass" or fg_title in titles or any(t.lower() == fg_title.lower() for t in titles):
                        rect = WindowFinder._get_rect(fg_hwnd)
                        if rect:
                            return (fg_hwnd, *rect)

            # 2. Enumerate visible windows and pick the game window with the largest area
            candidates = []
            def enum_win(hwnd, _):
                if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
                    cls = win32gui.GetClassName(hwnd)
                    title = win32gui.GetWindowText(hwnd).strip()
                    if is_ignored(title, cls):
                        return True
                    if cls == "UnityWndClass" or any(t.lower() == title.lower() for t in titles) or (any(t.lower() in title.lower() for t in titles) and "hoyoplay" not in title.lower()):
                        rect = WindowFinder._get_rect(hwnd)
                        if rect:
                            area = rect[2] * rect[3]
                            candidates.append((area, hwnd, *rect))
                return True

            win32gui.EnumWindows(enum_win, None)
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                best = candidates[0]
                return (best[1], best[2], best[3], best[4], best[5])

            if allow_screen_fallback:
                screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
                screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
                return (0, 0, 0, screen_w, screen_h)
            return None

        except Exception as e:
            logger.debug(f"Window lookup error: {e}")
            if allow_screen_fallback:
                screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
                screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
                return (0, 0, 0, screen_w, screen_h)
            return None

    @staticmethod
    def is_window_foreground(hwnd: int) -> bool:
        """Returns True iff hwnd is currently the active foreground window."""
        if not hwnd:
            return False
        try:
            return bool(win32gui.GetForegroundWindow() == hwnd)
        except Exception:
            return False

    # Foreground windows that never hide the game (desktop, taskbars, screenshot snips, GPU/game overlays)
    _NON_COVERING_CLASSES = frozenset({
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "ScreenClippingHost",
        "SnippingTool",
        "XamlExplorerHostIslandWindow",
        "CEF-OSC-WIDGET",
        "NVOverlayWindow",
    })
    _NON_COVERING_PROCESSES = frozenset({
        "screenclippinghost.exe",
        "snippingtool.exe",
        "nvidia overlay.exe",
        "nvsphelper64.exe",
        "gamebar.exe",
        "sharex.exe",
        "lightshot.exe",
        "flameshot.exe",
    })

    @staticmethod
    def _get_hwnd_process_basename(hwnd: int) -> str:
        """Returns lowercase executable basename for a Win32 HWND, or empty string on failure."""
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            return ""
        try:
            buf_len = wintypes.DWORD(520)
            buf = ctypes.create_unicode_buffer(520)
            if _kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(buf_len)):
                path_val = buf.value.strip()
                if path_val:
                    return path_val.replace("/", "\\").rsplit("\\", 1)[-1].lower()
            return ""
        finally:
            _kernel32.CloseHandle(h_proc)

    @staticmethod
    def foreground_covers_game(
        game_rect: Tuple[int, int, int, int],
        fg_rect: Optional[Tuple[int, int, int, int]],
        threshold: float = 0.10,
    ) -> bool:
        """
        True when the foreground window (screen rect `(l, t, r, b)`) hides more than `threshold`
        of the game client area `(left, top, width, height)`: typical 1-monitor Alt+Tab.
        A foreground window on another monitor (2-monitor setup) does not cover the game.
        """
        if fg_rect is None:
            return False
        gl, gt, gw, gh = game_rect
        if gw <= 0 or gh <= 0:
            return False
        fl, ft, fr, fb = fg_rect
        ix = max(0, min(gl + gw, fr) - max(gl, fl))
        iy = max(0, min(gt + gh, fb) - max(gt, ft))
        return (ix * iy) / float(gw * gh) > float(threshold)

    @staticmethod
    def is_game_visible_unfocused(
        hwnd: int,
        game_rect: Tuple[int, int, int, int],
        own_hwnds: Tuple[Optional[int], ...] = (),
        keep_when_unfocused: bool = False,
    ) -> bool:
        """
        True when the game is unfocused but the Mini-HUD should remain visible (not minimized,
        and either capture/demo mode keeps it up, the foreground window is a capture/GPU overlay,
        or the foreground window is on another monitor).
        """
        if not hwnd:
            return False
        try:
            if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd) or not win32gui.IsWindowVisible(hwnd):
                return False
            if keep_when_unfocused:
                return True
            fg = win32gui.GetForegroundWindow()
            if not fg or fg == hwnd or fg in {h for h in own_hwnds if h}:
                return True
            try:
                if win32gui.GetClassName(fg) in WindowFinder._NON_COVERING_CLASSES:
                    return True
            except Exception:
                pass
            if WindowFinder._get_hwnd_process_basename(fg) in WindowFinder._NON_COVERING_PROCESSES:
                return True
            if win32gui.IsIconic(fg):
                return True
            return not WindowFinder.foreground_covers_game(game_rect, tuple(win32gui.GetWindowRect(fg)))
        except Exception:
            return False

    @staticmethod
    def _get_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
        try:
            client_rect = win32gui.GetClientRect(hwnd)
            point = win32gui.ClientToScreen(hwnd, (0, 0))
            left, top = point[0], point[1]
            width, height = client_rect[2], client_rect[3]
            if width >= 1000 and height >= 600:
                return (left, top, width, height)
            
            # Fallback to WindowRect
            wrect = win32gui.GetWindowRect(hwnd)
            ww = wrect[2] - wrect[0]
            wh = wrect[3] - wrect[1]
            if ww >= 1000 and wh >= 600:
                return (wrect[0], wrect[1], ww, wh)
            return None
        except Exception:
            return None

    @staticmethod
    def get_screen_dimensions() -> Tuple[int, int]:
        screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        return screen_w, screen_h

    @staticmethod
    def get_browser_window() -> Optional[Tuple[int, int, int, int, int]]:
        """
        Locates the browser window showing HoYoLAB Interactive Map:
        1. Searches for windows whose title matches HoYoLAB / Interactive Map keywords.
        2. Searches for common browser classes (Chrome_WidgetWin_1, MozillaWindowClass) with large visible area.
        Returns (hwnd, left, top, width, height) or None.
        """
        try:
            browser_keywords = ("hoyolab", "interactive map", "bản đồ tương tác", "genshin helper")
            browser_classes = ("chrome_widgetwin_1", "mozillawindowclass")

            candidates_specific = []
            candidates_generic_browser = []

            def enum_browser(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                    return True
                
                title = win32gui.GetWindowText(hwnd).strip().lower()
                cls = win32gui.GetClassName(hwnd).strip().lower()

                if not title:
                    return True

                rect = WindowFinder._get_rect(hwnd)
                if not rect or rect[2] < 500 or rect[3] < 400:
                    # Try raw WindowRect if client rect was too small
                    try:
                        wr = win32gui.GetWindowRect(hwnd)
                        ww = wr[2] - wr[0]
                        wh = wr[3] - wr[1]
                        if ww >= 500 and wh >= 400:
                            rect = (wr[0], wr[1], ww, wh)
                    except Exception:
                        pass

                if not rect:
                    return True

                area = rect[2] * rect[3]

                # Match specific map title
                if any(kw in title for kw in browser_keywords) and "genshin impact" not in title:
                    candidates_specific.append((area, hwnd, *rect))
                # Match browser window class
                elif any(bc in cls for bc in browser_classes) or any(bname in title for bname in ("chrome", "edge", "firefox", "brave", "opera", "cốc cốc")):
                    if "genshin impact" not in title and "code" not in title and "visual studio" not in title:
                        candidates_generic_browser.append((area, hwnd, *rect))

                return True

            win32gui.EnumWindows(enum_browser, None)

            if candidates_specific:
                candidates_specific.sort(key=lambda x: x[0], reverse=True)
                best = candidates_specific[0]
                return (best[1], best[2], best[3], best[4], best[5])

            if candidates_generic_browser:
                candidates_generic_browser.sort(key=lambda x: x[0], reverse=True)
                best = candidates_generic_browser[0]
                return (best[1], best[2], best[3], best[4], best[5])

            return None
        except Exception as e:
            logger.debug(f"Browser lookup error: {e}")
            return None

    @staticmethod
    def get_monitor_count() -> int:
        """Returns the number of active display monitors (defaults to 1 on failure)."""
        try:
            return max(1, int(win32api.GetSystemMetrics(win32con.SM_CMONITORS)))
        except Exception:
            return 1

    @staticmethod
    def _normal_window_area(hwnd: int) -> int:
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
            rc = placement[4]
            return max(0, rc[2] - rc[0]) * max(0, rc[3] - rc[1])
        except Exception:
            try:
                wr = win32gui.GetWindowRect(hwnd)
                return max(0, wr[2] - wr[0]) * max(0, wr[3] - wr[1])
            except Exception:
                return 0

    @staticmethod
    def find_game_window_any_state(titles: Tuple[str, ...]) -> Optional[int]:
        """Finds Genshin Impact HWND even if minimized (for Single-Monitor quick switch)."""
        active = WindowFinder.get_game_window(titles)
        if active:
            return int(active[0])
        try:
            _ensure_default_desktop()
            candidates: List[Tuple[int, int]] = []
            lower_titles = tuple(t.lower() for t in titles)

            def enum_win(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd).strip()
                tl = title.lower()
                if any(x in tl for x in ("helper", "hoyolab sync", "visual studio", "code", "cmd.exe", "powershell")):
                    return True
                if cls in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
                    return True
                if (
                    cls == "UnityWndClass"
                    or tl in lower_titles
                    or (any(t in tl for t in lower_titles) and "hoyoplay" not in tl)
                ):
                    area = WindowFinder._normal_window_area(hwnd)
                    candidates.append((area, int(hwnd)))
                return True

            win32gui.EnumWindows(enum_win, None)
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1]
        except Exception as e:
            logger.debug(f"Any-state game window lookup error: {e}")
        return None

    @staticmethod
    def find_browser_window_any_state() -> Optional[int]:
        """Finds HoYoLAB Interactive Map browser HWND even if minimized."""
        active = WindowFinder.get_browser_window()
        if active:
            return int(active[0])
        try:
            _ensure_default_desktop()
            browser_keywords = ("hoyolab", "interactive map", "bản đồ tương tác", "genshin helper")
            browser_classes = ("chrome_widgetwin_1", "mozillawindowclass")
            candidates_specific: List[Tuple[int, int]] = []
            candidates_generic: List[Tuple[int, int]] = []

            def enum_browser(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd).strip().lower()
                cls = win32gui.GetClassName(hwnd).strip().lower()
                if not title:
                    return True
                area = WindowFinder._normal_window_area(hwnd)
                if area < 500 * 400:
                    return True
                if any(kw in title for kw in browser_keywords) and "genshin impact" not in title:
                    candidates_specific.append((area, int(hwnd)))
                elif any(bc in cls for bc in browser_classes) or any(
                    bname in title for bname in ("chrome", "edge", "firefox", "brave", "opera", "cốc cốc")
                ):
                    if "genshin impact" not in title and "code" not in title and "visual studio" not in title:
                        candidates_generic.append((area, int(hwnd)))
                return True

            win32gui.EnumWindows(enum_browser, None)
            if candidates_specific:
                candidates_specific.sort(key=lambda x: x[0], reverse=True)
                return candidates_specific[0][1]
            if candidates_generic:
                candidates_generic.sort(key=lambda x: x[0], reverse=True)
                return candidates_generic[0][1]
        except Exception as e:
            logger.debug(f"Any-state browser window lookup error: {e}")
        return None

    @staticmethod
    def focus_window(hwnd: int) -> bool:
        """Brings hwnd to foreground using pure Win32 window calls (zero input injection)."""
        if not hwnd:
            return False
        try:
            if not win32gui.IsWindow(hwnd):
                return False
            _ensure_default_desktop()
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

            try:
                _user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
            except Exception:
                pass

            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass

            if win32gui.GetForegroundWindow() == hwnd:
                return True

            fg_hwnd = win32gui.GetForegroundWindow()
            cur_tid = _kernel32.GetCurrentThreadId()
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0

            attached_pairs: List[Tuple[int, int]] = []
            try:
                if fg_tid and fg_tid != cur_tid:
                    if win32process.AttachThreadInput(cur_tid, fg_tid, True):
                        attached_pairs.append((cur_tid, fg_tid))
                if target_tid and target_tid != cur_tid and target_tid != fg_tid:
                    if win32process.AttachThreadInput(cur_tid, target_tid, True):
                        attached_pairs.append((cur_tid, target_tid))
                if fg_tid and target_tid and fg_tid != target_tid:
                    if win32process.AttachThreadInput(fg_tid, target_tid, True):
                        attached_pairs.append((fg_tid, target_tid))

                try:
                    win32gui.BringWindowToTop(hwnd)
                except Exception:
                    pass
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            finally:
                for tid_a, tid_b in reversed(attached_pairs):
                    try:
                        win32process.AttachThreadInput(tid_a, tid_b, False)
                    except Exception:
                        pass

            return bool(win32gui.GetForegroundWindow() == hwnd)
        except Exception as e:
            logger.debug(f"focus_window({hwnd}) error: {e}")
            return False


class _DibSlot:
    """Single reusable top-down 32-bit DIBSection entry inside `_GdiSession`."""

    __slots__ = ("width", "height", "dib", "dib_buf", "bgra_view")

    def __init__(
        self,
        width: int,
        height: int,
        dib: int,
        dib_buf: Any,
        bgra_view: np.ndarray,
    ) -> None:
        self.width = width
        self.height = height
        self.dib = dib
        self.dib_buf = dib_buf
        self.bgra_view = bgra_view


class _GdiSession:
    """Thread-bound GDI BitBlt session with an LRU Multi-Slot DIBSection pool."""

    DEFAULT_MAX_SLOTS = 4

    def __init__(self, max_slots: int = DEFAULT_MAX_SLOTS) -> None:
        self.srcdc = _user32.GetWindowDC(0)
        if not self.srcdc:
            raise RuntimeError("GetWindowDC(0) failed")
        self.memdc = _gdi32.CreateCompatibleDC(self.srcdc)
        if not self.memdc:
            _user32.ReleaseDC(0, self.srcdc)
            self.srcdc = None
            raise RuntimeError("CreateCompatibleDC failed")
        self.max_slots = max(1, int(max_slots))
        self._slots: "OrderedDict[Tuple[int, int], _DibSlot]" = OrderedDict()
        self._default_bitmap = None
        self._active_size: Optional[Tuple[int, int]] = None
        self.size: Optional[Tuple[int, int]] = None
        self.bgra_view: Optional[np.ndarray] = None
        self.dib_alloc_count: int = 0
        self.closed = False

    def _evict_lru_slot(self) -> None:
        if not self._slots:
            return
        evict_key, evict_slot = self._slots.popitem(last=False)
        if self._active_size == evict_key and self._default_bitmap is not None:
            _gdi32.SelectObject(self.memdc, self._default_bitmap)
            self._active_size = None
            self.size = None
            self.bgra_view = None
        if evict_slot.dib:
            _gdi32.DeleteObject(evict_slot.dib)
            evict_slot.dib = None
        evict_slot.dib_buf = None
        evict_slot.bgra_view = None

    def _ensure_dib(self, width: int, height: int) -> _DibSlot:
        key = (width, height)
        slot = self._slots.get(key)
        if slot is not None:
            self._slots.move_to_end(key)
            if self._active_size != key:
                prev = _gdi32.SelectObject(self.memdc, slot.dib)
                if not prev:
                    raise OSError(ctypes.get_last_error(), "SelectObject failed")
                self._active_size = key
            self.size = key
            self.bgra_view = slot.bgra_view
            return slot

        while len(self._slots) >= self.max_slots:
            self._evict_lru_slot()

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # Top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0

        bits = ctypes.c_void_p()
        dib = _gdi32.CreateDIBSection(
            self.memdc,
            ctypes.byref(bmi),
            DIB_RGB_COLORS,
            ctypes.byref(bits),
            None,
            0,
        )
        if not dib or not bits.value:
            raise OSError(ctypes.get_last_error(), "CreateDIBSection failed")

        prev_obj = _gdi32.SelectObject(self.memdc, dib)
        if not prev_obj:
            _gdi32.DeleteObject(dib)
            raise OSError(ctypes.get_last_error(), "SelectObject failed")
        if self._default_bitmap is None:
            self._default_bitmap = prev_obj

        buf_type = ctypes.c_ubyte * (width * height * 4)
        dib_buf = buf_type.from_address(bits.value)
        bgra_view = np.ctypeslib.as_array(dib_buf).reshape((height, width, 4))
        slot = _DibSlot(width=width, height=height, dib=dib, dib_buf=dib_buf, bgra_view=bgra_view)
        self._slots[key] = slot
        self._active_size = key
        self.size = key
        self.bgra_view = bgra_view
        self.dib_alloc_count += 1
        return slot

    def _bitblt_into_slot(self, left: int, top: int, width: int, height: int) -> _DibSlot:
        if self.closed:
            raise RuntimeError("GDI session is closed")
        slot = self._ensure_dib(width, height)
        ctypes.set_last_error(0)
        # DECISION: Use plain SRCCOPY (0x00CC0020) without CAPTUREBLT (0x40000000).
        # CAPTUREBLT causes DWM to hide/re-composite the hardware mouse cursor on every BitBlt,
        # producing rapid cursor flicker during continuous Map M polling.
        ok = _gdi32.BitBlt(self.memdc, 0, 0, width, height, self.srcdc, left, top, SRCCOPY)
        if not ok:
            raise OSError(ctypes.get_last_error(), "BitBlt failed")
        _gdi32.GdiFlush()
        return slot

    def grab_bgr(self, left: int, top: int, width: int, height: int) -> np.ndarray:
        slot = self._bitblt_into_slot(left, top, width, height)
        return cv2.cvtColor(slot.bgra_view, cv2.COLOR_BGRA2BGR)

    def grab_bgra(self, left: int, top: int, width: int, height: int) -> np.ndarray:
        slot = self._bitblt_into_slot(left, top, width, height)
        return slot.bgra_view.copy()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.memdc and self._default_bitmap is not None:
                _gdi32.SelectObject(self.memdc, self._default_bitmap)
        except Exception:
            pass
        self._default_bitmap = None
        self._active_size = None
        for slot in list(self._slots.values()):
            try:
                if slot.dib:
                    _gdi32.DeleteObject(slot.dib)
            except Exception:
                pass
            slot.dib = None
            slot.dib_buf = None
            slot.bgra_view = None
        self._slots.clear()
        self.bgra_view = None
        self.size = None
        try:
            if self.memdc:
                _gdi32.DeleteDC(self.memdc)
        except Exception:
            pass
        self.memdc = None
        try:
            if self.srcdc:
                _user32.ReleaseDC(0, self.srcdc)
        except Exception:
            pass
        self.srcdc = None


class ScreenCapturer:
    BITBLT_ROP = SRCCOPY

    def __init__(self, mode: str = "auto", max_dib_slots: int = _GdiSession.DEFAULT_MAX_SLOTS):
        _ensure_dpi_awareness()
        self.mode = mode
        self.max_dib_slots = max(1, int(max_dib_slots))
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: List[_GdiSession] = []
        logger.info("Thread-safe flicker-free Screen capture initialized via GDI SRCCOPY (no CAPTUREBLT).")

    def _get_session(self) -> _GdiSession:
        _ensure_default_desktop()
        session = getattr(self._local, "session", None)
        if session is None or session.closed:
            session = _GdiSession(max_slots=self.max_dib_slots)
            with self._sessions_lock:
                self._sessions.append(session)
            self._local.session = session
        return session

    def _capture_with_recovery(
        self,
        left: int,
        top: int,
        width: int,
        height: int,
        *,
        bgra: bool,
    ) -> Optional[np.ndarray]:
        left = int(left)
        top = int(top)
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return None

        try:
            session = self._get_session()
            if bgra:
                return session.grab_bgra(left, top, width, height)
            return session.grab_bgr(left, top, width, height)
        except Exception as e:
            logger.warning(f"Frame capture error on thread {threading.current_thread().name}: {e}")
            session = getattr(self._local, "session", None)
            if session is not None:
                with self._sessions_lock:
                    try:
                        self._sessions.remove(session)
                    except ValueError:
                        pass
                try:
                    session.close()
                except Exception:
                    pass
            self._local.session = None
            return None

    def capture_region(self, left: int, top: int, width: int, height: int) -> Optional[np.ndarray]:
        """Captures a region of the screen and returns an independent BGR ndarray."""
        return self._capture_with_recovery(left, top, width, height, bgra=False)

    def capture_region_bgra(self, left: int, top: int, width: int, height: int) -> Optional[np.ndarray]:
        """Captures a region of the screen and returns an independent BGRA ndarray (preserves alpha)."""
        return self._capture_with_recovery(left, top, width, height, bgra=True)

    def close(self):
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass
        self._local.session = None
