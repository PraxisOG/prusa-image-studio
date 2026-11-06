from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .geometry import Placement, load_model, panel, project_image
from .palette import make_palette, quantize, read_image
from .project import export_project


@dataclass
class Result:
    mesh: object
    labels: np.ndarray
    grid: np.ndarray
    recipes: list
    name: str
    details: dict

    def export(self, path):
        return export_project(path, self.mesh, self.labels, self.recipes, self.name, self.details)


def generate(image, placement=None, model=None, depth=3., recipes=None, dither=False,
             fit="contain", progress=lambda _: None, color_count=15):
    placement = (placement or Placement()).validate()
    recipes = recipes or make_palette()
    source = read_image(image) if isinstance(image, (str, Path)) else image
    progress(f"Matching image to at most {color_count} filament recipes…")
    grid = quantize(source, placement.grid_size, recipes, dither=dither, fit=fit, color_count=color_count,
                    physical_size=(placement.width, placement.height))
    if model is None:
        if placement.side != "Front (-Y)":
            raise ValueError("Upright panels use Front (-Y); choose an existing model for other sides")
        mesh = panel(placement.width, placement.height, depth)
    else:
        progress("Reading model geometry…")
        mesh = load_model(model) if isinstance(model, (str, Path)) else model.copy()
    mesh, labels = project_image(mesh, grid, placement, progress)
    name = (Path(image).stem if isinstance(image, (str, Path)) else "Image")
    details = {"mode": "existing_model" if model else "upright_panel", "placement": asdict(placement),
               "dither": dither, "fit": fit, "input_image": Path(image).name if isinstance(image, (str, Path)) else "generated",
               "input_model": Path(model).name if isinstance(model, (str, Path)) else None,
               "image_color_limit": color_count,
               "geometry_only_import": bool(model)}
    return Result(mesh, labels, grid, recipes, name, details)


def calibration(recipes=None, progress=lambda _: None):
    """15 vertical stripes, 6 mm each, laid out in recipe-ID order."""
    recipes = recipes or make_palette()
    p = Placement(width=90, height=12, pixel=1.)
    grid = np.repeat(np.arange(1, 16, dtype=np.uint8)[None, :], 12, axis=0)
    mesh, labels = project_image(panel(90, 12, 3), grid, p, progress)
    return Result(mesh, labels, grid, recipes, "CMYWB 50-50 calibration",
                  {"mode": "calibration", "left_to_right_recipe_ids": list(range(1, 16)),
                   "stripe_width_mm": 6., "view": "Front (-Y)", "placement": asdict(p)})


def calibration_legend(path, recipes):
    image = Image.new("RGB", (1050, 640), "#151B24")
    draw = ImageDraw.Draw(image)
    draw.text((28, 20), "CMYWB / 50:50 CALIBRATION — left to right on the front wall", fill="white", font_size=22)
    for i, r in enumerate(recipes):
        row, column = divmod(i, 3)
        x, y = 28+column*345, 78+row*102
        draw.rounded_rectangle((x, y, x+62, y+64), radius=7, fill=r.color)
        draw.text((x+76, y+4), f"{r.id:02d}  {r.name}", fill="white", font_size=17)
        draw.text((x+76, y+30), "Tool "+" / ".join(map(str, r.tools)), fill="#B5C0CF", font_size=15)
    draw.text((28, 607), "Predicted colors; compare your printed Sunlu PETG and adjust the palette if needed.", fill="#B5C0CF", font_size=17)
    image.save(path)
