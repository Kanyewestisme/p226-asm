"""Loopback-only multi-project REST application and static frontend host."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import subprocess
import sys
import uuid
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
try:
    from .jobs import JobManager, ProjectBusy
    from .project_store import ProjectStore, public_value, read_json, write_json
except ImportError:
    from jobs import JobManager, ProjectBusy
    from project_store import ProjectStore, public_value, read_json, write_json


class Engine:
    """Lazy calls into the existing source-bound CAD/manual workflow."""
    def _run_worker(self, arguments, progress, failure_message):
        # OpenCascade's STEP transfer can retain the Python GIL for minutes.
        # A thread alone therefore does not keep GET/job polling responsive.
        # The child owns native CAD calls; the HTTP worker only drains output.
        command = [sys.executable, "-X", "utf8", "-u", str(ROOT / "backend" / "cad_worker.py"), *arguments]
        error = None
        result = None
        recent = []
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", bufsize=1,
                              creationflags=flags) as process:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                recent = (recent + [line])[-8:]
                if line.startswith("MANUAL_WORKER_JSON "):
                    event = json.loads(line.removeprefix("MANUAL_WORKER_JSON "))
                    if event.get("event") == "progress":
                        progress(event["message"])
                    elif event.get("event") == "failed":
                        error = event.get("error")
                    elif event.get("event") == "succeeded":
                        result = event.get("result")
                elif "CONTACT_PROGRESS" in line:
                    progress("正在分析新版本零件的连接候选。" + line.split("CONTACT_PROGRESS", 1)[1])
                elif "RESUME_COMPLETED_IMPORT" in line:
                    progress("已复用完成的 STP 几何导入，正在生成装配建议。")
                elif line.startswith("RENDER_START "):
                    progress("正在计算步骤图：" + line.removeprefix("RENDER_START ") + "。复杂总成可能需要数分钟。")
                elif line.startswith("RENDER_READY "):
                    progress("步骤图已完成：" + line.removeprefix("RENDER_READY "))
            exit_code = process.wait()
        if exit_code:
            raise RuntimeError(error or failure_message + ("\n" + recent[-1] if recent else ""))
        return result

    def draft(self, source, project, title, progress):
        progress("正在导入 STP 并生成可编辑建议；尚未生成步骤图。")
        self._run_worker(["--operation", "draft", "--source", str(source),
                          "--project", str(project), "--title", title], progress, "STP 导入进程未完成。")
        plan = read_json(project / "steps.json")
        if not isinstance(plan, dict):
            raise RuntimeError("STP 导入进程没有产生完整草案。")
        return plan

    def save(self, project, plan, progress):
        from manual import save_plan
        progress("正在验证并保存配置。")
        return save_plan(project, plan, export_document=False)

    def preview(self, project, plan, step_id, progress):
        progress("正在生成所选步骤的预览；其他步骤保留原图。")
        candidate = project / (".candidate-" + uuid.uuid4().hex + ".json")
        try:
            write_json(candidate, plan)
            result = self._run_worker(["--operation", "preview", "--project", str(project),
                                       "--candidate", str(candidate), "--step-id", step_id],
                                      progress, "单步插图进程未完成。")
            if not isinstance(result, dict) or not isinstance(result.get("manifest"), dict):
                raise RuntimeError("单步插图进程没有返回完整结果。")
            return result
        finally:
            candidate.unlink(missing_ok=True)

    def render(self, project, progress):
        progress("正在生成当前项目的全部步骤图。")
        result = self._run_worker(["--operation", "render", "--project", str(project)],
                                  progress, "步骤插图进程未完成。")
        if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
            raise RuntimeError("步骤插图进程没有返回完整结果。")
        return result

    def workflow(self, project, body, progress):
        from manual import load_project, save_plan
        from generic_render import plan_digest
        from workflow_editor import apply_workflow_edit
        plan, analysis = load_project(project)
        if body.get("source_sha256") != plan["source"]["sha256"]:
            raise ValueError("STP 版本已变化，请刷新后再编排安装动作。")
        if body.get("plan_sha256") not in (None, plan_digest(plan)):
            raise ValueError("步骤配置已变化，请刷新后再编排安装动作。")
        progress("正在核对交付单元和安装动作；现有视角和旧图保留供对照。")
        candidate = apply_workflow_edit(plan, analysis, body.get("operation"))
        return save_plan(project, candidate, export_document=False)

    def languages(self, project):
        from manual_languages import read_document
        return read_document(project)

    def languages_save(self, project, body, progress):
        from manual_languages import save_translations
        progress("正在核对原文版本、规格和件数并保存译文；不生成图或 PDF。")
        return save_translations(project, body)

    def translate(self, project, body, progress):
        from manual_languages import draft_translations
        progress("正在起草译文并核对原文中的数字、规格和件数；不发送三维模型。")
        return draft_translations(project, body)

    def export(self, project, progress):
        from manual import load_project, current_manifest, prepare_exports, publish_exports
        plan, _ = load_project(project)
        if not plan.get("pdf_template"):
            raise ValueError("请先上传并绑定原模板。")
        progress("正在将当前步骤图放回原模板。")
        publish_exports(project, prepare_exports(project, plan, current_manifest(project, plan)))
        return {"pdf_ready": True}

    def export_language(self, project, body, progress):
        from manual import load_project
        from manual_languages import LANGUAGES
        from template_translation import export_language_pdf
        plan, _ = load_project(project)
        if body.get("source_sha256") != plan["source"]["sha256"]:
            raise ValueError("STP 版本已变化，请刷新后再导出多语言说明书。")
        locale, draft = body.get("locale"), body.get("draft", False)
        if locale not in LANGUAGES or type(draft) is not bool:
            raise ValueError("请选择目标语言；draft 必须为布尔值。")
        progress("正在把当前译文放回原模板；保留中文 PDF 与原插图。")
        return export_language_pdf(project, locale, require_reviewed=not draft)

    def confirm(self, project, reviewer, progress):
        from manual import confirm_project
        progress("正在验证所选版本、当前图与包装确认。")
        return confirm_project(project, reviewer, export_document=False)

    def bind(self, project, template, config, progress):
        from manual import bind_template_project
        progress("正在校验原模板的图槽、文字和步骤绑定；不生成图。")
        return bind_template_project(project, template, config)

    def merge(self, project, group_ids, label, progress):
        from manual import load_project, save_plan
        from group_edits import merge_groups
        current, analysis = load_project(project)
        if current.get("pdf_template"):
            raise ValueError("原模板的步骤数量和含义固定；合并总成前须重新校准模板。")
        progress("正在合并所选总成并更新草案。")
        return save_plan(project, merge_groups(current, analysis, group_ids, label), export_document=False)

    def scene_cached(self, project):
        from scene_export import load_cached_scene
        return load_cached_scene(project)

    def scene(self, project, progress):
        # Tessellation also calls long-running native CAD code. Return only
        # metadata through IPC; the large source-bound mesh stays on disk and
        # is loaded by scene_cached when the client requests the completed job.
        progress("正在准备三维预览；页面可继续查看任务状态。")
        result = self._run_worker(["--operation", "scene", "--project", str(project)],
                                  progress, "三维预览进程未完成。")
        if (not isinstance(result, dict) or not isinstance(result.get("source_sha256"), str)
                or any(type(result.get(key)) is not int or result[key] < 0
                       for key in ("part_count", "triangle_count")) or "parts" in result):
            raise RuntimeError("三维预览进程没有返回完整缓存信息。")
        return result

    def template_metadata(self, path):
        import fitz
        with fitz.open(path) as doc:
            if not doc.is_pdf or doc.needs_pass or len(doc) != 2:
                raise ValueError("原模板必须是可读取的两页 PDF（封面与安装页）。")
            return {"page_count": len(doc), "pages": [{"width": page.rect.width, "height": page.rect.height} for page in doc]}

    def template_page(self, path, page, output):
        import fitz
        with fitz.open(path) as doc:
            if len(doc) != 2 or doc.needs_pass or not 1 <= page <= len(doc):
                raise ValueError("原模板页码无效。")
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(output.stem + "." + uuid.uuid4().hex + ".tmp.png")
            try:
                doc[page-1].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).save(temporary)
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)


class Application:
    def __init__(self, data_dir, frontend_dir=None, engine=None):
        self.store = ProjectStore(data_dir)
        self.engine = engine or Engine()
        self.frontend = Path(frontend_dir or ROOT / "frontend").resolve()
        self.token = secrets.token_urlsafe(32)
        self.jobs = JobManager(sanitize=public_value)

    def _effective_template(self, identity):
        record = self.store.get(identity)
        pending = record.get("pending_template")
        if pending:
            return pending
        project = self.store.workspace(identity)
        config = read_json(project / "steps.json", {}).get("pdf_template")
        if isinstance(config, dict) and config.get("path"):
            path = Path(config["path"])
            path = path.resolve() if path.is_absolute() else (project / path).resolve()
            return {"path": str(path), "filename": path.name, "sha256": config.get("sha256"),
                    **self.engine.template_metadata(path)}
        return None

    def _template_public(self, identity, item, *, bound=False, pending=False):
        if item is None:
            return None
        return {key: item.get(key) for key in ("filename", "sha256", "page_count", "pages")} | {
            "bound": bound, "pending": pending, "preview_base_url": f"/projects/{identity}/template/page/"}

    def state(self, identity):
        record = self.store.get(identity)
        project = Path(record["workspace"])
        plan = read_json(project / "steps.json")
        directory = self.store.display_directory(identity)
        manifest = read_json(directory / "manifest.json")
        current, stale_ids = False, []
        if plan and manifest:
            from generic_render import plan_digest
            current = (manifest.get("source_sha256") == plan.get("source", {}).get("sha256")
                       and manifest.get("plan_sha256") == plan_digest(plan) and manifest.get("complete") is True)
            entries = {entry.get("step_id", entry.get("id")): entry for entry in manifest.get("entries", [])}
            try:
                from preview_service import step_recipe_sha256
            except ImportError:
                step_recipe_sha256 = None
            for step in plan.get("steps", []):
                entry = entries.get(step["id"])
                stale = entry is None or (not current and (step_recipe_sha256 is None
                        or entry.get("recipe_sha256") != step_recipe_sha256(plan, step["id"])))
                if entry and not stale:
                    for kind in ("svg", "png"):
                        filename = entry.get(kind + "_path", "")
                        target = (directory / filename).resolve()
                        if not target.is_relative_to(directory) or not target.is_file():
                            stale = True
                            break
                        if entry.get(kind + "_sha256") != hashlib.sha256(target.read_bytes()).hexdigest():
                            stale = True
                            break
                if stale:
                    stale_ids.append(step["id"])
                if entry:
                    entry["stale"] = stale
            current = current and not stale_ids
        elif plan:
            stale_ids = [step["id"] for step in plan.get("steps", [])]
        pdf_ready = False
        if plan and current:
            from review_output import is_current_pdf
            pdf_ready = is_current_pdf(project, plan)
        catalog = read_json(project / "inventory" / "assembly_parts.json", [])
        workflow = None
        analysis = read_json(project / "inventory" / "analysis.json")
        if plan and analysis:
            from workflow_editor import workflow_summary
            workflow = workflow_summary(plan, analysis)
        suggestions = (plan or {}).get("assembly_review", {}).get("suggestions", [])
        pending = record.get("pending_template")
        bound = None
        config = (plan or {}).get("pdf_template")
        if isinstance(config, dict) and config.get("path"):
            try:
                path = Path(config["path"])
                path = path.resolve() if path.is_absolute() else (project / path).resolve()
                bound = {"filename": path.name, "sha256": config["sha256"], **self.engine.template_metadata(path)}
            except (OSError, ValueError, RuntimeError, KeyError):
                bound = None
        return public_value({
            "project": {"id": identity, "title": record["title"], "status": (plan or {}).get("status", "empty")},
            "plan": plan, "manifest": manifest, "stale": not current,
            "stale_step_ids": stale_ids, "pdf_ready": pdf_ready,
            "pdf_previous_available": bool(record.get("previous_pdf")),
            "part_catalog": [{key: row.get(key) for key in ("id", "name", "index", "solids", "faces")} for row in catalog],
            "assembly_suggestions": suggestions,
            "workflow": workflow,
            "languages_url": f"/api/projects/{identity}/languages",
            "template_info": self._template_public(identity, pending or bound, bound=not bool(pending) and bool(bound), pending=bool(pending)),
            "pending_template_info": self._template_public(identity, pending, pending=True),
            "bound_template_info": self._template_public(identity, bound, bound=True),
            "active_jobs": self.jobs.active(identity), "diagram_base_url": f"/projects/{identity}/diagrams/",
            "source_filename": record.get("source_upload", {}).get("filename"),
            "source_model_name": record.get("source_upload", {}).get("model_name"),
            "revision_count": len(record.get("revisions", [])),
            "revision_report": read_json(project / "revision_report.json"),
            "pending_source_info": self.pending_source_info(identity),
        })

    def pending_source_info(self, identity):
        item = self.store.get(identity).get("pending_source")
        if not item:
            return None
        upload = item.get("upload", {})
        active = self.jobs.active(identity)
        status = item.get("status", "failed")
        if status in {"queued", "running"} and not active:
            status = "interrupted"
        return {"filename": upload.get("filename"), "sha256": upload.get("sha256"),
                "bytes": upload.get("bytes"), "status": status, "error": item.get("error"),
                "retry_available": not bool(active)}

    def preserve_template_for_revision(self, identity):
        """Keep the user's template file, never its old step/source bindings."""
        if self.store.get(identity).get("pending_template"):
            return None
        try:
            item = self._effective_template(identity)
            if item is None:
                return None
            with Path(item["path"]).open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != item.get("sha256"):
                raise ValueError("原模板文件内容与已提供版本不一致。")
            # _effective_template contains file metadata only. The previous
            # source SHA, figure_slots and step_bindings remain in old history.
            self.store.update(identity, pending_template=item)
            return None
        except Exception as error:
            # A broken or missing template must not discard a valid new STP.
            return "新版本已起草；原模板无法保留，请重新提供模板后校准。" + str(error)

    def submit_source(self, identity, reservation=None):
        # No uploaded paths are accepted in the request. This light validation
        # precedes scheduling; the potentially large SHA check stays off HTTP.
        self.store.pending_source(identity)
        old_workspace = self.store.workspace(identity)
        title = self.store.get(identity)["title"]

        def draft(progress):
            try:
                self.store.pending_source_status(identity, "running")
                item = self.store.pending_source(identity, verify_digest=True)
                upload = item["upload"]
                workspace = self.store.prepare_pending_workspace(identity, item)
                plan = self.engine.draft(Path(upload["path"]), workspace, title, progress)
                if plan.get("source", {}).get("sha256") != upload["sha256"]:
                    raise ValueError("新版本草案与上传的 STP 校验值不一致。")
                revision_warning = None
                if (old_workspace / "steps.json").is_file():
                    progress("正在比较已保存版本；旧确认和图不会自动用于新源。")
                    try:
                        from revision_analysis import compare_projects
                        comparison = compare_projects(old_workspace, workspace)
                        write_json(workspace / "revision_report.json", comparison)
                    except (OSError, ValueError, KeyError, RuntimeError) as error:
                        revision_warning = "旧版缓存无法完成核验；新版本保持未确认。" + str(error)
                template_warning = self.preserve_template_for_revision(identity)
                if template_warning:
                    revision_warning = (revision_warning + " " if revision_warning else "") + template_warning
                self.store.preserve_outputs(identity)
                self.store.activate(identity, workspace, upload)
                return {"plan": plan, "revision_warning": revision_warning, "template_warning": template_warning}
            except Exception as error:
                self.store.pending_source_status(identity, "failed", error)
                raise

        return self.jobs.submit(identity, "source", draft, reservation=reservation)

    def restore_plan_paths(self, identity, supplied):
        if not isinstance(supplied, dict):
            raise ValueError("请求须包含 plan 对象。")
        plan = deepcopy(supplied)
        old = read_json(self.store.workspace(identity) / "steps.json", {})
        def restore(value, original):
            if isinstance(original, str) and public_value(original) != original and value == public_value(original):
                return original
            if isinstance(value, dict) and isinstance(original, dict):
                return {key: restore(child, original.get(key)) for key, child in value.items()}
            if isinstance(value, list) and isinstance(original, list):
                by_id = {item.get("id"): item for item in original if isinstance(item, dict) and item.get("id")}
                return [restore(item, by_id.get(item.get("id")) if isinstance(item, dict) and item.get("id")
                                else original[index] if index < len(original) else None)
                        for index, item in enumerate(value)]
            return value
        old_config = old.get("pdf_template")
        if isinstance(old_config, dict) and isinstance(plan.get("pdf_template"), dict):
            # The logical filename shown to the browser must not turn into an
            # arbitrary local path or loosen the existing immutable binding.
            expected = public_value(old_config.get("path"))
            if plan["pdf_template"].get("path") not in (expected, old_config.get("path")):
                raise ValueError("原模板路径不可在编辑器中修改。")
            plan["pdf_template"]["path"] = old_config.get("path")
        return restore(plan, old)

    def submit(self, identity, action, body, reservation=None):
        project = self.store.workspace(identity)
        plan = self.restore_plan_paths(identity, body.get("plan")) if action in ("plan", "preview") or body.get("plan") is not None else None
        if action == "preview" and not isinstance(body.get("step_id"), str):
            raise ValueError("请选择一个步骤。")
        if action == "workflow":
            current = read_json(project / "steps.json", {})
            if body.get("source_sha256") != current.get("source", {}).get("sha256") or not body.get("source_sha256"):
                raise ValueError("STP 版本已变化，请刷新后再编排安装动作。")
            if not isinstance(body.get("operation"), dict):
                raise ValueError("请求须包含 operation 编排动作。")

        def operation(progress):
            if action not in {"preview", "languages", "translate", "export-language"}:
                self.store.preserve_outputs(identity, include_diagrams=action != "export")
            if action in {"plan", "workflow"}:
                result = (self.engine.save(project, plan, progress) if action == "plan"
                          else self.engine.workflow(project, body, progress))
                # save_plan archives stale assets. Keep their bytes available
                # for display with their old source/recipe checks, never current.
                display = self.store.display_directory(identity)
                target = project / "diagrams"
                if not (target / "manifest.json").is_file() and (display / "manifest.json").is_file():
                    import shutil
                    if not target.exists():
                        shutil.copytree(display, target)
                return {"plan": result}
            if action == "languages":
                return self.engine.languages_save(project, body, progress)
            if action == "translate":
                return self.engine.translate(project, body, progress)
            if action == "export-language":
                return self.engine.export_language(project, body, progress)
            if action == "preview":
                return self.engine.preview(project, plan, body["step_id"], progress)
            if action == "render":
                if plan is not None:
                    self.engine.save(project, plan, progress)
                return {"manifest": self.engine.render(project, progress)}
            if action == "export":
                return self.engine.export(project, progress)
            if action == "confirm":
                if plan is not None:
                    self.engine.save(project, plan, progress)
                return self.engine.confirm(project, body.get("reviewer", "包装确认"), progress)
            if action == "merge":
                return {"plan": self.engine.merge(project, body.get("group_ids"), body.get("label"), progress)}
            if action == "bind-template":
                item = self._effective_template(identity)
                if item is None:
                    raise ValueError("请先上传两页原模板。")
                config = deepcopy(body.get("config"))
                if not isinstance(config, dict):
                    raise ValueError("请求须包含 config 图槽配置。")
                current = read_json(project / "steps.json")
                if not current:
                    raise ValueError("请先上传 STP 并完成起草。")
                source_hash = current["source"]["sha256"]
                if config.get("source_sha256") not in (None, source_hash):
                    raise ValueError("图槽配置属于其他模型版本。")
                if config.get("sha256") not in (None, item["sha256"]):
                    raise ValueError("图槽配置与所选原模板不一致。")
                config.update(path=item["path"], sha256=item["sha256"], source_sha256=source_hash, page_count=2)
                result = self.engine.bind(project, Path(item["path"]), config, progress)
                self.store.update(identity, pending_template=None)
                return {"plan": result}
            raise ValueError("未知操作。")
        return self.jobs.submit(identity, action, operation, reservation=reservation)


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def server_close(self):
        self.app.jobs.close()
        super().server_close()


