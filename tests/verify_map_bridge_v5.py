"""
Verification suite for Genshin-Helper v5 Cross-Map Pin Bridge (Track 2C / F8):
1. Declarative N:1 category aliasing (Shrine of Depths, Warming Seelie, chests, oculi, landmarks)
2. Full HoYoLAB API envelope parsing + O(1) lookup + missing_from_point_list reporting
3. Per-map auto-affine calibration (Teyvat twt40 seed + anchor refinement, sub-map with Y-axis flip,
   robust outlier trimming, strict sub-map isolation)
4. Strict max_dist rejection & category bucketing (never matches pins km away or across categories)
5. Optimal 1-to-1 collision prevention on double-chest clusters, augmenting-path swap, ambiguity
   flagging (d2 <= d1 * ratio OR d2 < 6.0), brute-force optimality check & spatial grid equivalence
6. Underground & multi-floor layer separation (Surface vs B1 vs B2 at identical x/y)
7. Round-trip schema validation for Kongying (Data_KYJG/Time_KYJG), Appsample (ids + Web-Mercator
   delta-encoded markers), genshinmap.github.io (UTF-8 safe GM_007~ Base64), and Canonical JSON
"""
from __future__ import annotations

import base64
import itertools
import json
import math
import os
import random
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.map_bridge import (  # noqa: E402
    AppsampleAdapter,
    AxisAffine,
    CanonicalJsonAdapter,
    CanonicalPin,
    CategoryRegistry,
    GenshinMapGithubAdapter,
    KONGYING_TWT40_SEED,
    KongyingAdapter,
    PerMapAffineRegistry,
    TargetPin,
    _solve_bipartite_one_to_one,
    calibrate_from_shared_anchors,
    fit_axis_affine,
    load_category_aliases,
    match_pins_one_to_one,
    parse_hoyolab_export,
    pixel_to_web_mercator,
    web_mercator_to_pixel,
)


def verify_category_aliases_n_to_1() -> None:
    reg = load_category_aliases()

    # Shrine of Depths N:1 across all regions -> canonical shrine_of_depths / Appsample o8
    for lid in (8, 9, 212, 411, 509, 577):
        assert reg.resolve_hoyolab_label(lid) == "shrine_of_depths", f"label {lid} failed"
    assert reg.get_appsample_code("shrine_of_depths") == "o8"
    assert reg.resolve_target_category(appsample_code="o8") == "shrine_of_depths"
    assert reg.resolve_target_category(category_name="地灵龛") == "shrine_of_depths"
    assert reg.resolve_target_category(feature_key="FontaineShrineOfDepths/deadbeef") == "shrine_of_depths"

    # Warming Seelie (148) + Seelie (18) -> seelie / o18; Electro Seelie (217) stays distinct
    assert reg.resolve_hoyolab_label(18) == "seelie"
    assert reg.resolve_hoyolab_label(148) == "seelie"
    assert reg.get_appsample_code("seelie") == "o18"
    assert reg.resolve_hoyolab_label(217) == "electro_seelie"
    assert reg.resolve_target_category(feature_key="InazumaElectroSeelie/abc123") == "electro_seelie"
    assert reg.resolve_target_category(feature_key="DragonspineSeelie/abc123") == "seelie"

    # Chest tiers & Oculi & Calibration anchors
    assert reg.resolve_hoyolab_label(44) == "common_chest"
    assert reg.resolve_hoyolab_label(45) == "exquisite_chest"
    assert reg.resolve_hoyolab_label(46) == "precious_chest"
    assert reg.resolve_hoyolab_label(64) == "luxurious_chest"
    assert reg.resolve_hoyolab_label(236) == "remarkable_chest"
    for lid, expected in (
        (5, "anemoculus"),
        (6, "geoculus"),
        (147, "crimson_agate"),
        (210, "electroculus"),
        (345, "lumenspar"),
        (395, "dendroculus"),
        (508, "hydroculus"),
        (559, "spirit_carp"),
        (575, "pyroculus"),
    ):
        assert reg.resolve_hoyolab_label(lid) == expected

    for anchor_lid in (2, 3, 154):
        cat = reg.resolve_hoyolab_label(anchor_lid)
        assert cat is not None and reg.is_calibration_anchor(cat)

    # Duplicate label_id in malformed config must raise ValueError
    try:
        CategoryRegistry(
            {
                "categories": {
                    "cat_a": {"hoyolab_label_ids": [99]},
                    "cat_b": {"hoyolab_label_ids": [99]},
                }
            }
        )
        raise AssertionError("Expected ValueError for duplicate hoyolab_label_ids")
    except ValueError:
        pass

    print("  [OK] N:1 category aliasing & registry integrity passed")


