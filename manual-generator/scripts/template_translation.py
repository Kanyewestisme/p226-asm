"""Translate explicitly calibrated template text; retain pages and geometry."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import os
import re

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')).hexdigest()

def discover_template_copy(path: Path) -> dict:
    """Read text and its original glyph regions, never infer installation facts.

    Paragraphs sharing one PDF block/column stay together. Separate columns in
    one block remain distinct, as do part labels. Diagram letters/numerals and
    unreadable outlined branding stay untouched.
    """
    import fitz
    path=Path(path)
    source_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    entries,regions=[],[]
    with fitz.open(path) as doc:
        for number,page in enumerate(doc,1):
            for block_index,block in enumerate(page.get_text('dict')['blocks']):
                if 'lines' not in block: continue
                lines=[]
                for line in block['lines']:
                    text=''.join(span['text'] for span in line['spans']).strip()
                    if not text or text in {'*','·'} or (len(text)<=3 and all(c.isdigit() or c.isascii() and c.isalpha() for c in text)):
                        continue
                    spans=[s for s in line['spans'] if s['text'].strip()]
                    if not spans: continue
                    # Broken ToUnicode cover text must be supplied by a person.
                    if number==1 and any('\u0200'<=c<='\u023f' for c in text): continue
                    lines.append({'text':text,'bbox':list(line['bbox']),'spans':spans})
                clusters=[]
                for line in lines:
                    starts_item=bool(re.match(r'^(?:[a-f][.．]|[·•]|\d+[.．])',line['text']))
                    group=next((g for g in clusters if not starts_item and abs(g[-1]['bbox'][0]-line['bbox'][0])<12
                                and line['bbox'][1]>=g[-1]['bbox'][1]+1
                                and line['bbox'][1]-g[-1]['bbox'][3]<8),None)
                    if group is None: clusters.append([line])
                    else: group.append(line)
                for index,group in enumerate(clusters):
                    key=f'document.p{number}.b{block_index}.c{index}'
                    text='\n'.join(item['text'] for item in group)
                    rect=[min(l['bbox'][0] for l in group),min(l['bbox'][1] for l in group),
                          max(l['bbox'][2] for l in group),max(l['bbox'][3] for l in group)]
                    spans=[s for l in group for s in l['spans']]
                    size=min(float(s['size']) for s in spans)
                    color=spans[0].get('color',0)
                    entries.append({'key':key,'text':text,'scope':'consumer','kind':'template_text',
                                    'source_reference_sha256':source_hash})
                    regions.append({'key':key,'page':number,'rect':rect,'source_rects':[l['bbox'] for l in group],
                                    'source_text':text,'fontsize':size,'minimum_fontsize':max(4.5,size*.67),
                                    'color':[(color>>16&255)/255,(color>>8&255)/255,(color&255)/255],
                                    'align':0,'needs_layout_review':True})
    # Some exporters split a word across PDF blocks. Join only a continuation
    # in the same column and font; bullets and function items remain separate.
    merged_entries,merged_regions=[],[]
    for entry,region in zip(entries,regions):
        previous=merged_regions[-1] if merged_regions else None
        text=entry['text']
        starts_item=bool(re.match(r'^(?:[a-f][.．]|[·•]|\d+[.．])',text))
        if (previous and not starts_item and previous['page']==region['page']
                and abs(previous['rect'][0]-region['rect'][0])<12
                and abs(previous['fontsize']-region['fontsize'])<.1
                and -.1<=region['rect'][1]-previous['rect'][3]<5
                and previous['source_rects'][-1][2]-previous['source_rects'][-1][0]>150
                and not re.search(r'[。；;：:!?！？）)]$',previous['source_text'])):
            previous['source_text']+='\n'+text
            previous['source_rects']+=region['source_rects']
            previous['rect']=[min(previous['rect'][0],region['rect'][0]),previous['rect'][1],
                              max(previous['rect'][2],region['rect'][2]),region['rect'][3]]
            merged_entries[-1]['text']=previous['source_text']
        else:
            merged_entries.append(entry);merged_regions.append(region)
    return {'reference_sha256':source_hash,'entries':merged_entries,'text_regions':merged_regions}

def _load(project,locale,require_reviewed=True):
    from manual import load_project,current_manifest
    from manual_languages import read_document,select_language,LANGUAGES
    from template_pdf import validate_template_source
    project=Path(project).resolve()
    if locale not in LANGUAGES: raise ValueError('不支持的语言。')
    plan,_=load_project(project)
    template=validate_template_source(project,plan)
    manifest=current_manifest(project,plan)
    document=read_document(project,plan)
    regions=plan['pdf_template'].get('text_regions')
    if not isinstance(regions,list) or not regions:
        raise ValueError('请先读取并校准原模板的可翻译文字区域。')
    if any(region.get('needs_layout_review',False) for region in regions):
        raise ValueError('原模板文字区域已读取，请先校准翻译排版区域。')
    keys=[r.get('key') for r in regions]
    if any(not isinstance(k,str) or not k for k in keys) or len(set(keys))!=len(keys):
        raise ValueError('文字区域的翻译键必须完整且唯一。')
    texts=select_language(plan,document,locale,require_reviewed=require_reviewed,keys=keys)
    return project,plan,template,manifest,document,regions,texts

def _rect(value,page):
    import fitz
    if not isinstance(value,list) or len(value)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in value):
        raise ValueError('文字区域坐标无效。')
    result=fitz.Rect(value)
    if result.is_empty or not page.rect.contains(result): raise ValueError('文字区域超出原模板。')
    return result

def _layout_text(page,region,text,fontname):
    """Measure in a temporary page so overflow cannot damage the final PDF."""
    import fitz
    rect=_rect(region['rect'],page)
    size=float(region.get('fontsize',7))
    minimum=float(region.get('minimum_fontsize',max(4.5,size*.67)))
    if not .5<=minimum<=size<=72: raise ValueError('译文字号范围无效。')
    for index in range(int((size-minimum)/.15)+2):
        selected=max(minimum,size-index*.15)
        probe=fitz.open();test=probe.new_page(width=page.rect.width,height=page.rect.height)
        try:
            if fontname=='manual-latin':
                test.insert_font(fontname=fontname,fontbuffer=fitz.Font('helv').buffer)
            spare=test.insert_textbox(rect,text,fontname=fontname,fontsize=selected,
                                      color=tuple(region.get('color',[0,0,0])),align=region.get('align',0),lineheight=1.05)
        finally: probe.close()
        if spare>=-.01: return rect,selected
    raise ValueError(f"译文无法放入原模板区域：{region['key']}。请缩短译文或在模板中扩大该文字区。")

def export_language_pdf(project,locale,require_reviewed=True):
    import fitz
    from generic_render import plan_digest
    from book_pdf import _current_assets,_vector
    from template_pdf import validate_template_regions,editable_template
    from preview_service import _archive_and_publish
    project,plan,template,manifest,document,regions,texts=_load(project,locale,require_reviewed)
    assets=_current_assets(plan,manifest,project/'diagrams')
    filename=f'manual-{locale}.pdf'
    target=project/filename
    temporary=None
    temporary_receipt=None
    with editable_template(template) as doc:
        figure_regions,notes=validate_template_regions(plan,doc)
        copied={row['key']:row['source_text'] for row in document['entries']}
        measured=[]
        for region in regions:
            number=region.get('page')
            if type(number)!=int or not 1<=number<=len(doc): raise ValueError('译文页码无效。')
            page=doc[number-1]
            if region.get('source_text')!=copied.get(region['key']):
                raise ValueError('文字区域与当前原文不一致，请重新校准。')
            text=texts[region['key']]
            if not isinstance(text,str) or not text.strip(): raise ValueError('译文为空。')
            # Use a Type0 Unicode font. Built-in WinAnsi helv silently turns
            # inequality symbols and dashes into '?' even when glyphs exist.
            fontname='china-s' if locale=='zh' else 'manual-latin'
            rect,size=_layout_text(page,region,text,fontname)
            source_rects=[_rect(list(r),page) for r in region.get('source_rects',[region['rect']])]
            for previous in measured:
                if previous['page']==number and rect.intersects(previous['rect']):
                    raise ValueError(f"译文区域互相覆盖：{region['key']} 与 {previous['key']}。")
            if any(r['page']==number-1 and any(rect.intersects(other) for other in [r['target'],*r['masks']]) for r in figure_regions):
                raise ValueError('译文区域覆盖了安装图，请校准文字区域。')
            measured.append({'page':number,'key':region['key'],'rect':rect,'source_rects':source_rects,
                             'size':size,'text':text,'fontname':fontname,'color':region.get('color',[0,0,0]),'align':region.get('align',0)})
        # Remove only source glyphs. Keep all original vectors, images and card
        # backgrounds; white paint would damage the supplied grey template.
        for row in measured:
            page=doc[row['page']-1]
            for rect in row['source_rects']:page.add_redact_annot(rect,fill=False,cross_out=False)
        for page in doc: page.apply_redactions(images=0,graphics=0,text=0)
        for row in measured:
            if row['fontname']=='manual-latin':
                doc[row['page']-1].insert_font(fontname=row['fontname'],fontbuffer=fitz.Font('helv').buffer)
            spare=doc[row['page']-1].insert_textbox(row['rect'],row['text'],fontname=row['fontname'],
                       fontsize=row['size'],color=tuple(row['color']),align=row['align'],lineheight=1.05)
            if spare<-.01: raise ValueError('译文排版发生溢出，旧 PDF 已保留。')
        for region in figure_regions:
            page=doc[region['page']]
            for mask in region['masks']:page.draw_rect(mask,color=None,fill=(1,1,1),overlay=True)
            _vector(page,region['target'],assets[region['step_id']]['main'])
        try:
            descriptor,name=tempfile.mkstemp(prefix=f'.lang-{locale}-',suffix='.pdf',dir=project)
            os.close(descriptor);temporary=Path(name)
            doc.save(temporary,garbage=3,deflate=True)
            with fitz.open(temporary) as check:
                if len(check)!=len(doc): raise ValueError('多语言 PDF 页数变化。')
            receipt={'locale':locale,'filename':filename,'source_sha256':plan['source']['sha256'],
                     'plan_sha256':plan_digest(plan),'template_sha256':plan['pdf_template']['sha256'],
                     'base_sha256':document['base_sha256'],'translation_sha256':digest(texts),
                     'pdf_sha256':hashlib.sha256(temporary.read_bytes()).hexdigest(),'draft':not require_reviewed,
                     'regions':[{k:r[k] for k in ('key','size')} for r in measured]}
            descriptor,name=tempfile.mkstemp(prefix=f'.lang-{locale}-',suffix='.json',dir=project)
            os.close(descriptor);temporary_receipt=Path(name)
            temporary_receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            # PDF and provenance form one recoverable publication. A failed
            # backup, receipt write or second replacement retains both old files.
            receipt_name=Path(f'language-export-{locale}.json')
            _archive_and_publish(project,{Path(filename):temporary,receipt_name:temporary_receipt},
                                 set(),[Path(filename),receipt_name])
        finally:
            if temporary: temporary.unlink(missing_ok=True)
            if temporary_receipt: temporary_receipt.unlink(missing_ok=True)
    return receipt

def is_current_language_pdf(project,locale):
    try:
        project=Path(project).resolve()
        receipt=json.loads((project/f'language-export-{locale}.json').read_text(encoding='utf-8'))
        _,plan,_,_,document,_,texts=_load(project,locale,not receipt.get('draft',False))
        from generic_render import plan_digest
        target=project/f'manual-{locale}.pdf'
        return (receipt.get('source_sha256')==plan['source']['sha256']
                and receipt.get('plan_sha256')==plan_digest(plan)
                and receipt.get('template_sha256')==plan['pdf_template']['sha256']
                and receipt.get('base_sha256')==document['base_sha256']
                and receipt.get('translation_sha256')==digest(texts)
                and receipt.get('pdf_sha256')==hashlib.sha256(target.read_bytes()).hexdigest())
    except (OSError,KeyError,ValueError,RuntimeError,TypeError):
        return False
