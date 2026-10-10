"""Booklet contract checks without CAD runtimes or old product geometry."""
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from book_pdf import export_book_pdf
from generic_render import plan_digest


@unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF required")
class BookPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.diagrams = self.project / "diagrams"
        self.diagrams.mkdir()
        self.plan = {
            "product": {"title": "装配说明草案"}, "source": {"sha256": "a"*64, "name": "current.step"}, "status": "draft",
            "groups": [{"id": "base", "label": "参考底座", "part_ids": ["p1"]}, {"id": "cap", "label": "上部件", "part_ids": ["p2"]}],
            "steps": [{"id": "one", "title": "核对底座", "instruction": "核对模型中的参考底座。", "assembled_groups": [], "moving_groups": ["base"], "warnings": ["当前顺序待确认。"]},
                      {"id": "two", "title": "核对上部件", "instruction": "核对上部件与底座的相对位置。", "assembled_groups": ["base"], "moving_groups": ["cap"], "warnings": ["连接方式待确认。"]}],
        }
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300"><rect x="50" y="140" width="300" height="110" fill="none" stroke="black"/><rect x="135" y="40" width="130" height="80" fill="none" stroke="black"/><path d="M 90 175 L 300 175 M 90 200 L 300 200 M 200 120 L 200 140" fill="none" stroke="black"/></svg>'
        digest = hashlib.sha256(svg.encode()).hexdigest()
        for name in ("one", "two", "detail"):
            (self.diagrams / f"{name}.svg").write_text(svg, encoding="utf-8")
        self.manifest = {"complete": True, "source_sha256": self.plan["source"]["sha256"], "plan_sha256": plan_digest(self.plan),
                         "entries": [{"step_id": sid, "status": "ready", "svg_path": f"{sid}.svg", "svg_sha256": digest} for sid in ("one", "two")]}

    def tearDown(self):
        self.temp.cleanup()

    def test_a4_n_plus_three_extractable_cjk_and_current_vector_figures(self):
        import fitz
        export_book_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 5)
            self.assertTrue(all(abs(page.rect.width-595.276) < 0.01 and abs(page.rect.height-841.890) < 0.01 for page in pdf))
            self.assertIn("配件与工具", pdf[0].get_text())
            self.assertIn("总装概览", pdf[0].get_text())
            self.assertNotIn("配件概览", pdf[0].get_text())
            self.assertIn("核对底座", pdf[1].get_text())
            self.assertIn("安全使用与日常保养", pdf[3].get_text())
            self.assertIn("来源与待确认事项", pdf[4].get_text())
            for page in (pdf[0], pdf[1], pdf[2]):
                self.assertGreater(len(page.get_drawings()), 3)
                self.assertEqual(page.get_images(), [])
            text = "".join(page.get_text() for page in pdf)
            self.assertIn("工具清单与规格待确认", text)
            self.assertIn("数量待确认", text)
            self.assertNotIn("5 mm", text)
            self.assertNotIn("气压杆", text)
            self.assertIn("current.step", text)
            self.assertIn("a"*32, text)

    def test_document_options_and_focus_do_not_change_page_count(self):
        import fitz
        self.plan["manual_document"] = {"title": "新产品安装说明", "model_label": "型号：试验款", "parts": [{"label": "参考底座", "quantity": 1, "source": "包装确认"}],
            "tools": [{"label": "指定工具", "quantity": 1, "source": "包装确认"}], "preparation": ["按已确认的准备事项操作。"],
            "functions": ["已确认的功能简介。"], "cover_figure_caption": "真实模型配件概览", "review_title": "发布前必须核对", "review_introduction": ["以下事项需要核对后完善。"], "review_items": [{"title": "连接待核", "body": "需核对实际连接位置。"}],
            "sources": ["产品文字来源：包装确认记录"], "reference": {"name": "版式示例.pdf", "sha256": "b"*64}}
        self.plan["steps"][0]["consumer"] = {"parts_text": "参考底座", "caution": ["注意：按已确认方式放置。"], "engineering_notes": ["部件对应待核。"], "figure_caption": "底座真实模型示意"}
        self.manifest["entries"][0]["focus"] = {"svg_path": "detail.svg", "svg_sha256": self.manifest["entries"][0]["svg_sha256"]}
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        export_book_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 5)
            self.assertIn("实际模型局部细节", pdf[1].get_text())
            self.assertIn("功能概览", pdf[2].get_text())
            self.assertIn("版式示例.pdf", pdf[4].get_text())
            self.assertIn("b"*32, pdf[4].get_text())
            self.assertIn("总装概览", pdf[0].get_text())
            self.assertNotIn("真实模型配件概览", pdf[0].get_text())
            self.assertIn("发布前必须核对", pdf[4].get_text())
            self.assertIn("以下事项需要核对后完善", pdf[4].get_text())

    def test_many_automatic_cad_groups_are_summarized_without_becoming_a_bom(self):
        import fitz
        self.plan["groups"] = [{"id": f"g{i}", "label": f"层级实例建议分组 {i} 的模型部件", "part_ids": [f"p{i}"]} for i in range(40)]
        self.plan["steps"][0]["moving_groups"] = ["g0"]
        self.plan["steps"][1]["assembled_groups"] = ["g0"]
        self.plan["steps"][1]["moving_groups"] = [f"g{i}" for i in range(1, 40)]
        self.plan["steps"].append({"id": "overview", "title": "完整模型总览", "instruction": "核对当前 STP 中的全部实例。", "assembled_groups": [f"g{i}" for i in range(40)], "moving_groups": [], "warnings": ["当前分组待包装确认。"]})
        entry = deepcopy(self.manifest["entries"][-1])
        entry["step_id"] = "overview"
        self.manifest["entries"].append(entry)
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        export_book_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 6)
            self.assertIn("建议分组共 40 组", pdf[0].get_text())
            self.assertIn("包装配件名称、数量待核对", pdf[0].get_text())
            self.assertIn("本步新增建议分组 39 组", pdf[2].get_text())
            self.assertIn("完整 STP 总成（40 个建议组）", pdf[3].get_text())
            self.assertNotIn("×40", pdf[0].get_text())
        before = (self.project / "manual.pdf").read_bytes()
        # Explicit product parts are editorial content, not automatic CAD
        # hierarchy labels: they must never be replaced with this summary.
        self.plan["manual_document"] = {"parts": [{"label": f"包装明确提供的第 {i} 种配件，需要完整保留该名称", "quantity": 1} for i in range(40)]}
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        with self.assertRaisesRegex(ValueError, "manual_document.parts/tools; shorten/split"):
            export_book_pdf(self.project, self.plan, self.manifest)
        self.assertEqual(before, (self.project / "manual.pdf").read_bytes())

    def test_rejects_changed_source_plan_or_svg(self):
        bad = deepcopy(self.manifest)
        bad["source_sha256"] = "c"*64
        with self.assertRaisesRegex(ValueError, "source SHA256"):
            export_book_pdf(self.project, self.plan, bad)
        bad = deepcopy(self.plan)
        bad["steps"][0]["instruction"] = "修订后的文字"
        with self.assertRaisesRegex(ValueError, "plan SHA256 is stale"):
            export_book_pdf(self.project, bad, self.manifest)
        with (self.diagrams / "one.svg").open("a", encoding="utf-8") as stream:
            stream.write("\n ")
        with self.assertRaisesRegex(ValueError, "Changed diagram"):
            export_book_pdf(self.project, self.plan, self.manifest)

    def test_reference_function_block_and_actual_machine_title_fit_without_truncation(self):
        import fitz
        functions = ["头枕：升降、前后、旋转；扶手：升降、前后、旋转、悬停。",
                     "腰靠：升降、翻转、自适应；座椅：坐深、坐高、后仰调节。",
                     "脚托：仅部分版本具备。",
                     "功能名据原PDF列示，适用版本以实物确认；控制位置及具体操作方法待核。"]
        machine_title = "位置确认：13543D202303230001_ASM_1_1_ASM（CAD 子总成，42 个实例）"
        self.plan["manual_document"] = {"functions": functions}
        self.plan["steps"][0]["title"] = machine_title
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        export_book_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 5)
            first = "".join(pdf[1].get_text().split())
            self.assertIn("".join(machine_title.split()), first)
            final = "".join(pdf[2].get_text().split())
            for value in functions:
                self.assertIn("".join(value.split()), final)
            self.assertGreater(len(pdf[2].get_drawings()), 3)

    def test_explicit_cover_vectors_bind_to_current_source_recipe_and_asset(self):
        import fitz
        cover_svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><circle cx="200" cy="150" r="90" fill="none" stroke="black"/></svg>'
        (self.diagrams / "cover.svg").write_text(cover_svg, encoding="utf-8")
        self.plan["cover_scene"] = {"groups": ["base", "cap"], "offsets": {"cap": [0, 0, 40]}}
        self.plan["manual_document"] = {"cover_figure_caption": "包装配件分开展示 | 当前模型图"}
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        # Isolate the renderer's recipe interface: this booklet unit test uses
        # deliberate synthetic vectors, and never needs to import CAD engines.
        helper = types.ModuleType("cover_render")
        helper.cover_plan = lambda plan: {"source": plan["source"], "cover_scene": plan["cover_scene"]}
        self.manifest["cover"] = {"status": "ready", "source_sha256": self.plan["source"]["sha256"],
                                  "cover_plan_sha256": plan_digest(helper.cover_plan(self.plan)),
                                  "svg_path": "cover.svg", "svg_sha256": hashlib.sha256(cover_svg.encode()).hexdigest()}
        with patch.dict(sys.modules, {"cover_render": helper}):
            export_book_pdf(self.project, self.plan, self.manifest)
            with fitz.open(self.project / "manual.pdf") as pdf:
                self.assertEqual(len(pdf), 5)
                self.assertIn("包装配件分开展示", pdf[0].get_text())
                self.assertTrue(any(item[0] == "c" for drawing in pdf[0].get_drawings() for item in drawing["items"]))
                self.assertEqual(pdf[0].get_images(), [])
            bad = deepcopy(self.manifest)
            bad["cover"]["source_sha256"] = "c"*64
            with self.assertRaisesRegex(ValueError, "cover source SHA256"):
                export_book_pdf(self.project, self.plan, bad)
            bad = deepcopy(self.manifest)
            bad["cover"]["cover_plan_sha256"] = "c"*64
            with self.assertRaisesRegex(ValueError, "cover recipe SHA256 is stale"):
                export_book_pdf(self.project, self.plan, bad)
            revised = deepcopy(self.plan)
            revised["cover_scene"]["offsets"]["cap"] = [0, 0, 80]
            bad = deepcopy(self.manifest)
            bad["plan_sha256"] = plan_digest(revised)
            with self.assertRaisesRegex(ValueError, "cover recipe SHA256 is stale"):
                export_book_pdf(self.project, revised, bad)
            with (self.diagrams / "cover.svg").open("a", encoding="utf-8") as stream:
                stream.write("\n ")
            with self.assertRaisesRegex(ValueError, "Changed diagram for cover scene"):
                export_book_pdf(self.project, self.plan, self.manifest)

    def test_overflow_preserves_existing_pdf_and_names_content(self):
        existing = self.project / "manual.pdf"
        existing.write_bytes(b"previous PDF")
        self.plan["steps"][0]["instruction"] = "这是过长的说明文字，需要拆分或缩短。"*400
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        with self.assertRaisesRegex(ValueError, "step one instruction; shorten/split"):
            export_book_pdf(self.project, self.plan, self.manifest)
        self.assertEqual(existing.read_bytes(), b"previous PDF")
        self.assertFalse(list(self.project.glob("manual-book-*.tmp.pdf")))

    def test_arbitrary_step_count_and_confirmation_footer(self):
        import fitz
        self.plan["steps"] = [self.plan["steps"][0]]
        self.plan["status"] = "confirmed"
        self.manifest["entries"] = self.manifest["entries"][:1]
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        export_book_pdf(self.project, self.plan, self.manifest)
        with fitz.open(self.project / "manual.pdf") as pdf:
            self.assertEqual(len(pdf), 4)
            self.assertIn("包装已确认", pdf[0].get_text())


if __name__ == "__main__":
    unittest.main()
