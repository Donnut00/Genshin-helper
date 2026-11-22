//! Visual 3-Anchor Map (M) detector, 4-state FSM, and RGB64 Bhattacharyya scene-cut (`src/map_state.py`).

pub const ANCHOR_THRESHOLD: f64 = 0.82;
pub const SLIDER_THRESHOLD: f64 = 0.88;
pub const SLIDER_STRICT_MATCH: f64 = 0.975;
pub const SLIDER_CCOEFF_THRESHOLD: f64 = 0.65;
