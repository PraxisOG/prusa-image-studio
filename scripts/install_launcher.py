"""Install a user-local Linux application launcher for this checkout."""
from pathlib import Path
import os
import subprocess

root = Path(__file__).resolve().parents[1]
launch = root/'launch.sh'
launch.chmod(0o755)
desktop = Path.home()/'.local/share/applications/prusa-image-studio.desktop'
desktop.parent.mkdir(parents=True, exist_ok=True)
desktop.write_text(f'''[Desktop Entry]
Version=1.0
Type=Application
Name=Prusa Image Studio
Comment=Print images on vertical walls with CMYWB 50:50 blends
Exec="{launch}"
Path={root}
Icon={root}/src/prusa_image_studio/assets/studio.svg
Terminal=false
Categories=Graphics;3DGraphics;
StartupWMClass=PrusaImageStudio
''')
desktop.chmod(0o755)
print(desktop)
