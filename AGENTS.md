<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **Genshin-Helper** (8128 symbols, 13330 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/Genshin-Helper/context` | Codebase overview, check index freshness |
| `gitnexus://repo/Genshin-Helper/clusters` | All functional areas |
| `gitnexus://repo/Genshin-Helper/processes` | All execution flows |
| `gitnexus://repo/Genshin-Helper/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Project Invariants

- **Python runtime**: Always run Python scripts, `py_compile`, and tests via `.\.venv\Scripts\python.exe` (system Python lacks `mss`/`cv2`).
- **Offline verification fixtures**: `tests/verify_v4_suite.py` reads gitignored fixtures in `scratch/diagnostics/`, `scratch/Test-images/`, `scratch/0zoom.png`, and `scratch/100zoom.png` — preserve them when cleaning `scratch/`.
- **Multi-tab lossless sync (`src/server.py`)**: `/poll?since=<seq>` uses a monotonic `_event_seq` + `_event_log` ring buffer so background HoYoLAB tabs or overlapping polls never steal teleport events; do not revert to destructive single-consumer pops.
- **Zoom slider anti-false-positive (`src/zoom_slider.py`)**: Diamond thumb detection must verify both `TM_CCORR_NORMED` and zero-mean `TM_CCOEFF_NORMED` (`>= 0.65`); `TM_CCORR_NORMED` alone saturates on flat bright panels (region drawer) and triggers false map-open events.
- **World alignment & compass rejection (`src/locator.py`)**: `terrain_data` Sobel refinement (with `-11.5` Y anchor fallback on maps `2, 9, 34, 36`) corrects Genshin's waypoint foot-anchor vs HoYoLAB's center-anchor offset; `detect_player_arrow` rejects off-screen edge compass badges.
- **Map datasets (`src/data/*_v4.*`)**: Regenerate from HoYoLAB public API via `.\.venv\Scripts\python.exe scripts/sync_hoyolab_data.py`.
- **Off-store pinned Extension ID (`extension/manifest.json`)**: Keep the RSA-2048 SPKI `"key"` (ID `nfkijdoblmphmdljfogooajhbldkeojh`) synchronized with `installer/native_host_manifest.json` and `installer/GenshinHelper.iss`; verify with `.\.venv\Scripts\python.exe tests/verify_v45_release.py`.
- **64-bit Win32 hook prototypes & read-only invariant (`src/hotkey.py`, `src/capture.py`)**: Keep explicit `argtypes`/`restype` on `GetModuleHandleW`/`SetWindowsHookExW`/`CallNextHookEx` (prevents 64-bit `LastError=126`), and never use `SendInput` against the game window; verify with `.\.venv\Scripts\python.exe tests/verify_single_monitor.py`.
- **V5.3 live minimap engine (`src/hd_assets.py`, `src/global_relocator.py`, `src/minimap_tracker.py`)**: Keep `HoyolabHdTileProvider`/`UndergroundOverlayProvider` non-blocking (`async_fetch=True`), check `TeyvatGlobalRelocator.is_ready` before incrementing `_global_query_count`, preserve Follow-View `-theta_mm_to_ref` sign conversion, and keep runtime Paimon auto-bootstrap IP-clean in `%LOCALAPPDATA%\GenshinHelper\cache\paimon_tpl.npz` (skipping `hd_sobel_fast` until bootstrap completes); verify with `.\.venv\Scripts\python.exe tests/verify_v53_production.py`.
- **Smart hotkey suppression & capture-visible Mini-HUD (`src/hotkey.py`, `src/hotkey_conflicts.py`, `src/nav_hud.py`, `src/capture.py`)**: Keep `HOTKEY_NAV_MARK`/`HOTKEY_NAV_UNDO` defaults off Genshin's always-triggered menu keys (`alt+k`/`alt+shift+k`), suppress only the trigger VK in `WH_KEYBOARD_LL` (never modifiers, zero `SendInput`, gated by foreground game/HUD), isolate `NvidiaOverlayAdapter` tests with temp paths + `write_registry=False`, and toggle `WDA_NONE`/`WDA_EXCLUDEFROMCAPTURE` via `WM_APP_CAPTURE_AFFINITY` (`0x8015`) + `_NON_COVERING_CLASSES`/`_NON_COVERING_PROCESSES`; verify with `.\.venv\Scripts\python.exe tests/verify_hotkey_conflicts_and_capture.py`.
