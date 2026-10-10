"""Verify multilingual fixed-template output using a tiny synthetic two-page PDF.

No STEP is imported or rendered. The current-project and geometry-asset lookup
are mocked; source text selection, PDF calibration, drawing preservation and
publication are real. All generated files live in an automatically removed
temporary directory.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from book_pdf import _vector
from generic_render import plan_digest
from manual_languages import read_document, save_translations, _numbers, _protected_tokens
from preview_service import _archive_and_publish
from template_pdf import validate_template_binding
from template_translation import export_language_pdf, is_current_language_pdf


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TemplateTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.template = self.project / "provided-two-page-template.pdf"
        self.plan = {"source": {"sha256": "a" * 64}, "product": {"title": "Synthetic assembly manual"},
                     "steps": [], "groups": [], "warnings": [], "status": "draft"}
        self.assets, regions, slots, copies = {}, [], [], []
        with fitz.open() as doc:
            cover = doc.new_page(width=600, height=420)
            cover.draw_rect(fitz.Rect(20, 20, 580, 400), color=(0.2, 0.3, 0.7), fill=(0.94, 0.96, 1))
            cover.insert_text((45, 70), "UNCHANGED COVER AND BRAND", fontsize=16)
            cover.draw_circle((480, 320), 45, color=(0.1, 0.6, 0.3), width=2)
            page = doc.new_page(width=600, height=420)
            page.insert_text((30, 35), "UNCHANGED ORIGINAL HEADER", fontsize=11)
            page.draw_rect(fitz.Rect(25, 60, 575, 398), color=(0.4, 0.4, 0.4), fill=(0.85, 0.85, 0.85), width=0.7)
            for index in range(7):
                number = index + 1
                sid, key = f"assembly-{number}", f"document.step-{number}"
                x, y = 40 + (index % 3) * 180, 78 + (index // 3) * 105
                figure = [x, y, x + 140, y + 50]
                text_rect = [x, y + 57, x + 140, y + 86]
                source = f"原文说明{number}"
                page.insert_text((x + 2, y + 69), source, fontsize=8, fontname="china-s")
                # This vector intentionally intersects the text redaction box;
                # glyph-only replacement must preserve it and the grey card.
                page.draw_line((x + 10, y + 81), (x + 130, y + 81), color=(0.05, 0.25, 0.65), width=0.8)
                page.draw_line((x + 20, y + 10), (x + 110, y + 40), color=(0.2, 0.5, 0.2), width=1)
                self.plan["steps"].append({"id": sid, "title": f"Action {number}", "instruction": source,
                                           "reviewed": False, "warnings": ["Engineering hypothesis, not consumer copy"]})
                copies.append({"key": key, "text": source, "scope": "consumer", "kind": "template_text"})
                regions.append({"key": key, "page": 2, "rect": text_rect, "source_rects": [text_rect],
                                "source_text": source, "fontsize": 8, "minimum_fontsize": 6,
                                "color": [0, 0, 0], "align": 0})
                slots.append({"step_id": sid, "page": 2, "rect": figure, "safe_white_masks_pt": [figure]})
                svg = self.project / f"{sid}.svg"
                svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60"><path d="M10 10L90 50M90 10L10 50" fill="none" stroke="#9b2222" stroke-width="2"/></svg>', encoding="utf-8")
                self.assets[sid] = {"main": svg}
            doc.save(self.template)
        self.plan["steps"].append({"id": "inspection-overview", "title": "Source-complete inspection overview",
                                   "instruction": "Keep all current source geometry available for inspection.",
                                   "include_in_manual": False, "kind": "overview", "warnings": [], "reviewed": False})
        self.plan["document_copy"] = {"entries": copies}
        self.plan["pdf_template"] = {"path": str(self.template), "sha256": sha(self.template),
                                     "source_sha256": self.plan["source"]["sha256"], "page_count": 2,
                                     "figure_slots": slots, "text_regions": regions,
                                     "step_bindings": [{"step_id": step["id"], "title": step["title"],
                                                        "instruction": step["instruction"]} for step in self.plan["steps"][:7]]}
        self.write_plan()
        self.template_bytes = self.template.read_bytes()
        # Language output may not overwrite this existing Chinese output.
        (self.project / "manual.pdf").write_bytes(b"existing Chinese PDF must remain untouched")
        document = read_document(self.project)
        edits = []
        for index, row in enumerate([row for row in document["entries"] if row["key"].startswith("document.step-")], 1):
            edits.append({"key": row["key"], "locale": "en", "text": f"Install part {index}.",
                          "status": "reviewed", "source_sha256": row["source_sha256"]})
        save_translations(self.project, {"source_sha256": document["source_sha256"], "base_sha256": document["base_sha256"],
                                         "locales": ["en"], "translations": edits})
        self.patches = [patch("manual.load_project", side_effect=lambda project: (deepcopy(self.plan), {})),
                        patch("manual.current_manifest", side_effect=self.manifest),
                        patch("book_pdf._current_assets", return_value=self.assets)]
        for active in self.patches:
            active.start()

    def tearDown(self):
        for active in reversed(self.patches):
            active.stop()
        self.temporary.cleanup()

    def write_plan(self):
        (self.project / "steps.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")

    def manifest(self, project, plan):
        return {"source_sha256": plan["source"]["sha256"], "plan_sha256": plan_digest(plan), "complete": True,
                "entries": [{"step_id": step["id"]} for step in plan["steps"]]}

    def edit_translation(self, key="document.step-1", *, text=None, status="reviewed"):
        doc = read_document(self.project)
        row = next(row for row in doc["entries"] if row["key"] == key)
        value = row["translations"]["en"]["text"] if text is None else text
        return save_translations(self.project, {"source_sha256": doc["source_sha256"], "base_sha256": doc["base_sha256"],
            "translations": [{"key": key, "locale": "en", "text": value, "status": status, "source_sha256": row["source_sha256"]}]})

    def test_text_replacement_preserves_two_pages_cover_grey_background_and_source_vectors(self):
        chinese_before = (self.project / "manual.pdf").read_bytes()
        receipt = export_language_pdf(self.project, "en")
        self.assertFalse(receipt["draft"])
        self.assertEqual(self.template.read_bytes(), self.template_bytes)
        self.assertEqual((self.project / "manual.pdf").read_bytes(), chinese_before)
        with fitz.open(self.template) as original, fitz.open(self.project / receipt["filename"]) as result:
            self.assertEqual(len(result), 2)
            self.assertEqual(list(result[0].rect), list(original[0].rect))
            self.assertEqual(result[0].get_pixmap(alpha=False).samples, original[0].get_pixmap(alpha=False).samples)
            text = result[1].get_text()
            self.assertNotIn("原文说明", text)
            self.assertIn("UNCHANGED ORIGINAL HEADER", text)
            self.assertEqual(sum(f"Install part {number}." in text for number in range(1, 8)), 7)
            before, after = original[1].get_pixmap(alpha=False), result[1].get_pixmap(alpha=False)
            # A source-grey pixel inside the replaced text box remains grey.
            point = 155, 160
            self.assertEqual(before.pixel(*point), after.pixel(*point))
            self.assertTrue(all(200 <= channel <= 225 for channel in after.pixel(*point)))
            original_blue = [drawing for drawing in original[1].get_drawings() if drawing["color"] and drawing["color"][2] > 0.6 and drawing["color"][0] < 0.1]
            result_blue = [drawing for drawing in result[1].get_drawings() if drawing["color"] and drawing["color"][2] > 0.6 and drawing["color"][0] < 0.1]
            self.assertEqual(len(original_blue), 7)
            self.assertEqual(len(result_blue), len(original_blue))
            self.assertEqual([list(drawing["rect"]) for drawing in result_blue], [list(drawing["rect"]) for drawing in original_blue])
        self.assertTrue(is_current_language_pdf(self.project, "en"))

    def test_seven_printed_steps_and_separate_source_overview_bind_without_printing_overview(self):
        validate_template_binding(self.plan)
        with patch("book_pdf._vector", wraps=_vector) as vector:
            receipt = export_language_pdf(self.project, "en")
        self.assertEqual(vector.call_count, 7)
        self.assertEqual({call.args[2].name for call in vector.call_args_list}, {f"assembly-{number}.svg" for number in range(1, 8)})
        self.assertEqual(len(receipt["regions"]), 7)
        self.assertEqual(len(self.plan["steps"]), 8)
        with fitz.open(self.project / receipt["filename"]) as result:
            self.assertNotIn("inspection overview", result[1].get_text())

    def test_missing_translation_is_rejected_before_overwriting_an_existing_pdf(self):
        export_language_pdf(self.project, "en")
        before = (self.project / "manual-en.pdf").read_bytes()
        self.edit_translation(text="")
        with self.assertRaisesRegex(ValueError, "缺失"):
            export_language_pdf(self.project, "en")
        self.assertEqual((self.project / "manual-en.pdf").read_bytes(), before)

    def test_unreviewed_copy_requires_explicit_draft_export_and_receipt_marks_draft(self):
        self.edit_translation(status="proposed")
        with self.assertRaisesRegex(ValueError, "未确认"):
            export_language_pdf(self.project, "en")
        self.assertFalse((self.project / "manual-en.pdf").exists())
        receipt = export_language_pdf(self.project, "en", require_reviewed=False)
        self.assertTrue(receipt["draft"])
        self.assertTrue(is_current_language_pdf(self.project, "en"))

    def test_overflow_preserves_previous_pdf_receipt_and_original_template(self):
        export_language_pdf(self.project, "en")
        before_pdf = (self.project / "manual-en.pdf").read_bytes()
        before_receipt = (self.project / "language-export-en.json").read_bytes()
        self.edit_translation(text="Install part 1. " + "Excessively long assembly instructions. " * 400)
        with self.assertRaisesRegex(ValueError, "无法放入"):
            export_language_pdf(self.project, "en")
        self.assertEqual((self.project / "manual-en.pdf").read_bytes(), before_pdf)
        self.assertEqual((self.project / "language-export-en.json").read_bytes(), before_receipt)
        self.assertEqual(self.template.read_bytes(), self.template_bytes)
        self.assertFalse(list(self.project.glob(".lang-*.pdf")))

    def test_receipt_becomes_invalid_after_current_translation_changes(self):
        export_language_pdf(self.project, "en")
        self.assertTrue(is_current_language_pdf(self.project, "en"))
        self.edit_translation(text="Fit part 1.")
        self.assertFalse(is_current_language_pdf(self.project, "en"))

    def test_receipt_becomes_invalid_after_source_or_original_copy_changes(self):
        export_language_pdf(self.project, "en")
        original = deepcopy(self.plan)
        self.plan["source"]["sha256"] = "b" * 64
        self.plan["pdf_template"]["source_sha256"] = self.plan["source"]["sha256"]
        self.write_plan()
        self.assertFalse(is_current_language_pdf(self.project, "en"))
        self.plan = deepcopy(original)
        self.plan["document_copy"]["entries"][0]["text"] = "原文已修改1"
        self.plan["pdf_template"]["text_regions"][0]["source_text"] = "原文已修改1"
        self.write_plan()
        self.assertFalse(is_current_language_pdf(self.project, "en"))

    def test_receipt_rejects_modified_template_pdf_and_modified_language_pdf(self):
        export_language_pdf(self.project, "en")
        self.assertTrue(is_current_language_pdf(self.project, "en"))
        self.template.write_bytes(self.template_bytes + b"\nmodified source")
        self.assertFalse(is_current_language_pdf(self.project, "en"))
        self.template.write_bytes(self.template_bytes)
        self.assertTrue(is_current_language_pdf(self.project, "en"))
        target = self.project / "manual-en.pdf"
        target.write_bytes(target.read_bytes() + b"\nmodified export")
        self.assertFalse(is_current_language_pdf(self.project, "en"))

    def test_text_calibration_must_match_current_source_copy(self):
        self.plan["pdf_template"]["text_regions"][0]["source_text"] = "wrong reference text"
        with self.assertRaisesRegex(ValueError, "原文不一致"):
            export_language_pdf(self.project, "en")
        self.assertFalse((self.project / "manual-en.pdf").exists())

    def test_duplicate_calibration_keys_and_text_figure_overlap_are_rejected(self):
        old = deepcopy(self.plan["pdf_template"]["text_regions"])
        self.plan["pdf_template"]["text_regions"][1]["key"] = old[0]["key"]
        with self.assertRaisesRegex(ValueError, "唯一"):
            export_language_pdf(self.project, "en")
        self.plan["pdf_template"]["text_regions"] = old
        self.plan["pdf_template"]["text_regions"][0]["rect"] = [40, 110, 180, 156]
        with self.assertRaisesRegex(ValueError, "覆盖了安装图"):
            export_language_pdf(self.project, "en")
        self.assertFalse((self.project / "manual-en.pdf").exists())

    def test_unicode_specs_survive_actual_english_export_and_preserve_source_constraints(self):
        source = "指标≤0.08 mg/m³；数量×2；旋转360°；拧入3–4圈。"
        translated = "Limit ≤0.08 mg/m³; ×2; 360°; 3–4 turns."
        region = self.plan["pdf_template"]["text_regions"][0]
        # The variant changes the real synthetic source PDF as well as its
        # calibrated source-copy entry. No production template is touched.
        replacement = self.project / "unicode-source-fixture.pdf"
        with fitz.open(self.template) as doc:
            page = doc[1]
            rect = fitz.Rect(region["rect"])
            page.add_redact_annot(rect, fill=False, cross_out=False)
            page.apply_redactions(images=0, graphics=0, text=0)
            spare = page.insert_textbox(rect, source, fontsize=8, fontname="china-s", lineheight=1.05)
            self.assertGreaterEqual(spare, 0)
            doc.save(replacement, garbage=3, deflate=True)
        replacement.replace(self.template)
        self.template_bytes = self.template.read_bytes()
        self.plan["pdf_template"]["sha256"] = sha(self.template)
        region["source_text"] = source
        self.plan["document_copy"]["entries"][0]["text"] = source
        self.plan["steps"][0]["instruction"] = source
        self.plan["pdf_template"]["step_bindings"][0]["instruction"] = source
        self.write_plan()
        self.edit_translation(text=translated)
        self.assertEqual(_numbers(source), _numbers(translated))
        self.assertEqual(_protected_tokens(source), _protected_tokens(translated))

        receipt = export_language_pdf(self.project, "en")
        with fitz.open(self.project / receipt["filename"]) as result:
            output = result[1].get_text()
        for token in ("≤0.08 mg/m³", "×2", "360°", "3–4"):
            self.assertIn(token, output)
        self.assertNotIn("?", output)
        row = next(row for row in read_document(self.project)["entries"] if row["key"] == "document.step-1")
        self.assertEqual(row["source_text"], source)
        self.assertEqual(_numbers(source), _numbers(row["translations"]["en"]["text"]))
        self.assertEqual(_protected_tokens(source), _protected_tokens(row["translations"]["en"]["text"]))
        self.assertEqual(self.template.read_bytes(), self.template_bytes)
        self.assertTrue(is_current_language_pdf(self.project, "en"))

    def test_receipt_replacement_failure_rolls_back_pdf_receipt_and_cleans_both_staged_files(self):
        export_language_pdf(self.project, "en")
        pdf = self.project / "manual-en.pdf"
        receipt = self.project / "language-export-en.json"
        before_pdf, before_receipt = pdf.read_bytes(), receipt.read_bytes()
        self.edit_translation(text="Fit part 1.")
        real_replace = Path.replace

        def fail_second_publication(project, staged_files, removed_files, history_names):
            receipt_stage = staged_files[Path(receipt.name)]
            self.assertEqual(set(staged_files), {Path(pdf.name), Path(receipt.name)})
            self.assertNotEqual(staged_files[Path(pdf.name)].read_bytes(), before_pdf)

            def replace(source, destination):
                if source == receipt_stage:
                    raise OSError("Injected receipt replacement failure")
                return real_replace(source, destination)

            with patch.object(Path, "replace", replace):
                return _archive_and_publish(project, staged_files, removed_files, history_names)

        with patch("preview_service._archive_and_publish", side_effect=fail_second_publication) as publish:
            with self.assertRaisesRegex(OSError, "receipt replacement failure"):
                export_language_pdf(self.project, "en")
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(pdf.read_bytes(), before_pdf)
        self.assertEqual(receipt.read_bytes(), before_receipt)
        self.assertEqual(self.template.read_bytes(), self.template_bytes)
        self.assertFalse(list(self.project.glob(".lang-*.pdf")))
        self.assertFalse(list(self.project.glob(".lang-*.json")))
        backups = [directory for directory in (self.project / "history").iterdir()
                   if (directory / pdf.name).is_file() and (directory / receipt.name).is_file()]
        self.assertTrue(backups)
        self.assertTrue(any((directory / pdf.name).read_bytes() == before_pdf
                            and (directory / receipt.name).read_bytes() == before_receipt for directory in backups))

    def test_malformed_illustrator_private_object_does_not_discard_saved_translations_or_figures(self):
        # A damaged lazy-loaded private object can trigger repair during save,
        # rebuilding the original xref table and silently dropping added streams.
        # Keep its advertised xref offset unchanged by corrupting one header byte.
        with fitz.open(self.template) as doc:
            private = doc.get_new_xref()
            doc.update_object(private, "<< /Private (Synthetic Illustrator private payload) >>")
            doc.xref_set_key(doc[1].xref, "PieceInfo", f"<< /Illustrator {private} 0 R >>")
            valid = doc.tobytes(garbage=0)
        header = f"{private} 0 obj".encode("ascii")
        self.assertEqual(valid.count(header), 1)
        damaged = valid.replace(header, b"x" + header[1:], 1)
        self.template.write_bytes(damaged)
        self.template_bytes = damaged
        self.plan["pdf_template"]["sha256"] = sha(self.template)
        self.write_plan()

        # Confirm this is the actual late-repair failure, rather than a generic
        # malformed fixture which is already repaired when first opened.
        with fitz.open(self.template) as original:
            self.assertFalse(original.is_repaired)
            original[1].insert_text((30, 50), "UNSAFE_PRIVATE_OBJECT_PROBE", fontsize=8)
            direct = original.tobytes(garbage=3, deflate=True)
        with fitz.open(stream=direct, filetype="pdf") as lost:
            self.assertNotIn("UNSAFE_PRIVATE_OBJECT_PROBE", lost[1].get_text())

        receipt = export_language_pdf(self.project, "en")
        with fitz.open(self.template) as original, fitz.open(self.project / receipt["filename"]) as result:
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].get_pixmap(alpha=False).samples, original[0].get_pixmap(alpha=False).samples)
            text = result[1].get_text()
            self.assertNotIn("原文说明", text)
            self.assertEqual(sum(f"Install part {number}." in text for number in range(1, 8)), 7)
            # The preserved blue rules remain; every red vector comes from a
            # freshly inserted STEP-asset substitute in this synthetic fixture.
            red = [drawing for drawing in result[1].get_drawings()
                   if drawing["color"] and drawing["color"][0] > 0.5 and drawing["color"][1] < 0.25]
            self.assertGreaterEqual(len(red), 7)
        self.assertEqual(self.template.read_bytes(), damaged)
        self.assertTrue(is_current_language_pdf(self.project, "en"))


if __name__ == "__main__":
    unittest.main()
