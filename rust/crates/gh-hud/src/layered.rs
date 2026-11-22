//! Reusable Win32 `WS_EX_LAYERED` window host, premultiplied BGRA DIBSection,
//! 2.0s `WDA_EXCLUDEFROMCAPTURE` refresh timer, and `WM_APP_CAPTURE_AFFINITY` (`0x8015`).

pub const WDA_REFRESH_INTERVAL_MS: u64 = 2000;
pub const CAPTURE_AFFINITY_MSG: u32 = gh_core::WM_APP_CAPTURE_AFFINITY;
pub const CAPTURE_ROP_GUARD: u32 = gh_win32::capture::BITBLT_ROP_SRCCOPY;
