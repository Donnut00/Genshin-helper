"""
LAN Companion Server (F6) — Same-Origin HTTP + RFC 6455 WebSocket + Offline QR.

Clean-room original implementation for Genshin-Helper v5:
- Strictly OPT-IN: never binds any network socket until `start()` is called.
- Ephemeral 6-digit PIN + 128-bit session token per session.
- Pure-Python ISO/IEC 18004 Byte-mode QR Code encoder (Versions 1-6, ECC-L) to
  bit matrix and crisp offline SVG (zero external packages or network calls).
- Single-port same-origin HTTP + RFC 6455 WebSocket server (zero Mixed Content
  and zero self-signed TLS warnings on mobile browsers).
- Thread-safe 10-15 Hz coalesced state broadcaster for live mobile companion UI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import struct
import threading
import time
import urllib.parse
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("GenshinHelper.LanCompanion")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HTTP_HEADER_BYTES = 32 * 1024
_MAX_HTTP_BODY_BYTES = 32 * 1024
_MAX_WS_PAYLOAD_BYTES = 64 * 1024

# ---------------------------------------------------------------------------
# Pure-Python Offline QR Code Generator (ISO/IEC 18004, Byte mode, ECC-L, V1-6)
# ---------------------------------------------------------------------------

# (version, size, max_payload_bytes, total_data_cw, ec_cw_per_block, num_blocks, align_centers)
_QR_SPECS_L: Tuple[Tuple[int, int, int, int, int, int, Tuple[int, ...]], ...] = (
    (1, 21, 17, 19, 7, 1, ()),
    (2, 25, 32, 34, 10, 1, (6, 18)),
    (3, 29, 53, 55, 15, 1, (6, 22)),
    (4, 33, 78, 80, 20, 1, (6, 26)),
    (5, 37, 106, 108, 26, 1, (6, 30)),
    (6, 41, 134, 136, 18, 2, (6, 34)),
)


def _init_gf256() -> Tuple[List[int], List[int]]:
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _init_gf256()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(degree: int) -> List[int]:
    poly = [1]
    for i in range(degree):
        factor = _GF_EXP[i]
        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, factor)
        poly = nxt
    return poly


def _rs_encode(data: List[int], ec_len: int) -> List[int]:
    gen = _rs_generator_poly(ec_len)
    rem = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = rem[i]
        if coef != 0:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gf_mul(gen[j], coef)
    return rem[len(data) :]


def _mask_bit(mask_id: int, r: int, c: int) -> bool:
    if mask_id == 0:
        return (r + c) % 2 == 0
    if mask_id == 1:
        return r % 2 == 0
    if mask_id == 2:
        return c % 3 == 0
    if mask_id == 3:
        return (r + c) % 3 == 0
    if mask_id == 4:
        return ((r // 2) + (c // 3)) % 2 == 0
    if mask_id == 5:
        return ((r * c) % 2) + ((r * c) % 3) == 0
    if mask_id == 6:
        return (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
    return (((r + c) % 2) + ((r * c) % 3)) % 2 == 0


def _compute_format_bits_l(mask_id: int) -> int:
    # ECC Level L indicator is 0b01
    data = (0b01 << 3) | (mask_id & 0x7)
    rem = data << 10
    for bit in range(14, 9, -1):
        if (rem >> bit) & 1:
            rem ^= 0x537 << (bit - 10)
    return ((data << 10) | rem) ^ 0x5412


def _place_format_bits(grid: List[List[bool]], mask_id: int) -> None:
    n = len(grid)
    fmt = _compute_format_bits_l(mask_id)

    def bit(idx: int) -> bool:
        return bool((fmt >> idx) & 1)

    # Copy 1 around top-left finder
    tl_cols = (0, 1, 2, 3, 4, 5, 7, 8)
    for k, c in enumerate(tl_cols):
        grid[8][c] = bit(14 - k)
    tl_rows = (7, 5, 4, 3, 2, 1, 0)
    for k, r in enumerate(tl_rows):
        grid[r][8] = bit(6 - k)

    # Copy 2 split between bottom-left col 8 and top-right row 8
    for k in range(7):
        grid[n - 1 - k][8] = bit(14 - k)
    for k in range(8):
        grid[8][n - 8 + k] = bit(7 - k)


def _qr_penalty(grid: List[List[bool]]) -> int:
    n = len(grid)
    penalty = 0

    # Rule 1: 5+ consecutive same-color modules in rows and columns
    for r in range(n):
        row = grid[r]
        run_len = 1
        for c in range(1, n):
            if row[c] == row[c - 1]:
                run_len += 1
            else:
                if run_len >= 5:
                    penalty += 3 + (run_len - 5)
                run_len = 1
        if run_len >= 5:
            penalty += 3 + (run_len - 5)

    for c in range(n):
        run_len = 1
        for r in range(1, n):
            if grid[r][c] == grid[r - 1][c]:
                run_len += 1
            else:
                if run_len >= 5:
                    penalty += 3 + (run_len - 5)
                run_len = 1
        if run_len >= 5:
            penalty += 3 + (run_len - 5)

    # Rule 2: 2x2 same-color blocks
    for r in range(n - 1):
        row0 = grid[r]
        row1 = grid[r + 1]
        for c in range(n - 1):
            v = row0[c]
            if v == row0[c + 1] and v == row1[c] and v == row1[c + 1]:
                penalty += 3

    # Rule 3: finder-like 1:1:3:1:1 patterns with 4 light modules on either side
    pat_a = (True, False, True, True, True, False, True, False, False, False, False)
    pat_b = (False, False, False, False, True, False, True, True, True, False, True)
    for r in range(n):
        row = grid[r]
        for c in range(n - 10):
            seg = tuple(row[c : c + 11])
            if seg == pat_a or seg == pat_b:
                penalty += 40
    for c in range(n):
        col = [grid[r][c] for r in range(n)]
        for r in range(n - 10):
            seg = tuple(col[r : r + 11])
            if seg == pat_a or seg == pat_b:
                penalty += 40

    # Rule 4: dark module balance around 50%
    dark = sum(1 for row in grid for cell in row if cell)
    total = n * n
    k = abs(dark * 20 - total * 10) // total
    penalty += k * 10
    return penalty


def generate_qr_matrix(data: str) -> List[List[bool]]:
    """Encodes UTF-8 text into an ISO/IEC 18004 QR bit matrix (Versions 1-6, ECC-L).

    Returns a square `N x N` list of booleans (`True` = dark module, `False` = light).
    """
    payload = data.encode("utf-8")
    spec = None
    for candidate in _QR_SPECS_L:
        if len(payload) <= candidate[2]:
            spec = candidate
            break
    if spec is None:
        raise ValueError(
            f"QR payload too long ({len(payload)} bytes; max supported is {_QR_SPECS_L[-1][2]} bytes)"
        )

    version, n, _max_bytes, total_data_cw, ec_cw_per_block, num_blocks, align_centers = spec

    # 1. Encode Byte-mode bit stream: mode (0100) + 8-bit count + payload bytes
    bits: List[int] = [0, 1, 0, 0]
    length = len(payload)
    for shift in range(7, -1, -1):
        bits.append((length >> shift) & 1)
    for b in payload:
        for shift in range(7, -1, -1):
            bits.append((b >> shift) & 1)

    max_data_bits = total_data_cw * 8
    terminator = min(4, max_data_bits - len(bits))
    bits.extend([0] * terminator)
    while len(bits) % 8 != 0:
        bits.append(0)

    data_cw: List[int] = []
    for i in range(0, len(bits), 8):
        val = 0
        for j in range(8):
            val = (val << 1) | bits[i + j]
        data_cw.append(val)

    pad_bytes = (0xEC, 0x11)
    pad_idx = 0
    while len(data_cw) < total_data_cw:
        data_cw.append(pad_bytes[pad_idx])
        pad_idx ^= 1

    # 2. Split into Reed-Solomon blocks & interleave (V1-V6 ECC-L all have equal block sizes)
    block_size = total_data_cw // num_blocks
    data_blocks: List[List[int]] = []
    ec_blocks: List[List[int]] = []
    for b_idx in range(num_blocks):
        blk = data_cw[b_idx * block_size : (b_idx + 1) * block_size]
        data_blocks.append(blk)
        ec_blocks.append(_rs_encode(blk, ec_cw_per_block))

    interleaved: List[int] = []
    for i in range(block_size):
        for blk in data_blocks:
            interleaved.append(blk[i])
    for i in range(ec_cw_per_block):
        for blk in ec_blocks:
            interleaved.append(blk[i])

    codeword_bits: List[bool] = []
    for cw in interleaved:
        for shift in range(7, -1, -1):
            codeword_bits.append(bool((cw >> shift) & 1))

    # 3. Allocate matrix & function-module mask
    base: List[List[bool]] = [[False] * n for _ in range(n)]
    is_func: List[List[bool]] = [[False] * n for _ in range(n)]

    def set_func(r: int, c: int, val: bool) -> None:
        if 0 <= r < n and 0 <= c < n:
            base[r][c] = val
            is_func[r][c] = True

    # Finder patterns + 1-module white separators
    def draw_finder(top: int, left: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top + dr, left + dc
                if 0 <= r < n and 0 <= c < n:
                    in_outer = 0 <= dr <= 6 and 0 <= dc <= 6
                    on_border = dr in (0, 6) or dc in (0, 6)
                    in_center = 2 <= dr <= 4 and 2 <= dc <= 4
                    set_func(r, c, in_outer and (on_border or in_center))

    draw_finder(0, 0)
    draw_finder(0, n - 7)
    draw_finder(n - 7, 0)

    # Alignment patterns (for V2-V6)
    for ar in align_centers:
        for ac in align_centers:
            if is_func[ar][ac]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) in (0, 2)
                    set_func(ar + dr, ac + dc, dark)

    # Timing patterns (row 6 and col 6)
    for i in range(8, n - 8):
        if not is_func[6][i]:
            set_func(6, i, (i % 2) == 0)
        if not is_func[i][6]:
            set_func(i, 6, (i % 2) == 0)

    # Dark module (4*version + 9, 8)
    set_func(4 * version + 9, 8, True)

    # Reserve format info modules
    for c in (0, 1, 2, 3, 4, 5, 7, 8):
        is_func[8][c] = True
    for r in (7, 5, 4, 3, 2, 1, 0):
        is_func[r][8] = True
    for k in range(7):
        is_func[n - 1 - k][8] = True
    for k in range(8):
        is_func[8][n - 8 + k] = True

    # 4. Place data + EC bits in standard 2-column zigzag
    bit_idx = 0
    total_bits = len(codeword_bits)
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not is_func[r][c]:
                    if bit_idx < total_bits:
                        base[r][c] = codeword_bits[bit_idx]
                        bit_idx += 1
                    else:
                        base[r][c] = False
        upward = not upward
        col -= 2

    # 5. Evaluate all 8 masks and choose the lowest-penalty masked matrix
    best_penalty = 1 << 30
    best_grid: Optional[List[List[bool]]] = None
    for mask_id in range(8):
        cand = [row[:] for row in base]
        for r in range(n):
            for c in range(n):
                if not is_func[r][c] and _mask_bit(mask_id, r, c):
                    cand[r][c] = not cand[r][c]
        _place_format_bits(cand, mask_id)
        score = _qr_penalty(cand)
        if score < best_penalty:
            best_penalty = score
            best_grid = cand

    assert best_grid is not None
    return best_grid


def generate_qr_svg(
    data: str,
    *,
    quiet_zone: int = 4,
    module_px: int = 6,
    fg: str = "#111111",
    bg: str = "#ffffff",
) -> str:
    """Renders an offline ISO/IEC 18004 QR code as a crisp self-contained SVG."""
    matrix = generate_qr_matrix(data)
    return qr_matrix_to_svg(
        matrix,
        quiet_zone=quiet_zone,
        module_px=module_px,
        fg=fg,
        bg=bg,
    )


def qr_matrix_to_svg(
    matrix: List[List[bool]],
    *,
    quiet_zone: int = 4,
    module_px: int = 6,
    fg: str = "#111111",
    bg: str = "#ffffff",
) -> str:
    """Converts a QR boolean matrix to a compact single-path SVG string."""
    n = len(matrix)
    qz = max(0, int(quiet_zone))
    total_modules = n + 2 * qz
    px = max(1, int(module_px))
    size_px = total_modules * px

    cmds: List[str] = []
    for r, row in enumerate(matrix):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                run = c - start
                cmds.append(f"M{start + qz} {r + qz}h{run}v1h-{run}z")
            else:
                c += 1

    path_d = "".join(cmds)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{size_px}" height="{size_px}" '
        f'viewBox="0 0 {total_modules} {total_modules}" '
        f'shape-rendering="crispEdges" role="img" aria-label="LAN Companion QR">'
        f'<rect width="{total_modules}" height="{total_modules}" fill="{bg}"/>'
        f'<path d="{path_d}" fill="{fg}"/>'
        f"</svg>"
    )


# ---------------------------------------------------------------------------
# Primary LAN IPv4 Detection (offline routing-table probe, zero packets sent)
# ---------------------------------------------------------------------------


def _is_usable_lan_ipv4(ip_str: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip_str)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast)


def detect_lan_ipv4() -> str:
    """Detects the host's primary LAN IPv4 address without sending network traffic.

    Uses a local UDP `connect()` routing-table probe to private LAN targets,
    falling back to local hostname IPv4 enumeration and finally `127.0.0.1`.
    """
    probe_targets = (
        ("10.255.255.255", 1),
        ("192.168.1.1", 80),
        ("172.16.0.1", 80),
    )
    for target_ip, target_port in probe_targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.1)
            sock.connect((target_ip, target_port))
            local_ip = sock.getsockname()[0]
            if _is_usable_lan_ipv4(local_ip):
                return local_ip
        except OSError:
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # Fallback: inspect hostname IPv4 interfaces, preferring RFC 1918 private LAN IPs
    fallback_non_private: Optional[str] = None
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip_str = info[4][0]
            if not _is_usable_lan_ipv4(ip_str):
                continue
            addr = ipaddress.IPv4Address(ip_str)
            if addr.is_private:
                return ip_str
            if fallback_non_private is None:
                fallback_non_private = ip_str
    except OSError:
        pass

    return fallback_non_private or "127.0.0.1"


# ---------------------------------------------------------------------------
# RFC 6455 WebSocket Frame Helpers (pure stdlib)
# ---------------------------------------------------------------------------


def compute_ws_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key.strip() + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_ws_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Builds an unmasked RFC 6455 server->client frame."""
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


