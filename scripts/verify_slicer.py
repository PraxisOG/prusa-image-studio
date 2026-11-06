"""End-to-end check: native v3 roundtrip and actual 50:50 outer-wall toolpaths.

Run from the project root: .venv/bin/python scripts/verify_slicer.py
Writes inspectable calibration artifacts. Does not contact a printer.
"""
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

from prusa_image_studio.palette import make_palette
from prusa_image_studio.project import run_slicer, PROJECT_FILE, PAINT_FILE
from prusa_image_studio.workflow import calibration


def check_gcode(path):
    number = r'([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)'
    xyz = dict(X=0., Y=0., Z=0.)
    e, relative, tool, feature = 0., False, None, ''
    layers = [defaultdict(set) for _ in range(15)]
    for line in Path(path).read_text().splitlines():
        if line.startswith(';TYPE:'): feature = line[6:].strip()
        s = line.split(';')[0].strip()
        if not s: continue
        t = re.match(r'^T(\d+)\b', s)
        if t: tool = int(t[1])+1  # firmware tool IDs are zero-based
        cmd = s.split()[0]
        if cmd == 'M83': relative = True
        if cmd == 'M82': relative = False
        values = {k: float(v) for k, v in re.findall(r'([XYZEFIJK])'+number, s)}
        if cmd == 'G92':
            if 'E' in values: e = values['E']
            xyz.update({k: v for k, v in values.items() if k in xyz})
            continue
        if cmd not in ('G0', 'G1', 'G2', 'G3'): continue
        prev = xyz.copy()
        xyz.update({k: v for k, v in values.items() if k in xyz})
        extrusion = values.get('E', 0) if relative else values.get('E', e)-e
        if 'E' in values: e = e+values['E'] if relative else values['E']
        # Only straight model outer-wall segments. Exclude tower, purge, brim,
        # solid fill, retraction, Z-hop, and circular corner arcs.
        if cmd != 'G1' or extrusion <= 0 or feature != 'External perimeter' or tool is None: continue
        if not (1.-1e-5 <= xyz['Z'] < 11.-1e-5 and abs(xyz['Y']-prev['Y']) < .005
                and abs(xyz['Z']-prev['Z']) < .005 and 158.5 < xyz['Y'] < 159): continue
        for i in range(15):
            probe = 135+(i+.5)*6
            if min(prev['X'], xyz['X']) <= probe <= max(prev['X'], xyz['X']):
                layers[i][round(xyz['Z'], 3)].add(tool)
    report = []
    for recipe, layer in zip(make_palette(), layers):
        counts = Counter(t for tools in layer.values() for t in tools)
        sequence = [next(iter(t)) for z, t in sorted(layer.items()) if len(t) == 1]
        expected = {t: 100//len(recipe.tools) for t in recipe.tools}
        alternations = sum(a != b for a, b in zip(sequence, sequence[1:]))
        assert len(layer) == 100, (recipe.name, 'missing layers', len(layer))
        assert all(len(t) == 1 for t in layer.values()), (recipe.name, 'ambiguous tool')
        assert dict(counts) == expected, (recipe.name, dict(counts), expected)
        assert alternations == (99 if len(recipe.tools) == 2 else 0), (recipe.name, 'not alternating')
        report.append({'recipe_id': recipe.id, 'name': recipe.name, 'sampled_layers': 100,
                       'tool_layer_counts': dict(counts), 'alternations': alternations})
    return report


def main():
    output = Path('artifacts/verification').resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = calibration(progress=print)
    source = output/'calibration.3mf'
    roundtrip = output/'roundtrip.3mf'
    gcode = output/'calibration.gcode'
    result.export(source)
    for file in (roundtrip, gcode): file.unlink(missing_ok=True)
    run_slicer(['--export-3mf', '--output', roundtrip, source], roundtrip, output)
    (output/'slicer.log').rename(output/'roundtrip.log')
    with zipfile.ZipFile(source) as a, zipfile.ZipFile(roundtrip) as b:
        for key in (PROJECT_FILE, PAINT_FILE):
            expected, actual = json.loads(a.read(key)), json.loads(b.read(key))
            if key == PROJECT_FILE:
                expected = expected['config_containers'][0]['virtual_extruders']
                actual = actual['config_containers'][0]['virtual_extruders']
            else:
                # Slicer may renumber resources. Annotations reference volumes
                # (component objects); older projects may reference their mesh.
                ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
                for archive, annotations in ((a, expected), (b, actual)):
                    xml = ET.fromstring(archive.read('3D/3dmodel.model'))
                    for annotation in annotations:
                        obj = xml.find(f"m:resources/m:object[@id='{annotation['id']}']", ns)
                        assert obj is not None
                        if obj.find('m:mesh', ns) is None:
                            child = obj.find('m:components/m:component', ns)
                            assert child is not None
                            obj = xml.find(f"m:resources/m:object[@id='{child.attrib['objectid']}']", ns)
                        assert obj.find('m:mesh', ns) is not None, 'Painting does not resolve to a mesh'
                expected = [(x['mmSegmentationFacetsVersion'], x['mmSegmentationFacets']) for x in expected]
                actual = [(x['mmSegmentationFacetsVersion'], x['mmSegmentationFacets']) for x in actual]
            assert expected == actual, f'Roundtrip changed {key}'
    run_slicer(['--export-gcode', '--no-binary-gcode', '--output', gcode, roundtrip], gcode, output)
    report = check_gcode(gcode)
    (output/'toolpath-verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print('PASS: all 15 recipes roundtrip; all ten blends alternate exactly 50/50 over 100 sampled layers.')


if __name__ == '__main__': main()