def verify_hoyolab_envelope_parser() -> None:
    point_list_envelope = {
        "retcode": 0,
        "message": "OK",
        "data": {
            "label_list": [
                {"id": 44, "name": "Common Chest"},
                {"id": 577, "name": "Natlan Shrine of Depths"},
                {"id": 148, "name": "Warming Seelie"},
                {"id": 9991, "name": "Mysterious Totem"},
                {"id": 9992, "name": "Shrine of Depths"},  # resolves via name alias fallback
            ],
            "point_list": [
                {
                    "id": 1001,
                    "label_id": 44,
                    "x_pos": 120.5,
                    "y_pos": -340.25,
                    "z_level": 0,
                    "icon_sign": 0,
                    "point_group": None,
                },
                {
                    "id": 1002,
                    "label_id": 577,
                    "x_pos": -500.0,
                    "y_pos": 820.0,
                    "z_level": 0,
                    "icon_sign": 1,
                    "point_group": {"group_id": 248, "floor_id": 372},
                },
                {
                    "id": 1003,
                    "label_id": 148,
                    "x_pos": "42.25",
                    "y_pos": "19.75",
                },
                {
                    "id": 1004,
                    "label_id": 9991,
                    "x_pos": 10.0,
                    "y_pos": 20.0,
                },
                {
                    "id": 1005,
                    "label_id": 9992,
                    "x_pos": 30.0,
                    "y_pos": 40.0,
                },
            ],
        },
    }
    marked_list_envelope = {
        "retcode": 0,
        "message": "OK",
        "data": {
            "list": [
                {"point_id": 1001, "created_at": "2026-10-01 12:30:00"},
                {"point_id": "1001", "created_at": "2026-10-02 00:00:00"},  # duplicate ignored
                {"point_id": 1002, "updated_at": 1760000000},
                {"point_id": 1003, "marked_at": "2026/10/05 08:15:30"},
                {"point_id": 1004},
                {"point_id": 1005},
                {"point_id": 777777},  # missing from point_list (filter was off!)
                {"point_id": 888888},  # missing from point_list
            ]
        },
    }

    parsed = parse_hoyolab_export(point_list_envelope, marked_list_envelope, map_id=2)
    assert parsed.map_id == 2
    assert parsed.point_index_size == 5
    assert parsed.total_marked == 7
    assert parsed.missing_from_point_list == [777777, 888888]
    assert parsed.unmapped_label_ids == [9991]
    assert len(parsed.pins) == 5

    by_id = {p.hoyolab_id: p for p in parsed.pins}
    assert by_id[1001].canonical_category == "common_chest"
    assert by_id[1001].is_underground is False
    assert by_id[1001].marked_at == "2026-10-01T12:30:00Z"

    assert by_id[1002].canonical_category == "shrine_of_depths"
    assert by_id[1002].point_group == 248
    assert by_id[1002].floor_id == "372"
    assert by_id[1002].is_underground is True
    assert by_id[1002].marked_at is not None

    assert by_id[1003].canonical_category == "seelie"
    assert by_id[1003].x_pos == 42.25 and by_id[1003].y_pos == 19.75

    assert by_id[1004].canonical_category == "unmapped_9991"
    assert by_id[1004].label_name == "Mysterious Totem"

    # Label 9992 resolved automatically via label_list name alias -> shrine_of_depths
    assert by_id[1005].canonical_category == "shrine_of_depths"

    # Error envelope must raise ValueError instead of silently returning empty
    try:
        parse_hoyolab_export({"retcode": -100, "message": "Not logged in"}, marked_list_envelope)
        raise AssertionError("Expected ValueError on non-zero retcode")
    except ValueError:
        pass

    # Performance check: O(1) dictionary lookup with 20,000 catalog points + 3,000 marked pins
    big_pts = {
        "retcode": 0,
        "data": {
            "point_list": [
                {"id": i, "label_id": 44, "x_pos": float(i), "y_pos": float(-i)}
                for i in range(1, 20001)
            ]
        },
    }
    big_marked = {
        "retcode": 0,
        "data": {"list": [{"point_id": i * 5} for i in range(1, 3001)] + [{"point_id": 999999}]},
    }
    t0 = time.perf_counter()
    big_res = parse_hoyolab_export(big_pts, big_marked, map_id=2)
    elapsed = time.perf_counter() - t0
    assert len(big_res.pins) == 3000
    assert big_res.missing_from_point_list == [999999]
    assert elapsed < 2.0, f"O(1) parser took unexpectedly long: {elapsed:.3f}s"

    # Real multimap_v4.json compatibility sanity check (if dataset exists)
    multimap_path = os.path.join(PROJECT_ROOT, "src", "data", "multimap_v4.json")
    if os.path.exists(multimap_path):
        with open(multimap_path, "r", encoding="utf-8") as f:
            mm = json.load(f)
        enkanomiya_wps = mm["maps"]["7"]["waypoints"]
        enk_res = parse_hoyolab_export(
            {"retcode": 0, "data": {"point_list": enkanomiya_wps}},
            {"retcode": 0, "data": {"list": [{"point_id": w["id"]} for w in enkanomiya_wps[:5]]}},
            map_id=7,
        )
        assert len(enk_res.pins) == 5 and all(p.map_id == 7 for p in enk_res.pins)

    print("  [OK] HoYoLAB envelope parser, O(1) index & missing_from_point_list passed")


