"""Local model-first editor. Texture previews never tessellate the print mesh."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk, ImageOps

from . import __version__
from .geometry import Placement, SIDES, load_model, panel, projection_coordinates
from .palette import make_palette, read_image, quantize, grid_preview, save_palette, load_palette
from .project import open_project
from .viewport import Viewport, Camera
from .workflow import generate, calibration, calibration_legend

BG, PANEL, TEXT, MUTED, ACCENT = "#10161E", "#1B2531", "#E8EEF5", "#A0B0C2", "#F39751"


class Studio:
    def __init__(self, root):
        self.root = root
        root.title("Prusa Image Studio — CMYWB 50:50")
        root.geometry("1320x920")
        root.minsize(1100, 740)
        root.configure(bg=BG)
        self.recipes = make_palette()
        self.image_path = self.model_path = self.source = self.mesh = None
        self.result = self.export_path = self.grid = None
        self.model_busy = self.export_busy = False
        self.quant_pending = set()
        self.revision = self.model_revision = 0
        self.suspend_changes = False
        self.messages = queue.Queue()
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="studio")
        self.camera = Camera()
        self.drag = None
        self.render_after = self.quant_after = self.poll_after = None
        self.render_tk = self.render_item = None
        self.frame_ms = 0.
        self.frame_count = 0
        self.last_placement = Placement(pixel=.4, pixel_z=.1)
        try:
            self.viewport = Viewport()
            self.graphics_error = None
        except Exception as exc:
            self.viewport = None
            self.graphics_error = str(exc)
        self.style()
        self.status = tk.StringVar(value="Load a model, then add an image to its side wall.")
        self.mode = tk.StringVar(value="Existing model")
        self.side = tk.StringVar(value="Front (-Y)")
        self.fit = tk.StringVar(value="contain")
        self.dither = tk.BooleanVar(value=False)
        self.color_count = tk.StringVar(value="15")
        self.gesture = tk.StringVar(value="Move image")
        self.vars = {key: tk.StringVar(value=value) for key, value in
                     {"width":"64", "height":"48", "pixel":"0.4", "pixel_z":"0.1",
                      "panel_width":"80", "panel_height":"60", "depth":"3",
                      "offset_x":"0", "offset_z":"0"}.items()}
        self.layout()
        for key in ("width", "height", "pixel", "pixel_z"):
            self.vars[key].trace_add("write", self.schedule_quantize)
        for key in ("offset_x", "offset_z"):
            self.vars[key].trace_add("write", self.schedule_render)
        for key in ("panel_width", "panel_height", "depth"):
            self.vars[key].trace_add("write", self.panel_changed)
        for var in (self.fit, self.dither, self.color_count): var.trace_add("write", self.schedule_quantize)
        self.mode.trace_add("write", self.mode_changed)
        self.side.trace_add("write", self.side_changed)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_after = root.after(25, self.poll)

    @property
    def busy(self):
        return self.model_busy or self.export_busy or bool(self.quant_pending) or self.quant_after is not None

    def style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("DejaVu Sans", 10), background=BG, foreground=TEXT)
        style.configure("TButton", background="#28384A", foreground=TEXT, padding=(10, 7), borderwidth=0)
        style.map("TButton", background=[("active", "#344A61"), ("disabled", "#202B38")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#161D26", font=("DejaVu Sans", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#FFB47A"), ("disabled", "#594739")])
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, padding=5, insertcolor=TEXT)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        for widget in ("TCheckbutton", "TRadiobutton"):
            style.configure(widget, background=BG, foreground=TEXT)
            style.map(widget, background=[("active", BG)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    def label(self, parent, text, size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=BG, fg=color, anchor="w", justify="left",
                        font=("DejaVu Sans", size, "bold" if bold else "normal"), **kwargs)

    def fields(self, parent, entries):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="x")
        for row, (key, label) in enumerate(entries):
            self.label(frame, label, 9, MUTED).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(frame, textvariable=self.vars[key], width=8).grid(row=row, column=1, sticky="e", padx=(8, 0), pady=3)
        return frame

    def layout(self):
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=24, pady=(18, 10))
        self.label(top, "Prusa Image Studio", 23, bold=True).pack(side="left")
        self.label(top, "XL 5 tools  /  CMYWB  /  50:50", 10, MUTED).pack(side="right")
        self.label(self.root, "Load a model. Place an image. Print it into the side wall.", 11, MUTED).pack(anchor="w", padx=25, pady=(0, 16))
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=24)
        sidebar = tk.Frame(body, bg=BG, width=286)
        sidebar.pack(side="left", fill="y", padx=(0, 18))
        sidebar.pack_propagate(False)
        scroller = tk.Canvas(sidebar, bg=BG, highlightthickness=0, width=264)
        scroll = ttk.Scrollbar(sidebar, orient="vertical", command=scroller.yview)
        scroll.pack(side="right", fill="y")
        scroller.pack(side="left", fill="both", expand=True)
        scroller.configure(yscrollcommand=scroll.set)
        controls = tk.Frame(scroller, bg=BG)
        scroller.create_window(0, 0, anchor="nw", window=controls, width=264)
        controls.bind("<Configure>", lambda _: scroller.configure(scrollregion=scroller.bbox("all")))
        self.label(controls, "01  MODEL", 10, ACCENT, True).pack(anchor="w", pady=(0, 8))
        ttk.Combobox(controls, textvariable=self.mode, state="readonly", values=("Existing model", "Picture panel")).pack(fill="x", pady=(0, 8))
        self.model_button = ttk.Button(controls, text="Load model…", command=self.choose_model)
        self.model_button.pack(fill="x")
        self.model_label = self.label(controls, "STL, OBJ or 3MF geometry", 9, MUTED, wraplength=255)
        self.model_label.pack(fill="x", pady=(6, 8))
        self.panel_fields = self.fields(controls, (("panel_width", "Panel width · mm"), ("panel_height", "Panel height · mm"), ("depth", "Panel depth · mm")))
        self.panel_fields.pack_forget()
        self.image_heading = self.label(controls, "02  IMAGE & PLACEMENT", 10, ACCENT, True)
        self.image_heading.pack(anchor="w", pady=(12, 8))
        self.image_button = ttk.Button(controls, text="Add image…", command=self.choose_image, state="disabled")
        self.image_button.pack(fill="x")
        self.image_label = self.label(controls, "Load a model first", 9, MUTED, wraplength=255)
        self.image_label.pack(fill="x", pady=(6, 8))
        self.side_combo = ttk.Combobox(controls, textvariable=self.side, state="readonly", values=list(SIDES))
        self.side_combo.pack(fill="x", pady=(0, 8))
        self.fields(controls, (("width", "Image width · mm"), ("height", "Image height · mm"), ("offset_x", "Move right · mm"), ("offset_z", "Move up · mm")))
        ttk.Button(controls, text="Fit image to this side", command=self.fit_image).pack(fill="x", pady=8)
        fits = tk.Frame(controls, bg=BG)
        fits.pack(fill="x")
        self.label(fits, "Image fit", 9, MUTED).pack(side="left")
        ttk.Combobox(fits, textvariable=self.fit, state="readonly", width=13, values=("contain", "cover", "stretch")).pack(side="right")
        self.label(controls, "03  COLOR & DETAIL", 10, ACCENT, True).pack(anchor="w", pady=(18, 8))
        colors = tk.Frame(controls, bg=BG)
        colors.pack(fill="x")
        self.label(colors, "Maximum image colors", 9, MUTED).pack(side="left")
        ttk.Combobox(colors, textvariable=self.color_count, values=list(range(1, 16)), state="readonly", width=4).pack(side="right")
        ttk.Checkbutton(controls, text="Dither selected colors", variable=self.dither).pack(anchor="w", pady=(8, 8))
        self.fields(controls, (("pixel", "Horizontal detail · mm"), ("pixel_z", "Vertical detail · mm")))
        self.label(controls, "Both accept 0.10–5 mm. Vertical detail follows 0.10 mm layers; a full 50:50 cycle uses two layers. Fine horizontal marks may not resolve with a 0.4 mm nozzle.", 9, MUTED, wraplength=255).pack(anchor="w", pady=(8, 16))
        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        toolbar = tk.Frame(main, bg=BG)
        toolbar.pack(fill="x", pady=(0, 8))
        for text in ("Move image", "Orbit model"):
            ttk.Radiobutton(toolbar, text=text, value=text, variable=self.gesture).pack(side="left", padx=(0, 12))
        ttk.Button(toolbar, text="Face selected side", command=self.front).pack(side="right")
        self.canvas = tk.Canvas(main, bg="#111821", highlightthickness=1, highlightbackground="#263547")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.schedule_render)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.begin_drag(e, self.gesture.get()))
        self.canvas.bind("<ButtonPress-3>", lambda e: self.begin_drag(e, "Orbit model"))
        self.canvas.bind("<B1-Motion>", self.move_drag)
        self.canvas.bind("<B3-Motion>", self.move_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda _: setattr(self, "drag", None))
        self.canvas.bind("<ButtonRelease-3>", lambda _: setattr(self, "drag", None))
        self.canvas.bind("<Double-Button-1>", self.front)
        self.canvas.bind("<Button-4>", lambda _: self.change_zoom(1.1))
        self.canvas.bind("<Button-5>", lambda _: self.change_zoom(1/1.1))
        self.canvas.bind("<MouseWheel>", lambda e: self.change_zoom(1.1 if e.delta > 0 else 1/1.1))
        self.info = self.label(main, "Drag image to place it · right-drag to orbit · scroll to zoom", 9, MUTED)
        self.info.pack(fill="x", pady=8)
        bottom = tk.Frame(main, bg=BG)
        bottom.pack(fill="x", pady=(2, 10))
        self.preview_image = tk.Label(bottom, bg=PANEL)
        self.preview_image.pack(side="left", padx=(0, 14))
        self.show_image(Image.new("RGB", (210, 125), PANEL))
        palette_frame = tk.Frame(bottom, bg=BG)
        palette_frame.pack(side="left", fill="both", expand=True)
        self.palette_label = self.label(palette_frame, "5 FILAMENTS + 10 EQUAL BLENDS", 9, MUTED, True)
        self.palette_label.pack(anchor="w", pady=(0, 5))
        self.swatches = tk.Frame(palette_frame, bg=BG)
        self.swatches.pack(fill="x")
        self.draw_swatches()
        buttons = tk.Frame(palette_frame, bg=BG)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Save palette…", command=self.save_colors).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Load palette…", command=self.load_colors).pack(side="left")
        ttk.Button(buttons, text="About", command=self.about).pack(side="right")
        self.label(palette_frame, "Orange outlines mark image colors. Click to calibrate a swatch.", 9, MUTED).pack(anchor="w", pady=(6, 0))
        actions = tk.Frame(main, bg=BG)
        actions.pack(fill="x", pady=(0, 14))
        self.export_button = ttk.Button(actions, text="Export 3MF…", style="Accent.TButton", command=self.export, state="disabled")
        self.export_button.pack(side="left", padx=(0, 8))
        self.open_button = ttk.Button(actions, text="Open in PrusaSlicer", command=self.open_export, state="disabled")
        self.open_button.pack(side="left")
        self.calibration_button = ttk.Button(actions, text="50:50 calibration…", command=self.build_calibration)
        self.calibration_button.pack(side="right")
        foot = tk.Frame(self.root, bg="#19232F", padx=24, pady=10)
        foot.pack(fill="x")
        tk.Label(foot, textvariable=self.status, bg="#19232F", fg=TEXT, anchor="w", font=("DejaVu Sans", 9), wraplength=1250).pack(fill="x")
        self.label(self.root, "Sunlu PETG  ·  0.4 mm nozzles  ·  0.10 mm layers  ·  T1 C / T2 M / T3 Y / T4 W / T5 B", 9, MUTED).pack(anchor="w", padx=24, pady=(8, 10))

    def submit(self, kind, revision, work):
        future = self.workers.submit(work)
        def done(f):
            try: self.messages.put((kind, revision, f.result(), None))
            except Exception as exc: self.messages.put((kind, revision, None, str(exc)))
        future.add_done_callback(done)

    def mode_changed(self, *_):
        self.model_revision += 1
        self.model_busy = False
        existing = self.mode.get() == "Existing model"
        self.model_button.configure(state="normal" if existing else "disabled")
        if existing:
            self.panel_fields.pack_forget()
            if self.model_path:
                self.load_model_path(self.model_path)
            else:
                self.mesh = None
                self.model_label.configure(text="STL, OBJ or 3MF geometry")
                self.image_button.configure(state="disabled")
                self.export_button.configure(state="disabled")
                self.schedule_render()
        else:
            self.panel_fields.pack(fill="x", before=self.image_heading)
            self.panel_changed()

    def panel_changed(self, *_):
        if self.suspend_changes or self.mode.get() != "Picture panel": return
        try:
            mesh = panel(float(self.vars['panel_width'].get()), float(self.vars['panel_height'].get()), float(self.vars['depth'].get()))
            self.install_mesh(mesh, "Upright picture panel")
        except ValueError as exc: self.status.set(str(exc))

    def load_model_path(self, path):
        self.model_revision += 1
        self.model_busy = True
        self.status.set("Loading model geometry…")
        self.image_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.submit("model", self.model_revision, lambda: (load_model(path), path))

    def choose_model(self):
        if self.export_busy: return
        path = filedialog.askopenfilename(title="Load model geometry", filetypes=[("3D models", "*.stl *.obj *.3mf")])
        if path:
            self.load_model_path(path)

    def install_mesh(self, mesh, name):
        self.mesh = mesh
        if self.viewport: self.viewport.set_mesh(mesh)
        self.camera.zoom = 1.
        self.model_label.configure(text=f"{name}\n"+" × ".join(f"{n:.1f}" for n in mesh.extents)+" mm")
        self.image_button.configure(state="normal")
        if self.source is None:
            self.image_label.configure(text="PNG, JPEG, WebP or other image")
            self.status.set("Model loaded. Add an image to place on its side.")
        else: self.fit_image()
        self.schedule_render()

    def choose_image(self):
        if self.mesh is None or self.model_busy or self.export_busy: return
        path = filedialog.askopenfilename(title="Add an image", filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"), ("All files", "*")])
        if path:
            try:
                self.source = read_image(path)
                self.image_path = path
                self.image_label.configure(text=Path(path).name)
                self.front()
                self.fit_image()
            except Exception as exc: messagebox.showerror("Could not load image", str(exc))

    def placement(self):
        p = Placement(**{key: float(self.vars[key].get()) for key in ("width", "height", "pixel", "pixel_z", "offset_x", "offset_z")}, side=self.side.get()).validate()
        self.last_placement = p
        return p

    def fit_image(self):
        if self.mesh is None or self.source is None: return
        coords = projection_coordinates(self.mesh.vertices, Placement(side=self.side.get()))
        width, height = np.ptp(coords[:, :2], axis=0)*.9
        ratio = self.source.width/self.source.height
        width = min(width, height*ratio)
        height = width/ratio
        self.suspend_changes = True
        for key, value in dict(width=width, height=height, offset_x=0, offset_z=0).items():
            self.vars[key].set(f"{value:.3f}")
        self.suspend_changes = False
        self.schedule_quantize()
        self.schedule_render()

    def side_changed(self, *_):
        self.front()
        self.fit_image()

    def schedule_quantize(self, *_):
        if self.suspend_changes: return
        self.revision += 1
        if self.quant_after: self.root.after_cancel(self.quant_after)
        self.quant_after = None
        if self.source is not None and self.mesh is not None:
            self.quant_after = self.root.after(160, self.start_quantize)
        self.schedule_render()

    def start_quantize(self):
        self.quant_after = None
        try:
            p = self.placement()
            count = int(self.color_count.get())
            source, recipes = self.source.copy(), list(self.recipes)
            fit, dither = self.fit.get(), self.dither.get()
        except (ValueError, tk.TclError) as exc:
            self.status.set(f"Check placement: {exc}")
            return
        revision = self.revision
        self.quant_pending.add(revision)
        self.status.set("Matching image colors…")
        self.submit("quant", revision, lambda: quantize(source, p.grid_size, recipes, dither=dither,
                    fit=fit, color_count=count, physical_size=(p.width, p.height)))

    def show_image(self, image, physical_size=None):
        if physical_size:
            width, height = physical_size
            scale = min(210/width, 125/height)
            image = image.resize((max(1, round(width*scale)), max(1, round(height*scale))), Image.Resampling.NEAREST)
        else: image = ImageOps.contain(image.convert("RGB"), (210, 125), method=Image.Resampling.NEAREST)
        background = Image.new("RGB", (210, 125), PANEL)
        background.paste(image, ((210-image.width)//2, (125-image.height)//2))
        self.image_tk = ImageTk.PhotoImage(background)
        self.preview_image.configure(image=self.image_tk, width=210, height=125)

    def draw_swatches(self):
        for w in self.swatches.winfo_children(): w.destroy()
        used = set(np.unique(self.grid))-{0} if self.grid is not None else set()
        for i, r in enumerate(self.recipes):
            text = ("C", "M", "Y", "W", "B")[i] if i < 5 else str(r.id)
            button = tk.Button(self.swatches, text=text, bg=r.color, fg="#101820" if sum(int(r.color[j:j+2], 16) for j in (1, 3, 5)) > 390 else "white",
                               activebackground=r.color, relief="flat", width=4, height=1, cursor="hand2", highlightthickness=2,
                               highlightbackground=ACCENT if r.id in used else BG,
                               command=lambda index=i: self.edit_color(index))
            button.grid(row=i//5, column=i%5, sticky="ew", padx=2, pady=2)
            button.bind("<Enter>", lambda _, recipe=r: self.status.set(f"{recipe.id:02d}  {recipe.name}  ·  {recipe.color}"))
        for i in range(5): self.swatches.columnconfigure(i, weight=1)
        if hasattr(self, 'palette_label'):
            self.palette_label.configure(text=f"{len(used)} IMAGE COLORS / 15 RECIPES" if used else "5 FILAMENTS + 10 EQUAL BLENDS")

    def edit_color(self, index):
        if self.export_busy: return
        color = colorchooser.askcolor(color=self.recipes[index].color, title=self.recipes[index].name)[1]
        if not color: return
        colors = [r.color for r in self.recipes[:5]]
        overrides = {r.id: r.color for r in self.recipes[5:]}
        if index < 5:
            colors[index] = color
            overrides = {r.id: r.color for r in self.recipes[5:] if index+1 not in r.tools}
        else: overrides[index+1] = color
        self.recipes = make_palette(colors, overrides)
        self.draw_swatches()
        self.schedule_quantize()

    def save_colors(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="sunlu-petg-palette.json", filetypes=[("Palette", "*.json")])
        if path: save_palette(path, self.recipes)

    def load_colors(self):
        if self.export_busy: return
        path = filedialog.askopenfilename(filetypes=[("Palette", "*.json")])
        if path:
            try:
                self.recipes = load_palette(path)
                self.draw_swatches()
                self.schedule_quantize()
            except Exception as exc: messagebox.showerror("Invalid palette", str(exc))

    def progress(self, text):
        self.messages.put(("progress", 0, text, None))

    def export(self):
        if self.export_busy or self.model_busy or self.mesh is None or self.source is None: return
        try: p = self.placement()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Check placement", str(exc)); return
        output = filedialog.asksaveasfilename(defaultextension=".3mf", initialfile=Path(self.image_path).stem+"-ColorMix.3mf", filetypes=[("PrusaSlicer project", "*.3mf")])
        if not output: return
        args = dict(image=self.source.copy(), placement=p, model=self.mesh.copy(), recipes=list(self.recipes),
                    dither=self.dither.get(), fit=self.fit.get(), color_count=int(self.color_count.get()), progress=self.progress)
        name = Path(self.image_path).stem
        details = dict(mode="existing_model" if self.mode.get() == "Existing model" else "upright_panel",
                       input_image=Path(self.image_path).name,
                       input_model=Path(self.model_path).name if self.mode.get() == "Existing model" and self.model_path else None)
        def work():
            result = generate(**args)
            result.name = name
            result.details.update(details)
            result.export(output)
            return result, output
        self.start_export(work)

    def start_export(self, work):
        self.export_busy = True
        self.export_button.configure(state="disabled")
        self.calibration_button.configure(state="disabled")
        self.status.set("Preparing print geometry…")
        self.submit("export", 0, work)

    def build_calibration(self):
        if self.export_busy: return
        output = filedialog.asksaveasfilename(defaultextension=".3mf", initialfile="CMYWB-50-50-calibration.3mf", filetypes=[("PrusaSlicer project", "*.3mf")])
        if output:
            recipes = list(self.recipes)
            def work():
                result = calibration(recipes, self.progress)
                result.export(output)
                calibration_legend(Path(output).with_suffix(".png"), recipes)
                return result, output
            self.start_export(work)

    def poll(self):
        try:
            while True:
                kind, revision, value, error = self.messages.get_nowait()
                if kind == "progress":
                    self.status.set(value); continue
                if kind == "quant":
                    self.quant_pending.discard(revision)
                    if revision != self.revision: continue
                if kind == "model":
                    if revision != self.model_revision: continue
                    self.model_busy = False
                if kind == "export":
                    self.export_busy = False
                    self.calibration_button.configure(state="normal")
                if error:
                    self.status.set(error)
                    if kind != "quant": messagebox.showerror("Could not complete operation", error)
                elif kind == "model":
                    mesh, self.model_path = value
                    self.install_mesh(mesh, Path(self.model_path).name)
                elif kind == "quant":
                    self.grid = value
                    if self.viewport: self.viewport.set_picture(value)
                    p = self.last_placement
                    self.show_image(grid_preview(value, self.recipes), (p.width, p.height))
                    self.draw_swatches()
                    self.status.set(f"Preview ready · {len(set(value.flat)-{0})} image colors · drag to position, then export.")
                    self.schedule_render()
                elif kind == "export":
                    self.result, self.export_path = value
                    self.open_button.configure(state="normal")
                    self.status.set(f"Saved {Path(self.export_path).name}. Open as a project in PrusaSlicer to keep the 50:50 blends.")
                self.export_button.configure(state="normal" if self.source is not None and self.mesh is not None and not self.export_busy and not self.model_busy else "disabled")
                self.image_button.configure(state="normal" if self.mesh is not None and not self.model_busy else "disabled")
        except queue.Empty: pass
        self.poll_after = self.root.after(25, self.poll)

    def schedule_render(self, *_):
        if self.suspend_changes: return
        # Throttle, not debounce: a long drag must produce frames before release.
        if self.render_after is None: self.render_after = self.root.after(16, self.render)

    def render(self):
        self.render_after = None
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 100 or height < 100: return
        self.canvas.delete("overlay")
        if self.mesh is None or self.viewport is None:
            self.canvas.delete("all")
            self.render_item = None
            text = "Load a model to begin" if self.mesh is None else "Preview unavailable: "+str(self.graphics_error)
            self.canvas.create_text(width/2, height/2-15, text=text, fill=TEXT, font=("DejaVu Sans", 17, "bold"), width=width-60, tags="overlay")
            self.canvas.create_text(width/2, height/2+30, text="Or choose Picture panel to create a new surface.", fill=MUTED, font=("DejaVu Sans", 10), tags="overlay")
            return
        start = time.perf_counter()
        try: p = self.placement()
        except (ValueError, tk.TclError): p = self.last_placement
        image = self.viewport.render(self.camera, p, self.recipes, (width, height))
        if self.render_tk is None or self.render_tk.width() != width or self.render_tk.height() != height:
            self.render_tk = ImageTk.PhotoImage(image)
            if self.render_item: self.canvas.itemconfigure(self.render_item, image=self.render_tk)
        else: self.render_tk.paste(image)
        if self.render_item is None: self.render_item = self.canvas.create_image(0, 0, anchor="nw", image=self.render_tk)
        if self.source is not None:
            outline = self.viewport.outline(self.camera, p, (width, height))
            if outline is not None: self.canvas.create_polygon(*outline.ravel(), fill="", outline=ACCENT, dash=(5, 4), width=2, tags="overlay")
        self.frame_ms = (time.perf_counter()-start)*1000
        self.frame_count += 1
        self.info.configure(text=f"Drag image to place · right-drag to orbit · scroll to zoom  |  {len(self.mesh.faces):,} model faces · {self.frame_ms:.0f} ms")

    def begin_drag(self, event, gesture):
        if self.mesh is None: return
        if self.source is None: gesture = "Orbit model"
        self.drag = event.x, event.y, gesture

    def move_drag(self, event):
        if self.drag is None or self.mesh is None: return
        x, y, gesture = self.drag
        dx, dy = event.x-x, event.y-y
        self.drag = event.x, event.y, gesture
        if gesture == "Move image" and not self.export_busy:
            try:
                delta = self.camera.image_drag(dx, dy, self.mesh, (self.canvas.winfo_width(), self.canvas.winfo_height()), self.side.get())
                for key, value in zip(("offset_x", "offset_z"), delta):
                    self.vars[key].set(f"{float(self.vars[key].get())+value:.3f}")
            except ValueError as exc: self.status.set(str(exc))
        elif gesture == "Orbit model": self.camera.orbit(dx, dy)
        self.schedule_render()

    def front(self, _=None):
        self.camera.face(self.side.get())
        self.schedule_render()

    def change_zoom(self, factor):
        self.camera.zoom = max(.25, min(5., self.camera.zoom*factor))
        self.schedule_render()

    def open_export(self):
        if self.export_path:
            try: open_project(self.export_path)
            except Exception as exc: messagebox.showerror("Open project", str(exc))

    def about(self):
        messagebox.showinfo(f"Prusa Image Studio {__version__}", "Copyright © 2026 PraxisOG and contributors.\n\n"
            "Free software under the GNU AGPL v3, without warranty. See LICENSE in the source distribution. "
            "Prusa color prediction is adapted under its MIT license; see THIRD_PARTY_NOTICES.md.\n\n"
            "Source: https://github.com/PraxisOG/prusa-image-studio\n"
            "An independent project, not affiliated with Prusa Research.")

    def close(self):
        for job in (self.render_after, self.quant_after, self.poll_after):
            if job: self.root.after_cancel(job)
        self.workers.shutdown(wait=False, cancel_futures=True)
        if self.viewport: self.viewport.release()
        self.root.destroy()


def main():
    root = tk.Tk(className="PrusaImageStudio")
    Studio(root)
    root.mainloop()


if __name__ == "__main__": main()
