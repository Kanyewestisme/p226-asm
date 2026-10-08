"""Render real STEP-derived assembly stages. Never substitute synthetic geometry."""
from pathlib import Path
import json,time,argparse,math,os,hashlib
import cadquery as cq
import fitz
from cad_pipeline import hidden_line_svg,project_point

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/diagrams'
INV=ROOT/'output/model_inventory'
rows=[]
by={}
source_info={}

def project_path(path):
    path=Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)

def configure(inventory, output):
    global INV,OUT,rows,by,source_info,SEAT,BACK,ARMR,ARML,HEAD,BASE,CORE,ALL
    INV=inventory.resolve();OUT=output.resolve()
    required=[INV/'assembly_parts.json',INV/'import_complete.json']
    missing=[str(p) for p in required if not p.is_file()]
    if missing:raise FileNotFoundError('Run import_actual_step.py first. Missing: '+', '.join(missing))
    rows=json.loads(required[0].read_text(encoding='utf-8'))
    source_info=json.loads(required[1].read_text(encoding='utf-8'))
    if len(rows)!=source_info['parts']:raise ValueError('Incomplete inventory; rerun STEP import')
    by={x['index']:x for x in rows}
    expected=json.loads((ROOT/'configuration/assembly_mapping.json').read_text(encoding='utf-8'))
    if source_info.get('source_sha256') != expected['step_sha256']:
        raise ValueError('STEP identity is unverified for these P226-specific indices. Review the new inventory, update render_steps.py groups/stages and assembly_mapping.json for the new STEP before rendering.')
    SEAT=[r['index'] for r in rows if r['index']<=43 and r['solids']]
    BACK=[r['index'] for r in rows if (57<=r['index']<=74 or 121<=r['index']<=134 or r['index']==97) and r['solids']]
    ARMR=[r['index'] for r in rows if 75<=r['index']<=96 and (r['solids'] or r['index']==75)]
    ARML=[r['index'] for r in rows if 99<=r['index']<=120 and (r['solids'] or r['index']==107)]
    HEAD=[135,136,137]
    BASE=[45]+list(range(47,57))
    CORE=SEAT+BACK
    ALL=CORE+ARMR+ARML+HEAD+BASE+[44]
    for index in ALL:
        path=Path(by[index]['brep'])
        base=INV if source_info.get('brep_paths_relative_to')=='inventory' else ROOT
        if not (base/path).is_file():raise FileNotFoundError(f'Missing BREP cache: {base/path}')
    OUT.mkdir(parents=True,exist_ok=True)

def shape(index,translation=(0,0,0)):
    base=INV if source_info.get('brep_paths_relative_to')=='inventory' else ROOT
    raw=cq.Shape.importBrep(str(base/by[index]['brep']))
    solids=raw.Solids()
    if not solids:
        if index not in (75,107):raise ValueError(f'Part {index} is an unreviewed non-solid')
        return raw.translate(translation)
    return cq.Compound.makeCompound(solids).translate(translation)

def render(asset_id,items,camera,up=(0,1,0),arrows=(),caption='',notes=()):
    t=time.monotonic();print('START',asset_id,flush=True)
    shapes=[shape(i,tr) for i,tr in items]
    print('GEOMETRY_READY',asset_id,len(shapes),round(time.monotonic()-t,2),flush=True)
    mode=os.environ.get('P226_HLR_MODE','poly' if asset_id=='step_07_assembled' else 'exact')
    svg,projection=hidden_line_svg(cq.Compound.makeCompound(shapes),camera=camera,up=up,width=1100,height=800,margin=55,caption='Generated from actual P226 STEP; engineering review draft',poly=mode=='poly')
    overlay=''
    for a,b in arrows:
        x1,y1=project_point(a,projection);x2,y2=project_point(b,projection)
        dx,dy=x2-x1,y2-y1;length=math.hypot(dx,dy);ux,uy=dx/length,dy/length
        x2-=ux*10;y2-=uy*10;hx,hy=x2-17*ux,y2-17*uy
        overlay+=f'<line x1="{x1}" y1="{y1}" x2="{hx}" y2="{hy}" stroke="#d62720" stroke-width="3.1"/>'
        overlay+=f'<polygon points="{x2},{y2} {hx-6*uy},{hy+6*ux} {hx+6*uy},{hy-6*ux}" fill="#d62720"/>'
    svg=svg.replace('</svg>',overlay+'</svg>')
    file=OUT/f'{asset_id}.svg';file.write_text(svg,encoding='utf-8')
    svgd=fitz.open(stream=svg.encode(),filetype='svg');pdf=fitz.open('pdf',svgd.convert_to_pdf());pdf[0].get_pixmap(matrix=fitz.Matrix(1.2,1.2),alpha=False).save(OUT/f'{asset_id}.png')
    p=OUT/'manifest.json';m=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'source_step':source_info.get('source_step'),'source_sha256':source_info.get('source_sha256'),'entries':[]}
    if m.get('source_sha256')!=source_info.get('source_sha256'):raise ValueError('Output manifest belongs to another STEP; use a fresh --output directory')
    entry={'asset_id':asset_id,'svg_path':project_path(file),'png_path':project_path(OUT/f'{asset_id}.png'),'caption':caption,'status':'ready','notes':list(notes),'cad_part_indices':[i for i,tr in items],'transforms':{str(i):tr for i,tr in items},'projection':projection,'seconds':round(time.monotonic()-t,2)}
    entry.update(render_method=projection['algorithm'],linear_deflection_mm=0.20 if mode=='poly' else None,angular_deflection_rad=0.20 if mode=='poly' else None,svg_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),visual_review='pending: inspect rendered PNG after regeneration')
    m['entries']=[x for x in m['entries'] if x['asset_id']!=asset_id]+[entry];p.write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8');print('READY',asset_id,entry['seconds'],flush=True)

