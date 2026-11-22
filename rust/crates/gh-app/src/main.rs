//! Main desktop GUI binary (`GenshinHelper.exe`) wiring `gh-core`, `gh-win32`,
//! `gh-net`, `gh-cv`, and `gh-hud` (`main.py`).
//!
//! Note: `#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]` will be
//! enabled once the desktop event loop and tray are wired in Phase 4.

pub mod callbacks;
pub mod config_watcher;
pub mod diagnostics;
pub mod minimap_recorder;
pub mod tracking_loop;

use gh_core::{APP_VERSION, PINNED_EXTENSION_ID};

fn main() {
    // Phase 4 target: CLI flags (--no-tray, --standby, --minimized, --record-minimap),
    // single-instance mutex, first-run dataset hydration, and live tracking orchestration.
    let _ = (
        APP_VERSION,
        PINNED_EXTENSION_ID,
        gh_win32::capture::BITBLT_ROP_SRCCOPY,
        gh_net::sync_server::EVENT_LOG_MAXLEN,
        gh_cv::global_relocator::INDEX_VERSION,
        gh_hud::layered::WDA_REFRESH_INTERVAL_MS,
    );
}
