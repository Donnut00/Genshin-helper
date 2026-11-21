"""
Verification checks for Genshin Helper v4.5.0 Release-Ready Architecture:
1. src/paths.py dev vs frozen PyInstaller path resolution (non-destructive env simulation)
2. DualSyncServer /set_engine_state + ENGINE_STATE_UPDATED broadcast + /status engine_active & last_sync
3. main.tracking_loop Standby 0-capture behavior when engine_state['active'] == False
4. src/native_host.py binary framing & ping/start_server/stop_server against ephemeral server
5. src/tray.py menu items + all required Paimon .ico assets present
6. Extension manifest key -> FIXED_EXT_ID consistency across extension & installer + clean page_main/popup UI
"""
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import urllib.request
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import main as app_main
from src import native_host, paths
from src.server import DualSyncServer
from src.tray import SystemTrayApp


def _http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=4.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify_paths_dev_and_frozen() -> None:
    assert paths.is_frozen() is False
    assert os.path.normpath(paths.get_config_path()) == os.path.normpath(os.path.join(PROJECT_ROOT, "config.json"))
    assert os.path.normpath(paths.get_diagnostics_dir()) == os.path.normpath(
        os.path.join(PROJECT_ROOT, "scratch", "diagnostics")
    )
    assert os.path.normpath(paths.get_ocr_cache_dir()) == os.path.normpath(os.path.join(PROJECT_ROOT, "scratch"))
    assert os.path.exists(paths.get_asset_path("assets", "icons", "app.ico"))

    with tempfile.TemporaryDirectory() as tmpdir:
        roaming = os.path.join(tmpdir, "Roaming")
        local_app = os.path.join(tmpdir, "Local")
        bundle = os.path.join(tmpdir, "Bundle")
        os.makedirs(roaming, exist_ok=True)
        os.makedirs(local_app, exist_ok=True)
        os.makedirs(bundle, exist_ok=True)
        fake_exe = os.path.join(tmpdir, "App", "GenshinHelper.exe")

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", bundle, create=True),
            patch.object(sys, "executable", fake_exe),
            patch.dict(os.environ, {"APPDATA": roaming, "LOCALAPPDATA": local_app}),
        ):
            assert paths.is_frozen() is True
            assert paths.get_bundle_dir() == os.path.abspath(bundle)
            assert paths.get_app_dir() == os.path.dirname(os.path.abspath(fake_exe))
            assert paths.get_config_path() == os.path.join(os.path.abspath(roaming), "GenshinHelper", "config.json")
            assert paths.get_diagnostics_dir() == os.path.join(os.path.abspath(roaming), "GenshinHelper", "diagnostics")
            assert paths.get_ocr_cache_dir() == os.path.join(os.path.abspath(local_app), "GenshinHelper", "ocr_cache")

    print("  [OK] src/paths.py dev & simulated frozen resolution passed")


def verify_standby_tracking_loop() -> None:
    class _MockServer:
        is_running = True

    class _MockCapturer:
        def __init__(self) -> None:
            self.calls = 0

        def capture_region(self, *_args):
            self.calls += 1
            return None

    class _MockStateMachine:
        STATE_CLOSED = "CLOSED"
        STATE_SETTLING = "SETTLING"
        STATE_SYNCED_IDLE = "SYNCED_IDLE"
        STATE_PANNING = "PANNING"

        def __init__(self) -> None:
            self.state = self.STATE_SYNCED_IDLE
            self.resets = 0

        def reset(self) -> None:
            self.resets += 1
            self.state = self.STATE_CLOSED

    orig_sleep = app_main.asyncio.sleep

    async def _run() -> None:
        stop_ev = asyncio.Event()
        srv = _MockServer()
        cap = _MockCapturer()
        sm = _MockStateMachine()
        engine_state = {"active": False}

        async def _stop_on_sleep(_sec: float) -> None:
            stop_ev.set()

        app_main.asyncio.sleep = _stop_on_sleep
        try:
            await app_main.tracking_loop(srv, cap, object(), sm, stop_ev, engine_state=engine_state)
        finally:
            app_main.asyncio.sleep = orig_sleep

        assert cap.calls == 0, f"Expected 0 captures in Standby, got {cap.calls}"
        assert sm.resets >= 1 and sm.state == sm.STATE_CLOSED

    asyncio.run(_run())
    print("  [OK] tracking_loop Standby 0-capture / 0% CPU gate passed")


