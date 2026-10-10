"""Source-bound STEP inventory and explicitly unconfirmed geometric contacts.

Occurrence IDs identify one occurrence *within this exact STEP source only*.
Names and coarse geometry fingerprints are descriptive hints, never proof of
correspondence between revisions. Distances do not establish an assembly joint,
installation order, insertion direction, screw specification, or safe motion.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value: Any) -> None:
    """Publish a complete JSON file, never a partially written manifest."""
    handle, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _occurrence_id(digest: str, path: list[str]) -> str:
    return f"{digest}:{'/'.join(path)}"


def _finite(values: list[float], description: str) -> list[float]:
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"Non-finite {description}")
    return values


def _is_empty_compound(raw: Any) -> bool:
    """Recognize structural placeholders without discarding surfaces or curves."""
    from OCP.TopAbs import TopAbs_COMPOUND, TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    if raw.IsNull() or raw.ShapeType() != TopAbs_COMPOUND:
        return False
    return not any(TopExp_Explorer(raw, kind).More()
                   for kind in (TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX))


def import_model(step: Path, inventory: Path) -> dict:
    """Import STP/STEP into a new or empty inventory directory.

    BREP files contain the actual complete leaf shape in its assembly position,
    including surfaces and shells. Compound leaves are not silently split into
    invented physical parts. Output is staged separately and published only
    after every BREP and manifest has succeeded; an existing nonempty inventory
    is never overwritten. Create a fresh directory for each source revision.
    """
    source, out = Path(step).resolve(), Path(inventory).resolve()
    if source.suffix.lower() not in {".stp", ".step"}:
        raise ValueError("Only STP/STEP input is supported")
    if not source.is_file():
        raise FileNotFoundError(f"STEP not found: {source}")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"Inventory must be a fresh or empty directory: {out}")
    digest = _sha256(source)

    import cadquery as cq
    from cadquery.occ_impl.importers.assembly import _get_name
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.STEPConstruct import STEPConstruct_ExternRefs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-import-", dir=out.parent))
    started = time.monotonic()
    rows: list[dict] = []
    assemblies: list[dict] = []
    non_geometric_occurrences: list[dict] = []
    warnings = [
        "Occurrence identities apply only to this source SHA256; do not reuse across revisions.",
        "Geometry fingerprints are coarse hints, not authoritative revision correspondence.",
    ]

    def log(*values: Any) -> None:
        print(round(time.monotonic() - started, 1), *values, flush=True)

    try:
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(False)
        reader.SetLayerMode(False)
        reader.SetPropsMode(False)
        log("READ_START", source.name)
        if reader.ReadFile(str(source)) != IFSelect_RetDone:
            raise RuntimeError(f"OpenCascade could not read STEP: {source}")
        # Use the bound STEPConstruct API before transfer. ExternFiles() exposes
        # an unregistered C++ map in pinned OCP and cannot be called from Python.
        references = STEPConstruct_ExternRefs(reader.Reader().WS())
        references.LoadExternRefs()
        if references.NbExternRefs() > 0:
            raise ValueError("External STEP references are unsupported; export a self-contained STP assembly")
        doc = TDocStd_Document(TCollection_ExtendedString("ManualGenerator"))
        # OCCT's XCAF reader uses the document length unit when translating.
        # This overload is expressed in metres: .001 m per internal unit = mm.
        XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 0.001)
        if not reader.Transfer(doc):
            raise RuntimeError("STEP XCAF transfer failed")
        log("TRANSFER_DONE")
        tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        roots = TDF_LabelSequence()
        tool.GetFreeShapes(roots)

        def walk(label: Any, location: Any, names: list[str],
                 occurrence: list[str], ancestors: list[dict]) -> None:
            name = _get_name(label)
            located = location * cq.Location(tool.GetLocation_s(label))
            if tool.IsReference_s(label):
                referred = TDF_Label()
                if not tool.GetReferredShape_s(label, referred):
                    raise ValueError(f"Unresolved STEP reference: {'/'.join(occurrence)}")
                label = referred
                name = _get_name(referred) or name
            name = str(name or "Unnamed component")
            full_names = names + [name]
            identity = _occurrence_id(digest, occurrence)
            if tool.IsAssembly_s(label):
                node = {"id": identity, "name": name,
                        "occurrence_path": occurrence,
                        "parent_occurrence": _occurrence_id(digest, occurrence[:-1])
                        if len(occurrence) > 1 else ""}
                assemblies.append(node)
                children = TDF_LabelSequence()
                tool.GetComponents_s(label, children)
                if children.Length() == 0:
                    own_shape = tool.GetShape_s(label)
                    if own_shape.IsNull() or _is_empty_compound(own_shape):
                        warnings.append(f"Empty assembly occurrence: {identity}")
                        non_geometric_occurrences.append({**node, "path": full_names,
                            "reason": "empty_assembly", "geometry_status": "no_geometry"})
                        return
                    warnings.append(f"Assembly with no components retains its own geometry: {identity}")
                else:
                    for number in range(1, children.Length() + 1):
                        walk(children.Value(number), located, full_names,
                             occurrence + [f"component:{number:04d}"], ancestors + [node])
                    return

            raw = tool.GetShape_s(label)
            if raw.IsNull():
                raise ValueError(f"STEP occurrence has no geometry: {identity}")
            if _is_empty_compound(raw):
                non_geometric_occurrences.append({"id": identity, "name": name,
                    "occurrence_path": occurrence, "path": full_names,
                    "parent_occurrence": ancestors[-1]["id"] if ancestors else "",
                    "reason": "empty_compound", "geometry_status": "no_geometry"})
                log("EMPTY_PLACEHOLDER", '/'.join(occurrence), name)
                return
            local_shape = cq.Shape.cast(raw)
            world_shape = local_shape.moved(located)
            transform = located.wrapped.Transformation()
            identity_placement = all(transform.Value(i, j) == (1.0 if i == j else 0.0)
                                     for i in range(1, 4) for j in range(1, 5))
            try:
                bounds = world_shape.BoundingBox()
                # An identity placement leaves the exact same OCCT geometry
                # and location. Avoid its second expensive spline-extrema scan.
                local_bounds = bounds if identity_placement else local_shape.BoundingBox()
            except Exception as error:
                counts = {key: len(getattr(world_shape, key)())
                          for key in ("Solids", "Faces", "Edges", "Vertices")}
                raise ValueError(f"无法计算 STP 对象的有效包围盒：{name}；"
                                 f"实例 {'/'.join(occurrence)}；拓扑 {counts}。"
                                 "该对象未被跳过，请检查其导出几何。") from error
            solids = world_shape.Solids()
            volume = sum(abs(solid.Volume()) for solid in solids)
            area = abs(world_shape.Area())
            _finite([volume, area], "shape properties")
            index = len(rows)
            brep = staging / f"part_{index:05d}.brep"
            if not world_shape.exportBrep(str(brep)) or not brep.is_file():
                raise RuntimeError(f"Failed to export BREP: {identity}")
            properties = {
                "volume": round(volume, 6), "area": round(area, 6),
                "local_bbox_size": sorted(round(x, 6) for x in
                                          [local_bounds.xlen, local_bounds.ylen, local_bounds.zlen]),
                "solids": len(solids), "faces": len(world_shape.Faces()),
                "edges": len(world_shape.Edges()),
            }
            row = {
                "index": index, "id": identity, "name": name, "path": full_names,
                "occurrence_path": occurrence,
                "parent_occurrence": ancestors[-1]["id"] if ancestors else "",
                "assembly_ancestry": [dict(node) for node in ancestors],
                "bbox_min": _finite([bounds.xmin, bounds.ymin, bounds.zmin], "bbox minimum"),
                "bbox_max": _finite([bounds.xmax, bounds.ymax, bounds.zmax], "bbox maximum"),
                "bbox_size": _finite([bounds.xlen, bounds.ylen, bounds.zlen], "bbox size"),
                "solids": len(solids), "shells": len(world_shape.Shells()),
                "faces": len(world_shape.Faces()), "edges": len(world_shape.Edges()),
                "brep": brep.name, "brep_sha256": _sha256(brep),
                "volume_mm3": volume, "area_mm2": area,
                "geometry_fingerprint": hashlib.sha256(
                    json.dumps(properties, sort_keys=True).encode("utf-8")).hexdigest(),
                "world_transform": [[transform.Value(i, j) for j in range(1, 5)]
                                    for i in range(1, 4)] + [[0.0, 0.0, 0.0, 1.0]],
                "source_sha256": digest,
            }
            rows.append(row)
            log("PART", index, name)

        for number in range(1, roots.Length() + 1):
            walk(roots.Value(number), cq.Location(), [], [f"root:{number:04d}"], [])
        if not rows:
            raise ValueError("STEP contains no leaf shapes")
        # Reading a changing source must never publish a falsely attributed cache.
        if _sha256(source) != digest:
            raise ValueError("Source STEP changed during import; retry with a stable file")
        surface_count = sum(row["solids"] == 0 for row in rows)
        if surface_count:
            warnings.append(f"Preserved {surface_count} non-solid leaf occurrences; inspect auxiliary surfaces.")
        if any(row["solids"] > 1 for row in rows):
            warnings.append("Some leaf occurrences contain multiple solids; leaf identity is not physical part identity.")
        if non_geometric_occurrences:
            warnings.append(f"已记录 {len(non_geometric_occurrences)} 个没有实体、面、边或顶点的空结构占位；"
                            "它们不作为几何零件参与出图，有面、边和顶点的对象仍保留。")
        _write_json(staging / "assembly_parts.json", rows)
        complete = {
            "schema_version": 1, "source_step": source.name, "source_path": str(source),
            "source_sha256": digest, "parts": len(rows), "units": "mm",
            "solids": sum(row["solids"] for row in rows),
            "brep_paths_relative_to": "inventory", "seconds": time.monotonic() - started,
            "inventory_sha256": _sha256(staging / "assembly_parts.json"),
            "assembly_occurrences": assemblies, "warnings": warnings,
            "non_geometric_occurrences": non_geometric_occurrences,
            "identity_scope": "source SHA256 plus numeric root/component occurrence path",
        }
        _write_json(staging / "import_complete.json", complete)
        # Recheck immediately before publication so another caller cannot lose files.
        if out.exists():
            if not out.is_dir() or any(out.iterdir()):
                raise FileExistsError(f"Inventory became nonempty during import: {out}")
            out.rmdir()  # Only the verified empty target directory.
        staging.rename(out)
        log("COMPLETE", len(rows), "solids", complete["solids"])
        return complete
    finally:
        # Only this call's generated staging directory, never the target inventory.
        if staging.exists() and staging.resolve().parent == out.parent:
            shutil.rmtree(staging)


def _load_inventory(inventory: Path) -> tuple[list[dict], dict]:
    complete_path = inventory / "import_complete.json"
    rows_path = inventory / "assembly_parts.json"
    if not complete_path.is_file() or not rows_path.is_file():
        raise ValueError("Inventory is incomplete; import STEP into a fresh directory first")
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    if complete.get("units") != "mm" or complete.get("brep_paths_relative_to") != "inventory":
        raise ValueError("Expected a generic inventory with explicit millimetre units")
    if _sha256(rows_path) != complete.get("inventory_sha256"):
        raise ValueError("Inventory manifest differs from the completed import")
    rows = json.loads(rows_path.read_text(encoding="utf-8"))
    if not rows or len(rows) != complete.get("parts"):
        raise ValueError("Inventory part count differs from completed import")
    ids = set()
    for row in rows:
        identity = row["id"]
        if identity in ids or row.get("source_sha256") != complete.get("source_sha256"):
            raise ValueError("Duplicate occurrence identity or mismatched source in inventory")
        ids.add(identity)
        cached = (inventory / row["brep"]).resolve()
        if not cached.is_relative_to(inventory) or not cached.is_file():
            raise ValueError(f"Missing or unsafe BREP cache: {row['brep']}")
        if _sha256(cached) != row.get("brep_sha256"):
            raise ValueError(f"BREP cache differs from completed import: {row['brep']}")
        _finite(row["bbox_min"] + row["bbox_max"], "inventory bbox")
    return rows, complete


def _bbox_distance(a: dict, b: dict) -> float:
    gaps = [max(a["bbox_min"][axis] - b["bbox_max"][axis],
                b["bbox_min"][axis] - a["bbox_max"][axis], 0.0) for axis in range(3)]
    return math.sqrt(sum(gap * gap for gap in gaps))


def _branch_ids(rows: list[dict], digest: str) -> tuple[dict[int, str], int]:
    """Skip shared wrapper assemblies, then identify native numeric branches."""
    common = list(rows[0]["occurrence_path"])
    for row in rows[1:]:
        path = row["occurrence_path"]
        length = 0
        while length < min(len(common), len(path)) and common[length] == path[length]:
            length += 1
        common = common[:length]
    depth = len(common) + 1
    return {row["index"]: _occurrence_id(digest, row["occurrence_path"][:depth])
            for row in rows}, depth


def _select_pairs(candidates: list[tuple], branches: dict[int, str],
                  max_pairs: int) -> tuple[list[tuple], list[dict]]:
    """Round-robin native branch pairs before consuming internal-contact budget."""
    buckets: dict[tuple[str, str], deque] = defaultdict(deque)
    for candidate in candidates:
        _, _, a, b = candidate
        key = tuple(sorted((branches[a], branches[b])))
        buckets[key].append(candidate)
    # First examine each available cross-branch relation. Within each bucket,
    # keep bbox-gap/face-count-product/index ordering so cheap candidates lead.
    cross = sorted((key for key in buckets if key[0] != key[1]),
                   key=lambda key: (buckets[key][0][:2], key))
    internal = sorted((key for key in buckets if key[0] == key[1]),
                      key=lambda key: (buckets[key][0][:2], key))
    selected: list[tuple] = []
    tested = defaultdict(int)
    for keys in (cross, internal):
        active = deque(keys)
        while active and len(selected) < max_pairs:
            key = active.popleft()
            selected.append(buckets[key].popleft())
            tested[key] += 1
            if buckets[key]:
                active.append(key)
    coverage = [{"branch_ids": list(key),
                 "candidate_pairs": len(buckets[key]) + tested[key],
                 "tested_pairs": tested[key],
                 "scope": "cross_branch" if key[0] != key[1] else "within_branch"}
                for key in cross + internal]
    return selected, coverage


class _ContactWorker:
    """One owned native subprocess; a stalled computation cannot trap the caller."""

    def __init__(self, timeout_seconds: float):
        self.timeout = timeout_seconds
        self.process: subprocess.Popen | None = None
        self.messages: queue.Queue | None = None
        self.reader: threading.Thread | None = None

    @staticmethod
    def _read_lines(stream: Any, messages: queue.Queue) -> None:
        try:
            for line in stream:
                messages.put(line)
        except (OSError, ValueError) as error:
            messages.put(error)
        finally:
            messages.put(None)

    def _receive(self, timeout: float, stage: str) -> dict:
        assert self.messages is not None
        try:
            line = self.messages.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError(f"Native contact worker {stage} timed out after {timeout:g} seconds") from error
        if line is None or isinstance(line, Exception):
            code = self.process.poll() if self.process else None
            raise RuntimeError(f"Native contact worker stopped unexpectedly (exit={code})")
        try:
            response = json.loads(line)
        except (ValueError, TypeError) as error:
            raise RuntimeError("Invalid JSON from native contact worker") from error
        if not isinstance(response, dict):
            raise RuntimeError("Invalid native contact worker response")
        return response

    def _start(self) -> None:
        if self.process is not None:
            return
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--contact-worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert self.process.stdout is not None
        self.reader = threading.Thread(target=self._read_lines,
                                       args=(self.process.stdout, self.messages), daemon=True)
        self.reader.start()
        # Startup is separately bounded; the actual pair watchdog starts only
        # once OCP is loaded. The worker avoids CadQuery/VTK import entirely.
        ready = self._receive(max(5.0, min(self.timeout, 30.0)), "startup")
        if ready != {"ready": True}:
            raise RuntimeError("Native contact worker did not initialize")

    def measure(self, path_a: Path, path_b: Path) -> dict:
        try:
            self._start()
            assert self.process is not None and self.process.stdin is not None
            self.process.stdin.write(json.dumps({"path_a": str(path_a), "path_b": str(path_b)}) + "\n")
            self.process.stdin.flush()
            response = self._receive(self.timeout, "pair measurement")
            if "error" in response:
                raise ValueError(response["error"])
            if set(response) != {"distance_mm", "point_a", "point_b", "solution_count", "inner_solution"}:
                raise RuntimeError("Incomplete native contact measurement")
            _finite([response["distance_mm"]] + response["point_a"] + response["point_b"], "native contact result")
            return response
        except BaseException:
            # This also closes an owned child on cancellation or broken pipes.
            self.close()
            raise

    def close(self) -> None:
        process, reader = self.process, self.reader
        self.process, self.reader, self.messages = None, None, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if reader is not None:
            reader.join(timeout=1)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _run_contact_worker() -> None:
    """Private JSON-lines protocol: read verified BREP paths and measure only."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    shapes = {}

    def load(path_text: str) -> Any:
        path = Path(path_text).resolve()
        if path.suffix.lower() != ".brep" or not path.is_file():
            raise ValueError("Expected an existing BREP cache file")
        key = str(path)
        if key not in shapes:
            shape = TopoDS_Shape()
            BRepTools.Read_s(shape, key, BRep_Builder())
            if shape.IsNull():
                raise ValueError("Could not read BREP cache")
            shapes[key] = shape
        return shapes[key]

    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"path_a", "path_b"}:
                raise ValueError("Only a BREP path pair is accepted")
            distance = BRepExtrema_DistShapeShape(load(request["path_a"]), load(request["path_b"]))
            if not distance.IsDone() or distance.NbSolution() < 1:
                raise ValueError("No minimum-distance solution")
            point_a, point_b = distance.PointOnShape1(1), distance.PointOnShape2(1)
            response = {
                "distance_mm": distance.Value(),
                "point_a": [point_a.X(), point_a.Y(), point_a.Z()],
                "point_b": [point_b.X(), point_b.Y(), point_b.Z()],
                "solution_count": distance.NbSolution(), "inner_solution": distance.InnerSolution(),
            }
        except Exception as error:
            response = {"error": str(error)}
        print(json.dumps(response, allow_nan=False), flush=True)


