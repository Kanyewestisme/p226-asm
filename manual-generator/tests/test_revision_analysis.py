"""Revision review never silently maps names, inherits review or reuses figures."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from draft_planner import build_plan
from revision_analysis import compare_inventories, compare_projects, compare_revisions


def part(identity, *, fingerprint="shape", x=0, name="same-name", index=0):
    return {"id": identity, "index": index, "name": name,
            "bbox_min": [x, 0, 0], "bbox_max": [x+10, 10, 10], "bbox_size": [10, 10, 10],
            "solids": 1, "faces": 6, "volume_mm3": 1000, "area_mm2": 600,
            "geometry_fingerprint": fingerprint}


def analysis(rows, digest="old-source"):
    return {"source_sha256": digest, "parts": rows, "contacts": [], "warnings": []}


def plan_for(source):
    result = build_plan(source)
    result["sequence_mode"] = "configured"
    return result


class RevisionAnalysisTests(unittest.TestCase):
    def test_changed_source_invalidates_every_old_step_even_if_not_reviewed(self):
        old = analysis([part("old-a", fingerprint="a"), part("old-b", fingerprint="b", x=20)])
        new = analysis([part("new-a", fingerprint="a"), part("new-b", fingerprint="b", x=20)], "new-source")
        plan = plan_for(old)
        plan["steps"][0]["reviewed"] = True
        plan["steps"][-1]["focus_part_ids"] = ["old-a"]
        old_before, new_before, plan_before = deepcopy(old), deepcopy(new), deepcopy(plan)
        report = compare_inventories(old, new, plan)
        expected = [step["id"] for step in plan["steps"]]
        self.assertEqual(report["invalidated_step_ids"], expected)
        self.assertEqual([item["step_id"] for item in report["diagram_updates"]], expected)
        self.assertTrue(report["confirmations_invalidated"])
        self.assertTrue(all(item["needs_diagram_update"] for item in report["step_impacts"]))
        self.assertTrue(all(item["confirmation_invalidated"] for item in report["step_impacts"]))
        self.assertTrue(all(item["status"] == "unchanged_candidate" for item in report["object_impacts"]))
        self.assertFalse(report["old_diagrams_may_be_used_as_new_source"])
        self.assertFalse(report["automatically_applied"])
        self.assertEqual(report["diagram_updates"][-1]["assets"],
                         [{"asset_id": expected[-1], "kind": "main"},
                          {"asset_id": expected[-1]+"_focus", "kind": "focus"}])
        self.assertEqual(old, old_before)
        self.assertEqual(new, new_before)
        self.assertEqual(plan, plan_before)

    def test_names_and_indices_do_not_establish_correspondence(self):
        old = analysis([part("old", fingerprint="old-shape", name="bolt", index=2)])
        new = analysis([part("new", fingerprint="other-shape", name="bolt", index=2)], "new-source")
        report = compare_inventories(old, new, plan_for(old))
        obj = report["object_impacts"][0]
        self.assertEqual(obj["status"], "missing")
        self.assertEqual(obj["new_part_ids"], [])
        self.assertEqual(report["matches"], [])
        self.assertFalse(report["names_used_for_matching"])
        self.assertFalse(report["indices_used_for_matching"])
        self.assertIn("不能据此判定零件被删除", obj["reason"])

    def test_unique_moved_geometry_is_observed_change_and_review_clue(self):
        old = analysis([part("old")])
        new = analysis([part("new", x=35)], "new-source")
        report = compare_inventories(old, new, plan_for(old))
        obj = report["object_impacts"][0]
        self.assertEqual(obj["status"], "changed")
        self.assertEqual(obj["new_part_ids"], ["new"])
        self.assertTrue(obj["mutually_unique_candidate"])
        self.assertTrue(obj["requires_review"])
        self.assertFalse(obj["correspondence_verified"])
        self.assertEqual({item["field"] for item in obj["observed_differences"]}, {"bbox_min", "bbox_max"})
        self.assertTrue(all("old" in step["objects_by_status"]["changed"] for step in report["step_impacts"]))

    def test_same_coarse_fingerprint_does_not_hide_topology_change(self):
        old = analysis([part("old")])
        new_part = part("new")
        new_part["faces"] = 8
        new_part["volume_mm3"] = 1100
        report = compare_inventories(old, analysis([new_part], "new-source"))
        obj = report["object_impacts"][0]
        self.assertEqual(obj["status"], "changed")
        self.assertEqual({item["field"] for item in obj["observed_differences"]}, {"faces", "volume_mm3"})
        self.assertFalse(obj["correspondence_verified"])

    def test_repeated_overlapping_geometry_stays_ambiguous(self):
        old = analysis([part("old-a"), part("old-b")])
        new = analysis([part("new-a"), part("new-b")], "new-source")
        report = compare_inventories(old, new, plan_for(old))
        self.assertEqual(report["matches"], [])
        self.assertTrue(all(obj["status"] == "ambiguous" for obj in report["object_impacts"]))
        self.assertTrue(all(obj["new_part_ids"] == ["new-a", "new-b"] for obj in report["object_impacts"]))
        self.assertTrue(all(not obj["mutually_unique_candidate"] for obj in report["object_impacts"]))
        self.assertTrue(all(step["objects_by_status"]["ambiguous"] for step in report["step_impacts"]))

    def test_many_old_parts_competing_for_one_new_part_stay_ambiguous(self):
        old = analysis([part("old-a"), part("old-b")])
        new = analysis([part("new")], "new-source")
        report = compare_inventories(old, new)
        self.assertEqual(report["matches"], [])
        self.assertEqual([obj["status"] for obj in report["object_impacts"]], ["ambiguous", "ambiguous"])

    def test_placement_disambiguates_clues_but_never_certifies_identity(self):
        old = analysis([part("old-a", x=0), part("old-b", x=25)])
        new = analysis([part("new-b", x=25), part("new-a", x=0)], "new-source")
        report = compare_inventories(old, new)
        self.assertEqual({(obj["old_part_id"], obj["new_part_ids"][0]) for obj in report["object_impacts"]},
                         {("old-a", "new-a"), ("old-b", "new-b")})
        self.assertTrue(all(obj["status"] == "unchanged_candidate" for obj in report["object_impacts"]))
        self.assertTrue(all(not obj["correspondence_verified"] for obj in report["object_impacts"]))

    def test_hidden_object_changes_are_reported_without_claiming_visible_change(self):
        old = analysis([part("old-a", fingerprint="a"), part("old-b", fingerprint="b", x=20)])
        plan = plan_for(old)
        last = plan["steps"][-1]
        # Keep the final overview valid; inspect a separately configured scene.
        detail = deepcopy(last)
        detail["id"] = "inspection"
        detail["hidden_part_ids"] = ["old-b"]
        detail["visibility_reason"] = "Show the connector clearly"
        plan["steps"].insert(-1, detail)
        new = analysis([part("new-a", fingerprint="a"), part("new-b", fingerprint="b", x=45)], "new-source")
        report = compare_inventories(old, new, plan)
        impact = next(item for item in report["step_impacts"] if item["step_id"] == "inspection")
        self.assertEqual(impact["objects_by_status"]["changed"], ["old-b"])
        self.assertEqual(impact["visible_objects_by_status"]["changed"], [])
        self.assertEqual(impact["hidden_old_part_ids"], ["old-b"])
        self.assertTrue(impact["needs_diagram_update"])

    def test_view_configuration_is_only_a_suggestion_without_positions_or_confirmation(self):
        old = analysis([part("old")])
        plan = plan_for(old)
        plan["pdf_template"] = {"sha256": "template-sha", "figure_slots": []}
        plan["cover_scene"] = {"some": "old-source-scene"}
        new = analysis([part("new", x=40)], "new-source")
        report = compare_inventories(old, new, plan)
        suggestion = report["view_configuration_suggestions"][0]
        self.assertEqual(suggestion["camera"], plan["steps"][0]["camera"])
        self.assertEqual(suggestion["up"], plan["steps"][0]["up"])
        self.assertTrue(suggestion["requires_review"])
        self.assertFalse(suggestion["automatically_applied"])
        self.assertNotIn("offsets", suggestion)
        self.assertNotIn("arrows", suggestion)
        self.assertIn("arrows", suggestion["must_rebind_before_use"])
        self.assertTrue(report["fixed_template_binding"]["requires_new_source_binding_review"])
        self.assertFalse(report["fixed_template_binding"]["automatically_transferred"])
        self.assertTrue(report["parts_overview_update"]["needs_update"])

    def test_missing_old_plan_explicitly_prevents_claiming_step_coverage(self):
        old = analysis([part("old", fingerprint=None)])
        new = analysis([part("new")], "new-source")
        report = compare_inventories(old, new)
        self.assertTrue(report["requires_old_plan_to_assess_steps"])
        self.assertEqual(report["step_impacts"], [])
        self.assertEqual(report["diagram_updates"], [])
        self.assertEqual(report["object_impacts"][0]["status"], "missing")
        self.assertIn("没有可用几何指纹", report["object_impacts"][0]["reason"])

    def test_same_source_does_not_invalidate_unchanged_review(self):
        old = analysis([part("a"), part("b")])
        plan = plan_for(old)
        for step in plan["steps"]:
            step["reviewed"] = True
        plan["status"] = "confirmed"
        report = compare_revisions(old, deepcopy(old), plan)
        self.assertFalse(report["source_changed"])
        self.assertFalse(report["confirmations_invalidated"])
        self.assertEqual(report["invalidated_step_ids"], [])
        self.assertEqual(report["diagram_updates"], [])
        self.assertEqual(report["ambiguous_matches"], [])
        self.assertEqual(report["matching_inputs"], ["source_sha256", "occurrence_id"])

    def test_same_source_with_conflicting_geometry_is_rejected(self):
        old = analysis([part("old")])
        bad = deepcopy(old)
        bad["parts"][0]["bbox_min"][0] = 1
        with self.assertRaisesRegex(ValueError, "conflicting geometry"):
            compare_inventories(old, bad)


class SavedProjectComparisonTests(unittest.TestCase):
    @staticmethod
    def write_inventory(directory, source):
        directory.mkdir(parents=True)
        source = deepcopy(source)
        for row in source["parts"]:
            row["source_sha256"] = source["source_sha256"]
            row["brep"] = f"part_{row['id']}.brep"
            data = ("existing cache for " + row["id"]).encode("utf-8")
            (directory / row["brep"]).write_bytes(data)
            row["brep_sha256"] = hashlib.sha256(data).hexdigest()
        rows_text = json.dumps(source["parts"], ensure_ascii=False).encode("utf-8")
        (directory / "assembly_parts.json").write_bytes(rows_text)
        completion = {"source_sha256": source["source_sha256"], "parts": len(source["parts"]),
                      "units": "mm", "brep_paths_relative_to": "inventory",
                      "inventory_sha256": hashlib.sha256(rows_text).hexdigest()}
        (directory / "import_complete.json").write_text(json.dumps(completion), encoding="utf-8")
        (directory / "analysis.json").write_text(json.dumps(source), encoding="utf-8")
        return source

    def test_project_comparison_only_reads_existing_snapshots(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            old, new = root / "old", root / "new"
            old_analysis = self.write_inventory(old / "inventory", analysis([part("old")]))
            self.write_inventory(new / "inventory", analysis([part("new", x=25)], "new-source"))
            (old / "steps.json").write_text(json.dumps(plan_for(old_analysis)), encoding="utf-8")
            before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with patch("model_analysis.import_model", side_effect=AssertionError("must not import")), \
                 patch("model_analysis.analyze_inventory", side_effect=AssertionError("must not solve")):
                report = compare_projects(old, new)
            after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(report["object_impacts"][0]["status"], "changed")
            self.assertEqual(len(report["old_analysis_sha256"]), 64)
            self.assertEqual(len(report["new_analysis_sha256"]), 64)
            self.assertEqual(len(report["old_plan_sha256"]), 64)

    def test_modified_saved_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = self.write_inventory(root / "inventory", analysis([part("old")]))
            brep = root / "inventory" / source["parts"][0]["brep"]
            brep.write_bytes(brep.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "BREP cache differs"):
                compare_inventories(root / "inventory", deepcopy(source))


if __name__ == "__main__":
    unittest.main()
