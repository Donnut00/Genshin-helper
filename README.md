<div align="center">
  <img src="assets/icons/app_compass_origin.png" alt="Genshin-Helper Compass Icon" width="96" height="96" />
  <h1>Genshin-Helper v5.3</h1>
  <p><strong>Passive Computer Vision Bridge &amp; 360° Navigation Companion for HoYoLAB Interactive Map</strong></p>
  <p>
    <strong>English</strong> | <a href="README.vi.md">Tiếng Việt</a>
  </p>
</div>

---

## Overview

**Genshin-Helper** is a local-only, 100% passive Computer Vision companion for Windows 10/11 (64-bit) that synchronizes your in-game position from **Genshin Impact** onto the official **HoYoLAB Interactive Map** (Chrome / Edge / Brave MV3 Extension) — with zero game memory access, zero DLL injection, and zero synthetic game input.

### Key Features

- **Exact Map (`M`) & Hybrid Minimap Tracking (V5.3):** Multi-map localization (Teyvat, Enkanomiya, The Chasm, Sea of Bygone Eras, etc.) with Sobel terrain alignment, layered underground cave floor matching, and continuous 4-state minimap tracking (`LOST` / `ACQUIRING` / `TRACKING` / `RELOCATING`), enhanced with non-blocking HoYoLAB HD SIFT + zero-mean Sobel tracking, automatic surface ↔ underground layer switching, runtime HUD auto-calibration, and offline Full-Teyvat RootSIFT global auto-relocation.
- **360° Camera-Synced Mini-HUD & Proximity Spot Cards:** Click-through layered overlay (`UpdateLayeredWindow` + `WDA_EXCLUDEFROMCAPTURE`) displaying true 360° camera FOV & character heading, sticky target navigation, hold-to-collect confirmation guard, and nearby pin hint cards.
- **TSP Route Planner & LAN Companion:** Generate shortest-path farming routes rendered cleanly on the map, with optional read-only LAN companion view on a phone or tablet.
- **Cross-Map Pin Exporter:** Passively export your marked HoYoLAB pins inside your browser session to open-source interactive maps (Kongying Tavern, `genshinmap.github.io`, Appsample) with per-map coordinate calibration and automatic backups.
- **Browser Extension Control Center (MV3) & Single-Monitor Mode:** Zero-clutter HoYoLAB map surface, toolbar badge (`ON` / `ZZZ` Standby at 0% CPU / `OFF`), Native Messaging Host auto-wake, `Alt+Q` quick game/map switch, `Alt+F11` sync & mark nearest pin, and `Alt+F12` privacy-safe diagnostic capture (automatically blurs in-game UID).
- **Bilingual UI (EN / VI) & Local Asset Hydration:** Map tiles and POI datasets hydrate locally into `%LOCALAPPDATA%\GenshinHelper\data` with atomic staged promotion and MD5 manifest tracking (`src/asset_setup.py`).

---

## Safety, Anti-Cheat & Terms of Service Disclaimer

