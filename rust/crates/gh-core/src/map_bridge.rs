//! Cross-map pin exporter & affine coordinate bridge (`src/map_bridge.py`).
//! Phase 2 target: embedded `category_aliases.json` + Canonical/Kongying/Appsample/GenshinMap.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ExportFormat {
    Canonical,
    Kongying,
    Appsample,
    Genshinmap,
}