def verify_per_map_affine_and_y_flip() -> None:
    # 1. Empirical Kongying twt40 seed for map_id=2 (Teyvat)
    assert KONGYING_TWT40_SEED.map_id == 2
    kx, ky = KONGYING_TWT40_SEED.transform(300.0, 600.0)
    assert math.isclose(kx, 400.0 + 1523.4, abs_tol=1e-6)
    assert math.isclose(ky, 800.0 - 3921.6, abs_tol=1e-6)

    # 2. Refine Kongying twt40 seed with slightly shifted shared Waypoint anchors
    true_teyvat = AxisAffine(ax=4.0 / 3.0, bx=1531.0, cy=4.0 / 3.0, dy=-3915.0, map_id=2)
    src_anchors = [
        CanonicalPin(1, 2, 3, "teleport_waypoint", -2000.0, -1500.0),
        CanonicalPin(2, 2, 3, "teleport_waypoint", 1200.0, -800.0),
        CanonicalPin(3, 2, 2, "statue_of_the_seven", -500.0, 1400.0),
        CanonicalPin(4, 2, 3, "teleport_waypoint", 2400.0, 900.0),
    ]
    dst_anchors = [
        TargetPin(
            target_id=100 + idx,
            x=true_teyvat.transform(p.x_pos, p.y_pos)[0],
            y=true_teyvat.transform(p.x_pos, p.y_pos)[1],
            canonical_category=p.canonical_category,
            map_id=2,
        )
        for idx, p in enumerate(src_anchors)
    ]
    refined = calibrate_from_shared_anchors(
        src_anchors, dst_anchors, map_id=2, initial_seed=KONGYING_TWT40_SEED, max_seed_dist=100.0
    )
    assert refined.map_id == 2
    assert math.isclose(refined.ax, true_teyvat.ax, abs_tol=1e-6)
    assert math.isclose(refined.bx, true_teyvat.bx, abs_tol=1e-6)
    assert math.isclose(refined.cy, true_teyvat.cy, abs_tol=1e-6)
    assert math.isclose(refined.dy, true_teyvat.dy, abs_tol=1e-6)
    assert refined.rmse < 1e-6

    # 3. Sub-map (Enkanomiya map_id=7) with Y-axis flip (cy < 0) & non-isotropic scale
    enk_src = [(-400.0, -300.0), (500.0, -100.0), (-200.0, 600.0), (700.0, 400.0)]
    enk_true = AxisAffine(ax=1.25, bx=240.0, cy=-1.75, dy=1180.0, map_id=7)
    enk_dst = [enk_true.transform(x, y) for x, y in enk_src]
    enk_fit = fit_axis_affine(enk_src, enk_dst, map_id=7)
    assert enk_fit.map_id == 7
    assert enk_fit.has_y_flip() is True
    assert math.isclose(enk_fit.ax, 1.25, abs_tol=1e-9)
    assert math.isclose(enk_fit.bx, 240.0, abs_tol=1e-9)
    assert math.isclose(enk_fit.cy, -1.75, abs_tol=1e-9)
    assert math.isclose(enk_fit.dy, 1180.0, abs_tol=1e-9)
    assert enk_fit.rmse < 1e-9

    # Inverse round-trip
    inv = enk_fit.inverse()
    rx, ry = inv.transform(*enk_fit.transform(123.4, -567.8))
    assert math.isclose(rx, 123.4, abs_tol=1e-7)
    assert math.isclose(ry, -567.8, abs_tol=1e-7)

    # 4. Robust outlier trimming (1 bad anchor out of 6)
    rob_src = [(0.0, 0.0), (100.0, 50.0), (200.0, -100.0), (-150.0, 200.0), (300.0, 250.0), (50.0, -250.0)]
    rob_dst = [enk_true.transform(x, y) for x, y in rob_src]
    rob_dst[2] = (rob_dst[2][0] + 600.0, rob_dst[2][1] - 450.0)  # corrupt one anchor
    rob_fit = fit_axis_affine(rob_src, rob_dst, map_id=7, robust=True)
    assert rob_fit.anchor_count == 5
    assert math.isclose(rob_fit.ax, 1.25, abs_tol=1e-6)
    assert math.isclose(rob_fit.cy, -1.75, abs_tol=1e-6)

    # 5. Sub-map isolation: PerMapAffineRegistry NEVER reuses map_id=2 seed for map_id=7 or 9
    aff_reg = PerMapAffineRegistry()
    assert aff_reg.get("kongying", 2) is not None
    assert aff_reg.get("kongying", 7) is None
    assert aff_reg.get("kongying", 9) is None
    try:
        aff_reg.require("kongying", 7)
        raise AssertionError("Expected KeyError for uncalibrated sub-map 7")
    except KeyError:
        pass
    aff_reg.set("kongying", 7, enk_fit)
    assert aff_reg.require("kongying", 7).has_y_flip() is True

    # 6. Cross-map mismatch guard in match_pins_one_to_one
    try:
        match_pins_one_to_one(
            [CanonicalPin(1, 7, 44, "common_chest", 0.0, 0.0)],
            [TargetPin(10, 0.0, 0.0, canonical_category="common_chest", map_id=2)],
            affine=KONGYING_TWT40_SEED,
        )
        raise AssertionError("Expected ValueError when passing map_id=7 pin to map_id=2 affine")
    except ValueError:
        pass

    # 7. Web-Mercator pixel projection round-trip
    for lng, lat in ((-120.5, 35.2), (0.0, 0.0), (45.125, -22.75)):
        px, py = web_mercator_to_pixel(lng, lat, zoom=13)
        lng2, lat2 = pixel_to_web_mercator(px, py, zoom=13)
        assert math.isclose(lng, lng2, abs_tol=1e-7)
        assert math.isclose(lat, lat2, abs_tol=1e-7)

    # 8. Input validation guards on fit_axis_affine
    for bad_src, bad_dst in (
        ([(0.0, 0.0)], [(1.0, 1.0)]),  # < 2 points
        ([(0.0, 0.0), (1.0, 1.0)], [(1.0, 1.0)]),  # length mismatch
        ([(5.0, 0.0), (5.0, 10.0)], [(1.0, 2.0), (3.0, 4.0)]),  # zero X variance
    ):
        try:
            fit_axis_affine(bad_src, bad_dst, map_id=7)
            raise AssertionError(f"Expected ValueError for degenerate anchors: {bad_src}")
        except ValueError:
            pass

    print("  [OK] Per-map affine fitting, Y-flip, outlier trim & sub-map isolation passed")


