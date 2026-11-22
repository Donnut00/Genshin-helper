//! Runtime path resolution replacing PyInstaller's `sys._MEIPASS` (`src/paths.py`).

use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeMode {
    /// Installed layout: `%APPDATA%\GenshinHelper` & `%LOCALAPPDATA%\GenshinHelper`.
    Installed,
    /// Developer workspace layout: repo root `config.json`, `scratch/diagnostics/`, `src/data/`.
    Dev,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AppPaths {
    pub mode: RuntimeMode,
    pub config_path: PathBuf,
    pub diagnostics_dir: PathBuf,
    pub data_dir: PathBuf,
    pub tiles_dir: PathBuf,
    pub ug_overlays_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub dev_data_dir: Option<PathBuf>,
}
