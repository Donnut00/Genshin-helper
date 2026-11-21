"""
Genshin-Helper v5 Cross-Map Pin Exporter & Sync Bridge (F8).

Clean-room Python implementation providing:
- Declarative N:1 HoYoLAB label_id normalization via src/data/category_aliases.json
- Full raw HoYoLAB API envelope parsing with O(1) point lookup and missing-ID tracking
- Per-map independent axis-affine calibration (supporting Y-axis flip & sub-maps)
- Optimal 1-to-1 spatial + category + underground-layer matcher with strict max_dist
  cutoff and ambiguity flagging
- Target adapters for Kongying Tavern (Data_KYJG/Time_KYJG), Appsample /
  GenshinMapOverlay (ids + Web-Mercator projection), genshinmap.github.io
  (native UTF-8 safe GM_007~ Base64), and Canonical JSON.
"""
from __future__ import annotations

import base64
import heapq
import json
import math
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from src.paths import get_asset_path

_MICRO_SCALE = 1_000_000
_SURFACE_FLOOR_TOKENS = frozenset({"g", "ground", "surface", "teyvat"})
_EMPTY_FLOOR_TOKENS = frozenset({"", "0", "none", "null"})
_VALID_TIMESTAMP_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
)


# ---------------------------------------------------------------------------
# Timestamp & String Normalization Helpers
# ---------------------------------------------------------------------------


