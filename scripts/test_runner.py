"""Run GUI checks without disturbing the desktop, with private application state."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from tempfile import TemporaryDirectory, gettempdir


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = 'librevault-tests'
FLOATING_DESKTOPS = {'gnome', 'kde', 'xfce', 'cinnamon', 'x-cinnamon', 'mate', 'lxqt', 'budgie', 'unity', 'pantheon'}
DEFAULT_TESTS = ['discover', '-s', 'tests', '-p', 'test_*.py', '-v']
BOOTSTRAP = '''import os
import sys
from pathlib import Path
from importlib.machinery import PathFinder
# Preserve the interpreter's sitecustomize (Homebrew adds its packages there).
paths = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
original = PathFinder.find_spec('sitecustomize', paths)
if original is not None and original.loader is not None:
    code = original.loader.get_code('sitecustomize')
    if code is not None:
        exec(code, dict(__name__='sitecustomize', __file__=original.origin))
if os.environ.get('LIBREVAULT_TEST_CLASS'):
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('Gdk', '3.0')
    from gi.repository import GLib, Gdk
    GLib.set_prgname(os.environ['LIBREVAULT_TEST_CLASS'])
    Gdk.set_program_class(os.environ['LIBREVAULT_TEST_CLASS'])
'''


def hyprctl(code):
    result = subprocess.run(['hyprctl', 'eval', code], capture_output=True, text=True, timeout=5)
    if result.returncode or result.stdout.strip() != 'ok':
        raise RuntimeError(f'Could not configure the test workspace: {result.stdout.strip()} {result.stderr.strip()}')


@contextmanager
def background_workspace(identity):
    # Match only this run's class, including dialogs and Python subprocesses.
    handle = '_librevault_test_rule_' + str(os.getpid())
    hyprctl(f'{handle} = hl.window_rule({{name="{identity}",'
            f'match={{class="(?i)^{identity}$"}},workspace="name:{WORKSPACE} silent",'
            'no_initial_focus=true,suppress_event="activate activatefocus",float=true})')
    try:
        yield
    finally:
        hyprctl(f'{handle}:set_enabled(false); {handle}=nil')


def floating_desktop(env):
    """True where new windows open without taking over the layout or focus."""
    if sys.platform in ('win32', 'darwin'):
        return True
    if not env.get('DISPLAY') and not env.get('WAYLAND_DISPLAY'):
        return False
    # An unlisted desktop may tile, so it still needs an explicit --foreground.
    return bool(FLOATING_DESKTOPS.intersection(env.get('XDG_CURRENT_DESKTOP', '').lower().split(':')))


def command_for(args, remaining):
    if args.smoke:
        return [sys.executable, str(ROOT / 'tests' / 'smoke_gui.py'), *remaining]
    if args.script:
        return [sys.executable, str(Path(args.script).resolve()), *remaining]
    return [sys.executable, '-m', 'unittest', *(remaining or DEFAULT_TESTS)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--smoke', action='store_true', help='Run the GUI smoke test')
    target.add_argument('--script', help='Run a Python GUI probe instead of the test suite')
    parser.add_argument('--broadway', action='store_true', help='Use a private headless GTK Broadway display')
    parser.add_argument('--foreground', action='store_true', help='Allow test windows on the current desktop')
    args, remaining = parser.parse_known_args(argv)
    command = command_for(args, remaining)
    with TemporaryDirectory(prefix='librevault-tests-') as directory:
        base = Path(directory)
        bootstrap = base / 'bootstrap'
        bootstrap.mkdir()
        (bootstrap / 'sitecustomize.py').write_text(BOOTSTRAP)
        env = dict(os.environ, XDG_CONFIG_HOME=str(base / 'config'), XDG_STATE_HOME=str(base / 'state'),
                   XDG_CACHE_HOME=str(base / 'cache'))
        env['PYTHONPATH'] = os.pathsep.join([str(bootstrap), str(ROOT), env.get('PYTHONPATH', '')])
        # macOS reaches its temp directory through a symlink and Windows may shorten
        # it, so give tests the canonical path that resolved vault paths will match.
        env['TMPDIR'] = str(Path(gettempdir()).resolve())
        # Launchers run by name, such as run.sh, must find this same interpreter.
        env['PATH'] = os.pathsep.join(filter(None, [str(Path(sys.executable).parent), env.get('PATH')]))
        identity = 'librevault-tests-' + str(os.getpid())
        if args.broadway:
            daemon = shutil.which('broadwayd')
            if not daemon:
                parser.error('--broadway requires a GTK build with broadwayd')
            runtime = base / 'runtime'
            runtime.mkdir(mode=0o700)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            address = f':tcp{port - 9090}'
            env.update(GDK_BACKEND='broadway', BROADWAY_DISPLAY=address, XDG_RUNTIME_DIR=str(runtime))
            env.pop('DISPLAY', None)
            env.pop('WAYLAND_DISPLAY', None)
            with subprocess.Popen([daemon, '--unixsocket', str(base / 'http.socket'), address], env=env,
                                  stdout=subprocess.DEVNULL) as display:
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        if display.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('Private Broadway display did not start')
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                                break
                        except OSError:
                            time.sleep(0.02)
                    print('GUI checks use a private headless Broadway display.', flush=True)
                    return subprocess.run(command, cwd=ROOT, env=env).returncode
                finally:
                    display.terminate()
                    display.wait(timeout=5)
        if args.foreground:
            return subprocess.run(command, cwd=ROOT, env=env).returncode
        if env.get('HYPRLAND_INSTANCE_SIGNATURE') and env.get('DISPLAY') and shutil.which('hyprctl'):
            env['GDK_BACKEND'] = 'x11'
            env['LIBREVAULT_TEST_CLASS'] = identity
            with background_workspace(identity):
                print(f'GUI checks use background workspace "{WORKSPACE}".', flush=True)
                return subprocess.run(command, cwd=ROOT, env=env).returncode
        if shutil.which('xvfb-run'):
            env['GDK_BACKEND'] = 'x11'
            env.pop('WAYLAND_DISPLAY', None)
            print('GUI checks use a private Xvfb display.', flush=True)
            return subprocess.run(['xvfb-run', '-a', '-s', '-screen 0 1600x1000x24', *command],
                                  cwd=ROOT, env=env).returncode
        if floating_desktop(env):
            print('GUI checks use the floating desktop.', flush=True)
            return subprocess.run(command, cwd=ROOT, env=env).returncode
        if not env.get('DISPLAY') and not env.get('WAYLAND_DISPLAY'):
            print('No desktop display; GUI tests will be skipped.', flush=True)
            return subprocess.run(command, cwd=ROOT, env=env).returncode
        parser.error('Background GUI tests need Hyprland with XWayland or xvfb-run. '
                     'Use --foreground only when you want windows on your desktop.')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)
