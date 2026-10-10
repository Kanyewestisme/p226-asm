"""Packaging confirmation is independent of application PDF export."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manual
from generic_render import plan_digest


class ConfirmModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.plan = {"source": {"sha256": "a" * 64}, "status": "draft",
                     "product": {"title": "Current product"},
                     "steps": [{"id": "one", "reviewed": True}, {"id": "two", "reviewed": True}]}
        (self.project / "steps.json").write_text(json.dumps(self.plan))
        (self.project / "manual.pdf").write_bytes(b"existing PDF placeholder")
        (self.project / "pdf_export.json").write_bytes(b"existing receipt placeholder")
        self.manifest = {"source_sha256": self.plan["source"]["sha256"], "plan_sha256": plan_digest(self.plan), "complete": True}
        self.loader = patch("manual.load_project", return_value=(self.plan, {}))
        self.current = patch("manual.current_manifest", return_value=self.manifest)
        self.prepare = patch("manual.prepare_exports", return_value=self.project / "prepared-export")
        self.publish = patch("manual.publish_exports")
        self.pdf_current = patch("review_output.is_current_pdf", return_value=True)
        self.loader.start()
        self.current_mock = self.current.start()
        self.prepare_mock = self.prepare.start()
        self.publish_mock = self.publish.start()
        self.pdf_current_mock = self.pdf_current.start()
        for item in (self.loader, self.current, self.prepare, self.publish, self.pdf_current):
            self.addCleanup(item.stop)

    def tearDown(self):
        self.temp.cleanup()

    def test_application_confirmation_does_not_export_and_keeps_current_pdf_bytes(self):
        old_pdf = (self.project / "manual.pdf").read_bytes()
        old_receipt = (self.project / "pdf_export.json").read_bytes()
        confirmation = manual.confirm_project(self.project, "包装同事", export_document=False)
        confirmed = json.loads((self.project / "steps.json").read_text(encoding="utf-8"))
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmation["reviewer"], "包装同事")
        self.assertEqual(json.loads((self.project / "confirmation.json").read_text(encoding="utf-8")), confirmation)
        self.assertEqual(plan_digest(confirmed), plan_digest(self.plan))
        self.assertEqual((self.project / "manual.pdf").read_bytes(), old_pdf)
        self.assertEqual((self.project / "pdf_export.json").read_bytes(), old_receipt)
        self.pdf_current_mock.assert_called_once_with(self.project, confirmed)
        self.prepare_mock.assert_not_called()
        self.publish_mock.assert_not_called()

    def test_stale_export_loses_receipt_without_creating_a_new_pdf(self):
        self.pdf_current_mock.return_value = False
        old_pdf = (self.project / "manual.pdf").read_bytes()
        old_receipt = (self.project / "pdf_export.json").read_bytes()
        manual.confirm_project(self.project, export_document=False)
        self.assertEqual((self.project / "manual.pdf").read_bytes(), old_pdf)
        self.assertFalse((self.project / "pdf_export.json").exists())
        receipts = list((self.project / "history").glob("*/pdf_export.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].read_bytes(), old_receipt)
        self.prepare_mock.assert_not_called()
        self.publish_mock.assert_not_called()

    def test_no_previous_export_confirms_without_manufacturing_any_pdf_or_receipt(self):
        (self.project / "manual.pdf").unlink()
        (self.project / "pdf_export.json").unlink()
        manual.confirm_project(self.project, export_document=False)
        self.assertFalse((self.project / "manual.pdf").exists())
        self.assertFalse((self.project / "pdf_export.json").exists())
        self.pdf_current_mock.assert_not_called()
        self.prepare_mock.assert_not_called()

    def test_default_cli_confirmation_keeps_combined_export_behavior(self):
        confirmation = manual.confirm_project(self.project)
        self.prepare_mock.assert_called_once()
        prepared_plan = self.prepare_mock.call_args.args[1]
        self.assertEqual(prepared_plan["status"], "confirmed")
        self.assertEqual(prepared_plan["confirmation"], confirmation)
        self.publish_mock.assert_called_once_with(self.project, self.project / "prepared-export")
        self.pdf_current_mock.assert_not_called()

    def test_unreviewed_or_stale_figures_cannot_create_confirmation(self):
        before = (self.project / "steps.json").read_bytes()
        self.plan["steps"][1]["reviewed"] = False
        with self.assertRaisesRegex(ValueError, "Review each step"):
            manual.confirm_project(self.project, export_document=False)
        self.assertEqual((self.project / "steps.json").read_bytes(), before)
        self.assertFalse((self.project / "confirmation.json").exists())
        self.plan["steps"][1]["reviewed"] = True
        self.current_mock.side_effect = ValueError("stale current figures")
        with self.assertRaisesRegex(ValueError, "stale current"):
            manual.confirm_project(self.project, export_document=False)
        self.assertFalse((self.project / "confirmation.json").exists())
        self.prepare_mock.assert_not_called()
        self.publish_mock.assert_not_called()

    def test_default_export_failure_preserves_unconfirmed_plan(self):
        before = (self.project / "steps.json").read_bytes()
        self.prepare_mock.side_effect = ValueError("export failed")
        with self.assertRaisesRegex(ValueError, "export failed"):
            manual.confirm_project(self.project)
        self.assertEqual((self.project / "steps.json").read_bytes(), before)
        self.assertFalse((self.project / "confirmation.json").exists())

    def status_edit(self):
        self.plan["status"] = "confirmed"
        self.plan["confirmation"] = {"reviewer": "Packaging"}
        (self.project / "steps.json").write_text(json.dumps(self.plan), encoding="utf-8")
        candidate = deepcopy(self.plan)
        candidate["status"] = "draft"
        candidate.pop("confirmation")
        return candidate

    def test_application_status_only_save_never_prepares_exports_and_keeps_pdf_bytes(self):
        candidate = self.status_edit()
        old_pdf = (self.project / "manual.pdf").read_bytes()
        old_receipt = (self.project / "pdf_export.json").read_bytes()
        with patch("draft_planner.validate_plan"):
            saved = manual.save_plan(self.project, candidate, export_document=False)
        self.assertEqual(saved["status"], "draft")
        self.assertEqual(plan_digest(saved), plan_digest(self.plan))
        self.prepare_mock.assert_not_called()
        self.publish_mock.assert_not_called()
        self.assertEqual((self.project / "manual.pdf").read_bytes(), old_pdf)
        self.assertEqual((self.project / "pdf_export.json").read_bytes(), old_receipt)

    def test_default_status_only_save_keeps_combined_export_behavior(self):
        candidate = self.status_edit()
        with patch("draft_planner.validate_plan"):
            manual.save_plan(self.project, candidate)
        self.prepare_mock.assert_called_once()
        self.publish_mock.assert_called_once_with(self.project, self.project / "prepared-export")


if __name__ == "__main__":
    unittest.main()