def stage1():
    items=[(45,(0,0,0))]+[(i,(0,-125,0)) for i in range(47,57)]
    arrows=[]
    for i in [47,49,51,53,55]:
        b=by[i];x=(b['bbox_min'][0]+b['bbox_max'][0])/2;z=(b['bbox_min'][2]+b['bbox_max'][2])/2
        arrows.append(((x,-282,z),(x,-198,z)))
    render('step_01_casters',items,camera=(-1,-1.15,1),up=(0,-1,0),arrows=arrows,caption='真实STEP五爪与5组椅轮，分离展示插装方向。')

def stage2():
    items=[(45,(0,0,0))]+[(i,(0,0,0)) for i in range(47,57)]+[(44,(0,210,0))]
    render('step_02_gas_lift',items,camera=(-1,0.9,1),arrows=[((0,-24,0),(0,-85,0))],caption='真实STEP气压杆与五爪，气压杆沿自身轴线分离。')

def items(ids,tr=(0,0,0)):return [(i,tr) for i in ids]

def stage3():
    render('step_03_overview',items(SEAT)+items(BACK,(180,0,0)),camera=(1,-1.1,1),up=(0,-1,0),
           arrows=[((285,20,0),(125,20,0))],caption='真实STEP坐垫与靠背总成分离展示；按原PDF使用C螺丝×3。',
           notes=['C螺丝未与STEP独立零件可靠对应，本图未添加或伪造三颗螺丝；孔位与插接行程待产品方核对。','STEP未提供完整坐面/靠背网布与原PDF所示脚托，图中保留原模型实际结构。'])

def stage4():
    render('step_04_upper_to_base',items(CORE,(0,210,0))+items(BASE+[44]),camera=(-1,0.75,1),
           arrows=[((0,190,0),(0,55,0))],caption='真实STEP椅身总成与底座沿气压杆轴线分离。',
           notes=['STEP与原PDF存在网布/脚托表示差异；示意仅用于核对安装顺序，不代表完整外观。'])

def stage5():
    render('step_05_left_right_armrests',items(CORE+BASE+[44])+items(ARMR,(0,0,-155))+items(ARML,(0,0,155)),camera=(1,0.45,1),
           arrows=[((89,261,-450),(89,261,-304)),((89,261,450),(89,261,304))],caption='真实STEP左右扶手向外分离；安装方向指向靠背两侧。',
           notes=['B螺丝×2取自原PDF，未在STEP中可靠识别对应零件；螺丝应从靠背内侧安装。红箭头仅表示扶手总成复位方向，不是B螺丝进入方向。','左右扶手支架为源STEP曲面，按原几何保留。'])

def stage6():
    render('step_06_headrest',items(CORE+ARMR+ARML+BASE+[44])+items(HEAD,(0,190,0)),camera=(1,0.3,1),
           arrows=[((212,850,0),(212,746,0))],caption='真实STEP头枕总成沿竖直方向分离，插入靠背顶部。',
           notes=['头枕导向件总成边界按STEP层级识别，滑块/插入深度仍需与实物版本核对。'])

def stage7():
    render('step_07_assembled',items(ALL),camera=(-1,0.38,1),caption='实际STEP完整装配视图，保留真实几何缺项。',
           notes=['该2025-01-14 STEP与原PDF外观不同：未补画网布和脚托。须确认型号/版本后才可作为消费者正式说明书。'])

def inventory():
    # One exploded actual-model view; the printed BOM remains authoritative for supplied quantities.
    groups=items(SEAT)+items(BACK,(210,110,0))+items(ARMR,(0,40,-330))+items(ARML,(0,40,330))+items(HEAD,(190,280,0))+items(BASE,(0,-140,0))+items([44],(-300,0,0))
    render('parts_inventory',groups,camera=(-1,0.6,1),caption='真实STEP安装总成分离概览；配件数量与A/B/C标签以原PDF为准。',
           notes=['总成视图未显示虚构工具或B/C螺丝。网布、脚托和正式型号版本待核。'])

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',choices=['1','2','3','4','5','6','7','inventory','all'])
    p.add_argument('--inventory',type=Path,default=INV)
    p.add_argument('--output',type=Path,default=OUT)
    a=p.parse_args()
    configure(a.inventory,a.output)
    for key,fn in [('1',stage1),('2',stage2),('3',stage3),('4',stage4),('5',stage5),('6',stage6),('7',stage7),('inventory',inventory)]:
        if a.stage in [key,'all']:fn()
