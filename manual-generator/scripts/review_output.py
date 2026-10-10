"""Editable previews and exports using only the explicitly supplied PDF template."""
from __future__ import annotations

import html
import json
import hashlib
from pathlib import Path


def html_page(plan: dict, manifest: dict | None, editable: bool = False, token: str = "", part_catalog: list | None = None, pdf_ready: bool = False, assembly_suggestions: list | None = None) -> str:
    suggestions = assembly_suggestions if assembly_suggestions is not None else plan.get("assembly_review", {}).get("suggestions", [])
    payload = json.dumps({"plan": plan, "manifest": manifest, "editable": editable, "token": token, "part_catalog": part_catalog or [], "pdf_ready": pdf_ready, "assembly_suggestions": suggestions}, ensure_ascii=False).replace("<", "\\u003c")
    template = (Path(__file__).parent / "review.html").read_text(encoding="utf-8")
    return template.replace("/*__STATE__*/", payload)


def _text(page, rect, text, size=10, color=(0.17, 0.19, 0.22)):
    import fitz
    # insert_htmlbox performs wrapping and fitting and includes CJK font fallback.
    escaped = html.escape(str(text)).replace("\n", "<br>")
    result = page.insert_htmlbox(rect, f"<div>{escaped}</div>", css=f"* {{font-family: sans-serif; font-size: {size}pt; color: rgb({int(color[0]*255)}, {int(color[1]*255)}, {int(color[2]*255)});}}", scale_low=0.65)
    if result[0] < 0:
        raise ValueError("Text does not fit PDF layout; shorten the instruction or warnings.")


def export_landscape_review_pdf(project: Path, plan: dict, manifest: dict, diagrams: Path | None = None):
    import fitz
    entries = {entry.get("step_id", entry.get("id")): entry for entry in manifest["entries"]}
    doc = fitz.open()
    try:
        for number, step in enumerate(plan["steps"], 1):
            page = doc.new_page(width=842, height=595)
            page.draw_rect(fitz.Rect(0, 0, 842, 77), fill=(0.96, 0.97, 0.98), color=None)
            _text(page, fitz.Rect(30, 17, 680, 48), f"{number:02d}  {step['title']}", 19)
            status = "包装已确认" if plan.get("status") == "confirmed" else "待包装确认"
            _text(page, fitz.Rect(688, 22, 812, 55), status, 11, (0.72, 0.23, 0.12))
            _text(page, fitz.Rect(31, 53, 670, 75), plan["product"]["title"], 9)
            entry = entries[step["id"]]
            source = (diagrams or project / "diagrams") / entry["svg_path"]
            with fitz.open(source) as svg:
                with fitz.open("pdf", svg.convert_to_pdf()) as vector:
                    page.show_pdf_page(fitz.Rect(25, 89, 566, 518), vector, 0, keep_proportion=True)
            page.draw_line((587, 100), (587, 520), color=(0.85, 0.87, 0.89), width=0.7)
            _text(page, fitz.Rect(609, 100, 811, 126), "步骤说明", 12)
            _text(page, fitz.Rect(609, 135, 811, 278), step["instruction"], 11)
            _text(page, fitz.Rect(609, 294, 811, 320), "需要核对", 12, (0.72, 0.23, 0.12))
            warnings = list(step.get("warnings", []))
            if entry.get("skipped_arrows"):
                warnings.append("有复位箭头投影过短而未显示，请换角度或调整箭头点位。")
            warning_text = "\n\n".join(str(warning) for warning in warnings) or "核对图示、文字与实际包装安装步骤。"
            _text(page, fitz.Rect(609, 329, 811, 508), warning_text, 9)
            summary = plan.get("analysis_summary", {})
            if summary:
                hidden = len(plan.get("excluded_part_ids", []))
                _text(page, fitz.Rect(30, 526, 800, 544), f"邻近候选已测试 {summary['tested_pairs']}/{summary['candidate_pairs']}，未测试 {summary['untested_pairs']}；模型有 {summary['non_solid_instances']} 个非实体叶实例，当前配置明确隐藏 {hidden} 个源实例。", 8)
            _text(page, fitz.Rect(30, 545, 800, 563), "箭头为模型复位示意；显示分离距离和方向不等于安装行程。所有零件图形均取自当前 STP。", 8)
            _text(page, fitz.Rect(30, 563, 775, 583), f"STP {plan['source']['sha256'][:16]}  |  配置 {manifest['plan_sha256'][:16]}", 8, (0.45, 0.46, 0.48))
            page.insert_text((796, 577), str(number), fontname="helv", fontsize=9)
        doc.set_metadata({"title": plan["product"]["title"], "subject": "STEP-derived editable installation draft; source-bound packaging review"})
        temp = project / "manual.tmp.pdf"
        # HTML text boxes otherwise embed many complete CJK font copies.
        # Subsetting keeps vector geometry/text and produces a portable file.
        try:
            doc.subset_fonts()
        except RuntimeError:
            doc.subset_fonts(fallback=True)
        doc.save(temp, garbage=4, deflate=True)
        temp.replace(project / "manual.pdf")
    finally:
        doc.close()


