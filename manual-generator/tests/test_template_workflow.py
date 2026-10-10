"""Template binding and editor guards; no model import or figure generation."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manual
from generic_render import plan_digest
from review_output import html_page, is_current_pdf


class TemplateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.template = self.project / "provided-template.stub"
        self.plan = {
            "source": {"sha256": "a"*64}, "product": {"title": "Example"}, "status": "draft",
            "groups": [], "style": {"camera": [1,1,1]},
            "steps": [{"id": "second", "title": "two", "instruction": "second instruction", "camera": [1,1,1], "reviewed": True},
                      {"id": "first", "title": "one", "instruction": "first instruction", "camera": [1,1,1], "reviewed": True}],
        }
        self.config = {"source_sha256": "a"*64, "sha256": "b"*64, "figure_slots": [{"step_id": "first"}, {"step_id": "second"}]}
        self.manifest = {"plan_sha256": plan_digest(self.plan), "entries": [{"step_id": s["id"], "svg_sha256": "existing-bytes"} for s in self.plan["steps"]]}
        (self.project / "steps.json").write_text(json.dumps(self.plan), encoding="utf-8")
        (self.project / "diagrams").mkdir()
        manual.write_json(self.project / "diagrams/manifest.json", self.manifest)

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, config=None):
        with patch("manual.load_project", return_value=(self.plan, {})), \
             patch("manual.current_manifest", return_value=self.manifest), \
             patch("draft_planner.validate_plan"), \
             patch("template_pdf.validate_template_source", return_value=self.template), \
             patch("template_pdf.validate_template_regions"), \
             patch("template_translation.discover_template_copy", return_value={"entries": [], "text_regions": []}), \
             patch("fitz.open", return_value=MagicMock()), \
             patch("manual.render_project", side_effect=AssertionError("must not render")), \
             patch("review_output.export_pdf", side_effect=AssertionError("must not export")):
            return manual.bind_template_project(self.project, self.template, config or self.config)

    def test_binding_uses_semantic_ids_and_retains_recipe_without_rendering(self):
        original = deepcopy(self.plan)
        result = self.bind()
        self.assertEqual([s["id"] for s in result["steps"]], ["first", "second"])
        self.assertEqual(manual._diagram_recipe(original), manual._diagram_recipe(result))
        self.assertFalse(any(s["reviewed"] for s in result["steps"]))
        updated = manual.read_json(self.project / "diagrams/manifest.json")
        self.assertEqual(updated["plan_sha256"], plan_digest(result))
        self.assertEqual([e["svg_sha256"] for e in updated["entries"]], ["existing-bytes"]*2)
        self.assertTrue(manual.read_json(self.project / "template_binding.json")["figure_recipes_unchanged"])
        self.assertFalse((self.project / "manual.pdf").exists())

    def test_other_source_template_does_not_mutate_project(self):
        wrong = deepcopy(self.config)
        wrong["source_sha256"] = "c"*64
        before = (self.project / "steps.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "different STEP"):
            self.bind(wrong)
        self.assertEqual(before, (self.project / "steps.json").read_bytes())

    def test_missing_or_duplicate_slot_binding_is_rejected(self):
        bad = deepcopy(self.config)
        bad["figure_slots"] = [{"step_id": "first"}]*2
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.bind(bad)

    def test_editor_cannot_unbind_template_or_silently_change_consumer_text(self):
        old = self.bind()
        for mutate in (lambda p: p.pop("pdf_template"),
                       lambda p: p["steps"][0].update(instruction="different"),
                       lambda p: p["steps"].reverse()):
            candidate = deepcopy(old)
            mutate(candidate)
            with patch("manual.load_project", return_value=(old, {})):
                with self.assertRaises(ValueError):
                    manual.save_plan(self.project, candidate)

    def test_view_edit_still_invalidates_old_diagram_and_confirmation(self):
        old = self.bind()
        edited = deepcopy(old)
        edited["steps"][0]["camera"] = [2,1,1]
        with patch("manual.load_project", return_value=(old, {})), patch("draft_planner.validate_plan"):
            result = manual.save_plan(self.project, edited)
        self.assertFalse((self.project / "diagrams").exists())
        self.assertFalse(any(s["reviewed"] for s in result["steps"]))

    def test_pdf_link_requires_current_template_receipt(self):
        self.assertFalse(is_current_pdf(self.project, self.plan))
        page = html_page(self.plan, self.manifest, editable=True)
        self.assertIn('"pdf_ready": false', page)
        self.assertIn("用已有图套入原模板", page)

    def test_receipt_rejects_changed_pdf_or_source_without_creating_a_pdf(self):
        plan = self.bind()
        output_bytes = b"mock current output bytes"
        receipt = {"mode": "original_template_figure_replacement", "source_sha256": plan["source"]["sha256"],
                   "plan_sha256": plan_digest(plan), "template_sha256": plan["pdf_template"]["sha256"],
                   "pdf_sha256": hashlib.sha256(output_bytes).hexdigest()}
        with patch("template_pdf.validate_template_source"), patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_text", return_value=json.dumps(receipt)), \
             patch.object(Path, "read_bytes", return_value=output_bytes):
            self.assertTrue(is_current_pdf(self.project, plan))
            receipt["source_sha256"] = "c"*64
            with patch.object(Path, "read_text", return_value=json.dumps(receipt)):
                self.assertFalse(is_current_pdf(self.project, plan))
            receipt["source_sha256"] = plan["source"]["sha256"]
            with patch.object(Path, "read_bytes", return_value=b"tampered output"):
                self.assertFalse(is_current_pdf(self.project, plan))


if __name__ == "__main__":
    unittest.main()
