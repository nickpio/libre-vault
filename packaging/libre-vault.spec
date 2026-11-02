from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / 'packaging' / 'launch.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'librevault' / 'style.css'), 'librevault'),
           (str(root / 'LICENSE'), 'licenses/libre-vault')],
    hiddenimports=['gi.repository.Poppler'],
    hookspath=[str(root / 'packaging' / 'hooks')],
    hooksconfig={
        'gi': {'module-versions': {'Gtk': '3.0', 'Gdk': '3.0'},
               'icons': ['Adwaita'], 'themes': ['Adwaita']},
        'matplotlib': {'backends': ['Agg']},
    },
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='libre-vault', debug=False, strip=False, upx=False,
          console=True)
