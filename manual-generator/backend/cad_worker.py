"""Isolate STEP import/native CAD work from the application's HTTP process."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
EVENT_PREFIX = "MANUAL_WORKER_JSON "


def emit(value):
    print(EVENT_PREFIX + json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def run_draft(source, project, title):
    from manual import draft_project, load_project
    # Recover a fully validated draft if a restart interrupted activation.
    if (project / "steps.json").is_file():
        plan, _ = load_project(project)
        import hashlib
        with source.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if plan.get("source", {}).get("sha256") != digest:
            raise ValueError("重试工作区的草案属于其他 STP。")
        emit({"event": "progress", "message": "已恢复完成的导入和草案，正在核验新版本。"})
        return plan
    return draft_project(source, project, title, 1.0, 40, 5.0)


def run_scene(project):
    from scene_export import export_scene
    def progress(value):
        current, total = value.get("current"), value.get("total")
        message = (f"正在准备三维预览：已处理 {current}/{total} 个零件。"
                   if current is not None and total is not None else "正在准备三维预览。")
        emit({"event": "progress", "message": message,
              **{key: value[key] for key in ("phase", "current", "total", "triangles") if key in value}})
    data = export_scene(project, progress=progress)
    return {"source_sha256": data["source_sha256"], "part_count": len(data["parts"]),
            "triangle_count": data.get("triangle_count", sum(len(part.get("indices", [])) // 3 for part in data["parts"]))}


def run_preview(project, candidate, step_id):
    from preview_service import preview_step
    project = project.resolve()
    if candidate is None or not isinstance(step_id, str) or not step_id:
        raise ValueError("单步插图须指定候选配置和步骤。")
    candidate = candidate.resolve()
    if candidate.parent != project or not re.fullmatch(r"\.candidate-[a-f0-9]{32}\.json", candidate.name):
        raise ValueError("单步插图配置不在受控工作区内。")
    plan = json.loads(candidate.read_text(encoding="utf-8"))
    emit({"event": "progress", "message": "正在计算所选步骤的隐藏线和投影；其他步骤不重算。"})
    return preview_step(project, plan, step_id)


def run_render(project):
    from manual import render_project
    emit({"event": "progress", "message": "正在计算当前步骤的隐藏线；页面仍可查看状态。"})
    return render_project(project, export_document=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("draft", "scene", "preview", "render"), default="draft")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--title")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--step-id")
    args = parser.parse_args()
    try:
        if args.operation == "draft":
            if args.source is None or args.title is None:
                raise ValueError("STP 起草必须指定源文件和项目名称。")
            emit({"event": "progress", "message": "正在导入 STP 并生成可编辑建议；页面可继续查看任务状态。"})
            run_draft(args.source, args.project, args.title)
            result = None
        elif args.operation == "scene":
            result = run_scene(args.project)
        elif args.operation == "preview":
            result = run_preview(args.project, args.candidate, args.step_id)
        else:
            result = run_render(args.project)
        emit({"event": "succeeded", "result": result})
        return 0
    except Exception as error:
        traceback.print_exc()
        emit({"event": "failed", "error": str(error), "error_type": type(error).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
