"""
Verification suite for Track 3B (Genshin-Helper v5):
- F7 TSP Smart Route Planner (`src/tsp_route.py`)
- F6 Same-Origin LAN Companion Server + Offline QR (`src/lan_companion.py`)
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import math
import os
import random
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
import websockets

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.lan_companion import (
    LanCompanionServer,
    compute_ws_accept_key,
    detect_lan_ipv4,
    generate_qr_matrix,
    generate_qr_svg,
)
from src.tsp_route import (
    _build_distance_matrix,
    load_waypoints_for_map,
    nearest_neighbor_order,
    path_length,
    plan_route,
    two_opt_improve,
)


# ---------------------------------------------------------------------------
# F7 — TSP Smart Route Planner Tests
# ---------------------------------------------------------------------------


def verify_tsp_50_pins_speed_and_2opt() -> None:
    rng = random.Random(20260501)
    pins: List[Dict[str, Any]] = []
    for i in range(50):
        pins.append(
            {
                "id": 1000 + i,
                "lat": rng.uniform(-1200.0, 1200.0),
                "lng": rng.uniform(-1200.0, 1200.0),
                "name": f"Oculus #{i + 1}",
                "label_id": 12,
                "floor_id": 1 if i % 7 == 0 else 0,
                "is_underground": (i % 7 == 0),
            }
        )

    start_pos = (0.0, 0.0)
    t0 = time.perf_counter()
    result = plan_route(pins, start_pos=start_pos, waypoints=None, teleport_penalty=180.0, max_2opt_iters=200)
    wall_ms = (time.perf_counter() - t0) * 1000.0

    assert wall_ms < 15.0, f"50-pin TSP exceeded 15 ms budget: {wall_ms:.2f} ms"
    assert result["pin_count"] == 50
    assert len(result["ordered_pins"]) == 50
    assert {p["id"] for p in result["ordered_pins"]} == {p["id"] for p in pins}
    assert [p["step"] for p in result["ordered_pins"]] == list(range(1, 51))
    assert len(result["legs"]) == 50  # start_pos -> pin1 -> ... -> pin50
    assert result["teleport_count"] == 0
    assert result["raw_walk_distance"] <= result["nn_distance"] + 1e-6, (
        f"2-opt worsened NN path: opt={result['raw_walk_distance']} vs nn={result['nn_distance']}"
    )
    assert math.isclose(result["total_distance"], result["raw_walk_distance"], rel_tol=1e-6)

    # Confirm 2-opt strictly improves on an explicit crossing path
    coords = np.array(
        [
            [0.0, 0.0],
            [100.0, 100.0],
            [0.0, 100.0],
            [100.0, 0.0],
            [200.0, 100.0],
            [200.0, 0.0],
        ],
        dtype=np.float64,
    )
    dmat = _build_distance_matrix(coords)
    crossed = [0, 1, 2, 3, 4, 5]
    crossed_len = path_length(crossed, dmat)
    improved, iters = two_opt_improve(crossed, dmat, fix_first=True, max_iters=50)
    improved_len = path_length(improved, dmat)
    assert improved_len < crossed_len - 1.0, (
        f"Expected strict 2-opt improvement on crossing path: {improved_len} vs {crossed_len}"
    )
    assert iters >= 1

    # Confirm 2-opt strictly improves or equals NN across 10 random 50-pin trials
    strict_gains = 0
    for seed in range(10):
        r2 = random.Random(seed + 42)
        pts = np.array([[r2.uniform(0, 1000), r2.uniform(0, 1000)] for _ in range(50)], dtype=np.float64)
        dm = _build_distance_matrix(pts)
        nn = nearest_neighbor_order(dm, start_idx=0, fixed_start=True)
        opt, _ = two_opt_improve(nn, dm, fix_first=True, max_iters=200)
        d_nn = path_length(nn, dm)
        d_opt = path_length(opt, dm)
        assert d_opt <= d_nn + 1e-6
        if d_opt < d_nn - 1e-3:
            strict_gains += 1
    assert strict_gains >= 5, f"Expected 2-opt to strictly improve most random 50-pin tours, got {strict_gains}/10"

    print(
        f"  [OK] TSP 50-pin runtime={wall_ms:.2f} ms (<15 ms), "
        f"nn={result['nn_distance']:.1f} -> 2-opt={result['raw_walk_distance']:.1f}, "
        f"strict gains={strict_gains}/10"
    )


def verify_tsp_teleport_leg_splitting_and_edge_cases() -> None:
    # Empty & single-pin edge cases
    empty = plan_route([])
    assert empty["ordered_pins"] == [] and empty["legs"] == [] and empty["total_distance"] == 0.0

    single = plan_route(
        [{"id": "p1", "lat": 10.0, "lng": 20.0, "name": "Solo Chest", "label_id": 1, "floor_id": 0, "is_underground": False}],
        start_pos={"lat": 10.0, "lng": 20.0},
    )
    assert single["pin_count"] == 1 and len(single["legs"]) == 1 and single["total_distance"] == 0.0

    # Two distant islands separated by ~2500 units with waypoints on both islands
    island_a = [
        {"id": "A1", "lat": 0.0, "lng": 0.0, "name": "Mondstadt Chest 1", "label_id": 17, "floor_id": 0, "is_underground": False},
        {"id": "A2", "lat": 25.0, "lng": 15.0, "name": "Mondstadt Chest 2", "label_id": 17, "floor_id": 0, "is_underground": False},
        {"id": "A3", "lat": 50.0, "lng": 5.0, "name": "Mondstadt Chest 3", "label_id": 17, "floor_id": 0, "is_underground": False},
    ]
    island_b = [
        {"id": "B1", "lat": 2500.0, "lng": 2500.0, "name": "Inazuma Chest 1", "label_id": 17, "floor_id": 2, "is_underground": True},
        {"id": "B2", "lat": 2530.0, "lng": 2510.0, "name": "Inazuma Chest 2", "label_id": 17, "floor_id": 2, "is_underground": True},
        {"id": "B3", "lat": 2515.0, "lng": 2545.0, "name": "Inazuma Chest 3", "label_id": 17, "floor_id": 0, "is_underground": False},
    ]
    waypoints = [
        {"id": "WP_A", "x_pos": 10.0, "y_pos": -5.0, "name": "Mondstadt Waypoint"},
        {"id": "WP_B", "x_pos": 2492.0, "y_pos": 2496.0, "name": "Inazuma Statue"},
    ]

    route = plan_route(
        island_a + island_b,
        start_pos=(0.0, -5.0),
        waypoints=waypoints,
        teleport_penalty=180.0,
    )

    tp_legs = [leg for leg in route["legs"] if leg["is_teleport"]]
    assert len(tp_legs) == 1, f"Expected exactly 1 inter-island teleport leg, got {len(tp_legs)}"
    assert tp_legs[0]["waypoint_id"] == "WP_B"
    assert tp_legs[0]["distance"] == 0.0
    assert tp_legs[0]["effective_cost"] == 180.0
    assert len(route["teleport_hints"]) == 1
    assert route["teleport_hints"][0]["waypoint_id"] == "WP_B"
    assert route["teleport_hints"][0]["saved_distance"] > 2000.0
    assert route["total_distance"] < route["raw_walk_distance"] - 2000.0
    assert route["effective_distance"] < route["raw_walk_distance"] - 1800.0

    # Verify floor_change flag on walk leg between underground B2 (floor 2) and surface B3 (floor 0)
    assert any(leg["floor_change"] for leg in route["legs"]), "Expected floor_change=True on multi-floor transition"

    # Verify start_pos=None also produces a valid open route with teleport splitting
    no_start_route = plan_route(island_a + island_b, start_pos=None, waypoints=waypoints, teleport_penalty=180.0)
    assert no_start_route["pin_count"] == 6
    assert no_start_route["teleport_count"] == 1
    assert no_start_route["total_distance"] < no_start_route["raw_walk_distance"] - 2000.0

    # Verify real dataset waypoint loader works with src/waypoints_db.json
    real_wps = load_waypoints_for_map(2)
    assert len(real_wps) > 100, f"Expected >100 Teyvat waypoints, got {len(real_wps)}"

    print(
        f"  [OK] Teleport leg splitting passed (saved={route['teleport_hints'][0]['saved_distance']:.1f} u, "
        f"loaded {len(real_wps)} Teyvat waypoints)"
    )


# ---------------------------------------------------------------------------
# F6 — Offline QR Code & LAN IP Detection Tests
# ---------------------------------------------------------------------------


def _decode_qr_matrix_with_cv2(matrix: List[List[bool]], quiet_zone: int = 4, scale: int = 8) -> str:
    n = len(matrix)
    total = n + 2 * quiet_zone
    img = np.full((total * scale, total * scale), 255, dtype=np.uint8)
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                y0 = (r + quiet_zone) * scale
                x0 = (c + quiet_zone) * scale
                img[y0 : y0 + scale, x0 : x0 + scale] = 0
    detector = cv2.QRCodeDetector()
    decoded, points, _ = detector.detectAndDecode(img)
    assert points is not None, "cv2.QRCodeDetector failed to detect QR finder geometry"
    return decoded


def verify_offline_qr_and_lan_ip() -> None:
    lan_ip = detect_lan_ipv4()
    ipaddress.IPv4Address(lan_ip)  # Raises ValueError if invalid

    # Test QR Versions 1 through 6 (including Version 6 with 2 interleaved RS blocks!)
    test_payloads = [
        (21, "http://10.0.0.1"),  # V1 (15 bytes <= 17)
        (25, "http://192.168.1.5:8766/c"),  # V2 (25 bytes <= 32)
        (29, "http://192.168.1.25:8766/companion?token=abc123456"),  # V3 (50 bytes <= 53)
        (33, "http://192.168.100.255:8766/companion?token=ABCDEFGHIJKLMNOPQRSTUV"),  # V4 (66 bytes <= 78)
        (37, "http://192.168.100.255:8766/companion?token=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_abcd"),  # V5 (85 bytes <= 106)
        (41, "http://192.168.100.255:8766/companion?token=" + ("X" * 80)),  # V6 (124 bytes <= 134, 2 RS blocks)
    ]
    for expected_size, text in test_payloads:
        mat = generate_qr_matrix(text)
        assert len(mat) == expected_size and all(len(row) == expected_size for row in mat), (
            f"Expected {expected_size}x{expected_size} QR matrix for len={len(text)}, got {len(mat)}"
        )
        decoded = _decode_qr_matrix_with_cv2(mat)
        assert decoded == text, f"QR decode mismatch: expected {text!r}, got {decoded!r}"

    # Verify SVG structure is valid XML and strictly self-contained
    sample_url = f"http://{lan_ip}:8766/companion?token=SampleToken1234567890A"
    svg = generate_qr_svg(sample_url)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "viewBox" in root.attrib
    assert "http://www.w3.org/2000/svg" in root.tag
    assert "<path" in svg and "<rect" in svg
    assert "cdn" not in svg.lower()

    print(f"  [OK] Offline QR V1-V6 decoded 6/6 via cv2.QRCodeDetector + valid SVG (LAN IP={lan_ip})")


# ---------------------------------------------------------------------------
# F6 — Live Same-Origin HTTP + RFC 6455 WebSocket Server Tests
# ---------------------------------------------------------------------------


def _http_request(url: str, *, method: str = "GET", body: bytes | None = None, headers: Dict[str, str] | None = None) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def _encode_client_masked_frame(opcode: int, payload: bytes) -> bytes:
    mask_key = os.urandom(4)
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    header.extend(mask_key)
    masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    return bytes(header) + masked


async def _read_server_ws_frame(reader: asyncio.StreamReader, timeout: float = 3.0) -> Tuple[int, bytes]:
    head = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    assert not masked, "RFC 6455 server->client frames must not be masked"
    if length == 126:
        length = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), timeout=timeout))[0]
    elif length == 127:
        length = struct.unpack("!Q", await asyncio.wait_for(reader.readexactly(8), timeout=timeout))[0]
    payload = await asyncio.wait_for(reader.readexactly(length), timeout=timeout) if length else b""
    return opcode, payload


async def _ws_raw_handshake(host: str, port: int, path: str) -> Tuple[int, Dict[str, str], asyncio.StreamReader, asyncio.StreamWriter, str]:
    reader, writer = await asyncio.open_connection(host, port)
    client_key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {client_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    writer.write(req)
    await writer.drain()

    status_line = await asyncio.wait_for(reader.readline(), timeout=3.0)
    parts = status_line.decode("ascii").strip().split()
    status_code = int(parts[1])
    headers: Dict[str, str] = {}
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=3.0)
        if not line or not line.strip():
            break
        k, v = line.decode("ascii", errors="ignore").strip().split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return status_code, headers, reader, writer, client_key


async def verify_lan_companion_server_async() -> None:
    # 1. Opt-in invariant: instantiating must NOT start server or bind any port
    srv = LanCompanionServer(host="127.0.0.1", port=0, max_hz=15.0)
    assert srv.is_running is False and srv._server is None
    assert len(srv.pin) == 6 and srv.pin.isdigit()
    assert len(srv.token) >= 20  # 128-bit urlsafe token = 22 chars
    assert srv.broadcast_state({"lat": 1.0, "lng": 2.0}) is False

    await srv.start()
    try:
        assert srv.is_running is True
        assert srv.port > 0
        base = f"http://127.0.0.1:{srv.port}"

        # 2. Auth rejection tests on /companion and /ws
        st_no_tok, _, _ = await asyncio.to_thread(_http_request, f"{base}/companion")
        assert st_no_tok in (401, 403), f"Expected 401/403 on missing /companion token, got {st_no_tok}"

        st_bad_tok, _, _ = await asyncio.to_thread(_http_request, f"{base}/companion?token=badtoken")
        assert st_bad_tok in (401, 403), f"Expected 401/403 on bad /companion token, got {st_bad_tok}"

        ws_bad_st, _, _, ws_bad_w, _ = await _ws_raw_handshake("127.0.0.1", srv.port, "/ws/badtoken")
        ws_bad_w.close()
        await ws_bad_w.wait_closed()
        assert ws_bad_st in (401, 403), f"Expected 401/403 on /ws/badtoken, got {ws_bad_st}"

        ws_missing_st, _, _, ws_miss_w, _ = await _ws_raw_handshake("127.0.0.1", srv.port, "/ws")
        ws_miss_w.close()
        await ws_miss_w.wait_closed()
        assert ws_missing_st in (401, 403), f"Expected 401/403 on /ws without token, got {ws_missing_st}"

        # 3. Valid /companion serves same-origin bilingual dark HTML5 viewer
        st_ok, hdrs_ok, html_bytes = await asyncio.to_thread(_http_request, srv.get_companion_url())
        assert st_ok == 200, f"Expected 200 on valid /companion, got {st_ok}"
        assert "text/html" in hdrs_ok.get("content-type", "")
        html_text = html_bytes.decode("utf-8")
        assert "<canvas" in html_text
        assert "/ws/" in html_text
        assert "Theo dõi" in html_text and "Follow" in html_text  # Bilingual VI/EN

        # 4. Ephemeral PIN auth endpoint
        bad_pin_st, _, _ = await asyncio.to_thread(
            _http_request,
            f"{base}/auth/pin",
            method="POST",
            body=json.dumps({"pin": "000000" if srv.pin != "000000" else "999999"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert bad_pin_st == 403
        good_pin_st, _, good_pin_body = await asyncio.to_thread(
            _http_request,
            f"{base}/auth/pin",
            method="POST",
            body=json.dumps({"pin": srv.pin}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert good_pin_st == 200
        pin_json = json.loads(good_pin_body.decode("utf-8"))
        assert pin_json["ok"] is True and pin_json["token"] == srv.token

        # 5. Server QR SVG encodes exact live companion URL and decodes cleanly
        qr_svg = srv.get_qr_svg()
        ET.fromstring(qr_svg)
        qr_decoded = _decode_qr_matrix_with_cv2(generate_qr_matrix(srv.get_companion_url()))
        assert qr_decoded == srv.get_companion_url()

        # 6. Real RFC 6455 WebSocket handshake + Ping/Pong + thread-safe broadcast_state
        ws_st, ws_hdrs, ws_r, ws_w, cli_key = await _ws_raw_handshake(
            "127.0.0.1", srv.port, f"/ws/{srv.token}"
        )
        assert ws_st == 101, f"Expected 101 Switching Protocols, got {ws_st}"
        assert ws_hdrs.get("sec-websocket-accept") == compute_ws_accept_key(cli_key)

        # Verify RFC 6455 Ping -> Pong
        ws_w.write(_encode_client_masked_frame(0x9, b"ping42"))
        await ws_w.drain()
        pong_op, pong_data = await _read_server_ws_frame(ws_r)
        assert pong_op == 0xA and pong_data == b"ping42"

        # Broadcast live state from a separate worker thread (simulating tracker thread)
        sample_payload = {
            "lat": -1420.5,
            "lng": 835.25,
            "heading": 127.5,
            "camera_heading": 140.0,
            "floor_name": "B1 — Chasm",
            "target_pin": {"id": 999, "name": "Luxurious Chest", "lat": -1410.0, "lng": 850.0},
            "nearby_pins": [{"id": 999, "lat": -1410.0, "lng": 850.0, "is_underground": True}],
            "route_legs": [
                {
                    "from_lat": -1420.5,
                    "from_lng": 835.25,
                    "to_lat": -1410.0,
                    "to_lng": 850.0,
                    "is_teleport": False,
                }
            ],
        }
        sent_ok: List[bool] = []
        t = threading.Thread(target=lambda: sent_ok.append(srv.broadcast_state(sample_payload)))
        t.start()
        t.join(timeout=2.0)
        assert sent_ok == [True]

        op, raw_json = await _read_server_ws_frame(ws_r)
        assert op == 0x1, f"Expected text opcode 0x1, got {op}"
        recv = json.loads(raw_json.decode("utf-8"))
        assert recv["lat"] == -1420.5
        assert recv["lng"] == 835.25
        assert recv["heading"] == 127.5
        assert recv["camera_heading"] == 140.0
        assert recv["floor_name"] == "B1 — Chasm"
        assert recv["target_pin"]["name"] == "Luxurious Chest"
        assert len(recv["route_legs"]) == 1

        # Verify trailing-edge coalescing under rapid burst: final lat must arrive!
        for step in range(5):
            srv.broadcast_state({"lat": float(step), "lng": 100.0 + step})
        op2, raw_json2 = await _read_server_ws_frame(ws_r, timeout=2.0)
        assert op2 == 0x1
        recv2 = json.loads(raw_json2.decode("utf-8"))
        assert recv2["lat"] == 4.0 and recv2["lng"] == 104.0

        # Independent RFC 6455 client (`websockets` pkg) verifies late-joiner snapshot + live push
        async with websockets.connect(f"ws://127.0.0.1:{srv.port}/ws/{srv.token}") as ws_ext:
            snapshot_raw = await asyncio.wait_for(ws_ext.recv(), timeout=2.0)
            snapshot = json.loads(snapshot_raw)
            assert snapshot["lat"] == 4.0 and snapshot["lng"] == 104.0

            srv.broadcast_state({"lat": 77.5, "lng": -88.25, "floor_name": "Surface"}, force=True)
            live_raw = await asyncio.wait_for(ws_ext.recv(), timeout=2.0)
            live_msg = json.loads(live_raw)
            assert live_msg["lat"] == 77.5 and live_msg["lng"] == -88.25

        # Clean client close
        ws_w.write(_encode_client_masked_frame(0x8, b""))
        await ws_w.drain()
        ws_w.close()
        await ws_w.wait_closed()
    finally:
        await srv.stop()
        assert srv.is_running is False

    # 7. Verify sync background start/stop helper for desktop UI toggle
    bg_srv = LanCompanionServer(host="127.0.0.1", port=0)
    info = bg_srv.start_in_background()
    try:
        assert info["is_running"] is True and info["port"] > 0
        st_health, _, body_health = await asyncio.to_thread(_http_request, f"http://127.0.0.1:{info['port']}/health")
        assert st_health == 200 and json.loads(body_health.decode("utf-8"))["ok"] is True
    finally:
        bg_srv.stop_background()
        assert bg_srv.is_running is False

    print("  [OK] LAN Companion HTTP/WS auth, same-origin HTML, Ping/Pong, websockets interop & background toggle passed")


def main() -> None:
    print("=== Verifying Track 3B: TSP Route Planner (F7) & LAN Companion + QR (F6) ===")
    verify_tsp_50_pins_speed_and_2opt()
    verify_tsp_teleport_leg_splitting_and_edge_cases()
    verify_offline_qr_and_lan_ip()
    asyncio.run(verify_lan_companion_server_async())
    print("=== ALL TRACK 3B CHECKS PASSED ===")


if __name__ == "__main__":
    main()
