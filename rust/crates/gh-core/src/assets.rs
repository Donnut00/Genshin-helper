//! Dataset manifest verification, staged promotion & rollback (`src/asset_setup.py`).
//! Phase 2 target: orchestrates `DatasetSyncRunner` without depending on `gh-net`.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DataStatus {
    Ready,
    Missing,
    Corrupt,
}
