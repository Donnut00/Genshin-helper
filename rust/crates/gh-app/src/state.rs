//! Shared runtime state and cross-subsystem actions for `gh-app`.

use crate::diagnostics::DiagnosticManager;
use crate::minimap_recorder::MinimapRecorder;
use gh_core::config::AppConfig;
use gh_core::frame::RectI32;
use gh_core::i18n::{get_language, set_language, t};
use gh_core::map_bridge::PerMapAffineRegistry;
use gh_core::multimap::MultimapDb;
use gh_core::nav_controller::{FloorResolver, NavPin, NavigationController};
use gh_core::paths::AppPaths;
use gh_core::protocol::format_hotkey_display_name;
use gh_core::{CoreResult, DEFAULT_SYNC_HOST};
use gh_cv::locator::{LocateResult, MapLocator};
use gh_cv::map_state::MapScreenStateMachine;
use gh_cv::minimap_tracker::HybridMinimapTracker;
use gh_hud::nav_hud::{HudPalette, NavMiniHud, PositionMode};
use gh_hud::toast::{HudToastOverlay, ToastKind};
use gh_net::hd_assets::{HoyolabHdTileProvider, UndergroundOverlayProvider};
use gh_net::lan_companion::LanCompanionServer;
use gh_net::pin_guide::{HoYoLABLabelCatalog, PinGuide, PinGuideFetcher};
use gh_net::sync_server::{HOTKEY_ACTIONS, SyncServer};
use gh_win32::audio::AudioFeedback;
use gh_win32::capture::{ScreenCapturer, WindowFinder};
use gh_win32::game_settings::UipiStatus;
use gh_win32::hotkey::UnifiedHotkeyManager;
use gh_win32::hotkey_conflicts::HotkeyConflictAdvisor;
use gh_win32::locale::Win32LocaleProvider;
use gh_win32::switcher::WindowSwitcher;
use gh_win32::tray::{SystemTrayApp, TrayState};
use serde_json::{Map, Value, json};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Instant, SystemTime};
use tokio::sync::watch;

pub const WINDOW_TITLES: [&str; 2] = ["Genshin Impact", "原神"];
pub const NAV_MARK_CONFIRM_DIST_M: f64 = 25.0;

#[derive(Debug, Clone, Default)]
pub struct GameState {
    pub hwnd: Option<isize>,
    pub rect: Option<RectI32>,
    pub last_pos: Option<Value>,
    pub uipi_status: Option<UipiStatus>,
}

#[derive(Debug, Clone, Default)]
pub struct RouteState {
    pub legs: Vec<Value>,
    pub last_route: Option<Value>,
}

#[derive(Debug, Clone, Default)]
pub struct NavMarkConfirm {
    pub pin_id: Option<String>,
    pub until: Option<Instant>,
}

pub struct AppFloorResolver {
    pub locator: Option<Arc<MapLocator>>,
    pub maps_db: Arc<MultimapDb>,
}

impl FloorResolver for AppFloorResolver {
    fn infer_unique_underground_floor(
        &self,
        map_id: i32,
        lat: f64,
        lng: f64,
    ) -> Option<(String, String)> {
        if let Some(loc) = &self.locator {
            loc.infer_unique_underground_floor(map_id, lat, lng, 15.0)
        } else {
            self.maps_db
                .infer_unique_underground_floor(map_id, lat, lng, 15.0)
                .map(|(fid, lbl)| (fid.to_string(), lbl))
        }
    }

    fn resolve_floor_label(&self, map_id: i32, floor_id: &str) -> Option<String> {
        let lbl = if let Some(loc) = &self.locator {
            loc.resolve_floor_label_str(map_id, floor_id)
        } else {
            let fid: i32 = floor_id.trim().parse().ok()?;
            self.maps_db.resolve_floor_label(map_id, fid)?
        };
        if lbl.is_empty() { None } else { Some(lbl) }
    }
}

