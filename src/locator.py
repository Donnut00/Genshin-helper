import os
import json
import math
import logging
import re
import unicodedata
from typing import Optional, Tuple, Dict, Any, List, Mapping
import cv2
import numpy as np
from src.asset_setup import resolve_data_asset
from src.win_ocr import WindowsOCRService
from src.zoom_slider import ZoomSliderDetector

logger = logging.getLogger("GenshinHelper.Locator")


class MapLocator:
    """
    Ultra-fast Computer Vision Landmark & Terrain Locator for Genshin Impact fullscreen map (M).
    Uses spatial neighborhood graph indexing, ZoomSlider linear scale law, detail_v2 terrain alignment,
    4-template elevator detection, and cave alpha-mask contrast matching.
    """

    PRIMARY_LABELS = {2, 3, 154}
    # Screen-space icon-anchor offset at 1440p baseline (added to detected blue-core (cx, cy),
    # scaled by scale_ui = frame_h / 1440.0 before LSQ). Calibrated via same-waypoint cross-zoom
    # regression and within-frame mixed-label comparisons across 50+ real fixtures.
    ICON_ANCHOR_OFFSET_PX_1440: Dict[int, Tuple[float, float]] = {
        2: (0.0, 3.5),    # Statue of the Seven
        3: (0.0, 5.0),    # Teleport Waypoint teardrop (sprite center ~5px below blue core)
        154: (2.5, 0.0),  # Domain diamond (blue core slightly left of geometric center)
    }
    DEFAULT_ICON_ANCHOR_OFFSET_PX_1440: Tuple[float, float] = (0.0, 5.0)
    # World-space Y offset between HoYoLAB point/list coordinates and detail_v2 terrain/Leaflet tiles.
    WORLD_ANCHOR_Y_OFFSET: float = -8.5
    WORLD_ANCHOR_MAP_IDS = frozenset({2, 9, 34, 36})
    # Strict cyan player arrow contour centroid lags ~2.78 px @1440p behind its true rotation pivot.
    ARROW_PIVOT_OFFSET_PX_1440: float = 2.78
    DEFAULT_AREA_NAMES: Dict[int, str] = {
        1: "Mondstadt",
        2: "Liyue",
        3: "Inazuma",
        4: "Sumeru",
        8: "Fontaine",
        11: "Natlan",
        13: "Nod-Krai",
        16: "Snezhnaya",
    }

    def __init__(self, db_path: Optional[str] = None):
        base_dir = os.path.dirname(os.path.dirname(__file__))
        if db_path is None:
            candidates = [
                str(resolve_data_asset("multimap_v4.json")),
                os.path.join(base_dir, "src", "data", "multimap_v4.json"),
                os.path.join(base_dir, "src", "waypoints_db.json"),
                os.path.join(base_dir, "scratch", "waypoints.json"),
            ]
            db_path = next((p for p in candidates if os.path.exists(p)), candidates[0])

        self.maps: Dict[int, Dict[str, Any]] = {}
        self.waypoints: List[Dict[str, Any]] = []
        self.neighbors: Dict[int, List] = {}
        self.underground_groups: List[Dict[str, Any]] = []
        self.map_point_groups: Dict[int, List[Dict[str, Any]]] = {}
        self._floor_label_index: Dict[Tuple[int, str], str] = {}
        self.area_names_map: Dict[int, str] = dict(self.DEFAULT_AREA_NAMES)
        self._area_bboxes: Dict[Tuple[int, Optional[int]], Tuple[float, float, float, float]] = {}
        self._edge_indices: Dict[int, Dict[str, Any]] = {}
        self.cave_masks: Dict[str, np.ndarray] = {}
        self.terrain_data: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
        self._terrain_coarse_cache: Dict[int, Tuple[np.ndarray, np.ndarray, float]] = {}
        self.ui_anchors: Dict[str, np.ndarray] = {}
        self._elev_tpl_cache: Dict[int, Dict[str, np.ndarray]] = {}
        self.ocr = WindowsOCRService()
        self.last_known_pos: Optional[Tuple[float, float]] = None
        self.last_map_id: Optional[int] = None

        # Load legacy underground_db as fallback / floor_short enrichment
        legacy_ug_groups: List[Dict[str, Any]] = []
        legacy_shorts: Dict[Tuple[int, int], str] = {}
        ug_path = os.path.join(os.path.dirname(__file__), "underground_db.json")
        if os.path.exists(ug_path):
            try:
                with open(ug_path, "r", encoding="utf-8") as f:
                    legacy_ug_groups = json.load(f)
                for g in legacy_ug_groups:
                    gid = int(g.get("id", 0))
                    for fl in g.get("floors", []):
                        fid = int(fl.get("id", 0))
                        fs = (fl.get("floor_short") or fl.get("floor_name_short") or "").strip().upper()
                        if gid and fid and fs:
                            legacy_shorts[(gid, fid)] = fs
            except Exception as e:
                logger.warning(f"Could not load underground_db.json: {e}")

        # Load Cave Alpha Masks, Terrain Raster Tiles, and optional UI Anchor Templates
        masks_path = str(resolve_data_asset("cave_masks_v4.npz"))
        if os.path.exists(masks_path):
            try:
                with np.load(masks_path) as zf:
                    self.cave_masks = {k: zf[k].copy() for k in zf.files}
                logger.info(f"Loaded {len(self.cave_masks)} cave floor alpha masks.")
            except Exception as e:
                logger.warning(f"Could not load cave_masks_v4.npz: {e}")

        terrain_path = str(resolve_data_asset("terrain_n2_v4.npz"))
        if os.path.exists(terrain_path):
            try:
                with np.load(terrain_path) as zf:
                    for key in zf.files:
                        if key.startswith("map_") and key.endswith("_gray"):
                            mid = int(key[4:-5])
                            meta_key = f"map_{mid}_meta"
                            if meta_key in zf.files:
                                self.terrain_data[mid] = (zf[key].copy(), zf[meta_key].copy())
                logger.info(f"Loaded terrain raster tiles for {len(self.terrain_data)} maps.")
            except Exception as e:
                logger.warning(f"Could not load terrain_n2_v4.npz: {e}")

        anchors_path = str(resolve_data_asset("ui_anchors_v4.npz"))
        if os.path.exists(anchors_path):
            try:
                with np.load(anchors_path) as zf:
                    self.ui_anchors = {k: zf[k].copy() for k in zf.files}
            except Exception as e:
                logger.warning(f"Could not load ui_anchors_v4.npz: {e}")

        if os.path.exists(db_path):
            try:
                with open(db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict) and "maps" in data:
                    total_wps = 0
                    all_pgs: List[Dict[str, Any]] = []
                    for mid_str, mdata in data["maps"].items():
                        mid = int(mid_str)
                        wps = mdata.get("waypoints", [])
                        nb = self._build_spatial_index(wps)
                        pgs = mdata.get("point_groups", [])
                        areas = mdata.get("areas", [])

                        # Merge area names into area_names_map (Map 2 defaults take priority on overlapping IDs)
                        for ar in areas:
                            aid = int(ar.get("id") or 0)
                            aname = (ar.get("name") or "").strip()
                            if aid and aname and (mid == 2 or aid not in self.area_names_map):
                                self.area_names_map.setdefault(aid, aname)

                        # Normalize floor_short fields on loaded point_groups
                        for g in pgs:
                            gid = int(g.get("id", 0))
                            for f_idx, fl in enumerate(g.get("floors", [])):
                                fid = int(fl.get("id", 0))
                                fs = (fl.get("floor_short") or fl.get("floor_name_short") or legacy_shorts.get((gid, fid), "")).strip().upper()
                                fl["floor_short"] = fs
                                fl["floor_name_short"] = fs
                                fn = (fl.get("floor_name") or "").strip()
                                if fid:
                                    self._floor_label_index[(mid, str(fid))] = fs or fn or f"B{f_idx + 1}"

                        self.map_point_groups[mid] = pgs
                        all_pgs.extend(pgs)

                        self.maps[mid] = {
                            "id": mid,
                            "name": mdata.get("name", f"Map {mid}"),
                            "priority": mdata.get("priority", 99),
                            "waypoints": wps,
                            "neighbors": nb,
                            "areas": areas,
                            "point_groups": pgs,
                        }
                        self._register_bboxes(mid, wps)
                        self._edge_indices[mid] = self._build_edge_index(wps, nb)
                        total_wps += len(wps)

                    self.underground_groups = all_pgs if all_pgs else legacy_ug_groups
                    logger.info(
                        f"Loaded Multi-Map v4 database: {len(self.maps)} maps, {total_wps} waypoints, "
                        f"{len(self.underground_groups)} point_groups."
                    )
                elif isinstance(data, list):
                    nb = self._build_spatial_index(data)
                    self.maps[2] = {
                        "id": 2,
                        "name": "Bản Đồ Tương Tác Teyvat",
                        "priority": 1,
                        "waypoints": data,
                        "neighbors": nb,
                        "areas": [],
                        "point_groups": [],
                    }
                    self._register_bboxes(2, data)
                    self._edge_indices[2] = self._build_edge_index(data, nb)
                    self.underground_groups = legacy_ug_groups
                    logger.info(f"Loaded legacy waypoints database with {len(data)} waypoints.")

                if 2 in self.maps:
                    self.waypoints = self.maps[2]["waypoints"]
                    self.neighbors = self.maps[2]["neighbors"]
                elif self.maps:
                    first_m = next(iter(self.maps.values()))
                    self.waypoints = first_m["waypoints"]
                    self.neighbors = first_m["neighbors"]
            except Exception as e:
                logger.error(f"Failed to load waypoints database: {e}")
                self.underground_groups = legacy_ug_groups
        else:
            logger.warning(f"Waypoints database not found at {db_path}")
            self.underground_groups = legacy_ug_groups

    def _register_bboxes(self, map_id: int, waypoints: List[Dict[str, Any]]) -> None:
        if not waypoints:
            return
        xs = [w["x_pos"] for w in waypoints]
        ys = [w["y_pos"] for w in waypoints]
        self._area_bboxes[(map_id, None)] = (min(xs), max(xs), min(ys), max(ys))
        by_area: Dict[int, List[Dict[str, Any]]] = {}
        for w in waypoints:
            aid = int(w.get("area_id") or 0)
            if aid:
                by_area.setdefault(aid, []).append(w)
        for aid, lst in by_area.items():
            axs = [w["x_pos"] for w in lst]
            ays = [w["y_pos"] for w in lst]
            self._area_bboxes[(map_id, aid)] = (min(axs), max(axs), min(ays), max(ays))

    def _build_spatial_index(self, waypoints: List[Dict[str, Any]]) -> Dict[int, List]:
        """Precomputes neighbor waypoints within 2500m (3600m between Statues/Domains for 0% zoom)."""
        neighbors = {}
        n = len(waypoints)
        for i in range(n):
            w1 = waypoints[i]
            w1_id = w1["id"]
            w1_major = int(w1.get("label_id") or 0) in (2, 154)
            nb_list = []
            for j in range(n):
                if i == j:
                    continue
                w2 = waypoints[j]
                dw_x = w2["x_pos"] - w1["x_pos"]
                dw_y = w2["y_pos"] - w1["y_pos"]
                dist = math.hypot(dw_x, dw_y)
                max_d = 3600.0 if (w1_major and int(w2.get("label_id") or 0) in (2, 154)) else 2500.0
                if 35.0 <= dist <= max_d:
                    nb_list.append((w2, dw_x, dw_y, dist, math.atan2(dw_y, dw_x)))
            neighbors[w1_id] = nb_list
        return neighbors

    def _build_edge_index(self, waypoints: List[Dict[str, Any]], neighbors: Dict[int, List]) -> Dict[str, Any]:
        """Builds an angle-sorted NumPy edge index and 2D spatial grid for fast _match_single_map queries."""
        id_to_dense: Dict[int, int] = {w["id"]: idx for idx, w in enumerate(waypoints)}
        nb_fast: Dict[int, List[Tuple[int, float, float, Dict[str, Any]]]] = {}
        nb_id_sets: Dict[int, set] = {}
        grid_cell = 32.0
        inv_cell = 1.0 / grid_cell
        grid: Dict[Tuple[int, int], List[Tuple[int, float, float, Dict[str, Any]]]] = {}
        raw_angles: List[float] = []
        raw_dists: List[float] = []
        raw_w1_dense: List[int] = []
        raw_w1_list: List[Dict[str, Any]] = []
        raw_w2_list: List[Dict[str, Any]] = []

        for dense_idx, w1 in enumerate(waypoints):
            w1_id = w1["id"]
            wx = float(w1["x_pos"])
            wy = float(w1["y_pos"])
            g_key = (int(math.floor(wx * inv_cell)), int(math.floor(wy * inv_cell)))
            grid.setdefault(g_key, []).append((w1_id, wx, wy, w1))

            nb_list = neighbors.get(w1_id, [])
            fast_list: List[Tuple[int, float, float, Dict[str, Any]]] = []
            id_set = set()
            for w2, _, _, dist_w, angle_w in nb_list:
                w2_id = w2["id"]
                fast_list.append((w2_id, float(w2["x_pos"]), float(w2["y_pos"]), w2))
                id_set.add(w2_id)
                raw_angles.append(angle_w)
                raw_dists.append(dist_w)
                raw_w1_dense.append(dense_idx)
                raw_w1_list.append(w1)
                raw_w2_list.append(w2)
            nb_fast[w1_id] = fast_list
            nb_id_sets[w1_id] = id_set

        if raw_angles:
            raw_angles_arr = np.asarray(raw_angles, dtype=np.float64)
            raw_dists_arr = np.asarray(raw_dists, dtype=np.float64)
            raw_w1_dense_arr = np.asarray(raw_w1_dense, dtype=np.int32)
            order = np.argsort(raw_angles_arr, kind="mergesort")
            angles_sorted = raw_angles_arr[order]
            dists_sorted = raw_dists_arr[order]
            w1_dense_sorted = raw_w1_dense_arr[order]
            orig_order = order.astype(np.int32)
            w1_sorted = [raw_w1_list[int(idx)] for idx in order]
            w2_sorted = [raw_w2_list[int(idx)] for idx in order]
        else:
            angles_sorted = np.empty(0, dtype=np.float64)
            dists_sorted = np.empty(0, dtype=np.float64)
            w1_dense_sorted = np.empty(0, dtype=np.int32)
            orig_order = np.empty(0, dtype=np.int32)
            w1_sorted = []
            w2_sorted = []

        return {
            "id_to_dense": id_to_dense,
            "n_wps": len(waypoints),
            "angles": angles_sorted,
            "dists": dists_sorted,
            "w1_dense": w1_dense_sorted,
            "orig_order": orig_order,
            "w1_list": w1_sorted,
            "w2_list": w2_sorted,
            "nb_fast": nb_fast,
            "nb_id_sets": nb_id_sets,
            "grid": grid,
            "grid_cell": grid_cell,
            "neighbors_ref": neighbors,
        }

    @staticmethod
    def _query_edge_index(
        edge_idx: Dict[str, Any],
        w1_allowed: np.ndarray,
        angle_s: float,
        ang_tol: float,
        min_w_dist: float,
        max_w_dist: float,
    ) -> List[int]:
        """Queries matching edges within angle/distance tolerances while preserving original iteration order."""
        angles = edge_idx["angles"]
        dists = edge_idx["dists"]
        w1_dense = edge_idx["w1_dense"]
        orig_order = edge_idx["orig_order"]

        eps = 1e-9
        a_lo = angle_s - ang_tol - eps
        a_hi = angle_s + ang_tol + eps
        two_pi = 2.0 * math.pi
        slices: List[Tuple[int, int]] = []

        p_lo = max(-math.pi, a_lo)
        p_hi = min(math.pi, a_hi)
        if p_lo <= p_hi:
            slices.append((
                int(np.searchsorted(angles, p_lo, side="left")),
                int(np.searchsorted(angles, p_hi, side="right")),
            ))

        if a_lo < -math.pi:
            w_lo = a_lo + two_pi
            slices.append((
                int(np.searchsorted(angles, w_lo, side="left")),
                len(angles),
            ))
        if a_hi > math.pi:
            w_hi = a_hi - two_pi
            slices.append((
                0,
                int(np.searchsorted(angles, w_hi, side="right")),
            ))

        hit_chunks = []
        for lo, hi in slices:
            if hi <= lo:
                continue
            mask = (
                (dists[lo:hi] >= min_w_dist)
                & (dists[lo:hi] <= max_w_dist)
                & w1_allowed[w1_dense[lo:hi]]
            )
            rel = np.flatnonzero(mask)
            if rel.size:
                hit_chunks.append(rel + lo)

        if not hit_chunks:
            return []
        hits = hit_chunks[0] if len(hit_chunks) == 1 else np.unique(np.concatenate(hit_chunks))
        if hits.size > 1:
            hits = hits[np.argsort(orig_order[hits], kind="mergesort")]
        return hits.tolist()

    def _normalize_region_text(self, text: str) -> str:
        """Normalizes raw OCR text: strips diacritics, unifies transliterations, lowercases."""
        if not text:
            return ""
        replacements = {
            "ä": "a", "å": "a", "ë": "e", "ö": "o", "ø": "o", "ü": "u",
            "ciru": "cuu", "cir": "cuu", "cb": "co", "mé": "mo",
        }
        t = unicodedata.normalize("NFKD", text)
        t = "".join(c for c in t if not unicodedata.combining(c))
        t = t.lower().replace("đ", "d").replace("Đ", "d")
        for k, v in replacements.items():
            t = t.replace(k, v)
        return re.sub(r"[^a-z0-9\s]", " ", t)

    def detect_region_name(self, frame: np.ndarray) -> Tuple[Optional[int], Optional[int], str]:
        """
        Fast OCR detection of the map region text displayed at bottom-right of Genshin map (<15ms).
        Returns (detected_map_id, detected_area_id, raw_text).
        """
        if frame is None or frame.size == 0:
            return None, None, ""

        h, w = frame.shape[:2]
        roi = frame[int(h * 0.88):int(h * 0.96), int(w * 0.80):int(w * 0.96)]
        raw_text = self.ocr.recognize_text(roi)
        if not raw_text:
            return None, None, ""

        norm = self._normalize_region_text(raw_text)

        # 1. Check isolated sub-maps first
        submap_keywords = {
            9: ["khu mo ngam", "vuc da sau", "mo ngam", "chasm"],
            7: ["enkanomiya"],
            34: ["ky nguyen cu", "bien ky", "remiuria", "remuria"],
            36: ["thanh vien co", "nui thanh vien", "nui thanh", "vien co", "te dan tu lua"],
            37: ["bien lang vo chu", "bien lang"],
            40: ["suong nguyet", "cao nguyen trang", "nguyen trang", "ungien", "nuur", "ibni", "belum", "va cham lu khach"],
        }
        for mid, kws in submap_keywords.items():
            if any(k in norm for k in kws):
                return mid, None, raw_text

        # 2. Check main Teyvat map (Map 2) areas
        area_keywords = {
            8: ["beryl", "belleau", "fontaine", "fontam", "fontain", "dai sanh", "dei sanh", "sanh font", "court", "merusea", "morte", "erinnyes", "nostoi", "romaritime", "fleuve", "marcotte", "opera"],
            4: ["sumeru", "lokapala", "ardravi", "ashavan", "avidya", "vanarana", "hadramaveth", "sa mac", "vissudha", "desert"],
            2: ["liyue", "tuyet van", "khanh van", "dao quang", "quy ly", "tram ngoc", "chenyu", "quynh co", "song quynh"],
            1: ["mondstadt", "phong khoi", "tuyet son", "long tich", "dragonspine", "phong tieu", "doi phong", "falcon"],
            3: ["inazuma", "narukami", "kannazuka", "yashiori", "watatsumi", "seirai", "tsurumi"],
            11: ["natlan", "luu tuyen", "to tro tan", "hat lua", "son dat", "coatepec", "tepeacac", "ven hoa", "long chao", "tequemecan", "quenepa", "malipo", "luu huynh", "sulfur", "tollan", "atocpan", "ochkanatlan", "tieng vang"],
            13: ["nod-krai", "nod krai", "lempe", "lempo", "hiisi", "nodkrai"],
            16: ["snezhnaya", "dat bang", "bang vinh cuu", "vinh cuu", "bang vinh", "rankova", "zorya", "bach duong tuyet", "bach duong", "bech du", "lang mo", "chan bang", "long vu lua", "volkodlak", "dinh bang", "vuon vo uu", "novokitezhgrad", "kitezh", "sai canh", "dinh sai canh", "xuyen sao"],
        }

        for aid, kws in area_keywords.items():
            if any(k in norm for k in kws):
                return 2, aid, raw_text

        return None, None, raw_text

    def check_underground_alignment(
        self, world_x: float, world_y: float, floor_name: Optional[str], is_underground: bool
    ) -> float:
        """
        Calculates spatial alignment bonus against HoYoLAB's authentic underground point groups.
        """
        if not is_underground or not self.underground_groups:
            return 0.0

        clean_floor = (floor_name or "").strip().upper()
        for g in self.underground_groups:
            for f in g.get("floors", []):
                ov = f.get("overlay")
                if not ov: continue
                minLat, maxLat = ov["minLat"], ov["maxLat"]
                minLng, maxLng = ov["minLng"], ov["maxLng"]
                if (minLng - 100 <= world_x <= maxLng + 100) and (minLat - 100 <= world_y <= maxLat + 100):
                    fs = (f.get("floor_short") or "").strip().upper()
                    fn = (f.get("floor_name") or "").upper()
                    if clean_floor and (fs == clean_floor or clean_floor in fn):
                        return 800.0   # Exact floor overlay match
                    return 300.0       # Same underground group

        return 0.0

    def detect_player_arrow(
        self, frame: np.ndarray, hsv: Optional[np.ndarray] = None
    ) -> Optional[Tuple[float, float, float]]:
        """Finds the strict neon-cyan player arrow on fullscreen map M (no loose fallback)."""
        if frame is None or frame.size == 0:
            return None

        h, w = frame.shape[:2]
        if hsv is None:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # Strict neon-cyan gate avoids false positives on blue water, waypoints, or text
        cyan_lower = np.array([88, 215, 230], dtype=np.uint8)
        cyan_upper = np.array([99, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, cyan_lower, cyan_upper)

        # Mask UI headers, footers, left panel, bottom-right panel
        mask[:int(h * 0.12), :] = 0
        mask[int(h * 0.92):, :] = 0
        mask[:int(h * 0.45), :int(w * 0.18)] = 0
        mask[int(h * 0.80):, int(w * 0.82):] = 0

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        su = h / 1440.0
        sf = su * su
        min_area = 550.0 * sf
        max_area = 1650.0 * sf
        min_dim = 16.0 * su
        max_dim = 56.0 * su

        r_out = int(round(75.0 * su))
        yel_thresh = max(25, int(45.0 * sf))
        valid = []
        for c in contours:
            area = cv2.contourArea(c)
            if not (min_area <= area <= max_area):
                continue
            _, _, bw, bh = cv2.boundingRect(c)
            if not (min_dim <= bw <= max_dim and min_dim <= bh <= max_dim):
                continue
            aspect = max(bw, bh) / max(min(bw, bh), 1)
            if aspect > 1.85:
                continue
            hull = cv2.convexHull(c)
            hull_area = cv2.contourArea(hull)
            if hull_area <= 0 or (area / hull_area) < 0.55:
                continue

            # Reject off-screen edge return-to-player compass badge (dark disc + outer yellow triangle)
            mc = cv2.moments(c)
            if mc["m00"] <= 0:
                continue
            ccx = float(mc["m10"] / mc["m00"])
            ccy = float(mc["m01"] / mc["m00"])
            x0 = max(0, int(ccx) - r_out)
            x1 = min(w, int(ccx) + r_out + 1)
            y0 = max(0, int(ccy) - r_out)
            y1 = min(h, int(ccy) + r_out + 1)
            if x1 > x0 and y1 > y0:
                yy, xx = np.ogrid[y0:y1, x0:x1]
                dist = np.hypot(xx - ccx, yy - ccy) / su
                roi_hsv = hsv[y0:y1, x0:x1]
                m_disc = (dist >= 22.0) & (dist <= 38.0)
                dark_disc = 0.0
                v_p90 = 255.0
                if np.any(m_disc):
                    v_disc = roi_hsv[m_disc, 2]
                    dark_disc = float(np.mean(v_disc < 65))
                    v_p90 = float(np.percentile(v_disc, 90))
                m_ptr = (dist >= 44.0) & (dist <= 74.0)
                yel_ptr = int(
                    np.sum(
                        m_ptr
                        & (roi_hsv[:, :, 0] >= 18)
                        & (roi_hsv[:, :, 0] <= 34)
                        & (roi_hsv[:, :, 1] >= 140)
                        & (roi_hsv[:, :, 2] >= 200)
                    )
                )
                if (dark_disc >= 0.70 and v_p90 <= 85.0) or (dark_disc >= 0.55 and yel_ptr >= yel_thresh):
                    continue

            valid.append(c)

        if not valid:
            return None

        center_x, center_y = w / 2, h / 2

        def dist_to_center(c):
            M = cv2.moments(c)
            if M["m00"] == 0:
                return 99999.0
            return math.hypot(M["m10"] / M["m00"] - center_x, M["m01"] / M["m00"] - center_y)

        best = min(valid, key=dist_to_center)
        M = cv2.moments(best)
        if M["m00"] == 0:
            return None

        cx = float(M["m10"] / M["m00"])
        cy = float(M["m01"] / M["m00"])

        # Recover true rotation pivot & nose heading from the asymmetric 3-corner cyan arrowhead:
        # the 2 swept-back wings sit farther from the centroid than the nose (which lies opposite
        # the shortest edge), shifting the area centroid ~2.78 px @1440p behind the rotation pivot.
        peri = cv2.arcLength(best, True)
        tri_pts = None
        for eps_f in (0.04, 0.05, 0.06, 0.03, 0.07, 0.08, 0.09, 0.10):
            ap = cv2.approxPolyDP(best, eps_f * peri, True)
            if len(ap) == 3:
                tri_pts = ap.reshape(3, 2).astype(np.float64)
                break
        if tri_pts is None:
            _, tri_raw = cv2.minEnclosingTriangle(best.astype(np.float32))
            if tri_raw is not None and len(tri_raw) == 3:
                tri_pts = tri_raw.reshape(3, 2).astype(np.float64)

        if tri_pts is not None:
            v_dists = [math.hypot(tri_pts[i, 0] - cx, tri_pts[i, 1] - cy) for i in range(3)]
            opp_lens = [
                math.hypot(
                    tri_pts[(i + 1) % 3, 0] - tri_pts[(i + 2) % 3, 0],
                    tri_pts[(i + 1) % 3, 1] - tri_pts[(i + 2) % 3, 1],
                )
                for i in range(3)
            ]
            nose_idx = int(np.argmin([opp_lens[i] + v_dists[i] for i in range(3)]))
            dx_n = float(tri_pts[nose_idx, 0]) - cx
            dy_n = float(tri_pts[nose_idx, 1]) - cy
            nd = math.hypot(dx_n, dy_n)
            if nd > 1e-6:
                ux, uy = dx_n / nd, dy_n / nd
                cx += self.ARROW_PIVOT_OFFSET_PX_1440 * su * ux
                cy += self.ARROW_PIVOT_OFFSET_PX_1440 * su * uy
                angle = float(math.degrees(math.atan2(ux, -uy)) % 360.0)
                return (cx, cy, angle)

        # Fallback: farthest contour vertex from centroid -> 0 deg = Up, clockwise
        pts = best.reshape(-1, 2)
        dists = (pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2
        tip = pts[int(np.argmax(dists))]
        angle = float(math.degrees(math.atan2(float(tip[0]) - cx, -(float(tip[1]) - cy))) % 360.0)

        return (cx, cy, angle)

    def _get_elev_templates(self, scale_ui: float) -> Optional[Dict[str, np.ndarray]]:
        """Loads and caches 0.5x resized 4-type elevator slot templates (~3x faster matching)."""
        if not all(k in self.ui_anchors for k in ("tpl_ma", "tpl_mi", "tpl_la", "tpl_li")):
            return None
        sz = max(12, int(round(24.0 * scale_ui)))
        if sz in self._elev_tpl_cache:
            return self._elev_tpl_cache[sz]
        interp = cv2.INTER_AREA if sz < 48 else cv2.INTER_LINEAR
        entry = {}
        for short_k, arr_k in (("MA", "tpl_ma"), ("MI", "tpl_mi"), ("LA", "tpl_la"), ("LI", "tpl_li")):
            tpl = self.ui_anchors[arr_k]
            entry[short_k] = tpl if tpl.shape[0] == sz else cv2.resize(tpl, (sz, sz), interpolation=interp)
        self._elev_tpl_cache[sz] = entry
        return entry

    def _detect_layers_template(self, frame: np.ndarray) -> Optional[Dict[str, Any]]:
        """4-Template Elevator Slot Detector (MA, MI, LA, LI) at 0.5x scale, immune to terrain glow."""
        h, w = frame.shape[:2]
        scale_ui = h / 1440.0
        tpls = self._get_elev_templates(scale_ui)
        if not tpls:
            return None

        sz_h = tpls["MA"].shape[0]
        full_sz = max(20, int(round(48.0 * scale_ui)))
        x_col = int(round(w - 97.0 * scale_ui))
        half_strip = max(full_sz // 2 + 8, int(round(40.0 * scale_ui)))
        x0 = max(0, x_col - half_strip)
        x1 = min(w, x_col + half_strip)
        y0 = int(0.20 * h)
        y1 = int(0.82 * h)
        roi = frame[y0:y1, x0:x1]
        if roi.shape[0] < full_sz or roi.shape[1] < full_sz:
            return None

        rh_h = max(sz_h, roi.shape[0] // 2)
        rw_h = max(sz_h, roi.shape[1] // 2)
        roi_h = cv2.resize(roi, (rw_h, rh_h), interpolation=cv2.INTER_AREA)
        sx_f = roi.shape[1] / float(rw_h)
        sy_f = roi.shape[0] / float(rh_h)

        maps_res: Dict[str, np.ndarray] = {}
        raw_hits: List[Tuple[float, str, float, float, int, int]] = []
        for tname, tpl in tpls.items():
            res = cv2.matchTemplate(roi_h, tpl, cv2.TM_CCOEFF_NORMED)
            maps_res[tname] = res
            ys, xs = np.where(res >= 0.74)
            for ry, rx in zip(ys.tolist(), xs.tolist()):
                sc = float(res[ry, rx])
                cx_g = float(x0 + (rx + sz_h / 2.0) * sx_f)
                cy_g = float(y0 + (ry + sz_h / 2.0) * sy_f)
                raw_hits.append((sc, tname, cx_g, cy_g, rx, ry))

        if not raw_hits:
            return {
                "has_layers": False,
                "is_underground": False,
                "total_floors": 1,
                "active_floor_idx": 0,
                "surface_idx": 0,
                "floor_name": "G",
                "slot_types": [],
            }

        # Enrich active score for hits added before MA/LA were evaluated
        ma_map, la_map = maps_res["MA"], maps_res["LA"]
        enriched_hits: List[Dict[str, Any]] = []
        for sc, tname, cx_g, cy_g, rx, ry in raw_hits:
            active_sc = max(float(ma_map[ry, rx]), float(la_map[ry, rx]))
            enriched_hits.append({
                "score": sc,
                "type": tname,
                "cx": cx_g,
                "cy": cy_g,
                "active_score": active_sc,
            })

        # Non-maximum suppression across all 4 template types within min_dy / min_dx
        enriched_hits.sort(key=lambda d: d["score"], reverse=True)
        min_dy = 28.0 * scale_ui
        min_dx = 18.0 * scale_ui
        slots: List[Dict[str, Any]] = []
        for h_item in enriched_hits:
            if any(abs(h_item["cy"] - s["cy"]) < min_dy and abs(h_item["cx"] - s["cx"]) < min_dx for s in slots):
                continue
            slots.append(h_item)

        if len(slots) < 2:
            return {
                "has_layers": False,
                "is_underground": False,
                "total_floors": 1,
                "active_floor_idx": 0,
                "surface_idx": 0,
                "floor_name": "G",
                "slot_types": [s["type"] for s in slots],
            }

        # Filter horizontally to dominant elevator column
        med_x = float(np.median([s["cx"] for s in slots]))
        col_tol = 14.0 * scale_ui
        slots = [s for s in slots if abs(s["cx"] - med_x) <= col_tol]
        slots.sort(key=lambda s: s["cy"])

        # Require at least one active (MA or LA) detection above threshold
        active_candidates = [s for s in slots if s["type"] in ("MA", "LA") or s["active_score"] >= 0.74]
        if len(slots) < 2 or not active_candidates:
            return {
                "has_layers": False,
                "is_underground": False,
                "total_floors": 1,
                "active_floor_idx": 0,
                "surface_idx": 0,
                "floor_name": "G",
                "slot_types": [s["type"] for s in slots],
            }

        # Keep contiguous vertical cluster around the strongest active slot
        best_active_slot = max(slots, key=lambda s: s["active_score"] + (0.25 if s["type"] in ("MA", "LA") else 0.0))
        a_pos = slots.index(best_active_slot)
        max_gap = 135.0 * scale_ui
        cluster = [slots[a_pos]]
        for idx in range(a_pos - 1, -1, -1):
            if abs(cluster[0]["cy"] - slots[idx]["cy"]) <= max_gap:
                cluster.insert(0, slots[idx])
            else:
                break
        for idx in range(a_pos + 1, len(slots)):
            if abs(slots[idx]["cy"] - cluster[-1]["cy"]) <= max_gap:
                cluster.append(slots[idx])
            else:
                break

        N = len(cluster)
        if N < 2:
            return {
                "has_layers": False,
                "is_underground": False,
                "total_floors": 1,
                "active_floor_idx": 0,
                "surface_idx": 0,
                "floor_name": "G",
                "slot_types": [s["type"] for s in cluster],
            }

        active_idx = int(np.argmax([
            (1.0 if s["type"] in ("MA", "LA") else 0.0) + s["active_score"] for s in cluster
        ]))

        if cluster[0]["type"] in ("MA", "MI"):
            surface_idx = 0
        elif cluster[-1]["type"] in ("MA", "MI"):
            surface_idx = N - 1
        else:
            surface_idx = 0 if active_idx > 0 else N - 1

        is_underground = bool(active_idx != surface_idx)
        if not is_underground:
            floor_name = "G"
        elif surface_idx == 0:
            floor_name = f"B{active_idx}"
        else:
            floor_name = f"B{active_idx + 1}"

        return {
            "has_layers": True,
            "is_underground": is_underground,
            "total_floors": N,
            "active_floor_idx": active_idx,
            "surface_idx": surface_idx,
            "floor_name": floor_name,
            "slot_types": [s["type"] for s in cluster],
        }

    def detect_layers_universal(self, frame: np.ndarray) -> Dict[str, Any]:
        """
        Universal Multilayer & Elevator detector for Genshin Impact Map M.
        Uses 4-Template Slot Detection (MA, MI, LA, LI) with HSV fallback.
        Supports 1080p, 2K, and 4K displays.
        """
        default_res = {
            "has_layers": False,
            "is_underground": False,
            "total_floors": 1,
            "active_floor_idx": 0,
            "surface_idx": 0,
            "floor_name": "G",
            "slot_types": [],
        }
        if frame is None or frame.size == 0:
            return default_res

        tpl_res = self._detect_layers_template(frame)
        if tpl_res is not None:
            return tpl_res

        h, w = frame.shape[:2]
        roi = frame[int(h * 0.22):int(h * 0.78), int(w * 0.90):int(w * 0.99)]
        if roi.size == 0:
            return default_res

        # 1. Find cyan glowing active button (fallback when ui_anchors_v4.npz unavailable)
        hsv_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask_cyan = cv2.inRange(hsv_roi, np.array([85, 120, 140]), np.array([115, 255, 255]))
        cyan_contours, _ = cv2.findContours(mask_cyan, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_cyan = 150 * (h / 1440.0) ** 2
        max_cyan = 2200 * (h / 1440.0) ** 2
        max_box_dim = 65 * (w / 2560.0)
        valid_cyan = []
        for c in cyan_contours:
            area = cv2.contourArea(c)
            if min_cyan <= area <= max_cyan:
                _, _, bw, bh = cv2.boundingRect(c)
                if bw <= max_box_dim and bh <= max_box_dim:
                    valid_cyan.append(c)

        if not valid_cyan:
            return default_res

        ac = max(valid_cyan, key=cv2.contourArea)
        M = cv2.moments(ac)
        if M["m00"] <= 0:
            return default_res
        cyan_cx = M["m10"] / M["m00"]
        cyan_cy = M["m01"] / M["m00"]
        _, _, ac_bw, ac_bh = cv2.boundingRect(ac)

        # 2. Find all icon contours vertically aligned with the elevator column
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        min_area = 180 * (h / 1440.0) ** 2
        max_area = 15000 * (h / 1440.0) ** 2
        min_w = 20 * (w / 2560.0)
        min_h = 18 * (h / 1440.0)
        dx_tol = 25 * (w / 2560.0)

        best_button_ys = []
        for th_val in [105, 90, 75, 60]:
            _, thresh = cv2.threshold(gray, th_val, 255, cv2.THRESH_BINARY)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            button_ys = []
            for c in contours:
                area = cv2.contourArea(c)
                if min_area <= area <= max_area:
                    _, _, bw, bh = cv2.boundingRect(c)
                    if bw >= min_w and bh >= min_h:
                        M_c = cv2.moments(c)
                        if M_c["m00"] > 0:
                            cx = M_c["m10"] / M_c["m00"]
                            cy = M_c["m01"] / M_c["m00"]
                            if abs(cx - cyan_cx) <= dx_tol:
                                button_ys.append((cy, area))
            if len(button_ys) >= 2:
                best_button_ys = button_ys
                break
            elif len(button_ys) > len(best_button_ys):
                best_button_ys = button_ys

        button_ys = best_button_ys
        button_ys.sort(key=lambda x: x[0])
        merged = []
        merge_dist = 14.0 * (h / 1440.0)
        for cy, a in button_ys:
            if not merged or abs(cy - merged[-1][0]) > merge_dist:
                merged.append([cy, a])
            else:
                merged[-1][0] = (merged[-1][0] + cy) / 2
                merged[-1][1] = max(merged[-1][1], a)

        active_match_tol = max(merge_dist, ac_bh * 0.75)
        if not any(abs(m[0] - cyan_cy) <= active_match_tol for m in merged):
            merged.append([cyan_cy, cv2.contourArea(ac)])
            merged.sort(key=lambda x: x[0])

        max_gap = 135.0 * (h / 1440.0)
        cyan_idx = int(np.argmin([abs(m[0] - cyan_cy) for m in merged]))
        cluster = [merged[cyan_idx]]
        for idx in range(cyan_idx - 1, -1, -1):
            if abs(cluster[0][0] - merged[idx][0]) <= max_gap:
                cluster.insert(0, merged[idx])
            else:
                break
        for idx in range(cyan_idx + 1, len(merged)):
            if abs(merged[idx][0] - cluster[-1][0]) <= max_gap:
                cluster.append(merged[idx])
            else:
                break

        N = len(cluster)
        if N <= 1:
            return default_res

        dists = [abs(m[0] - cyan_cy) for m in cluster]
        active_idx = int(np.argmin(dists))

        areas = [m[1] for m in cluster]
        max_a_idx = int(np.argmax(areas))
        if max_a_idx == 0:
            surface_idx = 0
        elif max_a_idx == N - 1:
            surface_idx = N - 1
        else:
            surface_idx = 0 if active_idx > 0 else (N - 1 if N >= 5 else 0)

        is_underground = bool(active_idx != surface_idx)
        if not is_underground:
            floor_name = "G"
        elif surface_idx == 0:
            floor_name = f"B{active_idx}"
        else:
            floor_name = f"B{active_idx + 1}"

        return {
            "has_layers": True,
            "is_underground": is_underground,
            "total_floors": N,
            "active_floor_idx": active_idx,
            "surface_idx": surface_idx,
            "floor_name": floor_name,
            "slot_types": [],
        }

    def detect_underground_state(self, frame: np.ndarray) -> Tuple[bool, int]:
        """Backward compatibility wrapper."""
        info = self.detect_layers_universal(frame)
        return info["is_underground"], info["active_floor_idx"]

    @staticmethod
    def _build_ui_exclusion_mask(h: int, w: int) -> np.ndarray:
        """Surgical HUD exclusion mask that keeps edge statues (e.g. 0% zoom) visible."""
        ui_ok = np.full((h, w), 255, dtype=np.uint8)
        ui_ok[:int(h * 0.065), :] = 0                                  # Top bar
        ui_ok[:int(h * 0.42), :int(w * 0.18)] = 0                      # Top-left avatar + notices
        ui_ok[int(h * 0.36):int(h * 0.62), :int(w * 0.05)] = 0         # Left zoom slider
        ui_ok[int(h * 0.90):, :int(w * 0.07)] = 0                      # Bottom-left gear
        ui_ok[int(h * 0.90):, int(w * 0.37):int(w * 0.63)] = 0         # Bottom-center gadget banner
        ui_ok[int(h * 0.88):, int(w * 0.76):] = 0                      # Bottom-right region + UID
        ui_ok[int(h * 0.20):int(h * 0.80), int(w * 0.93):] = 0         # Right elevator column
        return ui_ok

    def detect_waypoint_candidates(
        self,
        frame: np.ndarray,
        arrow_pos: Optional[Tuple[float, float]] = None,
        hsv: Optional[np.ndarray] = None
    ) -> List[Tuple[float, float, int]]:
        """
        Extracts candidate waypoint coordinates with high-precision adaptive thresholding.
        Preserves waypoints under the cyan player arrow using neutral-white border verification,
        and recovers dimmed/teal statues and high-contrast white pins when Pass 1 is sparse.
        """
        h, w = frame.shape[:2]
        if hsv is None:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        ui_ok = self._build_ui_exclusion_mask(h, w)

        scale_ui = w / 2560.0
        scale_factor = scale_ui ** 2
        min_area = max(6.0, 14.0 * scale_factor)
        max_area = max(110.0, 1450.0 * scale_factor)
        max_dim = 58.0 * scale_ui
        box_rad = max(12, int(round(18 * scale_ui)))
        arrow_tol = 36.0 * scale_ui
        min_neutral_arrow = max(16, int(80 * scale_factor))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)) if w < 1600 else cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

        # Primary pass: Active waypoint & statue diamond blue/cyan core (H >= 85, S >= 115)
        mask = cv2.bitwise_and(
            cv2.inRange(hsv, np.array([85, 115, 130], dtype=np.uint8), np.array([126, 255, 255], dtype=np.uint8)),
            ui_ok,
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        candidates: List[Tuple[float, float, int]] = []
        min_white_p1 = max(15, int(120 * scale_factor))
        min_white_dom = max(10, int(65 * scale_factor))
        min_dark_dom = max(12, int(100 * scale_factor))
        white200_u8 = (gray_frame > 200).view(np.uint8)
        for c in contours:
            area = cv2.contourArea(c)
            if min_area <= area <= max_area:
                _, _, bw, bh = cv2.boundingRect(c)
                if bw > max_dim or bh > max_dim:
                    continue
                M = cv2.moments(c)
                if M["m00"] > 0:
                    cx = float(M["m10"] / M["m00"])
                    cy = float(M["m01"] / M["m00"])
                    y0, y1 = max(0, int(cy) - box_rad), min(h, int(cy) + box_rad)
                    x0, x1 = max(0, int(cx) - box_rad), min(w, int(cx) + box_rad)
                    white_px = cv2.countNonZero(white200_u8[y0:y1, x0:x1])

                    # Keep candidate even near arrow if neutral-white teardrop border is clearly present
                    if arrow_pos and math.hypot(cx - arrow_pos[0], cy - arrow_pos[1]) < arrow_tol:
                        roi_g = gray_frame[y0:y1, x0:x1]
                        roi_s = hsv[y0:y1, x0:x1, 1]
                        neutral_white = int(np.sum((roi_g > 198) & (roi_s < 65)))
                        if neutral_white < min_neutral_arrow:
                            continue
                        candidates.append((cx, cy, max(white_px, neutral_white + 40)))
                    elif white_px >= min_white_p1:
                        candidates.append((cx, cy, white_px))
                    elif white_px >= min_white_dom:
                        dark_px = cv2.countNonZero((gray_frame[y0:y1, x0:x1] < 65).view(np.uint8))
                        if dark_px >= min_dark_dom:
                            candidates.append((cx, cy, white_px + dark_px))

        merge_dist = 34.0 * scale_ui
        clean: List[Tuple[float, float, int]] = []
        candidates.sort(key=lambda x: x[2], reverse=True)
        for c in candidates:
            if not any(math.hypot(c[0] - x[0], c[1] - x[1]) < merge_dist for x in clean):
                clean.append(c)

        # Pass 2b: Dimmed / Anemo-teal / inactive-layer waypoints with ring contrast (snow-immune)
        if len(clean) < 6:
            mask2 = cv2.bitwise_and(
                cv2.inRange(hsv, np.array([82, 78, 105], dtype=np.uint8), np.array([126, 255, 255], dtype=np.uint8)),
                ui_ok,
            )
            mask2 = cv2.morphologyEx(mask2, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
            cnts2, _ = cv2.findContours(mask2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cands2: List[Tuple[float, float, int]] = []
            outer_rad = max(14, int(round(20 * scale_ui)))
            min_silver = max(14, int(75 * scale_factor))
            silver_u8 = ((gray_frame > 158) & (hsv[:, :, 1] < 68)).view(np.uint8)
            for c in cnts2:
                area = cv2.contourArea(c)
                if min_area <= area <= max_area:
                    M = cv2.moments(c)
                    if M["m00"] > 0:
                        cx = float(M["m10"] / M["m00"])
                        cy = float(M["m01"] / M["m00"])
                        if any(math.hypot(cx - x[0], cy - x[1]) < merge_dist for x in clean):
                            continue
                        y0, y1 = max(0, int(cy) - outer_rad), min(h, int(cy) + outer_rad)
                        x0, x1 = max(0, int(cx) - outer_rad), min(w, int(cx) + outer_rad)
                        silver_slice = silver_u8[y0:y1, x0:x1]
                        if silver_slice.size == 0:
                            continue
                        silver_cnt = cv2.countNonZero(silver_slice)
                        if silver_cnt < min_silver:
                            continue
                        roi_g = gray_frame[y0:y1, x0:x1]
                        if arrow_pos and math.hypot(cx - arrow_pos[0], cy - arrow_pos[1]) < arrow_tol:
                            roi_s = hsv[y0:y1, x0:x1, 1]
                            if int(np.sum((roi_g > 195) & (roi_s < 65))) < min_neutral_arrow:
                                continue
                        silver_mask = silver_slice.astype(bool)
                        # Outer perimeter pixels vs silver border mean reject uniform snow/bright backgrounds
                        ph, pw = roi_g.shape
                        ring = np.zeros_like(silver_mask)
                        ring[:3, :] = True
                        ring[-3:, :] = True
                        ring[:, :3] = True
                        ring[:, -3:] = True
                        ring_bg = roi_g[ring & (~silver_mask)]
                        contrast = float(np.mean(roi_g[silver_mask])) - (float(np.mean(ring_bg)) if ring_bg.size > 0 else 255.0)
                        if contrast > 28.0:
                            cands2.append((cx, cy, silver_cnt))
            cands2.sort(key=lambda x: x[2], reverse=True)
            for c in cands2:
                if not any(math.hypot(c[0] - x[0], c[1] - x[1]) < merge_dist for x in clean):
                    clean.append(c)

        # Pass 2c: Locked/dark-backdrop neutral white teardrop pins (e.g. Moon Plateau / inactive floors)
        if len(clean) < 5:
            white_mask = ((gray_frame > 205) & (hsv[:, :, 1] < 50)).astype(np.uint8) * 255
            white_mask = cv2.bitwise_and(white_mask, ui_ok)
            cnts_w, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cands3: List[Tuple[float, float, int]] = []
            outer_rad = max(14, int(round(20 * scale_ui)))
            for cw in cnts_w:
                wa = cv2.contourArea(cw)
                if max(16.0, 22.0 * scale_factor) <= wa <= max(120.0, 650.0 * scale_factor):
                    Mw = cv2.moments(cw)
                    if Mw["m00"] > 0:
                        wcx, wcy = float(Mw["m10"] / Mw["m00"]), float(Mw["m01"] / Mw["m00"])
                        if any(math.hypot(wcx - x[0], wcy - x[1]) < merge_dist for x in clean):
                            continue
                        if arrow_pos and math.hypot(wcx - arrow_pos[0], wcy - arrow_pos[1]) < arrow_tol:
                            continue
                        y0, y1 = max(0, int(wcy) - outer_rad), min(h, int(wcy) + outer_rad)
                        x0, x1 = max(0, int(wcx) - outer_rad), min(w, int(wcx) + outer_rad)
                        roi_g = gray_frame[y0:y1, x0:x1]
                        if roi_g.shape[0] >= 12 and roi_g.shape[1] >= 12:
                            border_vals = np.concatenate([roi_g[:3, :].ravel(), roi_g[-3:, :].ravel(), roi_g[:, :3].ravel(), roi_g[:, -3:].ravel()])
                            if float(np.mean(border_vals)) < 145.0:
                                cands3.append((wcx, wcy, int(wa)))
            cands3.sort(key=lambda x: x[2], reverse=True)
            for c in cands3:
                if not any(math.hypot(c[0] - x[0], c[1] - x[1]) < merge_dist for x in clean):
                    clean.append(c)

        return clean

    @classmethod
    def _icon_anchor_offset_px(cls, waypoint: Dict[str, Any], scale_ui: float) -> Tuple[float, float]:
        """Returns (dx_px, dy_px) screen-space offset from detected blue-core centroid to icon anchor."""
        lbl = int(waypoint.get("label_id") or 0)
        ox, oy = cls.ICON_ANCHOR_OFFSET_PX_1440.get(lbl, cls.DEFAULT_ICON_ANCHOR_OFFSET_PX_1440)
        return (ox * scale_ui, oy * scale_ui)

    @classmethod
    def _compensate_inliers_screen_anchors(
        cls,
        inliers: List[Tuple[Tuple[float, float, int], Dict[str, Any]]],
        scale_ui: float,
    ) -> List[Tuple[Tuple[float, float, int], Dict[str, Any]]]:
        """Applies resolution-scaled screen-space icon anchor offsets to matched inliers before LSQ."""
        adjusted: List[Tuple[Tuple[float, float, int], Dict[str, Any]]] = []
        for s, wp in inliers:
            ox, oy = cls._icon_anchor_offset_px(wp, scale_ui)
            adjusted.append(((float(s[0]) + ox, float(s[1]) + oy, s[2]), wp))
        return adjusted

    @staticmethod
    def _fit_isometric_lsq(
        inliers: List[Tuple[Tuple[float, float, int], Dict[str, Any]]]
    ) -> Optional[Tuple[float, float, float, float]]:
        """Closed-form uniform-scale isometric fit minimizing squared residual across all inliers."""
        if len(inliers) < 2:
            return None
        sx = np.array([float(s[0]) for s, _ in inliers], dtype=np.float64)
        sy = np.array([float(s[1]) for s, _ in inliers], dtype=np.float64)
        wx = np.array([float(w["x_pos"]) for _, w in inliers], dtype=np.float64)
        wy = np.array([float(w["y_pos"]) for _, w in inliers], dtype=np.float64)
        msx, msy = float(sx.mean()), float(sy.mean())
        mwx, mwy = float(wx.mean()), float(wy.mean())
        dsx, dsy = sx - msx, sy - msy
        dwx, dwy = wx - mwx, wy - mwy
        denom = float(np.sum(dsx * dsx + dsy * dsy))
        if denom < 1e-6:
            return None
        scale = float(np.sum(dsx * dwx + dsy * dwy) / denom)
        if scale <= 0:
            return None
        tx = float(mwx - scale * msx)
        ty = float(mwy - scale * msy)
        rmse = float(np.sqrt(np.mean((scale * sx + tx - wx) ** 2 + (scale * sy + ty - wy) ** 2)))
        return scale, tx, ty, rmse

    def _match_terrain_patch(
        self,
        frame_gray: Optional[np.ndarray],
        map_id: int,
        scale: float,
        tx: float,
        ty: float,
        search_pad: int,
    ) -> Optional[Tuple[np.ndarray, float, float, int, int, float]]:
        """Runs Sobel gradient NCC between cropped live map frame and detail_v2 terrain tiles."""
        if frame_gray is None or frame_gray.size == 0 or map_id not in self.terrain_data or scale <= 0:
            return None
        canvas, meta = self.terrain_data[map_id]
        tx0, ty0, z, orig_x, orig_y = [float(v) for v in meta]
        pow2z = 2.0 ** z

        fh, fw = frame_gray.shape[:2]
        rx0, rx1 = int(0.16 * fw), int(0.84 * fw)
        ry0, ry1 = int(0.14 * fh), int(0.86 * fh)
        crop = frame_gray[ry0:ry1, rx0:rx1]
        if crop.size == 0:
            return None

        cw = max(24, int(round((rx1 - rx0) * scale * pow2z)))
        ch = max(24, int(round((ry1 - ry0) * scale * pow2z)))
        if cw >= canvas.shape[1] or ch >= canvas.shape[0]:
            return None

        q = cv2.resize(crop, (cw, ch), interpolation=cv2.INTER_AREA)
        x0_f = ((rx0 * scale + tx) + orig_x) * pow2z - tx0 * 256.0
        y0_f = ((ry0 * scale + ty) + orig_y) * pow2z - ty0 * 256.0
        x0 = int(round(x0_f))
        y0 = int(round(y0_f))

        rx_a = max(0, x0 - search_pad)
        ry_a = max(0, y0 - search_pad)
        rx_b = min(canvas.shape[1], x0 + cw + search_pad)
        ry_b = min(canvas.shape[0], y0 + ch + search_pad)
        if rx_b - rx_a < cw or ry_b - ry_a < ch:
            return None

        roi = canvas[ry_a:ry_b, rx_a:rx_b]
        if float(np.mean(roi > 2)) < 0.40 or float(np.std(roi)) < 6.0 or float(np.std(q)) < 6.0:
            return None

        qg = cv2.magnitude(cv2.Sobel(q, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(q, cv2.CV_32F, 0, 1, ksize=3))
        rg = cv2.magnitude(cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3))
        res = cv2.matchTemplate(rg, qg, cv2.TM_CCOEFF_NORMED)
        return res, x0_f, y0_f, rx_a, ry_a, pow2z

    def _score_terrain_alignment(
        self,
        frame_gray: Optional[np.ndarray],
        map_id: int,
        scale: float,
        tx: float,
        ty: float,
        search_pad: int = 8,
    ) -> float:
        """
        Scores isometric alignment between the live game frame and official detail_v2 terrain tiles
        using Sobel gradient magnitude normalized cross-correlation (immune to zero-padding seams).
        """
        matched = self._match_terrain_patch(frame_gray, map_id, scale, tx, ty, search_pad=search_pad)
        if matched is None:
            return 0.0
        return float(cv2.minMaxLoc(matched[0])[1])

    def _refine_transform_with_terrain(
        self,
        frame_gray: Optional[np.ndarray],
        map_id: int,
        scale: float,
        tx: float,
        ty: float,
        search_pad: int = 12,
        is_underground: bool = False,
    ) -> Tuple[float, float, bool]:
        """Refines residual world translation (tx, ty) via high-confidence surface Sobel registration."""
        if is_underground:
            return tx, ty, False
        matched = self._match_terrain_patch(frame_gray, map_id, scale, tx, ty, search_pad=search_pad)
        if matched is None:
            return tx, ty, False
        res, x0_f, y0_f, rx_a, ry_a, pow2z = matched
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        mx, my = max_loc

        rh, rw = res.shape[:2]
        res_sup = res.copy()
        res_sup[max(0, my - 5):min(rh, my + 6), max(0, mx - 5):min(rw, mx + 6)] = -1.0
        _, second_val, _, _ = cv2.minMaxLoc(res_sup)
        if max_val < 0.30 or (max_val - second_val) < 0.06:
            return tx, ty, False

        sx_off = 0.0
        if 0 < mx < rw - 1:
            v0, v1, v2 = float(res[my, mx - 1]), float(res[my, mx]), float(res[my, mx + 1])
            denom_x = v0 - 2.0 * v1 + v2
            if abs(denom_x) > 1e-6:
                sx_off = float(np.clip(0.5 * (v0 - v2) / denom_x, -0.5, 0.5))

        sy_off = 0.0
        if 0 < my < rh - 1:
            v0, v1, v2 = float(res[my - 1, mx]), float(res[my, mx]), float(res[my + 1, mx])
            denom_y = v0 - 2.0 * v1 + v2
            if abs(denom_y) > 1e-6:
                sy_off = float(np.clip(0.5 * (v0 - v2) / denom_y, -0.5, 0.5))

        dwx = ((rx_a + mx + sx_off) - x0_f) / pow2z
        dwy = ((ry_a + my + sy_off) - y0_f) / pow2z
        if abs(dwx) > 8.0 or abs(dwy) > 8.0:
            return tx, ty, False
        return tx + dwx, ty + dwy, True

    def _match_single_map(
        self,
        candidates: List[Tuple[float, float, int]],
        waypoints: List[Dict[str, Any]],
        neighbors: Dict[int, List],
        cand_limit: int = 10,
        frame_w: Optional[int] = None,
        base_scale: float = 1.12,
        target_area_id: Optional[int] = None,
        arrow_pos: Optional[Tuple[float, float]] = None,
        allow_2point: bool = False,
        strict_scale: Optional[float] = None,
        frame_gray: Optional[np.ndarray] = None,
        map_id: int = 2,
        is_underground: bool = False,
    ) -> Optional[Tuple[List, float, np.ndarray, float]]:
        """
        Matches screen candidates using direct Isometric Transformation (Zero-rotation, Uniform Scale).
        Constrained by ZoomSlider strict_scale and terrain alignment when available.
        """
        n_cands = len(candidates)
        if n_cands < 2 or not waypoints:
            return None

        # When target_area_id is known, filter waypoints to that area and its direct neighbors
        if target_area_id is not None:
            valid_ids = {w["id"] for w in waypoints if w.get("area_id") == target_area_id}
            for wid in list(valid_ids):
                for nb_item in neighbors.get(wid, []):
                    valid_ids.add(nb_item[0]["id"])
            target_waypoints = [w for w in waypoints if w["id"] in valid_ids]
            if len(target_waypoints) >= 2:
                waypoints = target_waypoints

        if strict_scale is not None:
            expected_scale = strict_scale
            min_scale = strict_scale * 0.85
            max_scale = strict_scale * 1.15
        elif frame_w:
            fw_ratio = 2560.0 / float(frame_w)
            expected_scale = base_scale * fw_ratio
            min_scale = (base_scale * 0.40 / 1.12) * fw_ratio
            max_scale = (base_scale * 2.10 / 1.12) * fw_ratio
        else:
            expected_scale = base_scale
            min_scale = base_scale * 0.40 / 1.12
            max_scale = base_scale * 2.10 / 1.12

        cand_limit = min(n_cands, cand_limit)
        cand_xy = [(c[0], c[1]) for c in candidates[:cand_limit]]
        best_match = None
        best_score = -99999.0

        edge_idx = self._edge_indices.get(map_id)
        use_fast_index = False
        w1_allowed: Optional[np.ndarray] = None
        if edge_idx is not None and edge_idx["neighbors_ref"] is neighbors and edge_idx["angles"].size > 0:
            id_to_dense = edge_idx["id_to_dense"]
            w1_allowed = np.zeros(edge_idx["n_wps"], dtype=bool)
            all_mapped = True
            for w in waypoints:
                dense_i = id_to_dense.get(w["id"])
                if dense_i is None:
                    all_mapped = False
                    break
                w1_allowed[dense_i] = True
            use_fast_index = all_mapped

        edge_w1 = edge_idx["w1_list"] if use_fast_index else None
        edge_w2 = edge_idx["w2_list"] if use_fast_index else None
        edge_dists = edge_idx["dists"] if use_fast_index else None
        edge_angles = edge_idx["angles"] if use_fast_index else None
        nb_fast_map = edge_idx["nb_fast"] if use_fast_index else None
        nb_id_sets = edge_idx["nb_id_sets"] if use_fast_index else None
        grid_get = edge_idx["grid"].get if use_fast_index else None
        inv_cell = (1.0 / edge_idx["grid_cell"]) if use_fast_index else 0.0
        floor_fn = math.floor
        hyps_3p: Dict[Tuple[int, ...], Tuple[float, List, float, float, float, float]] = {}

        def _iter_edges(ang_tol: float, dist_min: float, dist_max: float, angle_s_val: float):
            if use_fast_index:
                hits = self._query_edge_index(
                    edge_idx, w1_allowed, angle_s_val, ang_tol, dist_min, dist_max
                )
                for hidx in hits:
                    w1 = edge_w1[hidx]
                    yield w1, edge_w2[hidx], float(edge_dists[hidx]), float(edge_angles[hidx]), nb_fast_map[w1["id"]]
            else:
                for w1 in waypoints:
                    nb_list = neighbors.get(w1["id"], [])
                    if not nb_list:
                        continue
                    nb_fast_local = None
                    for w2, _, _, dist_w, angle_w in nb_list:
                        if dist_min <= dist_w <= dist_max:
                            if nb_fast_local is None:
                                nb_fast_local = [
                                    (wc["id"], float(wc["x_pos"]), float(wc["y_pos"]), wc)
                                    for wc, _, _, _, _ in nb_list
                                ]
                            yield w1, w2, dist_w, angle_w, nb_fast_local

        for i in range(cand_limit):
            s1 = candidates[i]
            s1x, s1y = s1[0], s1[1]
            for j in range(i + 1, cand_limit):
                s2 = candidates[j]
                ds_x = s2[0] - s1x
                ds_y = s2[1] - s1y
                dist_s = math.hypot(ds_x, ds_y)
                if dist_s < 55:
                    continue
                angle_s = math.atan2(ds_y, ds_x)

                min_w_dist = min_scale * dist_s
                max_w_dist = max_scale * dist_s

                for w1, w2, dist_w, angle_w, nb_fast_list in _iter_edges(0.048, min_w_dist, max_w_dist, angle_s):
                    scale = dist_w / dist_s
                    diff = abs((angle_w - angle_s + math.pi) % (2 * math.pi) - math.pi)
                    if diff > 0.048:
                        continue

                    w1_id = w1["id"]
                    w1x, w1y = w1["x_pos"], w1["y_pos"]
                    inliers = [(s1, w1), (s2, w2)]
                    used = {w1_id, w2["id"]}
                    box_tol = max(6.0, 12.0 * scale)

                    if use_fast_index:
                        nb_ids = nb_id_sets[w1_id]
                        for k in range(cand_limit):
                            if k == i or k == j:
                                continue
                            skx, sky = cand_xy[k]
                            exp_wx = w1x + (skx - s1x) * scale
                            exp_wy = w1y + (sky - s1y) * scale

                            closest_d = 99999.0
                            closest_w = None
                            gx0 = int(floor_fn((exp_wx - box_tol) * inv_cell))
                            gx1 = int(floor_fn((exp_wx + box_tol) * inv_cell))
                            gy0 = int(floor_fn((exp_wy - box_tol) * inv_cell))
                            gy1 = int(floor_fn((exp_wy + box_tol) * inv_cell))
                            for gx in range(gx0, gx1 + 1):
                                for gy in range(gy0, gy1 + 1):
                                    bucket = grid_get((gx, gy))
                                    if bucket is None:
                                        continue
                                    for wc_id, wc_x, wc_y, wc in bucket:
                                        if wc_id in used or wc_id not in nb_ids:
                                            continue
                                        dx = wc_x - exp_wx
                                        if dx >= box_tol or dx <= -box_tol:
                                            continue
                                        dy = wc_y - exp_wy
                                        if dy >= box_tol or dy <= -box_tol:
                                            continue
                                        d = math.hypot(dx, dy)
                                        if d < closest_d:
                                            closest_d = d
                                            closest_w = wc

                            if closest_d < box_tol and closest_w is not None:
                                inliers.append((candidates[k], closest_w))
                                used.add(closest_w["id"])
                    else:
                        for k in range(cand_limit):
                            if k == i or k == j:
                                continue
                            skx, sky = cand_xy[k]
                            exp_wx = w1x + (skx - s1x) * scale
                            exp_wy = w1y + (sky - s1y) * scale

                            closest_d = 99999.0
                            closest_w = None
                            for wc_id, wc_x, wc_y, wc in nb_fast_list:
                                if wc_id in used:
                                    continue
                                dx = wc_x - exp_wx
                                if dx >= box_tol or dx <= -box_tol:
                                    continue
                                dy = wc_y - exp_wy
                                if dy >= box_tol or dy <= -box_tol:
                                    continue
                                d = math.hypot(dx, dy)
                                if d < closest_d:
                                    closest_d = d
                                    closest_w = wc

                            if closest_d < box_tol and closest_w is not None:
                                inliers.append((candidates[k], closest_w))
                                used.add(closest_w["id"])

                    N = len(inliers)
                    if N >= 3:
                        areas = [w.get("area_id") for _, w in inliers if w.get("area_id")]
                        dom_area = max(set(areas), key=areas.count) if areas else None
                        if areas:
                            dom_ratio = areas.count(dom_area) / len(areas)
                            if N <= 4 and dom_ratio < 1.0:
                                continue
                            if N >= 5 and dom_ratio < 0.75:
                                continue

                        tx = sum(w["x_pos"] - scale * s[0] for s, w in inliers) / N
                        ty = sum(w["y_pos"] - scale * s[1] for s, w in inliers) / N

                        sum_sq_err = 0.0
                        for s, w in inliers:
                            pred_wx = scale * s[0] + tx
                            pred_wy = scale * s[1] + ty
                            sum_sq_err += (pred_wx - w["x_pos"]) ** 2 + (pred_wy - w["y_pos"]) ** 2
                        rmse = math.sqrt(sum_sq_err / N)

                        max_rmse = 4.5 if N >= 4 else (max(3.5, 4.2 * scale) if strict_scale is not None else 2.8)
                        if target_area_id is not None and dom_area == target_area_id and N == 3:
                            max_rmse = max(max_rmse, 4.0)
                        if rmse > max_rmse:
                            continue

                        if strict_scale is None and N == 3 and not (expected_scale * 0.60 <= scale <= expected_scale * 1.70):
                            continue

                        total_white = sum(s[2] for s, w in inliers)
                        scale_dev = abs(scale - expected_scale) / max(1e-6, expected_scale)
                        scale_penalty = scale_dev * (320.0 if strict_scale is not None else 40.0)

                        area_bonus = 0.0
                        if target_area_id is not None:
                            area_bonus = 220.0 if dom_area == target_area_id else -150.0

                        base_score = N * 160.0 + (total_white * 0.08) - (rmse * 5.0) - scale_penalty + area_bonus

                        if N == 3 and strict_scale is not None and frame_gray is not None:
                            tri_key = tuple(sorted(used))
                            prev_h = hyps_3p.get(tri_key)
                            if prev_h is None or base_score > prev_h[0]:
                                hyps_3p[tri_key] = (base_score, inliers, scale, tx, ty, rmse)
                            continue

                        score = base_score
                        if score > best_score:
                            M = np.array([
                                [scale, 0.0],
                                [0.0, scale],
                                [tx, ty],
                            ])
                            best_score = score
                            best_match = (inliers, scale, M, rmse)

                            if (N >= 5 and rmse <= 3.5 and scale_dev <= 0.16) or (N >= 4 and rmse <= 2.4 and scale_dev <= 0.14):
                                return best_match
                            if N == 3 and rmse <= 1.2 and scale_dev <= 0.10 and target_area_id is not None and dom_area == target_area_id:
                                return best_match

        if hyps_3p and (best_match is None or best_score < 700.0):
            top_3p = sorted(hyps_3p.values(), key=lambda x: x[0], reverse=True)[:12]
            for b_sc, inl_3, sc_3, tx_3, ty_3, rmse_3 in top_3p:
                t_bonus = self._score_terrain_alignment(frame_gray, map_id, sc_3, tx_3, ty_3) * 450.0
                total_3 = b_sc + t_bonus
                if total_3 > best_score:
                    best_score = total_3
                    M3 = np.array([[sc_3, 0.0], [0.0, sc_3], [tx_3, ty_3]])
                    best_match = (inl_3, sc_3, M3, rmse_3)

        # Ranked 2-point fallback with terrain alignment disambiguation
        if best_match is None and len(candidates) >= 2 and (target_area_id is not None or allow_2point):
            cand_limit_2 = min(len(candidates), 8)
            hyps_2p: List[Tuple[float, List, float, np.ndarray]] = []
            max_dev_allowed = 0.18 if strict_scale is not None else 0.35
            for i in range(cand_limit_2):
                s1 = candidates[i]
                for j in range(i + 1, cand_limit_2):
                    s2 = candidates[j]
                    ds_x = s2[0] - s1[0]
                    ds_y = s2[1] - s1[1]
                    dist_s = math.hypot(ds_x, ds_y)
                    if dist_s < 75:
                        continue
                    angle_s = math.atan2(ds_y, ds_x)

                    for w1, w2, dist_w, angle_w, _ in _iter_edges(0.035, min_scale * dist_s, max_scale * dist_s, angle_s):
                        scale = dist_w / dist_s
                        diff = abs((angle_w - angle_s + math.pi) % (2 * math.pi) - math.pi)
                        if diff > 0.035:
                            continue

                        if target_area_id is not None:
                            if w1.get("area_id") != target_area_id and w2.get("area_id") != target_area_id:
                                continue

                        scale_dev = abs(scale - expected_scale) / max(1e-6, expected_scale)
                        if scale_dev > max_dev_allowed:
                            continue

                        geom_score = 250.0 - scale_dev * 180.0 - diff * 500.0
                        tx = (w1["x_pos"] - scale * s1[0] + w2["x_pos"] - scale * s2[0]) / 2.0
                        ty = (w1["y_pos"] - scale * s1[1] + w2["y_pos"] - scale * s2[1]) / 2.0
                        M = np.array([[scale, 0.0], [0.0, scale], [tx, ty]])
                        hyps_2p.append((geom_score, [(s1, w1), (s2, w2)], scale, M))

            if hyps_2p:
                hyps_2p.sort(key=lambda x: x[0], reverse=True)
                top_hyps = hyps_2p[:10]
                best_2p_final = None
                best_2p_score = -999999.0
                for g_sc, inl_2p, sc_2p, M_2p in top_hyps:
                    t_sc = 0.0
                    if frame_gray is not None:
                        t_sc = self._score_terrain_alignment(frame_gray, map_id, sc_2p, float(M_2p[2, 0]), float(M_2p[2, 1]))
                    prev_bonus = 0.0
                    if self.last_known_pos is not None and arrow_pos is not None:
                        pwx = sc_2p * arrow_pos[0] + float(M_2p[2, 0])
                        pwy = sc_2p * arrow_pos[1] + float(M_2p[2, 1])
                        dp = math.hypot(pwx - self.last_known_pos[0], pwy - self.last_known_pos[1])
                        if dp <= 800.0:
                            prev_bonus = (800.0 - dp) * 0.25
                    total_sc = g_sc + t_sc * 650.0 + prev_bonus
                    if total_sc > best_2p_score:
                        best_2p_score = total_sc
                        best_2p_final = (inl_2p, sc_2p, M_2p, 0.5)
                if best_2p_final is not None:
                    return best_2p_final

        return best_match

    def _match_single_waypoint_rigid(
        self,
        candidate_pt: Tuple[float, float],
        arrow_pos: Tuple[float, float],
        scale: float,
        waypoints: List[Dict[str, Any]],
        frame_shape: Tuple[int, int],
        target_area_id: Optional[int] = None,
        sub_area_text: str = "",
        strict_scale: Optional[float] = None,
        frame_gray: Optional[np.ndarray] = None,
        map_id: int = 2,
    ) -> Optional[Tuple[List, float, np.ndarray, float]]:
        """
        Disambiguates and resolves world coordinates when ONLY 1 waypoint is visible.
        Uses Negative Evidence, ZoomSlider linear scale, and detail_v2 terrain alignment.
        """
        h, w = frame_shape[:2]
        cx, cy = candidate_pt
        px, py = arrow_pos
        eff_scale = strict_scale if strict_scale is not None else scale

        pool = waypoints
        if target_area_id is not None:
            area_pool = [wp for wp in waypoints if wp.get("area_id") == target_area_id]
            if area_pool:
                pool = area_pool

        valid_hypotheses = []
        for wp in pool:
            tx = wp["x_pos"] - eff_scale * cx
            ty = wp["y_pos"] - eff_scale * cy

            view_min_x = tx
            view_max_x = eff_scale * w + tx
            view_min_y = ty
            view_max_y = eff_scale * h + ty

            margin = 65.0 * eff_scale
            unseen_count = 0
            for other_wp in pool:
                if other_wp["id"] == wp["id"]:
                    continue
                ox, oy = other_wp["x_pos"], other_wp["y_pos"]
                if (view_min_x + margin <= ox <= view_max_x - margin) and (view_min_y + margin <= oy <= view_max_y - margin):
                    unseen_count += 1

            player_wx = eff_scale * px + tx
            player_wy = eff_scale * py + ty

            score = 1000.0 - (unseen_count * 250.0)
            dist_prev = None
            if self.last_known_pos is not None:
                dist_prev = math.hypot(player_wx - self.last_known_pos[0], player_wy - self.last_known_pos[1])
                if dist_prev <= 600.0:
                    score += (600.0 - dist_prev) * 2.0
                else:
                    score -= (dist_prev - 600.0) * 0.5

            valid_hypotheses.append({
                "waypoint": wp,
                "tx": tx,
                "ty": ty,
                "unseen_count": unseen_count,
                "dist_prev": dist_prev,
                "player_world": (player_wx, player_wy),
                "terrain_score": 0.0,
                "score": score,
            })

        if not valid_hypotheses:
            return None

        valid_hypotheses.sort(key=lambda x: x["score"], reverse=True)

        # Evaluate terrain Sobel NCC on top zero/low-unseen hypotheses
        if frame_gray is not None and map_id in self.terrain_data:
            eval_pool = [hyp for hyp in valid_hypotheses if hyp["unseen_count"] <= 1][:12]
            for hyp in eval_pool:
                tsc = self._score_terrain_alignment(frame_gray, map_id, eff_scale, hyp["tx"], hyp["ty"])
                hyp["terrain_score"] = tsc
                hyp["score"] += tsc * 700.0
            valid_hypotheses.sort(key=lambda x: x["score"], reverse=True)

        best = valid_hypotheses[0]
        chosen = None
        if len(valid_hypotheses) == 1 and best["unseen_count"] <= 1:
            chosen = best
        elif self.last_known_pos is not None and best["dist_prev"] is not None and best["dist_prev"] <= 500.0:
            chosen = best
        elif len(valid_hypotheses) > 1:
            second_best = valid_hypotheses[1]
            t_margin = best["terrain_score"] - second_best["terrain_score"]
            if (best["unseen_count"] == 0 and (best["score"] - second_best["score"]) >= 120.0) or (
                best["terrain_score"] >= 0.22 and t_margin >= 0.07 and best["unseen_count"] <= 1
            ):
                chosen = best
            else:
                logger.warning("1-Waypoint matching: Ambiguous candidates. False positive rejected.")
                return None
        elif best["unseen_count"] == 0:
            chosen = best
        else:
            logger.warning(f"1-Waypoint matching: Ambiguity unresolved ({len(valid_hypotheses)} candidates).")
            return None

        M = np.array([
            [eff_scale, 0.0],
            [0.0, eff_scale],
            [chosen["tx"], chosen["ty"]],
        ])
        inliers = [((cx, cy, 500), chosen["waypoint"])]
        return (inliers, eff_scale, M, 0.0)

    def _match_zero_waypoint_terrain(
        self,
        frame_gray: Optional[np.ndarray],
        map_id: int,
        scale: float,
        target_area_id: Optional[int] = None,
    ) -> Optional[Tuple[List, float, np.ndarray, float]]:
        """
        Fallback when 0 waypoints are detected on screen (e.g. ocean/wilderness) using known region/prior
        bounds and ZoomSlider scale against official detail_v2 terrain tiles.
        """
        if frame_gray is None or frame_gray.size == 0 or map_id not in self.terrain_data or scale <= 0:
            return None

        canvas, meta = self.terrain_data[map_id]
        tx0, ty0, z, orig_x, orig_y = [float(v) for v in meta]
        pow2z = 2.0 ** z
        ch_h, ch_w = canvas.shape[:2]

        fh, fw = frame_gray.shape[:2]
        rx0, rx1 = int(0.16 * fw), int(0.84 * fw)
        ry0, ry1 = int(0.14 * fh), int(0.86 * fh)
        crop = frame_gray[ry0:ry1, rx0:rx1]
        if crop.size == 0 or float(np.std(crop)) < 8.0:
            return None

        cw = max(24, int(round((rx1 - rx0) * scale * pow2z)))
        ch = max(24, int(round((ry1 - ry0) * scale * pow2z)))
        if cw >= ch_w or ch >= ch_h:
            return None

        # Whole-Teyvat coarse-to-fine fallback when neither OCR region nor prior position is available
        if target_area_id is None and map_id == 2 and self.last_known_pos is None:
            cached_coarse = self._terrain_coarse_cache.get(map_id)
            if cached_coarse is None:
                ds = 0.5
                c_small = cv2.resize(
                    canvas,
                    (max(32, int(round(ch_w * ds))), max(32, int(round(ch_h * ds)))),
                    interpolation=cv2.INTER_AREA,
                )
                rg_small = cv2.magnitude(
                    cv2.Sobel(c_small, cv2.CV_32F, 1, 0, ksize=3),
                    cv2.Sobel(c_small, cv2.CV_32F, 0, 1, ksize=3),
                )
                cached_coarse = (c_small, rg_small, ds)
                self._terrain_coarse_cache[map_id] = cached_coarse
            c_small, rg_small, ds = cached_coarse
            cw_s = max(16, int(round(cw * ds)))
            ch_s = max(16, int(round(ch * ds)))
            if cw_s >= c_small.shape[1] - 4 or ch_s >= c_small.shape[0] - 4:
                return None
            q_small = cv2.resize(crop, (cw_s, ch_s), interpolation=cv2.INTER_AREA)
            qg_small = cv2.magnitude(
                cv2.Sobel(q_small, cv2.CV_32F, 1, 0, ksize=3),
                cv2.Sobel(q_small, cv2.CV_32F, 0, 1, ksize=3),
            )
            res_c = cv2.matchTemplate(rg_small, qg_small, cv2.TM_CCOEFF_NORMED)
            nz_c = (c_small > 2).astype(np.float32)
            nz_frac_c = cv2.boxFilter(nz_c, -1, (cw_s, ch_s), normalize=True)
            cf = c_small.astype(np.float32)
            mean_c = cv2.boxFilter(cf, -1, (cw_s, ch_s), normalize=True)
            std_c = np.sqrt(np.maximum(0.0, cv2.boxFilter(cf * cf, -1, (cw_s, ch_s), normalize=True) - mean_c * mean_c))
            hc_s, wc_s = ch_s // 2, cw_s // 2
            rh_c, rw_c = res_c.shape[:2]
            valid_c = (nz_frac_c[hc_s:hc_s + rh_c, wc_s:wc_s + rw_c] >= 0.85) & (std_c[hc_s:hc_s + rh_c, wc_s:wc_s + rw_c] >= 8.0)
            res_c_clean = np.where(valid_c, res_c, -1.0)
            _, peak_c, _, peak_loc_c = cv2.minMaxLoc(res_c_clean)
            if peak_c < 0.34:
                return None
            res_c_sup = res_c_clean.copy()
            res_c_sup[max(0, peak_loc_c[1] - 20):min(rh_c, peak_loc_c[1] + 20), max(0, peak_loc_c[0] - 20):min(rw_c, peak_loc_c[0] + 20)] = -1.0
            _, second_c, _, _ = cv2.minMaxLoc(res_c_sup)
            if (peak_c - second_c) < 0.10:
                return None
            bx = float(peak_loc_c[0]) / ds
            by = float(peak_loc_c[1]) / ds
            wx_rx0 = (bx + tx0 * 256.0) / pow2z - orig_x
            wy_ry0 = (by + ty0 * 256.0) / pow2z - orig_y
            tx = wx_rx0 - scale * rx0
            ty = wy_ry0 - scale * ry0
            tx_ref, ty_ref, ok_ref = self._refine_transform_with_terrain(
                frame_gray, map_id, scale, tx, ty, search_pad=16, is_underground=False
            )
            if ok_ref:
                tx, ty = tx_ref, ty_ref
            M = np.array([[scale, 0.0], [0.0, scale], [tx, ty]])
            return ([], scale, M, 0.0)

        # Determine search bounds in canvas coordinates
        if target_area_id is None and map_id == 2 and self.last_known_pos is not None:
            wx_min = self.last_known_pos[0] - 1600.0
            wx_max = self.last_known_pos[0] + 1600.0
            wy_min = self.last_known_pos[1] - 1600.0
            wy_max = self.last_known_pos[1] + 1600.0
        else:
            bbox = self._area_bboxes.get((map_id, target_area_id)) or self._area_bboxes.get((map_id, None))
            if not bbox:
                return None
            wx_min, wx_max, wy_min, wy_max = bbox[0] - 700.0, bbox[1] + 700.0, bbox[2] - 700.0, bbox[3] + 700.0

        cx0 = max(0, int(math.floor((wx_min + orig_x) * pow2z - tx0 * 256.0)))
        cy0 = max(0, int(math.floor((wy_min + orig_y) * pow2z - ty0 * 256.0)))
        cx1 = min(ch_w, int(math.ceil((wx_max + orig_x) * pow2z - tx0 * 256.0)))
        cy1 = min(ch_h, int(math.ceil((wy_max + orig_y) * pow2z - ty0 * 256.0)))

        if cx1 - cx0 <= cw + 8 or cy1 - cy0 <= ch + 8:
            return None

        roi = canvas[cy0:cy1, cx0:cx1]
        use_coarse = max(roi.shape[0], roi.shape[1]) > 360 and cw >= 48 and ch >= 48
        ds = 0.5 if use_coarse else 1.0
        if use_coarse:
            roi_w_s = max(16, int(round(roi.shape[1] * ds)))
            roi_h_s = max(16, int(round(roi.shape[0] * ds)))
            cw_s = max(16, int(round(cw * ds)))
            ch_s = max(16, int(round(ch * ds)))
            if roi_w_s <= cw_s + 4 or roi_h_s <= ch_s + 4:
                use_coarse = False
                ds = 1.0
        if use_coarse:
            roi_work = cv2.resize(roi, (roi_w_s, roi_h_s), interpolation=cv2.INTER_AREA)
            q_work = cv2.resize(crop, (cw_s, ch_s), interpolation=cv2.INTER_AREA)
            kw, kh = cw_s, ch_s
        else:
            roi_work = roi
            q_work = cv2.resize(crop, (cw, ch), interpolation=cv2.INTER_AREA)
            kw, kh = cw, ch

        qg = cv2.magnitude(cv2.Sobel(q_work, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(q_work, cv2.CV_32F, 0, 1, ksize=3))
        rg = cv2.magnitude(cv2.Sobel(roi_work, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(roi_work, cv2.CV_32F, 0, 1, ksize=3))

        res = cv2.matchTemplate(rg, qg, cv2.TM_CCOEFF_NORMED)

        # Mask out padding/empty tiles
        nz = (roi_work > 2).astype(np.float32)
        nz_frac = cv2.boxFilter(nz, -1, (kw, kh), normalize=True)
        roi_f = roi_work.astype(np.float32)
        mean_r = cv2.boxFilter(roi_f, -1, (kw, kh), normalize=True)
        std_r = np.sqrt(np.maximum(0.0, cv2.boxFilter(roi_f * roi_f, -1, (kw, kh), normalize=True) - mean_r * mean_r))

        hc, wc = kh // 2, kw // 2
        rh, rw = res.shape[:2]
        valid = (nz_frac[hc:hc + rh, wc:wc + rw] >= 0.85) & (std_r[hc:hc + rh, wc:wc + rw] >= 8.0)
        res_clean = np.where(valid, res, -1.0)

        _, peak, _, peak_loc = cv2.minMaxLoc(res_clean)
        if peak < 0.24:
            return None

        res_sup = res_clean.copy()
        sup_r = max(12, int(round(40 * ds)))
        sy0, sy1 = max(0, peak_loc[1] - sup_r), min(rh, peak_loc[1] + sup_r)
        sx0, sx1 = max(0, peak_loc[0] - sup_r), min(rw, peak_loc[0] + sup_r)
        res_sup[sy0:sy1, sx0:sx1] = -1.0
        _, second_peak, _, _ = cv2.minMaxLoc(res_sup)

        if (peak - second_peak) < 0.07:
            return None

        best_cx = float(cx0) + float(peak_loc[0]) / ds
        best_cy = float(cy0) + float(peak_loc[1]) / ds
        wx_rx0 = (best_cx + tx0 * 256.0) / pow2z - orig_x
        wy_ry0 = (best_cy + ty0 * 256.0) / pow2z - orig_y
        tx = wx_rx0 - scale * rx0
        ty = wy_ry0 - scale * ry0

        if use_coarse:
            tx_ref, ty_ref, ok_ref = self._refine_transform_with_terrain(
                frame_gray, map_id, scale, tx, ty, search_pad=12, is_underground=False
            )
            if ok_ref:
                tx, ty = tx_ref, ty_ref

        M = np.array([[scale, 0.0], [0.0, scale], [tx, ty]])
        return ([], scale, M, 0.0)

    def _resolve_exact_hoyolab_floor(
        self,
        frame: np.ndarray,
        map_id: int,
        world_x: float,
        world_y: float,
        scale: float,
        tx: float,
        ty: float,
        layer_info: Dict[str, Any],
        inliers: List[Any],
        frame_v_u8: Optional[np.ndarray] = None,
    ) -> Dict[str, Any]:
        """
        Deterministically resolves exact HoYoLAB (group_id, floor_id, floor_short, hoyolab_floor_name)
        using Cave Alpha-Mask Illumination Contrast (InV - OutV), elevator slot count & index,
        and inlier point_group hints.
        """
        if not layer_info.get("is_underground"):
            return {
                "is_hoyolab_underground": False,
                "group_id": None,
                "group_name": None,
                "floor_id": None,
                "floor_short": "G",
                "floor_name": "G",
                "hoyolab_floor_name": "G",
            }

        groups = self.map_point_groups.get(map_id) or [
            g for g in self.underground_groups if g.get("map_id", 2) == map_id
        ]
        if not groups:
            return {
                "is_hoyolab_underground": False,
                "group_id": None,
                "group_name": None,
                "floor_id": None,
                "floor_short": "G",
                "floor_name": "G",
                "hoyolab_floor_name": "G",
            }

        inlier_group_ids = set()
        inlier_floor_ids = set()
        for _, w in inliers:
            pg = w.get("point_group")
            if isinstance(pg, dict) and pg.get("group_id") is not None:
                inlier_group_ids.add(int(pg["group_id"]))
                if pg.get("floor_id") is not None:
                    inlier_floor_ids.add(int(pg["floor_id"]))

        cand_groups: List[Dict[str, Any]] = []
        for g in groups:
            gid = int(g.get("id", 0))
            in_bbox = False
            for fl in g.get("floors", []):
                ov = fl.get("overlay")
                if not ov:
                    continue
                if (ov["minLng"] - 40.0 <= world_x <= ov["maxLng"] + 40.0) and (
                    ov["minLat"] - 40.0 <= world_y <= ov["maxLat"] + 40.0
                ):
                    in_bbox = True
                    break
            if in_bbox:
                cand_groups.append(g)

        if not cand_groups:
            return {
                "is_hoyolab_underground": False,
                "group_id": None,
                "group_name": None,
                "floor_id": None,
                "floor_short": "G",
                "floor_name": "G",
                "hoyolab_floor_name": "G",
            }

        h, w = frame.shape[:2]
        total_floors = int(layer_info.get("total_floors") or 1)
        active_idx = int(layer_info.get("active_floor_idx") or 0)
        surface_idx = int(layer_info.get("surface_idx") or 0)
        target_f_idx = (active_idx - 1) if surface_idx == 0 else active_idx
        target_short = (layer_info.get("floor_name") or "").strip().upper()

        best_choice: Optional[Tuple[float, Dict[str, Any], Dict[str, Any]]] = None
        for g in cand_groups:
            gid = int(g["id"])
            floors = g.get("floors", [])
            if not floors:
                continue

            expected_total = len(floors) + 1
            diff_total = abs(expected_total - total_floors)
            group_score = 350.0 if diff_total == 0 else (-120.0 * diff_total)
            if gid in inlier_group_ids:
                group_score += 200.0

            for f_idx, fl in enumerate(floors):
                ov = fl.get("overlay")
                if not ov:
                    continue
                fid = int(fl["id"])
                mask_key = f"{map_id}_{gid}_{fid}"
                alpha = self.cave_masks.get(mask_key)

                player_in_floor = False
                u_p = -1
                v_p = -1
                mw = mh = 0
                if alpha is not None and ov["r_x"] != ov["l_x"] and ov["r_y"] != ov["l_y"]:
                    mh, mw = alpha.shape
                    u_p = int(round((world_x - ov["l_x"]) / (ov["r_x"] - ov["l_x"]) * mw))
                    v_p = int(round((world_y - ov["l_y"]) / (ov["r_y"] - ov["l_y"]) * mh))
                    if 0 <= u_p < mw and 0 <= v_p < mh:
                        patch_p = alpha[max(0, v_p - 4):min(mh, v_p + 5), max(0, u_p - 4):min(mw, u_p + 5)]
                        player_in_floor = bool(np.any(patch_p > 0))
                else:
                    player_in_floor = (ov["minLng"] <= world_x <= ov["maxLng"]) and (ov["minLat"] <= world_y <= ov["maxLat"])

                if not player_in_floor:
                    continue

                score = group_score + 220.0
                if f_idx == target_f_idx:
                    score += 260.0
                if fid in inlier_floor_ids:
                    score += 80.0

                fs = (fl.get("floor_short") or fl.get("floor_name_short") or "").strip().upper()
                if fs and target_short and fs == target_short:
                    score += 120.0

                if alpha is not None and ov["r_x"] != ov["l_x"] and ov["r_y"] != ov["l_y"]:
                    # Illumination contrast InV - OutV on visible overlay bbox (ROI-only V channel)
                    x0 = max(int(0.05 * w), int(round((ov["minLng"] - tx) / scale)))
                    x1 = min(int(0.93 * w), int(round((ov["maxLng"] - tx) / scale)))
                    y0 = max(int(0.06 * h), int(round((ov["minLat"] - ty) / scale)))
                    y1 = min(int(0.92 * h), int(round((ov["maxLat"] - ty) / scale)))
                    if frame_v_u8 is not None:
                        patch_v = frame_v_u8[y0:y1, x0:x1].astype(np.float32)
                    else:
                        patch_v = np.max(frame[y0:y1, x0:x1], axis=2).astype(np.float32)
                    if patch_v.shape[0] >= 8 and patch_v.shape[1] >= 8:
                        xs = np.clip(((np.arange(x0, x1) * scale + tx - ov["l_x"]) / (ov["r_x"] - ov["l_x"]) * mw).astype(np.int32), 0, mw - 1)
                        ys = np.clip(((np.arange(y0, y1) * scale + ty - ov["l_y"]) / (ov["r_y"] - ov["l_y"]) * mh).astype(np.int32), 0, mh - 1)
                        m_sub = alpha[ys[:, None], xs[None, :]] > 0
                        if m_sub.sum() >= 15 and (~m_sub).sum() >= 15:
                            delta = float(patch_v[m_sub].mean() - patch_v[~m_sub].mean())
                            score += 8.0 * delta

                if best_choice is None or score > best_choice[0]:
                    best_choice = (score, g, fl)

        if best_choice is None:
            return {
                "is_hoyolab_underground": False,
                "group_id": None,
                "group_name": None,
                "floor_id": None,
                "floor_short": "G",
                "floor_name": "G",
                "hoyolab_floor_name": "G",
            }

        _, g_best, fl_best = best_choice
        fs_best = (fl_best.get("floor_short") or fl_best.get("floor_name_short") or target_short or "").strip()
        fn_best = (fl_best.get("floor_name") or "").strip()
        return {
            "is_hoyolab_underground": True,
            "group_id": int(g_best["id"]),
            "group_name": g_best.get("name") or fn_best or f"Group {g_best['id']}",
            "floor_id": int(fl_best["id"]),
            "floor_short": fs_best or fn_best or target_short,
            "floor_name": fs_best or fn_best or target_short,
            "hoyolab_floor_name": fn_best or fs_best or target_short,
        }

    def locate_player(
        self,
        frame: np.ndarray,
        debug: bool = False,
        slider_res: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Universal Multi-Map Landmark & Terrain Locator.
        Automatically identifies the player's exact region, submap, zoom scale, and underground floor.
        """
        if frame is None or frame.size == 0:
            return None

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        arrow_res = self.detect_player_arrow(frame, hsv=hsv)
        if not arrow_res:
            if debug:
                return {
                    "detected": False,
                    "has_world_coords": False,
                    "reason": "Không tìm thấy mũi tên người chơi (Player Arrow) trên màn hình.",
                }
            return None

        px, py, angle = arrow_res
        layer_info = self.detect_layers_universal(frame)
        if slider_res is None:
            slider_res = ZoomSliderDetector.detect_slider(frame)
        frame_w = frame.shape[1]
        zoom_t = slider_res["ratio_t"] if slider_res else None
        known_scale_m2 = ZoomSliderDetector.get_map_scale(slider_res, map_id=2, frame_w=frame_w) if slider_res else None

        candidates = self.detect_waypoint_candidates(frame, arrow_pos=(px, py), hsv=hsv)
        if not self.maps:
            res_dict = {
                "detected": True,
                "has_world_coords": False,
                "pixel_x": px,
                "pixel_y": py,
                "heading": angle,
                "is_underground": layer_info["is_underground"],
                "in_game_is_underground": bool(layer_info["is_underground"]),
                "floor_idx": layer_info["active_floor_idx"],
                "total_floors": layer_info["total_floors"],
                "active_floor_idx": layer_info["active_floor_idx"],
                "surface_idx": layer_info["surface_idx"],
                "floor_name": layer_info["floor_name"],
                "floor_short": layer_info["floor_name"],
                "has_layers": layer_info["has_layers"],
                "zoom_ratio_t": zoom_t,
            }
            if debug:
                res_dict["reason"] = "Không có cơ sở dữ liệu bản đồ."
                res_dict["candidates"] = []
                res_dict["slider_res"] = slider_res
            return res_dict

        target_map_id, target_area_id, region_text = self.detect_region_name(frame)
        if target_map_id or target_area_id:
            logger.info(
                f"Decisive region detection: '{region_text}' -> Map {target_map_id}, Area {target_area_id} "
                f"({self.area_names_map.get(target_area_id) if target_area_id else 'Sub-map'})"
            )

        best_candidate_match = None
        best_score = -99999.0

        map_config = {
            2: {"base_scale": 1.12, "max_scale_dev": 0.45},
            7: {"base_scale": 0.49, "max_scale_dev": 0.28},
            9: {"base_scale": 0.75, "max_scale_dev": 0.20},
            34: {"base_scale": 0.60, "max_scale_dev": 0.25},
            36: {"base_scale": 0.80, "max_scale_dev": 0.35},
            37: {"base_scale": 0.60, "max_scale_dev": 0.25},
            40: {"base_scale": 0.62, "max_scale_dev": 0.35},
        }

        # Filter helpers for primary-only vs all landmarks
        def _primary_pool(wps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            p = [w for w in wps if w.get("is_primary", True)]
            return p if len(p) >= 2 else wps

        # Case 0: ONLY 1 waypoint visible + Known Scale from Zoom Slider
        if len(candidates) == 1:
            active_mid = target_map_id if (target_map_id and target_map_id in self.maps) else 2
            strict_s = ZoomSliderDetector.get_map_scale(slider_res, map_id=active_mid, frame_w=frame_w) if slider_res else None
            if strict_s is not None and active_mid in self.maps:
                mdata = self.maps[active_mid]
                res_1p = self._match_single_waypoint_rigid(
                    candidate_pt=(candidates[0][0], candidates[0][1]),
                    arrow_pos=(px, py),
                    scale=strict_s,
                    waypoints=_primary_pool(mdata["waypoints"]),
                    frame_shape=frame.shape,
                    target_area_id=target_area_id,
                    sub_area_text=region_text,
                    strict_scale=strict_s,
                    frame_gray=frame_gray,
                    map_id=active_mid,
                )
                if res_1p:
                    best_candidate_match = (mdata, res_1p[0], res_1p[1], res_1p[2], res_1p[3])

        is_ug_hint = bool(layer_info.get("is_underground", False))

        # Case A: OCR detected an isolated sub-map (e.g. Chasm, Enkanomiya, Remuria, Sacred Mountain, Moon Plateau)
        if not best_candidate_match and len(candidates) >= 2 and target_map_id and target_map_id != 2 and target_map_id in self.maps:
            mdata = self.maps[target_map_id]
            strict_sub = ZoomSliderDetector.get_map_scale(slider_res, map_id=target_map_id, frame_w=frame_w) if slider_res else None
            cfg = map_config.get(target_map_id, {"base_scale": 0.75})
            base_s = strict_sub if strict_sub is not None else cfg["base_scale"] * (2560.0 / float(frame_w))
            for lim in (12, 18) if len(candidates) > 12 else (12,):
                res = self._match_single_map(
                    candidates, mdata["waypoints"], mdata["neighbors"],
                    cand_limit=lim, frame_w=frame_w, base_scale=base_s,
                    target_area_id=None, arrow_pos=(px, py), allow_2point=True,
                    strict_scale=strict_sub, frame_gray=frame_gray, map_id=target_map_id,
                    is_underground=is_ug_hint,
                )
                if res:
                    best_candidate_match = (mdata, res[0], res[1], res[2], res[3])
                    break

        # Case B: Map 2 (Teyvat)
        if not best_candidate_match and len(candidates) >= 2 and (target_map_id == 2 or target_map_id is None) and 2 in self.maps:
            m2 = self.maps[2]
            strict_m2 = known_scale_m2
            base_m2 = strict_m2 if strict_m2 is not None else 1.12
            prim_wps = _primary_pool(m2["waypoints"])

            # At very zoomed-out views (t <= 0.12), Genshin hides blue waypoints and displays only Statues & Domains
            if zoom_t is not None and zoom_t <= 0.12:
                major_wps = [w for w in prim_wps if int(w.get("label_id") or 0) in (2, 154)]
                if len(major_wps) >= 2:
                    res2 = self._match_single_map(
                        candidates, major_wps, m2["neighbors"],
                        cand_limit=26, frame_w=frame_w, base_scale=base_m2,
                        target_area_id=target_area_id, arrow_pos=(px, py),
                        allow_2point=(target_area_id is not None),
                        strict_scale=strict_m2, frame_gray=frame_gray, map_id=2,
                        is_underground=is_ug_hint,
                    )
                    if res2:
                        best_candidate_match = (m2, res2[0], res2[1], res2[2], res2[3])

            if not best_candidate_match:
                for lim in (12, 18) if len(candidates) > 12 else (12,):
                    res2 = self._match_single_map(
                        candidates, prim_wps, m2["neighbors"],
                        cand_limit=lim, frame_w=frame_w, base_scale=base_m2,
                        target_area_id=target_area_id, arrow_pos=(px, py),
                        allow_2point=(target_area_id is not None),
                        strict_scale=strict_m2, frame_gray=frame_gray, map_id=2,
                        is_underground=is_ug_hint,
                    )
                    if res2:
                        best_candidate_match = (m2, res2[0], res2[1], res2[2], res2[3])
                        break

            # Secondary landmarks retry if primary failed while region is confirmed (e.g. 100% zoom or cities)
            if not best_candidate_match and len(m2["waypoints"]) > len(prim_wps):
                res2 = self._match_single_map(
                    candidates, m2["waypoints"], m2["neighbors"],
                    cand_limit=14, frame_w=frame_w, base_scale=base_m2,
                    target_area_id=target_area_id, arrow_pos=(px, py),
                    allow_2point=(target_area_id is not None),
                    strict_scale=strict_m2, frame_gray=frame_gray, map_id=2,
                    is_underground=is_ug_hint,
                )
                if res2:
                    best_candidate_match = (m2, res2[0], res2[1], res2[2], res2[3])

        # Case C: Fallback to isolated submaps when OCR gave no region/map hint
        if not best_candidate_match and len(candidates) >= 2 and target_map_id is None and target_area_id is None:
            for mid, mdata in self.maps.items():
                if mid == 2:
                    continue
                strict_sub = ZoomSliderDetector.get_map_scale(slider_res, map_id=mid, frame_w=frame_w) if slider_res else None
                cfg = map_config.get(mid, {"base_scale": 0.75})
                base_s = strict_sub if strict_sub is not None else cfg["base_scale"]
                res = self._match_single_map(
                    candidates, mdata["waypoints"], mdata["neighbors"],
                    cand_limit=10, frame_w=frame_w, base_scale=base_s,
                    target_area_id=None, arrow_pos=(px, py), allow_2point=False,
                    strict_scale=strict_sub, frame_gray=frame_gray, map_id=mid,
                    is_underground=is_ug_hint,
                )
                if res is not None:
                    inliers, scale, M, rmse = res
                    score = len(inliers) * 150.0 - (rmse * 5.0)
                    if score > best_score:
                        best_score = score
                        best_candidate_match = (mdata, inliers, scale, M, rmse)
                        break

        # Surface Terrain Head-to-Head Guard: when a low-inlier (<= 4) surface match has weak
        # Sobel terrain correlation (< 0.24), test terrain-only localization and override ONLY
        # if terrain-only finds a decisively superior match (>= 0.40 and >= +0.20 margin).
        # This rejects false roof/snow coincidences (e.g. Kitezhgrad Cũ) while preserving real foggy matches.
        if (
            best_candidate_match is not None
            and not is_ug_hint
            and slider_res is not None
        ):
            _mdata, _inl, _sc, _M, _rmse = best_candidate_match
            if len(_inl) <= 4 and _mdata["id"] in self.terrain_data:
                _t_sc = self._score_terrain_alignment(
                    frame_gray, _mdata["id"], float(_sc), float(_M[2, 0]), float(_M[2, 1])
                )
                if _t_sc < 0.24:
                    _fb_mid = target_map_id if (target_map_id and target_map_id in self.maps) else _mdata["id"]
                    _fb_scale = ZoomSliderDetector.get_map_scale(slider_res, map_id=_fb_mid, frame_w=frame_w)
                    if _fb_scale is not None:
                        _alt_0p = self._match_zero_waypoint_terrain(
                            frame_gray, _fb_mid, _fb_scale, target_area_id=target_area_id
                        )
                        if _alt_0p is not None:
                            _alt_t = self._score_terrain_alignment(
                                frame_gray, _fb_mid, float(_alt_0p[1]), float(_alt_0p[2][2, 0]), float(_alt_0p[2][2, 1])
                            )
                            if _alt_t >= 0.40 and _alt_t >= _t_sc + 0.20:
                                logger.warning(
                                    f"Overriding low-terrain surface match on Map {_mdata['id']} "
                                    f"(inliers={len(_inl)}, ncc={_t_sc:.3f}) with terrain lock (ncc={_alt_t:.3f})."
                                )
                                best_candidate_match = (self.maps[_fb_mid], _alt_0p[0], _alt_0p[1], _alt_0p[2], _alt_0p[3])

        # Case D: Zero-waypoint terrain-only fallback when slider + region/prior bounds exist
        if not best_candidate_match and slider_res is not None:
            fallback_mid = target_map_id if (target_map_id and target_map_id in self.maps) else (2 if 2 in self.maps else None)
            if fallback_mid is not None:
                fb_scale = ZoomSliderDetector.get_map_scale(slider_res, map_id=fallback_mid, frame_w=frame_w)
                if fb_scale is not None:
                    res_0p = self._match_zero_waypoint_terrain(frame_gray, fallback_mid, fb_scale, target_area_id=target_area_id)
                    if res_0p:
                        mdata = self.maps[fallback_mid]
                        best_candidate_match = (mdata, res_0p[0], res_0p[1], res_0p[2], res_0p[3])

        if best_candidate_match:
            mdata, inliers, scale, M, rmse = best_candidate_match
            tx = float(M[2, 0])
            ty = float(M[2, 1])
            scale_ui = frame.shape[0] / 1440.0

            if len(inliers) >= 1:
                adj_inliers = self._compensate_inliers_screen_anchors(inliers, scale_ui)
                if len(adj_inliers) >= 2:
                    lsq = self._fit_isometric_lsq(adj_inliers)
                    if lsq is not None and abs(lsq[0] - scale) / max(1e-6, scale) <= 0.06:
                        scale, tx, ty, rmse = lsq
                    else:
                        tx = sum(w["x_pos"] - scale * s[0] for s, w in adj_inliers) / len(adj_inliers)
                        ty = sum(w["y_pos"] - scale * s[1] for s, w in adj_inliers) / len(adj_inliers)
                else:
                    s0, w0 = adj_inliers[0]
                    tx = float(w0["x_pos"]) - scale * float(s0[0])
                    ty = float(w0["y_pos"]) - scale * float(s0[1])

                if mdata["id"] in self.WORLD_ANCHOR_MAP_IDS:
                    ty += self.WORLD_ANCHOR_Y_OFFSET

                # Surface-only residual Sobel refinement (guard prevents surface edges from pulling caves)
                tx_ref, ty_ref, terr_ok = self._refine_transform_with_terrain(
                    frame_gray,
                    mdata["id"],
                    scale,
                    tx,
                    ty,
                    is_underground=bool(layer_info.get("is_underground", False)),
                )
                if terr_ok:
                    tx, ty = tx_ref, ty_ref

                M = np.array([[scale, 0.0], [0.0, scale], [tx, ty]])

            pw = np.array([px, py, 1.0]) @ M
            world_x = float(pw[0])
            world_y = float(pw[1])

            area_id = None
            area_name = None
            if mdata["id"] == 2:
                area_ids = [w.get("area_id") for s, w in inliers if w.get("area_id")]
                if area_ids:
                    area_id = max(set(area_ids), key=area_ids.count)
                elif target_area_id is not None:
                    area_id = target_area_id
                elif mdata.get("waypoints"):
                    nearest_wp = min(
                        mdata["waypoints"],
                        key=lambda w: (w["x_pos"] - world_x) ** 2 + (w["y_pos"] - world_y) ** 2,
                    )
                    area_id = nearest_wp.get("area_id")
                if area_id:
                    area_name = self.area_names_map.get(area_id, f"Area {area_id}")

            floor_resolved = self._resolve_exact_hoyolab_floor(
                frame=frame,
                map_id=mdata["id"],
                world_x=world_x,
                world_y=world_y,
                scale=scale,
                tx=tx,
                ty=ty,
                layer_info=layer_info,
                inliers=inliers,
                frame_v_u8=hsv[:, :, 2],
            )

            logger.info(
                f"Multi-Map localization SUCCESS: Matched Map {mdata['id']} ({mdata['name']}) "
                f"with {len(inliers)} waypoints (RMSE={rmse:.1f})! World=({world_x:.1f}, {world_y:.1f})"
            )
            self.last_known_pos = (world_x, world_y)
            self.last_map_id = mdata["id"]

            res_payload = {
                "detected": True,
                "has_world_coords": True,
                "map_id": mdata["id"],
                "map_name": mdata["name"],
                "area_id": area_id,
                "area_name": area_name,
                "world_x": world_x,
                "world_y": world_y,
                "lat": world_y,
                "lng": world_x,
                "pixel_x": px,
                "pixel_y": py,
                "heading": angle,
                "scale": scale,
                "inliers_count": len(inliers),
                "rmse": rmse,
                "is_underground": bool(floor_resolved.get("is_hoyolab_underground", False)) if layer_info["is_underground"] else False,
                "in_game_is_underground": bool(layer_info["is_underground"]),
                "floor_idx": layer_info["active_floor_idx"],
                "total_floors": layer_info["total_floors"],
                "active_floor_idx": layer_info["active_floor_idx"],
                "surface_idx": layer_info["surface_idx"],
                "floor_name": floor_resolved["floor_name"],
                "floor_short": floor_resolved["floor_short"],
                "hoyolab_floor_name": floor_resolved["hoyolab_floor_name"],
                "group_id": floor_resolved["group_id"],
                "group_name": floor_resolved["group_name"],
                "floor_id": floor_resolved["floor_id"],
                "has_layers": layer_info["has_layers"],
                "zoom_ratio_t": zoom_t,
            }
            if debug:
                res_payload["candidates"] = [{"x": float(c[0]), "y": float(c[1]), "r": float(c[2])} for c in candidates]
                res_payload["inliers"] = [
                    {
                        "pixel_x": float(s[0]),
                        "pixel_y": float(s[1]),
                        "waypoint_id": w.get("id"),
                        "world_x": w.get("x_pos"),
                        "world_y": w.get("y_pos"),
                        "name": w.get("name", f"WP_{w.get('id')}"),
                    }
                    for s, w in inliers
                ]
                res_payload["slider_res"] = slider_res
                res_payload["region_text"] = region_text
                res_payload["target_map_id"] = target_map_id
                res_payload["target_area_id"] = target_area_id
                res_payload["slot_types"] = layer_info.get("slot_types", [])
                res_payload["reason"] = f"Định vị thành công Map {mdata['id']} ({mdata['name']}) qua {len(inliers)} mốc."
            return res_payload

        fail_payload = {
            "detected": True,
            "has_world_coords": False,
            "pixel_x": px,
            "pixel_y": py,
            "heading": angle,
            "is_underground": layer_info["is_underground"],
            "floor_idx": layer_info["active_floor_idx"],
            "total_floors": layer_info["total_floors"],
            "active_floor_idx": layer_info["active_floor_idx"],
            "surface_idx": layer_info["surface_idx"],
            "floor_name": layer_info["floor_name"],
            "floor_short": layer_info["floor_name"],
            "has_layers": layer_info["has_layers"],
            "zoom_ratio_t": zoom_t,
        }
        if debug:
            fail_payload["candidates"] = [{"x": float(c[0]), "y": float(c[1]), "r": float(c[2])} for c in candidates]
            fail_payload["slider_res"] = slider_res
            fail_payload["region_text"] = region_text
            fail_payload["target_map_id"] = target_map_id
            fail_payload["target_area_id"] = target_area_id
            fail_payload["slot_types"] = layer_info.get("slot_types", [])
            fail_payload["reason"] = (
                f"Phát hiện {len(candidates)} waypoint nhưng không tìm thấy cấu hình mốc khớp với cơ sở dữ liệu."
            )
        return fail_payload

    @staticmethod
    def _extract_floor_label(fl: Mapping[str, Any], f_idx: int) -> str:
        fs = str(fl.get("floor_short") or fl.get("floor_name_short") or "").strip().upper()
        fn = str(fl.get("floor_name") or "").strip()
        return fs or fn or f"B{f_idx + 1}"

    def resolve_floor_label(self, map_id: int, floor_id: Any) -> str:
        """Resolves a HoYoLAB `floor_id` into its short human label (`B1`, `B2`, ...)."""
        if floor_id is None or isinstance(floor_id, bool):
            return ""
        fid_str = str(floor_id).strip()
        if not fid_str or fid_str == "0":
            return ""
        mid = int(map_id)
        cached = self._floor_label_index.get((mid, fid_str))
        if cached:
            return cached
        for g in self.map_point_groups.get(mid, ()):
            for f_idx, fl in enumerate(g.get("floors", ())):
                fl_id = fl.get("id")
                if fl_id is not None and str(fl_id).strip() == fid_str:
                    label = self._extract_floor_label(fl, f_idx)
                    self._floor_label_index[(mid, fid_str)] = label
                    return label
        return ""

    def infer_unique_underground_floor(
        self,
        map_id: int,
        lat: float,
        lng: float,
        margin: float = 15.0,
    ) -> Optional[Tuple[str, str]]:
        """
        Infers `(floor_id, floor_label)` ONLY when a single underground overlay covers `(lat, lng)`.
        Returns `None` when zero or >= 2 overlapping floors contain the coordinate.
        """
        try:
            plat = float(lat)
            plng = float(lng)
            tol = max(0.0, float(margin))
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(plat) and math.isfinite(plng)):
            return None

        matches: Dict[str, str] = {}
        for g in self.map_point_groups.get(int(map_id), ()):
            for f_idx, fl in enumerate(g.get("floors", ())):
                ov = fl.get("overlay")
                if not isinstance(ov, Mapping):
                    continue
                try:
                    if "minLng" in ov and "maxLng" in ov and "minLat" in ov and "maxLat" in ov:
                        min_lng = float(ov["minLng"])
                        max_lng = float(ov["maxLng"])
                        min_lat = float(ov["minLat"])
                        max_lat = float(ov["maxLat"])
                    else:
                        lx, rx = float(ov["l_x"]), float(ov["r_x"])
                        ly, ry = float(ov["l_y"]), float(ov["r_y"])
                        min_lng, max_lng = min(lx, rx), max(lx, rx)
                        min_lat, max_lat = min(ly, ry), max(ly, ry)
                except (KeyError, TypeError, ValueError):
                    continue
                if (min_lng - tol <= plng <= max_lng + tol) and (min_lat - tol <= plat <= max_lat + tol):
                    fl_id = fl.get("id")
                    if fl_id is None:
                        continue
                    fid_str = str(fl_id).strip()
                    if not fid_str or fid_str == "0":
                        continue
                    matches.setdefault(fid_str, self._extract_floor_label(fl, f_idx))
                    if len(matches) > 1:
                        return None

        if len(matches) == 1:
            return next(iter(matches.items()))
        return None

    def close(self) -> None:
        """Releases helper subprocesses such as the persistent Windows OCR worker."""
        ocr = getattr(self, "ocr", None)
        if ocr is not None:
            try:
                ocr.close()
            except Exception:
                pass
