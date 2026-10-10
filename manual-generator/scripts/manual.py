"""Generic, source-bound installation draft workflow; the P226 entries remain available."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import uuid


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_project(project: Path, check_source: bool = True):
    from draft_planner import validate_plan
    from model_analysis import _load_inventory
    project = project.resolve()
    analysis = read_json(project / "inventory" / "analysis.json")
    plan = read_json(project / "steps.json")
    validate_plan(plan, analysis)
    if plan.get("pdf_template") is not None:
        from template_pdf import validate_template_binding, validate_template_source
        validate_template_binding(plan)
        validate_template_source(project, plan)
    imported_parts, completion = _load_inventory(project / "inventory")
    if imported_parts != analysis["parts"]:
        raise ValueError("Analysis no longer matches the imported inventory. Reanalyze the current source.")
    if completion["source_sha256"] != plan["source"]["sha256"]:
        raise ValueError("Inventory and draft belong to different STEP files.")
    source = read_json(project / "source.json")
    if source["sha256"] != plan["source"]["sha256"]:
        raise ValueError("Project source and draft differ. Create a new revision project.")
    # Work can continue from the source-bound BREP when the original file is moved.
    # When the source still exists, do not let an overwritten STEP silently reuse cache.
    path = Path(source["path"])
    if check_source and path.is_file():
        with path.open("rb") as handle:
            current = hashlib.file_digest(handle, "sha256").hexdigest()
        if current != source["sha256"]:
            raise ValueError("The input STEP has changed. Draft a new project and compare revisions.")
    return plan, analysis


def draft_project(step: Path, project: Path, title: str, tolerance: float, max_pairs: int, pair_timeout: float = 10.0):
    from model_analysis import import_model, analyze_inventory, _load_inventory
    from draft_planner import build_plan, validate_plan
    step, project = step.resolve(), project.resolve()
    resumed = project.exists() and any(project.iterdir())
    if resumed and (not (project / "inventory" / "import_complete.json").is_file()
                    or not {path.name for path in project.iterdir()} <= {"inventory", "source.json"}):
        raise ValueError("Project already contains work. Use a new directory to preserve previous drafts.")
    project.mkdir(parents=True, exist_ok=True)
    if resumed:
        _, completion = _load_inventory(project / "inventory")
        with step.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != completion["source_sha256"]:
                raise ValueError("The completed import belongs to another STEP. Choose a fresh project.")
        print("RESUME_COMPLETED_IMPORT", flush=True)
    else:
        completion = import_model(step, project / "inventory")
    write_json(project / "source.json", {"path": str(step), "sha256": completion["source_sha256"]})
    analysis = analyze_inventory(project / "inventory", tolerance_mm=tolerance, max_pairs=max_pairs, pair_timeout_seconds=pair_timeout)
    plan = build_plan(analysis, title=title)
    from assembly_proposals import propose_assembly_candidates
    plan["assembly_review"] = propose_assembly_candidates(analysis, plan["groups"])
    validate_plan(plan, analysis)
    write_json(project / "steps.json", plan)
    write_json(project / "proposed_steps.json", plan)
    print(f"DRAFT_READY {project / 'steps.json'}", flush=True)
    return plan


def archive_path(project: Path, path: Path):
    if path.exists():
        target = project / "history" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        target.mkdir(parents=True)
        shutil.move(str(path), str(target / path.name))


def prepare_exports(project: Path, plan: dict, manifest: dict) -> Path:
    from review_output import export_preview
    staging = project / (".export-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        export_preview(staging, plan, manifest, diagrams=project / "diagrams", inventory=project / "inventory")
        return staging
    except Exception:
        archive_path(project, staging)
        raise


def publish_exports(project: Path, staging: Path):
    for name in ("manual.pdf", "pdf_export.json", "instructions.json", "index.html"):
        archive_path(project, project / name)
        if (staging / name).exists():
            (staging / name).replace(project / name)
    staging.rmdir()


def save_plan(project: Path, plan: dict, *, export_document: bool = True):
    """Save validated edits; application review changes export PDF separately."""
    from draft_planner import validate_plan
    from generic_render import plan_digest
    old, analysis = load_project(project)
    if plan == old:
        return old
    candidate = deepcopy(plan)
    # User edits cannot switch the project to a different source.
    if candidate.get("source") != old["source"]:
        raise ValueError("Source identity is read-only. Create a new project for another STEP.")
    if old.get("pdf_template") is not None:
        from template_pdf import validate_template_binding
        if candidate.get("pdf_template") != old["pdf_template"]:
            raise ValueError("原模板绑定不能在编辑器中更换或解除；请使用 template 命令显式校准。")
        if candidate.get("manual_document") != old.get("manual_document"):
            raise ValueError("原模板文字和排版固定；本模式只修改步骤图。")
        validate_template_binding(candidate)
    elif candidate.get("pdf_template") is not None:
        raise ValueError("请使用 template 命令绑定并校验用户提供的原模板。")
    candidate["status"] = "draft"
    candidate.pop("confirmation", None)
    changed = plan_digest(candidate) != plan_digest(old)
    if changed:
        for step in candidate["steps"]:
            step["reviewed"] = False
    validate_plan(candidate, analysis)
    if candidate != old:
        exports = None
        if export_document and not changed and old.get("status") != candidate["status"]:
            exports = prepare_exports(project, candidate, current_manifest(project, old))
        archive_path(project, project / "steps.json")
        write_json(project / "steps.json", candidate)
        archive_path(project, project / "confirmation.json")
        if changed:
            # Previous outputs stay recoverable but cannot masquerade as the edited plan.
            for name in ("diagrams", "index.html", "manual.pdf", "pdf_export.json", "instructions.json"):
                archive_path(project, project / name)
        elif exports:
            publish_exports(project, exports)
    return candidate


def current_manifest(project: Path, plan: dict):
    from generic_render import plan_digest
    path = project / "diagrams" / "manifest.json"
    if not path.is_file():
        raise ValueError("No current diagrams. Render the draft first.")
    manifest = read_json(path)
    if manifest.get("source_sha256") != plan["source"]["sha256"] or manifest.get("plan_sha256") != plan_digest(plan):
        raise ValueError("The preview is stale. Render the current draft before confirming or exporting.")
    entries = {entry.get("step_id", entry.get("id")): entry for entry in manifest["entries"]}
    for step in plan["steps"]:
        if step["id"] not in entries:
            raise ValueError(f"Missing current diagram: {step['id']}")
        entry = entries[step["id"]]
        for asset in [entry] + ([entry["focus"]] if entry.get("focus") else []):
            for kind in ("svg", "png"):
                raw_path = Path(asset[f"{kind}_path"])
                target = (project / "diagrams" / raw_path).resolve()
                if not target.is_relative_to((project / "diagrams").resolve()) or not target.is_file():
                    raise ValueError(f"Invalid/missing {kind} asset: {step['id']}")
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != asset[f"{kind}_sha256"]:
                    raise ValueError(f"Changed diagram: {target.name}. Render again.")
    if plan.get("cover_scene") is not None and not plan.get("pdf_template"):
        from cover_render import cover_plan
        cover = manifest.get("cover", {})
        if (cover.get("status") != "ready" or cover.get("source_sha256") != plan["source"]["sha256"]
                or cover.get("cover_plan_sha256") != plan_digest(cover_plan(plan))):
            raise ValueError("Parts overview is missing or stale. Render the current draft.")
        for kind in ("svg", "png"):
            target = (project / "diagrams" / cover[f"{kind}_path"]).resolve()
            if (not target.is_relative_to((project / "diagrams").resolve()) or not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest() != cover[f"{kind}_sha256"]):
                raise ValueError("Parts overview asset changed. Render the current draft.")
    return manifest


def render_project(project: Path, *, export_document: bool = True):
    """Render current illustrations; document export is an explicit option.

    The CLI retains its combined preview/export behavior. The application can
    request illustrations only, archiving any previous document outputs after
    a successful render without creating PDF, receipt, HTML or instructions.
    """
    from generic_render import render_plan
    from review_output import export_preview
    plan, _ = load_project(project)
    staging = project / (".render-" + uuid.uuid4().hex)
    try:
        manifest = render_plan(plan, project / "inventory", staging / "diagrams")
        from cover_render import render_cover
        cover = None if plan.get("pdf_template") else render_cover(plan, project / "inventory", staging / "cover")
        if cover is not None:
            for kind in ("svg", "png"):
                name = cover[f"{kind}_path"]
                (staging / "cover" / name).replace(staging / "diagrams" / name)
            (staging / "cover" / "manifest.json").replace(staging / "diagrams" / "cover_render_manifest.json")
            (staging / "cover").rmdir()
            manifest["cover"] = cover
            write_json(staging / "diagrams" / "manifest.json", manifest)
        if export_document:
            export_preview(staging, plan, manifest, inventory=project / "inventory")
        from generic_render import plan_digest
        if plan_digest(load_project(project)[0]) != plan_digest(plan):
            raise ValueError("The draft changed during rendering. Previous outputs are preserved; render the new draft.")
        # Only publish after every step succeeds. Illustration-only application
        # jobs also roll back a filesystem publication failure, preserving the
        # displayed images instead of moving their whole directory away first.
        if not export_document:
            from preview_service import _archive_and_publish
            staged_files = {Path("diagrams") / path.name: path
                            for path in (staging / "diagrams").iterdir() if path.is_file() and path.name != "manifest.json"}
            staged_files[Path("diagrams/manifest.json")] = staging / "diagrams/manifest.json"
            removed = {Path(name) for name in ("index.html", "manual.pdf", "pdf_export.json", "instructions.json")
                       if (project / name).exists()}
            if (project / "diagrams").exists():
                removed.update(Path("diagrams") / path.name for path in (project / "diagrams").iterdir()
                               if path.is_file() and Path("diagrams") / path.name not in staged_files)
            _archive_and_publish(project, staged_files, removed, [])
            (staging / "diagrams").rmdir()
        else:
            for name in ("diagrams", "index.html", "manual.pdf", "pdf_export.json", "instructions.json"):
                archive_path(project, project / name)
                if (staging / name).exists():
                    (staging / name).replace(project / name)
        staging.rmdir()
    except Exception:
        if staging.exists():
            archive_path(project, staging)
        raise
    manifest = current_manifest(project, plan)
    if export_document:
        print(f"PREVIEW_READY {project / 'index.html'}", flush=True)
    else:
        print(f"DIAGRAMS_READY {project / 'diagrams'}", flush=True)
    return manifest


def confirm_project(project: Path, reviewer: str = "包装确认", *, export_document: bool = True):
    """Confirm current reviewed figures; the application exports PDF separately.

    Fixed-template PDFs remain valid when their existing receipt still matches
    source, content digest, template and bytes. A stale or status-sensitive old
    export loses its receipt instead of being silently regenerated.
    """
    from generic_render import plan_digest
    plan, _ = load_project(project)
    manifest = current_manifest(project, plan)
    if not all(step.get("reviewed") is True for step in plan["steps"]):
        raise ValueError("Review each step in the preview before confirming.")
    confirmation = {
        "source_sha256": plan["source"]["sha256"], "plan_sha256": plan_digest(plan),
        "reviewer": reviewer, "confirmed_at": utc_now(),
        "step_ids": [step["id"] for step in plan["steps"]],
        "meaning": "Packaging review of this specific draft; no external approval workflow.",
    }
    confirmed = deepcopy(plan)
    confirmed["status"] = "confirmed"
    confirmed["confirmation"] = confirmation
    if export_document:
        exports = prepare_exports(project, confirmed, manifest)
        # The CLI keeps its combined confirmation / export behavior.
        publish_exports(project, exports)
    else:
        from review_output import is_current_pdf
        if (project / "pdf_export.json").is_file() and not is_current_pdf(project, confirmed):
            # Keep the existing PDF available as historical display; removing
            # its invalid receipt makes it explicitly ineligible as current.
            archive_path(project, project / "pdf_export.json")
    archive_path(project, project / "steps.json")
    write_json(project / "steps.json", confirmed)
    write_json(project / "confirmation.json", confirmation)
    return confirmation


def _diagram_recipe(plan: dict) -> dict:
    """Geometry fields only, for an explicitly requested template migration."""
    editorial = {"title", "instruction", "consumer", "warnings", "evidence", "reviewed"}
    return {
        key: deepcopy(plan.get(key))
        for key in ("source", "groups", "style", "excluded_part_ids", "exclusion_reason", "cover_scene")
    } | {"steps": sorted([
        {key: deepcopy(value) for key, value in step.items() if key not in editorial}
        for step in plan["steps"]
    ], key=lambda step: step["id"])}


def bind_template_project(project: Path, template: Path, configuration: dict):
    """Bind a supplied template without generating any diagram or PDF.

    A verified figure may keep its bytes only if every geometry/visibility/
    arrow/view field remains identical by step ID. The migration is recorded
    separately; consumer wording and order become the explicit template values.
    """
    from draft_planner import validate_plan
    from generic_render import plan_digest
    from template_pdf import validate_template_binding, validate_template_source, validate_template_regions
    import fitz
    project, template = project.resolve(), template.resolve()
    old, analysis = load_project(project)
    config = deepcopy(configuration)
    if config.get("source_sha256") != old["source"]["sha256"]:
        raise ValueError("Template configuration belongs to a different STEP revision.")
    config["path"] = str(template)
    candidate = deepcopy(old)
    from template_pdf import manual_steps
    by_id = {step["id"]: deepcopy(step) for step in manual_steps(old)}
    inspection_scenes = [deepcopy(step) for step in old["steps"] if not step.get("include_in_manual", True)]
    bindings = config.get("step_bindings")
    slots = config.get("figure_slots", [])
    if bindings is None:
        if {slot.get("step_id") for slot in slots} != set(by_id) or len(slots) != len(by_id):
            raise ValueError("Template slots must explicitly map exactly one current step each.")
        bindings = [{"step_id": slot["step_id"], "title": by_id[slot["step_id"]]["title"],
                     "instruction": by_id[slot["step_id"]]["instruction"],
                     "consumer": deepcopy(by_id[slot["step_id"]].get("consumer", {}))} for slot in slots]
        config["step_bindings"] = bindings
    ids = [row.get("step_id") for row in bindings]
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ValueError("Template bindings must map every current step ID exactly once.")
    candidate["steps"] = [by_id[sid] for sid in ids] + inspection_scenes
    for step, binding in zip(manual_steps(candidate), bindings):
        for key in ("title", "instruction", "consumer"):
            if key in binding:
                step[key] = deepcopy(binding[key])
        step["reviewed"] = False
    config["document_binding"] = deepcopy(candidate.get("manual_document", {}))
    # Read copy from any supplied template. Geometry remains unchanged and
    # extracted text regions stay provisional until their layout is calibrated.
    from template_translation import discover_template_copy
    template_copy = discover_template_copy(template)
    if "text_regions" not in config:
        config["text_regions"] = template_copy["text_regions"]
    requested_keys = {region.get("key") for region in config.get("text_regions", [])}
    candidate["document_copy"] = {"entries": [row for row in template_copy["entries"]
                                               if row["key"] in requested_keys]}
    candidate["pdf_template"] = config
    candidate["status"] = "draft"
    candidate.pop("confirmation", None)
    validate_plan(candidate, analysis)
    validate_template_binding(candidate)
    validate_template_source(project, candidate)
    with fitz.open(template) as doc:
        validate_template_regions(candidate, doc)
    if _diagram_recipe(candidate) != _diagram_recipe(old):
        raise ValueError("Template binding must not change any current figure recipe.")
    try:
        manifest = deepcopy(current_manifest(project, old))
    except ValueError:
        manifest = None
    migration = {
        "kind": "template_binding_without_rerender", "at": utc_now(),
        "old_plan_sha256": plan_digest(old), "new_plan_sha256": plan_digest(candidate),
        "template_sha256": config["sha256"], "figure_recipes_unchanged": True,
        "figures_retained": manifest is not None,
    }
    if manifest is not None:
        manifest["plan_sha256"] = plan_digest(candidate)
        manifest.setdefault("editorial_migrations", []).append(migration)
        for entry in manifest["entries"]:
            sid = entry.get("step_id", entry.get("id"))
            if sid not in by_id:
                continue  # independent source-complete inspection overview
            step = by_id[sid]
            binding = next(row for row in bindings if row["step_id"] == step["id"])
            entry["title"], entry["instruction"] = binding["title"], binding["instruction"]
        if manifest.get("cover"):
            from cover_render import cover_plan
            manifest["cover"]["cover_plan_sha256"] = plan_digest(cover_plan(candidate))
    # Every old export stays recoverable, but none can be mistaken for the
    # newly bound template result. The next explicit render/export creates it.
    for name in ("steps.json", "manual.pdf", "pdf_export.json", "instructions.json", "index.html", "confirmation.json"):
        archive_path(project, project / name)
    write_json(project / "steps.json", candidate)
    if manifest is not None:
        archive_path(project, project / "diagrams" / "manifest.json")
        write_json(project / "diagrams" / "manifest.json", manifest)
    write_json(project / "template_binding.json", migration)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft", help="Import STEP and propose editable groups, contacts and steps")
    draft.add_argument("--step", required=True, type=Path)
    draft.add_argument("--project", required=True, type=Path)
    draft.add_argument("--title", default="安装说明草案")
    draft.add_argument("--tolerance", type=float, default=1.0, help="Candidate surface distance in normalized millimetres")
    draft.add_argument("--max-pairs", type=int, default=1000, help="Budget for exact nearest-surface comparisons")
    draft.add_argument("--pair-timeout", type=float, default=10.0, help="Seconds allowed for one native surface-distance calculation")
    for name in ("render", "review", "confirm", "export", "profile"):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True, type=Path)
        if name == "review":
            command.add_argument("--port", default=8765, type=int)
            command.add_argument("--no-browser", action="store_true")
        if name == "confirm":
            command.add_argument("--reviewer", default="包装确认")
        if name == "profile":
            command.add_argument("--profile", required=True, type=Path)
    template = commands.add_parser("template", help="Bind the supplied PDF and calibrated step slots without generating images")
    template.add_argument("--project", required=True, type=Path)
    template.add_argument("--template", required=True, type=Path)
    template.add_argument("--regions", required=True, type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("--old-project", required=True, type=Path)
    compare.add_argument("--new-project", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "draft":
            draft_project(args.step, args.project, args.title, args.tolerance, args.max_pairs, args.pair_timeout)
        elif args.command == "compare":
            from revision_analysis import compare_projects
            # Revision comparison only needs saved source-bound inventories;
            # an old consumer template may already have moved or changed.
            report = compare_projects(args.old_project, args.new_project)
            write_json(args.new_project / "revision_report.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "review":
            from review_server import serve
            serve(args.project.resolve(), port=args.port, open_browser=not args.no_browser)
        elif args.command == "render":
            render_project(args.project.resolve())
        elif args.command == "profile":
            from source_profile import apply_source_profile
            plan, analysis = load_project(args.project)
            save_plan(args.project.resolve(), apply_source_profile(plan, analysis, read_json(args.profile)))
        elif args.command == "template":
            bind_template_project(args.project, args.template, read_json(args.regions))
            print("TEMPLATE_BOUND: existing figures retained; no new figures or PDF generated.")
        elif args.command == "confirm":
            print(json.dumps(confirm_project(args.project.resolve(), args.reviewer), ensure_ascii=False, indent=2))
        elif args.command == "export":
            plan, _ = load_project(args.project)
            if not plan.get("pdf_template"):
                raise ValueError("最终 PDF 只能使用用户提供的模板；请先运行 template 命令绑定。")
            publish_exports(args.project, prepare_exports(args.project, plan, current_manifest(args.project, plan)))
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
