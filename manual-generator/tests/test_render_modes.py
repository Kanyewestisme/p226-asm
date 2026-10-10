"""Application illustration jobs cannot implicitly export a document."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manual


class RenderModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "diagrams").mkdir()
        self.plan = {"product": {"title": "Current product"}, "source": {"sha256": "a" * 64},
                     "pdf_template": {"fixed": True}, "steps": [{"id": "one"}], "status": "draft"}
        self.analysis = {"parts": []}
        (self.project / "steps.json").write_text(json.dumps(self.plan))
        self.old_files = {"diagrams/one.svg": b"previous diagram placeholder",
                          "diagrams/manifest.json": b'{"old":true}', "manual.pdf": b"previous document placeholder",
                          "pdf_export.json": b"previous receipt", "instructions.json": b"previous instructions",
                          "index.html": b"previous exported HTML"}
        for name, content in self.old_files.items():
            (self.project / name).write_bytes(content)
        self.loader = patch("manual.load_project", return_value=(self.plan, self.analysis))
        self.render_patch = patch("generic_render.render_plan", side_effect=self.mock_render)
        self.cover_patch = patch("cover_render.render_cover", return_value=None)
        self.export_patch = patch("review_output.export_preview", side_effect=self.mock_export)
        self.manifest_patch = patch("manual.current_manifest", side_effect=lambda project, plan: json.loads((project / "diagrams/manifest.json").read_text()))
        self.load_mock = self.loader.start()
        self.render_mock = self.render_patch.start()
        self.cover_mock = self.cover_patch.start()
        self.export_mock = self.export_patch.start()
        self.manifest_patch.start()
        for item in (self.loader, self.render_patch, self.cover_patch, self.export_patch, self.manifest_patch):
            self.addCleanup(item.stop)

    def tearDown(self):
        self.temp.cleanup()

    def mock_render(self, plan, inventory, output):
        output.mkdir(parents=True)
        (output / "one.svg").write_bytes(b"new diagram placeholder")
        manifest = {"complete": True, "source_sha256": plan["source"]["sha256"], "entries": [{"step_id": "one"}]}
        (output / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def mock_export(self, project, plan, manifest, **kwargs):
        for name in ("manual.pdf", "pdf_export.json", "instructions.json", "index.html"):
            (project / name).write_bytes(b"new combined export placeholder")

    def assert_old_outputs(self):
        for name, payload in self.old_files.items():
            self.assertEqual((self.project / name).read_bytes(), payload)

    def test_illustrations_only_never_call_document_export_and_archive_old_exports(self):
        result = manual.render_project(self.project, export_document=False)
        self.export_mock.assert_not_called()
        self.assertTrue(result["complete"])
        self.assertEqual((self.project / "diagrams/one.svg").read_bytes(), b"new diagram placeholder")
        for name in ("manual.pdf", "pdf_export.json", "instructions.json", "index.html"):
            self.assertFalse((self.project / name).exists())
            self.assertTrue(list((self.project / "history").glob("*/" + name)))
        self.assertEqual(json.loads((self.project / "steps.json").read_text()), self.plan)

    def test_default_cli_behavior_still_generates_combined_exports(self):
        manual.render_project(self.project)
        self.export_mock.assert_called_once()
        for name in ("manual.pdf", "pdf_export.json", "instructions.json", "index.html"):
            self.assertEqual((self.project / name).read_bytes(), b"new combined export placeholder")

    def test_render_failure_preserves_all_previous_display_and_export_files(self):
        self.render_mock.side_effect = RuntimeError("CAD render failed")
        with self.assertRaisesRegex(RuntimeError, "CAD render failed"):
            manual.render_project(self.project, export_document=False)
        self.export_mock.assert_not_called()
        self.assert_old_outputs()

    def test_source_or_plan_change_during_render_does_not_publish_partial_assets(self):
        self.load_mock.side_effect = [(self.plan, self.analysis), ValueError("Source changed")]
        with self.assertRaisesRegex(ValueError, "Source changed"):
            manual.render_project(self.project, export_document=False)
        self.export_mock.assert_not_called()
        self.assert_old_outputs()
        changed = deepcopy(self.plan)
        changed["product"]["title"] = "Concurrent edit"
        self.load_mock.side_effect = [(self.plan, self.analysis), (changed, self.analysis)]
        with self.assertRaisesRegex(ValueError, "draft changed during"):
            manual.render_project(self.project, export_document=False)
        self.assert_old_outputs()

    def test_default_document_failure_still_preserves_previous_whole_preview(self):
        self.export_mock.side_effect = ValueError("Document export failed")
        with self.assertRaisesRegex(ValueError, "Document export failed"):
            manual.render_project(self.project)
        self.assert_old_outputs()

    def test_illustration_publication_failure_rolls_back_displayed_images(self):
        original_replace = Path.replace
        failed = False
        def fail_once(path, target):
            nonlocal failed
            if not failed and Path(target) == self.project / "diagrams/manifest.json":
                failed = True
                raise OSError("simulated publication failure")
            return original_replace(path, target)
        with patch.object(Path, "replace", fail_once):
            with self.assertRaisesRegex(OSError, "publication failure"):
                manual.render_project(self.project, export_document=False)
        self.export_mock.assert_not_called()
        self.assert_old_outputs()


if __name__ == "__main__":
    unittest.main()
