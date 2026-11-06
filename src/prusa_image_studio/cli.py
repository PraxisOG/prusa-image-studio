import argparse
import json
from pathlib import Path

from .geometry import Placement, SIDES
from .palette import make_palette, load_palette, grid_preview
from .project import build_template, ASSETS
from .render import render_mesh
from .workflow import generate, calibration, calibration_legend


def main():
    parser = argparse.ArgumentParser(description="Image → native PrusaSlicer 3 project, CMYWB 50:50 blends")
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("generate", help="Create an upright panel or project onto model geometry")
    p.add_argument("image", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--model", type=Path, help="STL, OBJ, or 3MF geometry; existing paint/settings are not retained")
    p.add_argument("--width", type=float, default=80)
    p.add_argument("--height", type=float, help="Defaults to source image aspect ratio")
    p.add_argument("--depth", type=float, default=3)
    p.add_argument("--pixel", type=float, default=.4, help="Horizontal detail spacing in mm (0.10–5)")
    p.add_argument("--pixel-z", type=float, default=.1, help="Vertical detail spacing in mm (0.10–5)")
    p.add_argument("--colors", type=int, choices=range(1, 16), default=15, metavar="1..15", help="Maximum number of image colors")
    p.add_argument("--offset-x", type=float, default=0)
    p.add_argument("--offset-z", type=float, default=0)
    p.add_argument("--side", choices=list(SIDES), default="Front (-Y)")
    p.add_argument("--fit", choices=("contain", "cover", "stretch"), default="contain")
    p.add_argument("--dither", action="store_true")
    p.add_argument("--palette", type=Path)
    p = subs.add_parser("calibration", help="Generate all 15 calibration stripes and a color legend")
    p.add_argument("output", type=Path)
    p.add_argument("--palette", type=Path)
    p = subs.add_parser("refresh-template", help="Rebuild and validate the XL/Sunlu profile using installed PrusaSlicer 3")
    p.add_argument("--output", type=Path, default=ASSETS/"xl_sunlu_petg.3mf")
    args = parser.parse_args()
    try:
        if args.command == "refresh-template":
            build_template(args.output)
            print(args.output)
            return
        recipes = load_palette(args.palette) if args.palette else make_palette()
        if args.command == "calibration":
            result = calibration(recipes, print)
        else:
            from .palette import read_image
            image = read_image(args.image)
            height = args.height if args.height is not None else args.width*image.height/image.width
            p = Placement(args.width, height, args.pixel, args.offset_x, args.offset_z, args.side, pixel_z=args.pixel_z)
            result = generate(args.image, p, args.model, args.depth, recipes, args.dither, args.fit, print, color_count=args.colors)
        report = result.export(args.output)
        render_mesh(result.mesh, result.labels, recipes, (1100, 800)).save(args.output.with_suffix(".preview.png"))
        grid_preview(result.grid, recipes).save(args.output.with_suffix(".palette.png"))
        if args.command == "calibration":
            calibration_legend(args.output.with_suffix(".legend.png"), recipes)
        print(json.dumps({"project": str(args.output.resolve()), "triangles": report["triangle_count"],
                          "size_mm": report["size_mm"], "used_recipes": sorted(map(int, set(result.labels)))}, indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
