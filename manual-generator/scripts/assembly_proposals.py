"""Optional source-bound assembly/batch review clues, never automatic merges.

CAD occurrence branches frequently divide one delivered assembly, or combine
several installation operations. Measured proximity and coarse fingerprints
can reduce review work but cannot establish packaging, joints or safe order.
No names, geometry editing, CAD imports, rendering or file writes are used.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math

from draft_planner import _analysis_parts


def _parent(part):
    value = part.get("occurrence_path")
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return tuple(str(token) for token in value[:-1])
    if isinstance(value, str) and "/" in value:
        return tuple(value.split("/")[:-1])
    return ()


def _common_parent(rows):
    paths = [_parent(part) for part in rows]
    if not paths or not all(paths):
        return ()
    prefix = paths[0]
    for path in paths[1:]:
        length = 0
        for first, second in zip(prefix, path):
            if first != second:
                break
            length += 1
        prefix = prefix[:length]
    return prefix


def _inside(point, row, tolerance):
    return all(lower - tolerance <= value <= upper + tolerance
               for value, lower, upper in zip(point, row["bbox_min"], row["bbox_max"]))


def _id(source, kind, groups, evidence):
    value = json.dumps([source, kind, sorted(groups), evidence], sort_keys=True,
                       ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "assembly-proposal-" + hashlib.sha256(value).hexdigest()[:20]


def _proposal(source, kind, group_ids, member_ids, evidence, explanation):
    return {
        "id": _id(source, kind, group_ids, evidence), "kind": kind,
        "source_sha256": source, "group_ids": sorted(group_ids), "part_ids": sorted(member_ids),
        "status": "proposed", "requires_review": True, "automatically_applied": False,
        "packaging_assembly_verified": False, "assembly_order_verified": False,
        "may_change_step_count": True, "basis": explanation, "evidence": evidence,
        "warnings": ["几何和层级仅提供复核线索，不能证明这些组作为同一安装总成交付。",
                     "选用批次或合并建议后仍须核对连接、实际操作和先后顺序。",
                     "已有固定模板的步骤格绑定需要重新核对；步骤数或含义变化时须重新校准。"],
    }


def _components(graph):
    remaining = set(graph)
    while remaining:
        root = min(remaining)
        pending, component = [root], set()
        while pending:
            item = pending.pop()
            if item in component:
                continue
            component.add(item)
            pending.extend(sorted(set(graph[item]) - component, reverse=True))
        remaining.difference_update(component)
        yield sorted(component)


def propose_assembly_candidates(analysis: dict, groups: list[dict], *,
                                max_contact_mm: float = 1.0, max_cluster_groups: int = 6) -> dict:
    """Return optional merge / batch suggestions while retaining every group.

    Pair suggestions require measured, in-bounds, solid-to-solid proximity;
    surface-only or known inner-distance solutions are weaker clues and are
    reported but never used for merge proposals. A larger cluster additionally
    needs a shared non-root occurrence parent and a dense measured contact
    graph. Repeated geometry creates a batch-review suggestion, not a merge or
    bill of materials. Fingerprints remain coarse clues requiring review.
    """
    parts = _analysis_parts(analysis)
    if (isinstance(max_contact_mm, bool) or not isinstance(max_contact_mm, (float, int))
            or not math.isfinite(max_contact_mm) or not 0 <= max_contact_mm <= 10):
        raise ValueError("max_contact_mm must be a finite distance from 0 to 10 mm")
    if isinstance(max_cluster_groups, bool) or not isinstance(max_cluster_groups, int) or not 2 <= max_cluster_groups <= 32:
        raise ValueError("max_cluster_groups must be an integer from 2 to 32")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Assembly suggestions need the retained CAD candidate groups")
    by_group, part_group = {}, {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not group["id"] or group["id"] in by_group:
            raise ValueError("Assembly suggestion groups need distinct IDs")
        members = group.get("part_ids")
        if not isinstance(members, list) or not members or len(set(members)) != len(members):
            raise ValueError("Assembly suggestion groups need nonempty distinct instance sets")
        for pid in members:
            if pid not in parts or pid in part_group:
                raise ValueError("Assembly suggestion groups must preserve unique current STEP instances")
            part_group[pid] = group["id"]
        by_group[group["id"]] = group
    if set(part_group) != set(parts):
        raise ValueError("Assembly suggestion groups must cover every current STEP instance")
    source = analysis["source_sha256"]
    span = max(max(part["bbox_max"][axis] for part in parts.values()) -
               min(part["bbox_min"][axis] for part in parts.values()) for axis in range(3))
    tolerance = max(span * 1e-7, 1e-8)
    pairs = defaultdict(list)
    skipped = Counter()
    for contact in analysis.get("contacts", []):
        a, b = contact["part_ids"]
        group_pair = tuple(sorted({part_group[a], part_group[b]}))
        if len(group_pair) != 2:
            skipped["within_existing_group"] += 1
            continue
        if contact["distance_mm"] > max_contact_mm:
            skipped["beyond_distance_threshold"] += 1
            continue
        if not _inside(contact["point_a"], parts[a], tolerance) or not _inside(contact["point_b"], parts[b], tolerance):
            skipped["out_of_source_bounds"] += 1
            continue
        if not parts[a].get("solids", 0) or not parts[b].get("solids", 0):
            skipped["non_solid_contact"] += 1
            continue
        if contact.get("inner_solution") is True:
            skipped["inner_solution"] += 1
            continue
        # Canonicalize measured endpoints so operand order cannot change IDs.
        members = sorted((a, b))
        points = {a: list(contact["point_a"]), b: list(contact["point_b"])}
        pairs[group_pair].append({"part_ids": members, "points": [points[pid] for pid in members],
                                  "distance_mm": contact["distance_mm"], "status": "unverified",
                                  "method": contact.get("method", "measured geometric distance")})
    graph = {gid: {} for gid in by_group}
    suggestions = []
    for group_pair, contacts in sorted(pairs.items()):
        contacts.sort(key=lambda contact: (tuple(contact["part_ids"]), contact["distance_mm"], tuple(map(tuple, contact["points"]))))
        a, b = group_pair
        graph[a][b] = graph[b][a] = contacts
        members = [pid for gid in group_pair for pid in by_group[gid]["part_ids"]]
        parent = _common_parent([parts[pid] for pid in members])
        evidence = {"measured_contacts": contacts, "common_occurrence_parent": list(parent),
                    "non_root_parent_shared": len(parent) >= 2, "distance_limit_mm": max_contact_mm}
        proposal = _proposal(source, "measured_contact_pair", group_pair, members, evidence,
                             "两组间存在已测实体近邻点；可复核是否属于同一安装总成，未自动合并。")
        proposal["action"] = "review_optional_pair_merge"
        suggestions.append(proposal)
    suppressed_clusters = 0
    for component in _components(graph):
        if len(component) < 3:
            continue
        members = [pid for gid in component for pid in by_group[gid]["part_ids"]]
        parent = _common_parent([parts[pid] for pid in members])
        edge_count = sum(len(graph[gid]) for gid in component) // 2
        density = 2 * edge_count / (len(component) * (len(component) - 1))
        if len(component) > max_cluster_groups or len(parent) < 2 or density < 0.6:
            suppressed_clusters += 1
            continue
        evidence = {"common_occurrence_parent": list(parent), "measured_edge_count": edge_count,
                    "measured_graph_density": density, "group_pairs": [list(pair) for pair in sorted(pairs) if set(pair) <= set(component)]}
        proposal = _proposal(source, "hierarchy_contact_cluster", component, members, evidence,
                             "共同非根层 occurrence 父级与较密集的已测实体近邻共同支持候选聚类；总成边界仍需确认。")
        proposal["action"] = "review_optional_cluster_merge"
        suggestions.append(proposal)
    repeats = defaultdict(list)
    for gid, group in sorted(by_group.items()):
        rows = [parts[pid] for pid in group["part_ids"]]
        fingerprints = [row.get("geometry_fingerprint") for row in rows]
        if not all(isinstance(value, str) and value for value in fingerprints):
            continue
        signature = tuple(sorted((row["geometry_fingerprint"], row.get("solids", 0), row.get("faces", 0)) for row in rows))
        repeats[signature].append(gid)
    for signature, repeated_groups in sorted(repeats.items()):
        if len(repeated_groups) < 2:
            continue
        members = [pid for gid in repeated_groups for pid in by_group[gid]["part_ids"]]
        evidence = {"geometry_fingerprint_multiset": [list(value) for value in signature],
                    "instances_per_group": len(signature), "group_count": len(repeated_groups),
                    "placement_equivalence_verified": False, "fingerprint_is_coarse": True,
                    "common_occurrence_parent": list(_common_parent([parts[pid] for pid in members]))}
        proposal = _proposal(source, "repeated_geometry_batch", repeated_groups, members, evidence,
                             "多组具有相同粗几何指纹集合；可核对是否适合在同一步批量展示，未认定为紧固件或相同安装动作。")
        proposal["action"] = "review_optional_batch_without_default_merge"
        proposal["warnings"].append("同指纹可能对应不同姿态、镜像、位置或连接方式；批装数量和动作须人工核对。")
        suggestions.append(proposal)
    suggestions.sort(key=lambda proposal: (proposal["kind"], tuple(proposal["group_ids"]), proposal["id"]))
    return {
        "schema_version": 1, "source_sha256": source, "status": "proposed", "automatically_applied": False,
        "original_groups": deepcopy(groups), "suggestions": suggestions,
        "diagnostics": {"group_count": len(groups), "leaf_instance_count": len(parts),
                        "group_size_histogram": {str(size): count for size, count in sorted(Counter(len(group["part_ids"]) for group in groups).items())},
                        "measured_contacts": len(analysis.get("contacts", [])), "usable_solid_group_pairs": len(pairs),
                        "skipped_contacts": dict(sorted(skipped.items())), "suppressed_contact_clusters": suppressed_clusters,
                        "candidate_pairs": analysis.get("candidate_pair_count", len(analysis.get("contacts", []))),
                        "tested_pairs": analysis.get("tested_pair_count", len(analysis.get("contacts", []))),
                        "untested_pairs": len(analysis.get("untested_candidates", [])), "failed_pairs": len(analysis.get("failed_pairs", []))},
        "warnings": ["CAD 根层分支不是包装安装总成，原建议组全部保留供选择。",
                     "邻近测量可能不完整；没有已测边不能证明不存在连接。",
                     "这些仅是可编辑复核建议，未自动合并、隐藏几何或确认安装顺序。"],
    }
