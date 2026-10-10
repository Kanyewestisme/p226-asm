"""Render a configured exploded cover independently of installation steps."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from generic_render import _identifier, plan_digest, render_plan


def cover_plan(plan: dict) -> dict | None:
    """Derive one source-bound cover scene without modifying the manual plan."""
    if not isinstance(plan, dict):
        raise ValueError("Plan must be an object")
    scene = plan.get("cover_scene")
    if scene is None:
        return None
    if not isinstance(scene, dict):
        raise ValueError("cover_scene must be a normal step object")
    cover_id = _identifier(scene.get("id"), "cover")
    cover_names = {cover_id.lower(), (cover_id + "_focus").lower()}
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("Installation steps must be a list")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Each installation step must be an object")
        step_id = _identifier(step.get("id"), "step")
        if cover_names.intersection({step_id.lower(), (step_id + "_focus").lower()}):
            raise ValueError("Cover asset ID collides with an installation step or focus filename")
    derived = deepcopy(plan)
    derived["steps"] = [deepcopy(scene)]
    derived.pop("cover_scene")
    return derived


def render_cover(plan: dict, inventory: Path, output: Path) -> dict | None:
    """Return the actual-model cover entry and its separate source/recipe hashes.

    The output is a fresh cover directory. Paths are filenames relative to that
    directory, ready for the caller to publish separately from numbered steps.
    Source surfaces are retained unless the plan explicitly excludes their IDs.
    """
    derived = cover_plan(plan)
    if derived is None:
        return None
    manifest = render_plan(derived, inventory, output)
    return {
        **manifest["entries"][0],
        "source_sha256": manifest["source_sha256"],
        "cover_plan_sha256": plan_digest(derived),
    }
