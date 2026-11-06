from itertools import combinations
import json
from pathlib import Path
import zipfile

import numpy as np
from PIL import Image
import pytest
import trimesh

from prusa_image_studio.geometry import Placement, SIDES, panel, project_image, projection_coordinates, image_bounds, load_model
from prusa_image_studio.palette import make_palette, rgb_lab, lab_rgb, hex_rgb, quantize, save_palette, load_palette
from prusa_image_studio.project import export_project, PAINT_FILE, PROJECT_FILE, facet_code


def test_all_ten_equal_pairs():
    p = make_palette()
    assert len(p) == 15
    assert {r.tools for r in p[5:]} == set(combinations(range(1, 6), 2))
    for r in p[5:]:
        assert [c['ratio'] for c in r.virtual()['components']] == [.5, .5]
        assert sum(c['ratio'] for c in r.virtual()['components']) == 1


def test_color_roundtrip():
    rgb = np.array([hex_rgb(r.color) for r in make_palette()])
    assert np.max(np.abs(lab_rgb(rgb_lab(rgb))-rgb)) < 1e-6


def test_palette_measurements_do_not_change_recipes(tmp_path):
    p = make_palette(overrides={6: '#123456'})
    path = tmp_path/'palette.json'
    save_palette(path, p)
    loaded = load_palette(path)
    assert loaded == p
    assert loaded[5].color == '#123456'
    assert loaded[5].tools == (1, 2)


@pytest.mark.parametrize('dither', [False, True])
def test_exact_palette_and_transparency(dither):
    p = make_palette()
    image = Image.new('RGBA', (16, 1))
    for i, r in enumerate(p):
        image.putpixel((i, 0), tuple(int(x) for x in np.rint(hex_rgb(r.color)*255))+(255,))
    result = quantize(image, (16, 1), p, dither=dither)
    assert result.tolist() == [list(range(1, 16))+[0]]


@pytest.mark.parametrize('side', list(SIDES))
def test_orientation_and_topology_on_each_side(side):
    m = panel(8, 8, 8)
    p = Placement(width=8, height=8, pixel=1, side=side)
    grid = np.array([[1, 2], [3, 5]], dtype=np.uint8)
    result, labels = project_image(m, grid, p)
    assert result.is_watertight and result.is_winding_consistent
    assert result.volume == pytest.approx(m.volume)
    assert np.allclose(result.bounds, m.bounds)
    xy = projection_coordinates(result.triangles_center, p)[:, :2]-image_bounds(result, p)
    axis, sign, _, _ = SIDES[side]
    facing = result.face_normals[:, axis]*sign > .9
    # Exclude image-cell boundaries: verify image left/right and top/bottom do not mirror.
    for x, z, label in [(2, 6, 1), (6, 6, 2), (2, 2, 3), (6, 2, 5)]:
        selected = facing & (np.abs(xy[:, 0]-x) < 1) & (np.abs(xy[:, 1]-z) < 1)
        assert selected.sum() > 0
        assert set(labels[selected]) == {label}
    assert set(labels[~facing]) == {4}


def test_hidden_wall_is_not_painted():
    front = panel(6, 6, 2)
    behind = front.copy()
    behind.apply_translation([0, 4, 0])
    m = trimesh.util.concatenate([front, behind])
    result, labels = project_image(m, np.ones((6, 6), dtype=np.uint8), Placement(6, 6, 1))
    centers = result.triangles_center
    assert set(labels[centers[:, 1] > 3]) == {4}
    assert 1 in labels[centers[:, 1] < .1]
    assert result.is_watertight and result.volume == pytest.approx(m.volume)


def test_transparent_image_leaves_body():
    m = panel(6, 6, 2)
    grid = np.array([[1, 0], [0, 5]], dtype=np.uint8)
    result, labels = project_image(m, grid, Placement(6, 6, 1))
    x, y, z = result.triangles_center.T
    assert set(labels[(y < .01) & (x > 4) & (z > 4)]) == {4}
    assert set(labels[(y < .01) & (x < 2) & (z < 2)]) == {4}


def test_off_model_placement_has_clear_error():
    with pytest.raises(ValueError, match='does not overlap'):
        project_image(panel(6, 6, 2), np.ones((4, 4)), Placement(4, 4, 1, offset_x=30))


def test_native_project_writes_every_recipe_and_closed_geometry(tmp_path):
    m = panel(15, 3, 2)
    p = Placement(15, 3, 1)
    m, labels = project_image(m, np.tile(np.arange(1, 16), (3, 1)), p)
    target = tmp_path/'picture.3mf'
    recipes = make_palette()
    export_project(target, m, labels, recipes, name='A & B <image>')
    with zipfile.ZipFile(target) as z:
        config = json.loads(z.read(PROJECT_FILE))['config_containers'][0]
        assert config['virtual_extruders'] == [r.virtual() for r in recipes[5:]]
        assert config['configuration']['print_settings']['layer_height'] == .1
        assert len(config['configuration']['filament_settings']['temperature']) == 5
        facets = json.loads(z.read(PAINT_FILE))[0]
        assert facets['id'] == 2
        assert {f['dividing'] for f in facets['mmSegmentationFacets']} == {facet_code(i) for i in range(1, 16)}
    loaded = load_model(target)
    assert loaded.is_watertight
    assert loaded.volume == pytest.approx(m.volume, rel=1e-7)


def test_3mf_instance_transform_is_applied(tmp_path):
    m = panel(10, 8, 2)
    p = tmp_path/'box.3mf'
    export_project(p, m, np.full(len(m.faces), 4), make_palette())
    loaded = load_model(p)
    assert np.allclose(loaded.extents, [10, 2, 8])
    assert np.allclose(loaded.bounds[0], 0)


def test_invalid_and_open_models_are_rejected(tmp_path):
    mesh = panel(6, 6, 2)
    mesh.update_faces(np.arange(len(mesh.faces)-1))
    path = tmp_path/'open.stl'
    mesh.export(path)
    with pytest.raises(ValueError, match='watertight'): load_model(path)
    with pytest.raises(ValueError): Placement(pixel=float('nan')).validate()
    with pytest.raises(ValueError): Placement(pixel=.05).validate()


def test_curved_side_preserves_volume_and_topology():
    m = trimesh.creation.cylinder(radius=5, height=8, sections=24)
    m.apply_translation(-m.bounds[0])
    result, labels = project_image(m, np.full((8, 10), 6), Placement(10, 8, 1))
    assert result.is_watertight and result.is_winding_consistent
    assert result.volume == pytest.approx(m.volume)
    assert set(labels) == {4, 6}
    assert np.all(result.face_normals[labels == 6, 1] < -.5)


def test_3mf_inches_convert_to_mm(tmp_path):
    source, target = tmp_path/'mm.3mf', tmp_path/'inch.3mf'
    m = panel(10, 8, 2)
    export_project(source, m, np.full(len(m.faces), 4), make_palette())
    with zipfile.ZipFile(source) as a, zipfile.ZipFile(target, 'w') as b:
        for entry in a.infolist():
            data = a.read(entry.filename)
            if entry.filename == '3D/3dmodel.model':
                data = data.replace(b'unit="millimeter"', b'unit="inch"')
            b.writestr(entry, data)
    assert np.allclose(load_model(target).extents, np.array([10, 2, 8])*25.4)
