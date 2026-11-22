//! Overlay HUD & Vietnamese PNG annotation crate: Win32 `UpdateLayeredWindow` +
//! `tiny-skia` + `cosmic-text` (pre-baked 2-layer `<0.4 ms/frame`, zero D3D/DXGI contention).
//!
//! Acyclic dependency: depends ONLY on `gh-core` + `gh-win32`.

pub mod diag_annotate;
pub mod fonts;
pub mod layered;
pub mod nav_hud;
pub mod toast;
