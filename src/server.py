import asyncio
import json
import logging
import os
import subprocess
import time
from collections import deque
from typing import Optional, Set, Dict, Any

logger = logging.getLogger("GenshinHelper.Server")

class DualSyncServer:
    """
    Zero-dependency pure Python async HTTP long-poll & WebSocket server.
    Equipped with Event Queue (zero lost messages) and Live Browser State Reporting.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self.is_running = False
        self._server = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending_responses: Set[asyncio.Future] = set()
        self._event_queue = deque(maxlen=30)
        self.last_browser_state: Dict[str, Any] = {}
        self.on_hotkey_change = None
        self.get_hotkey_info = None

    async def _safe_close(self, writer: asyncio.StreamWriter):
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:
            pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            try:
                request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            except asyncio.TimeoutError:
                await self._safe_close(writer)
                return

            if not request_line:
                await self._safe_close(writer)
                return

            line = request_line.decode("utf-8", errors="ignore").strip()
            parts = line.split()
            if len(parts) < 2:
                await self._safe_close(writer)
                return

            method, path = parts[0].upper(), parts[1]

            # Read headers
            headers = {}
            while True:
                try:
                    header_line = await asyncio.wait_for(reader.readline(), timeout=3.0)
                except asyncio.TimeoutError:
                    break
                if not header_line or not header_line.strip():
                    break
                h_decoded = header_line.decode("utf-8", errors="ignore").strip()
                if ":" in h_decoded:
                    k, v = h_decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = int(headers.get("content-length", 0))
            body_bytes = b""
            if content_length > 0:
                try:
                    body_bytes = await asyncio.wait_for(reader.readexactly(content_length), timeout=3.0)
                except Exception:
                    pass

            cors_headers = (
                "Access-Control-Allow-Origin: *\r\n"
                "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                "Access-Control-Allow-Headers: *\r\n"
                "Access-Control-Allow-Private-Network: true\r\n"
                "Connection: close\r\n"
            )

            if method == "OPTIONS":
                response = f"HTTP/1.1 204 No Content\r\n{cors_headers}Content-Length: 0\r\n\r\n"
                writer.write(response.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Serve userscript
            if path.startswith("/script.js") or path.startswith("/hoyolab_sync.user.js") or path.endswith(".user.js"):
                script_path = os.path.join(os.path.dirname(__file__), "..", "tampermonkey", "hoyolab_sync.user.js")
                try:
                    with open(script_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    encoded = content.encode("utf-8")
                    res = (
                        f"HTTP/1.1 200 OK\r\n{cors_headers}"
                        "Content-Type: application/javascript; charset=utf-8\r\n"
                        "Cache-Control: no-cache, no-store, must-revalidate\r\n"
                        f"Content-Length: {len(encoded)}\r\n\r\n"
                    ).encode("utf-8") + encoded
                    writer.write(res)
                    await writer.drain()
                    await self._safe_close(writer)
                    return
                except Exception as e:
                    logger.error(f"Failed to serve script.js: {e}")

            # Status endpoint
            if path.startswith("/status"):
                hotkey_info = {"hotkey": "alt+f11", "display_name": "ALT+F11"}
                if self.get_hotkey_info:
                    try:
                        hotkey_info = self.get_hotkey_info()
                    except Exception:
                        pass
                status_payload = {
                    "status": "connected",
                    "version": "3.0.0",
                    "queue_len": len(self._event_queue),
                    **hotkey_info
                }
                body = json.dumps(status_payload)
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Set Hotkey endpoint (supports keyboard, mouse, and combos)
            if path.startswith("/set_hotkey"):
                new_key = ""
                if body_bytes:
                    try:
                        data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                        new_key = data.get("hotkey", "").strip()
                    except Exception:
                        pass
                if not new_key and "?" in path:
                    import urllib.parse
                    qs = urllib.parse.parse_qs(path.split("?", 1)[1])
                    if "hotkey" in qs:
                        new_key = qs["hotkey"][0].strip()

                success = False
                display_name = new_key
                if new_key and self.on_hotkey_change:
                    try:
                        success, display_name = self.on_hotkey_change(new_key)
                        if success:
                            self.broadcast({
                                "type": "HOTKEY_UPDATED",
                                "hotkey": new_key,
                                "display_name": display_name
                            })
                    except Exception as e:
                        logger.error(f"Error executing on_hotkey_change: {e}")

                body = json.dumps({"ok": success, "hotkey": new_key, "display_name": display_name})
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Report state endpoint (called by userscript to sync live status)
            if path.startswith("/report_state"):
                if body_bytes:
                    try:
                        data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                        self.last_browser_state.update(data)
                    except Exception:
                        pass
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Get browser state endpoint (called by test scripts)
            if path.startswith("/browser_state"):
                body = json.dumps(self.last_browser_state)
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # External trigger sync endpoint (allows test scripts or UI to trigger map pan)
            if path.startswith("/trigger_sync"):
                payload = {"type": "SYNC_AND_MARK"}
                if body_bytes:
                    try:
                        payload.update(json.loads(body_bytes.decode("utf-8", errors="ignore")))
                    except Exception:
                        pass
                self.broadcast(payload)
                body = json.dumps({"ok": True, "broadcasted": payload})
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Long-polling endpoint for Tampermonkey
            if path.startswith("/poll"):
                # 1. If we have queued events, return immediately!
                if self._event_queue:
                    event_data = self._event_queue.popleft()
                else:
                    fut = self._loop.create_future()
                    self._pending_responses.add(fut)
                    try:
                        event_data = await asyncio.wait_for(fut, timeout=15.0)
                    except asyncio.TimeoutError:
                        event_data = {"type": "HEARTBEAT"}
                    finally:
                        self._pending_responses.discard(fut)

                body = json.dumps(event_data)
                res = (
                    f"HTTP/1.1 200 OK\r\n{cors_headers}"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n{body}"
                )
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Default 404
            writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await self._safe_close(writer)

        except Exception as e:
            logger.debug(f"HTTP handler exception: {e}")
            await self._safe_close(writer)

    def _free_port_if_busy(self):
        try:
            current_pid = os.getpid()
            output = subprocess.check_output(
                f'netstat -ano | findstr ":{self.port}" | findstr "LISTENING"',
                shell=True, text=True, errors="ignore"
            )
            for line in output.strip().splitlines():
                parts = line.strip().split()
                if len(parts) >= 5:
                    pid = int(parts[-1])
                    if pid != current_pid and pid > 0:
                        logger.info(f"Port {self.port} is held by old process (PID {pid}). Releasing...")
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
                        time.sleep(0.5)
        except Exception:
            pass

    async def start(self):
        self._free_port_if_busy()
        self._loop = asyncio.get_running_loop()
        for attempt in range(5):
            try:
                self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
                break
            except OSError as e:
                if attempt < 4 and getattr(e, 'errno', None) == 10048:
                    logger.info(f"Port {self.port} in TIME_WAIT. Retrying in 1s (attempt {attempt+1}/5)...")
                    await asyncio.sleep(1.0)
                else:
                    raise
        self.is_running = True
        logger.info(f"Sync Server listening on http://{self.host}:{self.port} (Queue & Hot-Reload enabled)")

    async def stop(self):
        self.is_running = False
        for fut in list(self._pending_responses):
            if not fut.done():
                fut.set_result({"type": "SHUTDOWN"})
        self._pending_responses.clear()
        self._event_queue.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Sync Server stopped.")

    def broadcast(self, message: dict):
        """Thread-safe event broadcast with persistent Event Queue."""
        if not self.is_running or not self._loop:
            return

        def _dispatch():
            count = len(self._pending_responses)
            msg_type = message.get("type", "UNKNOWN")
            if count > 0:
                logger.info(f"==> Broadcasted [{msg_type}] to {count} waiting browser tab(s)!")
                for fut in list(self._pending_responses):
                    if not fut.done():
                        fut.set_result(message)
            else:
                # Queue message so the browser receives it on next poll! Zero lost messages!
                self._event_queue.append(message)
                logger.info(f"==> Queued [{msg_type}] (Pending polls: 0, Queue size: {len(self._event_queue)})")

        self._loop.call_soon_threadsafe(_dispatch)

SyncServer = DualSyncServer
