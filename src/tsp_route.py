"""
TSP Smart Route Planner (F7) for Genshin-Helper v5.

Clean-room original implementation of a fast Nearest-Neighbor + 2-opt open-path
TSP solver with Genshin-specific teleport waypoint leg splitting.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np


CoordInput = Union[Tuple[float, float], List[float], Dict[str, Any]]


def _extract_lat_lng(item: CoordInput) -> Tuple[float, float]:
    """Extracts (lat, lng) from a pin/waypoint dict or (lat, lng) sequence.

    Supports both HoYoLAB Leaflet keys (`lat`, `lng`) and raw dataset keys
    (`y_pos`, `x_pos`, where `lat == y_pos` and `lng == x_pos`).
    """
    if isinstance(item, dict):
        if "lat" in item and "lng" in item:
            return float(item["lat"]), float(item["lng"])
        if "y_pos" in item and "x_pos" in item:
            return float(item["y_pos"]), float(item["x_pos"])
        raise KeyError("Expected ('lat', 'lng') or ('y_pos', 'x_pos') in coordinate dict")
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        return float(item[0]), float(item[1])
    raise TypeError(f"Unsupported coordinate format: {type(item).__name__}")


def _normalize_pin(raw: Dict[str, Any], fallback_idx: int) -> Dict[str, Any]:
    lat, lng = _extract_lat_lng(raw)
    pin_id = raw.get("id", fallback_idx)
    name = str(raw.get("name") or f"Pin {pin_id}")
    return {
        "id": pin_id,
        "lat": lat,
        "lng": lng,
        "name": name,
        "label_id": raw.get("label_id"),
        "floor_id": raw.get("floor_id"),
        "is_underground": bool(raw.get("is_underground", False)),
    }


def _normalize_waypoints(waypoints: Optional[Sequence[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not waypoints:
        return []
    out: List[Dict[str, Any]] = []
    for idx, wp in enumerate(waypoints):
        try:
            lat, lng = _extract_lat_lng(wp)
        except (KeyError, TypeError, ValueError):
            continue
        wp_id = wp.get("id", f"wp_{idx}")
        out.append(
            {
                "id": wp_id,
                "lat": lat,
                "lng": lng,
                "name": str(wp.get("name") or f"Waypoint {wp_id}"),
                "label_id": wp.get("label_id"),
                "area_id": wp.get("area_id"),
                "map_id": wp.get("map_id"),
            }
        )
    return out


def load_waypoints_for_map(
    map_id: int = 2,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Loads teleport waypoints for a HoYoLAB map_id from `src/waypoints_db.json`."""
    path = db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "waypoints_db.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    map_entry = (data.get("maps") or {}).get(str(map_id)) or {}
    return _normalize_waypoints(map_entry.get("waypoints") or [])


def _build_distance_matrix(coords: np.ndarray) -> np.ndarray:
    """Computes symmetric Euclidean distance matrix for N x 2 coordinates."""
    diff = coords[:, None, :] - coords[None, :, :]
    return np.hypot(diff[:, :, 0], diff[:, :, 1])


def path_length(order: Sequence[int], dist_matrix: np.ndarray) -> float:
    """Computes total open-path distance along `order`."""
    if len(order) < 2:
        return 0.0
    total = 0.0
    for k in range(len(order) - 1):
        total += float(dist_matrix[order[k], order[k + 1]])
    return total


