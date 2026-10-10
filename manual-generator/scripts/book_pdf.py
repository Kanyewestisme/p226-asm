"""Portrait installation booklet with current STEP vectors and editable text.

The optional ``manual_document`` supplies product text; it never supplies model
geometry or certifies inferred assembly facts. Default booklets add three pages
to the configured steps: preparation, care, and sources / review notes.
"""
from __future__ import annotations

import hashlib
import html
import os
from pathlib import Path
import tempfile

from generic_render import plan_digest


WIDTH, HEIGHT = 595.276, 841.890
MARGIN = 42
BLACK, GRAY, RED = "#111111", "#565656", "#a7231b"


def _escaped(value) -> str:
    return html.escape(str(value)).replace("\n", "<br>")


def _paragraphs(values, *, bullets=False) -> str:
    return "".join(f"<p>{'· ' if bullets else ''}{_escaped(value)}</p>" for value in values)


def _text(page, rect, markup, *, size=11, color=BLACK, bold=False,
          field="text", minimum_scale=0.85, paragraph_gap=5):
    """Fit within a fixed region; fail before replacing any existing output."""
    result = page.insert_htmlbox(
        rect, markup,
        css=(f"* {{font-family: sans-serif; font-size:{size}pt; color:{color};"
             f"font-weight:{'bold' if bold else 'normal'}; line-height:1.32;}}"
             f"body {{margin:0;}} p {{margin:0 0 {paragraph_gap}pt 0;}} h3 {{margin:0 0 6pt 0;}}"),
        scale_low=minimum_scale,
    )
    if result[0] < 0:
        raise ValueError(
            f"PDF content overflow in {field}; shorten/split this content before exporting."
        )
    return result


def _plain(page, rect, value, **kwargs):
    return _text(page, rect, f"<p>{_escaped(value)}</p>", **kwargs)


def _asset_path(directory: Path, asset: dict, field: str) -> Path:
    name = asset.get("svg_path")
    if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name or ":" in name:
        raise ValueError(f"Invalid current SVG path for {field}")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory) or path.suffix.lower() != ".svg":
        raise ValueError(f"SVG path escapes current diagrams for {field}")
    if not path.is_file():
        raise FileNotFoundError(f"Missing current SVG for {field}: {path}")
    expected = asset.get("svg_sha256")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise ValueError(f"Changed diagram for {field}; render current STEP geometry again")
    return path


def _current_assets(plan: dict, manifest: dict, directory: Path) -> dict:
    source_hash = plan.get("source", {}).get("sha256")
    if not source_hash or manifest.get("source_sha256") != source_hash:
        raise ValueError("PDF manifest source SHA256 does not match the current plan")
    if manifest.get("plan_sha256") != plan_digest(plan):
        raise ValueError("PDF manifest plan SHA256 is stale; render the edited plan again")
    steps = plan.get("steps", [])
    if not steps or manifest.get("complete") is not True:
        raise ValueError("PDF requires a complete current diagram manifest and at least one step")
    entries = manifest.get("entries", [])
    ids = [entry.get("step_id", entry.get("id")) for entry in entries]
    if len(set(ids)) != len(ids) or set(ids) != {step["id"] for step in steps}:
        raise ValueError("PDF manifest must contain exactly one current entry per configured step")
    assets = {}
    for entry, sid in zip(entries, ids):
        if entry.get("status") != "ready":
            raise ValueError(f"PDF requires a ready current diagram for step {sid}")
        assets[sid] = {"entry": entry, "main": _asset_path(directory, entry, str(sid))}
        if isinstance(entry.get("focus"), dict):
            assets[sid]["focus"] = _asset_path(directory, entry["focus"], f"{sid} focus")
    cover = manifest.get("cover")
    if cover is not None:
        if not isinstance(cover, dict) or cover.get("status") != "ready":
            raise ValueError("PDF requires a ready current cover diagram")
        if cover.get("source_sha256") != source_hash:
            raise ValueError("PDF cover source SHA256 does not match the current plan")
        from cover_render import cover_plan
        if cover.get("cover_plan_sha256") != plan_digest(cover_plan(plan)):
            raise ValueError("PDF cover recipe SHA256 is stale; render the current cover scene again")
        assets["__cover__"] = {"entry": cover, "main": _asset_path(directory, cover, "cover scene")}
    return assets


