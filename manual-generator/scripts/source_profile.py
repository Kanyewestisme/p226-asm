"""Apply explicitly authored, exact-source illustration profiles as data."""
from __future__ import annotations

from copy import deepcopy
from draft_planner import validate_plan


def apply_source_profile(current: dict, analysis: dict, profile: dict) -> dict:
    validate_plan(current, analysis)
    if profile.get("source_sha256") != analysis["source_sha256"]:
        raise ValueError("Profile belongs to another STEP revision; indices cannot be reused.")
    parts = {row["index"]: row for row in analysis["parts"]}
    groups = []
    for definition in profile["groups"]:
        indices = definition["indices"]
        if any(isinstance(index, bool) or not isinstance(index, int) or index not in parts for index in indices):
            raise ValueError("Profile references an unknown source-bound index.")
        groups.append({
            "id": definition["id"], "label": definition["label"],
            "part_ids": [parts[index]["id"] for index in indices],
            "basis": "沿用明确匹配当前源摘要的既有人工配置；未从名称推断。",
            "status": "proposed",
        })
    excluded_indices = profile.get("excluded_indices", [])
    if any(isinstance(index, bool) or not isinstance(index, int) or index not in parts for index in excluded_indices):
        raise ValueError("Profile exclusion references an unknown source-bound index.")
    excluded = [parts[index]["id"] for index in excluded_indices]
    steps = []
    for definition in profile["steps"]:
        step = deepcopy(definition)
        step.setdefault("offsets", {})
        step.setdefault("arrows", [])
        step.setdefault("focus_part_ids", [])
        if "focus_indices" in step:
            focus_indices = step.pop("focus_indices")
            if not isinstance(focus_indices, list) or any(isinstance(index, bool) or not isinstance(index, int) or index not in parts for index in focus_indices):
                raise ValueError("Profile detail view references an unknown source-bound index.")
            step["focus_part_ids"] = [parts[index]["id"] for index in focus_indices]
        step["evidence"] = [{"kind": "existing_source_profile", "basis": profile.get("origin", "Explicit source profile")}]
        step["warnings"] = list(step.get("warnings", [])) + [
            "本步沿用原项目配置；箭头表示模型复位示意，紧固件身份和连接细节仍须按当前 STP 核对。"
        ]
        step["reviewed"] = False
        steps.append(step)
    plan = {
        "schema_version": 1, "source": deepcopy(current["source"]),
        "product": {"title": profile.get("title", current["product"]["title"])},
        "status": "draft", "sequence_mode": "configured",
        "groups": groups, "steps": steps, "style": deepcopy(profile["style"]),
        "connection_candidates": [], "excluded_part_ids": excluded,
        "exclusion_reason": profile.get("exclusion_reason", ""),
        "configuration_origin": {"kind": "explicit_exact_source_profile", "description": profile.get("origin", "")},
        "analysis_summary": deepcopy(current.get("analysis_summary", {})),
        "warnings": list(current.get("warnings", [])) + [
            "本稿使用原项目人工配置，自动识别建议仍保存在 proposed_steps.json；这不是自动识别效果的证明。"
        ],
    }
    if "manual_document" in profile:
        plan["manual_document"] = deepcopy(profile["manual_document"])
    if "cover_scene" in profile:
        plan["cover_scene"] = deepcopy(profile["cover_scene"])
    validate_plan(plan, analysis)
    return plan
