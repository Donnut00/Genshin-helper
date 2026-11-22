import ctypes
from ctypes import wintypes
import logging
import os
import queue
import threading
import time
from typing import Any, Dict, Optional, Tuple
import winsound

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import win32api
import win32con
import win32gui

from src.capture import WindowFinder

logger = logging.getLogger("GenshinHelper.SingleMonitor")


class AudioFeedback:
    """
    Non-blocking 3-status audio chime worker using winsound.Beep.
    Worker thread starts lazily on first play() so 2-monitor mode incurs zero overhead.
    """

    TONES: Dict[str, Tuple[Tuple[int, int], ...]] = {
        "success": ((784, 70), (1047, 110)),
        "info": ((659, 65), (659, 65)),
        "warning": ((440, 85), (311, 130)),
        "error": ((370, 95), (277, 150)),
    }

    def __init__(self) -> None:
        self._queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=4)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="GH_AudioFeedback", daemon=True)
                self._thread.start()

    def _worker(self) -> None:
        while True:
            kind = self._queue.get()
            if kind is None:
                break
            seq = self.TONES.get(kind, self.TONES["info"])
            for freq, dur in seq:
                if self._closed:
                    break
                try:
                    winsound.Beep(int(freq), int(dur))
                except Exception:
                    pass

    def play(self, kind: str = "info") -> None:
        if self._closed:
            return
        self._ensure_worker()
        norm_kind = kind if kind in self.TONES else "info"
        while True:
            try:
                self._queue.put_nowait(norm_kind)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    return

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            t = self._thread
        if t is not None and t.is_alive():
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            t.join(timeout=0.5)
        self._thread = None


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
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


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE

_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_user32.UnregisterClassW.restype = wintypes.BOOL
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = _LRESULT
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.TranslateMessage.restype = wintypes.BOOL
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = _LRESULT
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.PostQuitMessage.restype = None
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(_POINT),
    ctypes.POINTER(_SIZE),
    wintypes.HDC,
    ctypes.POINTER(_POINT),
    wintypes.COLORREF,
    ctypes.POINTER(_BLENDFUNCTION),
    wintypes.DWORD,
]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
_user32.KillTimer.restype = wintypes.BOOL

