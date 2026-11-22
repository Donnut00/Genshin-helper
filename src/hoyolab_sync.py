"""
HoYoLAB v4 Official Data Sync Pipeline (in-process runtime & CLI backend).
Fetches authentic multi-map metadata, areas, Category-1 landmark waypoints,
point_group layered maps, alpha cave masks, and compact detail_v2 terrain tiles
from HoYoLAB public API (ys_obc) into a target data directory.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("SyncHoYoLABv4")

BASE_API = "https://sg-public-api.hoyolab.com/common/map_user/ys_obc"
TILE_BASE = "https://act-webstatic.hoyoverse.com/map_manage/map"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "x-rpc-map_version": "4.5",
}

DEFAULT_MAP_IDS = [2, 7, 9, 34, 36, 37, 40]
PRIMARY_LANDMARK_LABELS = {2, 3, 154}
MAP_PRIORITIES = {2: 1, 7: 4, 9: 3, 34: 5, 36: 6, 37: 7, 40: 2}


def _fetch_json(url: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            logger.warning("HTTP %s fetching %s", e.code, url)
        return None
    except Exception as e:
        logger.warning("Failed fetching %s: %s", url, e)
        return None


def _fetch_bytes(url: str, timeout: int = 12) -> Optional[bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def discover_active_maps() -> Dict[int, Dict[str, Any]]:
    """Probes HoYoLAB map IDs 1..54 and collects all active maps with detail_v2."""
    maps_info: Dict[int, Dict[str, Any]] = {}

    def _probe(mid: int):
        data = _fetch_json(f"{BASE_API}/v1/map/info?map_id={mid}&app_sn=ys_obc&lang=vi-vn", timeout=10)
        if not data or data.get("retcode", -1) != 0:
            return None
        info = data.get("data", {}).get("info")
        if not info or not info.get("detail_v2"):
            return None
        d2 = json.loads(info["detail_v2"]) if isinstance(info["detail_v2"], str) else info["detail_v2"]
        if not isinstance(d2, dict) or not d2.get("map_version"):
            return None
        return mid, info, d2

    probe_ids = list(dict.fromkeys(DEFAULT_MAP_IDS + list(range(1, 55))))
    with ThreadPoolExecutor(max_workers=12) as pool:
        for res in pool.map(_probe, probe_ids):
            if res is not None:
                mid, info, d2 = res
                maps_info[mid] = {
                    "id": mid,
                    "name": info.get("name", f"Map {mid}"),
                    "priority": MAP_PRIORITIES.get(mid, 50 + mid),
                    "detail_v2": d2,
                }
    return dict(sorted(maps_info.items()))


def load_legacy_floor_shorts(legacy_path: str) -> Dict[Tuple[int, int], str]:
    """Loads floor_short overrides from legacy src/underground_db.json keyed by (group_id, floor_id)."""
    shorts: Dict[Tuple[int, int], str] = {}
    if not legacy_path or not os.path.exists(legacy_path):
        return shorts
    try:
        with open(legacy_path, "r", encoding="utf-8") as f:
            ug = json.load(f)
        for g in ug:
            gid = int(g.get("id", 0))
            for fl in g.get("floors", []):
                fid = int(fl.get("id", 0))
                fs = (fl.get("floor_short") or "").strip().upper()
                if gid and fid and fs:
                    shorts[(gid, fid)] = fs
    except Exception as e:
        logger.warning("Could not read legacy underground_db.json: %s", e)
    return shorts


def fetch_map_bundle(mid: int, meta: Dict[str, Any], legacy_shorts: Dict[Tuple[int, int], str]) -> Dict[str, Any]:
    """Fetches areas, Category-1 labels, waypoints, and point_groups for one map."""
    areas_resp = _fetch_json(f"{BASE_API}/v1/map/get_area_pageLabel?map_id={mid}&app_sn=ys_obc&lang=vi-vn")
    areas_raw = (areas_resp or {}).get("data", {}).get("list", [])
    areas = [{"id": int(a["id"]), "name": a.get("name", "")} for a in areas_raw if "id" in a]

    tree_resp = _fetch_json(f"{BASE_API}/v1/map/label/tree?map_id={mid}&app_sn=ys_obc&lang=vi-vn")
    tree = (tree_resp or {}).get("data", {}).get("tree", [])
    cat1 = next((c for c in tree if c.get("id") == 1), tree[0] if tree else None)
    cat1_labels: List[Dict[str, Any]] = []
    if cat1:
        for ch in cat1.get("children", []):
            lid = int(ch["id"])
            cat1_labels.append({
                "id": lid,
                "name": ch.get("name", ""),
                "is_primary": lid in PRIMARY_LANDMARK_LABELS,
            })
    cat1_id_set = {lbl["id"] for lbl in cat1_labels} or set(PRIMARY_LANDMARK_LABELS)

    pts_resp = _fetch_json(f"{BASE_API}/v2/map/point/list?map_id={mid}&app_sn=ys_obc&lang=vi-vn", timeout=20)
    raw_pts = (pts_resp or {}).get("data", {}).get("point_list", [])
    waypoints = []
    for p in raw_pts:
        lid = int(p.get("label_id", 0))
        if lid not in cat1_id_set:
            continue
        pg = p.get("point_group")
        pg_clean = None
        if isinstance(pg, dict) and pg.get("group_id") is not None:
            pg_clean = {
                "group_id": int(pg["group_id"]),
                "floor_id": int(pg["floor_id"]) if pg.get("floor_id") is not None else None,
            }
        waypoints.append({
            "id": int(p["id"]),
            "label_id": lid,
            "is_primary": lid in PRIMARY_LANDMARK_LABELS,
            "x_pos": float(p["x_pos"]),
            "y_pos": float(p["y_pos"]),
            "area_id": int(p.get("area_id") or 0),
            "map_id": mid,
            "z_level": int(p.get("z_level") or 0),
            "icon_sign": int(p.get("icon_sign") or 0),
            "point_group": pg_clean,
        })

    pg_resp = _fetch_json(f"{BASE_API}/v2/map/point_group?map_id={mid}&app_sn=ys_obc&lang=vi-vn", timeout=15)
    raw_pgs = (pg_resp or {}).get("data", {}).get("list", [])
    point_groups = []
    for g in raw_pgs:
        gid = int(g["id"])
        floors_out = []
        for f_idx, fl in enumerate(g.get("floors", [])):
            fid = int(fl["id"])
            fs = (fl.get("floor_name_short") or "").strip().upper()
            if not fs:
                fs = legacy_shorts.get((gid, fid), "")
            ov = fl.get("overlay")
            ov_clean = None
            if isinstance(ov, dict) and ov.get("url"):
                lx, rx = float(ov["l_x"]), float(ov["r_x"])
                ly, ry = float(ov["l_y"]), float(ov["r_y"])
                ov_clean = {
                    "url": ov["url"],
                    "l_x": lx,
                    "l_y": ly,
                    "r_x": rx,
                    "r_y": ry,
                    "minLng": min(lx, rx),
                    "maxLng": max(lx, rx),
                    "minLat": min(ly, ry),
                    "maxLat": max(ly, ry),
                    "width": float(ov.get("width", abs(rx - lx))),
                    "height": float(ov.get("height", abs(ry - ly))),
                }
            floors_out.append({
                "id": fid,
                "floor_name": fl.get("floor_name") or "",
                "floor_name_short": fs,
                "overlay": ov_clean,
                "index": f_idx,
            })
        point_groups.append({
            "id": gid,
            "map_id": mid,
            "name": g.get("name") or "",
            "g_floor_name": g.get("g_floor_name") or "G",
            "default_floor_index": int(g.get("default_floor_index") or 0),
            "floors": floors_out,
        })

    return {
        "id": mid,
        "name": meta["name"],
        "priority": meta["priority"],
        "detail_v2": meta["detail_v2"],
        "areas": areas,
        "landmark_labels": cat1_labels,
        "waypoints": waypoints,
        "point_groups": point_groups,
    }


def download_cave_masks(maps_data: Dict[str, Dict[str, Any]], out_npz: str) -> int:
    """Downloads all underground/sky floor overlay PNGs and stores resized alpha masks."""
    tasks: List[Tuple[str, str]] = []
    for mid_str, mdata in maps_data.items():
        for g in mdata.get("point_groups", []):
            gid = g["id"]
            for fl in g.get("floors", []):
                ov = fl.get("overlay")
                if ov and ov.get("url"):
                    key = f"{mid_str}_{gid}_{fl['id']}"
                    tasks.append((key, ov["url"]))

    masks: Dict[str, np.ndarray] = {}

    def _fetch_mask(item: Tuple[str, str]) -> Optional[Tuple[str, np.ndarray]]:
        key, url = item
        raw = _fetch_bytes(url, timeout=12)
        if not raw:
            return None
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        if img.ndim == 3 and img.shape[2] >= 4:
            alpha = (img[:, :, 3] > 30).astype(np.uint8)
        elif img.ndim == 3:
            alpha = (cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) > 10).astype(np.uint8)
        else:
            alpha = (img > 10).astype(np.uint8)

        mh, mw = alpha.shape[:2]
        max_dim = max(mh, mw)
        if max_dim > 256:
            scale = 256.0 / float(max_dim)
            new_w = max(8, int(round(mw * scale)))
            new_h = max(8, int(round(mh * scale)))
            alpha = cv2.resize(alpha, (new_w, new_h), interpolation=cv2.INTER_NEAREST)
        return key, alpha

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(_fetch_mask, t) for t in tasks]
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None:
                k, a = res
                masks[k] = a

    np.savez_compressed(out_npz, **masks)
    return len(masks)


def download_terrain_tiles(maps_data: Dict[str, Dict[str, Any]], out_npz: str) -> Dict[int, Tuple[int, int]]:
    """Downloads detail_v2 terrain tiles (z=-2 for Map 2, z=-1 for submaps) and stitches grayscale canvases."""
    payload: Dict[str, np.ndarray] = {}
    shapes: Dict[int, Tuple[int, int]] = {}

    for mid_str, mdata in maps_data.items():
        mid = int(mid_str)
        d2 = mdata["detail_v2"]
        mv = d2["map_version"]
        ts = d2["total_size"]
        pad = d2["padding"]
        orig = d2["origin"]
        z = -2 if mid == 2 else -1
        s = 2 ** z
        z_str = f"N{abs(z)}" if z < 0 else f"P{z}"

        px0, py0 = float(pad[0]), float(pad[1])
        px1 = max(px0 + 256.0, float(ts[0]) - px0)
        py1 = max(py0 + 256.0, float(ts[1]) - py0)

        max_tx = max(0, int((float(ts[0]) * s - 1.0) // 256.0))
        max_ty = max(0, int((float(ts[1]) * s - 1.0) // 256.0))

        tx_start = max(0, int((px0 * s) // 256.0))
        ty_start = max(0, int((py0 * s) // 256.0))
        tx_end = min(max_tx, int((max(1.0, px1 * s) - 1.0) // 256.0))
        ty_end = min(max_ty, int((max(1.0, py1 * s) - 1.0) // 256.0))

        cols = tx_end - tx_start + 1
        rows = ty_end - ty_start + 1
        canvas = np.zeros((rows * 256, cols * 256), dtype=np.uint8)

        tile_jobs = [
            (tx, ty, f"{TILE_BASE}/{mid}/{mv}/{tx}_{ty}_{z_str}.webp")
            for ty in range(ty_start, ty_end + 1)
            for tx in range(tx_start, tx_end + 1)
        ]
        logger.info("Downloading Map %d terrain (%d tiles, z=%d, %dx%d)...", mid, len(tile_jobs), z, cols * 256, rows * 256)

        def _fetch_tile(job: Tuple[int, int, str]) -> Optional[Tuple[int, int, np.ndarray]]:
            tx, ty, url = job
            raw = _fetch_bytes(url, timeout=10)
            if not raw:
                return None
            im = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if im is None:
                return None
            if im.shape[:2] != (256, 256):
                im = cv2.resize(im, (256, 256), interpolation=cv2.INTER_AREA)
            return tx, ty, im

        ok_tiles = 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            for res in pool.map(_fetch_tile, tile_jobs):
                if res is not None:
                    tx, ty, im = res
                    ox_px = (tx - tx_start) * 256
                    oy_px = (ty - ty_start) * 256
                    canvas[oy_px:oy_px + 256, ox_px:ox_px + 256] = im
                    ok_tiles += 1

        payload[f"map_{mid}_gray"] = canvas
        payload[f"map_{mid}_meta"] = np.array([tx_start, ty_start, z, int(orig[0]), int(orig[1])], dtype=np.int64)
        shapes[mid] = (canvas.shape[1], canvas.shape[0])
        logger.info("  -> Map %d stitched %d/%d tiles (%dx%d)", mid, ok_tiles, len(tile_jobs), canvas.shape[1], canvas.shape[0])

    np.savez_compressed(out_npz, **payload)
    return shapes


def sync_to_directory(
    data_dir: str | Path,
    *,
    legacy_ug_path: Optional[str | Path] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Path:
    """
    Runs the full HoYoLAB v4 dataset sync directly into `data_dir` (in-process, PyInstaller-safe).
    Produces `multimap_v4.json`, `cave_masks_v4.npz`, and `terrain_n2_v4.npz`.
    """
    out_dir = Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    legacy_shorts = load_legacy_floor_shorts(str(legacy_ug_path)) if legacy_ug_path else {}
    if legacy_shorts:
        logger.info("Loaded %d legacy floor_short entries", len(legacy_shorts))

    if progress_cb:
        progress_cb("Discovering active HoYoLAB maps...")
    logger.info("Discovering active HoYoLAB maps...")
    active_maps = discover_active_maps()
    if not active_maps:
        raise RuntimeError("Failed to reach HoYoLAB map API (no active maps discovered).")
    logger.info("Active maps discovered: %s", list(active_maps.keys()))

    maps_bundle: Dict[str, Dict[str, Any]] = {}
    total_wps = 0
    total_pgs = 0
    for mid, meta in active_maps.items():
        if progress_cb:
            progress_cb(f"Syncing map metadata {mid} ({meta.get('name', mid)})...")
        bundle = fetch_map_bundle(mid, meta, legacy_shorts)
        maps_bundle[str(mid)] = bundle
        total_wps += len(bundle["waypoints"])
        total_pgs += len(bundle["point_groups"])
        logger.info(
            "  Map %d (%s): %d waypoints, %d point_groups, %d areas",
            mid, bundle["name"], len(bundle["waypoints"]), len(bundle["point_groups"]), len(bundle["areas"])
        )

    json_path = out_dir / "multimap_v4.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "version": "4.0.0",
                "maps": maps_bundle,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info("Saved %s (%d maps, %d waypoints, %d point_groups)", json_path, len(maps_bundle), total_wps, total_pgs)

    if progress_cb:
        progress_cb("Downloading underground cave masks...")
    masks_path = out_dir / "cave_masks_v4.npz"
    mask_count = download_cave_masks(maps_bundle, str(masks_path))
    logger.info("Saved %d cave floor alpha masks to %s", mask_count, masks_path)

    if progress_cb:
        progress_cb("Downloading terrain tiles...")
    terrain_path = out_dir / "terrain_n2_v4.npz"
    shapes = download_terrain_tiles(maps_bundle, str(terrain_path))
    logger.info("Saved terrain tiles to %s: %s", terrain_path, shapes)
    return out_dir
