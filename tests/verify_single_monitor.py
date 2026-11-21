"""
Verification checks for Genshin Helper v4.4.0 Single-Monitor Mode (Package 1-A):
- AppConfig round-trip for hotkey_switch & single_monitor_mode (default False)
- UnifiedHotkeyManager.set_enabled() dynamic hook lifecycle
- HudToastOverlay layered style, lazy thread, premultiplied BGRA, Vietnamese text, and ROI-safe geometry
- AudioFeedback non-blocking worker with mocked winsound.Beep
- WindowFinder monitor & window helpers
- DualSyncServer v4.4.0 endpoints (/status, /set_single_monitor, /set_hotkeys switch, /action_feedback dedup, /quick_switch)
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.capture import WindowFinder
from src.config import AppConfig
from src.hotkey import UnifiedHotkeyManager
from src.server import DualSyncServer
from src.single_monitor import AudioFeedback, HudToastOverlay, WindowSwitcher


def verify_config_roundtrip() -> None:
    cfg = AppConfig()
    assert cfg.SINGLE_MONITOR_MODE is False, f"Expected default SINGLE_MONITOR_MODE=False, got {cfg.SINGLE_MONITOR_MODE}"
    assert cfg.HOTKEY_SWITCH == "alt+q", f"Expected default HOTKEY_SWITCH='alt+q', got {cfg.HOTKEY_SWITCH}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_cfg = os.path.join(tmpdir, "config.json")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            json.dump({
                "hotkey_sync": "alt+f11",
                "hotkey_debug": "alt+f12",
                "hotkey_switch": "Mouse5",
                "single_monitor_mode": True,
                "auto_sync_m": False,
            }, f)

        loaded = AppConfig()
        loaded.load_from_file(tmp_cfg)
        assert loaded.HOTKEY_SWITCH == "mouse5", f"Unexpected hotkey_switch: {loaded.HOTKEY_SWITCH}"
        assert loaded.SINGLE_MONITOR_MODE is True, "Expected SINGLE_MONITOR_MODE=True"
        assert loaded.AUTO_SYNC_MAP_M is False

        loaded.SINGLE_MONITOR_MODE = False
        loaded.HOTKEY_SWITCH = "alt+m"
        loaded.save_to_file(tmp_cfg)

        reloaded = AppConfig()
        reloaded.load_from_file(tmp_cfg)
        assert reloaded.SINGLE_MONITOR_MODE is False
        assert reloaded.HOTKEY_SWITCH == "alt+m"

    print("  [OK] AppConfig single_monitor_mode & hotkey_switch round-trip passed")


def verify_hotkey_enable_disable() -> None:
    mgr = UnifiedHotkeyManager()
    fired = []
    mgr.register("sync", "f9", lambda: fired.append("sync"), enabled=True)
    mgr.register("switch", "mouse5", lambda: fired.append("switch"), enabled=False)

    assert mgr.start() is True
    try:
        # Disabled mouse binding must NOT start the global low-level mouse hook
        assert mgr._mouse_hook is None, "Disabled mouse binding must not install WH_MOUSE_LL hook"

        assert mgr.set_enabled("switch", True) is True
        assert mgr._mouse_hook is not None, "Enabling mouse binding must install WH_MOUSE_LL hook"

        assert mgr.set_enabled("switch", False) is True
        assert mgr._mouse_hook is None, "Disabling last mouse binding must remove WH_MOUSE_LL hook"
    finally:
        mgr.stop()

    assert mgr._mouse_hook is None
    print("  [OK] UnifiedHotkeyManager.set_enabled() dynamic hook lifecycle passed")


def verify_hud_toast_and_audio() -> None:
    expected_ex = (
        HudToastOverlay.WS_EX_LAYERED
        | HudToastOverlay.WS_EX_TRANSPARENT
        | HudToastOverlay.WS_EX_TOPMOST
        | HudToastOverlay.WS_EX_NOACTIVATE
        | HudToastOverlay.WS_EX_TOOLWINDOW
    )
    assert HudToastOverlay.EX_STYLE == expected_ex

    hud = HudToastOverlay()
    assert hud._thread is None, "HudToastOverlay must not spawn a window thread until show_toast() is called"

    for kind in ("success", "info", "warning", "error"):
        img = HudToastOverlay.render_toast_image(
            title="Đã ghim: Rương Hiếm (9m)",
            subtitle="Khu vực: Đồng bằng Phong Khởi • Tầng B1",
            kind=kind,
            scale=1.0,
        )
        assert img.mode == "RGBA"
        assert 200 <= img.width <= 560 and 38 <= img.height <= 96, f"Unexpected toast size {img.size}"
        arr = np.asarray(img)
        assert int(arr[:, :, 3].max()) >= 220, "Toast image alpha channel unexpectedly empty"
        assert float(arr[:, :, :3].std()) > 5.0, "Toast RGB has unexpectedly low contrast"

        raw = HudToastOverlay.rgba_to_premultiplied_bgra(img)
        assert len(raw) == img.width * img.height * 4
        bgra = np.frombuffer(raw, dtype=np.uint8).reshape((img.height, img.width, 4))
        assert np.all(bgra[:, :, 0] <= bgra[:, :, 3]), "Premultiplied B > A"
        assert np.all(bgra[:, :, 1] <= bgra[:, :, 3]), "Premultiplied G > A"
        assert np.all(bgra[:, :, 2] <= bgra[:, :, 3]), "Premultiplied R > A"

        for rect in ((0, 0, 1920, 1080), (120, 80, 2560, 1440)):
            rx, ry, rw, rh = rect
            px, py = HudToastOverlay.compute_toast_position(img.width, img.height, anchor_rect=rect)
            assert abs((px + img.width / 2.0) - (rx + rw / 2.0)) <= 1.5, "Toast not horizontally centered"
            assert py >= ry + 20, "Toast clipped at top"
            assert (py + img.height) <= (ry + rh * 0.115), (
                f"Toast bottom {py + img.height} intrudes below top 11.5% safe band on {rect}"
            )
            # Must stay strictly clear of left zoom slider (<6.2% w) and right close/elevator (>90% w)
            assert px >= rx + rw * 0.15 and (px + img.width) <= rx + rw * 0.85

    # Smoke test real Win32 UpdateLayeredWindow lifecycle briefly
    assert hud.show_toast("Kiểm tra HUD Toast", "Chế độ 1 màn hình", kind="info", duration_ms=650) is True
    time.sleep(0.15)
    assert hud._hwnd is not None
    hud.close()
    assert hud._hwnd is None

    # AudioFeedback lazy start + mocked winsound.Beep
    beeps = []
    audio = AudioFeedback()
    assert audio._thread is None, "AudioFeedback must start worker lazily"
    with patch("src.single_monitor.winsound.Beep", side_effect=lambda f, d: beeps.append((f, d))):
        audio.play("success")
        audio.play("warning")
        audio.play("error")
        deadline = time.monotonic() + 2.0
        while len(beeps) < 6 and time.monotonic() < deadline:
            time.sleep(0.02)
        audio.close()

    assert len(beeps) == 6, f"Expected 6 tone notes across 3 chimes, got {beeps}"
    assert beeps[:2] == [(784, 70), (1047, 110)]
    print("  [OK] HudToastOverlay (Win32 layered + premultiplied BGRA + ROI bounds) & AudioFeedback passed")


def verify_window_helpers() -> None:
    cnt = WindowFinder.get_monitor_count()
    assert isinstance(cnt, int) and cnt >= 1
    assert WindowFinder.find_game_window_any_state(("__DefinitelyMissingGenshinWindow__",)) is None
    assert WindowFinder.focus_window(0) is False
    print(f"  [OK] WindowFinder helpers passed (monitor_count={cnt})")


def _http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=4.0) as resp:
        raw = resp.read()
        cl = resp.headers.get("Content-Length")
        if cl is not None:
            assert int(cl) == len(raw), f"Content-Length mismatch: header={cl}, actual_bytes={len(raw)}"
        return json.loads(raw.decode("utf-8"))


def verify_server_v44_endpoints() -> None:
    server = DualSyncServer(host="127.0.0.1", port=0)
    state = {
        "single_monitor_mode": False,
        "hotkey_switch": "alt+q",
        "display_name_switch": "Alt + Q",
        "feedbacks": [],
        "switches": 0,
    }

    def get_info():
        return {
            "hotkey": "alt+f11",
            "display_name": "Alt + F11",
            "hotkey_sync": "alt+f11",
            "display_name_sync": "Alt + F11",
            "hotkey_debug": "alt+f12",
            "display_name_debug": "Alt + F12",
            "hotkey_switch": state["hotkey_switch"],
            "display_name_switch": state["display_name_switch"],
            "auto_sync_m": True,
            "single_monitor_mode": state["single_monitor_mode"],
            "monitor_count": 1,
        }

    def on_single_monitor_change(enabled: bool) -> bool:
        state["single_monitor_mode"] = bool(enabled)
        return state["single_monitor_mode"]

    def on_hotkey_change(act: str, new_k: str):
        if act == "switch":
            state["hotkey_switch"] = new_k
            state["display_name_switch"] = new_k.upper()
            return True, state["display_name_switch"]
        return True, new_k.upper()

    def on_action_feedback(payload: dict) -> bool:
        if not state["single_monitor_mode"]:
            return False
        state["feedbacks"].append(payload)
        return True

    def on_quick_switch() -> str:
        state["switches"] += 1
        return "browser"

    server.get_hotkey_info = get_info
    server.on_single_monitor_change = on_single_monitor_change
    server.on_hotkey_change = on_hotkey_change
    server.on_action_feedback = on_action_feedback
    server.on_quick_switch = on_quick_switch

    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    ready_ev = threading.Event()
    stop_future_holder: dict[str, asyncio.Future] = {}

    def _run_srv():
        async def _runner():
            loop = asyncio.get_running_loop()
            loop_holder["loop"] = loop
            await server.start()
            stop_future_holder["fut"] = loop.create_future()
            ready_ev.set()
            try:
                await stop_future_holder["fut"]
            finally:
                await server.stop()

        asyncio.run(_runner())

    srv_thread = threading.Thread(target=_run_srv, daemon=True)
    srv_thread.start()
    assert ready_ev.wait(timeout=3.0), "Server failed to start"
    base = f"http://127.0.0.1:{server.port}"

    try:
        assert server.has_active_client() is False
        st = _http_json(f"{base}/status")
        assert st["version"] == "5.3.0"
        assert st["script_version"] == "v5.3.0"
        assert st["single_monitor_mode"] is False
        assert st["hotkey_switch"] == "alt+q"
        assert st["monitor_count"] == 1
        assert server.has_active_client() is True

        # 2-monitor mode: action_feedback must return shown=False and skip callback
        fb_off = _http_json(
            f"{base}/action_feedback",
            method="POST",
            payload={"event_type": "SYNC_AND_MARK", "status": "marked", "title": "Rương Hiếm", "seq": 10},
        )
        assert fb_off == {"ok": True, "shown": False, "deduped": False}
        assert len(state["feedbacks"]) == 0

        # Toggle Single-Monitor Mode ON + verify broadcast
        cur_seq = st["latest_seq"]
        res_sm = _http_json(f"{base}/set_single_monitor", method="POST", payload={"single_monitor_mode": True})
        assert res_sm == {"ok": True, "single_monitor_mode": True}
        ev_sm = _http_json(f"{base}/poll?since={cur_seq}")
        assert ev_sm["type"] == "SINGLE_MONITOR_UPDATED" and ev_sm["single_monitor_mode"] is True

        # Update switch hotkey + verify broadcast
        res_hk = _http_json(f"{base}/set_hotkeys", method="POST", payload={"hotkey_switch": "f8"})
        assert res_hk["ok"] is True and res_hk["results"]["switch"]["ok"] is True
        ev_hk = _http_json(f"{base}/poll?since={ev_sm['_seq']}")
        assert ev_hk["type"] == "HOTKEY_UPDATED" and ev_hk["action"] == "switch"

        # Multi-tab dedup on /action_feedback
        fb_1 = _http_json(
            f"{base}/action_feedback",
            method="POST",
            payload={"event_type": "SYNC_AND_MARK", "status": "marked", "title": "Đã ghim: Rương Hiếm", "detail": "9m", "seq": 55},
        )
        fb_dup = _http_json(
            f"{base}/action_feedback",
            method="POST",
            payload={"event_type": "SYNC_AND_MARK", "status": "already_marked", "title": "Đã ghim từ trước", "seq": 55},
        )
        fb_next = _http_json(
            f"{base}/action_feedback",
            method="POST",
            payload={"event_type": "SYNC_AND_MARK", "status": "no_nearby", "title": "Không có điểm gần (< 60m)", "seq": 56},
        )
        assert fb_1 == {"ok": True, "shown": True, "deduped": False}
        assert fb_dup == {"ok": True, "shown": False, "deduped": True}
        assert fb_next == {"ok": True, "shown": True, "deduped": False}
        assert len(state["feedbacks"]) == 2

        # Quick-switch endpoint
        qs = _http_json(f"{base}/quick_switch", method="POST", payload={})
        assert qs == {"ok": True, "target": "browser"}
        assert state["switches"] == 1
    finally:
        loop = loop_holder.get("loop")
        fut = stop_future_holder.get("fut")
        if loop and fut and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, None)
        srv_thread.join(timeout=3.0)

    print("  [OK] DualSyncServer v4.5.0 endpoints + UTF-8 Content-Length + multi-tab dedup passed")


def main() -> None:
    print("[Single-Monitor Verification] Running Package 1-A checks...")
    verify_config_roundtrip()
    verify_hotkey_enable_disable()
    verify_hud_toast_and_audio()
    verify_window_helpers()
    verify_server_v44_endpoints()
    print("ALL SINGLE-MONITOR VERIFICATION CHECKS PASSED!")


if __name__ == "__main__":
    main()
