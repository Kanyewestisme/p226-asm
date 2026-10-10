"""Render editable assembly plans using only the imported STEP's actual geometry.

The output argument is the diagram directory. A different source, plan, or BREP
inventory requires a fresh output directory; a partial render never certifies
unrendered or modified diagrams as current. No product-specific indices are used.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import time
from pathlib import Path
from typing import Any


MAX_PARTS = 4096
MAX_STEPS = MAX_PARTS + 1  # one fallback step per occurrence plus the overview
MAX_ARROWS = 128
MAX_SVG_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}
_REVIEW_KEYS = {
    "status", "review", "reviewed", "review_status", "review_metadata",
    "confirmation", "confirmed", "confirmed_at", "confirmation_status",
    "user_confirmed", "user_confirmation", "approved", "approved_at",
}
_TOP_METADATA_KEYS = _REVIEW_KEYS | {
    "revision", "revision_id", "revision_history", "review_history", "updated_at",
}


def plan_digest(plan: dict) -> str:
    """Hash the illustrated content; confirmation bookkeeping needs no re-HLR.

    Arrow status and evidence remain content. Only top-level review/revision
    metadata and each step's user review flags are omitted.
    """
    content = {key: value for key, value in plan.items() if key not in _TOP_METADATA_KEYS}
    if isinstance(content.get("steps"), list):
        content["steps"] = [
            {key: value for key, value in step.items() if key not in _REVIEW_KEYS}
            if isinstance(step, dict) else step for step in content["steps"]
        ]
    # JSON.stringify writes 1.0 as 1 and -0.0 as 0. Those values describe the
    # same placement; a browser round-trip must not invalidate reviewed images.
    return hashlib.sha256(_canonical(_plan_numbers(content))).hexdigest()


def _plan_numbers(value: Any) -> Any:
    """Normalize JSON number semantics only for the plan content hash."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Plan content contains a non-finite number")
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {key: _plan_numbers(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plan_numbers(child) for child in value]
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"Missing required file: {path}")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"JSON exceeds the {MAX_JSON_BYTES // 1024 // 1024} MiB limit: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _identifier(value: Any, kind: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or value.upper() in _RESERVED:
        raise ValueError(f"Invalid {kind} ID {value!r}; use 1–80 ASCII letters, numbers, underscores or hyphens")
    return value


def _vector(value: Any, field: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field} must contain three finite coordinates")
    if any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in value):
        raise ValueError(f"{field} must contain three finite coordinates")
    result = tuple(float(number) for number in value)
    if not all(math.isfinite(number) and abs(number) <= 1e9 for number in result):
        raise ValueError(f"{field} coordinates must be finite and at most 1e9 in magnitude")
    return result


def _camera(camera: Any, up: Any) -> tuple[tuple, tuple]:
    camera, up = _vector(camera, "camera"), _vector(up, "up")
    camera_length, up_length = math.sqrt(sum(x*x for x in camera)), math.sqrt(sum(x*x for x in up))
    if camera_length < 1e-9 or up_length < 1e-9:
        raise ValueError("Camera and up must be nonzero")
    cross = (up[1]*camera[2]-up[2]*camera[1], up[2]*camera[0]-up[0]*camera[2], up[0]*camera[1]-up[1]*camera[0])
    if math.sqrt(sum(x*x for x in cross)) / camera_length / up_length < 1e-8:
        raise ValueError("Camera and up are parallel; choose another up vector")
    return camera, up


def _relative_brep(inventory: Path, row: dict) -> Path:
    raw = row.get("brep")
    if not isinstance(raw, str) or not raw:
        raise ValueError("Each inventory part must name a relative BREP file")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or ":" in raw or "\\" in raw and "../" in raw.replace("\\", "/"):
        raise ValueError(f"BREP path must remain inside the inventory: {raw!r}")
    result = (inventory / relative).resolve()
    if not result.is_relative_to(inventory) or result.suffix.lower() != ".brep":
        raise ValueError(f"BREP path must remain inside the inventory: {raw!r}")
    if not result.is_file():
        raise FileNotFoundError(f"Missing imported BREP: {result}")
    return result