def export_pdf(project: Path, plan: dict, manifest: dict, diagrams: Path | None = None):
    """Preserve the supplied cover and installation sheet; replace mapped figures."""
    from template_pdf import export_template_pdf
    from generic_render import plan_digest
    export_template_pdf(project, plan, manifest, diagrams=diagrams)
    receipt = {
        "source_sha256": plan["source"]["sha256"], "plan_sha256": plan_digest(plan),
        "template_sha256": plan["pdf_template"]["sha256"],
        "pdf_sha256": hashlib.sha256((project / "manual.pdf").read_bytes()).hexdigest(),
        "mode": "original_template_figure_replacement",
    }
    temp = project / "pdf_export.json.tmp"
    temp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(project / "pdf_export.json")


def is_current_pdf(project: Path, plan: dict) -> bool:
    """An old redesigned PDF must never be offered as the template result."""
    from generic_render import plan_digest
    from template_pdf import validate_template_source
    path, record = project / "manual.pdf", project / "pdf_export.json"
    if not plan.get("pdf_template") or not path.is_file() or not record.is_file():
        return False
    try:
        validate_template_source(project, plan)
        receipt = json.loads(record.read_text(encoding="utf-8"))
        return (receipt.get("mode") == "original_template_figure_replacement"
                and receipt.get("source_sha256") == plan["source"]["sha256"]
                and receipt.get("plan_sha256") == plan_digest(plan)
                and receipt.get("template_sha256") == plan["pdf_template"]["sha256"]
                and receipt.get("pdf_sha256") == hashlib.sha256(path.read_bytes()).hexdigest())
    except (ValueError, OSError, KeyError):
        return False


def export_preview(project: Path, plan: dict, manifest: dict, diagrams: Path | None = None, inventory: Path | None = None):
    project = project.resolve()
    inventory = inventory or project / "inventory"
    catalog_path = inventory / "assembly_parts.json"
    catalog = [{key: row.get(key) for key in ("id", "name", "index", "solids", "faces")} for row in json.loads(catalog_path.read_text(encoding="utf-8"))] if catalog_path.is_file() else []
    instructions = {
        "source_sha256": plan["source"]["sha256"], "plan_sha256": manifest["plan_sha256"],
        "status": plan["status"], "title": plan.get("manual_document", {}).get("title", plan["product"]["title"]),
        "manual_document": plan.get("manual_document", {}),
        "pdf_mode": "original_template_figure_replacement" if plan.get("pdf_template") else "template_not_bound",
        "steps": [{"id": step["id"], "title": step["title"], "instruction": step["instruction"], "consumer": step.get("consumer", {}), "warnings": step.get("warnings", [])} for step in plan["steps"]],
    }
    (project / "instructions.json").write_text(json.dumps(instructions, ensure_ascii=False, indent=2), encoding="utf-8")
    # Missing old instructions must never prevent a STEP-only draft. Its PDF
    # waits for an explicit template binding rather than inventing a layout.
    if plan.get("pdf_template"):
        export_pdf(project, plan, manifest, diagrams=diagrams)
    (project / "index.html").write_text(html_page(plan, manifest, part_catalog=catalog, pdf_ready=bool(plan.get("pdf_template"))), encoding="utf-8")
