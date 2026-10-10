"""Reviewable revision impacts from existing source-bound inventories only.

This module never imports a new STEP, creates an image, rewrites a plan, copies
an old diagram, or establishes cross-revision identity. Equal fingerprints are
coarse clues. A changed source invalidates every old diagram and confirmation,
including those for which a unique geometric candidate was found.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from draft_planner import compare_revisions as _candidate_comparison


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _analysis(value: dict | Path) -> dict:
    if isinstance(value, dict):
        return value
    inventory = Path(value).resolve()
    from model_analysis import _load_inventory
    rows, complete = _load_inventory(inventory)
    analysis = json.loads((inventory / "analysis.json").read_text(encoding="utf-8"))
    if analysis.get("source_sha256") != complete["source_sha256"] or analysis.get("parts") != rows:
        raise ValueError("Revision analysis does not match the completed inventory; verify the saved source version")
    return analysis


def _part_snapshot(part: dict) -> dict:
    # Names and indices help the person find a row; neither affects matching.
    fields = ("id", "index", "name", "occurrence_path", "bbox_min", "bbox_max",
              "bbox_size", "world_transform", "volume_mm3", "area_mm2", "solids",
              "shells", "faces", "edges", "geometry_fingerprint")
    return {key: deepcopy(part[key]) for key in fields if key in part}


def _difference(old: dict, new: dict) -> list[dict]:
    """Describe observed metadata differences without proving part identity."""
    differences = []
    for field in ("bbox_min", "bbox_max"):
        delta = max(abs(a-b) for a, b in zip(old[field], new[field]))
        if delta > 0.01:
            differences.append({"field": field, "old": deepcopy(old[field]),
                                "new": deepcopy(new[field]), "max_absolute_delta_mm": delta})
    if "world_transform" in old and "world_transform" in new:
        a, b = old["world_transform"], new["world_transform"]
        valid = (isinstance(a, list) and isinstance(b, list) and len(a) == len(b) == 4
                 and all(isinstance(row, list) and len(row) == 4
                         and all(isinstance(value, (float, int)) and not isinstance(value, bool)
                                 and math.isfinite(value) for value in row) for row in a+b))
        if valid:
            rotation = max(abs(a[i][j]-b[i][j]) for i in range(3) for j in range(3))
            translation = max(abs(a[i][3]-b[i][3]) for i in range(3))
            if rotation > 1e-8 or translation > 0.01:
                differences.append({"field": "world_transform", "old": deepcopy(a), "new": deepcopy(b),
                                    "max_rotation_coefficient_delta": rotation,
                                    "max_translation_delta_mm": translation})
    for field in ("volume_mm3", "area_mm2"):
        if field in old and field in new:
            a, b = old[field], new[field]
            if (isinstance(a, (int, float)) and not isinstance(a, bool)
                    and isinstance(b, (int, float)) and not isinstance(b, bool)
                    and math.isfinite(a) and math.isfinite(b)):
                delta = abs(a-b)
                if delta > max(0.01, max(abs(a), abs(b)) * 1e-6):
                    differences.append({"field": field, "old": a, "new": b, "absolute_delta": delta})
    for field in ("solids", "shells", "faces", "edges"):
        if field in old and field in new and old[field] != new[field]:
            differences.append({"field": field, "old": old[field], "new": new[field]})
    return differences


def _check_same_source(old: dict[str, dict], new: dict[str, dict]) -> None:
    if set(old) != set(new):
        raise ValueError("Same source SHA256 has conflicting occurrence identities; verify the saved inventories")
    for identity, part in old.items():
        other = new[identity]
        if (_difference(part, other)
                or part.get("geometry_fingerprint") != other.get("geometry_fingerprint")):
            raise ValueError("Same source SHA256 has conflicting geometry evidence; verify the saved inventories")


def _objects(old_parts: dict, new_parts: dict, report: dict) -> list[dict]:
    matches = {item["old_part_id"]: item for item in report["matches"]}
    ambiguous = {item["old_part_id"]: item for item in report["ambiguous_matches"]}
    impacts = []
    for identity, old in sorted(old_parts.items()):
        match = matches.get(identity)
        alternatives = ambiguous.get(identity)
        if not report["source_changed"]:
            possible = [identity]
            changes = []
            status, reason = "unchanged_candidate", "同一源摘要与实例身份；未发生 STEP 换版。"
            basis = "same_source_occurrence"
            unique = True
        elif match:
            possible = [match["new_part_id"]]
            changes = _difference(old, new_parts[possible[0]])
            status = "changed" if changes else "unchanged_candidate"
            reason = ("唯一粗几何候选的位置或属性发生可见变化，仍须核对是否为同一对象。" if changes else
                      "找到唯一粗几何及位置候选，仍不能证明跨版本实例或连接语义相同。")
            basis, unique = match["basis"], True
        elif alternatives:
            possible = list(alternatives["new_part_ids"])
            changes = []
            status, reason = "ambiguous", "重复几何或多个旧实例竞争同一新候选，不能确定对应关系。"
            basis, unique = alternatives["basis"], False
        else:
            possible, changes = [], []
            status, unique = "missing", False
            basis = "no_equal_geometry_fingerprint_candidate"
            reason = ("没有可用几何指纹，无法检索对应候选；不能据此判定零件被删除。" if not old.get("geometry_fingerprint") else
                      "未找到同指纹候选；可能为修改、移除或指纹变化，不能据此判定零件被删除。")
        impacts.append({
            "old_part_id": identity, "status": status, "reason": reason,
            "old_object": _part_snapshot(old), "new_part_ids": possible,
            "new_candidates": [_part_snapshot(new_parts[part_id]) for part_id in possible],
            "basis": basis, "observed_differences": changes,
            "mutually_unique_candidate": unique, "requires_review": report["source_changed"],
            "correspondence_verified": False, "automatically_applied": False,
        })
    return impacts


def _steps(plan: dict | None, objects: list[dict], changed: bool) -> tuple[list[dict], list[dict], list[dict]]:
    if plan is None:
        return [], [], []
    groups = {group["id"]: set(group["part_ids"]) for group in plan["groups"]}
    by_part = {part["old_part_id"]: part for part in objects}
    candidates = {item["id"]: item for item in plan.get("connection_candidates", [])}
    excluded = set(plan.get("excluded_part_ids", []))
    impacts, updates, views = [], [], []
    for step in plan["steps"]:
        scene = {part_id for group_id in step["assembled_groups"] + step["moving_groups"]
                 for part_id in groups[group_id]}
        hidden = set(step.get("hidden_part_ids", []))
        visible = scene - hidden - excluded
        evidence = set()
        for item in step.get("evidence", []):
            evidence.update(item.get("part_ids", []))
            if item.get("group_id") in groups:
                evidence.update(groups[item["group_id"]])
            if item.get("candidate_id") in candidates:
                evidence.update(candidates[item["candidate_id"]].get("part_ids", []))
        focus = set(step.get("focus_part_ids", []))
        referenced = scene | evidence | focus
        categories = defaultdict(list)
        for part_id in sorted(referenced):
            categories[by_part[part_id]["status"]].append(part_id)
        impact = {
            "step_id": step["id"], "title": step["title"],
            "old_reviewed": step.get("reviewed", False),
            "referenced_old_part_ids": sorted(referenced),
            "visible_old_part_ids": sorted(visible), "focus_old_part_ids": sorted(focus),
            "hidden_old_part_ids": sorted(hidden), "excluded_old_part_ids": sorted(scene & excluded),
            "objects_by_status": {status: categories[status] for status in
                                  ("changed", "ambiguous", "missing", "unchanged_candidate")},
            "visible_objects_by_status": {status: [part_id for part_id in categories[status] if part_id in visible]
                                          for status in ("changed", "ambiguous", "missing", "unchanged_candidate")},
            "needs_diagram_update": changed, "needs_new_review": changed,
            "confirmation_invalidated": changed,
            "reason_codes": (["source_revision_changed", "old_diagram_source_binding_invalid"] if changed else []),
            "new_candidate_ids_for_review": sorted({new_id for part_id in referenced
                                                    for new_id in by_part[part_id]["new_part_ids"]}),
            "mapping_applied": False,
        }
        impacts.append(impact)
        assets = [{"asset_id": step["id"], "kind": "main"}]
        if focus:
            assets.append({"asset_id": step["id"] + "_focus", "kind": "focus"})
        if changed:
            updates.append({"step_id": step["id"], "assets": assets,
                            "action": "review_new_source_then_render",
                            "reason": "源摘要变化；即使几何候选看似未变，旧图也不能作为新源图。",
                            "old_asset_reuse_allowed": False})
        views.append({"old_step_id": step["id"], "camera": deepcopy(step["camera"]),
                      "up": deepcopy(step["up"]), "status": "suggestion",
                      "requires_review": changed, "automatically_applied": False,
                      "meaning": "可人工尝试保留视角；未迁移零件引用、箭头位置或安装含义。",
                      "must_rebind_before_use": ["part_ids", "offsets", "arrows", "connection_candidates",
                                                 "focus_part_ids", "hidden_part_ids"] if changed else []})
    return impacts, updates, views


def compare_inventories(old: dict | Path, new: dict | Path, old_plan: dict | None = None) -> dict:
    """Return an additive, reviewable report; never apply matching candidates.

    Dictionary inputs are caller-validated saved analyses. Path inputs validate
    the existing BREP inventory hashes and analysis snapshot without importing
    any model. A ``missing`` old object means no same-fingerprint candidate, not
    proven removal. ``changed`` means observed differences in a *candidate*, not
    proven identity. All old steps/confirmations fail on a changed source.
    """
    old_analysis, new_analysis = _analysis(old), _analysis(new)
    report = _candidate_comparison(old_analysis, new_analysis, old_plan)
    old_parts = {part["id"]: part for part in old_analysis["parts"]}
    new_parts = {part["id"]: part for part in new_analysis["parts"]}
    if not report["source_changed"]:
        _check_same_source(old_parts, new_parts)
        report.update(matches=[{"old_part_id": identity, "new_part_id": identity,
                                "status": "possible", "same_placement": True,
                                "basis": "same_source_occurrence", "requires_review": False}
                               for identity in sorted(old_parts)],
                      ambiguous_matches=[], unmatched_old=[], unmatched_new=[])
    objects = _objects(old_parts, new_parts, report)
    step_impacts, diagram_updates, views = _steps(old_plan, objects, report["source_changed"])
    step_ids = [step["id"] for step in old_plan["steps"]] if old_plan else []
    report.update({
        "report_version": 2, "old_analysis_sha256": _digest(old_analysis),
        "new_analysis_sha256": _digest(new_analysis),
        "matching_inputs": (["geometry_fingerprint", "bbox_min", "bbox_max"] if report["source_changed"]
                            else ["source_sha256", "occurrence_id"]),
        "comparison_tolerances": {"placement_mm": 0.01, "transform_rotation_coefficient": 1e-8,
                                  "volume_area_absolute": 0.01, "volume_area_relative": 1e-6},
        "names_used_for_matching": False, "indices_used_for_matching": False,
        "geometry_comparison_scope": "saved inventory metadata only; no new CAD import, distance solve or image generation",
        "object_impacts": objects, "step_impacts": step_impacts,
        "diagram_updates": diagram_updates, "view_configuration_suggestions": views,
        "invalidated_step_ids": step_ids if report["source_changed"] else [],
        "invalidated_confirmation_step_ids": step_ids if report["source_changed"] else [],
        "previously_reviewed_step_ids": [step["id"] for step in old_plan["steps"] if step.get("reviewed")] if old_plan else [],
        "old_diagrams_may_be_used_as_new_source": False,
        "migration_applied": False,
        "requires_old_plan_to_assess_steps": old_plan is None,
        "old_plan_sha256": _digest(old_plan) if old_plan is not None else None,
    })
    counts = {status: sum(part["status"] == status for part in objects) for status in
              ("changed", "ambiguous", "missing", "unchanged_candidate")}
    report["summary"] = {"old_objects": len(old_parts), "new_objects": len(new_parts),
                         "old_objects_by_status": counts, "old_steps": len(step_ids),
                         "diagrams_requiring_update": len(diagram_updates),
                         "unassigned_new_objects_without_candidate": len(report["unmatched_new"])}
    report["unmatched_new_meaning"] = "新实例未进入任何旧实例的当前几何候选集；不自动判定新增，也不分配到旧步骤。"
    if old_plan and old_plan.get("cover_scene") is not None:
        report["parts_overview_update"] = {"needs_update": report["source_changed"],
                                           "old_asset_reuse_allowed": False,
                                           "reason": "总览图也绑定旧源，需按新源实例重新核对。"}
    if old_plan and isinstance(old_plan.get("pdf_template"), dict):
        report["fixed_template_binding"] = {
            "old_template_sha256": old_plan["pdf_template"].get("sha256"),
            "requires_new_source_binding_review": report["source_changed"],
            "automatically_transferred": False,
            "meaning": "原模板图槽、步骤含义和文字与新型号/版本的对应需重新确认；不复制旧产品文案到新源。",
        }
    return report


def compare_projects(old_project: Path, new_project: Path) -> dict:
    """Read existing project snapshots and return a report without file writes."""
    old_project, new_project = Path(old_project).resolve(), Path(new_project).resolve()
    old_plan = json.loads((old_project / "steps.json").read_text(encoding="utf-8"))
    return compare_inventories(old_project / "inventory", new_project / "inventory", old_plan)


def compare_revisions(old_analysis: dict, new_analysis: dict, old_plan: dict | None = None) -> dict:
    """Compatibility spelling for callers that already load project snapshots."""
    return compare_inventories(old_analysis, new_analysis, old_plan)