def _vector(page, rect, path: Path):
    import fitz
    with fitz.open(path) as svg:
        with fitz.open("pdf", svg.convert_to_pdf()) as vector:
            # Fit the ink, including arrows, instead of the SVG's blank canvas.
            bounds = fitz.Rect()
            for drawing in vector[0].get_drawings():
                box = drawing["rect"]
                padding = max(1, (drawing.get("width") or 0) / 2)
                bounds |= fitz.Rect(box.x0-padding, box.y0-padding, box.x1+padding, box.y1+padding)
            clip = (fitz.Rect(bounds.x0-2, bounds.y0-2, bounds.x1+2, bounds.y1+2) & vector[0].rect) if not bounds.is_empty else vector[0].rect
            page.show_pdf_page(rect, vector, 0, clip=clip, keep_proportion=True)


def _page(doc, plan, title: str, number: int, total: int):
    import fitz
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    product = plan.get("manual_document", {}).get("title") or plan.get("product", {}).get("title", "安装说明")
    _plain(page, fitz.Rect(MARGIN, 23, 350, 41), product, size=9, color=GRAY, field="page header")
    _plain(page, fitz.Rect(364, 23, WIDTH-MARGIN, 41), title, size=9, color=GRAY, field="page section")
    page.draw_line((MARGIN, 43), (WIDTH-MARGIN, 43), color=(0.82, 0.82, 0.82), width=0.5)
    page.draw_line((MARGIN, 790), (WIDTH-MARGIN, 790), color=(0.82, 0.82, 0.82), width=0.5)
    status = "包装已确认 · 安装说明审核稿" if plan.get("status") == "confirmed" else "工程审核草稿 · 待包装确认"
    _plain(page, fitz.Rect(MARGIN, 800, 449, 823), status, size=9, color=RED, bold=True, field="review footer")
    _plain(page, fitz.Rect(490, 800, WIDTH-MARGIN, 823), f"{number:02d} / {total:02d}", size=9, color=GRAY, field="page number")
    return page


def _item_rows(items) -> list[str]:
    rows = []
    for item in items:
        quantity = item.get("quantity")
        count = "数量待确认" if quantity is None or quantity == "" else f"×{quantity}"
        rows.append(f"{item['label']} {count}")
    return rows


def _cover(doc, plan: dict, assets: dict, total: int):
    import fitz
    content = plan.get("manual_document", {})
    page = _page(doc, plan, "配件与安装准备", 1, total)
    title = content.get("title") or plan.get("product", {}).get("title", "安装说明")
    _plain(page, fitz.Rect(MARGIN, 65, WIDTH-MARGIN, 106), title, size=23, bold=True, field="manual_document.title")
    _plain(page, fitz.Rect(MARGIN, 110, WIDTH-MARGIN, 135), content.get("subtitle") or "工程审核草稿", size=16, color=RED, bold=True, field="manual_document.subtitle")
    model = content.get("model_label")
    if model:
        _plain(page, fitz.Rect(MARGIN, 143, WIDTH-MARGIN, 167), model, size=11, field="manual_document.model_label")
    introduction = content.get("introduction") or ["依据当前 STP 起草图示与步骤；建议分组、连接位置和安装顺序待包装同事修改确认。"]
    _text(page, fitz.Rect(MARGIN, 169, WIDTH-MARGIN, 210), _paragraphs(introduction), size=11, field="manual_document.introduction")
    explicit_cover = assets.get("__cover__")
    figure = explicit_cover or assets[plan["steps"][-1]["id"]]
    _vector(page, fitz.Rect(MARGIN, 215, WIDTH-MARGIN, 456), figure["main"])
    # An assembled final view is useful on an unconfigured draft, but cannot
    # stand in for an exploded parts view or imply a consumer parts inventory.
    caption = (content.get("cover_figure_caption") or "配件概览 | 当前 STP 衍生图") if explicit_cover else "总装概览 | 当前 STP 衍生图"
    _plain(page, fitz.Rect(MARGIN, 462, WIDTH-MARGIN, 482), caption, size=9, color=GRAY, field="manual_document.cover_figure_caption")
    _plain(page, fitz.Rect(MARGIN, 493, WIDTH-MARGIN, 522), "配件与工具", size=16, bold=True, field="cover parts heading")
    explicit_parts = content.get("parts")
    groups = plan.get("groups", [])
    if explicit_parts:
        rows = _item_rows(explicit_parts)
    elif len(groups) > 8 or sum(len(group["label"]) for group in groups) > 180:
        rows = [f"建议分组共 {len(groups)} 组，完整明细见逐步图示与步骤配置；包装配件名称、数量待核对。"]
    else:
        rows = _item_rows([{"label": f"建议分组：{group['label']}", "quantity": None} for group in groups])
    tools = _item_rows(content.get("tools", []))
    tools_text = "； ".join(tools) if tools else "工具清单与规格待确认。"
    # Separate cells keep familiar part labels and quantities together.
    columns = min(3, len(rows)) or 1
    table_rows = []
    for start in range(0, len(rows), columns):
        cells = rows[start:start+columns]
        table_rows.append("<tr>" + "".join(f'<td style="padding:0 8pt 4pt 0">{_escaped(row)}</td>' for row in cells) + "</tr>")
    parts_markup = '<table style="width:100%;border-collapse:collapse">' + "".join(table_rows) + "</table>" if rows else "<p>配件清单与数量待确认。</p>"
    markup = parts_markup + _paragraphs([tools_text])
    _text(page, fitz.Rect(MARGIN, 528, WIDTH-MARGIN, 621), markup, size=11, field="manual_document.parts/tools")
    _plain(page, fitz.Rect(MARGIN, 632, WIDTH-MARGIN, 661), "安装前准备", size=16, bold=True, field="preparation heading")
    preparation = content.get("preparation") or ["安装准备事项待包装同事补充确认。"]
    _text(page, fitz.Rect(MARGIN, 668, WIDTH-MARGIN, 724), _paragraphs(preparation, bullets=True), size=11, field="manual_document.preparation")
    notes = content.get("cover_notes") or ["配件名称、数量、工具及安装条件不能仅凭几何确定，请核对后完善。"]
    _text(page, fitz.Rect(MARGIN, 737, WIDTH-MARGIN, 781), _paragraphs(notes), size=9, color=RED, field="manual_document.cover_notes")


