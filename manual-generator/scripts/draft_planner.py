"""Deterministic, editable installation proposals from STEP analysis only.

CAD occurrences, proximity and solid volume are useful *clues*. They do not
establish a purchased assembly, insertion direction, fastener specification or
safe assembly order. Every generated group, connection and step stays proposed
until a person reviews it. The module has no CAD or third-party dependency.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any


def _fail(message: str) -> None:
    raise ValueError(message)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number")
    if not math.isfinite(value):
        _fail(f"{label} must be a finite number")
    return float(value)


def _vector(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        _fail(f"{label} must be a three-dimensional vector")
    return [_number(v, label) for v in value]


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a nonempty string")
    return value


def _file_id(value: Any, label: str) -> str:
    result = _identifier(value, label)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", result) or result.upper() in reserved:
        _fail(f"{label} must be a safe ASCII ID of at most 80 characters")
    return result


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        _fail(f"{label} must be a list of strings")
    return value


def _ids(value: Any, allowed: set[str], label: str) -> list[str]:
    result = _strings(value, label)
    if len(result) != len(set(result)):
        _fail(f"{label} contains duplicate IDs")
    if not set(result) <= allowed:
        _fail(f"{label} references unknown IDs")
    return result


def _finite_tree(value: Any, label: str = "document") -> None:
    """Reject JSON NaN/Infinity even inside optional metadata."""
    if isinstance(value, float) and not math.isfinite(value):
        _fail(f"{label} contains a non-finite number")
    if isinstance(value, dict):
        for key, child in value.items():
            _finite_tree(child, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _finite_tree(child, f"{label}[{index}]")


def _analysis_parts(analysis: dict) -> dict[str, dict]:
    if not isinstance(analysis, dict):
        _fail("analysis must be an object")
    _finite_tree(analysis, "analysis")
    _identifier(analysis.get("source_sha256"), "analysis.source_sha256")
    rows = analysis.get("parts")
    if not isinstance(rows, list) or not rows:
        _fail("analysis.parts must contain at least one STEP occurrence")
    result = {}
    for part in rows:
        if not isinstance(part, dict):
            _fail("each analysis part must be an object")
        part_id = _identifier(part.get("id"), "part.id")
        if part_id in result:
            _fail(f"duplicate analysis part ID: {part_id}")
        lower = _vector(part.get("bbox_min"), f"{part_id}.bbox_min")
        upper = _vector(part.get("bbox_max"), f"{part_id}.bbox_max")
        if any(a > b for a, b in zip(lower, upper)):
            _fail(f"invalid bounding box for {part_id}")
        for a, b in zip(lower, upper):
            _number(b - a, f"{part_id}.bounding-box extent")
        if "bbox_size" in part:
            size = _vector(part["bbox_size"], f"{part_id}.bbox_size")
            if any(v < 0 for v in size):
                _fail(f"negative bounding-box size for {part_id}")
        if "volume_mm3" in part and _number(part["volume_mm3"], "volume_mm3") < 0:
            _fail(f"negative volume for {part_id}")
        result[part_id] = part
    if not isinstance(analysis.get("contacts", []), list):
        _fail("analysis.contacts must be a list")
    for contact in analysis.get("contacts", []):
        if not isinstance(contact, dict):
            _fail("each candidate contact must be an object")
        pair = _ids(contact.get("part_ids"), set(result), "contact.part_ids")
        if len(pair) != 2:
            _fail("a candidate contact requires two distinct occurrences")
        if _number(contact.get("distance_mm"), "contact.distance_mm") < 0:
            _fail("candidate contact distance cannot be negative")
        _vector(contact.get("point_a"), "contact.point_a")
        _vector(contact.get("point_b"), "contact.point_b")
    _strings(analysis.get("warnings", []), "analysis.warnings")
    return result


def _bounds(rows: list[dict]) -> tuple[list[float], list[float]]:
    return (
        [min(p["bbox_min"][axis] for p in rows) for axis in range(3)],
        [max(p["bbox_max"][axis] for p in rows) for axis in range(3)],
    )


def _center(bounds: tuple[list[float], list[float]]) -> list[float]:
    return [a + (b - a) / 2 for a, b in zip(*bounds)]


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(v * v for v in vector))


def _camera(camera: Any, up: Any, label: str) -> None:
    view = _vector(camera, f"{label}.camera")
    upward = _vector(up, f"{label}.up")
    a, b = _norm(view), _norm(upward)
    if a < 1e-12 or b < 1e-12 or a > 1e9 or b > 1e9:
        _fail(f"{label} camera and up must have nonzero magnitudes of at most 1e9")
    cross = [view[1] * upward[2] - view[2] * upward[1],
             view[2] * upward[0] - view[0] * upward[2],
             view[0] * upward[1] - view[1] * upward[0]]
    if _norm(cross) / (a * b) < 1e-8:
        _fail(f"{label} camera and up cannot be parallel")


def _path(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(token) for token in value if str(token))
    if isinstance(value, str) and value:
        return tuple(token for token in value.replace("\\", "/").split("/") if token)
    return ()


def _parent_path(part: dict) -> tuple[str, ...]:
    # Importers often store parent_occurrence as an opaque source-scoped hash.
    # The numeric occurrence path preserves hierarchy depth; prefer it when
    # available so nested fasteners inherit their meaningful top-level branch.
    occurrence = _path(part.get("occurrence_path"))
    if len(occurrence) > 1:
        return occurrence[:-1]
    explicit = _path(part.get("parent_occurrence"))
    if explicit:
        return explicit
    return ()


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not paths:
        return ()
    prefix = paths[0]
    for path in paths[1:]:
        prefix = prefix[:next((i for i, (a, b) in enumerate(zip(prefix, path))
                               if a != b), min(len(prefix), len(path)))]
    return prefix


def _stable_id(prefix: str, values: Any) -> str:
    encoded = json.dumps(values, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()[:16]


def _groups(parts: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Use occurrence branches, never label equality, as grouping evidence."""
    parents = {part_id: _parent_path(part) for part_id, part in parts.items()}
    # A leaf with no hierarchy must not erase the common CAD wrapper of the
    # remaining, identified occurrences and collapse them into the root.
    common = _common_prefix([parent for parent in parents.values() if parent])
    buckets = defaultdict(list)
    for part_id in sorted(parts):
        parent = parents[part_id]
        # Strip the common product/root assembly. Its direct leaves remain
        # separate: grouping them all would hide the installation problem.
        branch = parent[:len(common) + 1] if len(parent) > len(common) else ()
        key = ("branch", branch) if branch else ("part", part_id)
        buckets[key].append(part_id)
    groups, fallback_count = [], 0
    for key, part_ids in sorted(buckets.items(), key=lambda item: item[1][0]):
        labels = [str(parts[p].get("name") or p) for p in part_ids]
        if key[0] == "branch":
            # Display labels may repeat; the identity remains occurrence based.
            original_path = parts[part_ids[0]].get("path", [])
            depth = len(key[1]) - 1
            label = (str(original_path[depth]) if isinstance(original_path, list)
                     and len(original_path) > depth else labels[0])
            if len(part_ids) > 1:
                label += f"（CAD 子总成，{len(part_ids)} 个实例）"
            basis = "同一 CAD occurrence 层级分支；是否作为安装总成交付仍需确认。"
        else:
            label = labels[0]
            fallback_count += 1
            basis = "缺少可区分的安装总成层级，暂按单个 STEP 实例分组；可人工合并。"
        groups.append({"id": _stable_id("group-", part_ids), "label": label,
                       "part_ids": part_ids, "basis": basis, "status": "proposed",
                       "role": "unclassified"})
    warnings = []
    if fallback_count:
        warnings.append(f"{fallback_count} 个实例缺少可用总成分支，已保留为独立组；"
                        "请确认是否应合并为随产品交付的总成。")
    return groups, warnings


