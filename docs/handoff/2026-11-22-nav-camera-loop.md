# Hand-off: nav UX v5.4, camera heading, live loop speed-up (2026-11-22)

Everything below is committed on `main` locally and **not pushed**. Version string is still `5.3.0`
(`src/config.py`); bump it when this ships.

## 1. What shipped in this round

| Area | State | Where |
|---|---|---|
| Nav UX features | Done, tested | `src/nav_controller.py`, `src/pin_guide.py`, `src/nav_hud.py`, `extension/page_main.js`, `src/server.py` |
| Tampermonkey path removed (MV3 extension only) | Done | deleted `tampermonkey/`, `scripts/update_tampermonkey.py`, `update_tampermonkey.bat` |
| Opening a clustered pin's popup on the HoYoLAB map | Done | `extension/page_main.js` `_openPinPopup` |
| HUD stays visible on Alt+Tab when the game is still visible (2nd monitor / uncovered) | Done | `main.py` background branch, `src/capture.py` `is_game_visible_unfocused` |
| Target re-lock: proximity takeover, teleport, clearly-closer | Done, **not yet confirmed in game** | `src/nav_controller.py` |
| Camera heading fuser hardening | Done | `src/heading360.py` `CameraHeadingFuser.update` |
| Two-rate live loop (heading fast path) + 1 ms timer | Done; live rate after the timer fix **not yet measured** | `main.py`, `src/minimap_tracker.py` |
| Minimap recorder (`--record-minimap SECONDS`) | Done | `src/minimap_recorder.py`, `record_minimap.bat` |
| Diagnostics report includes `nav_state` (target, heading sources, nearby pins) | Done | `src/diagnostics.py`, `main.py` `current_nav_state` |
| Test HUD windows no longer flash on the desktop | Done | `SCREEN_OFFSET` on `NavMiniHud` / `HudToastOverlay` |

The nav UX features in the first row:
- A stable target lock while the web feed churns.
- Nearest pin chosen after a collect, with debounce.
- Undo re-inserts a pin the web dropped.
- Calm needle and radar.
- HUD labels and cards.
- An in-game Guide Card (`src/pin_guide.py`), which fetches the HoYoLAB `point/info` notes and image in
  the background.
- A player pane and individual-pin feed on the web side.
- Nav only to uncompleted, enabled pins.

## 2. How the pieces work

### 2.1 Navigation target selection (`NavigationController`)
Order inside `compute_guidance(space_key, player_pos, ...)`:
1. `prev_pos` = last position in this space. If the jump is `> TELEPORT_JUMP_M` (50 m), call
   `_relock_after_jump`, which forgets the auto lock and the cached order. It does nothing in route mode,
   with a pinned override, or when the current lock was hand-picked (`_user_locked_id`, set by
   `select_target` / `next_target` / `prev_target`).
2. `_resolve_target`: sticky lock (unchanged behaviour).
3. `_proximity_takeover`:
   - The nearest pin within `PROXIMITY_TAKEOVER_M` (10 m) wins at once, provided the current target is
     more than 15 m further away.
   - **Clearly-closer rule:** a pin that is ≤ `CLOSER_RATIO` (0.5×) the target distance **and**
     ≥ `CLOSER_MARGIN_M` (30 m) nearer wins after `CLOSER_DWELL_S` (0.8 s).
   - The dwell uses `self._clock`, which tests inject.

Why: report `diag_20261122_115106` (map 36). The arrow pointed at "Câu đố nhỏ" 252 m away while an Oculus
was about 20 m away. The old sticky lock only yielded at 10 m.

Pins excluded from nav: completed pins, disabled pins, and the parent labels in
`NAV_EXCLUDED_PARENT_LABELS` (1 and 426) in `page_main.js`.

### 2.2 HoYoLAB map (extension)
- Individual markers: `_collectIndividualMarkers(ctx)` combines the RBush entries in
  `ciLayer._latlngMarkers` with `_topClusterLevel.getAllChildMarkers()`. Markers carry `pointId` and
  `attrs.config`.
- Opening a clustered pin:
  1. `_openPinPopup` calls `setView(latlng, max(zoom, maxZoom-1))` until `marker._map` is set.
  2. It then calls `ciLayer.onMarkerClick(evt, [{data: marker}])` with a **full** event
     (`latlng`, `containerPoint`, `layerPoint`, `originalEvent`, `target`). With only `containerPoint`,
     `_executeListeners` crashes.
  3. Fallbacks: `openPopup`, then `_executeListeners`.
  4. The original view is restored afterwards.
