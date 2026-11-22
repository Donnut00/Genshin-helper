//! Pure Unity CanvasScaler UI layout geometry (split from `src/game_settings.py`).

use crate::frame::RectI32;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct UiLayout {
    pub viewport_w: u32,
    pub viewport_h: u32,
    pub scale_ui: f64,
    pub minimap_rect: RectI32,
    pub is_gamepad: bool,
}
