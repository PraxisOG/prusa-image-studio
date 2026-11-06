"""Native v3 project writer, based on a project emitted by PrusaSlicer itself."""
from datetime import datetime, timezone
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import zipfile
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from . import __version__
from .palette import DEFAULT_HEX

ASSETS = Path(__file__).parent / "assets"
PROJECT_FILE = "Metadata/PrusaSlicer3_project.json"
PAINT_FILE = "Metadata/Slic3r_facets_annotation.json"
CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def facet_code(tool):
    if type(tool) is not int or not 1 <= tool <= 15:
        raise ValueError("Recipe ID must be 1–15")
    return f"{tool << 2:X}" if tool < 3 else f"{tool-3:X}C"


def slicer_command():
    import shutil
    override = os.environ.get("PRUSA_IMAGE_SLICER")
    if override:
        return [override]
    if shutil.which("flatpak"):
        result = subprocess.run(["flatpak", "info", "com.prusa3d.PrusaSlicer//beta"],
                                capture_output=True, timeout=20)
        if result.returncode == 0:
            return ["flatpak", "run", "com.prusa3d.PrusaSlicer//beta"]
    for executable in ("prusa-slicer", "PrusaSlicer"):
        if shutil.which(executable):
            return [executable]
    raise RuntimeError("PrusaSlicer 3 was not found. Open the exported 3MF with File → Open Project.")


def run_slicer(args, expected, workdir, timeout=600):
    expected = Path(expected)
    previous_mtime = expected.stat().st_mtime_ns if expected.exists() else None
    command = slicer_command()
    if command[0] == "flatpak":
        command.insert(2, "--unshare=network")
    command += ["--datadir", str(Path(workdir)/"slicer-data"), "--threads", "4"]
    command += [str(x) for x in args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    log = Path(workdir)/"slicer.log"
    log.write_text(result.stdout+result.stderr)
    if (result.returncode or not expected.is_file() or expected.stat().st_size == 0
            or expected.stat().st_mtime_ns == previous_mtime):
        raise RuntimeError(f"PrusaSlicer did not produce the expected file. See {log}")
    return log


def build_template(output):
    """Developer/revalidation command; discover exact installed profiles."""
    from .geometry import panel
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = Path.home()/".cache"/"prusa-image-studio"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="template-", dir=cache) as tmp:
        work = Path(tmp)
        printer = "Prusa XL 5T 0.4, 0.4, 0.4, 0.4, 0.4"
        query = work/"profiles.json"
        run_slicer(["--printer-profile", printer, "--query-print-tool-filament-profiles", "--output", query], query, work)
        profiles = json.loads(query.read_text())
        profile = next((p for p in profiles["print_profiles"] if p["name"] == "0.10mm"), None)
        if profile is None or "Sunlu PETG@XL 0.4" not in profile["filament_profiles"]:
            raise RuntimeError("Installed slicer lacks the XL 0.10 mm / Sunlu PETG profiles")
        source = work/"seed.stl"
        # ASCII avoids a binary-STL parsing issue reproduced in alpha12.
        panel(10, 10, 3).export(source, file_type="stl_ascii")
        args = ["--printer-profile", printer, "--print-profile", "0.10mm",
                "--tool-print-profile", ",".join(profile["tool_print_profiles"]),
                "--material-profile", ",".join(["Sunlu PETG@XL 0.4"]*5),
                "--no-binary-gcode", "--export-3mf", "--output", output, source]
        run_slicer(args, output, work)
    with zipfile.ZipFile(output) as z:
        entries = {name: z.read(name) for name in z.namelist()}
        data = json.loads(entries[PROJECT_FILE])
    hw = data["config_containers"][0]["preset"]["hw_config"]
    if hw["legacy_printer_model"] != "XL5IS" or hw["tool_count"] != 5:
        raise RuntimeError("Template did not resolve to an Original XL with five tools")
    if any(t["features"]["nozzle_diameter"] != .4 or t["features"]["nozzle_high_flow"]
           for t in hw["tools"].values()):
        raise RuntimeError("Template nozzles must be standard-flow 0.4 mm")
    # The slicer records the temporary seed's absolute path. A distributable
    # stock template needs neither source locations nor printer connections.
    for obj in data["objects"]:
        for volume in obj["volumes"]: volume.pop("source", None)
    for cc in data["config_containers"]: cc.pop("physical_printer", None)
    entries[PROJECT_FILE] = json.dumps(data, separators=(",", ":")).encode()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items(): z.writestr(name, content)