- `page_main.js` uses **CRLF** line endings; keep them.
- Never click "Ghim" or change the user's real HoYoLAB marks while testing.

### 2.3 Camera view cone (`src/heading360.py`)
- `extract_camera_view_cone` has three tiers:
  - `bgra_alpha` never fires live, because alpha is always 255.
  - `bgr_blend` is accurate.
  - `hsv_adaptive` is the weak tier.
- `CameraHeadingFuser`:
  - Temporal gate.
  - **New:** a large jump in the guess that disagrees with the body heading (or comes while the body is
    not OK) is ignored and never self-confirms. This stops a run of consistent false tier-3 guesses from
    taking over.
- Live cone model, measured from the user's recording:
  - Radial fade, `in = a·out + b`.
  - `b ≈ 70` at the centre, falling to 0 at r ≈ 0.8.
  - Stored in `scratch/yt_bench/blend_model.json` (local only).

### 2.4 Live loop (`main.py` `tracking_loop`)
**Constants:**
- `MINIMAP_TICK_S = 1/60`: target tick.
- `MINIMAP_FULL_STEP_S = 0.05`: a full position step at least every 50 ms, i.e. 20 Hz.
- `WINDOW_LOOKUP_CACHE_S = 0.25`.

**Fast path.** `heading_fast_ok` requires all of:
- closed state, minimap active, tracking;
- less than 50 ms since the last full step;
- not recording.

When it holds, `_tick_worker(fast_ok=True)` captures only `tracker.minimap_capture_rect(...)` and calls
`tracker.step_headings_only(img)`:
- It returns `None` unless the tracker is TRACKING and not paused.
- It rejects flat or dark frames.
- Otherwise it updates the headings with a forced gate and returns a pose with
  `match_method="heading_fast"`, `should_broadcast_web=False`.
- The tick mode is `"minimap_fast"`. Downstream code treats it like `"minimap"`, and
  `allow_heading_only` also accepts `"heading_fast"`.

**Pacing:**
- `_HighResTimer.set(True)` (`winmm.timeBeginPeriod(1)`) only while live minimap tracking runs.
- It is switched off in standby, in the background branch and on exit.
- Sleep is `remaining` if more than 1 ms is left; on overrun, `sleep(0)`.
- Web broadcast is throttled to 0.05 s.

**Telemetry.** A `[Loop] minimap X Hz (heading fast N%, full Y Hz), tick work med/p90` line every 10 s.

**Measured facts (Windows, this machine):**

| Fact | Value |
|---|---|
| `asyncio.sleep(3 ms)` actual length | 15.5 ms; 3.5 ms with `timeBeginPeriod(1)` |
| GDI BitBlt of a screen region | ~16 ms (DWM sync); main cost of a full step |
| `bettercam` (DXGI Desktop Duplication) grab | 0.09 ms, but only from the thread that created it (cross-thread: 47/60 failures) |
| Second monitor | rotated 270° |
| Live, before the fast path | 16 Hz |
| User-measured, fast path without the timer fix | 33 Hz (53% fast, full 15.7 Hz, work med 14 ms / p90 25 ms) |
| Expected with the timer fix | 45–55 Hz (**unmeasured**) |

### 2.5 Recorder
- `python main.py --record-minimap 90` runs the normal app and records the HUD as lossless BGRA PNGs.
  `record_minimap.bat` does the same with elevation, after freeing port 8765.
- Each frame is saved with its pose metadata (`meta.jsonl`), plus `info.json`.
- Output goes to `scratch/minimap_rec/rec_YYYYMMDD_HHMMSS/`.
- "REC OK" is flashed when finished.
- The fast path is disabled while recording, so every frame gets a full step.

### 2.6 Diagnostics
`capture_and_create_bundle(..., nav_state=...)` writes `nav_state` into the report. It contains:
- state, target, distance, bearing, delta, player;
- heading `view` / `camera` / `body` / `used` / `camera_source`;
- `hud_visible`;
- `nearby`: the 6 nearest candidate pins.

Use it to debug wrong-target reports.

## 3. Benchmark findings (camera heading)

