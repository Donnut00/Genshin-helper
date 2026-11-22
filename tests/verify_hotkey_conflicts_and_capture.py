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
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

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
from src.nav_controller import NavPin, NavigationController
from src.nav_hud import NavMiniHud
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
    assert fresh.NAV_HUD_CAPTURE_VISIBLE is False
    assert fresh.HOTKEY_SUPPRESS_CONFLICTS is True

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "config.json")
        fresh.NAV_HUD_CAPTURE_VISIBLE = True
        fresh.HOTKEY_SUPPRESS_CONFLICTS = False
        fresh.HOTKEY_NAV_MARK = "alt+j"
        fresh.save_to_file(cfg_path)

        reloaded = AppConfig()
        reloaded.load_from_file(cfg_path)
        assert reloaded.NAV_HUD_CAPTURE_VISIBLE is True
        assert reloaded.HOTKEY_SUPPRESS_CONFLICTS is False
        assert reloaded.HOTKEY_NAV_MARK == "alt+j"
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

        # Also keep HUD open for unfocused visibility check
        assert WindowFinder.is_game_visible_unfocused(
            hud.hwnd,
            (0, 0, 1920, 1080),
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
    for cls_name in ("ScreenClippingHost", "SnippingTool", "CEF-OSC-WIDGET", "NVOverlayWindow"):
        assert cls_name in WindowFinder._NON_COVERING_CLASSES, f"Missing {cls_name} in _NON_COVERING_CLASSES"
    for proc_name in ("screenclippinghost.exe", "nvidia overlay.exe"):
        assert proc_name in WindowFinder._NON_COVERING_PROCESSES, f"Missing {proc_name} in _NON_COVERING_PROCESSES"
    print("  [OK] Capture/GPU overlay non-covering allowlist passed")


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

        # Genshin always-triggered menu keys fail under Alt AND Alt+Shift
        for risky in ("alt+g", "alt+shift+g"):
            res_g = adv.analyze_hotkey(risky, action="nav_mark", current_bindings=bindings)
            assert res_g["ok"] is False
            assert any(c["kind"] == "genshin" and c["severity"] == "warn" for c in res_g["conflicts"])

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
    state = {"capture_visible": False, "nav_mark": "alt+k", "remap_calls": 0, "restore_calls": 0}

    def on_nav_hud_change(enabled=None, position=None, req_data=None):
        data = req_data if isinstance(req_data, dict) else {}
        cap = data.get("capture_visible", data.get("nav_hud_capture_visible"))
        if cap is not None:
            state["capture_visible"] = bool(cap)
        return {
            "enabled": True if enabled is None else bool(enabled),
            "position": position or "under_minimap",
            "capture_visible": state["capture_visible"],
            "nav_hud_capture_visible": state["capture_visible"],
        }

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
        assert st_code == 200 and st["version"] == "5.3.0"
        cur_seq = st["latest_seq"]

        code, hud_res = _http_json(f"{base}/set_nav_hud", "POST", {"capture_visible": True})
        assert code == 200 and hud_res["nav_hud_capture_visible"] is True
        _, ev1 = _http_json(f"{base}/poll?since={cur_seq}")
        assert ev1["type"] == "NAV_HUD_UPDATED" and ev1["nav_hud_capture_visible"] is True

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
        _, ev2 = _http_json(f"{base}/poll?since={ev1['_seq']}")
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


def main() -> None:
    print("Running Smart Hotkey Conflict & Capture-Visible Overlay Verification...")
    verify_config_defaults_and_roundtrip()
    verify_nav_hud_capture_affinity()
    verify_non_covering_constants()
    verify_conflict_advisor_and_nvidia_adapter()
    verify_ll_keyboard_hook_and_no_sendinput()
    verify_server_endpoints_integration()
    print("ALL HOTKEY CONFLICTS & CAPTURE CHECKS PASSED (100%)")


if __name__ == "__main__":
    main()
