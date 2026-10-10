"""Stage and publish one edited STEP illustration without discarding others.

Projects must be serialized by their job queue. Old illustrations remain
available during rendering and after a failed job. Per-step recipes bind the
actual image inputs, while a whole-plan digest is current only when every
configured step has validated assets for its current recipe. No PDF is created.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import uuid

from draft_planner import validate_plan
from generic_render import _canonical, _plan_numbers, _validate, plan_digest, render_plan
from manual import load_project


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def step_recipe_sha256(plan: dict, step_id: str) -> str:
    """Hash source, membership and effective drawing inputs of just this step.

    Other steps' cameras/offsets/text do not invalidate this step. Global group
    membership and drawing style do, conservatively. Review flags, inferred
    evidence, warnings and editorial text are not geometry/arrow inputs.
    """
    step = next((step for step in plan["steps"] if step["id"] == step_id), None)
    if step is None:
        raise ValueError(f"Unknown preview step: {step_id}")
    defaults = {"camera": [1, .7, 1], "up": [0, 1, 0], "width": 1100,
                "height": 800, "margin": 70, "hlr": "poly"}
    style = {key: plan.get("style", {}).get(key, value) for key, value in defaults.items()}
    recipe = {
        "schema_version": 1, "source": plan["source"], "style": style,
        "groups": sorted([{"id": group["id"], "part_ids": list(group["part_ids"])}
                          for group in plan["groups"]], key=lambda group: group["id"]),
        "excluded_part_ids": sorted(plan.get("excluded_part_ids", [])),
        "step": {"id": step_id, "assembled_groups": list(step.get("assembled_groups", [])),
                 "moving_groups": list(step.get("moving_groups", [])), "offsets": step.get("offsets", {}),
                 "camera": step.get("camera", style["camera"]), "up": step.get("up", style["up"]),
                 "hidden_part_ids": list(step.get("hidden_part_ids", [])),
                 "focus_part_ids": list(step.get("focus_part_ids", [])),
                 "arrows": [{key: arrow.get(key) for key in ("from", "to", "meaning", "status")}
                            for arrow in step.get("arrows", [])]},
    }
    return hashlib.sha256(_canonical(_plan_numbers(recipe))).hexdigest()


def _path(directory: Path, name: str, extension: str) -> Path:
    if (not isinstance(name, str) or not name or Path(name).name != name or
            "/" in name or "\\" in name or ":" in name or Path(name).suffix.lower() != "." + extension):
        raise ValueError("Invalid tracked preview asset filename")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Preview asset escapes its diagram directory")
    return path


def _assets(entry, directory):
    result = []
    if not isinstance(entry, dict) or entry.get("status") != "ready":
        raise ValueError("Preview requires a ready rendered entry")
    children = [entry] + ([entry["focus"]] if isinstance(entry.get("focus"), dict) else [])
    for asset in children:
        for extension in ("svg", "png"):
            path = _path(directory, asset.get(f"{extension}_path"), extension)
            expected = asset.get(f"{extension}_sha256")
            if not path.is_file() or expected != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError(f"Preview asset is missing or changed: {path.name}")
            result.append(path)
    return result


def _valid_assets(entry, directory):
    try:
        _assets(entry, directory)
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False


def _candidate(old, candidate, analysis):
    if not isinstance(candidate, dict):
        raise ValueError("The preview candidate must be a plan object")
    candidate = deepcopy(candidate)
    if candidate.get("source") != old["source"]:
        raise ValueError("Source identity is read-only. Create a new STEP revision project.")
    if old.get("pdf_template") is not None:
        from template_pdf import validate_template_binding
        if candidate.get("pdf_template") != old["pdf_template"]:
            raise ValueError("原模板绑定不能在预览请求中更换或解除；请显式校准。")
        if candidate.get("manual_document") != old.get("manual_document"):
            raise ValueError("原模板文字和排版固定；本模式只修改步骤图。")
        validate_template_binding(candidate)
    elif candidate.get("pdf_template") is not None:
        raise ValueError("请显式绑定并校验用户提供的原模板。")
    if plan_digest(candidate) != plan_digest(old):
        candidate["status"] = "draft"
        candidate.pop("confirmation", None)
        for step in candidate.get("steps", []):
            step["reviewed"] = False
    else:
        # A preview request cannot manufacture confirmation or review state.
        candidate["status"] = old["status"]
        if "confirmation" in old:
            candidate["confirmation"] = deepcopy(old["confirmation"])
        else:
            candidate.pop("confirmation", None)
        review = {step["id"]: step["reviewed"] for step in old["steps"]}
        for step in candidate.get("steps", []):
            step["reviewed"] = review[step["id"]]
    validate_plan(candidate, analysis)
    return candidate


def _old_entries(manifest, old, directory, inventory_hash):
    """Migrate legacy recipe metadata only from validated matching provenance."""
    entries = manifest.get("entries", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Existing preview manifest entries are invalid")
    ids = [entry.get("step_id", entry.get("id")) for entry in entries]
    if any(not isinstance(sid, str) for sid in ids) or len(set(ids)) != len(ids):
        raise ValueError("Existing preview manifest needs distinct step IDs")
    trusted = (manifest.get("source_sha256") == old["source"]["sha256"] and
               manifest.get("inventory_sha256") == inventory_hash)
    legacy_matches_plan = trusted and manifest.get("plan_sha256") == plan_digest(old)
    old_ids = {step["id"] for step in old["steps"]}
    result = {}
    for original, sid in zip(entries, ids):
        entry = deepcopy(original)
        entry["step_id"] = sid
        entry.setdefault("source_sha256", manifest.get("source_sha256"))
        entry.setdefault("inventory_sha256", manifest.get("inventory_sha256"))
        if (not entry.get("recipe_sha256") and legacy_matches_plan and sid in old_ids
                and _valid_assets(entry, directory)):
            entry["recipe_sha256"] = step_recipe_sha256(old, sid)
            entry["recipe_migration"] = "validated_legacy_plan_and_assets"
        result[sid] = entry
    return result


def _archive_and_publish(project, staged_files, removed_files, history_names):
    """Publish a small transaction, keeping backups for rollback and recovery."""
    project = Path(project).resolve()
    directory = project / "history" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-preview-" + uuid.uuid4().hex[:8])
    touched = {Path(name) for name in staged_files} | {Path(name) for name in removed_files} | {Path(name) for name in history_names}
    prior = {}
    for relative in touched:
        path = project / relative
        if not path.resolve().is_relative_to(project):
            raise ValueError("Preview transaction path escapes the project")
        prior[relative] = path.exists()
    directory.mkdir(parents=True)
    # Copy before changing anything. A failed backup leaves all displayed files.
    for relative, exists in prior.items():
        if exists:
            backup = directory / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / relative, backup)
    changed = []
    try:
        # Publish assets first; steps and manifest are committed last.
        for relative, staged in staged_files.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(target)
            changed.append(relative)
        for relative in removed_files:
            target = project / relative
            if target.exists():
                target.unlink()
                changed.append(Path(relative))
    except Exception:
        for relative in reversed(changed):
            target = project / relative
            if prior[Path(relative)]:
                rollback = directory / relative
                temporary = target.with_name(target.name + ".preview-rollback-" + uuid.uuid4().hex)
                shutil.copy2(rollback, temporary)
                temporary.replace(target)
            else:
                target.unlink(missing_ok=True)
        raise
    return directory


def preview_step(project: Path, candidate_plan: dict, step_id: str) -> dict:
    """Generate only ``step_id`` and keep other images visible with stale flags.

    No candidate or file is published if validation, rendering, source checks or
    output-proof checks fail. The calling queue must serialize writes per project.
    """
    project = Path(project).resolve()
    old, analysis = load_project(project)
    candidate = _candidate(old, candidate_plan, analysis)
    if not isinstance(step_id, str) or step_id not in {step["id"] for step in candidate["steps"]}:
        raise ValueError("Select one known step for preview")
    _, _, _, _, inventory_hash, _ = _validate(candidate, project / "inventory")
    steps_path = project / "steps.json"
    prior_steps = steps_path.read_bytes()
    directory = project / "diagrams"
    manifest_path = directory / "manifest.json"
    prior_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
    previous = json.loads(prior_manifest.decode("utf-8")) if prior_manifest else {}
    entries = _old_entries(previous, old, directory, inventory_hash)
    staging = Path(tempfile.mkdtemp(prefix=".step-preview-", dir=project))
    try:
        generated = render_plan(candidate, project / "inventory", staging / "diagrams", step_ids=[step_id])
        rendered = generated.get("entries", [])
        if (generated.get("source_sha256") != candidate["source"]["sha256"] or
                generated.get("plan_sha256") != plan_digest(candidate) or
                generated.get("inventory_sha256") != inventory_hash or len(rendered) != 1 or
                rendered[0].get("step_id") != step_id):
            raise ValueError("Single-step render proof does not match the requested source/plan/inventory/step")
        updated = deepcopy(rendered[0])
        requested = next(step for step in candidate["steps"] if step["id"] == step_id)
        if bool(updated.get("focus")) != bool(requested.get("focus_part_ids")):
            raise ValueError("Single-step render proof does not contain the requested focus image")
        groups = {group["id"]: group for group in candidate["groups"]}
        expected_parts = {pid for gid in requested["assembled_groups"] + requested["moving_groups"]
                          for pid in groups[gid]["part_ids"]} - set(candidate.get("excluded_part_ids", [])) - set(requested.get("hidden_part_ids", []))
        if set(updated.get("part_ids", [])) != expected_parts:
            raise ValueError("Single-step render proof does not contain all retained source instances")
        if updated.get("focus") and updated["focus"].get("part_ids") != requested["focus_part_ids"]:
            raise ValueError("Single-step render proof focus differs from the selected source instances")
        new_assets = _assets(updated, staging / "diagrams")
        expected_names = {f"{step_id}.svg", f"{step_id}.png"}
        if requested.get("focus_part_ids"):
            expected_names.update({f"{step_id}_focus.svg", f"{step_id}_focus.png"})
        if {path.name for path in new_assets} != expected_names:
            raise ValueError("Single-step render assets must belong only to the requested step")
        updated.update(recipe_sha256=step_recipe_sha256(candidate, step_id),
                       source_sha256=candidate["source"]["sha256"], inventory_sha256=inventory_hash)
        replaced_entry = entries.get(step_id)
        entries[step_id] = updated
        stale_ids = []
        ordered_entries = []
        for step in candidate["steps"]:
            sid = step["id"]
            entry = entries.get(sid)
            expected = step_recipe_sha256(candidate, sid)
            current = bool(entry and entry.get("recipe_sha256") == expected
                           and entry.get("source_sha256") == candidate["source"]["sha256"]
                           and entry.get("inventory_sha256") == inventory_hash
                           and (sid == step_id or _valid_assets(entry, directory)))
            if not current:
                stale_ids.append(sid)
            if entry:
                entry["stale"] = not current
                entry["expected_recipe_sha256"] = expected
                entry["title"], entry["instruction"] = step["title"], step["instruction"]
                ordered_entries.append(entry)
        manifest = deepcopy(previous) if previous else deepcopy(generated)
        manifest.update(source_sha256=candidate["source"]["sha256"], inventory_sha256=inventory_hash,
                        entries=ordered_entries, stale_step_ids=stale_ids, remaining_step_ids=stale_ids,
                        requested_step_ids=[step_id], rendered_step_ids=[step_id], complete=not stale_ids,
                        plan_sha256=plan_digest(candidate) if not stale_ids else None,
                        candidate_plan_sha256=plan_digest(candidate), product_title=candidate["product"]["title"],
                        pdf_current=False, preview_mode="single_step")
        manifest.pop("error", None)
        # A changed generic cover stays explicitly stale; this function cannot
        # generate it as a side effect of previewing an installation step.
        stale_cover = False
        if candidate.get("cover_scene") and not candidate.get("pdf_template"):
            from cover_render import cover_plan
            cover = manifest.get("cover", {})
            stale_cover = not (cover.get("source_sha256") == candidate["source"]["sha256"]
                               and cover.get("cover_plan_sha256") == plan_digest(cover_plan(candidate))
                               and _valid_assets(cover, directory))
            if cover:
                cover["stale"] = stale_cover
        manifest["stale_cover"] = stale_cover
        # Detect edits or source/cache changes made by another process while CAD
        # was busy; the queue itself serializes only its own requests.
        now, _ = load_project(project)
        if steps_path.read_bytes() != prior_steps or plan_digest(now) != plan_digest(old):
            raise ValueError("The project changed during step preview; keep the previous images and retry")
        if (manifest_path.read_bytes() if manifest_path.is_file() else None) != prior_manifest:
            raise ValueError("The diagrams changed during step preview; keep the previous images and retry")
        if _validate(candidate, project / "inventory")[4] != inventory_hash:
            raise ValueError("The source inventory changed during step preview")
        (staging / "steps.json").write_bytes(_json_bytes(candidate))
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        staged_files = {Path("diagrams") / path.name: path for path in new_assets}
        staged_files[Path("steps.json")] = staging / "steps.json"
        staged_files[Path("diagrams/manifest.json")] = staging / "manifest.json"
        removed = {Path(name) for name in ("manual.pdf", "pdf_export.json", "instructions.json", "index.html", "confirmation.json")
                   if (project / name).exists()}
        if replaced_entry:
            for asset in [replaced_entry] + ([replaced_entry["focus"]] if isinstance(replaced_entry.get("focus"), dict) else []):
                for extension in ("svg", "png"):
                    name = asset.get(f"{extension}_path")
                    if name:
                        _path(directory, name, extension)
                        relative = Path("diagrams") / name
                        if relative not in staged_files:
                            removed.add(relative)
        history = _archive_and_publish(project, staged_files, removed,
                                       [Path("steps.json"), Path("diagrams/manifest.json")])
        return {"plan": candidate, "manifest": manifest, "stale_step_ids": stale_ids,
                "step_id": step_id, "pdf_current": False,
                "export_ready": bool(not stale_ids and not stale_cover and candidate.get("pdf_template")),
                "history_path": str(history), "rendered_step_ids": [step_id]}
    finally:
        # A recursive cleanup may remove only the resolved staging directory
        # created inside this explicitly selected project.
        if staging.name.startswith(".step-preview-") and staging.resolve().is_relative_to(project):
            shutil.rmtree(staging, ignore_errors=True)
