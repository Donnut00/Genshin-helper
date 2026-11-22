# Clean-room Win32 layered Navigation Mini-HUD + Pre-Cached 2-Layer Renderer + Proximity Spot Card
# for Genshin-Helper v5.
# Periodic SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) refresh adapts the Win32 reliability
# insight for display/resolution changes using src.game_settings.refresh_exclude_from_capture.
"""Pre-cached 2-layer Win32 Navigation Mini-HUD, Circular EMA Smoother, and Proximity Spot Card."""
from __future__ import annotations

from collections import OrderedDict
import ctypes
from ctypes import wintypes
from functools import lru_cache
import io
import logging
import math
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.game_settings import compute_ui_layout, refresh_exclude_from_capture
from src.i18n import get_language, t
from src.nav_controller import NavGuidance

logger = logging.getLogger("GenshinHelper.NavHud")


# ---------------------------------------------------------------------------
# Explicit 64-bit Win32 ctypes structures & prototypes
# ---------------------------------------------------------------------------
class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


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
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


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
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
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
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.MonitorFromPoint.argtypes = [_POINT, wintypes.DWORD]
_user32.MonitorFromPoint.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
_user32.LoadCursorW.restype = wintypes.HANDLE
_user32.SetCursor.argtypes = [wintypes.HANDLE]
_user32.SetCursor.restype = wintypes.HANDLE

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

_MONITOR_DEFAULTTONEAREST = 2
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1


def _query_monitor_work_area(point_x: int, point_y: int) -> Tuple[int, int, int, int]:
    """Queries Win32 monitor work area `(left, top, right, bottom)` containing `(point_x, point_y)`."""
    try:
        hmon = _user32.MonitorFromPoint(_POINT(int(point_x), int(point_y)), _MONITOR_DEFAULTTONEAREST)
        if hmon:
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                rc = mi.rcWork
                if rc.right > rc.left and rc.bottom > rc.top:
                    return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))
    except Exception as exc:
        logger.debug("GetMonitorInfoW fallback: %s", exc)
    w = int(_user32.GetSystemMetrics(_SM_CXSCREEN)) or 1920
    h = int(_user32.GetSystemMetrics(_SM_CYSCREEN)) or 1080
    return (0, 0, w, h)


def rgba_to_premultiplied_bgra(img: Image.Image) -> bytes:
    """Converts RGBA Pillow image into top-down premultiplied BGRA bytes for UpdateLayeredWindow."""
    rgba_img = img if img.mode == "RGBA" else img.convert("RGBA")
    rgba = np.asarray(rgba_img, dtype=np.uint16)
    alpha = rgba[:, :, 3:4]
    rgb_pm = (rgba[:, :, :3] * alpha + 127) // 255
    bgra = np.empty_like(rgba, dtype=np.uint8)
    bgra[:, :, 0] = rgb_pm[:, :, 2]
    bgra[:, :, 1] = rgb_pm[:, :, 1]
    bgra[:, :, 2] = rgb_pm[:, :, 0]
    bgra[:, :, 3] = rgba[:, :, 3]
    return bgra.tobytes()


# ---------------------------------------------------------------------------
# Circular EMA Smoother (30 Hz, wrap-safe across 0°/360° and ±180°)
# ---------------------------------------------------------------------------
class CircularAngleSmoother:
    """
    Wrap-safe circular Exponential Moving Average smoother on `(sin θ, cos θ)`.
    Eliminates 0°/360° and -180°/+180° discontinuity snaps and suppresses micro-jitter.
    """

    def __init__(self, alpha: float = 0.35, deadband_deg: float = 1.5) -> None:
        self.alpha = max(0.01, min(1.0, float(alpha)))
        self.deadband_deg = max(0.0, float(deadband_deg))
        self._sin: float = 0.0
        self._cos: float = 1.0
        self._value_deg: Optional[float] = None
        self._target_deg: Optional[float] = None

    @property
    def value(self) -> Optional[float]:
        return self._value_deg

    @property
    def target(self) -> Optional[float]:
        return self._target_deg

    def reset(self) -> None:
        self._sin = 0.0
        self._cos = 1.0
        self._value_deg = None
        self._target_deg = None

    def set_target(self, angle_deg: Optional[float]) -> None:
        if angle_deg is None or not math.isfinite(float(angle_deg)):
            self._target_deg = None
            return
        norm = ((float(angle_deg) + 180.0) % 360.0) - 180.0
        self._target_deg = norm

    def update(self, angle_deg: Optional[float]) -> Optional[float]:
        """Updates target angle and performs one EMA smoothing step."""
        self.set_target(angle_deg)
        return self.step()

    def step(self) -> Optional[float]:
        """Advances the circular EMA one tick toward the latest target angle."""
        if self._target_deg is None:
            return self._value_deg

        target = self._target_deg
        rad = math.radians(target)
        t_sin = math.sin(rad)
        t_cos = math.cos(rad)

        if self._value_deg is None:
            self._sin = t_sin
            self._cos = t_cos
            self._value_deg = target
            return self._value_deg

        diff = ((target - self._value_deg + 180.0) % 360.0) - 180.0
        if abs(diff) < self.deadband_deg:
            return self._value_deg

        a = self.alpha
        ns = (1.0 - a) * self._sin + a * t_sin
        nc = (1.0 - a) * self._cos + a * t_cos
        mag = math.hypot(ns, nc)
        if mag < 1e-6:
            # Exact 180° opposition: step deterministically along positive CW side
            step_rad = math.radians(self._value_deg + diff * a)
            ns = math.sin(step_rad)
            nc = math.cos(step_rad)
        else:
            ns /= mag
            nc /= mag

        self._sin = ns
        self._cos = nc
        self._value_deg = math.degrees(math.atan2(self._sin, self._cos))
        return self._value_deg


# ---------------------------------------------------------------------------
# Calm Compass Needle: critically-damped spring + distance-adaptive deadband
# ---------------------------------------------------------------------------
POSITION_NOISE_M: float = 1.4  # Typical minimap position jitter (meters) at 30 Hz


def noise_deadband_deg(distance_m: Optional[float], noise_m: float = POSITION_NOISE_M) -> float:
    """
    Angular uncertainty caused by position jitter at a given distance.
    Far targets (100m) -> ~1°, near targets (8m) -> ~10°: small position noise near a pin
    swings the bearing a lot, which is exactly what made the old needle 'lose its north'.
    """
    if distance_m is None or not math.isfinite(float(distance_m)):
        return 2.0
    d = max(1.0, float(distance_m))
    return max(1.2, min(14.0, math.degrees(math.atan2(float(noise_m), d))))


class SpringAngleSmoother:
    """
    Wrap-safe critically-damped spring for the HUD compass needle.

    - Retargets only when the new bearing leaves the current deadband (no micro-jitter).
    - Spring (zeta = 1) glides to the target without overshoot, with a hard angular speed cap,
      so a sudden 170° change sweeps smoothly instead of snapping.
    - `set_distance()` adapts stiffness & deadband: calm & heavy near a pin, snappy when far.
    """

    MAX_SPEED_DEG_S: float = 420.0

    def __init__(self, omega_far: float = 14.0, omega_near: float = 4.5) -> None:
        self.omega_far = float(omega_far)
        self.omega_near = float(omega_near)
        self._omega = self.omega_far
        self._deadband = 2.0
        self._value: Optional[float] = None
        self._velocity: float = 0.0
        self._goal: Optional[float] = None
        self._last_ts: Optional[float] = None

    @property
    def value(self) -> Optional[float]:
        return None if self._value is None else ((self._value + 180.0) % 360.0) - 180.0

    @property
    def goal(self) -> Optional[float]:
        return self._goal

    @property
    def deadband(self) -> float:
        return self._deadband

    def reset(self) -> None:
        self._value = None
        self._velocity = 0.0
        self._goal = None
        self._last_ts = None

    def set_distance(self, distance_m: Optional[float]) -> None:
        self._deadband = noise_deadband_deg(distance_m)
        if distance_m is None:
            self._omega = self.omega_far
            return
        # Blend stiffness between 12m (near) and 60m (far)
        k = max(0.0, min(1.0, (float(distance_m) - 12.0) / 48.0))
        self._omega = self.omega_near + (self.omega_far - self.omega_near) * k

    def set_target(self, angle_deg: Optional[float]) -> None:
        if angle_deg is None or not math.isfinite(float(angle_deg)):
            self._goal = None
            return
        target = ((float(angle_deg) + 180.0) % 360.0) - 180.0
        if self._goal is None:
            self._goal = target
            return
        diff = ((target - self._goal + 180.0) % 360.0) - 180.0
        if abs(diff) >= self._deadband:
            self._goal = target

    def step(self, now: Optional[float] = None, dt: Optional[float] = None) -> Optional[float]:
        if self._goal is None:
            return self.value
        if dt is None:
            ts = time.monotonic() if now is None else float(now)
            dt = (ts - self._last_ts) if self._last_ts is not None else 1.0 / 30.0
            self._last_ts = ts
        dt = max(1e-3, min(0.1, float(dt)))

        if self._value is None:
            self._value = self._goal
            self._velocity = 0.0
            return self.value

        err = ((self._goal - self._value + 180.0) % 360.0) - 180.0
        if abs(err) < 0.05 and abs(self._velocity) < 0.5:
            return self.value
        w = self._omega
        # Semi-implicit Euler on x'' = w^2 * err - 2 w x' (critically damped)
        accel = w * w * err - 2.0 * w * self._velocity
        self._velocity += accel * dt
        self._velocity = max(-self.MAX_SPEED_DEG_S, min(self.MAX_SPEED_DEG_S, self._velocity))
        self._value += self._velocity * dt
        return self.value


class ScalarSmoother:
    """Frame-rate independent exponential smoother for distances (meters)."""

    def __init__(self, tau_s: float = 0.35) -> None:
        self.tau_s = max(1e-3, float(tau_s))
        self._value: Optional[float] = None
        self._last_ts: Optional[float] = None

    @property
    def value(self) -> Optional[float]:
        return self._value

    def reset(self) -> None:
        self._value = None
        self._last_ts = None

    def update(self, target: Optional[float], now: Optional[float] = None) -> Optional[float]:
        if target is None or not math.isfinite(float(target)):
            return self._value
        ts = time.monotonic() if now is None else float(now)
        if self._value is None or self._last_ts is None or abs(float(target) - self._value) > 60.0:
            self._value = float(target)
        else:
            dt = max(0.0, min(0.5, ts - self._last_ts))
            a = 1.0 - math.exp(-dt / self.tau_s)
            self._value += (float(target) - self._value) * a
        self._last_ts = ts
        return self._value


# ---------------------------------------------------------------------------
# Font Loader (cached)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _load_font(bold: bool, size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf")
        if bold
        else (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf")
    )
    clamped = max(10, int(round(size_px)))
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, clamped)
            except Exception:
                pass
    return ImageFont.load_default()


def _rotate_point(x: float, y: float, angle_deg_cw: float) -> Tuple[float, float]:
    """Rotates screen-space offset `(x, y)` clockwise by `angle_deg_cw` (+y downward)."""
    rad = math.radians(angle_deg_cw)
    c = math.cos(rad)
    s = math.sin(rad)
    return (x * c - y * s, x * s + y * c)


