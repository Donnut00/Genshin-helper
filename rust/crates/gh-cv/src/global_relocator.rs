//! Full-Teyvat RootSIFT KD-Tree global relocator (`src/global_relocator.py`).
//!
//! Bumped to `INDEX_VERSION = 3` (`teyvat_global_index_v3.npz`) so Rust never mixes
//! descriptors with Python OpenCV's `v2` cache.

pub const INDEX_VERSION: i32 = 3;
