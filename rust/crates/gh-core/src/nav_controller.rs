//! Shared navigation target and guidance types (`src/nav_controller.py`).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NavPin {
    pub id: String,
    pub map_id: i32,
    pub lat: f64,
    pub lng: f64,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub category_id: Option<i64>,
    #[serde(default)]
    pub floor_id: Option<String>,
    #[serde(default)]
    pub floor_name: Option<String>,
    #[serde(default)]
    pub icon_url: Option<String>,
    #[serde(default)]
    pub priority: i32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct NavGuidance {
    pub target: Option<NavPin>,
    pub distance_m: f64,
    pub relative_bearing_deg: f64,
    pub floor_mismatch: bool,
    pub is_route_mode: bool,
    pub remaining_count: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NavAction {
    Next,
    Prev,
    Skip,
    Undo,
    Refresh,
    ClearRoute,
    Blacklist,
    Mark,
    Guide,
    Layout,
}
