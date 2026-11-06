"""Watertight panels and conforming mesh refinement for surface projection."""
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

MAX_FACES = 600_000
SIDES = {"Front (-Y)": (1, -1, 0, 1), "Back (+Y)": (1, 1, 0, -1),
         "Right (+X)": (0, 1, 1, 1), "Left (-X)": (0, -1, 1, -1)}


@dataclass
class Placement:
    width: float = 80.
    height: float = 60.
    pixel: float = .8
    offset_x: float = 0.
    offset_z: float = 0.
    side: str = "Front (-Y)"
    body_tool: int = 4
    pixel_z: float | None = None

    @property
    def spacing(self):
        return np.array([self.pixel, self.pixel if self.pixel_z is None else self.pixel_z])

    def validate(self):
        if not np.isfinite([self.width, self.height, *self.spacing, self.offset_x, self.offset_z]).all():
            raise ValueError("Dimensions must be finite numbers")
        if not 1 <= self.width <= 300 or not 1 <= self.height <= 330:
            raise ValueError("Image size must be 1–300 mm wide and 1–330 mm tall")
        if np.any((self.spacing < .1) | (self.spacing > 5)):
            raise ValueError("Horizontal and vertical detail spacing must be 0.10–5 mm")
        if self.side not in SIDES or self.body_tool not in range(1, 6):
            raise ValueError("Select a vertical side and a physical body filament (1–5)")
        return self

    @property
    def grid_size(self):
        self.validate()
        return tuple(np.maximum(1, np.ceil(np.array([self.width, self.height])/self.spacing)).astype(int))


def load_model(path):
    path = Path(path)
    if path.suffix.lower() not in (".stl", ".obj", ".3mf"):
        raise ValueError("Use an STL, OBJ, or 3MF model")
    if path.stat().st_size > 100*1024**2:
        raise ValueError("Model file limit is 100 MiB")
    if path.suffix.lower() == ".3mf":
        import zipfile
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > 200*1024**2:
                raise ValueError("Uncompressed 3MF exceeds 200 MiB")
    loaded = trimesh.load(path, force="scene", process=True)
    mesh = loaded.to_mesh()
    if mesh.units:
        mesh.convert_units("mm")
    if not len(mesh.faces) or len(mesh.faces) > MAX_FACES or not np.isfinite(mesh.vertices).all():
        raise ValueError(f"Model must contain 1–{MAX_FACES:,} finite triangles")
    mesh.remove_unreferenced_vertices()
    if not mesh.is_watertight:
        raise ValueError("Model is not watertight. Repair it in PrusaSlicer or CAD and export STL first.")
    mesh.fix_normals(multibody=True)
    if mesh.volume <= 0 or np.any(mesh.extents <= 0):
        raise ValueError("Model must enclose a positive volume")
    if np.any(mesh.extents > [300, 300, 330]):
        raise ValueError("Model limit is 300 × 300 × 330 mm to leave room for a prime tower and brim")
    mesh.apply_translation(-mesh.bounds[0])
    return mesh


def panel(width, height, depth=3.):
    if not np.isfinite([width, height, depth]).all() or not (1 <= width <= 300 and 1 <= height <= 330 and 1.2 <= depth <= 30):
        raise ValueError("Panel dimensions out of range (depth 1.2–30 mm)")
    mesh = trimesh.creation.box([width, depth, height])
    mesh.apply_translation(-mesh.bounds[0])
    return mesh


def projection_coordinates(points, placement):
    axis, sign, horizontal, horizontal_sign = SIDES[placement.side]
    return np.column_stack((points[:, horizontal]*horizontal_sign, points[:, 2], points[:, axis]*sign))


def image_bounds(mesh, placement):
    coords = projection_coordinates(mesh.vertices, placement)
    center = (coords[:, :2].min(axis=0)+coords[:, :2].max(axis=0))/2
    center += [placement.offset_x, placement.offset_z]
    return center-np.array([placement.width, placement.height])/2


