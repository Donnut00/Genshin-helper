# Charter: Genshin-Helper

<!-- Drafted by the agent, confirmed by the user. Bullet ids are read by `distill check`:
     G goal, V core value, C constraint, N non-goal. -->

distribution: public

## Goals
- [G1] Real-time passive player localization (Map M anchor + continuous Minimap tracking + multi-floor detection) with sub-meter coordinate alignment matching exact in-game player position on HoYoLAB Interactive Map.
- [G2] Ultra-responsive bilingual (VI/EN) Navigation Mini-HUD with an accurate 360-degree directional arrow synced to real camera/player facing, floor-mismatch alerts, and proximity spot hints.
- [G3] Smart farming ergonomics: TSP route planning on HoYoLAB pins, optional LAN companion view, and cross-map pin export/sync from HoYoLAB (`act.hoyolab.com/ys/app/interactive-map`) to popular open-source maps.
- [G4] Public GitHub release readiness: clean open-source licensing, zero copyrighted game/HoYoLAB assets bundled in git, reproducible builds, and clear ToS/safety documentation.

## Core values (what we optimise for)
- [V1] Strict anti-cheat safety & legality: 100% passive screen capture and browser DOM/store sync; zero memory reads/writes, zero DLL injection, zero SendInput/macro automation against the game.
- [V2] Sustainable low-overhead performance: bounded O(1) RAM via LRU tile cache, sub-1ms ROI screen capture, <5% single-core CPU while exploring, zero Electron/WebView bloat.
- [V3] Maximum resilience across user game settings: adapt cleanly to aspect ratios (16:9, 16:10, 21:9), DPI scaling, Lock-Direction & Follow-View minimaps, languages, graphics settings, and surface hard OS/game limits (Exclusive Fullscreen, UIPI Admin) transparently.
- [V4] Clean license & IP hygiene: strictly enforce SPDX ceilings for public distribution (no code copied from unlicensed/copyleft repos; download third-party map tiles/POIs into local user cache instead of committing to git).
- [V5] First-class bilingual UX (Vietnamese & English) across Extension, HUD, Tray, and diagnostics.

## Constraints
- [C1] Python 3.x (.venv) + OpenCV/NumPy + native Win32 APIs (GDI SRCCOPY, UpdateLayeredWindow, WinRT OCR) on Windows 10/11, paired with the MV3 Chrome Extension.
- [C2] Preserve existing Map M invariants (`tests/verify_v4_suite.py`, `verify_v45_release.py`, `verify_single_monitor.py`, pinned Extension ID, lossless `/poll?since=<seq>`).

## Non-goals
- [N1] No auto-play, auto-pickup, macro input emulation, or game memory reading.
- [N2] No Always-on-Top / transparent click-through wrapper for the HoYoLAB browser window.
- [N3] No device-to-device cloud backup replacement (HoYoLAB already provides native cloud sync); export targets cross-map interoperability only.
