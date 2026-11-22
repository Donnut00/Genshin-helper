//! Wire-compatible HTTP, WebSocket, and Chrome Native Messaging DTOs
//! matching 100% of `extension/` expectations without modifying `extension/`.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const MAX_NATIVE_MESSAGE_BYTES: usize = 1_048_576;

/// Response schema for `GET /status` (`src/server.py` + `main.py` callback fields).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StatusResponse {
    pub status: String,
    pub version: String,
    pub script_version: String,
    pub queue_len: usize,
    pub latest_seq: u64,
    pub hotkey: String,
    pub display_name: String,
    pub hotkey_sync: String,
    pub display_name_sync: String,
    pub hotkey_debug: String,
    pub display_name_debug: String,
    pub hotkey_switch: String,
    pub display_name_switch: String,
    pub hotkey_nav_mark: String,
    pub display_name_nav_mark: String,
    pub hotkey_nav_undo: String,
    pub display_name_nav_undo: String,
    pub hotkey_nav_guide: String,
    pub display_name_nav_guide: String,
    pub hotkey_nav_next: String,
    pub display_name_nav_next: String,
    pub hotkey_nav_layout: String,
    pub display_name_nav_layout: String,
    pub hotkey_nav_toggle: String,
    pub display_name_nav_toggle: String,
    pub hotkey_suppress_conflicts: bool,
    pub auto_sync_m: bool,
    pub single_monitor_mode: bool,
    pub monitor_count: usize,
    pub engine_active: bool,
    pub language: String,
    pub language_setting: String,
    pub minimap_tracking: bool,
    pub nav_hud_enabled: bool,
    pub nav_hud_position: String,
    pub nav_hud_custom_pos: Option<[f64; 2]>,
    pub nav_hud_edit_mode: bool,
    pub nav_hud_capture_visible: bool,
    pub nav_hud_guide_zoom_mode: String,
    pub audio_enabled: bool,
    pub audio_volume: i32,
    pub lan_companion_running: bool,
    pub uipi_mismatch: bool,
    pub uipi_status: Value,
    pub last_sync: Option<Value>,
}

/// Event kinds broadcast over lossless `/poll?since=<seq>`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BroadcastEventType {
    // Durable events stored in `_event_log` ring buffer (maxlen = 128)
    SyncPositionOnly,
    SyncAndMark,
    EngineStateUpdated,
    AutoSyncUpdated,
    SingleMonitorUpdated,
    HotkeyUpdated,
    LanguageUpdated,
    MinimapTrackingUpdated,
    NavHudUpdated,
    AudioUpdated,
    TspRouteUpdated,
    DiagnosticCaptured,
    FocusMapTab,
    NavMarkPin,
    LanCompanionUpdated,
    UipiStatusUpdated,
    // Ephemeral coalesced event (never evicts `_event_log`)
    MinimapPosition,
    // Control frames
    Heartbeat,
    Shutdown,
}

/// Enveloped event returned by `/poll?since=<seq>` with monotonic `_seq`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PollEnvelope {
    #[serde(rename = "type")]
    pub event_type: BroadcastEventType,
    #[serde(rename = "_seq", skip_serializing_if = "Option::is_none")]
    pub seq: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub script_version: Option<String>,
    #[serde(flatten)]
    pub extra: serde_json::Map<String, Value>,
}

impl PollEnvelope {
    pub fn heartbeat(seq: u64) -> Self {
        Self {
            event_type: BroadcastEventType::Heartbeat,
            seq: Some(seq),
            script_version: Some(crate::SCRIPT_VERSION.to_owned()),
            extra: serde_json::Map::new(),
        }
    }

    pub fn shutdown() -> Self {
        Self {
            event_type: BroadcastEventType::Shutdown,
            seq: None,
            script_version: None,
            extra: serde_json::Map::new(),
        }
    }
}

/// Request payload for Chrome Native Messaging (`com.genshinhelper.host`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum NativeRequest {
    Ping,
    StartServer,
    StopServer {
        #[serde(default = "default_stop_mode")]
        mode: String,
    },
}

fn default_stop_mode() -> String {
    "standby".to_owned()
}

/// Response payload for Chrome Native Messaging (`GenshinHelperHost.exe`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NativeResponse {
    pub ok: bool,
    pub host_version: String,
    pub server_running: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub engine_active: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub acknowledged: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActionFeedbackRequest {
    pub event_type: String,
    pub status: String,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub detail: Option<String>,
    #[serde(default)]
    pub seq: Option<i64>,
    #[serde(default)]
    pub action: Option<String>,
    #[serde(default)]
    pub pin_id: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn poll_heartbeat_serializes_exact_wire_keys() {
        let hb = PollEnvelope::heartbeat(42);
        let v = serde_json::to_value(&hb).expect("serialize heartbeat");
        assert_eq!(v["type"], "HEARTBEAT");
        assert_eq!(v["_seq"], 42);
        assert_eq!(v["script_version"], crate::SCRIPT_VERSION);
    }

    #[test]
    fn native_messaging_request_roundtrip() {
        let ping: NativeRequest = serde_json::from_str(r#"{"cmd":"ping"}"#).expect("parse ping");
        assert_eq!(ping, NativeRequest::Ping);

        let stop: NativeRequest =
            serde_json::from_str(r#"{"cmd":"stop_server","mode":"shutdown"}"#).expect("parse stop");
        assert_eq!(
            stop,
            NativeRequest::StopServer {
                mode: "shutdown".to_owned()
            }
        );
    }
}
