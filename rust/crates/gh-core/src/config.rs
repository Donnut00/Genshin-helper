//! Backwards-compatible `config.json` schema matching `src/config.py`,
//! extended with optional overridable `hoyolab_sync` settings (defaulted via `#[serde(default)]`).

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const VALID_LANGUAGES: [&str; 3] = ["auto", "vi", "en"];
pub const VALID_GUIDE_ZOOM_MODES: [&str; 3] = ["cycle", "compact", "large"];
pub const VALID_AUTO_UPDATE_MODES: [&str; 3] = ["auto_download", "notify_only", "off"];

pub const DEFAULT_HOYOLAB_BASE_API: &str = "https://sg-public-api.hoyolab.com/common/map_user/ys_obc";
pub const DEFAULT_HOYOLAB_TILE_BASE: &str = "https://act-webstatic.hoyoverse.com/map_manage/map";
pub const DEFAULT_HOYOLAB_RPC_MAP_VERSION: &str = "4.5";
pub const DEFAULT_HOYOLAB_USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)";
pub const DEFAULT_PROBE_ID_RANGE: [i32; 2] = [1, 80];

pub const DEFAULT_INSTRUCTIONS_VI: &str = "Cấu hình phím tắt cho Genshin Helper. Hỗ trợ: mouse4, mouse5, middle, alt+f11, alt+f12, alt+q, f10, alt+mouse4, ctrl+q, v.v. Bạn có thể sửa trực tiếp tại đây hoặc click vào biểu tượng bánh răng trên Web Map.";

/// Overridable HoYoLAB map dataset sync & startup version-check settings (`src/hoyolab_sync.py`).
///
/// Kept optional in `config.json` via `#[serde(default)]` so legacy configs and `extension/`
/// remain 100% compatible without changes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct HoyolabSyncConfig {
    pub base_api: String,
    pub tile_base: String,
    pub rpc_map_version: String,
    pub user_agent: String,
    pub default_map_ids: Vec<i32>,
    /// Inclusive `[min_id, max_id]` probe range when discovering newly added HoYoLAB maps.
    pub probe_id_range: [i32; 2],
    /// Map search priority keyed by stringified `map_id` (lower = higher priority).
    pub map_priorities: BTreeMap<String, i32>,
    pub primary_landmark_labels: Vec<i32>,
    /// Non-blocking check comparing online `detail_v2.map_version` vs local `multimap_v4.json` on startup.
    pub auto_check_on_startup: bool,
    /// `"auto_download"` (background staged download + atomic swap) | `"notify_only"` | `"off"`.
    pub auto_update_mode: String,
}

impl Default for HoyolabSyncConfig {
    fn default() -> Self {
        let mut map_priorities = BTreeMap::new();
        for (mid, prio) in [(2, 1), (7, 4), (9, 3), (34, 5), (36, 6), (37, 7), (40, 2)] {
            map_priorities.insert(mid.to_string(), prio);
        }
        Self {
            base_api: DEFAULT_HOYOLAB_BASE_API.to_owned(),
            tile_base: DEFAULT_HOYOLAB_TILE_BASE.to_owned(),
            rpc_map_version: DEFAULT_HOYOLAB_RPC_MAP_VERSION.to_owned(),
            user_agent: DEFAULT_HOYOLAB_USER_AGENT.to_owned(),
            default_map_ids: vec![2, 7, 9, 34, 36, 37, 40],
            probe_id_range: DEFAULT_PROBE_ID_RANGE,
            map_priorities,
            primary_landmark_labels: vec![2, 3, 154],
            auto_check_on_startup: true,
            auto_update_mode: "auto_download".to_owned(),
        }
    }
}

impl HoyolabSyncConfig {
    pub fn normalize(&mut self) {
        if self.base_api.trim().is_empty() {
            self.base_api = DEFAULT_HOYOLAB_BASE_API.to_owned();
        } else {
            self.base_api = self.base_api.trim().trim_end_matches('/').to_owned();
        }
        if self.tile_base.trim().is_empty() {
            self.tile_base = DEFAULT_HOYOLAB_TILE_BASE.to_owned();
        } else {
            self.tile_base = self.tile_base.trim().trim_end_matches('/').to_owned();
        }
        if self.rpc_map_version.trim().is_empty() {
            self.rpc_map_version = DEFAULT_HOYOLAB_RPC_MAP_VERSION.to_owned();
        } else {
            self.rpc_map_version = self.rpc_map_version.trim().to_owned();
        }
        if self.user_agent.trim().is_empty() {
            self.user_agent = DEFAULT_HOYOLAB_USER_AGENT.to_owned();
        }
        if self.default_map_ids.is_empty() {
            self.default_map_ids = vec![2, 7, 9, 34, 36, 37, 40];
        }
        if self.primary_landmark_labels.is_empty() {
            self.primary_landmark_labels = vec![2, 3, 154];
        }

        let min_id = self.probe_id_range[0].clamp(1, 500);
        let max_id = self.probe_id_range[1].clamp(min_id, 500);
        self.probe_id_range = [min_id, max_id];

        let mode = self.auto_update_mode.trim().to_ascii_lowercase();
        self.auto_update_mode = if VALID_AUTO_UPDATE_MODES.contains(&mode.as_str()) {
            mode
        } else {
            "auto_download".to_owned()
        };
    }

