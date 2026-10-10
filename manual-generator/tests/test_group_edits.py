"""Instance-safe, reversible proposals for packaging-selected group merges."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from draft_planner import build_plan, validate_plan
from group_edits import merge_groups


def fixture():
    parts = [{"id": f"part-{index}", "name": "同名实例", "index": index,
              "occurrence_path": ["root", f"branch-{index}", "leaf"],
              "parent_occurrence": f"opaque-{index}", "path": ["整机", "同名总成", "同名零件"],
              "bbox_min": [index * 25, 0, 0], "bbox_max": [index * 25 + 20, 20, 20],
              "bbox_size": [20, 20, 20], "solids": 1, "volume_mm3": 8000,
              "geometry_fingerprint": "repeated-shape"} for index in range(4)]
    contacts = [{"part_ids": [f"part-{index}", f"part-{index + 1}"],
                 "distance_mm": 0.1, "point_a": [index * 25 + 20, 10, 10],
                 "point_b": [index * 25 + 20.1, 10, 10], "status": "candidate"}
                for index in range(3)]
    analysis = {"source_sha256": "source-a", "parts": parts, "contacts": contacts, "warnings": []}
    return analysis, build_plan(analysis, title="用户产品标题")


class GroupEditTests(unittest.TestCase):
    def test_regroup_keeps_product_text_without_claiming_original_order(self):
        analysis, plan = fixture()
        plan["manual_document"] = {
            "title": "用户说明书",
            "introduction": ["本册保留原说明书7步顺序。"],
            "review_introduction": ["完整7步原版顺序。"],
            "care_sections": [{"title": "清洁", "items": ["用户已提供的清洁文字"]}],
        }
        result = merge_groups(plan, analysis, [g["id"] for g in plan["groups"][:2]], "包装总成")
        self.assertEqual(result["manual_document"]["care_sections"], plan["manual_document"]["care_sections"])
        self.assertNotIn("保留原说明书", "".join(result["manual_document"]["introduction"]))
        self.assertIn("重新提出", "".join(result["manual_document"]["review_introduction"]))
        self.assertEqual(plan["manual_document"]["introduction"], ["本册保留原说明书7步顺序。"])

    def test_merge_reduces_groups_and_keeps_all_repeated_name_instances(self):
        analysis, plan = fixture()
        selected = [plan["groups"][0]["id"], plan["groups"][1]["id"]]
        result = merge_groups(plan, analysis, selected, "包装总成")
        self.assertEqual(len(result["groups"]), 3)
        self.assertEqual(len(result["steps"]), 4)
        self.assertEqual(sorted(p for g in result["groups"] for p in g["part_ids"]),
                         sorted(p["id"] for p in analysis["parts"]))
        merged = next(g for g in result["groups"] if g["label"] == "包装总成")
        self.assertEqual(merged["part_ids"], ["part-0", "part-1"])
        self.assertEqual(merged["status"], "proposed")
        evidence = [e for s in result["steps"] for e in s["evidence"] if e.get("group_id") == merged["id"]]
        self.assertEqual(evidence[0]["kind"], "packaging_selected_group")
        retained = {g["id"]: g["label"] for g in result["groups"] if g["id"] not in {merged["id"]}}
        self.assertEqual(retained, {g["id"]: g["label"] for g in plan["groups"] if g["id"] not in selected})
        validate_plan(result, analysis)

    def test_resets_confirmation_and_does_not_modify_inputs(self):
        analysis, plan = fixture()
        for step in plan["steps"]:
            step["reviewed"] = True
        plan["status"] = "confirmed"
        plan["confirmation"] = {"reviewer": "包装同事"}
        before_plan, before_analysis = deepcopy(plan), deepcopy(analysis)
        result = merge_groups(plan, analysis, [g["id"] for g in plan["groups"][:2]], "合并组")
        self.assertEqual(result["status"], "draft")
        self.assertNotIn("confirmation", result)
        self.assertTrue(all(step["reviewed"] is False for step in result["steps"]))
        self.assertEqual(plan, before_plan)
        self.assertEqual(analysis, before_analysis)

    def test_preserves_style_title_summary_and_recomputes_displacements(self):
        analysis, plan = fixture()
        plan["style"].update(camera=[-1, 0.7, 1], up=[0, 1, 0], width=1200,
                             height=900, margin=80, hlr="exact", explode_ratio=0.11)
        plan["analysis_summary"]["note"] = "当前源分析"
        result = merge_groups(plan, analysis, [g["id"] for g in plan["groups"][:2]], "合并组")
        self.assertEqual(result["style"], plan["style"])
        self.assertEqual(result["product"]["title"], plan["product"]["title"])
        self.assertEqual(result["analysis_summary"], plan["analysis_summary"])
        default = build_plan(analysis, title=plan["product"]["title"], groups=result["groups"])
        for actual, initial in zip(result["steps"], default["steps"]):
            self.assertEqual(actual["camera"], plan["style"]["camera"])
            self.assertEqual(actual["up"], plan["style"]["up"])
            for gid in actual["offsets"]:
                self.assertEqual(actual["offsets"][gid], [v / 2 for v in initial["offsets"][gid]])
            for arrow in actual["arrows"]:
                self.assertEqual(arrow["meaning"], "reposition")
        self.assertEqual(result["connection_candidates"], default["connection_candidates"])

    def test_selection_order_does_not_change_identity_or_proposal(self):
        analysis, plan = fixture()
        selected = [g["id"] for g in plan["groups"][:3]]
        self.assertEqual(merge_groups(plan, analysis, selected, "合并组"),
                         merge_groups(plan, analysis, selected[::-1], "合并组"))

    def test_zero_explode_ratio_is_preserved(self):
        analysis, plan = fixture()
        plan["style"]["explode_ratio"] = 0
        result = merge_groups(plan, analysis, [g["id"] for g in plan["groups"][:2]], "合并组")
        self.assertTrue(all(offset == [0, 0, 0] for s in result["steps"] for offset in s["offsets"].values()))
        self.assertTrue(all(a["from"] == a["to"] for s in result["steps"] for a in s["arrows"]))

    def test_unknown_duplicate_insufficient_or_invalid_selection_rejected(self):
        analysis, plan = fixture()
        ids = [g["id"] for g in plan["groups"]]
        for selection in ([], ids[:1], [ids[0], ids[0]], [ids[0], "unknown"], [ids[0], 2], "ids"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                merge_groups(plan, analysis, selection, "合并组")
        for label in ("", "   ", None):
            with self.subTest(label=label), self.assertRaises(ValueError):
                merge_groups(plan, analysis, ids[:2], label)

    def test_changed_source_rejected_and_supplied_groups_need_exact_coverage(self):
        analysis, plan = fixture()
        changed = deepcopy(analysis)
        changed["source_sha256"] = "source-b"
        with self.assertRaises(ValueError):
            merge_groups(plan, changed, [g["id"] for g in plan["groups"][:2]], "合并组")
        invalid_sets = [plan["groups"][:-1], plan["groups"] + [plan["groups"][0]],
                        [{**plan["groups"][0], "part_ids": ["unknown"]}]]
        for groups in invalid_sets:
            with self.assertRaises(ValueError):
                build_plan(analysis, groups=groups)

    def test_merge_keeps_explicit_exclusions_and_reproposes_cumulative_sequence(self):
        analysis, plan = fixture()
        auxiliary = deepcopy(analysis["parts"][0])
        auxiliary["id"] = "auxiliary-instance"
        auxiliary["occurrence_path"] = ["root", "branch-0", "auxiliary"]
        auxiliary["solids"] = 0
        analysis["parts"].append(auxiliary)
        plan = build_plan(analysis)
        plan["excluded_part_ids"] = [auxiliary["id"]]
        plan["exclusion_reason"] = "包装选定的辅助几何。"
        plan["sequence_mode"] = "configured"
        original = deepcopy(plan)
        result = merge_groups(plan, analysis, [g["id"] for g in plan["groups"][:2]], "合并组")
        self.assertEqual(result["excluded_part_ids"], plan["excluded_part_ids"])
        self.assertEqual(result["exclusion_reason"], plan["exclusion_reason"])
        self.assertEqual(result["sequence_mode"], "cumulative")
        self.assertIn(auxiliary["id"], {p for g in result["groups"] for p in g["part_ids"]})
        self.assertEqual(plan, original)
        validate_plan(result, analysis)


if __name__ == "__main__":
    unittest.main()
