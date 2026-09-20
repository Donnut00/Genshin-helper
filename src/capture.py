import logging
import time
from typing import Optional, Tuple
import cv2
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
        2. Enumerates visible windows to find UnityWndClass / Game Titles (picking the largest area)
        3. Fallback: Full primary screen dimensions
        """
        try:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                h_def = user32.OpenDesktopW('Default', 0, False, 0x01FF)
                if h_def:
                    user32.SetThreadDesktop(h_def)
            except Exception:
                pass

            def is_ignored(title_str: str, class_str: str) -> bool:
                tl = title_str.lower()
                if any(x in tl for x in ("helper", "hoyolab sync", "visual studio", "code", "cmd.exe", "powershell")):
                    return True
                if class_str in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
                    return True
                return False

            # 1. Check active foreground window
            fg_hwnd = win32gui.GetForegroundWindow()
            if fg_hwnd and win32gui.IsWindowVisible(fg_hwnd):
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
                if win32gui.IsWindowVisible(hwnd):
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

            # 3. Fallback to Primary Screen if running in exclusive fullscreen
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
            bgra = np.frombuffer(screenshot.raw, dtype=np.uint8).reshape((int(height), int(width), 4))
            frame = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
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
