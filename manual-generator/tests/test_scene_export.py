"""Current source display meshes preserve identity, coverage and cache binding."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
import subprocess
import builtins
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scene_export import export_scene, load_cached_scene, _sample_part


def write_json(path, content):
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")


class SceneExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.inventory = self.project / "inventory"
        self.inventory.mkdir()
        self.source = self.project / "assembly.step"
        self.source.write_bytes(b"source fixture identity")
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.rows = []
        for index in range(2):
            brep = self.inventory / f"part_{index}.brep"
            brep.write_bytes(f"retained BREP fixture {index}".encode())
            self.rows.append({"id": f"source-occurrence-{index}", "name": "duplicate name", "brep": brep.name,
                              "brep_sha256": hashlib.sha256(brep.read_bytes()).hexdigest(), "source_sha256": self.sha,
                              "bbox_min": [index*10, 0, 0], "bbox_max": [index*10+5, 5, 5], "solids": 1 if index == 0 else 0, "faces": 1})
        write_json(self.inventory / "assembly_parts.json", self.rows)
        complete = {"units": "mm", "brep_paths_relative_to": "inventory", "parts": 2, "source_sha256": self.sha,
                    "inventory_sha256": hashlib.sha256((self.inventory / "assembly_parts.json").read_bytes()).hexdigest()}
        write_json(self.inventory / "import_complete.json", complete)
        write_json(self.project / "source.json", {"path": str(self.source), "sha256": self.sha})
        write_json(self.project / "steps.json", {"source": {"sha256": self.sha}})

    def tearDown(self):
        self.temp.cleanup()

    def mesh(self, path, row):
        low, high = row["bbox_min"], row["bbox_max"]
        return {"id": row["id"], "name": row["name"], "positions": [*low, high[0], low[1], low[2], high[0], high[1], low[2]],
                "indices": [0, 1, 2], "bbox": [low, high], "solids": row["solids"], "faces": row["faces"]}

    def test_every_solid_and_surface_occurrence_and_duplicate_name_are_retained(self):
        progress = []
        with patch("scene_export._sample_part", side_effect=self.mesh) as sample:
            scene = export_scene(self.project, progress.append)
        self.assertEqual(sample.call_count, 2)
        self.assertEqual(scene["source_sha256"], self.sha)
        self.assertEqual([part["id"] for part in scene["parts"]], [row["id"] for row in self.rows])
        self.assertEqual(scene["bounds"], [[0, 0, 0], [15, 5, 5]])
        self.assertEqual(scene["triangle_count"], 2)
        self.assertEqual(scene["units"], "mm")
        self.assertTrue(scene["display_only"])
        self.assertEqual(progress[-1]["phase"], "complete")
        self.assertFalse(list(self.project.rglob("*.svg")))
        self.assertFalse(list(self.project.rglob("*.pdf")))

    def test_cache_reuse_needs_no_cad_and_is_independent_of_step_text_or_view(self):
        with patch("scene_export._sample_part", side_effect=self.mesh):
            export_scene(self.project)
        write_json(self.project / "steps.json", {"source": {"sha256": self.sha}, "steps": [{"title": "changed", "camera": [0, 1, 0]}]})
        with patch("scene_export._sample_part", side_effect=AssertionError("cache must not tessellate")):
            scene = export_scene(self.project)
            self.assertEqual(len(scene["parts"]), 2)
        original_import = builtins.__import__
        def no_cad(name, *args, **kwargs):
            if name == "cadquery" or name.startswith("OCP"):
                raise AssertionError("cache reads must not import native CAD")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=no_cad):
            self.assertEqual(load_cached_scene(self.project)["triangle_count"], 2)
        self.source.unlink()
        self.assertEqual(load_cached_scene(self.project)["source_sha256"], self.sha)

    def test_changed_source_or_retained_brep_is_rejected(self):
        self.source.write_bytes(b"new source")
        with self.assertRaisesRegex(ValueError, "input STEP has changed"):
            load_cached_scene(self.project)
        self.source.write_bytes(b"source fixture identity")
        (self.inventory / self.rows[0]["brep"]).write_bytes(b"changed BREP")
        with self.assertRaisesRegex(ValueError, "BREP cache differs"):
            export_scene(self.project)

    def test_mesh_cache_tampering_is_detected_and_rebuilt(self):
        with patch("scene_export._sample_part", side_effect=self.mesh):
            export_scene(self.project)
        path = self.project / "scene" / "scene.json"
        scene = json.loads(path.read_text(encoding="utf-8"))
        scene["parts"][0]["positions"][0] = 987654
        write_json(path, scene)
        self.assertIsNone(load_cached_scene(self.project))
        with patch("scene_export._sample_part", side_effect=self.mesh) as sample:
            result = export_scene(self.project)
        self.assertEqual(sample.call_count, 2)
        self.assertEqual(result["parts"][0]["positions"][0], 0)

    def test_inventory_metadata_or_plan_source_cannot_relabel_cache(self):
        rows = deepcopy(self.rows)
        rows[0]["name"] = "renamed without importer evidence"
        write_json(self.inventory / "assembly_parts.json", rows)
        with self.assertRaisesRegex(ValueError, "Inventory manifest differs"):
            export_scene(self.project)
        write_json(self.inventory / "assembly_parts.json", self.rows)
        write_json(self.project / "steps.json", {"source": {"sha256": "b"*64}})
        with self.assertRaisesRegex(ValueError, "Scene source and retained STEP inventory differ"):
            export_scene(self.project)

    def test_budget_failure_or_source_change_never_publishes_partial_cache(self):
        with patch("scene_export._sample_part", side_effect=self.mesh), patch("scene_export.MAX_VERTICES", 1):
            with self.assertRaisesRegex(ValueError, "No source instances were dropped"):
                export_scene(self.project)
        self.assertFalse((self.project / "scene" / "scene.json").exists())
        def changing(path, row):
            mesh = self.mesh(path, row)
            if row["id"] == self.rows[-1]["id"]:
                self.source.write_bytes(b"changed during tessellation")
            return mesh
        with patch("scene_export._sample_part", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "input STEP has changed"):
                export_scene(self.project)
        self.assertFalse((self.project / "scene" / "scene.json").exists())

    @unittest.skipUnless(shutil.which("node"), "Node required for viewer vector-math checks without WebGL")
    def test_viewer_camera_and_group_offsets_match_source_hlr_semantics(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
import * as THREE from './frontend/vendor/three.module.js';
import {StepViewer} from './frontend/viewer.js';
const camera = new THREE.OrthographicCamera();
camera.up.set(0,0,1); camera.position.set(7,3,4); camera.lookAt(new THREE.Vector3(1,2,1));
const view = StepViewer.prototype.getView.call({camera,controls:{target:new THREE.Vector3(1,2,1),update(){}}});
const viewer = Object.create(StepViewer.prototype);
viewer.origin = new THREE.Vector3(100,0,0); viewer.arrowGroup = new THREE.Group(); viewer.objects = new Map();
viewer.sceneData={source_sha256:'current'};
for(const [id,x] of [['a',100],['b',110],['c',140]]) {const o=new THREE.Group();o.userData.center=new THREE.Vector3(x,0,0);viewer.objects.set(id,o);}
const plan={source:{sha256:'current'},groups:[{id:'base',part_ids:['a']},{id:'moving',part_ids:['b']}],excluded_part_ids:['a']};
const step={assembled_groups:['base'],moving_groups:['moving'],offsets:{moving:[5,2,0]},arrows:[]};
viewer.setStep(plan,step,{preserveView:true}); viewer.explodeRatio=2;viewer._applyPositions();
const visible=[...viewer.objects].map(([id,o])=>({id,visible:o.visible,position:o.position.toArray()}));
step.hidden_part_ids=['b'];viewer._applyPositions();
console.log(JSON.stringify({view,visible,hiddenB:viewer.objects.get('b').visible,retained:viewer.objects.size}));
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script], cwd=root, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        camera, up = data["view"]["camera"], data["view"]["up"]
        length = sum(value*value for value in [6, 1, 3])**0.5
        for actual, expected in zip(camera, [6/length, 1/length, 3/length]):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(sum(a*b for a, b in zip(camera, up)), 0)
        self.assertEqual(data["visible"][1], {"id": "b", "visible": True, "position": [20, 4, 0]})
        self.assertFalse(data["visible"][0]["visible"])
        self.assertFalse(data["visible"][2]["visible"])
        self.assertFalse(data["hiddenB"])
        self.assertEqual(data["retained"], 3)

    @unittest.skipUnless(importlib.util.find_spec("cadquery"), "CadQuery required for retained BREP tessellation")
    def test_real_brep_solid_surface_and_wire_geometry_tessellation(self):
        import cadquery as cq
        shapes = [cq.Workplane("XY").box(8, 8, 8).val(), cq.Face.makePlane(5, 5), cq.Edge.makeLine(cq.Vector(0, 0, 0), cq.Vector(5, 0, 0))]
        for index, shape in enumerate(shapes):
            with self.subTest(index=index):
                path = self.inventory / f"native-{index}.brep"
                shape.exportBrep(str(path))
                box = shape.BoundingBox()
                row = {"id": f"actual-{index}", "name": "model geometry", "faces": len(shape.Faces()), "solids": len(shape.Solids()),
                       "bbox_min": [box.xmin, box.ymin, box.zmin], "bbox_max": [box.xmax, box.ymax, box.zmax]}
                part = _sample_part(path, row)
                self.assertEqual(part["id"], row["id"])
                if row["faces"]:
                    self.assertGreater(len(part["indices"]), 0)
                    self.assertTrue(all(0 <= i < len(part["positions"])//3 for i in part["indices"]))
                else:
                    self.assertTrue(part["lines"])
                    self.assertEqual(part["indices"], [])


if __name__ == "__main__":
    unittest.main()
