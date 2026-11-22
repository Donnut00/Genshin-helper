# Migration Map (`rust/MAP.md`)

> Single source of truth for wave progress, unit ownership, and exit gates across `/teamwork` sessions.
> Full architecture, HTTP/WS/Native-Messaging contracts, invariants, and crate pins live in [docs/rust-migration-plan.md](../docs/rust-migration-plan.md).

## Global Invariants (Quick Reminder for Every Worker)
- **Untouched**: `extension/**` (100% immutable), `scratch/**` (preserve all offline fixtures), `src/**` & `main.py` (reference oracle until Phase 4 cutover).
- **Build cache off `E:`**: Repo root `/.cargo/config.toml` points to `D:\Dev\CargoTarget\Genshin-Helper`. Parallel worktree workers MUST set `$env:CARGO_TARGET_DIR = "D:\Dev\CargoTarget\Genshin-Helper-<worker>"` to avoid `.cargo-lock` contention.
- **Cargo manifest ownership**: Only the Coordinator edits root `rust/Cargo.toml` `[workspace.dependencies]` and this `rust/MAP.md`.
- **Game safety**: Read-only only; ZERO `SendInput` against the game window; GDI `SRCCOPY` only (never `CAPTUREBLT`); `WDA_EXCLUDEFROMCAPTURE` refreshed every 2s & toggled via `0x8015`; `WORLD_ANCHOR_Y_OFFSET = -8.5`.
- **Gate before reporting done**:
  - `cargo nextest run --manifest-path rust/Cargo.toml -p <crate>`
  - `cargo clippy --manifest-path rust/Cargo.toml -p <crate> --all-targets -- -D warnings`
  - `.\.venv\Scripts\python.exe scripts/audit_release.py --dir rust/crates/<crate> --strict-ip`

---

## Wave 1 — Foundation & Workspace Scaffolding (`done`)

| Unit | Target | Status | Gate |
|---|---|---|---|
| Architecture & contract spec | `docs/rust-migration-plan.md` | `done` | `audit_release.py --dir docs --strict-ip` PASS |
| Workspace + release hygiene + nextest profiles | `rust/Cargo.toml`, `rust/.cargo/config.toml`, `rust/.config/nextest.toml`, `/.cargo/config.toml` | `done` | `cargo nextest` + `cargo clippy -- -D warnings` PASS; 0 bytes in `rust/target` |
| Shared constants, `AppConfig` + `HoyolabSyncConfig`, `FrameBuf`, `protocol` DTOs, acyclic `traits` | `gh-core::{lib,config,paths,frame,ui_layout,nav_controller,protocol,traits}` | `done` | 7/7 `gh-core` tests PASS |
| Crate skeletons | `gh-win32`, `gh-net`, `gh-cv`, `gh-hud`, `gh-host`, `gh-app` | `done` | Acyclic DAG verified via `cargo metadata` |

---

## Wave 2A — `gh-core` Full Implementation (Run FIRST in Batch 1)

| Python Source | Rust Target | Status | Owner | Oracle / Gate |
|---|---|---|---|---|
| `src/paths.py` | `gh-core::paths` | `open` | — | `verify_v45_release.py` |
| `src/image_io.py` (atomic JSON/bytes) | `gh-core::atomic_io` | `open` | — | `verify_perf_and_settings_v5.py` |
| NumPy `.npy`/`.npz` I/O (`u8/i32/i64/f32`) | `gh-core::npz` | `open` | — | Cross-read `src/data/*.npz` / `scratch/` |
| `src/i18n.py` + `src/locales/*.json` | `gh-core::i18n` | `open` | — | `verify_i18n_and_ext_v5.py` |
| `src/game_settings.py` (CanvasScaler + HDR) | `gh-core::ui_layout` | `open` | — | `verify_perf_and_settings_v5.py`, `verify_minimap_tracker_v5.py` |
| `src/hd_assets.py` (MultimapDb model) | `gh-core::multimap` | `open` | — | `verify_v53_production.py` |
| `src/asset_setup.py` | `gh-core::assets` | `open` | — | `verify_public_readiness_v5.py` |
| `src/nav_controller.py` | `gh-core::nav_controller` | `open` | — | `verify_nav_hud_v5.py`, `verify_nav_ux_v54.py` |
| `src/tsp_route.py` | `gh-core::tsp_route` | `open` | — | `verify_tsp_and_lan_v5.py` |
| `src/map_bridge.py` + `category_aliases.json` | `gh-core::map_bridge` | `open` | — | `verify_map_bridge_v5.py` |

---

## Wave 2B — Parallel Platform, Net, Host & HUD (3 Parallel Workers after Wave 2A)

### Track B: `gh-win32`
| Python Source | Rust Target | Status | Owner | Oracle / Gate |
|---|---|---|---|---|
| `src/capture.py` | `gh-win32::capture` | `open` | — | `verify_capture_no_flicker.py`, `verify_hotkey_conflicts_and_capture.py` |
| `src/hotkey.py` | `gh-win32::hotkey` | `open` | — | `verify_single_monitor.py`, `verify_hotkey_conflicts_and_capture.py` |
| `src/hotkey_conflicts.py` | `gh-win32::hotkey_conflicts` | `open` | — | `verify_hotkey_conflicts_and_capture.py` (temp path + `write_registry=false`) |
| `src/game_settings.py` (Win32) | `gh-win32::game_settings` | `open` | — | `verify_perf_and_settings_v5.py`, `verify_nav_hud_v5.py` |
| `src/win_ocr.py` | `gh-win32::win_ocr` | `open` | — | `scratch/ocr_query_*.png` (`OcrProvider`) |
| `src/tray.py` | `gh-win32::tray` | `open` | — | `verify_v45_release.py` |
| `src/single_monitor.py` (audio/switch) | `gh-win32::{audio,switcher}` | `open` | — | `verify_single_monitor.py` |
| `src/i18n.py` (Win32 locale) + mutex/timer | `gh-win32::{locale,runtime}` | `open` | — | `gh-win32` unit tests |

