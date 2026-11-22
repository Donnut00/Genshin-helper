"""
HoYoLAB v4 Official Data Sync CLI Wrapper.
Delegates in-process sync logic to `src.hoyolab_sync` so both source runs and frozen
PyInstaller builds execute identical hydration logic.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.hoyolab_sync import (  # noqa: F401
    BASE_API,
    DEFAULT_MAP_IDS,
    HEADERS,
    MAP_PRIORITIES,
    PRIMARY_LANDMARK_LABELS,
    TILE_BASE,
    discover_active_maps,
    download_cave_masks,
    download_terrain_tiles,
    fetch_map_bundle,
    load_legacy_floor_shorts,
    sync_to_directory,
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    data_dir = PROJECT_ROOT / "src" / "data"
    legacy_ug_path = PROJECT_ROOT / "src" / "underground_db.json"
    sync_to_directory(data_dir, legacy_ug_path=legacy_ug_path if legacy_ug_path.is_file() else None)


if __name__ == "__main__":
    main()
