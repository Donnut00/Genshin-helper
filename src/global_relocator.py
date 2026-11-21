"""Offline Full-Teyvat RootSIFT FLANN Global Relocator (Map 2 surface) with async warmup."""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
import threading
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from src.asset_setup import get_user_data_cache_dir
from src.paths import get_asset_path
from src.tile_cache import default_disk_tile_dir

logger = logging.getLogger("GenshinHelper.GlobalRelocator")

INDEX_VERSION = 2
SCALE_FACTOR = 400.0
BIN_WORLD = 240.0
NMS_DIST_W = 420.0


def default_global_index_path() -> Path:
    """Returns per-user cache path for the quantized RootSIFT global Teyvat index."""
    return default_disk_tile_dir().parent / "cache" / "teyvat_global_index_v2.npz"


def _load_map2_from_npz() -> Optional[Tuple[np.ndarray, np.ndarray]]:
    candidates = [
        Path(get_asset_path("src", "data", "terrain_n2_v4.npz")),
        get_user_data_cache_dir() / "terrain_n2_v4.npz",
    ]
    for npz_path in candidates:
        if not npz_path.is_file():
            continue
        try:
            with np.load(npz_path) as zf:
                if "map_2_gray" in zf.files and "map_2_meta" in zf.files:
                    return np.array(zf["map_2_gray"], copy=True), np.array(zf["map_2_meta"], copy=True)
        except Exception as exc:
            logger.warning(f"Failed reading Map 2 terrain from {npz_path}: {exc}")
    return None