def _connections(analysis: dict, groups: list[dict]) -> list[dict]:
    part_group = {part_id: group["id"] for group in groups for part_id in group["part_ids"]}
    buckets = defaultdict(list)
    for contact in analysis.get("contacts", []):
        group_pair = tuple(sorted({part_group[p] for p in contact["part_ids"]}))
        item = deepcopy(contact)
        item["status"] = "unverified"
        buckets[group_pair].append(item)
    result = []
    for pair, contacts in sorted(buckets.items()):
        contacts.sort(key=lambda c: (tuple(sorted(c["part_ids"])), c["distance_mm"],
                                     tuple(c["point_a"]), tuple(c["point_b"])))
        result.append({"id": _stable_id("connection-", [pair, contacts]),
                       "group_ids": list(pair),
                       "part_ids": sorted({p for c in contacts for p in c["part_ids"]}),
                       "contacts": contacts,
                       "distance_mm": min(c["distance_mm"] for c in contacts),
                       "status": "unverified",
                       "basis": "STEP 几何距离候选；不能证明连接、紧固方式或装配先后。"})
    return result


def build_plan(analysis: dict, title: str = "安装说明草案", *, groups: list[dict] | None = None) -> dict:
    """Propose groups, order and consistent exploded views without old manuals."""
    parts = _analysis_parts(analysis)
    _identifier(title, "title")
    automatic_grouping = groups is None
    if groups is None:
        groups, hierarchy_warnings = _groups(parts)
    else:
        if not isinstance(groups, list) or not groups:
            _fail("supplied groups must be a nonempty list")
        groups = deepcopy(groups)
        covered, group_ids = set(), set()
        for group in groups:
            if not isinstance(group, dict):
                _fail("supplied group must be an object")
            group_id = _file_id(group.get("id"), "group.id")
            if group_id in group_ids:
                _fail("supplied groups contain duplicate IDs")
            group_ids.add(group_id)
            members = _ids(group.get("part_ids"), set(parts), "group.part_ids")
            if not members or covered.intersection(members):
                _fail("supplied groups require distinct, nonempty STEP instance sets")
            covered.update(members)
            _identifier(group.get("label"), "group.label")
            _identifier(group.get("basis"), "group.basis")
            if group.get("status") not in ("proposed", "confirmed"):
                _fail("supplied group status must be proposed or confirmed")
        if covered != set(parts):
            _fail("supplied groups must cover every STEP occurrence")
        hierarchy_warnings = ["使用包装调整后的实例分组重新提出步骤；总成边界和新的先后顺序需要核对。"]
    by_group = {group["id"]: group for group in groups}
    connections = _connections(analysis, groups)
    graph = {group_id: {} for group_id in by_group}
    for candidate in connections:
        if len(candidate["group_ids"]) == 2:
            a, b = candidate["group_ids"]
            graph[a][b] = candidate
            graph[b][a] = candidate
    bounds = _bounds(list(parts.values()))
    sizes = [b - a for a, b in zip(*bounds)]
    span = max(sizes)
    if span <= 0:
        _fail("STEP bounding box must have a positive extent")
    model_center = _center(bounds)
    # Three geometry-only presets keep long products readable on the page.
    long_axis = max(range(3), key=lambda axis: (sizes[axis], -axis))
    preset = ([0.7, 1, 1], [1, 0.7, 1], [1, 1, 0.7])[long_axis]
    style = {"camera": list(preset), "up": [0, 1, 0], "width": 1100,
             "height": 800, "margin": 70, "hlr": "poly", "explode_ratio": 0.22}
    group_bounds = {g: _bounds([parts[p] for p in by_group[g]["part_ids"]]) for g in by_group}
    volume = {g: sum(parts[p].get("volume_mm3", 0) for p in by_group[g]["part_ids"])
              for g in by_group}
    box_volume = {g: math.prod(b - a for a, b in zip(*group_bounds[g])) for g in by_group}

    def base_key(group_id):
        return (-volume[group_id], -len(graph[group_id]), -box_volume[group_id], group_id)

    remaining, assembled, steps = set(by_group), [], []
    disconnected_starts = 0
    while remaining:
        frontier = [(graph[g][a]["distance_mm"], base_key(g), g, a)
                    for g in sorted(remaining) for a in assembled if a in graph[g]]
        candidate = None
        if frontier:
            _, _, group_id, neighbor = min(frontier)
            candidate = graph[group_id][neighbor]
        else:
            group_id = min(remaining, key=base_key)
            if assembled:
                disconnected_starts += 1
        group = by_group[group_id]
        center = _center(group_bounds[group_id])
        warnings = ["自动提出的先后顺序尚未确认；CAD 层级和几何距离不能证明实际装配顺序。"]
        evidence = [{"kind": "cad_occurrence_group", "group_id": group_id,
                     "part_ids": group["part_ids"], "status": "proposed"}]
        if not assembled:
            offset, arrows = [0, 0, 0], []
            instruction = (f"以「{group['label']}」作为草案的起始参考组。"
                           "起始组按 STEP 体积和邻近候选选择，请确认实际操作是否合适。")
            warnings.append("STEP 坐标未证明产品重力方向、稳定放置方式或安装基座。")
        else:
            direction = [c - m for c, m in zip(center, model_center)]
            if _norm(direction) < span * 1e-6:
                direction = [1, 0, 0]
            distance = span * style["explode_ratio"]
            length = _norm(direction)
            offset = [v / length * distance for v in direction]
            arrows = [{"from": [c + d for c, d in zip(center, offset)],
                       "to": list(center),
                       "meaning": "reposition", "status": "proposed"}]
            instruction = (f"核对「{group['label']}」与已显示组的相对位置。"
                           "图示将该组分开展示，箭头指向本 STEP 中的最终位置；"
                           "确认连接方式和操作先后后再修改为正式安装文字。")
            warnings.append("箭头只表示从分开展示位置回到 STEP 位置，不证明插入方向或可行运动路径。")
            if candidate:
                evidence.append({"kind": "candidate_contact", "candidate_id": candidate["id"],
                                 "status": "unverified", "part_ids": candidate["part_ids"]})
                warnings.append("邻近位置只是一处连接候选；接触也可能来自已装配、过盈或模型误差。")
            else:
                warnings.append("本轮已测位置未找到与之前步骤的连接候选；检测可能不完整，"
                                "不能据此排除真实连接，位置与先后需人工确认。")
        steps.append({"id": _stable_id("step-", [group_id]),
                      "title": ("待编排起点：" if not assembled else "待编排场景：") + group["label"],
                      "instruction": instruction, "assembled_groups": list(assembled),
                      "moving_groups": [group_id], "offsets": {group_id: offset},
                      "camera": list(style["camera"]), "up": list(style["up"]),
                      "arrows": arrows, "focus_part_ids": [],
                      "hidden_part_ids": [], "visibility_reason": "",
                      "evidence": evidence, "warnings": warnings, "reviewed": False,
                      "kind": "assembly", "draft_intent": "position_review"})
        assembled.append(group_id)
        remaining.remove(group_id)
    steps.append({"id": "step-overview", "title": "STEP 最终位置总览",
                  "instruction": "核对全部 STEP 实例的最终位置和前述步骤。确认后方可用于安装说明。",
                  "assembled_groups": list(assembled), "moving_groups": [], "offsets": {},
                  "camera": list(style["camera"]), "up": list(style["up"]), "arrows": [],
                  "focus_part_ids": [], "hidden_part_ids": [], "visibility_reason": "", "evidence": [],
                  "warnings": ["总览仅复现 STEP 几何，不证明步骤顺序、连接方式或装配可操作性。"],
                  "reviewed": False, "kind": "overview"})
    warnings = list(analysis.get("warnings", [])) + hierarchy_warnings
    warnings.extend(["只使用当前 STEP 中已有几何，未补造零件、网布、脚托、孔或紧固件。",
                     "分组、顺序、连接候选和箭头均为可修改提议；包装同事需要确认。",
                     "整册视角采用同一组坐标朝上约定；包围盒代理可建议局部视角，"
                     "不证明产品重力朝向，也不能保证所有连接细节可见。"])
    if disconnected_starts:
        warnings.append(f"已测连接候选图包含 {disconnected_starts + 1} 个独立分支；"
                        "检测可能不完整，不能排除跨分支实际连接，跨分支顺序需确认。")
    plan = {"schema_version": 1, "source": {"sha256": analysis["source_sha256"]},
            "product": {"title": title}, "status": "draft", "groups": groups,
            "style": style, "steps": steps, "connection_candidates": connections,
            "warnings": warnings,
            "workflow": {"version": 1, "basis": "geometry_scene_queue",
                         "source_sha256": analysis["source_sha256"],
                         "installation_sequence_verified": False,
                         "unit_roles_confirmed": False,
                         "order_basis": "几何候选图遍历；无可用候选时按体积排列待编排场景。"}}
    plan["analysis_summary"] = {
        "leaf_instances": len(parts),
        "non_geometric_occurrences": len(analysis.get("non_geometric_occurrences", [])),
        "non_solid_instances": sum(not p.get("solids", 0) for p in parts.values()),
        "multi_solid_instances": sum(p.get("solids", 0) > 1 for p in parts.values()),
        "candidate_pairs": analysis.get("candidate_pair_count", len(analysis.get("contacts", []))),
        "tested_pairs": analysis.get("tested_pair_count", len(analysis.get("contacts", []))),
        "untested_pairs": len(analysis.get("untested_candidates", [])),
        "failed_pairs": len(analysis.get("failed_pairs", [])),
    }
    if automatic_grouping:
        # Only fresh automatic proposals receive these readability suggestions.
        # Re-grouped scenes and exact-source profiles keep their editor-owned
        # camera/visibility/arrow recipes; rendering never calls this planner.
        try:
            from illustration_rules import suggest_initial_illustrations
        except ModuleNotFoundError as error:
            if error.name != "illustration_rules":
                raise
            # The planner is also loaded by absolute filename by lightweight
            # callers. Resolve its sibling without editing global sys.path.
            import importlib.util
            from pathlib import Path
            spec = importlib.util.spec_from_file_location(
                "illustration_rules", Path(__file__).with_name("illustration_rules.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            suggest_initial_illustrations = module.suggest_initial_illustrations
        plan = suggest_initial_illustrations(plan, analysis, fresh_automatic_draft=True)
    validate_plan(plan, analysis)
    return plan


def validate_plan(plan: dict, analysis: dict) -> None:
    """Validate explicit scene visibility without changing source or plan data."""
    parts = _analysis_parts(analysis)
    if not isinstance(plan, dict):
        _fail("plan must be an object")
    _finite_tree(plan, "plan")
    if plan.get("schema_version") != 1 or isinstance(plan.get("schema_version"), bool):
        _fail("unsupported plan schema_version")
    if not isinstance(plan.get("source"), dict) or plan["source"].get("sha256") != analysis["source_sha256"]:
        _fail("plan source does not match this STEP analysis; re-draft is required")
    if not isinstance(plan.get("product"), dict):
        _fail("plan.product must be an object")
    _identifier(plan["product"].get("title"), "product.title")
    if plan.get("status") not in ("draft", "confirmed"):
        _fail("plan.status must be draft or confirmed")
    workflow = plan.get("workflow")
    if workflow is not None:
        if not isinstance(workflow, dict):
            _fail("plan.workflow must be an object")
        if "source_sha256" in workflow and workflow["source_sha256"] != analysis["source_sha256"]:
            _fail("workflow belongs to another STEP source")
        if workflow.get("installation_sequence_verified", False) is not False:
            _fail("geometry/reference proposals cannot claim verified assembly motion")
    excluded = set(_ids(plan.get("excluded_part_ids", []), set(parts), "excluded_part_ids"))
    if excluded:
        _identifier(plan.get("exclusion_reason"), "exclusion_reason for explicitly hidden STEP instances")
    elif "exclusion_reason" in plan and not isinstance(plan["exclusion_reason"], str):
        _fail("exclusion_reason must be a string")
    sequence_mode = plan.get("sequence_mode", "cumulative")
    if sequence_mode not in ("cumulative", "configured"):
        _fail("sequence_mode must be cumulative or configured")
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        _fail("plan.groups must be a nonempty list")
    group_map, covered = {}, set()
    for group in groups:
        if not isinstance(group, dict):
            _fail("each group must be an object")
        group_id = _file_id(group.get("id"), "group.id")
        if group_id in group_map:
            _fail(f"duplicate group ID: {group_id}")
        part_ids = _ids(group.get("part_ids"), set(parts), "group.part_ids")
        if not part_ids or covered.intersection(part_ids):
            _fail("groups must contain distinct, nonempty STEP instance sets")
        if not set(part_ids) - excluded:
            _fail("each group must retain at least one visible STEP instance; "
                  "merge hidden provenance members into a visible group or revise explicit exclusions")
        covered.update(part_ids)
        _identifier(group.get("label"), "group.label")
        _identifier(group.get("basis"), "group.basis")
        if group.get("status") not in ("proposed", "confirmed"):
            _fail("group.status must be proposed or confirmed")
        if "role" in group and group["role"] not in ("unclassified", "preassembled", "installation", "auxiliary"):
            _fail("group.role must be unclassified, preassembled, installation or auxiliary")
        if "role_reason" in group and not isinstance(group["role_reason"], str):
            _fail("group.role_reason must be a string")
        group_map[group_id] = group
    if covered != set(parts):
        _fail("groups must cover every STEP occurrence; geometry cannot be silently dropped")
    style = plan.get("style")
    if not isinstance(style, dict):
        _fail("plan.style must be an object")
    _camera(style.get("camera"), style.get("up"), "style")
    width, height = (_number(style.get(key), f"style.{key}") for key in ("width", "height"))
    margin = _number(style.get("margin"), "style.margin")
    if (any(not isinstance(style.get(key), int) or isinstance(style.get(key), bool) for key in ("width", "height"))
            or not 256 <= width <= 4096 or not 256 <= height <= 4096
            or not 32 <= margin < min(width, height) / 2 - 10):
        _fail("style canvas or margin is out of bounds")
    if style.get("hlr") not in ("poly", "exact"):
        _fail("style.hlr must be poly or exact")
    if not 0 <= _number(style.get("explode_ratio"), "style.explode_ratio") <= 2:
        _fail("style.explode_ratio must be between 0 and 2")
    candidate_ids = set()
    candidates = plan.get("connection_candidates", [])
    if not isinstance(candidates, list):
        _fail("connection_candidates must be a list")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            _fail("each connection candidate must be an object")
        candidate_id = _identifier(candidate.get("id"), "candidate.id")
        if candidate_id in candidate_ids:
            _fail("duplicate connection candidate ID")
        candidate_ids.add(candidate_id)
        if candidate.get("status") != "unverified":
            _fail("geometric connection candidates must remain unverified")
        if len(_ids(candidate.get("group_ids"), set(group_map), "candidate.group_ids")) not in (1, 2):
            _fail("connection candidate requires one or two known groups")
        _ids(candidate.get("part_ids"), set(parts), "candidate.part_ids")
        if _number(candidate.get("distance_mm"), "candidate.distance_mm") < 0:
            _fail("candidate distance cannot be negative")
        raw_contacts = candidate.get("contacts")
        if not isinstance(raw_contacts, list) or not raw_contacts:
            _fail("candidate.contacts must be a nonempty list")
        for contact in raw_contacts:
            if not isinstance(contact, dict):
                _fail("candidate contact must be an object")
            if len(_ids(contact.get("part_ids"), set(parts), "contact.part_ids")) != 2:
                _fail("candidate contact must reference two distinct parts")
            _vector(contact.get("point_a"), "contact.point_a")
            _vector(contact.get("point_b"), "contact.point_b")
            if contact.get("status") != "unverified" or _number(contact.get("distance_mm"), "contact.distance_mm") < 0:
                _fail("candidate contact must remain unverified and have nonnegative distance")
    bounds = _bounds(list(parts.values()))
    span = max(b - a for a, b in zip(*bounds))
    if span <= 0:
        _fail("STEP bounding box must have positive extent")
    steps = plan.get("steps")
    if not isinstance(steps, list) or len(steps) < 2:
        _fail("steps require at least an initial step and a final overview")
    step_ids, output_names, completed = set(), set(), set()
    for number, step in enumerate(steps):
        if not isinstance(step, dict):
            _fail("each step must be an object")
        step_id = _file_id(step.get("id"), "step.id")
        if step_id in step_ids:
            _fail(f"duplicate step ID: {step_id}")
        step_ids.add(step_id)
        filenames = {step_id.lower(), (step_id + "_focus").lower()}
        if output_names.intersection(filenames):
            _fail("step IDs collide with another step or local-view filename")
        output_names.update(filenames)
        _identifier(step.get("title"), "step.title")
        _identifier(step.get("instruction"), "step.instruction")
        if "include_in_manual" in step and not isinstance(step["include_in_manual"], bool):
            _fail("step.include_in_manual must be a boolean")
        _camera(step.get("camera"), step.get("up"), step_id)
        assembled = set(_ids(step.get("assembled_groups"), set(group_map), "step.assembled_groups"))
        moving = set(_ids(step.get("moving_groups"), set(group_map), "step.moving_groups"))
        if assembled.intersection(moving):
            _fail("assembled and moving groups must be disjoint in each scene")
        if sequence_mode == "cumulative" and (assembled != completed or moving.intersection(completed)):
            _fail("steps must accumulate assembled groups coherently without repeated moving groups")
        final = number == len(steps) - 1
        if final:
            if moving or assembled != set(group_map):
                _fail("final overview must display every STEP group at its final location")
        elif sequence_mode == "cumulative" and not moving:
            _fail("each nonfinal cumulative step must introduce a moving group")
        elif sequence_mode == "configured" and not assembled and not moving:
            _fail("each configured scene must show at least one retained group")
        scene_parts = {p for group_id in assembled | moving for p in group_map[group_id]["part_ids"]}
        hidden = set(_ids(step.get("hidden_part_ids", []), set(parts), "step.hidden_part_ids"))
        if not hidden <= scene_parts:
            _fail("step.hidden_part_ids may reference only occurrences in this step's scene")
        if hidden.intersection(excluded):
            _fail("step.hidden_part_ids cannot repeat globally excluded occurrences")
        if hidden:
            _identifier(step.get("visibility_reason"), "step.visibility_reason for temporarily hidden STEP instances")
        elif "visibility_reason" in step and not isinstance(step["visibility_reason"], str):
            _fail("step.visibility_reason must be a string")
        if final and hidden:
            _fail("final overview must restore all temporarily hidden STEP instances")
        offsets = step.get("offsets")
        if not isinstance(offsets, dict) or not set(offsets) <= moving:
            _fail("offsets may reference only this step's moving groups")
        for group_id, offset in offsets.items():
            if _norm(_vector(offset, f"offset {group_id}")) > span * 3:
                _fail("exploded offset exceeds three model extents")
        visible_parts = scene_parts - excluded - hidden
        if not visible_parts:
            _fail("each scene must retain at least one visible STEP instance")
        if any(not set(group_map[group_id]["part_ids"]).intersection(visible_parts) for group_id in moving):
            _fail("each moving group must retain at least one visible STEP instance")
        _ids(step.get("focus_part_ids"), visible_parts, "step.focus_part_ids")
        arrows = step.get("arrows")
        if not isinstance(arrows, list):
            _fail("step.arrows must be a list")
        for arrow in arrows:
            if not isinstance(arrow, dict) or arrow.get("meaning") != "reposition" or arrow.get("status") != "proposed":
                _fail("arrows must describe proposed display repositioning")
            start, end = _vector(arrow.get("from"), "arrow.from"), _vector(arrow.get("to"), "arrow.to")
            if not moving or _norm([b - a for a, b in zip(start, end)]) > span * 3:
                _fail("arrow requires a moving group and a bounded display displacement")
            if any(point < lower - 3 * span or point > upper + 3 * span
                   for point, lower, upper in zip(start, *bounds)):
                _fail("arrow origin is out of model display bounds")
            moving_bounds = [_bounds([parts[p] for p in group_map[g]["part_ids"]]) for g in moving]
            tolerance = max(span * 1e-7, 1e-8)
            if not any(all(a - tolerance <= p <= b + tolerance for p, a, b in zip(end, *box))
                       for box in moving_bounds):
                _fail("arrow must end within a moving group's STEP final bounding box")
        _strings(step.get("warnings"), "step.warnings")
        evidence = step.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
            _fail("step.evidence must be a list of objects")
        for item in evidence:
            if "candidate_id" in item:
                candidate_id = _identifier(item["candidate_id"], "evidence.candidate_id")
                if candidate_id not in candidate_ids:
                    _fail("step evidence references an unknown connection candidate")
            if "part_ids" in item:
                _ids(item["part_ids"], set(parts), "evidence.part_ids")
            if "group_id" in item:
                group_id = _identifier(item["group_id"], "evidence.group_id")
                if group_id not in group_map:
                    _fail("step evidence references an unknown group")
        if not isinstance(step.get("reviewed"), bool):
            _fail("step.reviewed must be a boolean")
        if plan["status"] == "confirmed" and not step["reviewed"]:
            _fail("all steps must be reviewed before confirmation")
        completed.update(moving)
    _strings(plan.get("warnings", []), "plan.warnings")


def compare_revisions(old_analysis: dict, new_analysis: dict, old_plan: dict | None = None) -> dict:
    """Report possible matches; a changed STEP never inherits confirmations.

    Names and list indices do not participate. A geometry fingerprint is still
    only a matching clue: equal coarse fingerprints may describe distinct parts.
    V1 deliberately does not rewrite old steps or apply any proposed match.
    """
    old_parts, new_parts = _analysis_parts(old_analysis), _analysis_parts(new_analysis)
    if old_plan is not None:
        validate_plan(old_plan, old_analysis)
    changed = old_analysis["source_sha256"] != new_analysis["source_sha256"]

    def fingerprint(part):
        value = part.get("geometry_fingerprint")
        return json.dumps(value, sort_keys=True, separators=(",", ":")) if value else None

    def same_placement(a, b):
        return all(abs(x - y) <= 0.01 for key in ("bbox_min", "bbox_max")
                   for x, y in zip(a[key], b[key]))

    candidates, placement = {}, {}
    reverse = defaultdict(list)
    for old_id, old in sorted(old_parts.items()):
        geometry = [new_id for new_id, new in sorted(new_parts.items())
                    if fingerprint(old) is not None and fingerprint(old) == fingerprint(new)]
        positioned = [new_id for new_id in geometry if same_placement(old, new_parts[new_id])]
        candidates[old_id] = positioned or geometry
        placement[old_id] = bool(positioned)
        for new_id in candidates[old_id]:
            reverse[new_id].append(old_id)
    matches, ambiguous, unmatched_old = [], [], []
    for old_id, possible in candidates.items():
        if not possible:
            unmatched_old.append(old_id)
        elif len(possible) == 1 and len(reverse[possible[0]]) == 1:
            matches.append({"old_part_id": old_id, "new_part_id": possible[0],
                            "status": "possible", "same_placement": placement[old_id],
                            "basis": ("geometry_fingerprint_and_placement" if placement[old_id]
                                      else "geometry_fingerprint_only_placement_changed"),
                            "requires_review": True})
        else:
            ambiguous.append({"old_part_id": old_id, "new_part_ids": possible,
                              "status": "ambiguous", "requires_review": True,
                              "basis": "geometry_fingerprint_and_placement" if placement[old_id]
                                       else "geometry_fingerprint_only"})
    reviewed = [step["id"] for step in old_plan.get("steps", []) if step["reviewed"]] if old_plan else []
    return {"schema_version": 1, "old_source_sha256": old_analysis["source_sha256"],
            "new_source_sha256": new_analysis["source_sha256"], "source_changed": changed,
            "requires_redraft": changed, "confirmations_invalidated": changed,
            "invalidated_step_ids": reviewed if changed else [],
            "matches": matches, "ambiguous_matches": ambiguous,
            "unmatched_old": unmatched_old, "unmatched_new": sorted(set(new_parts) - set(reverse)),
            "automatically_applied": False,
            "warnings": (["STEP 来源已换版，旧步骤和视角仅供参考；所有确认失效，请基于新 STEP 重新起草。"] if changed else [])
                        + ["匹配只提供人工复核线索，未按零件名或索引自动沿用。",
                           "相同几何指纹和包围盒仍不能证明实例或连接语义相同。"]}
