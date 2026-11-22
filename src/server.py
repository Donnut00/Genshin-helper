import asyncio
import inspect
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Callable, Dict, Optional, Set, Tuple

from src.config import APP_VERSION, SCRIPT_VERSION as _APP_SCRIPT_VERSION

logger = logging.getLogger("GenshinHelper.Server")

_VALID_LANGUAGES = frozenset({"vi", "en", "auto"})
_VALID_NAV_ACTIONS = frozenset({
    "next", "prev", "skip", "undo", "refresh", "clear_route", "blacklist", "mark", "guide", "layout",
})
_HOTKEY_ACTIONS = ("sync", "debug", "switch", "nav_mark", "nav_undo", "nav_guide", "nav_next", "nav_layout")
_VALID_LAN_ACTIONS = frozenset({"status", "start", "stop"})
_VALID_EXPORT_FORMATS = frozenset({"kongying", "appsample", "genshinmap", "canonical"})
_PAYLOAD_PARAM_NAMES = frozenset({"payload", "data", "req", "req_data", "request", "body"})


class DualSyncServer:
    """
    Zero-dependency pure Python async HTTP long-poll server.
    Equipped with Event Queue (zero lost messages), Auto-Sync config sync, Standby control, Single-Monitor feedback, and graceful loopback shutdown.
    """
    SCRIPT_VERSION = _APP_SCRIPT_VERSION

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self.is_running = False
        self._server = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending_responses: Set[asyncio.Future] = set()
        self._event_queue = deque(maxlen=64)
        self._event_seq: int = 0
        self._event_log = deque(maxlen=128)
        self._ephemeral_events: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._last_client_poll_ts: float = 0.0
        self._feedback_seen = deque(maxlen=24)
        self.last_browser_state: Dict[str, Any] = {}
        self.last_sync_info: Optional[Dict[str, Any]] = None
        self.on_hotkey_change = None
        self.get_hotkey_info = None
        self.on_trigger_diagnostic = None
        self.on_auto_sync_change = None
        self.on_single_monitor_change = None
        self.on_engine_state_change = None
        self.on_action_feedback = None
        self.on_quick_switch = None
        self.on_shutdown_request = None
        # v5 optional callbacks
        self.on_language_change = None
        self.on_minimap_tracking_change = None
        self.on_nav_hud_change = None
        self.on_nearby_pins = None
        self.on_nav_command = None
        self.on_plan_tsp_route = None
        self.on_lan_companion_action = None
        self.on_launch_borderless = None
        self.on_cross_map_export = None

    @staticmethod
    def _parse_json_body(body_bytes: bytes) -> Dict[str, Any]:
        if not body_bytes:
            return {}
        try:
            parsed = json.loads(body_bytes.decode("utf-8", errors="ignore"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _call_cb(
        cb: Callable[..., Any],
        positional_args: Tuple[Any, ...],
        payload: Dict[str, Any],
        single_arg_default: Any = None,
        use_single_arg_default: bool = False,
    ) -> Any:
        try:
            sig = inspect.signature(cb)
            params = list(sig.parameters.values())
        except (TypeError, ValueError):
            return cb(*positional_args)

        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        pos_params = [
            p for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]

        if has_varargs:
            return cb(*positional_args)
        if len(pos_params) == 0:
            return cb()
        if len(pos_params) == 1:
            p_name = pos_params[0].name.lower()
            if p_name in _PAYLOAD_PARAM_NAMES:
                return cb(payload)
            if use_single_arg_default:
                return cb(single_arg_default)
            if len(positional_args) == 1:
                return cb(positional_args[0])
            return cb(payload)
        return cb(*positional_args[: len(pos_params)])

    @staticmethod
    def _run_default_cross_map_export(req_data: Dict[str, Any]) -> Dict[str, Any]:
        from src.map_bridge import (
            AppsampleAdapter,
            AxisAffine,
            CanonicalJsonAdapter,
            CanonicalPin,
            GenshinMapGithubAdapter,
            KongyingAdapter,
            PerMapAffineRegistry,
            TargetPin,
            fit_axis_affine,
            match_pins_one_to_one,
            parse_hoyolab_export,
            parse_hoyolab_points,
        )

        try:
            map_id = int(req_data.get("map_id", 2))
        except (TypeError, ValueError):
            map_id = 2

        target_format = str(req_data.get("target_format") or req_data.get("format") or "canonical").strip().lower()
        point_list = req_data.get("point_list")
        marked_list = req_data.get("marked_list")
        raw_pins = req_data.get("pins")

        parsed_export = None
        missing_ids = []
        if point_list is not None and marked_list is not None:
            parsed_export = parse_hoyolab_export(point_list, marked_list, map_id=map_id)
            source_pins = parsed_export.pins
            missing_ids = parsed_export.missing_from_point_list
        elif point_list is not None:
            source_pins = parse_hoyolab_points(point_list, map_id=map_id)
        elif isinstance(raw_pins, list):
            source_pins = [
                p if isinstance(p, CanonicalPin) else CanonicalPin.from_dict(p)
                for p in raw_pins
                if isinstance(p, (CanonicalPin, dict))
            ]
        else:
            source_pins = []

        if target_format == "canonical":
            payload = CanonicalJsonAdapter.export(
                parsed_export if parsed_export is not None else source_pins,
                map_id=map_id,
            )
            unmapped_cats = sorted(
                {p.canonical_category for p in source_pins if p.canonical_category.startswith("unmapped_")}
            )
            total_considered = len(source_pins) + len(missing_ids)
            report = {
                "total_source_pins": len(source_pins),
                "matched_count": len(source_pins),
                "confident_count": len(source_pins),
                "ambiguous_count": 0,
                "unmatched_count": 0,
                "unmapped_categories": unmapped_cats,
                "missing_from_point_list_count": len(missing_ids),
                "match_rate_pct": round((100.0 * len(source_pins) / total_considered), 2) if total_considered > 0 else 0.0,
            }
            return {"ok": True, "format": target_format, "payload": payload, "report": report}

        raw_targets = req_data.get("target_pins")
        if raw_targets is None:
            raw_targets = []

        if isinstance(raw_targets, list) and all(isinstance(t, TargetPin) for t in raw_targets):
            parsed_targets = list(raw_targets)
        elif target_format == "kongying":
            parsed_targets = KongyingAdapter.parse_markers(raw_targets, map_id=map_id)
        elif target_format == "appsample":
            parsed_targets = AppsampleAdapter.parse_markers(raw_targets, map_id=map_id)
        elif target_format == "genshinmap":
            parsed_targets = GenshinMapGithubAdapter.parse_markers(raw_targets, map_id=map_id)
        else:
            raise ValueError(f"Unsupported target_format: {target_format!r}")

        affine = None
        if isinstance(req_data.get("affine"), dict):
            affine = AxisAffine.from_dict(req_data["affine"])
            if affine.map_id is None:
                affine = AxisAffine(
                    ax=affine.ax,
                    bx=affine.bx,
                    cy=affine.cy,
                    dy=affine.dy,
                    map_id=map_id,
                    rmse=affine.rmse,
                    max_residual=affine.max_residual,
                    anchor_count=affine.anchor_count,
                    source=affine.source,
                )
        elif isinstance(req_data.get("src_anchors"), list) and isinstance(req_data.get("dst_anchors"), list):
            affine = fit_axis_affine(req_data["src_anchors"], req_data["dst_anchors"], map_id=map_id)
        else:
            affine = PerMapAffineRegistry().get(target_format, map_id, allow_default_seed=True)
            if affine is None and req_data.get("use_identity_affine"):
                affine = AxisAffine(ax=1.0, bx=0.0, cy=1.0, dy=0.0, map_id=map_id, source="identity")

        if affine is None:
            return {
                "ok": False,
                "format": target_format,
                "payload": None,
                "report": None,
                "error": f"No calibrated affine transform available for target={target_format!r}, map_id={map_id}",
            }

        try:
            max_dist = float(req_data.get("max_dist", 15.0))
        except (TypeError, ValueError):
            max_dist = 15.0
        include_ambiguous = bool(req_data.get("include_ambiguous", True))

        transfer = match_pins_one_to_one(
            source_pins,
            parsed_targets,
            affine=affine,
            max_dist=max_dist,
            missing_from_point_list=missing_ids,
        )

        if target_format == "kongying":
            payload = KongyingAdapter.export(transfer, include_ambiguous=include_ambiguous)
        elif target_format == "appsample":
            payload = AppsampleAdapter.export(transfer, include_ambiguous=include_ambiguous)
        else:
            payload = GenshinMapGithubAdapter.export(transfer, include_ambiguous=include_ambiguous)

        return {
            "ok": True,
            "format": target_format,
            "payload": payload,
            "report": transfer.summary_dict(),
        }

    def has_active_client(self, max_age_s: float = 25.0) -> bool:
        """Returns True if at least one Web Map tab/extension client recently polled or is waiting."""
        if self._pending_responses:
            return True
        if self._last_client_poll_ts <= 0.0:
            return False
        return (time.monotonic() - self._last_client_poll_ts) <= max_age_s

    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        payload: Any,
        cors_headers: str,
        status_line: str = "200 OK",
    ) -> None:
        body_bytes_out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = (
            f"HTTP/1.1 {status_line}\r\n{cors_headers}"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes_out)}\r\n\r\n"
        ).encode("utf-8")
        writer.write(header + body_bytes_out)
        await writer.drain()
        await self._safe_close(writer)

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
            route = path.split("?", 1)[0]

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

            try:
                content_length = max(0, int(headers.get("content-length", 0)))
            except (TypeError, ValueError):
                content_length = 0
            body_bytes = b""
            if content_length > 0:
                read_timeout = 10.0 if content_length > 262144 else 3.0
                try:
                    body_bytes = await asyncio.wait_for(reader.readexactly(content_length), timeout=read_timeout)
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

            # Status endpoint
            if path.startswith("/status"):
                self._last_client_poll_ts = time.monotonic()
                hotkey_info = {
                    "hotkey": "alt+f11",
                    "display_name": "ALT+F11",
                    "hotkey_sync": "alt+f11",
                    "display_name_sync": "ALT+F11",
                    "hotkey_debug": "alt+f12",
                    "display_name_debug": "ALT+F12",
                    "hotkey_switch": "alt+q",
                    "display_name_switch": "Alt + Q",
                    "auto_sync_m": True,
                    "single_monitor_mode": False,
                    "monitor_count": 1,
                    "engine_active": True,
                    "language": "auto",
                    "minimap_tracking": True,
                    "nav_hud_enabled": True,
                    "nav_hud_position": "under_minimap",
                    "lan_companion_running": False,
                    "last_sync": self.last_sync_info,
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
                    "version": APP_VERSION,
                    "script_version": self.SCRIPT_VERSION,
                    "queue_len": len(self._event_queue),
                    "latest_seq": self._event_seq,
                    **hotkey_info
                }
                await self._send_json(writer, status_payload, cors_headers)
                return

            # Engine Active <-> Standby toggle endpoint
            if path.startswith("/set_engine_state"):
                self._last_client_poll_ts = time.monotonic()
                req_data = {}
                if body_bytes:
                    try:
                        req_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
                active = bool(req_data.get("active", True))
                if self.on_engine_state_change:
                    try:
                        active = bool(self.on_engine_state_change(active))
                    except Exception as e:
                        logger.error(f"Error updating engine_state: {e}")
                self.broadcast({"type": "ENGINE_STATE_UPDATED", "engine_active": active})
                await self._send_json(writer, {"ok": True, "engine_active": active}, cors_headers)
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
                await self._send_json(writer, {"ok": True, "auto_sync_m": enabled}, cors_headers)
                return

            # Single-Monitor Mode toggle endpoint
            if path.startswith("/set_single_monitor"):
                req_data = {}
                if body_bytes:
                    try:
                        req_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
                enabled = bool(req_data.get("single_monitor_mode", False))
                if self.on_single_monitor_change:
                    try:
                        enabled = bool(self.on_single_monitor_change(enabled))
                    except Exception as e:
                        logger.error(f"Error updating single_monitor_mode: {e}")
                self.broadcast({"type": "SINGLE_MONITOR_UPDATED", "single_monitor_mode": enabled})
                await self._send_json(writer, {"ok": True, "single_monitor_mode": enabled}, cors_headers)
                return

            # Action Feedback endpoint for Single-Monitor HUD Toast & Audio
            if path.startswith("/action_feedback"):
                self._last_client_poll_ts = time.monotonic()
                req_data = {}
                if body_bytes:
                    try:
                        req_data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    except Exception:
                        req_data = {}
                if not isinstance(req_data, dict):
                    req_data = {}

                event_type = str(req_data.get("event_type", "") or "").strip()
                status = str(req_data.get("status", "") or "").strip()
                try:
                    seq = int(req_data.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0

                now = time.monotonic()
                deduped = False
                if seq > 0:
                    for prev_seq, prev_ev, prev_st, prev_ts in self._feedback_seen:
                        if (now - prev_ts) > 3.0 or prev_seq != seq:
                            continue
                        if prev_st == status or (event_type == "SYNC_AND_MARK" and prev_ev == "SYNC_AND_MARK"):
                            deduped = True
                            break
                    if not deduped:
                        self._feedback_seen.append((seq, event_type, status, now))

                shown = False
                if not deduped and self.on_action_feedback:
                    try:
                        shown = bool(self.on_action_feedback(req_data))
                    except Exception as e:
                        logger.error(f"Error handling action_feedback: {e}")

                await self._send_json(writer, {"ok": True, "shown": shown, "deduped": deduped}, cors_headers)
                return

            # Quick-Switch between Game and Browser Map
            if path.startswith("/quick_switch"):
                target = "none"
                if self.on_quick_switch:
                    try:
                        target = str(self.on_quick_switch() or "none")
                    except Exception as e:
                        logger.error(f"Error handling quick_switch: {e}")
                await self._send_json(writer, {"ok": True, "target": target}, cors_headers)
                return

            # Loopback-only graceful shutdown endpoint
            if path.startswith("/shutdown"):
                peer = writer.get_extra_info("peername")
                peer_ip = peer[0] if isinstance(peer, tuple) and peer else ""
                if method != "POST" or peer_ip not in ("127.0.0.1", "::1"):
                    await self._send_json(writer, {"ok": False, "error": "Forbidden"}, cors_headers, status_line="403 Forbidden")
                    return

                await self._send_json(writer, {"ok": True, "shutting_down": True}, cors_headers)
                if self.on_shutdown_request:
                    self._loop.call_soon(self.on_shutdown_request)
                else:
                    self._loop.create_task(self.stop())
                return

            # Set Hotkey endpoint (supports single, action-based, or batch {hotkey_sync, hotkey_debug, hotkey_switch})
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
                # Case 1: Batch sync / debug / switch update
                if any(f"hotkey_{act}" in req_data for act in _HOTKEY_ACTIONS):
                    for act in _HOTKEY_ACTIONS:
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

                await self._send_json(writer, {"ok": True, "results": results}, cors_headers)
                return

            # Report state endpoint (called by the MV3 extension to sync live status)
            if path.startswith("/report_state"):
                self._last_client_poll_ts = time.monotonic()
                if body_bytes:
                    try:
                        data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                        self.last_browser_state.update(data)
                    except Exception:
                        pass
                await self._send_json(writer, {}, cors_headers)
                return

            # Get browser state endpoint (called by test scripts)
            if path.startswith("/browser_state"):
                await self._send_json(writer, self.last_browser_state, cors_headers)
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

                await self._send_json(writer, {"ok": True, "bundle": bundle_res}, cors_headers)
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
                await self._send_json(writer, note_res, cors_headers, status_line=status_line)
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
                await self._send_json(writer, {"ok": True, "broadcasted": payload}, cors_headers)
                return

            # v5: Bilingual language switcher endpoint
            if route == "/set_language":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                raw_lang = str(req_data.get("language", "auto") or "auto").strip().lower()
                if raw_lang not in _VALID_LANGUAGES:
                    await self._send_json(
                        writer,
                        {"ok": False, "error": f"Invalid language {raw_lang!r}; expected 'vi', 'en', or 'auto'"},
                        cors_headers,
                        status_line="400 Bad Request",
                    )
                    return

                from src.i18n import resolve_language, set_language

                language = raw_lang
                resolved = resolve_language(language)
                if self.on_language_change:
                    try:
                        cb_res = self._call_cb(self.on_language_change, (raw_lang,), req_data)
                        if inspect.isawaitable(cb_res):
                            cb_res = await cb_res
                        if isinstance(cb_res, dict):
                            if cb_res.get("language"):
                                language = str(cb_res["language"]).strip().lower()
                            resolved = str(cb_res.get("resolved") or resolve_language(language))
                        elif isinstance(cb_res, str) and cb_res.strip():
                            language = cb_res.strip().lower()
                            resolved = resolve_language(language)
                    except Exception as e:
                        logger.error(f"Error updating language: {e}")
                else:
                    resolved = set_language(language)

                self.broadcast({
                    "type": "LANGUAGE_UPDATED",
                    "language": language,
                    "resolved": resolved,
                })
                await self._send_json(
                    writer,
                    {"ok": True, "language": language, "resolved": resolved},
                    cors_headers,
                )
                return

            # v5: Minimap continuous tracking toggle endpoint
            if route == "/set_minimap_tracking":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                enabled = bool(req_data.get("enabled", req_data.get("minimap_tracking", True)))
                if self.on_minimap_tracking_change:
                    try:
                        cb_res = self._call_cb(self.on_minimap_tracking_change, (enabled,), req_data)
                        if inspect.isawaitable(cb_res):
                            cb_res = await cb_res
                        if isinstance(cb_res, dict):
                            enabled = bool(cb_res.get("enabled", cb_res.get("minimap_tracking", enabled)))
                        elif cb_res is not None:
                            enabled = bool(cb_res)
                    except Exception as e:
                        logger.error(f"Error updating minimap_tracking: {e}")
                self.broadcast({
                    "type": "MINIMAP_TRACKING_UPDATED",
                    "enabled": enabled,
                    "minimap_tracking": enabled,
                })
                await self._send_json(
                    writer,
                    {"ok": True, "enabled": enabled, "minimap_tracking": enabled},
                    cors_headers,
                )
                return

            # v5: Mini-HUD Navigator config endpoint
            if route == "/set_nav_hud":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                enabled_in = req_data.get("enabled", req_data.get("nav_hud_enabled"))
                enabled_val = bool(enabled_in) if enabled_in is not None else None
                pos_in = req_data.get("position", req_data.get("nav_hud_position"))
                pos_val = str(pos_in).strip().lower() if pos_in is not None and str(pos_in).strip() else None
                cb_res_holder: Dict[str, Any] = {}

                if self.on_nav_hud_change:
                    try:
                        cb_res = self._call_cb(self.on_nav_hud_change, (enabled_val, pos_val, req_data), req_data)
                        if inspect.isawaitable(cb_res):
                            cb_res = await cb_res
                        cb_res_holder["res"] = cb_res
                        if isinstance(cb_res, dict):
                            if "enabled" in cb_res:
                                enabled_val = bool(cb_res["enabled"])
                            elif "nav_hud_enabled" in cb_res:
                                enabled_val = bool(cb_res["nav_hud_enabled"])
                            if cb_res.get("position") is not None:
                                pos_val = str(cb_res["position"]).strip().lower()
                            elif cb_res.get("nav_hud_position") is not None:
                                pos_val = str(cb_res["nav_hud_position"]).strip().lower()
                        elif isinstance(cb_res, tuple) and len(cb_res) >= 2:
                            enabled_val = bool(cb_res[0])
                            if cb_res[1] is not None:
                                pos_val = str(cb_res[1]).strip().lower()
                        elif isinstance(cb_res, bool):
                            enabled_val = cb_res
                    except Exception as e:
                        logger.error(f"Error updating nav_hud: {e}")

                final_enabled = True if enabled_val is None else bool(enabled_val)
                final_position = pos_val or "under_minimap"
                extra: Dict[str, Any] = {}
                if isinstance(cb_res_holder.get("res"), dict):
                    for k in ("custom_pos", "edit_mode"):
                        if k in cb_res_holder["res"]:
                            extra[k] = cb_res_holder["res"][k]
                self.broadcast({
                    "type": "NAV_HUD_UPDATED",
                    "enabled": final_enabled,
                    "position": final_position,
                    "nav_hud_enabled": final_enabled,
                    "nav_hud_position": final_position,
                    **extra,
                })
                await self._send_json(
                    writer,
                    {"ok": True, "enabled": final_enabled, "position": final_position, **extra},
                    cors_headers,
                )
                return

            # v5: Browser-supplied nearby uncollected pins feed for Mini-HUD & Sticky Target
            if route == "/nearby_pins":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                raw_pins = req_data.get("pins")
                pins = raw_pins if isinstance(raw_pins, list) else []
                try:
                    map_id = int(req_data.get("map_id", 2))
                except (TypeError, ValueError):
                    map_id = 2
                floor_id = req_data.get("floor_id")
                count = len(pins)

                if self.on_nearby_pins:
                    try:
                        cb_res = self._call_cb(
                            self.on_nearby_pins,
                            (pins, map_id, floor_id, req_data),
                            req_data,
                            single_arg_default=req_data,
                            use_single_arg_default=True,
                        )
                        if inspect.isawaitable(cb_res):
                            cb_res = await cb_res
                        if isinstance(cb_res, dict) and "count" in cb_res:
                            count = int(cb_res["count"])
                        elif isinstance(cb_res, int) and not isinstance(cb_res, bool):
                            count = cb_res
                    except Exception as e:
                        logger.error(f"Error handling nearby_pins: {e}")

                await self._send_json(writer, {"ok": True, "count": count}, cors_headers)
                return

            # v5: Navigation controller command endpoint
            if route == "/nav_command":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                action = str(req_data.get("action", "") or "").strip().lower()
                if action not in _VALID_NAV_ACTIONS:
                    await self._send_json(
                        writer,
                        {"ok": False, "action": action, "error": f"Unsupported nav action: {action!r}"},
                        cors_headers,
                        status_line="400 Bad Request",
                    )
                    return

                if self.on_nav_command:
                    try:
                        cb_res = self._call_cb(
                            self.on_nav_command,
                            (action, req_data),
                            req_data,
                            single_arg_default=action,
                            use_single_arg_default=True,
                        )
                        if inspect.isawaitable(cb_res):
                            cb_res = await cb_res
                        if isinstance(cb_res, dict):
                            res_payload = dict(cb_res)
                            res_payload.setdefault("ok", True)
                            res_payload.setdefault("action", action)
                        else:
                            res_payload = {
                                "ok": True if cb_res is None else bool(cb_res),
                                "action": action,
                            }
                    except Exception as e:
                        logger.error(f"Error handling nav_command ({action}): {e}")
                        res_payload = {"ok": False, "action": action, "error": str(e)}
                else:
                    res_payload = {"ok": False, "action": action, "error": "Navigation controller not configured"}

                await self._send_json(writer, res_payload, cors_headers)
                return

            # v5: TSP Smart Route Planner endpoint (with direct src.tsp_route fallback)
            if route == "/plan_tsp_route":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                raw_pins = req_data.get("pins")
                pins = raw_pins if isinstance(raw_pins, list) else []
                start_pos = req_data.get("start_pos")
                try:
                    map_id = int(req_data.get("map_id", 2))
                except (TypeError, ValueError):
                    map_id = 2
                try:
                    teleport_penalty = float(req_data.get("teleport_penalty", 180.0))
                except (TypeError, ValueError):
                    teleport_penalty = 180.0

                try:
                    if self.on_plan_tsp_route:
                        if inspect.iscoroutinefunction(self.on_plan_tsp_route):
                            route_res = await self._call_cb(
                                self.on_plan_tsp_route,
                                (pins, start_pos, map_id, teleport_penalty),
                                req_data,
                            )
                        else:
                            route_res = await asyncio.to_thread(
                                self._call_cb,
                                self.on_plan_tsp_route,
                                (pins, start_pos, map_id, teleport_penalty),
                                req_data,
                            )
                    else:
                        from src.tsp_route import load_waypoints_for_map, plan_route

                        def _run_default_tsp():
                            wps = req_data.get("waypoints")
                            if wps is None:
                                wps = load_waypoints_for_map(map_id)
                            return plan_route(
                                pins,
                                start_pos=start_pos,
                                waypoints=wps,
                                teleport_penalty=teleport_penalty,
                            )

                        route_res = await asyncio.to_thread(_run_default_tsp)
                except Exception as e:
                    logger.error(f"Error planning TSP route: {e}")
                    await self._send_json(
                        writer,
                        {"ok": False, "error": str(e)},
                        cors_headers,
                        status_line="400 Bad Request",
                    )
                    return

                if not isinstance(route_res, dict):
                    route_res = {"ordered_pins": [], "legs": [], "teleport_hints": []}

                response_payload = {"ok": True, "map_id": map_id, **route_res}
                self.broadcast({
                    "type": "TSP_ROUTE_UPDATED",
                    "map_id": map_id,
                    **route_res,
                })
                await self._send_json(writer, response_payload, cors_headers)
                return

            # v5: LAN Companion server control endpoint
            if route == "/lan_companion":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                action = str(req_data.get("action", "status") or "status").strip().lower()
                if action not in _VALID_LAN_ACTIONS:
                    await self._send_json(
                        writer,
                        {
                            "ok": False,
                            "running": False,
                            "url": "",
                            "pin": "",
                            "qr_svg": "",
                            "error": f"Unsupported lan_companion action: {action!r}",
                        },
                        cors_headers,
                        status_line="400 Bad Request",
                    )
                    return

                lan_res: Dict[str, Any] = {
                    "ok": False,
                    "running": False,
                    "url": "",
                    "pin": "",
                    "qr_svg": "",
                }
                if self.on_lan_companion_action:
                    try:
                        cb_res = self._call_cb(
                            self.on_lan_companion_action,
                            (action,),
                            req_data,
                            single_arg_default=action,
                            use_single_arg_default=True,
                        )
                        if inspect.isawaitable(cb_res):
                            cb_res = await cb_res
                        if isinstance(cb_res, dict):
                            lan_res["ok"] = bool(cb_res.get("ok", True))
                            lan_res["running"] = bool(cb_res.get("running", False))
                            lan_res["url"] = str(cb_res.get("url") or cb_res.get("companion_url") or "")
                            lan_res["pin"] = str(cb_res.get("pin") or "")
                            lan_res["qr_svg"] = str(cb_res.get("qr_svg") or "")
                            for k, v in cb_res.items():
                                if k not in lan_res:
                                    lan_res[k] = v
                    except Exception as e:
                        logger.error(f"Error handling lan_companion ({action}): {e}")
                        lan_res["ok"] = False
                        lan_res["error"] = str(e)
                else:
                    lan_res["ok"] = action == "status"
                    if action != "status":
                        lan_res["error"] = "LAN companion handler not configured"

                await self._send_json(writer, lan_res, cors_headers)
                return

            # v5: 1-Click Borderless Windowed Game Launcher endpoint
            if route == "/launch_borderless":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                launch_res: Dict[str, Any] = {
                    "ok": False,
                    "launched": False,
                    "message": "Borderless launcher not configured",
                }
                if self.on_launch_borderless:
                    try:
                        cb_res = self._call_cb(self.on_launch_borderless, (req_data,), req_data)
                        if inspect.isawaitable(cb_res):
                            cb_res = await cb_res
                        if isinstance(cb_res, dict):
                            ok_val = bool(cb_res.get("ok", False))
                            launched_val = bool(
                                cb_res.get("launched", ok_val and not cb_res.get("dry_run", False))
                            )
                            msg_val = str(
                                cb_res.get("message")
                                or cb_res.get("error")
                                or ("Launched" if launched_val else "")
                            )
                            launch_res = {
                                **cb_res,
                                "ok": ok_val,
                                "launched": launched_val,
                                "message": msg_val,
                            }
                        elif isinstance(cb_res, tuple) and len(cb_res) >= 1:
                            ok_val = bool(cb_res[0])
                            msg_val = str(cb_res[1]) if len(cb_res) > 1 else ""
                            launch_res = {"ok": ok_val, "launched": ok_val, "message": msg_val}
                        else:
                            ok_val = bool(cb_res)
                            launch_res = {"ok": ok_val, "launched": ok_val, "message": ""}
                    except Exception as e:
                        logger.error(f"Error handling launch_borderless: {e}")
                        launch_res = {"ok": False, "launched": False, "message": str(e)}

                await self._send_json(writer, launch_res, cors_headers)
                return

            # v5: Cross-Map Pin Exporter & Bridge endpoint (with direct src.map_bridge fallback)
            if route == "/cross_map_export":
                self._last_client_poll_ts = time.monotonic()
                req_data = self._parse_json_body(body_bytes)
                target_format = str(req_data.get("target_format") or req_data.get("format") or "canonical").strip().lower()
                if target_format not in _VALID_EXPORT_FORMATS:
                    await self._send_json(
                        writer,
                        {
                            "ok": False,
                            "format": target_format,
                            "payload": None,
                            "report": None,
                            "error": f"Unsupported target_format: {target_format!r}",
                        },
                        cors_headers,
                        status_line="400 Bad Request",
                    )
                    return

                try:
                    if self.on_cross_map_export:
                        try:
                            map_id = int(req_data.get("map_id", 2))
                        except (TypeError, ValueError):
                            map_id = 2
                        pos_args = (
                            req_data.get("point_list"),
                            req_data.get("marked_list"),
                            map_id,
                            target_format,
                            req_data.get("target_pins"),
                        )
                        if inspect.iscoroutinefunction(self.on_cross_map_export):
                            exp_res = await self._call_cb(self.on_cross_map_export, pos_args, req_data)
                        else:
                            exp_res = await asyncio.to_thread(
                                self._call_cb, self.on_cross_map_export, pos_args, req_data
                            )
                    else:
                        exp_res = await asyncio.to_thread(self._run_default_cross_map_export, req_data)
                except Exception as e:
                    logger.error(f"Error handling cross_map_export ({target_format}): {e}")
                    await self._send_json(
                        writer,
                        {
                            "ok": False,
                            "format": target_format,
                            "payload": None,
                            "report": None,
                            "error": str(e),
                        },
                        cors_headers,
                        status_line="400 Bad Request",
                    )
                    return

                if isinstance(exp_res, dict):
                    out_payload = {
                        "ok": bool(exp_res.get("ok", True)),
                        "format": str(exp_res.get("format") or target_format),
                        "payload": exp_res.get("payload"),
                        "report": exp_res.get("report"),
                    }
                    for k, v in exp_res.items():
                        if k not in out_payload:
                            out_payload[k] = v
                else:
                    out_payload = {
                        "ok": True,
                        "format": target_format,
                        "payload": exp_res,
                        "report": None,
                    }

                await self._send_json(writer, out_payload, cors_headers)
                return

            # Long-polling endpoint for the MV3 Extension
            if path.startswith("/poll"):
                self._last_client_poll_ts = time.monotonic()
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
                        event_data = self._next_event_since(since_seq)
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

                await self._send_json(writer, event_data, cors_headers)
                return

            # Default 404
            writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await self._safe_close(writer)

        except Exception as e:
            logger.debug(f"HTTP handler exception: {e}")
            await self._safe_close(writer)

    def _next_event_since(self, since_seq: int) -> Optional[Dict[str, Any]]:
        """Returns the earliest pending event with `_seq > since_seq` across durable log and coalesced ephemeral slots."""
        best_seq: Optional[int] = None
        best_payload: Optional[Dict[str, Any]] = None

        for seq, payload in self._event_log:
            if seq > since_seq:
                best_seq = seq
                best_payload = payload
                break

        for seq, payload in self._ephemeral_events.values():
            if seq > since_seq and (best_seq is None or seq < best_seq):
                best_seq = seq
                best_payload = payload

        return best_payload

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
        self._loop = asyncio.get_running_loop()
        freed_existing = False
        for attempt in range(5):
            try:
                self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
                break
            except OSError as e:
                if getattr(e, "errno", None) == 10048:
                    if not freed_existing and self.port != 0:
                        freed_existing = True
                        await asyncio.to_thread(self._free_port_if_busy)
                        continue
                    if attempt < 4:
                        logger.info(f"Port {self.port} in TIME_WAIT. Retrying in 1s (attempt {attempt+1}/5)...")
                        await asyncio.sleep(1.0)
                        continue
                raise
        if self._server and self._server.sockets:
            self.port = int(self._server.sockets[0].getsockname()[1])
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
        self._ephemeral_events.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("Sync Server stopped.")

    def broadcast(self, message: dict, feedback_handled: bool = False, ephemeral: bool = False):
        """Thread-safe event broadcast with multi-tab cursor log, ephemeral coalescing & legacy Event Queue."""
        if not self.is_running or not self._loop:
            return

        def _dispatch():
            self._event_seq += 1
            stamped = dict(message)
            stamped["_seq"] = self._event_seq
            msg_type = str(stamped.get("type", "UNKNOWN") or "UNKNOWN")

            if ephemeral:
                # Store only the latest event per ephemeral type outside _event_log so rapid
                # 10Hz MINIMAP_POSITION frames never evict durable Map M / SYNC_AND_MARK events.
                self._ephemeral_events[msg_type] = (self._event_seq, stamped)
            else:
                self._event_log.append((self._event_seq, stamped))

            if msg_type in ("SYNC_POSITION_ONLY", "SYNC_AND_MARK") and stamped.get("has_world_coords"):
                self.last_sync_info = {
                    "map_id": stamped.get("map_id"),
                    "map_name": stamped.get("map_name") or "",
                    "area_name": stamped.get("area_name") or "",
                    "lat": stamped.get("lat"),
                    "lng": stamped.get("lng"),
                    "is_underground": bool(stamped.get("is_underground")),
                    "floor": stamped.get("hoyolab_floor_name") or stamped.get("floor_short") or stamped.get("floor_name") or "G",
                    "ts": time.time(),
                }
            if feedback_handled and self._event_seq > 0:
                self._feedback_seen.append((self._event_seq, msg_type, "local_handled", time.monotonic()))

            count = len(self._pending_responses)
            if count > 0:
                if ephemeral:
                    logger.debug(
                        f"==> Broadcasted ephemeral [{msg_type}] #{self._event_seq} to {count} waiting browser tab(s)"
                    )
                else:
                    logger.info(
                        f"==> Broadcasted [{msg_type}] #{self._event_seq} to {count} waiting browser tab(s)!"
                    )
                for fut in list(self._pending_responses):
                    if not fut.done():
                        fut.set_result(stamped)
            else:
                if ephemeral:
                    replaced = False
                    for idx, existing in enumerate(self._event_queue):
                        if existing.get("type") == msg_type:
                            self._event_queue[idx] = stamped
                            replaced = True
                            break
                    if not replaced:
                        self._event_queue.append(stamped)
                    logger.debug(
                        f"==> Coalesced ephemeral [{msg_type}] #{self._event_seq} "
                        f"(Pending polls: 0, Queue size: {len(self._event_queue)})"
                    )
                else:
                    self._event_queue.append(stamped)
                    logger.info(
                        f"==> Queued [{msg_type}] #{self._event_seq} "
                        f"(Pending polls: 0, Queue size: {len(self._event_queue)})"
                    )

        self._loop.call_soon_threadsafe(_dispatch)

    def broadcast_ephemeral(self, message: dict):
        """Thread-safe coalesced broadcast for high-frequency events (e.g. 10Hz MINIMAP_POSITION).
        Never evicts durable Map M / SYNC_AND_MARK events from the lossless _event_log ring buffer."""
        self.broadcast(message, feedback_handled=False, ephemeral=True)

SyncServer = DualSyncServer