def verify_strict_max_dist_and_category_bucketing() -> None:
    ident = AxisAffine(ax=1.0, bx=0.0, cy=1.0, dy=0.0, map_id=2)

    sources = [
        # 1. Exact category match within max_dist
        CanonicalPin(1, 2, 44, "common_chest", 100.0, 100.0),
        # 2. Missing target in region: nearest common_chest is 5000 units away -> MUST NOT match!
        CanonicalPin(2, 2, 44, "common_chest", 5100.0, 100.0),
        # 3. Wrong category at distance 0.0 (Exquisite target at Common source) -> MUST NOT match!
        CanonicalPin(3, 2, 44, "common_chest", 300.0, 300.0),
        # 4. N:1 regional Shrine of Depths (Natlan label_id=577 -> shrine_of_depths / o8)
        CanonicalPin(4, 2, 577, "shrine_of_depths", 500.0, 500.0),
        # 5. Warming Seelie (label_id=148 -> seelie / o18)
        CanonicalPin(5, 2, 148, "seelie", 700.0, 700.0),
        # 6. Unmapped label with name fallback match
        CanonicalPin(6, 2, 9991, "unmapped_9991", 900.0, 900.0, label_name="Mysterious Totem"),
        # 7. Truly unmapped label with no target match
        CanonicalPin(7, 2, 9999, "unmapped_9999", 1100.0, 1100.0, label_name="Ghost Relic"),
    ]
    targets = [
        TargetPin(101, 102.0, 100.0, canonical_category="common_chest", map_id=2),
        TargetPin(102, 300.0, 300.0, canonical_category="exquisite_chest", map_id=2),
        TargetPin(103, 501.0, 499.0, appsample_code="o8", map_id=2),
        TargetPin(104, 699.5, 700.5, appsample_code="o18", map_id=2),
        TargetPin(105, 901.0, 900.0, category_name="Mysterious Totem", map_id=2),
    ]

    report = match_pins_one_to_one(
        sources,
        targets,
        affine=ident,
        max_dist=15.0,
        missing_from_point_list=[888888],
    )

    matched_map = {m.source.hoyolab_id: m for m in report.matched}
    unmatched_map = {u.source.hoyolab_id: u for u in report.unmatched}

    assert set(matched_map.keys()) == {1, 4, 5, 6}
    assert matched_map[1].target.target_id == 101
    assert matched_map[4].target.target_id == 103
    assert matched_map[5].target.target_id == 104
    assert matched_map[6].target.target_id == 105
    assert matched_map[6].matched_via == "name_fallback"

    # Pin 2 (5000 units away) & Pin 3 (wrong category at 0 distance) strictly rejected
    assert unmatched_map[2].reason == "exceeds_max_dist"
    assert unmatched_map[2].nearest_distance is not None and unmatched_map[2].nearest_distance > 4900.0
    assert unmatched_map[3].reason == "exceeds_max_dist"
    assert unmatched_map[7].reason == "unmapped_category"
    assert report.unmapped_categories == ["unmapped_9999"]
    assert report.missing_from_point_list == [888888]
    # 4 matched out of (7 sources + 1 missing_from_point_list) = 50.0%
    assert math.isclose(report.match_rate_pct, 50.0, abs_tol=1e-6)

    print("  [OK] Strict max_dist cutoff, N:1 bucketing & name fallback passed")


