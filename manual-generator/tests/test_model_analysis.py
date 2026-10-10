"""Real STEP tests for occurrence identity, positioning and uncertain contacts."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_analysis
from model_analysis import analyze_inventory, import_model

HAS_CAD = importlib.util.find_spec("cadquery") is not None


@unittest.skipUnless(HAS_CAD, "CadQuery/OCP required for real STEP tests")
class ModelAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cadquery as cq
        cls.temporary = tempfile.TemporaryDirectory(prefix="manual-model-analysis-")
        cls.root = Path(cls.temporary.name)
        cls.step = cls.root / "repeated.stp"
        shared = cq.Workplane("XY").box(10, 4, 2).val()
        left = cq.Assembly(name="left_group")
        left.add(shared, name="repeat")
        right = cq.Assembly(name="right_group")
        right.add(shared, name="repeat")
        assembly = cq.Assembly(name="root")
        assembly.add(left, name="left", loc=cq.Location((0, 20, 0)))
        assembly.add(right, name="right", loc=cq.Location((11, 20, 0)))
        panel = cq.Face.makeFromWires(cq.Workplane("XY").rect(3, 3).val())
        assembly.add(panel, name="surface", loc=cq.Location((0, 100, 0)))
        assembly.export(str(cls.step), exportType="STEP")
        cls.inventory = cls.root / "inventory"
        cls.complete = import_model(cls.step, cls.inventory)
        cls.rows = json.loads((cls.inventory / "assembly_parts.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_repeated_names_are_distinct_positioned_occurrences(self):
        boxes = sorted((row for row in self.rows if row["solids"]), key=lambda row: row["bbox_min"][0])
        self.assertEqual(len(boxes), 2)
        self.assertEqual(boxes[0]["name"], boxes[1]["name"])
        self.assertNotEqual(boxes[0]["id"], boxes[1]["id"])
        self.assertNotEqual(boxes[0]["occurrence_path"], boxes[1]["occurrence_path"])
        self.assertNotEqual(boxes[0]["parent_occurrence"], boxes[1]["parent_occurrence"])
        self.assertEqual(boxes[0]["geometry_fingerprint"], boxes[1]["geometry_fingerprint"])
        self.assertAlmostEqual(boxes[0]["bbox_min"][0], -5, places=5)
        self.assertAlmostEqual(boxes[1]["bbox_min"][0], 6, places=5)
        self.assertAlmostEqual(boxes[0]["bbox_min"][1], 18, places=5)
        self.assertAlmostEqual(boxes[1]["bbox_min"][1], 18, places=5)
        self.assertAlmostEqual(boxes[1]["world_transform"][0][3], 11, places=5)
        self.assertAlmostEqual(boxes[1]["world_transform"][1][3], 20, places=5)
        self.assertAlmostEqual(boxes[0]["volume_mm3"], 80, places=5)
        self.assertEqual(self.complete["units"], "mm")

    def test_surfaces_preserved_without_product_exceptions(self):
        surfaces = [row for row in self.rows if not row["solids"]]
        self.assertEqual(len(surfaces), 1)
        self.assertAlmostEqual(surfaces[0]["area_mm2"], 9, places=5)
        self.assertTrue((self.inventory / surfaces[0]["brep"]).is_file())

    def test_real_distance_points_are_candidates(self):
        result = analyze_inventory(self.inventory, tolerance_mm=1.01, max_pairs=10)
        self.assertEqual(len(result["contacts"]), 1)
        contact = result["contacts"][0]
        self.assertEqual(contact["status"], "candidate")
        self.assertAlmostEqual(contact["distance_mm"], 1, places=5)
        self.assertAlmostEqual(abs(contact["point_a"][0] - contact["point_b"][0]), 1, places=5)
        self.assertEqual(result["tested_pair_count"], 1)
        self.assertEqual(result["completed_pair_count"], 1)
        self.assertEqual(result["timed_out_pair_count"], 0)
        self.assertEqual(len(result["untested_candidates"]), 0)
        below = analyze_inventory(self.inventory, tolerance_mm=0.5)
        self.assertEqual(below["contacts"], [])
        self.assertEqual(below["candidate_pair_count"], 0)

    def test_pair_budget_reports_untested_pairs(self):
        result = analyze_inventory(self.inventory, tolerance_mm=1.01, max_pairs=0)
        self.assertEqual(result["contacts"], [])
        self.assertEqual(result["tested_pair_count"], 0)
        self.assertEqual(len(result["untested_candidates"]), 1)
        self.assertEqual(result["untested_candidates"][0]["status"], "untested")

    def test_small_budget_covers_distinct_native_branch_relations(self):
        import cadquery as cq
        branch_step = self.root / "branches.stp"
        block = cq.Workplane("XY").box(10, 4, 2).val()
        assembly = cq.Assembly(name="wrapper")
        for number, x in enumerate((0, 11, 22)):
            group = cq.Assembly(name=f"group_{number}")
            group.add(block, name="first")
            group.add(block, name="second", loc=cq.Location((0, 0.5, 0)))
            assembly.add(group, name=f"branch_{number}", loc=cq.Location((x, 0, 0)))
        assembly.export(str(branch_step), exportType="STEP")
        branch_inventory = self.root / "branch_inventory"
        import_model(branch_step, branch_inventory)
        result = analyze_inventory(branch_inventory, tolerance_mm=12.01, max_pairs=3)
        cross = [entry for entry in result["branch_pair_coverage"] if entry["scope"] == "cross_branch"]
        internal = [entry for entry in result["branch_pair_coverage"] if entry["scope"] == "within_branch"]
        self.assertEqual(len(cross), 3)
        self.assertTrue(all(entry["tested_pairs"] == 1 for entry in cross))
        self.assertTrue(all(entry["tested_pairs"] == 0 for entry in internal))
        self.assertEqual(result["tested_pair_count"], 3)
        self.assertGreater(len(result["untested_candidates"]), 0)

    def test_nonempty_inventory_cannot_reuse_identity(self):
        with self.assertRaises(FileExistsError):
            import_model(self.step, self.inventory)
        before = (self.inventory / "import_complete.json").read_bytes()
        changed = self.root / "changed.stp"
        changed.write_bytes(self.step.read_bytes() + b"\n")
        with self.assertRaises(FileExistsError):
            import_model(changed, self.inventory)
        self.assertEqual(before, (self.inventory / "import_complete.json").read_bytes())

    def test_fresh_source_revision_receives_new_identities(self):
        changed = self.root / "fresh_revision.stp"
        changed.write_bytes(self.step.read_bytes() + b"\n")
        revised_inventory = self.root / "fresh_revision_inventory"
        revised_complete = import_model(changed, revised_inventory)
        revised = json.loads((revised_inventory / "assembly_parts.json").read_text(encoding="utf-8"))
        self.assertNotEqual(self.complete["source_sha256"], revised_complete["source_sha256"])
        self.assertTrue(set(row["id"] for row in self.rows).isdisjoint(row["id"] for row in revised))

    def test_nested_rotation_remains_in_world_geometry(self):
        import cadquery as cq
        rotated_step = self.root / "rotated.stp"
        inner = cq.Assembly(name="inner")
        inner.add(cq.Workplane("XY").box(10, 4, 2).val(), name="block")
        outer = cq.Assembly(name="outer")
        outer.add(inner, name="rotated", loc=cq.Location((10, 20, 0), (0, 0, 1), 90))
        outer.export(str(rotated_step), exportType="STEP")
        rotated_inventory = self.root / "rotated_inventory"
        import_model(rotated_step, rotated_inventory)
        row = json.loads((rotated_inventory / "assembly_parts.json").read_text(encoding="utf-8"))[0]
        for actual, expected in zip(row["bbox_size"], (4, 10, 2)):
            self.assertAlmostEqual(actual, expected, places=5)
        for actual, expected in zip(row["bbox_min"], (8, 15, -1)):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_centimetre_source_is_normalized_to_millimetres(self):
        import cadquery as cq
        units_step = self.root / "centimetres.stp"
        cq.exporters.export(cq.Workplane("XY").box(10, 4, 2).val(), str(units_step), exportType="STEP")
        content = units_step.read_text(encoding="utf-8")
        self.assertIn("SI_UNIT(.MILLI.,.METRE.)", content)
        units_step.write_text(content.replace("SI_UNIT(.MILLI.,.METRE.)", "SI_UNIT(.CENTI.,.METRE.)"), encoding="utf-8")
        units_inventory = self.root / "centimetre_inventory"
        import_model(units_step, units_inventory)
        row = json.loads((units_inventory / "assembly_parts.json").read_text(encoding="utf-8"))[0]
        for actual, expected in zip(row["bbox_size"], (100, 40, 20)):
            self.assertAlmostEqual(actual, expected, places=5)
        self.assertAlmostEqual(row["volume_mm3"], 80000, places=4)

    def test_modified_cache_is_rejected(self):
        brep = self.inventory / self.rows[0]["brep"]
        original = brep.read_bytes()
        try:
            brep.write_bytes(original + b"\n")
            with self.assertRaisesRegex(ValueError, "BREP cache differs"):
                analyze_inventory(self.inventory, max_pairs=0)
        finally:
            brep.write_bytes(original)

    def test_failed_import_does_not_publish_completion(self):
        bad = self.root / "bad.stp"
        bad.write_text("not a STEP file", encoding="utf-8")
        target = self.root / "failed_inventory"
        with self.assertRaises(RuntimeError):
            import_model(bad, target)
        self.assertFalse(target.exists())

    def test_stalled_native_worker_is_killed_and_timeout_is_reported(self):
        fixture = self.root / "stalled_worker.py"
        fixture.write_text(
            "import sys, time\nprint('{\"ready\": true}', flush=True)\n"
            "for line in sys.stdin:\n    time.sleep(30)\n", encoding="utf-8")
        real_popen = model_analysis.subprocess.Popen
        children = []

        def stalled_worker(command, **kwargs):
            child = real_popen([sys.executable, str(fixture)], **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with mock.patch.object(model_analysis.subprocess, "Popen", side_effect=stalled_worker):
            result = analyze_inventory(self.inventory, tolerance_mm=1.01, max_pairs=1,
                                       pair_timeout_seconds=0.15)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["contacts"], [])
        self.assertEqual(result["tested_pair_count"], 1)
        self.assertEqual(result["completed_pair_count"], 0)
        self.assertEqual(result["timed_out_pair_count"], 1)
        self.assertEqual(result["failed_pairs"][0]["failure_type"], "timeout")
        self.assertIn("timed out", result["failed_pairs"][0]["reason"])
        self.assertEqual(result["untested_candidates"], [])
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_native_worker_restarts_after_timeout(self):
        fixture = self.root / "sometimes_stalled_worker.py"
        fixture.write_text(
            "import sys, time\nprint('{\"ready\": true}', flush=True)\n"
            "for line in sys.stdin:\n    time.sleep(30)\n", encoding="utf-8")
        real_popen = model_analysis.subprocess.Popen
        children = []

        def first_stalls(command, **kwargs):
            child = real_popen([sys.executable, str(fixture)] if not children else command, **kwargs)
            children.append(child)
            return child

        worker = model_analysis._ContactWorker(0.15)
        breps = [self.inventory / row["brep"] for row in self.rows if row["solids"]]
        try:
            with mock.patch.object(model_analysis.subprocess, "Popen", side_effect=first_stalls):
                with self.assertRaises(TimeoutError):
                    worker.measure(*breps)
                worker.timeout = 10
                result = worker.measure(*breps)
                self.assertAlmostEqual(result["distance_mm"], 1, places=5)
        finally:
            worker.close()
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.poll() is not None for child in children))


class ArgumentValidationTests(unittest.TestCase):
    def test_invalid_tolerance_and_budget_are_rejected(self):
        for tolerance in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                analyze_inventory(Path("missing"), tolerance)
        for budget in (-1, 1.5, True):
            with self.assertRaises(ValueError):
                analyze_inventory(Path("missing"), max_pairs=budget)
        for timeout in (-1, 0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                analyze_inventory(Path("missing"), pair_timeout_seconds=timeout)


if __name__ == "__main__":
    unittest.main()
