# Third-Party Notices & Attributions

Genshin-Helper is licensed under the [MIT License](LICENSE) for original source code authored in this repository. It incorporates ported MIT-licensed algorithms, relies on open-source runtime dependencies, and interacts locally with third-party map services whose content remains outside this project's license.

---

## 1. Ported Open-Source Modules (MIT License)

The following open-source repositories provided algorithms or modules ported into Genshin-Helper under the MIT License. Each ported module carries a top-of-file attribution header pointing to its upstream commit and source path.

### GengGode/cvAutoTrack (`@336a4a6`, MIT License)
- Repository: <https://github.com/GengGode/cvAutoTrack>
- License: MIT License (Copyright (c) GengGode / cvAutoTrack Contributors)
- Ported modules in Genshin-Helper:
  - `src/heading360.py` — Polar-coordinate 360-degree camera FOV cone extraction (alpha + RGB fallback) and 3-vertex contour avatar direction extraction (ported from `source/algorithms/algorithms.rotation.h` and `source/algorithms/algorithms.direction.h`).
  - `src/game_settings.py` — Unity `CanvasScaler` multi-aspect HUD layout normalization (`16:9`, `16:10`, `21:9`, `4:3`, keyboard & gamepad safe-area) and O(1) Paimon anchor fast-path checks (ported from `source/genshin/genshin.handle.cpp` and `source/genshin/check/paimon/genshin.check.paimon.cpp`).
- Scope note: No patented SURF algorithms and no binary game-icon assets from `cvAutoTrack` are included in Genshin-Helper.

### MaxWay12/Genshin-Navigator (`@99e1e13`, MIT License)
- Repository: <https://github.com/MaxWay12/Genshin-Navigator>
- License: MIT License (Copyright (c) MaxWay12 / Genshin-Navigator Contributors)
- Ported modules in Genshin-Helper:
  - `src/image_io.py` — Unicode-path-safe OpenCV image I/O helpers.
  - `src/asset_setup.py` — Local asset staging, integrity validation, and atomic `_promote_assets` promotion with full rollback (`src/genshin_navigator/asset_setup.py`).
  - `src/minimap_tracker.py` — 4-state minimap tracking FSM (`LOST` / `ACQUIRING` / `TRACKING` / `RELOCATING`), screen gate, and backoff scheduling (`src/genshin_navigator/tracker.py`, `screen_gate.py`, `performance.py`).
  - `src/nav_controller.py` — Sticky target navigation state, undo stack, and hold-to-collect confirmation guard (`src/genshin_navigator/navigation.py`).
  - `scripts/audit_release.py` — Tracked-file and release-artifact privacy/secret/IP leak auditor (`scripts/audit_release.py`).
  - `scripts/collect_licenses.py` — Installed Python dependency license collector (`scripts/collect_licenses.py`).

---

## 2. Runtime & Build Dependencies

Run `.\.venv\Scripts\python.exe scripts/collect_licenses.py` to inspect exact installed package versions and licenses, or pass `--copy-dir <dir>` to bundle full license texts for portable builds.

| Component | Purpose | License Family | Project |
| --- | --- | --- | --- |
| Python | Runtime interpreter | PSF-2.0 | <https://www.python.org/> |
| NumPy | Numerical arrays & matrix math | BSD-3-Clause | <https://numpy.org/> |
| OpenCV (`opencv-python`) | Passive computer vision & template matching | Apache-2.0 | <https://opencv.org/> |
| Pillow | Layered HUD rendering & original icon generation | MIT-CMU / HPND | <https://python-pillow.org/> |
| BetterCam | High-FPS DXGI Desktop Duplication capture | MIT | <https://github.com/RootKit-Org/BetterCam> |
| MSS | Multi-monitor GDI screen capture fallback | MIT | <https://github.com/BoboTiG/python-mss> |
| websockets | Localhost WebSocket sync transport | BSD-3-Clause | <https://websockets.readthedocs.io/> |
| PyWin32 | Windows API bindings | PSF-2.0 | <https://github.com/mhammond/pywin32> |
| pynput | Input listener support | LGPL-3.0 | <https://github.com/moses-palmer/pynput> |
| requests | HTTP client utilities | Apache-2.0 | <https://requests.readthedocs.io/> |
| comtypes | COM interop (required by BetterCam) | MIT | <https://github.com/enthought/comtypes> |
| PyInstaller (build-only) | Windows executable packaging | GPL-2.0-or-later with bootloader exception | <https://pyinstaller.org/> |
| Inno Setup (build-only) | Windows installer packaging | Inno Setup License | <https://jrsoftware.org/isinfo.php> |

> This table is informational. Where an installed package ships its own `LICENSE` or `NOTICE` text, that license text controls.

---

## 3. HoYoverse / HoYoLAB Third-Party IP Disclaimer

**Genshin Impact**, **HoYoLAB**, **HoYoverse**, **miHoYo**, and **Cognosphere** are trademarks, service marks, or copyrights of **COGNOSPHERE PTE. LTD.** and/or **miHoYo Co., Ltd.**

- Genshin-Helper is an independent, unofficial community tool and is **not affiliated with, endorsed by, sponsored by, or approved by** HoYoverse / COGNOSPHERE PTE. LTD.
- HoYoLAB interactive map tiles, terrain images, underground floor overlays, point-of-interest (POI) coordinates, icons, screenshots, and community descriptions are **not licensed under this project's MIT License**.
- Map datasets hydrated locally by the user via `scripts/sync_hoyolab_data.py` into the user's local cache (`%LOCALAPPDATA%\GenshinHelper\data`) are downloaded directly from public HoYoLAB endpoints by the end user for personal reference only and are excluded from this repository's MIT License.
- Original application branding such as `assets/icons/app_compass_origin.png` is original artwork created for Genshin-Helper with zero HoYoverse assets.