def _to_rootsift(des: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if des is None or len(des) == 0:
        return None
    f = des.astype(np.float32)
    l1 = np.sum(f, axis=1, keepdims=True) + np.float32(1e-7)
    return np.sqrt(f / l1)


def build_teyvat_global_index(
    map2_gray: np.ndarray,
    map2_meta: np.ndarray,
    cache_path: Optional[Path] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Tiles Map 2 `terrain_n2` into 384x384 raw RootSIFT patches and saves quantized uint8 index."""
    dest = Path(cache_path or default_global_index_path())
    tx_start = int(map2_meta[0])
    ty_start = int(map2_meta[1])
    native_z = int(map2_meta[2])
    orig_x = float(map2_meta[3])
    orig_y = float(map2_meta[4])
    s_src = 2.0 ** native_z

    h, w = map2_gray.shape[:2]
    tile_sz = 384
    stride = 360
    margin = 12

    ys = list(range(0, max(1, h - tile_sz + 1), stride))
    if not ys:
        ys = [0]
    if ys[-1] + tile_sz < h:
        ys.append(max(0, h - tile_sz))

    xs = list(range(0, max(1, w - tile_sz + 1), stride))
    if not xs:
        xs = [0]
    if xs[-1] + tile_sz < w:
        xs.append(max(0, w - tile_sz))

    sift = cv2.SIFT_create(nfeatures=900, contrastThreshold=0.011, edgeThreshold=15)

    all_des: List[np.ndarray] = []
    all_pts: List[np.ndarray] = []

    for iy, y0 in enumerate(ys):
        y1 = min(h, y0 + tile_sz)
        th = y1 - y0
        keep_y0 = 0 if iy == 0 else ((ys[iy - 1] + tile_sz - margin) - y0)
        keep_y1 = th if iy == len(ys) - 1 else (th - margin)
        keep_y0 = max(0, min(th, keep_y0))
        keep_y1 = max(keep_y0, min(th, keep_y1))

        for ix, x0 in enumerate(xs):
            x1 = min(w, x0 + tile_sz)
            tw = x1 - x0
            tile = map2_gray[y0:y1, x0:x1]
            if float(np.std(tile)) < 8.0:
                continue

            kp, des = sift.detectAndCompute(tile, None)
            if des is None or not kp:
                continue

            keep_x0 = 0 if ix == 0 else ((xs[ix - 1] + tile_sz - margin) - x0)
            keep_x1 = tw if ix == len(xs) - 1 else (tw - margin)
            keep_x0 = max(0, min(tw, keep_x0))
            keep_x1 = max(keep_x0, min(tw, keep_x1))

            sel_idx: List[int] = []
            pts_list: List[Tuple[float, float]] = []
            for k_idx, k in enumerate(kp):
                kx, ky = float(k.pt[0]), float(k.pt[1])
                if keep_x0 <= kx < keep_x1 and keep_y0 <= ky < keep_y1:
                    px = float(x0) + kx
                    py = float(y0) + ky
                    wx = (tx_start * 256.0 + (px + 0.5)) / s_src - orig_x
                    wy = (ty_start * 256.0 + (py + 0.5)) / s_src - orig_y
                    sel_idx.append(k_idx)
                    pts_list.append((wx, wy))

            if not sel_idx:
                continue

            rdes = _to_rootsift(des[sel_idx])
            if rdes is None:
                continue
            des_u8 = np.clip(np.round(rdes * SCALE_FACTOR), 0, 255).astype(np.uint8)
            all_des.append(des_u8)
            all_pts.append(np.asarray(pts_list, dtype=np.float32))

    if not all_des:
        raise RuntimeError("No descriptors extracted when building Teyvat global index")

    des_u8_all = np.vstack(all_des)
    pts_all = np.vstack(all_pts)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f".{dest.stem}.tmp.{os.getpid()}.npz")
    np.savez_compressed(
        tmp_path,
        des_u8=des_u8_all,
        pts_world=pts_all,
        scale_factor=np.float32(SCALE_FACTOR),
        index_version=np.int32(INDEX_VERSION),
    )
    os.replace(tmp_path, dest)
    return des_u8_all, pts_all


class TeyvatGlobalRelocator:
    """Offline RootSIFT FLANN global relocator across all of Map 2 Teyvat (~10-12 ms/query)."""

    def __init__(
        self,
        terrain_data: Optional[Dict[int, tuple]] = None,
        cache_path: Optional[Path] = None,
        *,
        lazy: bool = False,
    ):
        self._terrain_data = terrain_data
        self.cache_path = Path(cache_path) if cache_path is not None else default_global_index_path()
        self._lock = threading.RLock()
        self._flann: Optional[cv2.FlannBasedMatcher] = None
        self.pts_world: Optional[np.ndarray] = None
        self.num_descriptors: int = 0
        self._warmup_thread: Optional[threading.Thread] = None
        self._sift_q = cv2.SIFT_create(nfeatures=160, contrastThreshold=0.008, edgeThreshold=15)
        self._annulus_masks: Dict[int, np.ndarray] = {}

        if not lazy:
            self.ensure_ready()

    @property
    def is_ready(self) -> bool:
        return self._flann is not None and self.pts_world is not None

    def warmup_async(self) -> None:
        """Builds/loads the RootSIFT FLANN index on a background daemon thread (zero startup block)."""
        with self._lock:
            if self.is_ready:
                return
            if self._warmup_thread is not None and self._warmup_thread.is_alive():
                return

            def _worker() -> None:
                try:
                    t0 = time.perf_counter()
                    if self.ensure_ready():
                        dt_ms = (time.perf_counter() - t0) * 1000.0
                        logger.info(
                            f"[GlobalRelocator] Ready ({self.num_descriptors} RootSIFT landmarks in {dt_ms:.0f} ms)"
                        )
                except Exception as exc:
                    logger.warning(f"[GlobalRelocator] Background warmup skipped: {exc}")

            t = threading.Thread(target=_worker, name="gh-global-relocator-warmup", daemon=True)
            self._warmup_thread = t
            t.start()

    def ensure_ready(self) -> bool:
        """Loads cached index (or builds from `terrain_n2_v4.npz`) and trains the FLANN KD-Tree."""
        with self._lock:
            if self.is_ready:
                return True

            des_u8: Optional[np.ndarray] = None
            pts_world: Optional[np.ndarray] = None
            scale_f = float(SCALE_FACTOR)

            if self.cache_path.is_file():
                try:
                    with np.load(self.cache_path) as zf:
                        ver = int(zf["index_version"]) if "index_version" in zf.files else -1
                        if ver == INDEX_VERSION and "des_u8" in zf.files and "pts_world" in zf.files:
                            des_u8 = np.array(zf["des_u8"], copy=True)
                            pts_world = np.array(zf["pts_world"], copy=True)
                            if "scale_factor" in zf.files:
                                scale_f = float(zf["scale_factor"])
                except Exception:
                    des_u8 = None
                    pts_world = None

            if des_u8 is None or pts_world is None:
                m2_pair: Optional[Tuple[np.ndarray, np.ndarray]] = None
                if self._terrain_data is not None and 2 in self._terrain_data:
                    m2_raw = self._terrain_data[2]
                    if len(m2_raw) >= 2:
                        m2_pair = (m2_raw[0], m2_raw[1])
                if m2_pair is None:
                    m2_pair = _load_map2_from_npz()
                if m2_pair is None:
                    return False
                try:
                    des_u8, pts_world = build_teyvat_global_index(m2_pair[0], m2_pair[1], self.cache_path)
                    scale_f = float(SCALE_FACTOR)
                except Exception as exc:
                    logger.warning(f"[GlobalRelocator] Failed building index: {exc}")
                    return False

            if len(des_u8) < 2 or len(pts_world) != len(des_u8):
                return False

            pts_f32 = np.ascontiguousarray(pts_world, dtype=np.float32)
            des_f32 = np.ascontiguousarray(des_u8.astype(np.float32) * np.float32(1.0 / scale_f))
            flann = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=48))
            flann.add([des_f32])
            flann.train()

            self.pts_world = pts_f32
            self.num_descriptors = int(pts_f32.shape[0])
            self._flann = flann
            return True

    def _get_annulus_mask(self, sz: int) -> np.ndarray:
        m = self._annulus_masks.get(sz)
        if m is not None:
            return m
        yy, xx = np.mgrid[0:sz, 0:sz]
        c = (sz - 1) * 0.5
        rr = np.hypot(xx - c, yy - c)
        r_max = sz * 0.5
        m = np.where((rr > 0.22 * r_max) & (rr <= 0.85 * r_max), np.uint8(255), np.uint8(0))
        self._annulus_masks[sz] = m
        return m

    def _extract_query_rootsift(self, minimap_img: np.ndarray, sz: int) -> Optional[np.ndarray]:
        mm_gray = minimap_img if minimap_img.ndim == 2 else cv2.cvtColor(minimap_img[:, :, :3], cv2.COLOR_BGR2GRAY)
        s_sz = min(mm_gray.shape[:2])
        use_sz = sz if s_sz >= sz else s_sz
        mm_sq_g = (
            mm_gray
            if (mm_gray.shape[0] == use_sz and mm_gray.shape[1] == use_sz)
            else cv2.resize(mm_gray, (use_sz, use_sz), interpolation=cv2.INTER_AREA)
        )
        mask = self._get_annulus_mask(use_sz).copy()
        if minimap_img.ndim == 3 and minimap_img.shape[2] >= 3:
            mm_sq = (
                minimap_img[:, :, :3]
                if (minimap_img.shape[0] == use_sz and minimap_img.shape[1] == use_sz)
                else cv2.resize(minimap_img[:, :, :3], (use_sz, use_sz), interpolation=cv2.INTER_AREA)
            )
            hsv = cv2.cvtColor(mm_sq, cv2.COLOR_BGR2HSV)
            white_pins = (mm_sq_g >= 238) & (hsv[:, :, 1] <= 38)
            active_px = int(cv2.countNonZero(mask))
            if active_px - int(np.sum(white_pins)) >= 0.4 * active_px:
                mask[white_pins] = 0

        kp, des = self._sift_q.detectAndCompute(mm_sq_g, mask)
        if des is None or len(kp) < 4:
            return None
        return _to_rootsift(des)

    def query_candidates(
        self,
        minimap_img: Optional[np.ndarray],
        *,
        vote_bins: Optional[Dict[Tuple[int, int], List[Tuple[float, float, float]]]] = None,
        top_k: int = 4,
        min_bin_score: float = 3.5,
    ) -> List[Tuple[float, float, float]]:
        """Multi-scale (90, 60, 44) raw RootSIFT FLANN query + spatial 3x3 bin voting + NMS."""
        if not self.is_ready:
            return []
        if minimap_img is None or not isinstance(minimap_img, np.ndarray) or minimap_img.size == 0:
            return []
        if min(minimap_img.shape[:2]) < 32:
            return []

        bins = vote_bins if vote_bins is not None else {}

        with self._lock:
            if self._flann is None or self.pts_world is None:
                return []
            des_parts: List[np.ndarray] = []
            for sz in (90, 60, 44):
                rdes = self._extract_query_rootsift(minimap_img, sz)
                if rdes is not None and len(rdes) > 0:
                    des_parts.append(rdes)
            if not des_parts:
                return []
            des_all = np.ascontiguousarray(np.vstack(des_parts), dtype=np.float32)
            pairs = self._flann.knnMatch(des_all, k=2)
            pts = self.pts_world

        for pair in pairs:
            if len(pair) < 2:
                continue
            m0, m1 = pair[0], pair[1]
            d0 = float(m0.distance)
            d1 = float(m1.distance)
            if d1 <= 1e-6:
                continue
            ratio = d0 / d1
            if ratio >= 0.78:
                continue
            w = 1.0 / (ratio + 0.15)
            p = pts[int(m0.trainIdx)]
            wx = float(p[0])
            wy = float(p[1])
            bkey = (int(math.floor(wx / BIN_WORLD)), int(math.floor(wy / BIN_WORLD)))
            lst = bins.get(bkey)
            if lst is None:
                bins[bkey] = [(wx, wy, w)]
            else:
                lst.append((wx, wy, w))

        if not bins:
            return []

        bin_sums: Dict[Tuple[int, int], float] = {
            k: sum(item[2] for item in v) for k, v in bins.items()
        }
        scored: List[Tuple[float, Tuple[int, int]]] = []
        for bx, by in bin_sums:
            nb_score = 0.0
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb_score += bin_sums.get((bx + dx, by + dy), 0.0)
            if nb_score >= min_bin_score:
                scored.append((nb_score, (bx, by)))

        if not scored:
            return []

        scored.sort(key=lambda item: item[0], reverse=True)
        chosen: List[Tuple[float, float, float]] = []
        for nb_score, (bx, by) in scored:
            sum_w = 0.0
            sum_wx = 0.0
            sum_wy = 0.0
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for wx, wy, w in bins.get((bx + dx, by + dy), ()):
                        sum_w += w
                        sum_wx += wx * w
                        sum_wy += wy * w
            if sum_w <= 0.0:
                continue
            cx = sum_wx / sum_w
            cy = sum_wy / sum_w
            if any(math.hypot(cx - ox, cy - oy) < NMS_DIST_W for ox, oy, _ in chosen):
                continue
            chosen.append((cx, cy, float(nb_score)))
            if len(chosen) >= top_k:
                break
        return chosen
