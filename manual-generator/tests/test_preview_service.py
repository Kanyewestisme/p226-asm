"""One-step publishing with mocked CAD, no PDFs or rendered images produced."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generic_render import plan_digest
from preview_service import preview_step, step_recipe_sha256


SOURCE = "a" * 64
INVENTORY = "b" * 64


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def asset(directory, sid, payload):
    result = {}
    for kind in ("svg", "png"):
        path = directory / f"{sid}.{kind}"
        data = f"{payload} {kind}".encode()
        path.write_bytes(data)
        result[f"{kind}_path"] = path.name
        result[f"{kind}_sha256"] = hashlib.sha256(data).hexdigest()
    return result


class PreviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.diagrams = self.project / "diagrams"
        self.diagrams.mkdir()
        self.analysis = {"source_sha256": SOURCE, "warnings": [], "contacts": [],
                         "parts": [{"id": "a", "bbox_min": [0, 0, 0], "bbox_max": [20, 20, 20], "solids": 1},
                                   {"id": "b", "bbox_min": [25, 0, 0], "bbox_max": [45, 20, 20], "solids": 1}]}
        def step(sid, assembled, moving, offsets):
            return {"id": sid, "title": sid, "instruction": "核对当前源模型位置。", "assembled_groups": assembled,
                    "moving_groups": moving, "offsets": offsets, "camera": [1, .7, 1], "up": [0, 1, 0],
                    "arrows": [], "focus_part_ids": [], "hidden_part_ids": [], "visibility_reason": "",
                    "warnings": [], "evidence": [], "reviewed": False}
        self.plan = {"schema_version": 1, "source": {"sha256": SOURCE}, "product": {"title": "用户产品"}, "status": "draft",
                     "groups": [{"id": "base", "label": "底座", "part_ids": ["a"], "basis": "source", "status": "proposed"},
                                {"id": "cap", "label": "上部件", "part_ids": ["b"], "basis": "source", "status": "proposed"}],
                     "style": {"camera": [1, .7, 1], "up": [0, 1, 0], "width": 1100, "height": 800,
                               "margin": 70, "hlr": "poly", "explode_ratio": .22},
                     "steps": [step("reference", [], ["base"], {"base": [0, 0, 0]}),
                               step("install", ["base"], ["cap"], {"cap": [10, 0, 0]}),
                               step("overview", ["base", "cap"], [], {})], "warnings": []}
        write_json(self.project / "steps.json", self.plan)
        entries = [{"step_id": step["id"], "status": "ready", **asset(self.diagrams, step["id"], "old " + step["id"])} for step in self.plan["steps"]]
        self.manifest = {"source_sha256": SOURCE, "inventory_sha256": INVENTORY,
                         "plan_sha256": plan_digest(self.plan), "complete": True, "entries": entries}
        write_json(self.diagrams / "manifest.json", self.manifest)
        # Existing output placeholders test archival, never author PDF content.
        for name in ("manual.pdf", "pdf_export.json", "instructions.json", "index.html"):
            (self.project / name).write_bytes(("old export " + name).encode())
        self.loader = patch("preview_service.load_project", side_effect=lambda project: (read_json(project / "steps.json"), deepcopy(self.analysis)))
        self.validator = patch("preview_service._validate", return_value=({}, {}, {}, [], INVENTORY, []))
        self.renderer = patch("preview_service.render_plan", side_effect=self.fake_render)
        self.loader.start()
        self.validator.start()
        self.render_mock = self.renderer.start()
        self.addCleanup(self.renderer.stop)
        self.addCleanup(self.validator.stop)
        self.addCleanup(self.loader.stop)

    def tearDown(self):
        self.temp.cleanup()

    def fake_render(self, plan, inventory, output, step_ids=None):
        self.assertEqual(len(step_ids), 1)
        self.assertEqual(inventory, self.project / "inventory")
        sid = step_ids[0]
        output.mkdir(parents=True)
        step = next(step for step in plan["steps"] if step["id"] == sid)
        groups = {group["id"]: group for group in plan["groups"]}
        members = {pid for gid in step["assembled_groups"] + step["moving_groups"] for pid in groups[gid]["part_ids"]}
        members -= set(plan.get("excluded_part_ids", [])) | set(step.get("hidden_part_ids", []))
        entry = {"step_id": sid, "status": "ready", "part_ids": sorted(members),
                 **asset(output, sid, "new " + step_recipe_sha256(plan, sid))}
        if step.get("focus_part_ids"):
            entry["focus"] = {"part_ids": step["focus_part_ids"], **asset(output, sid + "_focus", "new focus")}
        manifest = {"source_sha256": SOURCE, "inventory_sha256": INVENTORY,
                    "plan_sha256": plan_digest(plan), "entries": [entry], "complete": False}
        write_json(output / "manifest.json", manifest)
        return manifest

    def candidate(self):
        result = deepcopy(self.plan)
        result["steps"][1]["offsets"]["cap"] = [15, 0, 0]
        return result

    def live_bytes(self):
        return {name: (self.project / name).read_bytes() for name in
                ("steps.json", "diagrams/manifest.json", "diagrams/reference.svg", "diagrams/reference.png",
                 "diagrams/install.svg", "diagrams/install.png", "diagrams/overview.svg", "diagrams/overview.png")}

    def test_single_step_preserves_other_files_and_migrates_valid_legacy_recipes(self):
        before = self.live_bytes()
        result = preview_step(self.project, self.candidate(), "install")
        self.assertEqual(self.render_mock.call_args.kwargs["step_ids"], ["install"])
        self.assertEqual(result["rendered_step_ids"], ["install"])
        self.assertEqual(result["stale_step_ids"], [])
        self.assertTrue(result["manifest"]["complete"])
        self.assertEqual(result["manifest"]["plan_sha256"], plan_digest(result["plan"]))
        for sid in ("reference", "overview"):
            for extension in ("svg", "png"):
                self.assertEqual(before[f"diagrams/{sid}.{extension}"], (self.diagrams / f"{sid}.{extension}").read_bytes())
        self.assertNotEqual(before["diagrams/install.svg"], (self.diagrams / "install.svg").read_bytes())
        self.assertEqual(read_json(self.project / "steps.json"), result["plan"])
        for entry in result["manifest"]["entries"]:
            self.assertEqual(entry["recipe_sha256"], step_recipe_sha256(result["plan"], entry["step_id"]))
            self.assertFalse(entry["stale"])
        history = Path(result["history_path"])
        self.assertEqual((history / "diagrams/install.svg").read_bytes(), before["diagrams/install.svg"])
        self.assertFalse((history / "diagrams/reference.svg").exists())
        self.assertTrue((history / "manual.pdf").exists())
        self.assertFalse((self.project / "manual.pdf").exists())
        self.assertFalse(result["pdf_current"])

    def test_other_changed_step_stays_displayed_stale_until_its_own_render_completes(self):
        candidate = self.candidate()
        candidate["steps"][0]["camera"] = [-1, .7, 1]
        old_reference = (self.diagrams / "reference.png").read_bytes()
        first = preview_step(self.project, candidate, "install")
        self.assertEqual(first["stale_step_ids"], ["reference"])
        self.assertFalse(first["manifest"]["complete"])
        self.assertIsNone(first["manifest"]["plan_sha256"])
        self.assertEqual((self.diagrams / "reference.png").read_bytes(), old_reference)
        second = preview_step(self.project, first["plan"], "reference")
        self.assertEqual(second["stale_step_ids"], [])
        self.assertTrue(second["manifest"]["complete"])
        self.assertEqual(self.render_mock.call_count, 2)

    def test_unproven_legacy_plan_hash_does_not_certify_unrendered_images(self):
        self.manifest["plan_sha256"] = "unrelated plan"
        write_json(self.diagrams / "manifest.json", self.manifest)
        result = preview_step(self.project, self.candidate(), "install")
        self.assertEqual(result["stale_step_ids"], ["reference", "overview"])
        entries = {entry["step_id"]: entry for entry in result["manifest"]["entries"]}
        self.assertNotIn("recipe_sha256", entries["reference"])
        self.assertTrue(entries["reference"]["stale"])
        self.assertTrue((self.diagrams / "reference.svg").exists())

    def test_wrong_old_inventory_or_corrupt_image_cannot_migrate_current_recipe(self):
        for corruption in ("inventory", "asset"):
            manifest = deepcopy(self.manifest)
            if corruption == "inventory":
                manifest["inventory_sha256"] = "old inventory"
            else:
                (self.diagrams / "reference.svg").write_bytes(b"changed by another process")
            write_json(self.diagrams / "manifest.json", manifest)
            result = preview_step(self.project, self.candidate(), "install")
            self.assertIn("reference", result["stale_step_ids"])
            self.assertFalse(result["manifest"]["complete"])

    def test_failed_cad_does_not_publish_candidate_or_remove_displayed_assets(self):
        before = self.live_bytes()
        old_pdf = (self.project / "manual.pdf").read_bytes()
        def fail(*args, **kwargs):
            self.assertEqual(before, self.live_bytes())
            self.assertEqual(old_pdf, (self.project / "manual.pdf").read_bytes())
            raise RuntimeError("CAD failed")
        self.render_mock.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "CAD failed"):
            preview_step(self.project, self.candidate(), "install")
        self.assertEqual(before, self.live_bytes())
        self.assertEqual(old_pdf, (self.project / "manual.pdf").read_bytes())
        self.assertFalse(list(self.project.glob(".step-preview-*")))

    def test_incomplete_or_wrong_render_proof_preserves_previous_files(self):
        before = self.live_bytes()
        for field, value in (("source_sha256", "wrong"), ("plan_sha256", "wrong"), ("inventory_sha256", "wrong"), ("entries", [])):
            def wrong(*args, _field=field, _value=value, **kwargs):
                result = self.fake_render(*args, **kwargs)
                result[_field] = _value
                return result
            self.render_mock.side_effect = wrong
            with self.assertRaises(ValueError):
                preview_step(self.project, self.candidate(), "install")
            self.assertEqual(before, self.live_bytes())
        self.render_mock.side_effect = self.fake_render

    def test_step_render_cannot_publish_into_another_steps_filenames(self):
        before = self.live_bytes()
        def colliding(*args, **kwargs):
            result = self.fake_render(*args, **kwargs)
            output = args[2]
            replacement = asset(output, "reference", "wrong target")
            result["entries"][0].update(replacement)
            return result
        self.render_mock.side_effect = colliding
        with self.assertRaisesRegex(ValueError, "requested step"):
            preview_step(self.project, self.candidate(), "install")
        self.assertEqual(before, self.live_bytes())

    def test_requested_focus_is_published_and_missing_focus_proof_is_rejected(self):
        candidate = self.candidate()
        candidate["steps"][1]["focus_part_ids"] = ["a", "b"]
        result = preview_step(self.project, candidate, "install")
        entry = next(entry for entry in result["manifest"]["entries"] if entry["step_id"] == "install")
        self.assertEqual(entry["focus"]["part_ids"], ["a", "b"])
        self.assertTrue((self.diagrams / "install_focus.svg").exists())
        before = self.live_bytes()
        def missing(*args, **kwargs):
            result = self.fake_render(*args, **kwargs)
            result["entries"][0].pop("focus")
            return result
        self.render_mock.side_effect = missing
        with self.assertRaisesRegex(ValueError, "requested focus"):
            preview_step(self.project, candidate, "install")
        self.assertEqual(before, self.live_bytes())

    def test_removed_focus_is_archived_without_touching_other_diagrams(self):
        candidate = self.candidate()
        candidate["steps"][1]["focus_part_ids"] = ["b"]
        first = preview_step(self.project, candidate, "install")
        candidate = deepcopy(first["plan"])
        candidate["steps"][1]["focus_part_ids"] = []
        old_focus = (self.diagrams / "install_focus.png").read_bytes()
        second = preview_step(self.project, candidate, "install")
        self.assertFalse((self.diagrams / "install_focus.png").exists())
        self.assertEqual((Path(second["history_path"]) / "diagrams/install_focus.png").read_bytes(), old_focus)
        self.assertTrue((self.diagrams / "overview.png").exists())

    def test_source_or_template_switch_is_rejected_before_rendering(self):
        before = self.live_bytes()
        for edit in (lambda p: p["source"].update(sha256="c" * 64),
                     lambda p: p.update(pdf_template={"path": "new template"})):
            candidate = self.candidate()
            edit(candidate)
            with self.assertRaises(ValueError):
                preview_step(self.project, candidate, "install")
        self.render_mock.assert_not_called()
        self.assertEqual(before, self.live_bytes())

    def bind_fixture_template(self):
        self.plan["pdf_template"] = {
            "path": str(self.project / "original.pdf"), "sha256": "c" * 64, "source_sha256": SOURCE,
            "step_bindings": [{"step_id": step["id"], "title": step["title"], "instruction": step["instruction"],
                               "consumer": {}} for step in self.plan["steps"]],
            "figure_slots": [{"step_id": step["id"]} for step in self.plan["steps"]], "document_binding": {},
        }
        write_json(self.project / "steps.json", self.plan)
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        write_json(self.diagrams / "manifest.json", self.manifest)

    def test_fixed_template_text_order_and_binding_are_frozen_but_camera_is_editable(self):
        self.bind_fixture_template()
        for edit in (lambda p: p["steps"][1].update(title="changed title"),
                     lambda p: p["steps"][1].update(instruction="changed instruction"),
                     lambda p: p["steps"].reverse(), lambda p: p.pop("pdf_template"),
                     lambda p: p.update(manual_document={"title": "changed document"})):
            candidate = self.candidate()
            edit(candidate)
            with self.assertRaises(ValueError):
                preview_step(self.project, candidate, "install")
        self.render_mock.assert_not_called()
        candidate = self.candidate()
        candidate["steps"][1]["camera"] = [-1, 1, .7]
        result = preview_step(self.project, candidate, "install")
        self.assertTrue(result["export_ready"])
        self.assertEqual(result["plan"]["pdf_template"], self.plan["pdf_template"])

    def test_unknown_step_and_invalid_visibility_never_publish(self):
        before = self.live_bytes()
        with self.assertRaisesRegex(ValueError, "known step"):
            preview_step(self.project, self.candidate(), "not-a-step")
        candidate = self.candidate()
        candidate["steps"][1]["hidden_part_ids"] = ["b"]
        candidate["steps"][1]["visibility_reason"] = "User hides all moving geometry"
        with self.assertRaises(ValueError):
            preview_step(self.project, candidate, "install")
        self.assertEqual(before, self.live_bytes())
        self.render_mock.assert_not_called()

    def test_concurrent_project_edit_preserves_old_images_without_overwriting_the_edit(self):
        before = self.live_bytes()
        concurrent = deepcopy(self.plan)
        concurrent["product"]["title"] = "a concurrent edit"
        def concurrent_render(*args, **kwargs):
            result = self.fake_render(*args, **kwargs)
            write_json(self.project / "steps.json", concurrent)
            return result
        self.render_mock.side_effect = concurrent_render
        with self.assertRaisesRegex(ValueError, "changed during"):
            preview_step(self.project, self.candidate(), "install")
        self.assertEqual(read_json(self.project / "steps.json"), concurrent)
        for name, payload in before.items():
            if name != "steps.json":
                self.assertEqual((self.project / name).read_bytes(), payload)

    def test_recipe_ignores_other_steps_and_editorial_metadata_but_tracks_actual_drawing_inputs(self):
        original = step_recipe_sha256(self.plan, "install")
        candidate = deepcopy(self.plan)
        candidate["steps"][0]["camera"] = [-1, .8, 1]
        candidate["steps"][1]["title"] = "new text"
        candidate["steps"][1]["instruction"] = "new instruction"
        candidate["steps"][1]["warnings"] = ["new warning"]
        candidate["steps"][1]["reviewed"] = True
        candidate["style"]["illustration_rules"] = {"suggestion": "metadata"}
        self.assertEqual(original, step_recipe_sha256(candidate, "install"))
        for edit in (lambda p: p["steps"][1].update(camera=[-1, .7, 1]),
                     lambda p: p["steps"][1]["offsets"].update(cap=[15, 0, 0]),
                     lambda p: p["steps"][1].update(focus_part_ids=["a"]),
                     lambda p: p["steps"][1].update(hidden_part_ids=["a"]),
                     lambda p: p["style"].update(hlr="exact"),
                     lambda p: p["groups"][0]["part_ids"].append("b"),
                     lambda p: p.update(excluded_part_ids=["a"])):
            candidate = deepcopy(self.plan)
            edit(candidate)
            self.assertNotEqual(original, step_recipe_sha256(candidate, "install"))
        candidate = deepcopy(self.plan)
        candidate["steps"][1]["camera"] = [1.0, .7, 1.0]
        self.assertEqual(original, step_recipe_sha256(candidate, "install"))

    def test_changed_content_resets_confirmation_but_request_cannot_create_confirmation(self):
        self.plan["status"] = "confirmed"
        self.plan["confirmation"] = {"reviewer": "包装同事"}
        for step in self.plan["steps"]:
            step["reviewed"] = True
        write_json(self.project / "steps.json", self.plan)
        self.manifest["plan_sha256"] = plan_digest(self.plan)
        write_json(self.diagrams / "manifest.json", self.manifest)
        result = preview_step(self.project, self.candidate(), "install")
        self.assertEqual(result["plan"]["status"], "draft")
        self.assertNotIn("confirmation", result["plan"])
        self.assertTrue(all(step["reviewed"] is False for step in result["plan"]["steps"]))

    def test_failed_publication_rolls_back_changed_assets_and_manifest(self):
        before = self.live_bytes()
        original_replace = Path.replace
        failed = False
        def once_fail(path, target):
            nonlocal failed
            if not failed and Path(target) == self.project / "steps.json":
                failed = True
                raise OSError("simulated disk publication failure")
            return original_replace(path, target)
        with patch.object(Path, "replace", once_fail):
            with self.assertRaisesRegex(OSError, "publication failure"):
                preview_step(self.project, self.candidate(), "install")
        self.assertEqual(before, self.live_bytes())
        self.assertTrue((self.project / "manual.pdf").exists())


if __name__ == "__main__":
    unittest.main()
