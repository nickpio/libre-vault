"""Resource discovery must happen before GLib's first lookup, without a display."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.gtk_runtime import configure_data_dirs, require_gtk_schemas


class RuntimeTests(unittest.TestCase):
    def test_macos_preserves_overrides_and_adds_framework_homebrew_prefix(self):
        with TemporaryDirectory() as directory:
            prefix = Path(directory)
            schema = prefix / 'share/glib-2.0/schemas/gschemas.compiled'
            schema.parent.mkdir(parents=True)
            schema.touch()
            python = prefix / 'opt/python/Frameworks/Python.framework/Versions/3.15'
            with patch('sys.platform', 'darwin'), patch('sys.base_prefix', str(python)), \
                    patch.dict(os.environ, {'XDG_DATA_DIRS': '/custom/share'}, clear=True), \
                    patch.object(Path, 'is_file', lambda path: path == schema):
                configure_data_dirs()
                self.assertEqual(os.environ['XDG_DATA_DIRS'], f'/custom/share:{prefix}/share')
                configure_data_dirs()
                self.assertEqual(os.environ['XDG_DATA_DIRS'], f'/custom/share:{prefix}/share')

    def test_other_platforms_and_frozen_builds_keep_environment(self):
        for platform, frozen in [('linux', False), ('win32', False), ('darwin', True)]:
            with self.subTest(platform=platform), patch('sys.platform', platform), \
                    patch.object(sys, 'frozen', frozen, create=True), \
                    patch.dict(os.environ, {'XDG_DATA_DIRS': '/bundle/share'}, clear=True):
                configure_data_dirs()
                self.assertEqual(dict(os.environ), {'XDG_DATA_DIRS': '/bundle/share'})

    def test_missing_schema_reports_repair_instead_of_constructing_settings(self):
        try:
            from gi.repository import Gio
        except ImportError as error:
            self.skipTest(f'GObject introspection is unavailable: {error}')
        with patch('sys.platform', 'darwin'), \
                patch.object(Gio.SettingsSchemaSource, 'get_default', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'brew reinstall gtk\\+3'):
                require_gtk_schemas()

    @unittest.skipUnless(sys.platform == 'darwin', 'Homebrew integration requires macOS')
    def test_fresh_process_finds_homebrew_resources_with_restricted_data_path(self):
        try:
            import gi  # noqa: F401
        except ImportError as error:
            self.skipTest(f'GObject introspection is unavailable: {error}')
        code = '''
import json
import librevault
from librevault.gtk_runtime import require_gtk_schemas
from gi.repository import GLib
from pathlib import Path
require_gtk_schemas()
dirs = [Path(p) for p in GLib.get_system_data_dirs()]
print(json.dumps({name: any((p / 'icons' / name / 'index.theme').is_file() for p in dirs)
                  for name in ('Adwaita', 'hicolor')}))
'''
        with TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', code],
                                    env=dict(os.environ, XDG_DATA_DIRS=directory),
                                    text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'Adwaita': True, 'hicolor': True})
