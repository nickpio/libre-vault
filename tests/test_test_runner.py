"""Background test routing must never silently fall back to the active desktop."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import test_runner


class TestRunnerTests(unittest.TestCase):
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