def model_xml(mesh, name):
    lo, hi = mesh.bounds
    # Reserve the rear of the bed for the prime tower. Center the model at Y=160.
    offset = np.array([180., 160., 0.])-(lo+hi)/2
    offset[2] = -lo[2]
    out = io.StringIO()
    out.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{CORE}">\n')
    out.write('<metadata name="Application">Prusa Image Studio</metadata><resources><object id="1"><mesh><vertices>')
    for v in mesh.vertices:
        out.write(f'<vertex x="{v[0]:.9g}" y="{v[1]:.9g}" z="{v[2]:.9g}"/>')
    out.write('</vertices><triangles>')
    for a, b, c in mesh.faces:
        out.write(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>')
    out.write('</triangles></mesh></object>')
    for id_, child in ((2, 1), (3, 2)):
        out.write(f'<object id="{id_}" name={quoteattr(name)}><components><component objectid="{child}"/></components></object>')
    out.write('</resources><build><item objectid="3" transform="1 0 0 0 1 0 0 0 1 ')
    out.write(' '.join(f'{x:.9g}' for x in offset))
    out.write('"/></build></model>')
    return out.getvalue()


def export_project(path, mesh, labels, recipes, name="Image panel", metadata=None, template=None):
    path = Path(path)
    labels = np.asarray(labels)
    if labels.shape != (len(mesh.faces),) or np.any((labels < 1) | (labels > 15)):
        raise ValueError("Every triangle must have a valid recipe")
    if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
        raise ValueError("Cannot export a mesh with invalid topology")
    if np.any(mesh.extents > [300, 300, 330]):
        raise ValueError("Model exceeds the supported 300 × 300 × 330 mm working area")
    if [r.id for r in recipes] != list(range(1, 16)):
        raise ValueError("Expected the five physical tools and ten pair blends")
    template = Path(template or ASSETS/"xl_sunlu_petg.3mf")
    with zipfile.ZipFile(template) as z:
        data = json.loads(z.read(PROJECT_FILE))
        content_types, relationships = z.read("[Content_Types].xml"), z.read("_rels/.rels")
    data["project"]["id"] = str(uuid.uuid4())
    data["objects"] = [{"id": 3, "volumes": [{"id": 2, "type": "ModelPart", "volume_settings": {}}],
                        "object_settings": {"extruder": 4, "wipe_into_objects": False}}]
    cc = data["config_containers"][0]
    # V3 stores this per configuration container, not in the legacy spectrum file.
    cc["virtual_extruders"] = [r.virtual() for r in recipes[5:]]
    cfg = cc["configuration"]
    cfg["project_settings"]["extruder_colour"] = [r.color for r in recipes[:5]]
    cfg["filament_settings"]["filament_colour"] = [r.color for r in recipes[:5]]
    settings = cfg["print_settings"]
    settings.update(layer_height=.1, fill_pattern="gyroid", fill_density={"is_percent": True, "value": 10.},
                    support_material="none", bed_temperature_extruder=4,
                    perimeter_extruder=4, infill_extruder=4, solid_infill_extruder=4,
                    brim_width=5., brim_type="outer_only")
    # Keep the prime tower outside the object's XY bounds for all supported sizes.
    cc["beds"][0]["wipe_tower"].update(x=10., y=330., rotation_angle=0.)
    cc["beds"][0]["custom_gcode"] = None
    cc.pop("physical_printer", None)
    paint = [{"id": 2, "mmSegmentationFacetsVersion": 1,
              "mmSegmentationFacets": [{"triangle": i, "dividing": facet_code(int(label))}
                                        for i, label in enumerate(labels)]}]
    record = {"generator": f"Prusa Image Studio {__version__}", "created_utc": datetime.now(timezone.utc).isoformat(),
              "palette": [{"id": r.id, "name": r.name, "color": r.color, "tools": list(r.tools)} for r in recipes],
              "triangle_count": len(mesh.faces), "size_mm": mesh.extents.tolist(),
              "base_material": "Sunlu PETG", "layer_height_mm": .1,
              "blend_ratios": "50:50 only", "physical_print_validated": False,
              "details": metadata or {}}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix=".3mf", dir=path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", relationships)
            z.writestr("3D/3dmodel.model", model_xml(mesh, name))
            z.writestr(PROJECT_FILE, json.dumps(data, separators=(",", ":")))
            z.writestr(PAINT_FILE, json.dumps(paint, separators=(",", ":")))
            z.writestr("Metadata/PrusaImageStudio.json", json.dumps(record, indent=2))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record


def open_project(path):
    # Pass the project as an argument; never interpolate paths into a shell command.
    subprocess.Popen(slicer_command()+[str(Path(path).resolve())],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
