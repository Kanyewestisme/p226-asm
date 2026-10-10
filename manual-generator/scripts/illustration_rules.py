"""Geometry-only suggestions for initial, editable installation illustrations.

Scores use orthographic projections of bounding boxes, not hidden-line CAD
results. A candidate distance point is a measured geometric clue, never a
verified joint, insertion axis, clearance or screw location. This module has no
CAD/AI dependency and cannot change model geometry or a saved user profile.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import product
import math


RULE_VERSION = 1
PROXY_WARNING = (
    "视角与分离距离按包围盒投影重叠和箭头投影长度建议；这是包围盒代理评分，"
    "不是精确遮挡、连接可见性或装配运动验证。"
)
CONTACT_WARNING = (
    "箭头锚点取自已测几何距离候选点；局部图仅选取候选的源实例，"
    "不能证明孔位、螺丝身份或实际连接。"
)


def _add(a, b):
    return [x + y for x, y in zip(a, b)]


def _sub(a, b):
    return [x - y for x, y in zip(a, b)]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _length(a):
    return math.hypot(*a)


def _unit(a):
    size = _length(a)
    if size < 1e-12:
        raise ValueError("Illustration direction must be nonzero")
    return [x / size for x in a]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _frame(camera, up):
    view = _unit(camera)
    right = _unit(_cross(up, view))
    upward = _unit(_cross(view, right))
    return right, upward


def _center(bounds):
    return [a + (b - a) / 2 for a, b in zip(*bounds)]


def _bounds(rows):
    return ([min(row["bbox_min"][axis] for row in rows) for axis in range(3)],
            [max(row["bbox_max"][axis] for row in rows) for axis in range(3)])


def _project_box(bounds, frame, offset):
    points = [_add(corner, offset) for corner in product(*zip(*bounds))]
    xs = [_dot(point, frame[0]) for point in points]
    ys = [_dot(point, frame[1]) for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def projection_metrics(camera, up, moving_bounds, assembled_bounds, offset):
    """Return bounded proxy metrics; no metric claims actual CAD visibility.

    ``assembled_bounds`` is the union bounding box of the retained groups. Its
    empty gaps also count as proxy overlap, so this deliberately conservative
    metric is labelled in every proposal and is never used to hide geometry.
    """
    frame = _frame(camera, up)
    moving = _project_box(moving_bounds, frame, offset)
    stationary = _project_box(assembled_bounds, frame, [0, 0, 0])
    area = (moving[2] - moving[0]) * (moving[3] - moving[1])
    intersection = max(0, min(moving[2], stationary[2]) - max(moving[0], stationary[0])) * max(
        0, min(moving[3], stationary[3]) - max(moving[1], stationary[1]))
    overlap = min(1.0, intersection / area) if area > 1e-12 else 0.0
    length = _length(offset)
    arrow_fraction = min(1.0, math.hypot(_dot(offset, frame[0]), _dot(offset, frame[1])) / length) if length else 0.0
    return {"kind": "bounding_box_projection_proxy", "projected_arrow_fraction": arrow_fraction,
            "moving_box_overlap_fraction": overlap, "visibility_verified": False,
            "assembly_motion_verified": False}


def _inside(point, part, tolerance):
    return (isinstance(point, (list, tuple)) and len(point) == 3 and
            all(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
                and lower - tolerance <= value <= upper + tolerance
                for value, lower, upper in zip(point, part["bbox_min"], part["bbox_max"])))


def _contact_options(contacts, parts, moving_ids, assembled_ids, tolerance):
    result = []
    for contact in contacts:
        pair = contact.get("part_ids", [])
        if len(pair) != 2 or pair[0] not in parts or pair[1] not in parts:
            continue
        a, b = pair
        point_a, point_b = contact.get("point_a"), contact.get("point_b")
        if not _inside(point_a, parts[a], tolerance) or not _inside(point_b, parts[b], tolerance):
            continue
        if a in moving_ids and b in assembled_ids:
            moving, stationary, endpoint, other = a, b, point_a, point_b
        elif b in moving_ids and a in assembled_ids:
            moving, stationary, endpoint, other = b, a, point_b, point_a
        else:
            continue
        result.append({"moving_part_id": moving, "stationary_part_id": stationary,
                       "point": list(endpoint), "stationary_point": list(other),
                       "distance_mm": contact["distance_mm"]})
    # Limit local proposals to the closest measured candidates. Ordering only
    # uses occurrence identity and measured geometry, never display names.
    return sorted(result, key=lambda item: (item["distance_mm"], item["moving_part_id"],
                                           item["stationary_part_id"], tuple(item["point"]),
                                           tuple(item["stationary_point"])))[:16]


def _camera_options(camera):
    # Four related book views retain the same up convention. The default view
    # wins near-ties; a local view is suggested only for a material score gain.
    candidates = [list(camera)]
    for x_sign, z_sign in ((-1, 1), (1, -1), (-1, -1)):
        value = [camera[0] * x_sign, camera[1], camera[2] * z_sign]
        if value not in candidates:
            candidates.append(value)
    return candidates


def _best_layout(camera, up, moving_bounds, assembled_bounds, span, ratio, away):
    fallback = [1, 0, 0]
    primary = _unit(away if _length(away) > span * 1e-7 else fallback)
    directions = [primary]
    for axis in range(3):
        for sign in (1, -1):
            value = [0, 0, 0]
            value[axis] = sign
            if value not in directions:
                directions.append(value)
    cameras = _camera_options(camera)
    choices = []
    for camera_index, proposed_camera in enumerate(cameras):
        for direction_index, direction in enumerate(directions):
            for distance_index, factor in enumerate((1.0, 1.35, 0.8)):
                distance = span * ratio * factor
                offset = [value * distance for value in direction]
                metrics = projection_metrics(proposed_camera, up, moving_bounds, assembled_bounds, offset)
                # Prefer the global view, modest distances and the outward
                # reference direction when readability proxy gains are small.
                score = (0.65 * metrics["projected_arrow_fraction"] +
                         0.35 * (1 - metrics["moving_box_overlap_fraction"]) -
                         0.04 * abs(factor - 1) - 0.025 * (direction_index != 0))
                choices.append((score, -camera_index, -direction_index, -distance_index,
                                proposed_camera, offset, metrics))
    base = max(choice for choice in choices if choice[1] == 0)
    best = max(choices)
    chosen = best if best[0] > base[0] + 0.10 else base
    return {"camera": list(chosen[4]), "offset": list(chosen[5]), "metrics": chosen[6],
            "local_camera": chosen[1] != 0,
            "score": chosen[0], "default_camera_score": base[0]}


def suggest_initial_illustrations(plan: dict, analysis: dict, *, fresh_automatic_draft: bool = False) -> dict:
    """Return a proposed initial draft; saved profiles/edits remain untouched.

    Call only while creating a fresh automatic draft. Guarded plans are copied
    unchanged rather than reinterpreting prior user cameras, hidden parts or
    original-template mappings. Geometry/contact validity belongs to the draft
    planner's analysis validator; defensively ignore out-of-box contact points.
    """
    result = deepcopy(plan)
    if (fresh_automatic_draft is not True or plan.get("status") != "draft" or plan.get("configuration_origin") or
            plan.get("pdf_template") or plan.get("manual_document") or
            any(step.get("reviewed") or step.get("hidden_part_ids") for step in plan.get("steps", []))):
        return result
    parts = {part["id"]: part for part in analysis["parts"]}
    groups = {group["id"]: group for group in plan["groups"]}
    model_bounds = _bounds(list(parts.values()))
    span = max(b - a for a, b in zip(*model_bounds))
    if not math.isfinite(span) or span <= 0:
        raise ValueError("Illustration suggestions require positive finite model bounds")
    group_bounds = {gid: _bounds([parts[pid] for pid in group["part_ids"]]) for gid, group in groups.items()}
    style = result["style"]
    style["illustration_rules"] = {
        "version": RULE_VERSION, "status": "proposed", "camera_family": "four_related_isometric_views",
        "up_basis": "source_coordinate_convention_not_verified_gravity", "base_explode_ratio": style["explode_ratio"],
        "arrow_color": "#d62720", "arrow_meaning": "display_reposition_only", "geometry_simplification": "none_added_or_removed",
        "automatic_hidden_parts": False, "scoring": "bounding_box_projection_proxy",
    }
    tolerance = max(span * 1e-7, 1e-8)
    for step in result["steps"]:
        if not step.get("assembled_groups") or not step.get("moving_groups"):
            # Initial reference and final overview use the same book view.
            step["camera"], step["up"] = list(style["camera"]), list(style["up"])
            continue
        if len(step["moving_groups"]) != 1:
            continue  # Multi-group authored scenes belong to their editor.
        gid = step["moving_groups"][0]
        moving_ids = set(groups[gid]["part_ids"])
        assembled_ids = {pid for other in step["assembled_groups"] for pid in groups[other]["part_ids"]}
        stationary_bounds = _bounds([parts[pid] for pid in sorted(assembled_ids)])
        contacts = _contact_options(analysis.get("contacts", []), parts, moving_ids, assembled_ids, tolerance)
        contact = contacts[0] if contacts else None
        moving_bounds = group_bounds[gid]
        center = _center(moving_bounds)
        if contact:
            neighbor_bounds = (parts[contact["stationary_part_id"]]["bbox_min"], parts[contact["stationary_part_id"]]["bbox_max"])
            away = _sub(center, _center(neighbor_bounds))
            anchor = contact["point"]
        else:
            away = _sub(center, _center(stationary_bounds))
            anchor = center
        layout = _best_layout(style["camera"], style["up"], moving_bounds, stationary_bounds,
                              span, style["explode_ratio"], away)
        step["camera"], step["up"] = layout["camera"], list(style["up"])
        step["offsets"] = {gid: layout["offset"]}
        step["arrows"] = [{"from": _add(anchor, layout["offset"]), "to": list(anchor),
                           "meaning": "reposition", "status": "proposed"}]
        step["illustration_suggestion"] = {
            "version": RULE_VERSION, "status": "proposed", "metrics": layout["metrics"],
            "score": layout["score"], "default_camera_score": layout["default_camera_score"],
            "local_camera": layout["local_camera"], "source_sha256": analysis["source_sha256"],
            "anchor_basis": "measured_candidate_point" if contact else "moving_group_bbox_center_proxy",
        }
        step["warnings"].append(PROXY_WARNING)
        if contact:
            pair = [contact["moving_part_id"], contact["stationary_part_id"]]
            step["focus_part_ids"] = pair
            step["illustration_suggestion"]["contact_focus"] = {
                "part_ids": pair, "point": contact["point"], "stationary_point": contact["stationary_point"],
                "distance_mm": contact["distance_mm"], "status": "unverified",
                "scope": "whole_source_instances_not_inferred_hole_or_fastener",
            }
            step["warnings"].append(CONTACT_WARNING)
            step["evidence"].append({"kind": "illustration_candidate_point", "part_ids": pair,
                                     "status": "unverified", "source_sha256": analysis["source_sha256"]})
        elif analysis.get("contacts"):
            step["warnings"].append("未采用不在源实例包围盒内或不属于本步两侧的候选点；箭头暂用组中心代理。")
    result["warnings"].append(PROXY_WARNING)
    return result
