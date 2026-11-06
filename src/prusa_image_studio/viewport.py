"""Interactive projection on the original mesh; fine tessellation is export-only."""
from dataclasses import dataclass
from contextlib import contextmanager
import ctypes
import ctypes.util
import os
import sys

import moderngl
import numpy as np
from PIL import Image

from .geometry import SIDES, image_bounds, projection_coordinates
from .palette import hex_rgb


@contextmanager
def preserve_x11_error_handler():
    """Keep the GUI's process-wide X11 handler across GLX initialization.

    glcontext's X11 backend resets XSetErrorHandler to the fatal default after
    creating a context. Tk needs its own handler for transient BadWindow events
    during file-dialog focus changes. Restore it even if context creation fails;
    do not replace it with a handler that silently ignores all X11 errors.
    """
    if not sys.platform.startswith("linux"):
        yield
        return
    library = os.environ.get("GLCONTEXT_LINUX_LIBX11") or ctypes.util.find_library("X11")
    if not library:
        raise RuntimeError("Desktop preview requires the X11 runtime library")
    x11 = ctypes.CDLL(library)
    x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    x11.XSetErrorHandler.restype = ctypes.c_void_p
    previous = x11.XSetErrorHandler(None)
    x11.XSetErrorHandler(previous)
    try:
        yield
    finally:
        x11.XSetErrorHandler(previous)


@dataclass
class Camera:
    yaw: float = -24.
    pitch: float = 16.
    zoom: float = 1.

    @property
    def basis(self):
        yaw, pitch = np.radians([self.yaw, self.pitch])
        right = np.array([np.cos(yaw), np.sin(yaw), 0.])
        toward = np.array([np.sin(yaw)*np.cos(pitch), -np.cos(yaw)*np.cos(pitch), np.sin(pitch)])
        return np.array([right, np.cross(toward, right), toward])

    def scale(self, mesh, size):
        return max(1, min(size[0]-64, size[1]-64))/max(np.linalg.norm(mesh.extents), 1)*self.zoom

    def orbit(self, dx, dy):
        # Grab the visible object: dragging right moves its front to the right.
        self.yaw -= dx*.45
        self.pitch = float(np.clip(self.pitch+dy*.45, -80, 80))

    def face(self, side):
        self.yaw = {"Front (-Y)": 0, "Back (+Y)": 180, "Right (+X)": 90, "Left (-X)": -90}[side]
        self.pitch = 0.

    def project(self, points, mesh, size):
        center = (mesh.bounds[0]+mesh.bounds[1])/2
        return ((np.asarray(points)-center) @ self.basis[:2].T)*[self.scale(mesh, size), -self.scale(mesh, size)]+np.array(size)/2

    def image_drag(self, dx, dy, mesh, size, side):
        _, _, horizontal, sign = SIDES[side]
        h = np.zeros(3); h[horizontal] = sign
        plane = np.column_stack((h, [0, 0, 1]))
        mapping = self.basis[:2] @ plane
        mapping[1] *= -1
        mapping *= self.scale(mesh, size)
        if abs(np.linalg.det(mapping)) < self.scale(mesh, size)**2*.12:
            raise ValueError("Face the selected side to move the image from this angle.")
        return np.linalg.solve(mapping, [dx, dy])


VERTEX = '''#version 330
in vec3 position;
in vec3 normal;
uniform vec3 center, camera_right, camera_up, camera_toward;
uniform vec2 scale;
uniform float zrange;
out vec3 world, face_normal;
void main() {
    vec3 p = position-center;
    gl_Position = vec4(dot(p,camera_right)*scale.x, dot(p,camera_up)*scale.y,
                       -dot(p,camera_toward)/zrange, 1.0);
    world = position;
    face_normal = normal;
}
'''

