//! Main desktop GUI binary (`GenshinHelper.exe`) wiring `gh-core`, `gh-win32`,
//! `gh-net`, `gh-cv`, and `gh-hud` (`main.py`).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

pub mod callbacks;
pub mod config_watcher;
pub mod diagnostics;
pub mod minimap_recorder;
pub mod state;
pub mod tracking_loop;

use crate::callbacks::{
    AppConflictAdvisor, AppServerCallbacks, prefetch_startup_labels_if_online,
};
use crate::config_watcher::run_config_watcher;
use crate::diagnostics::DiagnosticManager;
use crate::minimap_recorder::MinimapRecorder;
use crate::state::{
    AppState, GameState, NavMarkConfirm, RouteState, WINDOW_TITLES, get_hotkey_field,
};
use crate::tracking_loop::run_tracking_loop;
use clap::Parser;
use gh_core::assets::{EnsureDataOptions, REQUIRED_ASSETS, ensure_data_ready};
use gh_core::config::AppConfig;
use gh_core::i18n::{get_language, set_language, t};
use gh_core::map_bridge::PerMapAffineRegistry;
use gh_core::multimap::load_multimap_db;
use gh_core::nav_controller::NavigationController;
use gh_core::paths::{AppPaths, RuntimeMode};
use gh_core::traits::{ConflictAdvisor, DiagnosticService, OcrProvider, ServerCallbacks};
use gh_core::{APP_VERSION, CoreResult, DEFAULT_SYNC_HOST};
use gh_cv::global_relocator::TeyvatGlobalRelocator;
use gh_cv::locator::MapLocator;
use gh_cv::map_state::MapScreenStateMachine;
use gh_cv::minimap_tracker::{HybridMinimapTracker, MinimapTrackerConfig};
use gh_cv::tile_cache::{LruTerrainTileCache, TileCacheConfig};
use gh_hud::nav_hud::{GuideZoomMode, HudPalette, NavHudConfig, NavMiniHud, PositionMode};
use gh_hud::toast::HudToastOverlay;
use gh_net::hd_assets::{
    HoyolabHdTileProvider, UndergroundOverlayProvider,
};
use gh_net::hoyolab_sync::{
    HoyolabSyncClient, StartupUpdateOutcome, check_and_apply_startup_update,
};
use gh_net::lan_companion::{LanCompanionConfig, LanCompanionServer};
use gh_net::pin_guide::{HoYoLABLabelCatalog, LabelCatalogConfig, PinGuideFetcher};
use gh_net::sync_server::{HOTKEY_ACTIONS, SyncServer, SyncServerConfig};
use gh_win32::audio::AudioFeedback;
use gh_win32::capture::{DIB_POOL_SLOTS, ScreenCapturer, WindowFinder};
use gh_win32::hotkey::UnifiedHotkeyManager;
use gh_win32::hotkey_conflicts::HotkeyConflictAdvisor;
use gh_win32::locale::Win32LocaleProvider;
use gh_win32::runtime::SingleInstanceMutex;
use gh_win32::tray::{SystemTrayApp, TrayCallbacks};
use gh_win32::win_ocr::WindowsOcrService;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};
use tokio::sync::watch;
use windows::Win32::UI::Shell::ShellExecuteW;
use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
use windows::core::{PCWSTR, w};

#[derive(Debug, Clone, Parser)]
#[command(
    name = "GenshinHelper",
    version = APP_VERSION,
    about = "Genshin Impact Exploration & HoYoLAB Auto-Sync Tool"
)]
pub struct CliArgs {
    /// Disable Windows System Tray icon
    #[arg(long)]
    pub no_tray: bool,

    /// Start in Standby mode (0% CPU until awakened)
    #[arg(long)]
    pub standby: bool,

    /// Start silently in background tray
    #[arg(long)]
    pub minimized: bool,

    /// Sync/hydrate HoYoLAB map datasets into the data directory and exit
    #[arg(long)]
    pub sync_data: bool,

    /// Losslessly record SECONDS of in-game minimap/HUD frames (30 fps)
    #[arg(long, value_name = "SECONDS", default_value_t = 0.0)]
    pub record_minimap: f64,

    /// Override Sync Server bind port (`AppConfig.ws_port`)
    #[arg(long)]
    pub port: Option<u16>,

