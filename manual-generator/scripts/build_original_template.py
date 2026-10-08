"""Replace seven illustrations in the verified original two-page P226 PDF template."""
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
NAMES = ['step_01_casters', 'step_02_gas_lift', 'step_03_overview',
         'step_04_upper_to_base', 'step_05_left_right_armrests',
         'step_06_headrest', 'step_07_assembled']
SLOTS = [[598,212,755,309], [805,219,939,309], [1008,195,1150,309],
         [611,378,721,527], [804,374,950,527], [1010,371,1130,527],
         [608,609,725,785]]
NOTES = [
    ('新线稿审核说明', 9.0),
    ('图示取自所提供的STP模型；网布、脚托未在模型中完整表示。', 7.4),
    ('B/C螺丝、最终孔位及头枕插入深度待核，不作为已确认细节。', 7.4),
    ('第5步红箭头表示扶手总成复位方向，并非螺丝旋入方向。', 7.4),
    ('型号／版本与原图仍需核对，本版用于新图排版和工程审核。', 7.4),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, default=ROOT / 'input/P226(M)(S) 安装说明书 HBD-RD-P226-006 A0.pdf')
    parser.add_argument('--diagrams', type=Path, default=ROOT / 'output/diagrams')
    parser.add_argument('--regions', type=Path, default=ROOT / 'configuration/safe_replacement_regions.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/original_template/P226_original_template_review.pdf')
    args = parser.parse_args()
    base, src, output = args.template.resolve(), args.diagrams.resolve(), args.output.resolve()
    if output == base:
        parser.error('Output must differ from the original template.')
    required = [base, args.regions] + [src / f'{name}.svg' for name in NAMES]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        parser.error('Missing input(s): ' + ', '.join(missing))
    settings = json.loads(args.regions.read_text(encoding='utf-8'))
    source_sha = hashlib.sha256(base.read_bytes()).hexdigest()
    if source_sha != settings['source_sha256']:
        parser.error('Template SHA256 differs from the reviewed PDF. Recalibrate and review regions before using a different template.')
    masks = settings['regions']
    if [m['step'] for m in masks] != list(range(1, 8)) or any(m['page'] != 2 for m in masks):
        parser.error('Regions must describe steps 1 through 7 on page 2.')

    import fitz
    work = output.parent
    for folder in (work, work / 'vector', work / 'qa'):
        folder.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(base)
    if len(doc) != 2:
        raise ValueError('Expected the original two-page template')
    page = doc[1]
    manifest = []
    for name, slot, region in zip(NAMES, SLOTS, masks):
        svg = fitz.open(src / f'{name}.svg')
        vector = fitz.open('pdf', svg.convert_to_pdf())
        # Original tight vector bounds and padding; no rasterization.
        rect = fitz.Rect()
        for drawing in vector[0].get_drawings():
            b = drawing['rect']
            pad = (drawing.get('width') or 0) / 2 + .01
            rect |= fitz.Rect(b.x0-pad, b.y0-pad, b.x1+pad, b.y1+pad)
        if rect.is_empty:
            raise ValueError(f'No vector geometry: {name}')
        rect = fitz.Rect(rect.x0-2, rect.y0-2, rect.x1+2, rect.y1+2) & vector[0].rect
        vector.save(work / 'vector' / f'{name}.pdf', garbage=3, deflate=True)
        for mask in region['safe_white_masks_pt']:
            page.draw_rect(fitz.Rect(mask), color=None, fill=(1,1,1), overlay=True)
        page.show_pdf_page(fitz.Rect(slot), vector, 0, clip=rect, keep_proportion=True, overlay=True)
        manifest.append({
            'asset': name, 'source_svg_sha256': hashlib.sha256((src / f'{name}.svg').read_bytes()).hexdigest(),
            'source_clip': list(rect), 'target_slot': slot,
            'masks': region['safe_white_masks_pt'], 'source_path_count': len(vector[0].get_drawings()),
        })
        vector.close()
        svg.close()
    # Original wording remains untouched. These notes occupy previously blank space.
    note_rect = fitz.Rect(779,702,1148,775)
    y = 714
    for text, size in NOTES:
        page.insert_text((779,y), text, fontname='china-s', fontsize=size, color=(0.35,0.35,0.35), overlay=True)
        y += 13
    metadata = doc.metadata.copy()
    metadata['title'] = 'P226 原模板七步新线稿 审核版'
    metadata['subject'] = 'Original 2-page A3 template retained; seven STEP-derived vector installation illustrations replaced; engineering review only'
    doc.set_metadata(metadata)
    doc.save(output, garbage=3, deflate=True)
    doc.close()
    (work / 'build_manifest.json').write_text(json.dumps({
        'source': str(base), 'output': str(output), 'source_sha256': source_sha,
        'replacements': manifest, 'note_rect': list(note_rect), 'notes': [v[0] for v in NOTES],
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    with fitz.open(output) as new:
        new[1].get_pixmap(matrix=fitz.Matrix(1.5,1.5), alpha=False).save(work / 'installation_page_preview.png')
        for i, pg in enumerate(new):
            pg.get_pixmap(matrix=fitz.Matrix(2,2), alpha=False).save(work / 'qa' / f'page_{i+1}.png')
        print(output)
        print(work / 'installation_page_preview.png')
        print('page_count', len(new), 'bytes', output.stat().st_size)


if __name__ == '__main__':
    main()
