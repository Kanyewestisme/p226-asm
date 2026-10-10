"""Source-bound exploded cover geometry and separation from installation steps."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cover_render import cover_plan, render_cover
from generic_render import plan_digest

CAD_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("cadquery", "fitz"))
SOURCE_HASH = hashlib.sha256(b"Actual-geometry cover fixture").hexdigest()


def fixture_plan():
    groups = [{"id": "base", "label": "Base", "part_ids": ["base-part"]},
              {"id": "cap", "label": "Cap", "part_ids": ["cap-part", "surface-part"]}]
    def step(sid, assembled, moving):
        return {"id": sid, "title": sid, "instruction": "Inspect model geometry.",
                "assembled_groups": assembled, "moving_groups": moving, "offsets": {},
                "arrows": [], "focus_part_ids": [], "evidence": [], "warnings": [], "reviewed": False}
    return {
        "schema_version": 1, "source": {"sha256": SOURCE_HASH},
        "product": {"title": "Source cover fixture"}, "status": "draft", "groups": groups,
        "style": {"camera": [0, 0, 1], "up": [0, 1, 0], "width": 600, "height": 480, "margin": 45, "hlr": "poly"},
        "steps": [step("base_step", [], ["base"]), step("cap_step", ["base"], ["cap"]), step("overview", ["base", "cap"], [])],
        "cover_scene": {**step("parts_overview", [], ["base", "cap"]),
                        "offsets": {"base": [-30, 0, 0], "cap": [30, 0, 0]}},
        "excluded_part_ids": ["surface-part"], "exclusion_reason": "Explicit fixture visibility choice.",
    }


class CoverPlanTests(unittest.TestCase):
    def test_optional_cover_deepcopies_recipe_and_does_not_add_installation_steps(self):
        plan = fixture_plan()
        before = deepcopy(plan)
        derived = cover_plan(plan)
        self.assertEqual(len(plan["steps"]), 3)
        self.assertEqual(derived["steps"], [plan["cover_scene"]])
        self.assertNotIn("cover_scene", derived)
        for key in ("source", "groups", "style", "excluded_part_ids", "exclusion_reason"):
            self.assertEqual(derived[key], plan[key])
        derived["steps"][0]["offsets"]["base"][0] = -60
        derived["groups"][0]["label"] = "Edited copy"
        self.assertEqual(plan, before)
        plan.pop("cover_scene")
        self.assertIsNone(cover_plan(plan))
        # An absent cover does not touch paths or import geometry.
        self.assertIsNone(render_cover(plan, Path("missing-inventory"), Path("missing-output")))

    def test_cover_and_focus_filenames_cannot_shadow_installation_assets(self):
        for sid in ("base_step", "BASE_STEP", "base_step_focus"):
            with self.subTest(sid=sid):
                plan = fixture_plan()
                plan["cover_scene"]["id"] = sid
                with self.assertRaisesRegex(ValueError, "collides"):
                    cover_plan(plan)
        plan = fixture_plan()
        plan["steps"][0]["id"] = "parts_overview_focus"
        with self.assertRaisesRegex(ValueError, "collides"):
            cover_plan(plan)
        plan["cover_scene"]["id"] = "../outside"
        with self.assertRaisesRegex(ValueError, "Invalid cover ID"):
            cover_plan(plan)


@unittest.skipUnless(CAD_AVAILABLE, "CadQuery and PyMuPDF required for actual cover geometry")
class CoverGeometryTests(unittest.TestCase):
    def setUp(self):
        import cadquery as cq
        self.temp = tempfile.TemporaryDirectory(prefix="cover-render-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inventory, self.output = self.root / "inventory", self.root / "cover"
        self.inventory.mkdir()
        shapes = [cq.Workplane("XY").box(20, 20, 2).val(),
                  cq.Workplane("XY").box(8, 8, 2).val().translate((0, 0, 5)),
                  cq.Face.makeFromWires(cq.Workplane("XY").rect(6, 6).val())]
        rows = []
        for index, (pid, shape) in enumerate(zip(["base-part", "cap-part", "surface-part"], shapes)):
            brep = self.inventory / f"part_{index}.brep"
            shape.exportBrep(str(brep))
            rows.append({"id": pid, "index": index, "name": pid, "brep": brep.name,
                         "solids": len(shape.Solids()), "brep_sha256": hashlib.sha256(brep.read_bytes()).hexdigest()})
        row_path = self.inventory / "assembly_parts.json"
        row_path.write_text(json.dumps(rows), encoding="utf-8")
        (self.inventory / "import_complete.json").write_text(json.dumps({
            "source_sha256": SOURCE_HASH, "parts": 3, "brep_paths_relative_to": "inventory",
            "inventory_sha256": hashlib.sha256(row_path.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        self.plan = fixture_plan()

    def test_exploded_cover_uses_actual_retained_geometry_and_separate_recipe_hash(self):
        import fitz
        before = deepcopy(self.plan)
        entry = render_cover(self.plan, self.inventory, self.output)
        self.assertEqual(self.plan, before)
        self.assertEqual(entry["source_sha256"], SOURCE_HASH)
        self.assertEqual(entry["cover_plan_sha256"], plan_digest(cover_plan(self.plan)))
        self.assertEqual(entry["step_id"], "parts_overview")
        self.assertEqual(entry["part_ids"], ["base-part", "cap-part"])
        self.assertEqual(entry["excluded_part_ids"], ["surface-part"])
        self.assertEqual(entry["transforms"], {"base-part": [-30, 0, 0], "cap-part": [30, 0, 0]})
        self.assertEqual(entry["svg_path"], "parts_overview.svg")
        svg = self.output / entry["svg_path"]
        self.assertIn("<path", svg.read_text(encoding="utf-8"))
        with fitz.open(svg) as source:
            with fitz.open(stream=source.convert_to_pdf(), filetype="pdf") as vector:
                self.assertGreater(len(vector[0].get_drawings()), 1)
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["entries"]), 1)
        self.assertEqual(len(self.plan["steps"]), 3)
        self.plan["cover_scene"]["offsets"]["cap"][0] = 40
        self.assertNotEqual(entry["cover_plan_sha256"], plan_digest(cover_plan(self.plan)))

    def test_wrong_source_or_unknown_cover_group_is_rejected_before_output(self):
        self.plan["source"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            render_cover(self.plan, self.inventory, self.output)
        self.assertFalse(self.output.exists())
        self.plan = fixture_plan()
        self.plan["cover_scene"]["moving_groups"].append("unknown-group")
        with self.assertRaisesRegex(ValueError, "known assembled/moving groups"):
            render_cover(self.plan, self.inventory, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
