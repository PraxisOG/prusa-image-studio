"""GLX must leave Tk's handler intact, including failed initialization."""
import ctypes
import ctypes.util
import os
import sys
import tkinter as tk
from unittest.mock import patch

import pytest

from prusa_image_studio.viewport import Viewport

pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux') or not os.environ.get('DISPLAY'),
                                reason='Requires Tk on X11')


@pytest.mark.parametrize('fail', [False, True])
def test_context_creation_preserves_tk_error_handler(fail):
    root = tk.Tk()
    root.withdraw()
    x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
    x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    x11.XSetErrorHandler.restype = ctypes.c_void_p
    def current_handler():
        previous = x11.XSetErrorHandler(None)
        x11.XSetErrorHandler(previous)
        return previous
    previous = current_handler()
    viewport = None
    try:
        if fail:
            def broken_backend(**kwargs):
                # Simulate the backend changing global X11 state, then failing.
                x11.XSetErrorHandler(None)
                raise RuntimeError('context unavailable')
            with patch('prusa_image_studio.viewport.moderngl.create_context', side_effect=broken_backend):
                with pytest.raises(RuntimeError, match='context unavailable'):
                    Viewport()
        else:
            viewport = Viewport()
        assert current_handler() == previous, 'GLX replaced Tk error handling with a fatal X11 default'
    finally:
        x11.XSetErrorHandler(previous)
        if viewport: viewport.release()
        root.destroy()