def _validate(plan: dict, inventory: Path) -> tuple[dict, dict, dict, list, str, list]:
    if not isinstance(plan, dict) or isinstance(plan.get("schema_version", plan.get("schema")), bool) or plan.get("schema_version", plan.get("schema")) != 1:
        raise ValueError("Plan must use schema version 1")
    rows = _read_json(inventory / "assembly_parts.json")
    completion = _read_json(inventory / "import_complete.json")
    if not isinstance(completion, dict):
        raise ValueError("Invalid inventory completion record")
    source_hash = completion.get("source_sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_hash):
        raise ValueError("Inventory has no valid source SHA256; import the STEP again")
    if not isinstance(plan.get("source"), dict) or plan["source"].get("sha256") != source_hash:
        raise ValueError("Plan source SHA256 does not match the imported STEP; draft a plan for the new model")
    if completion.get("brep_paths_relative_to") != "inventory":
        raise ValueError("Generic rendering requires BREP paths relative to the inventory directory")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PARTS or completion.get("parts") != len(rows):
        raise ValueError("Incomplete or oversized inventory; import the STEP again")
    if completion.get("inventory_sha256") and _file_digest(inventory / "assembly_parts.json") != completion["inventory_sha256"]:
        raise ValueError("Inventory JSON differs from its completed STEP import")
    parts, inventory_content = {}, []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("Inventory parts need distinct instance IDs; run the generic STEP importer")
        if row["id"] in parts:
            raise ValueError(f"Duplicate inventory instance ID: {row['id']}")
        if row.get("source_sha256") and row["source_sha256"] != source_hash:
            raise ValueError(f"Part {row['id']} belongs to another STEP source")
        brep = _relative_brep(inventory, row)
        brep_hash = _file_digest(brep)
        if row.get("brep_sha256") and row["brep_sha256"] != brep_hash:
            raise ValueError(f"BREP cache differs from its completed STEP import: {row['brep']}")
        parts[row["id"]] = {"row": row, "brep": brep}
        inventory_content.append({"row": row, "brep_sha256": brep_hash})
    inventory_hash = hashlib.sha256(_canonical(inventory_content)).hexdigest()
    excluded_ids = plan.get("excluded_part_ids", [])
    if (not isinstance(excluded_ids, list) or not all(isinstance(pid, str) for pid in excluded_ids)
            or len(set(excluded_ids)) != len(excluded_ids) or any(pid not in parts for pid in excluded_ids)):
        raise ValueError("excluded_part_ids must list distinct known source instance IDs")
    excluded = set(excluded_ids)
    reason = plan.get("exclusion_reason")
    if excluded and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("Explicit part exclusions require a nonempty exclusion_reason")
    group_rows = plan.get("groups")
    if not isinstance(group_rows, list) or not 1 <= len(group_rows) <= MAX_PARTS:
        raise ValueError("Plan needs at least one assembly group")
    groups, assigned = {}, set()
    for group in group_rows:
        if not isinstance(group, dict):
            raise ValueError("Each assembly group must be an object")
        gid = _identifier(group.get("id"), "group")
        if gid in groups:
            raise ValueError(f"Duplicate group ID: {gid}")
        ids = group.get("part_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(pid, str) for pid in ids) or len(set(ids)) != len(ids):
            raise ValueError(f"Group {gid} needs distinct part IDs")
        for pid in ids:
            if pid not in parts:
                raise ValueError(f"Group {gid} references unknown part ID: {pid}")
            if pid in assigned:
                raise ValueError(f"Part {pid} belongs to more than one group")
            assigned.add(pid)
        groups[gid] = group
    if assigned != set(parts):
        raise ValueError("Groups must cover every source instance, including explicitly excluded parts")
    style = {"camera": [1, .7, 1], "up": [0, 1, 0], "width": 1100, "height": 800, "margin": 70, "hlr": "poly"}
    configured_style = plan.get("style", {})
    if not isinstance(configured_style, dict):
        raise ValueError("style must be an object")
    style.update(configured_style)
    _camera(style["camera"], style["up"])
    for key in ("width", "height"):
        if isinstance(style[key], bool) or not isinstance(style[key], int) or not 256 <= style[key] <= 4096:
            raise ValueError(f"style.{key} must be an integer between 256 and 4096")
    margin = style["margin"]
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(margin) or not 32 <= margin < min(style["width"], style["height"]) / 2 - 10:
        raise ValueError("style.margin must leave room for the model and be at least 32")
    if style["hlr"] not in ("poly", "exact"):
        raise ValueError("style.hlr must be 'poly' or 'exact'")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"Plan needs between 1 and {MAX_STEPS} steps")
    filenames = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Each assembly step must be an object")
        sid = _identifier(step.get("id"), "step")
        # Windows filenames are case insensitive, including the generated local view.
        for filename in (sid.lower(), f"{sid.lower()}_focus"):
            if filename in filenames:
                raise ValueError(f"Step IDs collide as output filenames: {sid}")
            filenames.add(filename)
        assembled, moving = step.get("assembled_groups", []), step.get("moving_groups", [])
        if not isinstance(assembled, list) or not isinstance(moving, list):
            raise ValueError(f"Step {sid} groups must be lists")
        displayed = assembled + moving
        if not displayed or not all(isinstance(gid, str) for gid in displayed) or len(set(displayed)) != len(displayed) or any(gid not in groups for gid in displayed):
            raise ValueError(f"Step {sid} needs distinct known assembled/moving groups")
        offsets = step.get("offsets", {})
        if not isinstance(offsets, dict) or any(gid not in displayed for gid in offsets):
            raise ValueError(f"Step {sid} offsets may only refer to groups in that step")
        for gid, offset in offsets.items():
            _vector(offset, f"Step {sid} offset {gid}")
        _camera(step.get("camera", style["camera"]), step.get("up", style["up"]))
        arrows = step.get("arrows", [])
        if not isinstance(arrows, list) or len(arrows) > MAX_ARROWS:
            raise ValueError(f"Step {sid} allows at most {MAX_ARROWS} arrows")
        for arrow in arrows:
            if not isinstance(arrow, dict) or arrow.get("meaning") != "reposition":
                raise ValueError(f"Step {sid} arrows must explicitly mean 'reposition'")
            _vector(arrow.get("from"), "arrow.from")
            _vector(arrow.get("to"), "arrow.to")
        shown_parts = {pid for gid in displayed for pid in groups[gid]["part_ids"]}
        hidden_ids = step.get("hidden_part_ids", [])
        if (not isinstance(hidden_ids, list) or len(hidden_ids) > MAX_PARTS
                or not all(isinstance(pid, str) for pid in hidden_ids)
                or len(set(hidden_ids)) != len(hidden_ids) or any(pid not in parts for pid in hidden_ids)):
            raise ValueError(f"Step {sid} hidden_part_ids must list distinct known source instance IDs")
        hidden = set(hidden_ids)
        if not hidden <= shown_parts:
            raise ValueError(f"Step {sid} hidden parts must belong to displayed assembled/moving groups")
        if hidden & excluded:
            raise ValueError(f"Step {sid} hidden_part_ids must not overlap globally excluded_part_ids")
        if hidden and (not isinstance(step.get("visibility_reason"), str) or not step["visibility_reason"].strip()):
            raise ValueError(f"Step {sid} temporary part hiding requires a nonempty visibility_reason")
        retained_parts = shown_parts - excluded - hidden
        if not retained_parts:
            raise ValueError(f"Step {sid} has no retained geometry after exclusions and temporary hiding")
        for gid in moving:
            if not set(groups[gid]["part_ids"]) - excluded - hidden:
                raise ValueError(f"Step {sid} moving group {gid} has no retained geometry after exclusions and temporary hiding")
        focus = step.get("focus_part_ids", [])
        if not isinstance(focus, list) or len(focus) > MAX_PARTS or not all(isinstance(pid, str) for pid in focus) or len(set(focus)) != len(focus) or any(pid not in retained_parts for pid in focus):
            raise ValueError(f"Step {sid} focus parts must be distinct parts visible in that step")
    return parts, groups, style, steps, inventory_hash, excluded_ids


def _arrow_svg(arrows: list, projection: dict) -> tuple[str, list]:
    from cad_pipeline import project_point

    overlay, skipped = [], []
    for index, arrow in enumerate(arrows):
        x1, y1 = project_point(arrow["from"], projection)
        x2, y2 = project_point(arrow["to"], projection)
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 2:
            skipped.append({"index": index, "reason": "Arrow projects to less than two pixels; choose another view or edit its points"})
            continue
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        head = min(14., length * .35)
        hx, hy = x2 - head * ux, y2 - head * uy
        wing = head * .38
        dash = ' stroke-dasharray="7 4"' if arrow.get("status", "proposed") == "proposed" else ""
        overlay.append(
            f'<g><title>{html.escape(str(arrow.get("status", "proposed")))} repositioning; not a fastener direction</title>'
            f'<line x1="{x1:.4f}" y1="{y1:.4f}" x2="{hx:.4f}" y2="{hy:.4f}" stroke="#d62720" stroke-width="2.5"{dash}/>'
            f'<polygon points="{x2:.4f},{y2:.4f} {hx-wing*uy:.4f},{hy+wing*ux:.4f} {hx+wing*uy:.4f},{hy-wing*ux:.4f}" fill="#d62720"/></g>'
        )
    return "".join(overlay), skipped


def _write_asset(output: Path, asset_id: str, svg: str) -> dict:
    import fitz

    data = svg.encode("utf-8")
    if len(data) > MAX_SVG_BYTES:
        raise ValueError(f"Diagram {asset_id} exceeds the {MAX_SVG_BYTES // 1024 // 1024} MiB SVG limit; simplify the view or use polygonal HLR")
    svg_path, png_path = output / f"{asset_id}.svg", output / f"{asset_id}.png"
    svg_path.write_bytes(data)
    # Both documents own native resources; close them even if conversion fails.
    with fitz.open(stream=data, filetype="svg") as svg_document:
        pdf_bytes = svg_document.convert_to_pdf()
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf_document:
        pdf_document[0].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).save(png_path)
    return {
        "svg_path": svg_path.name, "png_path": png_path.name,
        "svg_sha256": hashlib.sha256(data).hexdigest(), "png_sha256": _file_digest(png_path),
    }