def refine(mesh, placement, progress=lambda _: None, grid=None):
    """Split shared edges on *both* incident faces, avoiding T-junctions.

    Only edges of outward-facing triangles intersecting the image need refinement.
    All neighbors share the same midpoint and conform even outside the painted area.
    """
    placement.validate()
    vertices, faces = mesh.vertices.copy(), mesh.faces.copy()
    lower = image_bounds(mesh, placement)
    upper = lower+[placement.width, placement.height]
    axis, sign, _, _ = SIDES[placement.side]
    # Convex surfaces cannot hide one another from the projection direction.
    # Uniform image rectangles therefore need no extra triangles. Integral sums
    # let us test these rectangles in constant time, including transparent areas.
    integral = None
    if grid is not None and mesh.is_convex:
        values = np.where(grid == 0, placement.body_tool, grid).astype(np.int64)
        integral = (np.pad(values.cumsum(0).cumsum(1), ((1, 0), (1, 0))),
                    np.pad((values*values).cumsum(0).cumsum(1), ((1, 0), (1, 0))))
    for iteration in range(25):
        points = vertices[faces]
        normals = np.cross(points[:, 1]-points[:, 0], points[:, 2]-points[:, 0])
        norm = np.linalg.norm(normals, axis=1)
        coords = projection_coordinates(vertices, placement)
        projected = coords[faces, :2]
        candidates = ((normals[:, axis]*sign > .5*norm) &
                      np.all(projected.max(axis=1) >= lower, axis=1) &
                      np.all(projected.min(axis=1) <= upper, axis=1))
        if integral is not None:
            indices = np.flatnonzero(candidates)
            low = (projected[indices].min(axis=1)-lower)/[placement.width, placement.height]
            high = (projected[indices].max(axis=1)-lower)/[placement.width, placement.height]
            h, w = grid.shape
            x0 = np.clip(np.floor(low[:, 0]*w).astype(int), 0, w-1)
            x1 = np.clip(np.ceil(high[:, 0]*w).astype(int), 1, w)
            y0 = np.clip(np.floor((1-high[:, 1])*h).astype(int), 0, h-1)
            y1 = np.clip(np.ceil((1-low[:, 1])*h).astype(int), 1, h)
            area = (x1-x0)*(y1-y0)
            sums = [a[y1, x1]-a[y0, x1]-a[y1, x0]+a[y0, x0] for a in integral]
            uniform = (area > 0) & (sums[1]*area == sums[0]*sums[0])
            outside = np.any((low < -1e-9) | (high > 1+1e-9), axis=1)
            uniform &= ~outside | (sums[0] == area*placement.body_tool)
            candidates[indices[uniform]] = False
        edges = np.sort(np.stack((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]), axis=1), axis=2)
        lengths = np.linalg.norm((coords[edges[:, :, 1], :2]-coords[edges[:, :, 0], :2])/placement.spacing, axis=2)
        marked = np.unique(edges[candidates[:, None] & (lengths > .98)], axis=0)
        if not len(marked):
            result = trimesh.Trimesh(vertices, faces, process=False)
            if not result.is_watertight or not result.is_winding_consistent:
                raise RuntimeError("Refinement failed topology validation")
            return result
        if len(faces)+len(marked)*2 > MAX_FACES:
            raise ValueError("This detail setting would exceed 600,000 faces. Increase detail spacing.")
        progress(f"Refining surface: {len(faces):,} triangles")
        midpoint = {tuple(edge): len(vertices)+i for i, edge in enumerate(marked)}
        vertices = np.vstack((vertices, vertices[marked].mean(axis=1)))
        new = []
        for face, face_edges in zip(faces, edges):
            a, b, c = face
            ab, bc, ca = [midpoint.get(tuple(edge), -1) for edge in face_edges]
            mask = (ab >= 0)+2*(bc >= 0)+4*(ca >= 0)
            if mask == 0: new.append((a, b, c))
            elif mask == 1: new.extend(((a, ab, c), (ab, b, c)))
            elif mask == 2: new.extend(((b, bc, a), (bc, c, a)))
            elif mask == 4: new.extend(((c, ca, b), (ca, a, b)))
            elif mask == 3: new.extend(((b, bc, ab), (a, ab, c), (ab, bc, c)))
            elif mask == 6: new.extend(((c, ca, bc), (b, bc, a), (bc, ca, a)))
            elif mask == 5: new.extend(((a, ab, ca), (c, ca, b), (ca, ab, b)))
            else: new.extend(((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca)))
        faces = np.asarray(new, dtype=np.int64)
    raise ValueError("Mesh refinement did not converge; increase detail spacing")


def project_image(mesh, grid, placement, progress=lambda _: None):
    mesh = refine(mesh, placement, progress, grid)
    centers = mesh.triangles_center
    coords = projection_coordinates(centers, placement)
    lower = image_bounds(mesh, placement)
    uv = (coords[:, :2]-lower)/[placement.width, placement.height]
    axis, sign, _, _ = SIDES[placement.side]
    candidates = np.flatnonzero(np.all((uv >= 0) & (uv < 1), axis=1) &
                               (mesh.face_normals[:, axis]*sign > .5))
    labels = np.full(len(mesh.faces), placement.body_tool, dtype=np.uint8)
    if not len(candidates):
        raise ValueError("Image does not overlap a wall facing this direction. Adjust side or offsets.")
    # Visibility prevents painting a rear surface through a foreground wall.
    direction = np.zeros(3); direction[axis] = sign
    visible = []
    for start in range(0, len(candidates), 4000):
        ids = candidates[start:start+4000]
        progress(f"Checking visible surface: {min(start+4000, len(candidates)):,}/{len(candidates):,}")
        hidden = mesh.ray.intersects_any(centers[ids]+direction*1e-5, np.tile(direction, (len(ids), 1)))
        visible.extend(ids[~hidden])
    ids = np.asarray(visible, dtype=int)
    h, w = grid.shape
    x = np.minimum((uv[ids, 0]*w).astype(int), w-1)
    y = np.minimum(((1-uv[ids, 1])*h).astype(int), h-1)
    recipe = grid[y, x]
    ids = ids[recipe != 0]
    labels[ids] = recipe[recipe != 0]
    if not len(ids):
        raise ValueError("No visible, nontransparent image pixels overlap this surface")
    return mesh, labels