| Detector | Result on the curated set |
|---|---|
| `extract_camera_view_cone` raw | 42/43, median 0.1° (wrong on #85, city) |
| Shipped (fuser-gated, from the recording) | 42/43, median 0.7° |
| Prototype `cone_v3` (paired-edge wedge matched filter) | 43/43, max 1.0°, **but 9 ms per frame and misses dark caves** |

- The curated set is 43 hand-verified frames covering 11 terrain types, taken from the user's recording.
- `cone_v3` was **not shipped**; the user deferred it.
- YouTube clips are useless for the cone: compression removes it. They were deleted.
- Eye-labelled ground truth was 25–40° off, so ground truth comes from forced-choice overlay checks:
  - per-frame consensus, stored in `curate/gt.json`;
  - #85 set to 96° by hand;
  - #121 rejected.

## 4. Local-only data (gitignored `scratch/`, not in the commit)

| Path | Size | What |
|---|---|---|
| `scratch/minimap_rec/rec_20261122_043401/` | ~349 MB | User's lossless live recording (2191 frames) |
| `scratch/yt_bench/` | ~277 MB | Bench scripts and caches, listed below |
| `scratch/diagnostics/diag_*` | — | Report fixtures the tests read, incl. `diag_20261122_032842`, `diag_20261122_115106` |

Contents of `scratch/yt_bench/`:
- Scripts: `bench_camera.py`, `cone_v2.py`, `cone_v3.py`, `eval_curated.py`, `eval_live.py`,
  `gt_fit.py`, `blend_fit*.py`, `tune_wedge.py`, `ref_probe.py`.
- Model: `blend_model.json`.
- Cache: `cache/live_rec.npz` and others.
- Curation: `curate/` (`cands.json`, `cand_values.json`, `gt.json`, verification PNGs), `label_live/`.

To re-run the curated evaluation:
`.venv/Scripts/python.exe scratch/yt_bench/eval_curated.py`.

## 5. Tests

- Use `.venv/Scripts/python.exe`. Every test is a standalone script, `tests/verify_*.py`; run them one by
  one, e.g. in a loop over the folder.
- The full suite takes about 60 s: 16 scripts.
- This round's tests are in `tests/verify_nav_ux_v54.py`, among them:
  - `verify_proximity_takeover`
  - `verify_teleport_and_clearly_closer_relock`
  - `verify_camera_heading_fuser`
  - `verify_web_feed_individual_pins`
  - `verify_hud_background_visibility_and_report`
  - `verify_minimap_recorder`
  - `verify_heading_fast_path_and_pacing`
- **Depend on the environment.** These pass with Genshin closed and fail while `GenshinImpact.exe` runs:
  - `verify_capture_no_flicker.py`: it expects a zero-variance capture.
  - `verify_single_monitor.py`: a real game window is found.
- `verify_nav_hud_v5.py` counts USER/GDI handles. It waits up to 1 s for the count to settle, because
  Windows frees them after the thread exits. One run hit a flaky +1; if it recurs, re-run before
  debugging.
- Several tests read `scratch/diagnostics/diag_*`. On a fresh clone without those fixtures they cannot
  run.
- Test HUD windows are drawn off-screen: tests set `SCREEN_OFFSET = (-20000, -20000)`. Keep this in new
  HUD tests.

## 6. Open items, in suggested order

1. **Measure the live loop after the timer fix.**
   - Run the app in game for 30 s or more and read the `[Loop]` line.
   - Target: 45–55 Hz with the full step near 20 Hz.
2. **Confirm the re-lock in game.** Stand near a pin while the arrow points at a far one: within 1 s it
   should switch. Each new report now includes `nav_state.nearby`.
3. **DXGI capture** to remove the 16 ms BitBlt.
   - Run `bettercam` on a dedicated capture thread that owns the camera.
   - Map the game window to its output/monitor; the second monitor is rotated 270°, so handle rotation.
   - Hand frames to the loop through a 1-slot queue.
   - Keep GDI as the fallback.
4. **Camera tier-3 replacement.** Use `cone_v3` (or a cheaper variant) in place of `hsv_adaptive`
   only. Needs:
   - fewer than 3 ms per frame (coarse angle search, then refine);
   - a dark-cave fallback;
   - re-evaluation on the curated set.
5. **Refresh the GitNexus index:** `npx gitnexus analyze`. `AGENTS.md` and `CLAUDE.md` carry an
   uncommitted symbol-count change from the index tool; they were left out of the commit on purpose.
6. **Push and release** when the user asks: bump `APP_VERSION`, push `main`.

## 7. House rules for this repo

- Run `gitnexus impact` before editing any symbol, and warn on HIGH or CRITICAL risk. Run
  `gitnexus_detect_changes` before every commit.
- Commit or push only when asked.
- Line endings: `extension/page_main.js` and `record_minimap.bat` use CRLF; the other files use LF.
- Downloads need the user's explicit OK.
- Do not accept cookie banners in the browser.
