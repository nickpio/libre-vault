"""Resource discovery must happen before GLib's first lookup, without a display."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from librevault.gtk_runtime import (configure_data_dirs, configure_scrollbars,
                                   install_window_header, require_gtk_schemas)


class RuntimeTests(unittest.TestCase):
    def test_macos_keeps_native_titlebar_and_moves_actions_into_content(self):
        window, header, content = Mock(), Mock(), Mock()
        with patch('sys.platform', 'darwin'):
            install_window_header(window, header, content)
        header.set_show_close_button.assert_called_once_with(False)
        content.pack_start.assert_called_once_with(header, False, False, 0)
        window.set_titlebar.assert_not_called()

    def test_other_platforms_keep_gtk_titlebar(self):
        for platform in ('linux', 'win32'):
            with self.subTest(platform=platform), patch('sys.platform', platform):
                window, header, content = Mock(), Mock(), Mock()
                install_window_header(window, header, content)
                header.set_show_close_button.assert_called_once_with(True)
                window.set_titlebar.assert_called_once_with(header)
                content.pack_start.assert_not_called()

    def test_macos_defaults_to_standard_scrollbars_in_source_and_frozen_builds(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen), patch('sys.platform', 'darwin'), \
                    patch.object(sys, 'frozen', frozen, create=True), \
                    patch.dict(os.environ, {}, clear=True):
                configure_scrollbars()
                self.assertEqual(os.environ['GTK_OVERLAY_SCROLLING'], '0')

    def test_scrollbar_default_preserves_explicit_overrides(self):
        for value in ('0', '1'):
            with self.subTest(value=value), patch('sys.platform', 'darwin'), \
                    patch.dict(os.environ, {'GTK_OVERLAY_SCROLLING': value}, clear=True):
                configure_scrollbars()
                configure_scrollbars()
                self.assertEqual(dict(os.environ), {'GTK_OVERLAY_SCROLLING': value})

    def test_other_platforms_keep_scrollbar_defaults(self):
        for platform in ('linux', 'win32'):
            with self.subTest(platform=platform), patch('sys.platform', platform), \
                    patch.dict(os.environ, {}, clear=True):
                configure_scrollbars()
                self.assertNotIn('GTK_OVERLAY_SCROLLING', os.environ)

    def test_package_import_configures_scrollbars_before_gtk(self):
        code = '''
import os
import sys
sys.platform = 'darwin'
import librevault
assert 'gi.repository.Gtk' not in sys.modules
print(os.environ['GTK_OVERLAY_SCROLLING'])
'''
        env = dict(os.environ)
        env.pop('GTK_OVERLAY_SCROLLING', None)
        result = subprocess.run([sys.executable, '-c', code], env=env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '0')

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