    pub fn priority_for_map(&self, map_id: i32) -> i32 {
        self.map_priorities
            .get(&map_id.to_string())
            .copied()
            .unwrap_or(50 + map_id)
    }
}

/// Persisted user configuration (`config.json`, 26 preserved legacy fields + `hoyolab_sync`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    pub hotkey: String,
    pub hotkey_sync: String,
    pub hotkey_debug: String,
    pub hotkey_switch: String,
    pub hotkey_nav_mark: String,
    pub hotkey_nav_undo: String,
    pub hotkey_nav_guide: String,
    pub hotkey_nav_next: String,
    pub hotkey_nav_layout: String,
    pub hotkey_nav_toggle: String,
    pub hotkey_suppress_conflicts: bool,
    pub audio_enabled: bool,
    pub audio_volume: i32,
    pub chest_search_radius: f64,
    pub ws_port: u16,
    pub auto_sync_m: bool,
    pub single_monitor_mode: bool,
    pub language: String,
    pub minimap_tracking: bool,
    pub nav_hud_enabled: bool,
    pub nav_hud_capture_visible: bool,
    pub nav_hud_position: String,
    pub nav_hud_custom_pos: Option<[f64; 2]>,
    pub nav_hud_guide_zoom_mode: String,
    pub lan_companion_enabled: bool,
    pub lan_companion_port: u16,
    pub hoyolab_sync: HoyolabSyncConfig,
    pub instructions: String,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            hotkey: "alt+f11".to_owned(),
            hotkey_sync: "alt+f11".to_owned(),
            hotkey_debug: "alt+f12".to_owned(),
            hotkey_switch: "alt+q".to_owned(),
            hotkey_nav_mark: "alt+k".to_owned(),
            hotkey_nav_undo: "alt+shift+k".to_owned(),
            hotkey_nav_guide: "alt+h".to_owned(),
            hotkey_nav_next: "alt+n".to_owned(),
            hotkey_nav_layout: "alt+shift+h".to_owned(),
            hotkey_nav_toggle: "alt+x".to_owned(),
            hotkey_suppress_conflicts: true,
            audio_enabled: true,
            audio_volume: 50,
            chest_search_radius: 30.0,
            ws_port: crate::DEFAULT_SYNC_PORT,
            auto_sync_m: true,
            single_monitor_mode: false,
            language: "auto".to_owned(),
            minimap_tracking: true,
            nav_hud_enabled: true,
            nav_hud_capture_visible: false,
            nav_hud_position: "under_minimap".to_owned(),
            nav_hud_custom_pos: None,
            nav_hud_guide_zoom_mode: "cycle".to_owned(),
            lan_companion_enabled: false,
            lan_companion_port: crate::DEFAULT_LAN_PORT,
            hoyolab_sync: HoyolabSyncConfig::default(),
            instructions: DEFAULT_INSTRUCTIONS_VI.to_owned(),
        }
    }
}

impl AppConfig {
    /// Normalizes, clamps, and validates fields identically to `src/config.py`.
    pub fn normalize(&mut self) {
        let primary = normalize_hotkey(&self.hotkey);
        let sync = normalize_hotkey(&self.hotkey_sync);
        let resolved_sync = if !primary.is_empty() {
            primary
        } else if !sync.is_empty() {
            sync
        } else {
            "alt+f11".to_owned()
        };
        self.hotkey = resolved_sync.clone();
        self.hotkey_sync = resolved_sync;

        self.hotkey_debug = non_empty_or(&self.hotkey_debug, "alt+f12");
        self.hotkey_switch = non_empty_or(&self.hotkey_switch, "alt+q");
        self.hotkey_nav_mark = non_empty_or(&self.hotkey_nav_mark, "alt+k");
        self.hotkey_nav_undo = non_empty_or(&self.hotkey_nav_undo, "alt+shift+k");
        self.hotkey_nav_guide = non_empty_or(&self.hotkey_nav_guide, "alt+h");
        self.hotkey_nav_next = non_empty_or(&self.hotkey_nav_next, "alt+n");
        self.hotkey_nav_layout = non_empty_or(&self.hotkey_nav_layout, "alt+shift+h");
        self.hotkey_nav_toggle = non_empty_or(&self.hotkey_nav_toggle, "alt+x");

        self.audio_volume = self.audio_volume.clamp(0, 100);
        if self.ws_port == 0 {
            self.ws_port = crate::DEFAULT_SYNC_PORT;
        }
        if self.lan_companion_port == 0 {
            self.lan_companion_port = crate::DEFAULT_LAN_PORT;
        }

        let lang = self.language.trim().to_ascii_lowercase();
        self.language = if VALID_LANGUAGES.contains(&lang.as_str()) {
            lang
        } else {
            "auto".to_owned()
        };

        let pos = self.nav_hud_position.trim().to_ascii_lowercase();
        self.nav_hud_position = if pos.is_empty() {
            "under_minimap".to_owned()
        } else {
            pos
        };

        self.nav_hud_custom_pos = self.nav_hud_custom_pos.and_then(|[fx, fy]| {
            if fx.is_finite()
                && fy.is_finite()
                && (0.0..=1.0).contains(&fx)
                && (0.0..=1.0).contains(&fy)
            {
                Some([
                    (fx * 10_000.0).round() / 10_000.0,
                    (fy * 10_000.0).round() / 10_000.0,
                ])
            } else {
                None
            }
        });

        let gzm = self.nav_hud_guide_zoom_mode.trim().to_ascii_lowercase();
        self.nav_hud_guide_zoom_mode = if VALID_GUIDE_ZOOM_MODES.contains(&gzm.as_str()) {
            gzm
        } else {
            "cycle".to_owned()
        };

        self.hoyolab_sync.normalize();
    }

