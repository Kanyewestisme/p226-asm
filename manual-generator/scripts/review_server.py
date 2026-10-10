"""Local browser editing and packaging confirmation, without a separate approval service."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
from urllib.parse import unquote, urlsplit
import webbrowser

from manual import load_project, save_plan, render_project, confirm_project, current_manifest
from review_output import html_page, is_current_pdf


def serve(project: Path, port: int = 8765, open_browser: bool = True):
    project = project.resolve()
    load_project(project)
    token = secrets.token_urlsafe(32)
    lock = threading.Lock()
    validation_cache = {"key": None, "data": None}

    def footprint():
        # Reuse a verified snapshot for unchanged local files while loading many
        # images. Confirmation still performs fresh full source/cache hashing.
        paths = [project / name for name in ("steps.json", "source.json", "manual.pdf", "pdf_export.json", "instructions.json")]
        paths += [project / "inventory" / name for name in ("analysis.json", "assembly_parts.json", "import_complete.json")]
        paths += list((project / "inventory").glob("*.brep"))
        paths += list((project / "diagrams").glob("*"))
        from manual import read_json
        source_info = read_json(project / "source.json")
        paths.append(Path(source_info["path"]))
        plan_info = read_json(project / "steps.json")
        if plan_info.get("pdf_template", {}).get("path"):
            paths.append(Path(plan_info["pdf_template"]["path"]))
        result = []
        for path in sorted(paths, key=str):
            try:
                info = path.stat()
                result.append((str(path), info.st_size, info.st_mtime_ns))
            except FileNotFoundError:
                result.append((str(path), None, None))
        return tuple(result)

    def state():
        key = footprint()
        if key == validation_cache["key"]:
            return validation_cache["data"]
        plan, analysis = load_project(project)
        from assembly_proposals import propose_assembly_candidates
        assembly = propose_assembly_candidates(analysis, plan["groups"])
        try:
            manifest = current_manifest(project, plan)
        except ValueError:
            manifest = None
        data = {"plan": plan, "manifest": manifest, "pdf_ready": manifest is not None and is_current_pdf(project, plan), "assembly_suggestions": assembly["suggestions"], "part_catalog": [{key: row.get(key) for key in ("id", "name", "index", "solids", "faces")} for row in analysis["parts"]]}
        if key != footprint():
            raise ValueError("Project files changed during verification. Reload the preview.")
        validation_cache.update(key=key, data=data)
        return data

    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            route = unquote(urlsplit(self.path).path)
            if route in ("/", "/index.html"):
                try:
                    with lock:
                        data = state()
                    page = html_page(data["plan"], data["manifest"], editable=True, token=token, part_catalog=data["part_catalog"], pdf_ready=data["pdf_ready"], assembly_suggestions=data["assembly_suggestions"])
                    self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                except Exception as exc:
                    self.send_json(409, {"error": str(exc)})
                return
            allowed = route in ("/manual.pdf", "/instructions.json", "/steps.json") or route.startswith("/diagrams/")
            target = (project / route.lstrip("/")).resolve()
            if not allowed or not target.is_relative_to(project) or not target.is_file():
                self.send_json(404, {"error": "File unavailable. Render the current draft if necessary."})
                return
            try:
                with lock:
                    verified = state()
                    if route != "/steps.json" and verified["manifest"] is None:
                        raise ValueError("No current preview. Render the edited draft first.")
                    if route == "/manual.pdf" and not verified["pdf_ready"]:
                        raise ValueError("当前模板 PDF 尚未生成；旧版式不会作为当前结果提供。")
                    payload = target.read_bytes()
                # Windows registry MIME associations may report image/svg,
                # which browsers reject with nosniff. Serve our formats explicitly.
                formats = {".svg": "image/svg+xml", ".png": "image/png", ".pdf": "application/pdf", ".json": "application/json; charset=utf-8"}
                content_type = formats.get(target.suffix.lower(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send(200, payload, content_type)
            except Exception as exc:
                self.send_json(409, {"error": str(exc)})

        def do_POST(self):
            if self.headers.get("X-Review-Token") != token:
                self.send_json(403, {"error": "Reload this local review page."})
                return
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
                self.send_json(403, {"error": "Use the local review page."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16_000_000:
                    raise ValueError("Invalid request size.")
                body = json.loads(self.rfile.read(length))
                route = urlsplit(self.path).path
                with lock:
                    if route == "/api/save":
                        save_plan(project, body["plan"])
                    elif route == "/api/merge":
                        from group_edits import merge_groups
                        current, analysis = load_project(project)
                        if current.get("pdf_template"):
                            raise ValueError("原模板步骤槽位固定，合并总成后需重新校准模板对应关系。")
                        merged = merge_groups(current, analysis, body["group_ids"], body["label"])
                        save_plan(project, merged)
                    elif route == "/api/render":
                        render_project(project)
                    elif route == "/api/export":
                        from manual import prepare_exports, publish_exports
                        current, _ = load_project(project)
                        if not current.get("pdf_template"):
                            raise ValueError("请先绑定用户提供的原模板。")
                        publish_exports(project, prepare_exports(project, current, current_manifest(project, current)))
                    elif route == "/api/confirm":
                        confirm_project(project)
                    else:
                        self.send_json(404, {"error": "Unknown operation."})
                        return
                    validation_cache["key"] = None
                    self.send_json(200, state())
            except Exception as exc:
                self.send_json(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"REVIEW {url}\nPress Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    from manual import main
    main()