def _parts_text(plan: dict, step: dict) -> str:
    labels = {group["id"]: group["label"] for group in plan.get("groups", [])}
    # A CAD hierarchy can contain dozens of small instances. Listing every
    # cumulative group mistakes that hierarchy for a consumer bill of parts
    # and eventually crowds the instruction off the page.
    ids = list(dict.fromkeys(step.get("moving_groups", [])))
    if not ids:
        return f"完整 STP 总成（{len(labels)} 个建议组）" if step.get("assembled_groups") else "涉及部件待确认"
    if len(ids) > 6 or sum(len(labels.get(gid, gid)) for gid in ids) > 86:
        return f"本步新增建议分组 {len(ids)} 组（完整明细见步骤配置；包装配件名称待核对）"
    return "建议分组：" + "、".join(labels.get(gid, gid) for gid in ids)


def _step(doc, plan: dict, step: dict, asset: dict, number: int, total: int):
    import fitz
    page = _page(doc, plan, f"安装步骤 {number} / {len(plan['steps'])}", number+1, total)
    consumer = step.get("consumer", {})
    _plain(page, fitz.Rect(MARGIN, 66, WIDTH-MARGIN, 109), f"{number:02d}  {step['title']}", size=23, bold=True, field=f"step {step['id']} title", minimum_scale=0.65)
    _plain(page, fitz.Rect(MARGIN, 117, WIDTH-MARGIN, 149), "本步涉及：" + (consumer.get("parts_text") or _parts_text(plan, step)), size=10, color=GRAY, field=f"step {step['id']} parts")
    _plain(page, fitz.Rect(MARGIN, 155, WIDTH-MARGIN, 211), step.get("instruction", ""), size=12, field=f"step {step['id']} instruction")
    functions = plan.get("manual_document", {}).get("functions", []) if number == len(plan["steps"]) else []
    # The final overview has a separate consumer function block. Reserve its
    # space before placing either image; all other pages retain the large view.
    image_bottom = 585 if functions else 628
    image_rect = fitz.Rect(MARGIN, 222, WIDTH-MARGIN, image_bottom)
    if "focus" in asset:
        # Keep separate actual model geometry visible; never overlay an old PDF
        # crop or suggest that this inset proves unmodelled holes / fasteners.
        _vector(page, fitz.Rect(MARGIN, 222, 354, image_bottom), asset["main"])
        _vector(page, fitz.Rect(365, image_bottom-176, WIDTH-MARGIN, image_bottom-18), asset["focus"])
        _plain(page, fitz.Rect(365, image_bottom-14, WIDTH-MARGIN, image_bottom+2), "实际模型局部细节", size=8, color=GRAY, field=f"step {step['id']} detail caption")
    else:
        _vector(page, image_rect, asset["main"])
    caption = consumer.get("figure_caption") or f"{step['title']} | 当前 STP 衍生图"
    caption_top = 592 if functions else 635
    _plain(page, fitz.Rect(MARGIN, caption_top, WIDTH-MARGIN, caption_top+22), caption, size=9, color=GRAY, field=f"step {step['id']} figure_caption")
    hidden = step.get("hidden_part_ids", [])
    if hidden:
        _plain(page, fitz.Rect(MARGIN, caption_top+23, WIDTH-MARGIN, caption_top+39), f"图示临时隐藏 {len(hidden)} 个源实例用于观察；不表示已确认的拆装动作。", size=8, color=GRAY, field=f"step {step['id']} visibility caption")
    caution = consumer.get("caution", [])
    if functions:
        markup = "<h3>功能概览</h3>" + _paragraphs(functions)
        if caution:
            markup += "".join(f'<p style="color:{RED};font-weight:bold;">{_escaped(value)}</p>' for value in caution)
        _text(page, fitz.Rect(MARGIN, 624, WIDTH-MARGIN, 738), markup, size=10, field="manual_document.functions / final step caution")
    elif caution:
        _text(page, fitz.Rect(MARGIN, 673, WIDTH-MARGIN, 733), _paragraphs(caution), size=11, color=RED, bold=True, field=f"step {step['id']} caution")
    else:
        _plain(page, fitz.Rect(MARGIN, 678, WIDTH-MARGIN, 726), "操作注意事项待包装同事确认。", size=10, color=GRAY, field=f"step {step['id']} caution placeholder")
    notes = list(consumer.get("engineering_notes", step.get("warnings", [])))
    if asset["entry"].get("skipped_arrows"):
        notes.append("复位箭头投影过短而未显示，请调整视角或点位。")
    _text(page, fitz.Rect(MARGIN, 742, WIDTH-MARGIN, 780), _paragraphs(notes or ["核对图示位置、连接方式与操作顺序。"]), size=9, color=RED, field=f"step {step['id']} engineering_notes", minimum_scale=0.80)


