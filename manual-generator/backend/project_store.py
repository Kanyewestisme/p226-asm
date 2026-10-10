"""Persistent multi-project registry; local paths stay inside the backend."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
import uuid


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else deepcopy(default)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def public_value(value):
    """Expose logical filenames, never arbitrary absolute filesystem paths."""
    if isinstance(value, Path):
        return value.name
    if isinstance(value, dict):
        return {key: public_value(child) for key, child in value.items()
                if key not in {"workspace", "previous_diagrams", "previous_pdf", "upload_path"}}
    if isinstance(value, list):
        return [public_value(child) for child in value]
    if isinstance(value, tuple):
        return [public_value(child) for child in value]
    if isinstance(value, str):
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
            return value.replace("\\", "/").rsplit("/", 1)[-1]
        if value.startswith("/") and not value.startswith(("/api/", "/projects/", "/assets/")):
            return value.rsplit("/", 1)[-1]
        return re.sub(r"[A-Za-z]:[\\/][^\r\n\"']+", "[local file]", value)
    return value


class ProjectStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = self.root / "projects.json"
        self.lock = threading.RLock()
        self.data = read_json(self.registry, {"schema_version": 1, "active_project_id": None, "projects": []})

    def _save(self):
        write_json(self.registry, self.data)

    def get(self, identity):
        with self.lock:
            if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
                raise KeyError("项目不存在。")
            for project in self.data["projects"]:
                if project["id"] == identity:
                    return deepcopy(project)
            raise KeyError("项目不存在。")

    def workspace(self, identity):
        return Path(self.get(identity)["workspace"]).resolve()

    def update(self, identity, **values):
        with self.lock:
            for project in self.data["projects"]:
                if project["id"] == identity:
                    project.update(deepcopy(values))
                    self._save()
                    return deepcopy(project)
            raise KeyError("项目不存在。")

    def list(self):
        with self.lock:
            result = []
            for record in self.data["projects"]:
                plan = read_json(Path(record["workspace"]) / "steps.json", {})
                result.append({"id": record["id"], "title": record["title"],
                               "status": plan.get("status", "empty")})
            return result

    def create(self, title):
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise ValueError("项目名称须为 1 至 120 个字符。")
        identity = uuid.uuid4().hex
        record = {"id": identity, "title": title.strip(), "workspace": str(self.new_workspace(identity)),
                  "external": False, "created_at": datetime.now(timezone.utc).isoformat(), "revisions": []}
        with self.lock:
            self.data["projects"].append(record)
            self.data["active_project_id"] = identity
            self._save()
        return {"id": identity, "title": record["title"], "status": "empty"}

    def register(self, path):
        path = Path(path).resolve()
        if not path.is_dir() or not (path / "steps.json").is_file():
            raise ValueError("注册目录必须包含已有 steps.json。")
        with self.lock:
            for record in self.data["projects"]:
                if Path(record["workspace"]).resolve() == path:
                    self.data["active_project_id"] = record["id"]
                    self._save()
                    return record["id"]
            plan = read_json(path / "steps.json")
            identity = uuid.uuid4().hex
            self.data["projects"].append({"id": identity, "title": plan.get("product", {}).get("title", path.name),
                                          "workspace": str(path), "external": True,
                                          "created_at": datetime.now(timezone.utc).isoformat(), "revisions": []})
            self.data["active_project_id"] = identity
            self._save()
            return identity

    def new_workspace(self, identity):
        # Native Windows CAD/file APIs still encounter MAX_PATH on machines
        # without long-path support. Public UUIDs do not need verbose folders.
        parent = self.root / "p" / identity[:12]
        parent.mkdir(parents=True, exist_ok=True)
        while True:
            workspace = parent / uuid.uuid4().hex[:12]
            try:
                workspace.mkdir()
                return workspace
            except FileExistsError:
                continue

    def activate(self, identity, workspace, source):
        old = self.get(identity)
        revisions = old.get("revisions", [])
        if (Path(old["workspace"]) / "steps.json").is_file():
            revisions.append({"workspace": old["workspace"], "source_sha256": read_json(
                Path(old["workspace"]) / "steps.json", {}).get("source", {}).get("sha256")})
        self.update(identity, workspace=str(Path(workspace).resolve()), external=False,
                    source_upload=source, revisions=revisions, pending_source=None)

    def _pending_paths(self, identity, item):
        """Only backend-generated upload/revision paths may be resumed."""
        self.get(identity)
        if not isinstance(item, dict) or not isinstance(item.get("upload"), dict):
            raise ValueError("没有可重试的新版本；请重新上传 STP。")
        upload = item["upload"]
        try:
            source = Path(upload["path"]).resolve(strict=True)
            workspace = Path(item["workspace"]).resolve(strict=True)
        except (KeyError, OSError, TypeError):
            raise ValueError("已上传的新版本文件不完整，请重新上传 STP。")
        source_parent = self.root / "u" / identity[:12] / "s"
        workspace_parent = self.root / "p" / identity[:12]
        if (not source.is_relative_to(self.root) or source.parent != source_parent
                or not re.fullmatch(r"[a-f0-9]{16}\.(stp|step)", source.name)
                or not source.is_file()
                or not workspace.is_relative_to(self.root) or workspace.parent != workspace_parent
                or not re.fullmatch(r"[a-f0-9]{12}", workspace.name) or not workspace.is_dir()
                or workspace == self.workspace(identity)):
            raise ValueError("重试源文件或工作区不属于此项目的受控上传目录。")
        if (not re.fullmatch(r"[a-f0-9]{64}", str(upload.get("sha256", "")))
                or upload.get("bytes") != source.stat().st_size):
            raise ValueError("已上传的新版本文件校验信息不一致，请重新上传。")
        return source, workspace

    def set_pending_source(self, identity, upload, workspace):
        """Persist before scheduling so a failed upload survives server restart.

        This is an internal recovery entry point as well: an older upload can
        be registered using its original generated upload path and workspace.
        It is never exposed as arbitrary-path input through the HTTP API.
        """
        item = {"upload": deepcopy(upload), "workspace": str(Path(workspace).resolve()),
                "status": "queued", "error": None,
                "created_at": datetime.now(timezone.utc).isoformat()}
        self._pending_paths(identity, item)
        self.update(identity, pending_source=item)
        return item

    def pending_source(self, identity, *, verify_digest=False):
        item = self.get(identity).get("pending_source")
        source, _ = self._pending_paths(identity, item)
        if verify_digest:
            with source.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != item["upload"]["sha256"]:
                raise ValueError("已上传的新版本 STP 内容发生变化，请重新上传。")
        return item

    def pending_source_status(self, identity, status, error=None):
        item = self.get(identity).get("pending_source")
        if item is not None:
            item.update(status=status, error=None if error is None else str(error))
            self.update(identity, pending_source=item)

    def prepare_pending_workspace(self, identity, item):
        """Reuse completed imports; archive incomplete attempts without loss."""
        _, workspace = self._pending_paths(identity, item)
        children = list(workspace.iterdir())
        complete = read_json(workspace / "inventory" / "import_complete.json", {})
        if complete and complete.get("source_sha256") != item["upload"]["sha256"]:
            raise ValueError("重试工作区的几何缓存属于其他 STP，请重新上传。")
        # A successful draft that was interrupted before activation is reused
        # by the worker after normal source-bound validation.
        if complete and (workspace / "steps.json").is_file():
            return workspace
        keep = {"inventory", "source.json"} if complete else set()
        discarded = [path for path in children if path.name not in keep]
        if discarded:
            for path in discarded:
                if path.is_symlink() or not path.resolve().is_relative_to(workspace):
                    raise ValueError("失败工作区包含非受控路径，不能自动重试。")
            archive = self.root / "r" / identity[:12] / uuid.uuid4().hex[:12]
            if not archive.resolve().is_relative_to(self.root):
                raise ValueError("恢复归档目录无效。")
            archive.mkdir(parents=True)
            for path in discarded:
                shutil.move(str(path), str(archive / path.name))
        return workspace

    def upload(self, identity, kind, filename, stream, length):
        self.get(identity)
        if not isinstance(filename, str) or not filename or len(filename) > 255 or any(c in filename for c in "/\\\x00"):
            raise ValueError("上传文件名无效。")
        suffix = Path(filename).suffix.lower()
        allowed = {".stp", ".step"} if kind == "source" else {".pdf"}
        if suffix not in allowed:
            raise ValueError("模型仅支持 STP/STEP；原模板仅支持 PDF。")
        directory = self.root / "u" / identity[:12] / kind[:1]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (uuid.uuid4().hex[:16] + suffix)
        digest, remaining = hashlib.sha256(), length
        try:
            with target.open("xb") as handle:
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("上传内容不完整。")
                    handle.write(block)
                    digest.update(block)
                    remaining -= len(block)
            return {"path": str(target), "filename": filename, "sha256": digest.hexdigest(), "bytes": length}
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def preserve_outputs(self, identity, include_diagrams=True):
        """Copy output bytes, never a model or a new rendering, for stale display."""
        project = self.workspace(identity)
        directory = self.root / "h" / identity[:12] / uuid.uuid4().hex[:12]
        updates = {}
        if include_diagrams and (project / "diagrams" / "manifest.json").is_file():
            directory.mkdir(parents=True, exist_ok=True)
            saved = directory / "diagrams"
            saved.mkdir()
            for path in (project / "diagrams").iterdir():
                if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".svg", ".png", ".json"}:
                    shutil.copy2(path, saved / path.name)
            updates["previous_diagrams"] = str(directory / "diagrams")
        if (project / "manual.pdf").is_file():
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / "manual.pdf", directory / "manual.pdf")
            updates["previous_pdf"] = str(directory / "manual.pdf")
        if updates:
            self.update(identity, **updates)

    def display_directory(self, identity):
        record = self.get(identity)
        current = Path(record["workspace"]) / "diagrams"
        if (current / "manifest.json").is_file():
            return current.resolve()
        previous = record.get("previous_diagrams")
        return Path(previous).resolve() if previous else current.resolve()
