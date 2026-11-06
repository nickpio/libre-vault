"""Python plugins integrated with settings, palette, and real note buffers."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.gui_wait import wait_for_work

try:
    from librevault.app import Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class PluginUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.PluginTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        (self.vault / 'Note.md').write_text('hello world')
        self.preferences_path = self.root / 'config' / 'settings.json'
        folder = self.preferences_path.parent / 'plugins'
        folder.mkdir(parents=True)
        self.plugin_path = folder / 'example.py'
        self.plugin_path.write_text('''API_VERSION = 1
unloaded = False
def cleanup():
    global unloaded
    unloaded = True
def setup(api):
    api.on_unload(cleanup)
    api.register_command('upper', 'Uppercase selection',
                         lambda: api.replace_selection(api.get_selection().upper()))
''')
        self.window = self.new_window()

    def new_window(self):
        window = Window(self.app, self.vault, preferences_path=self.preferences_path)
        self.addCleanup(window.destroy)
        wait_for_work(window)
        return window

    def enable(self):
        self.window.set_preferences(replace(self.window.preferences, python_plugins=True))

    def test_palette_command_edits_selection_and_undo_restores_it(self):
        self.assertEqual(self.window.plugins.plugins, {})
        self.enable()
        pane = self.window.current()
        pane.buffer.select_range(pane.buffer.get_iter_at_offset(0), pane.buffer.get_iter_at_offset(5))
        self.window.show_command_palette()
        palette = self.window.palette
        row = next(row for row in palette.results.get_children() if row.title == 'example: Uppercase selection')
        palette.results.select_row(row)
        palette.run_selected()
        self.assertEqual(pane.text(), 'HELLO world')
        self.assertTrue(pane.dirty)
        pane.undo()
        self.assertEqual(pane.text(), 'hello world')

    def test_settings_cancel_opt_in_does_not_execute_code(self):
        self.window.show_settings()
        dialog = self.window.settings_dialog
        with patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.CANCEL):
            dialog.controls['python_plugins'].set_active(True)
        self.assertFalse(self.window.preferences.python_plugins)
        self.assertFalse(dialog.controls['python_plugins'].get_active())
        self.assertEqual(self.window.plugins.plugins, {})
        self.assertFalse(self.preferences_path.exists())

    def test_settings_confirm_and_failed_save(self):
        self.window.show_settings()
        dialog = self.window.settings_dialog
        with patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.OK):
            with patch.object(self.window.preferences_store, 'save', side_effect=OSError('disk full')):
                dialog.controls['python_plugins'].set_active(True)
            self.assertFalse(self.window.plugins.enabled)
            self.assertTrue(dialog.error.get_visible())
            dialog.controls['python_plugins'].set_active(True)
        self.assertIn('example', self.window.plugins.plugins)
        self.assertIn('example', dialog.plugin_status.get_text())
        dialog.controls['python_plugins'].set_active(False)
        self.assertEqual(self.window.plugins.plugins, {})

    def test_reload_disable_and_restart_across_windows(self):
        other = self.new_window()
        self.enable()
        first = self.window.plugins.plugins['example'].module
        second = other.plugins.plugins['example'].module
        self.window.reload_plugins()
        self.assertTrue(first.unloaded)
        self.assertTrue(second.unloaded)
        restarted = self.new_window()
        self.assertIn('example', restarted.plugins.plugins)
        self.window.set_preferences(replace(self.window.preferences, python_plugins=False))
        for window in (self.window, other, restarted):
            self.assertFalse(window.plugins.enabled)
            self.assertEqual(window.plugins.command_actions(), [])

    def test_window_destroy_unloads_and_vault_scripts_never_run(self):
        folder = self.vault / 'plugins'
        folder.mkdir()
        (folder / 'untrusted.py').write_text('raise RuntimeError("do not run")')
        self.enable()
        self.assertEqual(list(self.window.plugins.plugins), ['example'])
        module = self.window.plugins.plugins['example'].module
        self.window.destroy()
        self.assertTrue(module.unloaded)
