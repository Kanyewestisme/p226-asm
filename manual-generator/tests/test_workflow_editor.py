"""Packaging actions retain source geometry and reject misleading structural edits."""
from copy import deepcopy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from draft_planner import build_plan, validate_plan
from workflow_editor import apply_workflow_edit, apply_reference_profile, workflow_summary


def source():
    rows = []
    for index, (group, child) in enumerate((("one", "a"), ("one", "b"), ("two", "c"), ("three", "d"))):
        lo = [index * 30, 0, 0]
        rows.append({"id": f"p{index}", "name": "Repeated name", "occurrence_path": ["root", group, child],
                     "path": ["Product", "Repeated assembly", "Repeated name"],
                     "bbox_min": lo, "bbox_max": [lo[0] + 20, 20, 20],
                     "bbox_size": [20, 20, 20], "volume_mm3": 8000, "solids": 1})
    return {"source_sha256": "new-source", "parts": rows, "contacts": [], "warnings": [],
            "candidate_pair_count": 30, "tested_pair_count": 5, "completed_pair_count": 3,
            "timed_out_pair_count": 2, "non_geometric_occurrences": [{"id": "empty"}]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.analysis = source()
        self.plan = build_plan(self.analysis)
        self.groups = [g["id"] for g in self.plan["groups"]]
        self.steps = [s["id"] for s in self.plan["steps"]]

    def edit(self, action, **args):
        return apply_workflow_edit(self.plan, self.analysis, {"action": action, **args})

    def assertGeometry(self, result):
        validate_plan(result, self.analysis)
        self.assertEqual({p for g in result["groups"] for p in g["part_ids"]}, {"p0", "p1", "p2", "p3"})
        self.assertEqual(set(result["steps"][-1]["assembled_groups"]), {g["id"] for g in result["groups"]})
        self.assertEqual(result["steps"][-1]["moving_groups"], [])
        self.assertEqual(result["steps"][-1]["hidden_part_ids"], [])

    def test_unit_roles_do_not_drop_or_hide_actual_geometry(self):
        for role in ("unclassified", "preassembled", "installation", "auxiliary"):
            result = self.edit("set_group_role", group_id=self.groups[0], role=role, reason="Reviewed package")
            self.assertEqual(result["groups"][0]["role"], role)
            self.assertEqual(result.get("excluded_part_ids", []), [])
            self.assertTrue(all(not s["hidden_part_ids"] for s in result["steps"]))
            self.assertGeometry(result)

    def test_role_invalid_or_foreign_identity_is_rejected_without_input_mutation(self):
        original = deepcopy(self.plan)
        for op in ({"action": "set_group_role", "group_id": self.groups[0], "role": "hidden"},
                   {"action": "rename_group", "group_id": "other", "label": "Friendly"}):
            with self.assertRaises(ValueError):
                apply_workflow_edit(self.plan, self.analysis, op)
        self.assertEqual(self.plan, original)
        altered = deepcopy(self.analysis)
        altered["source_sha256"] = "old-source"
        with self.assertRaises(ValueError):
            apply_workflow_edit(self.plan, altered, {"action": "rename_group", "group_id": self.groups[0], "label": "Label"})

    def test_rename_preserves_camera_and_drawing_scene(self):
        original = deepcopy(self.plan)
        result = self.edit("rename_group", group_id=self.groups[0], label="  Friendly name  ")
        self.assertEqual(result["groups"][0]["label"], "Friendly name")
        for old, new in zip(original["steps"], result["steps"]):
            for field in ("camera", "up", "assembled_groups", "moving_groups", "offsets", "arrows"):
                self.assertEqual(old[field], new[field])
        self.assertEqual(self.plan, original)

    def test_split_preserves_source_and_moving_scene_but_exposes_two_installation_units(self):
        original = deepcopy(self.plan)
        result = self.edit("split_group", group_id=self.groups[0], part_ids=["p1"], label="New unit")
        self.assertEqual(len(result["groups"]), 4)
        new_id = next(g["id"] for g in result["groups"] if g["label"] == "New unit")
        self.assertEqual(result["groups"][0]["part_ids"], ["p0"])
        for old, new in zip(original["steps"], result["steps"]):
            if self.groups[0] in old["moving_groups"]:
                self.assertIn(new_id, new["moving_groups"])
                self.assertEqual(new["offsets"][new_id], old["offsets"][self.groups[0]])
            self.assertEqual(old["camera"], new["camera"])
        self.assertGeometry(result)
        self.assertEqual(self.plan, original)

    def test_split_never_accepts_foreign_duplicate_empty_or_entire_group(self):
        for members in ([], ["unknown"], ["p0", "p0"], ["p0", "p1"]):
            with self.assertRaises(ValueError):
                self.edit("split_group", group_id=self.groups[0], part_ids=members, label="New")

    def test_merge_preserves_user_steps_and_views_without_redrafting_everything(self):
        original_ids = [s["id"] for s in self.plan["steps"]]
        result = self.edit("merge_groups", group_ids=self.groups[:2], label="Preassembled")
        self.assertEqual([s["id"] for s in result["steps"]], original_ids)
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(result["sequence_mode"], "configured")
        self.assertTrue(all(not set(s["assembled_groups"]).intersection(s["moving_groups"]) for s in result["steps"]))
        self.assertEqual([s["camera"] for s in result["steps"]], [s["camera"] for s in self.plan["steps"]])
        self.assertGeometry(result)

    def test_reorder_is_free_of_wrong_cumulative_constraint_but_preserves_final(self):
        order = [self.steps[2], self.steps[0], self.steps[1], self.steps[3]]
        result = self.edit("reorder_steps", step_ids=order)
        self.assertEqual([s["id"] for s in result["steps"]], order)
        self.assertEqual(result["sequence_mode"], "configured")
        self.assertGeometry(result)
        for invalid in (list(reversed(order)), order[:-1], [order[0]] * len(order)):
            with self.assertRaises(ValueError):
                self.edit("reorder_steps", step_ids=invalid)

    def test_add_action_accepts_multiple_units_and_source_bound_offsets(self):
        result = self.edit("add_step", after_step_id=self.steps[0], title="Fit two units", instruction="Explicit operator words",
                           moving_groups=self.groups[1:], assembled_groups=[self.groups[0]],
                           offsets={self.groups[1]: [50, 0, 0], self.groups[2]: [-50, 0, 0]})
        added = result["steps"][1]
        self.assertEqual(added["moving_groups"], self.groups[1:])
        self.assertEqual(added["authored_by"], "packaging")
        self.assertEqual(len(added["arrows"]), 2)
        self.assertGeometry(result)
        with self.assertRaises(ValueError):
            self.edit("add_step", after_step_id=self.steps[-1], title="After final", instruction="No", moving_groups=self.groups[:1])

    def test_remove_action_requires_reassignment_for_unclassified_units(self):
        with self.assertRaisesRegex(ValueError, "接收动作"):
            self.edit("remove_step", step_id=self.steps[0])
        result = self.edit("remove_step", step_id=self.steps[0], reassign_to=self.steps[1])
        self.assertEqual(len(result["steps"]), 3)
        moved = next(s for s in result["steps"] if s["id"] == self.steps[1])
        self.assertIn(self.plan["steps"][0]["moving_groups"][0], moved["moving_groups"])
        self.assertGeometry(result)
        with self.assertRaises(ValueError):
            self.edit("remove_step", step_id=self.steps[-1], reassign_to=self.steps[0])

    def test_preassembled_classification_allows_remove_but_keeps_geometry(self):
        gid = self.plan["steps"][0]["moving_groups"][0]
        prepared = self.edit("set_group_role", group_id=gid, role="preassembled")
        result = apply_workflow_edit(prepared, self.analysis, {"action": "remove_step", "step_id": self.steps[0]})
        self.assertGeometry(result)

    def test_update_scene_assigns_multiple_units_and_keeps_explicit_user_camera(self):
        result = self.edit("update_step", step_id=self.steps[0], title="User installation action", instruction="Actual steps",
                           assembled_groups=[], moving_groups=self.groups[:2], offsets={self.groups[1]: [40, 0, 0]},
                           camera=[1, 1, 1], up=[0, 0, 1])
        self.assertEqual(result["steps"][0]["camera"], [1.0, 1.0, 1.0])
        self.assertEqual(result["steps"][0]["offsets"][self.groups[0]], [0, 0, 0])
        self.assertGeometry(result)
        with self.assertRaises(ValueError):
            self.edit("update_step", step_id=self.steps[0], camera=[0, 1, 0], up=[0, 1, 0])

    def test_change_invalidates_confirmation_and_does_not_mutate_input(self):
        self.plan["status"] = "confirmed"
        self.plan["confirmation"] = {"source": "previous"}
        for s in self.plan["steps"]:
            s["reviewed"] = True
        original = deepcopy(self.plan)
        result = self.edit("rename_group", group_id=self.groups[0], label="Edited")
        self.assertEqual(result["status"], "draft")
        self.assertNotIn("confirmation", result)
        self.assertTrue(all(not s["reviewed"] for s in result["steps"]))
        self.assertEqual(self.plan, original)

    def test_bound_template_structural_changes_cannot_silently_relabel_original_consumer_copy(self):
        self.plan["pdf_template"] = {"path": "original.pdf", "step_bindings": []}
        for op in ({"action": "add_step", "title": "New", "instruction": "Words", "moving_groups": self.groups[:1]},
                   {"action": "remove_step", "step_id": self.steps[0], "reassign_to": self.steps[1]},
                   {"action": "reorder_steps", "step_ids": [self.steps[1], self.steps[0], self.steps[2], self.steps[3]]},
                   {"action": "update_step", "step_id": self.steps[0], "instruction": "Different meaning"}):
            with self.assertRaisesRegex(ValueError, "原说明书模板"):
                apply_workflow_edit(self.plan, self.analysis, op)
        renamed = self.edit("rename_group", group_id=self.groups[0], label="Readable source unit")
        self.assertEqual(renamed["pdf_template"], self.plan["pdf_template"])

    def test_summary_reports_incomplete_measurements_and_identity_bound_split_choices(self):
        summary = workflow_summary(self.plan, self.analysis)
        self.assertEqual(summary["geometry_count"], 4)
        self.assertEqual(summary["non_geometric_count"], 1)
        self.assertEqual(summary["completed_pairs"], 3)
        self.assertEqual(summary["unclassified_unit_count"], 3)
        self.assertFalse(summary["installation_sequence_verified"])
        first = summary["groups"][0]
        self.assertEqual({p for c in first["split_candidates"] for p in c["part_ids"]}, {"p0", "p1"})
        self.assertEqual(len({c["id"] for c in first["split_candidates"]}), 2)
        self.assertTrue(any("30" in w and "3" in w for w in summary["warnings"]))

    def reference_profile(self):
        return {"source_sha256": self.analysis["source_sha256"], "reference": {"sha256": "a" * 64},
                "title": "Current reference product", "groups": deepcopy(self.plan["groups"]),
                "steps": deepcopy(self.plan["steps"]), "style": deepcopy(self.plan["style"])}

    def test_reference_profile_uses_full_occurrence_ids_and_keeps_overview_outside_consumer_manual(self):
        profile = self.reference_profile()
        original = deepcopy(self.plan)
        result = apply_reference_profile(self.plan, self.analysis, profile)
        self.assertGeometry(result)
        self.assertEqual(result["product"]["title"], "Current reference product")
        self.assertEqual(result["configuration_origin"]["reference"]["sha256"], "a" * 64)
        self.assertFalse(result["configuration_origin"]["mapping_confirmed"])
        self.assertFalse(result["steps"][-1]["include_in_manual"])
        self.assertTrue(all(s["include_in_manual"] for s in result["steps"][:-1]))
        self.assertTrue(all(not s["reviewed"] for s in result["steps"]))
        self.assertEqual(self.plan, original)

    def test_reference_profile_rejects_other_version_missing_coverage_and_forged_reference_digest(self):
        for change in (lambda p: p.update(source_sha256="old-source"),
                       lambda p: p["reference"].update(sha256="bad"),
                       lambda p: p["groups"][0].update(part_ids=["foreign-geometry"]),
                       lambda p: p["groups"].pop(),
                       lambda p: p["groups"][1].update(part_ids=p["groups"][0]["part_ids"])):
            profile = self.reference_profile()
            change(profile)
            with self.assertRaises(ValueError):
                apply_reference_profile(self.plan, self.analysis, profile)

    def test_new_metadata_cannot_claim_motion_verification_or_hide_manual_steps_with_invalid_type(self):
        for change in (lambda p: p.update(workflow=[]),
                       lambda p: p["workflow"].update(installation_sequence_verified=True),
                       lambda p: p["workflow"].update(source_sha256="old"),
                       lambda p: p["steps"][0].update(include_in_manual="no"),
                       lambda p: p["groups"][0].update(role="not-geometry")):
            plan = deepcopy(self.plan)
            change(plan)
            with self.assertRaises(ValueError):
                validate_plan(plan, self.analysis)


if __name__ == "__main__":
    unittest.main()
