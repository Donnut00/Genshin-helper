"""
Verification checks for flicker-free GDI SRCCOPY capture, strict game window detection,
buffer independence, multi-thread cleanup, and zero desktop handle leaks.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import os
import sys

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import main as app_main
from src.capture import ScreenCapturer, WindowFinder


def _get_process_handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL
    count = wintypes.DWORD(0)
    ok = kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    assert ok, f"GetProcessHandleCount failed: {ctypes.get_last_error()}"
    return int(count.value)


def verify_rop_constant() -> None:
    rop = ScreenCapturer.BITBLT_ROP
    assert rop == 0x00CC0020, f"Expected SRCCOPY (0x00CC0020), got {hex(rop)}"
    assert (rop & 0x40000000) == 0, f"CAPTUREBLT flag must NOT be set in ROP: {hex(rop)}"
    print(f"  [OK] BITBLT_ROP={hex(rop)} (SRCCOPY without CAPTUREBLT)")


def verify_capture_behavior() -> None:
    capturer = ScreenCapturer()
    try:
        frame1 = capturer.capture_region(0, 0, 320, 180)
        assert isinstance(frame1, np.ndarray), f"Expected ndarray, got {type(frame1)}"
        assert frame1.shape == (180, 320, 3), f"Unexpected shape: {frame1.shape}"
        assert frame1.dtype == np.uint8, f"Unexpected dtype: {frame1.dtype}"
        assert float(frame1.std()) > 0.0, "Captured frame has zero variance (unexpected blank/black frame)"

        # Mutate frame1 and ensure subsequent capture returns an independent buffer
        sentinel = np.array([17, 42, 99], dtype=np.uint8)
        frame1[:] = sentinel
        frame2 = capturer.capture_region(0, 0, 320, 180)
        assert isinstance(frame2, np.ndarray) and frame2.shape == (180, 320, 3)
        assert not np.shares_memory(frame1, frame2), "frame1 and frame2 share underlying memory!"
        assert not np.all(frame2 == sentinel), "Mutating frame1 affected frame2!"
        assert float(frame2.std()) > 0.0, "Second frame has zero variance"

        # Resize capture region dynamically
        frame_resized = capturer.capture_region(0, 0, 640, 360)
        assert isinstance(frame_resized, np.ndarray) and frame_resized.shape == (360, 640, 3)
        assert float(frame_resized.std()) > 0.0, "Resized frame has zero variance"

        # Invalid dimensions must return None cleanly
        assert capturer.capture_region(0, 0, 0, 100) is None, "width=0 must return None"
        assert capturer.capture_region(0, 0, 100, -5) is None, "height=-5 must return None"

        # Negative virtual-desktop coordinates must not clamp or fail
        neg_frame = capturer.capture_region(-8, 0, 64, 64)
        assert isinstance(neg_frame, np.ndarray) and neg_frame.shape == (64, 64, 3), f"Unexpected neg_frame: {None if neg_frame is None else neg_frame.shape}"
        assert neg_frame.dtype == np.uint8

        # Multi-thread worker capture
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker_frame = pool.submit(capturer.capture_region, 0, 0, 320, 180).result(timeout=5.0)
        assert isinstance(worker_frame, np.ndarray) and worker_frame.shape == (180, 320, 3)
        assert float(worker_frame.std()) > 0.0, "Worker thread captured zero-variance frame"
        assert len(capturer._sessions) >= 2, f"Expected >= 2 tracked GDI sessions, got {len(capturer._sessions)}"
    finally:
        capturer.close()

    assert len(capturer._sessions) == 0, f"Expected 0 tracked sessions after close(), got {len(capturer._sessions)}"
    print("  [OK] Capture BGR shape, non-zero variance, buffer independence, resize, multi-thread, and close() passed")


def verify_window_finder_and_handle_leak() -> None:
    missing_title = ("__DefinitelyNoSuchGameWindow__",)
    no_fallback = WindowFinder.get_game_window(missing_title)
    assert no_fallback is None, f"Expected None when game not found, got {no_fallback}"

    sw, sh = WindowFinder.get_screen_dimensions()
    with_fallback = WindowFinder.get_game_window(missing_title, allow_screen_fallback=True)
    assert with_fallback == (0, 0, 0, sw, sh), f"Unexpected fallback rect: {with_fallback}"

    assert WindowFinder.is_window_foreground(0) is False

    h_before = _get_process_handle_count()
    for _ in range(50):
        res = WindowFinder.get_game_window(missing_title)
        assert res is None
    h_after = _get_process_handle_count()
    delta = h_after - h_before
    assert delta <= 2, f"Handle leak detected in WindowFinder.get_game_window: delta={delta} ({h_before} -> {h_after})"
    print(f"  [OK] WindowFinder strict lookup + handle leak check passed (handles {h_before} -> {h_after}, delta={delta})")


def verify_foreground_gating() -> None:
    class _MockServer:
        is_running = True

    class _MockCapturer:
        def __init__(self) -> None:
            self.calls = 0

        def capture_region(self, left: int, top: int, width: int, height: int):
            self.calls += 1
            return None

    class _MockStateMachine:
        STATE_CLOSED = "CLOSED"
        STATE_SETTLING = "SETTLING"
        STATE_SYNCED_IDLE = "SYNCED_IDLE"
        STATE_PANNING = "PANNING"

        def __init__(self) -> None:
            self.state = self.STATE_SYNCED_IDLE

        def reset(self) -> None:
            raise AssertionError("state_machine.reset() must NOT run when game window merely loses focus")

    orig_get_game_window = app_main.WindowFinder.get_game_window
    orig_is_fg = app_main.WindowFinder.is_window_foreground
    orig_sleep = app_main.asyncio.sleep
    orig_auto_sync = app_main.config.AUTO_SYNC_MAP_M

    async def _run() -> None:
        stop_ev = asyncio.Event()
        srv = _MockServer()
        cap = _MockCapturer()
        sm = _MockStateMachine()

        async def _stop_on_sleep(_sec: float) -> None:
            stop_ev.set()

        app_main.config.AUTO_SYNC_MAP_M = True
        app_main.WindowFinder.get_game_window = staticmethod(lambda _titles: (12345, 0, 0, 1920, 1080))
        app_main.WindowFinder.is_window_foreground = staticmethod(lambda _hwnd: False)
        app_main.asyncio.sleep = _stop_on_sleep
        try:
            await app_main.tracking_loop(srv, cap, object(), sm, stop_ev)
        finally:
            app_main.config.AUTO_SYNC_MAP_M = orig_auto_sync
            app_main.WindowFinder.get_game_window = orig_get_game_window
            app_main.WindowFinder.is_window_foreground = orig_is_fg
            app_main.asyncio.sleep = orig_sleep

        assert cap.calls == 0, f"Expected 0 captures when unfocused in SYNCED_IDLE, got {cap.calls}"
        assert sm.state == sm.STATE_SYNCED_IDLE, f"Expected STATE_SYNCED_IDLE preserved, got {sm.state}"

    asyncio.run(_run())
    print("  [OK] tracking_loop foreground-gating check passed (0 captures when unfocused, FSM preserved)")


def main() -> None:
    print("[Capture Verification] Running flicker-free GDI capture & handle checks...")
    verify_rop_constant()
    verify_capture_behavior()
    verify_window_finder_and_handle_leak()
    verify_foreground_gating()
    print("ALL CAPTURE VERIFICATION CHECKS PASSED!")


if __name__ == "__main__":
    main()