def analyze_inventory(inventory: Path, tolerance_mm: float = 1.0, *,
                      max_pairs: int = 1000, pair_timeout_seconds: float = 10.0) -> dict:
    """Write analysis.json with measured, unconfirmed proximity candidates.

    An AABB broad phase rejects clearly distant pairs. The configurable pair
    budget limits exact BREP distance computations, not the validity of untested
    pairs. Untested and failed pairs are explicitly reported; neither is a
    negative connection finding. Native cache reading/distance is run in an
    owned persistent worker with a per-pair watchdog; timed-out workers are
    terminated and restarted. Tested counts include attempts that failed, with
    successful and timed-out counts reported separately. A contact can be an auxiliary/duplicate surface
    and requires human interpretation, even if its measured gap is zero.
    """
    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("tolerance_mm must be finite and nonnegative")
    if isinstance(max_pairs, bool) or not isinstance(max_pairs, int) or max_pairs < 0:
        raise ValueError("max_pairs must be a nonnegative integer")
    if not math.isfinite(pair_timeout_seconds) or pair_timeout_seconds <= 0:
        raise ValueError("pair_timeout_seconds must be finite and positive")
    started = time.monotonic()
    inventory = Path(inventory).resolve()
    rows, complete = _load_inventory(inventory)
    warnings = list(complete.get("warnings", []))
    warnings.append("Measured proximity is only a connection candidate, not a verified joint or installation direction.")
    candidates = []
    for position, a in enumerate(rows):
        for b in rows[position + 1:]:
            gap = _bbox_distance(a, b)
            if gap <= tolerance_mm:
                complexity = max(1, a.get("faces", 1)) * max(1, b.get("faces", 1))
                candidates.append((gap, complexity, a["index"], b["index"]))
    candidates.sort()
    by_index = {row["index"]: row for row in rows}
    branches, branch_depth = _branch_ids(rows, complete["source_sha256"])
    selected, branch_coverage = _select_pairs(candidates, branches, max_pairs)
    selected_ids = {(a, b) for _, _, a, b in selected}
    untested = [{"part_ids": [by_index[a]["id"], by_index[b]["id"]],
                 "bbox_distance_mm": gap, "status": "untested"}
                for gap, _, a, b in candidates if (a, b) not in selected_ids]
    if untested:
        warnings.append(f"Pair budget tested {len(selected)} of {len(candidates)} broad-phase candidates; {len(untested)} remain untested.")
    contacts, failed = [], []
    completed_count = 0
    timed_out_count = 0
    completed_by_branch = defaultdict(int)
    failed_by_branch = defaultdict(int)
    timeout_by_branch = defaultdict(int)
    if selected:
        worker = _ContactWorker(pair_timeout_seconds)
        try:
            for number, (_, _, index_a, index_b) in enumerate(selected, 1):
                a, b = by_index[index_a], by_index[index_b]
                pair_ids = [a["id"], b["id"]]
                branch_key = tuple(sorted((branches[index_a], branches[index_b])))
                pair_started = time.monotonic()
                try:
                    measurement = worker.measure(inventory / a["brep"], inventory / b["brep"])
                    completed_count += 1
                    completed_by_branch[branch_key] += 1
                    if measurement["distance_mm"] <= tolerance_mm:
                        contacts.append({
                            "part_ids": pair_ids, **measurement, "status": "candidate",
                            "method": "OCP minimum BREP distance",
                            "contains_non_solid": a["solids"] == 0 or b["solids"] == 0,
                            "elapsed_seconds": round(time.monotonic() - pair_started, 3),
                        })
                except Exception as error:
                    timed_out = isinstance(error, TimeoutError)
                    timed_out_count += timed_out
                    failed_by_branch[branch_key] += 1
                    timeout_by_branch[branch_key] += timed_out
                    failed.append({"part_ids": pair_ids, "status": "failed", "reason": str(error),
                                   "failure_type": "timeout" if timed_out else "native_error",
                                   "elapsed_seconds": round(time.monotonic() - pair_started, 3)})
                if number == 1 or number % 5 == 0 or number == len(selected):
                    print("CONTACT_PROGRESS", number, len(selected), "timeouts", timed_out_count, flush=True)
        finally:
            worker.close()
    if failed:
        warnings.append(f"Exact distance failed for {len(failed)} pairs; these were not classified as disconnected.")
    if timed_out_count:
        warnings.append(f"{timed_out_count} native distance attempts exceeded the watchdog; pair measurements are bounded at {pair_timeout_seconds:g} seconds each.")
    for entry in branch_coverage:
        key = tuple(entry["branch_ids"])
        entry.update(completed_pairs=completed_by_branch[key], failed_pairs=failed_by_branch[key],
                     timed_out_pairs=timeout_by_branch[key])
    result = {
        "schema_version": 1, "source_sha256": complete["source_sha256"], "units": "mm",
        "parts": rows, "contacts": contacts, "warnings": warnings,
        "assembly_occurrences": complete.get("assembly_occurrences", []),
        "non_geometric_occurrences": complete.get("non_geometric_occurrences", []),
        "tolerance_mm": tolerance_mm, "max_pairs": max_pairs,
        "pair_timeout_seconds": pair_timeout_seconds,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "candidate_pair_count": len(candidates), "tested_pair_count": len(selected),
        "completed_pair_count": completed_count, "timed_out_pair_count": timed_out_count,
        "untested_candidates": untested, "failed_pairs": failed,
        "branch_pair_coverage": branch_coverage,
        "native_branch_depth": branch_depth,
        "pair_selection": "native cross-branch pairs first; round-robin buckets ordered by bbox gap then face-count product",
        "identity_scope": complete.get("identity_scope"),
    }
    _write_json(inventory / "analysis.json", result)
    return result


def main() -> None:
    if sys.argv[1:] == ["--contact-worker"]:
        _run_contact_worker()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="Import into a fresh inventory")
    importer.add_argument("step", type=Path)
    importer.add_argument("inventory", type=Path)
    analyzer = commands.add_parser("analyze", help="Measure unconfirmed connection candidates")
    analyzer.add_argument("inventory", type=Path)
    analyzer.add_argument("--tolerance-mm", type=float, default=1.0)
    analyzer.add_argument("--max-pairs", type=int, default=1000)
    analyzer.add_argument("--pair-timeout", type=float, default=10.0)
    args = parser.parse_args()
    if args.command == "import":
        import_model(args.step, args.inventory)
    else:
        result = analyze_inventory(args.inventory, args.tolerance_mm, max_pairs=args.max_pairs,
                                   pair_timeout_seconds=args.pair_timeout)
        print("CANDIDATE_CONTACTS", len(result["contacts"]), "UNTESTED", len(result["untested_candidates"]))


if __name__ == "__main__":
    main()