def verify_server_and_native_host() -> None:
    server = DualSyncServer(host="127.0.0.1", port=0)
    engine_state = {"active": True}

    server.get_hotkey_info = lambda: {
        "engine_active": engine_state["active"],
        "last_sync": server.last_sync_info,
    }

    def on_engine_change(active: bool) -> bool:
        engine_state["active"] = bool(active)
        return engine_state["active"]

    server.on_engine_state_change = on_engine_change

    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    stop_future_holder: dict[str, asyncio.Future] = {}
    ready_ev = threading.Event()

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

    th = threading.Thread(target=_run_srv, daemon=True)
    th.start()
    assert ready_ev.wait(timeout=3.0)
    base = f"http://127.0.0.1:{server.port}"

    try:
        # 1. Binary Native Messaging framing round-trip
        buf = io.BytesIO()
        sample = {"cmd": "ping", "note": "Kiểm tra tiếng Việt UTF-8"}
        native_host.write_native_message(buf, sample)
        buf.seek(0)
        decoded = native_host.read_native_message(buf)
        assert decoded == sample

        # 2. /status + /trigger_sync populates last_sync
        st0 = _http_json(f"{base}/status")
        assert st0["version"] == "4.5.0" and st0["script_version"] == "v4.5.0"
        assert st0["engine_active"] is True and st0["last_sync"] is None

        seq0 = st0["latest_seq"]
        _http_json(
            f"{base}/trigger_sync",
            method="POST",
            payload={
                "has_world_coords": True,
                "map_id": 2,
                "map_name": "Teyvat",
                "area_name": "Fontaine",
                "lat": 111.5,
                "lng": -222.5,
                "is_underground": True,
                "floor_short": "B1",
            },
        )
        ev_sync = _http_json(f"{base}/poll?since={seq0}")
        assert ev_sync["type"] == "SYNC_AND_MARK"
        st1 = _http_json(f"{base}/status")
        assert st1["last_sync"] is not None and st1["last_sync"]["area_name"] == "Fontaine"
        assert st1["last_sync"]["floor"] == "B1"

        # 3. Native Host ping -> stop_server(standby) -> start_server(woke_standby)
        p_res = native_host.handle_command({"cmd": "ping"}, port=server.port)
        assert p_res["ok"] and p_res["server_running"] is True and p_res["engine_active"] is True

        seq1 = st1["latest_seq"]
        stop_res = native_host.handle_command({"cmd": "stop_server", "mode": "standby"}, port=server.port)
        assert stop_res["ok"] and stop_res["engine_active"] is False and engine_state["active"] is False
        ev_off = _http_json(f"{base}/poll?since={seq1}")
        assert ev_off["type"] == "ENGINE_STATE_UPDATED" and ev_off["engine_active"] is False

        wake_res = native_host.handle_command({"cmd": "start_server"}, port=server.port)
        assert wake_res["ok"] and wake_res["started"] == "woke_standby" and engine_state["active"] is True
        ev_on = _http_json(f"{base}/poll?since={ev_off['_seq']}")
        assert ev_on["type"] == "ENGINE_STATE_UPDATED" and ev_on["engine_active"] is True
    finally:
        loop = loop_holder.get("loop")
        fut = stop_future_holder.get("fut")
        if loop and fut and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, None)
        th.join(timeout=3.0)

    print("  [OK] DualSyncServer v4.5.0 (/set_engine_state + last_sync) & Native Host passed")


def verify_tray_and_assets() -> None:
    items_active = SystemTrayApp.build_menu_items(True)
    items_paused = SystemTrayApp.build_menu_items(False)
    assert items_active[0][1] != items_paused[0][1]
    assert any(cmd == SystemTrayApp.ID_QUIT for cmd, _, _ in items_active)

    for fname in ("app.ico", "tray_default.ico", "tray_connected.ico", "tray_disconnected.ico", "tray_paused.ico"):
        fp = os.path.join(PROJECT_ROOT, "assets", "icons", fname)
        assert os.path.isfile(fp), f"Missing icon asset: {fp}"

    print("  [OK] SystemTrayApp menu & icon assets check passed")


def verify_extension_and_installer_consistency() -> None:
    manifest_path = os.path.join(PROJECT_ROOT, "extension", "manifest.json")
    manifest = json.load(open(manifest_path, "r", encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "4.5.0"
    assert "nativeMessaging" in manifest.get("permissions", [])
    assert manifest.get("key"), "Missing pinned RSA public key in extension/manifest.json"

    der = base64.b64decode(manifest["key"])
    ext_id = "".join(chr(97 + (b >> 4)) + chr(97 + (b & 15)) for b in hashlib.sha256(der).digest()[:16])

    nh_path = os.path.join(PROJECT_ROOT, "installer", "native_host_manifest.json")
    nh = json.load(open(nh_path, "r", encoding="utf-8"))
    assert nh["name"] == "com.genshinhelper.host"
    assert f"chrome-extension://{ext_id}/" in nh["allowed_origins"]

    iss_path = os.path.join(PROJECT_ROOT, "installer", "GenshinHelper.iss")
    iss_txt = open(iss_path, "r", encoding="utf-8").read()
    assert ext_id in iss_txt, f"Extension ID {ext_id} missing from GenshinHelper.iss"
    assert "GenshinHelperHost.exe" in iss_txt and "install_extension.html" in iss_txt

    for fn in os.listdir(os.path.join(PROJECT_ROOT, "extension")):
        if fn.endswith(".js"):
            txt = open(os.path.join(PROJECT_ROOT, "extension", fn), "r", encoding="utf-8").read()
            assert "eval(" not in txt, f"Disallowed eval() found in extension/{fn}"

    page_main = open(os.path.join(PROJECT_ROOT, "extension", "page_main.js"), "r", encoding="utf-8").read()
    for banned in ('id="gh-card"', 'id="gh-pill"', "id='gh-card'", "id='gh-pill'", "gh-settings-modal"):
        assert banned not in page_main, f"Permanent map overlay/modal '{banned}' must be removed from extension/page_main.js"
    for required in ("gh-mini-toast", "updatePlayerMarker", "showDiagnosticNoteModal", "ENGINE_STATE_UPDATED"):
        assert required in page_main, f"Missing '{required}' in extension/page_main.js"

    popup_html = open(os.path.join(PROJECT_ROOT, "extension", "popup.html"), "r", encoding="utf-8").read()
    assert "btn-power-toggle" in popup_html or "power" in popup_html.lower()
    assert "trigger-diag" in popup_html or "diag" in popup_html.lower()

    print(f"  [OK] Extension Key/ID ({ext_id}) + Installer + Clean Control Center consistency passed")


def main() -> None:
    print("[v4.5.0 Release Verification] Running Phase 1 + Phase 2 checks...")
    verify_paths_dev_and_frozen()
    verify_standby_tracking_loop()
    verify_server_and_native_host()
    verify_tray_and_assets()
    verify_extension_and_installer_consistency()
    print("ALL V4.5.0 RELEASE CHECKS PASSED!")


if __name__ == "__main__":
    main()