def _care(doc, plan: dict, total: int):
    import fitz
    content = plan.get("manual_document", {})
    page = _page(doc, plan, "安全使用与日常保养", total-1, total)
    _plain(page, fitz.Rect(MARGIN, 66, WIDTH-MARGIN, 108), "安全使用与日常保养", size=23, bold=True, field="care title")
    sections = content.get("care_sections") or [{"title": "待补充确认", "items": ["安全使用、清洁与维护内容需按产品实际情况补充。当前 STP 不提供这些使用条件。"]}]
    markup = "".join(f"<h3>{_escaped(section['title'])}</h3>{_paragraphs(section.get('items', []), bullets=True)}<p> </p>" for section in sections)
    _text(page, fitz.Rect(MARGIN, 124, WIDTH-MARGIN, 700), markup, size=12, field="manual_document.care_sections")
    notes = content.get("care_notes") or ["以上使用说明待包装同事确认；不从几何推断维护周期、清洁材料或承载条件。"]
    _text(page, fitz.Rect(MARGIN, 729, WIDTH-MARGIN, 781), _paragraphs(notes), size=9, color=RED, field="manual_document.care_notes")


def _review(doc, plan: dict, manifest: dict, total: int):
    import fitz
    content = plan.get("manual_document", {})
    page = _page(doc, plan, "来源与待确认事项", total, total)
    _plain(page, fitz.Rect(MARGIN, 66, WIDTH-MARGIN, 107), content.get("review_title") or "来源与待确认事项", size=23, bold=True, field="manual_document.review_title")
    introduction = content.get("review_introduction") or []
    if isinstance(introduction, str):
        introduction = [introduction]
    item_top = 123
    if introduction:
        _text(page, fitz.Rect(MARGIN, 119, WIDTH-MARGIN, 174), _paragraphs(introduction), size=10, field="manual_document.review_introduction")
        item_top = 185
    items = list(content.get("review_items", []))
    if not items:
        items = [{"title": "分组、连接与顺序", "body": "自动建议需要包装同事修改确认。模型图形不证明安装行程、连接可靠性或可操作性。"},
                 {"title": "配件与使用文字", "body": "配件数量、工具规格、安全、功能、清洁和维护文字待补充确认。"}]
    if content.get("assembly_notes"):
        items.append({"title": "安装通用提示", "body": "\n".join(content["assembly_notes"])})
    summary = plan.get("analysis_summary", {})
    if summary:
        items.append({"title": "几何分析范围", "body": f"邻近候选已测试 {summary.get('tested_pairs', 0)}/{summary.get('candidate_pairs', 0)}，未测试 {summary.get('untested_pairs', 0)}；非实体叶实例 {summary.get('non_solid_instances', 0)}。检测不完整不能排除真实连接。"})
    excluded = plan.get("excluded_part_ids", [])
    if excluded:
        items.append({"title": "图中省略的源实例", "body": f"当前配置明确省略 {len(excluded)} 个实例。原因：{plan.get('exclusion_reason', '待确认')}"})
    markup = "".join(f"<h3>{i}  {_escaped(item['title'])}</h3><p>{_escaped(item['body'])}</p><p> </p>" for i, item in enumerate(items, 1))
    _text(page, fitz.Rect(MARGIN, item_top, WIDTH-MARGIN, 551), markup, size=10, field="manual_document.review_items")
    _plain(page, fitz.Rect(MARGIN, 560, WIDTH-MARGIN, 588), "来源", size=16, bold=True, field="sources heading")
    source = plan.get("source", {})
    name = source.get("path") or source.get("name") or source.get("filename") or "当前 STP（见项目来源配置）"
    sources = list(content.get("sources", []))
    text_sources = list(dict.fromkeys(item["source"] for item in content.get("parts", []) + content.get("tools", []) if item.get("source")))
    if text_sources:
        sources.append("配件与工具文字依据：" + "； ".join(text_sources))
    sources.append(f"几何来源：{name}")
    sources.append("所有部件图形均取自当前 STP；分开展示与箭头只表示模型复位提议。")
    for label, digest in (("STP SHA256", source["sha256"]), ("步骤配置 SHA256", manifest["plan_sha256"])):
        sources.append(f"{label}：\n{digest[:32]}\n{digest[32:]}")
    reference = content.get("reference")
    if isinstance(reference, dict):
        sources.append(f"版式参考：{reference.get('name', '已提供参考说明书')}")
        if reference.get("sha256"):
            digest = str(reference["sha256"])
            sources.append(f"参考 SHA256：\n{digest[:32]}\n{digest[32:]}")
    _text(page, fitz.Rect(MARGIN, 597, WIDTH-MARGIN, 781), _paragraphs(sources), size=8, color=GRAY, field="manual_document.sources", paragraph_gap=2)


