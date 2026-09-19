import logging
import time
from typing import Optional, Tuple
import numpy as np
import win32gui
import win32con
import win32api
import mss

logger = logging.getLogger("GenshinHelper.Capture")

class WindowFinder:
    @staticmethod
    def get_game_window(titles: Tuple[str, ...]) -> Optional[Tuple[int, int, int, int, int]]:
        """
        Robust Genshin Impact window finder:
        1. Checks active foreground window (when player is playing)
        2. Searches by UnityWndClass
        3. Searches by Window Titles
        4. Fallback: Full primary screen dimensions
        """
        try:
            # 1. Check active foreground window
            fg_hwnd = win32gui.GetForegroundWindow()
            if fg_hwnd and win32gui.IsWindowVisible(fg_hwnd):
                class_name = win32gui.GetClassName(fg_hwnd)
                fg_title = win32gui.GetWindowText(fg_hwnd)
                if class_name == "UnityWndClass" or any(t.lower() in fg_title.lower() for t in titles):
                    rect = WindowFinder._get_rect(fg_hwnd)
                    if rect:
                        return (fg_hwnd, *rect)

            # 2. Check UnityWndClass
            hwnd = win32gui.FindWindow("UnityWndClass", None)
            if hwnd and win32gui.IsWindowVisible(hwnd):
                rect = WindowFinder._get_rect(hwnd)
                if rect:
                    return (hwnd, *rect)

            # 3. Check by titles
            for title in titles:
                hwnd = win32gui.FindWindow(None, title)
                if hwnd and win32gui.IsWindowVisible(hwnd):
                    rect = WindowFinder._get_rect(hwnd)
                    if rect:
                        return (hwnd, *rect)

            # 4. Fallback to Primary Screen if running in exclusive fullscreen
            screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            return (0, 0, 0, screen_w, screen_h)

        except Exception as e:
            logger.debug(f"Window lookup error: {e}")
            screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            return (0, 0, 0, screen_w, screen_h)

    @staticmethod
    def _get_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
        try:
            client_rect = win32gui.GetClientRect(hwnd)
            point = win32gui.ClientToScreen(hwnd, (0, 0))
            left, top = point[0], point[1]
            width, height = client_rect[2], client_rect[3]
            if width > 300 and height > 300:
                return (left, top, width, height)
            
            # Fallback to WindowRect
            wrect = win32gui.GetWindowRect(hwnd)
            return (wrect[0], wrect[1], wrect[2] - wrect[0], wrect[3] - wrect[1])
        except Exception:
            return None


import ctypes
import threading

user32 = ctypes.windll.user32

class ScreenCapturer:
    def __init__(self, mode: str = "auto"):
        self._local = threading.local()
        logger.info("Thread-safe Screen capture initialized via MSS.")

    def _get_sct(self) -> mss.mss:
        if not hasattr(self._local, "sct") or self._local.sct is None:
            try:
                h_def = user32.OpenDesktopW('Default', 0, False, 0x01FF)
                if h_def:
                    user32.SetThreadDesktop(h_def)
            except Exception:
                pass
            self._local.sct = mss.mss()
        return self._local.sct

    def capture_region(self, left: int, top: int, width: int, height: int) -> Optional[np.ndarray]:
        """Captures a region of the screen and returns BGR ndarray."""
        if width <= 0 or height <= 0:
            return None

        left = max(0, int(left))
        top = max(0, int(top))

        try:
            sct = self._get_sct()
            monitor = {"left": left, "top": top, "width": int(width), "height": int(height)}
            screenshot = sct.grab(monitor)
            frame = np.array(screenshot, dtype=np.uint8)[:, :, :3]
            return frame
        except Exception as e:
            logger.warning(f"Frame capture error on thread {threading.current_thread().name}: {e}")
            try:
                if hasattr(self._local, "sct") and self._local.sct:
                    self._local.sct.close()
            except Exception:
                pass
            self._local.sct = None
            return None

    def close(self):
        try:
            if hasattr(self._local, "sct") and self._local.sct:
                self._local.sct.close()
                self._local.sct = None
        except Exception:
            pass
