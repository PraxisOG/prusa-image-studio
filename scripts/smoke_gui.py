"""Exercise actual Tk callbacks, live dragging, quantization, and both exports."""
import json
from pathlib import Path
import statistics
import time
import tkinter as tk
from unittest.mock import patch

from PIL import ImageGrab
from prusa_image_studio.app import Studio


def main():
    output = Path('artifacts/gui-smoke').resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    app = Studio(root)
    errors = []
    def pump():
        deadline = time.monotonic()+180
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        root.update()
        assert not app.busy, 'GUI task timed out'
        assert not errors, errors
    def visible_actions():
        root.update()
        for w in (app.export_button, app.open_button, app.calibration_button):
            assert w.winfo_rooty()+w.winfo_height() <= root.winfo_rooty()+root.winfo_height()
            assert w.winfo_rootx()+w.winfo_width() <= root.winfo_rootx()+root.winfo_width()
    def event(sequence, x, y):
        app.canvas.event_generate(sequence, x=x, y=y)
        root.update()
    try:
        root.update()
        assert app.viewport is not None, app.graphics_error
        assert app.mesh is None and str(app.image_button['state']) == 'disabled'
        with patch('prusa_image_studio.app.messagebox.showerror', side_effect=lambda *x: errors.append(x)):
            # Editing must not call expensive print-geometry generation.
            with patch('prusa_image_studio.geometry.refine', side_effect=AssertionError('Preview tessellated geometry')):
                with patch('prusa_image_studio.app.filedialog.askopenfilename', return_value=str(Path('examples/box.stl').resolve())):
                    app.model_button.invoke()
                pump()
                assert app.mesh is not None and app.source is None
                assert len(app.mesh.faces) == 12
                with patch('prusa_image_studio.app.filedialog.askopenfilename', return_value=str(Path('examples/mountain-demo.png').resolve())):
                    app.image_button.invoke()
                pump()
                assert app.grid is not None
                revision = app.revision
                event('<ButtonPress-1>', 350, 200)
                before = app.frame_count
                for i in range(1, 21):
                    event('<B1-Motion>', 350+i, 200+i)
                    time.sleep(.006)
                event('<ButtonRelease-1>', 370, 220)
                assert float(app.vars['offset_x'].get()) > 0
                assert float(app.vars['offset_z'].get()) < 0
                assert app.frame_count > before+2, 'Drag must render before release'
                assert app.revision == revision, 'Position-only drag must reuse quantized texture'
                old_yaw = app.camera.yaw
                event('<ButtonPress-3>', 350, 200)
                event('<B3-Motion>', 380, 200)
                event('<ButtonRelease-3>', 380, 200)
                assert app.camera.yaw < old_yaw
                app.color_count.set('3')
                app.dither.set(True)
                app.vars['pixel'].set('0.2')
                app.vars['pixel_z'].set('0.1')
                pump()
                assert len(set(app.grid.flat)-{0}) <= 3
                assert len(app.viewport.mesh.faces) == 12
                # Rapid edits must finish with the newest request.
                app.color_count.set('7'); app.start_quantize()
                app.color_count.set('2'); pump()
                assert len(set(app.grid.flat)-{0}) <= 2
            # Use a compact print for the actual mesh/export part of this smoke.
            app.vars['width'].set('24'); app.vars['height'].set('18')
            app.dither.set(False); app.color_count.set('4'); pump()
            with patch('prusa_image_studio.app.filedialog.asksaveasfilename', return_value=str(output/'box.3mf')):
                app.export_button.invoke()
            pump()
            assert (output/'box.3mf').is_file()
            assert app.result.details['mode'] == 'existing_model'
            assert app.result.details['image_color_limit'] == 4
            assert len(set(app.result.grid.flat)-{0}) <= 4
            assert app.result.details['placement']['offset_x'] > 0
            assert len(app.viewport.mesh.faces) == 12, 'Export must not slow the editor by replacing the original mesh'
            assert str(app.open_button['state']) == 'normal'
            frames = []
            for _ in range(30):
                app.camera.orbit(1, 0); app.render(); root.update()
                frames.append(app.frame_ms)
            visible_actions()
            app.fit_image(); app.color_count.set('8'); app.vars['pixel'].set('0.4'); pump()
            app.camera.yaw, app.camera.pitch, app.camera.zoom = -20, 12, 1.3
            app.render(); root.update(); time.sleep(.1); root.update()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x+root.winfo_width(), y+root.winfo_height())).save(output/'existing-model-desktop.png')
            app.mode.set('Picture panel')
            app.vars['panel_width'].set('30'); app.vars['panel_height'].set('24')
            pump()
            with patch('prusa_image_studio.app.filedialog.asksaveasfilename', return_value=str(output/'panel.3mf')):
                app.export_button.invoke()
            pump()
            assert (output/'panel.3mf').is_file()
            assert app.result.details['mode'] == 'upright_panel'
            root.geometry('1100x740'); root.update(); visible_actions()
            report = {'renderer': app.viewport.renderer_name, 'frames': len(frames),
                      'frame_median_ms': round(statistics.median(frames), 2),
                      'frame_max_ms': round(max(frames), 2),
                      'model_first': True, 'pointer_direction': True, 'continuous_drag': True,
                      'color_limit': True, 'export_both_modes': True}
            (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
            print(json.dumps(report, indent=2))
    finally: app.close()


if __name__ == '__main__': main()