def verify_one_to_one_and_ambiguity() -> None:
    ident = AxisAffine(ax=1.0, bx=0.0, cy=1.0, dy=0.0, map_id=2)

    # 1. Double-chest cluster (3 units apart): zero collision + both flagged ambiguous (d2 < 6.0)
    cluster_src = [
        CanonicalPin(10, 2, 44, "common_chest", 100.0, 100.0),
        CanonicalPin(11, 2, 44, "common_chest", 103.0, 100.0),
    ]
    cluster_dst = [
        TargetPin(201, 100.5, 100.0, canonical_category="common_chest"),
        TargetPin(202, 103.5, 100.0, canonical_category="common_chest"),
    ]
    rep = match_pins_one_to_one(cluster_src, cluster_dst, ident, max_dist=15.0, ambiguity_ratio=1.5)
    assert len(rep.matched) == 2
    assert len({m.target.target_id for m in rep.matched}) == 2, "1-to-1 collision detected!"
    assert len(rep.ambiguous) == 2, "Double-chest cluster must be flagged ambiguous"
    assert len(rep.unmatched) == 0

    # 2. Ambiguity ratio trigger (d1=8.0, d2=11.0 <= 8.0*1.5, even though d2 >= 6.0)
    #    vs unambiguous (d1=1.0, d2=25.0)
    ratio_src = [
        CanonicalPin(20, 2, 45, "exquisite_chest", 0.0, 0.0),
        CanonicalPin(21, 2, 46, "precious_chest", 0.0, 0.0),
    ]
    ratio_dst = [
        TargetPin(301, 8.0, 0.0, canonical_category="exquisite_chest"),
        TargetPin(302, 11.0, 0.0, canonical_category="exquisite_chest"),
        TargetPin(303, 1.0, 0.0, canonical_category="precious_chest"),
        TargetPin(304, 25.0, 0.0, canonical_category="precious_chest"),
    ]
    rep_r = match_pins_one_to_one(ratio_src, ratio_dst, ident, max_dist=15.0, ambiguity_ratio=1.5)
    by_src = {m.source.hoyolab_id: m for m in rep_r.matched}
    assert by_src[20].is_ambiguous is True
    assert by_src[21].is_ambiguous is False

    # 3. Augmenting-path optimality test:
    #    S1=(0,0), S2=(6,0); T1=(5,0), T2=(13,0); max_dist=10.0
    #    Greedy picks (S2, T1)=1.0 and strands S1 (d(S1,T2)=13 > 10)!
    #    Optimal 1-to-1 picks (S1, T1)=5.0 AND (S2, T2)=7.0 -> 2 matches, 0 collisions!
    aug_src = [
        CanonicalPin(30, 2, 44, "common_chest", 0.0, 0.0),
        CanonicalPin(31, 2, 44, "common_chest", 6.0, 0.0),
    ]
    aug_dst = [
        TargetPin(401, 5.0, 0.0, canonical_category="common_chest"),
        TargetPin(402, 13.0, 0.0, canonical_category="common_chest"),
    ]
    rep_opt = match_pins_one_to_one(aug_src, aug_dst, ident, max_dist=10.0, use_optimal_assignment=True)
    assert len(rep_opt.matched) == 2
    opt_pairs = {m.source.hoyolab_id: m.target.target_id for m in rep_opt.matched}
    assert opt_pairs == {30: 401, 31: 402}

    # 4. Capacity exhaustion (2 sources competing for 1 target -> 1 matched, 1 unmatched, 0 collision)
    cap_rep = match_pins_one_to_one(
        cluster_src,
        [TargetPin(501, 100.2, 100.0, canonical_category="common_chest")],
        ident,
        max_dist=15.0,
    )
    assert len(cap_rep.matched) == 1 and cap_rep.matched[0].source.hoyolab_id == 10
    assert len(cap_rep.unmatched) == 1 and cap_rep.unmatched[0].reason == "capacity_exhausted"

    # 5. Brute-force optimality verification across 25 random 5x5 bipartite graphs
    rng = random.Random(20261121)
    for _ in range(25):
        edges = []
        edge_map = {}
        for u in range(5):
            for v in range(5):
                if rng.random() < 0.55:
                    w = round(rng.uniform(0.1, 14.9), 3)
                    edges.append((u, v, w))
                    edge_map[(u, v)] = w

        got = _solve_bipartite_one_to_one(edges, use_optimal=True)
        assert len({v for v, _ in got.values()}) == len(got), "Duplicate target in SSAP!"
        got_card = len(got)
        got_cost = sum(d for _, d in got.values())

        # Exhaustive brute-force best (max cardinality, min cost)
        best_card = 0
        best_cost = float("inf")
        for k in range(5, -1, -1):
            if k < best_card:
                break
            for u_sub in itertools.combinations(range(5), k):
                for v_perm in itertools.permutations(range(5), k):
                    if all((u, v) in edge_map for u, v in zip(u_sub, v_perm)):
                        c = sum(edge_map[(u, v)] for u, v in zip(u_sub, v_perm))
                        if k > best_card or (k == best_card and c < best_cost):
                            best_card = k
                            best_cost = c
        assert got_card == best_card, f"SSAP card {got_card} != brute {best_card}"
        assert math.isclose(got_cost, best_cost, abs_tol=1e-5), f"SSAP cost {got_cost} != {best_cost}"

    # 6. Large bucket spatial grid equivalence check (200x200 = 40,000 > 25,000 threshold)
    grid_src = [
        CanonicalPin(1000 + i, 2, 44, "common_chest", float(i * 40), float((i % 7) * 50))
        for i in range(200)
    ]
    grid_dst = [
        TargetPin(2000 + i, float(i * 40) + 1.5, float((i % 7) * 50) - 0.5, canonical_category="common_chest")
        for i in range(195)
    ] + [
        TargetPin(9999, 999999.0, 999999.0, canonical_category="common_chest")
        for _ in range(5)
    ]
    grid_rep = match_pins_one_to_one(grid_src, grid_dst, ident, max_dist=15.0)
    assert len(grid_rep.matched) == 195
    assert len(grid_rep.unmatched) == 5
    assert all(u.reason == "exceeds_max_dist" for u in grid_rep.unmatched)

    print("  [OK] 1-to-1 optimal assignment, collision prevention, ambiguity & grid passed")