### Track C: `gh-net` + `gh-host`
| Python Source | Rust Target | Status | Owner | Oracle / Gate |
|---|---|---|---|---|
| `src/server.py` | `gh-net::sync_server` | `open` | — | 27 routes, `/poll?since=<seq>`, `/shutdown` 403, 32 MiB body, takeover |
| `src/lan_companion.py` | `gh-net::lan_companion` | `open` | — | `verify_tsp_and_lan_v5.py` (QR SVG, PIN 429, WS 15 Hz) |
| `src/hoyolab_sync.py` | `gh-net::hoyolab_sync` | `open` | — | `verify_public_readiness_v5.py` + `check_online_map_versions` |
| `src/hd_assets.py` (async providers) | `gh-net::hd_assets` | `open` | — | `verify_v53_production.py` |
| `src/pin_guide.py` | `gh-net::pin_guide` | `open` | — | `verify_nav_ux_v54.py`, `verify_hotkey_conflicts_and_capture.py` |
| `src/native_host.py` | `gh-host` (`GenshinHelperHost.exe`) | `open` | — | Black-box 4-byte LE stdio test + `verify_v45_release.py` |

### Track D: `gh-hud`
| Python Source | Rust Target | Status | Owner | Oracle / Gate |
|---|---|---|---|---|
| `src/nav_hud.py` (layered host + fonts) | `gh-hud::{layered,fonts}` | `open` | — | `WM_APP_CAPTURE_AFFINITY` (`0x8015`), 2s WDA refresh |
| `src/nav_hud.py` (renderer + Guide Card) | `gh-hud::nav_hud` | `open` | — | `verify_nav_hud_v5.py`, `verify_nav_ux_v54.py` (headless pixel assertions) |
| `src/single_monitor.py` (toast) | `gh-hud::toast` | `open` | — | `verify_single_monitor.py` |
| `src/diagnostics.py` (image annotation) | `gh-hud::diag_annotate` | `open` | — | Vietnamese UTF-8 PNG render test |

---

## Wave 3 — Pure-Rust Computer Vision (`gh-cv`, Batch 2)

### Gate 0 (Must Pass Before Track E1/E2)
| Scope | Rust Target | Status | Owner | Gate |
|---|---|---|---|---|
| Pure-Rust CV primitives (Sobel/Scharr, FFT & Padmanabhan masked 6-FFT NCC, phaseCorrelate, warpPolar/warpAffine, contours/Canny/CLAHE, DoG RootSIFT, 4-tree KD-Tree, ORB, RANSAC) | `gh-cv::ops` | `open` | — | `--profile cv-heavy --cargo-profile cv-bench` benchmark on `scratch/minimap_rec/` & `scratch/yt_bench/` vs Python oracle |

### Track E1 & E2 (2 Parallel Workers after Gate 0)
| Python Source | Rust Target | Status | Owner | Oracle / Gate |
|---|---|---|---|---|
| `src/image_io.py` (PNG/WebP) | `gh-cv::image_io` | `open` | — | Unicode path PNG/WebP tests |
| `src/zoom_slider.py` | `gh-cv::zoom_slider` | `open` | — | `scratch/0zoom.png`, `scratch/100zoom.png` |
| `src/map_state.py` | `gh-cv::map_state` | `open` | — | `verify_map_state_v5.py` (`0.88`, `0.975`/`0.65`, `0.82`) |
| `src/heading360.py` + `src/tracker.py` | `gh-cv::heading360` | `open` | — | `verify_heading360_v5.py` (<2 deg, <3 ms) |
| `src/locator.py` | `gh-cv::locator` | `open` | — | `verify_v4_suite.py`, `verify_alignment_v5.py` (`-8.5` Y offset) |
| `src/tile_cache.py` | `gh-cv::tile_cache` | `open` | — | `verify_minimap_tracker_v5.py`, `verify_v53_production.py` |
| `src/global_relocator.py` | `gh-cv::global_relocator` | `open` | — | `verify_v53_production.py` (`INDEX_VERSION = 3`) |
| `src/minimap_tracker.py` | `gh-cv::minimap_tracker` | `open` | — | `verify_minimap_tracker_v5.py`, `verify_v53_production.py`, `verify_nav_ux_v54.py` |

---

## Wave 4 — App Wiring, Packaging, Cutover & Docs Sync (Batch 3)

| Scope | Target | Status | Owner | Gate |
|---|---|---|---|---|
| `main.py` + `src/diagnostics.py` + `src/minimap_recorder.py` | `gh-app` (`GenshinHelper.exe`) | `open` | — | Black-box E2E with real `extension/` + `verify_i18n_and_ext_v5.py` |
| Release scripts & Inno Setup | `installer/build_release.ps1`, `scripts/{audit_release.py,collect_licenses.py,register_dev_native_host.ps1}` | `open` | — | `audit_release.py --strict-ip --artifact dist/GenshinHelper` + Setup EXE ~6-10 MB |
| Cutover & Python retirement (Plan 7.1) | Tag `v5.4.0-python-final`, move 4 static assets to `assets/`, delete runtime Python | `open` | — | User live-game approval |
| Documentation & Index sync (Plan 7.2) | `README*.md`, `PRODUCT.md`, `SECURITY.md`, `CONTRIBUTING.md`, `.github/*`, `THIRD_PARTY_NOTICES.md`, `AGENTS.md`, `CLAUDE.md`, `npx gitnexus analyze` | `open` | — | Full audit & clean `git status` |
