//! 60 Hz heading / 20 Hz full minimap tracking loop, Map (M) FSM, HUD sync, LAN push, and HDR warning.

use crate::state::{AppState, WINDOW_TITLES, json_f64, json_i64, json_truthy};
use gh_core::frame::{FrameBuf, RectI32};
use gh_core::i18n::{get_language, t};
use gh_core::nav_controller::NavState;
use gh_core::ui_layout::compute_ui_layout;
use gh_cv::locator::LocateResult;
use gh_cv::map_state::{
    CLOSE_PROBE_DEFAULT_THRESHOLD, MAX_RETRIES, MapScreenStateMachine, MapState, MapStepResult,
};
use gh_cv::minimap_tracker::{MinimapPose, TrackerState};
use gh_hud::layered::query_monitor_work_area;
use gh_hud::toast::ToastKind;
use gh_net::pin_guide::HoYoLABLabelCatalog;
use gh_win32::capture::WindowFinder;
use gh_win32::game_settings::check_uipi_status;
use gh_win32::runtime::HighResTimer;
use serde_json::{Map, Value, json};
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tokio::sync::watch;

pub const MINIMAP_TICK_S: f64 = 1.0 / 60.0;
pub const MINIMAP_FULL_STEP_S: f64 = 0.05;
pub const WINDOW_LOOKUP_CACHE_S: f64 = 0.25;
pub const HDR_CLIP_STREAK_WARN: usize = 8;

#[inline]
fn angle_diff_deg(a: Option<f64>, b: Option<f64>) -> f64 {
    match (a, b) {
        (None, None) => 0.0,
        (None, _) | (_, None) => 180.0,
        (Some(va), Some(vb)) => (((va - vb + 180.0).rem_euclid(360.0)) - 180.0).abs(),
    }
}

#[derive(Debug, Clone, PartialEq)]
struct MmBcastState {
    map_id: i32,
    lat: f64,
    lng: f64,
    heading: Option<f64>,
    camera_heading: Option<f64>,
    is_underground: bool,
    floor_id: Option<i32>,
}

#[derive(Debug, Clone, PartialEq)]
struct HudSig {
    state: NavState,
    target_id: Option<String>,
    dist_tenth: Option<i64>,
    delta_tenth: Option<i64>,
    floor_mismatch: bool,
    route_target_name: String,
    route_step: Option<usize>,
    route_total: Option<usize>,
    remaining_count: usize,
    show_spot_card: bool,
    rect: (i32, i32, i32, i32),
    position: String,
    lang: String,
    sync_label: String,
    guide_label: String,
    desc: String,
    image_url: String,
}

#[derive(Debug, Clone)]
enum TickMode {
    MinimapFast,
    Minimap,
    MapM,
    MapMPaused,
    IdleClosed,
}

struct TickOutput {
    mode: TickMode,
    full_frame: Option<FrameBuf>,
    step_res: Option<MapStepResult>,
    pose: Option<MinimapPose>,
}

struct PendingVerify {
    ticks: usize,
    res: LocateResult,
}

struct TickParams {
    cl: i32,
    ct: i32,
    cw: i32,
    ch: i32,
    is_closed: bool,
    do_auto_sync: bool,
    do_minimap: bool,
    fast_ok: bool,
}

