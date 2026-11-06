"""Real OpenGL texture orientation and visibility, run with a desktop or Xvfb."""
import os
import sys

import numpy as np
import pytest
import trimesh

from prusa_image_studio.geometry import Placement, SIDES, panel
from prusa_image_studio.palette import make_palette, hex_rgb
from prusa_image_studio.viewport import Camera, Viewport

pytestmark = pytest.mark.skipif(sys.platform.startswith('linux') and not os.environ.get('DISPLAY'),
                                reason='Desktop OpenGL needs DISPLAY; run under xvfb-run')


@pytest.mark.parametrize('side', list(SIDES))
def test_live_texture_orientation_on_every_side(side):
    mesh = panel(8, 8, 8)
    p = Placement(8, 8, pixel=.2, pixel_z=.1, side=side)
    recipes = make_palette()
    camera = Camera(); camera.face(side)
    viewport = Viewport()
    try:
        viewport.set_mesh(mesh)
        viewport.set_picture(np.array([[1, 2], [3, 5]], dtype=np.uint8))
        frame = np.asarray(viewport.render(camera, p, recipes, (512, 512)))
        axis, sign, horizontal, hsign = SIDES[side]
        for x, z, label in [(2, 6, 1), (6, 6, 2), (2, 2, 3), (6, 2, 5)]:
            point = np.zeros(3)
            point[horizontal] = x if hsign == 1 else 8-x
            point[axis] = 8 if sign == 1 else 0
            point[2] = z
            sx, sy = np.rint(camera.project([point], mesh, (512, 512))[0]).astype(int)
            expected = hex_rgb(recipes[label-1].color)*255
            assert np.max(np.abs(frame[sy, sx].astype(float)-expected)) <= 1
    finally: viewport.release()


def test_live_texture_does_not_paint_through_front_wall():
    front = panel(6, 6, 2)
    behind = front.copy(); behind.apply_translation([0, 4, 0])
    mesh = trimesh.util.concatenate([front, behind])
    camera = Camera(yaw=60, pitch=0)
    p = Placement(6, 6)
    recipes = make_palette()
    viewport = Viewport()
    try:
        viewport.set_mesh(mesh)
        viewport.set_picture(np.ones((6, 6), dtype=np.uint8))
        frame = np.asarray(viewport.render(camera, p, recipes, (640, 640)))
        # The rear front-facing wall is visible obliquely, but hidden from the
        # image projector at -Y. It must remain body white.
        sx, sy = np.rint(camera.project([[5, 4, 3]], mesh, (640, 640))[0]).astype(int)
        expected = hex_rgb(recipes[3].color)*255*(.76+.24*.5)
        assert np.max(np.abs(frame[sy, sx].astype(float)-expected)) <= 1
    finally: viewport.release()
