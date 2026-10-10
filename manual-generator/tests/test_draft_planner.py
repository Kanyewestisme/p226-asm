"""Safety and determinism checks for editable STEP-only draft proposals."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "draft_planner.py"
spec = importlib.util.spec_from_file_location("draft_planner", MODULE)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def part(part_id, name="重复零件", parent=None, lower=(0, 0, 0), size=(20, 20, 20), volume=8000,
         fingerprint=None):
    occurrence = list(parent) + [part_id] if parent else [part_id]
    return {"id": part_id, "index": 0, "name": name,
            "path": ["整机"] + ["重复总成"] * (len(occurrence) - 1),
            "occurrence_path": occurrence, "parent_occurrence": list(parent) if parent else None,
            "bbox_min": list(lower), "bbox_max": [a + b for a, b in zip(lower, size)],
            "bbox_size": list(size), "solids": 1, "volume_mm3": volume,
            "geometry_fingerprint": fingerprint or f"geometry-{part_id}"}


def contact(a, b, distance=0.1):
    return {"part_ids": [a, b], "distance_mm": distance, "point_a": [10, 10, 10],
            "point_b": [10 + distance, 10, 10], "status": "candidate"}


def analysis(parts=None, contacts=None, sha="source-a"):
    return {"source_sha256": sha, "parts": parts or [part("a"), part("b", lower=(25, 0, 0))],
            "contacts": contacts or [], "warnings": []}


class DraftPlannerTests(unittest.TestCase):
    def test_repeated_names_remain_separate_occurrences(self):
        source = analysis([part("a", parent=("root", "left")),
                           part("b", parent=("root", "right"), lower=(25, 0, 0))])
        plan = planner.build_plan(source)
        self.assertEqual(len(plan["groups"]), 2)
        self.assertEqual(len({g["id"] for g in plan["groups"]}), 2)
        self.assertEqual({tuple(g["part_ids"]) for g in plan["groups"]}, {("a",), ("b",)})
        self.assertTrue(all(g["status"] == "proposed" for g in plan["groups"]))

    def test_meaningful_cad_branch_groups_without_flat_root_merge(self):
        source = analysis([part("castor-a", parent=("root", "left")),
                           part("bolt-a", parent=("root", "left")),
                           part("castor-b", parent=("root", "right")),
                           part("bolt-b", parent=("root", "right")),
                           part("frame", parent=("root",), volume=30000)])
        plan = planner.build_plan(source)
        sets = {frozenset(g["part_ids"]) for g in plan["groups"]}
        self.assertEqual(sets, {frozenset(("castor-a", "bolt-a")),
                               frozenset(("castor-b", "bolt-b")), frozenset(("frame",))})
        source["parts"] = [part("a", parent=("root",)), part("b", parent=("root",))]
        flat = planner.build_plan(source)
        self.assertEqual(len(flat["groups"]), 2)
        self.assertTrue(any("层级" in g["basis"] for g in flat["groups"]))

    def test_names_do_not_create_hierarchy_when_occurrence_identity_missing(self):
        source = analysis()
        for row in source["parts"]:
            row.pop("occurrence_path")
            row["parent_occurrence"] = None
            row["path"] = ["整机", "同名总成", "同名零件"]
        plan = planner.build_plan(source)
        self.assertEqual(len(plan["groups"]), 2)
        self.assertTrue(any("独立组" in warning for warning in plan["warnings"]))

    def test_numeric_occurrence_path_overrides_opaque_parent_hash(self):
        rows = [part("seat", parent=("root:0001", "component:0001", "component:0001")),
                part("bolt", parent=("root:0001", "component:0001", "component:0002")),
                part("base", parent=("root:0001", "component:0002"))]
        for index, row in enumerate(rows):
            row["parent_occurrence"] = f"opaque-parent-hash-{index}"
        source = analysis(rows)
        plan = planner.build_plan(source)
        self.assertEqual({frozenset(g["part_ids"]) for g in plan["groups"]},
                         {frozenset(("seat", "bolt")), frozenset(("base",))})

    def test_missing_hierarchy_leaf_does_not_merge_identified_root(self):
        source = analysis([part("a", parent=("root", "left")),
                           part("b", parent=("root", "right")), part("unknown")])
        plan = planner.build_plan(source)
        self.assertEqual({frozenset(g["part_ids"]) for g in plan["groups"]},
                         {frozenset(("a",)), frozenset(("b",)), frozenset(("unknown",))})

    def test_graph_order_and_plan_are_deterministic_with_input_permutations(self):
        rows = [part("base", volume=40000), part("neighbor", lower=(25, 0, 0)),
                part("far", lower=(50, 0, 0)), part("disconnected", lower=(70, 0, 0), volume=16000)]
        links = [contact("base", "neighbor"), contact("neighbor", "far")]
        source = analysis(rows, links)
        plan = planner.build_plan(source)
        permuted = analysis(list(reversed(rows)), list(reversed(links)))
        self.assertEqual(plan, planner.build_plan(permuted))
        group_part = {g["id"]: g["part_ids"][0] for g in plan["groups"]}
        self.assertEqual([group_part[s["moving_groups"][0]] for s in plan["steps"][:-1]],
                         ["base", "neighbor", "far", "disconnected"])
        self.assertTrue(any("检测可能不完整" in w for w in plan["steps"][-2]["warnings"]))
        self.assertTrue(all(s["reviewed"] is False for s in plan["steps"]))
        self.assertEqual(plan["status"], "draft")

    def test_final_overview_contains_every_instance_and_no_displacement(self):
        source = analysis()
        plan = planner.build_plan(source)
        overview = plan["steps"][-1]
        self.assertEqual(set(overview["assembled_groups"]), {g["id"] for g in plan["groups"]})
        self.assertEqual(overview["moving_groups"], [])
        self.assertEqual(overview["offsets"], {})
        self.assertEqual(overview["arrows"], [])
        self.assertEqual(overview["hidden_part_ids"], [])
        self.assertEqual(overview["visibility_reason"], "")
        planner.validate_plan(plan, source)

    def test_arrows_only_point_to_original_step_positions(self):
        source = analysis(contacts=[contact("a", "b")])
        plan = planner.build_plan(source)
        step = plan["steps"][1]
        arrow = step["arrows"][0]
        self.assertEqual(arrow["meaning"], "reposition")
        self.assertEqual(arrow["status"], "proposed")
        self.assertTrue(any("不证明插入方向" in warning for warning in step["warnings"]))
        self.assertEqual(plan["connection_candidates"][0]["status"], "unverified")
        self.assertEqual(plan["connection_candidates"][0]["contacts"][0]["status"], "unverified")
        self.assertTrue(all(s["focus_part_ids"] == [] for s in plan["steps"]))

    def test_missing_volume_and_surfaces_are_not_silently_dropped(self):
        source = analysis()
        source["parts"][1].pop("volume_mm3")
        source["parts"][1]["solids"] = 0
        plan = planner.build_plan(source)
        self.assertEqual({p for g in plan["groups"] for p in g["part_ids"]}, {"a", "b"})
        self.assertTrue(all(s["hidden_part_ids"] == [] and s["visibility_reason"] == ""
                            for s in plan["steps"]))

    def test_invalid_analysis_fails_closed(self):
        for modify in [lambda s: s["parts"].append(deepcopy(s["parts"][0])),
                       lambda s: s["parts"][0]["bbox_max"].__setitem__(0, -1),
                       lambda s: s["contacts"].append(contact("a", "unknown")),
                       lambda s: s["parts"][0]["bbox_min"].__setitem__(0, float("inf"))]:
            source = analysis()
            modify(source)
            with self.assertRaises(ValueError):
                planner.build_plan(source)

    def test_malformed_or_geometry_dropping_edits_are_rejected(self):
        source = analysis(contacts=[contact("a", "b")])
        original = planner.build_plan(source)
        changes = [lambda p: p["source"].update(sha256="different-source"),
                   lambda p: p["groups"].pop(),
                   lambda p: p["groups"][1].update(id=p["groups"][0]["id"]),
                   lambda p: p["steps"][1].update(id=p["steps"][0]["id"]),
                   lambda p: p["steps"][1].update(assembled_groups=[]),
                   lambda p: p["steps"][1].update(camera=[0, 1, 0], up=[0, 2, 0]),
                   lambda p: p["steps"][1].update(camera=[0, 0, 0]),
                   lambda p: p["steps"][1]["offsets"].update(unknown=[0, 0, 0]),
                   lambda p: p["steps"][1]["offsets"].update({p["steps"][1]["moving_groups"][0]: [10000, 0, 0]}),
                   lambda p: p["steps"][1]["arrows"][0].update(to=[10000, 0, 0]),
                   lambda p: p["steps"][1]["arrows"][0].update(meaning="insertion"),
                   lambda p: p["steps"][-1].update(assembled_groups=[]),
                   lambda p: p["steps"][1].update(focus_part_ids=["unknown"]),
                   lambda p: p.update(status="confirmed"),
                   lambda p: p.update(status=[]),
                   lambda p: p["groups"][0].update(status={}),
                   lambda p: p["style"].update(hlr=[]),
                   lambda p: p["steps"][0]["evidence"][0].update(group_id=[]),
                   lambda p: p["style"].update(margin=1000),
                   lambda p: p["style"].update(margin=31),
                   lambda p: p["style"].update(width=1100.0),
                   lambda p: p["style"].update(width=float("nan")),
                   lambda p: p["steps"][1].update(id="CON"),
                   lambda p: p["groups"][0].update(id="../escape"),
                   lambda p: p["steps"][1].update(id=p["steps"][0]["id"] + "_focus"),
                   lambda p: p["connection_candidates"][0].update(status="proven")]
        for change in changes:
            plan = deepcopy(original)
            change(plan)
            with self.assertRaises(ValueError):
                planner.validate_plan(plan, source)

    def test_confirmation_requires_each_step_reviewed(self):
        source = analysis()
        plan = planner.build_plan(source)
        for step in plan["steps"]:
            step["reviewed"] = True
        plan["status"] = "confirmed"
        planner.validate_plan(plan, source)

    def test_revision_changed_hash_invalidates_even_identical_geometry(self):
        old = analysis()
        plan = planner.build_plan(old)
        for step in plan["steps"]:
            step["reviewed"] = True
        plan["status"] = "confirmed"
        new = deepcopy(old)
        new["source_sha256"] = "source-b"
        before = deepcopy(plan)
        report = planner.compare_revisions(old, new, plan)
        self.assertTrue(report["source_changed"])
        self.assertTrue(report["requires_redraft"])
        self.assertTrue(report["confirmations_invalidated"])
        self.assertEqual(len(report["invalidated_step_ids"]), len(plan["steps"]))
        self.assertEqual(plan, before)
        self.assertFalse(report["automatically_applied"])
        self.assertTrue(all(m["status"] == "possible" for m in report["matches"]))
        with self.assertRaises(ValueError):
            planner.validate_plan(plan, new)

    def test_revision_does_not_match_names_or_indices(self):
        old = analysis([part("old", fingerprint="old-geometry")])
        new = analysis([part("new", fingerprint="new-geometry")], sha="source-b")
        report = planner.compare_revisions(old, new)
        self.assertEqual(report["matches"], [])
        self.assertEqual(report["unmatched_old"], ["old"])
        self.assertEqual(report["unmatched_new"], ["new"])

    def test_revision_placement_disambiguates_repeated_geometry(self):
        old = analysis([part("old-a", fingerprint="same"),
                        part("old-b", lower=(25, 0, 0), fingerprint="same")])
        new = analysis([part("new-b", lower=(25, 0, 0), fingerprint="same"),
                        part("new-a", fingerprint="same")], sha="source-b")
        report = planner.compare_revisions(old, new)
        self.assertEqual({(m["old_part_id"], m["new_part_id"]) for m in report["matches"]},
                         {("old-a", "new-a"), ("old-b", "new-b")})
        self.assertTrue(all(m["same_placement"] for m in report["matches"]))

    def test_revision_ambiguous_overlap_and_many_to_one_never_auto_match(self):
        old = analysis([part("old-a", fingerprint="same"), part("old-b", fingerprint="same")])
        new = analysis([part("new-a", fingerprint="same")], sha="source-b")
        report = planner.compare_revisions(old, new)
        self.assertEqual(report["matches"], [])
        self.assertEqual(len(report["ambiguous_matches"]), 2)
        new["parts"].append(part("new-b", fingerprint="same"))
        report = planner.compare_revisions(old, new)
        self.assertEqual(report["matches"], [])
        self.assertTrue(all(len(m["new_part_ids"]) == 2 for m in report["ambiguous_matches"]))

    def test_revision_moved_unique_geometry_is_review_clue_only(self):
        old = analysis([part("old", fingerprint="same")])
        new = analysis([part("new", lower=(40, 0, 0), fingerprint="same")], sha="source-b")
        report = planner.compare_revisions(old, new)
        self.assertEqual(len(report["matches"]), 1)
        self.assertFalse(report["matches"][0]["same_placement"])
        self.assertEqual(report["matches"][0]["basis"], "geometry_fingerprint_only_placement_changed")
        self.assertTrue(report["requires_redraft"])

    def test_same_source_does_not_invalidate_confirmation(self):
        old = analysis()
        report = planner.compare_revisions(old, deepcopy(old))
        self.assertFalse(report["requires_redraft"])
        self.assertFalse(report["confirmations_invalidated"])

    def test_configured_subsets_repeat_moves_and_static_scene(self):
        source = analysis([part("a"), part("b", lower=(25, 0, 0)),
                           part("c", lower=(50, 0, 0))])
        plan = planner.build_plan(source)
        plan["sequence_mode"] = "configured"
        first = deepcopy(plan["steps"][0])
        first["id"] = "repeat-first"
        first["assembled_groups"] = []
        first["focus_part_ids"] = []
        first["evidence"] = []
        static = deepcopy(plan["steps"][1])
        static["id"] = "static-subset"
        static["assembled_groups"] = list(static["moving_groups"])
        static["moving_groups"] = []
        static["offsets"] = {}
        static["arrows"] = []
        static["evidence"] = []
        plan["steps"] = [plan["steps"][0], static, first, plan["steps"][-1]]
        planner.validate_plan(plan, source)
        invalid = deepcopy(plan)
        invalid["sequence_mode"] = "cumulative"
        with self.assertRaises(ValueError):
            planner.validate_plan(invalid, source)
        for change in [lambda p: p.update(sequence_mode="arbitrary"),
                       lambda p: p["steps"][0].update(assembled_groups=p["steps"][0]["moving_groups"]),
                       lambda p: p["steps"][-1].update(assembled_groups=p["steps"][-1]["assembled_groups"][:-1]),
                       lambda p: p["steps"][1].update(assembled_groups=[])]:
            invalid = deepcopy(plan)
            change(invalid)
            with self.assertRaises(ValueError):
                planner.validate_plan(invalid, source)

    def test_explicit_exclusion_retains_inventory_provenance(self):
        source = analysis([part("a", parent=("root", "left")),
                           part("aux", parent=("root", "left")),
                           part("b", parent=("root", "right"), lower=(25, 0, 0))])
        plan = planner.build_plan(source)
        plan["excluded_part_ids"] = ["aux"]
        plan["exclusion_reason"] = "包装明确选择隐藏重复辅助几何。"
        planner.validate_plan(plan, source)
        self.assertEqual({p for g in plan["groups"] for p in g["part_ids"]}, {"a", "aux", "b"})
        for change in [lambda p: p.update(excluded_part_ids=["unknown"]),
                       lambda p: p.update(excluded_part_ids=["aux", "aux"]),
                       lambda p: p.update(exclusion_reason=""),
                       lambda p: p.update(exclusion_reason="   "),
                       lambda p: p.pop("exclusion_reason"),
                       lambda p: p.update(excluded_part_ids=["a", "aux"]),
                       lambda p: p["steps"][-1].update(focus_part_ids=["aux"])]:
            invalid = deepcopy(plan)
            change(invalid)
            with self.assertRaises(ValueError):
                planner.validate_plan(invalid, source)

    def visibility_fixture(self):
        source = analysis([part("frame", parent=("root", "seat"), volume=30000),
                           part("mesh", parent=("root", "seat"), volume=0),
                           part("base", parent=("root", "base"), lower=(25, 0, 0))],
                          [contact("frame", "base")])
        source["parts"][1]["solids"] = 0
        return source, planner.build_plan(source)

    def test_step_visibility_hides_occurrence_inside_group_and_restores_overview(self):
        source, plan = self.visibility_fixture()
        before_source = deepcopy(source)
        self.assertEqual({p for g in plan["groups"] for p in g["part_ids"]},
                         {"frame", "mesh", "base"})
        step = plan["steps"][1]
        step["hidden_part_ids"] = ["mesh"]
        step["visibility_reason"] = "此步骤临时隐藏独立网布实例，以展示内部连接。"
        step["focus_part_ids"] = ["frame", "base"]
        before_plan = deepcopy(plan)
        planner.validate_plan(plan, source)
        self.assertEqual(source, before_source)
        self.assertEqual(plan, before_plan)
        self.assertEqual(plan["steps"][-1]["hidden_part_ids"], [])
        self.assertNotIn("excluded_part_ids", plan)
        # Visibility applies independently per scene, including a partial moving
        # group; it does not remove a member from the inventory or rewrite STEP.
        plan["steps"][0]["hidden_part_ids"] = ["mesh"]
        plan["steps"][0]["visibility_reason"] = "临时展示座框结构。"
        plan["steps"][0]["focus_part_ids"] = ["frame"]
        planner.validate_plan(plan, source)

    def test_step_visibility_fields_remain_optional_for_existing_plans(self):
        source, plan = self.visibility_fixture()
        for step in plan["steps"]:
            step.pop("hidden_part_ids")
            step.pop("visibility_reason")
        before = deepcopy(plan)
        planner.validate_plan(plan, source)
        self.assertEqual(plan, before)

    def test_invalid_step_visibility_is_rejected_without_mutation(self):
        source, original = self.visibility_fixture()
        changes = [
            lambda p: p["steps"][-1].update(hidden_part_ids=["mesh"], visibility_reason="临时隐藏"),
            lambda p: p["steps"][0].update(hidden_part_ids=["base"], visibility_reason="不在本步"),
            lambda p: p["steps"][1].update(hidden_part_ids=["unknown"], visibility_reason="未知实例"),
            lambda p: p["steps"][1].update(hidden_part_ids=["mesh", "mesh"], visibility_reason="重复实例"),
            lambda p: p["steps"][1].update(hidden_part_ids="mesh", visibility_reason="类型错误"),
            lambda p: p["steps"][1].update(hidden_part_ids=["mesh"], visibility_reason=""),
            lambda p: p["steps"][1].update(hidden_part_ids=["mesh"], visibility_reason="  "),
            lambda p: (p["steps"][1].update(hidden_part_ids=["mesh"]),
                       p["steps"][1].pop("visibility_reason")),
            lambda p: p["steps"][1].update(visibility_reason=[]),
            lambda p: p["steps"][1].update(hidden_part_ids=["base"], visibility_reason="隐藏整个移动组"),
            lambda p: p["steps"][1].update(hidden_part_ids=["mesh"], visibility_reason="隐藏网布",
                                          focus_part_ids=["mesh"]),
            lambda p: (p.update(excluded_part_ids=["mesh"], exclusion_reason="全局明确排除"),
                       p["steps"][1].update(hidden_part_ids=["mesh"], visibility_reason="重复排除")),
            # A fused leaf exposes only its occurrence ID. An invented fabric
            # fragment ID cannot be treated as a separately selectable part.
            lambda p: p["steps"][1].update(hidden_part_ids=["frame/fabric"], visibility_reason="虚构子几何"),
        ]
        for change in changes:
            invalid = deepcopy(original)
            change(invalid)
            before_plan, before_source = deepcopy(invalid), deepcopy(source)
            with self.assertRaises(ValueError):
                planner.validate_plan(invalid, source)
            self.assertEqual(invalid, before_plan)
            self.assertEqual(source, before_source)

    def test_temporarily_hidden_moving_group_cannot_leave_empty_display(self):
        source, plan = self.visibility_fixture()
        step = plan["steps"][1]
        step["hidden_part_ids"] = ["base"]
        step["visibility_reason"] = "整组被临时隐藏。"
        with self.assertRaisesRegex(ValueError, "each moving group must retain"):
            planner.validate_plan(plan, source)

    def test_configured_static_scene_can_hide_group_while_restoring_final_overview(self):
        source, plan = self.visibility_fixture()
        plan["sequence_mode"] = "configured"
        step = plan["steps"][1]
        step["assembled_groups"] += step["moving_groups"]
        step["moving_groups"] = []
        step["offsets"] = {}
        step["arrows"] = []
        step["hidden_part_ids"] = ["base"]
        step["visibility_reason"] = "静态连接细节图临时隐藏独立底座。"
        step["focus_part_ids"] = ["frame"]
        planner.validate_plan(plan, source)
        self.assertEqual(plan["steps"][-1]["hidden_part_ids"], [])

    def test_global_auxiliary_exclusion_and_temporary_visibility_are_distinct(self):
        source = analysis([part("frame", parent=("root", "seat"), volume=30000),
                           part("mesh", parent=("root", "seat"), volume=0),
                           part("aux", parent=("root", "seat"), volume=0),
                           part("base", parent=("root", "base"), lower=(25, 0, 0))])
        plan = planner.build_plan(source)
        plan["excluded_part_ids"] = ["aux"]
        plan["exclusion_reason"] = "包装明确排除辅助重复几何。"
        plan["steps"][1]["hidden_part_ids"] = ["mesh"]
        plan["steps"][1]["visibility_reason"] = "临时展示内部连接。"
        plan["steps"][1]["focus_part_ids"] = ["frame", "base"]
        planner.validate_plan(plan, source)
        self.assertEqual(plan["steps"][-1]["hidden_part_ids"], [])
        self.assertEqual(plan["excluded_part_ids"], ["aux"])


if __name__ == "__main__":
    unittest.main()
