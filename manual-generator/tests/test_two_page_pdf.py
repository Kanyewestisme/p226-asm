"""Consumer manual stays two A3 sheets without invented product facts.

Synthetic vectors exercise layout and source binding, independently of CAD.
"""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generic_render import plan_digest
from two_page_pdf import export_two_page_pdf


def compact(value):
    return "".join(value.split())


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF required")
class TwoPagePdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.diagrams = self.project / "diagrams"
        self.diagrams.mkdir()
        self.plan = {
            "product": {"title": "测试支架安装说明"},
            "source": {"sha256": "a" * 64, "name": "current.step"},
            "status": "draft",
            "groups": [{"id": "base", "label": "参考底座", "part_ids": ["p1"]},
                       {"id": "cap", "label": "上部件", "part_ids": ["p2"]}],
            "steps": [],
        }
        self.svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" '
                    'viewBox="0 0 400 300"><rect x="50" y="140" width="300" '
                    'height="110" fill="none" stroke="black"/><rect x="135" '
                    'y="40" width="130" height="80" fill="none" stroke="black"/>'
                    '<path d="M90 175 L300 175 M90 200 L300 200 M200 120 L200 140" '
                    'fill="none" stroke="black"/></svg>')
        self.svg_hash = hashlib.sha256(self.svg.encode()).hexdigest()
        self.manifest = {"complete": True, "source_sha256": self.plan["source"]["sha256"], "entries": []}
        self.set_steps(7)

    def tearDown(self):
        self.temp.cleanup()

    def set_steps(self, count):
        self.plan["steps"] = []
        self.manifest["entries"] = []
        for index in range(1, count + 1):
            sid = f"step{index}"
            self.plan["steps"].append({"id": sid, "title": f"核对连接第{index}步",
                                       "instruction": f"核对第{index}处相对位置，确认后按图组装。",
                                       "assembled_groups": ["base"], "moving_groups": ["cap"],
                                       "warnings": ["当前连接位置与顺序待确认。"]})
            (self.diagrams / f"{sid}.svg").write_text(self.svg, encoding="utf-8")
            self.manifest["entries"].append({"step_id": sid, "status": "ready",
                                              "svg_path": f"{sid}.svg", "svg_sha256": self.svg_hash})
        self.refresh_digest()

    def refresh_digest(self):
        self.manifest["plan_sha256"] = plan_digest(self.plan)

    def test_exact_two_a3_landscape_pages_cover_then_every_step_as_vectors(self):
        import fitz
        export_two_page_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 2)
            self.assertTrue(all(abs(page.rect.width - 1190.551) < 0.01 and
                                abs(page.rect.height - 841.890) < 0.01 for page in pdf))
            cover_text, steps_text = (compact(page.get_text()) for page in pdf)
            self.assertIn(compact(self.plan["product"]["title"]), cover_text)
            for step in self.plan["steps"]:
                self.assertNotIn(compact(step["title"]), cover_text)
                self.assertIn(compact(step["title"]), steps_text)
                self.assertIn(compact(step["instruction"]), steps_text)
            for page in pdf:
                self.assertEqual(page.get_images(), [])
                self.assertGreater(len(page.get_drawings()), 3)
            # Each step keeps its own model geometry on the one installation sheet.
            self.assertGreaterEqual(len(pdf[1].get_drawings()), 7 * 3)

    def test_default_text_does_not_import_previous_product_facts(self):
        import fitz
        self.plan["product"]["title"] = "其他产品：桌面支架"
        self.refresh_digest()
        export_two_page_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            text = "".join(page.get_text() for page in pdf)
        for old_fact in ("P226", "保友", "气压杆", "头枕", "脚托", "螺丝 B", "螺丝 C", "M6", "5 mm"):
            self.assertNotIn(old_fact, text)

    def test_changed_source_plan_or_svg_are_rejected_without_replacing_pdf(self):
        old = self.project / "manual.pdf"
        old.write_bytes(b"previous PDF")
        bad_manifest = deepcopy(self.manifest)
        bad_manifest["source_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "source SHA256"):
            export_two_page_pdf(self.project, self.plan, bad_manifest)
        changed_plan = deepcopy(self.plan)
        changed_plan["steps"][0]["instruction"] = "修订后的文字"
        with self.assertRaisesRegex(ValueError, "plan SHA256 is stale"):
            export_two_page_pdf(self.project, changed_plan, self.manifest)
        with (self.diagrams / "step1.svg").open("a", encoding="utf-8") as stream:
            stream.write("\n ")
        with self.assertRaisesRegex(ValueError, "Changed diagram"):
            export_two_page_pdf(self.project, self.plan, self.manifest)
        self.assertEqual(old.read_bytes(), b"previous PDF")

    def test_incomplete_manifest_cannot_publish_a_partial_sheet(self):
        bad = deepcopy(self.manifest)
        bad["complete"] = False
        with self.assertRaisesRegex(ValueError, "complete current diagram manifest"):
            export_two_page_pdf(self.project, self.plan, bad)
        bad = deepcopy(self.manifest)
        bad["entries"].pop()
        with self.assertRaisesRegex(ValueError, "exactly one current entry"):
            export_two_page_pdf(self.project, self.plan, bad)
        self.assertFalse((self.project / "manual.pdf").exists())

    def test_maximum_nine_steps_fit_in_the_installation_sheet(self):
        import fitz
        self.set_steps(9)
        export_two_page_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 2)
            text = compact(pdf[1].get_text())
            for step in self.plan["steps"]:
                self.assertIn(compact(step["title"]), text)
                self.assertIn(compact(step["instruction"]), text)

    def test_too_many_steps_preserve_existing_pdf_instead_of_adding_pages(self):
        old = self.project / "manual.pdf"
        old.write_bytes(b"previous PDF")
        self.set_steps(10)
        with self.assertRaisesRegex(ValueError, "at most 9 steps"):
            export_two_page_pdf(self.project, self.plan, self.manifest)
        self.assertEqual(old.read_bytes(), b"previous PDF")

    def test_instruction_overflow_preserves_existing_pdf(self):
        old = self.project / "manual.pdf"
        old.write_bytes(b"previous PDF")
        self.plan["steps"][0]["instruction"] = "这是过长的说明文字，必须缩短后才能放入安装图版。" * 400
        self.refresh_digest()
        with self.assertRaisesRegex(ValueError, "PDF content overflow"):
            export_two_page_pdf(self.project, self.plan, self.manifest)
        self.assertEqual(old.read_bytes(), b"previous PDF")
        self.assertFalse(list(self.project.glob("*.tmp.pdf")))

    def test_hidden_view_note_does_not_claim_an_assembly_action(self):
        import fitz
        self.plan["steps"][0]["hidden_part_ids"] = ["p1"]
        self.plan["steps"][0]["visibility_reason"] = "仅为观察连接位置。"
        self.refresh_digest()
        export_two_page_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            text = compact(pdf[1].get_text())
            self.assertIn("临时隐藏", text)
            self.assertIn("不表示", text)
            self.assertIn("拆装", text)
            self.assertEqual(len(pdf), 2)

    def test_actual_focus_remains_vector_and_visible_on_installation_sheet(self):
        import fitz
        focus = ('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160">'
                 '<circle cx="80" cy="80" r="60" fill="none" stroke="black"/></svg>')
        (self.diagrams / "focus.svg").write_text(focus, encoding="utf-8")
        self.manifest["entries"][2]["focus"] = {"svg_path": "focus.svg", "svg_sha256": hashlib.sha256(focus.encode()).hexdigest()}
        export_two_page_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 2)
            self.assertIn("局部", pdf[1].get_text())
            self.assertTrue(any(item[0] == "c" for drawing in pdf[1].get_drawings() for item in drawing["items"]))
            self.assertEqual(pdf[1].get_images(), [])

    def test_default_export_requires_user_template_instead_of_redesigning(self):
        from review_output import export_pdf
        with self.assertRaisesRegex(ValueError, 'requires plan.pdf_template'):
            export_pdf(self.project, self.plan, self.manifest)
        self.assertFalse((self.project / 'manual.pdf').exists())


if __name__ == "__main__":
    unittest.main()