- **100% Passive Computer Vision:** Reads only on-screen pixels using standard Windows desktop capture APIs (DXGI/BetterCam, MSS, BitBlt) and reads public HoYoLAB map data.
- **Zero Invasive Actions:** Never reads or writes game RAM, never injects DLLs, never hooks DirectX/game internals, and **never calls `SendInput`** to inject keyboard or mouse input into the game window.
- **Local-Only Privacy:** Sync server listens on `127.0.0.1:8765`. No analytics, no telemetry, and no HoYoLAB cookie collection.
- **ToS Disclaimer:** Genshin-Helper is an unofficial fan-made companion tool not affiliated with or endorsed by HoYoverse / COGNOSPHERE PTE. LTD. Although engineered to remain strictly read-only and passive, you use any third-party companion software at your own discretion and responsibility. See [SECURITY.md](SECURITY.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

## Game Settings Compatibility Matrix

Genshin-Helper normalizes HUD geometry via Unity `CanvasScaler` rules to maximize compatibility, while transparently surfacing **3 hard Windows / game limits**:

| Setting / Environment | Status | Behavior & Recommended Setup |
| --- | --- | --- |
| **Borderless Windowed / Windowed** | Full support | Recommended for Mini-HUD overlays, HUD toasts, and single-monitor play. |
| **Exclusive Fullscreen** *(Hard OS Limit)* | Limited (no overlay) | Windows DWM prevents click-through layered HUD overlays from rendering over Exclusive Fullscreen. Use the built-in **1-click Borderless Windowed launcher** (`-popupwindow -screen-fullscreen 0`) or switch in game settings. |
| **Admin Privilege / UIPI** *(Hard Windows Limit)* | Requires matching elevation | Genshin Impact launches elevated (Administrator). Windows UIPI blocks global hotkeys from non-elevated apps. Run Genshin-Helper as Administrator, or install via **Windows Setup** (creates an elevated Scheduled Task for UAC-free wake). |
| **Windows Auto-HDR / HDR** *(Hard OS/Capture Limit)* | Degraded / SDR fallback | HDR/Auto-HDR tone-mapping shifts pixel brightness thresholds and drops capture alpha channels. Disable **Auto-HDR** for Genshin Impact in Windows Display Settings for best CV accuracy (RGB fallback handles no-alpha frames where possible). |
| **Aspect Ratios (`16:9`, `16:10`, `21:9`, `4:3`)** | Full support | Dynamic Unity `CanvasScaler` HUD & minimap ROI normalization across ultrawide and standard monitors. |
| **Windows DPI Scaling (`100%–200%`)** | Full support | Per-monitor DPI-aware coordinates and independent X/Y scaling to prevent drift on fractional resolutions (e.g. `1366×768` at `125%`). |
| **Keyboard/Mouse & Controller/Gamepad UI** | Full support | Automatic safe-area minimap detection for both keyboard and controller HUD layouts. |
| **Minimap Rotation (Lock-Direction & Follow-View)** | Full support | 360° polar camera cone + 3-vertex avatar direction extraction works in both minimap rotation modes. |

---

## Installation

### Option 1: Windows Setup Installer (Recommended)

1. Download and run `GenshinHelper-Setup-v*.exe` from **Releases**.
2. The installer automatically registers the **Native Messaging Host** (`com.genshinhelper.host`) for Chrome, Edge, and Brave, and configures an elevated Scheduled Task so the Extension popup can wake the desktop engine without repeated UAC prompts.
3. Follow the bundled **Install Browser Extension** guide (`extension/` directory, pinned Extension ID `nfkijdoblmphmdljfogooajhbldkeojh`).

### Option 2: Run from Source (Windows 10/11 64-bit, Python 3.11+)

```powershell
git clone https://github.com/<owner>/Genshin-Helper.git
cd Genshin-Helper

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pillow

Copy-Item config.example.json config.json
```

Hydrate local HoYoLAB map data (tiles, waypoints, cave floor masks) into your local cache:

```powershell
.\.venv\Scripts\python.exe scripts/sync_hoyolab_data.py
```

Load the MV3 Browser Extension in Chrome / Edge / Brave:
1. Open `chrome://extensions` (or `edge://extensions`) and enable **Developer mode**.
2. Click **Load unpacked** and select the `extension/` folder.
3. Pin **Genshin Helper** to the toolbar and open the HoYoLAB Interactive Map.

Start the desktop engine (from an elevated PowerShell window if Genshin Impact runs as Admin):

```powershell
.\.venv\Scripts\python.exe main.py
```

---

## Architecture Overview

```text
+----------------------------------+        localhost:8765 (HTTP/WS)        +---------------------------------+
|   Genshin-Helper Desktop (Win64) | <====================================> | Chrome / Edge / Brave Extension |
|                                  |   Lossless /poll?since=<seq> ring buf  |  (MV3 Service Worker + Popup)   |
|  - Passive Capture (DXGI / MSS)  |                                        |  - HoYoLAB Map DOM Bridge       |
|  - Map M + Minimap CV Engine     |        Native Messaging (stdio)        |  - Zero-Throttle Background Sync|
|  - 360° Heading & Layered HUD    | <------------------------------------> |  - TSP Overlay & Pin Exporter   |
|  - Local Cache (%LOCALAPPDATA%)  |    com.genshinhelper.host (pinned ID)  +---------------------------------+
+----------------------------------+
```

- **Desktop CV Engine (`src/`):** Screen capture (`capture.py`), multi-map & underground locator (`locator.py`, `zoom_slider.py`, `map_state.py`), minimap tracking & 360° heading, single-monitor HUD toast & audio feedback (`single_monitor.py`), atomic local asset hydration (`asset_setup.py`), and diagnostics (`diagnostics.py`).
- **Sync Server (`src/server.py`):** Dual HTTP + WebSocket localhost server using a monotonic `/poll?since=<seq>` event ring buffer so multiple HoYoLAB tabs never drop or steal teleport/mark events.
- **Browser Extension (`extension/`):** Manifest V3 Control Center with pinned Extension ID (`nfkijdoblmphmdljfogooajhbldkeojh`), zero-throttle background worker, and clean map surface.

---

## Verification & Release Auditing

```powershell
.\.venv\Scripts\python.exe tests/verify_v45_release.py
.\.venv\Scripts\python.exe tests/verify_single_monitor.py
.\.venv\Scripts\python.exe tests/verify_public_readiness_v5.py
.\.venv\Scripts\python.exe scripts/audit_release.py
.\.venv\Scripts\python.exe scripts/collect_licenses.py
```

---

## License & Notices

- Source code: [MIT License](LICENSE).
- Third-party attributions & HoYoverse IP disclaimer: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- Security & privacy policy: [SECURITY.md](SECURITY.md).
- Contributing guidelines: [CONTRIBUTING.md](CONTRIBUTING.md).
