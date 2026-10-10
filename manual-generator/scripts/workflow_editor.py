"""Source-bound packaging units and installation actions, independent of CAD tree.

All functions are pure: they neither read CAD files nor draw, translate or export
a document. A CAD assembly can be split into several installation units and one
action can refer to several units. These edits record a person's instructions;
they do not turn geometric contact clues into proven installation facts.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from draft_planner import (
    _analysis_parts, _bounds, _center, _common_prefix, _connections,
    _file_id, _ids, _path, _stable_id, _vector, validate_plan,
)


GROUP_ROLES = ("unclassified", "preassembled", "installation", "auxiliary")
ROLE_LABELS = {"unclassified": "待确认交付方式", "preassembled": "预装单元",
               "installation": "需要安装的单元", "auxiliary": "辅助参考几何"}
_EDIT_WARNING = "安装动作由包装人员编排；请核对交付状态、连接方式和实际可操作性。"
_REPOSITION_WARNING = "分离箭头只指向当前 STEP 最终位置，不证明安装方向或可行运动路径。"


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}不能为空。")
    if len(value) > 10000:
        raise ValueError(f"{name}过长。")
    return value.strip()


def _step(plan, step_id, *, allow_final=True):
    matches = [step for step in plan["steps"] if step["id"] == step_id]
    if not matches:
        raise ValueError("所选步骤不属于当前 STP 草案。")
    if not allow_final and matches[0] is plan["steps"][-1]:
        raise ValueError("最终总览必须保留；请选择一个安装动作。")
    return matches[0]


def _group(plan, group_id):
    matches = [group for group in plan["groups"] if group["id"] == group_id]
    if not matches:
        raise ValueError("所选安装单元不属于当前 STP 草案。")
    return matches[0]


def _diagram_arrows(plan, analysis, step):
    parts = {part["id"]: part for part in analysis["parts"]}
    groups = {group["id"]: group for group in plan["groups"]}
    result = []
    for group_id in step["moving_groups"]:
        offset = step["offsets"].get(group_id, [0, 0, 0])
        if any(abs(value) > 1e-12 for value in offset):
            center = _center(_bounds([parts[pid] for pid in groups[group_id]["part_ids"]]))
            result.append({"from": [c + d for c, d in zip(center, offset)], "to": center,
                           "meaning": "reposition", "status": "proposed"})
    return result


def _clean_scene(plan, step, analysis, *, new_arrows=False):
    """Retain user views and source masks that still belong to the edited scene."""
    groups = {group["id"]: group for group in plan["groups"]}
    moving = set(step["moving_groups"])
    step["assembled_groups"] = [g for g in step["assembled_groups"] if g not in moving]
    step["offsets"] = {g: step["offsets"].get(g, [0, 0, 0]) for g in step["moving_groups"]}
    scene_parts = {pid for gid in step["assembled_groups"] + step["moving_groups"]
                   for pid in groups[gid]["part_ids"]}
    excluded = set(plan.get("excluded_part_ids", []))
    step["hidden_part_ids"] = [pid for pid in step.get("hidden_part_ids", [])
                               if pid in scene_parts and pid not in excluded]
    visible = scene_parts - excluded - set(step["hidden_part_ids"])
    step["focus_part_ids"] = [pid for pid in step.get("focus_part_ids", []) if pid in visible]
    if not step["hidden_part_ids"]:
        step["visibility_reason"] = ""
    if new_arrows:
        step["arrows"] = _diagram_arrows(plan, analysis, step)


def _refresh_groups(plan, analysis, remapping):
    """Update genuine measured candidates and human references after a boundary edit."""
    plan["connection_candidates"] = _connections(analysis, plan["groups"])
    # Old source-bound suggestions depended on the former group partition.
    # Source points remain in analysis; stale suggestions must not name removed groups.
    plan.pop("assembly_review", None)
    for step in plan["steps"]:
        for key in ("assembled_groups", "moving_groups"):
            mapped = []
            for group_id in step[key]:
                for replacement in remapping.get(group_id, [group_id]):
                    if replacement not in mapped:
                        mapped.append(replacement)
            step[key] = mapped
        previous = step["offsets"]
        offsets = {}
        for old_id, vector in previous.items():
            for new_id in remapping.get(old_id, [old_id]):
                if new_id in offsets and offsets[new_id] != vector:
                    offsets[new_id] = [0, 0, 0]
                    message = "合并单元原来使用不同分离位置，已恢复为 STEP 位置；请重新选择爆炸距离。"
                    if message not in step["warnings"]:
                        step["warnings"].append(message)
                elif new_id not in offsets:
                    offsets[new_id] = deepcopy(vector)
        step["offsets"] = offsets
        evidence = []
        for item in step.get("evidence", []):
            if "candidate_id" in item:
                continue
            if "group_id" in item:
                for replacement in remapping.get(item["group_id"], [item["group_id"]]):
                    copied = deepcopy(item)
                    copied["group_id"] = replacement
                    copied["kind"] = "packaging_selected_group"
                    copied["part_ids"] = deepcopy(_group(plan, replacement)["part_ids"])
                    evidence.append(copied)
            else:
                evidence.append(deepcopy(item))
        step["evidence"] = evidence
        _clean_scene(plan, step, analysis, new_arrows=True)
    plan["steps"][-1]["assembled_groups"] = [g["id"] for g in plan["groups"]]
    plan["steps"][-1].update(moving_groups=[], offsets={}, arrows=[], hidden_part_ids=[],
                             visibility_reason="", kind="overview")


def _template_guard(plan, operation):
    """A fixed consumer template cannot silently retain an unrelated step layout."""
    if not plan.get("pdf_template"):
        return
    action = operation["action"]
    structural = action in ("add_step", "remove_step")
    if action == "reorder_steps":
        structural = operation.get("step_ids") != [step["id"] for step in plan["steps"]]
    if action == "update_step":
        current = _step(plan, operation.get("step_id"))
        structural = any(field in operation and operation[field] != current.get(field, True if field == "include_in_manual" else None)
                         for field in ("title", "instruction", "consumer_note", "include_in_manual"))
    if structural:
        raise ValueError("当前步骤已绑定原说明书模板。请先解除步骤区域绑定，再调整步骤数量、顺序或原文，随后重新校准原模板。")


def apply_workflow_edit(plan: dict, analysis: dict, operation: dict) -> dict:
    """Apply one explicit packaging edit and validate source coverage before/after.

    ``operation.action`` supports rename_group, set_group_role, split_group,
    merge_groups, add_step, reorder_steps, remove_step and update_step. Returns
    a new draft; inputs are never changed. Scene/order edits use configured mode,
    so several actions can work on the same installation unit. The final overview
    remains fixed in last position and restores temporarily hidden source parts.
    """
    validate_plan(plan, analysis)
    if not isinstance(operation, dict) or not isinstance(operation.get("action"), str):
        raise ValueError("请提供一个步骤编排操作。")
    operation = deepcopy(operation)
    operation["action"] = operation["action"].replace("-", "_")
    _template_guard(plan, operation)
    result = deepcopy(plan)
    action = operation["action"]
    if action == "rename_group":
        group = _group(result, operation.get("group_id"))
        group["label"] = _text(operation.get("label"), "安装单元名称")
        group["authored_by"] = "packaging"
    elif action == "set_group_role":
        group = _group(result, operation.get("group_id"))
        role = operation.get("role")
        if role not in GROUP_ROLES:
            raise ValueError("安装单元角色必须为待确认、预装、安装或辅助参考。")
        reason = operation.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError("交付状态说明必须为文字。")
        group.update(role=role, role_reason=reason.strip(), authored_by="packaging")
        # Auxiliary is a classification, never an implicit display exclusion.
    elif action == "split_group":
        group = _group(result, operation.get("group_id"))
        selected = _ids(operation.get("part_ids"), set(group["part_ids"]), "split_group.part_ids")
        if not selected or set(selected) == set(group["part_ids"]):
            raise ValueError("请选择原单元中的部分实例；拆分后两侧都必须保留几何。")
        label = _text(operation.get("label"), "新安装单元名称")
        new_id = _stable_id("group-", sorted(selected))
        if any(g["id"] == new_id for g in result["groups"]):
            raise ValueError("新安装单元 ID 已存在。")
        new_group = deepcopy(group)
        new_group.update(id=new_id, label=label, part_ids=sorted(selected), status="proposed",
                         basis="包装人员从当前 STEP 单元中显式拆分的实例集合。", authored_by="packaging")
        group["part_ids"] = [pid for pid in group["part_ids"] if pid not in set(selected)]
        group.update(status="proposed", basis="包装人员拆分后保留的当前 STEP 实例集合。",
                     authored_by="packaging")
        result["groups"].insert(result["groups"].index(group) + 1, new_group)
        _refresh_groups(result, analysis, {group["id"]: [group["id"], new_id]})
    elif action == "merge_groups":
        selected = _ids(operation.get("group_ids"), {g["id"] for g in result["groups"]}, "merge_groups.group_ids")
        if len(selected) < 2:
            raise ValueError("请至少选择两个安装单元。")
        selected_set = set(selected)
        members = sorted(pid for g in result["groups"] if g["id"] in selected_set for pid in g["part_ids"])
        merged_id = _stable_id("group-", members)
        roles = {_group(result, gid).get("role", "unclassified") for gid in selected}
        new_group = {"id": merged_id, "label": _text(operation.get("label"), "合并单元名称"),
                     "part_ids": members, "basis": "包装人员显式合并的当前 STEP 实例集合。",
                     "status": "proposed", "authored_by": "packaging",
                     "role": roles.pop() if len(roles) == 1 else "unclassified"}
        groups, inserted = [], False
        for group in result["groups"]:
            if group["id"] in selected_set:
                if not inserted:
                    groups.append(new_group)
                    inserted = True
            else:
                groups.append(group)
        result["groups"] = groups
        _refresh_groups(result, analysis, {gid: [merged_id] for gid in selected})
        for step in result["steps"][:-1]:
            if merged_id in step["assembled_groups"] + step["moving_groups"]:
                step["warnings"].append("此动作中的单元已合并，现显示整组几何；请核对原动作文字和是否应合并安装动作。")
    elif action == "add_step":
        groups = {g["id"] for g in result["groups"]}
        moving = _ids(operation.get("moving_groups", []), groups, "step.moving_groups")
        assembled = _ids(operation.get("assembled_groups", []), groups, "step.assembled_groups")
        if set(assembled).intersection(moving) or not assembled and not moving:
            raise ValueError("安装动作必须显示至少一个单元，固定单元和分离单元不能重复。")
        serial = 1
        while f"action-{serial:03d}" in {s["id"] for s in result["steps"]}:
            serial += 1
        step = {"id": f"action-{serial:03d}", "title": _text(operation.get("title"), "步骤标题"),
                "instruction": _text(operation.get("instruction"), "操作文字"),
                "assembled_groups": assembled, "moving_groups": moving,
                "offsets": deepcopy(operation.get("offsets", {gid: [0, 0, 0] for gid in moving})),
                "camera": deepcopy(result["style"]["camera"]), "up": deepcopy(result["style"]["up"]),
                "arrows": [], "focus_part_ids": [], "hidden_part_ids": [], "visibility_reason": "",
                "evidence": [], "warnings": [_EDIT_WARNING, _REPOSITION_WARNING],
                "kind": "assembly", "authored_by": "packaging", "reviewed": False}
        if not isinstance(step["offsets"], dict) or not set(step["offsets"]) <= set(moving):
            raise ValueError("分离位置只能引用本动作的分离单元。")
        step["offsets"] = {gid: _vector(step["offsets"].get(gid, [0, 0, 0]), "offset") for gid in moving}
        step["arrows"] = _diagram_arrows(result, analysis, step)
        after = operation.get("after_step_id")
        if after is None:
            position = len(result["steps"]) - 1
        else:
            _step(result, after, allow_final=False)
            position = next(i for i, s in enumerate(result["steps"]) if s["id"] == after) + 1
        result["steps"].insert(position, step)
    elif action == "reorder_steps":
        expected = [step["id"] for step in result["steps"]]
        order = _ids(operation.get("step_ids"), set(expected), "reorder_steps.step_ids")
        if set(order) != set(expected) or not order or order[-1] != expected[-1]:
            raise ValueError("重排需要包含所有步骤，最终总览必须保留在末尾。")
        by_id = {step["id"]: step for step in result["steps"]}
        result["steps"] = [by_id[sid] for sid in order]
    elif action == "remove_step":
        step = _step(result, operation.get("step_id"), allow_final=False)
        if len(result["steps"]) <= 2:
            raise ValueError("至少保留一个安装动作和最终总览。")
        others = [s for s in result["steps"][:-1] if s is not step]
        moving_elsewhere = {g for s in others for g in s["moving_groups"]}
        roles = {g["id"]: g.get("role", "unclassified") for g in result["groups"]}
        lost = [g for g in step["moving_groups"] if g not in moving_elsewhere
                and roles[g] not in ("preassembled", "auxiliary")]
        target_id = operation.get("reassign_to")
        if lost and not target_id:
            raise ValueError("删除会失去部分单元的安装动作。请选择接收动作，或先明确这些单元为预装/辅助参考。")
        if target_id:
            target = _step(result, target_id, allow_final=False)
            if target is step:
                raise ValueError("接收动作不能是要删除的动作。")
            for gid in lost:
                if gid not in target["moving_groups"]:
                    target["moving_groups"].append(gid)
                    target["offsets"][gid] = deepcopy(step["offsets"].get(gid, [0, 0, 0]))
            _clean_scene(result, target, analysis, new_arrows=True)
            target["warnings"].append("此动作接收了被删除步骤的安装单元；请核对操作文字和组间先后。")
        result["steps"].remove(step)
    elif action == "update_step":
        step = _step(result, operation.get("step_id"))
        final = step is result["steps"][-1]
        for field in ("title", "instruction"):
            if field in operation:
                step[field] = _text(operation[field], "步骤标题" if field == "title" else "操作文字")
        if "include_in_manual" in operation:
            if not isinstance(operation["include_in_manual"], bool):
                raise ValueError("是否进入手册必须为布尔值。")
            step["include_in_manual"] = operation["include_in_manual"]
        groups = {g["id"] for g in result["groups"]}
        for field in ("assembled_groups", "moving_groups"):
            if field in operation:
                step[field] = _ids(operation[field], groups, f"step.{field}")
        if set(step["assembled_groups"]).intersection(step["moving_groups"]):
            raise ValueError("本动作的固定单元和分离单元不能重复。")
        if "offsets" in operation:
            if not isinstance(operation["offsets"], dict) or not set(operation["offsets"]) <= set(step["moving_groups"]):
                raise ValueError("分离位置只能引用本动作的分离单元。")
            step["offsets"] = {g: _vector(v, "offset") for g, v in operation["offsets"].items()}
        for field in ("camera", "up"):
            if field in operation:
                step[field] = _vector(operation[field], field)
        if final and (step["moving_groups"] or set(step["assembled_groups"]) != groups):
            raise ValueError("最终总览必须展示所有当前 STEP 单元的最终位置。")
        _clean_scene(result, step, analysis, new_arrows=any(field in operation for field in
                                                         ("assembled_groups", "moving_groups", "offsets")))
        step["authored_by"] = "packaging"
    else:
        raise ValueError("未知步骤编排操作。")

    result["sequence_mode"] = "configured"
    result["status"] = "draft"
    result.pop("confirmation", None)
    for step in result["steps"]:
        step["reviewed"] = False
        step["kind"] = "overview" if step is result["steps"][-1] else "assembly"
    result.setdefault("workflow", {}).update(version=1, basis="packaging_edit",
                                            installation_sequence_verified=False,
                                            source_sha256=analysis["source_sha256"])
    if _EDIT_WARNING not in result["warnings"]:
        result["warnings"].append(_EDIT_WARNING)
    validate_plan(result, analysis)
    return result


def workflow_summary(plan: dict, analysis: dict) -> dict:
    """Small UI-facing diagnostics, including source-identity CAD split choices."""
    validate_plan(plan, analysis)
    parts = _analysis_parts(analysis)
    moving_counts = {g["id"]: 0 for g in plan["groups"]}
    for step in plan["steps"][:-1]:
        for gid in step["moving_groups"]:
            moving_counts[gid] += 1
    groups = []
    for group in plan["groups"]:
        members = [parts[pid] for pid in group["part_ids"]]
        paths = [_path(part.get("occurrence_path")) for part in members]
        common = _common_prefix([path for path in paths if path])
        buckets = {}
        for part, path in zip(members, paths):
            branch = path[:len(common) + 1] if len(path) > len(common) else path
            key = branch or (part["id"],)
            buckets.setdefault(key, []).append(part)
        splits = []
        if len(buckets) > 1:
            for branch, rows in sorted(buckets.items()):
                ids = sorted(row["id"] for row in rows)
                display_path = rows[0].get("path", [])
                label = str(display_path[len(branch) - 1]) if display_path and len(display_path) >= len(branch) else str(rows[0].get("name", "CAD 子分支"))
                splits.append({"id": _stable_id("split-", [analysis["source_sha256"], branch, ids]),
                               "label": label, "part_ids": ids, "geometry_count": len(ids),
                               "basis": "同一 CAD occurrence 子分支；不是已确认的包装安装边界。"})
        role = group.get("role", "unclassified")
        groups.append({"id": group["id"], "label": group["label"], "role": role,
                       "role_label": ROLE_LABELS.get(role, ROLE_LABELS["unclassified"]),
                       "role_reason": group.get("role_reason", ""), "geometry_count": len(members),
                       "action_count": moving_counts[group["id"]], "split_candidates": splits})
    possible = analysis.get("candidate_pair_count", len(analysis.get("contacts", [])))
    tested = analysis.get("tested_pair_count", len(analysis.get("contacts", [])))
    complete = analysis.get("completed_pair_count", tested)
    risks = []
    unclassified = sum(g["role"] == "unclassified" for g in groups)
    if unclassified:
        risks.append(f"{unclassified} 个安装单元尚未确认预装或需要安装；CAD 分支不能证明实际交付状态。")
    if possible > complete:
        risks.append(f"{possible} 对几何距离候选中 {complete} 对完成测量；连接检测不完整，不能据此确认安装顺序。")
    untouched = sum(step.get("authored_by") != "packaging" for step in plan["steps"][:-1])
    if untouched:
        risks.append(f"{untouched} 个场景仍是几何位置提议，尚未由包装人员改为实际安装动作。")
    return {"version": 1, "source_sha256": analysis["source_sha256"],
            "geometry_count": len(parts), "non_geometric_count": len(analysis.get("non_geometric_occurrences", [])),
            "candidate_pairs": possible, "tested_pairs": tested, "completed_pairs": complete,
            "timed_out_pairs": analysis.get("timed_out_pair_count", 0),
            "installation_sequence_verified": False, "packaging_confirmed": plan.get("status") == "confirmed",
            "unclassified_unit_count": unclassified, "proposed_action_count": untouched,
            "warnings": risks, "roles": [{"id": role, "label": ROLE_LABELS[role]} for role in GROUP_ROLES],
            "groups": groups, "steps": [{"id": step["id"], "kind": "overview" if index == len(plan["steps"]) - 1 else "assembly",
                                           "authored_by": step.get("authored_by", "geometry_proposal"),
                                           "moving_group_ids": step["moving_groups"],
                                           "stationary_group_ids": step["assembled_groups"],
                                           "reviewed": step["reviewed"]}
                                          for index, step in enumerate(plan["steps"])]}


def apply_reference_profile(current: dict, analysis: dict, profile: dict) -> dict:
    """Apply explicitly authored reference actions only to their exact STEP source.

    Profiles store occurrence IDs, not names or list positions. A reference PDF
    supplies operation wording; it cannot by itself identify a CAD fastener or
    establish a variant's packaging boundary. Every mapping remains proposed.
    The caller verifies the actual reference file hash before publishing/export.
    """
    validate_plan(current, analysis)
    if not isinstance(profile, dict) or profile.get("source_sha256") != analysis["source_sha256"]:
        raise ValueError("参考动作配置不属于当前 STEP 换版；不能按名称或索引沿用。")
    reference = profile.get("reference")
    if (not isinstance(reference, dict) or not isinstance(reference.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])):
        raise ValueError("参考动作配置必须绑定原说明书 SHA256。")
    parts = set(_analysis_parts(analysis))
    if not isinstance(profile.get("groups"), list) or not isinstance(profile.get("steps"), list):
        raise ValueError("参考动作配置必须包含实例分组和安装动作。")
    groups = deepcopy(profile["groups"])
    covered = set()
    for group in groups:
        members = _ids(group.get("part_ids"), parts, "reference_profile.group.part_ids")
        if not members or covered.intersection(members):
            raise ValueError("参考安装单元必须由互不重复的非空当前 STEP 实例集合组成。")
        covered.update(members)
        group.update(status="proposed", authored_by="reference_candidate")
        group.setdefault("basis", "原说明书部件清单与当前 STEP occurrence 实例集合的候选对应；需要人工复核。")
        group.setdefault("role", "unclassified")
    if covered != parts:
        raise ValueError("参考安装单元必须保留全部当前 STEP 几何，不能静默丢失实例。")
    steps = deepcopy(profile["steps"])
    for step in steps:
        step.setdefault("camera", deepcopy(profile["style"]["camera"]))
        step.setdefault("up", deepcopy(profile["style"]["up"]))
        for field in ("offsets",):
            step.setdefault(field, {})
        for field in ("arrows", "focus_part_ids", "hidden_part_ids", "evidence", "warnings"):
            step.setdefault(field, [])
        step.setdefault("visibility_reason", "")
        step.setdefault("include_in_manual", True)
        step.update(reviewed=False, authored_by="reference_candidate")
        step["warnings"].append("操作文字来自本模型的参考说明书；源实例对应、预装边界及版本适用性仍需包装人员复核。")
        step["evidence"].append({"kind": "reference_manual_text", "reference_sha256": reference["sha256"],
                                 "source_sha256": analysis["source_sha256"], "status": "mapping_proposed"})
    if not steps:
        raise ValueError("参考动作配置不能为空。")
    steps[-1].update(kind="overview", include_in_manual=False)
    result = {"schema_version": 1, "source": deepcopy(current["source"]),
              "product": {"title": _text(profile.get("title", current["product"]["title"]), "产品名称")},
              "status": "draft", "sequence_mode": "configured", "groups": groups,
              "steps": steps, "style": deepcopy(profile["style"]),
              "connection_candidates": _connections(analysis, groups),
              "analysis_summary": deepcopy(current.get("analysis_summary", {})),
              "warnings": list(profile.get("warnings", [])) + [
                  "参考说明书提供安装动作文字，当前 STEP 提供唯一几何；未补造螺丝、孔位、网布或附件。",
                  "此配置只对应明确绑定的 STEP 和参考 PDF，不证明自动通用识别已解决包装分组。"],
              "configuration_origin": {"kind": "reference_grounded_source_candidate", "reference": deepcopy(reference),
                                       "source_sha256": analysis["source_sha256"], "mapping_confirmed": False},
              "workflow": {"version": 1, "basis": "reference_manual_proposed_mapping",
                           "source_sha256": analysis["source_sha256"], "reference_sha256": reference["sha256"],
                           "installation_sequence_verified": False}}
    validate_plan(result, analysis)
    return result