def _normalize_text_key(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[\s_\-/]+", " ", str(text).strip().lower())
    return cleaned.strip()


def _normalize_floor_id(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if s.lower() in _EMPTY_FLOOR_TOKENS:
        return None
    return s


def _normalize_point_group(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    return iv if iv > 0 else None


def _parse_timestamp_to_dt(value: Any) -> Optional[datetime]:
    """Parses ISO strings, Kongying timestamps, or Unix sec/ms into UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num) or num <= 0:
            return None
        # Heuristic: values >= 1e11 are epoch milliseconds.
        if num >= 1e11:
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return _parse_timestamp_to_dt(int(s))
        try:
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            dt = datetime.fromisoformat(iso)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        for fmt in _VALID_TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _normalize_timestamp_iso(value: Any) -> Optional[str]:
    dt = _parse_timestamp_to_dt(value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_kongying_time(value: Any, fallback_dt: datetime) -> str:
    dt = _parse_timestamp_to_dt(value) or fallback_dt
    return dt.strftime("%Y/%m/%d %H:%M:%S")


def _to_unix_seconds(value: Any, fallback_sec: int) -> int:
    dt = _parse_timestamp_to_dt(value)
    if dt is None:
        return int(fallback_sec)
    return int(dt.timestamp())


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _coerce_strict_int(value: Any, field_name: str = "id") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{field_name} must be an integer, got {value!r}")


# ---------------------------------------------------------------------------
# Core Data Models
# ---------------------------------------------------------------------------


@dataclass
class CanonicalPin:
    """Normalized representation of a marked or catalog HoYoLAB map pin."""

    hoyolab_id: int
    map_id: int
    label_id: int
    canonical_category: str
    x_pos: float
    y_pos: float
    floor_id: Optional[str] = None
    point_group: Optional[int] = None
    is_underground: bool = False
    marked_at: Optional[str] = None
    label_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "hoyolab_id": int(self.hoyolab_id),
            "map_id": int(self.map_id),
            "label_id": int(self.label_id),
            "canonical_category": str(self.canonical_category),
            "x_pos": float(self.x_pos),
            "y_pos": float(self.y_pos),
            "floor_id": self.floor_id,
            "point_group": self.point_group,
            "is_underground": bool(self.is_underground),
            "marked_at": self.marked_at,
        }
        if self.label_name is not None:
            payload["label_name"] = self.label_name
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CanonicalPin":
        return cls(
            hoyolab_id=_coerce_strict_int(raw["hoyolab_id"], "hoyolab_id"),
            map_id=_coerce_strict_int(raw.get("map_id", 2), "map_id"),
            label_id=_coerce_strict_int(raw["label_id"], "label_id"),
            canonical_category=str(raw["canonical_category"]),
            x_pos=float(raw["x_pos"]),
            y_pos=float(raw["y_pos"]),
            floor_id=_normalize_floor_id(raw.get("floor_id")),
            point_group=_normalize_point_group(raw.get("point_group")),
            is_underground=bool(raw.get("is_underground", False)),
            marked_at=_normalize_timestamp_iso(raw.get("marked_at")),
            label_name=str(raw["label_name"]) if raw.get("label_name") is not None else None,
        )


@dataclass
class TargetPin:
    """Normalized pin from a destination map catalog (Kongying, Appsample, genshinmap)."""

    target_id: Union[int, str]
    x: float
    y: float
    canonical_category: Optional[str] = None
    category_name: Optional[str] = None
    appsample_code: Optional[str] = None
    feature_key: Optional[str] = None
    map_id: Optional[int] = None
    floor_id: Optional[str] = None
    point_group: Optional[int] = None
    is_underground: Optional[bool] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HoyolabParseResult:
    """Result of parsing HoYoLAB point_list + mark_map_point_list API envelopes."""

    map_id: int
    pins: List[CanonicalPin]
    missing_from_point_list: List[int]
    unmapped_label_ids: List[int]
    total_marked: int
    point_index_size: int


@dataclass(frozen=True)
class AxisAffine:
    """Independent per-axis linear transform: x' = ax*x + bx, y' = cy*y + dy."""

    ax: float
    bx: float
    cy: float
    dy: float
    map_id: Optional[int] = None
    rmse: float = 0.0
    max_residual: float = 0.0
    anchor_count: int = 0
    source: str = "fitted"

    def transform(self, x: float, y: float) -> Tuple[float, float]:
        return (self.ax * float(x) + self.bx, self.cy * float(y) + self.dy)

    def inverse(self) -> "AxisAffine":
        if abs(self.ax) < 1e-12 or abs(self.cy) < 1e-12:
            raise ValueError("Cannot invert degenerate AxisAffine with zero scale")
        inv_ax = 1.0 / self.ax
        inv_cy = 1.0 / self.cy
        return AxisAffine(
            ax=inv_ax,
            bx=-self.bx * inv_ax,
            cy=inv_cy,
            dy=-self.dy * inv_cy,
            map_id=self.map_id,
            rmse=self.rmse,
            max_residual=self.max_residual,
            anchor_count=self.anchor_count,
            source=f"inverse:{self.source}",
        )

    def has_y_flip(self) -> bool:
        return self.cy < 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ax": float(self.ax),
            "bx": float(self.bx),
            "cy": float(self.cy),
            "dy": float(self.dy),
            "map_id": self.map_id,
            "rmse": float(self.rmse),
            "max_residual": float(self.max_residual),
            "anchor_count": int(self.anchor_count),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AxisAffine":
        return cls(
            ax=float(raw["ax"]),
            bx=float(raw["bx"]),
            cy=float(raw["cy"]),
            dy=float(raw["dy"]),
            map_id=int(raw["map_id"]) if raw.get("map_id") is not None else None,
            rmse=float(raw.get("rmse", 0.0)),
            max_residual=float(raw.get("max_residual", 0.0)),
            anchor_count=int(raw.get("anchor_count", 0)),
            source=str(raw.get("source", "loaded")),
        )


# Empirical Kongying Tavern Teyvat (twt40, map_id=2) affine seed:
# kx ≈ (4/3)*hx + 1523.4, ky ≈ (4/3)*hy - 3921.6
# Strictly scoped to map_id=2 (Teyvat). Never apply blindly to sub-maps!
KONGYING_TWT40_SEED = AxisAffine(
    ax=4.0 / 3.0,
    bx=1523.4,
    cy=4.0 / 3.0,
    dy=-3921.6,
    map_id=2,
    rmse=0.0,
    max_residual=0.0,
    anchor_count=0,
    source="seed_twt40",
)


@dataclass
class MatchResult:
    """Single 1-to-1 matched source->target pin pair with distance diagnostics."""

    source: CanonicalPin
    target: TargetPin
    distance: float
    second_best_distance: Optional[float]
    is_ambiguous: bool
    matched_via: str
    projected_x: float
    projected_y: float


@dataclass
class UnmatchedPin:
    """Source pin that could not be safely matched within constraints."""

    source: CanonicalPin
    reason: str
    nearest_distance: Optional[float] = None
    nearest_target_id: Optional[Union[int, str]] = None


@dataclass
class TransferReport:
    """Transparent transfer outcome across all pins in a sync/export batch."""

    matched: List[MatchResult]
    ambiguous: List[MatchResult]
    unmatched: List[UnmatchedPin]
    unmapped_categories: List[str]
    missing_from_point_list: List[int]
    match_rate_pct: float

    @property
    def confident_matches(self) -> List[MatchResult]:
        return [m for m in self.matched if not m.is_ambiguous]

    @property
    def total_source_pins(self) -> int:
        return len(self.matched) + len(self.unmatched)

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "total_source_pins": self.total_source_pins,
            "matched_count": len(self.matched),
            "confident_count": len(self.confident_matches),
            "ambiguous_count": len(self.ambiguous),
            "unmatched_count": len(self.unmatched),
            "unmapped_categories": list(self.unmapped_categories),
            "missing_from_point_list_count": len(self.missing_from_point_list),
            "match_rate_pct": round(self.match_rate_pct, 2),
        }


# ---------------------------------------------------------------------------
# Declarative N:1 Category Alias Registry
# ---------------------------------------------------------------------------


class CategoryRegistry:
    """Loads and queries src/data/category_aliases.json for N:1 normalization."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.schema_version: int = int(data.get("schema_version", 1))
        self.categories: Dict[str, Dict[str, Any]] = {}
        self._label_id_to_canonical: Dict[int, str] = {}
        self._appsample_to_canonical: Dict[str, str] = {}
        self._name_to_canonical: Dict[str, str] = {}
        self._genshinmap_key_to_canonical: Dict[str, str] = {}
        self._genshinmap_suffix_pairs: List[Tuple[str, str]] = []
        self._calibration_anchors: Set[str] = set()
        self._default_affines: Dict[Tuple[str, int], AxisAffine] = {}

        raw_affines = data.get("default_map_affines", {})
        if isinstance(raw_affines, dict):
            for target_name, by_map in raw_affines.items():
                if not isinstance(by_map, dict):
                    continue
                for mid_key, spec in by_map.items():
                    if not isinstance(spec, dict):
                        continue
                    mid = int(mid_key)
                    self._default_affines[(str(target_name).strip().lower(), mid)] = AxisAffine(
                        ax=float(spec["ax"]),
                        bx=float(spec["bx"]),
                        cy=float(spec["cy"]),
                        dy=float(spec["dy"]),
                        map_id=mid,
                        source=str(spec.get("source", "config_seed")),
                    )

        raw_categories = data.get("categories", {})
        if not isinstance(raw_categories, dict):
            raise ValueError("category_aliases.json 'categories' must be an object")

        for canonical, spec in raw_categories.items():
            if not isinstance(spec, dict):
                continue
            canon_key = str(canonical).strip()
            self.categories[canon_key] = spec

            if spec.get("is_calibration_anchor"):
                self._calibration_anchors.add(canon_key)

            self._name_to_canonical[_normalize_text_key(canon_key)] = canon_key

            for lid_raw in spec.get("hoyolab_label_ids", []):
                lid = _coerce_strict_int(lid_raw, "hoyolab_label_id")
                if lid in self._label_id_to_canonical and self._label_id_to_canonical[lid] != canon_key:
                    raise ValueError(
                        f"Duplicate HoYoLAB label_id {lid} mapped to both "
                        f"{self._label_id_to_canonical[lid]!r} and {canon_key!r}"
                    )
                self._label_id_to_canonical[lid] = canon_key

            app_code = spec.get("appsample_code")
            if app_code:
                self._appsample_to_canonical[str(app_code).strip().lower()] = canon_key

            for alias in list(spec.get("name_aliases", [])) + list(spec.get("kongying_names", [])):
                norm_alias = _normalize_text_key(alias)
                if norm_alias:
                    self._name_to_canonical[norm_alias] = canon_key

            for gm_key in spec.get("genshinmap_keys", []):
                gm_norm = str(gm_key).strip().lower()
                if gm_norm:
                    self._genshinmap_key_to_canonical[gm_norm] = canon_key
                    self._genshinmap_suffix_pairs.append((gm_norm, canon_key))

        # Prefer longer genshinmap key suffixes first so ElectroSeelie beats Seelie.
        self._genshinmap_suffix_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    @classmethod
    def from_file(cls, filepath: Union[str, os.PathLike[str]]) -> "CategoryRegistry":
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    def is_known_category(self, canonical_category: Optional[str]) -> bool:
        return bool(canonical_category) and str(canonical_category) in self.categories

    def is_calibration_anchor(self, canonical_category: Optional[str]) -> bool:
        return bool(canonical_category) and str(canonical_category) in self._calibration_anchors

    def get_appsample_code(self, canonical_category: str) -> Optional[str]:
        spec = self.categories.get(canonical_category)
        if not spec:
            return None
        code = spec.get("appsample_code")
        return str(code) if code else None

    def get_default_affine(self, target: str, map_id: int) -> Optional[AxisAffine]:
        """Returns a default affine seed ONLY if defined for that exact (target, map_id)."""
        key = (str(target).strip().lower(), int(map_id))
        if key in self._default_affines:
            return self._default_affines[key]
        if key == ("kongying", 2):
            return KONGYING_TWT40_SEED
        return None

    def resolve_hoyolab_label(
        self,
        label_id: int,
        label_name: Optional[str] = None,
    ) -> Optional[str]:
        """Resolves HoYoLAB label_id (N:1) or falls back to normalized label name."""
        lid = _coerce_strict_int(label_id, "label_id")
        if lid in self._label_id_to_canonical:
            return self._label_id_to_canonical[lid]
        if label_name:
            norm = _normalize_text_key(label_name)
            if norm in self._name_to_canonical:
                return self._name_to_canonical[norm]
        return None

    def resolve_target_category(
        self,
        canonical_category: Optional[str] = None,
        appsample_code: Optional[str] = None,
        category_name: Optional[str] = None,
        feature_key: Optional[str] = None,
        hoyolab_label_id: Optional[int] = None,
    ) -> Optional[str]:
        """Normalizes a target pin's category metadata into a canonical category key."""
        if canonical_category:
            c = str(canonical_category).strip()
            if c in self.categories:
                return c
            c_lower = c.lower()
            if c_lower in self._appsample_to_canonical:
                return self._appsample_to_canonical[c_lower]
            c_norm = _normalize_text_key(c)
            if c_norm in self._name_to_canonical:
                return self._name_to_canonical[c_norm]

        if appsample_code:
            code_norm = str(appsample_code).strip().lower()
            if code_norm in self._appsample_to_canonical:
                return self._appsample_to_canonical[code_norm]

        if hoyolab_label_id is not None:
            resolved = self.resolve_hoyolab_label(hoyolab_label_id)
            if resolved:
                return resolved

        if feature_key:
            fk_raw = str(feature_key).strip()
            fk_base = fk_raw.split("/", 1)[0].strip().lower()
            if fk_base in self._genshinmap_key_to_canonical:
                return self._genshinmap_key_to_canonical[fk_base]
            for suffix, canon in self._genshinmap_suffix_pairs:
                if fk_base.endswith(suffix):
                    return canon

        if category_name:
            norm = _normalize_text_key(category_name)
            if norm in self._name_to_canonical:
                return self._name_to_canonical[norm]

        return None


_DEFAULT_REGISTRY: Optional[CategoryRegistry] = None


def load_category_aliases(path: Optional[Union[str, os.PathLike[str]]] = None) -> CategoryRegistry:
    """Loads CategoryRegistry from src/data/category_aliases.json (cached by default)."""
    global _DEFAULT_REGISTRY
    if path is not None:
        return CategoryRegistry.from_file(path)
    if _DEFAULT_REGISTRY is None:
        candidate = get_asset_path("src", "data", "category_aliases.json")
        if not os.path.exists(candidate):
            candidate = str(Path(__file__).resolve().parent / "data" / "category_aliases.json")
        _DEFAULT_REGISTRY = CategoryRegistry.from_file(candidate)
    return _DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# HoYoLAB API Envelope Parser (O(1) point index + missing ID tracking)
# ---------------------------------------------------------------------------


def _extract_api_data(envelope: Any, name: str) -> Any:
    if isinstance(envelope, dict):
        if "retcode" in envelope and envelope["retcode"] not in (0, "0", None):
            raise ValueError(
                f"HoYoLAB {name} API error: retcode={envelope['retcode']}, "
                f"message={envelope.get('message', '')!r}"
            )
        if "data" in envelope and isinstance(envelope["data"], (dict, list)):
            return envelope["data"]
    return envelope


def _extract_label_names(data_obj: Any) -> Dict[int, str]:
    names: Dict[int, str] = {}
    if not isinstance(data_obj, dict):
        return names

    def _walk_labels(items: Any) -> None:
        if not isinstance(items, list):
            return
        for entry in items:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id") if entry.get("id") is not None else entry.get("label_id")
            raw_name = entry.get("name")
            if raw_id is not None and raw_name:
                try:
                    names[_coerce_strict_int(raw_id, "label_id")] = str(raw_name).strip()
                except ValueError:
                    pass
            if isinstance(entry.get("children"), list):
                _walk_labels(entry["children"])

    for key in ("label_list", "tree", "labels"):
        _walk_labels(data_obj.get(key))
    return names


def _build_canonical_pin_from_hoyolab_point(
    point: Dict[str, Any],
    default_map_id: int,
    label_names: Dict[int, str],
    registry: CategoryRegistry,
    marked_at: Optional[str] = None,
) -> Tuple[CanonicalPin, bool]:
    """Converts one HoYoLAB point dict to (CanonicalPin, is_mapped)."""
    pid = _coerce_strict_int(point["id"], "point.id")
    lid = _coerce_strict_int(point["label_id"], "point.label_id")
    mid = _coerce_strict_int(point.get("map_id", default_map_id), "point.map_id")
    x_pos = float(point["x_pos"])
    y_pos = float(point["y_pos"])

    label_name = point.get("label_name") or label_names.get(lid)
    label_name_str = str(label_name).strip() if label_name else None

    resolved = registry.resolve_hoyolab_label(lid, label_name_str)
    is_mapped = resolved is not None
    canonical_category = resolved if resolved is not None else f"unmapped_{lid}"

    pg_raw = point.get("point_group")
    group_id: Optional[int] = None
    floor_id: Optional[str] = None

    if isinstance(pg_raw, dict):
        group_id = _normalize_point_group(pg_raw.get("group_id") or pg_raw.get("id"))
        floor_id = _normalize_floor_id(pg_raw.get("floor_id"))
    else:
        group_id = _normalize_point_group(pg_raw if pg_raw is not None else point.get("group_id"))
        floor_id = _normalize_floor_id(point.get("floor_id"))

    if floor_id is None and point.get("floor_id") is not None:
        floor_id = _normalize_floor_id(point.get("floor_id"))

    if "is_underground" in point and point["is_underground"] is not None:
        is_underground = bool(point["is_underground"])
    else:
        z_level = int(point.get("z_level") or 0)
        icon_sign = int(point.get("icon_sign") or 0)
        is_underground = bool(
            floor_id is not None
            or group_id is not None
            or z_level != 0
            or icon_sign in (1, 2, 3)
        )

    pin = CanonicalPin(
        hoyolab_id=pid,
        map_id=mid,
        label_id=lid,
        canonical_category=canonical_category,
        x_pos=x_pos,
        y_pos=y_pos,
        floor_id=floor_id,
        point_group=group_id,
        is_underground=is_underground,
        marked_at=_normalize_timestamp_iso(marked_at),
        label_name=label_name_str,
    )
    return pin, is_mapped


def parse_hoyolab_points(
    point_list_envelope: Any,
    map_id: int = 2,
    label_ids: Optional[Iterable[int]] = None,
    registry: Optional[CategoryRegistry] = None,
) -> List[CanonicalPin]:
    """Parses raw HoYoLAB v2/map/point/list envelope into CanonicalPin catalog items."""
    reg = registry or load_category_aliases()
    data_obj = _extract_api_data(point_list_envelope, "point/list")
    label_names = _extract_label_names(data_obj)

    if isinstance(data_obj, dict):
        raw_points = data_obj.get("point_list", [])
    elif isinstance(data_obj, list):
        raw_points = data_obj
    else:
        raise ValueError("point_list_envelope must contain data.point_list[] or a list")

    allowed_labels: Optional[Set[int]] = None
    if label_ids is not None:
        allowed_labels = {_coerce_strict_int(x, "label_id") for x in label_ids}

    pins: List[CanonicalPin] = []
    for item in raw_points:
        if not isinstance(item, dict) or "id" not in item or "label_id" not in item:
            continue
        lid = _coerce_strict_int(item["label_id"], "label_id")
        if allowed_labels is not None and lid not in allowed_labels:
            continue
        pin, _ = _build_canonical_pin_from_hoyolab_point(item, map_id, label_names, reg)
        pins.append(pin)
    return pins


def parse_hoyolab_export(
    point_list_envelope: Any,
    marked_list_envelope: Any,
    map_id: int = 2,
    registry: Optional[CategoryRegistry] = None,
) -> HoyolabParseResult:
    """
    Parses full raw HoYoLAB API envelopes (data.point_list[] + data.list[].point_id).

    Builds an O(1) dictionary keyed by point_id and tracks every marked point_id
    absent from point_list in `missing_from_point_list` (preventing silent pin drops
    when HoYoLAB sidebar filters were inactive).
    """
    reg = registry or load_category_aliases()

    pt_data = _extract_api_data(point_list_envelope, "point/list")
    mk_data = _extract_api_data(marked_list_envelope, "mark_map_point_list")

    label_names = _extract_label_names(pt_data)

    if isinstance(pt_data, dict):
        raw_points = pt_data.get("point_list", [])
    elif isinstance(pt_data, list):
        raw_points = pt_data
    else:
        raise ValueError("point_list_envelope must contain data.point_list[] or a list")

    if isinstance(mk_data, dict):
        raw_marked = mk_data.get("list", [])
    elif isinstance(mk_data, list):
        raw_marked = mk_data
    else:
        raise ValueError("marked_list_envelope must contain data.list[] or a list")

    # O(1) lookup index by point_id
    points_by_id: Dict[int, Dict[str, Any]] = {}
    for pt in raw_points:
        if not isinstance(pt, dict) or "id" not in pt:
            continue
        pid = _coerce_strict_int(pt["id"], "point.id")
        points_by_id[pid] = pt

    # Deduplicate marked entries while preserving deterministic order and earliest valid timestamp
    seen_marked: Dict[int, Optional[str]] = {}
    marked_order: List[int] = []
    for entry in raw_marked:
        if isinstance(entry, dict):
            raw_pid = entry.get("point_id")
            if raw_pid is None:
                raw_pid = entry.get("id")
            if raw_pid is None:
                continue
            pid = _coerce_strict_int(raw_pid, "marked.point_id")
            raw_ts = (
                entry.get("marked_at")
                or entry.get("mark_time")
                or entry.get("updated_at")
                or entry.get("created_at")
            )
            ts_iso = _normalize_timestamp_iso(raw_ts)
        else:
            pid = _coerce_strict_int(entry, "marked.point_id")
            ts_iso = None

        if pid not in seen_marked:
            seen_marked[pid] = ts_iso
            marked_order.append(pid)
        elif seen_marked[pid] is None and ts_iso is not None:
            seen_marked[pid] = ts_iso

    pins: List[CanonicalPin] = []
    missing_from_point_list: List[int] = []
    unmapped_label_set: Set[int] = set()

    for pid in marked_order:
        pt = points_by_id.get(pid)
        if pt is None:
            missing_from_point_list.append(pid)
            continue
        pin, is_mapped = _build_canonical_pin_from_hoyolab_point(
            pt,
            default_map_id=map_id,
            label_names=label_names,
            registry=reg,
            marked_at=seen_marked[pid],
        )
        if not is_mapped:
            unmapped_label_set.add(pin.label_id)
        pins.append(pin)

    return HoyolabParseResult(
        map_id=int(map_id),
        pins=pins,
        missing_from_point_list=missing_from_point_list,
        unmapped_label_ids=sorted(unmapped_label_set),
        total_marked=len(marked_order),
        point_index_size=len(points_by_id),
    )


# ---------------------------------------------------------------------------
# Web-Mercator Helpers (Appsample / Leaflet Tile Projection)
# ---------------------------------------------------------------------------


def web_mercator_to_pixel(lng: float, lat: float, zoom: int = 13) -> Tuple[float, float]:
    """Converts Web-Mercator (lng, lat) degrees to world pixel coordinates at zoom."""
    world = 256.0 * (2.0 ** int(zoom))
    clamped_lat = max(-85.05112878, min(85.05112878, float(lat)))
    rad = math.radians(clamped_lat)
    px = (float(lng) + 180.0) / 360.0 * world
    py = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * world
    return (px, py)


def pixel_to_web_mercator(px: float, py: float, zoom: int = 13) -> Tuple[float, float]:
    """Inverse of web_mercator_to_pixel: world pixels at zoom -> (lng, lat) degrees."""
    world = 256.0 * (2.0 ** int(zoom))
    lng = (float(px) / world) * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * (float(py) / world)
    lat = math.degrees(math.atan(math.sinh(n)))
    return (lng, lat)


# ---------------------------------------------------------------------------
# Per-Map Auto-Affine Calibrator
# ---------------------------------------------------------------------------


def _fit_1d_linear(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float]:
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        raise ValueError("Degenerate calibration anchors: zero variance along coordinate axis")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    return slope, intercept


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def fit_axis_affine(
    src_points: Sequence[Tuple[float, float]],
    dst_points: Sequence[Tuple[float, float]],
    map_id: Optional[int] = None,
    robust: bool = True,
    outlier_sigma: float = 3.5,
    source: str = "fitted",
) -> AxisAffine:
    """
    Fits independent per-axis linear affine transform:
        x' = ax * x + bx
        y' = cy * y + dy

    Supports arbitrary scale/offset per map_id and Y-axis flip (cy < 0).
    When `robust=True` and >= 4 anchor pairs are supplied, trims gross outlier
    anchors (> max(5.0, median + outlier_sigma * 1.4826 * MAD)) and refits.
    """
    if len(src_points) != len(dst_points):
        raise ValueError(
            f"src_points ({len(src_points)}) and dst_points ({len(dst_points)}) must have equal length"
        )
    n = len(src_points)
    if n < 2:
        raise ValueError(f"At least 2 anchor pairs are required to fit AxisAffine, got {n}")

    src_x = [float(p[0]) for p in src_points]
    src_y = [float(p[1]) for p in src_points]
    dst_x = [float(p[0]) for p in dst_points]
    dst_y = [float(p[1]) for p in dst_points]

    ax, bx = _fit_1d_linear(src_x, dst_x)
    cy, dy = _fit_1d_linear(src_y, dst_y)

    residuals = [
        math.hypot(ax * sx + bx - dx, cy * sy + dy - dy_val)
        for sx, sy, dx, dy_val in zip(src_x, src_y, dst_x, dst_y)
    ]

    inlier_indices = list(range(n))
    if robust and n >= 4:
        med = _median(residuals)
        mad = _median([abs(r - med) for r in residuals])
        threshold = max(5.0, med + float(outlier_sigma) * 1.4826 * mad)
        candidates = [i for i, r in enumerate(residuals) if r <= threshold]
        if 3 <= len(candidates) < n:
            try:
                ax_r, bx_r = _fit_1d_linear([src_x[i] for i in candidates], [dst_x[i] for i in candidates])
                cy_r, dy_r = _fit_1d_linear([src_y[i] for i in candidates], [dst_y[i] for i in candidates])
                ax, bx, cy, dy = ax_r, bx_r, cy_r, dy_r
                inlier_indices = candidates
            except ValueError:
                pass

    inlier_residuals = [
        math.hypot(ax * src_x[i] + bx - dst_x[i], cy * src_y[i] + dy - dst_y[i])
        for i in inlier_indices
    ]
    rmse = math.sqrt(sum(r * r for r in inlier_residuals) / len(inlier_residuals))
    max_res = max(inlier_residuals)

    return AxisAffine(
        ax=ax,
        bx=bx,
        cy=cy,
        dy=dy,
        map_id=int(map_id) if map_id is not None else None,
        rmse=rmse,
        max_residual=max_res,
        anchor_count=len(inlier_indices),
        source=source,
    )


class PerMapAffineRegistry:
    """
    Stores per-map_id calibrated AxisAffine transforms.

    NEVER silently reuses Teyvat (map_id=2) affine constants for sub-maps
    (Enkanomiya=7, Chasm=9, Sea of Bygone Eras=34, etc.).
    """

    def __init__(self, category_registry: Optional[CategoryRegistry] = None) -> None:
        self._transforms: Dict[Tuple[str, int], AxisAffine] = {}
        self._category_registry = category_registry or load_category_aliases()

    def set(self, target: str, map_id: int, affine: AxisAffine) -> None:
        mid = int(map_id)
        if affine.map_id is not None and affine.map_id != mid:
            raise ValueError(
                f"Affine map_id={affine.map_id} does not match registration map_id={mid}"
            )
        stored = AxisAffine(
            ax=affine.ax,
            bx=affine.bx,
            cy=affine.cy,
            dy=affine.dy,
            map_id=mid,
            rmse=affine.rmse,
            max_residual=affine.max_residual,
            anchor_count=affine.anchor_count,
            source=affine.source,
        )
        self._transforms[(str(target).strip().lower(), mid)] = stored

    def get(self, target: str, map_id: int, allow_default_seed: bool = True) -> Optional[AxisAffine]:
        key = (str(target).strip().lower(), int(map_id))
        if key in self._transforms:
            return self._transforms[key]
        if allow_default_seed:
            return self._category_registry.get_default_affine(key[0], key[1])
        return None

    def require(self, target: str, map_id: int, allow_default_seed: bool = True) -> AxisAffine:
        affine = self.get(target, map_id, allow_default_seed=allow_default_seed)
        if affine is None:
            raise KeyError(
                f"No AxisAffine calibrated for target={target!r}, map_id={map_id}. "
                "Sub-maps must be calibrated with fit_axis_affine() using shared anchors."
            )
        return affine


def calibrate_from_shared_anchors(
    src_anchors: Sequence[CanonicalPin],
    dst_anchors: Sequence[TargetPin],
    map_id: int,
    initial_seed: Optional[AxisAffine] = None,
    max_seed_dist: float = 150.0,
    registry: Optional[CategoryRegistry] = None,
) -> AxisAffine:
    """
    Refines an initial seed affine (e.g., KONGYING_TWT40_SEED for map_id=2) using
    unpaired shared landmark anchors (Waypoints, Statues, Domains) matched 1-to-1.
    """
    reg = registry or load_category_aliases()
    seed = initial_seed
    if seed is None:
        raise ValueError(
            "initial_seed is required when pairing unpaired anchors; "
            "for sub-maps without a seed, pass explicit paired anchors to fit_axis_affine()"
        )
    if seed.map_id is not None and seed.map_id != int(map_id):
        raise ValueError(
            f"Seed map_id={seed.map_id} cannot be used to calibrate map_id={map_id}"
        )

    anchor_src = [
        p for p in src_anchors
        if p.map_id == int(map_id) and reg.is_calibration_anchor(p.canonical_category)
    ]
    if len(anchor_src) < 2:
        raise ValueError(
            f"Need at least 2 landmark source anchors on map_id={map_id}, got {len(anchor_src)}"
        )

    report = match_pins_one_to_one(
        anchor_src,
        dst_anchors,
        affine=AxisAffine(
            ax=seed.ax,
            bx=seed.bx,
            cy=seed.cy,
            dy=seed.dy,
            map_id=int(map_id),
            source=seed.source,
        ),
        max_dist=max_seed_dist,
        ambiguity_ratio=1.25,
        ambiguity_abs_dist=0.0,
        registry=reg,
    )
    if len(report.matched) < 2:
        raise ValueError(
            f"Anchor refinement found only {len(report.matched)} paired landmarks within "
            f"max_seed_dist={max_seed_dist}"
        )

    src_pts = [(m.source.x_pos, m.source.y_pos) for m in report.matched]
    dst_pts = [(m.target.x, m.target.y) for m in report.matched]
    return fit_axis_affine(
        src_pts,
        dst_pts,
        map_id=int(map_id),
        robust=True,
        source=f"refined:{seed.source}",
    )


# ---------------------------------------------------------------------------
# Optimal 1-to-1 Bipartite Assignment (Connected-Component Min-Cost Max-Flow)
# ---------------------------------------------------------------------------


def _solve_component_ssap(
    nu: int,
    nv: int,
    local_edges: Sequence[Tuple[int, int, float]],
) -> List[Tuple[int, int]]:
    """
    Exact maximum-cardinality minimum-cost bipartite matching via Successive
    Shortest Augmenting Path with Dijkstra + Johnson potentials on integer micro-costs.
    """
    if not local_edges:
        return []
    if nu == 1:
        best_v = min(local_edges, key=lambda e: (e[2], e[1]))[1]
        return [(0, best_v)]
    if nv == 1:
        best_u = min(local_edges, key=lambda e: (e[2], e[0]))[0]
        return [(best_u, 0)]

    src = 0
    snk = nu + nv + 1
    num_nodes = snk + 1
    # Adjacency list: [to, rev_idx, cap, int_cost]
    adj: List[List[List[int]]] = [[] for _ in range(num_nodes)]

    def _add_edge(u: int, v: int, cap: int, cost: int) -> None:
        fwd = [v, len(adj[v]), cap, cost]
        rev = [u, len(adj[u]), 0, -cost]
        adj[u].append(fwd)
        adj[v].append(rev)

    for i in range(nu):
        _add_edge(src, 1 + i, 1, 0)
    for j in range(nv):
        _add_edge(1 + nu + j, snk, 1, 0)

    # Track forward U->V edge references for result extraction
    uv_edge_refs: List[Tuple[int, int, List[int]]] = []
    for u_idx, v_idx, dist_val in local_edges:
        u_node = 1 + u_idx
        v_node = 1 + nu + v_idx
        int_cost = max(0, int(round(dist_val * _MICRO_SCALE)))
        _add_edge(u_node, v_node, 1, int_cost)
        uv_edge_refs.append((u_idx, v_idx, adj[u_node][-1]))

    pot = [0] * num_nodes
    inf = 1 << 62

    while True:
        dist = [inf] * num_nodes
        parent_node = [-1] * num_nodes
        parent_edge = [-1] * num_nodes
        dist[src] = 0
        pq: List[Tuple[int, int]] = [(0, src)]

        while pq:
            d, u = heapq.heappop(pq)
            if d != dist[u]:
                continue
            pot_u = pot[u]
            for e_idx, edge in enumerate(adj[u]):
                if edge[2] <= 0:
                    continue
                v = edge[0]
                nd = d + edge[3] + pot_u - pot[v]
                if nd < dist[v]:
                    dist[v] = nd
                    parent_node[v] = u
                    parent_edge[v] = e_idx
                    heapq.heappush(pq, (nd, v))

        if dist[snk] == inf:
            break

        for node in range(num_nodes):
            if dist[node] < inf:
                pot[node] += dist[node]

        cur = snk
        while cur != src:
            prev = parent_node[cur]
            e_idx = parent_edge[cur]
            edge = adj[prev][e_idx]
            edge[2] -= 1
            adj[cur][edge[1]][2] += 1
            cur = prev

    matched_pairs: List[Tuple[int, int]] = []
    for u_idx, v_idx, fwd_edge in uv_edge_refs:
        if fwd_edge[2] == 0:
            matched_pairs.append((u_idx, v_idx))
    return matched_pairs


def _solve_bipartite_one_to_one(
    edges: Sequence[Tuple[int, int, float]],
    use_optimal: bool = True,
) -> Dict[int, Tuple[int, float]]:
    """
    Computes 1-to-1 source->target assignment from candidate edges `(u, v, dist)`.
    Decomposes sparse spatial graphs into connected components for O(N) typical speed.
    """
    if not edges:
        return {}

    if not use_optimal:
        assigned_u: Dict[int, Tuple[int, float]] = {}
        used_v: Set[int] = set()
        for u, v, d in sorted(edges, key=lambda item: (item[2], item[0], item[1])):
            if u not in assigned_u and v not in used_v:
                assigned_u[u] = (v, d)
                used_v.add(v)
        return assigned_u

    u_adj: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    v_adj: Dict[int, List[int]] = defaultdict(list)
    edge_dist: Dict[Tuple[int, int], float] = {}

    for u, v, d in edges:
        if (u, v) not in edge_dist or d < edge_dist[(u, v)]:
            edge_dist[(u, v)] = d

    for (u, v), d in edge_dist.items():
        u_adj[u].append((v, d))
        v_adj[v].append(u)

    visited_u: Set[int] = set()
    visited_v: Set[int] = set()
    assignment: Dict[int, Tuple[int, float]] = {}

    for start_u in sorted(u_adj.keys()):
        if start_u in visited_u:
            continue
        comp_u: List[int] = []
        comp_v: List[int] = []
        queue: deque[Tuple[bool, int]] = deque([(True, start_u)])
        visited_u.add(start_u)

        while queue:
            is_u, node = queue.popleft()
            if is_u:
                comp_u.append(node)
                for v_nb, _ in u_adj[node]:
                    if v_nb not in visited_v:
                        visited_v.add(v_nb)
                        queue.append((False, v_nb))
            else:
                comp_v.append(node)
                for u_nb in v_adj[node]:
                    if u_nb not in visited_u:
                        visited_u.add(u_nb)
                        queue.append((True, u_nb))

        comp_u.sort()
        comp_v.sort()
        u_to_local = {u: idx for idx, u in enumerate(comp_u)}
        v_to_local = {v: idx for idx, v in enumerate(comp_v)}
        local_edges = [
            (u_to_local[u], v_to_local[v], d)
            for u in comp_u
            for v, d in u_adj[u]
        ]

        for lu, lv in _solve_component_ssap(len(comp_u), len(comp_v), local_edges):
            gu = comp_u[lu]
            gv = comp_v[lv]
            assignment[gu] = (gv, edge_dist[(gu, gv)])

    return assignment


# ---------------------------------------------------------------------------
# Spatial + Category + Underground Layer Matcher
# ---------------------------------------------------------------------------


def _layers_compatible(src: CanonicalPin, dst: TargetPin) -> bool:
    """
    Enforces surface/underground & multi-floor separation whenever the target pin
    provides layer metadata (`is_underground`, `floor_id`, or `point_group`).
    """
    src_floor = _normalize_floor_id(src.floor_id)
    dst_floor = _normalize_floor_id(dst.floor_id)

    dst_ug: Optional[bool] = dst.is_underground
    if dst_ug is None and dst_floor is not None:
        dst_ug = dst_floor.lower() not in _SURFACE_FLOOR_TOKENS

    if dst_ug is not None and bool(dst_ug) != bool(src.is_underground):
        return False

    if src_floor is not None and dst_floor is not None:
        if src_floor.lower() != dst_floor.lower():
            return False

    if src.point_group is not None and dst.point_group is not None:
        if int(src.point_group) != int(dst.point_group):
            return False

    return True


def _resolve_source_bucket(
    pin: CanonicalPin,
    registry: CategoryRegistry,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (canonical_category, normalized_name_fallback, unmapped_tag)."""
    cat = (pin.canonical_category or "").strip()
    if cat and not cat.startswith("unmapped_"):
        if registry.is_known_category(cat):
            return (cat, _normalize_text_key(pin.label_name), None)
        resolved = registry.resolve_target_category(canonical_category=cat, category_name=pin.label_name)
        if resolved:
            return (resolved, _normalize_text_key(pin.label_name), None)
        # Treat explicit non-unmapped custom category as valid bucket key
        return (cat, _normalize_text_key(pin.label_name), None)

    resolved = registry.resolve_hoyolab_label(pin.label_id, pin.label_name)
    if resolved:
        return (resolved, _normalize_text_key(pin.label_name), None)

    fallback_name = _normalize_text_key(pin.label_name) or None
    unmapped_tag = cat if cat else f"unmapped_{pin.label_id}"
    return (None, fallback_name, unmapped_tag)


def _resolve_target_bucket(
    pin: TargetPin,
    registry: CategoryRegistry,
) -> Tuple[Optional[str], Optional[str]]:
    """Returns (canonical_category, normalized_name_fallback)."""
    resolved = registry.resolve_target_category(
        canonical_category=pin.canonical_category,
        appsample_code=pin.appsample_code,
        category_name=pin.category_name,
        feature_key=pin.feature_key,
    )
    if resolved:
        return (resolved, _normalize_text_key(pin.category_name) or None)

    raw_cat = (pin.canonical_category or "").strip()
    if raw_cat and not raw_cat.startswith("unmapped_"):
        return (raw_cat, _normalize_text_key(pin.category_name) or None)

    return (None, _normalize_text_key(pin.category_name) or None)


def match_pins_one_to_one(
    source_pins: Sequence[CanonicalPin],
    target_pins: Sequence[TargetPin],
    affine: AxisAffine,
    max_dist: float = 15.0,
    ambiguity_ratio: float = 1.5,
    *,
    ambiguity_abs_dist: float = 6.0,
    registry: Optional[CategoryRegistry] = None,
    missing_from_point_list: Optional[Sequence[int]] = None,
    allow_name_fallback: bool = True,
    use_optimal_assignment: bool = True,
) -> TransferReport:
    """
    Matches HoYoLAB CanonicalPins 1-to-1 against destination TargetPins.

    Guarantees:
    - Enforces affine.map_id isolation (prevents applying Teyvat constants to sub-maps)
    - Buckets strictly by canonical_category (with optional name fallback for unmapped labels)
    - Enforces underground/floor separation when target provides layer metadata
    - Enforces strict `max_dist` cutoff (never matches pins kilometers away)
    - Performs optimal 1-to-1 assignment (zero target ID collisions on dense clusters)
    - Flags `ambiguous` when 2nd nearest candidate has d2 <= d1 * ambiguity_ratio or d2 < 6.0
    """
    if max_dist < 0.0:
        raise ValueError(f"max_dist must be non-negative, got {max_dist}")
    if ambiguity_ratio < 1.0:
        raise ValueError(f"ambiguity_ratio must be >= 1.0, got {ambiguity_ratio}")

    reg = registry or load_category_aliases()
    missing_ids = [int(x) for x in (missing_from_point_list or ())]

    # Guard against accidentally applying a Teyvat affine to sub-map pins
    if affine.map_id is not None:
        for pin in source_pins:
            if int(pin.map_id) != int(affine.map_id):
                raise ValueError(
                    f"Map ID mismatch: source pin {pin.hoyolab_id} has map_id={pin.map_id}, "
                    f"but affine is calibrated for map_id={affine.map_id}"
                )

    # Index target pins by canonical category and normalized name
    target_by_category: Dict[str, List[int]] = defaultdict(list)
    target_by_name: Dict[str, List[int]] = defaultdict(list)

    for t_idx, t_pin in enumerate(target_pins):
        if affine.map_id is not None and t_pin.map_id is not None and int(t_pin.map_id) != int(affine.map_id):
            continue
        t_canon, t_name = _resolve_target_bucket(t_pin, reg)
        if t_canon:
            target_by_category[t_canon].append(t_idx)
        if t_name:
            target_by_name[t_name].append(t_idx)

    # Partition source pins into matching buckets (canonical vs name fallback)
    bucket_sources: Dict[Tuple[str, str], List[int]] = defaultdict(list)
    unmapped_categories_set: Set[str] = set()
    unmatched: List[UnmatchedPin] = []
    projected_coords: List[Tuple[float, float]] = [
        affine.transform(pin.x_pos, pin.y_pos) for pin in source_pins
    ]

    for s_idx, s_pin in enumerate(source_pins):
        s_canon, s_name, unmapped_tag = _resolve_source_bucket(s_pin, reg)
        if s_canon is not None:
            bucket_sources[("canonical_category", s_canon)].append(s_idx)
        elif allow_name_fallback and s_name and s_name in target_by_name:
            bucket_sources[("name_fallback", s_name)].append(s_idx)
        else:
            if unmapped_tag:
                unmapped_categories_set.add(unmapped_tag)
            unmatched.append(
                UnmatchedPin(source=s_pin, reason="unmapped_category")
            )

    matched: List[MatchResult] = []
    used_target_indices: Set[int] = set()

    # Deterministic processing order: canonical buckets first, then name_fallback buckets
    ordered_buckets = sorted(
        bucket_sources.items(),
        key=lambda kv: (0 if kv[0][0] == "canonical_category" else 1, kv[0][1]),
    )

    for (mode, bucket_key), s_indices in ordered_buckets:
        raw_t_indices = (
            target_by_category.get(bucket_key, [])
            if mode == "canonical_category"
            else target_by_name.get(bucket_key, [])
        )
        t_indices = [ti for ti in raw_t_indices if ti not in used_target_indices]

        if not t_indices:
            for s_idx in s_indices:
                unmatched.append(
                    UnmatchedPin(source=source_pins[s_idx], reason="no_target_in_category")
                )
            continue

        edges: List[Tuple[int, int, float]] = []
        compat_dists_by_source: Dict[int, List[Tuple[float, int]]] = {}
        had_layer_mismatch: Dict[int, bool] = {}

        use_spatial_grid = (len(s_indices) * len(t_indices)) > 25_000
        grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        cell_size = max(max_dist * ambiguity_ratio, ambiguity_abs_dist, 1.0)
        if use_spatial_grid:
            for t_idx in t_indices:
                t_pin = target_pins[t_idx]
                cell = (math.floor(t_pin.x / cell_size), math.floor(t_pin.y / cell_size))
                grid[cell].append(t_idx)

        for s_idx in s_indices:
            s_pin = source_pins[s_idx]
            px, py = projected_coords[s_idx]
            compat_list: List[Tuple[float, int]] = []
            incompatible_count = 0

            if use_spatial_grid:
                cx = math.floor(px / cell_size)
                cy = math.floor(py / cell_size)
                cand_t_indices: List[int] = []
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        bucket_cells = grid.get((cx + dx, cy + dy))
                        if bucket_cells:
                            cand_t_indices.extend(bucket_cells)
            else:
                cand_t_indices = t_indices

            for t_idx in cand_t_indices:
                t_pin = target_pins[t_idx]
                if not _layers_compatible(s_pin, t_pin):
                    incompatible_count += 1
                    continue
                d = math.hypot(px - t_pin.x, py - t_pin.y)
                compat_list.append((d, t_idx))
                if d <= max_dist:
                    edges.append((s_idx, t_idx, d))

            compat_list.sort(key=lambda item: (item[0], str(target_pins[item[1]].target_id)))
            compat_dists_by_source[s_idx] = compat_list
            had_layer_mismatch[s_idx] = (len(compat_list) == 0 and incompatible_count > 0)

        assignment = _solve_bipartite_one_to_one(edges, use_optimal=use_optimal_assignment)

        for s_idx in s_indices:
            s_pin = source_pins[s_idx]
            compat_list = compat_dists_by_source[s_idx]

            if s_idx in assignment:
                t_idx, d_assigned = assignment[s_idx]
                used_target_indices.add(t_idx)
                t_pin = target_pins[t_idx]
                px, py = projected_coords[s_idx]

                # Distance to the nearest OTHER layer-compatible candidate in the same bucket
                second_best: Optional[float] = None
                for cand_d, cand_ti in compat_list:
                    if cand_ti != t_idx:
                        second_best = cand_d
                        break

                is_ambiguous = False
                if second_best is not None:
                    if second_best <= d_assigned * ambiguity_ratio or second_best < ambiguity_abs_dist:
                        is_ambiguous = True

                matched.append(
                    MatchResult(
                        source=s_pin,
                        target=t_pin,
                        distance=d_assigned,
                        second_best_distance=second_best,
                        is_ambiguous=is_ambiguous,
                        matched_via=mode,
                        projected_x=px,
                        projected_y=py,
                    )
                )
            else:
                if had_layer_mismatch[s_idx]:
                    unmatched.append(UnmatchedPin(source=s_pin, reason="layer_mismatch"))
                elif not compat_list:
                    unmatched.append(
                        UnmatchedPin(
                            source=s_pin,
                            reason="exceeds_max_dist" if use_spatial_grid else "no_target_in_category",
                        )
                    )
                else:
                    best_d, best_ti = compat_list[0]
                    reason = "capacity_exhausted" if best_d <= max_dist else "exceeds_max_dist"
                    unmatched.append(
                        UnmatchedPin(
                            source=s_pin,
                            reason=reason,
                            nearest_distance=best_d,
                            nearest_target_id=target_pins[best_ti].target_id,
                        )
                    )

    ambiguous = [m for m in matched if m.is_ambiguous]
    total_considered = len(source_pins) + len(missing_ids)
    match_rate_pct = (100.0 * len(matched) / total_considered) if total_considered > 0 else 0.0

    return TransferReport(
        matched=matched,
        ambiguous=ambiguous,
        unmatched=unmatched,
        unmapped_categories=sorted(unmapped_categories_set),
        missing_from_point_list=missing_ids,
        match_rate_pct=match_rate_pct,
    )


# ---------------------------------------------------------------------------
# Target Adapters (Kongying, Appsample/GenshinMapOverlay, genshinmap, Canonical)
# ---------------------------------------------------------------------------


def _select_matches(
    matches_or_report: Union[TransferReport, Sequence[MatchResult]],
    include_ambiguous: bool,
) -> List[MatchResult]:
    items = (
        matches_or_report.matched
        if isinstance(matches_or_report, TransferReport)
        else list(matches_or_report)
    )
    if include_ambiguous:
        return list(items)
    return [m for m in items if not m.is_ambiguous]


class KongyingAdapter:
    """
    Kongying Tavern (空荧酒馆) archive adapter.

    Strictly serializes `Data_KYJG` as a sorted flat JSON array `list[int]`
    (never a Set / empty object `{}`) alongside `Time_KYJG` (`YYYY/MM/DD HH:mm:ss`)
    and `updated_at` (epoch milliseconds).
    """

    TIME_REGEX = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$")

    @staticmethod
    def parse_markers(
        markers: Sequence[Dict[str, Any]],
        map_id: int = 2,
        registry: Optional[CategoryRegistry] = None,
    ) -> List[TargetPin]:
        """Converts Kongying MarkerVo dicts into TargetPin objects."""
        reg = registry or load_category_aliases()
        out: List[TargetPin] = []
        for m in markers:
            if not isinstance(m, dict) or "id" not in m:
                continue
            mid = _coerce_strict_int(m["id"], "marker.id")

            # Position can be "x,y", [x, y], or explicit x/y fields
            pos = m.get("position")
            if isinstance(pos, str) and "," in pos:
                px_s, py_s = pos.split(",", 1)
                x_val, y_val = float(px_s.strip()), float(py_s.strip())
            elif isinstance(pos, (list, tuple)) and len(pos) >= 2:
                x_val, y_val = float(pos[0]), float(pos[1])
            else:
                x_val = float(m["x"])
                y_val = float(m["y"])

            cat_name = m.get("category_name") or m.get("item_name") or m.get("markerTitle")
            if not cat_name and isinstance(m.get("itemList"), list) and m["itemList"]:
                first_item = m["itemList"][0]
                if isinstance(first_item, dict):
                    cat_name = first_item.get("name") or first_item.get("itemName")

            canon = reg.resolve_target_category(
                canonical_category=m.get("canonical_category"),
                category_name=str(cat_name) if cat_name else None,
            )

            is_ug: Optional[bool] = None
            if "is_underground" in m and m["is_underground"] is not None:
                is_ug = bool(m["is_underground"])
            elif "underground" in m and m["underground"] is not None:
                is_ug = bool(m["underground"])
            elif "hiddenFlag" in m and m["hiddenFlag"] is not None:
                # Kongying hiddenFlag: 0=normal surface, 2=underground
                hf = int(m["hiddenFlag"])
                if hf == 2:
                    is_ug = True
                elif hf == 0:
                    is_ug = False

            floor_id = _normalize_floor_id(m.get("floor_id"))
            if floor_id is None and isinstance(m.get("extra"), dict):
                ug_meta = m["extra"].get("underground")
                if isinstance(ug_meta, dict):
                    if ug_meta.get("is_underground") is not None:
                        is_ug = bool(ug_meta["is_underground"])
                    raw_floor = ug_meta.get("floor_id")
                    if raw_floor is None:
                        rl = ug_meta.get("region_levels")
                        if isinstance(rl, list) and rl:
                            raw_floor = rl[0]
                        elif rl is not None:
                            raw_floor = rl
                    floor_id = _normalize_floor_id(raw_floor)

            out.append(
                TargetPin(
                    target_id=mid,
                    x=x_val,
                    y=y_val,
                    canonical_category=canon,
                    category_name=str(cat_name) if cat_name else None,
                    map_id=int(m.get("map_id", map_id)),
                    floor_id=floor_id,
                    is_underground=is_ug,
                    extra={"markerTitle": m.get("markerTitle", "")},
                )
            )
        return out

    @classmethod
    def export(
        cls,
        matches_or_report: Union[TransferReport, Sequence[MatchResult]],
        *,
        include_ambiguous: bool = True,
        updated_at_ms: Optional[int] = None,
        default_timestamp: Optional[Any] = None,
    ) -> Dict[str, Any]:
        selected = _select_matches(matches_or_report, include_ambiguous=include_ambiguous)
        now_ms = int(updated_at_ms) if updated_at_ms is not None else int(time.time() * 1000)
        fallback_dt = _parse_timestamp_to_dt(default_timestamp) or datetime.fromtimestamp(
            now_ms / 1000.0, tz=timezone.utc
        )

        ids_set: Set[int] = set()
        time_map: Dict[str, str] = {}
        for m in selected:
            tid = _coerce_strict_int(m.target.target_id, "kongying.target_id")
            ids_set.add(tid)
            time_map[str(tid)] = _format_kongying_time(m.source.marked_at, fallback_dt)

        sorted_ids = sorted(ids_set)
        ordered_time_map = {str(tid): time_map[str(tid)] for tid in sorted_ids}
        return {
            "Data_KYJG": sorted_ids,
            "Time_KYJG": ordered_time_map,
            "updated_at": now_ms,
        }

    @classmethod
    def to_json(cls, payload: Dict[str, Any], indent: int = 2) -> str:
        errors = cls.validate_schema(payload)
        if errors:
            raise ValueError(f"Invalid Kongying archive payload: {'; '.join(errors)}")
        return json.dumps(payload, ensure_ascii=False, indent=indent)

    @classmethod
    def parse(cls, raw: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            raise ValueError("Kongying archive must be a JSON object")
        errors = cls.validate_schema(data)
        if errors:
            raise ValueError(f"Invalid Kongying archive: {'; '.join(errors)}")
        return {
            "Data_KYJG": sorted({_coerce_strict_int(x, "Data_KYJG[]") for x in data["Data_KYJG"]}),
            "Time_KYJG": {str(k): str(v) for k, v in data["Time_KYJG"].items()},
            "updated_at": int(data["updated_at"]),
        }

    @classmethod
    def merge_existing(
        cls,
        existing: Union[str, Dict[str, Any]],
        exported: Dict[str, Any],
        updated_at_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        base = cls.parse(existing)
        inc = cls.parse(exported)
        merged_ids = sorted(set(base["Data_KYJG"]) | set(inc["Data_KYJG"]))
        merged_times = dict(base["Time_KYJG"])
        for k, v in inc["Time_KYJG"].items():
            if not merged_times.get(k):
                merged_times[k] = v
        out_ms = (
            int(updated_at_ms)
            if updated_at_ms is not None
            else max(base["updated_at"], inc["updated_at"])
        )
        return {
            "Data_KYJG": merged_ids,
            "Time_KYJG": {str(k): merged_times[str(k)] for k in merged_ids if str(k) in merged_times},
            "updated_at": out_ms,
        }

    @classmethod
    def validate_schema(cls, payload: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(payload, dict):
            return ["Payload must be a JSON object"]
        data_ids = payload.get("Data_KYJG")
        if not isinstance(data_ids, list):
            errors.append("Data_KYJG must be a flat list[int]")
        else:
            if any(not _is_strict_int(x) for x in data_ids):
                errors.append("Data_KYJG elements must all be strict integers")
            if data_ids != sorted(set(data_ids)):
                errors.append("Data_KYJG must be sorted and deduplicated")

        time_map = payload.get("Time_KYJG")
        if not isinstance(time_map, dict):
            errors.append("Time_KYJG must be a dict[str, str]")
        else:
            for k, v in time_map.items():
                if not isinstance(k, str) or not k.lstrip("-").isdigit():
                    errors.append(f"Time_KYJG key {k!r} must be a numeric string ID")
                if not isinstance(v, str) or (v != "" and not cls.TIME_REGEX.match(v)):
                    errors.append(f"Time_KYJG[{k!r}] must match YYYY/MM/DD HH:mm:ss or '', got {v!r}")

        updated_at = payload.get("updated_at")
        if not _is_strict_int(updated_at) or updated_at < 0:
            errors.append("updated_at must be a non-negative integer epoch_ms")
        return errors


class AppsampleAdapter:
    """
    Appsample / GenshinMapOverlay progress & marker adapter.

    Exports `{ "ids": [int, ...] }` and supports Web-Mercator pixel projection
    (zoom 13) + columnar delta-encoded marker catalog parsing.
    """

    @staticmethod
    def parse_markers(
        markers_or_columnar: Union[Sequence[Dict[str, Any]], Dict[str, Any]],
        map_id: int = 2,
        zoom: int = 13,
        delta_encoded: bool = True,
        registry: Optional[CategoryRegistry] = None,
    ) -> List[TargetPin]:
        """
        Parses Appsample/GenshinMapOverlay markers (row list OR columnar delta-encoded
        object with `id`, `lng`, `lat`, `type`/`o<N>`, optional `mid`) into TargetPins
        projected in Web-Mercator pixel space at `zoom`.
        """
        reg = registry or load_category_aliases()
        out: List[TargetPin] = []

        if isinstance(markers_or_columnar, dict):
            raw_ids = markers_or_columnar.get("id") or markers_or_columnar.get("ids") or []
            lngs = markers_or_columnar.get("lng") or []
            lats = markers_or_columnar.get("lat") or []
            types = markers_or_columnar.get("type") or markers_or_columnar.get("cat") or []
            mids = markers_or_columnar.get("mid")

            count = min(len(raw_ids), len(lngs), len(lats))
            acc_id = 0
            for idx in range(count):
                raw_val = _coerce_strict_int(raw_ids[idx], "appsample.id")
                acc_id = (acc_id + raw_val) if delta_encoded else raw_val
                if mids is not None and idx < len(mids) and int(mids[idx]) != int(map_id):
                    continue
                code = str(types[idx]) if idx < len(types) and types[idx] is not None else None
                px, py = web_mercator_to_pixel(float(lngs[idx]), float(lats[idx]), zoom=zoom)
                canon = reg.resolve_target_category(appsample_code=code)
                out.append(
                    TargetPin(
                        target_id=acc_id,
                        x=px,
                        y=py,
                        canonical_category=canon,
                        appsample_code=code,
                        map_id=int(map_id),
                        extra={"lng": float(lngs[idx]), "lat": float(lats[idx])},
                    )
                )
            return out

        for row in markers_or_columnar:
            if not isinstance(row, dict) or "id" not in row:
                continue
            if row.get("mid") is not None and int(row["mid"]) != int(map_id):
                continue
            mid_val = _coerce_strict_int(row["id"], "appsample.id")
            if "x" in row and "y" in row and "lng" not in row:
                px, py = float(row["x"]), float(row["y"])
            else:
                px, py = web_mercator_to_pixel(float(row["lng"]), float(row["lat"]), zoom=zoom)
            code = row.get("appsample_code") or row.get("type") or row.get("o")
            code_str = str(code) if code is not None else None
            canon = reg.resolve_target_category(
                canonical_category=row.get("canonical_category"),
                appsample_code=code_str,
                category_name=row.get("category_name"),
            )
            out.append(
                TargetPin(
                    target_id=mid_val,
                    x=px,
                    y=py,
                    canonical_category=canon,
                    category_name=row.get("category_name"),
                    appsample_code=code_str,
                    map_id=int(row.get("mid", map_id)),
                    floor_id=_normalize_floor_id(row.get("floor_id")),
                    is_underground=row.get("is_underground"),
                )
            )
        return out

    @classmethod
    def export(
        cls,
        matches_or_report: Union[TransferReport, Sequence[MatchResult]],
        *,
        include_ambiguous: bool = True,
    ) -> Dict[str, List[int]]:
        selected = _select_matches(matches_or_report, include_ambiguous=include_ambiguous)
        ids = sorted({_coerce_strict_int(m.target.target_id, "appsample.target_id") for m in selected})
        return {"ids": ids}

    @classmethod
    def to_json(cls, payload: Dict[str, Any], indent: int = 2) -> str:
        errors = cls.validate_schema(payload)
        if errors:
            raise ValueError(f"Invalid Appsample payload: {'; '.join(errors)}")
        return json.dumps(payload, ensure_ascii=False, indent=indent)

    @classmethod
    def parse(cls, raw: Union[str, Dict[str, Any]]) -> Dict[str, List[int]]:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            raise ValueError("Appsample payload must be a JSON object")
        errors = cls.validate_schema(data)
        if errors:
            raise ValueError(f"Invalid Appsample payload: {'; '.join(errors)}")
        return {"ids": sorted({_coerce_strict_int(x, "ids[]") for x in data["ids"]})}

    @classmethod
    def merge_existing(
        cls,
        existing: Union[str, Dict[str, Any]],
        exported: Dict[str, Any],
    ) -> Dict[str, List[int]]:
        base = cls.parse(existing)
        inc = cls.parse(exported)
        return {"ids": sorted(set(base["ids"]) | set(inc["ids"]))}

    @classmethod
    def validate_schema(cls, payload: Any) -> List[str]:
        if not isinstance(payload, dict):
            return ["Payload must be a JSON object"]
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return ["'ids' must be a list[int]"]
        errors: List[str] = []
        if any(not _is_strict_int(x) for x in ids):
            errors.append("All elements of 'ids' must be strict integers")
        if ids != sorted(set(ids)):
            errors.append("'ids' must be sorted and deduplicated")
        return errors


class GenshinMapGithubAdapter:
    """
    genshinmap.github.io native `GM_007~` Base64 progress adapter.

    Wire format: UTF-8 safe Base64("GM_007~" + JSON) where JSON contains the 5
    top-level keys (`version`, `options`, `displayed`, `completed`, `editor`)
    and `completed.features` is a flat dict `{ "<featureKey>/<sha1_id>": unix_sec }`.
    """

    PREFIX = "GM_007~"
    VERSION = "GM_007"
    REQUIRED_KEYS = ("version", "options", "displayed", "completed", "editor")

    @staticmethod
    def parse_markers(
        features: Sequence[Dict[str, Any]],
        map_id: int = 2,
        registry: Optional[CategoryRegistry] = None,
    ) -> List[TargetPin]:
        """Converts genshinmap.github.io feature entries into TargetPin objects."""
        reg = registry or load_category_aliases()
        out: List[TargetPin] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            feature_key = str(feat.get("featureKey") or feat.get("feature_key") or "").strip()
            raw_id = feat.get("id") or feat.get("sha1_id") or feat.get("target_id")
            if raw_id is None:
                continue
            raw_id_str = str(raw_id).strip()
            full_id = raw_id_str if "/" in raw_id_str else (
                f"{feature_key}/{raw_id_str}" if feature_key else raw_id_str
            )
            if not feature_key and "/" in full_id:
                feature_key = full_id.split("/", 1)[0]

            if "x" in feat and "y" in feat:
                x_val, y_val = float(feat["x"]), float(feat["y"])
            elif isinstance(feat.get("coordinates"), (list, tuple)) and len(feat["coordinates"]) >= 2:
                # GeoJSON/L.CRS.Simple [lat, lng] or [x, y]
                x_val, y_val = float(feat["coordinates"][0]), float(feat["coordinates"][1])
            elif "lng" in feat and "lat" in feat:
                x_val, y_val = float(feat["lng"]), float(feat["lat"])
            else:
                continue

            canon = reg.resolve_target_category(
                canonical_category=feat.get("canonical_category"),
                feature_key=feature_key,
                category_name=feat.get("category_name"),
            )
            out.append(
                TargetPin(
                    target_id=full_id,
                    x=x_val,
                    y=y_val,
                    canonical_category=canon,
                    category_name=feat.get("category_name"),
                    feature_key=feature_key or None,
                    map_id=int(feat.get("map_id", map_id)),
                    floor_id=_normalize_floor_id(feat.get("floor_id")),
                    is_underground=feat.get("is_underground"),
                )
            )
        return out

    @classmethod
    def _default_preferences(cls) -> Dict[str, Any]:
        return {
            "version": cls.VERSION,
            "options": {},
            "displayed": {"features": {}, "routes": {}},
            "completed": {"features": {}},
            "editor": {"feature": {"data": []}, "route": {"data": []}},
        }

    @classmethod
    def _resolve_feature_compound_key(cls, target: TargetPin) -> str:
        tid = str(target.target_id).strip()
        if "/" in tid:
            left, right = tid.split("/", 1)
            if left.strip() and right.strip():
                return f"{left.strip()}/{right.strip()}"
        if target.feature_key and tid:
            return f"{str(target.feature_key).strip()}/{tid}"
        raise ValueError(
            f"genshinmap target_id must be '<featureKey>/<sha1_id>' or include feature_key, got {tid!r}"
        )

    @classmethod
    def build_payload(
        cls,
        matches_or_report: Union[TransferReport, Sequence[MatchResult]],
        *,
        include_ambiguous: bool = True,
        default_unix_sec: Optional[int] = None,
        base_preferences: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        selected = _select_matches(matches_or_report, include_ambiguous=include_ambiguous)
        fallback_sec = int(default_unix_sec) if default_unix_sec is not None else int(time.time())

        if base_preferences is not None:
            payload = json.loads(json.dumps(base_preferences, ensure_ascii=False))
            payload["version"] = cls.VERSION
            payload.setdefault("options", {})
            payload.setdefault("displayed", {"features": {}, "routes": {}})
            payload.setdefault("completed", {"features": {}})
            payload["completed"].setdefault("features", {})
            payload.setdefault("editor", {"feature": {"data": []}, "route": {"data": []}})
        else:
            payload = cls._default_preferences()

        features_map: Dict[str, int] = dict(payload["completed"]["features"])
        for m in selected:
            compound_key = cls._resolve_feature_compound_key(m.target)
            if compound_key not in features_map:
                features_map[compound_key] = _to_unix_seconds(m.source.marked_at, fallback_sec)

        payload["completed"]["features"] = dict(sorted(features_map.items()))
        return payload

    @classmethod
    def encode(cls, payload: Dict[str, Any]) -> str:
        errors = cls.validate_schema(payload)
        if errors:
            raise ValueError(f"Invalid GM_007 payload: {'; '.join(errors)}")
        raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        framed = f"{cls.PREFIX}{raw_json}"
        return base64.b64encode(framed.encode("utf-8")).decode("ascii")

    @classmethod
    def export(
        cls,
        matches_or_report: Union[TransferReport, Sequence[MatchResult]],
        *,
        include_ambiguous: bool = True,
        default_unix_sec: Optional[int] = None,
        base_preferences: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = cls.build_payload(
            matches_or_report,
            include_ambiguous=include_ambiguous,
            default_unix_sec=default_unix_sec,
            base_preferences=base_preferences,
        )
        return cls.encode(payload)

    @classmethod
    def decode(cls, encoded: str) -> Dict[str, Any]:
        if not isinstance(encoded, str) or not encoded.strip():
            raise ValueError("Encoded GM_007 string must be a non-empty string")
        try:
            decoded_bytes = base64.b64decode(encoded.strip().encode("ascii"), validate=True)
            framed = decoded_bytes.decode("utf-8")
        except Exception as exc:
            raise ValueError(f"Invalid UTF-8 Base64 GM_007 payload: {exc}") from exc

        if not framed.startswith(cls.PREFIX):
            raise ValueError(f"Missing required {cls.PREFIX!r} prefix")
        raw_json = framed[len(cls.PREFIX):]
        payload = json.loads(raw_json)
        errors = cls.validate_schema(payload)
        if errors:
            raise ValueError(f"Invalid decoded GM_007 schema: {'; '.join(errors)}")
        return payload

    @classmethod
    def merge_into_encoded(
        cls,
        existing_encoded: str,
        matches_or_report: Union[TransferReport, Sequence[MatchResult]],
        *,
        include_ambiguous: bool = True,
        default_unix_sec: Optional[int] = None,
    ) -> str:
        existing_payload = cls.decode(existing_encoded)
        return cls.export(
            matches_or_report,
            include_ambiguous=include_ambiguous,
            default_unix_sec=default_unix_sec,
            base_preferences=existing_payload,
        )

    @classmethod
    def validate_schema(cls, payload: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(payload, dict):
            return ["Payload must be a JSON object"]
        for req_key in cls.REQUIRED_KEYS:
            if req_key not in payload:
                errors.append(f"Missing required key {req_key!r}")
        if payload.get("version") != cls.VERSION:
            errors.append(f"version must be {cls.VERSION!r}, got {payload.get('version')!r}")
        completed = payload.get("completed")
        if not isinstance(completed, dict):
            errors.append("'completed' must be an object")
        else:
            feats = completed.get("features")
            if not isinstance(feats, dict):
                errors.append("'completed.features' must be a flat dict, not a list")
            else:
                for k, v in feats.items():
                    if not isinstance(k, str) or "/" not in k:
                        errors.append(f"completed.features key {k!r} must be '<featureKey>/<sha1_id>'")
                    else:
                        left, right = k.split("/", 1)
                        if not left.strip() or not right.strip():
                            errors.append(f"completed.features key {k!r} has empty featureKey or id")
                    if not _is_strict_int(v) or v < 0:
                        errors.append(f"completed.features[{k!r}] must be non-negative int unix_sec, got {v!r}")
        return errors


class CanonicalJsonAdapter:
    """Open, lossless canonical JSON adapter for Genshin-Helper plugins & backups."""

    SCHEMA = "genshin-helper.canonical-pins.v1"

    @classmethod
    def export(
        cls,
        source: Union[HoyolabParseResult, Sequence[CanonicalPin]],
        *,
        map_id: Optional[int] = None,
        exported_at: Optional[Any] = None,
    ) -> Dict[str, Any]:
        if isinstance(source, HoyolabParseResult):
            pins = list(source.pins)
            eff_map_id = source.map_id if map_id is None else int(map_id)
            missing_ids = list(source.missing_from_point_list)
            unmapped_labels = list(source.unmapped_label_ids)
        else:
            pins = list(source)
            eff_map_id = int(map_id) if map_id is not None else (pins[0].map_id if pins else 2)
            missing_ids = []
            unmapped_labels = sorted(
                {p.label_id for p in pins if p.canonical_category.startswith("unmapped_")}
            )

        ts_iso = _normalize_timestamp_iso(exported_at) or datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        sorted_pins = sorted(pins, key=lambda p: (p.map_id, p.hoyolab_id))

        return {
            "schema": cls.SCHEMA,
            "exported_at": ts_iso,
            "map_id": eff_map_id,
            "stats": {
                "total_pins": len(sorted_pins),
                "missing_from_point_list_count": len(missing_ids),
                "unmapped_label_ids": unmapped_labels,
            },
            "missing_from_point_list": missing_ids,
            "pins": [p.to_dict() for p in sorted_pins],
        }

    @classmethod
    def to_json(cls, payload: Dict[str, Any], indent: int = 2) -> str:
        errors = cls.validate_schema(payload)
        if errors:
            raise ValueError(f"Invalid Canonical JSON payload: {'; '.join(errors)}")
        return json.dumps(payload, ensure_ascii=False, indent=indent)

    @classmethod
    def parse(cls, raw: Union[str, Dict[str, Any]]) -> Tuple[List[CanonicalPin], Dict[str, Any]]:
        data = json.loads(raw) if isinstance(raw, str) else raw
        errors = cls.validate_schema(data)
        if errors:
            raise ValueError(f"Invalid Canonical JSON: {'; '.join(errors)}")
        pins = [CanonicalPin.from_dict(item) for item in data["pins"]]
        meta = {
            "schema": data["schema"],
            "exported_at": data["exported_at"],
            "map_id": int(data["map_id"]),
            "missing_from_point_list": [int(x) for x in data.get("missing_from_point_list", [])],
            "stats": dict(data.get("stats", {})),
        }
        return pins, meta

    @classmethod
    def validate_schema(cls, payload: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(payload, dict):
            return ["Payload must be a JSON object"]
        if payload.get("schema") != cls.SCHEMA:
            errors.append(f"schema must be {cls.SCHEMA!r}, got {payload.get('schema')!r}")
        if not isinstance(payload.get("pins"), list):
            errors.append("'pins' must be a list")
        if not _is_strict_int(payload.get("map_id")):
            errors.append("'map_id' must be an integer")
        return errors


__all__ = [
    "AxisAffine",
    "CanonicalJsonAdapter",
    "CanonicalPin",
    "CategoryRegistry",
    "GenshinMapGithubAdapter",
    "HoyolabParseResult",
    "KONGYING_TWT40_SEED",
    "KongyingAdapter",
    "MatchResult",
    "PerMapAffineRegistry",
    "TargetPin",
    "TransferReport",
    "UnmatchedPin",
    "AppsampleAdapter",
    "calibrate_from_shared_anchors",
    "fit_axis_affine",
    "load_category_aliases",
    "match_pins_one_to_one",
    "parse_hoyolab_export",
    "parse_hoyolab_points",
    "pixel_to_web_mercator",
    "web_mercator_to_pixel",
]
