"""
Verification suite for Track 1B (Genshin-Helper v5):
- Multi-Slot DIB Pool reuse (0 reallocations when alternating 3 sizes + LRU eviction + BGRA)
- Unicode-safe image_io (tiếng_việt/ảnh_mẫu.png read/write, atomic replacement)
- Atomic JSON write (UTF-8, retry on PermissionError, AppConfig v5 round-trip)
- Unity CanvasScaler UI layout normalization (16:9, 1440p, 21:9, 16:10, 4:3, 32:9, Gamepad)
- Windows UIPI elevation checks (64-bit ctypes prototypes + mismatch detection)
- HoYoPlay registry discovery & Unity borderless launch argument builder
- Auto-HDR SDR clipping detector
- SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE helper
- DiagnosticManager UID Gaussian blur privacy redaction & Unicode-safe writes
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import cv2
import numpy as np
import win32gui

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.capture import ScreenCapturer, _GdiSession
from src.config import AppConfig
from src.diagnostics import DiagnosticManager
from src.game_settings import (
    MINIMAP_RECT_DESKTOP_1080P,
    MINIMAP_RECT_GAMEPAD_1080P,
    PAIMON_RECT_GAMEPAD_1080P,
    WDA_EXCLUDEFROMCAPTURE,
    WDA_NONE,
    _user32 as gs_user32,
    build_borderless_launch_args,
    check_uipi_status,
    compute_ui_layout,
    detect_hdr_clipping,
    find_genshin_executable,
    is_hwnd_elevated,
    is_process_elevated,
    launch_genshin_borderless,
    refresh_exclude_from_capture,
)
from src.image_io import read_image, write_image, write_json_atomic
from src.zoom_slider import ZoomSliderDetector


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


def _get_gdi_handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.GetGuiResources.restype = wintypes.DWORD
    # GR_GDIOBJECTS = 0
    return int(user32.GetGuiResources(kernel32.GetCurrentProcess(), 0))


def verify_multi_slot_dib_pool() -> None:
    gdi_before = _get_gdi_handle_count()
    capturer = ScreenCapturer(max_dib_slots=4)
    try:
        # Alternate 3 typical capture sizes: Minimap ROI, Close-X Probe ROI, Fullscreen-like
        sizes = [(218, 218), (96, 96), (640, 360)]
        for i in range(15):
            for w, h in sizes:
                if i % 2 == 0:
                    frame = capturer.capture_region(0, 0, w, h)
                    assert frame is not None and frame.shape == (h, w, 3) and frame.dtype == np.uint8
                else:
                    frame_bgra = capturer.capture_region_bgra(0, 0, w, h)
                    assert frame_bgra is not None and frame_bgra.shape == (h, w, 4) and frame_bgra.dtype == np.uint8

        session = capturer._get_session()
        assert session.dib_alloc_count == 3, (
            f"Expected exactly 3 DIB allocations when alternating 3 sizes, got {session.dib_alloc_count}"
        )
        assert len(session._slots) == 3

        # Verify BGRA buffer independence
        b1 = capturer.capture_region_bgra(0, 0, 218, 218)
        assert b1 is not None
        sentinel = np.array([11, 22, 33, 44], dtype=np.uint8)
        b1[:] = sentinel
        b2 = capturer.capture_region_bgra(0, 0, 218, 218)
        assert b2 is not None
        assert not np.shares_memory(b1, b2), "BGRA captures share underlying DIB memory!"
        assert not np.all(b2 == sentinel), "Mutating b1 corrupted b2!"
        assert session.dib_alloc_count == 3, "BGRA reuse unexpectedly reallocated DIB!"

        # Fill 4th slot -> 4 total allocs, still 0 evictions
        f4 = capturer.capture_region(0, 0, 320, 180)
        assert f4 is not None and f4.shape == (180, 320, 3)
        assert session.dib_alloc_count == 4 and len(session._slots) == 4

        # 5th size -> evicts LRU slot (96, 96), keeping pool bounded at max_slots=4
        f5 = capturer.capture_region(0, 0, 160, 120)
        assert f5 is not None and f5.shape == (120, 160, 3)
        assert session.dib_alloc_count == 5 and len(session._slots) == 4
        assert (96, 96) not in session._slots, "Expected LRU slot (96, 96) to be evicted"
        assert (218, 218) in session._slots, "Recently used (218, 218) should remain cached"

        # Invalid dimensions return None for BGRA as well
        assert capturer.capture_region_bgra(0, 0, 0, 50) is None
        assert capturer.capture_region_bgra(0, 0, 50, -1) is None
    finally:
        capturer.close()

    # Also verify max_slots=1 edge case (evicting the currently selected DIB) + steady-state kernel handles
    h_before = _get_process_handle_count()
    for _ in range(3):
        single_sess = _GdiSession(max_slots=1)
        try:
            a = single_sess.grab_bgr(0, 0, 64, 64)
            b = single_sess.grab_bgra(0, 0, 80, 80)
            assert a.shape == (64, 64, 3) and b.shape == (80, 80, 4)
            assert single_sess.dib_alloc_count == 2 and len(single_sess._slots) == 1
        finally:
            single_sess.close()
    h_after = _get_process_handle_count()
    gdi_after = _get_gdi_handle_count()

    gdi_delta = gdi_after - gdi_before
    kernel_delta = h_after - h_before
    assert gdi_delta == 0, f"GDI handle leak detected after multi-slot close: {gdi_before} -> {gdi_after}"
    assert kernel_delta <= 2, f"Kernel handle leak detected: {h_before} -> {h_after} (delta={kernel_delta})"
    print(
        "  [OK] Multi-Slot DIB Pool reuse (0 reallocs on 3 sizes), LRU eviction, BGRA, "
        f"gdi_delta={gdi_delta}, kernel_delta={kernel_delta}"
    )


def verify_unicode_image_io() -> None:
    rng = np.random.default_rng(42)
    img = rng.integers(0, 256, size=(96, 128, 3), dtype=np.uint8)

    with tempfile.TemporaryDirectory() as tmpdir:
        unicode_path = Path(tmpdir) / "tiếng_việt" / "ảnh_mẫu.png"
        ok = write_image(unicode_path, img)
        assert ok is True, "write_image failed on Unicode Vietnamese path"
        assert unicode_path.is_file(), "Target Unicode PNG file does not exist"

        # Ensure no leftover temporary files
        leftovers = [p.name for p in unicode_path.parent.iterdir() if p.suffix == ".tmp"]
        assert not leftovers, f"Leftover temp files found: {leftovers}"

        loaded = read_image(unicode_path)
        assert loaded is not None, "read_image returned None on Unicode Vietnamese path"
        assert np.array_equal(loaded, img), "Round-tripped PNG image pixels do not match!"

        # Explicit ext override on extensionless path
        no_ext_path = Path(tmpdir) / "tiếng_việt" / "không_đuôi"
        assert write_image(no_ext_path, img, ext=".png") is True
        loaded2 = read_image(no_ext_path)
        assert loaded2 is not None and np.array_equal(loaded2, img)

        # Missing & corrupt files return None cleanly
        assert read_image(Path(tmpdir) / "tiếng_việt" / "không_tồn_tại.png") is None
        corrupt = Path(tmpdir) / "tiếng_việt" / "hỏng.png"
        corrupt.write_bytes(b"not-a-valid-png")
        assert read_image(corrupt) is None

    print("  [OK] Unicode-safe image_io (tiếng_việt/ảnh_mẫu.png) passed")


def verify_atomic_json_and_config_v5() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "cấu_hình" / "thiết_lập.json"
        payload = {"ngôn_ngữ": "vi", "tọa_độ": [123.45, -678.9], "bật": True}

        # Simulate 2 transient Windows Defender/OneDrive PermissionErrors before succeeding
        real_replace = os.replace
        attempts = {"n": 0}

        def flaky_replace(src, dst):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise PermissionError(13, "Simulated transient Defender lock")
            return real_replace(src, dst)

        with patch("src.image_io.os.replace", side_effect=flaky_replace):
            ok = write_json_atomic(json_path, payload, retries=5, retry_delay_s=0.005)
        assert ok is True
        assert attempts["n"] == 3, f"Expected 3 replace attempts, got {attempts['n']}"

        with open(json_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        assert parsed == payload
        leftovers = [p.name for p in json_path.parent.iterdir() if p.suffix == ".tmp"]
        assert not leftovers, f"Leftover temp JSON files: {leftovers}"

        # AppConfig v5 defaults & backwards-compatible legacy load + atomic round-trip
        legacy_cfg_path = os.path.join(tmpdir, "legacy_v4.json")
        with open(legacy_cfg_path, "w", encoding="utf-8") as f:
            json.dump({"hotkey": "alt+f11", "ws_port": 8765, "single_monitor_mode": True}, f)

        cfg = AppConfig()
        cfg.load_from_file(legacy_cfg_path)
        assert cfg.SINGLE_MONITOR_MODE is True
        assert cfg.LANGUAGE == "auto"
        assert cfg.MINIMAP_TRACKING is True
        assert cfg.NAV_HUD_ENABLED is True
        assert cfg.NAV_HUD_POSITION == "under_minimap"
        assert cfg.LAN_COMPANION_ENABLED is False
        assert cfg.LAN_COMPANION_PORT == 8766

        cfg.LANGUAGE = "en"
        cfg.MINIMAP_TRACKING = False
        cfg.NAV_HUD_ENABLED = False
        cfg.NAV_HUD_POSITION = "top_right"
        cfg.LAN_COMPANION_ENABLED = True
        cfg.LAN_COMPANION_PORT = 9876
        cfg.save_to_file(legacy_cfg_path)

        reloaded = AppConfig()
        reloaded.load_from_file(legacy_cfg_path)
        assert reloaded.LANGUAGE == "en"
        assert reloaded.MINIMAP_TRACKING is False
        assert reloaded.NAV_HUD_ENABLED is False
        assert reloaded.NAV_HUD_POSITION == "top_right"
        assert reloaded.LAN_COMPANION_ENABLED is True
        assert reloaded.LAN_COMPANION_PORT == 9876

    print("  [OK] Atomic JSON write (with PermissionError retry) & AppConfig v5 round-trip passed")


def verify_canvas_scaler_layouts() -> None:
    # 1. 1920x1080 Desktop (16:9)
    l_1080 = compute_ui_layout(1920, 1080, gamepad_mode=False)
    assert l_1080.canvas_size == (1920, 1080)
    assert abs(l_1080.ui_scale - 1.0) < 1e-6
    assert l_1080.aspect_label == "16:9"
    assert l_1080.minimap_rect == MINIMAP_RECT_DESKTOP_1080P == (59, 15, 218, 218)
    assert l_1080["minimap_rect"] == l_1080.minimap_rect  # dict-style access check

    # 2. 1920x1080 Gamepad safe-area
    l_1080_gp = compute_ui_layout(1920, 1080, gamepad_mode=True)
    assert l_1080_gp.gamepad_mode is True
    assert l_1080_gp.minimap_rect == MINIMAP_RECT_GAMEPAD_1080P == (117, 45, 182, 182)
    assert l_1080_gp.paimon_rect == PAIMON_RECT_GAMEPAD_1080P == (89, 42, 57, 64)

    # 3. 2560x1440 Desktop (16:9)
    l_1440 = compute_ui_layout(2560, 1440, gamepad_mode=False)
    assert l_1440.canvas_size == (1920, 1080)
    assert abs(l_1440.ui_scale - (4.0 / 3.0)) < 1e-6
    assert l_1440.aspect_label == "16:9"
    assert l_1440.minimap_rect == (79, 20, 291, 291)

    # 4. 3440x1440 Ultrawide (21:9): height anchored at 1080, width expands to 2580, minimap stays un-stretched!
    l_21_9 = compute_ui_layout(3440, 1440, gamepad_mode=False)
    assert l_21_9.canvas_size == (2580, 1080)
    assert abs(l_21_9.ui_scale - (4.0 / 3.0)) < 1e-6
    assert l_21_9.aspect_label == "21:9"
    assert l_21_9.minimap_rect == (79, 20, 291, 291)

    # 5. 1920x1200 (16:10): width anchored at 1920, height expands to 1200
    l_16_10 = compute_ui_layout(1920, 1200, gamepad_mode=False)
    assert l_16_10.canvas_size == (1920, 1200)
    assert abs(l_16_10.ui_scale - 1.0) < 1e-6
    assert l_16_10.aspect_label == "16:10"
    assert l_16_10.minimap_rect == (59, 15, 218, 218)

    # 6. 1600x1200 (4:3)
    l_4_3 = compute_ui_layout(1600, 1200, gamepad_mode=False)
    assert l_4_3.canvas_size == (1920, 1440)
    assert abs(l_4_3.ui_scale - (1600.0 / 1920.0)) < 1e-6
    assert l_4_3.aspect_label == "4:3"
    assert l_4_3.minimap_rect == (49, 12, 182, 182)

    # 7. 5120x1440 (32:9 Super-Ultrawide)
    l_32_9 = compute_ui_layout(5120, 1440, gamepad_mode=False)
    assert l_32_9.canvas_size == (3840, 1080)
    assert abs(l_32_9.ui_scale - (4.0 / 3.0)) < 1e-6
    assert l_32_9.aspect_label == "32:9"
    assert l_32_9.minimap_rect == (79, 20, 291, 291)

    print("  [OK] Unity CanvasScaler layouts (1080p, 1440p, 21:9, 16:10, 4:3, 32:9, Gamepad) passed")


def verify_uipi_and_display_affinity() -> None:
    elev = is_process_elevated()
    assert isinstance(elev, bool)
    assert is_hwnd_elevated(0) is None

    st_none = check_uipi_status(None)
    assert st_none["self_elevated"] is elev
    assert st_none["game_elevated"] is None
    assert st_none["uipi_blocked"] is False

    # Simulated UIPI mismatch: helper=standard, game=admin
    with patch("src.game_settings.is_process_elevated", return_value=False), patch(
        "src.game_settings.is_hwnd_elevated", return_value=True
    ):
        st_blocked = check_uipi_status(12345)
        assert st_blocked["uipi_blocked"] is True
        assert st_blocked["needs_elevation"] is True

    # Simulated matching elevation: helper=standard, game=standard
    with patch("src.game_settings.is_process_elevated", return_value=False), patch(
        "src.game_settings.is_hwnd_elevated", return_value=False
    ):
        st_ok = check_uipi_status(12345)
        assert st_ok["uipi_blocked"] is False
        assert st_ok["reason"] == "OK"

    # Real Win32 SetWindowDisplayAffinity check on a temporary owned hidden window
    assert refresh_exclude_from_capture(0, True) is False
    hwnd = win32gui.CreateWindow("STATIC", "GenshinHelperV5TestWDA", 0, 0, 0, 10, 10, 0, 0, 0, None)
    assert hwnd
    try:
        assert refresh_exclude_from_capture(hwnd, True) is True
        aff = wintypes.DWORD(0)
        assert gs_user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(aff))
        assert aff.value == WDA_EXCLUDEFROMCAPTURE, f"Expected WDA_EXCLUDEFROMCAPTURE, got {hex(aff.value)}"

        assert refresh_exclude_from_capture(hwnd, False) is True
        assert gs_user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(aff))
        assert aff.value == WDA_NONE, f"Expected WDA_NONE, got {hex(aff.value)}"
    finally:
        win32gui.DestroyWindow(hwnd)

    print(f"  [OK] UIPI elevation check (self_elevated={elev}) & real SetWindowDisplayAffinity passed")


def verify_borderless_and_registry() -> None:
    args = build_borderless_launch_args(2560, 1440)
    assert args == [
        "-popupwindow",
        "-screen-fullscreen",
        "0",
        "-screen-width",
        "2560",
        "-screen-height",
        "1440",
    ]
    args_with_exe = build_borderless_launch_args(1920, 1080, exe_path=r"D:\Genshin\GenshinImpact.exe")
    assert args_with_exe[0] == r"D:\Genshin\GenshinImpact.exe"
    assert args_with_exe[1:4] == ["-popupwindow", "-screen-fullscreen", "0"]

    with tempfile.TemporaryDirectory() as tmpdir:
        fake_global = Path(tmpdir) / "Genshin Impact game"
        fake_global.mkdir(parents=True)
        fake_exe = fake_global / "GenshinImpact.exe"
        fake_exe.write_bytes(b"MZ")

        class _FakeRegKey:
            def __init__(self, subkey: str) -> None:
                self.subkey = subkey

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def fake_open_key(_root, subkey):
            if subkey.endswith("hk4e_global"):
                return _FakeRegKey(subkey)
            raise FileNotFoundError(subkey)

        def fake_query_val(key, value_name):
            assert value_name == "GameInstallPath"
            return (str(fake_global), 1)

        with patch("src.game_settings.winreg.OpenKey", side_effect=fake_open_key), patch(
            "src.game_settings.winreg.QueryValueEx", side_effect=fake_query_val
        ):
            found = find_genshin_executable()
            assert found == str(fake_exe), f"Expected {fake_exe}, got {found}"

            dry = launch_genshin_borderless(1920, 1080, dry_run=True)
            assert dry["ok"] is True and dry["dry_run"] is True
            assert dry["cmd"] == [
                str(fake_exe),
                "-popupwindow",
                "-screen-fullscreen",
                "0",
                "-screen-width",
                "1920",
                "-screen-height",
                "1080",
            ]

    print("  [OK] HoYoPlay registry discovery & borderless launch args builder passed")


def verify_hdr_clipping_detector() -> None:
    # Normal mid-tone scene
    normal = np.full((360, 640, 3), 110, dtype=np.uint8)
    res_norm = detect_hdr_clipping(normal)
    assert res_norm["clipped"] is False
    assert abs(res_norm["p95"] - 110.0) <= 1.0
    assert res_norm["white_ratio"] == 0.0

    # Bright snowy scene with 10% highlights (must NOT false trigger)
    snow = np.full((360, 640, 3), 215, dtype=np.uint8)
    snow[:36, :, :] = 255  # 10% clipped specular highlights
    res_snow = detect_hdr_clipping(snow)
    assert res_snow["clipped"] is False
    assert 0.09 <= res_snow["white_ratio"] <= 0.11

    # Washed-out Auto-HDR clipped frame (60% pegged at 254..255)
    hdr = np.full((360, 640, 3), 254, dtype=np.uint8)
    hdr[216:, :, :] = 140  # bottom 40% mid-tone, top 60% blown out
    res_hdr = detect_hdr_clipping(hdr)
    assert res_hdr["clipped"] is True
    assert res_hdr["p95"] >= 250.0
    assert 0.59 <= res_hdr["white_ratio"] <= 0.61

    # Empty / None safety
    assert detect_hdr_clipping(None) == {"clipped": False, "p95": 0.0, "white_ratio": 0.0}
    print("  [OK] Auto-HDR clipping detector passed")


def verify_uid_privacy_blur_and_diagnostics() -> None:
    h, w = 1080, 1920
    frame = np.full((h, w, 3), 35, dtype=np.uint8)
    # Draw sharp landmarks outside UID area
    cv2.rectangle(frame, (100, 100), (400, 300), (220, 180, 40), 3)
    # Draw crisp white UID watermark inside bottom-right UID zone (x: 84%-100%, y: 95.5%-100%)
    cv2.putText(
        frame,
        "U" + "ID: 801234567",
        (int(w * 0.865), int(h * 0.988)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    frame_orig = frame.copy()
    redacted = DiagnosticManager.redact_uid_region(frame)
    # Must not mutate caller input
    assert np.array_equal(frame, frame_orig), "redact_uid_region mutated input frame in-place!"

    x1 = int(w * 0.84)
    y1 = int(h * 0.955)
    # Regions outside bottom-right UID box must remain bit-exact
    assert np.array_equal(redacted[:y1, :], frame[:y1, :]), "Pixels above UID band were altered!"
    assert np.array_equal(redacted[y1:, :x1], frame[y1:, :x1]), "Pixels left of UID box were altered!"

    # Inside UID box, Laplacian sharpness energy must drop massively (>85%)
    orig_uid_gray = cv2.cvtColor(frame[y1:, x1:], cv2.COLOR_BGR2GRAY)
    blur_uid_gray = cv2.cvtColor(redacted[y1:, x1:], cv2.COLOR_BGR2GRAY)
    orig_sharp = float(cv2.Laplacian(orig_uid_gray, cv2.CV_64F).var())
    blur_sharp = float(cv2.Laplacian(blur_uid_gray, cv2.CV_64F).var())
    assert orig_sharp > 200.0, f"Synthetic UID text unexpectedly low contrast: {orig_sharp}"
    assert blur_sharp < orig_sharp * 0.10, (
        f"UID blur insufficient: orig_laplacian_var={orig_sharp:.1f}, blur_laplacian_var={blur_sharp:.1f}"
    )

    # End-to-end DiagnosticManager.capture_and_create_bundle into a Unicode folder
    with tempfile.TemporaryDirectory() as tmpdir:
        unicode_diag_dir = os.path.join(tmpdir, "chẩn_đoán_v5")
        os.makedirs(unicode_diag_dir, exist_ok=True)
        seen_locator_frames = []

        class _FakeCapturer:
            def capture_region(self, *_args):
                return frame.copy()

        class _FakeLocator:
            def locate_player(self, f_in, debug=False):
                seen_locator_frames.append(f_in.copy())
                return {"detected": False, "has_world_coords": False, "reason": "Synthetic test"}

        with patch.object(DiagnosticManager, "_ensure_scratch_dir", return_value=unicode_diag_dir), patch(
            "src.diagnostics.WindowFinder.get_browser_window", return_value=(1, 0, 0, 1920, 1080)
        ):
            bundle = DiagnosticManager.capture_and_create_bundle(
                capturer=_FakeCapturer(),
                locator=_FakeLocator(),
                game_rect=(0, 0, w, h),
                redact_uid=True,
            )

        assert bundle["success"] is True
        bundle_dir = Path(bundle["bundle_dir"])
        # Locator must receive unredacted frame
        assert len(seen_locator_frames) == 1 and np.array_equal(seen_locator_frames[0], frame)

        # Saved 01_game.png and 04_cv_annotated.png must BOTH have UID blurred
        for img_name in ("01_game.png", "04_cv_annotated.png"):
            saved = read_image(bundle_dir / img_name)
            assert saved is not None, f"Failed reading {img_name} from Unicode bundle path"
            saved_uid_gray = cv2.cvtColor(saved[y1:, x1:], cv2.COLOR_BGR2GRAY)
            saved_sharp = float(cv2.Laplacian(saved_uid_gray, cv2.CV_64F).var())
            assert saved_sharp < orig_sharp * 0.10, (
                f"{img_name} UID was not redacted! laplacian_var={saved_sharp:.1f} vs orig={orig_sharp:.1f}"
            )

        with open(bundle_dir / "03_report.json", "r", encoding="utf-8") as rf:
            rep = json.load(rf)
        assert rep["uid_redacted"] is True

    # Regression check: ZoomSliderDetector still loads template via image_io and rejects flat bright panel
    flat_white = np.full((1440, 2560, 3), 235, dtype=np.uint8)
    slider_flat = ZoomSliderDetector.detect_slider(flat_white)
    assert slider_flat is None or float(slider_flat.get("ccoeff", 0.0)) < 0.65, (
        f"ZoomSliderDetector false-triggered on flat bright panel: {slider_flat}"
    )

    print(
        f"  [OK] UID privacy blur (Laplacian var {orig_sharp:.1f} -> {blur_sharp:.2f}) & "
        "Unicode diagnostics bundle passed"
    )


def main() -> None:
    print("[Track 1B Verification] Running v5 performance, Unicode I/O, atomic JSON & game settings checks...")
    verify_multi_slot_dib_pool()
    verify_unicode_image_io()
    verify_atomic_json_and_config_v5()
    verify_canvas_scaler_layouts()
    verify_uipi_and_display_affinity()
    verify_borderless_and_registry()
    verify_hdr_clipping_detector()
    verify_uid_privacy_blur_and_diagnostics()
    print("ALL TRACK 1B VERIFICATION CHECKS PASSED!")


if __name__ == "__main__":
    main()