fn run_tick_worker(state: &AppState, p: TickParams) -> TickOutput {
    let TickParams {
        cl,
        ct,
        cw,
        ch,
        is_closed,
        do_auto_sync,
        do_minimap,
        fast_ok,
    } = p;
    if fast_ok {
        let fast_rect_opt = {
            let mut tr_guard = state.tracker.lock().unwrap_or_else(|e| e.into_inner());
            tr_guard
                .as_mut()
                .and_then(|(tr, _)| tr.minimap_capture_rect(cw as u32, ch as u32, false).ok())
        };
        if let Some(rx_rect) = fast_rect_opt
            && rx_rect.width > 0
            && rx_rect.height > 0
        {
            let mm_fast = state.capturer.capture_region_bgra(
                cl + rx_rect.x,
                ct + rx_rect.y,
                rx_rect.width,
                rx_rect.height,
            );
            let now = state.start_instant.elapsed().as_secs_f64();
            let fast_pose = {
                let mut tr_guard = state.tracker.lock().unwrap_or_else(|e| e.into_inner());
                tr_guard.as_mut().and_then(|(tr, last_ts)| {
                    let ts = if now < *last_ts { *last_ts } else { now };
                    *last_ts = ts;
                    tr.step_headings_only(mm_fast.as_ref().map(|f| f.as_view()), Some(ts))
                })
            };
            if let Some(pose) = fast_pose {
                return TickOutput {
                    mode: TickMode::MinimapFast,
                    full_frame: None,
                    step_res: None,
                    pose: Some(pose),
                };
            }
        }
    }

    if is_closed {
        let probe_hit = {
            let mut sm = state
                .state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            if sm.has_anchor_templates() {
                let pr = MapScreenStateMachine::get_close_probe_rect(cl, ct, cw, ch);
                if pr.width > 0 && pr.height > 0 {
                    let roi = state
                        .capturer
                        .capture_region(pr.x, pr.y, pr.width, pr.height);
                    sm.is_close_x_probe_frame(
                        roi.as_ref().map(|f| f.as_view()),
                        ch as usize,
                        CLOSE_PROBE_DEFAULT_THRESHOLD,
                    )
                    .0
                } else {
                    false
                }
            } else {
                let pr = MapScreenStateMachine::get_slider_probe_rect(cl, ct, cw, ch);
                if pr.width > 0 && pr.height > 0 {
                    let roi = state
                        .capturer
                        .capture_region(pr.x, pr.y, pr.width, pr.height);
                    MapScreenStateMachine::is_slider_probe_frame(
                        roi.as_ref().map(|f| f.as_view()),
                        cw as usize,
                        ch as usize,
                    )
                    .0
                } else {
                    false
                }
            }
        };

        if probe_hit {
            state.pause_minimap_tracker("map_m_probe");
            if do_auto_sync {
                let Some(frame) = state.capturer.capture_region(cl, ct, cw, ch) else {
                    return TickOutput {
                        mode: TickMode::MapM,
                        full_frame: None,
                        step_res: None,
                        pose: None,
                    };
                };
                let step = state
                    .state_machine
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .step_frame(Some(frame.as_view()));
                return TickOutput {
                    mode: TickMode::MapM,
                    full_frame: Some(frame),
                    step_res: Some(step),
                    pose: None,
                };
            }
            return TickOutput {
                mode: TickMode::MapMPaused,
                full_frame: None,
                step_res: None,
                pose: None,
            };
        }

        if do_minimap && let Ok(layout) = compute_ui_layout(cw as u32, ch as u32, false) {
            let mm = layout.minimap_rect;
            let pm = layout.paimon_maybe_rect;
            let cal_pad_x = (48.0 * layout.scale_ui).round() as i32;
            let cal_pad_y = (28.0 * layout.scale_ui).round() as i32;
            let hud_w = cw.min((mm.x + mm.width + cal_pad_x).max(pm.x + pm.width));
            let hud_h = ch.min((mm.y + mm.height + cal_pad_y).max(pm.y + pm.height));
            let hud_bgra = if hud_w > 0 && hud_h > 0 {
                state.capturer.capture_region_bgra(cl, ct, hud_w, hud_h)
            } else {
                None
            };

            let rec_opt = state
                .minimap_recorder
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone();

            let now = state.start_instant.elapsed().as_secs_f64();
            let pose_opt = {
                let mut tr_guard = state.tracker.lock().unwrap_or_else(|e| e.into_inner());
                tr_guard.as_mut().and_then(|(tr, last_ts)| {
                    let ts = if now < *last_ts { *last_ts } else { now };
                    *last_ts = ts;
                    let pose = tr
                        .step_from_hud(
                            hud_bgra.as_ref().map(|f| f.as_view()),
                            cw as u32,
                            ch as u32,
                            Some(ts),
                            false,
                            None,
                        )
                        .ok()?;
                    if let Some(ref rec) = rec_opt
                        && rec.active()
                    {
                        let mut meta = Map::new();
                        meta.insert("state".to_owned(), json!(pose.state.as_str()));
                        meta.insert("match".to_owned(), json!(pose.match_method));
                        meta.insert("map_id".to_owned(), json!(pose.map_id));
                        meta.insert("world_x".to_owned(), json!(pose.world_x));
                        meta.insert("world_y".to_owned(), json!(pose.world_y));
                        meta.insert("span_world".to_owned(), json!(pose.span_world));
                        meta.insert("map_rotation_deg".to_owned(), json!(pose.map_rotation_deg));
                        meta.insert("underground".to_owned(), json!(pose.is_underground));
                        meta.insert("camera".to_owned(), json!(pose.camera_heading));
                        meta.insert("body".to_owned(), json!(pose.body_heading));
                        meta.insert("view".to_owned(), json!(pose.view_heading));
                        meta.insert("cal_dx_px".to_owned(), json!(pose.cal_dx_px));
                        meta.insert("cal_dy_px".to_owned(), json!(pose.cal_dy_px));
                        rec.add(hud_bgra.as_ref(), cw, ch, ts, Some(meta));
                    }
                    Some(pose)
                })
            };

            if let Some(pose) = pose_opt {
                return TickOutput {
                    mode: TickMode::Minimap,
                    full_frame: None,
                    step_res: None,
                    pose: Some(pose),
                };
            }
        }

        return TickOutput {
            mode: TickMode::IdleClosed,
            full_frame: None,
            step_res: None,
            pose: None,
        };
    }

    state.pause_minimap_tracker("map_m_open");
    let Some(frame) = state.capturer.capture_region(cl, ct, cw, ch) else {
        return TickOutput {
            mode: TickMode::MapM,
            full_frame: None,
            step_res: None,
            pose: None,
        };
    };
    let step = state
        .state_machine
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .step_frame(Some(frame.as_view()));
    TickOutput {
        mode: TickMode::MapM,
        full_frame: Some(frame),
        step_res: Some(step),
        pose: None,
    }
}

async fn sleep_or_shutdown(dur: Duration, shutdown_rx: &mut watch::Receiver<bool>) -> bool {
    if *shutdown_rx.borrow() {
        return true;
    }
    if dur.is_zero() {
        tokio::task::yield_now().await;
        return *shutdown_rx.borrow();
    }
    tokio::select! {
        _ = tokio::time::sleep(dur) => *shutdown_rx.borrow(),
        _ = shutdown_rx.changed() => *shutdown_rx.borrow(),
    }
}

