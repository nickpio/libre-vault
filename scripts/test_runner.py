"""Run GUI checks away from the active desktop, with private application state."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = 'librevault-tests'
DEFAULT_TESTS = ['discover', '-s', 'tests', '-p', 'test_*.py', '-v']
BOOTSTRAP = '''import os
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
    parser.add_argument('--foreground', action='store_true', help='Allow test windows on the current desktop')
    args, remaining = parser.parse_known_args(argv)
    command = command_for(args, remaining)
    with TemporaryDirectory(prefix='librevault-tests-') as directory:
        base = Path(directory)
        bootstrap = base / 'bootstrap'
        bootstrap.mkdir()
        (bootstrap / 'sitecustomize.py').write_text(BOOTSTRAP)
        env = dict(os.environ, XDG_CONFIG_HOME=str(base / 'config'), XDG_STATE_HOME=str(base / 'state'))
        env['PYTHONPATH'] = os.pathsep.join([str(bootstrap), str(ROOT), env.get('PYTHONPATH', '')])
        identity = 'librevault-tests-' + str(os.getpid())
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
