"""Optional assembly clues retain source geometry and require human choice."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from assembly_proposals import propose_assembly_candidates


def fixture(parent=("root:1", "subassembly:1")):
    parts = [{"id": f"p{i}", "name": "same name", "bbox_min": [i * 10.1, 0, 0],
              "bbox_max": [i * 10.1 + 10, 10, 10], "solids": 1, "faces": 6,
              "volume_mm3": 1000, "geometry_fingerprint": "same-coarse-geometry",
              "occurrence_path": [*parent, f"leaf:{i}"]} for i in range(3)]
    contacts = [{"part_ids": [f"p{i}", f"p{i+1}"], "distance_mm": 0.1,
                 "point_a": [i * 10.1 + 10, 5, 5], "point_b": [(i+1) * 10.1, 5, 5],
                 "status": "candidate", "method": "measured test distance"} for i in range(2)]
    analysis = {"source_sha256": "source-a", "parts": parts, "contacts": contacts,
                "warnings": [], "candidate_pair_count": 12, "tested_pair_count": 3,
                "untested_candidates": [{} for _ in range(9)], "failed_pairs": [{}]}
    groups = [{"id": f"g{i}", "label": "same assembly name", "part_ids": [f"p{i}"]} for i in range(3)]
    return analysis, groups


class AssemblyProposalTests(unittest.TestCase):
    def test_measured_pair_suggestions_trace_actual_source_contact_points(self):
        analysis, groups = fixture()
        result = propose_assembly_candidates(analysis, groups)
        pairs = [p for p in result["suggestions"] if p["kind"] == "measured_contact_pair"]
        self.assertEqual([p["group_ids"] for p in pairs], [["g0", "g1"], ["g1", "g2"]])
        first = pairs[0]
        self.assertEqual(first["evidence"]["measured_contacts"][0]["points"], [[10, 5, 5], [10.1, 5, 5]])
        self.assertEqual(first["source_sha256"], analysis["source_sha256"])
        self.assertEqual(first["evidence"]["measured_contacts"][0]["status"], "unverified")

    def test_dense_contact_cluster_requires_shared_non_root_occurrence_parent(self):
        analysis, groups = fixture()
        result = propose_assembly_candidates(analysis, groups)
        cluster = [p for p in result["suggestions"] if p["kind"] == "hierarchy_contact_cluster"]
        self.assertEqual(len(cluster), 1)
        self.assertEqual(cluster[0]["group_ids"], ["g0", "g1", "g2"])
        self.assertEqual(cluster[0]["evidence"]["common_occurrence_parent"], ["root:1", "subassembly:1"])
        self.assertAlmostEqual(cluster[0]["evidence"]["measured_graph_density"], 2 / 3)
        analysis, groups = fixture(parent=("root:1",))
        root_only = propose_assembly_candidates(analysis, groups)
        self.assertFalse(any(p["kind"] == "hierarchy_contact_cluster" for p in root_only["suggestions"]))
        self.assertEqual(root_only["diagnostics"]["suppressed_contact_clusters"], 1)
        self.assertEqual(root_only["diagnostics"]["usable_solid_group_pairs"], 2)

    def test_large_or_sparse_contact_chains_do_not_force_a_whole_assembly(self):
        analysis, groups = fixture()
        result = propose_assembly_candidates(analysis, groups, max_cluster_groups=2)
        self.assertFalse(any(p["kind"] == "hierarchy_contact_cluster" for p in result["suggestions"]))
        self.assertEqual(result["diagnostics"]["suppressed_contact_clusters"], 1)
        analysis["parts"].append({"id": "p3", "name": "same name", "bbox_min": [30.3, 0, 0],
                                  "bbox_max": [40.3, 10, 10], "solids": 1, "faces": 6,
                                  "geometry_fingerprint": "same-coarse-geometry",
                                  "occurrence_path": ["root:1", "subassembly:1", "leaf:3"]})
        groups.append({"id": "g3", "part_ids": ["p3"]})
        analysis["contacts"].append({"part_ids": ["p2", "p3"], "distance_mm": 0.1,
                                     "point_a": [30.2, 5, 5], "point_b": [30.3, 5, 5]})
        sparse = propose_assembly_candidates(analysis, groups)
        self.assertFalse(any(p["kind"] == "hierarchy_contact_cluster" for p in sparse["suggestions"]))
        self.assertEqual(sparse["diagnostics"]["suppressed_contact_clusters"], 1)

    def test_repeated_geometry_is_optional_batch_without_default_merge_or_fastener_identity(self):
        analysis, groups = fixture()
        analysis["contacts"] = []
        result = propose_assembly_candidates(analysis, groups)
        batch = next(p for p in result["suggestions"] if p["kind"] == "repeated_geometry_batch")
        self.assertEqual(batch["group_ids"], ["g0", "g1", "g2"])
        self.assertEqual(batch["action"], "review_optional_batch_without_default_merge")
        self.assertFalse(batch["evidence"]["placement_equivalence_verified"])
        self.assertTrue(batch["evidence"]["fingerprint_is_coarse"])
        self.assertIn("未认定为紧固件", batch["basis"])
        self.assertTrue(any("姿态" in warning for warning in batch["warnings"]))

    def test_same_names_without_current_geometry_fingerprints_never_form_repeat_batch(self):
        analysis, groups = fixture()
        for part in analysis["parts"]:
            part.pop("geometry_fingerprint")
        result = propose_assembly_candidates(analysis, groups)
        self.assertFalse(any(p["kind"] == "repeated_geometry_batch" for p in result["suggestions"]))

    def test_names_do_not_affect_proposal_identity_or_evidence(self):
        analysis, groups = fixture()
        first = propose_assembly_candidates(analysis, groups)
        for row in analysis["parts"]:
            row["name"] = "M6 screw mesh gaslift" if row["id"] == "p1" else "same name"
        for group in groups:
            group["label"] = "fastener assembly"
        changed = propose_assembly_candidates(analysis, groups)
        self.assertEqual(first["suggestions"], changed["suggestions"])

    def test_non_solid_inner_far_and_out_of_box_contacts_are_not_merge_evidence(self):
        changes = [("non_solid_contact", lambda a: a["parts"][1].update(solids=0)),
                   ("inner_solution", lambda a: a["contacts"][0].update(inner_solution=True)),
                   ("beyond_distance_threshold", lambda a: a["contacts"][0].update(distance_mm=2)),
                   ("out_of_source_bounds", lambda a: a["contacts"][0].update(point_a=[-100, 0, 0]))]
        for reason, edit in changes:
            analysis, groups = fixture()
            edit(analysis)
            result = propose_assembly_candidates(analysis, groups)
            self.assertGreater(result["diagnostics"]["skipped_contacts"].get(reason, 0), 0)
            self.assertFalse(any(p["kind"] == "measured_contact_pair" and p["group_ids"] == ["g0", "g1"] for p in result["suggestions"]))
            self.assertEqual(result["original_groups"], groups)

    def test_original_groups_and_every_occurrence_are_retained_without_input_mutation(self):
        analysis, groups = fixture()
        before = deepcopy((analysis, groups))
        result = propose_assembly_candidates(analysis, groups)
        self.assertEqual((analysis, groups), before)
        self.assertEqual(result["original_groups"], groups)
        self.assertEqual({pid for g in result["original_groups"] for pid in g["part_ids"]}, {row["id"] for row in analysis["parts"]})
        result["original_groups"][0]["part_ids"].append("not-in-source")
        self.assertEqual((analysis, groups), before)
        self.assertNotIn("hidden_part_ids", result)
        self.assertNotIn("excluded_part_ids", result)

    def test_every_suggestion_requires_review_and_template_mapping_recheck(self):
        result = propose_assembly_candidates(*fixture())
        self.assertFalse(result["automatically_applied"])
        for proposal in result["suggestions"]:
            self.assertEqual(proposal["status"], "proposed")
            self.assertTrue(proposal["requires_review"])
            self.assertFalse(proposal["automatically_applied"])
            self.assertFalse(proposal["packaging_assembly_verified"])
            self.assertFalse(proposal["assembly_order_verified"])
            self.assertTrue(proposal["may_change_step_count"])
            self.assertTrue(any("固定模板" in warning for warning in proposal["warnings"]))

    def test_input_permutation_and_contact_operand_order_keep_proposal_ids(self):
        analysis, groups = fixture()
        original = propose_assembly_candidates(analysis, groups)["suggestions"]
        analysis["parts"].reverse()
        analysis["contacts"].reverse()
        groups.reverse()
        for contact in analysis["contacts"]:
            contact["part_ids"].reverse()
            contact["point_a"], contact["point_b"] = contact["point_b"], contact["point_a"]
        self.assertEqual(original, propose_assembly_candidates(analysis, groups)["suggestions"])

    def test_new_source_revision_never_keeps_old_suggestion_binding(self):
        analysis, groups = fixture()
        first = propose_assembly_candidates(analysis, groups)
        analysis["source_sha256"] = "source-b"
        revised = propose_assembly_candidates(analysis, groups)
        self.assertTrue({p["id"] for p in first["suggestions"]}.isdisjoint({p["id"] for p in revised["suggestions"]}))
        self.assertTrue(all(p["source_sha256"] == "source-b" for p in revised["suggestions"]))

    def test_analysis_incompleteness_is_exposed_without_treating_missing_edges_as_no_connection(self):
        result = propose_assembly_candidates(*fixture())
        self.assertEqual(result["diagnostics"]["candidate_pairs"], 12)
        self.assertEqual(result["diagnostics"]["tested_pairs"], 3)
        self.assertEqual(result["diagnostics"]["untested_pairs"], 9)
        self.assertEqual(result["diagnostics"]["failed_pairs"], 1)
        self.assertTrue(any("不能证明不存在连接" in warning for warning in result["warnings"]))

    def test_invalid_or_dropped_source_groups_fail_closed(self):
        for edit in (lambda groups: groups.pop(),
                     lambda groups: groups[0]["part_ids"].append("p1"),
                     lambda groups: groups[0].update(part_ids=["unknown"]),
                     lambda groups: groups[0].update(id="g1")):
            analysis, groups = fixture()
            edit(groups)
            with self.assertRaises(ValueError):
                propose_assembly_candidates(analysis, groups)
        for kwargs in ({"max_contact_mm": -1}, {"max_contact_mm": float("nan")}, {"max_contact_mm": True},
                       {"max_cluster_groups": 1}, {"max_cluster_groups": True}):
            with self.assertRaises(ValueError):
                propose_assembly_candidates(*fixture(), **kwargs)


if __name__ == "__main__":
    unittest.main()
