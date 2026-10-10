"""New text/workflow REST routes leave STP, SVG and PDF assets untouched."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_backend as fixtures
read_json, write_json = fixtures.read_json, fixtures.write_json


class LanguageBackendHTTPTests(unittest.TestCase):
    def setUp(self):
        self.provider_env = patch.dict(os.environ, {name: "" for name in ("MANUAL_TRANSLATION_BASE_URL", "MANUAL_TRANSLATION_MODEL", "MANUAL_TRANSLATION_API_KEY")})
        self.provider_env.start()
        fixtures.BackendHTTPTests.setUp(self)

    def tearDown(self):
        fixtures.BackendHTTPTests.tearDown(self)
        self.provider_env.stop()
    request = fixtures.BackendHTTPTests.request
    route = fixtures.BackendHTTPTests.route
    wait_job = fixtures.BackendHTTPTests.wait_job

    def test_languages_get_draft_save_and_download_do_not_render(self):
        self.plan["product"]["title"] = "E301通用安装说明书"
        self.plan["steps"][0]["title"] = "安装头枕"
        self.plan["steps"][0]["instruction"] = "安装头枕"
        write_json(self.project / "steps.json", self.plan)
        svg = next((self.project / "diagrams").glob("*.svg"))
        original = svg.read_bytes()
        with patch.object(self.server.app.store, "preserve_outputs", side_effect=AssertionError("Text edits must not archive geometry")):
            status, doc = self.request("GET", self.route("languages"))
            self.assertEqual(status, 200)
            body = {"source_sha256": doc["source_sha256"], "base_sha256": doc["base_sha256"], "locales": ["en"]}
            status, submitted = self.request("POST", self.route("translate"), body)
            self.assertEqual(status, 202)
            job = self.wait_job(submitted["job_id"])
            self.assertEqual(job["status"], "succeeded")
            row = next(row for row in job["result"]["entries"] if row["key"].endswith(".instruction") and row["scope"] == "consumer")
            status, submitted = self.request("POST", self.route("languages"), body | {"translations": [{"key": row["key"], "locale": "en", "source_sha256": row["source_sha256"], "text": "Attach the headrest", "status": "reviewed"}]})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(submitted["job_id"])["status"], "succeeded")
        status, downloaded = self.request("GET", f"/projects/{self.identity}/languages.json")
        self.assertEqual(status, 200)
        self.assertEqual(downloaded["source_sha256"], "source-a")
        self.assertEqual(svg.read_bytes(), original)
        self.assertFalse((self.project / "manual.pdf").exists())
        self.assertEqual(self.engine.calls, [])

    def test_old_translation_source_is_rejected_without_mutating_language_doc(self):
        status, doc = self.request("GET", self.route("languages"))
        self.assertEqual(status, 200)
        status, submitted = self.request("POST", self.route("translate"), {"source_sha256": "old-source", "base_sha256": doc["base_sha256"], "locales": ["en"]})
        self.assertEqual(status, 202)
        job = self.wait_job(submitted["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("STP", job["error"])
        self.assertFalse((self.project / "languages.json").exists())

    def test_workflow_requires_current_source_and_keeps_editable_plan_path(self):
        status, rejected = self.request("POST", self.route("workflow"), {"source_sha256": "old-source", "operation": {"action": "rename_group"}})
        self.assertEqual(status, 400)
        self.assertIn("STP", rejected["error"])
        group_id = self.plan["groups"][0]["id"]
        changed = json.loads(json.dumps(self.plan))
        changed["groups"][0]["label"] = "Seat assembly"
        with patch.object(self.engine, "workflow", return_value=changed) as workflow:
            status, submitted = self.request("POST", self.route("workflow"), {"source_sha256": "source-a", "operation": {"action": "rename_group", "group_id": group_id, "label": "Seat assembly"}})
            self.assertEqual(status, 202)
            result = self.wait_job(submitted["job_id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["result"]["plan"]["groups"][0]["label"], "Seat assembly")
            workflow.assert_called_once()

    def test_export_language_is_async_and_download_never_serves_stale_receipt(self):
        result = {"locale": "en", "filename": "manual-en.pdf", "source_sha256": "source-a", "draft": True}
        with patch.object(self.engine, "export_language", return_value=result) as exporter, patch.object(self.server.app.store, "preserve_outputs", side_effect=AssertionError("Language PDF must not archive the original")):
            status, submitted = self.request("POST", self.route("export-language"), {"source_sha256": "source-a", "locale": "en", "draft": True})
            self.assertEqual(status, 202)
            job = self.wait_job(submitted["job_id"])
            self.assertEqual(job["result"]["filename"], "manual-en.pdf")
            exporter.assert_called_once()
        path = self.project / "manual-en.pdf"
        path.write_bytes(b"%PDF language result")
        checker = SimpleNamespace(is_current_language_pdf=lambda project, locale: False)
        with patch.dict(sys.modules, {"template_translation": checker}):
            status, response = self.request("GET", f"/projects/{self.identity}/manual-en.pdf")
            self.assertEqual(status, 400)
            self.assertIn("过期", response["error"])
        checker.is_current_language_pdf = lambda project, locale: True
        with patch.dict(sys.modules, {"template_translation": checker}):
            status, response = self.request("GET", f"/projects/{self.identity}/manual-en.pdf")
            self.assertEqual(status, 200)
            self.assertEqual(response, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
