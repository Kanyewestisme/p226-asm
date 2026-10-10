"""Source-bound browser display meshes from retained STEP BREP instances.

Tessellation is a preview approximation in millimetres, never a replacement for
the source BREP / hidden-line renderer. All source instances remain in the cache;
the viewer applies the selected step's visibility and whole-group offsets.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
import os
from typing import Callable


SCHEMA_VERSION = 1
LINEAR_DEFLECTION_MM = 0.8
ANGULAR_DEFLECTION_RAD = 0.35
MAX_SCENE_BYTES = 128 * 1024 * 1024
MAX_VERTICES = 3_000_000
MAX_TRIANGLES = 2_500_000


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _binding(project: Path) -> tuple[list, dict, dict]:
    # The importer validates every retained BREP digest without loading CAD.
    from model_analysis import _load_inventory
    inventory = project / "inventory"
    rows, completion = _load_inventory(inventory)
    source = _read(project / "source.json")
    plan = _read(project / "steps.json")
    source_hash = completion["source_sha256"]
    if source.get("sha256") != source_hash or plan.get("source", {}).get("sha256") != source_hash:
        raise ValueError("Scene source and retained STEP inventory differ; import the current revision first")
    source_path = Path(source["path"])
    if source_path.is_file() and _hash(source_path) != source_hash:
        raise ValueError("The input STEP has changed; do not reuse the previous display mesh")
    recipe = {"schema_version": SCHEMA_VERSION, "source_sha256": source_hash,
              "inventory_sha256": completion["inventory_sha256"],
              "linear_deflection_mm": LINEAR_DEFLECTION_MM,
              "angular_deflection_rad": ANGULAR_DEFLECTION_RAD}
    recipe_hash = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    evidence = {**recipe, "recipe_sha256": recipe_hash,
                "brep_sha256": {row["id"]: row["brep_sha256"] for row in rows},
                "original_step_checked": source_path.is_file()}
    return rows, completion, evidence


def load_cached_scene(project: Path) -> dict | None:
    """Return a verified current cache, without importing CadQuery or OCP.

    Source / inventory corruption fails explicitly. Missing, stale or modified
    mesh caches return None so the HTTP worker can rebuild from retained BREP.
    """
    project = Path(project).resolve()
    rows, _, evidence = _binding(project)
    scene_path, manifest_path = project / "scene" / "scene.json", project / "scene" / "scene_manifest.json"
    if not scene_path.is_file() or not manifest_path.is_file() or scene_path.stat().st_size > MAX_SCENE_BYTES:
        return None
    try:
        manifest = _read(manifest_path)
        if any(manifest.get(key) != evidence[key] for key in ("schema_version", "source_sha256", "inventory_sha256", "recipe_sha256", "brep_sha256")):
            return None
        if _hash(scene_path) != manifest.get("scene_sha256"):
            return None
        scene = _read(scene_path)
        if (scene.get("source_sha256") != evidence["source_sha256"] or scene.get("inventory_sha256") != evidence["inventory_sha256"]
                or scene.get("units") != "mm" or [part.get("id") for part in scene.get("parts", [])] != [row["id"] for row in rows]):
            return None
        return scene
    except (ValueError, TypeError, KeyError):
        return None


def _sample_part(path: Path, row: dict) -> dict:
    import cadquery as cq
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    shape = cq.Shape.importBrep(str(path))
    # CadQuery's mesh helper uses relative deflection. Build an absolute-mm
    # mesh first so the recorded preview tolerance has an honest unit meaning.
    BRepMesh_IncrementalMesh(shape.wrapped, LINEAR_DEFLECTION_MM, False, ANGULAR_DEFLECTION_RAD, True).Perform()
    vertices, triangles = shape.tessellate(LINEAR_DEFLECTION_MM, ANGULAR_DEFLECTION_RAD)
    if row.get("faces", 0) and (not vertices or not triangles):
        raise ValueError(f"Cannot tessellate source instance {row['id']}; no source faces may be silently omitted")
    positions = [round(float(coordinate), 6) for vertex in vertices for coordinate in vertex.toTuple()]
    indices = [int(index) for triangle in triangles for index in triangle]
    lines = []
    if not row.get("faces", 0):
        # Preserve actual wires and vertices too, without inventing surfaces.
        for edge in shape.Edges():
            points, _ = edge.sample(float(LINEAR_DEFLECTION_MM))
            pairs = list(zip(points, points[1:]))
            if points and edge.IsClosed():
                pairs.append((points[-1], points[0]))
            lines.extend(round(float(c), 6) for pair in pairs for point in pair for c in point.toTuple())
        if not lines:
            positions = [round(float(c), 6) for vertex in shape.Vertices() for c in vertex.Center().toTuple()]
    if any(not math.isfinite(number) for number in positions + lines):
        raise ValueError(f"Non-finite display coordinates in source instance {row['id']}")
    if any(index < 0 or index >= len(positions)//3 for index in indices):
        raise ValueError(f"Invalid triangle indices in source instance {row['id']}")
    result = {"id": row["id"], "name": row.get("name", ""), "positions": positions, "indices": indices,
              "bbox": [row["bbox_min"], row["bbox_max"]], "solids": row.get("solids", 0),
              "faces": row.get("faces", 0), "kind": "surface_mesh" if indices else "source_wire_or_points"}
    if lines:
        result["lines"] = lines
    return result


def _atomic_bytes(path: Path, data: bytes):
    descriptor, name = tempfile.mkstemp(prefix=".scene-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_scene(project: Path, progress: Callable[[dict], None] | None = None) -> dict:
    """Build/cache a complete current display scene; never generate SVG or PDF."""
    project = Path(project).resolve()
    cached = load_cached_scene(project)
    if cached is not None:
        if progress:
            progress({"phase": "cached", "current": len(cached["parts"]), "total": len(cached["parts"]), "triangles": cached["triangle_count"]})
        return cached
    rows, completion, evidence = _binding(project)
    parts, vertices, triangles, estimated_bytes = [], 0, 0, 0
    for number, row in enumerate(rows, 1):
        if progress:
            progress({"phase": "tessellating", "current": number-1, "total": len(rows), "part_id": row["id"], "triangles": triangles})
        part = _sample_part(project / "inventory" / row["brep"], row)
        vertices += len(part["positions"])//3
        triangles += len(part["indices"])//3
        estimated_bytes += len(json.dumps(part, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        if vertices > MAX_VERTICES or triangles > MAX_TRIANGLES or estimated_bytes > MAX_SCENE_BYTES:
            raise ValueError("Display scene exceeds the browser mesh budget; increase preview tessellation deflection explicitly. No source instances were dropped or partial cache published.")
        parts.append(part)
    bounds = [[min(row["bbox_min"][axis] for row in rows) for axis in range(3)],
              [max(row["bbox_max"][axis] for row in rows) for axis in range(3)]]
    scene = {"schema_version": SCHEMA_VERSION, "source_sha256": completion["source_sha256"],
             "inventory_sha256": completion["inventory_sha256"], "units": "mm", "parts": parts,
             "bounds": bounds, "triangle_count": triangles, "vertex_count": vertices,
             "display_only": True, "geometry_basis": "Retained source STEP BREP instances; tessellated for browser preview only",
             "tessellation": {"linear_deflection_mm": LINEAR_DEFLECTION_MM, "angular_deflection_rad": ANGULAR_DEFLECTION_RAD}}
    data = json.dumps(scene, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_SCENE_BYTES:
        raise ValueError("Display scene JSON exceeds 128 MiB; no partial mesh cache was published")
    # Catch a source/inventory replacement during native tessellation.
    _, _, current = _binding(project)
    if any(current[key] != evidence[key] for key in ("source_sha256", "inventory_sha256", "brep_sha256", "recipe_sha256")):
        raise ValueError("STEP inventory changed while building the display scene; retry the current source")
    target = project / "scene"
    target.mkdir(exist_ok=True)
    _atomic_bytes(target / "scene.json", data)
    metadata = {**evidence, "scene_sha256": hashlib.sha256(data).hexdigest(), "parts": len(parts), "triangles": triangles, "bytes": len(data)}
    _atomic_bytes(target / "scene_manifest.json", json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))
    if progress:
        progress({"phase": "complete", "current": len(rows), "total": len(rows), "triangles": triangles})
    return scene
