# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/navigation.py (MIT License)
# Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/hotkeys.py (MIT License)
# Copyright (c) 2026 MaxWay12
# HoYoLAB CRS bearing math, floor-mismatch cave-entrance / waypoint routing, TSP route queue mode,
# and proximity spot-card signaling are clean-room additions for Genshin-Helper v5.
"""Navigation Sticky Target Controller & 1-second Collected Hold Guard."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from time import monotonic
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

SpaceKey = Tuple[int, str]

ARRIVED_DISTANCE_M: float = 12.0
SPOT_CARD_DISTANCE_M: float = 20.0
# Proximity takeover: standing right next to another eligible pin while the locked target is
# noticeably farther away re-locks onto the pin at hand.
PROXIMITY_TAKEOVER_M: float = 12.0
PROXIMITY_TAKEOVER_MARGIN_M: float = 8.0
# Clearly-closer re-lock: an auto-selected target yields to a pin that is <= 75% as far AND
# at least 10 m nearer by raw distance (or effective rank), sustained for 0.5 s.
CLOSER_RATIO: float = 0.75
CLOSER_MARGIN_M: float = 10.0
CLOSER_DWELL_S: float = 0.5
# A position jump larger than this between two frames (teleport / waypoint) drops the auto lock.
TELEPORT_JUMP_M: float = 50.0
LOCK_ON_ANGLE_DEG: float = 12.0
BEHIND_ANGLE_DEG: float = 135.0


_GENERIC_PIN_NAMES = frozenset({"map pin", "điểm đánh dấu", "pin", "mini-hud chỉ đường"})


def _is_generic_pin_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return True
    if low in _GENERIC_PIN_NAMES:
        return True
    return low.startswith("pin ") and low[4:].replace(",", "").replace(".", "").replace("-", "").isdigit()


def _norm_floor_id(floor_id: Any) -> Optional[str]:
    if floor_id is None:
        return None
    cleaned = str(floor_id).strip()
    return cleaned if cleaned else None


def _opt_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# Smart pin prioritization: Distance > Camera Direction > Priority Tier
# (Oculi/Offerings > Shrines/Luxurious > Special Chests > Common Chests > Puzzles > Specialties > Other).
HEADING_BONUS_MAX_M: float = 8.0
HEADING_ALIGN_FULL_DEG: float = 35.0
HEADING_ALIGN_ZERO_DEG: float = 105.0
_TIER_BONUS_M: Dict[int, float] = {
    6: 8.0,
    5: 6.0,
    4: 4.5,
    3: 3.5,
    2: 2.0,
    1: 1.0,
    0: 0.0,
}

# HoYoLAB label_id sets (verified against live label/tree API + legacy category_aliases.json)
_TIER6_OCULUS_LABEL_IDS = frozenset({
    5, 6, 194, 403, 508, 626, 694, 833,   # Oculi (live)
    141, 357, 470, 554, 834, 791, 271,    # Agate, Lumenspar, Plume, Carp, Wing, Memory, Voucher
    147, 210, 345, 395, 559, 575,         # Legacy alias IDs
})
_TIER5_HIGH_VALUE_LABEL_IDS = frozenset({
    8, 9, 212, 411, 509, 577, 703, 835,   # Shrine of Depths
    806,                                  # Kuuhenki Shrine
})
_TIER4_CHEST_LABEL_IDS = frozenset({
    44, 45, 269, 236, 838, 879, 69, 75,
})
_TIER2_PUZZLE_LABEL_IDS = frozenset({
    18, 148, 205, 64, 70, 71, 77, 547, 600,
})

_TIER6_KEYWORDS = (
    "thần đồng", "oculus", "神瞳",
    "mã não", "crimson agate",
    "lưu minh", "lumenspar",
    "thương linh", "cá chép", "spirit carp",
    "tịnh quang vũ", "giọt cánh", "khối ký ức", "phiếu quà",
)
_TIER5_KEYWORDS = (
    "đền địa linh", "hòm địa linh", "shrine of depths", "đền kuuhenki",
    "rương hiếm", "rương hoa lệ", "luxurious chest", "华丽宝箱",
)
_TIER4_KEYWORDS = (
    "rương siêu cấp", "rương quý", "precious chest",
    "rương cao cấp", "rương tinh xảo", "exquisite chest",
    "rương kỳ dị", "remarkable chest",
    "rương snezhnaya", "rương báu",
)
_TIER3_KEYWORDS = ("rương thường", "common chest")
_TIER2_KEYWORDS = (
    "tiên linh", "seelie",
    "khiêu chiến", "time trial",
    "câu đố", "puzzle",
    "cơ quan", "huyền thoại", "bia nguyên tố", "challenge",
)
_TIER1_KEYWORDS = (
    "ngọc thạch", "spincrystal", "ấn khóa", "ốc biển",
    "điểm ngắm cảnh", "viewpoint", "đặc sản",
)


def _make_space_key(map_id: int, floor_id: Optional[str], is_underground: bool) -> SpaceKey:
    norm_floor = _norm_floor_id(floor_id)
    if norm_floor is not None:
        floor_tag = f"ug:{norm_floor}" if is_underground else f"sf:{norm_floor}"
    else:
        floor_tag = "ug" if is_underground else "surface"
    return (int(map_id), floor_tag)


def compute_hoyolab_bearing(
    player_lat: float,
    player_lng: float,
    target_lat: float,
    target_lng: float,
) -> Tuple[float, float]:
    """
    Computes `(distance, bearing_cw_north_deg)` in HoYoLAB world CRS.

    HoYoLAB world CRS (verified in `src/locator.py:2176-2179`):
        - `lng` (`world_x`) increases East (Right)
        - `lat` (`world_y`) increases South (Down); `-lat` is North (Up)
    Bearing convention (matches `detect_player_arrow` in `src/locator.py:562`):
        - 0° = North, 90° = East, 180° = South, 270° = West (clockwise).
    """
    dx = float(target_lng) - float(player_lng)
    dy = float(target_lat) - float(player_lat)
    dist = math.hypot(dx, dy)
    if dist <= 1e-9:
        return 0.0, 0.0
    bearing_deg = math.degrees(math.atan2(dx, -dy)) % 360.0
    return dist, bearing_deg


def normalize_angle_delta(bearing_deg: float, view_heading_deg: float) -> float:
    """
    Returns signed relative angle `delta_deg` in `[-180.0, 180.0]`:
        0° = straight ahead (press W), +90° = right (CW), -90° = left (CCW), ±180° = behind.
    """
    delta = (float(bearing_deg) - float(view_heading_deg) + 180.0) % 360.0 - 180.0
    if delta == -180.0 and ((float(bearing_deg) - float(view_heading_deg)) % 360.0) == 180.0:
        return 180.0
    return delta


@dataclass
class NavPin:
    """Navigation pin metadata in HoYoLAB world coordinates (`lat = world_y`, `lng = world_x`)."""

    id: str
    name: str
    lat: float
    lng: float
    map_id: int = 2
    floor_id: Optional[str] = None
    floor_name: str = ""
    is_underground: bool = False
    category: str = ""
    image_url: str = ""
    description: str = ""
    cave_entrance: Optional[Dict[str, Any]] = None
    label_id: Optional[int] = None
    parent_label_id: Optional[int] = None

    def __post_init__(self) -> None:
        self.id = str(self.id)
        self.name = str(self.name or "")
        self.lat = float(self.lat)
        self.lng = float(self.lng)
        self.map_id = int(self.map_id)
        self.floor_id = _norm_floor_id(self.floor_id)
        self.floor_name = str(self.floor_name or "")
        self.is_underground = bool(self.is_underground)
        self.category = str(self.category or "")
        self.image_url = str(self.image_url or "").strip()
        self.description = str(self.description or "").strip()
        if self.cave_entrance is not None:
            self.cave_entrance = dict(self.cave_entrance)
        self.label_id = _opt_int(self.label_id)
        self.parent_label_id = _opt_int(self.parent_label_id)

    @property
    def space_key(self) -> SpaceKey:
        return _make_space_key(self.map_id, self.floor_id, self.is_underground)

    @property
    def has_spot_content(self) -> bool:
        return bool(self.image_url or self.description)

    @property
    def display_name(self) -> str:
        """Human label for HUD: explicit pin name, else HoYoLAB category (label) name."""
        name = self.name.strip()
        if name and not _is_generic_pin_name(name):
            return name
        cat = self.category.strip()
        if cat and not cat.isdigit():
            return cat
        return name

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NavPin":
        lat_val = data.get("lat", data.get("y_pos", 0.0))
        lng_val = data.get("lng", data.get("x_pos", 0.0))
        cave = data.get("cave_entrance")
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            lat=float(lat_val),
            lng=float(lng_val),
            map_id=int(data.get("map_id", 2)),
            floor_id=_norm_floor_id(data.get("floor_id")),
            floor_name=str(data.get("floor_name", "")),
            is_underground=bool(data.get("is_underground", False)),
            category=str(data.get("category", "")),
            image_url=str(data.get("image_url", "")),
            description=str(data.get("description", "")),
            cave_entrance=dict(cave) if isinstance(cave, Mapping) else None,
            label_id=_opt_int(data.get("label_id", data.get("labelId"))),
            parent_label_id=_opt_int(data.get("parent_label_id", data.get("parentLabelId"))),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "lat": self.lat,
            "lng": self.lng,
            "map_id": self.map_id,
            "floor_id": self.floor_id,
            "floor_name": self.floor_name,
            "is_underground": self.is_underground,
            "category": self.category,
            "image_url": self.image_url,
            "description": self.description,
            "cave_entrance": dict(self.cave_entrance) if self.cave_entrance is not None else None,
            "label_id": self.label_id,
            "parent_label_id": self.parent_label_id,
        }


def pin_priority_tier(pin: NavPin) -> int:
    """
    Returns pin value tier 0..6 (Oculi/Offerings > Shrines/Luxurious > Chests > Puzzles > Specialties > Other).
    Used for distance-first tie-breaking when nearby pins compete for navigation lock.
    """
    lid = pin.label_id
    parent_lid = pin.parent_label_id
    text = f"{pin.category} {pin.name}".lower()

    if (lid is not None and lid in _TIER6_OCULUS_LABEL_IDS) or any(kw in text for kw in _TIER6_KEYWORDS):
        return 6
    if (
        (lid is not None and lid in _TIER5_HIGH_VALUE_LABEL_IDS)
        or (lid == 46 and parent_lid in (None, 13))
        or any(kw in text for kw in _TIER5_KEYWORDS)
    ):
        return 5
    if (lid is not None and lid in _TIER4_CHEST_LABEL_IDS) or any(kw in text for kw in _TIER4_KEYWORDS):
        return 4
    if (
        parent_lid == 13
        or (lid == 17 and parent_lid is None and not any(kw in text for kw in _TIER2_KEYWORDS))
        or any(kw in text for kw in _TIER3_KEYWORDS)
        or (("rương" in text or "chest" in text or "宝箱" in text) and "rương giải đố" not in text)
    ):
        return 3
    if (
        parent_lid == 186
        or (lid is not None and lid in _TIER2_PUZZLE_LABEL_IDS)
        or any(kw in text for kw in _TIER2_KEYWORDS)
    ):
        return 2
    if parent_lid in (4, 10, 51) or any(kw in text for kw in _TIER1_KEYWORDS):
        return 1
    return 0


def camera_alignment_score(bearing_deg: float, view_heading_deg: Optional[float]) -> float:
    """
    Returns camera alignment weight in [0.0, 1.0]:
    1.0 within ±35° ahead of camera, smooth cosine roll-off across 35°..105°, 0.0 behind.
    """
    if view_heading_deg is None:
        return 0.0
    try:
        heading = float(view_heading_deg)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(heading):
        return 0.0
    abs_delta = abs(normalize_angle_delta(bearing_deg, heading))
    if abs_delta <= HEADING_ALIGN_FULL_DEG:
        return 1.0
    if abs_delta >= HEADING_ALIGN_ZERO_DEG:
        return 0.0
    frac = (abs_delta - HEADING_ALIGN_FULL_DEG) / (HEADING_ALIGN_ZERO_DEG - HEADING_ALIGN_FULL_DEG)
    return 0.5 * (1.0 + math.cos(math.pi * frac))


def pin_effective_distance(
    pin: NavPin,
    dist_m: float,
    *,
    bearing_deg: Optional[float] = None,
    view_heading_deg: Optional[float] = None,
) -> float:
    """Subtracts bounded camera-alignment and value bonuses (Distance > Camera Direction > Priority)."""
    d = float(dist_m)
    heading_bonus = 0.0
    if bearing_deg is not None and view_heading_deg is not None:
        align = camera_alignment_score(bearing_deg, view_heading_deg)
        heading_bonus = min(HEADING_BONUS_MAX_M * align, d * 0.22)
    tier_bonus = min(_TIER_BONUS_M.get(pin_priority_tier(pin), 0.0), d * 0.25)
    return d - heading_bonus - tier_bonus


def pin_rank_key(
    pin: NavPin,
    player_lat: float,
    player_lng: float,
    *,
    view_heading_deg: Optional[float] = None,
) -> Tuple[float, float, int, str]:
    """Sort key: effective distance (Distance > Camera Direction > Priority), raw distance, priority, ID."""
    dist, bearing = compute_hoyolab_bearing(player_lat, player_lng, pin.lat, pin.lng)
    return (
        pin_effective_distance(
            pin,
            dist,
            bearing_deg=bearing,
            view_heading_deg=view_heading_deg,
        ),
        dist,
        -pin_priority_tier(pin),
        pin.id,
    )


@dataclass(frozen=True)
class NavGuidance:
    """Computed navigation HUD payload for a single tracker frame."""

    state: str  # "tracking" | "arrived" | "floor_mismatch" | "lost" | "idle"
    target: Optional[NavPin]
    distance_m: Optional[float]
    target_distance_m: Optional[float]
    bearing_deg: Optional[float]
    delta_deg: Optional[float]
    floor_mismatch: bool
    player_floor_id: Optional[str] = None
    player_is_underground: bool = False
    target_floor_id: Optional[str] = None
    target_floor_name: str = ""
    target_is_underground: bool = False
    route_mode: str = "direct"  # "direct" | "cave_entrance" | "waypoint" | "none"
    route_target_name: str = ""
    route_target_lat: Optional[float] = None
    route_target_lng: Optional[float] = None
    show_spot_card: bool = False
    is_locked_on: bool = False
    is_behind: bool = False
    route_step: Optional[int] = None
    route_total: Optional[int] = None
    remaining_count: int = 0


@dataclass(frozen=True)
class NavigationSummary:
    total: int
    collected: int
    remaining: int
    session_skipped: int
    blacklisted: int
    route_active: bool = False
    route_step: Optional[int] = None
    route_total: Optional[int] = None


@dataclass(frozen=True)
class _HistoryEntry:
    action: str  # "collected" | "skip" | "blacklist"
    pin_id: str
    space_key: SpaceKey
    prev_override_id: Optional[str]
    prev_route_index: Optional[int]


def _extract_cave_entrance(pin: NavPin) -> Optional[Tuple[float, float, str]]:
    cave = pin.cave_entrance
    if not isinstance(cave, Mapping):
        return None
    if "lat" not in cave or "lng" not in cave:
        return None
    try:
        lat = float(cave["lat"])
        lng = float(cave["lng"])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    name = str(cave.get("name", "") or "").strip()
    return (lat, lng, name)


def _extract_waypoint_coords(
    wp: Any,
    player_map_id: int,
    player_floor_id: Optional[str],
    player_is_underground: bool,
) -> Optional[Tuple[float, float, str]]:
    if isinstance(wp, NavPin):
        wp_map = wp.map_id
        wp_lat = wp.lat
        wp_lng = wp.lng
        wp_ug = wp.is_underground
        wp_floor = wp.floor_id
        wp_name = wp.name
    elif isinstance(wp, Mapping):
        wp_map = int(wp.get("map_id", player_map_id))
        lat_raw = wp.get("lat", wp.get("y_pos"))
        lng_raw = wp.get("lng", wp.get("x_pos"))
        if lat_raw is None or lng_raw is None:
            return None
        try:
            wp_lat = float(lat_raw)
            wp_lng = float(lng_raw)
        except (TypeError, ValueError):
            return None
        wp_ug = bool(wp.get("is_underground", False))
        wp_floor = _norm_floor_id(wp.get("floor_id"))
        wp_name = str(wp.get("name", "") or "").strip()
    else:
        return None

    if wp_map != int(player_map_id):
        return None
    if wp_ug != bool(player_is_underground):
        return None
    p_floor = _norm_floor_id(player_floor_id)
    if wp_floor is not None and p_floor is not None and wp_floor != p_floor:
        return None
    if not (math.isfinite(wp_lat) and math.isfinite(wp_lng)):
        return None
    return (wp_lat, wp_lng, wp_name)


def _is_floor_mismatch(
    target: NavPin,
    player_floor_id: Optional[str],
    player_is_underground: bool,
) -> bool:
    if bool(target.is_underground) != bool(player_is_underground):
        return True
    t_floor = _norm_floor_id(target.floor_id)
    p_floor = _norm_floor_id(player_floor_id)
    if t_floor is not None and p_floor is not None and t_floor != p_floor:
        return True
    return False


class NavigationController:
    """
    Sticky Target navigation controller with `(map_id, floor_id)` scoping,
    session skip, persistent blacklist, full undo stack, optional TSP route queue,
    and floor-mismatch cave-entrance / waypoint redirection.

    Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/navigation.py (MIT).
    """

    def __init__(
        self,
        pins: Optional[Sequence[NavPin]] = None,
        *,
        arrived_distance_m: float = ARRIVED_DISTANCE_M,
        spot_card_distance_m: float = SPOT_CARD_DISTANCE_M,
        waypoints: Optional[Sequence[Any]] = None,
        collected_ids: Optional[Iterable[str]] = None,
        blacklisted_ids: Optional[Iterable[str]] = None,
        space_debounce_frames: int = 0,
    ) -> None:
        # Frames a new (map_id, floor) space must persist before the controller follows it.
        # 0 = switch immediately (unit tests / explicit callers); the live loop uses a few frames
        # so a flickering floor/underground detection cannot flip the locked target back and forth.
        self.space_debounce_frames = max(0, int(space_debounce_frames))
        self._pending_space: Optional[SpaceKey] = None
        self._pending_space_frames: int = 0
        self.arrived_distance_m = max(0.5, float(arrived_distance_m))
        self.spot_card_distance_m = max(self.arrived_distance_m, float(spot_card_distance_m))

        self._pins_by_id: Dict[str, NavPin] = {}
        self._pin_order: List[str] = []
        self._waypoints: List[Any] = list(waypoints) if waypoints else []

        self.collected_ids: Set[str] = {str(x) for x in (collected_ids or ())}
        self.skipped_ids: Set[str] = set()
        self.blacklisted_ids: Set[str] = {str(x) for x in (blacklisted_ids or ())}

        # Sticky Target state per (map_id, floor_tag) space
        self._selected_by_space: Dict[SpaceKey, str] = {}
        self._orders: Dict[SpaceKey, List[str]] = {}
        self._last_pos_by_space: Dict[SpaceKey, Tuple[float, float]] = {}
        self._last_heading_by_space: Dict[SpaceKey, float] = {}
        self._active_space: Optional[SpaceKey] = None
        self._pinned_override_id: Optional[str] = None
        # Pin the user picked by hand (select / next / prev): never taken over by proximity
        self._user_locked_id: Optional[str] = None
        # Clearly-closer dwell state (injectable clock for tests)
        self._clock = monotonic
        self._closer_candidate: Optional[str] = None
        self._closer_since: float = 0.0

        # Optional TSP route queue state
        self._route_queue: Optional[List[str]] = None
        self._route_index: int = 0

        # Full undo stack (collect, skip, blacklist)
        self._history: List[_HistoryEntry] = []

        # Tracker readiness (True until compute_guidance reports non-TRACKING)
        self._selection_ready: bool = True
        self._last_guidance: Optional[NavGuidance] = None

        # Pins removed by user actions (for undo re-insertion) and short-lived protection so a
        # just-undone pin survives catalog refreshes until the web map un-marks it as well.
        self._history_pins: Dict[str, NavPin] = {}
        self._protected_until: Dict[str, float] = {}

        if pins:
            self.set_pins(pins)

    # ------------------------------------------------------------------
    # Catalog & Route Queue configuration
    # ------------------------------------------------------------------
    @property
    def pins(self) -> Tuple[NavPin, ...]:
        return tuple(self._pins_by_id[pid] for pid in self._pin_order if pid in self._pins_by_id)

    # ------------------------------------------------------------------
    # Read-only helpers for hotkey handlers / HUD
    # ------------------------------------------------------------------
    @property
    def last_guidance(self) -> Optional[NavGuidance]:
        return self._last_guidance

    @property
    def selection_ready(self) -> bool:
        return self._selection_ready

    def get_pin(self, pin_id: Optional[str]) -> Optional[NavPin]:
        if pin_id is None:
            return None
        pid = str(pin_id)
        return self._pins_by_id.get(pid) or self._history_pins.get(pid)

    def peek_history(self) -> Optional[Tuple[str, str]]:
        """Returns `(action, pin_id)` of the action `undo()` would revert, or None."""
        if not self._history:
            return None
        entry = self._history[-1]
        return (entry.action, entry.pin_id)

    def protect_pin(self, pin_id: str, seconds: float = 8.0) -> None:
        self._protected_until[str(pin_id)] = monotonic() + max(0.0, float(seconds))

    def _is_protected(self, pin_id: str) -> bool:
        until = self._protected_until.get(pin_id)
        if until is None:
            return False
        if monotonic() >= until:
            self._protected_until.pop(pin_id, None)
            return False
        return True

    def _retained_ids(self) -> Set[str]:
        """IDs that must survive a partial (nearest-N) catalog refresh: locked targets + route stops."""
        keep: Set[str] = set(self._selected_by_space.values())
        keep.update(pid for pid in list(self._protected_until) if self._is_protected(pid))
        if self._pinned_override_id is not None:
            keep.add(self._pinned_override_id)
        if self._route_queue:
            keep.update(self._route_queue)
        return keep

    def set_pins(
        self,
        pins: Sequence[NavPin],
        *,
        reset_order: bool = True,
        coverage: Optional[Tuple[float, float, float]] = None,
    ) -> None:
        """
        Replaces the pin catalog.

        `coverage=(center_lat, center_lng, radius)` describes the area the browser actually
        searched. A locked target (or route stop) missing from `pins` is kept when it lies OUTSIDE
        that area (it merely fell out of the nearest-N window, so dropping it would make the HUD
        arrow jump to another pin), and dropped when it lies INSIDE (the web no longer lists it,
        i.e. it was marked/collected there). Without `coverage`, route stops are still retained.
        """
        new_by_id: Dict[str, NavPin] = {}
        new_order: List[str] = []
        for item in pins:
            pin = item if isinstance(item, NavPin) else NavPin.from_dict(item)
            if pin.id not in new_by_id:
                new_order.append(pin.id)
            new_by_id[pin.id] = pin

        cov: Optional[Tuple[float, float, float]] = None
        if coverage is not None:
            try:
                c_lat, c_lng, c_rad = (float(v) for v in coverage)
                if math.isfinite(c_lat) and math.isfinite(c_lng) and math.isfinite(c_rad) and c_rad > 0:
                    cov = (c_lat, c_lng, c_rad)
            except (TypeError, ValueError):
                cov = None

        route_ids = set(self._route_queue or ())
        for pid in self._retained_ids():
            if pid in new_by_id:
                continue
            old = self._pins_by_id.get(pid)
            if old is None:
                continue
            if self._is_protected(pid):
                pass  # Just undone in-game: keep until the web map catches up
            elif cov is not None:
                c_lat, c_lng, c_rad = cov
                inside = math.hypot(old.lat - c_lat, old.lng - c_lng) <= c_rad * 0.97
                if inside and old.map_id == int(self._active_space[0] if self._active_space else old.map_id):
                    continue  # Browser searched here and no longer lists it -> collected on web
            elif pid not in route_ids:
                continue  # Legacy (no coverage info): only route stops are retained
            new_by_id[pid] = old
            new_order.append(pid)

        self._pins_by_id = new_by_id
        self._pin_order = new_order

        if reset_order:
            self._orders.clear()
        # Prune stale selections that no longer exist in catalog
        for sk in list(self._selected_by_space.keys()):
            if self._selected_by_space[sk] not in self._pins_by_id:
                self._selected_by_space.pop(sk, None)
        if self._pinned_override_id not in self._pins_by_id:
            self._pinned_override_id = None
        if self._active_space is None and new_order:
            first_pin = self._pins_by_id[new_order[0]]
            self._active_space = first_pin.space_key

    def set_waypoints(self, waypoints: Sequence[Any]) -> None:
        self._waypoints = list(waypoints) if waypoints else []

    def set_route_queue(self, ordered_pins: Optional[Sequence[Any]]) -> None:
        """
        Enables TSP Route Queue mode with an ordered sequence of `NavPin` objects or pin IDs.
        Passing `None` or an empty sequence clears route queue mode back to Sticky Target mode.
        """
        if not ordered_pins:
            self._route_queue = None
            self._route_index = 0
            return

        queue_ids: List[str] = []
        seen: Set[str] = set()
        for item in ordered_pins:
            if isinstance(item, NavPin):
                if item.id not in self._pins_by_id:
                    self._pins_by_id[item.id] = item
                    self._pin_order.append(item.id)
                pid = item.id
            elif isinstance(item, Mapping):
                pin = NavPin.from_dict(item)
                if pin.id not in self._pins_by_id:
                    self._pins_by_id[pin.id] = pin
                    self._pin_order.append(pin.id)
                pid = pin.id
            else:
                pid = str(item)
                if pid not in self._pins_by_id:
                    continue
            if pid not in seen:
                seen.add(pid)
                queue_ids.append(pid)

        if not queue_ids:
            self._route_queue = None
            self._route_index = 0
            return

        self._route_queue = queue_ids
        self._route_index = 0
        self._pinned_override_id = None
        first_pin = self._pins_by_id[queue_ids[0]]
        if self._active_space is None:
            self._active_space = first_pin.space_key
        self._sync_route_index_forward()

    def clear_route_queue(self) -> None:
        self._route_queue = None
        self._route_index = 0

    @property
    def is_route_mode(self) -> bool:
        return self._route_queue is not None and len(self._route_queue) > 0

    @property
    def route_progress(self) -> Optional[Tuple[int, int]]:
        if not self._route_queue:
            return None
        total = len(self._route_queue)
        if self._route_index >= total:
            return (total, total)
        return (self._route_index + 1, total)

    # ------------------------------------------------------------------
    # Filtering & Sticky Target ordering
    # ------------------------------------------------------------------
    def _excluded_ids(self) -> Set[str]:
        return self.collected_ids | self.skipped_ids | self.blacklisted_ids

    def _eligible_for_space(self, space_key: SpaceKey) -> List[NavPin]:
        excluded = self._excluded_ids()
        map_id, _ = space_key
        same_space: List[NavPin] = []
        for pid in self._pin_order:
            if pid in excluded:
                continue
            pin = self._pins_by_id[pid]
            if pin.space_key == space_key:
                same_space.append(pin)
        if same_space:
            return same_space

        # Fallback: no eligible pins on exact floor -> allow cross-floor pins on same map
        # so floor-mismatch routing can guide player to cave entrance / waypoint.
        same_map: List[NavPin] = []
        for pid in self._pin_order:
            if pid in excluded:
                continue
            pin = self._pins_by_id[pid]
            if pin.map_id == map_id:
                same_map.append(pin)
        return same_map

    def _ordered_for_space(
        self,
        space_key: SpaceKey,
        player_pos: Optional[Tuple[float, float]] = None,
        view_heading_deg: Optional[float] = None,
    ) -> List[NavPin]:
        eligible = self._eligible_for_space(space_key)
        if not eligible:
            self._orders.pop(space_key, None)
            return []

        pos = player_pos if player_pos is not None else self._last_pos_by_space.get(space_key)
        heading = view_heading_deg if view_heading_deg is not None else self._last_heading_by_space.get(space_key)
        by_id = {pin.id: pin for pin in eligible}
        order = self._orders.get(space_key, [])

        # Preserve frozen candidate order so movement does NOT reshuffle targets mid-run.
        if not any(pid in by_id for pid in order):
            if pos is not None:
                plat, plng = pos
                sorted_pins = sorted(
                    eligible,
                    key=lambda p: pin_rank_key(p, plat, plng, view_heading_deg=heading),
                )
            else:
                sorted_pins = list(eligible)
            order = [p.id for p in sorted_pins]
            self._orders[space_key] = order

        ordered = [by_id[pid] for pid in order if pid in by_id]
        # Append any newly added eligible pins without perturbing existing order
        existing = {p.id for p in ordered}
        extras = [p for p in eligible if p.id not in existing]
        if extras:
            if pos is not None:
                plat, plng = pos
                extras.sort(key=lambda p: pin_rank_key(p, plat, plng, view_heading_deg=heading))
            ordered.extend(extras)
            self._orders[space_key] = [p.id for p in ordered]
        return ordered

    def _sync_route_index_forward(self) -> Optional[NavPin]:
        if not self._route_queue:
            return None
        excluded = self._excluded_ids()
        for idx in range(max(0, self._route_index), len(self._route_queue)):
            pid = self._route_queue[idx]
            if pid not in excluded and pid in self._pins_by_id:
                self._route_index = idx
                return self._pins_by_id[pid]
        self._route_index = len(self._route_queue)
        return None

    def _resolve_target(
        self,
        space_key: SpaceKey,
        player_pos: Optional[Tuple[float, float]] = None,
        view_heading_deg: Optional[float] = None,
    ) -> Optional[NavPin]:
        excluded = self._excluded_ids()

        # 1. Active TSP Route Queue takes precedence
        if self._route_queue is not None:
            pin = self._sync_route_index_forward()
            if pin is not None:
                self._selected_by_space[space_key] = pin.id
            else:
                self._selected_by_space.pop(space_key, None)
            return pin

        # 2. Explicit cross-floor / user-pinned override
        if self._pinned_override_id is not None:
            override = self._pins_by_id.get(self._pinned_override_id)
            if (
                override is not None
                and override.id not in excluded
                and override.map_id == space_key[0]
            ):
                self._selected_by_space[space_key] = override.id
                return override
            self._pinned_override_id = None

        heading = view_heading_deg if view_heading_deg is not None else self._last_heading_by_space.get(space_key)
        # 3. Sticky Target for (map_id, floor_tag)
        candidates = self._ordered_for_space(space_key, player_pos=player_pos, view_heading_deg=heading)
        if not candidates:
            self._selected_by_space.pop(space_key, None)
            return None

        by_id = {pin.id: pin for pin in candidates}
        selected_id = self._selected_by_space.get(space_key)
        if selected_id is not None and selected_id in by_id:
            return by_id[selected_id]

        # No lock yet (first frame, or previous target collected/vanished): lock onto the pin
        # best matching Distance > Camera Direction > Priority from where the player stands NOW.
        pos = player_pos if player_pos is not None else self._last_pos_by_space.get(space_key)
        chosen = self._nearest_of(candidates, pos, view_heading_deg=heading) or candidates[0]
        self._selected_by_space[space_key] = chosen.id
        return chosen

    @staticmethod
    def _nearest_of(
        candidates: Sequence[NavPin],
        pos: Optional[Tuple[float, float]],
        view_heading_deg: Optional[float] = None,
    ) -> Optional[NavPin]:
        if not candidates or pos is None:
            return None
        plat, plng = pos
        return min(candidates, key=lambda p: pin_rank_key(p, plat, plng, view_heading_deg=view_heading_deg))

    @property
    def current_target(self) -> Optional[NavPin]:
        excluded = self._excluded_ids()
        if self._route_queue is not None:
            if 0 <= self._route_index < len(self._route_queue):
                pid = self._route_queue[self._route_index]
                if pid not in excluded and pid in self._pins_by_id:
                    return self._pins_by_id[pid]
            return self._sync_route_index_forward()

        if self._pinned_override_id is not None:
            override = self._pins_by_id.get(self._pinned_override_id)
            if override is not None and override.id not in excluded:
                return override

        if self._active_space is None:
            return None
        pid = self._selected_by_space.get(self._active_space)
        if pid is not None and pid not in excluded and pid in self._pins_by_id:
            return self._pins_by_id[pid]
        return self._resolve_target(self._active_space)

    @property
    def summary(self) -> NavigationSummary:
        if self._route_queue is not None:
            ids = {pid for pid in self._route_queue if pid in self._pins_by_id}
        elif self._active_space is not None:
            map_id = self._active_space[0]
            ids = {pid for pid, p in self._pins_by_id.items() if p.map_id == map_id}
        else:
            ids = set(self._pins_by_id.keys())

        collected = len(ids & self.collected_ids)
        skipped = len(ids & self.skipped_ids)
        blacklisted = len(ids & self.blacklisted_ids)
        remaining = max(0, len(ids - (self.collected_ids | self.skipped_ids | self.blacklisted_ids)))
        rp = self.route_progress
        return NavigationSummary(
            total=len(ids),
            collected=collected,
            remaining=remaining,
            session_skipped=skipped,
            blacklisted=blacklisted,
            route_active=self.is_route_mode,
            route_step=rp[0] if rp else None,
            route_total=rp[1] if rp else None,
        )

    # ------------------------------------------------------------------
    # User Navigation Commands (Next / Prev / Skip / Blacklist / Mark / Undo / Refresh)
    # ------------------------------------------------------------------
    def select_target(self, pin_id: str) -> Optional[NavPin]:
        pid = str(pin_id)
        if pid not in self._pins_by_id or pid in self._excluded_ids():
            return None
        pin = self._pins_by_id[pid]
        if self._route_queue is not None:
            if pid not in self._route_queue:
                return None
            self._route_index = self._route_queue.index(pid)
            if self._active_space is not None:
                self._selected_by_space[self._active_space] = pid
            return pin

        if self._active_space is None:
            self._active_space = pin.space_key
        self._selected_by_space[self._active_space] = pid
        self._user_locked_id = pid
        if pin.space_key != self._active_space:
            self._pinned_override_id = pid
        else:
            self._pinned_override_id = None
        return pin

    def _cycle(self, offset: int) -> Optional[NavPin]:
        if not self._selection_ready:
            return self.current_target

        if self._route_queue is not None:
            excluded = self._excluded_ids()
            total = len(self._route_queue)
            if total == 0:
                return None
            if offset > 0:
                for idx in range(self._route_index + 1, total):
                    pid = self._route_queue[idx]
                    if pid not in excluded and pid in self._pins_by_id:
                        self._route_index = idx
                        if self._active_space is not None:
                            self._selected_by_space[self._active_space] = pid
                        return self._pins_by_id[pid]
                return self.current_target
            if offset < 0:
                for idx in range(min(self._route_index - 1, total - 1), -1, -1):
                    pid = self._route_queue[idx]
                    if pid not in excluded and pid in self._pins_by_id:
                        self._route_index = idx
                        if self._active_space is not None:
                            self._selected_by_space[self._active_space] = pid
                        return self._pins_by_id[pid]
                return self.current_target
            return self.current_target

        if self._active_space is None:
            return None
        self._pinned_override_id = None
        candidates = self._ordered_for_space(self._active_space)
        if not candidates:
            self._selected_by_space.pop(self._active_space, None)
            return None

        current = self.current_target
        cur_idx = next((i for i, p in enumerate(candidates) if current is not None and p.id == current.id), 0)
        chosen = candidates[(cur_idx + offset) % len(candidates)]
        self._selected_by_space[self._active_space] = chosen.id
        return chosen

    def next_target(self) -> Optional[NavPin]:
        pin = self._cycle(1)
        self._user_locked_id = pin.id if pin is not None else None
        return pin

    def prev_target(self) -> Optional[NavPin]:
        pin = self._cycle(-1)
        self._user_locked_id = pin.id if pin is not None else None
        return pin

    def _proximity_takeover(
        self,
        space_key: SpaceKey,
        target: NavPin,
        player_pos: Tuple[float, float],
        view_heading_deg: Optional[float] = None,
    ) -> NavPin:
        """Re-lock onto an eligible same-space pin the player is standing next to."""
        if self._route_queue is not None or self._pinned_override_id is not None:
            return target
        if self._user_locked_id is not None and self._user_locked_id == target.id:
            return target
        if target.space_key != space_key:
            return target
        plat, plng = player_pos
        heading = view_heading_deg if view_heading_deg is not None else self._last_heading_by_space.get(space_key)
        target_dist, target_bearing = compute_hoyolab_bearing(plat, plng, target.lat, target.lng)
        target_eff = pin_effective_distance(
            target,
            target_dist,
            bearing_deg=target_bearing,
            view_heading_deg=heading,
        )
        target_ahead = heading is not None and abs(normalize_angle_delta(target_bearing, heading)) <= 75.0

        closest_near: Optional[NavPin] = None
        closest_near_key: Optional[Tuple[float, int, str]] = None
        closest_near_dist = float("inf")
        best_ranked: Optional[NavPin] = None
        best_rank: Optional[Tuple[float, float, int, str]] = None
        cand_bearings: Dict[str, float] = {}

        for pin in self._eligible_for_space(space_key):
            if pin.id == target.id or pin.space_key != space_key:
                continue
            d, b = compute_hoyolab_bearing(plat, plng, pin.lat, pin.lng)
            cand_bearings[pin.id] = b
            tier = pin_priority_tier(pin)
            near_key = (d, -tier, pin.id)
            if closest_near_key is None or near_key < closest_near_key:
                closest_near_key = near_key
                closest_near_dist = d
                closest_near = pin
            eff_d = pin_effective_distance(pin, d, bearing_deg=b, view_heading_deg=heading)
            rk = (eff_d, d, -tier, pin.id)
            if best_rank is None or rk < best_rank:
                best_rank = rk
                best_ranked = pin

        if (
            closest_near is not None
            and closest_near_dist <= PROXIMITY_TAKEOVER_M
            and target_dist > closest_near_dist + PROXIMITY_TAKEOVER_MARGIN_M
        ):
            self._selected_by_space[space_key] = closest_near.id
            self._closer_candidate = None
            return closest_near

        candidate: Optional[NavPin] = None
        if closest_near is not None:
            near_behind = (
                heading is not None
                and abs(normalize_angle_delta(cand_bearings[closest_near.id], heading)) > 110.0
            )
            raw_closer = (
                closest_near_dist <= CLOSER_RATIO * target_dist
                and (target_dist - closest_near_dist) >= CLOSER_MARGIN_M
            )
            if raw_closer and not (target_ahead and near_behind):
                candidate = closest_near
        if candidate is None and best_ranked is not None and best_rank is not None:
            best_eff, best_dist = best_rank[0], best_rank[1]
            best_behind = (
                heading is not None
                and abs(normalize_angle_delta(cand_bearings[best_ranked.id], heading)) > 110.0
            )
            eff_closer = (
                best_dist < target_dist
                and best_eff <= CLOSER_RATIO * target_eff
                and (target_eff - best_eff) >= CLOSER_MARGIN_M
            )
            if eff_closer and not (target_ahead and best_behind):
                candidate = best_ranked
        if candidate is not None:
            now = float(self._clock())
            if self._closer_candidate != candidate.id:
                self._closer_candidate = candidate.id
                self._closer_since = now
            elif (now - self._closer_since) >= CLOSER_DWELL_S:
                self._selected_by_space[space_key] = candidate.id
                self._closer_candidate = None
                return candidate
            return target
        self._closer_candidate = None
        return target

    def _relock_after_jump(self, space_key: SpaceKey) -> None:
        """Teleport / big jump: forget the auto-selected lock so the nearest pin is chosen again."""
        if self._route_queue is not None or self._pinned_override_id is not None:
            return
        selected = self._selected_by_space.get(space_key)
        if selected is not None and selected == self._user_locked_id:
            return
        self._selected_by_space.pop(space_key, None)
        self._orders.pop(space_key, None)
        self._closer_candidate = None

    previous_target = prev_target

    def _advance_after_removal(
        self,
        removed_id: str,
        space_key: SpaceKey,
        *,
        prefer_nearest: bool = False,
    ) -> Optional[NavPin]:
        if self._route_queue is not None:
            if (
                0 <= self._route_index < len(self._route_queue)
                and self._route_queue[self._route_index] == removed_id
            ):
                self._route_index = min(len(self._route_queue), self._route_index + 1)
            return self._resolve_target(space_key)

        if self._pinned_override_id == removed_id:
            self._pinned_override_id = None

        if prefer_nearest:
            # After collecting, the natural next target obeys Distance > Camera Direction > Priority.
            eligible = self._eligible_for_space(space_key)
            nearest = self._nearest_of(
                eligible,
                self._last_pos_by_space.get(space_key),
                view_heading_deg=self._last_heading_by_space.get(space_key),
            )
            if nearest is not None:
                self._selected_by_space[space_key] = nearest.id
                return self._resolve_target(space_key)

        order = self._orders.get(space_key, [])
        idx = order.index(removed_id) if removed_id in order else -1
        eligible_ids = {p.id for p in self._eligible_for_space(space_key)}
        next_id = next(
            (pid for pid in order[idx + 1 :] + order[: idx + 1] if pid in eligible_ids),
            None,
        )
        if next_id is None:
            self._selected_by_space.pop(space_key, None)
        else:
            self._selected_by_space[space_key] = next_id
        return self._resolve_target(space_key)

    def skip_current(self) -> Optional[NavPin]:
        """Session-only skip for the current target; reversible via `undo()`."""
        target = self.current_target
        if target is None or not self._selection_ready:
            return None
        space_key = self._active_space or target.space_key
        prev_route_idx = self._route_index if self._route_queue is not None else None
        self.skipped_ids.add(target.id)
        self._history_pins[target.id] = target
        self._history.append(
            _HistoryEntry("skip", target.id, space_key, self._pinned_override_id, prev_route_idx)
        )
        return self._advance_after_removal(target.id, space_key)

    def blacklist_current(self) -> Optional[NavPin]:
        """Hides current target from recommendations; reversible via `undo()`."""
        target = self.current_target
        if target is None or not self._selection_ready:
            return None
        space_key = self._active_space or target.space_key
        prev_route_idx = self._route_index if self._route_queue is not None else None
        self.blacklisted_ids.add(target.id)
        self._history_pins[target.id] = target
        self._history.append(
            _HistoryEntry("blacklist", target.id, space_key, self._pinned_override_id, prev_route_idx)
        )
        return self._advance_after_removal(target.id, space_key)

    def mark_collected(self, pin_id: Optional[str] = None) -> Optional[NavPin]:
        """
        Marks current target (or explicit `pin_id`) as collected, pushes undo entry,
        and advances to the next target (auto-advances route step in TSP queue mode).
        """
        if not self._selection_ready:
            return None
        target = self._pins_by_id.get(str(pin_id)) if pin_id is not None else self.current_target
        if target is None:
            return None
        space_key = self._active_space or target.space_key
        prev_route_idx = self._route_index if self._route_queue is not None else None
        current = self.current_target
        if pin_id is not None and current is not None and current.id != target.id:
            # Marking some other pin (e.g. clicked on the web map) must not move the locked target.
            self.collected_ids.add(target.id)
            self._history_pins[target.id] = target
            self._history.append(
                _HistoryEntry("collected", target.id, space_key, self._pinned_override_id, prev_route_idx)
            )
            return current
        self.collected_ids.add(target.id)
        self._history_pins[target.id] = target
        self._history.append(
            _HistoryEntry("collected", target.id, space_key, self._pinned_override_id, prev_route_idx)
        )
        return self._advance_after_removal(target.id, space_key, prefer_nearest=True)

    def undo(self) -> Optional[NavPin]:
        """Reverses the most recent `mark_collected`, `skip_current`, or `blacklist_current`."""
        if not self._history:
            return self.current_target
        entry = self._history.pop()
        if entry.pin_id not in self._pins_by_id and entry.pin_id in self._history_pins:
            # The web map already dropped it from the nearby feed -> put it back
            self._pins_by_id[entry.pin_id] = self._history_pins[entry.pin_id]
            self._pin_order.append(entry.pin_id)
        self.protect_pin(entry.pin_id)
        if entry.action == "skip":
            self.skipped_ids.discard(entry.pin_id)
        elif entry.action == "blacklist":
            self.blacklisted_ids.discard(entry.pin_id)
        else:
            self.collected_ids.discard(entry.pin_id)

        self._active_space = entry.space_key
        self._selected_by_space[entry.space_key] = entry.pin_id
        self._pinned_override_id = entry.prev_override_id

        if self._route_queue is not None and entry.prev_route_index is not None:
            self._route_index = max(0, min(len(self._route_queue) - 1, entry.prev_route_index))
        return self.current_target

    def rollback_collected(self, pin_id: str) -> Optional[NavPin]:
        """Reverts a specific optimistic `mark_collected` after the web map failed to confirm it."""
        pid = str(pin_id)
        if pid not in self.collected_ids:
            return None
        if pid not in self._pins_by_id and pid in self._history_pins:
            self._pins_by_id[pid] = self._history_pins[pid]
            self._pin_order.append(pid)
        self.collected_ids.discard(pid)
        matched_entry: Optional[_HistoryEntry] = None
        for idx in range(len(self._history) - 1, -1, -1):
            entry = self._history[idx]
            if entry.action == "collected" and entry.pin_id == pid:
                matched_entry = self._history.pop(idx)
                break
        self.protect_pin(pid, seconds=5.0)
        pin = self._pins_by_id.get(pid)
        if pin is None:
            return None
        target_space = matched_entry.space_key if matched_entry is not None else (self._active_space or pin.space_key)
        self._active_space = target_space
        self._selected_by_space[target_space] = pid
        if matched_entry is not None:
            self._pinned_override_id = matched_entry.prev_override_id
            if self._route_queue is not None and matched_entry.prev_route_index is not None:
                self._route_index = max(0, min(len(self._route_queue) - 1, matched_entry.prev_route_index))
        return pin

    def refresh_nearest(
        self,
        player_lat: Optional[float] = None,
        player_lng: Optional[float] = None,
        player_map_id: Optional[int] = None,
        player_floor_id: Optional[str] = None,
        player_is_underground: Optional[bool] = None,
    ) -> Optional[NavPin]:
        """
        Explicitly clears the sticky lock and re-sorts candidates by distance from player,
        locking onto the nearest eligible pin in the active space.
        """
        if player_map_id is not None:
            self._active_space = _make_space_key(
                int(player_map_id),
                player_floor_id,
                bool(player_is_underground),
            )
        if self._active_space is None:
            return None

        if player_lat is not None and player_lng is not None:
            self._last_pos_by_space[self._active_space] = (float(player_lat), float(player_lng))

        self._pinned_override_id = None
        if self._route_queue is not None:
            # In route queue mode, jump to the nearest remaining pin in the queue
            pos = self._last_pos_by_space.get(self._active_space)
            excluded = self._excluded_ids()
            best_idx: Optional[int] = None
            best_dist = float("inf")
            for idx, pid in enumerate(self._route_queue):
                if pid in excluded or pid not in self._pins_by_id:
                    continue
                pin = self._pins_by_id[pid]
                dist = math.hypot(pin.lng - pos[1], pin.lat - pos[0]) if pos is not None else float(idx)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = idx
            if best_idx is not None:
                self._route_index = best_idx
                return self._pins_by_id[self._route_queue[best_idx]]
            return None

        self._orders.pop(self._active_space, None)
        self._selected_by_space.pop(self._active_space, None)
        return self._resolve_target(
            self._active_space,
            player_pos=self._last_pos_by_space.get(self._active_space),
            view_heading_deg=self._last_heading_by_space.get(self._active_space),
        )

    # ------------------------------------------------------------------
    # Per-frame Guidance Calculation
    # ------------------------------------------------------------------
    def _find_nearest_same_floor_waypoint(
        self,
        player_lat: float,
        player_lng: float,
        player_map_id: int,
        player_floor_id: Optional[str],
        player_is_underground: bool,
        waypoints: Optional[Sequence[Any]] = None,
    ) -> Optional[Tuple[float, float, str]]:
        pool = waypoints if waypoints is not None else self._waypoints
        if not pool:
            return None

        best: Optional[Tuple[float, float, str]] = None
        best_dist = float("inf")
        for wp in pool:
            extracted = _extract_waypoint_coords(wp, player_map_id, player_floor_id, player_is_underground)
            if extracted is None:
                continue
            wlat, wlng, wname = extracted
            dist = math.hypot(wlng - player_lng, wlat - player_lat)
            if dist < best_dist:
                best_dist = dist
                best = (wlat, wlng, wname)
        return best

    def compute_guidance(
        self,
        player_lat: float,
        player_lng: float,
        view_heading_deg: Optional[float],
        player_map_id: int,
        player_floor_id: Optional[str] = None,
        player_is_underground: bool = False,
        tracking_state: str = "TRACKING",
        *,
        waypoints: Optional[Sequence[Any]] = None,
    ) -> NavGuidance:
        """
        Computes real-time navigation guidance toward the locked Sticky Target (or TSP route step).
        Does NOT flip targets while player moves around unless explicitly requested.
        """
        norm_state = str(tracking_state or "TRACKING").strip().upper()
        is_tracking = norm_state == "TRACKING"
        self._selection_ready = is_tracking

        norm_p_floor = _norm_floor_id(player_floor_id)
        space_key = _make_space_key(int(player_map_id), norm_p_floor, bool(player_is_underground))

        if not is_tracking:
            target = self.current_target
            rp = self.route_progress
            guidance = NavGuidance(
                state="lost",
                target=target,
                distance_m=None,
                target_distance_m=None,
                bearing_deg=None,
                delta_deg=None,
                floor_mismatch=False,
                player_floor_id=norm_p_floor,
                player_is_underground=bool(player_is_underground),
                target_floor_id=target.floor_id if target else None,
                target_floor_name=target.floor_name if target else "",
                target_is_underground=target.is_underground if target else False,
                route_mode="none",
                show_spot_card=False,
                is_locked_on=False,
                is_behind=False,
                route_step=rp[0] if rp else None,
                route_total=rp[1] if rp else None,
                remaining_count=self.summary.remaining,
            )
            self._last_guidance = guidance
            return guidance

        player_pos = (float(player_lat), float(player_lng))
        if (
            self.space_debounce_frames > 0
            and self._active_space is not None
            and space_key != self._active_space
            and space_key[0] == self._active_space[0]
        ):
            # Same map, different floor tag: wait until the new floor is stable for N frames.
            if self._pending_space == space_key:
                self._pending_space_frames += 1
            else:
                self._pending_space = space_key
                self._pending_space_frames = 1
            if self._pending_space_frames < self.space_debounce_frames:
                space_key = self._active_space
        else:
            self._pending_space = None
            self._pending_space_frames = 0
        if space_key != self._active_space:
            # Entering a new (map_id, floor) space refreshes initial candidate ordering for that space
            self._orders.pop(space_key, None)
        self._active_space = space_key
        prev_pos = self._last_pos_by_space.get(space_key)
        if prev_pos is not None and math.hypot(
            player_pos[0] - prev_pos[0], player_pos[1] - prev_pos[1]
        ) > TELEPORT_JUMP_M:
            self._relock_after_jump(space_key)
        self._last_pos_by_space[space_key] = player_pos

        eff_heading: Optional[float] = None
        if view_heading_deg is not None:
            try:
                cand_h = float(view_heading_deg)
                if math.isfinite(cand_h):
                    eff_heading = cand_h
                    self._last_heading_by_space[space_key] = cand_h
            except (TypeError, ValueError):
                pass
        if eff_heading is None:
            eff_heading = self._last_heading_by_space.get(space_key)

        target = self._resolve_target(space_key, player_pos=player_pos, view_heading_deg=eff_heading)
        if target is not None:
            target = self._proximity_takeover(space_key, target, player_pos, view_heading_deg=eff_heading)
        rp = self.route_progress
        remaining = self.summary.remaining

        if target is None:
            guidance = NavGuidance(
                state="idle",
                target=None,
                distance_m=None,
                target_distance_m=None,
                bearing_deg=None,
                delta_deg=None,
                floor_mismatch=False,
                player_floor_id=norm_p_floor,
                player_is_underground=bool(player_is_underground),
                route_mode="none",
                show_spot_card=False,
                is_locked_on=False,
                is_behind=False,
                route_step=rp[0] if rp else None,
                route_total=rp[1] if rp else None,
                remaining_count=remaining,
            )
            self._last_guidance = guidance
            return guidance

        target_dist, direct_bearing = compute_hoyolab_bearing(
            player_lat, player_lng, target.lat, target.lng
        )
        floor_mismatch = _is_floor_mismatch(target, norm_p_floor, bool(player_is_underground))

        route_mode = "direct"
        route_name = ""
        dest_lat = target.lat
        dest_lng = target.lng
        active_dist = target_dist
        active_bearing = direct_bearing

        if floor_mismatch:
            cave = _extract_cave_entrance(target)
            if cave is not None:
                dest_lat, dest_lng, route_name = cave
                active_dist, active_bearing = compute_hoyolab_bearing(
                    player_lat, player_lng, dest_lat, dest_lng
                )
                route_mode = "cave_entrance"
            else:
                wp_dest = self._find_nearest_same_floor_waypoint(
                    player_lat,
                    player_lng,
                    int(player_map_id),
                    norm_p_floor,
                    bool(player_is_underground),
                    waypoints=waypoints,
                )
                if wp_dest is not None:
                    dest_lat, dest_lng, route_name = wp_dest
                    active_dist, active_bearing = compute_hoyolab_bearing(
                        player_lat, player_lng, dest_lat, dest_lng
                    )
                    route_mode = "waypoint"

        heading = float(view_heading_deg) if view_heading_deg is not None else 0.0
        delta_deg = normalize_angle_delta(active_bearing, heading)

        if floor_mismatch:
            state = "floor_mismatch"
        elif target_dist <= self.arrived_distance_m:
            state = "arrived"
        else:
            state = "tracking"

        show_spot_card = bool(
            target.has_spot_content and min(active_dist, target_dist) <= self.spot_card_distance_m
        )
        is_locked_on = abs(delta_deg) <= LOCK_ON_ANGLE_DEG
        is_behind = abs(delta_deg) > BEHIND_ANGLE_DEG

        guidance = NavGuidance(
            state=state,
            target=target,
            distance_m=active_dist,
            target_distance_m=target_dist,
            bearing_deg=active_bearing,
            delta_deg=delta_deg,
            floor_mismatch=floor_mismatch,
            player_floor_id=norm_p_floor,
            player_is_underground=bool(player_is_underground),
            target_floor_id=target.floor_id,
            target_floor_name=target.floor_name,
            target_is_underground=target.is_underground,
            route_mode=route_mode,
            route_target_name=route_name,
            route_target_lat=dest_lat,
            route_target_lng=dest_lng,
            show_spot_card=show_spot_card,
            is_locked_on=is_locked_on,
            is_behind=is_behind,
            route_step=rp[0] if rp else None,
            route_total=rp[1] if rp else None,
            remaining_count=remaining,
        )
        self._last_guidance = guidance
        return guidance


@dataclass(frozen=True)
class HoldStatus:
    """Status snapshot returned by `CollectedHoldController.update()`."""

    active: bool
    progress: float
    confirmed: bool = False
    cancelled: bool = False


class CollectedHoldController:
    """
    1-second hold-to-confirm guard to prevent accidental pin marking.
    Auto-cancels on key release, target switch, or lost tracking.

    Ported from MaxWay12/Genshin-Navigator@99e1e13:src/genshin_navigator/hotkeys.py (MIT).
    """

    def __init__(self, duration_seconds: float = 1.0) -> None:
        if duration_seconds <= 0:
            raise ValueError("Hold duration must be positive")
        self.duration_seconds = float(duration_seconds)
        self._started_at: Optional[float] = None
        self._target_id: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self._started_at is not None

    @property
    def active_target_id(self) -> Optional[str]:
        return self._target_id

    def begin(
        self,
        target_id: Optional[str],
        *,
        available: bool,
        now: Optional[float] = None,
    ) -> bool:
        if target_id is None or not available:
            return False
        self._target_id = str(target_id)
        self._started_at = monotonic() if now is None else float(now)
        return True

    def cancel(self) -> None:
        self._started_at = None
        self._target_id = None

    def update(
        self,
        *,
        key_down: bool,
        target_id: Optional[str],
        available: bool,
        now: Optional[float] = None,
    ) -> HoldStatus:
        if self._started_at is None:
            return HoldStatus(False, 0.0)
        norm_target = str(target_id) if target_id is not None else None
        if not key_down or not available or norm_target != self._target_id:
            self.cancel()
            return HoldStatus(False, 0.0, cancelled=True)
        current_time = monotonic() if now is None else float(now)
        elapsed = current_time - self._started_at
        progress = min(1.0, max(0.0, elapsed / self.duration_seconds))
        if progress >= 1.0:
            self.cancel()
            return HoldStatus(False, 1.0, confirmed=True)
        return HoldStatus(True, progress)
