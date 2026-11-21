# Ported from GengGode/cvAutoTrack@336a4a6:source/genshin/genshin.handle.cpp (MIT License)
# Ported from GengGode/cvAutoTrack@336a4a6:source/global/global.genshin.h (MIT License)
# Copyright (c) 2021 GengGode
# Windows UIPI checks, HoYoPlay registry discovery, borderless launch argument builder,
# Auto-HDR clipping detector, and SetWindowDisplayAffinity helper are clean-room original code.
"""Game display settings resilience: Unity CanvasScaler UI layout, UIPI, borderless launch, HDR & WDA."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import logging
import math
from pathlib import Path
import subprocess
from typing import Any, Dict, Iterator, List, Optional, Tuple
import winreg

import cv2
import numpy as np

logger = logging.getLogger("GenshinHelper.GameSettings")

# ---------------------------------------------------------------------------
# Canonical 1080p Unity Canvas coordinates (ported from cvAutoTrack global.genshin.h)
# ---------------------------------------------------------------------------
REF_CANVAS_W = 1920
REF_CANVAS_H = 1080
REF_ASPECT_16_9 = REF_CANVAS_W / REF_CANVAS_H

MINIMAP_RECT_DESKTOP_1080P: Tuple[int, int, int, int] = (59, 15, 218, 218)
MINIMAP_RECT_GAMEPAD_1080P: Tuple[int, int, int, int] = (117, 45, 182, 182)
PAIMON_RECT_DESKTOP_1080P: Tuple[int, int, int, int] = (25, 12, 68, 77)
PAIMON_RECT_GAMEPAD_1080P: Tuple[int, int, int, int] = (89, 42, 57, 64)

# Auto-HDR SDR clipping thresholds
HDR_WHITE_LUMA_THRESHOLD = 250
HDR_CLIP_P95_THRESHOLD = 250.0
HDR_CLIP_WHITE_RATIO_THRESHOLD = 0.35

# Win32 constants
TOKEN_QUERY = 0x0008
TOKEN_INFORMATION_CLASS_ELEVATION = 20
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
SM_CXSCREEN = 0
SM_CYSCREEN = 1
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


# Explicit 64-bit Win32 ctypes prototypes
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL

_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
_user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowDisplayAffinity.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# 1. Unity CanvasScaler UI Layout Normalization
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UiLayout:
    """Normalized Unity CanvasScaler layout and physical frame ROIs `(x, y, w, h)`."""

    frame_size: Tuple[int, int]
    canvas_size: Tuple[int, int]
    ui_scale: float
    aspect_ratio: float
    aspect_label: str
    gamepad_mode: bool
    minimap_rect_canvas: Tuple[int, int, int, int]
    minimap_rect: Tuple[int, int, int, int]
    paimon_rect_canvas: Tuple[int, int, int, int]
    paimon_rect: Tuple[int, int, int, int]
    minimap_maybe_rect: Tuple[int, int, int, int]
    paimon_maybe_rect: Tuple[int, int, int, int]
    uid_rect_canvas: Tuple[int, int, int, int]
    uid_rect: Tuple[int, int, int, int]
    uid_maybe_rect: Tuple[int, int, int, int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)

    def keys(self) -> Iterator[str]:
        return iter(self.__dataclass_fields__.keys())


def _classify_aspect(aspect: float) -> str:
    if abs(aspect - (16.0 / 9.0)) < 0.03:
        return "16:9"
    if aspect >= 2.80:
        return "32:9"
    if 2.20 <= aspect < 2.80:
        return "21:9"
    if 1.55 <= aspect <= 1.65:
        return "16:10"
    if 1.25 <= aspect <= 1.40:
        return "4:3"
    return "ultrawide" if aspect > REF_ASPECT_16_9 else "tall"


def _scale_and_clamp_rect(
    rect_canvas: Tuple[int, int, int, int],
    scale: float,
    frame_w: int,
    frame_h: int,
) -> Tuple[int, int, int, int]:
    cx, cy, cw, ch = rect_canvas
    x = max(0, min(frame_w - 1, int(round(cx * scale))))
    y = max(0, min(frame_h - 1, int(round(cy * scale))))
    w = max(1, min(frame_w - x, int(round(cw * scale))))
    h = max(1, min(frame_h - y, int(round(ch * scale))))
    return (x, y, w, h)


def compute_ui_layout(
    frame_w: int,
    frame_h: int,
    gamepad_mode: bool = False,
) -> UiLayout:
    """
    Computes Unity CanvasScaler (`ScaleWithScreenSize`, 1920x1080 Expand) UI geometry
    across 16:9, 21:9, 16:10, 4:3, 32:9, and Gamepad safe-area modes.
    Ported from cvAutoTrack `GetUiRects` + `MiniMapRect` / `MiniMapHandleRect`.
    """
    fw = int(frame_w)
    fh = int(frame_h)
    if fw <= 0 or fh <= 0:
        raise ValueError(f"Invalid frame dimensions: ({fw}, {fh})")

    aspect = float(fw) / float(fh)
    if abs(aspect - REF_ASPECT_16_9) <= 1e-6:
        ui_scale = float(fh) / float(REF_CANVAS_H)
        canvas_w = REF_CANVAS_W
        canvas_h = REF_CANVAS_H
    elif aspect > REF_ASPECT_16_9:
        # Wider than 16:9 (21:9, 32:9): height anchors to 1080, width expands
        ui_scale = float(fh) / float(REF_CANVAS_H)
        canvas_w = int(fw / ui_scale)
        canvas_h = REF_CANVAS_H
    else:
        # Taller than 16:9 (16:10, 4:3): width anchors to 1920, height expands
        ui_scale = float(fw) / float(REF_CANVAS_W)
        canvas_w = REF_CANVAS_W
        canvas_h = int(fh / ui_scale)

    gp = bool(gamepad_mode)
    minimap_canvas = MINIMAP_RECT_GAMEPAD_1080P if gp else MINIMAP_RECT_DESKTOP_1080P
    paimon_canvas = PAIMON_RECT_GAMEPAD_1080P if gp else PAIMON_RECT_DESKTOP_1080P

    paimon_maybe_canvas = (0, 0, int(canvas_w * 0.10), int(canvas_h * 0.10))
    minimap_maybe_canvas = (0, 0, int(canvas_w * 0.18), int(canvas_h * 0.22))

    uid_maybe_left = int(canvas_w * 0.88)
    uid_maybe_top = int(canvas_h * 0.97)
    uid_maybe_canvas = (
        uid_maybe_left,
        uid_maybe_top,
        max(1, canvas_w - uid_maybe_left),
        max(1, canvas_h - uid_maybe_top),
    )

    uid_x = int(math.ceil(canvas_w - canvas_w * (1.0 - 0.865)))
    uid_y = int(math.ceil(canvas_h - float(REF_CANVAS_H) * (1.0 - 0.9755)))
    uid_w = int(math.ceil(REF_CANVAS_W * 0.11))
    uid_h = int(math.ceil(REF_CANVAS_W * 0.0938 * 0.11))
    uid_x = max(0, min(canvas_w - 1, uid_x))
    uid_y = max(0, min(canvas_h - 1, uid_y))
    uid_w = max(1, min(canvas_w - uid_x, uid_w))
    uid_h = max(1, min(canvas_h - uid_y, uid_h))
    uid_canvas = (uid_x, uid_y, uid_w, uid_h)

    return UiLayout(
        frame_size=(fw, fh),
        canvas_size=(canvas_w, canvas_h),
        ui_scale=ui_scale,
        aspect_ratio=aspect,
        aspect_label=_classify_aspect(aspect),
        gamepad_mode=gp,
        minimap_rect_canvas=minimap_canvas,
        minimap_rect=_scale_and_clamp_rect(minimap_canvas, ui_scale, fw, fh),
        paimon_rect_canvas=paimon_canvas,
        paimon_rect=_scale_and_clamp_rect(paimon_canvas, ui_scale, fw, fh),
        minimap_maybe_rect=_scale_and_clamp_rect(minimap_maybe_canvas, ui_scale, fw, fh),
        paimon_maybe_rect=_scale_and_clamp_rect(paimon_maybe_canvas, ui_scale, fw, fh),
        uid_rect_canvas=uid_canvas,
        uid_rect=_scale_and_clamp_rect(uid_canvas, ui_scale, fw, fh),
        uid_maybe_rect=_scale_and_clamp_rect(uid_maybe_canvas, ui_scale, fw, fh),
    )


# ---------------------------------------------------------------------------
# 2. Windows UIPI / Process Elevation Checks (metadata-only, zero memory read)
# ---------------------------------------------------------------------------
def _query_token_elevated(process_handle: int) -> Optional[bool]:
    h_token = wintypes.HANDLE()
    ctypes.set_last_error(0)
    if not _advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(h_token)):
        return None
    try:
        elev = TOKEN_ELEVATION()
        ret_len = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        ok = _advapi32.GetTokenInformation(
            h_token,
            TOKEN_INFORMATION_CLASS_ELEVATION,
            ctypes.byref(elev),
            ctypes.sizeof(elev),
            ctypes.byref(ret_len),
        )
        if not ok:
            return None
        return bool(elev.TokenIsElevated)
    finally:
        if h_token.value:
            _kernel32.CloseHandle(h_token)


def is_process_elevated() -> bool:
    """Returns True if the current Python process is running elevated (Administrator)."""
    try:
        res = _query_token_elevated(_kernel32.GetCurrentProcess())
        return bool(res)
    except Exception as e:
        logger.debug(f"is_process_elevated() error: {e}")
        return False


def is_hwnd_elevated(hwnd: int) -> Optional[bool]:
    """
    Returns True if the process owning `hwnd` is elevated, False if standard user,
    or None if the window is invalid or cannot be queried.
    """
    try:
        hwnd_int = int(hwnd or 0)
        if hwnd_int <= 0 or not _user32.IsWindow(hwnd_int):
            return None

        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd_int, ctypes.byref(pid))
        if not pid.value:
            return None

        ctypes.set_last_error(0)
        h_proc = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED and not is_process_elevated():
                return True
            return None

        try:
            res = _query_token_elevated(h_proc)
            if res is None:
                err = ctypes.get_last_error()
                if err == ERROR_ACCESS_DENIED and not is_process_elevated():
                    return True
            return res
        finally:
            _kernel32.CloseHandle(h_proc)
    except Exception as e:
        logger.debug(f"is_hwnd_elevated({hwnd}) error: {e}")
        return None


def check_uipi_status(game_hwnd: Optional[int] = None) -> Dict[str, Any]:
    """
    Diagnoses Windows UIPI mismatch where Genshin runs as Administrator while the helper
    runs at Medium integrity (which causes UIPI to block global hotkeys / focus calls).
    """
    self_elevated = is_process_elevated()
    game_elevated = is_hwnd_elevated(game_hwnd) if game_hwnd else None
    uipi_blocked = bool(game_elevated is True and not self_elevated)

    if uipi_blocked:
        reason = "Game process is elevated (Administrator) while helper is standard user; UIPI may block hotkeys."
    elif not game_hwnd:
        reason = "No game window handle provided."
    elif game_elevated is None:
        reason = "Unable to determine game process elevation."
    else:
        reason = "OK"

    return {
        "self_elevated": self_elevated,
        "game_elevated": game_elevated,
        "uipi_blocked": uipi_blocked,
        "needs_elevation": uipi_blocked,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 3. HoYoPlay Registry Discovery & Borderless Window Launch
# ---------------------------------------------------------------------------
_HOYOPLAY_REGISTRY_CANDIDATES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (r"Software\Cognosphere\HYP\1_0\hk4e_global", ("GenshinImpact.exe", "YuanShen.exe")),
    (r"Software\Cognosphere\HYP\1_0\hk4e_cn", ("YuanShen.exe", "GenshinImpact.exe")),
    (r"Software\miHoYo\HYP\1_0\hk4e_cn", ("YuanShen.exe", "GenshinImpact.exe")),
)


def find_genshin_executable() -> Optional[str]:
    """
    Locates `GenshinImpact.exe` / `YuanShen.exe` from HoYoPlay HKCU registry keys
    (`hk4e_global` and `hk4e_cn`). Returns full path string or None.
    """
    for subkey, exe_names in _HOYOPLAY_REGISTRY_CANDIDATES:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                install_path, _ = winreg.QueryValueEx(key, "GameInstallPath")
        except OSError:
            continue

        if not isinstance(install_path, str) or not install_path.strip():
            continue

        base_dir = Path(install_path.strip())
        for exe_name in exe_names:
            candidate = base_dir / exe_name
            if candidate.is_file():
                return str(candidate)
    return None


def build_borderless_launch_args(
    width: int,
    height: int,
    exe_path: Optional[str] = None,
) -> List[str]:
    """Builds Unity borderless windowed command-line arguments without altering game files."""
    w = int(width)
    h = int(height)
    if w <= 0 or h <= 0:
        raise ValueError(f"Invalid screen dimensions: ({w}, {h})")
    args = [
        "-popupwindow",
        "-screen-fullscreen",
        "0",
        "-screen-width",
        str(w),
        "-screen-height",
        str(h),
    ]
    if exe_path:
        return [str(exe_path), *args]
    return args


def launch_genshin_borderless(
    width: Optional[int] = None,
    height: Optional[int] = None,
    exe_path: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Launches Genshin Impact in Unity borderless windowed mode (`-popupwindow`)."""
    resolved_exe = exe_path or find_genshin_executable()
    if not resolved_exe:
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "exe_path": None,
            "cmd": [],
            "pid": None,
            "error": "Genshin Impact executable not found in HoYoPlay registry.",
        }

    w = int(width) if width is not None else int(_user32.GetSystemMetrics(SM_CXSCREEN))
    h = int(height) if height is not None else int(_user32.GetSystemMetrics(SM_CYSCREEN))
    cmd = build_borderless_launch_args(w, h, exe_path=resolved_exe)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "exe_path": str(resolved_exe),
            "cmd": cmd,
            "pid": None,
            "error": None,
        }

    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(resolved_exe).parent),
            creationflags=flags,
        )
        return {
            "ok": True,
            "dry_run": False,
            "exe_path": str(resolved_exe),
            "cmd": cmd,
            "pid": int(proc.pid),
            "error": None,
        }
    except OSError as e:
        return {
            "ok": False,
            "dry_run": False,
            "exe_path": str(resolved_exe),
            "cmd": cmd,
            "pid": None,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# 4. Windows Auto-HDR SDR Clipping Detector
# ---------------------------------------------------------------------------
def detect_hdr_clipping(frame_bgr: Optional[np.ndarray]) -> Dict[str, Any]:
    """
    Detects washed-out SDR clamping typical of Windows Auto-HDR captured via GDI BitBlt.
    Returns `{clipped: bool, p95: float, white_ratio: float}`.
    """
    if frame_bgr is None or not isinstance(frame_bgr, np.ndarray) or frame_bgr.size == 0:
        return {"clipped": False, "p95": 0.0, "white_ratio": 0.0}

    if frame_bgr.ndim == 2:
        gray = frame_bgr
    elif frame_bgr.ndim == 3 and frame_bgr.shape[2] >= 3:
        gray = cv2.cvtColor(frame_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
    else:
        return {"clipped": False, "p95": 0.0, "white_ratio": 0.0}

    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)

    total = int(gray.size)
    if total <= 0:
        return {"clipped": False, "p95": 0.0, "white_ratio": 0.0}

    hist = np.bincount(gray.ravel(), minlength=256)
    cdf = np.cumsum(hist)
    p95_idx = int(np.searchsorted(cdf, 0.95 * total, side="left"))
    p95 = float(min(255, p95_idx))
    white_ratio = float(hist[HDR_WHITE_LUMA_THRESHOLD:].sum()) / float(total)
    clipped = bool(p95 >= HDR_CLIP_P95_THRESHOLD and white_ratio >= HDR_CLIP_WHITE_RATIO_THRESHOLD)

    return {
        "clipped": clipped,
        "p95": p95,
        "white_ratio": white_ratio,
    }


# ---------------------------------------------------------------------------
# 5. Overlay Capture Exclusion (`SetWindowDisplayAffinity`)
# ---------------------------------------------------------------------------
def refresh_exclude_from_capture(hwnd: int, exclude: bool = True) -> bool:
    """
    Calls `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` (or `WDA_NONE`)
    so our own overlay windows never pollute game CV captures.
    """
    try:
        hwnd_int = int(hwnd or 0)
        if hwnd_int <= 0 or not _user32.IsWindow(hwnd_int):
            return False
        affinity = WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE
        ctypes.set_last_error(0)
        ok = bool(_user32.SetWindowDisplayAffinity(hwnd_int, affinity))
        if not ok:
            logger.debug(
                f"SetWindowDisplayAffinity(hwnd={hwnd_int}, affinity={hex(affinity)}) failed: "
                f"winerr={ctypes.get_last_error()}"
            )
        return ok
    except Exception as e:
        logger.debug(f"refresh_exclude_from_capture({hwnd}, exclude={exclude}) error: {e}")
        return False