def export_book_pdf(project: Path, plan: dict, manifest: dict, diagrams: Path | None = None) -> None:
    """Atomically write ``manual.pdf`` as n + 3 portrait A4 pages.

    Only a complete manifest matching this plan and its source is accepted.
    Every included SVG must still match its content digest. Source / review
    notes share one final page; excessive content fails with its field name
    instead of clipping or silently dropping paragraphs.
    """
    import fitz
    project = Path(project).resolve()
    directory = Path(diagrams or project / "diagrams").resolve()
    assets = _current_assets(plan, manifest, directory)
    total = len(plan["steps"]) + 3
    temporary = None
    with fitz.open() as doc:
        _cover(doc, plan, assets, total)
        for number, step in enumerate(plan["steps"], 1):
            _step(doc, plan, step, assets[step["id"]], number, total)
        _care(doc, plan, total)
        _review(doc, plan, manifest, total)
        doc.set_metadata({"title": plan.get("manual_document", {}).get("title") or plan.get("product", {}).get("title", "安装说明"),
                          "subject": "Current STEP-derived installation booklet; editable packaging review draft"})
        try:
            doc.subset_fonts()
        except RuntimeError:
            doc.subset_fonts(fallback=True)
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix="manual-book-", suffix=".tmp.pdf", dir=project)
            os.close(descriptor)
            temporary = Path(temporary_name)
            doc.save(temporary, garbage=4, deflate=True)
            temporary.replace(project / "manual.pdf")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
