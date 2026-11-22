"""Focused verification suite for capture-visible NavMiniHud, smart hotkey conflicts,
WH_KEYBOARD_LL suppression, and isolated NVIDIA Overlay remapping/restore.

Runs cleanly without touching the user's real NVIDIA config/registry or flashing GUI windows.
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import json
import os
import socket
import sys
import tempfile
import threading
import time
import tokenize
from unittest.mock import patch
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pathlib import Path
import numpy as np

from src import asset_setup
from src.capture import WindowFinder
from src.config import AppConfig
from src.hotkey import UnifiedHotkeyManager
from src.hotkey_conflicts import (
    HotkeyConflictAdvisor,
    NvidiaOverlayAdapter,
    format_display_name,
    hotkey_to_vk_sequence,
    normalize_hotkey,
    vk_sequence_to_hotkey,
)
from src.map_state import MapScreenStateMachine
from src.nav_controller import NavPin, NavigationController
from src.nav_hud import NavHudRenderer, NavMiniHud
from src.pin_guide import HoYoLABLabelCatalog, PinGuideFetcher
from src.server import DualSyncServer

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_user32.GetGuiResources.restype = wintypes.DWORD

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def _get_gdi_count() -> int:
    return int(_user32.GetGuiResources(_kernel32.GetCurrentProcess(), 0))


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_json(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="ignore")
        return int(err.code), json.loads(raw) if raw else {}


def verify_config_defaults_and_roundtrip() -> None:
    fresh = AppConfig()
    assert fresh.HOTKEY_NAV_MARK == "alt+k", f"Unexpected default nav_mark: {fresh.HOTKEY_NAV_MARK}"
    assert fresh.HOTKEY_NAV_UNDO == "alt+shift+k", f"Unexpected default nav_undo: {fresh.HOTKEY_NAV_UNDO}"
    assert fresh.HOTKEY_NAV_TOGGLE == "alt+x", f"Unexpected default nav_toggle: {fresh.HOTKEY_NAV_TOGGLE}"
    assert fresh.NAV_HUD_CAPTURE_VISIBLE is False
    assert fresh.NAV_HUD_GUIDE_ZOOM_MODE == "cycle"
    assert fresh.AUDIO_ENABLED is True
    assert fresh.AUDIO_VOLUME == 50
    assert fresh.HOTKEY_SUPPRESS_CONFLICTS is True

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "config.json")
        fresh.NAV_HUD_CAPTURE_VISIBLE = True
        fresh.NAV_HUD_GUIDE_ZOOM_MODE = "large"
        fresh.AUDIO_ENABLED = False
        fresh.AUDIO_VOLUME = 65
        fresh.HOTKEY_SUPPRESS_CONFLICTS = False
        fresh.HOTKEY_NAV_MARK = "alt+j"
        fresh.HOTKEY_NAV_TOGGLE = "alt+shift+x"
        fresh.save_to_file(cfg_path)

        reloaded = AppConfig()
        reloaded.load_from_file(cfg_path)
        assert reloaded.NAV_HUD_CAPTURE_VISIBLE is True
        assert reloaded.NAV_HUD_GUIDE_ZOOM_MODE == "large"
        assert reloaded.AUDIO_ENABLED is False
        assert reloaded.AUDIO_VOLUME == 65
        assert reloaded.HOTKEY_SUPPRESS_CONFLICTS is False
        assert reloaded.HOTKEY_NAV_MARK == "alt+j"
        assert reloaded.HOTKEY_NAV_TOGGLE == "alt+shift+x"
    print("  [OK] AppConfig defaults & roundtrip passed")


def verify_nav_hud_capture_affinity() -> None:
    prev_offset = NavMiniHud.SCREEN_OFFSET
    NavMiniHud.SCREEN_OFFSET = (-20000, -20000)
    hud = None
    gdi_before = _get_gdi_count()
    try:
        hud = NavMiniHud(position_mode="under_minimap", lang="vi", capture_visible=False)
        ctrl = NavigationController([NavPin(id="p1", name="Test Pin", lat=-15.0, lng=0.0, map_id=2)])
        g = ctrl.compute_guidance(0.0, 0.0, 0.0, player_map_id=2)
        assert hud.update(g, window_rect=(0, 0, 1920, 1080), work_area=(0, 0, 1920, 1040)) is True
        time.sleep(0.12)
        assert hud.hwnd is not None

        aff = wintypes.DWORD(999)
        assert _user32.GetWindowDisplayAffinity(hud.hwnd, ctypes.byref(aff))
        assert aff.value == WDA_EXCLUDEFROMCAPTURE, f"Expected WDA_EXCLUDEFROMCAPTURE, got {aff.value:#x}"

        hud.set_capture_visible(True)
        time.sleep(0.08)
        assert hud.capture_visible is True
        assert _user32.GetWindowDisplayAffinity(hud.hwnd, ctypes.byref(aff))
        assert aff.value == WDA_NONE, f"Expected WDA_NONE after enabling capture_visible, got {aff.value:#x}"

        # Real capture overlay in foreground keeps HUD visible when game is visible behind it
        with patch("src.capture.win32gui.IsWindow", return_value=True), \
             patch("src.capture.win32gui.IsIconic", return_value=False), \
             patch("src.capture.win32gui.IsWindowVisible", return_value=True), \
             patch("src.capture.win32gui.GetForegroundWindow", return_value=hud.hwnd):
            assert WindowFinder.is_game_visible_unfocused(
                hud.hwnd,
                (0, 0, 1920, 1080),
                own_hwnds=(hud.hwnd,),
                keep_when_unfocused=True,
            ) is True

        hud.set_capture_visible(False)
        time.sleep(0.08)
        assert hud.capture_visible is False
        assert _user32.GetWindowDisplayAffinity(hud.hwnd, ctypes.byref(aff))
        assert aff.value == WDA_EXCLUDEFROMCAPTURE, f"Expected WDA_EXCLUDEFROMCAPTURE restore, got {aff.value:#x}"

        hud.hide()
        time.sleep(0.05)
    finally:
        if hud is not None:
            hud.close()
        NavMiniHud.SCREEN_OFFSET = prev_offset

    gdi_after = _get_gdi_count()
    deadline = time.time() + 1.0
    while gdi_after > gdi_before and time.time() < deadline:
        time.sleep(0.05)
        gdi_after = _get_gdi_count()
    assert gdi_after - gdi_before <= 0, f"GDI handle leak detected: {gdi_before} -> {gdi_after}"
    print("  [OK] NavMiniHud WDA_NONE / WDA_EXCLUDEFROMCAPTURE toggle & unfocused capture check passed")


def verify_non_covering_constants() -> None:
    for cls_name in ("ScreenClippingHost", "SnippingTool", "MultitaskingViewFrame", "CEF-OSC-WIDGET", "NVOverlayWindow"):
        assert cls_name in WindowFinder._NON_COVERING_CLASSES, f"Missing {cls_name} in _NON_COVERING_CLASSES"
    for proc_name in ("screenclippinghost.exe", "ms-screenclip.exe", "nvidia overlay.exe"):
        assert proc_name in WindowFinder._NON_COVERING_PROCESSES, f"Missing {proc_name} in _NON_COVERING_PROCESSES"

    game_hwnd = 1001
    other_hwnd = 2002
    with patch("src.capture.win32gui.IsWindow", return_value=True), \
         patch("src.capture.win32gui.IsIconic", return_value=False), \
         patch("src.capture.win32gui.IsWindowVisible", return_value=True), \
         patch("src.capture.win32gui.GetForegroundWindow", return_value=other_hwnd):
        # 1. Browser covering the game on the SAME monitor must hide HUD even with keep_when_unfocused=True
        with patch("src.capture.win32gui.GetClassName", return_value="Chrome_WidgetWin_1"), \
             patch.object(WindowFinder, "_get_hwnd_process_basename", return_value="brave.exe"), \
             patch("src.capture.win32gui.GetWindowRect", return_value=(0, 0, 1920, 1080)):
            assert WindowFinder.is_game_visible_unfocused(
                game_hwnd, (0, 0, 1920, 1080), keep_when_unfocused=True
            ) is False, "Covering browser on same monitor must hide capture-visible HUD"

        # 2. Browser on a SECOND monitor keeps HUD visible
        with patch("src.capture.win32gui.GetClassName", return_value="Chrome_WidgetWin_1"), \
             patch.object(WindowFinder, "_get_hwnd_process_basename", return_value="brave.exe"), \
             patch("src.capture.win32gui.GetWindowRect", return_value=(1920, 0, 3840, 1080)):
            assert WindowFinder.is_game_visible_unfocused(
                game_hwnd, (0, 0, 1920, 1080), keep_when_unfocused=True
            ) is True, "2nd monitor browser should keep HUD visible"

        # 3. Screenshot snipping overlay covering game keeps HUD visible
        with patch("src.capture.win32gui.GetClassName", return_value="ScreenClippingHost"), \
             patch.object(WindowFinder, "_get_hwnd_process_basename", return_value="screenclippinghost.exe"), \
             patch("src.capture.win32gui.GetWindowRect", return_value=(0, 0, 1920, 1080)):
            assert WindowFinder.is_game_visible_unfocused(
                game_hwnd, (0, 0, 1920, 1080), keep_when_unfocused=True
            ) is True, "Capture overlay should keep HUD visible"

    print("  [OK] Capture/GPU overlay allowlist & same-monitor occlusion gate passed")


def verify_conflict_advisor_and_nvidia_adapter() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        share_path = os.path.join(tmpdir, "ShareSettings.json")
        backup_path = os.path.join(tmpdir, "hotkey_backups.json")
        with open(share_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "settings": {
                        "shortcuts": {
                            "OpenIGO": [18, 90],       # Alt + Z
                            "RecordToggle": [18, 120], # Alt + F9
                        }
                    }
                },
                fh,
                indent=2,
            )

        nv = NvidiaOverlayAdapter(
            share_settings_path=share_path,
            backup_path=backup_path,
            write_registry=False,
        )
        live_taken = {"ctrl+shift+f1"}
        adv = HotkeyConflictAdvisor(
            nvidia_adapter=nv,
            probe_fn=lambda hk: normalize_hotkey(hk) in live_taken,
            proc_fn=lambda: {"nvidia overlay.exe"},
        )

        # Roundtrip helpers sanity check
        vks = hotkey_to_vk_sequence("alt+shift+p")
        assert vks == [18, 16, 80] and vk_sequence_to_hotkey(vks) == "alt+shift+p"
        assert format_display_name("alt+k") == "Alt + K"

        bindings = {
            "sync": "alt+f11",
            "debug": "alt+f12",
            "switch": "alt+q",
            "nav_mark": "alt+k",
            "nav_undo": "alt+shift+k",
            "nav_guide": "alt+h",
            "nav_next": "alt+n",
            "nav_layout": "alt+shift+h",
        }

        # Safe defaults
        assert adv.analyze_hotkey("alt+k", action="nav_mark", current_bindings=bindings)["ok"] is True
        assert adv.analyze_hotkey("alt+shift+k", action="nav_undo", current_bindings=bindings)["ok"] is True

        # Genshin always-triggered menu keys fail under Alt AND Alt+Shift when suppression is off,
        # and switch to info/suppressed=True when WH_KEYBOARD_LL suppression is active.
        for risky in ("alt+g", "alt+shift+g"):
            res_g = adv.analyze_hotkey(risky, action="nav_mark", current_bindings=bindings, suppress_enabled=False)
            assert res_g["ok"] is False
            assert any(c["kind"] == "genshin" and c["severity"] == "warn" for c in res_g["conflicts"])

            res_sup = adv.analyze_hotkey(risky, action="nav_mark", current_bindings=bindings, suppress_enabled=True)
            assert res_sup["ok"] is True
            assert res_sup["suggestions"] == []
            assert any(
                c["kind"] == "genshin" and c["severity"] == "info" and c.get("suppressed") is True
                for c in res_sup["conflicts"]
            )

        # Internal duplicate
        dup_bindings = {**bindings, "nav_undo": "alt+k"}
        res_dup = adv.analyze_hotkey("alt+k", action="nav_mark", current_bindings=dup_bindings)
        assert any(c["kind"] == "internal" and c["severity"] == "error" for c in res_dup["conflicts"])

        # Mocked Windows global hotkey
        res_live = adv.analyze_hotkey("ctrl+shift+f1", action="nav_mark", current_bindings=bindings)
        assert any(c["kind"] == "external_live" for c in res_live["conflicts"])

        # Temp ShareSettings.json Alt+Z detection -> remappable
        res_nv = adv.analyze_hotkey("alt+z", action="nav_mark", current_bindings=bindings)
        nv_conf = [c for c in res_nv["conflicts"] if c["app_id"] == "nvidia_overlay"]
        assert len(nv_conf) == 1
        assert nv_conf[0]["kind"] == "external_config" and nv_conf[0]["can_remap"] is True
        assert nv_conf[0]["shortcut_id"] == "OpenIGO"

        # Collision guards when choosing replacement hotkey for NVIDIA
        assert adv.remap_external_hotkey("nvidia_overlay", "OpenIGO", "alt+k", current_bindings=bindings)["ok"] is False
        assert adv.remap_external_hotkey("nvidia_overlay", "OpenIGO", "alt+f9", current_bindings=bindings)["ok"] is False
        assert adv.remap_external_hotkey("nvidia_overlay", "OpenIGO", "ctrl+shift+f1", current_bindings=bindings)["ok"] is False

        # Valid remap + backup + claim verification
        res_remap = adv.remap_external_hotkey("nvidia_overlay", "OpenIGO", "alt+shift+p", current_bindings=bindings)
        assert res_remap["ok"] is True, res_remap
        with open(share_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        assert saved["settings"]["shortcuts"]["OpenIGO"] == [18, 16, 80]
        backups = nv.list_backups()
        assert len(backups) == 1 and backups[0]["shortcut_id"] == "OpenIGO"
        after_nv = adv.analyze_hotkey("alt+z", action="nav_mark", current_bindings=bindings)
        assert not any(c["app_id"] == "nvidia_overlay" for c in after_nv["conflicts"])

        # Restore 1-click undo
        res_rest = adv.restore_external_hotkey("nvidia_overlay", "OpenIGO")
        assert res_rest["ok"] is True
        with open(share_path, "r", encoding="utf-8") as fh:
            restored_json = json.load(fh)
        assert restored_json["settings"]["shortcuts"]["OpenIGO"] == [18, 90]
        assert nv.list_backups() == []
    print("  [OK] HotkeyConflictAdvisor & isolated NvidiaOverlayAdapter passed")


def verify_ll_keyboard_hook_and_no_sendinput() -> None:
    for rel in ("src/hotkey.py", "src/hotkey_conflicts.py"):
        with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as fh:
            for tok in tokenize.generate_tokens(fh.readline):
                if tok.type != tokenize.COMMENT:
                    assert "SendInput" not in tok.string, (
                        f"Disallowed SendInput token found in {rel}:{tok.start[0]}"
                    )

    suppressed_flags = [False]
    mgr = UnifiedHotkeyManager(
        "alt+k",
        lambda: None,
        suppress_triggers=True,
        should_suppress=lambda _act: suppressed_flags[0],
    )
    try:
        assert mgr.start() is True
        time.sleep(0.06)
        assert mgr._kb_hook is not None, "Expected WH_KEYBOARD_LL hook to be installed"
        mgr.set_suppress_triggers(False)
        assert mgr.bindings["sync"]["suppress"] is False
        mgr.set_suppress_triggers(True)
        mgr.set_should_suppress(lambda _act: True)
        assert mgr.update_binding("sync", "alt+shift+k") is True
        assert mgr._kb_hook is not None
    finally:
        mgr.stop()
    assert mgr._kb_hook is None, "Expected WH_KEYBOARD_LL hook cleared on stop()"
    print("  [OK] WH_KEYBOARD_LL lifecycle, should_suppress gating & zero-SendInput verified")


def verify_server_endpoints_integration() -> None:
    port = _get_free_port()
    srv = DualSyncServer(host="127.0.0.1", port=port)
    state = {
        "capture_visible": False,
        "guide_zoom_mode": "cycle",
        "audio_enabled": True,
        "audio_volume": 50,
        "nav_mark": "alt+k",
        "remap_calls": 0,
        "restore_calls": 0,
    }

    def on_nav_hud_change(enabled=None, position=None, req_data=None):
        data = req_data if isinstance(req_data, dict) else {}
        cap = data.get("capture_visible", data.get("nav_hud_capture_visible"))
        if cap is not None:
            state["capture_visible"] = bool(cap)
        zm = data.get("guide_zoom_mode", data.get("nav_hud_guide_zoom_mode"))
        if isinstance(zm, str) and zm.strip().lower() in ("cycle", "compact", "large"):
            state["guide_zoom_mode"] = zm.strip().lower()
        return {
            "enabled": True if enabled is None else bool(enabled),
            "position": position or "under_minimap",
            "capture_visible": state["capture_visible"],
            "nav_hud_capture_visible": state["capture_visible"],
            "guide_zoom_mode": state["guide_zoom_mode"],
            "nav_hud_guide_zoom_mode": state["guide_zoom_mode"],
        }

    def on_audio_change(req_data):
        data = req_data if isinstance(req_data, dict) else {}
        if "audio_enabled" in data or "enabled" in data:
            state["audio_enabled"] = bool(data.get("audio_enabled", data.get("enabled")))
        if "audio_volume" in data or "volume" in data:
            v = float(data.get("audio_volume", data.get("volume")))
            state["audio_volume"] = max(0, min(100, int(round(v))))
        return {"audio_enabled": state["audio_enabled"], "audio_volume": state["audio_volume"]}

    def on_check_conflicts(req_data):
        return {
            "ok": True,
            "actions": {
                "nav_mark": {
                    "hotkey": req_data.get("bindings", {}).get("nav_mark", state["nav_mark"]),
                    "ok": True,
                    "severity": "ok",
                    "conflicts": [],
                    "suggestions": [],
                }
            },
            "active_backups": [],
        }

    def on_remap(req_data):
        state["remap_calls"] += 1
        claim_act = req_data.get("claim_action") or "nav_mark"
        claim_hk = req_data.get("claim_hotkey") or "alt+z"
        state["nav_mark"] = claim_hk
        return {
            "ok": True,
            "new_hotkey": req_data.get("new_external_hotkey", "alt+shift+p"),
            "new_display": format_display_name(req_data.get("new_external_hotkey", "alt+shift+p")),
            "claim_updated": {
                "action": claim_act,
                "hotkey": claim_hk,
                "display_name": format_display_name(claim_hk),
            },
        }

    def on_restore(req_data):
        state["restore_calls"] += 1
        return {"ok": True, "restored_hotkey": "alt+z", "restored_display": "Alt + Z"}

    srv.on_nav_hud_change = on_nav_hud_change
    srv.on_audio_change = on_audio_change
    srv.on_check_hotkey_conflicts = on_check_conflicts
    srv.on_remap_external_hotkey = on_remap
    srv.on_restore_external_hotkey = on_restore

    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    stop_holder: dict[str, asyncio.Future] = {}
    ready = threading.Event()

    def _run():
        async def _runner():
            loop = asyncio.get_running_loop()
            loop_holder["loop"] = loop
            await srv.start()
            stop_holder["fut"] = loop.create_future()
            ready.set()
            try:
                await stop_holder["fut"]
            finally:
                await srv.stop()

        asyncio.run(_runner())

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    assert ready.wait(timeout=3.0), "Test DualSyncServer failed to start"
    base = f"http://127.0.0.1:{port}"
    try:
        st_code, st = _http_json(f"{base}/status")
        assert st_code == 200 and st["version"] == "5.4.0"
        cur_seq = st["latest_seq"]

        code, hud_res = _http_json(
            f"{base}/set_nav_hud",
            "POST",
            {"capture_visible": True, "guide_zoom_mode": "large"},
        )
        assert (
            code == 200
            and hud_res["nav_hud_capture_visible"] is True
            and hud_res["nav_hud_guide_zoom_mode"] == "large"
        )
        _, ev1 = _http_json(f"{base}/poll?since={cur_seq}")
        assert (
            ev1["type"] == "NAV_HUD_UPDATED"
            and ev1["nav_hud_capture_visible"] is True
            and ev1["nav_hud_guide_zoom_mode"] == "large"
        )

        code, aud_res = _http_json(
            f"{base}/set_audio",
            "POST",
            {"audio_enabled": False, "audio_volume": 45, "test": True},
        )
        assert (
            code == 200
            and aud_res["audio_enabled"] is False
            and aud_res["audio_volume"] == 45
            and aud_res["tested"] is True
        )
        _, ev_aud = _http_json(f"{base}/poll?since={ev1['_seq']}")
        assert ev_aud["type"] == "AUDIO_UPDATED" and ev_aud["audio_enabled"] is False and ev_aud["audio_volume"] == 45

        code, chk_res = _http_json(f"{base}/check_hotkey_conflicts", "POST", {"bindings": {"nav_mark": "alt+k"}, "lang": "vi"})
        assert code == 200 and chk_res["ok"] is True and "nav_mark" in chk_res["actions"]

        code, rem_res = _http_json(
            f"{base}/remap_external_hotkey",
            "POST",
            {
                "app_id": "nvidia_overlay",
                "shortcut_id": "OpenIGO",
                "new_external_hotkey": "alt+shift+p",
                "claim_action": "nav_mark",
                "claim_hotkey": "alt+z",
            },
        )
        assert code == 200 and rem_res["ok"] is True and state["remap_calls"] == 1
        _, ev2 = _http_json(f"{base}/poll?since={ev_aud['_seq']}")
        assert ev2["type"] == "HOTKEY_UPDATED" and ev2["action"] == "nav_mark" and ev2["hotkey"] == "alt+z"

        code, rst_res = _http_json(
            f"{base}/restore_external_hotkey",
            "POST",
            {"app_id": "nvidia_overlay", "shortcut_id": "OpenIGO"},
        )
        assert code == 200 and rst_res["ok"] is True and state["restore_calls"] == 1
    finally:
        loop = loop_holder.get("loop")
        fut = stop_holder.get("fut")
        if loop and fut and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, None)
        th.join(timeout=3.0)
    print("  [OK] DualSyncServer capture_visible & conflict/remap/restore endpoints passed")


def verify_bilingual_hud_and_template_free_fallback() -> None:
    # 1. PinGuideFetcher caches separately per (lang_tag, pin_id)
    cur_lang = ["vi"]
    calls = []

    def fake_guide_json(url: str) -> dict:
        calls.append(url)
        text = "Gợi ý tiếng Việt" if "lang=vi-vn" in url else "English community tip"
        return {"retcode": 0, "data": {"info": {"content": text, "img": ""}}}

    gf = PinGuideFetcher(
        lang_getter=lambda: cur_lang[0],
        fetch_json=fake_guide_json,
        fetch_bytes=lambda _u: b"",
    )
    done_ev = threading.Event()
    assert gf.request("12345", 2, lambda _g: done_ev.set()) is True
    assert done_ev.wait(timeout=2.0)
    vi_guide = gf.get_cached("12345")
    assert vi_guide is not None and vi_guide.description == "Gợi ý tiếng Việt"

    cur_lang[0] = "en"
    assert gf.get_cached("12345") is None, "Switching to EN must not return cached VI guide"
    done_ev.clear()
    assert gf.request("12345", 2, lambda _g: done_ev.set()) is True
    assert done_ev.wait(timeout=2.0)
    en_guide = gf.get_cached("12345")
    assert en_guide is not None and en_guide.description == "English community tip"
    gf.stop()

    # 2. HoYoLABLabelCatalog tree parsing, map names & NavHudRenderer localized title override
    def fake_tree_json(url: str) -> dict:
        is_vi = "lang=vi-vn" in url
        return {
            "retcode": 0,
            "data": {
                "tree": [
                    {
                        "id": 100,
                        "name": "Giải Đố" if is_vi else "Puzzle",
                        "children": [
                            {"id": 18, "name": "Khiêu Chiến Hạn Giờ" if is_vi else "Time Trial Challenge"}
                        ],
                    }
                ]
            },
        }

    catalog = HoYoLABLabelCatalog(fetch_json=fake_tree_json)
    catalog.fetch_sync(2, "vi")
    catalog.fetch_sync(2, "en")
    assert catalog.get_label(2, 18, "vi") == "Khiêu Chiến Hạn Giờ"
    assert catalog.get_label(2, 18, "en") == "Time Trial Challenge"
    assert HoYoLABLabelCatalog.get_map_name(2, "en") == "Teyvat Interactive Map"
    assert HoYoLABLabelCatalog.get_map_name(2, "vi") == "Bản Đồ Tương Tác Teyvat"

    pin = NavPin(
        id="p18",
        name="Khiêu Chiến Hạn Giờ",
        category="Khiêu Chiến Hạn Giờ",
        label_id=18,
        lat=-15.0,
        lng=0.0,
        map_id=2,
    )
    g = NavigationController([pin]).compute_guidance(0.0, 0.0, 0.0, player_map_id=2)
    title_en, _, _, _, _, _, _ = NavHudRenderer._resolve_texts_and_mode(
        g, lang="en", hotkey_label="Alt + K", label_resolver=catalog.get_label
    )
    assert title_en.startswith("Time Trial Challenge"), f"Expected English pin label, got {title_en!r}"
    title_vi, _, _, _, _, _, _ = NavHudRenderer._resolve_texts_and_mode(
        g, lang="vi", hotkey_label="Alt + K", label_resolver=catalog.get_label
    )
    assert title_vi.startswith("Khiêu Chiến Hạn Giờ"), f"Expected Vietnamese pin label, got {title_vi!r}"

    # 3. MapScreenStateMachine template-free procedural fallback + 3-asset REQUIRED_ASSETS
    assert set(asset_setup.REQUIRED_ASSETS) == {
        "multimap_v4.json",
        "terrain_n2_v4.npz",
        "cave_masks_v4.npz",
    }
    assert isinstance(asset_setup.resolve_data_asset("multimap_v4.json"), Path)

    fsm = MapScreenStateMachine(anchors_path="__missing_ui_anchors__.npz")
    assert fsm.has_anchor_templates is False
    px, py, pw, ph = fsm.get_slider_probe_rect(0, 0, 1920, 1080)
    assert pw > 20 and ph > 100
    blank_roi = np.full((ph, pw, 3), 240, dtype=np.uint8)
    ok_probe, _ = fsm.is_slider_probe(blank_roi, 1920, 1080)
    assert ok_probe is False, "Template-free slider probe must reject bright flat panels"
    blank_full = np.full((1080, 1920, 3), 240, dtype=np.uint8)
    assert fsm.is_map_m_open(blank_full)["is_open"] is False
    print("  [OK] Bilingual guide/label HUD localization & template-free Map M fallback passed")


def main() -> None:
    print("Running Smart Hotkey Conflict & Capture-Visible Overlay Verification...")
    verify_config_defaults_and_roundtrip()
    verify_nav_hud_capture_affinity()
    verify_non_covering_constants()
    verify_conflict_advisor_and_nvidia_adapter()
    verify_ll_keyboard_hook_and_no_sendinput()
    verify_server_endpoints_integration()
    verify_bilingual_hud_and_template_free_fallback()
    print("ALL HOTKEY CONFLICTS & CAPTURE CHECKS PASSED (100%)")


if __name__ == "__main__":
    main()
