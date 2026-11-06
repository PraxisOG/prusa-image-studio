"""Five physical filaments and ten equal pair blends.

The prediction is a Python adaptation of Prusa's MIT-licensed v7 model.
See THIRD_PARTY_NOTICES.md. Predicted color is separate from the tool recipe.
"""
from dataclasses import dataclass
from itertools import combinations
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy.spatial import cKDTree

NAMES = ("Cyan", "Magenta", "Yellow", "White", "Black")
DEFAULT_HEX = ("#00BCD4", "#DC3998", "#EED238", "#E9E9E4", "#292F39")
XYZ = np.array([[.4124564, .3575761, .1804375],
                [.2126729, .7151522, .0721750], [.0193339, .1191920, .9503041]])
WHITE = np.array([.95047, 1., 1.08883])


def hex_rgb(value):
    if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
        raise ValueError("Colors must use #RRGGBB")
    try:
        return np.array([int(value[i:i+2], 16) for i in (1, 3, 5)]) / 255.
    except ValueError as exc:
        raise ValueError("Colors must use #RRGGBB") from exc


def rgb_hex(rgb):
    return "#" + "".join(f"{v:02X}" for v in np.floor(np.clip(rgb, 0, 1)*255+.5).astype(int))


def linear(rgb):
    return np.where(rgb <= .04045, rgb / 12.92, ((np.maximum(rgb, 0)+.055)/1.055)**2.4)


def srgb(rgb):
    return np.where(rgb <= .0031308, rgb*12.92, 1.055*np.maximum(rgb, 0)**(1/2.4)-.055)


def rgb_lab(rgb):
    xyz = linear(np.asarray(rgb)) @ XYZ.T / WHITE
    delta = 6/29
    f = np.where(xyz > delta**3, np.cbrt(xyz), xyz/(3*delta**2)+4/29)
    return np.stack((116*f[..., 1]-16, 500*(f[..., 0]-f[..., 1]),
                     200*(f[..., 1]-f[..., 2])), axis=-1)


def lab_rgb(lab):
    lab = np.asarray(lab)
    fy = (lab[..., 0]+16)/116
    f = np.stack((fy+lab[..., 1]/500, fy, fy-lab[..., 2]/200), axis=-1)
    delta = 6/29
    xyz = np.where(f > delta, f**3, 3*delta**2*(f-4/29))*WHITE
    return np.clip(srgb(xyz @ np.linalg.inv(XYZ).T), 0, 1)


def predict_pair(a, b):
    rgb = np.array([hex_rgb(a), hex_rgb(b)])
    base = srgb(np.mean(np.cbrt(linear(rgb)), axis=0)**3)
    lab = rgb_lab(hex_rgb(rgb_hex(base)))  # upstream rounds base to 8-bit sRGB
    gap = np.ptp(rgb_lab(rgb)[:, 0])
    w = 1.375
    lab[0] += (-.0477*gap-2.112-.06*max(0, gap-15))*w
    chroma = np.linalg.norm(lab[1:])
    if chroma >= .01:
        lab[1:] *= max(0, chroma+(.278*lab[0]-15.58)*w)/chroma
    chroma = np.linalg.norm(lab[1:])
    hue = np.degrees(np.arctan2(lab[2], lab[1])) % 360
    distance = abs(hue-210)
    if chroma >= 1 and distance < 30:
        angle = np.radians(hue+10.38*(1-distance/30)*w)
        lab[1:] = chroma*np.array([np.cos(angle), np.sin(angle)])
    return rgb_hex(lab_rgb(lab))


@dataclass(frozen=True)
class Recipe:
    id: int
    name: str
    color: str
    tools: tuple

    def virtual(self):
        return {"id": self.id, "kind": "fullspectrum", "color": self.color,
                "components": [{"extruder": t, "ratio": .5} for t in self.tools]}


def make_palette(colors=DEFAULT_HEX, overrides=None):
    if len(colors) != 5:
        raise ValueError("Exactly five physical filament colors required")
    colors = [rgb_hex(hex_rgb(c)) for c in colors]
    overrides = overrides or {}
    recipes = [Recipe(i+1, name, colors[i], (i+1,)) for i, name in enumerate(NAMES)]
    for a, b in combinations(range(5), 2):
        id_ = len(recipes)+1
        color = overrides.get(str(id_), overrides.get(id_, predict_pair(colors[a], colors[b])))
        recipes.append(Recipe(id_, f"{NAMES[a]} + {NAMES[b]} 50:50", rgb_hex(hex_rgb(color)), (a+1, b+1)))
    return recipes


def save_palette(path, recipes):
    Path(path).write_text(json.dumps({"version": 1,
        "physical_colors": [r.color for r in recipes[:5]],
        "blend_colors": {str(r.id): r.color for r in recipes[5:]}}, indent=2)+"\n")


def load_palette(path):
    data = json.loads(Path(path).read_text())
    if data.get("version") != 1:
        raise ValueError("Unsupported palette version")
    return make_palette(data["physical_colors"], data.get("blend_colors"))


def read_image(path):
    with Image.open(path) as raw:
        if raw.width*raw.height > 40_000_000:
            raise ValueError("Image limit is 40 megapixels")
        return ImageOps.exif_transpose(raw).convert("RGBA")