    /// Override LAN Companion port (`AppConfig.lan_companion_port`)
    #[arg(long)]
    pub lan_port: Option<u16>,

    /// Override explicit `config.json` path
    #[arg(long)]
    pub config: Option<PathBuf>,

    /// Skip acquiring the Win32 single-instance mutex
    #[arg(long)]
    pub no_mutex: bool,

    /// Disable automatic UAC elevation prompt on startup
    #[arg(long)]
    pub no_elevate: bool,
}

fn env_flag_truthy(val: Option<&str>) -> bool {
    val.map(|s| matches!(s.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false)
}

fn should_auto_elevate(args: &CliArgs, env_no_elevate: bool, is_elevated: bool) -> bool {
    !is_elevated && !args.no_elevate && !args.no_tray && !args.sync_data && !env_no_elevate
}

fn open_shell_target(target: &str) {
    let wide: Vec<u16> = target.encode_utf16().chain(std::iter::once(0)).collect();
    // SAFETY: `wide` is NUL-terminated for the duration of `ShellExecuteW`.
    unsafe {
        let _ = ShellExecuteW(
            None,
            w!("open"),
            PCWSTR(wide.as_ptr()),
            None,
            None,
            SW_SHOWNORMAL,
        );
    }
}

fn run_sync_data_mode(paths: &AppPaths, cfg: &AppConfig) -> CoreResult<()> {
    let target_dir = if paths.mode == RuntimeMode::Dev
        && let Some(ref dev_dir) = paths.dev_data_dir
    {
        dev_dir.clone()
    } else {
        paths.data_dir.clone()
    };
    let client = HoyolabSyncClient::new();
    let out = client.sync_to_directory_with_cfg(&target_dir, &cfg.hoyolab_sync, None, None)?;
    tracing::info!("[--sync-data] Hydrated map datasets into {}", out.display());
    Ok(())
}

#[tokio::main]
async fn main() {
    let _ = tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .try_init();

    let args = CliArgs::parse();

    let env_no_elevate = env_flag_truthy(std::env::var("GH_NO_ELEVATE").ok().as_deref());
    if should_auto_elevate(
        &args,
        env_no_elevate,
        gh_win32::game_settings::is_process_elevated(),
    ) && gh_win32::game_settings::relaunch_current_exe_elevated(&["--no-elevate"])
    {
        return;
    }

    let paths = AppPaths::resolve();
    let config_path = args.config.clone().unwrap_or_else(|| paths.config_path.clone());
    let mut cfg = AppConfig::load_or_create(&config_path);
    let disk_ports = (cfg.ws_port, cfg.lan_companion_port);
    let cli_port = args.port.filter(|&p| p > 0);
    let cli_lan_port = args.lan_port.filter(|&lp| lp > 0);
    if let Some(p) = cli_port {
        cfg.ws_port = p;
    }
    if let Some(lp) = cli_lan_port {
        cfg.lan_companion_port = lp;
    }

    set_language(Some(&cfg.language), Some(&Win32LocaleProvider::new()));

    if args.sync_data {
        if let Err(err) = run_sync_data_mode(&paths, &cfg) {
            tracing::error!("[--sync-data] Failed: {err}");
            std::process::exit(1);
        }
        return;
    }

    let _mutex_guard = if args.no_mutex {
        None
    } else {
        match SingleInstanceMutex::acquire_for_port(cfg.ws_port) {
            Ok(Some(g)) => Some(g),
            Ok(None) => {
                tracing::info!("Another GenshinHelper instance is already running. Exiting.");
                return;
            }
            Err(err) => {
                tracing::warn!("SingleInstanceMutex warning: {err}; continuing anyway.");
                None
            }
        }
    };

    let sync_client = HoyolabSyncClient::new();
    let offline_mode = !cfg.hoyolab_sync.auto_check_on_startup
        || cfg
            .hoyolab_sync
            .auto_update_mode
            .eq_ignore_ascii_case("off");
    let ensure_opts = EnsureDataOptions {
        cache_dir: Some(&paths.data_dir),
        fallback_bundle_dir: paths.dev_data_dir.as_deref(),
        required_assets: &REQUIRED_ASSETS,
        auto_sync: !offline_mode,
        sync_cfg: &cfg.hoyolab_sync,
    };
    match ensure_data_ready(&ensure_opts, Some(&sync_client)) {
        Ok(dir) => tracing::info!("[Startup] Map data ready: {}", dir.display()),
        Err(err) => {
            tracing::warn!("[Startup] Map data hydration skipped/incomplete ({err}); using available assets.");
        }
    }

    let multimap_path = paths.resolve_data_asset("multimap_v4.json");
    let maps_db = Arc::new(load_multimap_db(&multimap_path).unwrap_or_default());

    let capturer = Arc::new(ScreenCapturer::new(DIB_POOL_SLOTS));
    let ocr: Option<Arc<dyn OcrProvider>> = WindowsOcrService::try_new()
        .ok()
        .map(|s| Arc::new(s) as Arc<dyn OcrProvider>);
    let locator = Arc::new(MapLocator::new(ocr));

    let hd_provider = Arc::new(
        HoyolabHdTileProvider::new(
            Some(paths.tiles_dir.clone()),
            Some(Arc::clone(&maps_db)),
            Duration::from_secs(5),
            true,
            offline_mode,
            4,
        )
        .expect("HoyolabHdTileProvider init"),
    );
    let ug_provider = Arc::new(
        UndergroundOverlayProvider::new(
            Some(paths.ug_overlays_dir.clone()),
            Some(Arc::clone(&maps_db)),
            Duration::from_secs(8),
            true,
            offline_mode,
            2,
        )
        .expect("UndergroundOverlayProvider init"),
    );

    let tile_cache = LruTerrainTileCache::from_config(TileCacheConfig {
        max_tiles: 72,
        terrain_data: locator.terrain_data().clone(),
        tile_provider: Some(Arc::clone(&hd_provider) as Arc<dyn gh_core::traits::TileProvider>),
        ..Default::default()
    })
    .expect("LruTerrainTileCache init");

    let global_relocator =
        TeyvatGlobalRelocator::from_terrain_data(locator.terrain_data(), None, true);
    global_relocator.warmup_async();

    let minimap_tracker = HybridMinimapTracker::new(
        Arc::new(tile_cache),
        MinimapTrackerConfig::production_defaults(),
    )
    .expect("HybridMinimapTracker init")
    .with_multimap_db(Arc::clone(&maps_db))
    .with_ug_provider(Arc::clone(&ug_provider) as Arc<dyn gh_core::traits::UndergroundOverlaySource>)
    .with_global_relocator(Arc::new(global_relocator))
    .with_paimon_cache_path(paths.paimon_cache_path());

    let lan_bind_host = gh_net::lan_companion::resolve_lan_bind_host(
        std::env::var("GH_LAN_BIND_HOST").ok().as_deref(),
    );
    let lan_companion = Arc::new(LanCompanionServer::new(LanCompanionConfig {
        host: lan_bind_host,
        port: cfg.lan_companion_port,
        ..Default::default()
    }));
    let server = SyncServer::new(SyncServerConfig {
        host: DEFAULT_SYNC_HOST.to_owned(),
        port: cfg.ws_port,
        config: cfg.clone(),
        config_path: Some(config_path.clone()),
        diagnostics_dir: Some(paths.diagnostics_dir.clone()),
        lan_companion: Some(Arc::clone(&lan_companion)),
        ..Default::default()
    });
    server.set_engine_active(!args.standby);

    let config_arc = Arc::new(RwLock::new(cfg.clone()));
    let audio = Arc::new(AudioFeedback::new(cfg.audio_enabled, cfg.audio_volume));
    let hud_toast = Arc::new(HudToastOverlay::new());
    if gh_hud::layered::is_hud_offscreen_env() {
        tracing::info!(
            "[Startup] HUD offscreen mode active (offset={:?})",
            gh_hud::layered::OFFSCREEN_TEST_OFFSET
        );
    }
    let guide_fetcher = PinGuideFetcher::new(Arc::new(get_language));

    let label_hud_holder: Arc<Mutex<Option<Arc<NavMiniHud>>>> = Arc::new(Mutex::new(None));
    let label_hud_ref = Arc::clone(&label_hud_holder);
    let label_catalog = HoYoLABLabelCatalog::with_config(LabelCatalogConfig {
        on_loaded: Some(Arc::new(move |_, _| {
            if let Some(ref hud) = *label_hud_ref.lock().unwrap_or_else(|e| e.into_inner()) {
                hud.invalidate_render();
            }
        })),
        ..Default::default()
    });

    // Create NavMiniHud and UnifiedHotkeyManager before wrapping in AppState
    let state_slot: Arc<Mutex<Option<Arc<AppState>>>> = Arc::new(Mutex::new(None));

    let state_slot_suppress = Arc::clone(&state_slot);
    let hotkey_mgr = Arc::new(UnifiedHotkeyManager::new(
        cfg.hotkey_suppress_conflicts,
        Some(Arc::new(move |_act: &str| {
            let Some(st) = state_slot_suppress
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone()
            else {
                return false;
            };
            if !st
                .config
                .read()
                .expect("config lock poisoned")
                .hotkey_suppress_conflicts
            {
                return false;
            }
            let hwnd = st
                .game_state
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .hwnd;
            if let Some(hw) = hwnd
                && WindowFinder::is_window_foreground(hw)
            {
                return true;
            }
            if let Some(hud_hw) = st.nav_hud.hwnd()
                && WindowFinder::is_window_foreground(hud_hw)
            {
                return true;
            }
            false
        })),
    ));

    let state_slot_pos = Arc::clone(&state_slot);
    let catalog_for_hud = label_catalog.clone();
    let nav_hud = Arc::new(
        NavMiniHud::new(NavHudConfig {
            position_mode: PositionMode::from_str_lossy(&cfg.nav_hud_position),
            custom_pos: cfg.nav_hud_custom_pos,
            gamepad_mode: false,
            lang: Some(get_language()),
            hotkey_label: hotkey_mgr.get_display_name("nav_mark"),
            guide_hotkey_label: hotkey_mgr.get_display_name("nav_guide"),
            layout_hotkey_label: hotkey_mgr.get_display_name("nav_layout"),
            capture_visible: cfg.nav_hud_capture_visible,
            guide_zoom_mode: GuideZoomMode::from_str_lossy(&cfg.nav_hud_guide_zoom_mode),
            screen_offset: (0, 0),
        })
        .with_position_callback(Arc::new(move |mode, pos| {
            if let Some(st) = state_slot_pos
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .clone()
            {
                st.on_hud_position_changed(mode, pos);
            }
        }))
        .with_label_resolver(Arc::new(move |mid, lid, lang| {
            catalog_for_hud.get_label(mid, lid, Some(lang))
        })),
    );
    *label_hud_holder.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::clone(&nav_hud));

    let srv_for_diag = server.clone();
    let nav_snap_reader_slot: Arc<Mutex<Option<Arc<AppState>>>> = Arc::clone(&state_slot);
    let game_rect_reader_slot: Arc<Mutex<Option<Arc<AppState>>>> = Arc::clone(&state_slot);
    let diagnostics = Arc::new(DiagnosticManager::new(
        paths.clone(),
        Arc::clone(&capturer),
        Some(Arc::clone(&locator)),
        Arc::clone(&config_arc),
        Arc::new(move || srv_for_diag.last_browser_state()),
        Arc::new(move || {
            nav_snap_reader_slot
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .as_ref()
                .and_then(|st| {
                    st.nav_state_snapshot
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .clone()
                })
        }),
        Arc::new(move || {
            game_rect_reader_slot
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .as_ref()
                .and_then(|st| st.game_state.lock().unwrap_or_else(|e| e.into_inner()).rect)
        }),
    ));

    let (stop_tx, shutdown_rx) = watch::channel(false);

    let state = Arc::new(AppState {
        paths: paths.clone(),
        config_path: config_path.clone(),
        config: config_arc,
        cli_port,
        cli_lan_port,
        disk_ports: Mutex::new(disk_ports),
        config_io_lock: Mutex::new(()),
        last_self_saved_mtime: Mutex::new(None),
        engine_active: AtomicBool::new(!args.standby),
        game_state: Mutex::new(GameState::default()),
        nav_state_snapshot: Mutex::new(None),
        nav_controller: Mutex::new(NavigationController::new().with_space_debounce_frames(4)),
        route_state: Mutex::new(RouteState::default()),
        nav_mark_confirm: Mutex::new(NavMarkConfirm::default()),
        tracker: Mutex::new(Some((minimap_tracker, 0.0))),
        locator: Some(locator),
        state_machine: Mutex::new(MapScreenStateMachine::new()),
        capturer,
        server: server.clone(),
        lan_companion: Arc::clone(&lan_companion),
        nav_hud: Arc::clone(&nav_hud),
        hud_toast: Arc::clone(&hud_toast),
        audio: Arc::clone(&audio),
        tray: Mutex::new(None),
        hotkey_mgr: Arc::clone(&hotkey_mgr),
        conflict_advisor: Arc::new(HotkeyConflictAdvisor::new()),
        guide_fetcher,
        label_catalog,
        maps_db: Arc::clone(&maps_db),
        affine_registry: PerMapAffineRegistry::new(),
        hd_provider,
        ug_provider,
        minimap_recorder: Mutex::new(None),
        diagnostics: Arc::clone(&diagnostics),
        stop_tx: stop_tx.clone(),
        start_instant: Instant::now(),
    });
    *state_slot.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::clone(&state));

    // Register all 9 hotkeys
    let active_init = !args.standby;
    for act in HOTKEY_ACTIONS {
        let st = Arc::clone(&state);
        let hk = get_hotkey_field(&cfg, act);
        let enabled = if act == "switch" {
            active_init && cfg.single_monitor_mode
        } else {
            active_init
        };
        let act_owned = act.to_owned();
        hotkey_mgr.register(
            act,
            &hk,
            Arc::new(move || match act_owned.as_str() {
                "sync" => st.handle_hotkey_action(),
                "debug" => {
                    let _ = st.handle_debug_action("hotkey_debug", "", true);
                }
                "switch" => st.handle_switch_hotkey(),
                "nav_mark" => st.handle_nav_mark_hotkey(),
                "nav_undo" => st.handle_nav_undo_hotkey(),
                "nav_guide" => st.handle_nav_guide_hotkey(),
                "nav_next" => st.handle_nav_next_hotkey(),
                "nav_layout" => st.handle_nav_layout_hotkey(),
                "nav_toggle" => st.handle_nav_toggle_hotkey(),
                _ => {}
            }),
            enabled,
            None,
        );
    }
    hotkey_mgr.start();
    state.refresh_nav_hud_labels();

    // Optional MinimapRecorder (--record-minimap)
    if args.record_minimap > 0.0 {
        let hud_for_rec = Arc::clone(&nav_hud);
        let rec = MinimapRecorder::new(
            args.record_minimap,
            &paths.minimap_rec_dir(),
            30.0,
            180,
            Some(Box::new(move |_out_dir, frames| {
                hud_for_rec.flash(
                    &format!("REC OK - {frames} frames"),
                    HudPalette::Success,
                    6.0,
                );
            })),
        );
        *state
            .minimap_recorder
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(rec);
    }

    // Opt-in LAN Companion startup
    if cfg.lan_companion_enabled {
        lan_companion.set_port(cfg.lan_companion_port);
        if let Err(err) = lan_companion.start_in_background() {
            tracing::error!("Failed to start opt-in LAN Companion: {err}");
        }
    }

    // Wire SyncServer traits & start HTTP server
    server.set_callbacks(Some(
        Arc::new(AppServerCallbacks::new(Arc::clone(&state))) as Arc<dyn ServerCallbacks>,
    ));
    server.set_conflict_advisor(Some(
        Arc::new(AppConflictAdvisor::new(Arc::clone(&state))) as Arc<dyn ConflictAdvisor>,
    ));
    server.set_diagnostic_service(Some(diagnostics as Arc<dyn DiagnosticService>));

    if let Err(err) = server.start().await {
        tracing::error!("Failed to start SyncServer: {err}");
        std::process::exit(1);
    }

    // Optional System Tray
    if !args.no_tray {
        let st_toggle = Arc::clone(&state);
        let st_diag = Arc::clone(&state);
        let st_cfg = Arc::clone(&state);
        let st_quit = Arc::clone(&state);
        let st_borderless = Arc::clone(&state);
        let tray = Arc::new(SystemTrayApp::new(
            TrayCallbacks {
                on_toggle_engine: Some(Arc::new(move || {
                    let next = !st_toggle.engine_active.load(Ordering::Acquire);
                    st_toggle.set_engine_active(next);
                    st_toggle.server.broadcast(
                        serde_json::json!({
                            "type": "ENGINE_STATE_UPDATED",
                            "engine_active": next,
                        }),
                        false,
                        false,
                    );
                })),
                on_open_map: Some(Arc::new(|| {
                    open_shell_target("https://act.hoyolab.com/ys/app/interactive-map/index.html#/map/2");
                })),
                on_open_diagnostics: Some(Arc::new(move || {
                    let _ = std::fs::create_dir_all(&st_diag.paths.diagnostics_dir);
                    open_shell_target(&st_diag.paths.diagnostics_dir.to_string_lossy());
                })),
                on_open_config_dir: Some(Arc::new(move || {
                    let dir = st_cfg.config_path.parent().unwrap_or(st_cfg.paths.config_dir());
                    let _ = std::fs::create_dir_all(dir);
                    open_shell_target(&dir.to_string_lossy());
                })),
                on_quit: Some(Arc::new(move || {
                    let _ = st_quit.stop_tx.send(true);
                })),
                on_launch_borderless: Some(Arc::new(move || {
                    if let Ok(cb) = AppServerCallbacks::new(Arc::clone(&st_borderless))
                        .on_launch_borderless(&serde_json::json!({}))
                    {
                        tracing::info!("[Tray] Borderless launch: {cb}");
                    }
                })),
                on_open_support: Some(Arc::new(|| {
                    let url = if get_language() == "vi" {
                        "https://github.com/tuanminh26596/Genshin-helper/blob/main/README.vi.md#%E1%BB%A7ng-h%E1%BB%99-t%C3%A1c-gi%E1%BA%A3-donate"
                    } else {
                        "https://ko-fi.com/minmories"
                    };
                    open_shell_target(url);
                })),
            },
            active_init,
            Some(get_language()),
        ));
        if tray.start() {
            *state.tray.lock().unwrap_or_else(|e| e.into_inner()) = Some(tray);
            state.refresh_tray_state();
        }
    }

    // Startup online checks (strictly gated on auto_check_on_startup && auto_update_mode != "off")
    if !offline_mode {
        prefetch_startup_labels_if_online(&state);
        let paths_up = paths.clone();
        let sync_up = cfg.hoyolab_sync.clone();
        let mut local_vers: BTreeMap<i32, String> = BTreeMap::new();
        for (&mid, meta) in &maps_db.maps {
            if let Some(ref d2) = meta.detail_v2
                && !d2.map_version.trim().is_empty()
            {
                local_vers.insert(mid, d2.map_version.clone());
            }
        }
        let _ = std::thread::Builder::new()
            .name("gh-startup-update".to_owned())
            .spawn(move || {
                let runner = HoyolabSyncClient::new();
                match check_and_apply_startup_update(&runner, &paths_up, &sync_up, &local_vers) {
                    Ok(StartupUpdateOutcome::Disabled) => {}
                    Ok(StartupUpdateOutcome::UpToDate(_)) => {
                        tracing::info!("[Startup] HoYoLAB map dataset is up to date.");
                    }
                    Ok(StartupUpdateOutcome::UpdateAvailable(res)) => {
                        tracing::info!(
                            "[Startup] HoYoLAB map update available: {:?} (new={:?})",
                            res.updated_maps.keys().collect::<Vec<_>>(),
                            res.new_map_ids
                        );
                    }
                    Ok(StartupUpdateOutcome::Updated(res)) => {
                        tracing::info!(
                            "[Startup] HoYoLAB map dataset updated atomically: {:?}",
                            res.updated_maps.keys().collect::<Vec<_>>()
                        );
                    }
                    Err(e) => {
                        tracing::debug!("[Startup] HoYoLAB version check skipped: {e}");
                    }
                }
            });
    }

    if !args.minimized {
        tracing::info!(
            "GenshinHelper v{} ready on http://{}:{}",
            APP_VERSION,
            DEFAULT_SYNC_HOST,
            server.port()
        );
    }
    let _ = (t("app.name", &[]), &WINDOW_TITLES);

    // Spawn tracking loop & config hot-reload watcher
    let track_handle = tokio::spawn(run_tracking_loop(
        Arc::clone(&state),
        shutdown_rx.clone(),
    ));
    let watch_handle = tokio::spawn(run_config_watcher(
        Arc::clone(&state),
        shutdown_rx.clone(),
    ));

    // Wait for shutdown signal (/shutdown, tray quit, or Ctrl+C)
    let mut main_rx = shutdown_rx;
    loop {
        if *main_rx.borrow() {
            break;
        }
        tokio::select! {
            res = main_rx.changed() => {
                if res.is_err() || *main_rx.borrow() {
                    break;
                }
            }
            _ = tokio::signal::ctrl_c() => {
                let _ = stop_tx.send(true);
                break;
            }
        }
    }

    // Deterministic shutdown cleanup & Arc cycle breaking
    let rec_opt = state
        .minimap_recorder
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .take();
    if let Some(rec) = rec_opt {
        rec.finish();
        rec.join_timeout(Duration::from_secs(10));
    }

    let _ = track_handle.await;
    let _ = watch_handle.await;

    if let Some(tray) = state
        .tray
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .take()
    {
        tray.close();
    }
    state.lan_companion.stop_background();
    state.nav_hud.close();
    state.guide_fetcher.stop();
    state.server.stop().await;

    // Break Arc reference cycles between SyncServer, NavMiniHud, and AppState
    state.server.set_callbacks(None);
    state.server.set_conflict_advisor(None);
    state.server.set_diagnostic_service(None);
    *state_slot.lock().unwrap_or_else(|e| e.into_inner()) = None;
    *label_hud_holder.lock().unwrap_or_else(|e| e.into_inner()) = None;

    state.hotkey_mgr.stop();
    state.audio.close();
    state.hud_toast.close();
    state.capturer.close();
    state.hd_provider.close();
    state.ug_provider.close();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_parses_defaults_and_all_override_flags() {
        let def = CliArgs::try_parse_from(["GenshinHelper"]).unwrap();
        assert!(!def.no_tray);
        assert!(!def.standby);
        assert!(!def.minimized);
        assert!(!def.sync_data);
        assert_eq!(def.record_minimap, 0.0);
        assert_eq!(def.port, None);
        assert_eq!(def.lan_port, None);
        assert_eq!(def.config, None);
        assert!(!def.no_mutex);
        assert!(!def.no_elevate);

        let full = CliArgs::try_parse_from([
            "GenshinHelper",
            "--no-tray",
            "--standby",
            "--minimized",
            "--sync-data",
            "--record-minimap",
            "15",
            "--port",
            "18765",
            "--lan-port",
            "18766",
            "--config",
            "custom_cfg.json",
            "--no-mutex",
            "--no-elevate",
        ])
        .unwrap();
        assert!(full.no_tray);
        assert!(full.standby);
        assert!(full.minimized);
        assert!(full.sync_data);
        assert_eq!(full.record_minimap, 15.0);
        assert_eq!(full.port, Some(18765));
        assert_eq!(full.lan_port, Some(18766));
        assert_eq!(full.config, Some(PathBuf::from("custom_cfg.json")));
        assert!(full.no_mutex);
        assert!(full.no_elevate);
    }

    #[test]
    fn env_flag_truthy_and_should_auto_elevate_respect_guards() {
        assert!(!env_flag_truthy(None));
        assert!(!env_flag_truthy(Some("")));
        assert!(!env_flag_truthy(Some("0")));
        assert!(!env_flag_truthy(Some("false")));
        assert!(env_flag_truthy(Some("1")));
        assert!(env_flag_truthy(Some(" True ")));
        assert!(env_flag_truthy(Some("YES")));
        assert!(env_flag_truthy(Some("on")));

        let def = CliArgs::try_parse_from(["GenshinHelper"]).unwrap();
        assert!(should_auto_elevate(&def, false, false));
        assert!(!should_auto_elevate(&def, false, true));
        assert!(!should_auto_elevate(&def, true, false));

        let no_el = CliArgs::try_parse_from(["GenshinHelper", "--no-elevate"]).unwrap();
        assert!(!should_auto_elevate(&no_el, false, false));

        let no_tray = CliArgs::try_parse_from(["GenshinHelper", "--no-tray"]).unwrap();
        assert!(!should_auto_elevate(&no_tray, false, false));

        let sync = CliArgs::try_parse_from(["GenshinHelper", "--sync-data"]).unwrap();
        assert!(!should_auto_elevate(&sync, false, false));
    }
}
