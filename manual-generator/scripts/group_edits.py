"""Explicit packaging group edits; callers archive and publish the returned draft."""
from __future__ import annotations

from copy import deepcopy

from draft_planner import build_plan, validate_plan, _stable_id

_MERGE_BASIS = "由包装选中当前STEP实例合并，边界待核对"


def merge_groups(plan: dict, analysis: dict, group_ids: list[str], label: str) -> dict:
    """Merge selected current-source instances and propose fresh steps.

    This pure function does not write files, apply name-based matches or reuse
    confirmations. The calling review UI must explain that a fresh proposal
    replaces step edits, and archive the previous plan/output before publishing.
    """
    validate_plan(plan, analysis)
    if (not isinstance(group_ids, list) or len(group_ids) < 2
            or any(not isinstance(group_id, str) for group_id in group_ids)
            or len(set(group_ids)) != len(group_ids)):
        raise ValueError("Select at least two distinct current group IDs.")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("The merged group needs a nonempty display label.")
    selected = set(group_ids)
    known = {group["id"] for group in plan["groups"]}
    if not selected <= known:
        raise ValueError("Selected group IDs do not belong to the current STEP draft.")
    members = sorted(part_id for group in plan["groups"] if group["id"] in selected
                     for part_id in group["part_ids"])
    merged = {"id": _stable_id("group-", members), "label": label.strip(),
              "part_ids": members, "basis": _MERGE_BASIS,
              "status": "proposed"}
    groups, inserted = [], False
    for original in plan["groups"]:
        if original["id"] in selected:
            if not inserted:
                groups.append(merged)
                inserted = True
        else:
            group = deepcopy(original)
            group["status"] = "proposed"
            groups.append(group)
    result = build_plan(analysis, title=plan["product"]["title"], groups=groups)
    # Old overview group references become invalid after a merge. Its existing
    # image stays in history; omit it until a current overview is configured.
    # Regrouping re-proposes step wording, while product/reference text remains
    # bound to the same source and available for packaging review.
    if "manual_document" in plan:
        result["manual_document"] = deepcopy(plan["manual_document"])
        # A fresh proposal cannot still claim the old reference's step count
        # or preserved order. Keep product facts, revise only this narrative.
        result["manual_document"]["introduction"] = [
            f"按当前分组重新提出 {len(result['steps'])} 个安装场景。CAD主图来自当前 STP；步骤与操作文字待包装同事核对。"
        ]
        result["manual_document"]["review_introduction"] = [
            "本册安装顺序按当前分组与几何候选重新提出。请核对分组边界、操作先后和参考文字的适用性。"
        ]
    # Re-proposal uses current measured candidates, with one uniform user style.
    # The default planner offset is dimension-relative; scale it to the retained
    # ratio rather than carrying offsets from old groups with different bounds.
    scale = plan["style"]["explode_ratio"] / result["style"]["explode_ratio"]
    result["style"] = deepcopy(plan["style"])
    packaging_groups = {g["id"] for g in groups if g["basis"] == _MERGE_BASIS}
    for step in result["steps"]:
        step["camera"] = deepcopy(result["style"]["camera"])
        step["up"] = deepcopy(result["style"]["up"])
        step["offsets"] = {group_id: [value * scale for value in offset]
                           for group_id, offset in step["offsets"].items()}
        for arrow in step["arrows"]:
            arrow["from"] = [end + (start - end) * scale
                             for start, end in zip(arrow["from"], arrow["to"])]
        for evidence in step["evidence"]:
            if evidence.get("group_id") in packaging_groups:
                evidence["kind"] = "packaging_selected_group"
    result["warnings"] = [
        "统一视角沿用当前配置，只调整图示，不证明产品朝向或连接细节可见性。"
        if warning.startswith("统一视角依据包围盒选择") else warning
        for warning in result["warnings"]
    ]
    if "analysis_summary" in plan:
        result["analysis_summary"] = deepcopy(plan["analysis_summary"])
    for field in ("excluded_part_ids", "exclusion_reason"):
        if field in plan:
            result[field] = deepcopy(plan[field])
    # Merging explicitly requests a fresh proposal, rather than inheriting
    # manually configured scene visibility or repeat moves from the old draft.
    result["sequence_mode"] = "cumulative"
    validate_plan(result, analysis)
    return result
