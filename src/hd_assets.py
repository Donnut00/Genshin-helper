"""Non-blocking HoYoLAB HD terrain tile provider (z=-1/z=0) and underground overlay loader."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from src.asset_setup import get_user_data_cache_dir
from src.image_io import read_image, write_image
from src.paths import get_asset_path
from src.tile_cache import (
    LruTerrainTileCache,
    default_disk_tile_dir,
    extract_underground_patch,
)

logger = logging.getLogger("GenshinHelper.HdAssets")

TILE_BASE = "https://act-webstatic.hoyoverse.com/map_manage/map"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_NET_BACKOFF_SECONDS = 60.0
_MAX_TRANSIENT_ERRORS = 3


def default_hd_tile_dir() -> Path:
    """Returns per-user disk cache directory for HD map tiles."""
    return default_disk_tile_dir()


def default_ug_overlay_dir() -> Path:
    """Returns per-user disk cache directory for HoYoLAB underground floor PNG overlays."""
    return default_hd_tile_dir().parent / "ug_overlays"


def default_paimon_cache_path() -> Path:
    """Returns per-user runtime Paimon template cache path (outside git & bundle)."""
    return default_hd_tile_dir().parent / "cache" / "paimon_tpl.npz"


def resolve_multimap_json_path(json_path: Optional[Path] = None) -> Path:
    """Resolves `multimap_v4.json` checking explicit path, bundled data, then user cache."""
    if json_path is not None:
        return Path(json_path)
    primary = Path(get_asset_path("src", "data", "multimap_v4.json"))
    if primary.is_file():
        return primary
    cached = get_user_data_cache_dir() / "multimap_v4.json"
    if cached.is_file():
        return cached
    return primary


def load_multimap_db(json_path: Optional[Path] = None) -> Dict[int, Dict[str, Any]]:
    """Loads full `multimap_v4.json` including `detail_v2` and `point_groups` metadata."""
    p = resolve_multimap_json_path(json_path)
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning(f"Failed to load multimap_v4.json from {p}: {exc}")
        return {}

    maps_raw = raw.get("maps", {}) if isinstance(raw, dict) else {}
    out: Dict[int, Dict[str, Any]] = {}
    if isinstance(maps_raw, dict):
        for k, v in maps_raw.items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
    elif isinstance(maps_raw, list):
        for v in maps_raw:
            if isinstance(v, dict) and "id" in v:
                try:
                    out[int(v["id"])] = v
                except (TypeError, ValueError):
                    continue
    return out


class HoyolabHdTileProvider:
    """Lazy non-blocking HoYoLAB terrain tile provider (`z = -1 / 0`) with disk cache & circuit breaker."""

    def __init__(
        self,
        disk_dir: Optional[Path] = None,
        maps_db: Optional[Dict[int, Dict[str, Any]]] = None,
        timeout: float = 5.0,
        *,
        async_fetch: bool = True,
        offline: bool = False,
        max_workers: int = 4,
    ):
        self.disk_dir = Path(disk_dir) if disk_dir is not None else default_hd_tile_dir()
        self.disk_dir.mkdir(parents=True, exist_ok=True)
        self.maps_db = maps_db if maps_db is not None else load_multimap_db()
        self.timeout = float(timeout)
        self.async_fetch = bool(async_fetch)
        self.offline = bool(offline)
        self._max_workers = max(1, int(max_workers))

        self._lock = threading.RLock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._closed = False
        self._missing: set[Tuple[int, int, int, int]] = set()
        self._inflight: set[Tuple[int, int, int, int]] = set()
        self._consecutive_errors = 0
        self._net_backoff_until = 0.0

        self.downloads = 0
        self.disk_hits = 0

    def _tile_paths(self, map_id: int, zoom: int, tx: int, ty: int) -> Tuple[Path, Path]:
        sub = self.disk_dir / str(int(map_id)) / str(int(zoom))
        return sub / f"{int(tx)}_{int(ty)}.png", sub / f"{int(tx)}_{int(ty)}.404"

    def _record_transient_error(self) -> None:
        with self._lock:
            self._consecutive_errors += 1
            if self._consecutive_errors >= _MAX_TRANSIENT_ERRORS:
                self._net_backoff_until = time.monotonic() + _NET_BACKOFF_SECONDS

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_errors = 0
            self._net_backoff_until = 0.0

    def _fetch_and_cache(self, mid: int, z: int, ix: int, iy: int) -> Optional[np.ndarray]:
        key = (mid, z, ix, iy)
        png_path, miss_path = self._tile_paths(mid, z, ix, iy)
        try:
            mdata = self.maps_db.get(mid)
            if not mdata:
                return None
            d2 = mdata.get("detail_v2") or {}
            mv = d2.get("map_version")
            ts = d2.get("total_size")
            if not mv or not ts:
                return None

            s = 2.0 ** z
            max_tx = max(0, int((float(ts[0]) * s - 1.0) // 256.0))
            max_ty = max(0, int((float(ts[1]) * s - 1.0) // 256.0))
            if ix > max_tx or iy > max_ty:
                return None

            z_str = f"N{abs(z)}" if z < 0 else f"P{z}"
            url = f"{TILE_BASE}/{mid}/{mv}/{ix}_{iy}_{z_str}.webp"
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    png_path.parent.mkdir(parents=True, exist_ok=True)
                    miss_path.write_bytes(b"")
                    with self._lock:
                        self._missing.add(key)
                else:
                    self._record_transient_error()
                return None
            except Exception:
                self._record_transient_error()
                return None

            im = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if im is None:
                self._record_transient_error()
                return None
            if im.shape[:2] != (256, 256):
                im = cv2.resize(im, (256, 256), interpolation=cv2.INTER_AREA)

            write_image(png_path, im)
            self._record_success()
            with self._lock:
                self.downloads += 1
            return im
        finally:
            with self._lock:
                self._inflight.discard(key)

    def __call__(self, map_id: int, zoom: int, tx: int, ty: int) -> Optional[np.ndarray]:
        mid, z, ix, iy = int(map_id), int(zoom), int(tx), int(ty)
        if z not in (-2, -1, 0) or ix < 0 or iy < 0:
            return None
        key = (mid, z, ix, iy)
        with self._lock:
            if key in self._missing:
                return None

        png_path, miss_path = self._tile_paths(mid, z, ix, iy)
        if png_path.is_file():
            img = read_image(png_path, cv2.IMREAD_GRAYSCALE)
            if img is not None:
                with self._lock:
                    self.disk_hits += 1
                return img
        if miss_path.is_file():
            with self._lock:
                self._missing.add(key)
            return None

        if self.offline:
            return None
        with self._lock:
            if self._closed or time.monotonic() < self._net_backoff_until:
                return None
            if not self.async_fetch:
                if key in self._inflight:
                    return None
                self._inflight.add(key)
            else:
                if key in self._inflight:
                    return None
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self._max_workers,
                        thread_name_prefix="gh-hd-tile",
                    )
                self._inflight.add(key)
                self._executor.submit(self._fetch_and_cache, mid, z, ix, iy)
                return None

        return self._fetch_and_cache(mid, z, ix, iy)

    def prefetch_region(
        self,
        tile_cache: LruTerrainTileCache,
        map_id: int,
        wx_min: float,
        wx_max: float,
        wy_min: float,
        wy_max: float,
        zoom: int = -1,
        max_workers: int = 8,
    ) -> int:
        """Prefetches a bounded world rectangle of HD tiles into local disk cache."""
        del tile_cache
        mdata = self.maps_db.get(int(map_id))
        if not mdata:
            return 0
        orig = (mdata.get("detail_v2") or {}).get("origin") or [0.0, 0.0]
        ox, oy = float(orig[0]), float(orig[1])
        s = 2.0 ** int(zoom)
        tx0 = max(0, int(math.floor((wx_min + ox) * s / 256.0)))
        tx1 = max(tx0, int(math.floor((wx_max + ox) * s / 256.0)))
        ty0 = max(0, int(math.floor((wy_min + oy) * s / 256.0)))
        ty1 = max(ty0, int(math.floor((wy_max + oy) * s / 256.0)))
        jobs = [(int(map_id), int(zoom), tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
        if len(jobs) > 400:
            raise ValueError(f"Refusing huge prefetch of {len(jobs)} tiles")
        loaded = 0
        was_async = self.async_fetch
        self.async_fetch = False
        try:
            with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
                for res in pool.map(lambda j: self(*j), jobs):
                    if res is not None:
                        loaded += 1
        finally:
            self.async_fetch = was_async
        return loaded

    def close(self) -> None:
        with self._lock:
            self._closed = True
            ex = self._executor
            self._executor = None
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)


class UndergroundOverlayProvider:
    """Caches full-res HoYoLAB underground floor PNGs from `multimap_v4.json`."""

    def __init__(
        self,
        disk_dir: Optional[Path] = None,
        maps_db: Optional[Dict[int, Dict[str, Any]]] = None,
        timeout: float = 8.0,
        *,
        async_fetch: bool = True,
        offline: bool = False,
        max_workers: int = 2,
    ):
        self.disk_dir = Path(disk_dir) if disk_dir is not None else default_ug_overlay_dir()
        self.disk_dir.mkdir(parents=True, exist_ok=True)
        self.maps_db = maps_db if maps_db is not None else load_multimap_db()
        self.timeout = float(timeout)
        self.async_fetch = bool(async_fetch)
        self.offline = bool(offline)
        self._max_workers = max(1, int(max_workers))

        self._lock = threading.RLock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._closed = False
        self._inflight: set[Tuple[int, int, int]] = set()
        self._consecutive_errors = 0
        self._net_backoff_until = 0.0

        self._mem_cache: Dict[Tuple[int, int, int], Tuple[np.ndarray, Dict[str, Any]]] = {}
        self._floors_by_map: Dict[int, List[Dict[str, Any]]] = {}
        self._by_floor_id: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self._by_group_floor: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

        for mid, mdata in self.maps_db.items():
            lst: List[Dict[str, Any]] = []
            for grp in mdata.get("point_groups") or []:
                gid = int(grp.get("id") or 0)
                for fl in grp.get("floors") or []:
                    fid = int(fl.get("id") or 0)
                    ov = fl.get("overlay") or {}
                    url = ov.get("url")
                    if not url or not all(k in ov for k in ("l_x", "l_y", "r_x", "r_y")):
                        continue
                    rec = {
                        "map_id": mid,
                        "group_id": gid,
                        "floor_id": fid,
                        "floor_name": fl.get("floor_name_short") or fl.get("floor_name") or f"F{fid}",
                        "full_name": fl.get("floor_name") or "",
                        "overlay": ov,
                        "url": url,
                        "lx": float(min(ov["l_x"], ov["r_x"])),
                        "rx": float(max(ov["l_x"], ov["r_x"])),
                        "ly": float(min(ov["l_y"], ov["r_y"])),
                        "ry": float(max(ov["l_y"], ov["r_y"])),
                    }
                    lst.append(rec)
                    self._by_floor_id[(mid, fid)] = rec
                    self._by_group_floor[(mid, gid, fid)] = rec
            self._floors_by_map[mid] = lst

    def find_overlapping_floors(self, map_id: int, wx: float, wy: float, pad: float = 40.0) -> List[Dict[str, Any]]:
        """Returns all underground floor metadata entries whose world bounding box covers `(wx, wy)`."""
        out: List[Dict[str, Any]] = []
        for rec in self._floors_by_map.get(int(map_id), ()):
            if (rec["lx"] - pad) <= wx <= (rec["rx"] + pad) and (rec["ly"] - pad) <= wy <= (rec["ry"] + pad):
                out.append(rec)
        return out

    def _record_transient_error(self) -> None:
        with self._lock:
            self._consecutive_errors += 1
            if self._consecutive_errors >= _MAX_TRANSIENT_ERRORS:
                self._net_backoff_until = time.monotonic() + _NET_BACKOFF_SECONDS

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_errors = 0
            self._net_backoff_until = 0.0

    def _fetch_and_cache_overlay(
        self,
        ckey: Tuple[int, int, int],
        local_png: Path,
        rec: Dict[str, Any],
    ) -> Optional[Tuple[np.ndarray, Dict[str, Any]]]:
        try:
            try:
                req = urllib.request.Request(rec["url"], headers=HEADERS)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                im = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            except Exception:
                self._record_transient_error()
                return None

            if im is None:
                self._record_transient_error()
                return None

            local_png.parent.mkdir(parents=True, exist_ok=True)
            tmp = local_png.with_suffix(".png.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, local_png)
            self._record_success()
            val = (im, rec)
            with self._lock:
                self._mem_cache[ckey] = val
            return val
        finally:
            with self._lock:
                self._inflight.discard(ckey)

    def get_overlay(
        self,
        map_id: int,
        floor_id: int,
        group_id: Optional[int] = None,
    ) -> Optional[Tuple[np.ndarray, Dict[str, Any]]]:
        """Returns `(overlay_bgra, floor_record)` from RAM/disk cache, or schedules async fetch."""
        mid = int(map_id)
        fid = int(floor_id)
        rec = (
            self._by_group_floor.get((mid, int(group_id), fid))
            if group_id is not None
            else self._by_floor_id.get((mid, fid))
        )
        if rec is None:
            rec = self._by_floor_id.get((mid, fid))
        if rec is None:
            return None

        gid = rec["group_id"]
        ckey = (mid, gid, fid)
        with self._lock:
            cached = self._mem_cache.get(ckey)
            if cached is not None:
                return cached

        local_png = self.disk_dir / f"{mid}_g{gid}_f{fid}.png"
        if local_png.is_file():
            im = read_image(local_png, cv2.IMREAD_UNCHANGED)
            if im is not None:
                val = (im, rec)
                with self._lock:
                    self._mem_cache[ckey] = val
                return val

        if self.offline:
            return None
        with self._lock:
            if self._closed or time.monotonic() < self._net_backoff_until:
                return None
            if ckey in self._inflight:
                return None
            if self.async_fetch:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self._max_workers,
                        thread_name_prefix="gh-ug-overlay",
                    )
                self._inflight.add(ckey)
                self._executor.submit(self._fetch_and_cache_overlay, ckey, local_png, rec)
                return None
            self._inflight.add(ckey)

        return self._fetch_and_cache_overlay(ckey, local_png, rec)

    def extract_patch(
        self,
        map_id: int,
        floor_id: int,
        wx: float,
        wy: float,
        half_extent_world: float,
        target_px_per_unit: float = 0.5,
        *,
        group_id: Optional[int] = None,
    ) -> Tuple[Optional[np.ndarray], Tuple[float, float, float]]:
        """Extracts a world-aligned underground patch using `extract_underground_patch`."""
        got = self.get_overlay(map_id, floor_id, group_id=group_id)
        if got is None:
            return None, (float(wx), float(wy), float(target_px_per_unit))
        im, rec = got
        return extract_underground_patch(
            im,
            rec["overlay"],
            float(wx),
            float(wy),
            float(half_extent_world),
            target_px_per_unit=float(target_px_per_unit),
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            ex = self._executor
            self._executor = None
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)
