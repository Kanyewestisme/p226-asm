"""Real topology regressions for non-geometric STEP assembly placeholders.

OCCT represents an empty compound as a non-null shape, while its bounding box
is void. Such a structural placeholder is distinct from a surface, edge,
vertex, or a damaged shape that should fail instead of being omitted.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_analysis

HAS_CAD = importlib.util.find_spec("cadquery") is not None


@unittest.skipUnless(HAS_CAD, "CadQuery/OCP required for real topology tests")
class EmptyCompoundTopologyTests(unittest.TestCase):
    @staticmethod
    def compound(*children):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        builder, result = BRep_Builder(), TopoDS_Compound()
        builder.MakeCompound(result)
        for child in children:
            builder.Add(result, child)
        return result

    def test_non_null_empty_compound_reproduces_void_bbox(self):
        import cadquery as cq
        raw = self.compound()
        self.assertFalse(raw.IsNull())
        with self.assertRaisesRegex(Exception, "Bnd_Box is void"):
            cq.Shape.cast(raw).BoundingBox()
        self.assertTrue(model_analysis._is_empty_compound(raw))

    def test_nested_empty_compounds_are_non_geometric(self):
        raw = self.compound(self.compound(), self.compound(self.compound()))
        self.assertTrue(model_analysis._is_empty_compound(raw))

    def test_vertex_edge_face_and_solid_compounds_are_retained(self):
        import cadquery as cq
        geometries = {
            "vertex": cq.Vertex.makeVertex(1, 2, 3),
            "edge": cq.Edge.makeLine((0, 0, 0), (10, 0, 0)),
            "face": cq.Face.makeFromWires(cq.Workplane("XY").rect(4, 5).val()),
            "solid": cq.Workplane("XY").box(2, 3, 4).val(),
        }
        for name, shape in geometries.items():
            with self.subTest(name=name):
                raw = self.compound(self.compound(), shape.wrapped)
                self.assertFalse(model_analysis._is_empty_compound(raw))
                self.assertIsNotNone(cq.Shape.cast(raw).BoundingBox())

    def test_null_and_other_empty_topology_are_not_silently_classified(self):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Shape, TopoDS_Shell
        self.assertFalse(model_analysis._is_empty_compound(TopoDS_Shape()))
        empty_shell = TopoDS_Shell()
        BRep_Builder().MakeShell(empty_shell)
        self.assertFalse(model_analysis._is_empty_compound(empty_shell))


@unittest.skipUnless(HAS_CAD, "CadQuery/OCP required for STEP import tests")
class EmptyOccurrenceImportTests(unittest.TestCase):
    """Real STEP/XCAF transfer, injecting actual OCCT structural shapes.

