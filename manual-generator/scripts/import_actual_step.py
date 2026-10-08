"""Import STEP through XCAF, preserving repeated component instances and locations."""
from pathlib import Path
import argparse
import hashlib
import json
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--step', type=Path, default=ROOT / 'input/model_archive/p226_asm-20250114.stp')
    parser.add_argument('--inventory', type=Path, default=ROOT / 'output/model_inventory')
    args = parser.parse_args()
    source, out = args.step.resolve(), args.inventory.resolve()
    if not source.is_file():
        parser.error(f'STEP not found: {source}. See README.md or pass --step.')

    import cadquery as cq
    from cadquery.occ_impl.importers.assembly import _get_name
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.IFSelect import IFSelect_RetDone

    out.mkdir(parents=True, exist_ok=True)
    complete = out / 'import_complete.json'
    complete.unlink(missing_ok=True)
    t = time.monotonic()

    def log(*values):
        print(round(time.monotonic() - t, 1), *values, flush=True)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(False)
    reader.SetLayerMode(False)
    log('READ_START')
    if reader.ReadFile(str(source)) != IFSelect_RetDone:
        raise RuntimeError(f'OpenCascade could not read STEP: {source}')
    log('READ_DONE_TRANSFER_START')
    doc = TDocStd_Document(TCollection_ExtendedString('P226'))
    if not reader.Transfer(doc):
        raise RuntimeError('XCAF transfer failed')
    log('TRANSFER_DONE')
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    rows = []

    def walk(label, location, path):
        name = _get_name(label)
        loc = location * cq.Location(tool.GetLocation_s(label))
        if tool.IsReference_s(label):
            ref = TDF_Label()
            tool.GetReferredShape_s(label, ref)
            label = ref
            name = _get_name(ref) or name
        full = path + [name]
        if tool.IsAssembly_s(label):
            seq = TDF_LabelSequence()
            tool.GetComponents_s(label, seq)
            for i in range(1, seq.Length() + 1):
                walk(seq.Value(i), loc, full)
        else:
            shape = cq.Shape.cast(tool.GetShape_s(label)).moved(loc)
            index = len(rows)
            file = out / f'part_{index:03d}.brep'
            shape.exportBrep(str(file))
            b = shape.BoundingBox()
            row = {
                'index': index, 'name': name, 'path': full,
                'bbox_min': [b.xmin, b.ymin, b.zmin],
                'bbox_max': [b.xmax, b.ymax, b.zmax],
                'bbox_size': [b.xlen, b.ylen, b.zlen],
                'solids': len(shape.Solids()),
                'brep': file.name,
            }
            rows.append(row)
            (out / 'assembly_parts.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
            log('PART', index, '/'.join(full), [round(v, 1) for v in row['bbox_size']])

    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), cq.Location(), [])
    if not rows:
        raise RuntimeError('STEP contains no leaf shapes')
    log('COMPLETE', len(rows), 'solid_count', sum(x['solids'] for x in rows))
    digest = hashlib.file_digest(source.open('rb'), 'sha256').hexdigest()
    complete.write_text(json.dumps({
        'parts': len(rows), 'solids': sum(x['solids'] for x in rows),
        'seconds': time.monotonic() - t, 'source_step': source.name,
        'source_sha256': digest, 'brep_paths_relative_to': 'inventory',
    }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