def verify_underground_layer_separation() -> None:
    ident = AxisAffine(ax=1.0, bx=0.0, cy=1.0, dy=0.0, map_id=2)

    # 3 chests stacked at exact same (x, y) across Surface, B1 (372), and B2 (373)
    stacked_src = [
        CanonicalPin(1, 2, 44, "common_chest", 500.0, -500.0, floor_id=None, point_group=None, is_underground=False),
        CanonicalPin(2, 2, 44, "common_chest", 500.0, -500.0, floor_id="372", point_group=248, is_underground=True),
        CanonicalPin(3, 2, 44, "common_chest", 500.0, -500.0, floor_id="373", point_group=248, is_underground=True),
    ]
    stacked_dst = [
        TargetPin(901, 500.0, -500.0, canonical_category="common_chest", floor_id="373", point_group=248, is_underground=True),
        TargetPin(902, 500.0, -500.0, canonical_category="common_chest", floor_id=None, is_underground=False),
        TargetPin(903, 500.0, -500.0, canonical_category="common_chest", floor_id="372", point_group=248, is_underground=True),
    ]
    rep = match_pins_one_to_one(stacked_src, stacked_dst, ident, max_dist=15.0)
    assert len(rep.matched) == 3
    pairs = {m.source.hoyolab_id: m.target.target_id for m in rep.matched}
    assert pairs == {1: 902, 2: 903, 3: 901}, f"Cross-floor leak detected: {pairs}"
    # Because each layer has only 1 layer-compatible target, none should be falsely flagged ambiguous!
    assert len(rep.ambiguous) == 0

    # Underground source vs surface-only target -> rejected with layer_mismatch
    ug_only = [CanonicalPin(4, 2, 44, "common_chest", 500.0, -500.0, floor_id="372", is_underground=True)]
    surf_only = [TargetPin(910, 500.0, -500.0, canonical_category="common_chest", is_underground=False)]
    rep_mis = match_pins_one_to_one(ug_only, surf_only, ident, max_dist=15.0)
    assert len(rep_mis.matched) == 0
    assert len(rep_mis.unmatched) == 1 and rep_mis.unmatched[0].reason == "layer_mismatch"

    # Underground source vs target WITHOUT layer metadata (is_underground=None, floor_id=None) -> allowed
    flat_target = [TargetPin(920, 500.5, -500.0, canonical_category="common_chest", is_underground=None, floor_id=None)]
    rep_flat = match_pins_one_to_one(ug_only, flat_target, ident, max_dist=15.0)
    assert len(rep_flat.matched) == 1 and rep_flat.matched[0].target.target_id == 920

    print("  [OK] Underground & multi-floor layer separation passed")


