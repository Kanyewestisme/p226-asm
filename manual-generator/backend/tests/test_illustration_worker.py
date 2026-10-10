"""Native HLR runs behind the same isolated worker boundary as STEP import."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app_server import Engine
from backend.cad_worker import run_preview, run_render


class IllustrationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.plan = {"source": {"sha256": "current"}, "steps": [{"id": "one"}]}

    def tearDown(self):
        self.temporary.cleanup()

    def test_preview_transfers_only_controlled_candidate_and_cleans_up_after_success(self):
        def worker(arguments, progress, error):
            self.assertEqual(arguments[1], "preview")
            candidate = Path(arguments[arguments.index("--candidate") + 1])
            self.assertEqual(candidate.parent, self.project)
            self.assertEqual(json.loads(candidate.read_text(encoding="utf-8")), self.plan)
            return {"plan": self.plan, "manifest": {"entries": []}}
        with patch.object(Engine, "_run_worker", side_effect=worker):
            result = Engine().preview(self.project, self.plan, "one", lambda *args: None)
        self.assertEqual(result["plan"], self.plan)
        self.assertFalse(list(self.project.glob(".candidate-*.json")))

    def test_failed_preview_leaves_old_image_and_cleans_candidate(self):
        old = self.project / "old.svg"
        old.write_bytes(b"previous source image")
        with patch.object(Engine, "_run_worker", side_effect=RuntimeError("worker failed")):
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                Engine().preview(self.project, self.plan, "one", lambda *args: None)
        self.assertEqual(old.read_bytes(), b"previous source image")
        self.assertFalse(list(self.project.glob(".candidate-*.json")))

    def test_preview_worker_rejects_arbitrary_candidate_paths_before_read(self):
        with self.assertRaisesRegex(ValueError, "受控"):
            run_preview(self.project, self.project.parent / "external.json", "one")

    def test_preview_worker_calls_atomic_single_step_service_without_pdf(self):
        candidate = self.project / (".candidate-" + "a" * 32 + ".json")
        candidate.write_text(json.dumps(self.plan), encoding="utf-8")
        calls = []
        def preview(project, plan, step_id):
            calls.append((project, plan, step_id))
            return {"manifest": {"entries": []}}
        with patch.dict(sys.modules, {"preview_service": SimpleNamespace(preview_step=preview)}), patch("backend.cad_worker.emit"):
            run_preview(self.project, candidate, "one")
        self.assertEqual(calls, [(self.project.resolve(), self.plan, "one")])
        self.assertFalse((self.project / "manual.pdf").exists())

    def test_render_worker_explicitly_disables_document_generation(self):
        calls = []
        def render(project, *, export_document):
            calls.append(export_document)
            return {"entries": []}
        with patch.dict(sys.modules, {"manual": SimpleNamespace(render_project=render)}), patch("backend.cad_worker.emit"):
            run_render(self.project)
        self.assertEqual(calls, [False])


if __name__ == "__main__":
    unittest.main()
