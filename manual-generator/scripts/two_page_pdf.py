"""Two-sheet A3 installation manual from current STEP-derived vector assets.

The first sheet is the product cover. The second keeps every configured step,
its instruction and consumer caution on one installation sheet. Engineering
review details remain in the editable project / preview, with short markers on
the sheet. Excess content fails explicitly instead of changing the page count.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from book_pdf import (
    BLACK, GRAY, RED, _current_assets, _escaped, _item_rows, _paragraphs,
    _parts_text, _plain, _text, _vector,
)


WIDTH, HEIGHT = 1190.551, 841.890
MAX_STEPS = 9
MARGIN = 26


def _footer(page, plan: dict, number: int):
    import fitz
    status = "包装已确认" if plan.get("status") == "confirmed" else "待包装确认"
    label = f"{status} · 当前 STP 图示；未确定的连接细项见项目预览。"
    page.draw_line((MARGIN, 807), (WIDTH-MARGIN, 807), color=(0.78, 0.78, 0.78), width=0.5)
    _plain(page, fitz.Rect(MARGIN, 814, 1080, 835), label, size=8.5, color=RED, field="two-page review footer", minimum_scale=1)
    _plain(page, fitz.Rect(1102, 814, WIDTH-MARGIN, 835), f"{number} / 2", size=8.5, color=GRAY, field="two-page page number", minimum_scale=1)


def _hidden_marker(step: dict, asset: dict) -> str:
    hidden = step.get("hidden_part_ids") or asset["entry"].get("hidden_part_ids") or []
    return "图示临时隐藏部分模型实例，不表示实物拆装。" if hidden else ""


def _cover(doc, plan: dict, assets: dict):
    import fitz
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    page.draw_rect(page.rect, color=None, fill=(0.72, 0.73, 0.74))
    content = plan.get("manual_document", {})
    title = content.get("title") or plan.get("product", {}).get("title", "安装说明")
    brand = content.get("brand")
    if brand:
        _plain(page, fitz.Rect(43, 34, 570, 94), brand, size=35, bold=True, field="manual_document.brand", minimum_scale=0.8)
    _plain(page, fitz.Rect(43, 110 if brand else 43, 578, 223), title, size=30, bold=True, field="two-page cover title", minimum_scale=0.8)
    subtitle = content.get("subtitle") or "安装说明"
    _plain(page, fitz.Rect(45, 230, 570, 265), subtitle, size=16, color=BLACK, field="manual_document.subtitle", minimum_scale=0.9)
    # A supplied historical introduction may describe its original sequence.
    # This sheet must describe the edited sequence that it actually renders.
    introduction = [f"本册按当前步骤配置顺序编排，共 {len(plan['steps'])} 步安装说明。",
                    "安装图形仅取自当前 STP；未确定的位置与操作要点见图示标记。"]
    _text(page, fitz.Rect(45, 281, 575, 377), _paragraphs(introduction), size=13, color=GRAY, field="two-page current sequence introduction", minimum_scale=0.9, paragraph_gap=3)
    model = content.get("cover_model") or content.get("model_label") or title
    # Strip an editorial field prefix, retaining its supplied model value.
    # No previous product name, brand or model is used as a default.
    if "：" in model:
        model = model.split("：", 1)[1].strip()
    _plain(page, fitz.Rect(40, 435, 600, 684), model, size=64, bold=True, field="two-page cover model", minimum_scale=0.85)
    final = assets[plan["steps"][-1]["id"]]
    _vector(page, fitz.Rect(620, 52, 1148, 742), final["main"])
    caption = "总装外观 | 当前 STP 衍生图"
    _plain(page, fitz.Rect(636, 758, 1148, 781), caption, size=10, color=BLACK, field="two-page cover figure caption", minimum_scale=1)
    hide = _hidden_marker(plan["steps"][-1], final)
    if hide:
        _plain(page, fitz.Rect(45, 747, 599, 781), hide, size=10, color=RED, field="two-page cover visibility", minimum_scale=1)
    _footer(page, plan, 1)


def _parts_band(page, plan: dict):
    import fitz
    content = plan.get("manual_document", {})
    page.draw_rect(fitz.Rect(MARGIN, 59, WIDTH-MARGIN, 126), color=None, fill=(0.95, 0.95, 0.95))
    _plain(page, fitz.Rect(41, 68, 131, 95), "配件与工具", size=12, bold=True, field="two-page parts heading", minimum_scale=1)
    parts = content.get("parts")
    if parts:
        parts_text = "； ".join(_item_rows(parts))
    else:
        parts_text = f"包装配件名称、数量待核对；模型建议分组共 {len(plan.get('groups', []))} 组，完整明细见步骤配置。"
    tools = content.get("tools")
    tools_text = "； ".join(_item_rows(tools)) if tools else "工具与紧固件规格待确认。"
    _plain(page, fitz.Rect(145, 67, WIDTH-43, 99), parts_text, size=10, field="manual_document.parts", minimum_scale=0.9)
    _plain(page, fitz.Rect(145, 101, WIDTH-43, 121), "工具：" + tools_text, size=10, color=GRAY, field="manual_document.tools", minimum_scale=0.9)


def _sidebar(page, plan: dict):
    import fitz
    content = plan.get("manual_document", {})
    page.draw_rect(fitz.Rect(MARGIN, 141, 285, 799), color=None, fill=(0.94, 0.94, 0.94))
    sections = [{"title": "安装前准备", "items": content.get("preparation") or ["准备事项待包装同事补充确认。"]}]
    care = content.get("care_sections") or [{"title": "安全使用与日常保养", "items": ["使用条件、安全、清洁与保养内容待按产品实际情况确认；STP 不提供这些使用信息。"]}]
    sections.extend(care)
    if content.get("assembly_notes"):
        sections.append({"title": "安装提示", "items": content["assembly_notes"]})
    sections.append({"title": "功能概览", "items": content.get("functions") or ["功能及操作方式待补充确认。"]})
    markup = "".join(
        f'<h3 style="font-size:11.5pt;">{_escaped(section["title"])}</h3>'
        + _paragraphs(section.get("items", []), bullets=True) + "<p> </p>"
        for section in sections
    )
    _text(page, fitz.Rect(40, 154, 272, 760), markup, size=9.3, field="two-page preparation/care/functions", minimum_scale=0.92, paragraph_gap=2)
    # Review items, source hashes and long conflict explanations belong to the
    # editable review view. The consumer sheet keeps a short visible marker.
    marker = "使用与功能文字待核对；工程细项见项目预览。" if content.get("care_notes") or content.get("review_items") else "安装步骤及使用文字待包装确认。"
    if plan.get("status") == "confirmed":
        marker = "请结合产品实际版本使用本安装说明。"
    _plain(page, fitz.Rect(40, 770, 272, 796), marker, size=8.2, color=RED, field="two-page sidebar review marker", minimum_scale=1)


def _step_card(page, plan: dict, step: dict, asset: dict, rect, number: int):
    import fitz
    x0, y0, x1, y1 = rect
    left, right = x0+10, x1-10
    page.draw_rect(rect, color=(0.86, 0.86, 0.86), fill=(1, 1, 1), width=0.55)
    consumer = step.get("consumer", {})
    _plain(page, fitz.Rect(left, y0+6, right, y0+28), f"{number:02d}  {step['title']}", size=14, bold=True, field=f"two-page step {step['id']} title", minimum_scale=0.8, paragraph_gap=0)
    parts = consumer.get("parts_text") or _parts_text(plan, step)
    _plain(page, fitz.Rect(left, y0+30, right, y0+44), "涉及：" + parts, size=8.2, color=GRAY, field=f"two-page step {step['id']} parts", minimum_scale=1, paragraph_gap=0)
    large_overview = number == len(plan["steps"]) and len(step.get("instruction", "")) <= 20 and not consumer.get("caution") and "focus" not in asset
    main_rect = fitz.Rect(left, y0+47, right, y0+(181 if large_overview else 132))
    if "focus" in asset:
        main_rect.x1 = x1-94
        _vector(page, main_rect, asset["main"])
        _vector(page, fitz.Rect(x1-85, y0+57, right, y0+116), asset["focus"])
        _plain(page, fitz.Rect(x1-87, y0+118, right, y0+133), "实际模型局部", size=7.2, color=GRAY, field=f"two-page step {step['id']} focus label", minimum_scale=1, paragraph_gap=0)
    else:
        _vector(page, main_rect, asset["main"])
    body = f"<p>{_escaped(step.get('instruction', ''))}</p>"
    for caution in consumer.get("caution", []):
        body += f'<p style="color:{RED};">{_escaped(caution)}</p>'
    _text(page, fitz.Rect(left, y0+(183 if large_overview else 138), right, y1-(14 if large_overview else 16)), body, size=9.3, field=f"two-page step {step['id']} instruction/caution", minimum_scale=0.9, paragraph_gap=1)
    hide = _hidden_marker(step, asset)
    markers = []
    if hide:
        markers.append(hide)
    if consumer.get("engineering_notes") or step.get("warnings"):
        markers.append("图示连接待核")
    if asset["entry"].get("skipped_arrows"):
        markers.append("部分箭头未显示")
    if markers:
        _plain(page, fitz.Rect(left, y1-13, right, y1-1), " · ".join(markers), size=7.5, color=RED, field=f"two-page step {step['id']} review/visibility marker", minimum_scale=1, paragraph_gap=0)


def _installation(doc, plan: dict, assets: dict):
    import fitz
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    content = plan.get("manual_document", {})
    title = content.get("title") or plan.get("product", {}).get("title", "安装说明")
    _plain(page, fitz.Rect(MARGIN, 22, WIDTH-MARGIN, 52), title, size=21, bold=True, field="two-page installation heading", minimum_scale=0.9)
    _parts_band(page, plan)
    _sidebar(page, plan)
    columns, rows = 3, 3
    x0, y0, x1, y1, gap = 300, 141, WIDTH-MARGIN, 799, 12
    cell_width = (x1-x0-gap*(columns-1))/columns
    cell_height = (y1-y0-gap*(rows-1))/rows
    for index, step in enumerate(plan["steps"]):
        row, column = divmod(index, columns)
        left = x0 + column*(cell_width+gap)
        top = y0 + row*(cell_height+gap)
        rect = fitz.Rect(left, top, left+cell_width, top+cell_height)
        _step_card(page, plan, step, assets[step["id"]], rect, index+1)
    _footer(page, plan, 2)


def export_two_page_pdf(project: Path, plan: dict, manifest: dict, diagrams: Path | None = None) -> None:
    """Atomically replace ``manual.pdf`` with exactly two A3 landscape pages.

    The current step order is preserved. All instructions, consumer cautions
    and parts labels are kept; unfit content or more than nine steps produces
    a specific error and leaves the existing PDF untouched.
    """
    import fitz
    if len(plan.get("steps", [])) > MAX_STEPS:
        raise ValueError("Two-page manual supports at most 9 steps; combine or shorten the configured steps before exporting.")
    project = Path(project).resolve()
    directory = Path(diagrams or project / "diagrams").resolve()
    assets = _current_assets(plan, manifest, directory)
    temporary = None
    with fitz.open() as doc:
        _cover(doc, plan, assets)
        _installation(doc, plan, assets)
        doc.set_metadata({"title": plan.get("manual_document", {}).get("title") or plan.get("product", {}).get("title", "安装说明"),
                          "subject": "Two A3 sheets; current STEP-derived editable installation manual"})
        try:
            doc.subset_fonts()
        except RuntimeError:
            doc.subset_fonts(fallback=True)
        try:
            descriptor, name = tempfile.mkstemp(prefix="manual-two-page-", suffix=".tmp.pdf", dir=project)
            os.close(descriptor)
            temporary = Path(name)
            doc.save(temporary, garbage=4, deflate=True)
            temporary.replace(project / "manual.pdf")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