pub fn json_truthy(v: Option<&Value>, default: bool) -> bool {
    match v {
        None => default,
        Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

pub fn json_f64(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::String(s)) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

pub fn json_i64(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Some(Value::String(s)) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

pub fn json_str_trimmed(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

pub fn get_hotkey_field(cfg: &AppConfig, action: &str) -> String {
    match action {
        "sync" => cfg.hotkey_sync.clone(),
        "debug" => cfg.hotkey_debug.clone(),
        "switch" => cfg.hotkey_switch.clone(),
        "nav_mark" => cfg.hotkey_nav_mark.clone(),
        "nav_undo" => cfg.hotkey_nav_undo.clone(),
        "nav_guide" => cfg.hotkey_nav_guide.clone(),
        "nav_next" => cfg.hotkey_nav_next.clone(),
        "nav_layout" => cfg.hotkey_nav_layout.clone(),
        "nav_toggle" => cfg.hotkey_nav_toggle.clone(),
        _ => String::new(),
    }
}

pub fn set_hotkey_field(cfg: &mut AppConfig, action: &str, new_hk: &str) -> bool {
    let norm = new_hk.trim().to_ascii_lowercase();
    if norm.is_empty() {
        return false;
    }
    match action {
        "sync" => {
            cfg.hotkey = norm.clone();
            cfg.hotkey_sync = norm;
        }
        "debug" => cfg.hotkey_debug = norm,
        "switch" => cfg.hotkey_switch = norm,
        "nav_mark" => cfg.hotkey_nav_mark = norm,
        "nav_undo" => cfg.hotkey_nav_undo = norm,
        "nav_guide" => cfg.hotkey_nav_guide = norm,
        "nav_next" => cfg.hotkey_nav_next = norm,
        "nav_layout" => cfg.hotkey_nav_layout = norm,
        "nav_toggle" => cfg.hotkey_nav_toggle = norm,
        _ => return false,
    }
    true
}

pub fn read_config_file_stamp(path: &std::path::Path) -> Option<(SystemTime, u64)> {
    let meta = std::fs::metadata(path).ok()?;
    let mtime = meta.modified().ok()?;
    Some((mtime, meta.len()))
}

pub struct AppState {
    pub paths: AppPaths,
    pub config_path: PathBuf,
    pub config: Arc<RwLock<AppConfig>>,
    pub cli_port: Option<u16>,
    pub cli_lan_port: Option<u16>,
    pub disk_ports: Mutex<(u16, u16)>,
    pub config_io_lock: Mutex<()>,
    pub last_self_saved_mtime: Mutex<Option<(SystemTime, u64)>>,
    pub engine_active: AtomicBool,
    pub game_state: Mutex<GameState>,
    pub nav_state_snapshot: Mutex<Option<Value>>,
    pub nav_controller: Mutex<NavigationController>,
    pub route_state: Mutex<RouteState>,
    pub nav_mark_confirm: Mutex<NavMarkConfirm>,
    pub tracker: Mutex<Option<(HybridMinimapTracker, f64)>>,
    pub locator: Option<Arc<MapLocator>>,
    pub state_machine: Mutex<MapScreenStateMachine>,
    pub capturer: Arc<ScreenCapturer>,
    pub server: SyncServer,
    pub lan_companion: Arc<LanCompanionServer>,
    pub nav_hud: Arc<NavMiniHud>,
    pub hud_toast: Arc<HudToastOverlay>,
    pub audio: Arc<AudioFeedback>,
    pub tray: Mutex<Option<Arc<SystemTrayApp>>>,
    pub hotkey_mgr: Arc<UnifiedHotkeyManager>,
    pub conflict_advisor: Arc<HotkeyConflictAdvisor>,
    pub guide_fetcher: PinGuideFetcher,
    pub label_catalog: HoYoLABLabelCatalog,
    pub maps_db: Arc<MultimapDb>,
    pub affine_registry: PerMapAffineRegistry,
    pub hd_provider: Arc<HoyolabHdTileProvider>,
    pub ug_provider: Arc<UndergroundOverlayProvider>,
    pub minimap_recorder: Mutex<Option<Arc<MinimapRecorder>>>,
    pub diagnostics: Arc<DiagnosticManager>,
    pub stop_tx: watch::Sender<bool>,
    pub start_instant: Instant,
}

impl AppState {
    pub fn floor_resolver(&self) -> AppFloorResolver {
        AppFloorResolver {
            locator: self.locator.clone(),
            maps_db: Arc::clone(&self.maps_db),
        }
    }

    pub fn next_tracker_ts(&self) -> f64 {
        let now = self.start_instant.elapsed().as_secs_f64();
        let mut guard = self.tracker.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((_, ref mut last_ts)) = *guard {
            let next = if now < *last_ts { *last_ts } else { now };
            *last_ts = next;
            next
        } else {
            now
        }
    }

    pub fn pause_minimap_tracker(&self, reason: &str) {
        let now = self.start_instant.elapsed().as_secs_f64();
        let mut guard = self.tracker.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((ref mut tracker, ref mut last_ts)) = *guard {
            let ts = if now < *last_ts { *last_ts } else { now };
            *last_ts = ts;
            let _ = tracker.pause(Some(ts), reason, 0.0);
        }
    }

    fn save_config_locked(&self, cfg: &AppConfig) -> CoreResult<()> {
        self.server.update_config(cfg.clone());
        let mut disk_cfg = cfg.clone();
        {
            let mut disk = self.disk_ports.lock().unwrap_or_else(|e| e.into_inner());
            if self.cli_port.is_some() {
                disk_cfg.ws_port = disk.0;
            } else {
                disk.0 = disk_cfg.ws_port;
            }
            if self.cli_lan_port.is_some() {
                disk_cfg.lan_companion_port = disk.1;
            } else {
                disk.1 = disk_cfg.lan_companion_port;
            }
        }
        disk_cfg.save_to_file(&self.config_path)?;
        if let Some(stamp) = read_config_file_stamp(&self.config_path) {
            *self
                .last_self_saved_mtime
                .lock()
                .unwrap_or_else(|e| e.into_inner()) = Some(stamp);
        }
        Ok(())
    }

    pub fn save_config(&self, cfg: &AppConfig) -> CoreResult<()> {
        let _io_guard = self
            .config_io_lock
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        self.save_config_locked(cfg)
    }

    pub fn mutate_and_save_config<R>(
        &self,
        mutator: impl FnOnce(&mut AppConfig) -> R,
    ) -> (AppConfig, R, CoreResult<()>) {
        let _io_guard = self
            .config_io_lock
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let (cfg_clone, out) = {
            let mut cfg = self.config.write().expect("config lock poisoned");
            let out = mutator(&mut cfg);
            (cfg.clone(), out)
        };
        let save_res = self.save_config_locked(&cfg_clone);
        (cfg_clone, out, save_res)
    }

    pub fn refresh_tray_state(&self) {
        let tray_opt = self
            .tray
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        let Some(tray) = tray_opt else {
            return;
        };
        let active = self.engine_active.load(Ordering::Acquire);
        if !active {
            tray.set_state(TrayState::Paused, "", Some(false));
        } else if self.server.has_active_client(10.0) {
            tray.set_state(TrayState::Connected, "", Some(true));
        } else {
            tray.set_state(TrayState::Idle, "", Some(true));
        }
    }

    pub fn build_lan_state_payload(&self) -> Value {
        let last_pos = self
            .game_state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .last_pos
            .clone();
        let legs = self
            .route_state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .legs
            .clone();

        let mut payload = Map::new();
        if let Some(Value::Object(pos_obj)) = last_pos {
            if let (Some(lat), Some(lng)) = (
                json_f64(pos_obj.get("lat")),
                json_f64(pos_obj.get("lng")),
            ) {
                payload.insert("lat".to_owned(), json!(lat));
                payload.insert("lng".to_owned(), json!(lng));
            }
            if let Some(mid) = json_i64(pos_obj.get("map_id")) {
                payload.insert("map_id".to_owned(), json!(mid));
            }
            if let Some(h) = json_f64(pos_obj.get("heading")) {
                payload.insert("heading".to_owned(), json!(h));
            }
            if let Some(ch) = json_f64(pos_obj.get("camera_heading")) {
                payload.insert("camera_heading".to_owned(), json!(ch));
            }
            let is_ug = json_truthy(pos_obj.get("is_underground"), false);
            payload.insert("is_underground".to_owned(), Value::Bool(is_ug));
            let fname = pos_obj
                .get("floor_name")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or(if is_ug { "B1" } else { "G" });
            payload.insert("floor_name".to_owned(), Value::String(fname.to_owned()));
        }

        {
            let mut nav = self.nav_controller.lock().unwrap_or_else(|e| e.into_inner());
            let cur_target = nav
                .current_target()
                .map(NavPin::to_wire_json)
                .unwrap_or(Value::Null);
            payload.insert("target_pin".to_owned(), cur_target);
            let excluded = nav.excluded_ids();
            let nearby: Vec<Value> = nav
                .pins()
                .into_iter()
                .filter(|p| !excluded.contains(&p.id))
                .take(50)
                .map(NavPin::to_wire_json)
                .collect();
            payload.insert("nearby_pins".to_owned(), Value::Array(nearby));
        }

        payload.insert("route_legs".to_owned(), Value::Array(legs));
        Value::Object(payload)
    }

    pub fn push_lan_snapshot(&self, force: bool) {
        if !self.lan_companion.is_running() {
            return;
        }
        let payload = self.build_lan_state_payload();
        self.lan_companion.broadcast_state(payload, force);
    }

    pub fn apply_map_m_anchor(&self, res: &LocateResult) {
        if !res.has_world_coords {
            return;
        }
        let Some(world_x) = res.world_x else {
            return;
        };
        let Some(world_y) = res.world_y else {
            return;
        };
        let map_id = res.map_id.unwrap_or(2);
        let is_ug = res.is_underground;
        let floor_id = res.floor_id;
        let floor_name = if !res.floor_short.is_empty() {
            res.floor_short.clone()
        } else if let Some(ref hf) = res.hoyolab_floor_name
            && !hf.is_empty()
        {
            hf.clone()
        } else if !res.floor_name.is_empty() {
            res.floor_name.clone()
        } else if is_ug {
            "B1".to_owned()
        } else {
            "G".to_owned()
        };

        let pos_info = json!({
            "map_id": map_id,
            "lat": world_y,
            "lng": world_x,
            "world_x": world_x,
            "world_y": world_y,
            "heading": res.heading,
            "camera_heading": res.heading,
            "view_heading": res.heading,
            "is_underground": is_ug,
            "floor_id": floor_id,
            "floor_name": floor_name,
        });
        {
            let mut gs = self.game_state.lock().unwrap_or_else(|e| e.into_inner());
            gs.last_pos = Some(pos_info);
        }

        let now = self.start_instant.elapsed().as_secs_f64();
        {
            let mut guard = self.tracker.lock().unwrap_or_else(|e| e.into_inner());
            if let Some((ref mut tracker, ref mut last_ts)) = *guard {
                let ts = if now < *last_ts { *last_ts } else { now };
                *last_ts = ts;
                let _ = tracker.set_map_m_anchor(
                    map_id,
                    world_x,
                    world_y,
                    is_ug,
                    floor_id,
                    &floor_name,
                    Some(ts),
                );
            }
        }

        self.push_lan_snapshot(true);
    }

    pub fn refresh_nav_hud_labels(&self) {
        let mark = self.hotkey_mgr.get_display_name("nav_mark");
        let guide = self.hotkey_mgr.get_display_name("nav_guide");
        let layout = self.hotkey_mgr.get_display_name("nav_layout");
        self.nav_hud
            .set_hotkey_labels(Some(&mark), Some(&guide), Some(&layout));
    }

    pub fn nav_flash(&self, key: &str, kind: HudPalette, duration_s: f64, args: &[(&str, &str)]) {
        self.nav_hud.flash(&t(key, args), kind, duration_s);
    }

    pub fn apply_guide(self: &Arc<Self>, guide: &PinGuide) {
        let cur_id = {
            let mut nav = self.nav_controller.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(pin) = nav.get_pin_mut(&guide.pin_id) {
                if !guide.description.is_empty() {
                    pin.description = guide.description.clone();
                }
                if !guide.image_url.is_empty() && pin.image_url.is_empty() {
                    pin.image_url = guide.image_url.clone();
                }
            }
            nav.current_target().map(|p| p.id.clone())
        };
        if let Some(ref bytes) = guide.image_bytes
            && !guide.image_url.is_empty()
        {
            self.nav_hud.put_guide_image(&guide.image_url, bytes);
            self.nav_hud.put_thumbnail(&guide.image_url, bytes);
        }
        if cur_id.as_deref() == Some(guide.pin_id.as_str()) {
            let has_content = !guide.description.is_empty() || guide.image_bytes.is_some();
            self.nav_hud.set_guide_status(if guide.ok && has_content {
                "ready"
            } else {
                "empty"
            });
        }
    }

    pub fn ensure_guide(self: &Arc<Self>, pin: Option<&NavPin>) {
        let Some(pin) = pin else {
            self.nav_hud.set_guide_status("");
            return;
        };
        if let Some(cached) = self.guide_fetcher.get_cached(&pin.id, None) {
            self.apply_guide(&cached);
            return;
        }
        let state_weak = Arc::downgrade(self);
        if self.guide_fetcher.request(
            &pin.id,
            pin.map_id,
            move |guide| {
                if let Some(st) = state_weak.upgrade() {
                    st.apply_guide(&guide);
                }
            },
            None,
        ) {
            self.nav_hud.set_guide_status("loading");
        } else if !self.guide_fetcher.is_loading(&pin.id, None) {
            self.nav_hud.set_guide_status(if pin.has_spot_content() {
                ""
            } else {
                "empty"
            });
        }
    }

    pub fn broadcast_nav_mark(&self, pin: &NavPin, action: &str) -> bool {
        if !self.server.has_active_client(10.0) {
            return false;
        }
        self.server.broadcast(
            json!({
                "type": "NAV_MARK_PIN",
                "action": action,
                "pin_id": pin.id,
                "lat": pin.lat,
                "lng": pin.lng,
                "map_id": pin.map_id,
                "floor_id": pin.floor_id,
                "is_underground": pin.is_underground,
                "name": pin.display_name(),
            }),
            false,
            false,
        );
        true
    }

    pub fn set_engine_active(&self, active: bool) -> bool {
        self.engine_active.store(active, Ordering::Release);
        self.server.set_engine_active(active);

        let single_mon = self
            .config
            .read()
            .expect("config lock poisoned")
            .single_monitor_mode;

        self.hotkey_mgr.set_enabled("sync", active);
        self.hotkey_mgr.set_enabled("debug", active);
        self.hotkey_mgr.set_enabled("switch", active && single_mon);
        for act in [
            "nav_mark",
            "nav_undo",
            "nav_guide",
            "nav_next",
            "nav_layout",
            "nav_toggle",
        ] {
            self.hotkey_mgr.set_enabled(act, active);
        }

        if !active {
            self.nav_hud.set_edit_mode(false);
            self.nav_hud.set_guide_open(false, None);
            self.state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .reset();
            self.pause_minimap_tracker("standby");
            self.nav_hud.hide();
        }
        self.refresh_tray_state();
        active
    }

    pub fn set_hotkeys(&self, payload: &Value) -> CoreResult<Value> {
        let req_map = payload.as_object().cloned().unwrap_or_default();
        let has_batch = HOTKEY_ACTIONS
            .iter()
            .any(|act| req_map.contains_key(&format!("hotkey_{act}")));

        let mut results = Map::new();
        let mut all_ok = true;
        let mut nav_changed = false;
        let mut applied: Vec<(String, String)> = Vec::new();

        // Keep `config_io_lock` outermost and NEVER hold `self.config.write()` across
        // `hotkey_mgr.update_binding` -> `join_hook_thread`, because `gh-kb-hook` invokes
        // `should_suppress` -> `config.read()`, which would AB-BA deadlock.
        let _io_guard = self
            .config_io_lock
            .lock()
            .unwrap_or_else(|e| e.into_inner());

        {
            let mut apply_one = |act: &str, raw_key: &str| {
                let norm = raw_key.trim().to_ascii_lowercase();
                if norm.is_empty() || !HOTKEY_ACTIONS.contains(&act) {
                    all_ok = false;
                    results.insert(
                        act.to_owned(),
                        json!({"ok": false, "hotkey": norm, "display_name": ""}),
                    );
                    return;
                }
                let ok = self.hotkey_mgr.update_binding(act, &norm);
                let dname = if ok {
                    applied.push((act.to_owned(), norm.clone()));
                    if act.starts_with("nav_") {
                        nav_changed = true;
                    }
                    self.hotkey_mgr.get_display_name(act)
                } else {
                    all_ok = false;
                    format_hotkey_display_name(&norm)
                };
                results.insert(
                    act.to_owned(),
                    json!({"ok": ok, "hotkey": norm, "display_name": dname}),
                );
            };

            if has_batch {
                for act in HOTKEY_ACTIONS {
                    let kname = format!("hotkey_{act}");
                    let val_str = json_str_trimmed(req_map.get(&kname));
                    if !val_str.is_empty() {
                        apply_one(act, &val_str);
                    }
                }
            } else if req_map.contains_key("action") && req_map.contains_key("hotkey") {
                let act = json_str_trimmed(req_map.get("action")).to_ascii_lowercase();
                let new_k = json_str_trimmed(req_map.get("hotkey"));
                apply_one(&act, &new_k);
            } else if req_map.contains_key("hotkey") {
                let new_k = json_str_trimmed(req_map.get("hotkey"));
                apply_one("sync", &new_k);
            }
        }

        if nav_changed {
            self.refresh_nav_hud_labels();
        }

        if !applied.is_empty() {
            let cfg_clone = {
                let mut cfg = self.config.write().expect("config lock poisoned");
                for (act, norm) in &applied {
                    set_hotkey_field(&mut cfg, act, norm);
                }
                cfg.clone()
            };
            if let Err(err) = self.save_config_locked(&cfg_clone) {
                tracing::warn!(
                    "Failed to save config after set_hotkeys ({}): {err}",
                    self.config_path.display()
                );
            }
        }

        Ok(json!({
            "ok": all_ok,
            "results": results,
        }))
    }

    pub fn set_language(self: &Arc<Self>, lang: &str) -> CoreResult<Value> {
        let lower = lang.trim().to_ascii_lowercase();
        let raw = if matches!(lower.as_str(), "vi" | "en" | "auto") {
            lower
        } else {
            "auto".to_owned()
        };

        let (_cfg_clone, (resolved, offline_gate), _) = self.mutate_and_save_config(|cfg| {
            let resolved = set_language(Some(&raw), Some(&Win32LocaleProvider::new()));
            cfg.language = raw.clone();
            let offline = !cfg.hoyolab_sync.auto_check_on_startup
                || cfg.hoyolab_sync.auto_update_mode.eq_ignore_ascii_case("off");
            (resolved, offline)
        });

        let cur_map_id = self
            .game_state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .last_pos
            .as_ref()
            .and_then(|v| json_i64(v.get("map_id")))
            .map(|i| i as i32)
            .unwrap_or(2);

        if !offline_gate {
            self.label_catalog.prefetch(cur_map_id, Some(&resolved));
        }
        let localized_map = HoYoLABLabelCatalog::get_map_name(cur_map_id, Some(&resolved), "");
        self.server
            .update_last_sync_map_name(cur_map_id, &localized_map);
        self.nav_hud.set_language(Some(&resolved));

        let cur_target = {
            let mut nav = self.nav_controller.lock().unwrap_or_else(|e| e.into_inner());
            let cur_id = nav.current_target().map(|p| p.id.clone());
            if let Some(ref pid) = cur_id
                && let Some(pin) = nav.get_pin_mut(pid)
            {
                pin.description.clear();
            }
            nav.current_target().cloned()
        };
        if let Some(ref tgt) = cur_target
            && !offline_gate
        {
            self.ensure_guide(Some(tgt));
        }

        if let Some(tray) = self
            .tray
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
        {
            tray.set_language(Some(&resolved));
        }
        self.refresh_tray_state();

        Ok(json!({
            "language": raw,
            "resolved": resolved,
        }))
    }

    pub fn set_nav_hud(&self, payload: &Value) -> CoreResult<Value> {
        let req = payload.as_object().cloned().unwrap_or_default();
        let (cfg_clone, edit_mode, _) = self.mutate_and_save_config(|cfg| {
            if let Some(v) = req.get("enabled").or_else(|| req.get("nav_hud_enabled"))
                && !v.is_null()
            {
                cfg.nav_hud_enabled = json_truthy(Some(v), true);
            }

            if let Some(cp_val) = req.get("custom_pos") {
                let parsed = if let Some(arr) = cp_val.as_array()
                    && arr.len() == 2
                    && let (Some(fx), Some(fy)) = (json_f64(Some(&arr[0])), json_f64(Some(&arr[1])))
                    && fx.is_finite()
                    && fy.is_finite()
                {
                    NavMiniHud::norm_custom_pos(Some([fx, fy]))
                } else {
                    None
                };
                cfg.nav_hud_custom_pos = parsed;
                self.nav_hud.set_custom_position(parsed);
            }

            if let Some(pos_raw) = req
                .get("position")
                .or_else(|| req.get("nav_hud_position"))
                .and_then(Value::as_str)
            {
                let norm = pos_raw.trim().to_ascii_lowercase();
                if !norm.is_empty() {
                    if norm == "custom" && cfg.nav_hud_custom_pos.is_none() {
                        if cfg.nav_hud_position == "custom" {
                            cfg.nav_hud_position = "under_minimap".to_owned();
                        }
                        self.nav_hud
                            .set_position_mode(&cfg.nav_hud_position, cfg.nav_hud_custom_pos);
                        self.nav_hud.set_edit_mode(true);
                    } else {
                        let parsed = NavMiniHud::norm_position_mode(Some(&norm));
                        cfg.nav_hud_position = parsed.as_str().to_owned();
                        self.nav_hud
                            .set_position_mode(&cfg.nav_hud_position, cfg.nav_hud_custom_pos);
                    }
                }
            }

            if let Some(edit_req) = req.get("edit_mode").filter(|v| !v.is_null()) {
                if edit_req
                    .as_str()
                    .is_some_and(|s| s.trim().eq_ignore_ascii_case("toggle"))
                {
                    self.nav_hud.toggle_edit_mode();
                } else {
                    let want = json_truthy(Some(edit_req), false);
                    self.nav_hud.set_edit_mode(want);
                }
            }

            if let Some(cap_req) = req
                .get("capture_visible")
                .or_else(|| req.get("nav_hud_capture_visible"))
                .filter(|v| !v.is_null())
            {
                let vis = json_truthy(Some(cap_req), false);
                cfg.nav_hud_capture_visible = vis;
                self.nav_hud.set_capture_visible(vis);
            }

            if let Some(zoom_req) = req
                .get("guide_zoom_mode")
                .or_else(|| req.get("nav_hud_guide_zoom_mode"))
                .and_then(Value::as_str)
            {
                let norm = NavMiniHud::norm_guide_zoom_mode(Some(zoom_req));
                cfg.nav_hud_guide_zoom_mode = norm.as_str().to_owned();
                self.nav_hud
                    .set_guide_zoom_mode(&cfg.nav_hud_guide_zoom_mode);
            }

            let edit_mode = self.nav_hud.is_edit_mode();
            if !cfg.nav_hud_enabled && !edit_mode {
                self.nav_hud.hide();
            }
            edit_mode
        });

        Ok(json!({
            "enabled": cfg_clone.nav_hud_enabled,
            "nav_hud_enabled": cfg_clone.nav_hud_enabled,
            "position": cfg_clone.nav_hud_position,
            "nav_hud_position": cfg_clone.nav_hud_position,
            "custom_pos": cfg_clone.nav_hud_custom_pos,
            "edit_mode": edit_mode,
            "capture_visible": cfg_clone.nav_hud_capture_visible,
            "nav_hud_capture_visible": cfg_clone.nav_hud_capture_visible,
            "guide_zoom_mode": cfg_clone.nav_hud_guide_zoom_mode,
            "nav_hud_guide_zoom_mode": cfg_clone.nav_hud_guide_zoom_mode,
        }))
    }

    pub fn handle_hotkey_action(&self) {
        let single_mon = self
            .config
            .read()
            .expect("config lock poisoned")
            .single_monitor_mode;

        let mut rect = self
            .game_state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .rect;
        if rect.is_none_or(|r| r.width < 1000 || r.height < 600)
            && let Some((_, l, t, w, h)) = WindowFinder::get_game_window(&WINDOW_TITLES, true)
        {
            let r = RectI32 {
                x: l,
                y: t,
                width: w,
                height: h,
            };
            rect = Some(r);
            self.game_state
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .rect = Some(r);
        }

        if single_mon && !self.server.has_active_client(10.0) {
            self.hud_toast.show_toast(
                &t("toast.map_not_ready.title", &[]),
                &t("toast.map_not_ready.detail", &[]),
                ToastKind::Error,
                2800,
                rect,
            );
            self.audio.play("error", false);
            return;
        }

        let Some(r) = rect else {
            if single_mon {
                self.hud_toast.show_toast(
                    &t("toast.not_detected.title", &[]),
                    &t("toast.not_detected.detail", &[]),
                    ToastKind::Warning,
                    2500,
                    None,
                );
                self.audio.play("warning", false);
            }
            self.server.broadcast(
                json!({"type": "SYNC_AND_MARK", "detected": false}),
                single_mon,
                false,
            );
            return;
        };

        let Some(full_frame) = self.capturer.capture_region(r.x, r.y, r.width, r.height) else {
            if single_mon {
                self.hud_toast.show_toast(
                    &t("toast.not_detected.title", &[]),
                    &t("borderless.tip", &[]),
                    ToastKind::Warning,
                    2500,
                    Some(r),
                );
                self.audio.play("warning", false);
            }
            self.server.broadcast(
                json!({"type": "SYNC_AND_MARK", "detected": false}),
                single_mon,
                false,
            );
            return;
        };

        let res_opt = self
            .locator
            .as_ref()
            .and_then(|loc| loc.locate_player_frame(full_frame.as_view(), false, None));

        if let Ok(bgr) = gh_cv::ops::color::frame_view_to_bgr(full_frame.as_view()) {
            self.state_machine
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .mark_synced(Some(bgr.view()));
        }

        if let Some(res) = res_opt
            && res.detected
        {
            let lang = get_language();
            let mid = res.map_id.unwrap_or(2);
            let fallback = res.map_name.as_deref().unwrap_or("");
            let loc_map_name = HoYoLABLabelCatalog::get_map_name(mid, Some(&lang), fallback);
            let mut payload = match res.to_json_value() {
                Value::Object(m) => m,
                _ => Map::new(),
            };
            payload.insert("type".to_owned(), Value::String("SYNC_AND_MARK".to_owned()));
            payload.insert("map_name".to_owned(), Value::String(loc_map_name));
            self.server
                .broadcast(Value::Object(payload), false, false);
            if res.has_world_coords {
                self.apply_map_m_anchor(&res);
            }
        } else {
            if single_mon {
                self.hud_toast.show_toast(
                    &t("toast.not_detected.title", &[]),
                    &t("toast.not_detected.detail", &[]),
                    ToastKind::Warning,
                    2500,
                    Some(r),
                );
                self.audio.play("warning", false);
            }
            self.server.broadcast(
                json!({"type": "SYNC_AND_MARK", "detected": false}),
                single_mon,
                false,
            );
        }
    }

    pub fn handle_debug_action(&self, trigger_source: &str, user_note: &str, broadcast: bool) -> Value {
        let bundle = self
            .diagnostics
            .capture_and_create_bundle(None, None, trigger_source, user_note, true, None)
            .unwrap_or_else(|e| json!({"success": false, "error": e.to_string()}));
        if broadcast {
            self.server.broadcast(
                json!({
                    "type": "DIAGNOSTIC_CAPTURED",
                    "folder_name": bundle.get("folder_name").and_then(Value::as_str).unwrap_or(""),
                    "bundle_dir": bundle.get("bundle_dir").and_then(Value::as_str).unwrap_or(""),
                    "has_world_coords": bundle.get("has_world_coords").and_then(Value::as_bool).unwrap_or(false),
                    "reason": bundle.get("reason").and_then(Value::as_str).unwrap_or(""),
                }),
                false,
                false,
            );
        }
        bundle
    }

    pub fn handle_switch_hotkey(&self) {
        if !self.engine_active.load(Ordering::Acquire)
            || !self
                .config
                .read()
                .expect("config lock poisoned")
                .single_monitor_mode
        {
            return;
        }
        let server_cb = self.server.clone();
        let cb = move || {
            server_cb.broadcast(json!({"type": "FOCUS_MAP_TAB"}), false, false);
        };
        let target = WindowSwitcher::toggle_game_and_browser(&WINDOW_TITLES, Some(&cb));
        if target == "no_browser" {
            let rect = self
                .game_state
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .rect;
            self.hud_toast.show_toast(
                &t("toast.map_not_ready.title", &[]),
                &t("toast.map_not_ready.detail", &[]),
                ToastKind::Warning,
                2400,
                rect,
            );
            self.audio.play("warning", false);
        }
    }

    pub fn handle_nav_mark_hotkey(&self) {
        if !self.engine_active.load(Ordering::Acquire) {
            return;
        }
        let (target, dist, ready) = {
            let mut nav = self.nav_controller.lock().unwrap_or_else(|e| e.into_inner());
            let tgt = nav.current_target().cloned();
            let d = nav.last_guidance().and_then(|g| {
                if g.target.as_ref().map(|t| &t.id) == tgt.as_ref().map(|t| &t.id) {
                    g.target_distance_m
                } else {
                    None
                }
            });
            (tgt, d, nav.selection_ready())
        };
        let Some(target) = target else {
            self.nav_flash("hud.flash.no_target", HudPalette::Warning, 2.6, &[]);
            self.audio.play("warning", false);
            return;
        };
        if !ready {
            self.nav_flash("hud.flash.no_target", HudPalette::Warning, 2.6, &[]);
            self.audio.play("warning", false);
            return;
        }

        let now = Instant::now();
        let confirmed = {
            let conf = self
                .nav_mark_confirm
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            conf.pin_id.as_deref() == Some(target.id.as_str())
                && conf.until.is_some_and(|u| now < u)
        };
        if let Some(d) = dist
            && d > NAV_MARK_CONFIRM_DIST_M
            && !confirmed
        {
            let mut conf = self
                .nav_mark_confirm
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            conf.pin_id = Some(target.id.clone());
            conf.until = Some(now + std::time::Duration::from_secs(3));
            let meters = (d.round() as i64).to_string();
            let hk = self.hotkey_mgr.get_display_name("nav_mark");
            self.nav_flash(
                "hud.flash.far_confirm",
                HudPalette::Warning,
                3.0,
                &[("meters", &meters), ("hotkey", &hk)],
            );
            return;
        }

        {
            let mut conf = self
                .nav_mark_confirm
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            conf.pin_id = None;
            conf.until = None;
        }

        let marked = {
            let mut nav = self.nav_controller.lock().unwrap_or_else(|e| e.into_inner());
            nav.mark_collected(Some(&target.id));
            nav.collected_ids.contains(&target.id)
        };
        if !marked {
            self.nav_flash("hud.flash.no_target", HudPalette::Warning, 2.6, &[]);
            return;
        }

        let name = target.display_name().to_owned();
        if self.broadcast_nav_mark(&target, "mark") {
            let undo_hk = self.hotkey_mgr.get_display_name("nav_undo");
            self.nav_flash(
                "hud.flash.marked",
                HudPalette::Success,
                3.2,
                &[("name", &name), ("undo", &undo_hk)],
            );
        } else {
            self.nav_flash(
                "hud.flash.marked_local",
                HudPalette::Success,
                3.2,
                &[("name", &name)],
            );
        }
        self.audio.play("success", false);
        self.push_lan_snapshot(true);
    }

    pub fn handle_nav_undo_hotkey(&self) {
        if !self.engine_active.load(Ordering::Acquire) {
            return;
        }
        let (last_action, pin_id, pin_opt) = {
            let mut nav = self.nav_controller.lock().unwrap_or_else(|e| e.into_inner());
            match nav.peek_history() {
                None => (None, String::new(), None),
                Some((act, pid)) => {
                    let act_owned = act.to_owned();
                    let pid_owned = pid.to_owned();
                    let pin = nav.get_pin(&pid_owned).cloned();
                    nav.undo();
                    (Some(act_owned), pid_owned, pin)
                }
            }
        };
        let Some(action) = last_action else {
            self.nav_flash("hud.flash.nothing_to_undo", HudPalette::Info, 2.6, &[]);
            return;
        };
        if action == "collected"
            && let Some(ref pin) = pin_opt
        {
            self.broadcast_nav_mark(pin, "unmark");
        }
        let display = pin_opt
            .as_ref()
            .map(|p| p.display_name().to_owned())
            .unwrap_or(pin_id);
        self.nav_flash(
            "hud.flash.undone",
            HudPalette::Info,
            2.6,
            &[("name", &display)],
        );
        self.audio.play("info", false);
        self.push_lan_snapshot(true);
    }

    pub fn handle_nav_guide_hotkey(self: &Arc<Self>) {
        if !self.engine_active.load(Ordering::Acquire) {
            return;
        }
        let opened = self.nav_hud.toggle_guide();
        if opened {
            let cur = self
                .nav_controller
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .current_target()
                .cloned();
            if cur.is_none() {
                self.nav_flash("hud.flash.no_target", HudPalette::Warning, 2.6, &[]);
            }
            self.ensure_guide(cur.as_ref());
        }
    }

    pub fn handle_nav_next_hotkey(&self) {
        if !self.engine_active.load(Ordering::Acquire) {
            return;
        }
        let nxt_name = {
            let mut nav = self.nav_controller.lock().unwrap_or_else(|e| e.into_inner());
            nav.next_target().map(|p| p.display_name().to_owned())
        };
        if let Some(name) = nxt_name {
            self.nav_flash("hud.flash.next", HudPalette::Info, 2.0, &[("name", &name)]);
        } else {
            self.nav_flash("hud.flash.no_target", HudPalette::Warning, 2.6, &[]);
        }
    }

    pub fn handle_nav_layout_hotkey(&self) {
        let editing = self.nav_hud.toggle_edit_mode();
        if !editing {
            self.nav_flash("hud.edit.saved", HudPalette::Success, 2.0, &[]);
        }
    }

    pub fn handle_nav_toggle_hotkey(&self) {
        if !self.engine_active.load(Ordering::Acquire) {
            return;
        }
        let next_enabled = !self
            .config
            .read()
            .expect("config lock poisoned")
            .nav_hud_enabled;
        let Ok(res) = self.set_nav_hud(&json!({"enabled": next_enabled})) else {
            return;
        };
        let final_enabled = res
            .get("nav_hud_enabled")
            .and_then(Value::as_bool)
            .unwrap_or(next_enabled);

        let mut bcast = match res {
            Value::Object(m) => m,
            _ => Map::new(),
        };
        bcast.insert(
            "type".to_owned(),
            Value::String("NAV_HUD_UPDATED".to_owned()),
        );
        self.server.broadcast(Value::Object(bcast), false, false);

        if final_enabled {
            let has_guidance = self
                .nav_controller
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .last_guidance()
                .is_some();
            if has_guidance && self.nav_hud.hwnd().is_some() {
                self.nav_hud.invalidate_render();
            }
            self.nav_flash("toast.hud_shown", HudPalette::Info, 1.8, &[]);
            self.audio.play("info", false);
        } else {
            let rect = self
                .game_state
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .rect;
            self.hud_toast.show_toast(
                &t("toast.hud_hidden", &[]),
                &self.hotkey_mgr.get_display_name("nav_toggle"),
                ToastKind::Info,
                1400,
                rect,
            );
            self.audio.play("info", false);
        }
    }

    pub fn on_hud_position_changed(&self, mode: PositionMode, custom_pos: Option<[f64; 2]>) {
        let (cfg_clone, _, _) = self.mutate_and_save_config(|cfg| {
            cfg.nav_hud_position = mode.as_str().to_owned();
            cfg.nav_hud_custom_pos = custom_pos;
        });
        let enabled = cfg_clone.nav_hud_enabled;
        let pos_str = cfg_clone.nav_hud_position;
        self.server.broadcast(
            json!({
                "type": "NAV_HUD_UPDATED",
                "enabled": enabled,
                "position": pos_str,
                "custom_pos": custom_pos,
                "nav_hud_enabled": enabled,
                "nav_hud_position": pos_str,
            }),
            false,
            false,
        );
    }

    pub fn current_sync_url(&self) -> String {
        format!("http://{}:{}", DEFAULT_SYNC_HOST, self.server.port())
    }
}

#[cfg(test)]
pub fn make_test_state(root: &std::path::Path) -> Arc<AppState> {
    make_test_state_with_cli_ports(root, None, None)
}

#[cfg(test)]
pub fn make_test_state_with_cli_ports(
    root: &std::path::Path,
    cli_port: Option<u16>,
    cli_lan_port: Option<u16>,
) -> Arc<AppState> {
    use gh_core::paths::RuntimeMode;
    use gh_net::sync_server::SyncServerConfig;
    use std::time::Duration;

    let mut cfg = AppConfig::default();
    cfg.hoyolab_sync.auto_check_on_startup = false;
    cfg.hoyolab_sync.auto_update_mode = "off".to_owned();
    let config_path = root.join("config.json");
    let _ = cfg.save_to_file(&config_path);
    let disk_ports = (cfg.ws_port, cfg.lan_companion_port);
    if let Some(p) = cli_port
        && p > 0
    {
        cfg.ws_port = p;
    }
    if let Some(lp) = cli_lan_port
        && lp > 0
    {
        cfg.lan_companion_port = lp;
    }

    let paths = AppPaths {
        mode: RuntimeMode::Installed,
        config_path: config_path.clone(),
        diagnostics_dir: root.join("diagnostics"),
        data_dir: root.join("data"),
        tiles_dir: root.join("tiles"),
        ug_overlays_dir: root.join("ug_overlays"),
        cache_dir: root.join("cache"),
        dev_data_dir: None,
    };

    let config = Arc::new(RwLock::new(cfg.clone()));
    let capturer = Arc::new(ScreenCapturer::new(2));
    let maps_db = Arc::new(MultimapDb::default());
    let hd_provider = Arc::new(
        HoyolabHdTileProvider::new(
            Some(paths.tiles_dir.clone()),
            Some(Arc::clone(&maps_db)),
            Duration::from_secs(1),
            true,
            true,
            1,
        )
        .unwrap(),
    );
    let ug_provider = Arc::new(
        UndergroundOverlayProvider::new(
            Some(paths.ug_overlays_dir.clone()),
            Some(Arc::clone(&maps_db)),
            Duration::from_secs(1),
            true,
            true,
            1,
        )
        .unwrap(),
    );

    let lan_companion = Arc::new(LanCompanionServer::new(gh_net::lan_companion::LanCompanionConfig {
        host: "127.0.0.1".to_owned(),
        port: cfg.lan_companion_port,
        advertised_host: Some("127.0.0.1".to_owned()),
        ..Default::default()
    }));
    let server = SyncServer::new(SyncServerConfig {
        port: 0,
        config: cfg.clone(),
        config_path: Some(config_path.clone()),
        diagnostics_dir: Some(paths.diagnostics_dir.clone()),
        lan_companion: Some(Arc::clone(&lan_companion)),
        ..Default::default()
    });

    let game_state = Mutex::new(GameState::default());
    let nav_state_snapshot = Mutex::new(None);

    let srv_for_bs = server.clone();
    let diagnostics = Arc::new(DiagnosticManager::new(
        paths.clone(),
        Arc::clone(&capturer),
        None,
        Arc::clone(&config),
        Arc::new(move || srv_for_bs.last_browser_state()),
        Arc::new(|| None),
        Arc::new(|| None),
    ));

    let (stop_tx, _stop_rx) = watch::channel(false);

    Arc::new(AppState {
        paths,
        config_path,
        config,
        cli_port: cli_port.filter(|&p| p > 0),
        cli_lan_port: cli_lan_port.filter(|&lp| lp > 0),
        disk_ports: Mutex::new(disk_ports),
        config_io_lock: Mutex::new(()),
        last_self_saved_mtime: Mutex::new(None),
        engine_active: AtomicBool::new(true),
        game_state,
        nav_state_snapshot,
        nav_controller: Mutex::new(NavigationController::new().with_space_debounce_frames(4)),
        route_state: Mutex::new(RouteState::default()),
        nav_mark_confirm: Mutex::new(NavMarkConfirm::default()),
        tracker: Mutex::new(None),
        locator: None,
        state_machine: Mutex::new(MapScreenStateMachine::with_anchors_path(None)),
        capturer,
        server,
        lan_companion,
        nav_hud: Arc::new(NavMiniHud::new(gh_hud::nav_hud::NavHudConfig {
            screen_offset: gh_hud::layered::OFFSCREEN_TEST_OFFSET,
            ..Default::default()
        })),
        hud_toast: Arc::new(HudToastOverlay::with_screen_offset(
            gh_hud::layered::OFFSCREEN_TEST_OFFSET,
        )),
        audio: Arc::new(AudioFeedback::new(false, 50)),
        tray: Mutex::new(None),
        hotkey_mgr: Arc::new(UnifiedHotkeyManager::new(false, None)),
        conflict_advisor: Arc::new(HotkeyConflictAdvisor::new()),
        guide_fetcher: PinGuideFetcher::new(Arc::new(get_language)),
        label_catalog: HoYoLABLabelCatalog::new(),
        maps_db,
        affine_registry: PerMapAffineRegistry::new(),
        hd_provider,
        ug_provider,
        minimap_recorder: Mutex::new(None),
        diagnostics,
        stop_tx,
        start_instant: Instant::now(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Duration;

    static STATE_TEST_SEQ: AtomicUsize = AtomicUsize::new(0);

    #[test]
    fn set_hotkeys_rebinding_kb_to_mouse_under_read_contention_does_not_deadlock() {
        let seq = STATE_TEST_SEQ.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "gh_app_state_hk_{}_{seq}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();

        let state = make_test_state(&root);
        let state_weak = Arc::downgrade(&state);
        state.hotkey_mgr.set_should_suppress(Some(Arc::new(move |_| {
            state_weak
                .upgrade()
                .map(|st| st.config.read().expect("config lock poisoned").hotkey_suppress_conflicts)
                .unwrap_or(false)
        })));

        {
            let cfg = state.config.read().unwrap().clone();
            for act in HOTKEY_ACTIONS {
                let hk = get_hotkey_field(&cfg, act);
                state
                    .hotkey_mgr
                    .register(act, &hk, Arc::new(|| {}), true, None);
            }
        }
        assert!(state.hotkey_mgr.start());

        let stop_reader = Arc::new(AtomicBool::new(false));
        let reader_ready = Arc::new(AtomicBool::new(false));
        let reader_stop = Arc::clone(&stop_reader);
        let reader_ready_tx = Arc::clone(&reader_ready);
        let reader_state = Arc::clone(&state);
        let reader_handle = std::thread::spawn(move || {
            let mut reads = 0usize;
            while !reader_stop.load(Ordering::Acquire) {
                let _suppress = reader_state
                    .config
                    .read()
                    .expect("config lock poisoned")
                    .hotkey_suppress_conflicts;
                let _disp = reader_state.hotkey_mgr.get_display_name("sync");
                reads += 1;
                if reads == 1 {
                    reader_ready_tx.store(true, Ordering::Release);
                }
                if reads.is_multiple_of(64) {
                    std::thread::yield_now();
                }
            }
            reads
        });

        let wait_start = Instant::now();
        while !reader_ready.load(Ordering::Acquire) {
            assert!(
                wait_start.elapsed() < Duration::from_secs(5),
                "reader_handle did not enter read loop within 5s"
            );
            std::thread::yield_now();
        }

        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let worker_state = Arc::clone(&state);
        let worker_handle = std::thread::spawn(move || {
            // Transition all 9 actions from keyboard -> mouse4 so `need_kb` becomes false
            // and `UnifiedHotkeyManager::update_binding` runs `join_hook_thread` on `gh-kb-hook`.
            let mut to_mouse = Map::new();
            for act in HOTKEY_ACTIONS {
                to_mouse.insert(format!("hotkey_{act}"), json!("mouse4"));
            }
            let r1 = worker_state.set_hotkeys(&Value::Object(to_mouse)).unwrap();
            assert_eq!(r1["ok"], json!(true));

            // Transition back to keyboard (`f9`) and then back to mouse (`mouse5`) under contention.
            let r2 = worker_state
                .set_hotkeys(&json!({"action": "sync", "hotkey": "f9"}))
                .unwrap();
            assert_eq!(r2["ok"], json!(true));
            assert_eq!(r2["results"]["sync"]["hotkey"], json!("f9"));

            let r3 = worker_state
                .set_hotkeys(&json!({"action": "sync", "hotkey": "mouse5"}))
                .unwrap();
            assert_eq!(r3["ok"], json!(true));
            assert_eq!(r3["results"]["sync"]["hotkey"], json!("mouse5"));

            // Partial batch failure: valid `nav_mark` updates, invalid `debug` stays at prior binding.
            let r4 = worker_state
                .set_hotkeys(&json!({
                    "hotkey_nav_mark": "f10",
                    "hotkey_debug": "not_a_valid_key_xyz",
                }))
                .unwrap();
            assert_eq!(r4["ok"], json!(false));
            assert_eq!(r4["results"]["nav_mark"]["ok"], json!(true));
            assert_eq!(r4["results"]["nav_mark"]["hotkey"], json!("f10"));
            assert_eq!(r4["results"]["debug"]["ok"], json!(false));

            let _ = done_tx.send(());
        });

        done_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("set_hotkeys deadlocked during kb <-> mouse rebinding under config.read contention");
        worker_handle.join().unwrap();

        stop_reader.store(true, Ordering::Release);
        let read_iters = reader_handle.join().unwrap();
        assert!(read_iters > 0);

        {
            let cfg = state.config.read().unwrap();
            assert_eq!(cfg.hotkey_sync, "mouse5");
            assert_eq!(cfg.hotkey, "mouse5");
            assert_eq!(cfg.hotkey_nav_mark, "f10");
            assert_eq!(cfg.hotkey_debug, "mouse4");
        }
        let disk_cfg = AppConfig::load_from_file(&state.config_path).unwrap();
        assert_eq!(disk_cfg.hotkey_sync, "mouse5");
        assert_eq!(disk_cfg.hotkey_nav_mark, "f10");
        assert_eq!(disk_cfg.hotkey_debug, "mouse4");

        state.hotkey_mgr.stop();
        state.nav_hud.close();
        state.hud_toast.close();
        state.guide_fetcher.stop();
        state.audio.close();
        state.capturer.close();
        let _ = std::fs::remove_dir_all(&root);
    }
}
