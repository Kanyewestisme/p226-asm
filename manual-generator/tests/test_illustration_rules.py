"""Geometry-only illustration suggestions, without CAD imports or rendering."""
from copy import deepcopy
from pathlib import Path
import math
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from draft_planner import build_plan, validate_plan
from illustration_rules import projection_metrics, suggest_initial_illustrations


def fixture():
    return {
        "source_sha256": "source-current", "warnings": [],
        "parts": [
            {"id": "base-instance", "name": "same name", "bbox_min": [0, 0, 0], "bbox_max": [30, 10, 30],
             "volume_mm3": 9000, "solids": 1, "occurrence_path": ["root:1", "component:1"]},
            {"id": "small-instance", "name": "same name", "bbox_min": [30.05, 4, 5], "bbox_max": [36.05, 6, 9],
             "volume_mm3": 48, "solids": 1, "occurrence_path": ["root:1", "component:2"]},
        ],
        "contacts": [{"part_ids": ["base-instance", "small-instance"], "distance_mm": 0.05,
                      "point_a": [30, 5, 7], "point_b": [30.05, 5, 7], "status": "candidate"}],
    }


class IllustrationRulesTests(unittest.TestCase):
    def test_projection_metrics_distinguish_collapsed_arrows_and_box_overlap(self):
        bounds = ([0, 0, 0], [10, 10, 10])
        collapsed = projection_metrics([1, 0, 0], [0, 1, 0], bounds, bounds, [20, 0, 0])
        separated = projection_metrics([0, 0, 1], [0, 1, 0], bounds, bounds, [20, 0, 0])
        self.assertAlmostEqual(collapsed["projected_arrow_fraction"], 0)
        self.assertAlmostEqual(collapsed["moving_box_overlap_fraction"], 1)
        self.assertAlmostEqual(separated["projected_arrow_fraction"], 1)
        self.assertAlmostEqual(separated["moving_box_overlap_fraction"], 0)
        for metrics in (collapsed, separated):
            self.assertEqual(metrics["kind"], "bounding_box_projection_proxy")
            self.assertIs(metrics["visibility_verified"], False)
            self.assertIs(metrics["assembly_motion_verified"], False)

    def test_actual_candidate_point_anchors_arrow_and_current_instances_supply_focus(self):
        source = fixture()
        unchanged = deepcopy(source)
        plan = build_plan(source)
        step = plan["steps"][1]
        self.assertEqual(step["arrows"][0]["to"], [30.05, 5, 7])
        self.assertNotEqual(step["arrows"][0]["to"], [33.05, 5, 7])
        self.assertEqual(step["arrows"][0]["meaning"], "reposition")
        self.assertEqual(step["arrows"][0]["status"], "proposed")
        gid = step["moving_groups"][0]
        self.assertEqual(step["arrows"][0]["from"], [a + d for a, d in zip(step["arrows"][0]["to"], step["offsets"][gid])])
        self.assertEqual(set(step["focus_part_ids"]), {"small-instance", "base-instance"})
        suggestion = step["illustration_suggestion"]
        self.assertEqual(suggestion["anchor_basis"], "measured_candidate_point")
        self.assertEqual(suggestion["source_sha256"], source["source_sha256"])
        self.assertEqual(suggestion["contact_focus"]["status"], "unverified")
        self.assertIn("whole_source_instances", suggestion["contact_focus"]["scope"])
        self.assertTrue(any("不能证明孔位" in warning for warning in step["warnings"]))
        self.assertTrue(any("不证明插入方向" in warning for warning in step["warnings"]))
        self.assertEqual(source, unchanged)
        validate_plan(plan, source)

    def test_candidate_point_direction_reversal_uses_moving_side_not_static_side(self):
        source = fixture()
        original = build_plan(source)["steps"][1]
        contact = source["contacts"][0]
        contact["part_ids"].reverse()
        contact["point_a"], contact["point_b"] = contact["point_b"], contact["point_a"]
        reversed_step = build_plan(source)["steps"][1]
        for key in ("camera", "up", "offsets", "arrows", "focus_part_ids", "illustration_suggestion"):
            self.assertEqual(original[key], reversed_step[key])

    def test_no_contact_and_out_of_box_measurements_use_center_proxy_without_inventing_details(self):
        for contacts in ([], [{"part_ids": ["base-instance", "small-instance"], "distance_mm": 0.05,
                              "point_a": [0, 0, 0], "point_b": [0.05, 0, 0], "status": "candidate"}]):
            source = fixture()
            source["contacts"] = contacts
            plan = build_plan(source)
            step = plan["steps"][1]
            self.assertEqual(step["focus_part_ids"], [])
            self.assertEqual(step["arrows"][0]["to"], [33.05, 5, 7])
            self.assertEqual(step["illustration_suggestion"]["anchor_basis"], "moving_group_bbox_center_proxy")
            self.assertNotIn("contact_focus", step["illustration_suggestion"])
            self.assertTrue(any("包围盒代理" in warning for warning in step["warnings"]))
            validate_plan(plan, source)

    def test_names_never_classify_fasteners_or_change_camera_displacement_or_focus(self):
        source = fixture()
        first = build_plan(source)["steps"][1]
        source["parts"][0]["name"] = "M6 screw threaded bolt"
        source["parts"][1]["name"] = "mesh fabric footrest"
        other = build_plan(source)["steps"][1]
        for key in ("camera", "up", "offsets", "arrows", "focus_part_ids", "illustration_suggestion"):
            self.assertEqual(first[key], other[key])

    def test_proposals_keep_all_geometry_visible_and_confirmation_unset(self):
        source = fixture()
        source["parts"][1]["solids"] = 0
        plan = build_plan(source)
        self.assertEqual(plan["status"], "draft")
        self.assertNotIn("excluded_part_ids", plan)
        self.assertEqual({p for group in plan["groups"] for p in group["part_ids"]}, {"base-instance", "small-instance"})
        for step in plan["steps"]:
            self.assertFalse(step["reviewed"])
            self.assertEqual(step["hidden_part_ids"], [])
            self.assertEqual(step["visibility_reason"], "")
        self.assertFalse(plan["style"]["illustration_rules"]["automatic_hidden_parts"])

    def test_book_style_reference_and_final_scene_stay_consistent(self):
        plan = build_plan(fixture())
        rules = plan["style"]["illustration_rules"]
        self.assertEqual(rules["status"], "proposed")
        self.assertEqual(rules["arrow_color"], "#d62720")
        self.assertEqual(rules["arrow_meaning"], "display_reposition_only")
        self.assertEqual(rules["up_basis"], "source_coordinate_convention_not_verified_gravity")
        self.assertEqual(plan["steps"][0]["camera"], plan["style"]["camera"])
        self.assertEqual(plan["steps"][-1]["camera"], plan["style"]["camera"])
        self.assertTrue(all(step["up"] == plan["style"]["up"] for step in plan["steps"]))
        self.assertEqual(plan["steps"][-1]["arrows"], [])
        self.assertEqual(plan["steps"][-1]["offsets"], {})

    def test_projection_proxy_avoids_tiny_arrows_with_bounded_dimension_relative_offsets(self):
        source = fixture()
        plan = build_plan(source)
        step = plan["steps"][1]
        metrics = step["illustration_suggestion"]["metrics"]
        self.assertGreater(metrics["projected_arrow_fraction"], 0.75)
        self.assertLessEqual(metrics["moving_box_overlap_fraction"], 1)
        displacement = next(iter(step["offsets"].values()))
        ratio = math.hypot(*displacement) / 36.05
        self.assertGreaterEqual(ratio, 0.22 * 0.8 - 1e-12)
        self.assertLessEqual(ratio, 0.22 * 1.35 + 1e-12)
        self.assertFalse(metrics["visibility_verified"])
        self.assertFalse(metrics["assembly_motion_verified"])

    def test_input_permutations_keep_proposals_deterministic(self):
        source = fixture()
        original = build_plan(source)
        source["parts"].reverse()
        self.assertEqual(original, build_plan(source))

    def test_uniform_model_scaling_keeps_display_ratios_and_current_contact_anchors(self):
        source = fixture()
        original = build_plan(source)["steps"][1]
        for part in source["parts"]:
            for key in ("bbox_min", "bbox_max"):
                part[key] = [value * 10 for value in part[key]]
        for contact in source["contacts"]:
            for key in ("point_a", "point_b"):
                contact[key] = [value * 10 for value in contact[key]]
            contact["distance_mm"] *= 10
        scaled = build_plan(source)["steps"][1]
        self.assertEqual(original["camera"], scaled["camera"])
        self.assertEqual(original["up"], scaled["up"])
        for before, after in zip(original["arrows"][0]["to"], scaled["arrows"][0]["to"]):
            self.assertAlmostEqual(before * 10, after)
        for before, after in zip(next(iter(original["offsets"].values())), next(iter(scaled["offsets"].values()))):
            self.assertAlmostEqual(before * 10, after)

    def test_saved_camera_edits_are_not_reproposed_without_explicit_fresh_draft_flag(self):
        source = fixture()
        plan = build_plan(source)
        plan["steps"][1]["camera"] = [-3, 2, 4]
        before = deepcopy(plan)
        self.assertEqual(suggest_initial_illustrations(plan, source), before)
        self.assertEqual(plan, before)

    def test_initial_only_integration_does_not_call_rules_for_user_supplied_groups(self):
        source = fixture()
        groups = build_plan(source)["groups"]
        with patch("illustration_rules.suggest_initial_illustrations", side_effect=AssertionError("must not re-propose authored scenes")):
            plan = build_plan(source, groups=groups)
        self.assertNotIn("illustration_rules", plan["style"])
        self.assertTrue(all("illustration_suggestion" not in step for step in plan["steps"]))

    def test_source_profiles_reviewed_or_hidden_edits_and_template_mappings_are_unchanged(self):
        source = fixture()
        original = build_plan(source)
        edits = [lambda plan: plan.update(configuration_origin={"kind": "explicit_exact_source_profile"}),
                 lambda plan: plan.update(pdf_template={"path": "original.pdf"}),
                 lambda plan: plan.update(manual_document={"title": "Authored text"}),
                 lambda plan: plan["steps"][0].update(reviewed=True),
                 lambda plan: plan["steps"][1].update(hidden_part_ids=["base-instance"], visibility_reason="User choice"),
                 lambda plan: plan.update(status="confirmed")]
        for edit in edits:
            plan = deepcopy(original)
            edit(plan)
            plan["steps"][1]["camera"] = [3, 2, -1]
            before = deepcopy(plan)
            self.assertEqual(suggest_initial_illustrations(plan, source, fresh_automatic_draft=True), before)
            self.assertEqual(plan, before)


if __name__ == "__main__":
    unittest.main()