async def read_ws_frame(reader: asyncio.StreamReader) -> Optional[Tuple[int, bytes]]:
    """Reads a single RFC 6455 frame from `reader`. Returns `(opcode, payload)` or `None` on EOF."""
    try:
        head = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None

    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    try:
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]

        if length > _MAX_WS_PAYLOAD_BYTES:
            return None

        mask_key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length) if length > 0 else b""
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None

    if masked and payload:
        payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

    return opcode, payload


# ---------------------------------------------------------------------------
# Same-Origin Mobile Companion HTML5 Viewer (Offline Bilingual VI/EN)
# ---------------------------------------------------------------------------


def _render_pin_login_html() -> str:
    return """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>Genshin Helper LAN Companion</title>
<style>
  :root{--bg:#121318;--card:#1b1d26;--fg:#edf0f7;--muted:#9aa3b2;--accent:#bb86fc;--err:#ff6b6b;--border:#2b2f3d}
  *{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  body{margin:0;min-height:100dvh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--fg);padding:20px}
  .card{width:100%;max-width:360px;background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;box-shadow:0 12px 36px rgba(0,0,0,.45)}
  h1{margin:0 0 6px;font-size:19px}
  p{margin:0 0 18px;color:var(--muted);font-size:13px;line-height:1.45}
  input{width:100%;padding:14px;font-size:24px;letter-spacing:8px;text-align:center;border-radius:10px;border:1px solid var(--border);background:#101218;color:var(--fg);outline:none}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:10px;background:var(--accent);color:#121318;font-weight:700;font-size:15px;cursor:pointer}
  .err{margin-top:10px;color:var(--err);font-size:13px;min-height:18px;text-align:center}
</style>
</head>
<body>
  <div class="card">
    <h1>Genshin Helper LAN</h1>
    <p>Nhập mã PIN 6 số hiển thị trên PC / Enter the 6-digit PIN shown on your PC.</p>
    <input id="pin" type="tel" inputmode="numeric" maxlength="6" placeholder="000000" autocomplete="one-time-code"/>
    <button id="go">Kết nối / Connect</button>
    <div class="err" id="err"></div>
  </div>
<script>
const pinEl = document.getElementById('pin');
const errEl = document.getElementById('err');
async function submitPin(){
  errEl.textContent = '';
  const pin = (pinEl.value || '').replace(/\\D/g, '');
  if (pin.length !== 6) { errEl.textContent = 'Cần đủ 6 chữ số / Need 6 digits'; return; }
  try {
    const res = await fetch('/auth/pin', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pin})
    });
    const data = await res.json();
    if (res.ok && data.companion_path) { location.href = data.companion_path; }
    else { errEl.textContent = data.error || 'Sai mã PIN / Invalid PIN'; }
  } catch (e) { errEl.textContent = 'Lỗi kết nối / Network error'; }
}
document.getElementById('go').addEventListener('click', submitPin);
pinEl.addEventListener('keydown', e => { if (e.key === 'Enter') submitPin(); });
</script>
</body>
</html>"""


