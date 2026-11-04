"""Current-user Start menu shortcut to RGBMoCap.cmd."""
from pathlib import Path
import json
import os
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NAME = 'RGB MoCap.lnk'


def shortcut_path():
    appdata = os.environ.get('APPDATA')
    if not appdata:
        raise OSError('APPDATA is not set.')
    return Path(appdata) / 'Microsoft/Windows/Start Menu/Programs' / NAME


def _png_ico(png, dest):
    data = Path(png).read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('App icon is not a PNG file.')
    width, height = struct.unpack('>II', data[16:24])
    dest.write_bytes(struct.pack('<HHH', 0, 1, 1) + struct.pack(
        '<BBBBHHII', width if width < 256 else 0, height if height < 256 else 0,
        0, 0, 1, 32, len(data), 22) + data)
    return dest


def install(root=None):
    root = Path(root or ROOT).resolve()
    launcher = root / 'RGBMoCap.cmd'
    if not launcher.is_file():
        raise FileNotFoundError(launcher)
    dest = shortcut_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    png = root / 'assets/rgbmocap-icon.png'
    icon = root / '.setup/rgbmocap-icon.ico'
    icon_path = ''
    if png.is_file():
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon_path = str(_png_ico(png, icon))
    ps = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    dest_s, launcher_s, root_s, icon_s = (json.dumps(value) for value in (str(dest), str(launcher), str(root), icon_path))
    command = (
        '$s = New-Object -ComObject WScript.Shell; '
        f'$l = $s.CreateShortcut({dest_s}); '
        f'$l.TargetPath = {launcher_s}; '
        f'$l.WorkingDirectory = {root_s}; '
        '$l.WindowStyle = 1; '
        '$l.Description = "RGB MoCap motion workspace"; '
        f'if ({icon_s}) {{ $l.IconLocation = {icon_s} }}; '
        '$l.Save()'
    )
    subprocess.run(
        [str(ps), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
        check=True, capture_output=True, text=True)
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except (AttributeError, OSError):
        pass
    return dest


if __name__ == '__main__':
    try:
        path = install()
    except Exception as exc:
        print('Start menu shortcut failed:', exc, file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({'shortcut': str(path)}))
