import os
import json
import math
import logging
from typing import Optional, Tuple, Dict, Any, List
import cv2
import numpy as np

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

        N = len(merged)
        if N <= 1:
            return default_res

        dists = [abs(m[0] - cyan_cy) for m in merged]
        active_idx = int(np.argmin(dists))

        areas = [m[1] for m in merged]
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
        Extracts candidate waypoint coordinates with two-pass adaptive thresholding.
        """
        h, w = frame.shape[:2]
        if hsv is None:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        lower_blue = np.array([92, 100, 150], dtype=np.uint8)
        upper_blue = np.array([125, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_blue, upper_blue)

        mask[:int(h * 0.12), :] = 0
        mask[int(h * 0.88):, :] = 0
        mask[:int(h * 0.45), :int(w * 0.18)] = 0
        mask[int(h * 0.80):, int(w * 0.82):] = 0

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []

        box_rad = 18 if w > 2000 else 14
        for c in contours:
            area = cv2.contourArea(c)
            if 10 <= area <= 800:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    cx = float(M["m10"] / M["m00"])
                    cy = float(M["m01"] / M["m00"])

                    if arrow_pos and math.hypot(cx - arrow_pos[0], cy - arrow_pos[1]) < 35.0:
                        continue

                    roi = frame[max(0, int(cy)-box_rad):min(h, int(cy)+box_rad), max(0, int(cx)-box_rad):min(w, int(cx)+box_rad)]
                    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                    white_px = int(np.sum(gray > 200))
                    if white_px >= 25:
                        candidates.append((cx, cy, white_px))

        clean = []
        candidates.sort(key=lambda x: x[2], reverse=True)
        for c in candidates:
            if not any(math.hypot(c[0] - x[0], c[1] - x[1]) < 22 for x in clean):
                clean.append(c)

        # Fallback second pass if fewer than 3 candidates found (e.g. shaded scenes)
        if len(clean) < 3:
            mask_rel = cv2.inRange(hsv, lower_blue, upper_blue)
            mask_rel[:int(h * 0.12), :] = 0
            mask_rel[int(h * 0.88):, :] = 0
            mask_rel[:int(h * 0.45), :int(w * 0.12)] = 0
            mask_rel[int(h * 0.80):, int(w * 0.82):] = 0
            mask_rel = cv2.morphologyEx(mask_rel, cv2.MORPH_OPEN, kernel)
            cnts2, _ = cv2.findContours(mask_rel, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cands2 = []
            for c in cnts2:
                area = cv2.contourArea(c)
                if 10 <= area <= 800:
                    M = cv2.moments(c)
                    if M["m00"] > 0:
                        cx = float(M["m10"] / M["m00"])
                        cy = float(M["m01"] / M["m00"])
                        if arrow_pos and math.hypot(cx - arrow_pos[0], cy - arrow_pos[1]) < 35.0:
                            continue
                        roi = frame[max(0, int(cy)-box_rad):min(h, int(cy)+box_rad), max(0, int(cx)-box_rad):min(w, int(cx)+box_rad)]
                        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                        white_px = int(np.sum(gray > 165))
                        if white_px >= 25:
                            cands2.append((cx, cy, white_px))
            cands2.sort(key=lambda x: x[2], reverse=True)
            for c in cands2:
                if not any(math.hypot(c[0] - x[0], c[1] - x[1]) < 22 for x in clean):
                    clean.append(c)

        return clean

    def _match_single_map(
        self,
        candidates: List[Tuple[float, float, int]],
        waypoints: List[Dict[str, Any]],
        neighbors: Dict[int, List],
        cand_limit: int = 14
    ) -> Optional[Tuple[List, float, np.ndarray, float]]:
        """
        Matches screen candidates against a specific map's spatial index.
        Returns (inliers, scale, affine_matrix, rmse) or None.
        """
        n_cands = len(candidates)
        if n_cands < 2 or not waypoints:
            return None

        cand_limit = min(n_cands, cand_limit)
        best_match = None
        best_score = -99999.0

        for i in range(cand_limit):
            for j in range(i + 1, cand_limit):
                s1 = candidates[i]
                s2 = candidates[j]
                ds_x = s2[0] - s1[0]
                ds_y = s2[1] - s1[1]
                dist_s = math.hypot(ds_x, ds_y)
                if dist_s < 70: continue
                angle_s = math.atan2(ds_y, ds_x)

                for w1 in waypoints:
                    w1_id = w1['id']
                    nb_list = neighbors.get(w1_id, [])
                    for w2, dw_x, dw_y, dist_w, angle_w in nb_list:
                        scale = dist_w / dist_s
                        if not (0.30 <= scale <= 2.30): continue

                        diff = abs((angle_w - angle_s + math.pi) % (2 * math.pi) - math.pi)
                        if diff > 0.055: continue

                        inliers = [(s1, w1), (s2, w2)]
                        used = {w1_id, w2['id']}

                        for k in range(cand_limit):
                            if k in (i, j): continue
                            sk = candidates[k]
                            exp_wx = w1['x_pos'] + (sk[0] - s1[0]) * scale
                            exp_wy = w1['y_pos'] + (sk[1] - s1[1]) * scale

                            closest_d = 99999
                            closest_w = None
                            for w_cand, _, _, _, _ in nb_list:
                                if w_cand['id'] in used: continue
                                d = math.hypot(w_cand['x_pos'] - exp_wx, w_cand['y_pos'] - exp_wy)
                                if d < closest_d:
                                    closest_d = d
                                    closest_w = w_cand

                            if closest_d < 12.0 * scale and closest_w:
                                inliers.append((sk, closest_w))
                                used.add(closest_w['id'])

                        if len(inliers) >= 3:
                            S = np.array([[s[0], s[1], 1.0] for s, w in inliers])
                            W = np.array([[w['x_pos'], w['y_pos']] for s, w in inliers])
                            M, _, _, _ = np.linalg.lstsq(S, W, rcond=None)
                            errors = np.linalg.norm(S @ M - W, axis=1)
                            rmse = float(np.sqrt(np.mean(errors**2)))
                            max_allowed_rmse = 4.8 if len(inliers) >= 4 else 1.5
                            if rmse <= max_allowed_rmse:
                                score = len(inliers) * 100.0 - rmse
                                if score > best_score:
                                    best_score = score
                                    best_match = (inliers, scale, M, rmse)
                                    if len(inliers) >= 4 and rmse <= 4.0:
                                        break
                    if best_match and len(best_match[0]) >= 4 and best_match[3] <= 4.0: break
                if best_match and len(best_match[0]) >= 4 and best_match[3] <= 4.0: break
            if best_match and len(best_match[0]) >= 4 and best_match[3] <= 4.0: break

        if best_match:
            max_allowed = 4.8 if len(best_match[0]) >= 4 else 1.5
            if best_match[3] <= max_allowed:
                return best_match

        return None

    def locate_player(self, frame: np.ndarray) -> Optional[Dict[str, Any]]:
        """
        Universal Multi-Map Landmark Locator (<50ms).
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

        # Priority 1: Check Map 2 first for instant Teyvat world localization (<15ms)
        if 2 in self.maps:
            m2 = self.maps[2]
            res2 = self._match_single_map(candidates, m2["waypoints"], m2["neighbors"], cand_limit=14)
            if res2 and len(res2[0]) >= 5 and res2[3] <= 4.8:
                best_candidate_match = (m2, res2[0], res2[1], res2[2], res2[3])

        if not best_candidate_match:
            for mid, mdata in self.maps.items():
                res = self._match_single_map(candidates, mdata["waypoints"], mdata["neighbors"], cand_limit=14)
                if res is not None:
                    inliers, scale, M, rmse = res
                    inliers_count = len(inliers)
                    score = inliers_count * 100.0 - rmse

                    # Map 2 Priority Bonus: The primary Teyvat world map is preferred over small submaps
                    if mid == 2 and inliers_count >= 4 and rmse <= 4.8:
                        score += 200.0

                    if score > best_score:
                        best_score = score
                        best_candidate_match = (mdata, inliers, scale, M, rmse)

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
