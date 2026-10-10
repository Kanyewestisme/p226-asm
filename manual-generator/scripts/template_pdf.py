"""Keep a verified source PDF and replace only its configured step figures.

The template owns all consumer wording, typography, cover, QR codes, parts and
function graphics. This module overlays configured white masks and current
STEP-derived SVG vectors. It does not lay out or rewrite any template text.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager

from book_pdf import _current_assets, _vector


@contextmanager
def editable_template(path):
    """Copy visual pages before editing, leaving Illustrator private data out.

    Some supplied PDFs have malformed private objects. A late lazy repair while
    saving them can silently discard added content. Graft pages into a fresh
    document first so the original file stays untouched and overlays persist.
    """
    import fitz
    with fitz.open(path) as source, fitz.open() as editable:
        editable.insert_pdf(source, widgets=True)
        editable.set_metadata(source.metadata)
        yield editable


def manual_steps(plan: dict) -> list[dict]:
    """Source-complete inspection scenes may stay in the app without printing."""
    return [step for step in plan.get("steps", []) if step.get("include_in_manual", True)]


def validate_template_binding(plan: dict) -> dict | None:
    """Validate fixed-template content bindings without reading or writing files.

    The editor must keep these bindings tied to the explicitly selected source
    template. Rebinding is an explicit operation, not a side effect of editing
    a title, instruction, consumer note, document text or step order.
    Plans without a selected PDF template can still be edited as drafts.
    """
    if not isinstance(plan, dict):
        raise ValueError("Template binding requires a plan object")
    config = plan.get("pdf_template")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError("plan.pdf_template must be a template configuration object")
    source_hash = plan.get("source", {}).get("sha256")
    if not source_hash or config.get("source_sha256") != source_hash:
        raise ValueError("PDF template source SHA256 does not match the current STEP source")
    raw_path = config.get("path")
    if not isinstance(raw_path, str) or not raw_path or not Path(raw_path).is_absolute():
        raise ValueError("pdf_template.path must be an absolute path to the explicitly supplied original PDF")
    template_hash = config.get("sha256")
    if not isinstance(template_hash, str) or len(template_hash) != 64 or any(character not in "0123456789abcdef" for character in template_hash):
        raise ValueError("pdf_template.sha256 must contain the original PDF's lowercase SHA256")
    expected_pages = config.get("page_count", 2)
    if isinstance(expected_pages, bool) or not isinstance(expected_pages, int) or expected_pages < 1:
        raise ValueError("pdf_template.page_count must be a positive page count")
    steps, bindings = manual_steps(plan), config.get("step_bindings")
    if not isinstance(steps, list) or not steps or any(not isinstance(step, dict) or not isinstance(step.get("id"), str) for step in steps):
        raise ValueError("Template binding requires the current configured steps")
    if not isinstance(bindings, list) or any(not isinstance(binding, dict) or not isinstance(binding.get("step_id"), str) for binding in bindings):
        raise ValueError("pdf_template.step_bindings must explicitly bind the original step text and order")
    step_ids = [step["id"] for step in steps]
    binding_ids = [binding["step_id"] for binding in bindings]
    if len(set(step_ids)) != len(step_ids) or binding_ids != step_ids:
        raise ValueError("Fixed PDF template step IDs/order changed; select and explicitly bind a matching template before editing the sequence")
    for step, binding in zip(steps, bindings):
        sid = step["id"]
        for field in ("title", "instruction"):
            if not isinstance(binding.get(field), str) or binding[field] != step.get(field):
                raise ValueError(f"Fixed PDF template step {sid} {field} changed; template consumer text must remain matched")
        consumer = step.get("consumer", {})
        bound_consumer = binding.get("consumer", {})
        if not isinstance(consumer, dict) or not isinstance(bound_consumer, dict) or consumer != bound_consumer:
            raise ValueError(f"Fixed PDF template step {sid} consumer text changed or is not bound; template wording would otherwise ignore this edit")
    if "document_binding" in config and config["document_binding"] != plan.get("manual_document", {}):
        raise ValueError("Fixed PDF template document text changed; template wording must remain matched")
    slots = config.get("figure_slots")
    if not isinstance(slots, list) or any(not isinstance(slot, dict) or not isinstance(slot.get("step_id"), str) for slot in slots):
        raise ValueError("pdf_template.figure_slots must explicitly map the bound step IDs")
    slot_ids = [slot["step_id"] for slot in slots]
    if len(set(slot_ids)) != len(slot_ids) or set(slot_ids) != set(step_ids):
        raise ValueError("pdf_template.figure_slots must bind exactly one region to every current step ID")
    return config


def validate_template_source(project: Path, plan: dict) -> Path:
    """Read only the selected original template and verify its content hash."""
    config = validate_template_binding(plan)
    if config is None:
        raise ValueError("The original-template PDF export requires plan.pdf_template")
    template = Path(config["path"]).resolve()
    project = Path(project).resolve()
    if not template.is_file():
        raise FileNotFoundError(f"Missing original PDF template: {template}")
    if template == project / "manual.pdf":
        raise ValueError("The output must differ from the original PDF template")
    digest = hashlib.sha256(template.read_bytes()).hexdigest()
    if config["sha256"] != digest:
        raise ValueError("Original template SHA256 differs; recalibrate and bind its figure regions before exporting")
    return template


def _page_number(value, total: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= total:
        raise ValueError(f"{field} must identify an existing template page using one-based numbering")
    return value - 1


def _rect(value, page_rect, field: str):
    import fitz
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(
        isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number)
        for number in value
    ):
        raise ValueError(f"{field} must contain four finite PDF point coordinates")
    x0, y0, x1, y1 = value
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"{field} must have positive width and height")
    rect = fitz.Rect(value)
    if not page_rect.contains(rect):
        raise ValueError(f"{field} must stay within its original template page")
    return rect


def _template_text_rects(page):
    import fitz
    return [
        fitz.Rect(span["bbox"])
        for block in page.get_text("dict")["blocks"] if "lines" in block
        for line in block["lines"]
        for span in line["spans"] if span.get("text", "").strip()
    ]


def _protect_text(rect, text_rects: list, field: str):
    if any(rect.intersects(original) for original in text_rects):
        raise ValueError(f"{field} overlaps original template text; recalibrate the figure-only region")


def _regions(config: dict, doc, plan: dict) -> tuple[list, dict | None]:
    slots = config.get("figure_slots")
    if not isinstance(slots, list) or not slots:
        raise ValueError("pdf_template.figure_slots must contain the explicitly calibrated figure regions")
    step_ids = [step["id"] for step in manual_steps(plan)]
    slot_ids = [slot.get("step_id") if isinstance(slot, dict) else None for slot in slots]
    if len(slot_ids) != len(set(slot_ids)) or set(slot_ids) != set(step_ids):
        raise ValueError("pdf_template.figure_slots must bind exactly one region to every current step ID")
    text_by_page = {}
    result = []
    for slot in slots:
        sid = slot["step_id"]
        page_number = _page_number(slot.get("page"), len(doc), f"figure slot {sid} page")
        page = doc[page_number]
        text = text_by_page.setdefault(page_number, _template_text_rects(page))
        target = _rect(slot.get("rect"), page.rect, f"figure slot {sid} rect")
        _protect_text(target, text, f"figure slot {sid} rect")
        raw_masks = slot.get("safe_white_masks_pt")
        if not isinstance(raw_masks, list) or not raw_masks:
            raise ValueError(f"figure slot {sid} must contain its calibrated safe_white_masks_pt")
        masks = []
        for index, raw in enumerate(raw_masks):
            mask = _rect(raw, page.rect, f"figure slot {sid} mask {index+1}")
            _protect_text(mask, text, f"figure slot {sid} mask {index+1}")
            masks.append(mask)
        # Separate steps must not paint over each other's replacements.
        for other in result:
            if other["page"] == page_number and any(
                rect.intersects(previous)
                for rect in [target, *masks]
                for previous in [other["target"], *other["masks"]]
            ):
                raise ValueError(f"figure slot {sid} overlaps another step's replacement region")
        result.append({"step_id": sid, "page": page_number, "target": target, "masks": masks})
    notes = config.get("notes")
    if notes is None:
        return result, None
    if not isinstance(notes, dict):
        raise ValueError("pdf_template.notes must be a configured notes object")
    page_number = _page_number(notes.get("page", len(doc)), len(doc), "pdf_template.notes page")
    page = doc[page_number]
    rect = _rect(notes.get("rect"), page.rect, "pdf_template.notes rect")
    _protect_text(rect, text_by_page.setdefault(page_number, _template_text_rects(page)), "pdf_template.notes rect")
    if any(region["page"] == page_number and any(rect.intersects(other) for other in [region["target"], *region["masks"]]) for region in result):
        raise ValueError("pdf_template.notes rect overlaps a step replacement region")
    lines = notes.get("lines", [])
    if not isinstance(lines, list):
        raise ValueError("pdf_template.notes lines must be a list")
    line_height = notes.get("line_height", 13)
    if isinstance(line_height, bool) or not isinstance(line_height, (float, int)) or not math.isfinite(line_height) or line_height <= 0:
        raise ValueError("pdf_template.notes line_height must be positive and finite")
    import fitz
    parsed = []
    baseline = rect.y0+12
    for index, line in enumerate(lines):
        if not isinstance(line, dict) or not isinstance(line.get("text"), str) or "\n" in line["text"] or "\r" in line["text"]:
            raise ValueError("pdf_template.notes lines must contain explicit single-line text")
        size = line.get("size", 7.4)
        if isinstance(size, bool) or not isinstance(size, (float, int)) or not math.isfinite(size) or not 1 <= size <= 72:
            raise ValueError("pdf_template.notes font size must be between 1 and 72 PDF points")
        if baseline-size < rect.y0 or baseline+size*0.3 > rect.y1 or fitz.get_text_length(line["text"], fontname="china-s", fontsize=size) > rect.width:
            raise ValueError(f"PDF content overflow in pdf_template.notes line {index+1}; shorten/split the configured notes")
        parsed.append({"text": line["text"], "size": size, "baseline": baseline})
        baseline += line_height
    return result, {"page": page_number, "rect": rect, "lines": parsed}


def validate_template_regions(plan: dict, doc) -> tuple[list, dict | None]:
    """Read original page dimensions / text to verify calibrated regions only."""
    config = validate_template_binding(plan)
    if config is None:
        raise ValueError("The original-template PDF export requires plan.pdf_template")
    if not doc.is_pdf or doc.needs_pass:
        raise ValueError("The supplied template must be a readable PDF")
    if len(doc) != config.get("page_count", 2):
        raise ValueError("Original template page count differs from pdf_template.page_count")
    return _regions(config, doc, plan)


def export_template_pdf(project: Path, plan: dict, manifest: dict, diagrams: Path | None = None) -> None:
    """Atomically export ``manual.pdf`` while retaining the original template.

    The fixed source, step order and original consumer titles / instructions
    must remain bound. This exporter only places explicitly mapped figure SVGs
    and optional explicitly supplied notes. All pages and regions are validated
    before any replacement is painted.
    """
    import fitz
    project = Path(project).resolve()
    template = validate_template_source(project, plan)
    directory = Path(diagrams or project / "diagrams").resolve()
    assets = _current_assets(plan, manifest, directory)
    temporary = None
    with editable_template(template) as doc:
        regions, notes = validate_template_regions(plan, doc)
        for region in regions:
            page = doc[region["page"]]
            for mask in region["masks"]:
                page.draw_rect(mask, color=None, fill=(1, 1, 1), overlay=True)
            _vector(page, region["target"], assets[region["step_id"]]["main"])
        if notes:
            page = doc[notes["page"]]
            for line in notes["lines"]:
                page.insert_text((notes["rect"].x0, line["baseline"]), line["text"], fontname="china-s", fontsize=line["size"], color=(0.35, 0.35, 0.35), overlay=True)
        # Retain every source page, its embedded text/fonts/graphics and its
        # existing metadata. No source text is redacted or re-laid out.
        try:
            descriptor, name = tempfile.mkstemp(prefix="manual-template-", suffix=".tmp.pdf", dir=project)
            os.close(descriptor)
            temporary = Path(name)
            doc.save(temporary, garbage=3, deflate=True)
            temporary.replace(project / "manual.pdf")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