def create_server(data_dir, *, port=8765, frontend_dir=None, register_projects=(), engine=None):
    app = Application(data_dir, frontend_dir, engine)
    for project in register_projects:
        app.store.register(project)

    class Handler(BaseHTTPRequestHandler):
        def discard_small_rejected_body(self):
            """Avoid an unread-body TCP reset without waiting on large uploads.

            Windows can abort the client's response read when a socket closes
            with a small POST payload still in its receive queue. Only discard
            a fixed, bounded body; never decode, log or persist rejected input.
            """
            if self.command != "POST" or getattr(self, "_body_consumed", False):
                return
            if self.headers.get("Transfer-Encoding"):
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if not 0 < length <= 64 * 1024:
                return
            previous_timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(0.25)
                self.rfile.read(length)
                self._body_consumed = True
            except (OSError, TimeoutError):
                pass
            finally:
                self.connection.settimeout(previous_timeout)

        def send(self, status, payload, content_type="application/json; charset=utf-8"):
            if status >= 400:
                self.discard_small_rejected_body()
            if not isinstance(payload, bytes):
                payload = json.dumps(public_value(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; font-src 'self' data:; frame-src 'self' blob:; object-src 'none'; worker-src 'self' blob:")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True

        def allowed(self, token=False):
            origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "")
            if host not in {value.removeprefix("http://") for value in origins}:
                self.send(403, {"error": "请使用本机应用地址。"})
                return False
            if (self.headers.get("Origin") not in (None, *origins)
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.send(403, {"error": "请求须来自本机应用页面。"})
                return False
            supplied = self.headers.get("X-App-Token", self.headers.get("X-Review-Token", ""))
            if token and not secrets.compare_digest(supplied.encode("utf-8"), app.token.encode("utf-8")):
                self.send(403, {"error": "请重新加载应用页面。"})
                return False
            return True

        def body_length(self, maximum):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("请求长度无效。")
            if not 0 < length <= maximum:
                raise ValueError("请求文件为空或超过大小限制。")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("请使用固定长度上传。")
            return length

        def json_body(self):
            if self.headers.get("Content-Length", "0") == "0":
                self._body_consumed = True
                return {}
            length = self.body_length(16 * 1024 * 1024)
            payload = self.rfile.read(length)
            self._body_consumed = True
            body = json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("非法 JSON 数值。")))
            if not isinstance(body, dict):
                raise ValueError("请求须为 JSON 对象。")
            return body

        def error(self, error):
            status = 409 if isinstance(error, ProjectBusy) else 404 if isinstance(error, (KeyError, FileNotFoundError)) else 400
            self.send(status, {"error": str(error), "error_type": type(error).__name__})

        def do_GET(self):
            route = unquote(urlsplit(self.path).path)
            if not self.allowed(token=route.startswith("/api/") and route != "/api/bootstrap"):
                return
            try:
                if route == "/api/bootstrap":
                    self.send(200, {"token": app.token, "projects": app.store.list(),
                                    "active_project_id": app.store.data.get("active_project_id")})
                    return
                if route == "/api/projects":
                    self.send(200, {"projects": app.store.list()})
                    return
                job_match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})", route)
                if job_match:
                    self.send(200, app.jobs.get(job_match[1]))
                    return
                project_match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(/scene|/languages)?", route)
                if project_match:
                    identity = project_match[1]
                    app.store.get(identity)
                    if project_match[2] == "/languages":
                        project = app.store.workspace(identity)
                        if not (project / "steps.json").is_file():
                            raise ValueError("请先上传 STP 并完成起草。")
                        self.send(200, app.engine.languages(project))
                    elif project_match[2] == "/scene":
                        project = app.store.workspace(identity)
                        if not (project / "steps.json").is_file():
                            raise ValueError("请先上传 STP 并完成起草。")
                        scene = app.engine.scene_cached(project)
                        if scene is not None:
                            self.send(200, scene)
                        else:
                            active = app.jobs.active(identity)
                            if active:
                                if not re.fullmatch(r"[a-f0-9]{32}", active[0]["id"]):
                                    raise ProjectBusy("上传尚未结束，请稍后再加载 3D。")
                                self.send(202, {"job_id": active[0]["id"], "job_type": active[0]["job_type"]})
                            else:
                                job_id = app.jobs.submit(identity, "scene", lambda progress: app.engine.scene(project, progress))
                                self.send(202, {"job_id": job_id, "job_type": "scene"})
                    else:
                        self.send(200, app.state(identity))
                    return
                resource = re.fullmatch(r"/projects/([a-f0-9]{32})/(.+)", route)
                if resource:
                    self.file_resource(resource[1], resource[2])
                    return
                if route in ("/", "/index.html"):
                    target = app.frontend / "index.html"
                elif route.startswith("/assets/"):
                    target = (app.frontend / route[len("/assets/"):]).resolve()
                else:
                    raise FileNotFoundError("页面不存在。")
                if not target.resolve().is_relative_to(app.frontend) or not target.is_file():
                    raise FileNotFoundError("页面资源不存在。")
                self.file_bytes(target)
            except Exception as error:
                self.error(error)

        def file_bytes(self, target):
            formats = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                       ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                       ".pdf": "application/pdf", ".json": "application/json; charset=utf-8"}
            self.send(200, target.read_bytes(), formats.get(target.suffix.lower(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"))

        def file_resource(self, identity, relative):
            app.store.get(identity)
            project = app.store.workspace(identity)
            page_match = re.fullmatch(r"template/page/([1-2])\.png", relative)
            if page_match:
                item = app._effective_template(identity)
                if item is None:
                    raise FileNotFoundError("请先上传原模板。")
                source = Path(item["path"])
                if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("原模板已变化，请重新上传并校准。")
                page = int(page_match[1])
                cached = app.store.root / "t" / identity[:12] / item["sha256"][:32] / f"{page}.png"
                if not cached.is_file():
                    app.engine.template_page(source, page, cached)
                self.file_bytes(cached)
                return
            if relative == "template.pdf":
                item = app._effective_template(identity)
                if item is None:
                    raise FileNotFoundError("原模板尚未上传。")
                target = Path(item["path"])
                if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("原模板已变化，请重新上传。")
                self.file_bytes(target)
                return
            if relative.startswith("diagrams/"):
                name = relative[len("diagrams/"):]
                directory = app.store.display_directory(identity)
                target = (directory / name).resolve()
                if not name or Path(name).name != name or "\\" in name or not target.is_relative_to(directory):
                    raise FileNotFoundError("图资源不存在。")
                if target.suffix.lower() not in {".svg", ".png", ".json"} or not target.is_file():
                    raise FileNotFoundError("图资源不存在。")
                if target.suffix == ".json":
                    self.send(200, read_json(target))
                else:
                    self.file_bytes(target)
                return
            language_pdf = re.fullmatch(r"manual-(zh|en|de|fr|es)\.pdf", relative)
            if language_pdf:
                from template_translation import is_current_language_pdf
                if not is_current_language_pdf(project, language_pdf[1]):
                    raise ValueError("该语言 PDF 缺失或已过期，请核对当前译文后重新导出。")
                target = project / relative
                if not target.is_file():
                    raise FileNotFoundError("该语言 PDF 尚未生成。")
                self.file_bytes(target)
                return
            if relative not in {"manual.pdf", "steps.json", "instructions.json", "revision_report.json", "pdf_export.json", "languages.json"}:
                raise FileNotFoundError("文件不存在。")
            target = (project / relative).resolve()
            if not target.is_relative_to(project) or not target.is_file():
                raise FileNotFoundError("文件尚未生成。")
            if relative == "manual.pdf":
                if not app.state(identity)["pdf_ready"]:
                    raise ValueError("当前原模板 PDF 尚未导出；旧版不作为当前结果提供。")
                self.file_bytes(target)
            elif relative == "languages.json":
                self.send(200, app.engine.languages(project))
            else:
                self.send(200, read_json(target))

        def do_POST(self):
            if not self.allowed(token=True):
                return
            try:
                route = unquote(urlsplit(self.path).path)
                if route == "/api/projects":
                    self.send(200, {"project": app.store.create(self.json_body().get("title"))})
                    return
                match = re.fullmatch(r"/api/projects/([a-f0-9]{32})/(source|retry-source|template|plan|preview|render|export|confirm|bind-template|merge|workflow|languages|translate|export-language)", route)
                if not match:
                    raise FileNotFoundError("操作不存在。")
                identity, action = match.groups()
                app.store.get(identity)
                if action in {"source", "template"}:
                    if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/octet-stream":
                        raise ValueError("请以二进制文件上传。")
                    filename = unquote(self.headers.get("X-File-Name", ""), errors="strict")
                    length = self.body_length(512 * 1024 * 1024 if action == "source" else 64 * 1024 * 1024)
                    with app.jobs.edit(identity, "upload_" + action) as reservation:
                        upload = app.store.upload(identity, action, filename, self.rfile, length)
                        self._body_consumed = True
                        if action == "template":
                            try:
                                info = upload | app.engine.template_metadata(Path(upload["path"]))
                            except Exception:
                                Path(upload["path"]).unlink(missing_ok=True)
                                raise
                            app.store.update(identity, pending_template=info)
                            self.send(200, {"template_info": app._template_public(identity, info, pending=True)})
                        else:
                            workspace = app.store.new_workspace(identity)
                            app.store.set_pending_source(identity, upload, workspace)
                            job_id = app.submit_source(identity, reservation=reservation)
                            self.send(202, {"job_id": job_id})
                    return
                body = self.json_body()
                with app.jobs.edit(identity, action) as reservation:
                    if action == "retry-source":
                        if body:
                            raise ValueError("重试只使用已上传的源文件，请勿提交本机文件路径。")
                        job_id = app.submit_source(identity, reservation=reservation)
                    else:
                        job_id = app.submit(identity, action, body, reservation=reservation)
                self.send(202, {"job_id": job_id})
            except Exception as error:
                self.error(error)

    server = AppHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "app-data")
    parser.add_argument("--register-project", action="append", type=Path, default=[])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.data_dir, port=args.port, register_projects=args.register_project)
    print(f"APPLICATION http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
