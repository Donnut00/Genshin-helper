import ctypes
from ctypes import wintypes
import logging
import threading
from typing import List, Optional, Tuple
import cv2
import numpy as np
import win32gui
import win32con
import win32api

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


class _GdiSession:
    """Thread-bound GDI BitBlt session with reused top-down DIBSection."""

    def __init__(self) -> None:
        self.srcdc = _user32.GetWindowDC(0)
        if not self.srcdc:
            raise RuntimeError("GetWindowDC(0) failed")
        self.memdc = _gdi32.CreateCompatibleDC(self.srcdc)
        if not self.memdc:
            _user32.ReleaseDC(0, self.srcdc)
            self.srcdc = None
            raise RuntimeError("CreateCompatibleDC failed")
        self.size: Optional[Tuple[int, int]] = None
        self.dib = None
        self.old_obj = None
        self._dib_buf = None
        self.bgra_view: Optional[np.ndarray] = None
        self.closed = False

    def _ensure_dib(self, width: int, height: int) -> None:
        if self.size == (width, height) and self.bgra_view is not None:
            return
        if self.dib:
            if self.old_obj is not None:
                _gdi32.SelectObject(self.memdc, self.old_obj)
                self.old_obj = None
            _gdi32.DeleteObject(self.dib)
            self.dib = None
            self._dib_buf = None
            self.bgra_view = None
            self.size = None

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

        old_obj = _gdi32.SelectObject(self.memdc, dib)
        if not old_obj:
            _gdi32.DeleteObject(dib)
            raise OSError(ctypes.get_last_error(), "SelectObject failed")

        buf_type = ctypes.c_ubyte * (width * height * 4)
        self._dib_buf = buf_type.from_address(bits.value)
        self.bgra_view = np.ctypeslib.as_array(self._dib_buf).reshape((height, width, 4))
        self.dib = dib
        self.old_obj = old_obj
        self.size = (width, height)

    def grab_bgr(self, left: int, top: int, width: int, height: int) -> np.ndarray:
        if self.closed:
            raise RuntimeError("GDI session is closed")
        self._ensure_dib(width, height)
        ctypes.set_last_error(0)
        # DECISION: Use plain SRCCOPY (0x00CC0020) without CAPTUREBLT (0x40000000).
        # CAPTUREBLT causes DWM to hide/re-composite the hardware mouse cursor on every BitBlt,
        # producing rapid cursor flicker during continuous Map M polling.
        ok = _gdi32.BitBlt(self.memdc, 0, 0, width, height, self.srcdc, left, top, SRCCOPY)
        if not ok:
            raise OSError(ctypes.get_last_error(), "BitBlt failed")
        _gdi32.GdiFlush()
        return cv2.cvtColor(self.bgra_view, cv2.COLOR_BGRA2BGR)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.memdc and self.old_obj is not None:
                _gdi32.SelectObject(self.memdc, self.old_obj)
        except Exception:
            pass
        self.old_obj = None
        try:
            if self.dib:
                _gdi32.DeleteObject(self.dib)
        except Exception:
            pass
        self.dib = None
        self._dib_buf = None
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

    def __init__(self, mode: str = "auto"):
        _ensure_dpi_awareness()
        self.mode = mode
        self._local = threading.local()
        self._sessions_lock = threading.Lock()
        self._sessions: List[_GdiSession] = []
        logger.info("Thread-safe flicker-free Screen capture initialized via GDI SRCCOPY (no CAPTUREBLT).")

    def _get_session(self) -> _GdiSession:
        _ensure_default_desktop()
        session = getattr(self._local, "session", None)
        if session is None or session.closed:
            session = _GdiSession()
            with self._sessions_lock:
                self._sessions.append(session)
            self._local.session = session
        return session

    def capture_region(self, left: int, top: int, width: int, height: int) -> Optional[np.ndarray]:
        """Captures a region of the screen and returns BGR ndarray."""
        left = int(left)
        top = int(top)
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return None

        try:
            session = self._get_session()
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
