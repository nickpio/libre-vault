"""Tiling desktops must never silently fall back to the active desktop."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory, gettempdir
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import test_runner


@unittest.skipIf(sys.platform == 'win32', 'Fake interpreters are POSIX shell scripts')
class InterpreterSelectionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copy(test_runner.ROOT / 'check.sh', self.root)
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts' / 'test_runner.py').write_text("import os; print(os.environ['CHOSEN'])\n")

    def interpreter(self, relative, works=True):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        real = f'CHOSEN={shlex.quote(relative)} exec {shlex.quote(sys.executable)} "$@"'
        path.write_text('#!/bin/sh\n' + (real if works else 'exit 1') + '\n')
        path.chmod(0o755)
        return path

    def check(self, **extra):
        path = os.pathsep.join([str(self.root / 'bin'), '/usr/bin', '/bin'])
        env = {'PATH': path, **extra}
        return subprocess.run(['sh', str(self.root / 'check.sh')], env=env, capture_output=True,
                              text=True, timeout=30)

    def test_uses_first_interpreter_that_has_the_required_packages(self):
        self.interpreter('bin/python3', works=False)
        self.interpreter('bin/python')
        self.assertEqual(self.check().stdout.strip(), 'bin/python')
        self.interpreter('.venv/bin/python')
        self.assertEqual(self.check().stdout.strip(), '.venv/bin/python')

    def test_explicit_python_takes_precedence(self):
        self.interpreter('.venv/bin/python')
        chosen = self.interpreter('other/python')
        self.assertEqual(self.check(PYTHON=str(chosen)).stdout.strip(), 'other/python')

    def test_reports_when_no_interpreter_is_usable(self):
        tools = self.root / 'bin'
        tools.mkdir()
        (tools / 'dirname').symlink_to(shutil.which('dirname'))
        result = subprocess.run([shutil.which('sh'), str(self.root / 'check.sh')], env={'PATH': str(tools)},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Set PYTHON', result.stderr)


class TestRunnerTests(unittest.TestCase):
    def test_children_get_canonical_temp_directory_and_this_interpreter(self):
        with patch.dict(os.environ, {'DISPLAY': ':0'}, clear=True), \
                patch.object(test_runner.shutil, 'which', return_value=None), \
                patch.object(test_runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(test_runner.main(['--foreground']), 0)
        env = run.call_args.kwargs['env']
        self.assertEqual(env['TMPDIR'], str(Path(gettempdir()).resolve()))
        self.assertEqual(env['PATH'].split(os.pathsep)[0], str(Path(sys.executable).parent))
        self.assertEqual(env['PYTHONFAULTHANDLER'], '1')

    def test_broadway_is_private_and_always_stopped_after_a_failed_check(self):
        display = unittest.mock.MagicMock()
        display.__enter__.return_value = display
        display.poll.return_value = None
        with patch.object(test_runner.shutil, 'which', return_value='/headless/broadwayd'), \
                patch.object(test_runner.subprocess, 'Popen', return_value=display) as launch, \
                patch.object(test_runner.socket, 'create_connection'), \
                patch.object(test_runner.subprocess, 'run', return_value=SimpleNamespace(returncode=7)) as run:
            self.assertEqual(test_runner.main(['--broadway', '--smoke']), 7)
        command = launch.call_args.args[0]
        self.assertEqual(command[1], '--unixsocket')
        env = run.call_args.kwargs['env']
        self.assertEqual(env['GDK_BACKEND'], 'broadway')
        self.assertTrue(env['BROADWAY_DISPLAY'].startswith(':tcp'))
        self.assertNotIn('DISPLAY', env)
        self.assertNotIn('WAYLAND_DISPLAY', env)
        display.terminate.assert_called_once()
        display.wait.assert_called_once()

    def test_broadway_missing_daemon_does_not_launch_tests(self):
        with patch.object(test_runner.shutil, 'which', return_value=None), \
                patch.object(test_runner.subprocess, 'run') as run:
            with self.assertRaises(SystemExit):
                test_runner.main(['--broadway'])
            run.assert_not_called()

    def test_bootstrap_preserves_existing_sitecustomize(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap, original = root / 'bootstrap', root / 'original'
            bootstrap.mkdir()
            original.mkdir()
            (bootstrap / 'sitecustomize.py').write_text(test_runner.BOOTSTRAP)
            (original / 'sitecustomize.py').write_text("import os\nos.environ['LV_SITE_TEST'] = 'loaded'\n")
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(bootstrap), str(original)]))
            env.pop('LIBREVAULT_TEST_CLASS', None)
            result = subprocess.run([sys.executable, '-c',
                                     "import os; print(os.environ.get('LV_SITE_TEST'))"],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'loaded')

    def test_hyprland_routes_case_insensitive_test_identity_and_cleans_up(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:2] == ['hyprctl', 'eval']:
                return SimpleNamespace(returncode=0, stdout='ok', stderr='')
            env = kwargs['env']
            self.assertEqual(env['GDK_BACKEND'], 'x11')
            self.assertTrue(env['LIBREVAULT_TEST_CLASS'].startswith('librevault-tests-'))
            self.assertNotEqual(env['XDG_CONFIG_HOME'], '/real/config')
            bootstrap = Path(env['PYTHONPATH'].split(os.pathsep)[0]) / 'sitecustomize.py'
            self.assertIn('set_program_class', bootstrap.read_text())
            return SimpleNamespace(returncode=7)
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': 'test', 'DISPLAY': ':0',
                                     'XDG_CONFIG_HOME': '/real/config'}, clear=True), \
                patch.object(test_runner.shutil, 'which', return_value='/usr/bin/hyprctl'), \
                patch.object(test_runner.subprocess, 'run', side_effect=run):
            self.assertEqual(test_runner.main(['--smoke']), 7)
        self.assertIn('(?i)^librevault-tests-', calls[0][0][2])
        self.assertIn('librevault-tests silent', calls[0][0][2])
        self.assertIn('no_initial_focus=true', calls[0][0][2])
        self.assertIn('set_enabled(false)', calls[-1][0][2])

    def test_rule_failure_does_not_launch_gui_on_desktop(self):
        with patch.dict(os.environ, {'HYPRLAND_INSTANCE_SIGNATURE': 'test', 'DISPLAY': ':0'}, clear=True), \
                patch.object(test_runner.shutil, 'which', return_value='/usr/bin/hyprctl'), \
                patch.object(test_runner.subprocess, 'run', return_value=
                             SimpleNamespace(returncode=0, stdout='rule rejected', stderr='')) as run:
            with self.assertRaises(RuntimeError):
                test_runner.main([])
            self.assertEqual(run.call_count, 1)

    def test_xvfb_is_used_when_background_workspace_is_unavailable(self):
        with patch.dict(os.environ, {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-1'}, clear=True), \
                patch.object(test_runner.shutil, 'which', return_value='/usr/bin/xvfb-run'), \
                patch.object(test_runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(test_runner.main([]), 0)
            self.assertEqual(run.call_args.args[0][0], 'xvfb-run')
            self.assertNotIn('WAYLAND_DISPLAY', run.call_args.kwargs['env'])

    def test_unsupported_desktop_requires_explicit_foreground(self):
        with patch.object(test_runner.sys, 'platform', 'linux'), \
                patch.dict(os.environ, {'DISPLAY': ':0'}, clear=True), \
                patch.object(test_runner.shutil, 'which', return_value=None), \
                patch.object(test_runner.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            with self.assertRaises(SystemExit) as error:
                test_runner.main([])
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()
            self.assertEqual(test_runner.main(['--foreground']), 0)
            self.assertNotIn('LIBREVAULT_TEST_CLASS', run.call_args.kwargs['env'])