_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL
_gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(_BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL


class HudToastOverlay:
    """
    Click-through, non-activating, per-pixel alpha Win32 layered HUD toast overlay.
    Renders in top-center of the game window so it never touches any CV detection ROI.
    Starts its UI thread lazily on first show_toast() call (zero overhead in 2-monitor mode).
    """

    # Screen-space offset added to every placement. (0, 0) in the app; the test suites set a far
    # off-screen offset so real windows are exercised (handle / lifecycle checks) without ever
    # flashing on the developer's desktop.
    SCREEN_OFFSET: Tuple[int, int] = (0, 0)

    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOPMOST = 0x00000008
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW

    WS_POPUP = 0x80000000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = wintypes.HWND(-1)
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001

    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01
    DIB_RGB_COLORS = 0

    WM_DESTROY = 0x0002
    WM_TIMER = 0x0113
    WM_APP_SHOW = 0x8001
    WM_APP_CLOSE = 0x8002

    TIMER_WAIT = 1
    TIMER_FADE = 2
    FADE_DURATION_MS = 350
    FADE_TICK_MS = 25

    _PALETTES: Dict[str, Dict[str, Tuple[int, int, int, int]]] = {
        "success": {
            "accent": (34, 197, 94, 255),
            "border": (34, 197, 94, 210),
            "badge_bg": (22, 163, 74, 48),
            "title": (248, 250, 252, 255),
            "sub": (148, 163, 184, 255),
        },
        "info": {
            "accent": (56, 189, 248, 255),
            "border": (56, 189, 248, 210),
            "badge_bg": (56, 189, 248, 48),
            "title": (248, 250, 252, 255),
            "sub": (148, 163, 184, 255),
        },
        "warning": {
            "accent": (245, 158, 11, 255),
            "border": (245, 158, 11, 220),
            "badge_bg": (245, 158, 11, 52),
            "title": (254, 243, 199, 255),
            "sub": (203, 213, 225, 255),
        },
        "error": {
            "accent": (239, 68, 68, 255),
            "border": (239, 68, 68, 225),
            "badge_bg": (239, 68, 68, 56),
            "title": (254, 226, 226, 255),
            "sub": (203, 213, 225, 255),
        },
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._hwnd: Optional[int] = None
        self._class_name = f"GenshinHelperHudToast_{id(self)}"
        self._hinst = _kernel32.GetModuleHandleW(None)
        self._wndproc_ref = None
        self._closed = False
        self._seq = 0
        self._pending: Optional[Tuple[int, str, str, str, int, Optional[Tuple[int, int, int, int]]]] = None

        # Thread-owned GDI surface state
        self._memdc: Optional[int] = None
        self._hbmp: Optional[int] = None
        self._old_obj: Optional[int] = None
        self._dib_ptr: Optional[ctypes.c_void_p] = None
        self._dib_size: Tuple[int, int] = (0, 0)
        self._dst_pos: Tuple[int, int] = (0, 0)
        self._show_start = 0.0
        self._total_ms = 2500
        self._visible = False

    @staticmethod
    def _load_font(bold: bool, size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = (
            (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf")
            if bold
            else (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf")
        )
        for path in candidates:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, max(10, int(round(size_px))))
                except Exception:
                    pass
        return ImageFont.load_default()

    @classmethod
    def _truncate_text(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_w: int,
    ) -> str:
        t = " ".join((text or "").split())
        if not t:
            return ""
        bbox = draw.textbbox((0, 0), t, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            return t
        ellipsis = "..."
        while len(t) > 3:
            t = t[:-1].rstrip()
            bbox = draw.textbbox((0, 0), t + ellipsis, font=font)
            if (bbox[2] - bbox[0]) <= max_w:
                return t + ellipsis
        return ellipsis

    @classmethod
    def render_toast_image(
        cls,
        title: str,
        subtitle: str = "",
        kind: str = "info",
        scale: float = 1.0,
    ) -> Image.Image:
        """Renders an anti-aliased dark glass pill toast as an RGBA Pillow image."""
        s = max(0.85, min(1.8, float(scale)))
        pal = cls._PALETTES.get(kind, cls._PALETTES["info"])

        title_font = cls._load_font(bold=True, size_px=int(round(15 * s)))
        sub_font = cls._load_font(bold=False, size_px=int(round(12.5 * s)))

        pad_x = int(round(14 * s))
        pad_y = int(round(10 * s))
        badge_d = int(round(24 * s))
        gap_badge = int(round(11 * s))
        max_text_w = int(round(390 * s))
        min_w = int(round(210 * s))

        probe = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(probe)
        clean_title = cls._truncate_text(pdraw, title or "Genshin Helper", title_font, max_text_w)
        clean_sub = cls._truncate_text(pdraw, subtitle, sub_font, max_text_w) if subtitle else ""

        tb = pdraw.textbbox((0, 0), clean_title, font=title_font)
        title_w, title_h = tb[2] - tb[0], tb[3] - tb[1]
        sub_w = sub_h = 0
        line_gap = int(round(3 * s)) if clean_sub else 0
        if clean_sub:
            sb = pdraw.textbbox((0, 0), clean_sub, font=sub_font)
            sub_w, sub_h = sb[2] - sb[0], sb[3] - sb[1]

        text_block_w = max(title_w, sub_w)
        text_block_h = title_h + line_gap + sub_h

        w = max(min_w, pad_x * 2 + badge_d + gap_badge + text_block_w + int(round(4 * s)))
        h = max(badge_d + pad_y * 2, text_block_h + pad_y * 2)

        # Render rounded card + icon glyph at 2x for smooth anti-aliased edges
        ss = 2
        card_hi = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(card_hi)
        radius = (h * ss) // 2 if not clean_sub else int(round(14 * s * ss))
        border_w = max(2, int(round(1.5 * s * ss)))
        cdraw.rounded_rectangle(
            (1, 1, w * ss - 2, h * ss - 2),
            radius=radius,
            fill=(15, 23, 42, 232),
            outline=pal["border"],
            width=border_w,
        )

        # Status badge circle
        bx0 = pad_x * ss
        by0 = ((h - badge_d) // 2) * ss
        bsz = badge_d * ss
        cdraw.ellipse((bx0, by0, bx0 + bsz, by0 + bsz), fill=pal["badge_bg"], outline=pal["accent"], width=border_w)

        cx = bx0 + bsz * 0.5
        cy = by0 + bsz * 0.5
        stroke = max(2, int(round(2.3 * s * ss)))
        if kind == "success":
            pts = [
                (cx - bsz * 0.22, cy + bsz * 0.02),
                (cx - bsz * 0.04, cy + bsz * 0.20),
                (cx + bsz * 0.24, cy - bsz * 0.18),
            ]
            cdraw.line(pts, fill=pal["accent"], width=stroke, joint="curve")
        elif kind == "warning":
            cdraw.line([(cx, cy - bsz * 0.22), (cx, cy + bsz * 0.06)], fill=pal["accent"], width=stroke)
            r_dot = max(2, int(round(1.6 * s * ss)))
            cdraw.ellipse((cx - r_dot, cy + bsz * 0.16 - r_dot, cx + r_dot, cy + bsz * 0.16 + r_dot), fill=pal["accent"])
        elif kind == "error":
            d = bsz * 0.17
            cdraw.line([(cx - d, cy - d), (cx + d, cy + d)], fill=pal["accent"], width=stroke)
            cdraw.line([(cx - d, cy + d), (cx + d, cy - d)], fill=pal["accent"], width=stroke)
        else:
            r_dot = max(3, int(round(4.0 * s * ss)))
            cdraw.ellipse((cx - r_dot, cy - r_dot, cx + r_dot, cy + r_dot), fill=pal["accent"])

        img = card_hi.resize((w, h), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(img)

        text_x = pad_x + badge_d + gap_badge
        text_top = (h - text_block_h) // 2
        draw.text((text_x - tb[0], text_top - tb[1]), clean_title, font=title_font, fill=pal["title"])
        if clean_sub:
            sub_y = text_top + title_h + line_gap
            draw.text((text_x - sb[0], sub_y - sb[1]), clean_sub, font=sub_font, fill=pal["sub"])

        return img

    @staticmethod
    def rgba_to_premultiplied_bgra(img: Image.Image) -> bytes:
        """Converts RGBA Pillow image into top-down premultiplied BGRA bytes for UpdateLayeredWindow."""
        rgba = np.asarray(img.convert("RGBA"), dtype=np.uint16)
        alpha = rgba[:, :, 3:4]
        rgb_pm = (rgba[:, :, :3] * alpha + 127) // 255
        bgra = np.empty_like(rgba, dtype=np.uint8)
        bgra[:, :, 0] = rgb_pm[:, :, 2]
        bgra[:, :, 1] = rgb_pm[:, :, 1]
        bgra[:, :, 2] = rgb_pm[:, :, 0]
        bgra[:, :, 3] = rgba[:, :, 3]
        return bgra.tobytes()

    @staticmethod
    def compute_toast_position(
        toast_w: int,
        toast_h: int,
        anchor_rect: Optional[Tuple[int, int, int, int]] = None,
    ) -> Tuple[int, int]:
        """Places the toast at top-center (inside the top ~10% band, clear of all CV ROIs)."""
        if anchor_rect and anchor_rect[2] > 200 and anchor_rect[3] > 200:
            left, top, width, height = [int(v) for v in anchor_rect]
        else:
            left = 0
            top = 0
            width = int(win32api.GetSystemMetrics(win32con.SM_CXSCREEN))
            height = int(win32api.GetSystemMetrics(win32con.SM_CYSCREEN))

        x = left + (width - int(toast_w)) // 2
        y = top + max(22, int(round(height * 0.032)))
        return int(x), int(y)

    def _ensure_window_thread(self) -> Optional[int]:
        if self._closed:
            return None
        with self._lock:
            if self._hwnd:
                return self._hwnd
            if self._thread is None or not self._thread.is_alive():
                self._ready_event.clear()
                self._thread = threading.Thread(target=self._ui_thread_main, name="GH_HudToast", daemon=True)
                self._thread.start()
        self._ready_event.wait(timeout=2.0)
        return self._hwnd

    def _release_gdi_surface(self) -> None:
        if self._memdc and self._old_obj:
            try:
                _gdi32.SelectObject(self._memdc, self._old_obj)
            except Exception:
                pass
            self._old_obj = None
        if self._hbmp:
            try:
                _gdi32.DeleteObject(self._hbmp)
            except Exception:
                pass
            self._hbmp = None
        if self._memdc:
            try:
                _gdi32.DeleteDC(self._memdc)
            except Exception:
                pass
            self._memdc = None
        self._dib_ptr = None
        self._dib_size = (0, 0)

    def _upload_image_to_window(self, img: Image.Image, x: int, y: int) -> None:
        if not self._hwnd:
            return
        w, h = img.size
        self._release_gdi_surface()

        memdc = _gdi32.CreateCompatibleDC(None)
        if not memdc:
            return
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # Top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        bits_ptr = ctypes.c_void_p()
        hbmp = _gdi32.CreateDIBSection(memdc, ctypes.byref(bmi), self.DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
        if not hbmp or not bits_ptr.value:
            _gdi32.DeleteDC(memdc)
            return

        old_obj = _gdi32.SelectObject(memdc, hbmp)
        raw = self.rgba_to_premultiplied_bgra(img)
        ctypes.memmove(bits_ptr.value, raw, len(raw))

        self._memdc = memdc
        self._hbmp = hbmp
        self._old_obj = old_obj
        self._dib_ptr = bits_ptr
        self._dib_size = (w, h)
        self._dst_pos = (int(x) + int(self.SCREEN_OFFSET[0]), int(y) + int(self.SCREEN_OFFSET[1]))

        self._apply_layered_frame(alpha=255, show=True)

    def _apply_layered_frame(self, alpha: int, show: bool = False) -> None:
        if not self._hwnd or not self._memdc or self._dib_size == (0, 0):
            return
        w, h = self._dib_size
        x, y = self._dst_pos
        pt_dst = _POINT(x, y)
        pt_src = _POINT(0, 0)
        sz = _SIZE(w, h)
        blend = _BLENDFUNCTION(self.AC_SRC_OVER, 0, max(0, min(255, int(alpha))), self.AC_SRC_ALPHA)
        _user32.UpdateLayeredWindow(
            self._hwnd,
            None,
            ctypes.byref(pt_dst),
            ctypes.byref(sz),
            self._memdc,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(blend),
            self.ULW_ALPHA,
        )
        if show:
            _user32.SetWindowPos(
                self._hwnd,
                self.HWND_TOPMOST,
                x,
                y,
                w,
                h,
                self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW,
            )
            self._visible = True

    def _handle_show_request(self) -> None:
        with self._lock:
            req = self._pending
        if not req or not self._hwnd:
            return
        _, title, subtitle, kind, duration_ms, anchor_rect = req
        _user32.KillTimer(self._hwnd, self.TIMER_WAIT)
        _user32.KillTimer(self._hwnd, self.TIMER_FADE)

        anchor_h = anchor_rect[3] if (anchor_rect and anchor_rect[3] > 200) else int(
            win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        )
        scale = max(0.9, min(1.6, anchor_h / 1080.0))
        img = self.render_toast_image(title=title, subtitle=subtitle, kind=kind, scale=scale)
        x, y = self.compute_toast_position(img.width, img.height, anchor_rect=anchor_rect)
        self._upload_image_to_window(img, x, y)

        self._show_start = time.monotonic()
        self._total_ms = max(600, int(duration_ms))
        wait_ms = max(120, self._total_ms - self.FADE_DURATION_MS)
        _user32.SetTimer(self._hwnd, self.TIMER_WAIT, wait_ms, None)

    def _ui_thread_main(self) -> None:
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == self.WM_APP_SHOW:
                try:
                    self._handle_show_request()
                except Exception as e:
                    logger.debug(f"HUD show error: {e}")
                return 0
            if msg == self.WM_TIMER:
                if wparam == self.TIMER_WAIT:
                    _user32.KillTimer(hwnd, self.TIMER_WAIT)
                    _user32.SetTimer(hwnd, self.TIMER_FADE, self.FADE_TICK_MS, None)
                    return 0
                if wparam == self.TIMER_FADE:
                    elapsed_ms = (time.monotonic() - self._show_start) * 1000.0
                    if elapsed_ms >= self._total_ms:
                        _user32.KillTimer(hwnd, self.TIMER_FADE)
                        _user32.ShowWindow(hwnd, self.SW_HIDE)
                        self._visible = False
                    else:
                        rem = max(0.0, min(1.0, (self._total_ms - elapsed_ms) / float(self.FADE_DURATION_MS)))
                        self._apply_layered_frame(alpha=int(round(255.0 * rem)), show=False)
                    return 0
            if msg == self.WM_APP_CLOSE:
                _user32.KillTimer(hwnd, self.TIMER_WAIT)
                _user32.KillTimer(hwnd, self.TIMER_FADE)
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == self.WM_DESTROY:
                self._release_gdi_surface()
                _user32.PostQuitMessage(0)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = _WNDPROC(wndproc)
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = self._hinst
        wc.lpszClassName = self._class_name

        atom = _user32.RegisterClassW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() != 1410:
            logger.warning(f"HudToast RegisterClassW failed: {ctypes.get_last_error()}")
            self._ready_event.set()
            return

        hwnd = _user32.CreateWindowExW(
            self.EX_STYLE,
            self._class_name,
            "GenshinHelperHUD",
            self.WS_POPUP,
            0,
            0,
            10,
            10,
            None,
            None,
            self._hinst,
            None,
        )
        if not hwnd:
            logger.warning(f"HudToast CreateWindowExW failed: {ctypes.get_last_error()}")
            self._ready_event.set()
            return

        self._hwnd = int(hwnd)
        self._ready_event.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        self._hwnd = None
        try:
            _user32.UnregisterClassW(self._class_name, self._hinst)
        except Exception:
            pass

    def show_toast(
        self,
        title: str,
        subtitle: str = "",
        kind: str = "info",
        duration_ms: int = 2500,
        anchor_rect: Optional[Tuple[int, int, int, int]] = None,
    ) -> bool:
        if self._closed:
            return False
        hwnd = self._ensure_window_thread()
        if not hwnd:
            return False
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._pending = (seq, str(title or ""), str(subtitle or ""), str(kind or "info"), int(duration_ms), anchor_rect)
        return bool(_user32.PostMessageW(hwnd, self.WM_APP_SHOW, seq, 0))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            hwnd = self._hwnd
            t = self._thread
        if hwnd:
            try:
                _user32.PostMessageW(hwnd, self.WM_APP_CLOSE, 0, 0)
            except Exception:
                pass
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None


class WindowSwitcher:
    """Single-Monitor quick toggle between Genshin Impact and HoYoLAB Interactive Map."""

    @staticmethod
    def toggle_game_and_browser(titles: Tuple[str, ...]) -> str:
        try:
            fg = int(win32gui.GetForegroundWindow() or 0)
        except Exception:
            fg = 0

        game_hwnd = WindowFinder.find_game_window_any_state(titles)
        browser_hwnd = WindowFinder.find_browser_window_any_state()

        if game_hwnd and fg == game_hwnd:
            if browser_hwnd:
                WindowFinder.focus_window(browser_hwnd)
                return "browser"
            return "no_browser"

        if game_hwnd:
            WindowFinder.focus_window(game_hwnd)
            return "game"

        if browser_hwnd:
            WindowFinder.focus_window(browser_hwnd)
            return "browser"

        return "none"