    pub fn from_json_str(raw: &str) -> crate::CoreResult<Self> {
        let mut cfg: Self = serde_json::from_str(raw)?;
        cfg.normalize();
        Ok(cfg)
    }
}

fn normalize_hotkey(raw: &str) -> String {
    raw.trim().to_ascii_lowercase()
}

fn non_empty_or(raw: &str, fallback: &str) -> String {
    let cleaned = normalize_hotkey(raw);
    if cleaned.is_empty() {
        fallback.to_owned()
    } else {
        cleaned
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_python_invariants() {
        let cfg = AppConfig::default();
        assert_eq!(cfg.hotkey, "alt+f11");
        assert_eq!(cfg.hotkey_sync, "alt+f11");
        assert_eq!(cfg.hotkey_nav_mark, "alt+k");
        assert_eq!(cfg.hotkey_nav_undo, "alt+shift+k");
        assert_eq!(cfg.ws_port, 8765);
        assert_eq!(cfg.lan_companion_port, 8766);
        assert!(!cfg.lan_companion_enabled);
        assert!(!cfg.nav_hud_capture_visible);
        assert!(cfg.hotkey_suppress_conflicts);
        assert_eq!(cfg.hoyolab_sync.rpc_map_version, "4.5");
        assert_eq!(cfg.hoyolab_sync.probe_id_range, [1, 80]);
        assert_eq!(cfg.hoyolab_sync.auto_update_mode, "auto_download");
        assert_eq!(cfg.hoyolab_sync.priority_for_map(40), 2);
        assert_eq!(cfg.hoyolab_sync.priority_for_map(99), 149);
    }

    #[test]
    fn json_roundtrip_and_normalization() {
        let raw = r#"{
            "hotkey": "ALT+F10",
            "audio_volume": 150,
            "language": "INVALID",
            "nav_hud_custom_pos": [0.123456, 0.5],
            "nav_hud_guide_zoom_mode": "LARGE"
        }"#;
        let cfg = AppConfig::from_json_str(raw).expect("valid config json");
        assert_eq!(cfg.hotkey, "alt+f10");
        assert_eq!(cfg.hotkey_sync, "alt+f10");
        assert_eq!(cfg.audio_volume, 100);
        assert_eq!(cfg.language, "auto");
        assert_eq!(cfg.nav_hud_custom_pos, Some([0.1235, 0.5]));
        assert_eq!(cfg.nav_hud_guide_zoom_mode, "large");
        // Missing `hoyolab_sync` block falls back cleanly to defaults
        assert_eq!(cfg.hoyolab_sync, HoyolabSyncConfig::default());

        let serialized = serde_json::to_string_pretty(&cfg).expect("serialize config");
        let reparsed = AppConfig::from_json_str(&serialized).expect("reparse config");
        assert_eq!(cfg, reparsed);
    }

    #[test]
    fn hoyolab_sync_partial_override_and_validation() {
        let raw = r#"{
            "hoyolab_sync": {
                "base_api": "https://example.com/api/",
                "rpc_map_version": "6.0",
                "probe_id_range": [50, 10],
                "auto_update_mode": "bogus"
            }
        }"#;
        let cfg = AppConfig::from_json_str(raw).expect("valid json");
        assert_eq!(cfg.hoyolab_sync.base_api, "https://example.com/api");
        assert_eq!(cfg.hoyolab_sync.rpc_map_version, "6.0");
        assert_eq!(cfg.hoyolab_sync.probe_id_range, [50, 50]);
        assert_eq!(cfg.hoyolab_sync.auto_update_mode, "auto_download");
        assert_eq!(cfg.hoyolab_sync.default_map_ids, vec![2, 7, 9, 34, 36, 37, 40]);
    }

    #[test]
    fn out_of_range_custom_pos_is_cleared() {
        let raw = r#"{"nav_hud_custom_pos": [1.5, 0.2]}"#;
        let cfg = AppConfig::from_json_str(raw).expect("valid json");
        assert_eq!(cfg.nav_hud_custom_pos, None);
    }
}
