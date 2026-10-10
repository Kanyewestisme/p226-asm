"""Template validation / export wiring; all authoring and file output is mocked.

These checks never create a PDF, SVG or PNG and never call the CAD renderer.
Only rectangle arithmetic and synthetic read-only page metadata are real.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from template_pdf import export_template_pdf, validate_template_binding, validate_template_regions, validate_template_source


TEMPLATE_BYTES = b"mocked existing PDF bytes; never written"
TEMPLATE_SHA = hashlib.sha256(TEMPLATE_BYTES).hexdigest()


class ReadOnlyPage:
    def __init__(self, text_rect=(20, 20, 80, 35)):
        import fitz
        self.rect = fitz.Rect(0, 0, 400, 300)
        self.text_rect = text_rect
        self.draw_rect = Mock()
        self.insert_text = Mock()

    def get_text(self, kind):
        assert kind == "dict"
        return {"blocks": [{"lines": [{"spans": [{"text": "original consumer wording", "bbox": self.text_rect}]}]}]}


class ReadOnlyDocument:
    def __init__(self):
        self.pages = [ReadOnlyPage(), ReadOnlyPage()]
        self.is_pdf = True
        self.needs_pass = False
        self.save = Mock()
        self.set_metadata = Mock()

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TemplatePdfValidationTests(unittest.TestCase):
    def setUp(self):
        self.project = Path.cwd().resolve() / "existing-mock-project"
        self.template = Path.cwd().resolve() / "existing-mock-template.pdf"
        self.plan = {"source": {"sha256": "a"*64}, "manual_document": {"title": "原模板文字"},
                     "steps": [{"id": "one", "title": "原步骤一", "instruction": "原安装文字一", "consumer": {"parts_text": "既有配件", "caution": ["原注意事项"]}},
                               {"id": "two", "title": "原步骤二", "instruction": "原安装文字二"}]}
        self.plan["pdf_template"] = {"path": str(self.template), "sha256": TEMPLATE_SHA, "source_sha256": "a"*64, "page_count": 2,
            "document_binding": deepcopy(self.plan["manual_document"]),
            "step_bindings": [{"step_id": step["id"], "title": step["title"], "instruction": step["instruction"], **({"consumer": deepcopy(step["consumer"])} if "consumer" in step else {})} for step in self.plan["steps"]],
            "figure_slots": [{"step_id": "one", "page": 2, "rect": [100, 50, 180, 150], "safe_white_masks_pt": [[102, 52, 178, 148]]},
                             {"step_id": "two", "page": 2, "rect": [210, 50, 290, 150], "safe_white_masks_pt": [[212, 52, 288, 148]]}]}
        self.doc = ReadOnlyDocument()

    def test_binding_is_pure_and_unselected_drafts_are_allowed(self):
        before = deepcopy(self.plan)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("binding must not read files")), patch("fitz.open", side_effect=AssertionError("binding must not open PDFs")):
            self.assertIs(validate_template_binding(self.plan), self.plan["pdf_template"])
            self.assertIsNone(validate_template_binding({"source": {"sha256": "a"*64}}))
        self.assertEqual(self.plan, before)

    def test_source_step_order_titles_instructions_and_consumers_are_bound(self):
        edits = [(lambda p: p["source"].update(sha256="b"*64), "source SHA256"),
                 (lambda p: p["steps"].reverse(), "step IDs/order"),
                 (lambda p: p["steps"][0].update(id="new"), "step IDs/order"),
                 (lambda p: p["steps"][0].update(title="用户修改标题"), "title changed"),
                 (lambda p: p["steps"][0].update(instruction="用户修改说明"), "instruction changed"),
                 (lambda p: p["steps"][0]["consumer"].update(parts_text="用户修改配件"), "consumer text changed"),
                 (lambda p: p["steps"][0]["consumer"].update(caution=["用户修改注意事项"]), "consumer text changed"),
                 (lambda p: p["manual_document"].update(title="用户修改原稿"), "document text changed")]
        for edit, error in edits:
            with self.subTest(error=error):
                plan = deepcopy(self.plan)
                edit(plan)
                with self.assertRaisesRegex(ValueError, error):
                    validate_template_binding(plan)

    def test_minimal_bindings_and_review_warnings_remain_editable(self):
        self.plan["steps"][0].pop("consumer")
        self.plan["pdf_template"]["step_bindings"][0].pop("consumer")
        self.plan["steps"][0]["warnings"] = ["新的工程核对事项"]
        validate_template_binding(self.plan)
        self.plan["steps"][0]["consumer"] = {"caution": ["未绑定的新文字"]}
        with self.assertRaisesRegex(ValueError, "consumer text changed or is not bound"):
            validate_template_binding(self.plan)

    def test_absolute_template_path_and_unique_slot_mapping_are_required(self):
        plan = deepcopy(self.plan)
        plan["pdf_template"]["path"] = "relative-template.pdf"
        with self.assertRaisesRegex(ValueError, "absolute path"):
            validate_template_binding(plan)
        plan = deepcopy(self.plan)
        plan["pdf_template"]["figure_slots"][1]["step_id"] = "one"
        with self.assertRaisesRegex(ValueError, "exactly one region"):
            validate_template_binding(plan)

    def test_selected_template_hash_and_no_source_overwrite(self):
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "read_bytes", return_value=TEMPLATE_BYTES):
            self.assertEqual(validate_template_source(self.project, self.plan), self.template)
            bad = deepcopy(self.plan)
            bad["pdf_template"]["sha256"] = "b"*64
            with self.assertRaisesRegex(ValueError, "Original template SHA256 differs"):
                validate_template_source(self.project, bad)
            bad = deepcopy(self.plan)
            bad["pdf_template"]["path"] = str(self.project / "manual.pdf")
            with self.assertRaisesRegex(ValueError, "output must differ"):
                validate_template_source(self.project, bad)

    def test_regions_keep_source_pages_and_text_without_default_notes(self):
        regions, notes = validate_template_regions(self.plan, self.doc)
        self.assertEqual([region["step_id"] for region in regions], ["one", "two"])
        self.assertIsNone(notes)
        self.assertEqual(len(self.doc), 2)
        for page in self.doc.pages:
            page.draw_rect.assert_not_called()
            page.insert_text.assert_not_called()

    def test_invalid_page_bounds_text_and_cross_figure_overlap_are_rejected(self):
        edits = [(lambda c: c["figure_slots"][0].update(page=3), "existing template page"),
                 (lambda c: c["figure_slots"][0].update(rect=[-1, 50, 180, 150]), "within its original template page"),
                 (lambda c: c["figure_slots"][0].update(rect=[20, 20, 80, 35]), "overlaps original template text"),
                 (lambda c: c["figure_slots"][0].update(safe_white_masks_pt=[[20, 20, 80, 35]]), "overlaps original template text"),
                 (lambda c: c["figure_slots"][1].update(rect=[160, 50, 230, 150]), "overlaps another step"),
                 (lambda c: c.update(page_count=3), "page count differs")]
        for edit, error in edits:
            with self.subTest(error=error):
                plan = deepcopy(self.plan)
                edit(plan["pdf_template"])
                with self.assertRaisesRegex(ValueError, error):
                    validate_template_regions(plan, self.doc)

    def test_only_explicit_notes_are_parsed_and_overflow_is_rejected(self):
        self.plan["pdf_template"]["notes"] = {"rect": [100, 200, 380, 270], "lines": [{"text": "原先配置的审核说明", "size": 9}]}
        _, notes = validate_template_regions(self.plan, self.doc)
        self.assertEqual(notes["lines"][0], {"text": "原先配置的审核说明", "size": 9, "baseline": 212})
        self.plan["pdf_template"]["notes"]["lines"][0]["text"] = "过长的配置文字"*100
        with self.assertRaisesRegex(ValueError, "PDF content overflow"):
            validate_template_regions(self.plan, self.doc)

    def test_export_wiring_only_paints_mapped_figures_and_keeps_metadata(self):
        assets = {sid: {"main": Path(f"{sid}.svg"), "focus": Path("ignored-focus.svg")} for sid in ("one", "two")}
        virtual_temp = str(self.project / "mock-output.tmp.pdf")
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "read_bytes", return_value=TEMPLATE_BYTES), \
             patch("template_pdf.editable_template", return_value=self.doc), patch("template_pdf._current_assets", return_value=assets) as current, \
             patch("template_pdf._vector") as vector, patch("template_pdf.tempfile.mkstemp", return_value=(987, virtual_temp)), \
             patch("template_pdf.os.close"), patch.object(Path, "replace") as replace, patch.object(Path, "unlink"):
            export_template_pdf(self.project, self.plan, {"verified": "by current asset validator"})
        current.assert_called_once()
        self.assertEqual(vector.call_count, 2)
        self.doc.pages[0].draw_rect.assert_not_called()
        self.doc.pages[0].insert_text.assert_not_called()
        self.assertEqual(self.doc.pages[1].draw_rect.call_count, 2)
        self.doc.pages[1].insert_text.assert_not_called()
        self.doc.set_metadata.assert_not_called()
        self.doc.save.assert_called_once()
        replace.assert_called_once_with(self.project / "manual.pdf")

    def test_export_asset_failure_does_not_start_any_output(self):
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "read_bytes", return_value=TEMPLATE_BYTES), \
             patch("template_pdf._current_assets", side_effect=ValueError("Changed diagram")), \
             patch("fitz.open") as open_doc, patch("template_pdf.tempfile.mkstemp") as temporary:
            with self.assertRaisesRegex(ValueError, "Changed diagram"):
                export_template_pdf(self.project, self.plan, {})
        open_doc.assert_not_called()
        temporary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
