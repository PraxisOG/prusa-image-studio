"""Checks for the second-pass color, placement and preview behavior."""
import numpy as np
from PIL import Image
import pytest

from prusa_image_studio.geometry import Placement, SIDES, panel
from prusa_image_studio.palette import make_palette, quantize, hex_rgb
from prusa_image_studio.viewport import Camera
from prusa_image_studio.workflow import generate


@pytest.mark.parametrize('count', [1, 2, 3, 6, 10, 15])
@pytest.mark.parametrize('dither', [False, True])
def test_color_limit_and_transparency(count, dither):
    rng = np.random.default_rng(17)
    pixels = rng.integers(0, 256, (32, 40, 4), dtype=np.uint8)
    pixels[:, :, 3] = 255
    pixels[0, :, 3] = 0
    ids = quantize(Image.fromarray(pixels), (40, 32), make_palette(),
                   color_count=count, dither=dither)
    assert 1 <= len(set(ids.flat)-{0}) <= count
    assert set(ids[0]) == {0}
    assert np.all((ids[1:] >= 1) & (ids[1:] <= 15))
    assert np.array_equal(ids, quantize(Image.fromarray(pixels), (40, 32), make_palette(),
                                      color_count=count, dither=dither))


def test_selects_useful_colors_without_renumbering_recipes():
    recipes = make_palette()
    ids = [3, 8, 14]
    image = Image.new('RGB', (90, 30))
    for i, id_ in enumerate(ids): image.paste(recipes[id_-1].color, (i*30, 0, (i+1)*30, 30))
    result = quantize(image, (90, 30), recipes, color_count=3)
    assert np.array_equal(result[0], np.repeat(ids, 30))


@pytest.mark.parametrize('fit', ['contain', 'cover', 'stretch'])
def test_anisotropic_pixels_preserve_physical_aspect(fit):
    image = Image.new('RGBA', (80, 60), make_palette()[1].color)
    image.paste(make_palette()[0].color, (0, 0, 40, 30))
    grid = quantize(image, (200, 600), make_palette(), fit=fit, physical_size=(80, 60))
    assert not np.any(grid == 0), 'Matching physical aspect must not introduce transparent bars'
    assert grid[100, 20] == 1 and grid[400, 20] == 2
    assert grid[100, 180] == 2


def test_anisotropic_contain_letterboxes_in_physical_space():
    grid = quantize(Image.new('RGB', (80, 40), make_palette()[0].color), (200, 600), make_palette(),
                    physical_size=(80, 60))
    assert np.all(grid[:100] == 0) and np.all(grid[500:] == 0)
    assert np.all(grid[100:500] == 1)


def test_sub_nozzle_detail_exports_valid_geometry():
    p = Placement(width=4, height=3, pixel=.2, pixel_z=.1)
    assert p.grid_size == (20, 30)
    image = Image.new('RGB', (4, 3), make_palette()[0].color)
    image.paste(make_palette()[1].color, (0, 0, 2, 2))
    result = generate(image, p, color_count=2)
    assert result.mesh.is_watertight and result.mesh.is_winding_consistent
    assert result.mesh.volume == pytest.approx(4*3*3)
    assert set(result.labels) == {1, 2, 4}
    assert len(set(result.grid.flat)) == 2
    Placement(pixel=.1, pixel_z=.1).validate()
    with pytest.raises(ValueError): Placement(pixel=.09).validate()
    with pytest.raises(ValueError): Placement(pixel_z=.09).validate()


@pytest.mark.parametrize('side', list(SIDES))
def test_image_drag_tracks_pointer_on_every_side(side):
    mesh = panel(30, 20, 10)
    camera = Camera()
    camera.face(side)
    camera.yaw += 16
    camera.pitch = 12
    delta = camera.image_drag(24, -13, mesh, (800, 600), side)
    _, _, horizontal, sign = SIDES[side]
    movement = np.zeros(3)
    movement[horizontal] = sign*delta[0]
    movement[2] = delta[1]
    origin, end = camera.project(np.array([mesh.centroid, mesh.centroid+movement]), mesh, (800, 600))
    assert np.allclose(end-origin, [24, -13])


def test_orbit_grabs_visible_object_and_edge_on_drag_is_rejected():
    mesh = panel(30, 20, 10)
    camera = Camera(yaw=0, pitch=0)
    front = np.array([[15, 0, 10]])
    before = camera.project(front, mesh, (800, 600))[0]
    camera.orbit(10, 10)
    after = camera.project(front, mesh, (800, 600))[0]
    assert np.all(after > before), (before, after)
    camera.yaw, camera.pitch = 90, 0
    with pytest.raises(ValueError, match='Face the selected side'):
        camera.image_drag(10, 0, mesh, (800, 600), 'Front (-Y)')
