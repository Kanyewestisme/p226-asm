"""Existing illustration profiles can apply only to the exact inventoried source."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from source_profile import apply_source_profile
from test_group_edits import fixture


class SourceProfileTests(unittest.TestCase):
    def setUp(self):
        self.analysis, self.current = fixture()
        def scene(sid, assembled, moving):
            return {"id": sid, "title": sid, "instruction": "核对图示与包装总成。",
                    "assembled_groups": assembled, "moving_groups": moving,
                    "camera": [1, .7, 1], "up": [0, 1, 0]}
        self.profile = {
            "source_sha256": self.analysis["source_sha256"], "style": self.current["style"],
            "groups": [{"id": "first", "label": "总成一", "indices": [0, 1]},
                       {"id": "second", "label": "总成二", "indices": [2, 3]}],
            "excluded_indices": [1], "exclusion_reason": "已选择的辅助几何",
            "steps": [scene("reference", ["first"], []), scene("second-view", [], ["second"]),
                      scene("reposition-first", ["second"], ["first"]),
                      scene("overview", ["first", "second"], [])],
        }

    def test_explicit_scenes_preserve_all_instances_and_do_not_change_inputs(self):
        snapshot = deepcopy((self.current, self.analysis, self.profile))
        result = apply_source_profile(self.current, self.analysis, self.profile)
        self.assertEqual(result["sequence_mode"], "configured")
        self.assertEqual(result["excluded_part_ids"], ["part-1"])
        self.assertEqual(sorted(p for g in result["groups"] for p in g["part_ids"]),
                         sorted(p["id"] for p in self.analysis["parts"]))
        self.assertTrue(all(not step["reviewed"] for step in result["steps"]))
        self.assertEqual((self.current, self.analysis, self.profile), snapshot)

    def test_stale_profiles_unknown_indices_and_dropped_geometry_are_rejected(self):
        for edit in (lambda p: p.update(source_sha256="new-source"),
                     lambda p: p["groups"][0].update(indices=[0, 99]),
                     lambda p: p["groups"][0].update(indices=[0]),
                     lambda p: p.update(excluded_indices=[1, 99]),
                     lambda p: p.update(exclusion_reason="")):
            candidate = deepcopy(self.profile)
            edit(candidate)
            with self.assertRaises(ValueError):
                apply_source_profile(self.current, self.analysis, candidate)

    def test_source_bound_detail_indices_resolve_only_to_visible_parts(self):
        profile = deepcopy(self.profile)
        profile["steps"][0]["focus_indices"] = [0]
        result = apply_source_profile(self.current, self.analysis, profile)
        self.assertEqual(result["steps"][0]["focus_part_ids"], ["part-0"])
        self.assertNotIn("focus_indices", result["steps"][0])
        for indices in (None, [True], [99], [1]):
            candidate = deepcopy(profile)
            candidate["steps"][0]["focus_indices"] = indices
            with self.assertRaises(ValueError):
                apply_source_profile(self.current, self.analysis, candidate)


if __name__ == "__main__":
    unittest.main()
