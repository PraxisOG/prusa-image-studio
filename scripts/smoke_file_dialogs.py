"""Drive real Tk file pickers; mocked dialog returns miss X11 focus crashes."""
from pathlib import Path
import tempfile
import time
import tkinter as tk

from prusa_image_studio.app import Studio


def main():
    root = tk.Tk()
    if root.tk.call('tk', 'windowingsystem') != 'x11':
        root.destroy()
        raise SystemExit('This real-dialog smoke test requires Tk on X11')
    app = Studio(root)
    root.update()
    errors = []
    root.report_callback_exception = lambda kind, value, trace: errors.append(str(value))

    def pump():
        deadline = time.monotonic()+120
        while app.busy and time.monotonic() < deadline:
            root.update(); time.sleep(.01)
        root.update()
        assert not app.busy, 'Operation timed out'
        assert not errors, errors

    def file_dialog(action, path=None):
        # Only automate widgets inside the real picker. Do not replace
        # askopenfilename/asksaveasfilename, or bypass the modal focus lifecycle.
        completed = []
        callbacks = []
        deadline = time.monotonic()+10
        def respond():
            dialog = '.__tk_filedialog'
            if not int(root.tk.call('winfo', 'exists', dialog)) or not int(root.tk.call('winfo', 'viewable', dialog)):
                if time.monotonic() > deadline:
                    errors.append('File dialog did not open')
                    root.destroy()
                else: callbacks.append(root.after(50, respond))
                return
            if path is None:
                root.tk.call('::tk::dialog::file::CancelCmd', dialog)
            else:
                entry = dialog+'.contents.f2.ent'
                root.tk.call(entry, 'delete', 0, 'end')
                root.tk.call(entry, 'insert', 0, str(Path(path).resolve()))
                root.tk.call('::tk::dialog::file::ActivateEnt', dialog)
            completed.append(True)
        callbacks.append(root.after(300, respond))
        try:
            action()
            assert completed, 'The real picker did not accept the test action'
        finally:
            for callback in callbacks: root.after_cancel(callback)
        pump()

    try:
        assert app.viewport is not None, app.graphics_error
        for _ in range(3):
            file_dialog(app.choose_model, 'examples/box.stl')
            assert app.mesh is not None and len(app.mesh.faces) == 12
            assert app.source is None
            app.render(); root.update()
            original = app.mesh
            file_dialog(app.choose_model)  # Cancel must retain the loaded model.
            assert app.mesh is original
        file_dialog(app.choose_image, 'examples/mountain-demo.png')
        assert app.source is not None and app.grid is not None
        app.vars['width'].set('24'); app.vars['height'].set('18')
        app.color_count.set('4'); pump()
        output = Path('artifacts/file-dialogs')
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output) as temp:
            target = Path(temp)/'model.3mf'
            file_dialog(app.export, target)
            assert target.is_file() and app.result.mesh.is_watertight
        app.render(); root.update()
        assert not errors, errors
        print('PASS: real STL picker opened/loaded/cancelled repeatedly; real image and save pickers completed; preview still renders.')
    finally: app.close()


if __name__ == '__main__': main()
