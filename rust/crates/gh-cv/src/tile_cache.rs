//! 72-tile O(1) LRU terrain tile cache + `diag(1, -1, 1)` underground Y-reflect patch extractor
//! (`src/tile_cache.py`).

pub const LRU_TILE_CAPACITY: usize = 72;