The STEP writer does not reliably serialize a purely empty component. The
proxy substitutes one transferred occurrence's shape after native transfer;
all labels, names, assembly locations, geometry, and BREP I/O remain real.
"""
    @classmethod
    def setUpClass(cls):
        import cadquery as cq
        cls.temporary = tempfile.TemporaryDirectory(prefix="manual-empty-occurrence-")
        cls.root = Path(cls.temporary.name)
        cls.step = cls.root / "placeholders.stp"
        assembly = cq.Assembly(name="root")
        for number, name in enumerate(("first", "placeholder", "last")):
            # Give native STEP products distinct geometry; reusing one TShape
            # intentionally gives all occurrences the referred product name.
            block = cq.Workplane("XY").box(2 + number, 3, 4).val()
            assembly.add(block, name=name, loc=cq.Location((10 * number, 0, 0)))
        assembly.export(str(cls.step), exportType="STEP")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def shape_tool_override(self, *, all_empty=False, empty_assembly=False, null=False, replacement_shape=None):
        from cadquery.occ_impl.importers.assembly import _get_name
        from OCP.TopoDS import TopoDS_Shape
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        real_document_tool = XCAFDoc_DocumentTool
        replacement = (replacement_shape if replacement_shape is not None else
                       TopoDS_Shape() if null else EmptyCompoundTopologyTests.compound())

        class ShapeToolProxy:
            def __init__(self, real):
                self.real = real

            def __getattr__(self, name):
                return getattr(self.real, name)

            def GetShape_s(self, label):
                if all_empty or str(_get_name(label)) == "placeholder":
                    return replacement
                return self.real.GetShape_s(label)

            def IsAssembly_s(self, label):
                return (empty_assembly and str(_get_name(label)) == "placeholder") or self.real.IsAssembly_s(label)

        class DocumentToolProxy:
            SetLengthUnit_s = staticmethod(real_document_tool.SetLengthUnit_s)

            @staticmethod
            def ShapeTool_s(main):
                return ShapeToolProxy(real_document_tool.ShapeTool_s(main))

        return mock.patch("OCP.XCAFDoc.XCAFDoc_DocumentTool", DocumentToolProxy)

    def test_empty_leaf_has_diagnostic_without_geometry_or_index_gap(self):
        inventory = self.root / "one_empty"
        with self.shape_tool_override():
            complete = model_analysis.import_model(self.step, inventory)
        rows, checked = model_analysis._load_inventory(inventory)
        self.assertEqual(complete["parts"], 2)
        self.assertEqual([row["index"] for row in rows], [0, 1])
        self.assertEqual([row["name"] for row in rows], ["first", "last"])
        self.assertEqual(len(list(inventory.glob("*.brep"))), 2)
        diagnostics = complete["non_geometric_occurrences"]
        self.assertEqual(len(diagnostics), 1)
        missing = diagnostics[0]
        self.assertEqual(missing["name"], "placeholder")
        self.assertEqual(missing["reason"], "empty_compound")
        self.assertEqual(missing["geometry_status"], "no_geometry")
        self.assertEqual(missing["path"][-1], "placeholder")
        self.assertTrue(missing["occurrence_path"])
        self.assertTrue(missing["parent_occurrence"])
        self.assertTrue(missing["id"].startswith(complete["source_sha256"] + ":"))
        self.assertNotIn(missing["id"], {row["id"] for row in rows})
        self.assertEqual(checked["non_geometric_occurrences"], diagnostics)
        analysis = model_analysis.analyze_inventory(inventory, max_pairs=0)
        self.assertEqual(analysis["non_geometric_occurrences"], diagnostics)
        self.assertEqual(len(analysis["parts"]), 2)

    def test_empty_assembly_is_explicit_non_geometric_occurrence(self):
        inventory = self.root / "empty_assembly"
        actual_empty = EmptyCompoundTopologyTests.compound()
        with self.shape_tool_override(empty_assembly=True, replacement_shape=actual_empty):
            complete = model_analysis.import_model(self.step, inventory)
        self.assertEqual(complete["parts"], 2)
        diagnostics = complete["non_geometric_occurrences"]
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["reason"], "empty_assembly")
        self.assertEqual(diagnostics[0]["geometry_status"], "no_geometry")

    def test_null_assembly_without_components_is_explicit_non_geometric_occurrence(self):
        inventory = self.root / "null_assembly"
        with self.shape_tool_override(empty_assembly=True, null=True):
            complete = model_analysis.import_model(self.step, inventory)
        self.assertEqual(complete["parts"], 2)
        self.assertEqual(len(complete["non_geometric_occurrences"]), 1)
        self.assertEqual(complete["non_geometric_occurrences"][0]["reason"], "empty_assembly")

    def test_assembly_without_components_retains_own_geometry(self):
        import cadquery as cq
        shapes = {
            "solid": cq.Workplane("XY").box(2, 3, 4).val(),
            "face": cq.Face.makeFromWires(cq.Workplane("XY").rect(4, 5).val()),
            "edge": cq.Edge.makeLine((0, 0, 0), (10, 0, 0)),
            "vertex": cq.Vertex.makeVertex(1, 2, 3),
        }
        for kind, shape in shapes.items():
            with self.subTest(kind=kind):
                inventory = self.root / ("assembly_own_" + kind)
                # An assembly marker alone cannot prove a leaf has no geometry.
                with self.shape_tool_override(empty_assembly=True, replacement_shape=shape.wrapped):
                    complete = model_analysis.import_model(self.step, inventory)
                rows, _ = model_analysis._load_inventory(inventory)
                self.assertEqual(len(rows), 3)
                self.assertEqual([row["index"] for row in rows], [0, 1, 2])
                self.assertEqual(complete["non_geometric_occurrences"], [])
                own = next(row for row in rows if row["name"] == "placeholder")
                restored = cq.Shape.importBrep(str(inventory / own["brep"]))
                self.assertGreater(len(restored.Vertices()), 0)
                self.assertEqual(own["solids"], len(shape.Solids()))
                self.assertEqual(own["faces"], len(shape.Faces()))
                self.assertEqual(own["edges"], len(shape.Edges()))
                self.assertAlmostEqual(own["area_mm2"], abs(shape.Area()), places=5)

    def test_identity_bbox_is_reused_but_translated_world_and_local_bounds_are_measured(self):
        import cadquery as cq
        step = self.root / "identity_and_translation.stp"
        assembly = cq.Assembly(name="root")
        shared = cq.Workplane("XY").box(2, 3, 4).val()
        assembly.add(shared, name="identity")
        assembly.add(shared, name="translated", loc=cq.Location((10, 20, 30)))
        assembly.export(str(step), exportType="STEP")
        real_bbox, measured = cq.Shape.BoundingBox, []

        def counted_bbox(shape, *args, **kwargs):
            measured.append(shape.Center().toTuple())
            return real_bbox(shape, *args, **kwargs)

        inventory = self.root / "bbox_reuse"
        with mock.patch.object(cq.Shape, "BoundingBox", new=counted_bbox):
            complete = model_analysis.import_model(step, inventory)
        rows, _ = model_analysis._load_inventory(inventory)
        self.assertEqual(complete["parts"], 2)
        # Identity requires one exact scan. Translation requires separate world
        # and local scans, despite keeping the same shape geometry fingerprint.
        self.assertEqual(len(measured), 3)
        origin_scans = sum(all(abs(value) < 1e-7 for value in center) for center in measured)
        translated_scans = sum(all(abs(value - expected) < 1e-7 for value, expected in
                                   zip(center, (10, 20, 30))) for center in measured)
        self.assertEqual(origin_scans, 2)
        self.assertEqual(translated_scans, 1)
        identity, translated = sorted(rows, key=lambda row: row["bbox_min"][0])
        for actual, expected in zip(identity["bbox_min"], (-1, -1.5, -2)):
            self.assertAlmostEqual(actual, expected, places=6)
        for actual, expected in zip(translated["bbox_min"], (9, 18.5, 28)):
            self.assertAlmostEqual(actual, expected, places=6)
        for row in rows:
            for actual, expected in zip(row["bbox_size"], (2, 3, 4)):
                self.assertAlmostEqual(actual, expected, places=6)
            self.assertAlmostEqual(row["volume_mm3"], 24, places=6)
            self.assertAlmostEqual(row["area_mm2"], 52, places=6)
        self.assertEqual(identity["geometry_fingerprint"], translated["geometry_fingerprint"])

    def test_non_solid_source_occurrences_retain_real_geometry(self):
        import cadquery as cq
        shapes = {
            "vertex": cq.Vertex.makeVertex(1, 2, 3),
            "edge": cq.Edge.makeLine((0, 0, 0), (10, 0, 0)),
            "face": cq.Face.makeFromWires(cq.Workplane("XY").rect(4, 5).val()),
        }
        for kind, shape in shapes.items():
            with self.subTest(kind=kind):
                inventory = self.root / ("retained_" + kind)
                # A non-solid compound is not an empty placeholder.
                raw = EmptyCompoundTopologyTests.compound(shape.wrapped)
                with self.shape_tool_override(replacement_shape=raw):
                    complete = model_analysis.import_model(self.step, inventory)
                rows, _ = model_analysis._load_inventory(inventory)
                self.assertEqual(len(rows), 3)
                self.assertEqual(complete["non_geometric_occurrences"], [])
                row = next(row for row in rows if row["name"] == "placeholder")
                self.assertEqual(row["solids"], 0)
                self.assertTrue((inventory / row["brep"]).is_file())
                restored = cq.Shape.importBrep(str(inventory / row["brep"]))
                self.assertGreater(len(restored.Vertices()), 0)
                if kind == "vertex":
                    self.assertEqual(row["faces"], 0)
                    self.assertEqual(row["edges"], 0)
                if kind == "edge":
                    self.assertEqual(row["faces"], 0)
                    self.assertGreater(row["edges"], 0)
                if kind == "face":
                    self.assertGreater(row["faces"], 0)
                    self.assertAlmostEqual(row["area_mm2"], 20, places=5)

    def test_all_empty_source_cannot_publish_inventory(self):
        inventory = self.root / "all_empty"
        with self.shape_tool_override(all_empty=True):
            with self.assertRaisesRegex(ValueError, "no leaf shapes|no geometric leaf"):
                model_analysis.import_model(self.step, inventory)
        self.assertFalse(inventory.exists())

    def test_null_geometry_still_fails_and_does_not_publish(self):
        inventory = self.root / "null_geometry"
        with self.shape_tool_override(null=True):
            with self.assertRaisesRegex(ValueError, "no geometry.*root:"):
                model_analysis.import_model(self.step, inventory)
        self.assertFalse(inventory.exists())

    def test_bbox_error_on_real_solid_is_not_skipped(self):
        import cadquery as cq
        inventory = self.root / "damaged_bbox"
        with mock.patch.object(cq.Shape, "BoundingBox", side_effect=RuntimeError("Bnd_Box is void")):
            with self.assertRaisesRegex(ValueError, "first.*root:|root:.*first"):
                model_analysis.import_model(self.step, inventory)
        self.assertFalse(inventory.exists())


if __name__ == "__main__":
    unittest.main()
