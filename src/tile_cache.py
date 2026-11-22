# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/underground_pyramid.py (MIT License)
# Copyright (c) 2026 MaxWay12
# Bounded O(1) LRU terrain tile cache, lazy NPZ tile slicing, disk tile loader,
# and sub-pixel local/underground patch extractors are clean-room original implementations.
"""Bounded O(1) RAM LRU terrain tile cache and sub-pixel surface/underground patch extractor."""
from __future__ import annotations

from collections import OrderedDict
import logging
import math
import os
from pathlib import Path
import threading
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import cv2
import numpy as np

from src.asset_setup import resolve_data_asset
from src.image_io import read_image

logger = logging.getLogger("GenshinHelper.TileCache")

TileKey = Tuple[int, int, int, int]  # (map_id, zoom, tx, ty)
TileProvider = Callable[[int, int, int, int], Optional[np.ndarray]]

# HoYoLAB underground overlay Y-axis reflection matrix (ported from Genshin-Navigator underground_pyramid.py).
# Maps opposite-sign underground world-Y into surface world-Y while preserving world-X.
UNDERGROUND_Y_REFLECT = np.diag([1.0, -1.0, 1.0]).astype(np.float64)


def default_disk_tile_dir() -> Path:
    """Returns default per-user disk tile cache directory (%LOCALAPPDATA%\\GenshinHelper\\tiles)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "GenshinHelper" / "tiles"


def _to_grayscale_u8(img: np.ndarray) -> Optional[np.ndarray]:
    """Converts BGR, BGRA (zeroing transparent alpha < 16), or grayscale image to contiguous uint8 gray."""
    if img is None or not isinstance(img, np.ndarray) or img.size == 0:
        return None
    if img.ndim == 2:
        gray = img
    elif img.ndim == 3 and img.shape[2] == 4:
        gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
        alpha = img[:, :, 3]
        if int(alpha.min()) < 16:
            gray = gray.copy()
            gray[alpha < 16] = 0
    elif img.ndim == 3 and img.shape[2] >= 3:
        gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
    else:
        return None

    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(gray)


def _parse_overlay_bounds(
    overlay_bounds: Union[Dict[str, Any], Sequence[float]],
) -> Tuple[float, float, float, float, Optional[bool]]:
    """
    Parses HoYoLAB overlay bounds into `(l_x, l_y, r_x, r_y, reflect_y_hint)`.
    Supports `multimap_v4.json` dicts (`l_x`, `l_y`, `r_x`, `r_y`), `min_x/max_x/min_y/max_y`,
    `minLng/maxLng/minLat/maxLat`, or a 4-sequence `(l_x, l_y, r_x, r_y)`.
    """
    reflect_hint: Optional[bool] = None
    if isinstance(overlay_bounds, dict):
        for flag_key in ("reflect_y", "y_flip", "y_inverted"):
            if flag_key in overlay_bounds and overlay_bounds[flag_key] is not None:
                reflect_hint = bool(overlay_bounds[flag_key])
                break
        if all(k in overlay_bounds for k in ("l_x", "l_y", "r_x", "r_y")):
            lx = float(overlay_bounds["l_x"])
            ly = float(overlay_bounds["l_y"])
            rx = float(overlay_bounds["r_x"])
            ry = float(overlay_bounds["r_y"])
            return lx, ly, rx, ry, reflect_hint
        if all(k in overlay_bounds for k in ("min_x", "min_y", "max_x", "max_y")):
            lx = float(overlay_bounds["min_x"])
            ly = float(overlay_bounds["min_y"])
            rx = float(overlay_bounds["max_x"])
            ry = float(overlay_bounds["max_y"])
            return lx, ly, rx, ry, reflect_hint
        if all(k in overlay_bounds for k in ("minLng", "minLat", "maxLng", "maxLat")):
            lx = float(overlay_bounds["minLng"])
            ly = float(overlay_bounds["minLat"])
            rx = float(overlay_bounds["maxLng"])
            ry = float(overlay_bounds["maxLat"])
            return lx, ly, rx, ry, reflect_hint
        raise ValueError(f"Unsupported overlay_bounds dict keys: {sorted(overlay_bounds.keys())}")

    if len(overlay_bounds) != 4:
        raise ValueError(f"overlay_bounds sequence must have 4 elements, got {len(overlay_bounds)}")
    lx, ly, rx, ry = [float(v) for v in overlay_bounds]
    return lx, ly, rx, ry, reflect_hint


def compose_underground_local_to_surface(
    overlay_bounds: Union[Dict[str, Any], Sequence[float]],
    img_w: int,
    img_h: int,
    *,
    world_y: Optional[float] = None,
    half_extent_world: float = 0.0,
    reflect_y: Optional[bool] = None,
) -> Tuple[np.ndarray, bool]:
    """
    Builds the 3x3 homogeneous affine matrix mapping underground overlay pixel `(u, v, 1)`
    (pixel-center convention) into Genshin-Helper surface world coordinates `(wx, wy, 1)`
    where `+X = East` and `+Y = South`.

    Explicitly applies the HoYoLAB underground `diag(1, -1, 1)` Y-reflection check verified
    against `l_y / r_y` ordering and `world_y` containment so underground patches never invert vertically.

    Returns:
        `(local_to_surface_3x3, applied_diag_reflect)`
    """
    if img_w <= 0 or img_h <= 0:
        raise ValueError(f"Invalid overlay dimensions: ({img_w}, {img_h})")

    lx, ly, rx, ry, hint = _parse_overlay_bounds(overlay_bounds)
    if abs(rx - lx) < 1e-9 or abs(ry - ly) < 1e-9:
        raise ValueError(f"Degenerate overlay bounds: ({lx}, {ly}) -> ({rx}, {ry})")

    sx = (rx - lx) / float(img_w)
    sy = (ry - ly) / float(img_h)
    # Pixel center (u + 0.5) maps across [lx, rx] and (v + 0.5) across [ly, ry]
    local_to_world = np.array(
        [
            [sx, 0.0, lx + 0.5 * sx],
            [0.0, sy, ly + 0.5 * sy],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )

    effective_reflect = reflect_y if reflect_y is not None else hint
    if effective_reflect is None and world_y is not None:
        min_y_raw, max_y_raw = (ly, ry) if ly <= ry else (ry, ly)
        margin = max(0.0, float(half_extent_world))
        in_raw = (min_y_raw - margin) <= world_y <= (max_y_raw + margin)
        in_neg = (min_y_raw - margin) <= (-world_y) <= (max_y_raw + margin)
        # If overlay metadata uses opposite-sign world-Y (e.g. north-up +Y vs surface south-down +Y),
        # world_y lies outside [min_y_raw, max_y_raw] while -world_y lies inside -> apply diag(1, -1, 1).
        effective_reflect = bool((not in_raw) and in_neg)

    applied_reflect = bool(effective_reflect)
    if applied_reflect:
        local_to_surface = UNDERGROUND_Y_REFLECT @ local_to_world
    else:
        local_to_surface = local_to_world

    return local_to_surface, applied_reflect


def extract_underground_patch(
    floor_overlay_img: Optional[np.ndarray],
    overlay_bounds: Union[Dict[str, Any], Sequence[float]],
    world_x: float,
    world_y: float,
    half_extent_world: float,
    target_px_per_unit: float = 0.5,
    *,
    reflect_y: Optional[bool] = None,
) -> Tuple[Optional[np.ndarray], Tuple[float, float, float]]:
    """
    Extracts a North-up grayscale patch centered on `(world_x, world_y)` from a HoYoLAB
    underground floor overlay image, explicitly applying the `diag(1, -1, 1)` Y-flip guard
    verified against `l_y / r_y` ordering so underground patches never invert vertically.

    Returns:
        `(patch_gray, (patch_origin_wx, patch_origin_wy, px_per_unit))`
        where pixel `(px, py)` center maps to:
            `wx = patch_origin_wx + (px + 0.5) / px_per_unit`
            `wy = patch_origin_wy + (py + 0.5) / px_per_unit`
    """
    if half_extent_world <= 0.0 or target_px_per_unit <= 0.0:
        return None, (float(world_x), float(world_y), float(target_px_per_unit))

    out_size = max(16, int(round(2.0 * float(half_extent_world) * float(target_px_per_unit))))
    eff_half = (0.5 * out_size) / float(target_px_per_unit)
    origin_wx = float(world_x) - eff_half
    origin_wy = float(world_y) - eff_half
    meta_out = (origin_wx, origin_wy, float(target_px_per_unit))

    gray = _to_grayscale_u8(floor_overlay_img) if floor_overlay_img is not None else None
    if gray is None:
        return None, meta_out

    img_h, img_w = gray.shape[:2]
    try:
        local_to_surface, _ = compose_underground_local_to_surface(
            overlay_bounds,
            img_w,
            img_h,
            world_y=float(world_y),
            half_extent_world=eff_half,
            reflect_y=reflect_y,
        )
    except ValueError:
        return None, meta_out

    # Compute overlay bounding box in surface world coordinates to reject non-overlapping queries
    corners_local = np.array(
        [
            [-0.5, -0.5, 1.0],
            [img_w - 0.5, -0.5, 1.0],
            [img_w - 0.5, img_h - 0.5, 1.0],
            [-0.5, img_h - 0.5, 1.0],
        ],
        dtype=np.float64,
    ).T
    corners_surf = local_to_surface @ corners_local
    ov_min_x = float(np.min(corners_surf[0]))
    ov_max_x = float(np.max(corners_surf[0]))
    ov_min_y = float(np.min(corners_surf[1]))
    ov_max_y = float(np.max(corners_surf[1]))

    q_min_x, q_max_x = origin_wx, origin_wx + 2.0 * eff_half
    q_min_y, q_max_y = origin_wy, origin_wy + 2.0 * eff_half
    if q_max_x <= ov_min_x or q_min_x >= ov_max_x or q_max_y <= ov_min_y or q_min_y >= ov_max_y:
        return None, meta_out

    # Map output patch pixel (px, py) -> surface world -> overlay pixel (u, v)
    inv_s = 1.0 / float(target_px_per_unit)
    patch_to_surface = np.array(
        [
            [inv_s, 0.0, origin_wx + 0.5 * inv_s],
            [0.0, inv_s, origin_wy + 0.5 * inv_s],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    surface_to_local = np.linalg.inv(local_to_surface)
    patch_to_local = surface_to_local @ patch_to_surface

    src_scale = max(abs(patch_to_local[0, 0]), abs(patch_to_local[1, 1]))
    interp = cv2.INTER_AREA if src_scale > 1.25 else cv2.INTER_LINEAR
    patch = cv2.warpAffine(
        gray,
        patch_to_local[:2, :],
        (out_size, out_size),
        flags=interp | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return patch, meta_out


class LruTerrainTileCache:
    """
    Bounded O(1) RAM LRU terrain tile cache.

    Holds at most `max_tiles` (default 16 -> ~1-4 MB RAM regardless of total Teyvat map size)
    in an OrderedDict. Loads tiles lazily from:
      1. Per-user disk cache (`%LOCALAPPDATA%\\GenshinHelper\\tiles\\<map_id>\\<z>\\<tx>_<ty>.png`)
      2. Optional higher-resolution `tile_provider(map_id, z, tx, ty)` callback (`z = -1 / 0`)
      3. On-the-fly slicing from already-loaded `terrain_data` or lazily read `terrain_n2_v4.npz`
         without duplicating whole-world canvases.
    """

    def __init__(
        self,
        max_tiles: int = 16,
        tile_size: int = 256,
        *,
        npz_path: Optional[Union[str, Path]] = None,
        terrain_data: Optional[Dict[int, Tuple[np.ndarray, np.ndarray]]] = None,
        disk_cache_dir: Optional[Union[str, Path]] = None,
        tile_provider: Optional[TileProvider] = None,
    ):
        if int(max_tiles) < 1:
            raise ValueError(f"max_tiles must be >= 1, got {max_tiles}")
        if int(tile_size) < 16:
            raise ValueError(f"tile_size must be >= 16, got {tile_size}")

        self.max_tiles = int(max_tiles)
        self.tile_size = int(tile_size)
        self.disk_cache_dir = Path(disk_cache_dir) if disk_cache_dir is not None else default_disk_tile_dir()
        self.tile_provider = tile_provider

        if npz_path is None:
            resolved_npz = str(resolve_data_asset("terrain_n2_v4.npz"))
            self.npz_path: Optional[Path] = Path(resolved_npz) if os.path.exists(resolved_npz) else None
        else:
            p = Path(npz_path)
            self.npz_path = p if p.exists() else None

        self._lock = threading.RLock()
        self._cache: "OrderedDict[TileKey, np.ndarray]" = OrderedDict()

        # Borrowed read-only reference to caller's already-loaded terrain_data (zero duplication)
        self._external_terrain: Optional[Dict[int, Tuple[np.ndarray, np.ndarray]]] = terrain_data
        # Lazily opened NPZ map canvases (only populated on-demand if external_terrain lacks map_id)
        self._lazy_npz_maps: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
        # Lightweight per-map coordinate metadata: map_id -> (tx_start, ty_start, native_z, orig_x, orig_y)
        self._map_meta: Dict[int, Tuple[int, int, int, float, float]] = {}

        if terrain_data is not None:
            for mid, (_, meta) in terrain_data.items():
                self._map_meta[int(mid)] = self._parse_meta_array(meta)

        self.hits: int = 0
        self.misses: int = 0
        self.evictions: int = 0

    @staticmethod
    def _parse_meta_array(meta: np.ndarray) -> Tuple[int, int, int, float, float]:
        arr = [float(v) for v in meta]
        return int(arr[0]), int(arr[1]), int(arr[2]), float(arr[3]), float(arr[4])

    @property
    def tile_count(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def approx_ram_bytes(self) -> int:
        with self._lock:
            return sum(int(t.nbytes) for t in self._cache.values())

    def clear(self) -> None:
        """Clears cached tiles from RAM."""
        with self._lock:
            self._cache.clear()

    def register_map_meta(
        self,
        map_id: int,
        orig_x: float,
        orig_y: float,
        *,
        native_zoom: int = -2,
        tx_start: int = 0,
        ty_start: int = 0,
    ) -> None:
        """Registers coordinate origin metadata for a map (useful for custom/disk-only tiles)."""
        with self._lock:
            self._map_meta[int(map_id)] = (
                int(tx_start),
                int(ty_start),
                int(native_zoom),
                float(orig_x),
                float(orig_y),
            )

    def _get_npz_source(self, map_id: int) -> Optional[Tuple[np.ndarray, Tuple[int, int, int, float, float]]]:
        """Returns `(canvas, parsed_meta)` for `map_id` lazily without duplicating existing canvases."""
        mid = int(map_id)
        if self._external_terrain is not None and mid in self._external_terrain:
            canvas, raw_meta = self._external_terrain[mid]
            meta = self._map_meta.get(mid)
            if meta is None:
                meta = self._parse_meta_array(raw_meta)
                self._map_meta[mid] = meta
            return canvas, meta

        if mid in self._lazy_npz_maps:
            canvas, raw_meta = self._lazy_npz_maps[mid]
            return canvas, self._map_meta[mid]

        if self.npz_path is None or not self.npz_path.is_file():
            return None

        gray_key = f"map_{mid}_gray"
        meta_key = f"map_{mid}_meta"
        try:
            with np.load(self.npz_path) as zf:
                if gray_key not in zf.files or meta_key not in zf.files:
                    return None
                canvas = zf[gray_key]
                raw_meta = zf[meta_key]
        except Exception as exc:
            logger.warning("Failed to lazily read map %d from %s: %s", mid, self.npz_path, exc)
            return None

        parsed = self._parse_meta_array(raw_meta)
        self._lazy_npz_maps[mid] = (canvas, raw_meta)
        self._map_meta[mid] = parsed
        return canvas, parsed

    def _get_map_meta(self, map_id: int) -> Optional[Tuple[int, int, int, float, float]]:
        mid = int(map_id)
        meta = self._map_meta.get(mid)
        if meta is not None:
            return meta
        src = self._get_npz_source(mid)
        return src[1] if src is not None else None

    def _disk_tile_path(self, map_id: int, zoom: int, tx: int, ty: int) -> Path:
        return self.disk_cache_dir / str(int(map_id)) / str(int(zoom)) / f"{int(tx)}_{int(ty)}.png"

    def _normalize_tile(self, tile: Optional[np.ndarray]) -> Optional[np.ndarray]:
        gray = _to_grayscale_u8(tile) if tile is not None else None
        if gray is None:
            return None
        if gray.shape != (self.tile_size, self.tile_size):
            interp = cv2.INTER_AREA if max(gray.shape[:2]) > self.tile_size else cv2.INTER_LINEAR
            gray = cv2.resize(gray, (self.tile_size, self.tile_size), interpolation=interp)
        return np.ascontiguousarray(gray)

    def _load_tile_uncached(self, map_id: int, zoom: int, tx: int, ty: int) -> Optional[np.ndarray]:
        # 1. Local disk tile cache (Unicode-safe image_io.read_image)
        disk_path = self._disk_tile_path(map_id, zoom, tx, ty)
        if disk_path.is_file():
            img = read_image(disk_path, cv2.IMREAD_GRAYSCALE)
            norm = self._normalize_tile(img)
            if norm is not None:
                return norm

        # 2. Optional higher-res tile provider (e.g., z = -1 / 0 lazy fetcher)
        if self.tile_provider is not None:
            provided = self.tile_provider(int(map_id), int(zoom), int(tx), int(ty))
            norm = self._normalize_tile(provided)
            if norm is not None:
                return norm

        # 3. On-the-fly 256x256 tile slice from terrain_n2_v4.npz (zero whole-world duplication)
        src = self._get_npz_source(map_id)
        if src is None:
            return None
        canvas, (tx_start, ty_start, native_z, _, _) = src
        if int(zoom) != native_z:
            return None

        ts = self.tile_size
        cx0 = (int(tx) - tx_start) * ts
        cy0 = (int(ty) - ty_start) * ts
        ch, cw = canvas.shape[:2]
        if cx0 < 0 or cy0 < 0 or cx0 >= cw or cy0 >= ch:
            return None

        cx1 = min(cw, cx0 + ts)
        cy1 = min(ch, cy0 + ts)
        sub = canvas[cy0:cy1, cx0:cx1]
        if sub.shape == (ts, ts):
            return np.ascontiguousarray(sub)

        padded = np.zeros((ts, ts), dtype=np.uint8)
        padded[: sub.shape[0], : sub.shape[1]] = sub
        return padded

    def _put_lru(self, key: TileKey, tile: np.ndarray) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
            self._cache[key] = tile
            return
        self._cache[key] = tile
        while len(self._cache) > self.max_tiles:
            self._cache.popitem(last=False)
            self.evictions += 1

    def get_tile(self, map_id: int, zoom: int, tx: int, ty: int) -> Optional[np.ndarray]:
        """O(1) LRU tile lookup with lazy disk/provider/NPZ loading."""
        key: TileKey = (int(map_id), int(zoom), int(tx), int(ty))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                self.hits += 1
                return cached
            self.misses += 1

        tile = self._load_tile_uncached(key[0], key[1], key[2], key[3])
        if tile is None:
            return None

        with self._lock:
            self._put_lru(key, tile)
            return tile

    def _choose_zoom(
        self,
        map_id: int,
        world_x: float,
        world_y: float,
        target_px_per_unit: float,
        native_z: int,
        orig_x: float,
        orig_y: float,
    ) -> int:
        """Selects higher-res disk/provider zoom when available at center tile, else falls back to native NPZ zoom."""
        z_ideal = max(-2, min(0, int(round(math.log2(max(1e-4, float(target_px_per_unit)))))))
        if z_ideal == native_z:
            return native_z

        # Probe candidate zoom levels from ideal down toward native_z (only if disk/provider can serve them)
        step = -1 if z_ideal > native_z else 1
        for z_cand in range(z_ideal, native_z, step):
            s_cand = 2.0 ** z_cand
            tx_c = int(math.floor((world_x + orig_x) * s_cand / self.tile_size))
            ty_c = int(math.floor((world_y + orig_y) * s_cand / self.tile_size))
            key: TileKey = (int(map_id), int(z_cand), tx_c, ty_c)
            with self._lock:
                if key in self._cache:
                    return z_cand
            if self._disk_tile_path(map_id, z_cand, tx_c, ty_c).is_file():
                return z_cand
            if self.tile_provider is not None:
                tile = self.get_tile(map_id, z_cand, tx_c, ty_c)
                if tile is not None:
                    return z_cand
        return native_z

    def extract_local_patch(
        self,
        map_id: int,
        world_x: float,
        world_y: float,
        half_extent_world: float,
        target_px_per_unit: float = 0.5,
    ) -> Tuple[Optional[np.ndarray], Tuple[float, float, float]]:
        """
        Extracts a sub-pixel-aligned North-up grayscale patch centered on `(world_x, world_y)`
        by stitching only the overlapping cached tiles from the O(1) LRU tile cache.

        Returns:
            `(patch_gray, (patch_origin_wx, patch_origin_wy, px_per_unit))`
            where pixel `(px, py)` center maps to:
                `wx = patch_origin_wx + (px + 0.5) / px_per_unit`
                `wy = patch_origin_wy + (py + 0.5) / px_per_unit`
        """
        wx = float(world_x)
        wy = float(world_y)
        half_w = float(half_extent_world)
        px_per_unit = float(target_px_per_unit)

        if half_w <= 0.0 or px_per_unit <= 0.0:
            return None, (wx, wy, px_per_unit)

        out_size = max(16, int(round(2.0 * half_w * px_per_unit)))
        eff_half = (0.5 * out_size) / px_per_unit
        origin_wx = wx - eff_half
        origin_wy = wy - eff_half
        meta_out = (origin_wx, origin_wy, px_per_unit)

        map_meta = self._get_map_meta(int(map_id))
        if map_meta is None:
            return None, meta_out

        _, _, native_z, orig_x, orig_y = map_meta
        z_src = self._choose_zoom(int(map_id), wx, wy, px_per_unit, native_z, orig_x, orig_y)
        s_src = 2.0 ** z_src
        ts = self.tile_size

        # Tile coverage for [origin_wx, origin_wx + 2*eff_half] with 1-tile-px interpolation guard
        gx_min = (origin_wx + orig_x) * s_src - 1.0
        gx_max = (origin_wx + 2.0 * eff_half + orig_x) * s_src + 1.0
        gy_min = (origin_wy + orig_y) * s_src - 1.0
        gy_max = (origin_wy + 2.0 * eff_half + orig_y) * s_src + 1.0

        tx_min = int(math.floor(gx_min / ts))
        tx_max = int(math.floor(gx_max / ts))
        ty_min = int(math.floor(gy_min / ts))
        ty_max = int(math.floor(gy_max / ts))

        cols = tx_max - tx_min + 1
        rows = ty_max - ty_min + 1
        if cols <= 0 or rows <= 0 or cols * rows > 64:
            return None, meta_out

        mosaic = np.zeros((rows * ts, cols * ts), dtype=np.uint8)
        loaded_any = False
        for ty in range(ty_min, ty_max + 1):
            ry = (ty - ty_min) * ts
            for tx in range(tx_min, tx_max + 1):
                tile = self.get_tile(int(map_id), z_src, tx, ty)
                if tile is not None:
                    rx = (tx - tx_min) * ts
                    mosaic[ry : ry + ts, rx : rx + ts] = tile
                    loaded_any = True

        if not loaded_any:
            return None, meta_out

        # Exact pixel-center affine transform from output patch (px, py) -> mosaic (mx, my):
        #   wx = origin_wx + (px + 0.5) / px_per_unit
        #   mx + 0.5 = (wx + orig_x) * s_src - tx_min * ts
        # => mx = ((origin_wx + orig_x) * s_src - tx_min * ts + 0.5 * (s_src / px_per_unit - 1.0))
        #         + px * (s_src / px_per_unit)
        scale_ratio = s_src / px_per_unit
        off_x = (origin_wx + orig_x) * s_src - tx_min * ts + 0.5 * (scale_ratio - 1.0)
        off_y = (origin_wy + orig_y) * s_src - ty_min * ts + 0.5 * (scale_ratio - 1.0)

        M_dst_to_src = np.array(
            [
                [scale_ratio, 0.0, off_x],
                [0.0, scale_ratio, off_y],
            ],
            dtype=np.float64,
        )
        interp = cv2.INTER_AREA if scale_ratio > 1.25 else cv2.INTER_LINEAR
        patch = cv2.warpAffine(
            mosaic,
            M_dst_to_src,
            (out_size, out_size),
            flags=interp | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        return patch, meta_out

    def extract_underground_patch(
        self,
        floor_overlay_img: Optional[np.ndarray],
        overlay_bounds: Union[Dict[str, Any], Sequence[float]],
        world_x: float,
        world_y: float,
        half_extent_world: float,
        target_px_per_unit: float = 0.5,
        *,
        reflect_y: Optional[bool] = None,
    ) -> Tuple[Optional[np.ndarray], Tuple[float, float, float]]:
        """Delegates to `extract_underground_patch` with HoYoLAB `diag(1, -1, 1)` Y-flip guard."""
        return extract_underground_patch(
            floor_overlay_img,
            overlay_bounds,
            world_x,
            world_y,
            half_extent_world,
            target_px_per_unit=target_px_per_unit,
            reflect_y=reflect_y,
        )
