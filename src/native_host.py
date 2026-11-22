"""
Pure stdlib Chrome / Edge Native Messaging Host for Genshin Helper (com.genshinhelper.host).
Allows the browser extension's Power Toggle to wake a Standby server or start GenshinHelper.exe.
"""
import argparse
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, BinaryIO, Dict, Optional

HOST_VERSION = "5.4.0"
TASK_NAME = "GenshinHelper_Elevated"
DEFAULT_PORT = 8765
MAX_MESSAGE_BYTES = 1_048_576
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def read_native_message(stream: BinaryIO) -> Optional[Dict[str, Any]]:
    """Reads a single 4-byte little-endian length-prefixed JSON object from stdin."""
    header = stream.read(4)
    if not header or len(header) < 4:
        return None
    (msg_len,) = struct.unpack("<I", header)
    if msg_len == 0 or msg_len > MAX_MESSAGE_BYTES:
        return None
    raw = stream.read(msg_len)
    if not raw or len(raw) < msg_len:
        return None
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def write_native_message(stream: BinaryIO, payload: Dict[str, Any]) -> None:
    """Writes a single 4-byte little-endian length-prefixed JSON object to stdout."""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(encoded)))
    stream.write(encoded)
    stream.flush()


def _http_json(
    url: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 0.8,
) -> Optional[Dict[str, Any]]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status < 200 or resp.status >= 300:
                return None
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return None


def _launch_desktop_app(host_dir: Optional[str] = None) -> bool:
    """Attempts to launch GenshinHelper via elevated Scheduled Task first, then direct executable."""
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["schtasks", "/run", "/tn", TASK_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass

    search_dirs = []
    if host_dir:
        search_dirs.append(os.path.abspath(host_dir))
    search_dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    repo_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    search_dirs.append(repo_root)

    flags = (CREATE_NO_WINDOW | DETACHED_PROCESS) if sys.platform == "win32" else 0
    for d in search_dirs:
        exe_path = os.path.join(d, "GenshinHelper.exe")
        if os.path.isfile(exe_path):
            try:
                subprocess.Popen(
                    [exe_path, "--minimized"],
                    cwd=d,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=flags,
                )
                return True
            except Exception:
                pass

    # Dev workspace fallback
    main_py = os.path.join(repo_root, "main.py")
    if os.path.isfile(main_py):
        py_dir = os.path.dirname(os.path.abspath(sys.executable))
        pythonw = os.path.join(py_dir, "pythonw.exe")
        py_bin = pythonw if os.path.isfile(pythonw) else sys.executable
        try:
            subprocess.Popen(
                [py_bin, main_py, "--minimized"],
                cwd=repo_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        except Exception:
            pass

    return False


def handle_command(
    msg: Dict[str, Any],
    host_dir: Optional[str] = None,
    port: int = DEFAULT_PORT,
    launch_timeout_s: float = 12.0,
) -> Dict[str, Any]:
    """Executes a single Native Messaging command ('ping', 'start_server', 'stop_server')."""
    cmd = str(msg.get("cmd", "") or "").strip().lower()
    base = f"http://127.0.0.1:{int(port)}"

    if cmd == "ping":
        st = _http_json(f"{base}/status", timeout=0.8)
        if st is not None:
            return {
                "ok": True,
                "host_version": HOST_VERSION,
                "server_running": True,
                "engine_active": bool(st.get("engine_active", True)),
                "version": st.get("version"),
            }
        return {
            "ok": True,
            "host_version": HOST_VERSION,
            "server_running": False,
            "engine_active": False,
        }

    if cmd == "start_server":
        st = _http_json(f"{base}/status", timeout=0.8)
        if st is not None:
            if not bool(st.get("engine_active", True)):
                _http_json(f"{base}/set_engine_state", method="POST", payload={"active": True}, timeout=1.5)
                return {
                    "ok": True,
                    "host_version": HOST_VERSION,
                    "server_running": True,
                    "engine_active": True,
                    "started": "woke_standby",
                }
            return {
                "ok": True,
                "host_version": HOST_VERSION,
                "server_running": True,
                "engine_active": True,
                "started": "already_active",
            }

        launched = _launch_desktop_app(host_dir=host_dir)
        if not launched:
            return {
                "ok": False,
                "host_version": HOST_VERSION,
                "server_running": False,
                "error": "Could not launch GenshinHelper executable or scheduled task.",
            }

        deadline = time.monotonic() + max(0.5, float(launch_timeout_s))
        while time.monotonic() < deadline:
            st = _http_json(f"{base}/status", timeout=0.6)
            if st is not None:
                if not bool(st.get("engine_active", True)):
                    _http_json(f"{base}/set_engine_state", method="POST", payload={"active": True}, timeout=1.2)
                return {
                    "ok": True,
                    "host_version": HOST_VERSION,
                    "server_running": True,
                    "engine_active": True,
                    "started": "launched",
                }
            time.sleep(0.2)

        return {
            "ok": False,
            "host_version": HOST_VERSION,
            "server_running": False,
            "error": "Timed out waiting for GenshinHelper server on localhost.",
        }

    if cmd == "stop_server":
        mode = str(msg.get("mode", "standby") or "standby").strip().lower()
        if mode == "shutdown":
            res = _http_json(f"{base}/shutdown", method="POST", payload={}, timeout=1.2)
            return {
                "ok": True,
                "host_version": HOST_VERSION,
                "server_running": False,
                "engine_active": False,
                "mode": "shutdown",
                "acknowledged": res is not None,
            }

        res = _http_json(f"{base}/set_engine_state", method="POST", payload={"active": False}, timeout=1.2)
        if res is not None:
            return {
                "ok": True,
                "host_version": HOST_VERSION,
                "server_running": True,
                "engine_active": bool(res.get("engine_active", False)),
                "mode": "standby",
            }
        return {
            "ok": True,
            "host_version": HOST_VERSION,
            "server_running": False,
            "engine_active": False,
            "mode": "standby",
        }

    return {
        "ok": False,
        "host_version": HOST_VERSION,
        "error": f"Unknown command: {cmd}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Genshin Helper Native Messaging Host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args, _ = parser.parse_known_args()

    host_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        try:
            msg = read_native_message(stdin)
            if msg is None:
                break
            resp = handle_command(msg, host_dir=host_dir, port=args.port)
        except Exception as e:
            resp = {"ok": False, "host_version": HOST_VERSION, "error": str(e)}
        try:
            write_native_message(stdout, resp)
        except Exception:
            break


if __name__ == "__main__":
    main()
