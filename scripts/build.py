"""Build a native executable and, optionally, an AppImage."""
import argparse
import importlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--appimage', action='store_true', help='Also package an AppImage')
    parser.add_argument('--appimagetool', default='appimagetool', help='Path to appimagetool')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error('Build with Python 3.10 or newer.')
    if args.appimage and sys.platform != 'linux':
        parser.error('AppImage packaging requires Linux.')
    tool = shutil.which(args.appimagetool) if args.appimage else None
    if args.appimage and not tool:
        parser.error('Install appimagetool or pass --appimagetool /path/to/appimagetool.')
    try:
        for name in ['PyInstaller', 'gi', 'cairo', 'markdown_it', 'yaml', 'matplotlib']:
            importlib.import_module(name)
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_foreign('cairo')
        from gi.repository import Gtk
    except (ImportError, ValueError) as error:
        parser.error(f'{error}. Install the runtime dependencies and requirements-build.txt '
                     'in a venv with --system-site-packages; see README.md.')
    build = ROOT / 'build'
    dist = ROOT / 'dist'
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(dist), '--workpath', str(build / 'pyinstaller'),
                    str(ROOT / 'packaging' / 'libre-vault.spec')], check=True, cwd=ROOT)
    executable = dist / ('libre-vault.exe' if sys.platform == 'win32' else 'libre-vault')
    # Import bundled modules and CSS without the checkout on Python's path.
    with TemporaryDirectory(prefix='librevault-build-') as directory:
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        subprocess.run([str(executable), '--help'], check=True, cwd=directory, env=env)
    print(f'Executable: {executable}', flush=True)
    if args.appimage:
        appdir = build / 'LibreVault.AppDir'
        if appdir.exists():
            shutil.rmtree(appdir)
        (appdir / 'usr' / 'bin').mkdir(parents=True)
        shutil.copy2(executable, appdir / 'usr' / 'bin' / 'libre-vault')
        for name in ['libre-vault.desktop', 'libre-vault.svg']:
            shutil.copy2(ROOT / 'packaging' / name, appdir / name)
        launcher = appdir / 'AppRun'
        launcher.write_text('#!/bin/sh\nset -eu\n'
                            'appdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                            'exec "$appdir/usr/bin/libre-vault" "$@"\n')
        launcher.chmod(0o755)
        output = dist / f'LibreVault-{platform.machine()}.AppImage'
        env = dict(os.environ, ARCH=platform.machine())
        subprocess.run([tool, str(appdir), str(output)], check=True, env=env)
        print(f'AppImage: {output}', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode)
