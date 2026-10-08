"""Local CAD helpers: assembly-wide hidden-line removal with explicit camera up."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Sequence

import cadquery as cq
from cadquery.occ_impl.exporters.svg import getPaths
from OCP.BRepLib import BRepLib
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape


def hidden_line_svg(
    shape: cq.Shape,
    camera: Sequence[float] = (1.5, -2.0, 1.4),
    up: Sequence[float] = (0.0, 0.0, 1.0),
    width: float = 900,
    height: float = 600,
    margin: float = 45,
    show_hidden: bool = False,
    caption: str = "",
    poly: bool = False,
) -> tuple[str, dict]:
    """Project one complete compound so parts hide each other correctly.

    `camera` points from the model toward the viewer. `up` selects page up.
    Camera and up must not be parallel; for top views use a Y-axis up.
    This does not infer product orientation, part mapping, or assembly order.
    """
    cam = cq.Vector(*camera).normalized()
    right_raw = cq.Vector(*up).cross(cam)
    if right_raw.Length < 1e-8:
        raise ValueError("Camera and up are parallel; choose another up vector")
    right = right_raw.normalized()
    page_up = cam.cross(right).normalized()
    coordinate_system = gp_Ax2(gp_Pnt(), gp_Dir(*cam.toTuple()), gp_Dir(*right.toTuple()))
    if poly:
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.HLRBRep import HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape
        BRepMesh_IncrementalMesh(shape.wrapped,0.20,False,0.20,True).Perform()
        hlr=HLRBRep_PolyAlgo();hlr.Load(shape.wrapped)
        hlr.Projector(HLRAlgo_Projector(coordinate_system));hlr.Update()
        result=HLRBRep_PolyHLRToShape();result.Update(hlr);result.Hide()
    else:
        hlr = HLRBRep_Algo()
        hlr.Add(shape.wrapped)
        hlr.Projector(HLRAlgo_Projector(coordinate_system))
        hlr.Update()
        hlr.Hide()
        result = HLRBRep_HLRToShape(hlr)
    visible_ocp = [result.VCompound(), result.Rg1LineVCompound(), result.OutLineVCompound()]
    hidden_ocp = [result.HCompound(), result.OutLineHCompound()]
    visible, hidden = [], []
    for raw, target in [(r, visible) for r in visible_ocp] + [(r, hidden) for r in hidden_ocp]:
        if not raw.IsNull():
            BRepLib.BuildCurves3d_s(raw, 1e-6)
            target.append(cq.Shape(raw))
    if not visible:
        raise ValueError("Hidden-line projection produced no visible edges")
    hidden_paths, visible_paths = getPaths(visible, hidden)
    bounds = cq.Compound.makeCompound(visible + hidden).BoundingBox()
    if bounds.xlen <= 0 or bounds.ylen <= 0:
        raise ValueError("Degenerate projection")
    scale = min((width - 2 * margin) / bounds.xlen, (height - 2 * margin) / bounds.ylen)
    tx = width / 2 - scale * (bounds.xmin + bounds.xmax) / 2
    ty = height / 2 + scale * (bounds.ymin + bounds.ymax) / 2
    stroke = 1.0 / scale
    paths = []
    if show_hidden:
        paths.append(f'<g stroke="#aaaaaa" stroke-dasharray="{3/scale},{3/scale}">')
        paths += [f'<path d="{p}"/>' for p in hidden_paths]
        paths.append("</g>")
    paths.append('<g stroke="#000000">')
    paths += [f'<path d="{p}"/>' for p in visible_paths]
    paths.append("</g>")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><title>{html.escape(caption)}</title>'
        f'<g fill="none" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-linejoin="round" transform="translate({tx},{ty}) scale({scale},{-scale})">'
        + "".join(paths) + "</g></svg>"
    )
    info = {
        "camera": cam.toTuple(), "right": right.toTuple(), "page_up": page_up.toTuple(),
        "scale": scale, "translate_x": tx, "translate_y": ty,
        "visible_paths": len(visible_paths), "hidden_paths": len(hidden_paths),
        "width": width, "height": height, "show_hidden": show_hidden,
        "algorithm": "OCP polygonal HLR at 0.20 mm deflection / 0.20 rad" if poly else "OCP exact BRep HLR",
    }
    return svg, info


def project_point(point: Sequence[float], projection: dict) -> tuple[float, float]:
    vector = cq.Vector(*point)
    return (
        projection["translate_x"] + projection["scale"] * vector.dot(cq.Vector(*projection["right"])),
        projection["translate_y"] - projection["scale"] * vector.dot(cq.Vector(*projection["page_up"])),
    )
