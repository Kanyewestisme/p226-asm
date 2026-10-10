"""HTTP integration without importing a STEP or generating any CAD illustrations."""
from copy import deepcopy
import hashlib
import http.client
import json
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from backend.app_server import Engine, create_server
from backend.project_store import ProjectStore, public_value, read_json, write_json
from draft_planner import build_plan
from generic_render import plan_digest


def fixture(project, title="Reusable product"):
    project.mkdir(parents=True)
    analysis = {"source_sha256": "source-a", "parts": [{"id": "source-a:part", "index": 0, "name": "Part",
                 "bbox_min": [0, 0, 0], "bbox_max": [10, 10, 10], "solids": 1}], "contacts": [], "warnings": []}
    plan = build_plan(analysis, title)
    write_json(project / "steps.json", plan)
    write_json(project / "inventory" / "assembly_parts.json", analysis["parts"])
    entries = []
    for step in plan["steps"]:
        svg, png = (step["id"] + suffix for suffix in (".svg", ".png"))
        (project / "diagrams").mkdir(exist_ok=True)
        (project / "diagrams" / svg).write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        (project / "diagrams" / png).write_bytes(b"existing image bytes")
        entries.append({"step_id": step["id"], "status": "ready", "svg_path": svg, "png_path": png,
                        "svg_sha256": hashlib.sha256((project / "diagrams" / svg).read_bytes()).hexdigest(),
                        "png_sha256": hashlib.sha256((project / "diagrams" / png).read_bytes()).hexdigest()})
    write_json(project / "diagrams" / "manifest.json", {"source_sha256": "source-a", "plan_sha256": plan_digest(plan),
                                                        "complete": True, "entries": entries})
    return plan


class FakeEngine(Engine):
    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.scene_data = None
        self.calls = []
        self.fail_draft = False
        self.draft_inputs = []

    def draft(self, source, project, title, progress):
        self.calls.append("draft")
        self.draft_inputs.append((source, project))
        self.gate.wait(3)
        project.mkdir(parents=True, exist_ok=True)
        if self.fail_draft:
            write_json(project / "source.json", {"path": str(source)})
            (project / ".unfinished-import").mkdir(exist_ok=True)
            raise ValueError("Bnd_Box is void")
        plan = {"source": {"sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, "product": {"title": title},
                "steps": [], "status": "draft"}
        write_json(project / "steps.json", plan)
        return plan

    def save(self, project, plan, progress):
        self.calls.append("save")
        self.gate.wait(3)
        if plan.get("fail"):
            raise ValueError("Rejected edit")
        write_json(project / "steps.json", plan)
        return plan

    def preview(self, project, plan, step_id, progress):
        self.calls.append("preview:" + step_id)
        self.gate.wait(3)
        if plan.get("fail"):
            raise ValueError("Preview failed")
        write_json(project / "steps.json", plan)
        return {"plan": plan, "manifest": read_json(project / "diagrams" / "manifest.json"), "pdf_current": False}

    def render(self, project, progress):
        self.calls.append("render")
        return read_json(project / "diagrams" / "manifest.json")

    def confirm(self, project, reviewer, progress):
        self.calls.append("confirm")
        return {"reviewer": reviewer}

    def bind(self, project, template, config, progress):
        self.calls.append("bind")
        plan = read_json(project / "steps.json")
        plan["pdf_template"] = config
        write_json(project / "steps.json", plan)
        return plan

    def scene_cached(self, project):
        return self.scene_data

    def scene(self, project, progress):
        self.calls.append("scene")
        self.gate.wait(3)
        self.scene_data = {"source_sha256": "source-a", "parts": [], "triangle_count": 0}
        return {"source_sha256": "source-a", "part_count": 0, "triangle_count": 0}

    def template_metadata(self, path):
        if not path.read_bytes().startswith(b"%PDF-2-pages"):
            raise ValueError("原模板必须是两页 PDF")
        return {"page_count": 2, "pages": [{"width": 1190, "height": 842}, {"width": 1190, "height": 842}]}

    def template_page(self, path, page, output):
        self.calls.append("template-page")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"cached template preview")


class BackendHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "external-existing-project"
        self.plan = fixture(self.project)
        self.frontend = self.root / "frontend"
        self.frontend.mkdir()
        (self.frontend / "index.html").write_text("<html>Generic app</html>", encoding="utf-8")
        (self.frontend / "app.js").write_text("export const generic = true;", encoding="utf-8")
        self.engine = FakeEngine()
        self.server = create_server(self.root / "data", port=0, frontend_dir=self.frontend,
                                    register_projects=[self.project], engine=self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, boot = self.request("GET", "/api/bootstrap", auth=False)
        self.assertEqual(status, 200)
        self.token = boot["token"]
        self.identity = boot["active_project_id"]

    def tearDown(self):
        self.engine.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.temp.cleanup()

    def request(self, method, path, body=None, *, auth=True, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        supplied = dict(headers or {})
        if auth and hasattr(self, "token"):
            supplied["X-App-Token"] = self.token
        if raw is not None:
            payload = raw
            supplied.setdefault("Content-Type", "application/octet-stream")
        elif body is not None:
            payload = json.dumps(body).encode("utf-8")
            supplied.setdefault("Content-Type", "application/json")
        else:
            payload = None
        connection.request(method, path, body=payload, headers=supplied)
        response = connection.getresponse()
        data = response.read()
        result = json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data
        status = response.status
        connection.close()
        return status, result

    def route(self, operation=""):
        return f"/api/projects/{self.identity}" + ("/" + operation if operation else "")

    def wait_job(self, identity):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, job = self.request("GET", "/api/jobs/" + identity)
            self.assertEqual(status, 200)
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.01)
        self.fail("Job did not finish")

    def test_bootstrap_registers_without_copying_models_and_supports_multiple_projects(self):
        self.assertEqual(self.server.app.store.workspace(self.identity), self.project.resolve())
        self.assertFalse((self.root / "data" / "projects" / self.identity).exists())
        status, created = self.request("POST", "/api/projects", {"title": "Another product"})
        self.assertEqual(status, 200)
        status, response = self.request("GET", "/api/projects")
        self.assertEqual(len(response["projects"]), 2)
        status, state = self.request("GET", "/api/projects/" + created["project"]["id"])
        self.assertIsNone(state["plan"])
        self.assertEqual(state["part_catalog"], [])
        self.assertNotIn(str(self.root), json.dumps(state))

    def test_token_origin_host_and_static_traversal_are_checked(self):
        # Real clients must receive the rejection, not an unread-body Windows
        # socket reset. Repeat with payloads larger than the receive buffer.
        for size in (3, 100, 9000, 32000) * 5:
            status, rejection = self.request("POST", "/api/projects", {"title": "x" * size}, auth=False)
            self.assertEqual(status, 403)
            self.assertIn("error", rejection)
        self.assertEqual(self.request("GET", self.route(), auth=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", auth=False, headers={"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", auth=False, headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/assets/app.js", auth=False)[0], 200)
        self.assertEqual(self.request("GET", "/assets/%2e%2e/data/projects.json", auth=False)[0], 404)
        self.assertEqual(self.request("GET", f"/projects/{self.identity}/diagrams/%2e%2e/steps.json", auth=False)[0], 404)

    def test_preview_is_async_reads_remain_responsive_and_project_is_exclusive(self):
        self.engine.gate.clear()
        status, result = self.request("POST", self.route("preview"), {"plan": self.plan, "step_id": self.plan["steps"][0]["id"]})
        self.assertEqual(status, 202)
        started = time.monotonic()
        status, state = self.request("GET", self.route())
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(state["active_jobs"])
        self.assertEqual(self.request("POST", self.route("plan"), {"plan": self.plan})[0], 409)
        self.engine.gate.set()
        self.assertEqual(self.wait_job(result["job_id"])["status"], "succeeded")
        self.assertEqual(self.engine.calls, ["preview:" + self.plan["steps"][0]["id"]])

    def test_failed_preview_preserves_existing_file_bytes(self):
        before = {path.name: path.read_bytes() for path in (self.project / "diagrams").iterdir()}
        bad = deepcopy(self.plan)
        bad["fail"] = True
        status, queued = self.request("POST", self.route("preview"), {"plan": bad, "step_id": self.plan["steps"][0]["id"]})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_job(queued["job_id"])["status"], "failed")
        self.assertEqual(before, {path.name: path.read_bytes() for path in (self.project / "diagrams").iterdir()})
        self.assertEqual(read_json(self.project / "steps.json"), self.plan)

    def test_stale_manifest_keeps_previous_diagrams_available(self):
        edited = deepcopy(self.plan)
        edited["steps"][0]["offsets"][edited["steps"][0]["moving_groups"][0]] = [3, 0, 0]
        write_json(self.project / "steps.json", edited)
        status, state = self.request("GET", self.route())
        self.assertEqual(status, 200)
        self.assertTrue(state["stale"])
        self.assertIsNotNone(state["manifest"])
        self.assertFalse(state["pdf_ready"])
        name = state["manifest"]["entries"][0]["svg_path"]
        self.assertEqual(self.request("GET", f"/projects/{self.identity}/diagrams/{name}", auth=False)[0], 200)

    def test_source_upload_creates_configuration_only_then_switches_version(self):
        old_workspace = self.server.app.store.workspace(self.identity)
        old_plan_bytes = (old_workspace / "steps.json").read_bytes()
        status, queued = self.request("POST", self.route("source"), raw=b"new STP content",
                                      headers={"X-File-Name": quote("new model.stp")})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_job(queued["job_id"])["status"], "succeeded")
        new_workspace = self.server.app.store.workspace(self.identity)
        self.assertNotEqual(old_workspace, new_workspace)
        self.assertEqual((old_workspace / "steps.json").read_bytes(), old_plan_bytes)
        self.assertFalse((new_workspace / "diagrams").exists())
        self.assertFalse((new_workspace / "manual.pdf").exists())
        self.assertEqual(self.engine.calls, ["draft"])
        status, state = self.request("GET", self.route())
        self.assertEqual(state["source_filename"], "new model.stp")
        self.assertTrue(state["stale"])
        self.assertIsNotNone(state["manifest"])
        self.assertIsNone(state["pending_source_info"])

    def test_source_revision_keeps_template_file_pending_without_old_bindings_or_pdf(self):
        for scenario in ("bound", "already-pending", "damaged"):
            with self.subTest(scenario=scenario):
                # Each scenario starts from the same registered old project.
                self.server.app.store.update(self.identity, workspace=str(self.project.resolve()),
                                             external=True, pending_template=None, revisions=[])
                template = self.root / "provided-template.pdf"
                original_bytes = b"%PDF-2-pages provided original"
                template.write_bytes(original_bytes)
                old = deepcopy(self.plan)
                old["pdf_template"] = {"path": str(template), "sha256": hashlib.sha256(original_bytes).hexdigest(),
                                       "source_sha256": "source-a", "figure_slots": [{"step_id": "old-step"}],
                                       "step_bindings": [{"step_id": "old-step"}]}
                write_json(self.project / "steps.json", old)
                expected_bytes, expected_filename = original_bytes, template.name
                if scenario == "already-pending":
                    expected_bytes, expected_filename = b"%PDF-2-pages newly provided", "new-template.pdf"
                    self.assertEqual(self.request("POST", self.route("template"), raw=expected_bytes,
                                                  headers={"X-File-Name": expected_filename})[0], 200)
                elif scenario == "damaged":
                    template.write_bytes(b"%PDF-2-pages changed since binding")
                status, queued = self.request("POST", self.route("source"), raw=("new STP " + scenario).encode(),
                                              headers={"X-File-Name": "new.stp"})
                self.assertEqual(status, 202)
                job = self.wait_job(queued["job_id"])
                self.assertEqual(job["status"], "succeeded")
                new_workspace = self.server.app.store.workspace(self.identity)
                new_plan = read_json(new_workspace / "steps.json")
                self.assertNotIn("pdf_template", new_plan)
                self.assertFalse((new_workspace / "manual.pdf").exists())
                self.assertFalse((new_workspace / "diagrams").exists())
                state = self.request("GET", self.route())[1]
                self.assertIsNone(state["bound_template_info"])
                pending = self.server.app.store.get(self.identity).get("pending_template")
                if scenario == "damaged":
                    self.assertIsNone(pending)
                    self.assertTrue(job["result"]["template_warning"])
                    self.assertIn("原模板无法保留", job["result"]["revision_warning"])
                else:
                    self.assertEqual(Path(pending["path"]).read_bytes(), expected_bytes)
                    self.assertEqual(pending["filename"], expected_filename)
                    self.assertNotIn("source_sha256", pending)
                    self.assertNotIn("figure_slots", pending)
                    self.assertNotIn("step_bindings", pending)
                    self.assertTrue(state["pending_template_info"]["pending"])
                    self.assertFalse(state["pending_template_info"]["bound"])
                    self.assertFalse(state["pdf_ready"])

    def test_failed_source_can_retry_original_upload_after_restart_without_touching_old_project(self):
        self.engine.fail_draft = True
        before = (self.project / "steps.json").read_bytes()
        status, queued = self.request("POST", self.route("source"), raw=b"new STP content",
                                      headers={"X-File-Name": "new.stp"})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_job(queued["job_id"])["status"], "failed")
        self.assertEqual(self.server.app.store.workspace(self.identity), self.project.resolve())
        self.assertEqual((self.project / "steps.json").read_bytes(), before)
        status, state = self.request("GET", self.route())
        self.assertEqual(state["pending_source_info"]["status"], "failed")
        self.assertEqual(state["pending_source_info"]["filename"], "new.stp")
        self.assertTrue(state["pending_source_info"]["retry_available"])
        self.assertNotIn(str(self.root), json.dumps(state))
        # Registry state is sufficient; the original in-memory job is unused.
        self.server.app.store = ProjectStore(self.root / "data")
        self.engine.fail_draft = False
        status, retried = self.request("POST", self.route("retry-source"), {})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait_job(retried["job_id"])["status"], "succeeded")
        self.assertEqual(self.engine.draft_inputs[0], self.engine.draft_inputs[1])
        self.assertEqual((self.project / "steps.json").read_bytes(), before)
        self.assertIsNone(self.server.app.store.get(self.identity)["pending_source"])
        self.assertTrue(list((self.root / "data" / "r" / self.identity[:12]).iterdir()))

    def test_retry_checks_source_digest_and_does_not_accept_browser_paths(self):
        self.engine.fail_draft = True
        _, queued = self.request("POST", self.route("source"), raw=b"new STP content",
                                  headers={"X-File-Name": "new.stp"})
        self.wait_job(queued["job_id"])
        pending = self.server.app.store.get(self.identity)["pending_source"]
        self.assertEqual(self.request("POST", self.route("retry-source"), {"path": "C:/other.stp"})[0], 400)
        # Same-size edits still fail the SHA check; no CAD import is started.
        Path(pending["upload"]["path"]).write_bytes(b"bad STP content")
        self.engine.fail_draft = False
        status, retried = self.request("POST", self.route("retry-source"), {})
        self.assertEqual(status, 202)
        job = self.wait_job(retried["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("内容发生变化", job["error"])
        self.assertEqual(self.engine.calls, ["draft"])
        self.assertEqual(self.server.app.store.workspace(self.identity), self.project.resolve())

    def test_completed_inventory_is_kept_and_partial_attempts_are_archived(self):
        store = self.server.app.store
        upload = store.upload(self.identity, "source", "new.stp", io.BytesIO(b"new"), 3)
        workspace = store.new_workspace(self.identity)
        store.set_pending_source(self.identity, upload, workspace)
        write_json(workspace / "inventory" / "import_complete.json", {"source_sha256": upload["sha256"]})
        (workspace / "inventory" / "part.brep").write_bytes(b"completed geometry")
        write_json(workspace / "source.json", {"sha256": upload["sha256"]})
        (workspace / ".interrupted-analysis").mkdir()
        store.prepare_pending_workspace(self.identity, store.pending_source(self.identity, verify_digest=True))
        self.assertEqual({path.name for path in workspace.iterdir()}, {"inventory", "source.json"})
        self.assertEqual((workspace / "inventory" / "part.brep").read_bytes(), b"completed geometry")
        outsider = self.root / "other-source.stp"
        outsider.write_bytes(b"new")
        with self.assertRaisesRegex(ValueError, "受控上传目录"):
            store.set_pending_source(self.identity, upload | {"path": str(outsider)}, workspace)

    def test_upload_rejects_path_names_and_wrong_formats(self):
        for filename in ("../model.stp", "model.exe", "folder\\model.stp"):
            self.assertEqual(self.request("POST", self.route("source"), raw=b"data", headers={"X-File-Name": quote(filename)})[0], 400)

    def test_template_pending_does_not_override_binding_and_preview_is_cached(self):
        bound_path = self.root / "old-template.pdf"
        bound_path.write_bytes(b"%PDF-2-pages old")
        plan = deepcopy(self.plan)
        plan["pdf_template"] = {"path": str(bound_path), "sha256": hashlib.sha256(bound_path.read_bytes()).hexdigest()}
        write_json(self.project / "steps.json", plan)
        status, uploaded = self.request("POST", self.route("template"), raw=b"%PDF-2-pages new",
                                        headers={"X-File-Name": quote("new template.pdf")})
        self.assertEqual(status, 200)
        status, state = self.request("GET", self.route())
        self.assertTrue(state["pending_template_info"]["pending"])
        self.assertTrue(state["bound_template_info"]["bound"])
        self.assertNotEqual(state["pending_template_info"]["sha256"], state["bound_template_info"]["sha256"])
        self.assertNotIn(str(self.root), json.dumps(state))
        self.assertEqual(read_json(self.project / "steps.json")["pdf_template"]["path"], str(bound_path))
        preview = f"/projects/{self.identity}/template/page/2.png"
        self.assertEqual(self.request("GET", preview, auth=False)[0], 200)
        self.assertEqual(self.request("GET", preview, auth=False)[0], 200)
        self.assertEqual(self.engine.calls.count("template-page"), 1)
        self.assertEqual(self.request("GET", f"/projects/{self.identity}/template/page/3.png", auth=False)[0], 404)
        self.assertEqual(self.request("POST", self.route("template"), raw=b"%PDF one page",
                                      headers={"X-File-Name": "bad.pdf"})[0], 400)
        self.assertEqual(self.server.app.store.get(self.identity)["pending_template"]["sha256"], uploaded["template_info"]["sha256"])

    def test_bind_uses_controlled_upload_path_and_fills_source_digest(self):
        self.request("POST", self.route("template"), raw=b"%PDF-2-pages new", headers={"X-File-Name": "original.pdf"})
        status, queued = self.request("POST", self.route("bind-template"), {"config": {"path": "C:/arbitrary.pdf", "figure_slots": []}})
        self.assertEqual(status, 202)
        job = self.wait_job(queued["job_id"])
        self.assertEqual(job["status"], "succeeded")
        config = read_json(self.project / "steps.json")["pdf_template"]
        self.assertNotEqual(config["path"], "C:/arbitrary.pdf")
        self.assertEqual(config["source_sha256"], "source-a")
        self.assertEqual(config["page_count"], 2)
        self.assertNotIn(str(self.root), json.dumps(job))

    def test_render_and_confirm_can_save_supplied_plan_in_one_job(self):
        for action, expected in (("render", ["save", "render"]), ("confirm", ["save", "confirm"])):
            self.engine.calls.clear()
            status, queued = self.request("POST", self.route(action), {"plan": self.plan})
            self.assertEqual(status, 202)
            self.assertEqual(self.wait_job(queued["job_id"])["status"], "succeeded")
            self.assertEqual(self.engine.calls, expected)

    def test_scene_202_handshake_then_200_and_only_one_job(self):
        self.engine.gate.clear()
        status, queued = self.request("GET", self.route("scene"))
        self.assertEqual(status, 202)
        self.assertEqual(queued["job_type"], "scene")
        status, repeated = self.request("GET", self.route("scene"))
        self.assertEqual(status, 202)
        self.assertEqual(queued["job_id"], repeated["job_id"])
        self.engine.gate.set()
        self.assertEqual(self.wait_job(queued["job_id"])["status"], "succeeded")
        status, scene = self.request("GET", self.route("scene"))
        self.assertEqual(status, 200)
        self.assertEqual(scene["source_sha256"], "source-a")
        self.assertEqual(self.engine.calls, ["scene"])

    def test_draft_runs_in_child_process_while_job_gets_remain_responsive(self):
        # Stand-in child exercises actual subprocess isolation without parsing
        # the user's large STEP a second time. Native GIL-blocking CAD work is
        # confined to this same child boundary in production.
        worker_root = self.root / "worker-engine"
        (worker_root / "backend").mkdir(parents=True)
        worker = worker_root / "backend" / "cad_worker.py"
        worker.write_text("""import argparse, hashlib, json, os, pathlib, sys, time
p=argparse.ArgumentParser()
p.add_argument('--operation')
p.add_argument('--source');p.add_argument('--project');p.add_argument('--title')
a=p.parse_args()
print('MANUAL_WORKER_JSON '+json.dumps({'event':'progress','message':'独立进程正在导入'}),flush=True)
until=time.monotonic()+0.65
while time.monotonic()<until: pass
project=pathlib.Path(a.project)
plan={'source':{'sha256':hashlib.sha256(pathlib.Path(a.source).read_bytes()).hexdigest()},'product':{'title':a.title},'steps':[],'status':'draft','worker_pid':os.getpid()}
(project/'steps.json').write_text(json.dumps(plan),encoding='utf-8')
print('MANUAL_WORKER_JSON '+json.dumps({'event':'succeeded'}),flush=True)
""", encoding="utf-8")
        self.server.app.engine = Engine()
        with patch("backend.app_server.ROOT", worker_root):
            status, queued = self.request("POST", self.route("source"), raw=b"small child fixture",
                                          headers={"X-File-Name": "new.stp"})
            self.assertEqual(status, 202)
            reads = 0
            while True:
                started = time.monotonic()
                status, job = self.request("GET", "/api/jobs/" + queued["job_id"])
                self.assertEqual(status, 200)
                self.assertLess(time.monotonic() - started, 0.3)
                reads += 1
                if job["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.025)
        self.assertGreater(reads, 5)
        self.assertEqual(job["status"], "succeeded", job)
        import os
        self.assertNotEqual(job["result"]["plan"]["worker_pid"], os.getpid())

    def test_worker_failure_keeps_error_and_original_project(self):
        worker_root = self.root / "worker-engine"
        (worker_root / "backend").mkdir(parents=True)
        (worker_root / "backend" / "cad_worker.py").write_text(
            "import json,sys\nprint('MANUAL_WORKER_JSON '+json.dumps({'event':'failed','error':'Specific import failure'}),flush=True)\nsys.exit(1)\n",
            encoding="utf-8")
        self.server.app.engine = Engine()
        with patch("backend.app_server.ROOT", worker_root):
            status, queued = self.request("POST", self.route("source"), raw=b"failure fixture",
                                          headers={"X-File-Name": "new.stp"})
            self.assertEqual(status, 202)
            job = self.wait_job(queued["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "Specific import failure")
        self.assertEqual(self.server.app.store.workspace(self.identity), self.project.resolve())
        self.assertEqual(self.server.app.store.get(self.identity)["pending_source"]["status"], "failed")

    def test_scene_child_keeps_job_gets_responsive_and_only_returns_metadata(self):
        worker_root = self.root / "worker-engine"
        (worker_root / "backend").mkdir(parents=True)
        (worker_root / "backend" / "cad_worker.py").write_text("""import argparse,json,pathlib,time
p=argparse.ArgumentParser();p.add_argument('--operation');p.add_argument('--project');a=p.parse_args()
assert a.operation=='scene'
print('MANUAL_WORKER_JSON '+json.dumps({'event':'progress','message':'正在准备三维预览：已处理 0/1 个零件。','current':0,'total':1}),flush=True)
until=time.monotonic()+0.65
while time.monotonic()<until: pass
target=pathlib.Path(a.project)/'scene';target.mkdir()
scene={'source_sha256':'source-a','parts':[{'id':'source-a:part','indices':[0,1,2]}],'triangle_count':1}
(target/'scene.json').write_text(json.dumps(scene),encoding='utf-8')
print('MANUAL_WORKER_JSON '+json.dumps({'event':'progress','message':'正在准备三维预览：已处理 1/1 个零件。','current':1,'total':1}),flush=True)
print('MANUAL_WORKER_JSON '+json.dumps({'event':'succeeded','result':{'source_sha256':'source-a','part_count':1,'triangle_count':1}}),flush=True)
""", encoding="utf-8")
        class CachedEngine(Engine):
            def scene_cached(self, project):
                return read_json(project / "scene" / "scene.json")
        self.server.app.engine = CachedEngine()
        with patch("backend.app_server.ROOT", worker_root):
            status, queued = self.request("GET", self.route("scene"))
            self.assertEqual(status, 202)
            self.assertEqual(queued["job_type"], "scene")
            reads, observed_progress = 0, False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                started = time.monotonic()
                status, job = self.request("GET", "/api/jobs/" + queued["job_id"])
                self.assertEqual(status, 200)
                self.assertLess(time.monotonic() - started, 0.3)
                reads += 1
                observed_progress |= "0/1" in job["message"]
                if job["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.05)
            else:
                self.fail("Scene child did not finish")
        self.assertGreater(reads, 5)
        self.assertTrue(observed_progress)
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["result"], {"source_sha256": "source-a", "part_count": 1, "triangle_count": 1})
        self.assertNotIn("parts", job["result"])
        status, scene = self.request("GET", self.route("scene"))
        self.assertEqual(status, 200)
        self.assertEqual(len(scene["parts"]), 1)

    def test_scene_worker_emits_exact_part_counts_and_metadata_without_mesh_ipc(self):
        from backend.cad_worker import EVENT_PREFIX, run_scene
        def fake_export(project, progress):
            progress({"phase": "tessellating", "current": 0, "total": 2, "triangles": 0})
            progress({"phase": "complete", "current": 2, "total": 2, "triangles": 2})
            return {"source_sha256": "source-a", "parts": [{"indices": [0, 1, 2]}, {"indices": [0, 1, 2]}]}
        captured = io.StringIO()
        with patch("scene_export.export_scene", side_effect=fake_export), patch("sys.stdout", captured):
            result = run_scene(self.project)
        events = [json.loads(line.removeprefix(EVENT_PREFIX)) for line in captured.getvalue().splitlines()]
        self.assertEqual([(event["current"], event["total"]) for event in events], [(0, 2), (2, 2)])
        self.assertIn("0/2", events[0]["message"])
        self.assertIn("2/2", events[1]["message"])
        self.assertEqual(result, {"source_sha256": "source-a", "part_count": 2, "triangle_count": 2})
        self.assertNotIn("parts", result)

    def test_scene_rejects_missing_or_mesh_worker_result(self):
        for bad in (None, {"source_sha256": "source-a", "part_count": True, "triangle_count": 1},
                    {"source_sha256": "source-a", "part_count": 1, "triangle_count": 1, "parts": []}):
            with patch.object(Engine, "_run_worker", return_value=bad):
                with self.assertRaisesRegex(RuntimeError, "完整缓存信息"):
                    Engine().scene(self.project, lambda *args: None)


if __name__ == "__main__":
    unittest.main()
