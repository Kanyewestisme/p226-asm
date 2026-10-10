"""Translation version binding and genuine offline-memory coverage, without CAD."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manual_languages import (
    _E301_EN, _numbers, _same_constraints, _translate_known, collect_current_copy, draft_translations,
    read_document, save_translations, select_language, sync_document,
)


def plan_fixture():
    return {"source": {"sha256": "source-e301"}, "product": {"title": "E301通用安装说明书"},
            "groups": [{"id": "headrest", "label": "头枕"}],
            "steps": [{"id": "headrest", "title": "安装头枕", "instruction": "安装头枕", "warnings": ["工程猜测可能有误"], "reviewed": False},
                      {"id": "overview", "title": "STEP 最终位置总览", "instruction": "总览仅用于工程核对", "warnings": [], "include_in_manual": False}],
            "warnings": ["几何邻近不能证明真实装配顺序"],
            "document_copy": {"entries": [{"key": "document.parts", "text": "D. 头枕螺丝(M6X16)×2", "kind": "template_text", "scope": "consumer", "source_reference_sha256": "reference-e301"}]}}


class ManualLanguageTests(unittest.TestCase):
    def setUp(self):
        self.provider_env = patch.dict(os.environ, {name: "" for name in ("MANUAL_TRANSLATION_BASE_URL", "MANUAL_TRANSLATION_MODEL", "MANUAL_TRANSLATION_API_KEY")})
        self.provider_env.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.plan = plan_fixture()
        self.write_plan()

    def tearDown(self):
        self.temporary.cleanup()
        self.provider_env.stop()

    def write_plan(self):
        (self.project / "steps.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")

    def body(self, document=None):
        doc = document or read_document(self.project)
        return {"source_sha256": doc["source_sha256"], "base_sha256": doc["base_sha256"]}

    def edit(self, document, key="step.headrest.instruction", text="Install the headrest", **extra):
        row = next(row for row in document["entries"] if row["key"] == key)
        return {"key": key, "locale": "en", "text": text, "status": "reviewed", "source_sha256": row["source_sha256"], **extra}

    def test_engineering_notes_and_hidden_final_overview_are_not_consumer_copy(self):
        rows = {row["key"]: row for row in collect_current_copy(self.plan)}
        self.assertEqual(rows["step.headrest.warnings.0"]["scope"], "engineering")
        self.assertEqual(rows["step.overview.title"]["scope"], "engineering")
        self.assertEqual(rows["document.parts"]["scope"], "consumer")
        self.assertNotIn("工程猜测", " ".join(row["source_text"] for row in rows.values() if row["scope"] == "consumer"))

    def test_sourced_document_copy_keys_are_stable_and_reference_digest_bound(self):
        first = sync_document(self.plan)
        self.plan["document_copy"]["entries"][0]["source_reference_sha256"] = "reference-new"
        second = sync_document(self.plan, first)
        self.assertNotEqual(first["base_sha256"], second["base_sha256"])
        a = next(row for row in first["entries"] if row["key"] == "document.parts")
        b = next(row for row in second["entries"] if row["key"] == "document.parts")
        self.assertNotEqual(a["source_sha256"], b["source_sha256"])

    def test_known_drafts_are_proposed_unknown_text_is_missing_no_fake_translation(self):
        document = draft_translations(self.project, self.body() | {"locales": ["en", "de"]})
        known = next(row for row in document["entries"] if row["key"] == "step.headrest.instruction")
        unknown = next(row for row in document["entries"] if row["key"] == "step.headrest.warnings.0")
        self.assertEqual(known["translations"]["en"]["text"], "Install the headrest")
        self.assertEqual(known["translations"]["en"]["status"], "proposed")
        self.assertNotIn("en", unknown["translations"])
        self.assertIn(unknown["key"], document["draft_report"]["missing_keys"]["en"])
        self.assertEqual([row["code"] for row in document["locales"]], ["zh", "en", "de", "fr", "es"])
        self.assertFalse(document["capabilities"]["external_text_sent"])

    def test_read_does_not_write_or_modify_figures(self):
        (self.project / "diagrams").mkdir()
        image = self.project / "diagrams" / "step.svg"
        image.write_bytes(b"untouched geometry")
        read_document(self.project)
        self.assertFalse((self.project / "languages.json").exists())
        draft_translations(self.project, self.body() | {"locales": ["en"]})
        self.assertEqual(image.read_bytes(), b"untouched geometry")
        self.assertFalse((self.project / "manual.pdf").exists())

    def test_source_text_edit_invalidates_only_affected_translation(self):
        initial = read_document(self.project)
        saved = save_translations(self.project, self.body(initial) | {"translations": [self.edit(initial)]})
        self.plan["steps"][0]["instruction"] = "新的安装文字"
        self.write_plan()
        current = read_document(self.project)
        row = next(row for row in current["entries"] if row["key"] == "step.headrest.instruction")
        self.assertEqual(row["translations"]["en"]["status"], "stale")
        self.assertEqual(row["translations"]["en"]["text"], "Install the headrest")
        with self.assertRaisesRegex(ValueError, "原文或 STP"):
            save_translations(self.project, self.body(saved) | {"translations": [self.edit(saved)]})
        with self.assertRaisesRegex(ValueError, "单条原文"):
            save_translations(self.project, self.body(current) | {"translations": [self.edit(saved)]})

    def test_step_order_or_camera_changes_do_not_invalidate_text(self):
        initial = read_document(self.project)
        saved = save_translations(self.project, self.body(initial) | {"translations": [self.edit(initial)]})
        self.plan["steps"][0]["camera"] = [0, 0, 1]
        current = sync_document(self.plan, saved)
        self.assertEqual(current["base_sha256"], saved["base_sha256"])
        self.assertEqual(select_language(self.plan, current, "en", keys=["step.headrest.instruction"]), {"step.headrest.instruction": "Install the headrest"})

    def test_model_revision_invalidates_even_identical_copy(self):
        initial = read_document(self.project)
        saved = save_translations(self.project, self.body(initial) | {"translations": [self.edit(initial)]})
        self.plan["source"]["sha256"] = "new-source"
        current = sync_document(self.plan, saved)
        row = next(row for row in current["entries"] if row["key"] == "step.headrest.instruction")
        self.assertEqual(row["translations"]["en"]["status"], "stale")
        with self.assertRaisesRegex(ValueError, "不一致"):
            select_language(self.plan, saved, "en", keys=["step.headrest.instruction"])

    def test_required_translation_must_be_reviewed_and_engineering_export_is_rejected(self):
        drafted = draft_translations(self.project, self.body() | {"locales": ["en"]})
        with self.assertRaisesRegex(ValueError, "未确认"):
            select_language(self.plan, drafted, "en", keys=["step.headrest.instruction"])
        saved = save_translations(self.project, self.body(drafted) | {"translations": [self.edit(drafted)]})
        self.assertEqual(select_language(self.plan, saved, "en", keys=["step.headrest.instruction"])["step.headrest.instruction"], "Install the headrest")
        with self.assertRaisesRegex(ValueError, "工程审核"):
            select_language(self.plan, saved, "en", keys=["step.headrest.warnings.0"])

    def test_numeric_and_specification_loss_is_rejected_atomically(self):
        document = read_document(self.project)
        edits = [self.edit(document), self.edit(document, "document.parts", "D. Headrest screws (M6X16) ×3")]
        with self.assertRaisesRegex(ValueError, "数字"):
            save_translations(self.project, self.body(document) | {"translations": edits})
        self.assertFalse((self.project / "languages.json").exists())
        valid = save_translations(self.project, self.body(document) | {"translations": [self.edit(document, "document.parts", "D. Headrest screws (M6X16) ×2")]})
        self.assertEqual(select_language(self.plan, valid, "en", keys=["document.parts"])["document.parts"], "D. Headrest screws (M6X16) ×2")

    def test_automatic_draft_never_overwrites_reviewed_human_text(self):
        doc = read_document(self.project)
        saved = save_translations(self.project, self.body(doc) | {"translations": [self.edit(doc, text="Attach the headrest")]})
        redrafted = draft_translations(self.project, self.body(saved) | {"locales": ["en"], "overwrite": True})
        self.assertEqual(select_language(self.plan, redrafted, "en", keys=["step.headrest.instruction"])["step.headrest.instruction"], "Attach the headrest")

    def test_e301_memory_retains_all_numbers_and_reference_advice_review_flags(self):
        for source, target in _E301_EN:
            with self.subTest(source=source):
                translated = _translate_known(source, "en")
                self.assertEqual(translated, target)
                self.assertEqual(_numbers(source), _numbers(translated))
                self.assertTrue(_same_constraints(source, translated))
        self.plan["document_copy"]["entries"].append({"key": "document.care", "text": "用天那水擦拭", "scope": "consumer"})
        self.write_plan()
        doc = draft_translations(self.project, self.body() | {"locales": ["en"]})
        care = next(row for row in doc["entries"] if row["key"] == "document.care")
        self.assertIn("thinner", care["translations"]["en"]["text"])
        self.assertEqual(care["review_flags"][0]["code"], "source_solvent_conflict")
        self.assertEqual(care["translations"]["en"]["status"], "proposed")

    def test_technical_units_and_fastener_markers_cannot_be_lost_in_translation(self):
        self.assertTrue(_same_constraints("推荐5v，3A", "5V, 3A is recommended"))
        self.assertFalse(_same_constraints("M6X16×2", "6 by 16, quantity 2"))
        self.assertFalse(_same_constraints("5v，3A", "5, 3"))
        self.assertFalse(_same_constraints("用D螺丝固定", "Secure with screws"))
        self.assertFalse(_same_constraints("不能连接电脑的USB接口", "Do not connect to a computer port"))

    def test_actual_draft_patterns_translate_without_inventing_a_connection(self):
        text = "以「E301-4P_ASM」作为草案的起始参考组。起始组按 STEP 体积和邻近候选选择，请确认实际操作是否合适。"
        translated = _translate_known(text, "en")
        self.assertIn("confirm whether", translated)
        self.assertIn("E301-4P_ASM", translated)
        self.assertIsNone(_translate_known(text.replace("E301-4P_ASM", "任意未知中文组件"), "en"))

    def test_complete_paragraph_translation_covers_line_wrapping_and_bullets(self):
        source = "· 请仔细阅读安装说明，掌握安装步骤和方法。\n· 清点配件，查看是否有配件缺少或破损。"
        translated = _translate_known(source, "en")
        self.assertIn("assembly instructions", translated)
        self.assertIn("Check all parts", translated)
        self.assertIsNone(_translate_known(source + "\n一个未知警告", "en"))

    def test_export_readiness_uses_calibrated_consumer_keys_only(self):
        self.plan["pdf_template"] = {"text_regions": [{"key": "document.parts"}]}
        self.write_plan()
        drafted = draft_translations(self.project, self.body() | {"locales": ["en"]})
        self.assertEqual(drafted["export_keys"], ["document.parts"])
        self.assertTrue(drafted["export_readiness"]["en"]["draft_ready"])
        self.assertFalse(drafted["export_readiness"]["en"]["final_ready"])
        self.assertEqual(drafted["export_readiness"]["en"]["unreviewed"], ["document.parts"])
        saved = save_translations(self.project, self.body(drafted) | {"translations": [self.edit(drafted, "document.parts", "D. Headrest screws (M6X16) ×2")]})
        self.assertTrue(saved["export_readiness"]["en"]["final_ready"])
        self.assertNotIn("step.overview.title", saved["export_keys"])
        self.plan.pop("pdf_template")
        self.write_plan()
        self.assertFalse(read_document(self.project)["export_readiness"]["en"]["draft_ready"])

    def test_provisional_template_text_regions_require_layout_calibration(self):
        self.plan["pdf_template"] = {"text_regions": [{"key": "document.parts", "needs_layout_review": True}]}
        self.write_plan()
        drafted = draft_translations(self.project, self.body() | {"locales": ["en"]})
        self.assertFalse(drafted["export_readiness"]["en"]["draft_ready"])
        self.assertIn("校准", drafted["export_readiness"]["en"]["reason"])
        self.plan["pdf_template"]["text_regions"][0]["needs_layout_review"] = False
        self.write_plan()
        self.assertTrue(read_document(self.project)["export_readiness"]["en"]["draft_ready"])


if __name__ == "__main__":
    unittest.main()
