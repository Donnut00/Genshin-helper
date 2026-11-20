import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Optional, Set, Dict, Any

logger = logging.getLogger("GenshinHelper.Server")

class DualSyncServer:
    """
    Zero-dependency pure Python async HTTP long-poll server.
    Equipped with Event Queue (zero lost messages), Auto-Sync config sync, and graceful loopback shutdown.
    """
    SCRIPT_VERSION = "v4.3.0"

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self.is_running = False
        self._server = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending_responses: Set[asyncio.Future] = set()
        self._event_queue = deque(maxlen=30)
        self._event_seq: int = 0
        self._event_log = deque(maxlen=30)
        self.last_browser_state: Dict[str, Any] = {}
        self.on_hotkey_change = None
        self.get_hotkey_info = None
        self.on_trigger_diagnostic = None
        self.on_auto_sync_change = None
        self.on_shutdown_request = None

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
                hotkey_info = {
                    "hotkey": "alt+f11",
                    "display_name": "ALT+F11",
                    "hotkey_sync": "alt+f11",
                    "display_name_sync": "ALT+F11",
                    "hotkey_debug": "alt+f12",
                    "display_name_debug": "ALT+F12",
                    "auto_sync_m": True,
                }
                if self.get_hotkey_info:
                    try:
                        info = self.get_hotkey_info()
                        if isinstance(info, dict):
                            hotkey_info.update(info)
                    except Exception as e:
                        logger.warning(f"Error calling get_hotkey_info: {e}")

                status_payload = {
                    "status": "connected",
                    "version": "4.0.0",
                    "script_version": self.SCRIPT_VERSION,
                    "queue_len": len(self._event_queue),
                    "latest_seq": self._event_seq,
                    **hotkey_info
                }
                body = json.dumps(status_payload)
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Auto-Sync Map (M) toggle endpoint
            if path.startswith("/set_auto_sync"):
                req_data = {}
                if body_bytes:
                    try:
                        req_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
                enabled = bool(req_data.get("auto_sync_m", True))
                if self.on_auto_sync_change:
                    try:
                        enabled = bool(self.on_auto_sync_change(enabled))
                    except Exception as e:
                        logger.error(f"Error updating auto_sync_m: {e}")
                self.broadcast({"type": "AUTO_SYNC_UPDATED", "auto_sync_m": enabled})
                body = json.dumps({"ok": True, "auto_sync_m": enabled})
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                return

            # Loopback-only graceful shutdown endpoint
            if path.startswith("/shutdown"):
                peer = writer.get_extra_info("peername")
                peer_ip = peer[0] if isinstance(peer, tuple) and peer else ""
                if method != "POST" or peer_ip not in ("127.0.0.1", "::1"):
                    body = json.dumps({"ok": False, "error": "Forbidden"})
                    res = f"HTTP/1.1 403 Forbidden\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                    writer.write(res.encode("utf-8"))
                    await writer.drain()
                    await self._safe_close(writer)
                    return

                body = json.dumps({"ok": True, "shutting_down": True})
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                writer.write(res.encode("utf-8"))
                await writer.drain()
                await self._safe_close(writer)
                if self.on_shutdown_request:
                    self._loop.call_soon(self.on_shutdown_request)
                else:
                    self._loop.create_task(self.stop())
                return

            # Set Hotkey endpoint (supports single, action-based, or batch {hotkey_sync, hotkey_debug})
            if path.startswith("/set_hotkey") or path.startswith("/set_hotkeys"):
                req_data = {}
                if body_bytes:
                    try:
                        req_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
                if not req_data and "?" in path:
                    qs = urllib.parse.parse_qs(path.split("?", 1)[1])
                    for k, v in qs.items():
                        req_data[k] = v[0].strip()

                results = {}
                # Case 1: Batch sync & debug update
                if "hotkey_sync" in req_data or "hotkey_debug" in req_data:
                    for act in ("sync", "debug"):
                        kname = f"hotkey_{act}"
                        if kname in req_data and req_data[kname] and self.on_hotkey_change:
                            try:
                                ok, dname = self.on_hotkey_change(act, req_data[kname])
                                results[act] = {"ok": ok, "hotkey": req_data[kname], "display_name": dname}
                                if ok:
                                    self.broadcast({
                                        "type": "HOTKEY_UPDATED",
                                        "action": act,
                                        "hotkey": req_data[kname],
                                        "display_name": dname
                                    })
                            except Exception as e:
                                logger.error(f"Error updating hotkey [{act}]: {e}")
                # Case 2: Specific action
                elif "action" in req_data and "hotkey" in req_data:
                    act = req_data["action"].strip().lower()
                    new_k = req_data["hotkey"].strip().lower()
                    if self.on_hotkey_change:
                        try:
                            ok, dname = self.on_hotkey_change(act, new_k)
                            results[act] = {"ok": ok, "hotkey": new_k, "display_name": dname}
                            if ok:
                                self.broadcast({
                                    "type": "HOTKEY_UPDATED",
                                    "action": act,
                                    "hotkey": new_k,
                                    "display_name": dname
                                })
                        except Exception as e:
                            logger.error(f"Error updating hotkey [{act}]: {e}")
                # Case 3: Legacy single hotkey (defaults to 'sync')
                elif "hotkey" in req_data:
                    new_k = req_data["hotkey"].strip().lower()
                    if self.on_hotkey_change:
                        try:
                            ok, dname = self.on_hotkey_change("sync", new_k)
                            results["sync"] = {"ok": ok, "hotkey": new_k, "display_name": dname}
                            if ok:
                                self.broadcast({
                                    "type": "HOTKEY_UPDATED",
                                    "action": "sync",
                                    "hotkey": new_k,
                                    "display_name": dname
                                })
                        except Exception as e:
                            logger.error(f"Error updating single hotkey: {e}")

                body = json.dumps({"ok": True, "results": results})
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

            # Trigger Diagnostic Capture endpoint (can be called from web UI or test scripts)
            if path.startswith("/trigger_diagnostic"):
                req_data = {}
                if body_bytes:
                    try:
                        req_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
                note = str(req_data.get("note", "") or "")
                bundle_res = {}
                if self.on_trigger_diagnostic:
                    try:
                        try:
                            bundle_res = self.on_trigger_diagnostic(self.last_browser_state, note)
                        except TypeError:
                            bundle_res = self.on_trigger_diagnostic(self.last_browser_state)
                    except Exception as e:
                        logger.error(f"Error triggering diagnostic: {e}")
                        bundle_res = {"success": False, "error": str(e)}

                body_bytes_out = json.dumps({"ok": True, "bundle": bundle_res}).encode("utf-8")
                res = f"HTTP/1.1 200 OK\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body_bytes_out)}\r\n\r\n".encode("utf-8") + body_bytes_out
                writer.write(res)
                await writer.drain()
                await self._safe_close(writer)
                return

            # Save bug note directly into an existing diagnostic bundle
            if path.startswith("/save_diagnostic_note"):
                req_data = {}
                if body_bytes:
                    try:
                        req_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    except Exception:
                        req_data = {}
                folder_name = str(req_data.get("folder_name", "") or "").strip() if isinstance(req_data, dict) else ""
                note = str(req_data.get("note", "") or "") if isinstance(req_data, dict) else ""
                from src.diagnostics import DiagnosticManager
                note_res = DiagnosticManager.save_user_note(folder_name, note)
                if note_res.get("ok"):
                    status_line = "200 OK"
                elif note_res.get("not_found"):
                    status_line = "404 Not Found"
                else:
                    status_line = "400 Bad Request"
                body_bytes_out = json.dumps(note_res).encode("utf-8")
                res = f"HTTP/1.1 {status_line}\r\n{cors_headers}Content-Type: application/json\r\nContent-Length: {len(body_bytes_out)}\r\n\r\n".encode("utf-8") + body_bytes_out
                writer.write(res)
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
                since_seq = None
                if "?" in path:
                    try:
                        qs = urllib.parse.parse_qs(path.split("?", 1)[1])
                        if "since" in qs and qs["since"]:
                            since_seq = int(qs["since"][0])
                    except Exception:
                        since_seq = None

                event_data = None
                if since_seq is not None:
                    if since_seq < 0:
                        event_data = {
                            "type": "HEARTBEAT",
                            "script_version": self.SCRIPT_VERSION,
                            "_seq": self._event_seq,
                        }
                    else:
                        for seq, payload in self._event_log:
                            if seq > since_seq:
                                event_data = payload
                                break
                        if event_data is None:
                            fut = self._loop.create_future()
                            self._pending_responses.add(fut)
                            try:
                                event_data = await asyncio.wait_for(fut, timeout=15.0)
                            except asyncio.TimeoutError:
                                event_data = {
                                    "type": "HEARTBEAT",
                                    "script_version": self.SCRIPT_VERSION,
                                    "_seq": self._event_seq,
                                }
                            finally:
                                self._pending_responses.discard(fut)
                else:
                    # Legacy cursorless /poll path: pop from queue or wait
                    if self._event_queue:
                        event_data = self._event_queue.popleft()
                    else:
                        fut = self._loop.create_future()
                        self._pending_responses.add(fut)
                        try:
                            event_data = await asyncio.wait_for(fut, timeout=15.0)
                        except asyncio.TimeoutError:
                            event_data = {
                                "type": "HEARTBEAT",
                                "script_version": self.SCRIPT_VERSION,
                                "_seq": self._event_seq,
                            }
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
            url = f"http://{self.host}:{self.port}/shutdown"
            req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    logger.info(f"Sent graceful /shutdown to existing server on port {self.port}. Waiting to bind...")
                    time.sleep(0.45)
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
        self._event_log.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Sync Server stopped.")

    def broadcast(self, message: dict):
        """Thread-safe event broadcast with multi-tab cursor log & legacy Event Queue."""
        if not self.is_running or not self._loop:
            return

        def _dispatch():
            self._event_seq += 1
            stamped = dict(message)
            stamped["_seq"] = self._event_seq
            self._event_log.append((self._event_seq, stamped))

            count = len(self._pending_responses)
            msg_type = stamped.get("type", "UNKNOWN")
            if count > 0:
                logger.info(
                    f"==> Broadcasted [{msg_type}] #{self._event_seq} to {count} waiting browser tab(s)!"
                )
                for fut in list(self._pending_responses):
                    if not fut.done():
                        fut.set_result(stamped)
            else:
                self._event_queue.append(stamped)
                logger.info(
                    f"==> Queued [{msg_type}] #{self._event_seq} "
                    f"(Pending polls: 0, Queue size: {len(self._event_queue)})"
                )

        self._loop.call_soon_threadsafe(_dispatch)

SyncServer = DualSyncServer
