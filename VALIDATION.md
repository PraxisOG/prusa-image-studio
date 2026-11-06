# Validation — 2026-11-06

Version **0.2.0**, validated locally on Linux with Python 3.12 and **PrusaSlicer 3.0.0-alpha13**, Flatpak commit `75e4b1ecccc80d6a0e55a375f0f7b1896d35dca3330dc52bd205c5a1e154a6a7`.

## Automated results

- **45 tests passed**, including five real OpenGL rendering tests. Coverage includes all four side orientations in both mesh painting and live textures, pointer-aligned placement and orbit direction, capped palette selection with and without dithering, transparency, physical aspect with non-square cells, sub-nozzle detail, hidden rear surfaces, straight/curved topology, preserved volume, native project metadata, 3MF instance transforms, and unit conversion.
- **Real Tk GUI smoke passed**: model loading before image selection, continuous left-drag placement, right-drag orbit, rapid quantization updates, color-limit enforcement, 0.2 mm horizontal / 0.1 mm vertical detail, both exports, completion callbacks, and accessible action buttons at 1320 × 920 and 1100 × 740. The test forbids geometry refinement during editing and verifies that export does not replace the fast preview model with its refined mesh.
- **Native 3MF roundtrip passed**: all ten virtual-extruder definitions and ordered facet annotations survive reopening/saving in PrusaSlicer. Resource renumbering is allowed, with every annotation checked to resolve to an actual mesh.
- **50:50 G-code check passed**: every pair stripe has 50 layers of each intended physical tool and 99 alternations across 100 consecutive 0.10 mm external-perimeter layers. Each solid stripe has 100 layers of its assigned tool. Prime tower, purge, infill, travel, retraction, and corner arcs are excluded. The test uses the reopened/saved project.
- **Both 0.2 × 0.1 mm smoke projects sliced successfully** in the installed slicer.
- **Both public sample projects sliced successfully**: eight image colors at 0.4 × 0.1 mm detail, with 58,036 exported triangles for the panel and 39,446 for the existing box.

## Preview timing

The GUI smoke measured **30 warm frames**, including offscreen rendering, framebuffer readback, and the Tk image update. On the 12-triangle example box at the default window size, the median was **2.06 ms**, with a **7.58 ms** maximum. Renderer: Mesa llvmpipe (LLVM 20.1.2), using software rendering.

These are local frame-rendering measurements, not a promise of the same performance for every mesh or device. They exclude model loading, image quantization, export tessellation, and initial shader setup. Drag updates are scheduled at 16 ms intervals; the visible frame rate also depends on the Tk event loop and display compositor. Complex original meshes cost more to render.

## Reproduction and limitations

Run the commands in `README.md`. The GUI writes its report and screenshot to `artifacts/gui-smoke/`; slicer integration writes inspectable projects, G-code, logs, and tool counts to `artifacts/verification/`. Those local directories are ignored by Git. The reviewed tool-count summary is included in `sample-projects/verified-toolpath-counts.json`.

The preview texture and exported painted facets approximate the same image differently: facet size limits the export's boundary accuracy. The physical nozzle and two-layer blend cycle further limit printable detail. The slicer may emit internal Voronoi retry messages while still completing successfully. **No physical print or measured filament-color validation has been performed.**

CI runs unit/OpenGL tests, the desktop smoke under Xvfb, and the public-file audit. The PrusaSlicer integration is optional and local because it needs the matching installed alpha and profiles.