pub async fn run_tracking_loop(state: Arc<AppState>, mut shutdown_rx: watch::Receiver<bool>) {
    tracing::info!(
        "Tracking loop running (v5.4 Hybrid Minimap + 3-Anchor Map M FSM). Waiting for Genshin Impact window..."
    );

    let mut hires_timer = HighResTimer::new();
    let mut window_attached = false;
    let mut last_res: Option<(i32, i32)> = None;
    let mut pending_verify: Option<PendingVerify> = None;
    let mut uipi_checked_hwnd: Option<isize> = None;
    let mut nav_hud_visible = false;
    let mut last_hud_sig: Option<HudSig> = None;
    let mut last_mm_bcast_ts: Option<Instant> = None;
    let mut last_mm_bcast_state: Option<MmBcastState> = None;
    let mut last_full_step_mono: Option<Instant> = None;
    let mut last_pose_tracking = false;
    let mut cached_win_info: Option<(isize, i32, i32, i32, i32)> = None;
    let mut cached_win_mono: Option<Instant> = None;

    let mut rate_win_start: Option<Instant> = None;
    let mut rate_minimap_full: usize = 0;
    let mut rate_minimap_fast: usize = 0;
    let mut rate_work_ms: Vec<f64> = Vec::new();

    let mut gate_reject_streak: usize = 0;
    let mut last_nav_target_id: Option<String> = None;
    let mut hdr_clip_streak: usize = 0;
    let mut hdr_warned = false;

    let reset_rate_window = |start: &mut Option<Instant>,
                             full: &mut usize,
                             fast: &mut usize,
                             work: &mut Vec<f64>| {
        *start = None;
        *full = 0;
        *fast = 0;
        work.clear();
    };

    let hide_nav_hud = |visible: &mut bool, sig: &mut Option<HudSig>| {
        if *visible || state.nav_hud.is_visible() {
            state.nav_hud.hide();
            *visible = false;
        }
        *sig = None;
    };

    while state.server.is_running() && !*shutdown_rx.borrow() {
        if !state.engine_active.load(Ordering::Acquire) {
            if window_attached {
                window_attached = false;
                last_res = None;
            }
            state
                .state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .reset();
            pending_verify = None;
            uipi_checked_hwnd = None;
            last_mm_bcast_state = None;
            state.pause_minimap_tracker("standby");
            hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
            hires_timer.set(false);
            reset_rate_window(
                &mut rate_win_start,
                &mut rate_minimap_full,
                &mut rate_minimap_fast,
                &mut rate_work_ms,
            );
            if sleep_or_shutdown(Duration::from_millis(350), &mut shutdown_rx).await {
                break;
            }
            continue;
        }

        let tick_start = Instant::now();
        let win_info = if let (Some(cached), Some(ts)) = (cached_win_info, cached_win_mono)
            && tick_start.duration_since(ts).as_secs_f64() < WINDOW_LOOKUP_CACHE_S
        {
            Some(cached)
        } else {
            let found = WindowFinder::get_game_window(&WINDOW_TITLES, false);
            cached_win_info = found;
            cached_win_mono = Some(tick_start);
            found
        };

        let Some((hwnd, left, top, width, height)) = win_info else {
            if window_attached {
                tracing::warn!("Genshin Impact window lost. Waiting for window...");
                window_attached = false;
                {
                    let mut gs = state.game_state.lock().unwrap_or_else(|e| e.into_inner());
                    gs.hwnd = None;
                    gs.rect = None;
                    gs.uipi_status = None;
                }
                last_res = None;
                uipi_checked_hwnd = None;
                if let Some(tray) = state
                    .tray
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .clone()
                {
                    tray.set_uipi_warning(false);
                }
            }
            state
                .state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .reset();
            pending_verify = None;
            last_mm_bcast_state = None;
            state.pause_minimap_tracker("window_lost");
            hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
            reset_rate_window(
                &mut rate_win_start,
                &mut rate_minimap_full,
                &mut rate_minimap_fast,
                &mut rate_work_ms,
            );
            if sleep_or_shutdown(Duration::from_millis(800), &mut shutdown_rx).await {
                break;
            }
            continue;
        };

        let game_rect = RectI32 {
            x: left,
            y: top,
            width,
            height,
        };
        {
            let mut gs = state.game_state.lock().unwrap_or_else(|e| e.into_inner());
            gs.hwnd = Some(hwnd);
            gs.rect = Some(game_rect);
        }

        if width < 800 || height < 500 {
            state
                .state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .reset();
            pending_verify = None;
            state.pause_minimap_tracker("window_small");
            hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
            reset_rate_window(
                &mut rate_win_start,
                &mut rate_minimap_full,
                &mut rate_minimap_fast,
                &mut rate_work_ms,
            );
            if sleep_or_shutdown(Duration::from_millis(500), &mut shutdown_rx).await {
                break;
            }
            continue;
        }

        if !window_attached || last_res != Some((width, height)) {
            tracing::info!("Attached to Genshin Impact window ({width}x{height})");
            window_attached = true;
            last_res = Some((width, height));
        }

        if hwnd != 0 && uipi_checked_hwnd != Some(hwnd) {
            uipi_checked_hwnd = Some(hwnd);
            if let Ok(uipi) = tokio::task::spawn_blocking(move || check_uipi_status(Some(hwnd))).await
            {
                let mismatch = uipi.mismatch;
                {
                    let mut gs = state.game_state.lock().unwrap_or_else(|e| e.into_inner());
                    gs.uipi_status = Some(uipi.clone());
                }
                if let Some(tray) = state
                    .tray
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .clone()
                {
                    tray.set_uipi_warning(mismatch);
                }
                if mismatch {
                    tracing::warn!("[UIPI] {}", uipi.reason);
                    state.hud_toast.show_toast(
                        &t("uipi.warning.title", &[]),
                        &t("uipi.warning.short", &[]),
                        ToastKind::Warning,
                        5500,
                        Some(game_rect),
                    );
                }
            }
        }

        let cfg_snap = state.config.read().expect("config lock poisoned").clone();
        let has_tracker = state
            .tracker
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .is_some();
        let minimap_active = has_tracker && cfg_snap.minimap_tracking;
        if !cfg_snap.auto_sync_m && !minimap_active {
            state
                .state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .reset();
            pending_verify = None;
            state.pause_minimap_tracker("disabled");
            hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
            reset_rate_window(
                &mut rate_win_start,
                &mut rate_minimap_full,
                &mut rate_minimap_fast,
                &mut rate_work_ms,
            );
            if sleep_or_shutdown(Duration::from_millis(500), &mut shutdown_rx).await {
                break;
            }
            continue;
        }

        let is_fg = WindowFinder::is_window_foreground(hwnd);
        let sm_state = state
            .state_machine
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .state();
        if !is_fg && sm_state != MapState::Settling && pending_verify.is_none() {
            state.pause_minimap_tracker("background");
            hires_timer.set(false);
            reset_rate_window(
                &mut rate_win_start,
                &mut rate_minimap_full,
                &mut rate_minimap_fast,
                &mut rate_work_ms,
            );
            let hud_hwnds: Vec<isize> = state.nav_hud.hwnd().into_iter().collect();
            let keep_hud = nav_hud_visible
                && WindowFinder::is_game_visible_unfocused(
                    hwnd,
                    (left, top, width, height),
                    &hud_hwnds,
                    true,
                );
            if !keep_hud {
                hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
            }
            if sleep_or_shutdown(Duration::from_millis(250), &mut shutdown_rx).await {
                break;
            }
            continue;
        }

        if !cfg_snap.auto_sync_m && sm_state != MapState::Closed {
            state
                .state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .reset();
            pending_verify = None;
        }

        let in_closed_state = state
            .state_machine
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .state()
            == MapState::Closed;
        let recorder_active = state
            .minimap_recorder
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
            .is_some_and(|r| r.active());
        let heading_fast_ok = in_closed_state
            && minimap_active
            && last_pose_tracking
            && last_full_step_mono
                .is_some_and(|t0| tick_start.duration_since(t0).as_secs_f64() < MINIMAP_FULL_STEP_S)
            && !recorder_active;

        let st_for_worker = Arc::clone(&state);
        let do_auto_sync = cfg_snap.auto_sync_m;
        let tick_out = match tokio::task::spawn_blocking(move || {
            run_tick_worker(
                &st_for_worker,
                TickParams {
                    cl: left,
                    ct: top,
                    cw: width,
                    ch: height,
                    is_closed: in_closed_state,
                    do_auto_sync,
                    do_minimap: minimap_active,
                    fast_ok: heading_fast_ok,
                },
            )
        })
        .await
        {
            Ok(out) => out,
            Err(_) => continue,
        };

        match tick_out.mode {
            TickMode::Minimap => {
                last_full_step_mono = Some(tick_start);
                if rate_win_start.is_none() {
                    rate_win_start = Some(tick_start);
                }
                rate_minimap_full += 1;
                rate_work_ms.push(tick_start.elapsed().as_secs_f64() * 1000.0);
            }
            TickMode::MinimapFast => {
                if rate_win_start.is_none() {
                    rate_win_start = Some(tick_start);
                }
                rate_minimap_fast += 1;
                rate_work_ms.push(tick_start.elapsed().as_secs_f64() * 1000.0);
            }
            _ => {
                reset_rate_window(
                    &mut rate_win_start,
                    &mut rate_minimap_full,
                    &mut rate_minimap_fast,
                    &mut rate_work_ms,
                );
            }
        }

        if let Some(win_t0) = rate_win_start {
            let elapsed_s = win_t0.elapsed().as_secs_f64();
            if elapsed_s >= 10.0 {
                let n_ticks = rate_minimap_full + rate_minimap_fast;
                if n_ticks > 0 && !rate_work_ms.is_empty() {
                    rate_work_ms.sort_by(f64::total_cmp);
                    let med = rate_work_ms[rate_work_ms.len() / 2];
                    let p90_idx = ((rate_work_ms.len() as f64) * 0.9)
                        .floor()
                        .min((rate_work_ms.len() - 1) as f64)
                        as usize;
                    let p90 = rate_work_ms[p90_idx];
                    tracing::info!(
                        "[Loop] minimap {:.1} Hz (heading fast {:.0}%, full {:.1} Hz), tick work med {med:.1} ms / p90 {p90:.1} ms",
                        n_ticks as f64 / elapsed_s,
                        (rate_minimap_fast as f64 / n_ticks as f64) * 100.0,
                        rate_minimap_full as f64 / elapsed_s,
                    );
                }
                reset_rate_window(
                    &mut rate_win_start,
                    &mut rate_minimap_full,
                    &mut rate_minimap_fast,
                    &mut rate_work_ms,
                );
            }
        }

        match tick_out.mode {
            TickMode::Minimap | TickMode::MinimapFast => {
                last_pose_tracking = tick_out
                    .pose
                    .as_ref()
                    .is_some_and(|p| p.state == TrackerState::Tracking);
            }
            _ => {
                last_pose_tracking = false;
            }
        }

        match tick_out.mode {
            TickMode::MapMPaused => {
                hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
            }
            TickMode::MapM => {
                let cur_sm_state = state
                    .state_machine
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .state();
                if cur_sm_state != MapState::Closed {
                    hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
                }
                if let (Some(full_frame), Some(step_res)) = (tick_out.full_frame, tick_out.step_res)
                {
                    let slider_hint = step_res.check.slider_res.clone();
                    if step_res.should_auto_sync {
                        pending_verify = None;
                        if let Some(ref loc) = state.locator {
                            let loc_clone = Arc::clone(loc);
                            let frame_arc = Arc::new(full_frame);
                            let frame_for_loc = Arc::clone(&frame_arc);
                            let sh_clone = slider_hint.clone();
                            let locate_res = tokio::task::spawn_blocking(move || {
                                loc_clone.locate_player_frame(
                                    frame_for_loc.as_view(),
                                    false,
                                    sh_clone.as_ref(),
                                )
                            })
                            .await
                            .ok()
                            .flatten();

                            match locate_res {
                                None => {
                                    let mut sm = state
                                        .state_machine
                                        .lock()
                                        .unwrap_or_else(|e| e.into_inner());
                                    if sm.retries() == 0 {
                                        sm.request_retry();
                                    }
                                }
                                Some(res) if res.has_world_coords => {
                                    let dx_norm = (res.pixel_x - width as f64 * 0.5).abs()
                                        / (width as f64 * 0.5).max(1.0);
                                    let dy_norm = (res.pixel_y - height as f64 * 0.5).abs()
                                        / (height as f64 * 0.5).max(1.0);
                                    let inliers = res.inliers_count.unwrap_or(0);
                                    if dx_norm > 0.65 || dy_norm > 0.65 {
                                        tracing::info!(
                                            "[Auto-Sync M] Skipped: player arrow off-center ({dx_norm:.2}, {dy_norm:.2}) — user is panning."
                                        );
                                    } else if res.map_id == Some(2) && inliers < 2 {
                                        let mut sm = state
                                            .state_machine
                                            .lock()
                                            .unwrap_or_else(|e| e.into_inner());
                                        if sm.retries() < MAX_RETRIES {
                                            sm.request_retry();
                                        }
                                    } else {
                                        let mid = res.map_id.unwrap_or(2);
                                        let lang = get_language();
                                        let loc_map = HoYoLABLabelCatalog::get_map_name(
                                            mid,
                                            Some(&lang),
                                            res.map_name.as_deref().unwrap_or(""),
                                        );
                                        let mut bcast = match res.to_json_value() {
                                            Value::Object(m) => m,
                                            _ => Map::new(),
                                        };
                                        bcast.insert(
                                            "type".to_owned(),
                                            Value::String("SYNC_POSITION_ONLY".to_owned()),
                                        );
                                        bcast.insert("map_name".to_owned(), Value::String(loc_map));
                                        state.server.broadcast(Value::Object(bcast), false, false);
                                        state.apply_map_m_anchor(&res);
                                        if inliers <= 3 {
                                            pending_verify = Some(PendingVerify { ticks: 4, res });
                                        }
                                    }
                                }
                                Some(_) => {
                                    state
                                        .state_machine
                                        .lock()
                                        .unwrap_or_else(|e| e.into_inner())
                                        .request_retry();
                                }
                            }
                        }
                    } else if let Some(mut pv) = pending_verify.take()
                        && step_res.state == MapState::SyncedIdle
                    {
                        pv.ticks = pv.ticks.saturating_sub(1);
                        if pv.ticks > 0 {
                            pending_verify = Some(pv);
                        } else if let Some(ref loc) = state.locator {
                            let loc_clone = Arc::clone(loc);
                            let sh_clone = slider_hint;
                            let v_res_opt = tokio::task::spawn_blocking(move || {
                                loc_clone.locate_player_frame(
                                    full_frame.as_view(),
                                    false,
                                    sh_clone.as_ref(),
                                )
                            })
                            .await
                            .ok()
                            .flatten();

                            if let Some(v_res) = v_res_opt
                                && v_res.has_world_coords
                                && let (
                                    Some(vwx),
                                    Some(vwy),
                                    Some(pwx),
                                    Some(pwy),
                                ) = (
                                    v_res.world_x,
                                    v_res.world_y,
                                    pv.res.world_x,
                                    pv.res.world_y,
                                )
                            {
                                let dx_norm = (v_res.pixel_x - width as f64 * 0.5).abs()
                                    / (width as f64 * 0.5).max(1.0);
                                let dy_norm = (v_res.pixel_y - height as f64 * 0.5).abs()
                                    / (height as f64 * 0.5).max(1.0);
                                if dx_norm <= 0.65 && dy_norm <= 0.65 {
                                    let dist = (vwx - pwx).hypot(vwy - pwy);
                                    let same_layer = v_res.is_underground == pv.res.is_underground
                                        && v_res.group_id == pv.res.group_id;
                                    let v_inl = v_res.inliers_count.unwrap_or(0);
                                    let p_inl = pv.res.inliers_count.unwrap_or(0);
                                    if (dist > 60.0 && v_inl > p_inl)
                                        || (!same_layer && v_inl >= p_inl)
                                    {
                                        let mid = v_res.map_id.unwrap_or(2);
                                        let lang = get_language();
                                        let loc_map = HoYoLABLabelCatalog::get_map_name(
                                            mid,
                                            Some(&lang),
                                            v_res.map_name.as_deref().unwrap_or(""),
                                        );
                                        let mut bcast = match v_res.to_json_value() {
                                            Value::Object(m) => m,
                                            _ => Map::new(),
                                        };
                                        bcast.insert(
                                            "type".to_owned(),
                                            Value::String("SYNC_POSITION_ONLY".to_owned()),
                                        );
                                        bcast.insert("map_name".to_owned(), Value::String(loc_map));
                                        state.server.broadcast(Value::Object(bcast), false, false);
                                        state.apply_map_m_anchor(&v_res);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            TickMode::Minimap | TickMode::MinimapFast => {
                if let Some(ref pose) = tick_out.pose {
                    if pose.hdr_clipped {
                        hdr_clip_streak += 1;
                        if hdr_clip_streak >= HDR_CLIP_STREAK_WARN && !hdr_warned {
                            hdr_warned = true;
                            tracing::warn!("[HDR] {}", t("hdr.warning.short", &[]));
                            state.hud_toast.show_toast(
                                &t("hdr.warning.title", &[]),
                                &t("hdr.warning.short", &[]),
                                ToastKind::Warning,
                                5500,
                                Some(game_rect),
                            );
                        }
                    } else {
                        hdr_clip_streak = 0;
                    }

                    let pose_valid = pose.state == TrackerState::Tracking
                        && pose.map_id.is_some()
                        && pose.lat.is_some()
                        && pose.lng.is_some();
                    let should_bc_pos = pose.should_broadcast_web;
                    let allow_heading_only = pose.stationary
                        && matches!(
                            pose.match_method.as_str(),
                            "stationary_skip" | "heading_fast"
                        );

                    if pose_valid && (should_bc_pos || allow_heading_only) {
                        let prev_last = state
                            .game_state
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .last_pos
                            .clone();

                        let heading_val = pose.body_heading.or(pose.view_heading);
                        let cam_val = pose.camera_heading.or(pose.view_heading);

                        let (eff_mid, eff_lat, eff_lng, eff_wx, eff_wy, eff_ug, eff_fid, eff_fname) =
                            if !should_bc_pos
                                && allow_heading_only
                                && let Some(ref pl) = prev_last
                                && let (Some(plat), Some(plng)) =
                                    (json_f64(pl.get("lat")), json_f64(pl.get("lng")))
                            {
                                (
                                    json_i64(pl.get("map_id"))
                                        .map(|i| i as i32)
                                        .unwrap_or_else(|| pose.map_id.unwrap_or(2)),
                                    plat,
                                    plng,
                                    json_f64(pl.get("world_x")).unwrap_or(plng),
                                    json_f64(pl.get("world_y")).unwrap_or(plat),
                                    json_truthy(pl.get("is_underground"), pose.is_underground),
                                    pl.get("floor_id")
                                        .and_then(|v| json_i64(Some(v)))
                                        .map(|i| i as i32)
                                        .or(pose.floor_id),
                                    pl.get("floor_name")
                                        .and_then(Value::as_str)
                                        .map(str::to_owned)
                                        .unwrap_or_else(|| pose.floor_name.clone()),
                                )
                            } else {
                                let mid = pose.map_id.unwrap_or(2);
                                let lat = pose.lat.unwrap_or(0.0);
                                let lng = pose.lng.unwrap_or(0.0);
                                (
                                    mid,
                                    lat,
                                    lng,
                                    pose.world_x.unwrap_or(lng),
                                    pose.world_y.unwrap_or(lat),
                                    pose.is_underground,
                                    pose.floor_id,
                                    pose.floor_name.clone(),
                                )
                            };

                        let pos_info = json!({
                            "map_id": eff_mid,
                            "lat": eff_lat,
                            "lng": eff_lng,
                            "world_x": eff_wx,
                            "world_y": eff_wy,
                            "heading": heading_val,
                            "camera_heading": cam_val,
                            "view_heading": pose.view_heading,
                            "is_underground": eff_ug,
                            "floor_id": eff_fid,
                            "floor_name": eff_fname,
                        });
                        {
                            let mut gs = state.game_state.lock().unwrap_or_else(|e| e.into_inner());
                            gs.last_pos = Some(pos_info);
                        }

                        let now_mono = Instant::now();
                        if last_mm_bcast_ts
                            .is_none_or(|t0| now_mono.duration_since(t0).as_secs_f64() >= 0.05)
                        {
                            let should_send = if !should_bc_pos && allow_heading_only {
                                last_mm_bcast_state.as_ref().is_some_and(|prev| {
                                    angle_diff_deg(heading_val, prev.heading) >= 2.0
                                        || angle_diff_deg(cam_val, prev.camera_heading) >= 2.0
                                })
                            } else if let Some(ref prev) = last_mm_bcast_state {
                                prev.map_id != eff_mid
                                    || prev.is_underground != eff_ug
                                    || prev.floor_id != eff_fid
                                    || (eff_lat - prev.lat).hypot(eff_lng - prev.lng) >= 0.8
                                    || angle_diff_deg(heading_val, prev.heading) >= 2.0
                                    || angle_diff_deg(cam_val, prev.camera_heading) >= 2.0
                            } else {
                                true
                            };

                            if should_send {
                                last_mm_bcast_ts = Some(now_mono);
                                last_mm_bcast_state = Some(MmBcastState {
                                    map_id: eff_mid,
                                    lat: eff_lat,
                                    lng: eff_lng,
                                    heading: heading_val,
                                    camera_heading: cam_val,
                                    is_underground: eff_ug,
                                    floor_id: eff_fid,
                                });
                                let conf_r = (pose.confidence * 1000.0).round() / 1000.0;
                                state.server.broadcast_ephemeral(json!({
                                    "type": "MINIMAP_POSITION",
                                    "source": "minimap",
                                    "has_world_coords": true,
                                    "map_id": eff_mid,
                                    "world_x": eff_wx,
                                    "world_y": eff_wy,
                                    "lat": eff_lat,
                                    "lng": eff_lng,
                                    "heading": heading_val,
                                    "camera_heading": cam_val,
                                    "body_heading": pose.body_heading,
                                    "view_heading": pose.view_heading,
                                    "minimap_mode": pose.minimap_mode.as_str(),
                                    "zoom_regime": pose.zoom_regime.as_str(),
                                    "is_underground": eff_ug,
                                    "floor_id": eff_fid,
                                    "floor_short": eff_fname,
                                    "floor_name": eff_fname,
                                    "confidence": conf_r,
                                    "hdr_clipped": pose.hdr_clipped,
                                    "state": pose.state.as_str(),
                                }));
                            }
                        }

                        state.push_lan_snapshot(false);
                    }

                    // Navigation Mini-HUD guidance update
                    if !cfg_snap.nav_hud_enabled {
                        hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
                    } else if matches!(
                        pose.match_method.as_str(),
                        "gate_rejected" | "gate_hold" | "paimon_pause" | "loading_pause" | "paused"
                    ) {
                        gate_reject_streak += 1;
                        if gate_reject_streak >= 3 {
                            hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
                        }
                    } else {
                        gate_reject_streak = 0;
                        let fallback_pos = state
                            .game_state
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .last_pos
                            .clone();
                        let use_fallback = pose.lat.is_none()
                            && fallback_pos
                                .as_ref()
                                .and_then(|v| json_f64(v.get("lat")))
                                .is_some();

                        let eff_map_id = pose
                            .map_id
                            .or_else(|| {
                                fallback_pos
                                    .as_ref()
                                    .and_then(|v| json_i64(v.get("map_id")))
                                    .map(|i| i as i32)
                            })
                            .unwrap_or(2);
                        let eff_lat = pose
                            .lat
                            .or_else(|| fallback_pos.as_ref().and_then(|v| json_f64(v.get("lat"))))
                            .unwrap_or(0.0);
                        let eff_lng = pose
                            .lng
                            .or_else(|| fallback_pos.as_ref().and_then(|v| json_f64(v.get("lng"))))
                            .unwrap_or(0.0);
                        let eff_heading = pose.view_heading.or(pose.camera_heading);
                        let eff_is_ug = if use_fallback {
                            fallback_pos
                                .as_ref()
                                .map(|v| json_truthy(v.get("is_underground"), pose.is_underground))
                                .unwrap_or(pose.is_underground)
                        } else {
                            pose.is_underground
                        };
                        let eff_floor_id = if use_fallback {
                            fallback_pos
                                .as_ref()
                                .and_then(|v| json_i64(v.get("floor_id")))
                                .map(|i| i as i32)
                                .or(pose.floor_id)
                        } else {
                            pose.floor_id
                        };
                        let mut eff_floor_name = if use_fallback {
                            fallback_pos
                                .as_ref()
                                .and_then(|v| v.get("floor_name").and_then(Value::as_str))
                                .unwrap_or(&pose.floor_name)
                                .trim()
                                .to_owned()
                        } else {
                            pose.floor_name.trim().to_owned()
                        };
                        if eff_is_ug
                            && (eff_floor_name.is_empty()
                                || eff_floor_name.eq_ignore_ascii_case("G")
                                || eff_floor_name.chars().all(|c| c.is_ascii_digit()))
                            && let Some(fid) = eff_floor_id
                            && let Some(ref loc) = state.locator
                        {
                            let resolved_lbl = loc.resolve_floor_label(eff_map_id, fid);
                            if !resolved_lbl.is_empty() {
                                eff_floor_name = resolved_lbl;
                            }
                        }

                        let fid_str = eff_floor_id.map(|f| f.to_string());
                        let (mut guidance, nearby_snap) = {
                            let mut nav = state
                                .nav_controller
                                .lock()
                                .unwrap_or_else(|e| e.into_inner());
                            let g = nav.compute_guidance(
                                eff_lat,
                                eff_lng,
                                eff_heading,
                                eff_map_id,
                                fid_str.as_deref(),
                                eff_is_ug,
                                pose.state.as_str(),
                                &eff_floor_name,
                            );
                            let nb = nav.nearby_snapshot(eff_lat, eff_lng, eff_map_id, 6);
                            (g, nb)
                        };

                        {
                            let tgt = guidance.target.as_ref();
                            let snap = json!({
                                "state": guidance.state.as_str(),
                                "target_id": tgt.map(|t| t.id.as_str()),
                                "target_name": tgt.map(|t| t.display_name()),
                                "target_category": tgt.map(|t| t.category.as_str()),
                                "target_lat": tgt.map(|t| t.lat),
                                "target_lng": tgt.map(|t| t.lng),
                                "distance_m": guidance.distance_m,
                                "bearing_deg": guidance.bearing_deg,
                                "delta_deg": guidance.delta_deg,
                                "route_mode": guidance.route_mode.as_str(),
                                "floor_mismatch": guidance.floor_mismatch,
                                "remaining_count": guidance.remaining_count,
                                "player": {
                                    "lat": eff_lat,
                                    "lng": eff_lng,
                                    "map_id": eff_map_id,
                                    "floor_id": eff_floor_id,
                                    "floor_name": eff_floor_name,
                                    "is_underground": eff_is_ug,
                                },
                                "heading": {
                                    "view": pose.view_heading,
                                    "camera": pose.camera_heading,
                                    "body": pose.body_heading,
                                    "used": eff_heading,
                                },
                                "hud_visible": nav_hud_visible,
                                "nearby": nearby_snap,
                            });
                            *state
                                .nav_state_snapshot
                                .lock()
                                .unwrap_or_else(|e| e.into_inner()) = Some(snap);
                        }

                        let cur_target_id = guidance.target.as_ref().map(|t| t.id.clone());
                        if cur_target_id != last_nav_target_id {
                            last_nav_target_id = cur_target_id;
                            state.ensure_guide(guidance.target.as_ref());
                        }

                        let cur_lang = get_language();
                        if guidance.state == NavState::Idle && guidance.target.is_none() {
                            let idle_hint = if !state.server.has_active_client(5.0) {
                                if cur_lang == "vi" {
                                    "Mở / F5 HoYoLAB Map để kết nối"
                                } else {
                                    "Open / F5 HoYoLAB Map to connect"
                                }
                            } else if cur_lang == "vi" {
                                "Bật bộ lọc rương/thần đồng trên HoYoLAB"
                            } else {
                                "Enable pin filters on HoYoLAB Map"
                            };
                            guidance.delta_deg = None;
                            guidance.route_target_name = idle_hint.to_owned();
                        }

                        let sync_label = state.hotkey_mgr.get_display_name("nav_mark");
                        let guide_label = state.hotkey_mgr.get_display_name("nav_guide");
                        let sig = HudSig {
                            state: guidance.state,
                            target_id: guidance.target.as_ref().map(|t| t.id.clone()),
                            dist_tenth: Some((guidance.distance_m * 10.0).round() as i64),
                            delta_tenth: guidance.delta_deg.map(|d| (d * 10.0).round() as i64),
                            floor_mismatch: guidance.floor_mismatch,
                            route_target_name: guidance.route_target_name.clone(),
                            route_step: guidance.route_step,
                            route_total: guidance.route_total,
                            remaining_count: guidance.remaining_count,
                            show_spot_card: guidance.show_spot_card,
                            rect: (left, top, width, height),
                            position: cfg_snap.nav_hud_position.clone(),
                            lang: cur_lang.clone(),
                            sync_label: sync_label.clone(),
                            guide_label: guide_label.clone(),
                            desc: guidance
                                .target
                                .as_ref()
                                .map(|t| t.description.clone())
                                .unwrap_or_default(),
                            image_url: guidance
                                .target
                                .as_ref()
                                .map(|t| t.image_url.clone())
                                .unwrap_or_default(),
                        };

                        if last_hud_sig.as_ref() != Some(&sig) {
                            let wa = query_monitor_work_area(left + width / 2, top + height / 2);
                            state.nav_hud.update(
                                guidance,
                                Some(game_rect),
                                Some(wa),
                                None,
                                Some(&cfg_snap.nav_hud_position),
                                Some(&cur_lang),
                                Some(&sync_label),
                                0.0,
                                None,
                                Some(&guide_label),
                            );
                            nav_hud_visible = true;
                            last_hud_sig = Some(sig);
                        }
                    }
                } else {
                    hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
                }
            }
            TickMode::IdleClosed => {
                hide_nav_hud(&mut nav_hud_visible, &mut last_hud_sig);
            }
        }

        let cur_state = state
            .state_machine
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .state();
        let live_minimap = cur_state == MapState::Closed && minimap_active;
        hires_timer.set(live_minimap);

        let sleep_dur = if cur_state == MapState::Settling {
            Duration::from_millis(45)
        } else if live_minimap {
            let elapsed = tick_start.elapsed().as_secs_f64();
            let rem = MINIMAP_TICK_S - elapsed;
            if rem > 0.001 {
                Duration::from_secs_f64(rem)
            } else {
                Duration::ZERO
            }
        } else if cur_state == MapState::Closed {
            Duration::from_millis(75)
        } else {
            Duration::from_millis(110)
        };

        if sleep_or_shutdown(sleep_dur, &mut shutdown_rx).await {
            break;
        }
    }

    hires_timer.set(false);
    state.nav_hud.hide();
}