def verify_adapters_round_trip() -> None:
    # Shared source pins
    src_pins = [
        CanonicalPin(11, 2, 44, "common_chest", 100.0, 200.0, marked_at="2026-10-15T14:22:10Z"),
        CanonicalPin(12, 2, 577, "shrine_of_depths", 400.0, -100.0, marked_at="2026-10-16T09:05:00Z"),
        CanonicalPin(13, 2, 148, "seelie", 402.0, -100.0, marked_at="2026-10-16T09:06:00Z"),
    ]

    # --- A. KongyingAdapter ---
    ky_raw_markers = [
        {
            "id": 50020,
            "position": f"{KONGYING_TWT40_SEED.transform(100.0, 200.0)[0]},{KONGYING_TWT40_SEED.transform(100.0, 200.0)[1]}",
            "itemList": [{"name": "普通宝箱"}],
            "hiddenFlag": 0,
        },
        {
            "id": 50005,
            "position": list(KONGYING_TWT40_SEED.transform(400.0, -100.0)),
            "markerTitle": "地灵龛",
            "hiddenFlag": 0,
        },
        {
            "id": 50010,
            "position": list(KONGYING_TWT40_SEED.transform(402.0, -100.0)),
            "itemList": [{"name": "温暖仙灵"}],
            "extra": {"underground": {"is_underground": False}},
        },
    ]
    ky_targets = KongyingAdapter.parse_markers(ky_raw_markers, map_id=2)
    ky_report = match_pins_one_to_one(src_pins, ky_targets, KONGYING_TWT40_SEED, max_dist=15.0)
    assert len(ky_report.matched) == 3

    ky_payload = KongyingAdapter.export(ky_report, updated_at_ms=1761000000123)
    assert KongyingAdapter.validate_schema(ky_payload) == []
    assert isinstance(ky_payload["Data_KYJG"], list)
    assert ky_payload["Data_KYJG"] == [50005, 50010, 50020]
    assert ky_payload["Time_KYJG"]["50020"] == "2026/10/15 14:22:10"
    assert ky_payload["Time_KYJG"]["50005"] == "2026/10/16 09:05:00"
    assert ky_payload["updated_at"] == 1761000000123

    ky_json = KongyingAdapter.to_json(ky_payload)
    assert '"Data_KYJG": [' in ky_json  # never serialized as Set `{}`!
    ky_parsed = KongyingAdapter.parse(ky_json)
    assert ky_parsed == ky_payload

    # Merge with existing archive preserves prior IDs & timestamps
    existing_ky = {
        "Data_KYJG": [10001, 50020],
        "Time_KYJG": {"10001": "2025/01/01 00:00:00", "50020": "2025/05/05 05:05:05"},
        "updated_at": 1750000000000,
    }
    merged_ky = KongyingAdapter.merge_existing(existing_ky, ky_payload)
    assert merged_ky["Data_KYJG"] == [10001, 50005, 50010, 50020]
    assert merged_ky["Time_KYJG"]["50020"] == "2025/05/05 05:05:05"
    assert merged_ky["Time_KYJG"]["50005"] == "2026/10/16 09:05:00"

    # Reject buggy Set-as-object JSON
    assert len(KongyingAdapter.validate_schema({"Data_KYJG": {}, "Time_KYJG": {}, "updated_at": 1})) > 0

    # Kongying underground metadata list region_levels parsing
    ky_ug = KongyingAdapter.parse_markers(
        [
            {
                "id": 60001,
                "x": 10.0,
                "y": 20.0,
                "itemList": [{"name": "普通宝箱"}],
                "extra": {"underground": {"is_underground": True, "region_levels": ["B1"]}},
            }
        ],
        map_id=2,
    )
    assert len(ky_ug) == 1 and ky_ug[0].is_underground is True and ky_ug[0].floor_id == "B1"

    # include_ambiguous=False filtering across adapters
    ident = AxisAffine(ax=1.0, bx=0.0, cy=1.0, dy=0.0, map_id=2)
    amb_src = [
        CanonicalPin(1, 2, 44, "common_chest", 0.0, 0.0),
        CanonicalPin(2, 2, 44, "common_chest", 3.0, 0.0),
        CanonicalPin(3, 2, 45, "exquisite_chest", 500.0, 500.0),
    ]
    amb_dst = [
        TargetPin(101, 0.2, 0.0, canonical_category="common_chest", feature_key="MondstadtCommonChest"),
        TargetPin(102, 3.2, 0.0, canonical_category="common_chest", feature_key="MondstadtCommonChest"),
        TargetPin(103, 500.2, 500.0, canonical_category="exquisite_chest", feature_key="MondstadtExquisiteChest"),
    ]
    amb_rep = match_pins_one_to_one(amb_src, amb_dst, ident, max_dist=15.0)
    assert len(amb_rep.matched) == 3 and len(amb_rep.ambiguous) == 2 and len(amb_rep.confident_matches) == 1
    assert KongyingAdapter.export(amb_rep, include_ambiguous=False, updated_at_ms=1000)["Data_KYJG"] == [103]
    assert AppsampleAdapter.export(amb_rep, include_ambiguous=False) == {"ids": [103]}
    gm_conf_dec = GenshinMapGithubAdapter.decode(
        GenshinMapGithubAdapter.export(amb_rep, include_ambiguous=False, default_unix_sec=1760000000)
    )
    assert list(gm_conf_dec["completed"]["features"].keys()) == ["MondstadtExquisiteChest/103"]

    # --- B. AppsampleAdapter (columnar delta-encoded Web-Mercator markers) ---
    # Create synthetic linear relationship in Web-Mercator pixel space (zoom=13)
    app_affine = AxisAffine(ax=12.5, bx=1048576.0, cy=-12.5, dy=1048576.0, map_id=2)
    coords_ll = [pixel_to_web_mercator(*app_affine.transform(p.x_pos, p.y_pos), zoom=13) for p in src_pins]
    columnar_app = {
        "id": [7000, 15, 20, 5],  # delta-encoded -> 7000, 7015, 7035, 7040
        "lng": [coords_ll[0][0], coords_ll[1][0], coords_ll[2][0], 0.0],
        "lat": [coords_ll[0][1], coords_ll[1][1], coords_ll[2][1], 0.0],
        "type": ["o44", "o8", "o18", "o44"],
        "mid": [2, 2, 2, 7],  # last item is on map_id=7 -> filtered out for map_id=2
    }
    app_targets = AppsampleAdapter.parse_markers(columnar_app, map_id=2, zoom=13, delta_encoded=True)
    assert [t.target_id for t in app_targets] == [7000, 7015, 7035]
    app_report = match_pins_one_to_one(src_pins, app_targets, app_affine, max_dist=15.0)
    assert len(app_report.matched) == 3

    app_payload = AppsampleAdapter.export(app_report)
    assert AppsampleAdapter.validate_schema(app_payload) == []
    assert app_payload == {"ids": [7000, 7015, 7035]}
    app_parsed = AppsampleAdapter.parse(AppsampleAdapter.to_json(app_payload))
    assert app_parsed == app_payload
    assert AppsampleAdapter.merge_existing({"ids": [100, 7000]}, app_payload) == {"ids": [100, 7000, 7015, 7035]}

    # --- C. GenshinMapGithubAdapter (native UTF-8 safe GM_007~ Base64) ---
    gm_affine = AxisAffine(ax=0.05, bx=10.0, cy=-0.05, dy=-20.0, map_id=2)
    gm_raw = [
        {
            "featureKey": "MondstadtCommonChest",
            "id": "a" * 40,
            "x": gm_affine.transform(100.0, 200.0)[0],
            "y": gm_affine.transform(100.0, 200.0)[1],
        },
        {
            "featureKey": "NatlanShrineOfDepths",
            "id": "b" * 40,
            "x": gm_affine.transform(400.0, -100.0)[0],
            "y": gm_affine.transform(400.0, -100.0)[1],
        },
        {
            "featureKey": "DragonspineSeelie",
            "id": "c" * 40,
            "x": gm_affine.transform(402.0, -100.0)[0],
            "y": gm_affine.transform(402.0, -100.0)[1],
        },
    ]
    gm_targets = GenshinMapGithubAdapter.parse_markers(gm_raw, map_id=2)
    gm_report = match_pins_one_to_one(src_pins, gm_targets, gm_affine, max_dist=5.0)
    assert len(gm_report.matched) == 3

    gm_b64 = GenshinMapGithubAdapter.export(gm_report, default_unix_sec=1760000000)
    raw_decoded = base64.b64decode(gm_b64.encode("ascii")).decode("utf-8")
    assert raw_decoded.startswith("GM_007~")
    gm_decoded = GenshinMapGithubAdapter.decode(gm_b64)
    assert set(gm_decoded.keys()) == {"version", "options", "displayed", "completed", "editor"}
    assert gm_decoded["version"] == "GM_007"
    feats = gm_decoded["completed"]["features"]
    assert isinstance(feats, dict) and len(feats) == 3
    assert f"MondstadtCommonChest/{'a' * 40}" in feats
    assert f"NatlanShrineOfDepths/{'b' * 40}" in feats
    assert f"DragonspineSeelie/{'c' * 40}" in feats
    assert all(isinstance(v, int) and v > 0 for v in feats.values())

    # UTF-8 safe Base64 non-destructive merge (Vietnamese + Chinese + Emoji in existing save)
    existing_gm_obj = {
        "version": "GM_007",
        "options": {"locale": "vi-VN", "note": "Rương ở vách đá 璃月 🧭"},
        "displayed": {"features": {"MondstadtCommonChest": True}, "routes": {}},
        "completed": {"features": {f"LiyueGeoculus/{'f' * 40}": 1700000001}},
        "editor": {"feature": {"data": [{"title": "Điểm đánh dấu tiếng Việt"}]}, "route": {"data": []}},
    }
    existing_gm_b64 = GenshinMapGithubAdapter.encode(existing_gm_obj)
    merged_gm_b64 = GenshinMapGithubAdapter.merge_into_encoded(existing_gm_b64, gm_report)
    merged_gm = GenshinMapGithubAdapter.decode(merged_gm_b64)
    assert merged_gm["options"]["note"] == "Rương ở vách đá 璃月 🧭"
    assert merged_gm["editor"]["feature"]["data"][0]["title"] == "Điểm đánh dấu tiếng Việt"
    assert merged_gm["completed"]["features"][f"LiyueGeoculus/{'f' * 40}"] == 1700000001
    assert len(merged_gm["completed"]["features"]) == 4

    # Reject invalid list-shaped completed.features (stale JSDoc trap)
    bad_gm = dict(existing_gm_obj)
    bad_gm["completed"] = {"features": ["MondstadtCommonChest/abc"]}
    assert len(GenshinMapGithubAdapter.validate_schema(bad_gm)) > 0

    # --- D. CanonicalJsonAdapter ---
    canon_payload = CanonicalJsonAdapter.export(src_pins, map_id=2, exported_at="2026-11-21T10:30:00Z")
    canon_json = CanonicalJsonAdapter.to_json(canon_payload)
    canon_pins, canon_meta = CanonicalJsonAdapter.parse(canon_json)
    assert canon_meta["schema"] == CanonicalJsonAdapter.SCHEMA
    assert canon_meta["map_id"] == 2
    assert len(canon_pins) == 3
    assert [p.hoyolab_id for p in canon_pins] == [11, 12, 13]
    assert canon_pins[0].marked_at == "2026-10-15T14:22:10Z"

    # Canonical export directly from HoyolabParseResult preserves missing_from_point_list
    parsed_with_missing = parse_hoyolab_export(
        {"retcode": 0, "data": {"point_list": [{"id": 11, "label_id": 44, "x_pos": 1.0, "y_pos": 2.0}]}},
        {"retcode": 0, "data": {"list": [{"point_id": 11}, {"point_id": 55555}]}},
        map_id=2,
    )
    canon_from_res = CanonicalJsonAdapter.export(parsed_with_missing, exported_at="2026-11-21T10:30:00Z")
    _, res_meta = CanonicalJsonAdapter.parse(CanonicalJsonAdapter.to_json(canon_from_res))
    assert res_meta["missing_from_point_list"] == [55555]
    assert res_meta["stats"]["missing_from_point_list_count"] == 1

    print("  [OK] Kongying, Appsample, GM_007~ UTF-8 Base64 & Canonical JSON round-trips passed")


def main() -> None:
    print("Running Genshin-Helper v5 Map Bridge verification suite...")
    verify_category_aliases_n_to_1()
    verify_hoyolab_envelope_parser()
    verify_per_map_affine_and_y_flip()
    verify_strict_max_dist_and_category_bucketing()
    verify_one_to_one_and_ambiguity()
    verify_underground_layer_separation()
    verify_adapters_round_trip()
    print("ALL MAP BRIDGE V5 CHECKS PASSED!")


if __name__ == "__main__":
    main()