def nearest_neighbor_order(
    dist_matrix: np.ndarray,
    start_idx: int = 0,
    fixed_start: bool = False,
) -> List[int]:
    """Builds a greedy nearest-neighbor open path over `0..n-1`.

    When `fixed_start` is False and `n > 2`, also checks a compact set of
    candidate starting nodes (all nodes when `n <= 24`, or 8 well-spread
    candidates for larger `n`) and returns the shortest NN open path.
    """
    n = int(dist_matrix.shape[0])
    if n <= 1:
        return list(range(n))

    def _run_from(seed: int) -> Tuple[float, List[int]]:
        visited = bytearray(n)
        order = [seed]
        visited[seed] = 1
        cur = seed
        total = 0.0
        for _ in range(n - 1):
            row = dist_matrix[cur]
            best_j = -1
            best_d = math.inf
            for j in range(n):
                if not visited[j]:
                    d = float(row[j])
                    if d < best_d:
                        best_d = d
                        best_j = j
            order.append(best_j)
            visited[best_j] = 1
            total += best_d
            cur = best_j
        return total, order

    if fixed_start:
        _, best_order = _run_from(start_idx)
        return best_order

    if n <= 24:
        candidates: Iterable[int] = range(n)
    else:
        step = max(1, n // 8)
        cand_set = {start_idx}
        for idx in range(0, n, step):
            cand_set.add(idx)
        candidates = sorted(cand_set)

    best_total = math.inf
    best_order: List[int] = list(range(n))
    for seed in candidates:
        total, order = _run_from(seed)
        if total < best_total:
            best_total = total
            best_order = order
    return best_order


def two_opt_improve(
    order: Sequence[int],
    dist_matrix: np.ndarray,
    *,
    fix_first: bool = False,
    max_iters: int = 200,
    eps: float = 1e-9,
) -> Tuple[List[int], int]:
    """Applies open-path 2-opt segment reversals until local optimality or `max_iters`.

    Returns `(improved_order, iterations_performed)`.
    """
    path = list(order)
    m = len(path)
    if m < 3 or max_iters <= 0:
        return path, 0

    # Convert to nested Python float list for fast scalar inner-loop indexing.
    dmat = dist_matrix.tolist()
    min_i = 1 if fix_first else 0
    iters = 0

    while iters < max_iters:
        improved = False
        iters += 1
        for i in range(min_i, m - 1):
            pi = path[i]
            prev_row = dmat[path[i - 1]] if i > 0 else None
            base_before = prev_row[pi] if prev_row is not None else 0.0

            for j in range(i + 1, m):
                if i == 0 and j == m - 1:
                    # Reversing the entire open path preserves exact length.
                    continue

                pj = path[j]
                if j + 1 < m:
                    nxt = path[j + 1]
                    old_cost = base_before + dmat[pj][nxt]
                    new_cost = (prev_row[pj] if prev_row is not None else 0.0) + dmat[pi][nxt]
                else:
                    old_cost = base_before
                    new_cost = prev_row[pj] if prev_row is not None else 0.0

                if new_cost + eps < old_cost:
                    path[i : j + 1] = reversed(path[i : j + 1])
                    pi = path[i]
                    base_before = prev_row[pi] if prev_row is not None else 0.0
                    improved = True

        if not improved:
            break

    return path, iters


def _make_leg(
    index: int,
    *,
    from_id: Any,
    to_id: Any,
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    distance: float,
    air_distance: float,
    effective_cost: float,
    is_teleport: bool,
    waypoint_id: Any = None,
    waypoint_name: Optional[str] = None,
    from_floor_id: Any = None,
    to_floor_id: Any = None,
) -> Dict[str, Any]:
    floor_change = (
        from_floor_id is not None
        and to_floor_id is not None
        and from_floor_id != to_floor_id
    )
    return {
        "index": index,
        "from_id": from_id,
        "to_id": to_id,
        "from_lat": round(from_lat, 4),
        "from_lng": round(from_lng, 4),
        "to_lat": round(to_lat, 4),
        "to_lng": round(to_lng, 4),
        "distance": round(distance, 4),
        "air_distance": round(air_distance, 4),
        "effective_cost": round(effective_cost, 4),
        "is_teleport": is_teleport,
        "waypoint_id": waypoint_id,
        "waypoint_name": waypoint_name,
        "from_floor_id": from_floor_id,
        "to_floor_id": to_floor_id,
        "floor_change": floor_change,
        "points": [
            [round(from_lat, 4), round(from_lng, 4)],
            [round(to_lat, 4), round(to_lng, 4)],
        ],
    }


def plan_route(
    pins: Sequence[Dict[str, Any]],
    start_pos: Optional[CoordInput] = None,
    waypoints: Optional[Sequence[Dict[str, Any]]] = None,
    teleport_penalty: float = 180.0,
    max_2opt_iters: int = 200,
) -> Dict[str, Any]:
    """Plans an optimized farming route through `pins` using NN + 2-opt TSP.

    Args:
        pins: Sequence of pin dicts `{id, lat, lng, name, label_id, floor_id, is_underground}`.
        start_pos: Optional player starting coordinate `(lat, lng)` or `{lat, lng}`.
        waypoints: Optional teleport waypoints for splitting long inter-island legs.
        teleport_penalty: Equivalent walking-unit cost of using a teleport waypoint.
            A leg is split into a teleport + post-teleport walk when
            `walk_dist > teleport_penalty` and a waypoint satisfies
            `teleport_penalty + dist(wp, next_pin) < walk_dist`.
        max_2opt_iters: Maximum number of 2-opt improvement passes.

    Returns:
        Route dictionary containing `ordered_pins`, `legs`, `teleport_hints`,
        `total_distance`, `raw_walk_distance`, `effective_distance`,
        `nn_distance`, `two_opt_iterations`, `teleport_count`, `pin_count`,
        and `elapsed_ms`.
    """
    t0 = time.perf_counter()

    norm_pins = [_normalize_pin(p, idx) for idx, p in enumerate(pins)]
    n_pins = len(norm_pins)
    penalty = max(0.0, float(teleport_penalty))

    if n_pins == 0:
        return {
            "ordered_pins": [],
            "legs": [],
            "teleport_hints": [],
            "total_distance": 0.0,
            "raw_walk_distance": 0.0,
            "effective_distance": 0.0,
            "nn_distance": 0.0,
            "two_opt_iterations": 0,
            "teleport_count": 0,
            "pin_count": 0,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 4),
        }

    has_start = start_pos is not None
    start_lat = 0.0
    start_lng = 0.0
    if has_start:
        start_lat, start_lng = _extract_lat_lng(start_pos)
        coords = np.empty((n_pins + 1, 2), dtype=np.float64)
        coords[0, 0] = start_lat
        coords[0, 1] = start_lng
        for idx, p in enumerate(norm_pins):
            coords[idx + 1, 0] = p["lat"]
            coords[idx + 1, 1] = p["lng"]
    else:
        coords = np.array([[p["lat"], p["lng"]] for p in norm_pins], dtype=np.float64)

    dist_matrix = _build_distance_matrix(coords)

    nn_order = nearest_neighbor_order(dist_matrix, start_idx=0, fixed_start=has_start)
    nn_dist = path_length(nn_order, dist_matrix)

    opt_order, two_opt_iters = two_opt_improve(
        nn_order,
        dist_matrix,
        fix_first=has_start,
        max_iters=max_2opt_iters,
    )
    raw_walk_dist = path_length(opt_order, dist_matrix)

    if has_start:
        pin_indices = [idx - 1 for idx in opt_order[1:]]
    else:
        pin_indices = list(opt_order)

    ordered_pins: List[Dict[str, Any]] = []
    for step_num, pin_idx in enumerate(pin_indices, start=1):
        pin_copy = dict(norm_pins[pin_idx])
        pin_copy["step"] = step_num
        ordered_pins.append(pin_copy)

    # Pre-extract waypoint array once for fast vectorized nearest-waypoint lookup
    norm_wps = _normalize_waypoints(waypoints)
    wp_coords: Optional[np.ndarray] = None
    if norm_wps:
        wp_coords = np.array([[w["lat"], w["lng"]] for w in norm_wps], dtype=np.float64)

    def _find_best_teleport(target_lat: float, target_lng: float, direct_dist: float) -> Optional[Tuple[Dict[str, Any], float]]:
        if wp_coords is None or direct_dist <= penalty:
            return None
        d_lat = wp_coords[:, 0] - target_lat
        d_lng = wp_coords[:, 1] - target_lng
        dists = np.hypot(d_lat, d_lng)
        best_idx = int(np.argmin(dists))
        wp_walk = float(dists[best_idx])
        if penalty + wp_walk + 1e-6 < direct_dist:
            return norm_wps[best_idx], wp_walk
        return None

    # Build step sequence (optional virtual start node + ordered pins)
    nodes: List[Dict[str, Any]] = []
    if has_start:
        nodes.append(
            {
                "id": "START",
                "lat": start_lat,
                "lng": start_lng,
                "name": "Start",
                "floor_id": None,
                "step": 0,
            }
        )
    nodes.extend(ordered_pins)

    legs: List[Dict[str, Any]] = []
    teleport_hints: List[Dict[str, Any]] = []
    total_walk_dist = 0.0
    effective_dist = 0.0

    for k in range(len(nodes) - 1):
        src = nodes[k]
        dst = nodes[k + 1]
        direct_dist = math.hypot(dst["lat"] - src["lat"], dst["lng"] - src["lng"])

        tp_candidate = _find_best_teleport(dst["lat"], dst["lng"], direct_dist)
        if tp_candidate is not None:
            wp, post_walk = tp_candidate
            tp_air = math.hypot(wp["lat"] - src["lat"], wp["lng"] - src["lng"])

            legs.append(
                _make_leg(
                    len(legs),
                    from_id=src["id"],
                    to_id=wp["id"],
                    from_lat=src["lat"],
                    from_lng=src["lng"],
                    to_lat=wp["lat"],
                    to_lng=wp["lng"],
                    distance=0.0,
                    air_distance=tp_air,
                    effective_cost=penalty,
                    is_teleport=True,
                    waypoint_id=wp["id"],
                    waypoint_name=wp["name"],
                    from_floor_id=src.get("floor_id"),
                    to_floor_id=None,
                )
            )
            legs.append(
                _make_leg(
                    len(legs),
                    from_id=wp["id"],
                    to_id=dst["id"],
                    from_lat=wp["lat"],
                    from_lng=wp["lng"],
                    to_lat=dst["lat"],
                    to_lng=dst["lng"],
                    distance=post_walk,
                    air_distance=post_walk,
                    effective_cost=post_walk,
                    is_teleport=False,
                    waypoint_id=wp["id"],
                    waypoint_name=wp["name"],
                    from_floor_id=None,
                    to_floor_id=dst.get("floor_id"),
                )
            )
            teleport_hints.append(
                {
                    "before_step": dst["step"],
                    "target_pin_id": dst["id"],
                    "target_pin_name": dst["name"],
                    "waypoint_id": wp["id"],
                    "waypoint_name": wp["name"],
                    "waypoint_lat": round(wp["lat"], 4),
                    "waypoint_lng": round(wp["lng"], 4),
                    "direct_walk_distance": round(direct_dist, 4),
                    "post_teleport_walk": round(post_walk, 4),
                    "saved_distance": round(direct_dist - (penalty + post_walk), 4),
                }
            )
            total_walk_dist += post_walk
            effective_dist += penalty + post_walk
        else:
            legs.append(
                _make_leg(
                    len(legs),
                    from_id=src["id"],
                    to_id=dst["id"],
                    from_lat=src["lat"],
                    from_lng=src["lng"],
                    to_lat=dst["lat"],
                    to_lng=dst["lng"],
                    distance=direct_dist,
                    air_distance=direct_dist,
                    effective_cost=direct_dist,
                    is_teleport=False,
                    from_floor_id=src.get("floor_id"),
                    to_floor_id=dst.get("floor_id"),
                )
            )
            total_walk_dist += direct_dist
            effective_dist += direct_dist

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "ordered_pins": ordered_pins,
        "legs": legs,
        "teleport_hints": teleport_hints,
        "total_distance": round(total_walk_dist, 4),
        "raw_walk_distance": round(raw_walk_dist, 4),
        "effective_distance": round(effective_dist, 4),
        "nn_distance": round(nn_dist, 4),
        "two_opt_iterations": two_opt_iters,
        "teleport_count": len(teleport_hints),
        "pin_count": n_pins,
        "elapsed_ms": round(elapsed_ms, 4),
    }