def select_colors(labs, palette_lab, visible, count):
    """Choose at most count printable recipes by weighted Lab reconstruction error."""
    if type(count) is not int or not 1 <= count <= len(palette_lab):
        raise ValueError(f"Image color count must be 1–{len(palette_lab)}")
    if count == len(palette_lab):
        return np.arange(count)
    samples = labs.reshape(-1, 3)[visible.ravel()]
    if not len(samples):
        return np.arange(count)
    if len(samples) > 8192:
        samples = samples[np.linspace(0, len(samples)-1, 8192, dtype=int)]
    distance = np.sum((samples[:, None, :]-palette_lab[None, :, :])**2, axis=2)
    chosen, remaining = [], list(range(len(palette_lab)))
    error = np.full(len(samples), np.inf)
    for _ in range(count):
        scores = np.minimum(distance[:, remaining], error[:, None]).sum(axis=0)
        pick = remaining.pop(int(np.argmin(scores)))
        chosen.append(pick)
        error = np.minimum(error, distance[:, pick])
    # Small exact swap search improves the greedy palette without adding colors.
    for _ in range(3):
        improved = False
        for i in range(count):
            others = chosen[:i]+chosen[i+1:]
            baseline = distance[:, others].min(axis=1) if others else np.full(len(samples), np.inf)
            candidates = [chosen[i]]+remaining
            scores = np.minimum(distance[:, candidates], baseline[:, None]).sum(axis=0)
            best = int(np.argmin(scores))
            if scores[best] < scores[0]-1e-6:
                old = chosen[i]; chosen[i] = candidates[best]
                remaining.remove(chosen[i]); remaining.append(old)
                improved = True
        if not improved: break
    return np.array(sorted(chosen))


def quantize(image, size, recipes, dither=False, fit="contain", color_count=15, physical_size=None):
    """Returns a grid of 1-based recipe IDs; transparent pixels are zero."""
    size = tuple(map(int, size))
    if min(size) < 1 or max(size) > 4096 or size[0]*size[1] > 4_000_000:
        raise ValueError("Image grid limit is 4096 per axis and 4 million pixels; increase detail spacing")
    target_ratio = (physical_size[0]/physical_size[1]) if physical_size else size[0]/size[1]
    source_ratio = image.width/image.height
    if fit == "cover":
        if source_ratio > target_ratio:
            crop_width, crop_height = image.height*target_ratio, image.height
        else:
            crop_width, crop_height = image.width, image.width/target_ratio
        left, top = (image.width-crop_width)/2, (image.height-crop_height)/2
        image = image.convert("RGBA").resize(size, Image.Resampling.LANCZOS,
                    box=(left, top, left+crop_width, top+crop_height))
    elif fit == "contain":
        # Grid cells can be wider than they are tall. Preserve physical aspect,
        # not the aspect ratio of the anisotropic pixel array.
        if source_ratio > target_ratio:
            fitted = (size[0], max(1, round(size[1]*target_ratio/source_ratio)))
        else:
            fitted = (max(1, round(size[0]*source_ratio/target_ratio)), size[1])
        small = image.convert("RGBA").resize(fitted, Image.Resampling.LANCZOS)
        image = Image.new("RGBA", size)
        image.paste(small, ((size[0]-small.width)//2, (size[1]-small.height)//2))
    elif fit == "stretch":
        image = image.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    else:
        raise ValueError("Image fit must be contain, cover, or stretch")
    rgba = np.asarray(image)/255.
    # Semi-transparent edges composite onto the white body, not black.
    rgb = rgba[..., :3]*rgba[..., 3:] + hex_rgb(recipes[3].color)*(1-rgba[..., 3:])
    labs = rgb_lab(rgb)
    colors = np.array([hex_rgb(r.color) for r in recipes])
    palette_lab = rgb_lab(colors)
    selected = select_colors(labs, palette_lab, rgba[..., 3] >= .1, color_count)
    palette_lab = palette_lab[selected]
    tree = cKDTree(palette_lab)
    if not dither:
        indices = tree.query(labs.reshape(-1, 3))[1].reshape(labs.shape[:2])
    else:
        # Vectorized ordered dithering keeps fine grids interactive. Interpolate
        # only between the two nearest *selected* recipes, so the cap is strict.
        neighbors = tree.query(labs.reshape(-1, 3), k=min(2, len(selected)))[1]
        if len(selected) == 1:
            indices = np.zeros(labs.shape[:2], dtype=int)
        else:
            a, b = palette_lab[neighbors[:, 0]], palette_lab[neighbors[:, 1]]
            delta = b-a
            fraction = np.clip(np.sum((labs.reshape(-1, 3)-a)*delta, axis=1)/np.maximum(np.sum(delta*delta, axis=1), 1e-10), 0, 1)
            bayer = np.array([[0., 2.], [3., 1.]])
            for _ in range(2):
                bayer = np.block([[4*bayer, 4*bayer+2], [4*bayer+3, 4*bayer+1]])
            y, x = np.indices(labs.shape[:2])
            threshold = ((bayer[y % 8, x % 8]+.5)/64).ravel()
            indices = np.where(fraction > threshold, neighbors[:, 1], neighbors[:, 0]).reshape(labs.shape[:2])
    ids = selected[indices]+1
    ids[rgba[..., 3] < .1] = 0
    return ids.astype(np.uint8)


def grid_preview(ids, recipes, background="#202630"):
    colors = np.array([hex_rgb(background)] + [hex_rgb(r.color) for r in recipes])
    return Image.fromarray(np.rint(colors[ids]*255).astype(np.uint8))
