"""Serial background work with nonblocking state reads and per-project exclusion."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import queue
import threading
import traceback
import uuid


class ProjectBusy(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, sanitize=lambda value: value):
        self._sanitize = sanitize
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._jobs = {}
        self._active = {}
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="manual-background-jobs", daemon=True)
        self._worker.start()

    @contextmanager
    def edit(self, project_id, operation="edit"):
        reservation = "reservation-" + uuid.uuid4().hex
        with self._lock:
            if project_id in self._active:
                raise ProjectBusy("项目正在处理任务，请等待当前任务结束。")
            self._active[project_id] = {"id": reservation, "status": "uploading" if "upload" in operation else "editing",
                                        "job_type": operation, "project_id": project_id}
        try:
            yield reservation
        finally:
            with self._lock:
                if self._active.get(project_id, {}).get("id") == reservation:
                    self._active.pop(project_id, None)

    def submit(self, project_id, job_type, operation, *, reservation=None):
        with self._lock:
            if self._closed:
                raise ValueError("服务正在关闭。")
            active = self._active.get(project_id)
            if active is not None and active["id"] != reservation:
                raise ProjectBusy("项目正在处理任务，请等待当前任务结束。")
            if reservation is not None and (active is None or active["id"] != reservation):
                raise ProjectBusy("项目编辑锁已失效，请重试。")
            identity = uuid.uuid4().hex
            job = {"id": identity, "job_id": identity, "project_id": project_id, "job_type": job_type,
                   "status": "queued", "progress": None, "message": "任务已排队。", "error": None,
                   "result": None, "created_at": _now(), "started_at": None, "finished_at": None}
            self._jobs[identity] = job
            self._active[project_id] = job
            self._queue.put((identity, operation))
            return identity

    def get(self, identity):
        with self._lock:
            if identity not in self._jobs:
                raise KeyError("任务不存在。")
            return deepcopy(self._jobs[identity])

    def active(self, project_id):
        with self._lock:
            job = self._active.get(project_id)
            return [deepcopy(job)] if job else []

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            identity, operation = item
            with self._lock:
                job = self._jobs[identity]
                job.update(status="running", started_at=_now(), message="正在处理；计算进度暂不可估算。")

            def progress(message, value=None):
                with self._lock:
                    job["message"] = self._sanitize(str(message))
                    job["progress"] = value

            try:
                result = operation(progress)
                with self._lock:
                    job.update(status="succeeded", progress=100, message="任务已完成。",
                               result=self._sanitize(result))
            except Exception as error:
                traceback.print_exc()
                with self._lock:
                    job.update(status="failed", message="处理失败；既有结果仍保留。",
                               error=self._sanitize(str(error)), error_type=type(error).__name__)
            finally:
                with self._lock:
                    job["finished_at"] = _now()
                    if self._active.get(job["project_id"], {}).get("id") == identity:
                        self._active.pop(job["project_id"], None)
                self._queue.task_done()

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._worker.join(timeout=1)