# ---------------------------------------------------------------------------
# High-Performance 2-Layer Pre-Baked Renderer + Proximity Spot Card
# ---------------------------------------------------------------------------
class NavHudRenderer:
    """
    2-Layer Pre-Baked Navigation Mini-HUD Renderer:
      - Layer 1 (Static Glass Card + Text + Optional Spot Card): LRU cached by
        `(title, dist_int, floor_tag, state, expanded, lang, scale, ...)`.
      - Layer 2 (Pre-Baked 36x36 Compass Badge, 90 steps @ 4°): lazily cached in a
        byte-bounded LRU dict (< 0.5 MB total).
    Per-frame compass rotation ONLY copies the cached card and alpha-composites the 36x36 badge
    (< 0.4 ms/frame steady state) with strict alpha integrity (zero sky hole-punching).
    """

    BADGE_STEP_DEG: int = 4
    BADGE_COUNT: int = 90
    MAX_BADGE_CACHE_BYTES: int = 480 * 1024  # Strictly < 0.5 MB
    MAX_CARD_CACHE_ENTRIES: int = 32
    MAX_THUMB_CACHE_ENTRIES: int = 30
    MAX_GUIDE_CACHE_ENTRIES: int = 12
    THUMB_SIZE: Tuple[int, int] = (72, 54)
    GUIDE_IMAGE_MAX: Tuple[int, int] = (300, 188)
    LOCK_ON_DEG: float = 12.0
    BEHIND_DEG: float = 135.0
    COMPACT_DIST_THRESHOLD_M: float = 14.0
    # Within this range the rotating needle is replaced by a calm radar dot: near a pin the
    # bearing is dominated by position noise, while a dot offset (∝ distance) barely moves.
    RADAR_RANGE_M: float = 25.0

    # Midnight Slate glass palette aligned with HudToastOverlay._PALETTES
    _GLASS_BG: Tuple[int, int, int, int] = (15, 23, 42, 236)
    _DIAL_BG: Tuple[int, int, int, int] = (15, 23, 42, 244)
    _AMBER_RING: Tuple[int, int, int, int] = (245, 158, 11, 240)
    _SPOT_BORDER: Tuple[int, int, int, int] = (148, 163, 184, 128)

    _PALETTES: Dict[str, Dict[str, Tuple[int, int, int, int]]] = {
        "info": {
            "accent": (56, 189, 248, 255),
            "border": (56, 189, 248, 210),
            "badge_tint": (56, 189, 248, 44),
            "tag_bg": (56, 189, 248, 52),
            "tag_text": (186, 230, 253, 255),
            "title": (248, 250, 252, 255),
            "sub": (148, 163, 184, 255),
        },
        "success": {
            "accent": (34, 197, 94, 255),
            "border": (34, 197, 94, 220),
            "badge_tint": (22, 163, 74, 48),
            "tag_bg": (34, 197, 94, 56),
            "tag_text": (187, 247, 208, 255),
            "title": (248, 250, 252, 255),
            "sub": (187, 247, 208, 255),
        },
        "warning": {
            "accent": (245, 158, 11, 255),
            "border": (245, 158, 11, 225),
            "badge_tint": (245, 158, 11, 52),
            "tag_bg": (245, 158, 11, 60),
            "tag_text": (254, 243, 199, 255),
            "title": (254, 243, 199, 255),
            "sub": (253, 230, 138, 255),
        },
        "lock_on": {
            "accent": (45, 212, 191, 255),
            "border": (45, 212, 191, 235),
            "badge_tint": (45, 212, 191, 58),
            "tag_bg": (45, 212, 191, 60),
            "tag_text": (204, 251, 241, 255),
            "title": (248, 250, 252, 255),
            "sub": (153, 246, 228, 255),
        },
        "edit": {
            "accent": (167, 139, 250, 255),
            "border": (167, 139, 250, 240),
            "badge_tint": (139, 92, 246, 52),
            "tag_bg": (139, 92, 246, 60),
            "tag_text": (237, 233, 254, 255),
            "title": (248, 250, 252, 255),
            "sub": (221, 214, 254, 255),
        },
    }

    def __init__(self) -> None:
        # LRU caches
        self._badge_cache: OrderedDict[Tuple[Any, ...], Tuple[Image.Image, int]] = OrderedDict()
        self._badge_cache_bytes: int = 0
        self._radar_cache: Dict[Tuple[Any, ...], Image.Image] = {}
        self._card_cache: OrderedDict[Tuple[Any, ...], Tuple[Image.Image, Tuple[int, int], int, bool]] = OrderedDict()
        self._guide_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self._thumb_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self._thumb_lock = threading.Lock()
        self._probe_draw = ImageDraw.Draw(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))
        self.last_render_meta: Dict[str, Any] = {}

    @property
    def badge_cache_bytes(self) -> int:
        return self._badge_cache_bytes

    @property
    def badge_cache_count(self) -> int:
        return len(self._badge_cache)

    @property
    def card_cache_count(self) -> int:
        return len(self._card_cache)

    @property
    def thumb_cache_count(self) -> int:
        with self._thumb_lock:
            return len(self._thumb_cache)

    def clear_caches(self) -> None:
        self._badge_cache.clear()
        self._badge_cache_bytes = 0
        self._radar_cache.clear()
        self._card_cache.clear()
        with self._thumb_lock:
            self._thumb_cache.clear()
            self._guide_cache.clear()

    # ------------------------------------------------------------------
    # Large guide image cache (in-game Guide Card, max 12 entries)
    # ------------------------------------------------------------------
    def put_guide_image(self, key: str, image_or_bytes: Image.Image | bytes) -> bool:
        """Stores both the small spot thumbnail and the large Guide Card image for `key`."""
        clean_key = str(key or "").strip()
        if not clean_key:
            return False
        try:
            if isinstance(image_or_bytes, (bytes, bytearray, memoryview)):
                with Image.open(io.BytesIO(bytes(image_or_bytes))) as opened:
                    src = opened.convert("RGBA")
            elif isinstance(image_or_bytes, Image.Image):
                src = image_or_bytes.convert("RGBA")
            else:
                return False
        except Exception as exc:
            logger.debug("Guide image decode failed: %s", exc)
            return False
        large = ImageOps.contain(src, self.GUIDE_IMAGE_MAX, method=Image.Resampling.LANCZOS)
        with self._thumb_lock:
            self._guide_cache[clean_key] = large
            self._guide_cache.move_to_end(clean_key)
            while len(self._guide_cache) > self.MAX_GUIDE_CACHE_ENTRIES:
                self._guide_cache.popitem(last=False)
        self.put_thumbnail(clean_key, src)
        return True

    def get_guide_image(self, key: str) -> Optional[Image.Image]:
        clean_key = str(key or "").strip()
        if not clean_key:
            return None
        with self._thumb_lock:
            img = self._guide_cache.get(clean_key)
            if img is not None:
                self._guide_cache.move_to_end(clean_key)
            return img

    # ------------------------------------------------------------------
    # Thumbnail LRU cache (72x54 RGBA, max 30 entries, zero network I/O)
    # ------------------------------------------------------------------
    @classmethod
    def _normalize_thumbnail(cls, image_or_bytes: Image.Image | bytes) -> Optional[Image.Image]:
        try:
            if isinstance(image_or_bytes, (bytes, bytearray, memoryview)):
                with Image.open(io.BytesIO(bytes(image_or_bytes))) as opened:
                    src = opened.convert("RGBA")
            elif isinstance(image_or_bytes, Image.Image):
                src = image_or_bytes.convert("RGBA")
            else:
                return None

            tw, th = cls.THUMB_SIZE
            fitted = ImageOps.fit(src, (tw, th), method=Image.Resampling.LANCZOS)

            # Apply rounded mask on alpha while keeping interior opaque
            mask_hi = Image.new("L", (tw * 2, th * 2), 0)
            mdraw = ImageDraw.Draw(mask_hi)
            mdraw.rounded_rectangle((0, 0, tw * 2 - 1, th * 2 - 1), radius=12, fill=255)
            mask = mask_hi.resize((tw, th), Image.Resampling.LANCZOS)

            out = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
            out.paste(fitted, (0, 0), mask=mask)
            return out
        except Exception as exc:
            logger.debug("Thumbnail normalize failed: %s", exc)
            return None

    def put_thumbnail(self, key: str, image_or_bytes: Image.Image | bytes) -> Optional[Image.Image]:
        clean_key = str(key or "").strip()
        if not clean_key:
            return None
        thumb = self._normalize_thumbnail(image_or_bytes)
        if thumb is None:
            return None
        with self._thumb_lock:
            self._thumb_cache[clean_key] = thumb
            self._thumb_cache.move_to_end(clean_key)
            while len(self._thumb_cache) > self.MAX_THUMB_CACHE_ENTRIES:
                self._thumb_cache.popitem(last=False)
        return thumb

    def get_thumbnail(self, key: str) -> Optional[Image.Image]:
        clean_key = str(key or "").strip()
        if not clean_key:
            return None
        with self._thumb_lock:
            thumb = self._thumb_cache.get(clean_key)
            if thumb is not None:
                self._thumb_cache.move_to_end(clean_key)
            return thumb

    # ------------------------------------------------------------------
    # Layer 2: Pre-Baked 36x36 Compass Badge (90 angles @ 4° steps)
    # ------------------------------------------------------------------
    @classmethod
    def quantize_angle_bucket(cls, angle_deg: Optional[float]) -> int:
        if angle_deg is None or not math.isfinite(float(angle_deg)):
            return -1
        norm = float(angle_deg) % 360.0
        return int(round(norm / float(cls.BADGE_STEP_DEG))) % cls.BADGE_COUNT

    @classmethod
    def _build_compass_badge(
        cls,
        palette_name: str,
        angle_bucket: int,
        *,
        badge_px: int,
        directional: bool,
        lock_on: bool,
        behind: bool,
    ) -> Image.Image:
        """
        Renders a single anti-aliased compass badge at 2x supersampling using strictly
        Porter-Duff alpha_composite so the dial interior stays >= 240 alpha with zero holes.
        """
        pal_key = "lock_on" if lock_on else (palette_name if palette_name in cls._PALETTES else "info")
        pal = cls._PALETTES[pal_key]
        ss = 2
        sz = max(24, int(badge_px))
        hi = sz * ss
        cx = hi * 0.5
        cy = hi * 0.5
        pad = max(2, int(round(2.0 * ss)))
        r = (hi - pad * 2) * 0.5

        # 1. Opaque-ish dark slate dial base (alpha 244 -> zero sky punch-through)
        badge_hi = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(badge_hi)
        bdraw.ellipse((pad, pad, hi - pad, hi - pad), fill=cls._DIAL_BG)

        # 2. Translucent accent dial glow via alpha_composite (NEVER raw draw on dial!)
        tint_hi = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(tint_hi)
        tint_color = (245, 158, 11, 46) if (behind and not lock_on) else pal["badge_tint"]
        inner_pad = pad + max(2, int(round(1.5 * ss)))
        tdraw.ellipse((inner_pad, inner_pad, hi - inner_pad, hi - inner_pad), fill=tint_color)
        badge_hi = Image.alpha_composite(badge_hi, tint_hi)

        # 3. Outer compass ring + cardinal micro-ticks + lock-on top highlight
        ring_hi = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        rdraw = ImageDraw.Draw(ring_hi)
        ring_color = cls._AMBER_RING if (behind and not lock_on) else pal["accent"]
        ring_w = max(2, int(round((2.4 if lock_on else 1.8) * ss)))
        rdraw.ellipse((pad, pad, hi - pad, hi - pad), outline=ring_color, width=ring_w)

        # Subtle cardinal reference ticks at E / S / W (and N)
        tick_len = max(3, int(round(3.0 * ss)))
        tick_w = max(2, int(round(1.2 * ss)))
        tick_col = (148, 163, 184, 150)
        for t_deg in (90.0, 180.0, 270.0):
            ox, oy = _rotate_point(0.0, -(r - 1.5), t_deg)
            ix, iy = _rotate_point(0.0, -(r - 1.5 - tick_len), t_deg)
            rdraw.line([(cx + ox, cy + oy), (cx + ix, cy + iy)], fill=tick_col, width=tick_w)

        if lock_on and directional:
            # Bright lock-on top-tick highlight at 12 o'clock
            top_y = pad + 1.0
            notch_w = max(4.0, r * 0.24)
            notch_h = max(4.0, r * 0.22)
            rdraw.polygon(
                [
                    (cx, top_y + notch_h),
                    (cx - notch_w, top_y - 0.5),
                    (cx + notch_w, top_y - 0.5),
                ],
                fill=(255, 255, 255, 255),
            )
            # Subtle inner glow arc / bright ring reinforce
            rdraw.ellipse(
                (pad + ring_w, pad + ring_w, hi - pad - ring_w, hi - pad - ring_w),
                outline=(204, 251, 241, 120),
                width=max(1, int(round(1.0 * ss))),
            )
        else:
            # Always draw a subtle top 12-o'clock forward reticle notch so player immediately reads
            # "align arrow to top notch = walk straight ahead (W)"
            top_y = pad + 1.0
            notch_w = max(3.5, r * 0.20)
            notch_h = max(3.5, r * 0.18)
            rdraw.polygon(
                [
                    (cx, top_y + notch_h),
                    (cx - notch_w, top_y - 0.5),
                    (cx + notch_w, top_y - 0.5),
                ],
                fill=(226, 232, 240, 200),
            )

        badge_hi = Image.alpha_composite(badge_hi, ring_hi)

        # 4. Directional navigation chevron OR non-directional status glyph
        glyph_hi = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glyph_hi)
        if directional and angle_bucket >= 0:
            deg = float(angle_bucket * cls.BADGE_STEP_DEG)
            # Sleek chevron pointing North (0, -tip) before CW rotation
            arrow_pts_local = [
                (0.0, -r * 0.62),         # Sharp tip
                (r * 0.42, r * 0.44),     # Right wing
                (0.0, r * 0.18),          # Tail notch
                (-r * 0.42, r * 0.44),    # Left wing
            ]
            rot_pts = [
                (cx + rx, cy + ry)
                for (rx, ry) in (_rotate_point(px, py, deg) for px, py in arrow_pts_local)
            ]
            arrow_fill = (255, 255, 255, 255) if lock_on else pal["accent"]
            gdraw.polygon(rot_pts, fill=arrow_fill)

            # Crisp center core highlight for immediate contrast
            core_pts_local = [
                (0.0, -r * 0.34),
                (r * 0.16, r * 0.14),
                (0.0, r * 0.04),
                (-r * 0.16, r * 0.14),
            ]
            rot_core = [
                (cx + rx, cy + ry)
                for (rx, ry) in (_rotate_point(px, py, deg) for px, py in core_pts_local)
            ]
            core_fill = pal["accent"] if lock_on else (248, 250, 252, 230)
            gdraw.polygon(rot_core, fill=core_fill)
        elif palette_name == "warning":
            # Lost / warning exclamation mark
            stroke = max(3, int(round(2.4 * ss)))
            gdraw.line([(cx, cy - r * 0.36), (cx, cy + r * 0.10)], fill=pal["accent"], width=stroke)
            dot_r = max(2.5, r * 0.11)
            gdraw.ellipse(
                (cx - dot_r, cy + r * 0.30 - dot_r, cx + dot_r, cy + r * 0.30 + dot_r),
                fill=pal["accent"],
            )
        elif palette_name == "success":
            # Arrived checkmark glyph
            stroke = max(3, int(round(2.4 * ss)))
            pts = [
                (cx - r * 0.34, cy + r * 0.02),
                (cx - r * 0.06, cy + r * 0.30),
                (cx + r * 0.38, cy - r * 0.28),
            ]
            gdraw.line(pts, fill=pal["accent"], width=stroke, joint="curve")
        else:
            # Idle center beacon dot
            dot_r = max(3.5, r * 0.22)
            gdraw.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=pal["accent"])

        badge_hi = Image.alpha_composite(badge_hi, glyph_hi)
        return badge_hi.resize((sz, sz), Image.Resampling.LANCZOS)

    def get_compass_badge(
        self,
        palette_name: str,
        angle_deg: Optional[float],
        *,
        scale: float = 1.0,
        directional: bool = True,
        lock_on: bool = False,
        behind: bool = False,
    ) -> Tuple[Image.Image, bool, int]:
        """Returns `(badge_rgba, cache_hit, angle_bucket)` from byte-bounded LRU cache (< 0.5 MB)."""
        s = max(0.85, min(1.8, float(scale)))
        badge_px = int(round(36 * s))
        bucket = self.quantize_angle_bucket(angle_deg) if directional else -1
        eff_pal = "lock_on" if lock_on else (palette_name if palette_name in self._PALETTES else "info")
        key = (eff_pal, bucket, bool(directional), bool(lock_on), bool(behind), badge_px)

        cached = self._badge_cache.get(key)
        if cached is not None:
            self._badge_cache.move_to_end(key)
            return cached[0], True, bucket

        badge = self._build_compass_badge(
            eff_pal,
            bucket,
            badge_px=badge_px,
            directional=bool(directional),
            lock_on=bool(lock_on),
            behind=bool(behind),
        )
        nbytes = badge.width * badge.height * 4
        self._badge_cache[key] = (badge, nbytes)
        self._badge_cache_bytes += nbytes
        self._badge_cache.move_to_end(key)

        while self._badge_cache_bytes > self.MAX_BADGE_CACHE_BYTES and len(self._badge_cache) > 1:
            _, (_, evicted_bytes) = self._badge_cache.popitem(last=False)
            self._badge_cache_bytes -= evicted_bytes

        return badge, False, bucket

    # ------------------------------------------------------------------
    # Close-range Radar Badge (static base + moving target dot)
    # ------------------------------------------------------------------
    @classmethod
    def _build_radar_base(cls, palette_name: str, badge_px: int) -> Image.Image:
        pal = cls._PALETTES.get(palette_name, cls._PALETTES["info"])
        ss = 2
        sz = max(24, int(badge_px))
        hi = sz * ss
        cx = cy = hi * 0.5
        pad = max(2, int(round(2.0 * ss)))
        r = (hi - pad * 2) * 0.5

        base = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        ImageDraw.Draw(base).ellipse((pad, pad, hi - pad, hi - pad), fill=cls._DIAL_BG)

        tint = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        inner_pad = pad + max(2, int(round(1.5 * ss)))
        ImageDraw.Draw(tint).ellipse((inner_pad, inner_pad, hi - inner_pad, hi - inner_pad), fill=pal["badge_tint"])
        base = Image.alpha_composite(base, tint)

        rings = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        rdraw = ImageDraw.Draw(rings)
        rdraw.ellipse((pad, pad, hi - pad, hi - pad), outline=pal["accent"], width=max(2, int(round(1.8 * ss))))
        # Range rings at 1/3 and 2/3 (≈ 8m / 16m) for an at-a-glance distance read
        for frac in (0.36, 0.70):
            rr = r * frac
            rdraw.ellipse(
                (cx - rr, cy - rr, cx + rr, cy + rr),
                outline=(148, 163, 184, 110),
                width=max(1, int(round(1.0 * ss))),
            )
        # Forward notch at 12 o'clock (= camera forward / press W)
        notch_w = max(3.5, r * 0.20)
        notch_h = max(3.5, r * 0.18)
        top_y = pad + 1.0
        rdraw.polygon(
            [(cx, top_y + notch_h), (cx - notch_w, top_y - 0.5), (cx + notch_w, top_y - 0.5)],
            fill=(226, 232, 240, 200),
        )
        # Player marker: small upward chevron in the center
        pr = r * 0.20
        rdraw.polygon(
            [(cx, cy - pr), (cx + pr * 0.78, cy + pr * 0.72), (cx, cy + pr * 0.30), (cx - pr * 0.78, cy + pr * 0.72)],
            fill=(226, 232, 240, 235),
        )
        base = Image.alpha_composite(base, rings)
        return base.resize((sz, sz), Image.Resampling.LANCZOS)

    @classmethod
    def _build_radar_dot(cls, palette_name: str, badge_px: int) -> Image.Image:
        pal = cls._PALETTES.get(palette_name, cls._PALETTES["info"])
        dot_px = max(7, int(round(badge_px * 0.27)))
        ss = 4
        hi = dot_px * ss
        img = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((0, 0, hi - 1, hi - 1), fill=(255, 255, 255, 255))
        inset = int(round(hi * 0.2))
        d.ellipse((inset, inset, hi - 1 - inset, hi - 1 - inset), fill=pal["accent"])
        return img.resize((dot_px, dot_px), Image.Resampling.LANCZOS)

    def get_radar_badge(
        self,
        palette_name: str,
        delta_deg: float,
        distance_m: float,
        *,
        scale: float = 1.0,
    ) -> Tuple[Image.Image, Tuple[int, int]]:
        """Returns `(badge_rgba, dot_xy)` — static cached base with the target dot composited."""
        s = max(0.85, min(1.8, float(scale)))
        badge_px = int(round(36 * s))
        pal_key = palette_name if palette_name in self._PALETTES else "info"
        base_key = ("base", pal_key, badge_px)
        dot_key = ("dot", pal_key, badge_px)
        base = self._radar_cache.get(base_key)
        if base is None:
            base = self._build_radar_base(pal_key, badge_px)
            self._radar_cache[base_key] = base
        dot = self._radar_cache.get(dot_key)
        if dot is None:
            dot = self._build_radar_dot(pal_key, badge_px)
            self._radar_cache[dot_key] = dot

        r = badge_px * 0.5 - max(2.0, badge_px * 0.12)
        frac = max(0.0, min(1.0, float(distance_m) / self.RADAR_RANGE_M))
        rr = r * (0.10 + 0.90 * frac)
        rad = math.radians(float(delta_deg))
        cx = badge_px * 0.5 + math.sin(rad) * rr
        cy = badge_px * 0.5 - math.cos(rad) * rr
        dx = int(round(cx - dot.width / 2.0))
        dy = int(round(cy - dot.height / 2.0))
        dx = max(0, min(badge_px - dot.width, dx))
        dy = max(0, min(badge_px - dot.height, dy))
        frame = base.copy()
        frame.alpha_composite(dot, dest=(dx, dy))
        return frame, (dx, dy)

    # ------------------------------------------------------------------
    # Text measurement & wrapping helpers
    # ------------------------------------------------------------------
    def _truncate_line(
        self,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_w: int,
    ) -> str:
        clean = " ".join((text or "").split())
        if not clean:
            return ""
        bbox = self._probe_draw.textbbox((0, 0), clean, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            return clean
        ellipsis = "..."
        lo, hi = 1, len(clean)
        best = ellipsis
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = clean[:mid].rstrip() + ellipsis
            cb = self._probe_draw.textbbox((0, 0), cand, font=font)
            if (cb[2] - cb[0]) <= max_w:
                best = cand
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _wrap_two_lines(
        self,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_w: int,
    ) -> List[str]:
        clean = " ".join((text or "").split())
        if not clean:
            return []
        bbox = self._probe_draw.textbbox((0, 0), clean, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            return [clean]

        words = clean.split(" ")
        line1 = ""
        idx = 0
        while idx < len(words):
            cand = f"{line1} {words[idx]}".strip()
            cb = self._probe_draw.textbbox((0, 0), cand, font=font)
            if (cb[2] - cb[0]) <= max_w:
                line1 = cand
                idx += 1
            else:
                break

        if not line1:
            # Single token wider than max_w -> split by characters
            cut = 1
            while cut < len(clean):
                cb = self._probe_draw.textbbox((0, 0), clean[: cut + 1], font=font)
                if (cb[2] - cb[0]) > max_w:
                    break
                cut += 1
            line1 = clean[:cut]
            rest = clean[cut:].strip()
        else:
            rest = " ".join(words[idx:]).strip()

        if not rest:
            return [line1]
        line2 = self._truncate_line(rest, font, max_w)
        return [line1, line2]

    def _wrap_lines(
        self,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_w: int,
        max_lines: int,
    ) -> List[str]:
        """Greedy word wrap across paragraphs, ellipsizing the last allowed line."""
        out: List[str] = []
        paragraphs = [p for p in str(text or "").replace("\r", "").split("\n") if p.strip()]
        for p_idx, para in enumerate(paragraphs):
            remaining = para
            while remaining and len(out) < max_lines:
                if len(out) == max_lines - 1:
                    rest_all = " ".join([remaining] + paragraphs[p_idx + 1 :])
                    out.append(self._truncate_line(rest_all, font, max_w))
                    return out
                lines = self._wrap_two_lines(remaining, font, max_w)
                if not lines:
                    break
                out.append(lines[0])
                consumed = " ".join(lines[0].split())
                clean_rem = " ".join(remaining.split())
                remaining = clean_rem[len(consumed):].strip() if clean_rem.startswith(consumed) else ""
            if len(out) >= max_lines:
                break
        return out

    # ------------------------------------------------------------------
    # Localized formatting helpers (bilingual vi / en via src.i18n.t)
    # ------------------------------------------------------------------
    @staticmethod
    def _format_floor_tag(guidance: NavGuidance, lang: str) -> str:
        if guidance.target is None:
            return ""
        if guidance.target_is_underground:
            raw_floor = guidance.target_floor_name or guidance.target_floor_id or "B1"
            short = raw_floor.strip()
            return short[:10] if len(short) <= 10 else short[:9] + "…"
        # Surface pins carry no tag: a lone "G" said nothing about the pin and stole title space.
        return ""

    @staticmethod
    def _format_floor_human(floor_id: Optional[str], floor_name: str, is_ug: bool, lang: str) -> str:
        if is_ug:
            label = (floor_name or floor_id or "B1").strip()
            return t("hud.floor.underground", lang=lang, floor=label)
        return t("hud.floor.surface", lang=lang)

    @classmethod
    def _resolve_texts_and_mode(
        cls,
        guidance: NavGuidance,
        *,
        lang: str,
        hotkey_label: str,
        guide_hotkey_label: str = "",
        distance_m: Optional[float] = None,
    ) -> Tuple[str, str, str, str, bool, Optional[int], str]:
        """
        Returns `(title_text, subtitle_text, floor_tag, palette_name, expanded, dist_int, description)`.
        """
        state = guidance.state
        eff_dist = distance_m if distance_m is not None else guidance.distance_m
        dist_int = int(round(eff_dist)) if eff_dist is not None else None
        floor_tag = cls._format_floor_tag(guidance, lang)
        target_name = guidance.target.display_name if guidance.target is not None else ""
        description = guidance.target.description if guidance.target is not None else ""
        mark_hint = t("hud.hint.mark_short", lang=lang, hotkey=hotkey_label) if hotkey_label else ""
        guide_hint = (
            t("hud.hint.guide_short", lang=lang, hotkey=guide_hotkey_label) if guide_hotkey_label else ""
        )
        action_hint = " • ".join(h for h in (mark_hint, guide_hint) if h)

        if state == "lost":
            title_text = t("hud.state.lost", lang=lang)
            subtitle_text = t("hud.hint.open_map", lang=lang)
            return title_text, subtitle_text, "", "warning", True, None, ""

        if state == "idle" or guidance.target is None:
            title_text = t("hud.target.none", lang=lang)
            sub_hint = (guidance.route_target_name or "").strip()
            return title_text, sub_hint, "", "info", bool(sub_hint), None, ""

        dist_str = t("hud.distance", lang=lang, meters=dist_int if dist_int is not None else 0)
        name_part = target_name or t("hud.title", lang=lang)
        if guidance.route_step is not None and guidance.route_total is not None:
            prefix = f"#{guidance.route_step}/{guidance.route_total} "
        else:
            prefix = ""
        title_text = f"{prefix}{name_part} • {dist_str}"

        if state == "floor_mismatch":
            p_floor = cls._format_floor_human(
                guidance.player_floor_id, "", guidance.player_is_underground, lang
            )
            t_floor = cls._format_floor_human(
                guidance.target_floor_id,
                guidance.target_floor_name,
                guidance.target_is_underground,
                lang,
            )
            sub = t(
                "hud.state.floor_mismatch",
                lang=lang,
                player_floor=p_floor,
                target_floor=t_floor,
            )
            if guidance.route_target_name:
                sub = f"{sub} • {guidance.route_target_name}"
            return title_text, sub, floor_tag, "warning", True, dist_int, description

        if state == "arrived":
            arrived_str = t("hud.state.arrived", lang=lang)
            sub = f"{arrived_str} • {action_hint}" if action_hint else arrived_str
            return title_text, sub, floor_tag, "success", True, dist_int, description

        # state == "tracking"
        expanded = eff_dist is not None and eff_dist <= cls.COMPACT_DIST_THRESHOLD_M
        sub = (action_hint or t("hud.hint.mark", lang=lang, hotkey=hotkey_label)) if expanded else ""
        return title_text, sub, floor_tag, "info", expanded, dist_int, description

    # ------------------------------------------------------------------
    # Layer 1: Static Glass Card + Text + Optional Spot Card (cached)
    # ------------------------------------------------------------------
    def _build_static_card(
        self,
        *,
        title_text: str,
        subtitle_text: str,
        floor_tag: str,
        palette_name: str,
        expanded: bool,
        scale: float,
        hold_progress: float,
        show_spot_card: bool,
        spot_thumb: Optional[Image.Image],
        spot_desc: str,
        spot_large: bool = False,
        spot_image_large: Optional[Image.Image] = None,
        spot_placeholder: str = "",
        spot_footer: str = "",
    ) -> Tuple[Image.Image, Tuple[int, int], int, bool]:
        s = max(0.85, min(1.8, float(scale)))
        pal = self._PALETTES.get(palette_name, self._PALETTES["info"])

        title_font = _load_font(bold=True, size_px=int(round(14 * s)))
        sub_font = _load_font(bold=False, size_px=int(round(11.5 * s)))
        tag_font = _load_font(bold=True, size_px=int(round(10.5 * s)))
        desc_font = _load_font(bold=False, size_px=int(round(11.5 * s)))

        pad_x = int(round(10 * s))
        pad_y = int(round(6 * s)) if not expanded else int(round(8 * s))
        badge_px = int(round(36 * s))
        gap_badge = int(round(10 * s))
        gap_tag = int(round(8 * s))
        max_text_w = int(round(270 * s))
        min_pill_w = int(round(212 * s))

        # Measure floor tag pill
        tag_w = tag_h = 0
        tag_pad_x = int(round(6 * s))
        tag_pad_y = int(round(2 * s))
        if floor_tag:
            tgb = self._probe_draw.textbbox((0, 0), floor_tag, font=tag_font)
            tag_text_w = tgb[2] - tgb[0]
            tag_text_h = tgb[3] - tgb[1]
            tag_w = tag_text_w + tag_pad_x * 2
            tag_h = max(int(round(18 * s)), tag_text_h + tag_pad_y * 2)

        avail_title_w = max(int(round(110 * s)), max_text_w - (tag_w + gap_tag if floor_tag else 0))
        clean_title = self._truncate_line(title_text, title_font, avail_title_w)
        clean_sub = self._truncate_line(subtitle_text, sub_font, max_text_w) if (expanded and subtitle_text) else ""

        tb = self._probe_draw.textbbox((0, 0), clean_title or "A", font=title_font)
        title_w, title_h = tb[2] - tb[0], tb[3] - tb[1]

        sub_w = sub_h = 0
        line_gap = int(round(4 * s)) if clean_sub else 0
        if clean_sub:
            sb = self._probe_draw.textbbox((0, 0), clean_sub, font=sub_font)
            sub_w, sub_h = sb[2] - sb[0], sb[3] - sb[1]

        top_row_w = title_w + (gap_tag + tag_w if floor_tag else 0)
        text_block_w = max(top_row_w, sub_w)
        text_block_h = max(title_h, tag_h) + line_gap + sub_h

        pill_w = max(min_pill_w, pad_x * 2 + badge_px + gap_badge + text_block_w + int(round(4 * s)))
        pill_h = max(badge_px + pad_y * 2, text_block_h + pad_y * 2)
        if expanded:
            # Guarantee visually distinct expanded height for 2-line layout
            pill_h = max(pill_h, int(round(62 * s)))

        # Optional Proximity Spot Card geometry
        has_spot = bool(show_spot_card and (spot_thumb is not None or bool(spot_desc)))
        spot_gap = int(round(6 * s))
        spot_h = 0
        spot_lines: List[str] = []
        thumb_w = int(round(self.THUMB_SIZE[0] * s))
        thumb_h = int(round(self.THUMB_SIZE[1] * s))
        spot_pad_x = int(round(10 * s))
        spot_pad_y = int(round(8 * s))
        thumb_gap = int(round(10 * s))

        total_w = pill_w
        # Large in-game Guide Card (toggled by hotkey): image on top, up to 5 text lines, footer hint
        guide_img: Optional[Image.Image] = None
        guide_lines: List[str] = []
        guide_footer = ""
        guide_line_h = int(round(16 * s))
        if spot_large:
            has_spot = True
            total_w = max(pill_w, int(round(312 * s)))
            inner_w = total_w - spot_pad_x * 2
            if spot_image_large is not None:
                gw = inner_w
                gh = int(round(spot_image_large.height * (gw / max(1, spot_image_large.width))))
                max_gh = int(round(196 * s))
                if gh > max_gh:
                    gh = max_gh
                    gw = int(round(spot_image_large.width * (gh / max(1, spot_image_large.height))))
                guide_img = spot_image_large.resize((max(1, gw), max(1, gh)), Image.Resampling.LANCZOS)
            body = spot_desc or spot_placeholder
            guide_lines = self._wrap_lines(body, desc_font, inner_w, 5) if body else []
            guide_footer = self._truncate_line(spot_footer, sub_font, inner_w) if spot_footer else ""
            content_h = 0
            if guide_img is not None:
                content_h += guide_img.height
            if guide_lines:
                content_h += (int(round(8 * s)) if guide_img is not None else 0) + len(guide_lines) * guide_line_h
            if guide_footer:
                content_h += int(round(8 * s)) + guide_line_h
            spot_h = max(int(round(40 * s)), content_h + spot_pad_y * 2)
        elif has_spot:
            total_w = max(pill_w, int(round(272 * s)))
            desc_avail_w = total_w - spot_pad_x * 2 - (thumb_w + thumb_gap if spot_thumb is not None else 0)
            desc_avail_w = max(int(round(110 * s)), desc_avail_w)
            spot_lines = self._wrap_two_lines(spot_desc, desc_font, desc_avail_w) if spot_desc else []
            line_h = int(round(16 * s))
            desc_h = len(spot_lines) * line_h + (int(round(2 * s)) if len(spot_lines) > 1 else 0)
            content_h = max(thumb_h if spot_thumb is not None else 0, desc_h)
            spot_h = content_h + spot_pad_y * 2

        total_h = pill_h + (spot_gap + spot_h if has_spot else 0)

        # Render rounded glass surfaces at 2x SS using STRICT alpha_composite (no low-alpha raw draws)
        ss = 2
        card_hi = Image.new("RGBA", (total_w * ss, total_h * ss), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(card_hi)

        pill_radius = (pill_h * ss) // 2 if not expanded else int(round(14 * s * ss))
        border_w = max(2, int(round(1.5 * s * ss)))

        # 1. Main pill solid dark glass base
        cdraw.rounded_rectangle(
            (1, 1, total_w * ss - 2, pill_h * ss - 2),
            radius=pill_radius,
            fill=self._GLASS_BG,
        )

        # 2. Main pill border on separate transparent layer -> alpha_composite
        border_hi = Image.new("RGBA", (total_w * ss, total_h * ss), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(border_hi)
        bdraw.rounded_rectangle(
            (1, 1, total_w * ss - 2, pill_h * ss - 2),
            radius=pill_radius,
            outline=pal["border"],
            width=border_w,
        )

        # 3. Spot card glass base + border
        if has_spot:
            sy0 = (pill_h + spot_gap) * ss
            sy1 = total_h * ss - 2
            spot_radius = int(round(12 * s * ss))
            cdraw.rounded_rectangle(
                (1, sy0, total_w * ss - 2, sy1),
                radius=spot_radius,
                fill=self._GLASS_BG,
            )
            bdraw.rounded_rectangle(
                (1, sy0, total_w * ss - 2, sy1),
                radius=spot_radius,
                outline=self._SPOT_BORDER,
                width=max(2, int(round(1.2 * s * ss))),
            )

        card_hi = Image.alpha_composite(card_hi, border_hi)

        # 4. Floor tag pill background (translucent tint via alpha_composite -> zero hole-punching!)
        text_x = pad_x + badge_px + gap_badge
        text_top = (pill_h - text_block_h) // 2
        tag_x = text_x + title_w + gap_tag
        tag_y = text_top + max(0, (max(title_h, tag_h) - tag_h) // 2)
        if floor_tag:
            tag_hi = Image.new("RGBA", (total_w * ss, total_h * ss), (0, 0, 0, 0))
            tgdraw = ImageDraw.Draw(tag_hi)
            tgdraw.rounded_rectangle(
                (tag_x * ss, tag_y * ss, (tag_x + tag_w) * ss, (tag_y + tag_h) * ss),
                radius=(tag_h * ss) // 2,
                fill=pal["tag_bg"],
                outline=pal["border"],
                width=max(1, int(round(1.0 * s * ss))),
            )
            card_hi = Image.alpha_composite(card_hi, tag_hi)

        # 5. Optional 1s hold-to-collect progress bar at bottom of main pill (via alpha_composite)
        if 0.0 < hold_progress < 1.0:
            bar_hi = Image.new("RGBA", (total_w * ss, total_h * ss), (0, 0, 0, 0))
            pdraw = ImageDraw.Draw(bar_hi)
            bar_margin = int(round(14 * s * ss))
            bar_h = max(3, int(round(2.5 * s * ss)))
            bar_y0 = pill_h * ss - bar_h - max(3, int(round(2.0 * s * ss)))
            full_bar_w = max(10, total_w * ss - bar_margin * 2)
            fill_w = max(4, int(round(full_bar_w * hold_progress)))
            pdraw.rounded_rectangle(
                (bar_margin, bar_y0, bar_margin + full_bar_w, bar_y0 + bar_h),
                radius=bar_h // 2,
                fill=(148, 163, 184, 70),
            )
            pdraw.rounded_rectangle(
                (bar_margin, bar_y0, bar_margin + fill_w, bar_y0 + bar_h),
                radius=bar_h // 2,
                fill=pal["accent"],
            )
            card_hi = Image.alpha_composite(card_hi, bar_hi)

        card = card_hi.resize((total_w, total_h), Image.Resampling.LANCZOS)

        # 6. Composite crisp 1x text layer onto card via alpha_composite (preserves alpha >= 236)
        text_layer = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(text_layer)

        title_y = text_top + max(0, (max(title_h, tag_h) - title_h) // 2)
        if clean_title:
            tdraw.text((text_x - tb[0], title_y - tb[1]), clean_title, font=title_font, fill=pal["title"])

        if floor_tag:
            tgb = tdraw.textbbox((0, 0), floor_tag, font=tag_font)
            tw_actual = tgb[2] - tgb[0]
            th_actual = tgb[3] - tgb[1]
            tx = tag_x + (tag_w - tw_actual) // 2 - tgb[0]
            ty = tag_y + (tag_h - th_actual) // 2 - tgb[1]
            tdraw.text((tx, ty), floor_tag, font=tag_font, fill=pal["tag_text"])

        if clean_sub:
            sb = tdraw.textbbox((0, 0), clean_sub, font=sub_font)
            sub_y = text_top + max(title_h, tag_h) + line_gap
            tdraw.text((text_x - sb[0], sub_y - sb[1]), clean_sub, font=sub_font, fill=pal["sub"])

        # 7a. Large Guide Card: image, wrapped description, muted footer hint
        if spot_large:
            spot_top = pill_h + spot_gap
            cur_y = spot_top + spot_pad_y
            if guide_img is not None:
                gx = (total_w - guide_img.width) // 2
                card.alpha_composite(guide_img, dest=(gx, cur_y))
                cur_y += guide_img.height + int(round(8 * s))
            for line in guide_lines:
                lb = tdraw.textbbox((0, 0), line, font=desc_font)
                tdraw.text((spot_pad_x - lb[0], cur_y - lb[1]), line, font=desc_font, fill=(226, 232, 240, 255))
                cur_y += guide_line_h
            if guide_footer:
                if guide_lines:
                    cur_y += int(round(8 * s))
                fb = tdraw.textbbox((0, 0), guide_footer, font=sub_font)
                tdraw.text((spot_pad_x - fb[0], cur_y - fb[1]), guide_footer, font=sub_font, fill=(148, 163, 184, 255))

        # 7b. Composite Spot Card thumbnail + 2-line description
        elif has_spot:
            spot_top = pill_h + spot_gap
            cur_x = spot_pad_x
            if spot_thumb is not None:
                thumb_resized = (
                    spot_thumb
                    if spot_thumb.size == (thumb_w, thumb_h)
                    else spot_thumb.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                )
                thumb_y = spot_top + (spot_h - thumb_h) // 2
                card.alpha_composite(thumb_resized, dest=(cur_x, thumb_y))
                cur_x += thumb_w + thumb_gap

            if spot_lines:
                line_h = int(round(16 * s))
                block_h = len(spot_lines) * line_h
                desc_y = spot_top + max(spot_pad_y, (spot_h - block_h) // 2)
                for idx, line in enumerate(spot_lines):
                    lb = tdraw.textbbox((0, 0), line, font=desc_font)
                    tdraw.text(
                        (cur_x - lb[0], desc_y + idx * line_h - lb[1]),
                        line,
                        font=desc_font,
                        fill=(226, 232, 240, 255),
                    )

        card = Image.alpha_composite(card, text_layer)
        badge_xy = (pad_x, (pill_h - badge_px) // 2)
        return card, badge_xy, badge_px, has_spot

    def render(
        self,
        guidance: NavGuidance,
        *,
        delta_deg: Optional[float] = None,
        lang: Optional[str] = None,
        scale: float = 1.0,
        hotkey_label: str = "Alt+F11",
        hold_progress: float = 0.0,
        spot_image: Optional[Image.Image | bytes] = None,
        distance_m: Optional[float] = None,
        guide_hotkey_label: str = "",
        guide_open: bool = False,
        guide_status: str = "",
        flash_text: str = "",
        flash_kind: str = "info",
        edit_mode: bool = False,
        edit_hint: str = "",
    ) -> Image.Image:
        """
        Renders the complete Navigation Mini-HUD (+ optional Spot / Guide Card) RGBA image.
        Steady-state compass rotation hits both static-card and badge caches (< 0.4 ms/frame).

        Optional overlays:
          - `distance_m`: smoothed distance (radar mode + title), defaults to `guidance.distance_m`.
          - `guide_open`: large in-game Guide Card (image + description) for the current target.
          - `flash_text`/`flash_kind`: short-lived action feedback in the subtitle row.
          - `edit_mode`: drag-to-move layout mode (violet frame + instruction subtitle).
        """
        eff_lang = (lang or get_language() or "en").strip().lower()
        eff_delta = guidance.delta_deg if delta_deg is None else float(delta_deg)
        eff_dist = guidance.distance_m if distance_m is None else float(distance_m)
        s = max(0.85, min(1.8, float(scale)))
        scale_q = round(s, 2)
        hold_clamped = max(0.0, min(1.0, float(hold_progress)))
        hold_q = int(round(hold_clamped * 20))

        title_text, subtitle_text, floor_tag, palette_name, expanded, dist_int, spot_desc = (
            self._resolve_texts_and_mode(
                guidance,
                lang=eff_lang,
                hotkey_label=hotkey_label,
                guide_hotkey_label=guide_hotkey_label,
                distance_m=eff_dist,
            )
        )
        base_state_palette = palette_name
        if flash_text:
            subtitle_text = flash_text
            expanded = True
            if flash_kind in self._PALETTES:
                palette_name = flash_kind
        if edit_mode:
            subtitle_text = edit_hint or t("hud.edit.hint", lang=eff_lang, hotkey="")
            expanded = True
            palette_name = "edit"
            if guidance.target is None:
                title_text = t("hud.edit.title", lang=eff_lang)

        target = guidance.target
        has_target = target is not None and guidance.state not in ("lost", "idle")
        show_guide = bool(guide_open and has_target)
        show_spot = bool(guidance.show_spot_card and target is not None) and not show_guide

        # Resolve optional Spot Card thumbnail (non-blocking LRU cache)
        spot_thumb: Optional[Image.Image] = None
        thumb_token: Optional[Any] = None
        if show_spot and target is not None:
            img_key = target.image_url or f"pin:{target.id}"
            if spot_image is not None:
                if isinstance(spot_image, Image.Image):
                    cache_key = f"{img_key}:{id(spot_image)}"
                    spot_thumb = self.get_thumbnail(cache_key)
                    if spot_thumb is None:
                        spot_thumb = self.put_thumbnail(cache_key, spot_image)
                else:
                    spot_thumb = self.put_thumbnail(img_key, spot_image)
            elif target.image_url:
                spot_thumb = self.get_thumbnail(target.image_url)
            if spot_thumb is not None:
                thumb_token = id(spot_thumb)

        guide_img: Optional[Image.Image] = None
        guide_placeholder = ""
        guide_footer = ""
        if show_guide and target is not None:
            if target.image_url:
                guide_img = self.get_guide_image(target.image_url)
            if guide_img is not None:
                thumb_token = ("guide", id(guide_img))
            if not target.description:
                if guide_status == "loading" or (target.image_url and guide_img is None):
                    guide_placeholder = t("hud.guide.loading", lang=eff_lang)
                elif guide_img is None:
                    guide_placeholder = t("hud.guide.empty", lang=eff_lang)
            if guide_hotkey_label:
                guide_footer = t("hud.guide.close_hint", lang=eff_lang, hotkey=guide_hotkey_label)

        # Layer 1 cache lookup keyed by static card state
        card_key = (
            title_text,
            dist_int,
            floor_tag,
            guidance.state,
            expanded,
            eff_lang,
            scale_q,
            subtitle_text,
            hold_q,
            bool(show_spot),
            thumb_token,
            spot_desc if (show_spot or show_guide) else "",
            palette_name,
            show_guide,
            guide_placeholder,
            guide_footer,
        )
        cached_card = self._card_cache.get(card_key)
        if cached_card is not None:
            self._card_cache.move_to_end(card_key)
            base_card, badge_xy, badge_px, has_spot = cached_card
            card_hit = True
        else:
            base_card, badge_xy, badge_px, has_spot = self._build_static_card(
                title_text=title_text,
                subtitle_text=subtitle_text,
                floor_tag=floor_tag,
                palette_name=palette_name,
                expanded=expanded,
                scale=scale_q,
                hold_progress=hold_q / 20.0,
                show_spot_card=show_spot,
                spot_thumb=spot_thumb,
                spot_desc=spot_desc,
                spot_large=show_guide,
                spot_image_large=guide_img,
                spot_placeholder=guide_placeholder,
                spot_footer=guide_footer,
            )
            self._card_cache[card_key] = (base_card, badge_xy, badge_px, has_spot)
            self._card_cache.move_to_end(card_key)
            while len(self._card_cache) > self.MAX_CARD_CACHE_ENTRIES:
                self._card_cache.popitem(last=False)
            card_hit = False

        # Layer 2: close-range radar dot, or pre-baked compass needle
        radar = bool(
            guidance.state in ("tracking", "arrived")
            and eff_delta is not None
            and eff_dist is not None
            and eff_dist <= self.RADAR_RANGE_M
        )
        badge_pal = base_state_palette if not edit_mode else "edit"
        lock_on = False
        behind = False
        angle_bucket = -1
        badge_hit = True
        radar_xy: Optional[Tuple[int, int]] = None
        if radar:
            badge, radar_xy = self.get_radar_badge(badge_pal, float(eff_delta), float(eff_dist), scale=scale_q)
        else:
            # Idle also spins a live compass if a view heading is supplied
            directional = guidance.state in ("tracking", "floor_mismatch", "arrived", "idle") and eff_delta is not None
            lock_on = bool(
                directional
                and not edit_mode
                and guidance.state == "tracking"
                and eff_delta is not None
                and abs(eff_delta) <= self.LOCK_ON_DEG
            )
            behind = bool(
                directional
                and not edit_mode
                and guidance.state in ("tracking", "floor_mismatch")
                and eff_delta is not None
                and abs(eff_delta) > self.BEHIND_DEG
            )
            badge, badge_hit, angle_bucket = self.get_compass_badge(
                badge_pal,
                eff_delta,
                scale=scale_q,
                directional=directional,
                lock_on=lock_on,
                behind=behind,
            )

        # Fast composite: copy cached card + alpha_composite 36x36 badge
        frame = base_card.copy()
        frame.alpha_composite(badge, dest=badge_xy)

        self.last_render_meta = {
            "expanded": expanded,
            "has_spot_card": has_spot,
            "guide_open": show_guide,
            "card_cache_hit": card_hit,
            "badge_cache_hit": badge_hit,
            "badge_rect": (badge_xy[0], badge_xy[1], badge_px, badge_px),
            "palette": "lock_on" if lock_on else palette_name,
            "lock_on": lock_on,
            "behind": behind,
            "radar": radar,
            "radar_xy": radar_xy,
            "angle_bucket": angle_bucket,
            "lang": eff_lang,
            "title": title_text,
            "subtitle": subtitle_text,
        }
        return frame


# ---------------------------------------------------------------------------
# Dedicated Win32 Layered Navigation Mini-HUD Window (30 Hz EMA + 2s WDA refresh)
# ---------------------------------------------------------------------------
POSITION_MODES: Tuple[str, ...] = (
    "under_minimap",
    "right_of_minimap",
    "top_center",
    "bottom_center",
    "custom",
)


class NavMiniHud:
    """
    Click-through, non-activating, per-pixel alpha Win32 layered Navigation Mini-HUD.
    - Placement presets (`POSITION_MODES`): under / right of the circular Minimap via
      `compute_ui_layout`, top-center, bottom-center, or a user-dragged `custom` anchor stored as
      fractions of the game window (resolution independent). Always clamped inside `work_area`.
    - Layout edit mode: the window temporarily drops `WS_EX_TRANSPARENT` and acts as a drag handle
      (hold Alt in Genshin to show the cursor, drag, press the layout hotkey again to finish).
    - 30 Hz critically-damped spring needle with distance-adaptive deadband + close-range radar.
    - Short-lived flash messages (action feedback) and a toggleable in-game Guide Card.
    - Periodically refreshes `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` every 2.0s.
    - Reuses GDI `DIBSection` when dimensions stay unchanged (zero handle leak).
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
    EX_STYLE = (
        WS_EX_LAYERED
        | WS_EX_TRANSPARENT
        | WS_EX_TOPMOST
        | WS_EX_NOACTIVATE
        | WS_EX_TOOLWINDOW
    )
    GWL_EXSTYLE = -20

    WS_POPUP = 0x80000000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = wintypes.HWND(-1)
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040

    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01
    DIB_RGB_COLORS = 0

    WM_DESTROY = 0x0002
    WM_SETCURSOR = 0x0020
    WM_MOUSEACTIVATE = 0x0021
    WM_NCHITTEST = 0x0084
    WM_TIMER = 0x0113
    WM_ENTERSIZEMOVE = 0x0231
    WM_EXITSIZEMOVE = 0x0232
    WM_APP_UPDATE = 0x8011
    WM_APP_HIDE = 0x8012
    WM_APP_CLOSE = 0x8013
    WM_APP_EDIT = 0x8014
    HTCAPTION = 2
    MA_NOACTIVATE = 3
    IDC_SIZEALL = 32646

    TIMER_RENDER = 101
    TIMER_WDA_REFRESH = 102
    RENDER_TICK_MS = 33       # 30 Hz spring smoothing loop
    WDA_REFRESH_MS = 2000     # 2.0s periodic WDA_EXCLUDEFROMCAPTURE refresh
    EDIT_TIMEOUT_S = 120.0    # Edit mode auto-exits so the HUD never stays click-blocking

    def __init__(
        self,
        *,
        position_mode: str = "under_minimap",
        gamepad_mode: bool = False,
        lang: Optional[str] = None,
        hotkey_label: str = "Alt+F11",
        ema_alpha: float = 0.55,
        ema_deadband_deg: float = 1.0,
        renderer: Optional[NavHudRenderer] = None,
        custom_pos: Optional[Tuple[float, float]] = None,
        on_position_changed: Optional[Callable[[str, Optional[Tuple[float, float]]], None]] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._hwnd: Optional[int] = None
        self._class_name = f"GenshinHelperNavMiniHud_{id(self)}"
        self._hinst = _kernel32.GetModuleHandleW(None)
        self._wndproc_ref = None
        self._closed = False

        self.renderer = renderer if renderer is not None else NavHudRenderer()
        # `ema_alpha` / `ema_deadband_deg` are kept for backwards-compatible construction; the
        # needle now uses a critically-damped spring with a distance-adaptive deadband.
        self.smoother = SpringAngleSmoother()
        self.distance_smoother = ScalarSmoother(tau_s=0.35)
        self._last_target_id: Optional[str] = None

        # Config & latest state shared across caller and UI thread
        self._position_mode = self._norm_position_mode(position_mode)
        self._custom_pos: Optional[Tuple[float, float]] = self._norm_custom_pos(custom_pos)
        self._gamepad_mode = bool(gamepad_mode)
        self._lang = lang
        self._hotkey_label = str(hotkey_label or "Alt+F11")
        self._guide_hotkey_label = ""
        self._layout_hotkey_label = ""
        self._window_rect: Optional[Tuple[int, int, int, int]] = None
        self._work_area: Optional[Tuple[int, int, int, int]] = None
        self._guidance: Optional[NavGuidance] = None
        self._hold_progress: float = 0.0
        self._spot_image: Optional[Image.Image | bytes] = None
        self._want_visible: bool = False
        self._state_version: int = 0
        self._guide_open: bool = False
        self._guide_status: str = ""
        self._flash: Optional[Tuple[str, str, float]] = None
        self._edit_mode: bool = False
        self._edit_started: float = 0.0
        self._on_position_changed = on_position_changed

        # UI-thread-owned GDI surface & stats
        self._memdc: Optional[int] = None
        self._hbmp: Optional[int] = None
        self._old_obj: Optional[int] = None
        self._dib_ptr: Optional[ctypes.c_void_p] = None
        self._dib_size: Tuple[int, int] = (0, 0)
        self._dst_pos: Tuple[int, int] = (0, 0)
        self._visible: bool = False
        self._render_timer_active: bool = False
        self._last_uploaded_sig: Optional[Tuple[Any, ...]] = None
        self._wda_refresh_count: int = 0
        self._dragging: bool = False
        self._click_through: bool = True

    @staticmethod
    def _norm_position_mode(mode: Optional[str]) -> str:
        norm = str(mode or "under_minimap").strip().lower()
        return norm if norm in POSITION_MODES else "under_minimap"

    @staticmethod
    def _norm_custom_pos(pos: Any) -> Optional[Tuple[float, float]]:
        if pos is None:
            return None
        try:
            fx, fy = (float(v) for v in pos)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(fx) and math.isfinite(fy)):
            return None
        return (max(0.0, min(0.98, fx)), max(0.0, min(0.98, fy)))

    @property
    def hwnd(self) -> Optional[int]:
        return self._hwnd

    @property
    def is_visible(self) -> bool:
        return self._visible

    @property
    def wda_refresh_count(self) -> int:
        return self._wda_refresh_count

    @property
    def position_mode(self) -> str:
        return self._position_mode

    @property
    def custom_pos(self) -> Optional[Tuple[float, float]]:
        return self._custom_pos

    @property
    def is_guide_open(self) -> bool:
        return self._guide_open

    @property
    def is_edit_mode(self) -> bool:
        return self._edit_mode

    def _post(self, msg: int) -> bool:
        hwnd = self._hwnd
        if not hwnd:
            return False
        return bool(_user32.PostMessageW(hwnd, msg, 0, 0))

    def set_position_mode(self, mode: str, custom_pos: Optional[Tuple[float, float]] = None) -> None:
        with self._lock:
            self._position_mode = self._norm_position_mode(mode)
            if custom_pos is not None:
                self._custom_pos = self._norm_custom_pos(custom_pos)
            self._state_version += 1
        self._post(self.WM_APP_UPDATE)

    def set_custom_position(self, custom_pos: Optional[Tuple[float, float]]) -> None:
        with self._lock:
            self._custom_pos = self._norm_custom_pos(custom_pos)
            self._state_version += 1
        self._post(self.WM_APP_UPDATE)

    def set_gamepad_mode(self, enabled: bool) -> None:
        with self._lock:
            self._gamepad_mode = bool(enabled)
            self._state_version += 1

    def set_language(self, lang: Optional[str]) -> None:
        with self._lock:
            self._lang = lang
            self._state_version += 1

    def set_hotkey_labels(
        self,
        *,
        mark: Optional[str] = None,
        guide: Optional[str] = None,
        layout: Optional[str] = None,
    ) -> None:
        with self._lock:
            if mark is not None:
                self._hotkey_label = str(mark)
            if guide is not None:
                self._guide_hotkey_label = str(guide)
            if layout is not None:
                self._layout_hotkey_label = str(layout)
            self._state_version += 1

    def put_thumbnail(self, key: str, image_or_bytes: Image.Image | bytes) -> bool:
        with self._lock:
            res = self.renderer.put_thumbnail(key, image_or_bytes)
            if res is not None:
                self._state_version += 1
                return True
            return False

    def put_guide_image(self, key: str, image_or_bytes: Image.Image | bytes) -> bool:
        ok = self.renderer.put_guide_image(key, image_or_bytes)
        if ok:
            with self._lock:
                self._state_version += 1
            self._post(self.WM_APP_UPDATE)
        return ok

    # ------------------------------------------------------------------
    # Flash feedback, Guide Card & Layout edit mode
    # ------------------------------------------------------------------
    def flash(self, text: str, kind: str = "info", duration_s: float = 2.6) -> None:
        """Shows `text` in the HUD subtitle row for `duration_s` (action feedback in-game)."""
        clean = " ".join(str(text or "").split())
        with self._lock:
            if clean:
                self._flash = (clean, str(kind or "info"), time.monotonic() + max(0.5, float(duration_s)))
            else:
                self._flash = None
            self._state_version += 1
        self._post(self.WM_APP_UPDATE)

    def set_guide_open(self, opened: bool) -> bool:
        with self._lock:
            self._guide_open = bool(opened)
            self._state_version += 1
        self._post(self.WM_APP_UPDATE)
        return self._guide_open

    def toggle_guide(self) -> bool:
        return self.set_guide_open(not self._guide_open)

    def set_guide_status(self, status: str) -> None:
        with self._lock:
            if self._guide_status != status:
                self._guide_status = str(status or "")
                self._state_version += 1
        self._post(self.WM_APP_UPDATE)

    def set_edit_mode(self, enabled: bool) -> bool:
        if enabled and not self._closed:
            self._ensure_window_thread()
        with self._lock:
            self._edit_mode = bool(enabled)
            self._edit_started = time.monotonic()
            self._state_version += 1
        self._post(self.WM_APP_EDIT)
        return self._edit_mode

    def toggle_edit_mode(self) -> bool:
        return self.set_edit_mode(not self._edit_mode)

    def _apply_click_through(self, click_through: bool) -> None:
        """UI thread only: toggles WS_EX_TRANSPARENT so the HUD can be grabbed in edit mode."""
        if not self._hwnd or self._click_through == click_through:
            return
        style = self.EX_STYLE if click_through else (self.EX_STYLE & ~self.WS_EX_TRANSPARENT)
        _user32.SetWindowLongPtrW(self._hwnd, self.GWL_EXSTYLE, style)
        _user32.SetWindowPos(
            self._hwnd,
            self.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE | self.SWP_FRAMECHANGED,
        )
        self._click_through = click_through

    def _finish_drag(self) -> None:
        """UI thread: converts the dropped window position into a resolution-independent anchor."""
        if not self._hwnd:
            return
        rc = _RECT()
        if not _user32.GetWindowRect(self._hwnd, ctypes.byref(rc)):
            return
        with self._lock:
            win_rect = self._window_rect
        if win_rect and win_rect[2] > 200 and win_rect[3] > 200:
            wl, wt, ww, wh = win_rect
        else:
            wl, wt = 0, 0
            ww = int(_user32.GetSystemMetrics(_SM_CXSCREEN)) or 1920
            wh = int(_user32.GetSystemMetrics(_SM_CYSCREEN)) or 1080
        fx = (int(rc.left) - wl) / float(max(1, ww))
        fy = (int(rc.top) - wt) / float(max(1, wh))
        pos = self._norm_custom_pos((fx, fy))
        with self._lock:
            self._position_mode = "custom"
            self._custom_pos = pos
            self._state_version += 1
        cb = self._on_position_changed
        if cb is not None:
            threading.Thread(target=self._safe_position_cb, args=(cb, "custom", pos), daemon=True).start()

    @staticmethod
    def _safe_position_cb(cb: Callable[..., None], mode: str, pos: Optional[Tuple[float, float]]) -> None:
        try:
            cb(mode, pos)
        except Exception as exc:
            logger.debug("NavMiniHud on_position_changed error: %s", exc)

    @staticmethod
    def compute_hud_position(
        hud_w: int,
        hud_h: int,
        window_rect: Optional[Tuple[int, int, int, int]] = None,
        *,
        position_mode: str = "under_minimap",
        gamepad_mode: bool = False,
        work_area: Optional[Tuple[int, int, int, int]] = None,
        custom_pos: Optional[Tuple[float, float]] = None,
    ) -> Tuple[int, int]:
        """
        Computes screen `(x, y)` for the Navigation Mini-HUD:
          - `"under_minimap"` (default): directly below the circular Minimap using
            `compute_ui_layout(win_w, win_h, gamepad_mode)`, avoiding top-center Boss HP bar.
          - `"right_of_minimap"`: beside the Minimap's right edge (keeps the quest tracker
            under the Minimap fully readable).
          - `"top_center"`: centers near the top edge of the game window.
          - `"bottom_center"`: centered above the party HP / skill row.
          - `"custom"`: top-left anchored at `custom_pos = (fx, fy)` fractions of the game window.
        Always clamps strictly inside `work_area` `(left, top, right, bottom)`.
        """
        if window_rect and window_rect[2] > 200 and window_rect[3] > 200:
            win_left, win_top, win_w, win_h = (int(v) for v in window_rect)
        else:
            win_left = 0
            win_top = 0
            win_w = int(_user32.GetSystemMetrics(_SM_CXSCREEN)) or 1920
            win_h = int(_user32.GetSystemMetrics(_SM_CYSCREEN)) or 1080

        if work_area is not None and len(work_area) == 4:
            wa_left, wa_top, wa_right, wa_bottom = (int(v) for v in work_area)
        else:
            wa_left, wa_top, wa_right, wa_bottom = _query_monitor_work_area(
                win_left + win_w // 2, win_top + win_h // 2
            )

        mode = NavMiniHud._norm_position_mode(position_mode)
        cpos = NavMiniHud._norm_custom_pos(custom_pos)
        if mode == "custom" and cpos is None:
            mode = "under_minimap"

        if mode == "top_center":
            raw_x = win_left + (win_w - int(hud_w)) / 2.0
            raw_y = win_top + max(18, int(round(win_h * 0.028)))
        elif mode == "bottom_center":
            raw_x = win_left + (win_w - int(hud_w)) / 2.0
            raw_y = win_top + win_h * 0.80 - int(hud_h)
        elif mode == "custom" and cpos is not None:
            raw_x = win_left + cpos[0] * win_w
            raw_y = win_top + cpos[1] * win_h
        else:
            layout = compute_ui_layout(win_w, win_h, gamepad_mode=bool(gamepad_mode))
            mm_x, mm_y, mm_w, mm_h = layout.minimap_rect
            gap = max(8, int(round(10.0 * layout.ui_scale)))
            if mode == "right_of_minimap":
                raw_x = win_left + mm_x + mm_w + gap
                raw_y = win_top + mm_y + max(0, int(round(mm_h * 0.08)))
            else:
                minimap_center_x = win_left + mm_x + (mm_w / 2.0)
                raw_x = minimap_center_x - (int(hud_w) / 2.0)
                raw_y = win_top + mm_y + mm_h + gap

        margin = 8
        min_x = wa_left + margin
        max_x = max(min_x, wa_right - int(hud_w) - margin)
        min_y = wa_top + margin
        max_y = max(min_y, wa_bottom - int(hud_h) - margin)

        clamped_x = max(min_x, min(max_x, int(round(raw_x))))
        clamped_y = max(min_y, min(max_y, int(round(raw_y))))
        return clamped_x, clamped_y

    # ------------------------------------------------------------------
    # Win32 Thread & GDI Surface Lifecycle (Zero Handle Leak)
    # ------------------------------------------------------------------
    def _ensure_window_thread(self) -> Optional[int]:
        if self._closed:
            return None
        with self._lock:
            if self._hwnd:
                return self._hwnd
            if self._thread is None or not self._thread.is_alive():
                self._ready_event.clear()
                self._thread = threading.Thread(
                    target=self._ui_thread_main,
                    name="GH_NavMiniHud",
                    daemon=True,
                )
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

    def _upload_image_to_window(self, img: Image.Image, x: int, y: int, *, keep_position: bool = False) -> None:
        x, y = int(x) + int(self.SCREEN_OFFSET[0]), int(y) + int(self.SCREEN_OFFSET[1])
        if not self._hwnd:
            return
        w, h = img.size
        if w <= 0 or h <= 0:
            return

        # Reuse existing DIBSection when dimensions match (zero GDI realloc per rotation frame)
        if self._dib_size != (w, h) or not self._memdc or not self._dib_ptr:
            self._release_gdi_surface()
            memdc = _gdi32.CreateCompatibleDC(None)
            if not memdc:
                return
            bmi = _BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h  # Top-down 32bpp DIB
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB

            bits_ptr = ctypes.c_void_p()
            hbmp = _gdi32.CreateDIBSection(
                memdc,
                ctypes.byref(bmi),
                self.DIB_RGB_COLORS,
                ctypes.byref(bits_ptr),
                None,
                0,
            )
            if not hbmp or not bits_ptr.value:
                _gdi32.DeleteDC(memdc)
                return
            old_obj = _gdi32.SelectObject(memdc, hbmp)
            self._memdc = memdc
            self._hbmp = hbmp
            self._old_obj = old_obj
            self._dib_ptr = bits_ptr
            self._dib_size = (w, h)

        raw = rgba_to_premultiplied_bgra(img)
        ctypes.memmove(self._dib_ptr.value, raw, len(raw))

        pt_src = _POINT(0, 0)
        sz = _SIZE(w, h)
        blend = _BLENDFUNCTION(self.AC_SRC_OVER, 0, 255, self.AC_SRC_ALPHA)
        if keep_position and self._visible:
            # While the user drags the HUD, let the system own the window position.
            pt_dst_ref = None
        else:
            self._dst_pos = (int(x), int(y))
            pt_dst_ref = ctypes.byref(_POINT(int(x), int(y)))
        _user32.UpdateLayeredWindow(
            self._hwnd,
            None,
            pt_dst_ref,
            ctypes.byref(sz),
            self._memdc,
            ctypes.byref(pt_src),
            0,
            ctypes.byref(blend),
            self.ULW_ALPHA,
        )
        if not self._visible:
            _user32.SetWindowPos(
                self._hwnd,
                self.HWND_TOPMOST,
                int(x),
                int(y),
                w,
                h,
                self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW,
            )
            self._visible = True

    def _placeholder_guidance(self) -> NavGuidance:
        return NavGuidance(
            state="idle",
            target=None,
            distance_m=None,
            target_distance_m=None,
            bearing_deg=None,
            delta_deg=None,
            floor_mismatch=False,
            route_mode="none",
        )

    def _render_tick(self, force: bool = False) -> None:
        if not self._hwnd:
            return
        now = time.monotonic()
        with self._lock:
            if self._flash is not None and now >= self._flash[2]:
                self._flash = None
                self._state_version += 1
            if self._edit_mode and (now - self._edit_started) > self.EDIT_TIMEOUT_S:
                self._edit_mode = False
                self._state_version += 1
            want_visible = self._want_visible
            guidance = self._guidance
            win_rect = self._window_rect
            work_area = self._work_area
            pos_mode = self._position_mode
            custom_pos = self._custom_pos
            gp_mode = self._gamepad_mode
            lang = self._lang
            hotkey_label = self._hotkey_label
            guide_label = self._guide_hotkey_label
            layout_label = self._layout_hotkey_label
            hold_progress = self._hold_progress
            spot_image = self._spot_image
            state_ver = self._state_version
            flash = self._flash
            guide_open = self._guide_open
            guide_status = self._guide_status
            edit_mode = self._edit_mode

        self._apply_click_through(not edit_mode)

        if not edit_mode and (not want_visible or guidance is None):
            if self._visible:
                _user32.ShowWindow(self._hwnd, self.SW_HIDE)
                self._visible = False
                self._last_uploaded_sig = None
            if self._render_timer_active:
                _user32.KillTimer(self._hwnd, self.TIMER_RENDER)
                self._render_timer_active = False
            return
        if guidance is None or (edit_mode and not want_visible):
            guidance = self._placeholder_guidance()

        if not self._render_timer_active:
            _user32.SetTimer(self._hwnd, self.TIMER_RENDER, self.RENDER_TICK_MS, None)
            self._render_timer_active = True

        # Distance smoothing (reset on target switch so the title never 'counts' between pins)
        target_id = guidance.target.id if guidance.target is not None else None
        if target_id != self._last_target_id:
            self.distance_smoother.reset()
            self._last_target_id = target_id
        smoothed_dist = self.distance_smoother.update(guidance.distance_m, now=now)
        if guidance.distance_m is None:
            smoothed_dist = None

        # 30 Hz critically-damped spring toward target delta_deg (adaptive deadband by distance)
        if guidance.delta_deg is not None and guidance.state in ("tracking", "floor_mismatch", "arrived", "idle"):
            self.smoother.set_distance(smoothed_dist)
            self.smoother.set_target(guidance.delta_deg)
            smoothed_delta = self.smoother.step(now=now)
        else:
            self.smoother.reset()
            smoothed_delta = None

        radar = bool(
            smoothed_delta is not None
            and smoothed_dist is not None
            and guidance.state in ("tracking", "arrived")
            and smoothed_dist <= NavHudRenderer.RADAR_RANGE_M
        )
        if radar:
            visual_key: Tuple[Any, ...] = ("radar", int(round(smoothed_delta)), int(round(smoothed_dist * 2.0)))
        else:
            visual_key = ("needle", NavHudRenderer.quantize_angle_bucket(smoothed_delta))

        win_h = win_rect[3] if (win_rect and win_rect[3] > 200) else (
            int(_user32.GetSystemMetrics(_SM_CYSCREEN)) or 1080
        )
        scale = max(0.85, min(1.6, win_h / 1080.0))

        sig = (
            state_ver,
            visual_key,
            int(round(smoothed_dist)) if smoothed_dist is not None else None,
            win_rect,
            work_area,
            pos_mode,
            custom_pos,
            gp_mode,
            self._dragging,
        )
        if not force and self._visible and self._last_uploaded_sig == sig:
            return

        edit_hint = t("hud.edit.hint", lang=lang, hotkey=layout_label or "") if edit_mode else ""
        img = self.renderer.render(
            guidance,
            delta_deg=smoothed_delta,
            lang=lang,
            scale=scale,
            hotkey_label=hotkey_label,
            hold_progress=hold_progress,
            spot_image=spot_image,
            distance_m=smoothed_dist,
            guide_hotkey_label=guide_label,
            guide_open=guide_open,
            guide_status=guide_status,
            flash_text=flash[0] if flash else "",
            flash_kind=flash[1] if flash else "info",
            edit_mode=edit_mode,
            edit_hint=edit_hint,
        )
        x, y = self.compute_hud_position(
            img.width,
            img.height,
            window_rect=win_rect,
            position_mode=pos_mode,
            gamepad_mode=gp_mode,
            work_area=work_area,
            custom_pos=custom_pos,
        )
        self._upload_image_to_window(img, x, y, keep_position=self._dragging)
        self._last_uploaded_sig = sig

    def _ui_thread_main(self) -> None:
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == self.WM_APP_UPDATE:
                try:
                    self._render_tick(force=True)
                except Exception as exc:
                    logger.debug("NavMiniHud render update error: %s", exc)
                return 0
            if msg == self.WM_APP_EDIT:
                try:
                    self._apply_click_through(not self._edit_mode)
                    self._render_tick(force=True)
                except Exception as exc:
                    logger.debug("NavMiniHud edit toggle error: %s", exc)
                return 0
            if msg == self.WM_APP_HIDE:
                if self._edit_mode:
                    return 0
                if self._render_timer_active:
                    _user32.KillTimer(hwnd, self.TIMER_RENDER)
                    self._render_timer_active = False
                if self._visible:
                    _user32.ShowWindow(hwnd, self.SW_HIDE)
                    self._visible = False
                self._last_uploaded_sig = None
                return 0
            if msg == self.WM_NCHITTEST and self._edit_mode:
                return self.HTCAPTION
            if msg == self.WM_MOUSEACTIVATE:
                return self.MA_NOACTIVATE
            if msg == self.WM_SETCURSOR and self._edit_mode:
                _user32.SetCursor(_user32.LoadCursorW(None, ctypes.c_void_p(self.IDC_SIZEALL)))
                return 1
            if msg == self.WM_ENTERSIZEMOVE:
                self._dragging = True
                return 0
            if msg == self.WM_EXITSIZEMOVE:
                self._dragging = False
                try:
                    self._finish_drag()
                    self._render_tick(force=True)
                except Exception as exc:
                    logger.debug("NavMiniHud drag finish error: %s", exc)
                return 0
            if msg == self.WM_TIMER:
                if wparam == self.TIMER_RENDER:
                    try:
                        self._render_tick(force=False)
                    except Exception as exc:
                        logger.debug("NavMiniHud tick error: %s", exc)
                    return 0
                if wparam == self.TIMER_WDA_REFRESH:
                    if refresh_exclude_from_capture(int(hwnd), True):
                        self._wda_refresh_count += 1
                    if self._visible:
                        _user32.SetWindowPos(
                            hwnd,
                            self.HWND_TOPMOST,
                            0,
                            0,
                            0,
                            0,
                            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE,
                        )
                    return 0
            if msg == self.WM_APP_CLOSE:
                _user32.KillTimer(hwnd, self.TIMER_RENDER)
                _user32.KillTimer(hwnd, self.TIMER_WDA_REFRESH)
                self._render_timer_active = False
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == self.WM_DESTROY:
                _user32.KillTimer(hwnd, self.TIMER_RENDER)
                _user32.KillTimer(hwnd, self.TIMER_WDA_REFRESH)
                self._render_timer_active = False
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
            logger.warning("NavMiniHud RegisterClassW failed: %s", ctypes.get_last_error())
            self._ready_event.set()
            return

        hwnd = _user32.CreateWindowExW(
            self.EX_STYLE,
            self._class_name,
            "GenshinHelperNavMiniHUD",
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
            logger.warning("NavMiniHud CreateWindowExW failed: %s", ctypes.get_last_error())
            self._ready_event.set()
            return

        self._hwnd = int(hwnd)
        self._click_through = True
        # Apply WDA_EXCLUDEFROMCAPTURE immediately and refresh every 2.0s
        if refresh_exclude_from_capture(self._hwnd, True):
            self._wda_refresh_count += 1
        _user32.SetTimer(self._hwnd, self.TIMER_WDA_REFRESH, self.WDA_REFRESH_MS, None)
        self._ready_event.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        self._hwnd = None
        self._visible = False
        try:
            _user32.UnregisterClassW(self._class_name, self._hinst)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def update(
        self,
        guidance: NavGuidance,
        *,
        window_rect: Optional[Tuple[int, int, int, int]] = None,
        work_area: Optional[Tuple[int, int, int, int]] = None,
        gamepad_mode: Optional[bool] = None,
        position_mode: Optional[str] = None,
        lang: Optional[str] = None,
        hotkey_label: Optional[str] = None,
        hold_progress: float = 0.0,
        spot_image: Optional[Image.Image | bytes] = None,
        guide_hotkey_label: Optional[str] = None,
    ) -> bool:
        """Updates HUD guidance payload and posts an immediate layered window refresh."""
        if self._closed:
            return False
        hwnd = self._ensure_window_thread()
        if not hwnd:
            return False

        with self._lock:
            self._guidance = guidance
            if window_rect is not None:
                self._window_rect = tuple(int(v) for v in window_rect)
            if work_area is not None:
                self._work_area = tuple(int(v) for v in work_area)
            if gamepad_mode is not None:
                self._gamepad_mode = bool(gamepad_mode)
            if position_mode is not None:
                self._position_mode = self._norm_position_mode(position_mode)
            if lang is not None:
                self._lang = lang
            if hotkey_label is not None:
                self._hotkey_label = str(hotkey_label)
            if guide_hotkey_label is not None:
                self._guide_hotkey_label = str(guide_hotkey_label)
            self._hold_progress = max(0.0, min(1.0, float(hold_progress)))
            self._spot_image = spot_image
            self._want_visible = True
            self._state_version += 1

        return bool(_user32.PostMessageW(hwnd, self.WM_APP_UPDATE, 0, 0))

    def set_window_rect(self, window_rect: Optional[Tuple[int, int, int, int]]) -> None:
        if window_rect is None:
            return
        with self._lock:
            self._window_rect = tuple(int(v) for v in window_rect)

    def hide(self) -> bool:
        with self._lock:
            self._want_visible = False
            hwnd = self._hwnd
        if not hwnd:
            return False
        return bool(_user32.PostMessageW(hwnd, self.WM_APP_HIDE, 0, 0))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._want_visible = False
            self._edit_mode = False
            hwnd = self._hwnd
            thread = self._thread
        if hwnd:
            try:
                _user32.PostMessageW(hwnd, self.WM_APP_CLOSE, 0, 0)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self._thread = None
        self._hwnd = None
        self._visible = False