def _render_companion_html(token: str) -> str:
    safe_token_json = json.dumps(token)
    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>Genshin Helper — LAN Companion</title>
<style>
  :root{{--bg:#121318;--panel:#1a1c24;--border:#2b2f3d;--fg:#eef1f8;--muted:#9aa3b2;--accent:#bb86fc;--ok:#4ade80;--warn:#facc15;--err:#ff6b6b;--tp:#38bdf8}}
  *{{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-tap-highlight-color:transparent}}
  html,body{{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--fg)}}
  #app{{display:flex;flex-direction:column;width:100%;height:100dvh}}
  header{{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;background:var(--panel);border-bottom:1px solid var(--border);z-index:2}}
  .brand{{display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px}}
  .dot{{width:9px;height:9px;border-radius:50%;background:var(--warn)}}
  .dot.ok{{background:var(--ok)}}
  .dot.err{{background:var(--err)}}
  .controls{{display:flex;align-items:center;gap:6px}}
  .pill{{background:#242736;border:1px solid var(--border);color:var(--fg);border-radius:8px;padding:5px 9px;font-size:12px;cursor:pointer}}
  .pill.active{{border-color:var(--accent);color:var(--accent)}}
  main{{position:relative;flex:1;overflow:hidden}}
  canvas{{display:block;width:100%;height:100%;touch-action:none}}
  .fab-col{{position:absolute;right:12px;bottom:14px;display:flex;flex-direction:column;gap:8px;z-index:2}}
  .fab{{width:42px;height:42px;border-radius:12px;border:1px solid var(--border);background:rgba(26,28,36,.92);color:var(--fg);font-size:18px;font-weight:700;cursor:pointer}}
  .hud{{position:absolute;left:12px;top:12px;display:flex;flex-direction:column;gap:6px;pointer-events:none;z-index:2;max-width:74%}}
  .badge{{display:inline-flex;align-items:center;gap:6px;background:rgba(26,28,36,.9);border:1px solid var(--border);border-radius:10px;padding:6px 10px;font-size:12px;backdrop-filter:blur(6px)}}
  footer{{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 12px;background:var(--panel);border-top:1px solid var(--border);font-size:12px;color:var(--muted)}}
  .target{{color:var(--fg);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
</style>
</head>
<body>
<div id="app">
  <header>
    <div class="brand"><span id="statusDot" class="dot"></span><span id="statusText">Đang kết nối...</span></div>
    <div class="controls">
      <button id="btnFollow" class="pill active">Theo dõi</button>
      <button id="btnLang" class="pill">VI / EN</button>
    </div>
  </header>
  <main>
    <canvas id="mapCanvas"></canvas>
    <div class="hud">
      <div class="badge" id="coordBadge">Lat: -- | Lng: --</div>
      <div class="badge" id="floorBadge">Tầng: Surface</div>
    </div>
    <div class="fab-col">
      <button class="fab" id="btnZoomIn">+</button>
      <button class="fab" id="btnZoomOut">−</button>
      <button class="fab" id="btnCenter">◎</button>
    </div>
  </main>
  <footer>
    <div class="target" id="targetLabel">Mục tiêu: —</div>
    <div id="distLabel">-- m</div>
  </footer>
</div>
<script>
(() => {{
  const TOKEN = {safe_token_json};
  const I18N = {{
    vi: {{
      connecting: 'Đang kết nối...',
      live: 'Đã kết nối LAN',
      offline: 'Mất kết nối — thử lại...',
      follow: 'Theo dõi',
      floor: 'Tầng',
      target: 'Mục tiêu',
      none: 'Không có',
      surface: 'Mặt đất',
      tp: 'Dịch chuyển'
    }},
    en: {{
      connecting: 'Connecting...',
      live: 'LAN Live',
      offline: 'Disconnected — retrying...',
      follow: 'Follow',
      floor: 'Floor',
      target: 'Target',
      none: 'None',
      surface: 'Surface',
      tp: 'Teleport'
    }}
  }};

  let lang = (navigator.language || '').toLowerCase().startsWith('vi') ? 'vi' : 'en';
  let follow = true;
  let zoom = 1.6;
  let camLat = 0, camLng = 0;
  let hasFix = false;
  let state = {{
    lat: 0, lng: 0, heading: 0, camera_heading: null,
    floor_name: '', target_pin: null, nearby_pins: [], route_legs: []
  }};

  const canvas = document.getElementById('mapCanvas');
  const ctx = canvas.getContext('2d');
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const coordBadge = document.getElementById('coordBadge');
  const floorBadge = document.getElementById('floorBadge');
  const targetLabel = document.getElementById('targetLabel');
  const distLabel = document.getElementById('distLabel');
  const btnFollow = document.getElementById('btnFollow');
  const btnLang = document.getElementById('btnLang');

  let wsStatus = 'connecting';
  function t(k) {{ return I18N[lang][k] || k; }}
  function refreshText() {{
    btnFollow.textContent = t('follow');
    btnFollow.classList.toggle('active', follow);
    btnLang.textContent = lang === 'vi' ? 'VI' : 'EN';
    statusText.textContent = t(wsStatus);
    statusDot.className = 'dot ' + (wsStatus === 'live' ? 'ok' : (wsStatus === 'offline' ? 'err' : ''));
    const floorVal = state.floor_name || t('surface');
    floorBadge.textContent = `${{t('floor')}}: ${{floorVal}}`;
    if (hasFix) {{
      coordBadge.textContent = `Lat: ${{Number(state.lat).toFixed(1)}} | Lng: ${{Number(state.lng).toFixed(1)}}`;
    }}
    if (state.target_pin && state.target_pin.lat != null && state.target_pin.lng != null) {{
      const name = state.target_pin.name || `#${{state.target_pin.id || ''}}`;
      targetLabel.textContent = `${{t('target')}}: ${{name}}`;
      const d = Math.hypot(Number(state.target_pin.lat) - Number(state.lat || 0), Number(state.target_pin.lng) - Number(state.lng || 0));
      distLabel.textContent = `${{d.toFixed(1)}} m`;
    }} else {{
      targetLabel.textContent = `${{t('target')}}: ${{t('none')}}`;
      distLabel.textContent = '-- m';
    }}
  }}

  function resize() {{
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }}
  window.addEventListener('resize', resize);

  function worldToScreen(lat, lng) {{
    const rect = canvas.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    // HoYoLAB CRS: lng = X (right), lat = Y (down in HoYoLAB tiles / canvas Y)
    return [cx + (lng - camLng) * zoom, cy + (lat - camLat) * zoom];
  }}

  function draw() {{
    const rect = canvas.getBoundingClientRect();
    const w = rect.width, h = rect.height;
    ctx.clearRect(0, 0, w, h);

    // Background grid
    ctx.fillStyle = '#121318';
    ctx.fillRect(0, 0, w, h);
    const stepWorld = zoom > 2.5 ? 25 : (zoom > 0.8 ? 50 : 200);
    const stepPx = stepWorld * zoom;
    if (stepPx >= 18) {{
      ctx.strokeStyle = 'rgba(255,255,255,0.05)';
      ctx.lineWidth = 1;
      const offX = ((w / 2 - camLng * zoom) % stepPx + stepPx) % stepPx;
      const offY = ((h / 2 - camLat * zoom) % stepPx + stepPx) % stepPx;
      ctx.beginPath();
      for (let x = offX; x < w; x += stepPx) {{ ctx.moveTo(x, 0); ctx.lineTo(x, h); }}
      for (let y = offY; y < h; y += stepPx) {{ ctx.moveTo(0, y); ctx.lineTo(w, y); }}
      ctx.stroke();
    }}

    // Route legs
    const legs = Array.isArray(state.route_legs) ? state.route_legs : [];
    for (const leg of legs) {{
      const fLat = leg.from_lat != null ? leg.from_lat : (leg.points && leg.points[0] ? leg.points[0][0] : null);
      const fLng = leg.from_lng != null ? leg.from_lng : (leg.points && leg.points[0] ? leg.points[0][1] : null);
      const tLat = leg.to_lat != null ? leg.to_lat : (leg.points && leg.points[1] ? leg.points[1][0] : null);
      const tLng = leg.to_lng != null ? leg.to_lng : (leg.points && leg.points[1] ? leg.points[1][1] : null);
      if (fLat == null || fLng == null || tLat == null || tLng == null) continue;
      const [x1, y1] = worldToScreen(Number(fLat), Number(fLng));
      const [x2, y2] = worldToScreen(Number(tLat), Number(tLng));
      ctx.save();
      ctx.beginPath();
      if (leg.is_teleport) {{
        ctx.setLineDash([6, 6]);
        ctx.strokeStyle = '#38bdf8';
        ctx.lineWidth = 2;
      }} else {{
        ctx.strokeStyle = '#bb86fc';
        ctx.lineWidth = 2.6;
      }}
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      // Midpoint direction chevron ►
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const ang = Math.atan2(y2 - y1, x2 - x1);
      ctx.translate(mx, my);
      ctx.rotate(ang);
      ctx.fillStyle = leg.is_teleport ? '#38bdf8' : '#e9d5ff';
      ctx.beginPath();
      ctx.moveTo(5, 0); ctx.lineTo(-4, -3.5); ctx.lineTo(-4, 3.5); ctx.closePath();
      ctx.fill();
      ctx.restore();
    }}

    // Nearby pins
    const pins = Array.isArray(state.nearby_pins) ? state.nearby_pins : [];
    for (const p of pins) {{
      if (p.lat == null || p.lng == null) continue;
      const [px, py] = worldToScreen(Number(p.lat), Number(p.lng));
      ctx.beginPath();
      ctx.arc(px, py, 5, 0, Math.PI * 2);
      ctx.fillStyle = p.is_underground ? '#60a5fa' : '#facc15';
      ctx.fill();
      ctx.strokeStyle = '#121318';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }}

    // Target pin
    if (state.target_pin && state.target_pin.lat != null && state.target_pin.lng != null) {{
      const [tx, ty] = worldToScreen(Number(state.target_pin.lat), Number(state.target_pin.lng));
      ctx.beginPath();
      ctx.arc(tx, ty, 10, 0, Math.PI * 2);
      ctx.strokeStyle = '#4ade80';
      ctx.lineWidth = 2.2;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(tx, ty, 4, 0, Math.PI * 2);
      ctx.fillStyle = '#4ade80';
      ctx.fill();
    }}

    // Player marker + camera cone
    if (hasFix) {{
      const [px, py] = worldToScreen(Number(state.lat), Number(state.lng));
      if (state.camera_heading != null && !Number.isNaN(Number(state.camera_heading))) {{
        const rad = (Number(state.camera_heading) - 90) * Math.PI / 180;
        ctx.save();
        ctx.translate(px, py);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.arc(0, 0, 38, rad - 0.45, rad + 0.45);
        ctx.closePath();
        ctx.fillStyle = 'rgba(187, 134, 252, 0.22)';
        ctx.fill();
        ctx.restore();
      }}
      const hdgRad = (Number(state.heading || 0) - 90) * Math.PI / 180;
      ctx.save();
      ctx.translate(px, py);
      ctx.rotate(hdgRad);
      ctx.beginPath();
      ctx.moveTo(11, 0);
      ctx.lineTo(-7, -6);
      ctx.lineTo(-4, 0);
      ctx.lineTo(-7, 6);
      ctx.closePath();
      ctx.fillStyle = '#ffffff';
      ctx.strokeStyle = '#bb86fc';
      ctx.lineWidth = 2;
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }}
  }}

  // Touch / pointer pan
  let dragging = false, lastX = 0, lastY = 0;
  canvas.addEventListener('pointerdown', e => {{
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    canvas.setPointerCapture(e.pointerId);
  }});
  canvas.addEventListener('pointermove', e => {{
    if (!dragging) return;
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    follow = false;
    camLng -= dx / zoom;
    camLat -= dy / zoom;
    refreshText();
    draw();
  }});
  canvas.addEventListener('pointerup', () => {{ dragging = false; }});

  btnFollow.addEventListener('click', () => {{
    follow = !follow;
    if (follow && hasFix) {{ camLat = Number(state.lat); camLng = Number(state.lng); }}
    refreshText(); draw();
  }});
  btnLang.addEventListener('click', () => {{ lang = lang === 'vi' ? 'en' : 'vi'; refreshText(); }});
  document.getElementById('btnZoomIn').addEventListener('click', () => {{ zoom = Math.min(8, zoom * 1.3); draw(); }});
  document.getElementById('btnZoomOut').addEventListener('click', () => {{ zoom = Math.max(0.15, zoom / 1.3); draw(); }});
  document.getElementById('btnCenter').addEventListener('click', () => {{
    follow = true;
    if (hasFix) {{ camLat = Number(state.lat); camLng = Number(state.lng); }}
    refreshText(); draw();
  }});

  // Same-origin WebSocket connection with exponential backoff
  let retryMs = 1000;
  function connectWs() {{
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${{proto}}//${{location.host}}/ws/${{encodeURIComponent(TOKEN)}}`;
    wsStatus = 'connecting';
    refreshText();
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => {{
      retryMs = 1000;
      wsStatus = 'live';
      refreshText();
    }};
    ws.onmessage = ev => {{
      try {{
        const msg = JSON.parse(ev.data);
        if (msg && msg.type === 'STATE' && msg.data) {{
          state = Object.assign(state, msg.data);
        }} else if (msg && (msg.lat != null || msg.lng != null || msg.route_legs || msg.nearby_pins)) {{
          state = Object.assign(state, msg);
        }}
        if (state.lat != null && state.lng != null) {{
          hasFix = true;
          if (follow) {{ camLat = Number(state.lat); camLng = Number(state.lng); }}
        }}
        refreshText();
        draw();
      }} catch (_) {{}}
    }};
    ws.onclose = () => {{
      wsStatus = 'offline';
      refreshText();
      setTimeout(connectWs, retryMs);
      retryMs = Math.min(8000, Math.round(retryMs * 1.5));
    }};
    ws.onerror = () => {{ try {{ ws.close(); }} catch (_) {{}} }};
  }}

  resize();
  refreshText();
  connectWs();
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Same-Origin Asyncio LAN Companion Server
# ---------------------------------------------------------------------------


class _WsClient:
    __slots__ = ("writer", "peer", "closed")

    def __init__(self, writer: asyncio.StreamWriter, peer: str) -> None:
        self.writer = writer
        self.peer = peer
        self.closed = False


class LanCompanionServer:
    """Opt-in same-origin HTTP + RFC 6455 WebSocket companion server for LAN devices.

    Never binds a socket on initialization — callers must explicitly invoke
    `await start()` (or `start_in_background()`) when the user enables LAN Companion.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8766,
        *,
        max_hz: float = 15.0,
        max_clients: int = 8,
        advertised_host: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.max_hz = max(1.0, float(max_hz))
        self.min_broadcast_interval = 1.0 / self.max_hz
        self.max_clients = max(1, int(max_clients))
        self._advertised_host_override = advertised_host

        self.pin: str = f"{secrets.randbelow(1_000_000):06d}"
        self.token: str = secrets.token_urlsafe(16)

        self.is_running: bool = False
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_clients: Set[_WsClient] = set()
        self._pin_failures: Dict[str, Deque[float]] = {}

        self.latest_state: Optional[Dict[str, Any]] = None
        self._pending_payload: Optional[Dict[str, Any]] = None
        self._last_flush_mono: float = 0.0
        self._flush_handle: Optional[asyncio.TimerHandle] = None
        self.on_client_message: Optional[Callable[[Dict[str, Any]], None]] = None

        # Optional background thread runner for sync UI callers
        self._bg_thread: Optional[threading.Thread] = None
        self._bg_stop_event: Optional[asyncio.Event] = None

    def rotate_credentials(self) -> Tuple[str, str]:
        """Generates a fresh ephemeral 6-digit PIN and 128-bit session token."""
        self.pin = f"{secrets.randbelow(1_000_000):06d}"
        self.token = secrets.token_urlsafe(16)
        self._pin_failures.clear()
        return self.pin, self.token

    def get_advertised_host(self) -> str:
        if self._advertised_host_override:
            return self._advertised_host_override
        if self.host in ("0.0.0.0", "", "::"):
            return detect_lan_ipv4()
        return self.host

    def get_companion_url(self) -> str:
        adv_host = self.get_advertised_host()
        return f"http://{adv_host}:{self.port}/companion?token={self.token}"

    def get_ws_url(self) -> str:
        adv_host = self.get_advertised_host()
        return f"ws://{adv_host}:{self.port}/ws/{self.token}"

    def get_qr_svg(self, *, quiet_zone: int = 4, module_px: int = 6) -> str:
        return generate_qr_svg(
            self.get_companion_url(),
            quiet_zone=quiet_zone,
            module_px=module_px,
        )

    def get_connection_info(self) -> Dict[str, Any]:
        url = self.get_companion_url()
        return {
            "is_running": self.is_running,
            "host": self.get_advertised_host(),
            "bind_host": self.host,
            "port": self.port,
            "pin": self.pin,
            "token": self.token,
            "url": url,
            "ws_url": self.get_ws_url(),
            "qr_svg": generate_qr_svg(url),
            "client_count": len(self._ws_clients),
        }

    def _verify_token(self, provided: Optional[str]) -> Tuple[bool, int]:
        """Returns `(ok, http_error_code)` using constant-time comparison."""
        if not provided:
            return False, 401
        if hmac.compare_digest(provided, self.token):
            return True, 200
        return False, 403

    def _check_pin_rate_limit(self, client_ip: str) -> bool:
        now = time.monotonic()
        bucket = self._pin_failures.get(client_ip)
        if bucket is None:
            return True
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        return len(bucket) < 5

    def _record_pin_failure(self, client_ip: str) -> None:
        now = time.monotonic()
        bucket = self._pin_failures.setdefault(client_ip, deque(maxlen=10))
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        bucket.append(now)

    async def _safe_close(self, writer: asyncio.StreamWriter) -> None:
        try:
            if not writer.is_closing():
                writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:
            pass

    async def _send_http(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
        extra_headers: str = "",
    ) -> None:
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\n"
            f"{extra_headers}"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(head + body)
        await writer.drain()
        await self._safe_close(writer)

    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        payload: Dict[str, Any],
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._send_http(
            writer,
            status,
            reason,
            body,
            content_type="application/json; charset=utf-8",
        )

    async def _handle_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: Dict[str, str],
        peer_str: str,
    ) -> None:
        upgrade = headers.get("upgrade", "").lower()
        ws_key = headers.get("sec-websocket-key", "")
        if upgrade != "websocket" or not ws_key:
            await self._send_http(
                writer,
                426,
                "Upgrade Required",
                b"WebSocket upgrade required",
                extra_headers="Upgrade: websocket\r\n",
            )
            return

        if len(self._ws_clients) >= self.max_clients:
            await self._send_http(writer, 503, "Service Unavailable", b"Too many companion clients")
            return

        accept = compute_ws_accept_key(ws_key)
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        writer.write(handshake)
        await writer.drain()

        client = _WsClient(writer, peer_str)
        self._ws_clients.add(client)
        logger.info("LAN companion WS connected: %s (total=%d)", peer_str, len(self._ws_clients))

        try:
            # Immediately push latest known state so phone renders without waiting
            if self.latest_state is not None:
                initial_bytes = json.dumps(self.latest_state, ensure_ascii=False).encode("utf-8")
                writer.write(encode_ws_frame(0x1, initial_bytes))
                await writer.drain()

            while self.is_running and not client.closed:
                frame = await read_ws_frame(reader)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:  # Close
                    try:
                        writer.write(encode_ws_frame(0x8, b""))
                        await writer.drain()
                    except Exception:
                        pass
                    break
                if opcode == 0x9:  # Ping -> Pong
                    writer.write(encode_ws_frame(0xA, payload))
                    await writer.drain()
                elif opcode == 0x1 and payload:  # Text frame
                    try:
                        parsed = json.loads(payload.decode("utf-8"))
                        if isinstance(parsed, dict) and self.on_client_message is not None:
                            self.on_client_message(parsed)
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("LAN companion WS loop ended (%s): %s", peer_str, exc)
        finally:
            client.closed = True
            self._ws_clients.discard(client)
            await self._safe_close(writer)
            logger.info("LAN companion WS disconnected: %s (total=%d)", peer_str, len(self._ws_clients))

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        peer_str = f"{peer_ip}:{peer[1]}" if isinstance(peer, tuple) and len(peer) >= 2 else peer_ip

        try:
            try:
                req_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            except asyncio.TimeoutError:
                await self._safe_close(writer)
                return

            if not req_line:
                await self._safe_close(writer)
                return

            parts = req_line.decode("utf-8", errors="ignore").strip().split()
            if len(parts) < 2:
                await self._send_http(writer, 400, "Bad Request", b"Malformed request line")
                return

            method, raw_target = parts[0].upper(), parts[1]

            headers: Dict[str, str] = {}
            header_bytes = len(req_line)
            while True:
                try:
                    hline = await asyncio.wait_for(reader.readline(), timeout=3.0)
                except asyncio.TimeoutError:
                    await self._safe_close(writer)
                    return
                if not hline or not hline.strip():
                    break
                header_bytes += len(hline)
                if header_bytes > _MAX_HTTP_HEADER_BYTES:
                    await self._send_http(writer, 431, "Request Header Fields Too Large", b"Headers too large")
                    return
                decoded = hline.decode("utf-8", errors="ignore").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = 0
            if "content-length" in headers:
                try:
                    content_length = int(headers["content-length"])
                except ValueError:
                    await self._send_http(writer, 400, "Bad Request", b"Invalid Content-Length")
                    return
                if content_length < 0 or content_length > _MAX_HTTP_BODY_BYTES:
                    await self._send_http(writer, 413, "Payload Too Large", b"Body too large")
                    return

            body_bytes = b""
            if content_length > 0:
                try:
                    body_bytes = await asyncio.wait_for(reader.readexactly(content_length), timeout=3.0)
                except Exception:
                    await self._send_http(writer, 400, "Bad Request", b"Incomplete body")
                    return

            parsed = urllib.parse.urlsplit(raw_target)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            # Extract optional bearer token header as secondary transport
            bearer_token: Optional[str] = None
            auth_header = headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                bearer_token = auth_header[7:].strip()

            # 1. Health endpoint (no secrets exposed)
            if method == "GET" and path == "/health":
                await self._send_json(
                    writer,
                    200,
                    "OK",
                    {"ok": True, "clients": len(self._ws_clients)},
                )
                return

            # 2. Root PIN login screen (does not expose token)
            if method == "GET" and path == "/":
                html = _render_pin_login_html().encode("utf-8")
                await self._send_http(
                    writer,
                    200,
                    "OK",
                    html,
                    content_type="text/html; charset=utf-8",
                    extra_headers=(
                        "Content-Security-Policy: default-src 'self'; "
                        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'\r\n"
                    ),
                )
                return

            # 3. Ephemeral 6-digit PIN exchange (rate-limited)
            if method == "POST" and path == "/auth/pin":
                if not self._check_pin_rate_limit(peer_ip):
                    await self._send_json(
                        writer,
                        429,
                        "Too Many Requests",
                        {"ok": False, "error": "Too many attempts; retry in 60s"},
                    )
                    return
                try:
                    req_obj = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                except Exception:
                    await self._send_json(writer, 400, "Bad Request", {"ok": False, "error": "Invalid JSON"})
                    return
                candidate_pin = str(req_obj.get("pin", "")).strip()
                if candidate_pin and hmac.compare_digest(candidate_pin, self.pin):
                    await self._send_json(
                        writer,
                        200,
                        "OK",
                        {
                            "ok": True,
                            "token": self.token,
                            "companion_path": f"/companion?token={self.token}",
                        },
                    )
                    return
                self._record_pin_failure(peer_ip)
                await self._send_json(writer, 403, "Forbidden", {"ok": False, "error": "Invalid PIN"})
                return

            # 4. Authenticated Companion HTML5 Viewer
            if method == "GET" and path == "/companion":
                q_token = (query.get("token") or [None])[0]
                ok, err_code = self._verify_token(q_token or bearer_token)
                if not ok:
                    reason = "Unauthorized" if err_code == 401 else "Forbidden"
                    await self._send_http(writer, err_code, reason, b"Unauthorized")
                    return
                html_bytes = _render_companion_html(self.token).encode("utf-8")
                await self._send_http(
                    writer,
                    200,
                    "OK",
                    html_bytes,
                    content_type="text/html; charset=utf-8",
                    extra_headers=(
                        "Content-Security-Policy: default-src 'self'; "
                        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                        "connect-src 'self' ws: wss:; img-src 'self' data:\r\n"
                    ),
                )
                return

            # 5. Authenticated Offline QR SVG endpoint
            if method == "GET" and path == "/qr.svg":
                q_token = (query.get("token") or [None])[0]
                ok, err_code = self._verify_token(q_token or bearer_token)
                if not ok:
                    reason = "Unauthorized" if err_code == 401 else "Forbidden"
                    await self._send_http(writer, err_code, reason, b"Unauthorized")
                    return
                svg_bytes = self.get_qr_svg().encode("utf-8")
                await self._send_http(
                    writer,
                    200,
                    "OK",
                    svg_bytes,
                    content_type="image/svg+xml; charset=utf-8",
                )
                return

            # 6. Authenticated Same-Origin WebSocket Endpoint: /ws/<token> or /ws?token=<token>
            if method == "GET" and (path == "/ws" or path.startswith("/ws/")):
                path_token: Optional[str] = None
                if path.startswith("/ws/"):
                    raw_seg = path[4:].strip("/")
                    path_token = urllib.parse.unquote(raw_seg) if raw_seg else None
                q_token = (query.get("token") or [None])[0]
                ok, err_code = self._verify_token(path_token or q_token or bearer_token)
                if not ok:
                    reason = "Unauthorized" if err_code == 401 else "Forbidden"
                    await self._send_http(writer, err_code, reason, b"Unauthorized WebSocket")
                    return
                await self._handle_websocket(reader, writer, headers, peer_str)
                return

            await self._send_http(writer, 404, "Not Found", b"Not Found")
        except Exception as exc:
            logger.debug("LAN companion HTTP handler error (%s): %s", peer_str, exc)
            await self._safe_close(writer)

    async def start(self) -> None:
        """Explicit opt-in bind and start of the LAN companion HTTP+WS server."""
        if self.is_running:
            return
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        if self._server.sockets:
            self.port = int(self._server.sockets[0].getsockname()[1])
        self.is_running = True
        logger.info("LAN Companion listening on %s (PIN: %s)", self.get_companion_url(), self.pin)

    async def stop(self) -> None:
        """Gracefully disconnects all WebSocket clients and shuts down the listener."""
        self.is_running = False
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._pending_payload = None

        clients = list(self._ws_clients)
        self._ws_clients.clear()
        for client in clients:
            client.closed = True
            try:
                client.writer.write(encode_ws_frame(0x8, b""))
                await asyncio.wait_for(client.writer.drain(), timeout=0.5)
            except Exception:
                pass
            await self._safe_close(client.writer)

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        logger.info("LAN Companion stopped.")

    def _flush_to_clients(self, payload: Dict[str, Any]) -> None:
        self._flush_handle = None
        self._pending_payload = None
        self._last_flush_mono = time.monotonic()
        self.latest_state = payload

        if not self._ws_clients:
            return

        frame = encode_ws_frame(0x1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        dead: List[_WsClient] = []
        for client in list(self._ws_clients):
            if client.closed or client.writer.is_closing():
                dead.append(client)
                continue
            transport = client.writer.transport
            if transport is not None and transport.get_write_buffer_size() > 262144:
                # Drop stalled slow clients rather than buffering unbounded frames
                dead.append(client)
                continue
            try:
                client.writer.write(frame)
            except Exception:
                dead.append(client)

        for client in dead:
            client.closed = True
            self._ws_clients.discard(client)
            if self._loop is not None:
                self._loop.create_task(self._safe_close(client.writer))

    def _flush_pending_cb(self) -> None:
        self._flush_handle = None
        if not self.is_running or self._pending_payload is None:
            return
        self._flush_to_clients(self._pending_payload)

    def broadcast_state(self, payload: Dict[str, Any], *, force: bool = False) -> bool:
        """Thread-safely broadcasts live telemetry to all connected LAN companion clients.

        Expected fields include `{lat, lng, heading, camera_heading, floor_name,
        target_pin, nearby_pins, route_legs}`. Coalesces rapid bursts to `max_hz`
        (default 15 Hz) while delivering isolated updates immediately.
        """
        if not self.is_running or self._loop is None or self._loop.is_closed():
            return False

        stamped = dict(payload)
        stamped.setdefault("type", "STATE")
        stamped.setdefault("ts", round(time.time(), 3))

        def _schedule() -> None:
            if not self.is_running:
                return
            now = time.monotonic()
            elapsed = now - self._last_flush_mono
            if force or elapsed >= self.min_broadcast_interval:
                if self._flush_handle is not None:
                    self._flush_handle.cancel()
                    self._flush_handle = None
                self._flush_to_clients(stamped)
            else:
                self._pending_payload = stamped
                self.latest_state = stamped
                if self._flush_handle is None and self._loop is not None:
                    delay = max(0.001, self.min_broadcast_interval - elapsed)
                    self._flush_handle = self._loop.call_later(delay, self._flush_pending_cb)

        self._loop.call_soon_threadsafe(_schedule)
        return True

    def start_in_background(self, timeout_s: float = 5.0) -> Dict[str, Any]:
        """Convenience synchronous opt-in starter running an isolated asyncio thread."""
        if self.is_running:
            return self.get_connection_info()

        started = threading.Event()
        err_holder: List[BaseException] = []

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            stop_ev = asyncio.Event()
            self._bg_stop_event = stop_ev

            async def _main() -> None:
                try:
                    await self.start()
                except BaseException as exc:
                    err_holder.append(exc)
                    started.set()
                    return
                started.set()
                await stop_ev.wait()
                await self.stop()

            try:
                loop.run_until_complete(_main())
            finally:
                loop.close()

        self._bg_thread = threading.Thread(
            target=_runner,
            name="GenshinHelper-LanCompanion",
            daemon=True,
        )
        self._bg_thread.start()
        if not started.wait(timeout=timeout_s):
            raise TimeoutError("Timed out starting LanCompanionServer")
        if err_holder:
            raise err_holder[0]
        return self.get_connection_info()

    def stop_background(self, timeout_s: float = 3.0) -> None:
        """Stops a server launched via `start_in_background()`."""
        if self._loop is not None and not self._loop.is_closed() and self._bg_stop_event is not None:
            self._loop.call_soon_threadsafe(self._bg_stop_event.set)
        if self._bg_thread is not None and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=timeout_s)
        self._bg_thread = None
        self._bg_stop_event = None