def _current_entry(entry: dict, output: Path) -> bool:
    assets = [entry] + ([entry["focus"]] if isinstance(entry.get("focus"), dict) else [])
    for asset in assets:
        for extension in ("svg", "png"):
            name = asset.get(f"{extension}_path")
            if not isinstance(name, str) or Path(name).name != name:
                return False
            path = output / name
            if not path.is_file() or _file_digest(path) != asset.get(f"{extension}_sha256"):
                return False
    return entry.get("status") == "ready"


def render_plan(plan: dict, inventory: Path, output: Path, step_ids: list | None = None) -> dict:
    """Render arbitrary configured groups together, preserving interpart hiding.

    Arrows illustrate proposed repositioning, not inferred insertion/fastener
    directions. Explicit focus parts get a separate model-derived detail view.
    Shape, placement, and surfaces come exclusively from imported BREP files.
    Global excluded_part_ids omit reviewed auxiliary geometry. Each step may
    temporarily hide separately imported source instances with hidden_part_ids;
    the manifest records their visibility_reason separately from exclusions.
    Both kinds of omitted instance stay unchanged in the source inventory/groups.
    """
    inventory, output = Path(inventory).resolve(), Path(output).resolve()
    parts, groups, style, steps, inventory_hash, excluded_ids = _validate(plan, inventory)
    excluded = set(excluded_ids)
    digest, source_hash = plan_digest(plan), plan["source"]["sha256"]
    ids = [step["id"] for step in steps]
    if step_ids is not None and not isinstance(step_ids, list):
        raise ValueError("step_ids must be a list of step IDs")
    requested = ids if step_ids is None else list(step_ids)
    if not requested or len(set(requested)) != len(requested) or any(sid not in ids for sid in requested):
        raise ValueError("Select distinct step IDs that exist in the current plan")
    if output == inventory or inventory.is_relative_to(output):
        raise ValueError("Diagram output must not contain or replace the BREP inventory")
    manifest_path = output / "manifest.json"
    entries = []
    if manifest_path.exists():
        previous = _read_json(manifest_path)
        if any(previous.get(key) != value for key, value in {
            "source_sha256": source_hash, "plan_sha256": digest, "inventory_sha256": inventory_hash,
        }.items()):
            raise ValueError("Output belongs to another source, plan or BREP inventory; render into a fresh directory")
        entries = [entry for entry in previous.get("entries", []) if entry.get("step_id") in ids and _current_entry(entry, output)]
    elif output.exists() and any(output.iterdir()):
        raise ValueError("Output directory contains untracked files; choose a fresh diagram directory")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "source_sha256": source_hash, "plan_sha256": digest,
        "inventory_sha256": inventory_hash, "product_title": plan.get("product", {}).get("title", ""),
        "geometry_basis": "Imported STEP BREP instances only, including source surfaces"
        + ("; explicitly listed instances are omitted from illustrations" if excluded else "")
        + ("; per-step temporary visibility does not alter source geometry" if any(step.get("hidden_part_ids") for step in steps) else ""),
        "excluded_part_ids": excluded_ids,
        "exclusion_reason": plan.get("exclusion_reason") if excluded else None,
        "arrow_meaning": "Proposed group repositioning, not verified insertion or screw direction",
        "requested_step_ids": requested, "entries": entries,
    }

    def save_manifest() -> None:
        by_id = {entry["step_id"]: entry for entry in manifest["entries"]}
        manifest["entries"] = [by_id[sid] for sid in ids if sid in by_id]
        manifest["rendered_step_ids"] = [sid for sid in ids if sid in by_id]
        manifest["remaining_step_ids"] = [sid for sid in ids if sid not in by_id]
        manifest["complete"] = not manifest["remaining_step_ids"]
        _atomic_json(manifest_path, manifest)

    # A requested regeneration is pending until newly rendered assets succeed.
    manifest["entries"] = [entry for entry in entries if entry["step_id"] not in requested]
    save_manifest()
    import cadquery as cq
    from cad_pipeline import hidden_line_svg

    shape_cache = {}
    for step in steps:
        sid = step["id"]
        if sid not in requested:
            continue
        started = time.monotonic()
        print(f"RENDER_START {sid}", flush=True)
        try:
            instances, transforms, planned_ids, omitted_ids = {}, {}, [], []
            hidden_ids = step.get("hidden_part_ids", [])
            hidden = set(hidden_ids)
            for gid in step.get("assembled_groups", []) + step.get("moving_groups", []):
                offset = _vector(step.get("offsets", {}).get(gid, [0, 0, 0]), f"offset {gid}")
                for pid in groups[gid]["part_ids"]:
                    planned_ids.append(pid)
                    if pid in excluded:
                        omitted_ids.append(pid)
                        continue
                    if pid in hidden:
                        continue
                    if pid not in shape_cache:
                        shape_cache[pid] = cq.Shape.importBrep(str(parts[pid]["brep"]))
                    instances[pid] = shape_cache[pid].translate(offset)
                    transforms[pid] = list(offset)
            camera, up = _camera(step.get("camera", style["camera"]), step.get("up", style["up"]))
            options = dict(camera=camera, up=up, width=style["width"], height=style["height"], margin=style["margin"], poly=style["hlr"] == "poly")
            caption = f"{plan.get('product', {}).get('title', '')}: {step.get('title', sid)}. Actual STEP geometry; proposed assembly draft."
            # One compound and one HLR pass: nearer groups hide farther groups.
            svg, projection = hidden_line_svg(cq.Compound.makeCompound(list(instances.values())), caption=caption, **options)
            overlay, skipped = _arrow_svg(step.get("arrows", []), projection)
            svg = svg.replace("</svg>", overlay + "</svg>")
            entry = {
                "step_id": sid, "asset_id": sid, "title": step.get("title", sid),
                "instruction": step.get("instruction", ""), "status": "ready",
                "part_ids": list(instances), "cad_part_indices": [parts[pid]["row"]["index"] for pid in instances],
                "planned_part_ids": planned_ids, "excluded_part_ids": omitted_ids,
                "hidden_part_ids": list(hidden_ids),
                "visibility_reason": step.get("visibility_reason") if hidden else None,
                "transforms": transforms, "projection": projection, "render_method": projection["algorithm"],
                "warnings": list(step.get("warnings", [])), "evidence": step.get("evidence", []),
                "arrows": step.get("arrows", []), "skipped_arrows": skipped,
                "visual_review": "pending: inspect the generated diagram; rendering is not user confirmation",
                **_write_asset(output, sid, svg),
            }
            focus = step.get("focus_part_ids", [])
            if focus:
                focus_svg, focus_projection = hidden_line_svg(cq.Compound.makeCompound([instances[pid] for pid in focus]), caption=f"Model-derived local view for {sid}; no added hardware or inferred holes", **options)
                entry["focus"] = {
                    "part_ids": focus, "projection": focus_projection,
                    "meaning": "Selected actual model parts, fitted to their own view; does not invent connection details",
                    **_write_asset(output, f"{sid}_focus", focus_svg),
                }
            entry["seconds"] = round(time.monotonic() - started, 3)
            manifest["entries"].append(entry)
            manifest.pop("error", None)
            save_manifest()
            print(f"RENDER_READY {sid} {entry['seconds']}s", flush=True)
        except Exception as error:
            manifest["error"] = {"step_id": sid, "message": str(error)}
            save_manifest()
            raise
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Fresh directory for SVG, PNG and manifest.json")
    parser.add_argument("--step", action="append", dest="step_ids", help="Render only this step ID; may be repeated")
    args = parser.parse_args()
    render_plan(_read_json(args.plan), args.inventory, args.output, args.step_ids)


if __name__ == "__main__":
    main()