FRAGMENT = '''#version 330
in vec3 world, face_normal;
uniform sampler2D picture, side_depth;
uniform vec3 palette[16];
uniform vec3 horizontal, projection_direction, camera_toward;
uniform vec2 image_lower, image_size, side_lower, side_span;
uniform float depth_center, depth_range;
uniform bool has_picture;
out vec4 color;
void main() {
    vec3 base = palette[4];
    vec2 projected = vec2(dot(world,horizontal), world.z);
    vec2 uv = (projected-image_lower)/image_size;
    if (has_picture && dot(normalize(face_normal),projection_direction) > 0.5
        && all(greaterThanEqual(uv,vec2(0))) && all(lessThan(uv,vec2(1)))) {
        vec2 visibility_uv = (projected-side_lower)/side_span;
        float current_depth = 0.5-0.5*(dot(world,projection_direction)-depth_center)/depth_range;
        float closest_depth = texture(side_depth,visibility_uv).r;
        if (current_depth <= closest_depth+0.015/depth_range) {
            int id = int(texture(picture,vec2(uv.x,1.0-uv.y)).r*255.0+0.5);
            if (id > 0 && id <= 15) base = palette[id];
        }
    }
    float light = 0.76+0.24*max(0.0,dot(normalize(face_normal),camera_toward));
    color = vec4(base*light,1);
}
'''

DEPTH_VERTEX = '''#version 330
in vec3 position;
uniform vec3 horizontal, projection_direction;
uniform vec2 side_lower, side_span;
uniform float depth_center, depth_range;
void main() {
    vec2 uv = (vec2(dot(position,horizontal),position.z)-side_lower)/side_span;
    float depth = -(dot(position,projection_direction)-depth_center)/depth_range;
    gl_Position = vec4(uv*2.0-1.0,depth,1);
}
'''


