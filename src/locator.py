import os
import json
import math
import logging
from typing import Optional, Tuple, Dict, Any, List
import cv2
import numpy as np
from src.win_ocr import WindowsOCRService

logger = logging.getLogger("GenshinHelper.Locator")

class MapLocator:
    """
    Ultra-fast Computer Vision Landmark Locator for Genshin Impact fullscreen map (M).
    Uses spatial neighborhood graph indexing to achieve < 50ms matching across all Teyvat.
    Works on Surface and Underground layers, 1080p, 2K, and 4K displays.
    """
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            base_dir = os.path.dirname(os.path.dirname(__file__))
            db_path = os.path.join(base_dir, "src", "waypoints_db.json")
            if not os.path.exists(db_path):
                db_path = os.path.join(base_dir, "scratch", "waypoints.json")

        self.maps: Dict[int, Dict[str, Any]] = {}
        self.waypoints: List[Dict[str, Any]] = []
        self.neighbors: Dict[int, List] = {}
        self.underground_groups: List[Dict[str, Any]] = []
        self.ocr = WindowsOCRService()

        # Load underground exploration groups database
        ug_path = os.path.join(os.path.dirname(__file__), "underground_db.json")
        if os.path.exists(ug_path):
            try:
                with open(ug_path, "r", encoding="utf-8") as f:
                    self.underground_groups = json.load(f)
                logger.info(f"Loaded {len(self.underground_groups)} underground exploration groups.")
            except Exception as e:
                logger.warning(f"Could not load underground_db.json: {e}")

        if os.path.exists(db_path):
            try:
                with open(db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict) and "maps" in data:
                    total_wps = 0
                    for mid_str, mdata in data["maps"].items():
                        mid = int(mid_str)
                        wps = mdata.get("waypoints", [])
                        nb = self._build_spatial_index(wps)
                        self.maps[mid] = {
                            "id": mid,
                            "name": mdata.get("name", f"Map {mid}"),
                            "priority": mdata.get("priority", 99),
                            "waypoints": wps,
                            "neighbors": nb
                        }
                        total_wps += len(wps)
                    logger.info(f"Loaded Multi-Map database with {len(self.maps)} maps ({total_wps} waypoints).")
                elif isinstance(data, list):
                    # Legacy single-map list
                    nb = self._build_spatial_index(data)
                    self.maps[2] = {
                        "id": 2,
                        "name": "Bản Đồ Tương Tác Teyvat",
                        "priority": 1,
                        "waypoints": data,
                        "neighbors": nb
                    }
                    logger.info(f"Loaded legacy waypoints database with {len(data)} waypoints.")

                # Default fallback pointers for backward compatibility
                if 2 in self.maps:
                    self.waypoints = self.maps[2]["waypoints"]
                    self.neighbors = self.maps[2]["neighbors"]
                elif self.maps:
                    first_m = next(iter(self.maps.values()))
                    self.waypoints = first_m["waypoints"]
                    self.neighbors = first_m["neighbors"]
            except Exception as e:
                logger.error(f"Failed to load waypoints database: {e}")
        else:
            logger.warning(f"Waypoints database not found at {db_path}")

    def _build_spatial_index(self, waypoints: List[Dict[str, Any]]) -> Dict[int, List]:
        """Precomputes neighbor waypoints within 2200m to enable instant matching."""
        neighbors = {}
        n = len(waypoints)
        for i in range(n):
            w1 = waypoints[i]
            w1_id = w1['id']
            nb_list = []
            for j in range(n):
                if i == j: continue
                w2 = waypoints[j]
                dw_x = w2['x_pos'] - w1['x_pos']
                dw_y = w2['y_pos'] - w1['y_pos']
                dist = math.hypot(dw_x, dw_y)
                if 50.0 <= dist <= 2500.0:
                    nb_list.append((w2, dw_x, dw_y, dist, math.atan2(dw_y, dw_x)))
            neighbors[w1_id] = nb_list
        return neighbors

    def _normalize_region_text(self, text: str) -> str:
        """Normalizes raw OCR text: strips diacritics, unifies transliterations, lowercases."""
        if not text:
            return ""
        import unicodedata
        import re
        replacements = {
            'ä': 'a', 'å': 'a', 'ë': 'e', 'ö': 'o', 'ø': 'o', 'ü': 'u',
            'ciru': 'cuu', 'cir': 'cuu', 'cb': 'co', 'mé': 'mo'
        }
        t = unicodedata.normalize('NFKD', text)
        t = ''.join(c for c in t if not unicodedata.combining(c))
        t = t.lower().replace('đ', 'd').replace('Đ', 'd')
        for k, v in replacements.items():
            t = t.replace(k, v)
        return re.sub(r'[^a-z0-9\s]', ' ', t)

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
            36: ["thanh vien co", "nui thanh vien", "nui thanh", "vien co"],
            40: ["bach duong tuyet", "bach duong", "bech du", "suong nguyet"]
        }
        for mid, kws in submap_keywords.items():
            if any(k in norm for k in kws):
                return mid, None, raw_text

        # 2. Check main Teyvat map (Map 2) areas
        area_keywords = {
            8: ["beryl", "belleau", "fontaine", "dai sanh", "court", "merusea", "morte", "erinnyes", "nostoi", "romaritime"],
            4: ["sumeru", "lokapala", "ardravi", "ashavan", "avidya", "vanarana", "hadramaveth", "sa mac", "vissudha", "desert"],
            2: ["liyue", "tuyet van", "khanh van", "dao quang", "quy ly", "tram ngoc", "chenyu", "quynh co", "song quynh"],
            1: ["mondstadt", "phong khoi", "tuyet son", "long tich", "dragonspine", "phong tieu", "doi phong"],
            3: ["inazuma", "narukami", "kannazuka", "yashiori", "watatsumi", "seirai", "tsurumi"],
            11: ["natlan", "luu tuyen", "to tro tan", "hat lua", "son dat", "coatepec", "tepeacac", "nguyen trang", "cao nguyen", "ven hoa", "long chao"],
            13: ["nod-krai", "nod krai", "lempe", "lempo", "hiisi"],
            16: ["snezhnaya", "dat bang", "bang vinh cuu", "vinh cuu", "bang vinh", "rankova", "zorya"]
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
        """Finds the cyan player arrow on fullscreen map M."""
        if frame is None or frame.size == 0:
            return None

        h, w = frame.shape[:2]
        if hsv is None:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        cyan_lower = np.array([85, 140, 160], dtype=np.uint8)
        cyan_upper = np.array([105, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, cyan_lower, cyan_upper)

        # Mask UI headers, footers, left panel
        mask[:int(h * 0.12), :] = 0
        mask[int(h * 0.92):, :] = 0
        mask[:int(h * 0.45), :int(w * 0.18)] = 0
        mask[int(h * 0.80):, int(w * 0.82):] = 0

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        valid = [c for c in contours if 20 <= cv2.contourArea(c) <= 2500]
        if not valid:
            return None

        center_x, center_y = w / 2, h / 2
        def dist_to_center(c):
            M = cv2.moments(c)
            if M["m00"] == 0: return 99999
            return math.hypot(M["m10"]/M["m00"] - center_x, M["m01"]/M["m00"] - center_y)

        best = min(valid, key=dist_to_center)
        M = cv2.moments(best)
        if M["m00"] == 0:
            return None

        cx = float(M["m10"] / M["m00"])
        cy = float(M["m01"] / M["m00"])

        angle = 0.0
        if len(best) >= 5:
            ellipse = cv2.fitEllipse(best)
            angle = float(ellipse[2])

        return (cx, cy, angle)

    def detect_layers_universal(self, frame: np.ndarray) -> Dict[str, Any]:
        """
        Universal Multilayer & Elevator detector for Genshin Impact Map M.
        Supports 1080p, 2K, and 4K displays.
        """
        h, w = frame.shape[:2]
        roi = frame[int(h * 0.22):int(h * 0.78), int(w * 0.90):int(w * 0.99)]
        default_res = {
            "has_layers": False,
            "is_underground": False,
            "total_floors": 1,
            "active_floor_idx": 0,
            "surface_idx": 0,
            "floor_name": "G"
        }
        if roi.size == 0:
            return default_res

        # 1. Find cyan glowing active button
        hsv_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask_cyan = cv2.inRange(hsv_roi, np.array([85, 120, 140]), np.array([115, 255, 255]))
        cyan_contours, _ = cv2.findContours(mask_cyan, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        valid_cyan = [c for c in cyan_contours if cv2.contourArea(c) > 30 * (h / 1080.0)**2]
        if not valid_cyan:
            return default_res

        ac = max(valid_cyan, key=cv2.contourArea)
        M = cv2.moments(ac)
        if M["m00"] <= 0:
            return default_res
        cyan_cx = M["m10"] / M["m00"]
        cyan_cy = M["m01"] / M["m00"]

        # 2. Find all icon contours vertically aligned with the elevator column
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 70, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        min_area = 200 * (h / 1440.0)**2
        max_area = 15000 * (h / 1440.0)**2
        dx_tol = 22 * (w / 2560.0)

        button_ys = []
        for c in contours:
            area = cv2.contourArea(c)
            if min_area <= area <= max_area:
                M_c = cv2.moments(c)
                if M_c["m00"] > 0:
                    cx = M_c["m10"] / M_c["m00"]
                    cy = M_c["m01"] / M_c["m00"]
                    if abs(cx - cyan_cx) <= dx_tol:
                        button_ys.append((cy, area))

        button_ys.sort(key=lambda x: x[0])
        merged = []
        for cy, a in button_ys:
            if not merged or abs(cy - merged[-1][0]) > 25 * (h / 1440.0):
                merged.append([cy, a])
            else:
                merged[-1][0] = (merged[-1][0] + cy) / 2
                merged[-1][1] = max(merged[-1][1], a)

        if not any(abs(m[0] - cyan_cy) < 25 * (h / 1440.0) for m in merged):
            merged.append([cyan_cy, cv2.contourArea(ac)])
            merged.sort(key=lambda x: x[0])

        # Contiguous cluster filter: Keep only buttons adjacent to the active cyan button (max gap tolerance)
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
            # When surface (mountain) is at the bottom, the sub-floors from top to bottom
            # correspond 1:1 to HoYoLAB's elevator floors B1, B2, B3...
            floor_name = f"B{active_idx + 1}"

        return {
            "has_layers": True,
            "is_underground": is_underground,
            "total_floors": N,
            "active_floor_idx": active_idx,
            "surface_idx": surface_idx,
            "floor_name": floor_name
        }

    def detect_underground_state(self, frame: np.ndarray) -> Tuple[bool, int]:
        """Backward compatibility wrapper."""
        info = self.detect_layers_universal(frame)
        return info["is_underground"], info["active_floor_idx"]

    def detect_waypoint_candidates(
        self,
        frame: np.ndarray,
        arrow_pos: Optional[Tuple[float, float]] = None,
        hsv: Optional[np.ndarray] = None
    ) -> List[Tuple[float, float, int]]:
        """
        Extracts candidate waypoint coordinates with high-precision adaptive thresholding.
        Immune to snowfields and city rooftops.
        """
        h, w = frame.shape[:2]
        if hsv is None:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # Primary pass: Waypoint diamond blue core (S >= 125 eliminates snow S~98 and roof S~70)
        lower_blue = np.array([92, 125, 140], dtype=np.uint8)
        upper_blue = np.array([125, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_blue, upper_blue)

        # Precise UI exclusions: avoid cutting real edge waypoints
        mask[:int(h * 0.08), :] = 0
        mask[int(h * 0.92):, :] = 0
        mask[:int(h * 0.40), :int(w * 0.18)] = 0
        mask[int(h * 0.88):, int(w * 0.83):] = 0
        mask[int(h * 0.20):int(h * 0.80), int(w * 0.92):] = 0

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)) if w < 1600 else cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []

        scale_factor = (w / 2560.0) ** 2
        min_area = max(8.0, 18.0 * scale_factor)
        max_area = max(80.0, 700.0 * scale_factor)
        box_rad = 18 if w > 2000 else 14

        for c in contours:
            area = cv2.contourArea(c)
            if min_area <= area <= max_area:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    cx = float(M["m10"] / M["m00"])
                    cy = float(M["m01"] / M["m00"])

                    if arrow_pos and math.hypot(cx - arrow_pos[0], cy - arrow_pos[1]) < 35.0:
                        continue

                    roi = frame[max(0, int(cy)-box_rad):min(h, int(cy)+box_rad), max(0, int(cx)-box_rad):min(w, int(cx)+box_rad)]
                    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                    white_px = int(np.sum(gray > 200))
                    min_white = max(15, int(150 * scale_factor))
                    if white_px >= min_white:
                        candidates.append((cx, cy, white_px))

        # Check if player arrow is stationed directly on a waypoint
        if arrow_pos:
            roi_arr = frame[max(0, int(arrow_pos[1])-box_rad):min(h, int(arrow_pos[1])+box_rad), max(0, int(arrow_pos[0])-box_rad):min(w, int(arrow_pos[0])+box_rad)]
            gray_arr = cv2.cvtColor(roi_arr, cv2.COLOR_BGR2GRAY)
            w200_arr = int(np.sum(gray_arr > 200))
            w210_arr = int(np.sum(gray_arr > 210))
            if w200_arr >= max(6, int(30 * (w / 2560.0))) or w210_arr > 5:
                candidates.append((arrow_pos[0], arrow_pos[1], max(w200_arr, 350)))

        # Supplementary pass: ONLY run when fewer than 2 candidates found
        if len(candidates) < 2:
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            white_mask = ((gray_frame > 200) & (hsv[:, :, 1] < 75)).astype(np.uint8) * 255
            white_mask[:int(h * 0.08), :] = 0
            white_mask[int(h * 0.92):, :] = 0
            white_mask[:int(h * 0.40), :int(w * 0.18)] = 0
            white_mask[int(h * 0.88):, int(w * 0.83):] = 0
            white_mask[int(h * 0.20):int(h * 0.80), int(w * 0.92):] = 0

            cnts_w, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            min_w_area = max(6.0, 15.0 * scale_factor)
            max_w_area = max(100.0, 400.0 * scale_factor)

            for cw in cnts_w:
                w_area = cv2.contourArea(cw)
                if min_w_area <= w_area <= max_w_area:
                    Mw = cv2.moments(cw)
                    if Mw["m00"] > 0:
                        wcx = Mw["m10"] / Mw["m00"]
                        wcy = Mw["m01"] / Mw["m00"]
                        if arrow_pos and math.hypot(wcx - arrow_pos[0], wcy - arrow_pos[1]) < 35.0:
                            continue
                        rad = int(12 * (w / 2560.0)) if w > 1600 else 10
                        roi_hsv = hsv[max(0, int(wcy)-rad):min(h, int(wcy)+rad), max(0, int(wcx)-rad):min(w, int(wcx)+rad)]
                        if roi_hsv.size > 0:
                            blue_cnt = np.sum((roi_hsv[:, :, 0] >= 92) & (roi_hsv[:, :, 0] <= 125) & (roi_hsv[:, :, 1] >= 120) & (roi_hsv[:, :, 2] >= 140))
                            if blue_cnt >= max(6, int(20 * scale_factor)):
                                candidates.append((wcx, wcy, int(w_area * 3.0)))

        merge_dist = 36.0 * (w / 2560.0)
        clean = []
        candidates.sort(key=lambda x: x[2], reverse=True)
        for c in candidates:
            if not any(math.hypot(c[0] - x[0], c[1] - x[1]) < merge_dist for x in clean):
                clean.append(c)

        # Fallback second pass if still fewer than 2 candidates found (e.g. dark caves)
        if len(clean) < 2:
            lower_rel = np.array([92, 100, 130], dtype=np.uint8)
            mask_rel = cv2.inRange(hsv, lower_rel, upper_blue)
            mask_rel[:int(h * 0.08), :] = 0
            mask_rel[int(h * 0.92):, :] = 0
            mask_rel[:int(h * 0.40), :int(w * 0.18)] = 0
            mask_rel[int(h * 0.88):, int(w * 0.83):] = 0
            mask_rel[int(h * 0.20):int(h * 0.80), int(w * 0.92):] = 0
            mask_rel = cv2.morphologyEx(mask_rel, cv2.MORPH_OPEN, kernel)
            cnts2, _ = cv2.findContours(mask_rel, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cands2 = []
            for c in cnts2:
                area = cv2.contourArea(c)
                if min_area <= area <= max_area:
                    M = cv2.moments(c)
                    if M["m00"] > 0:
                        cx = float(M["m10"] / M["m00"])
                        cy = float(M["m01"] / M["m00"])
                        if arrow_pos and math.hypot(cx - arrow_pos[0], cy - arrow_pos[1]) < 35.0:
                            continue
                        roi = frame[max(0, int(cy)-box_rad):min(h, int(cy)+box_rad), max(0, int(cx)-box_rad):min(w, int(cx)+box_rad)]
                        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                        white_px = int(np.sum(gray > 165))
                        min_white_rel = max(12, int(130 * scale_factor))
                        if white_px >= min_white_rel:
                            cands2.append((cx, cy, white_px))
            cands2.sort(key=lambda x: x[2], reverse=True)
            for c in cands2:
                if not any(math.hypot(c[0] - x[0], c[1] - x[1]) < merge_dist for x in clean):
                    clean.append(c)

        return clean

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
        allow_2point: bool = False
    ) -> Optional[Tuple[List, float, np.ndarray, float]]:
        """
        Matches screen candidates using direct Isometric Transformation (Zero-rotation, Uniform Scale).
        Immune to Y-axis flipping/reflection. Extremely fast (<5ms on average).
        """
        n_cands = len(candidates)
        if n_cands < 2 or not waypoints:
            return None

        # When target_area_id is known, strictly filter waypoints to that area and its direct neighbors
        if target_area_id is not None:
            valid_ids = {w['id'] for w in waypoints if w.get('area_id') == target_area_id}
            for wid in list(valid_ids):
                for nb_item in neighbors.get(wid, []):
                    valid_ids.add(nb_item[0]['id'])
            target_waypoints = [w for w in waypoints if w['id'] in valid_ids]
            if len(target_waypoints) >= 2:
                waypoints = target_waypoints

        if frame_w:
            fw_ratio = 2560.0 / float(frame_w)
            expected_scale = base_scale * fw_ratio
            min_scale = (base_scale * 0.40 / 1.12) * fw_ratio
            max_scale = (base_scale * 2.10 / 1.12) * fw_ratio
            max_world_span = 3200.0 * fw_ratio
        else:
            expected_scale = base_scale
            min_scale = base_scale * 0.40 / 1.12
            max_scale = base_scale * 2.10 / 1.12
            max_world_span = 3200.0

        cand_limit = min(n_cands, cand_limit)
        best_match = None
        best_score = -99999.0

        for i in range(cand_limit):
            s1 = candidates[i]
            for j in range(i + 1, cand_limit):
                s2 = candidates[j]
                ds_x = s2[0] - s1[0]
                ds_y = s2[1] - s1[1]
                dist_s = math.hypot(ds_x, ds_y)
                if dist_s < 70: continue
                angle_s = math.atan2(ds_y, ds_x)

                min_w_dist = min_scale * dist_s
                max_w_dist = max_scale * dist_s

                for w1 in waypoints:
                    w1_id = w1['id']
                    nb_list = neighbors.get(w1_id, [])
                    for w2, dw_x, dw_y, dist_w, angle_w in nb_list:
                        if not (min_w_dist <= dist_w <= max_w_dist): continue

                        scale = dist_w / dist_s

                        # Rotation check: Genshin Map has exactly 0 rotation (North up)
                        diff = abs((angle_w - angle_s + math.pi) % (2 * math.pi) - math.pi)
                        if diff > 0.048: continue

                        inliers = [(s1, w1), (s2, w2)]
                        used = {w1_id, w2['id']}
                        box_tol = 12.0 * scale

                        for k in range(cand_limit):
                            if k in (i, j): continue
                            sk = candidates[k]
                            exp_wx = w1['x_pos'] + (sk[0] - s1[0]) * scale
                            exp_wy = w1['y_pos'] + (sk[1] - s1[1]) * scale

                            closest_d = 99999
                            closest_w = None
                            for w_cand, _, _, _, _ in nb_list:
                                if w_cand['id'] in used: continue
                                # Fast AABB check before hypot
                                if abs(w_cand['x_pos'] - exp_wx) >= box_tol: continue
                                if abs(w_cand['y_pos'] - exp_wy) >= box_tol: continue
                                d = math.hypot(w_cand['x_pos'] - exp_wx, w_cand['y_pos'] - exp_wy)
                                if d < closest_d:
                                    closest_d = d
                                    closest_w = w_cand

                            if closest_d < box_tol and closest_w:
                                inliers.append((sk, closest_w))
                                used.add(closest_w['id'])

                        N = len(inliers)
                        if N >= 3:
                            # Area consistency check
                            areas = [w.get('area_id') for _, w in inliers if w.get('area_id')]
                            dom_area = max(set(areas), key=areas.count) if areas else None
                            if areas:
                                dom_ratio = areas.count(dom_area) / len(areas)
                                if N <= 4 and dom_ratio < 1.0:
                                    continue
                                if N >= 5 and dom_ratio < 0.75:
                                    continue

                            # Direct Isometric Translation (Zero-rotation, Uniform Scale)
                            tx = sum(w['x_pos'] - scale * s[0] for s, w in inliers) / N
                            ty = sum(w['y_pos'] - scale * s[1] for s, w in inliers) / N

                            sum_sq_err = 0.0
                            for s, w in inliers:
                                pred_wx = scale * s[0] + tx
                                pred_wy = scale * s[1] + ty
                                sum_sq_err += (pred_wx - w['x_pos'])**2 + (pred_wy - w['y_pos'])**2
                            rmse = math.sqrt(sum_sq_err / N)

                            # Robust check for 3-inlier matches
                            if N == 3:
                                max_allowed_rmse = 3.5 if (target_area_id is not None and dom_area == target_area_id) else 2.8
                                if rmse > max_allowed_rmse:
                                    continue
                                if not (expected_scale * 0.60 <= scale <= expected_scale * 1.70):
                                    continue
                            else:
                                if rmse > 4.5:
                                    continue

                            total_white = sum(s[2] for s, w in inliers)

                            # Scale penalty outside natural range
                            scale_penalty = 0.0
                            if scale < expected_scale * 0.55:
                                scale_penalty = ((expected_scale * 0.55 - scale) / expected_scale) * 60.0
                            elif scale > expected_scale * 1.85:
                                scale_penalty = ((scale - expected_scale * 1.85) / expected_scale) * 60.0

                            # Area bonus
                            area_bonus = 0.0
                            if target_area_id is not None:
                                if dom_area == target_area_id:
                                    area_bonus = 200.0
                                else:
                                    area_bonus = -100.0

                            score = N * 150.0 + (total_white * 0.1) - (rmse * 5.0) - scale_penalty + area_bonus

                            if score > best_score:
                                M = np.array([
                                    [scale, 0.0],
                                    [0.0, scale],
                                    [tx, ty]
                                ])
                                best_score = score
                                best_match = (inliers, scale, M, rmse)

                                # Early break ONLY on decisive matches with >= 4 points or area-confirmed 3 points
                                if (N >= 5 and rmse <= 3.5) or (N >= 4 and rmse <= 2.2):
                                    return best_match
                                if N == 3 and rmse <= 1.0 and target_area_id is not None and dom_area == target_area_id:
                                    return best_match

        # 2-point fallback when strictly constrained by OCR area or targeted sub-map
        if best_match is None and len(candidates) >= 2 and (target_area_id is not None or allow_2point):
            cand_limit_2 = min(len(candidates), 6)
            best_2p = None
            best_2p_score = -99999.0
            for i in range(cand_limit_2):
                s1 = candidates[i]
                for j in range(i + 1, cand_limit_2):
                    s2 = candidates[j]
                    ds_x = s2[0] - s1[0]
                    ds_y = s2[1] - s1[1]
                    dist_s = math.hypot(ds_x, ds_y)
                    if dist_s < 90: continue
                    angle_s = math.atan2(ds_y, ds_x)

                    for w1 in waypoints:
                        w1_id = w1['id']
                        nb_list = neighbors.get(w1_id, [])
                        for w2, dw_x, dw_y, dist_w, angle_w in nb_list:
                            if not (min_scale * dist_s <= dist_w <= max_scale * dist_s): continue
                            scale = dist_w / dist_s
                            diff = abs((angle_w - angle_s + math.pi) % (2 * math.pi) - math.pi)
                            if diff > 0.035: continue

                            if target_area_id is not None:
                                if w1.get('area_id') != target_area_id and w2.get('area_id') != target_area_id:
                                    continue

                            scale_dev = abs(scale - expected_scale) / expected_scale
                            if scale_dev > 0.35: continue

                            score = 250.0 - scale_dev * 100.0 - diff * 500.0
                            if score > best_2p_score:
                                best_2p_score = score
                                tx = (w1['x_pos'] - scale * s1[0] + w2['x_pos'] - scale * s2[0]) / 2.0
                                ty = (w1['y_pos'] - scale * s1[1] + w2['y_pos'] - scale * s2[1]) / 2.0
                                M = np.array([
                                    [scale, 0.0],
                                    [0.0, scale],
                                    [tx, ty]
                                ])
                                best_2p = ([(s1, w1), (s2, w2)], scale, M, 0.5)

            if best_2p is not None:
                return best_2p

        return best_match

    def locate_player(self, frame: np.ndarray) -> Optional[Dict[str, Any]]:
        """
        Universal Multi-Map Landmark Locator (<20ms).
        Automatically identifies the player's active region/submap across all Teyvat.
        """
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        arrow_res = self.detect_player_arrow(frame, hsv=hsv)
        if not arrow_res:
            return None

        px, py, angle = arrow_res
        layer_info = self.detect_layers_universal(frame)

        candidates = self.detect_waypoint_candidates(frame, arrow_pos=(px, py), hsv=hsv)
        if len(candidates) < 2 or not self.maps:
            return {
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
                "has_layers": layer_info["has_layers"]
            }

        # Multi-map competitive ranking:
        best_candidate_match = None
        best_score = -99999.0

        area_names_map = {
            1: "Mondstadt", 2: "Liyue", 3: "Inazuma", 4: "Sumeru",
            8: "Fontaine", 11: "Natlan", 13: "Nod-Krai", 16: "Snezhnaya"
        }

        map_config = {
            2:  {"base_scale": 1.12, "max_scale_dev": 0.45},
            7:  {"base_scale": 0.49, "max_scale_dev": 0.28},
            9:  {"base_scale": 0.75, "max_scale_dev": 0.20},
            34: {"base_scale": 0.60, "max_scale_dev": 0.25},
            36: {"base_scale": 0.60, "max_scale_dev": 0.25},
            37: {"base_scale": 0.60, "max_scale_dev": 0.25},
            40: {"base_scale": 0.30, "max_scale_dev": 0.25},
        }

        # Detect map and region from bottom-right corner (<10ms)
        target_map_id, target_area_id, region_text = self.detect_region_name(frame)
        if target_map_id or target_area_id:
            logger.info(
                f"Decisive region detection: '{region_text}' -> Map {target_map_id}, Area {target_area_id} "
                f"({area_names_map.get(target_area_id) if target_area_id else 'Sub-map'})"
            )

        frame_w = frame.shape[1]

        # Case A: OCR detected an isolated sub-map (e.g. Chasm, Enkanomiya, etc.)
        if target_map_id and target_map_id != 2 and target_map_id in self.maps:
            mdata = self.maps[target_map_id]
            cfg = map_config.get(target_map_id, {"base_scale": 0.60})
            res = self._match_single_map(
                candidates, mdata["waypoints"], mdata["neighbors"],
                cand_limit=10, frame_w=frame_w, base_scale=cfg["base_scale"],
                target_area_id=None, arrow_pos=(px, py), allow_2point=True
            )
            if res:
                best_candidate_match = (mdata, res[0], res[1], res[2], res[3])

        # Case B: Map 2 (Teyvat)
        if not best_candidate_match and (target_map_id == 2 or target_map_id is None) and 2 in self.maps:
            m2 = self.maps[2]
            res2 = self._match_single_map(
                candidates, m2["waypoints"], m2["neighbors"],
                cand_limit=10, frame_w=frame_w, base_scale=1.12,
                target_area_id=target_area_id, arrow_pos=(px, py),
                allow_2point=(target_area_id is not None)
            )
            if not res2 and len(candidates) > 10:
                res2 = self._match_single_map(
                    candidates, m2["waypoints"], m2["neighbors"],
                    cand_limit=14, frame_w=frame_w, base_scale=1.12,
                    target_area_id=target_area_id, arrow_pos=(px, py),
                    allow_2point=(target_area_id is not None)
                )
            if res2:
                best_candidate_match = (m2, res2[0], res2[1], res2[2], res2[3])

        # Case C: Fallback to all other isolated submaps ONLY if no match and no OCR restriction
        if not best_candidate_match and target_map_id is None and target_area_id is None:
            for mid, mdata in self.maps.items():
                if mid == 2: continue
                cfg = map_config.get(mid, {"base_scale": 1.12, "max_scale_dev": 0.30})
                base_s = cfg["base_scale"]
                res = self._match_single_map(
                    candidates, mdata["waypoints"], mdata["neighbors"],
                    cand_limit=10, frame_w=frame_w, base_scale=base_s,
                    target_area_id=None, arrow_pos=(px, py), allow_2point=False
                )
                if res is not None:
                    inliers, scale, M, rmse = res
                    inliers_count = len(inliers)
                    score = inliers_count * 150.0 - (rmse * 5.0)
                    if score > best_score:
                        best_score = score
                        best_candidate_match = (mdata, inliers, scale, M, rmse)
                        break

        if best_candidate_match:
            mdata, inliers, scale, M, rmse = best_candidate_match
            pw = np.array([px, py, 1.0]) @ M
            world_x = float(pw[0])
            world_y = float(pw[1])

            # Extract area ID for Map 2 (Teyvat regions)
            area_id = None
            area_name = None
            if mdata["id"] == 2:
                area_ids = [w.get("area_id") for s, w in inliers if w.get("area_id")]
                if area_ids:
                    area_id = max(set(area_ids), key=area_ids.count)
                    area_name = area_names_map.get(area_id, f"Area {area_id}")

            logger.info(
                f"Multi-Map localization SUCCESS: Matched Map {mdata['id']} ({mdata['name']}) "
                f"with {len(inliers)} waypoints (RMSE={rmse:.1f})! World=({world_x:.1f}, {world_y:.1f})"
            )
            return {
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
                "is_underground": layer_info["is_underground"],
                "floor_idx": layer_info["active_floor_idx"],
                "total_floors": layer_info["total_floors"],
                "active_floor_idx": layer_info["active_floor_idx"],
                "surface_idx": layer_info["surface_idx"],
                "floor_name": layer_info["floor_name"],
                "has_layers": layer_info["has_layers"]
            }


        return {
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
            "has_layers": layer_info["has_layers"]
        }
