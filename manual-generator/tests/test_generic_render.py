"""Geometry-based renderer checks, including occlusion and draft provenance."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from generic_render import plan_digest, render_plan

try:
    import cadquery as cq
    import fitz
    HAVE_CAD = True
except ImportError:
    HAVE_CAD = False


SOURCE_HASH = hashlib.sha256(b"synthetic STEP geometry fixture").hexdigest()


def sample_plan() -> dict:
    return {
        "schema_version": 1,
        "source": {"sha256": SOURCE_HASH},
        "product": {"title": "Synthetic occlusion fixture"},
        "status": "draft",
        "groups": [
            {"id": "front", "label": "Front", "part_ids": ["front-instance"]},
            {"id": "rear", "label": "Rear", "part_ids": ["rear-instance"]},
        ],
        "style": {"camera": [0, 0, 1], "up": [0, 1, 0], "width": 600, "height": 480, "margin": 45, "hlr": "exact"},
        "steps": [
            {"id": "first", "title": "Front", "instruction": "Inspect.", "assembled_groups": [], "moving_groups": ["front"], "offsets": {}, "arrows": [], "focus_part_ids": [], "warnings": [], "evidence": [], "reviewed": False},
            {"id": "covered", "title": "Both", "instruction": "Confirm the proposed placement.", "assembled_groups": ["front"], "moving_groups": ["rear"], "offsets": {}, "arrows": [{"from": [0, 0, -10], "to": [0, 0, 10], "meaning": "reposition", "status": "proposed"}], "focus_part_ids": ["front-instance"], "warnings": ["Order remains proposed."], "evidence": [], "reviewed": False},
        ],
    }


class DigestTests(unittest.TestCase):
    def test_review_metadata_does_not_require_reprojection(self):
        original = sample_plan()
        reviewed = copy.deepcopy(original)
        reviewed.update(status="confirmed", revision=7, confirmation={"user": "packaging"})
        reviewed["steps"][0].update(reviewed=True, status="confirmed", confirmed_at="2026-10-08")
        self.assertEqual(plan_digest(original), plan_digest(reviewed))
        reviewed["steps"][0]["instruction"] = "A meaningful instruction edit."
        self.assertNotEqual(plan_digest(original), plan_digest(reviewed))

    def test_arrow_status_and_offsets_are_illustrated_content(self):
        original = sample_plan()
        edited = copy.deepcopy(original)
        edited["steps"][1]["arrows"][0]["status"] = "confirmed"
        self.assertNotEqual(plan_digest(original), plan_digest(edited))
        edited = copy.deepcopy(original)
        edited["steps"][1]["offsets"] = {"rear": [25, 0, 0]}
        self.assertNotEqual(plan_digest(original), plan_digest(edited))

    def test_temporary_visibility_and_its_reason_are_illustrated_content(self):
        original = sample_plan()
        hidden = copy.deepcopy(original)
        hidden["steps"][1].update(hidden_part_ids=["front-instance"], visibility_reason="Expose the connection.")
        self.assertNotEqual(plan_digest(original), plan_digest(hidden))
        changed_reason = copy.deepcopy(hidden)
        changed_reason["steps"][1]["visibility_reason"] = "Expose the fastener location."
        self.assertNotEqual(plan_digest(hidden), plan_digest(changed_reason))

    def test_key_order_is_canonical_and_nonfinite_is_rejected(self):
        original = sample_plan()
        self.assertEqual(plan_digest(original), plan_digest(dict(reversed(list(original.items())))))
        original["style"]["margin"] = float("nan")
        with self.assertRaises(ValueError):
            plan_digest(original)

    def test_browser_integral_number_roundtrip_has_the_same_digest(self):
        original = sample_plan()
        original["style"].update(camera=[0.0, -0.0, 1.0], width=600.0)
        original["groups"][0]["geometry_evidence"] = {"volume": 20.0, "samples": [0.0, -0.0, 1.0], "flag": True}
        original["steps"][1]["offsets"] = {"rear": [30.0, -0.0, 1.0]}
        original["steps"][1]["evidence"] = [{"point": [1.0, 0.0, -0.0], "distance": 0.5}]
        browser = copy.deepcopy(original)
        browser["style"].update(camera=[0, 0, 1], width=600)
        browser["groups"][0]["geometry_evidence"] = {"volume": 20, "samples": [0, 0, 1], "flag": True}
        browser["steps"][1]["offsets"] = {"rear": [30, 0, 1]}
        browser["steps"][1]["evidence"] = [{"point": [1, 0, 0], "distance": 0.5}]
        self.assertEqual(plan_digest(original), plan_digest(browser))
        browser["steps"][1]["offsets"]["rear"][0] = 30.125
        self.assertNotEqual(plan_digest(original), plan_digest(browser))

    def test_numeric_normalization_preserves_booleans_and_rejects_nonfinite(self):
        original = sample_plan()
        original["groups"][0]["metadata"] = {"flag": True, "position": 0.125}
        changed = copy.deepcopy(original)
        changed["groups"][0]["metadata"]["flag"] = 1
        self.assertNotEqual(plan_digest(original), plan_digest(changed))
        changed = copy.deepcopy(original)
        changed["groups"][0]["metadata"]["position"] = 0.126
        self.assertNotEqual(plan_digest(original), plan_digest(changed))
        for nonfinite in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=nonfinite):
                changed["steps"][0]["evidence"] = [{"point": [0, nonfinite, 1]}]
                with self.assertRaises(ValueError):
                    plan_digest(changed)


@unittest.skipUnless(HAVE_CAD, "CadQuery and PyMuPDF are required for geometry checks")
class RenderGeometryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="generic-render-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inventory, self.output = self.root / "inventory", self.root / "diagrams"
        self.inventory.mkdir()
        # Small rear box is entirely behind the larger front box in this view.
        self.shapes = {
            "front-instance": cq.Workplane("XY").box(20, 20, 2).val().translate((0, 0, 5)),
            "rear-instance": cq.Workplane("XY").box(8, 8, 2).val().translate((0, 0, -5)),
        }
        self.rows = []
        for index, (pid, shape) in enumerate(self.shapes.items()):
            filename = f"part_{index}.brep"
            shape.exportBrep(str(self.inventory / filename))
            bounds = shape.BoundingBox()
            self.rows.append({
                "id": pid, "index": index, "name": "Repeated name", "path": ["Fixture", "Repeated name"],
                "bbox_min": [bounds.xmin, bounds.ymin, bounds.zmin], "bbox_max": [bounds.xmax, bounds.ymax, bounds.zmax],
                "bbox_size": [bounds.xlen, bounds.ylen, bounds.zlen], "solids": len(shape.Solids()), "brep": filename,
            })
        self.write_inventory()
        self.plan = sample_plan()

    def write_inventory(self, source_hash=SOURCE_HASH):
        (self.inventory / "assembly_parts.json").write_text(json.dumps(self.rows), encoding="utf-8")
        (self.inventory / "import_complete.json").write_text(json.dumps({"source_sha256": source_hash, "parts": len(self.rows), "brep_paths_relative_to": "inventory"}), encoding="utf-8")

    def test_shared_hlr_hides_rear_geometry_and_zero_projected_arrow_is_safe(self):
        manifest = render_plan(self.plan, self.inventory, self.output)
        self.assertTrue(manifest["complete"])
        first, covered = manifest["entries"]
        # Rendering individually then overlaying would leave the rear box visible.
        self.assertEqual(first["projection"]["visible_paths"], covered["projection"]["visible_paths"])
        self.assertEqual(covered["part_ids"], ["front-instance", "rear-instance"])
        self.assertEqual(len(covered["skipped_arrows"]), 1)
        self.assertNotIn("<polygon", (self.output / "covered.svg").read_text(encoding="utf-8"))
        self.assertEqual(covered["focus"]["part_ids"], ["front-instance"])
        self.assertTrue((self.output / "covered_focus.svg").is_file())
        self.assertEqual(manifest["plan_sha256"], plan_digest(self.plan))
        preview = fitz.Pixmap(str(self.output / "covered.png"))
        self.assertEqual((preview.width, preview.height), (600, 480))

    def test_partial_render_tracks_only_current_assets_and_detects_tampering(self):
        first = render_plan(self.plan, self.inventory, self.output, ["first"])
        self.assertFalse(first["complete"])
        self.assertEqual(first["remaining_step_ids"], ["covered"])
        completed = render_plan(self.plan, self.inventory, self.output, ["covered"])
        self.assertTrue(completed["complete"])
        (self.output / "first.svg").write_text("modified outside the renderer", encoding="utf-8")
        rerender = render_plan(self.plan, self.inventory, self.output, ["covered"])
        self.assertFalse(rerender["complete"])
        self.assertEqual(rerender["rendered_step_ids"], ["covered"])
        self.assertEqual(rerender["remaining_step_ids"], ["first"])

    def test_surface_parts_are_not_filtered_as_nonsolids(self):
        wire = cq.Workplane("XY").rect(12, 7).val()
        surface = cq.Face.makeFromWires(wire)
        surface.exportBrep(str(self.inventory / "surface.brep"))
        self.rows.append({"id": "surface-instance", "index": 2, "name": "Actual surface", "path": ["Fixture", "Surface"], "solids": 0, "brep": "surface.brep"})
        self.write_inventory()
        self.plan["groups"].append({"id": "surface", "label": "Surface", "part_ids": ["surface-instance"]})
        self.plan["steps"] = [{"id": "surface_only", "assembled_groups": ["surface"], "moving_groups": [], "arrows": [], "focus_part_ids": []}]
        result = render_plan(self.plan, self.inventory, self.output)
        self.assertEqual(result["entries"][0]["part_ids"], ["surface-instance"])
        self.assertGreater(result["entries"][0]["projection"]["visible_paths"], 0)

    def test_step_hiding_exposes_rear_geometry_then_restores_source_surface(self):
        # Import a real STEP fixture, including a continuous surface with no
        # solids. Visibility must never turn that surface into a wire or mask.
        source = self.root / "surface-and-rear.step"
        face = cq.Face.makeFromWires(cq.Workplane("XY").rect(20, 20).val()).translate((0, 0, 5))
        cq.exporters.export(cq.Compound.makeCompound([face, self.shapes["rear-instance"]]), str(source))
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        imported = list(cq.importers.importStep(str(source)).val())
        self.shapes = {
            "front-instance" if shape.Center().z > 0 else "rear-instance": shape
            for shape in imported
        }
        self.assertEqual(set(self.shapes), {"front-instance", "rear-instance"})
        self.assertEqual(len(self.shapes["front-instance"].Solids()), 0)
        for row in self.rows:
            self.shapes[row["id"]].exportBrep(str(self.inventory / row["brep"]))
            row["solids"] = len(self.shapes[row["id"]].Solids())
            row["source_sha256"] = source_hash
            row["brep_sha256"] = hashlib.sha256((self.inventory / row["brep"]).read_bytes()).hexdigest()
        self.write_inventory(source_hash)
        self.plan["source"]["sha256"] = source_hash
        self.plan["groups"] = [{"id": "assembly", "part_ids": ["front-instance", "rear-instance"]}]
        self.plan["steps"] = [
            {"id": "covered", "assembled_groups": ["assembly"], "moving_groups": [], "hidden_part_ids": []},
            {"id": "connection", "assembled_groups": ["assembly"], "moving_groups": [],
             "hidden_part_ids": ["front-instance"], "visibility_reason": "Temporarily hide the surface to show the internal connection."},
            {"id": "restored", "assembled_groups": ["assembly"], "moving_groups": [], "hidden_part_ids": []},
        ]
        files = [source, *self.inventory.iterdir()]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        for method in ("exact", "poly"):
            with self.subTest(hlr=method):
                self.plan["style"]["hlr"] = method
                manifest = render_plan(self.plan, self.inventory, self.root / method)
                covered, exposed, restored = manifest["entries"]
                self.assertEqual(covered["part_ids"], ["front-instance", "rear-instance"])
                self.assertEqual(exposed["part_ids"], ["rear-instance"])
                self.assertEqual(exposed["planned_part_ids"], covered["part_ids"])
                self.assertEqual(exposed["hidden_part_ids"], ["front-instance"])
                self.assertEqual(exposed["visibility_reason"], self.plan["steps"][1]["visibility_reason"])
                self.assertEqual(exposed["excluded_part_ids"], [])
                self.assertEqual(manifest["excluded_part_ids"], [])
                self.assertEqual(covered["hidden_part_ids"], [])
                self.assertEqual(restored["hidden_part_ids"], [])
                self.assertEqual(covered["projection"], restored["projection"])
                # Once the sheet is omitted the smaller rear object fills the
                # viewport. The displayed edges now come from its actual BREP.
                self.assertGreater(exposed["projection"]["scale"], covered["projection"]["scale"] * 2)
                self.assertEqual(covered["projection"]["visible_paths"], 4)
                self.assertEqual(exposed["projection"]["visible_paths"], 4)
                self.assertNotEqual(covered["svg_sha256"], exposed["svg_sha256"])
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files})

    def test_actual_surface_holes_and_outlines_do_not_invent_opaque_masks(self):
        from cad_pipeline import hidden_line_svg

        outer = cq.Workplane("XY").rect(20, 20).val()
        hole = cq.Workplane("XY").rect(12, 12).val()
        variants = {
            "continuous": cq.Face.makeFromWires(outer).translate((0, 0, 5)),
            "perforated": cq.Face.makeFromWires(outer, [hole]).translate((0, 0, 5)),
            "outline": outer.translate((0, 0, 5)),
        }
        options = {"camera": [0, 0, 1], "up": [0, 1, 0]}

        def paths(svg):
            return {element.attrib["d"] for element in ET.fromstring(svg).iter("{http://www.w3.org/2000/svg}path")}

        rear_svg, _ = hidden_line_svg(self.shapes["rear-instance"], **options)
        rear_paths = paths(rear_svg)
        self.assertEqual(len(rear_paths), 4)
        for label, front in variants.items():
            with self.subTest(geometry=label):
                svg, projection = hidden_line_svg(cq.Compound.makeCompound([front, self.shapes["rear-instance"]]), **options)
                visible = paths(svg)
                if label == "continuous":
                    self.assertFalse(rear_paths & visible)
                    self.assertEqual(projection["visible_paths"], 4)
                else:
                    # Each rear-object edge is visible through the real hole,
                    # and also through an outline with no covering face.
                    self.assertTrue(rear_paths <= visible)
                    self.assertGreater(projection["visible_paths"], 4)
                self.assertNotIn("<mask", svg)
                self.assertNotIn("opacity", svg)

    def test_polygonal_default_renders_actual_instances_and_focus(self):
        self.plan["style"]["hlr"] = "poly"
        result = render_plan(self.plan, self.inventory, self.output)
        self.assertTrue(result["complete"])
        entry = result["entries"][1]
        self.assertIn("polygonal HLR", entry["render_method"])
        self.assertEqual(entry["part_ids"], ["front-instance", "rear-instance"])
        self.assertTrue((self.output / entry["focus"]["svg_path"]).is_file())

    def test_offsets_and_projected_reposition_arrow_are_applied(self):
        self.plan["steps"] = [self.plan["steps"][1]]
        self.plan["steps"][0]["offsets"] = {"rear": [30, 0, 0]}
        self.plan["steps"][0]["arrows"] = [{"from": [30, 0, -5], "to": [0, 0, -5], "meaning": "reposition", "status": "proposed"}]
        entry = render_plan(self.plan, self.inventory, self.output)["entries"][0]
        self.assertEqual(entry["transforms"]["rear-instance"], [30, 0, 0])
        self.assertEqual(entry["skipped_arrows"], [])
        svg = (self.output / "covered.svg").read_text(encoding="utf-8")
        self.assertIn('<polygon points="', svg)
        self.assertIn('stroke-dasharray="7 4"', svg)
        self.assertIn("not a fastener direction", svg)

    def test_changed_source_or_plan_never_reuses_an_old_manifest(self):
        render_plan(self.plan, self.inventory, self.output, ["first"])
        changed = copy.deepcopy(self.plan)
        changed["steps"][0]["title"] = "Updated content"
        with self.assertRaisesRegex(ValueError, "fresh directory"):
            render_plan(changed, self.inventory, self.output)
        changed["source"]["sha256"] = "0" * 64
        empty_output = self.root / "mismatched-source"
        with self.assertRaisesRegex(ValueError, "does not match"):
            render_plan(changed, self.inventory, empty_output)
        self.assertFalse(empty_output.exists())

    def test_source_geometry_changes_invalidate_existing_manifest(self):
        render_plan(self.plan, self.inventory, self.output, ["first"])
        self.shapes["rear-instance"].translate((2, 0, 0)).exportBrep(str(self.inventory / "part_1.brep"))
        with self.assertRaisesRegex(ValueError, "BREP inventory"):
            render_plan(self.plan, self.inventory, self.output, ["covered"])

    def test_completed_import_brep_hash_is_checked_even_for_fresh_output(self):
        self.rows[0]["brep_sha256"] = "0" * 64
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "completed STEP import"):
            render_plan(self.plan, self.inventory, self.output)
        self.assertFalse(self.output.exists())

    def test_explicit_exclusion_omits_only_named_geometry_and_records_provenance(self):
        # Put the rear instance alongside the front so hiding cannot mimic exclusion.
        self.shapes["rear-instance"].translate((30, 0, 0)).exportBrep(str(self.inventory / "part_1.brep"))
        self.plan["groups"] = [{"id": "pair", "label": "Both", "part_ids": ["front-instance", "rear-instance"]}]
        self.plan["steps"] = [{"id": "pair", "assembled_groups": [], "moving_groups": ["pair"], "arrows": [], "focus_part_ids": []}]
        full = render_plan(self.plan, self.inventory, self.root / "all-parts")
        digest_before = plan_digest(self.plan)
        self.plan["excluded_part_ids"] = ["rear-instance"]
        self.plan["exclusion_reason"] = "User explicitly designated this source instance as an auxiliary CAD shape."
        result = render_plan(self.plan, self.inventory, self.output)
        entry = result["entries"][0]
        self.assertTrue(result["complete"])
        self.assertEqual(entry["part_ids"], ["front-instance"])
        self.assertEqual(entry["planned_part_ids"], ["front-instance", "rear-instance"])
        self.assertEqual(entry["excluded_part_ids"], ["rear-instance"])
        self.assertEqual(result["excluded_part_ids"], ["rear-instance"])
        self.assertEqual(result["exclusion_reason"], self.plan["exclusion_reason"])
        self.assertEqual(result["source_sha256"], SOURCE_HASH)
        self.assertNotEqual(result["plan_sha256"], digest_before)
        self.assertLess(entry["projection"]["visible_paths"], full["entries"][0]["projection"]["visible_paths"])
        self.assertGreater(entry["projection"]["scale"], full["entries"][0]["projection"]["scale"])
        self.assertTrue((self.inventory / "part_1.brep").is_file())

    def test_exclusions_require_known_distinct_ids_and_a_nonempty_reason(self):
        cases = [
            (["unknown-instance"], "Explicit instruction", "distinct known"),
            (["rear-instance", "rear-instance"], "Explicit instruction", "distinct known"),
            ("rear-instance", "Explicit instruction", "distinct known"),
            (["rear-instance"], "", "nonempty exclusion_reason"),
            (["rear-instance"], "   ", "nonempty exclusion_reason"),
            (["rear-instance"], None, "nonempty exclusion_reason"),
        ]
        for exclusions, reason, message in cases:
            with self.subTest(exclusions=exclusions, reason=reason):
                candidate = copy.deepcopy(self.plan)
                candidate["excluded_part_ids"] = exclusions
                candidate["exclusion_reason"] = reason
                with self.assertRaisesRegex(ValueError, message):
                    render_plan(candidate, self.inventory, self.output)
                self.assertFalse(self.output.exists())

    def test_hidden_parts_require_distinct_source_ids_current_scene_and_reason(self):
        cases = [
            ("first", ["unknown-instance"], "Show a connection", "distinct known"),
            ("first", ["front-instance", "front-instance"], "Show a connection", "distinct known"),
            ("first", "front-instance", "Show a connection", "distinct known"),
            ("first", [True], "Show a connection", "distinct known"),
            ("first", ["rear-instance"], "Show a connection", "displayed assembled/moving"),
            ("covered", ["front-instance"], "", "nonempty visibility_reason"),
            ("covered", ["front-instance"], "   ", "nonempty visibility_reason"),
            ("covered", ["front-instance"], None, "nonempty visibility_reason"),
            ("covered", ["front-instance"], 7, "nonempty visibility_reason"),
        ]
        for sid, hidden_ids, reason, message in cases:
            with self.subTest(hidden_ids=hidden_ids, reason=reason):
                candidate = copy.deepcopy(self.plan)
                step = next(step for step in candidate["steps"] if step["id"] == sid)
                step.update(hidden_part_ids=hidden_ids, visibility_reason=reason)
                with self.assertRaisesRegex(ValueError, message):
                    render_plan(candidate, self.inventory, self.output)
                self.assertFalse(self.output.exists())

    def test_temporary_hiding_cannot_remove_scene_moving_group_or_focus(self):
        cases = [
            ("first", ["front-instance"], "Step first has no retained geometry"),
            ("covered", ["rear-instance"], "moving group rear has no retained geometry"),
            ("covered", ["front-instance"], "focus parts.*visible"),
        ]
        for sid, hidden_ids, message in cases:
            with self.subTest(scene=sid, hidden=hidden_ids):
                candidate = copy.deepcopy(self.plan)
                step = next(step for step in candidate["steps"] if step["id"] == sid)
                step.update(hidden_part_ids=hidden_ids, visibility_reason="Expose the connection.")
                with self.assertRaisesRegex(ValueError, message):
                    render_plan(candidate, self.inventory, self.output)
                self.assertFalse(self.output.exists())

    def test_hidden_parts_are_distinct_from_global_auxiliary_exclusions(self):
        self.plan["steps"] = [self.plan["steps"][1]]
        self.plan["steps"][0].update(hidden_part_ids=["rear-instance"], visibility_reason="Expose a connection.")
        self.plan.update(excluded_part_ids=["rear-instance"], exclusion_reason="Reviewed auxiliary geometry.")
        with self.assertRaisesRegex(ValueError, "must not overlap globally excluded_part_ids"):
            render_plan(self.plan, self.inventory, self.output)
        self.assertFalse(self.output.exists())

    def test_empty_steps_moving_groups_and_excluded_focus_are_rejected(self):
        candidate = copy.deepcopy(self.plan)
        candidate["excluded_part_ids"] = ["front-instance"]
        candidate["exclusion_reason"] = "Explicit user instruction."
        with self.assertRaisesRegex(ValueError, "Step first has no retained geometry"):
            render_plan(candidate, self.inventory, self.output)
        candidate["excluded_part_ids"] = ["rear-instance"]
        with self.assertRaisesRegex(ValueError, "moving group rear has no retained geometry"):
            render_plan(candidate, self.inventory, self.output)
        candidate["groups"] = [{"id": "pair", "label": "Both", "part_ids": ["front-instance", "rear-instance"]}]
        candidate["steps"] = [{"id": "pair", "assembled_groups": [], "moving_groups": ["pair"], "arrows": [], "focus_part_ids": ["rear-instance"]}]
        with self.assertRaisesRegex(ValueError, "focus parts.*visible"):
            render_plan(candidate, self.inventory, self.output)
        self.assertFalse(self.output.exists())

    def test_explicit_exclusions_do_not_remove_source_group_membership(self):
        self.plan["excluded_part_ids"] = ["rear-instance"]
        self.plan["exclusion_reason"] = "Explicit user instruction."
        self.plan["groups"] = [self.plan["groups"][0]]
        self.plan["steps"] = [self.plan["steps"][0]]
        with self.assertRaisesRegex(ValueError, "cover every source instance"):
            render_plan(self.plan, self.inventory, self.output)
        self.assertFalse(self.output.exists())

    def test_filename_and_brep_path_escape_are_rejected_before_writing(self):
        self.plan["steps"][0]["id"] = "../outside"
        with self.assertRaisesRegex(ValueError, "Invalid step ID"):
            render_plan(self.plan, self.inventory, self.output)
        self.assertFalse(self.output.exists())
        self.plan = sample_plan()
        self.rows[0]["brep"] = "../outside.brep"
        self.write_inventory()
        with self.assertRaisesRegex(ValueError, "inside the inventory"):
            render_plan(self.plan, self.inventory, self.output)
        self.assertFalse(self.output.exists())

    def test_camera_and_asset_filename_collisions_are_rejected(self):
        self.plan["style"]["up"] = [0, 0, 2]
        with self.assertRaisesRegex(ValueError, "parallel"):
            render_plan(self.plan, self.inventory, self.output)
        self.plan = sample_plan()
        self.plan["steps"][1]["id"] = "first_focus"
        with self.assertRaisesRegex(ValueError, "collide"):
            render_plan(self.plan, self.inventory, self.output)


if __name__ == "__main__":
    unittest.main()
