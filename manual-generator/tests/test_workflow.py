"""Integration checks for review freshness, source changes, and usable vector exports."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manual import draft_project, load_project, render_project, save_plan, confirm_project, current_manifest, read_json
from generic_render import plan_digest

CAD_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("cadquery", "fitz"))


@unittest.skipUnless(CAD_AVAILABLE, "CadQuery and PyMuPDF required for end-to-end workflow")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        import cadquery as cq
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "assembly.step"
        self.project = self.root / "project"
        assembly = cq.Assembly(name="product")
        assembly.add(cq.Workplane("XY").box(20, 20, 10), name="base")
        assembly.add(cq.Workplane("XY").box(8, 8, 8), name="cap", loc=cq.Location(cq.Vector(0, 0, 9)))
        assembly.export(str(self.source))
        draft_project(self.source, self.project, "装配草案", 1.0, 20)
        render_project(self.project)

    def tearDown(self):
        self.temp.cleanup()

    def test_confirmation_binds_only_reviewed_current_assets(self):
        plan, _ = load_project(self.project)
        with self.assertRaisesRegex(ValueError, "Review each step"):
            confirm_project(self.project)
        before = read_json(self.project / "diagrams" / "manifest.json")
        # JSON.stringify in the browser emits 1 for 1.0 and 0 for -0.0.
        # A review-only save through that representation must retain flags.
        def browser_numbers(value):
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, dict):
                return {key: browser_numbers(child) for key, child in value.items()}
            if isinstance(value, list):
                return [browser_numbers(child) for child in value]
            return value
        plan = browser_numbers(plan)
        for step in plan["steps"]:
            step["reviewed"] = True
        save_plan(self.project, plan)
        self.assertTrue(all(step["reviewed"] for step in load_project(self.project)[0]["steps"]))
        current_manifest(self.project, plan)
        result = confirm_project(self.project, "包装同事")
        confirmed, _ = load_project(self.project)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(result["plan_sha256"], plan_digest(confirmed))
        self.assertEqual(before["plan_sha256"], result["plan_sha256"])
        # STEP-only proposals remain usable without inventing a final layout.
        self.assertFalse((self.project / "manual.pdf").exists())
        self.assertFalse((self.project / "pdf_export.json").exists())
        self.assertEqual(read_json(self.project / "instructions.json")["pdf_mode"], "template_not_bound")
        # A no-op save should preserve a valid packaging confirmation.
        save_plan(self.project, confirmed)
        self.assertEqual(load_project(self.project)[0]["status"], "confirmed")
        asset = self.project / "diagrams" / before["entries"][0]["svg_path"]
        asset.write_text(asset.read_text(encoding="utf-8") + "\n ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Changed diagram"):
            confirm_project(self.project)

    def test_content_edits_clear_review_and_archive_old_exports(self):
        plan, _ = load_project(self.project)
        for step in plan["steps"]:
            step["reviewed"] = True
        save_plan(self.project, plan)
        confirm_project(self.project)
        edited = deepcopy(load_project(self.project)[0])
        edited["steps"][0]["instruction"] = "确认该参考组的摆放，再按图示核对后续总成。"
        saved = save_plan(self.project, edited)
        self.assertEqual(saved["status"], "draft")
        self.assertTrue(all(not step["reviewed"] for step in saved["steps"]))
        for name in ("confirmation.json", "diagrams", "manual.pdf", "index.html", "instructions.json"):
            self.assertFalse((self.project / name).exists(), name)
        self.assertTrue(list((self.project / "history").rglob("index.html")))
        with self.assertRaisesRegex(ValueError, "No current diagrams"):
            confirm_project(self.project)

    def test_changed_source_or_cached_geometry_cannot_be_confirmed(self):
        original = self.source.read_bytes()
        self.source.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "input STEP has changed"):
            load_project(self.project)
        # Old cache may still be read as the old revision's comparison evidence.
        load_project(self.project, check_source=False)
        self.source.write_bytes(original)
        row = read_json(self.project / "inventory" / "assembly_parts.json")[0]
        brep = self.project / "inventory" / row["brep"]
        brep.write_bytes(brep.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "BREP cache"):
            load_project(self.project)

    def test_failed_regeneration_preserves_published_outputs(self):
        from unittest.mock import patch
        previous = (self.project / "index.html").read_bytes()
        with patch("review_output.export_preview", side_effect=ValueError("PDF export failed")):
            with self.assertRaisesRegex(ValueError, "PDF export failed"):
                render_project(self.project)
        self.assertEqual(previous, (self.project / "index.html").read_bytes())
        current_manifest(self.project, load_project(self.project)[0])

    def test_failed_confirmation_export_does_not_record_confirmation(self):
        from unittest.mock import patch
        plan, _ = load_project(self.project)
        for step in plan["steps"]:
            step["reviewed"] = True
        save_plan(self.project, plan)
        with patch("review_output.export_preview", side_effect=ValueError("PDF export failed")):
            with self.assertRaisesRegex(ValueError, "PDF export failed"):
                confirm_project(self.project)
        self.assertEqual(load_project(self.project)[0]["status"], "draft")
        self.assertFalse((self.project / "confirmation.json").exists())

    def test_retracting_review_refreshes_confirmed_pdf_label(self):
        plan, _ = load_project(self.project)
        for step in plan["steps"]:
            step["reviewed"] = True
        save_plan(self.project, plan)
        confirm_project(self.project)
        reviewed = load_project(self.project)[0]
        reviewed["steps"][0]["reviewed"] = False
        saved = save_plan(self.project, reviewed)
        self.assertEqual(saved["status"], "draft")
        self.assertFalse((self.project / "confirmation.json").exists())
        self.assertEqual(read_json(self.project / "instructions.json")["status"], "draft")
        self.assertFalse((self.project / "manual.pdf").exists())


if __name__ == "__main__":
    unittest.main()