class Viewport:
    def __init__(self):
        # Use the desktop context backend. Mesa's software path is very fast for
        # this low-polygon textured view and avoids device-specific EGL failures.
        # Restore the environment so launched slicers keep their own GL settings.
        old = os.environ.get("LIBGL_ALWAYS_SOFTWARE")
        software = sys.platform.startswith("linux") and os.environ.get("PRUSA_IMAGE_HARDWARE_GL") != "1"
        try:
            if software: os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
            with preserve_x11_error_handler():
                self.ctx = moderngl.create_context(standalone=True, require=330)
        finally:
            if old is None: os.environ.pop("LIBGL_ALWAYS_SOFTWARE", None)
            else: os.environ["LIBGL_ALWAYS_SOFTWARE"] = old
        self.renderer_name = self.ctx.info.get("GL_RENDERER", "OpenGL")
        self.program = self.ctx.program(vertex_shader=VERTEX, fragment_shader=FRAGMENT)
        self.depth_program = self.ctx.program(vertex_shader=DEPTH_VERTEX,
                                               fragment_shader='#version 330\nvoid main() {}')
        self.program['picture'].value = 0
        self.program['side_depth'].value = 1
        self.mesh = None
        self.vbo = self.vao = self.depth_vao = self.picture = self.framebuffer = None
        self.depth_texture = self.ctx.depth_texture((1536, 1536))
        self.depth_texture.compare_func = ""
        self.depth_texture.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.depth_texture.repeat_x = self.depth_texture.repeat_y = False
        self.depth_buffer = self.ctx.framebuffer(depth_attachment=self.depth_texture)
        self.side = None
        self.has_picture = False
        self.size = None
        self.set_picture(np.zeros((1, 1), dtype=np.uint8))
        self.has_picture = False

    def set_mesh(self, mesh):
        self.mesh = mesh
        for resource in (self.vao, self.depth_vao, self.vbo):
            if resource is not None: resource.release()
        positions = mesh.triangles.reshape(-1, 3).astype('f4')
        normals = np.repeat(mesh.face_normals, 3, axis=0).astype('f4')
        self.vbo = self.ctx.buffer(np.column_stack((positions, normals)).tobytes())
        self.vao = self.ctx.vertex_array(self.program, [(self.vbo, '3f 3f', 'position', 'normal')])
        self.depth_vao = self.ctx.vertex_array(self.depth_program, [(self.vbo, '3f 12x', 'position')])
        self.side = None

    def set_picture(self, grid):
        if self.picture is not None: self.picture.release()
        grid = np.ascontiguousarray(grid, dtype=np.uint8)
        self.picture = self.ctx.texture((grid.shape[1], grid.shape[0]), 1, grid.tobytes(), alignment=1)
        self.picture.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.picture.repeat_x = self.picture.repeat_y = False
        self.has_picture = True

    def clear_picture(self):
        self.has_picture = False

    def set_side(self, placement):
        if self.side == placement.side: return
        coords = projection_coordinates(self.mesh.vertices, placement)
        lo, hi = coords.min(axis=0), coords.max(axis=0)
        axis, sign, horizontal, hsign = SIDES[placement.side]
        h = np.zeros(3); h[horizontal] = hsign
        d = np.zeros(3); d[axis] = sign
        uniforms = dict(horizontal=tuple(h), projection_direction=tuple(d),
                        side_lower=tuple(lo[:2]-.01), side_span=tuple(hi[:2]-lo[:2]+.02),
                        depth_center=float((lo[2]+hi[2])/2), depth_range=float((hi[2]-lo[2])/2+1))
        for program in (self.program, self.depth_program):
            for key, value in uniforms.items(): program[key].value = value
        self.depth_buffer.use()
        self.depth_buffer.clear(depth=1.)
        self.ctx.enable(moderngl.DEPTH_TEST | moderngl.CULL_FACE)
        self.depth_vao.render()
        self.side = placement.side

    def render(self, camera, placement, recipes, size):
        if self.mesh is None: raise ValueError("Load a model first")
        self.set_side(placement)
        size = tuple(map(int, size))
        if self.size != size:
            if self.framebuffer is not None: self.framebuffer.release()
            self.framebuffer = self.ctx.simple_framebuffer(size, components=3)
            self.size = size
        self.framebuffer.use()
        self.framebuffer.clear(17/255, 24/255, 33/255, 1., depth=1.)
        basis = camera.basis
        scale = camera.scale(self.mesh, size)
        uniforms = dict(center=tuple((self.mesh.bounds[0]+self.mesh.bounds[1])/2),
                        camera_right=tuple(basis[0]), camera_up=tuple(basis[1]), camera_toward=tuple(basis[2]),
                        scale=(2*scale/size[0], 2*scale/size[1]), zrange=float(np.linalg.norm(self.mesh.extents)+1),
                        image_lower=tuple(image_bounds(self.mesh, placement)),
                        image_size=(placement.width, placement.height), has_picture=self.has_picture)
        for key, value in uniforms.items(): self.program[key].value = value
        palette = np.array([[0, 0, 0]]+[hex_rgb(r.color) for r in recipes], dtype='f4')
        self.program['palette'].write(palette.tobytes())
        self.picture.use(location=0)
        self.depth_texture.use(location=1)
        self.ctx.enable(moderngl.DEPTH_TEST | moderngl.CULL_FACE)
        self.vao.render()
        return Image.frombytes('RGB', size, self.framebuffer.read(components=3, alignment=1)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    def outline(self, camera, placement, size):
        """Screen-space image rectangle on the selected outer bounding plane."""
        axis, sign, horizontal, hsign = SIDES[placement.side]
        direction = np.zeros(3); direction[axis] = sign
        if np.dot(camera.basis[2], direction) < .15: return None
        lo = image_bounds(self.mesh, placement)
        points = np.zeros((4, 3))
        for i, (u, v) in enumerate(((0, 0), (1, 0), (1, 1), (0, 1))):
            points[i, horizontal] = (lo[0]+u*placement.width)*hsign
            points[i, 2] = lo[1]+v*placement.height
            points[i, axis] = self.mesh.bounds[1 if sign > 0 else 0, axis]
        return camera.project(points, self.mesh, size)

    def release(self):
        for resource in (self.vao, self.depth_vao, self.vbo, self.picture, self.framebuffer,
                         self.depth_buffer, self.depth_texture, self.program, self.depth_program):
            if resource is not None: resource.release()
        self.ctx.release()
