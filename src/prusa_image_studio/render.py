"""Small orthographic preview renderer; exported geometry remains full resolution."""
import numpy as np
from PIL import Image, ImageDraw

from .palette import hex_rgb


def render_mesh(mesh, labels, recipes, size=(760, 630), yaw=-24., pitch=16., zoom=1.):
    width, height = size
    image = Image.new("RGB", size, "#111821")
    draw = ImageDraw.Draw(image)
    yaw, pitch = np.radians([yaw, pitch])
    # At yaw=0, look at Front (-Y); world Z is up.
    right = np.array([np.cos(yaw), np.sin(yaw), 0.])
    toward = np.array([np.sin(yaw)*np.cos(pitch), -np.cos(yaw)*np.cos(pitch), np.sin(pitch)])
    up = np.cross(toward, right)
    basis = np.array([right, up, toward])
    vertices = (mesh.vertices-(mesh.bounds[0]+mesh.bounds[1])/2) @ basis.T
    extent = np.ptp(vertices[:, :2], axis=0)
    scale = min((width-90)/max(extent[0], 1), (height-110)/max(extent[1], 1))*zoom
    screen = vertices[:, :2]*[scale, -scale]+[width/2, height/2]
    normals = mesh.face_normals @ toward
    visible = np.flatnonzero(normals > 1e-6)
    depth = vertices[mesh.faces[visible], 2].mean(axis=1)
    visible = visible[np.argsort(depth)]
    colors = np.array([[0., 0., 0.]]+[hex_rgb(r.color) for r in recipes])
    triangles = screen[mesh.faces]
    shades = .72+.28*np.clip(normals, 0, 1)
    colored = np.rint(colors[np.asarray(labels)]*shades[:, None]*255).astype(int)
    for index in visible:
        draw.polygon([tuple(p) for p in triangles[index]], fill=tuple(colored[index]))
    draw.text((22, height-32), "Drag to rotate  ·  Scroll to zoom  ·  Double-click for front", fill="#8FA2B9", font_size=14)
    return image
