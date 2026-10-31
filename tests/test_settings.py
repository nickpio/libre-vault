"""Settings controls change the running editor and persist outside the vault."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.preferences import Preferences, PreferencesStore

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.SettingsTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Heading\n\nBody `code`\n')
        self.preferences_path = Path(self.directory.name) / 'config' / 'settings.json'
        self.window = Window(self.app, self.root, preferences_path=self.preferences_path)
        self.addCleanup(self.window.destroy)
        self.window.settings_button.clicked()
        self.dialog = self.window.settings_dialog
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                break
            context.iteration(False)

    def test_gear_opens_categories_and_does_not_write_configuration_until_changed(self):
        self.assertTrue(self.dialog.get_visible())
        self.assertEqual(self.dialog.stack.get_visible_child_name(), 'Appearance')
        self.dialog.categories.select_row(self.dialog.categories.get_row_at_index(1))
        self.assertEqual(self.dialog.stack.get_visible_child_name(), 'Editor')
        self.assertNotIn('notes', self.dialog.controls)
        self.assertNotIn('details', self.dialog.controls)
        self.assertFalse(self.preferences_path.exists())
        self.window.show_settings()
        self.assertIs(self.window.settings_dialog, self.dialog)
        self.dialog.response(Gtk.ResponseType.CLOSE)
        self.assertIsNone(self.window.settings_dialog)

    def test_appearance_applies_to_existing_and_new_notes_without_editing_source(self):
        pane = self.window.current()
        before = pane.text()
        self.dialog.controls['theme'].set_active_id('light')
        self.dialog.controls['editor_font'].set_active_id('monospace')
        self.dialog.controls['editor_size'].set_value(16)
        self.dialog.controls['interface_size'].set_value(12)
        self.assertEqual(self.window.preferences.theme, 'light')
        self.assertAlmostEqual(pane.tags['h1'].get_property('size-points'), 25 * 16 / 11, places=2)
        self.assertLess(pane.tags['h1'].get_property('foreground-rgba').red, 0.2)
        found, canvas = self.dialog.get_style_context().lookup_color('canvas')
        self.assertTrue(found)
        self.assertGreater(canvas.red, 0.9)
        self.assertGreater(self.dialog.get_style_context().get_background_color(Gtk.StateFlags.NORMAL).red, 0.9)
        self.assertEqual(PreferencesStore(self.preferences_path).load(), self.window.preferences)
        self.window.vault.create('Second')
        self.window.open_note('Second.md')
        self.assertAlmostEqual(self.window.current().tags['h1'].get_property('size-points'), 25 * 16 / 11, places=2)
        self.assertEqual(pane.text(), before)
        self.assertFalse(pane.dirty)
        self.assertEqual((self.root / 'Note.md').read_text(), before)
        self.assertFalse((self.root / '.obsidian').exists())
        self.assertFalse((self.root / 'settings.json').exists())

    def test_editor_controls_change_layout_and_list_continuation(self):
        pane = self.window.current()
        allocation = Gdk.Rectangle()
        allocation.width = 1200
        self.dialog.controls['line_width'].set_value(600)
        pane.size_editor(pane.editor, allocation)
        self.assertEqual(pane.editor.get_left_margin(), 300)
        self.dialog.controls['readable_length'].set_active(False)
        self.assertEqual(pane.editor.get_left_margin(), 32)
        self.assertFalse(self.dialog.controls['line_width'].get_sensitive())
        self.dialog.controls['wrap_lines'].set_active(False)
        self.assertEqual(pane.editor.get_wrap_mode(), Gtk.WrapMode.NONE)
        pane.buffer.set_text('- Item')
        pane.buffer.place_cursor(pane.buffer.get_end_iter())
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state = Gdk.KEY_Return, 0
        self.dialog.controls['continue_lists'].set_active(False)
        self.assertFalse(pane.editor_key_press(pane.editor, event))
        self.assertEqual(pane.text(), '- Item')
        self.dialog.controls['continue_lists'].set_active(True)
        self.assertTrue(pane.editor_key_press(pane.editor, event))
        self.assertEqual(pane.text(), '- Item\n- ')

    def test_saved_preferences_survive_new_window_and_restore_defaults(self):
        expected = replace(Preferences(), theme='light', editor_size=15, wrap_lines=False)
        self.dialog.save(expected)
        self.window.destroy()
        reopened = Window(self.app, self.root, preferences_path=self.preferences_path)
        self.addCleanup(reopened.destroy)
        self.assertEqual(reopened.preferences, expected)
        self.assertEqual(reopened.current().editor.get_wrap_mode(), Gtk.WrapMode.NONE)
        reopened.show_settings()
        reopened.settings_dialog.save(Preferences())
        self.assertEqual(reopened.preferences, Preferences())
        self.assertEqual(PreferencesStore(self.preferences_path).load(), Preferences())
        self.assertEqual(reopened.settings_dialog.controls['editor_size'].get_value_as_int(), 11)

    def test_save_failure_reports_error_and_reverts_control(self):
        original = self.window.preferences
        with patch.object(self.window.preferences_store, 'save', side_effect=OSError('Disk full')):
            self.dialog.controls['editor_size'].set_value(20)
        self.assertEqual(self.window.preferences, original)
        self.assertEqual(self.dialog.controls['editor_size'].get_value_as_int(), original.editor_size)
        self.assertTrue(self.dialog.error.get_visible())
        self.assertIn('Disk full', self.dialog.error.get_text())
        self.assertFalse(self.window.current().dirty)

    def test_preferences_update_other_open_windows(self):
        other = Window(self.app, self.root, preferences_path=self.preferences_path)
        self.addCleanup(other.destroy)
        expected = replace(Preferences(), editor_size=18, wrap_lines=False)
        self.dialog.save(expected)
        self.assertEqual(other.preferences, expected)
        self.assertEqual(other.current().editor.get_wrap_mode(), Gtk.WrapMode.NONE)
        self.assertAlmostEqual(other.current().tags['h1'].get_property('size-points'), 25 * 18 / 11, places=2)
        self.assertFalse(other.current().dirty)


if __name__ == '__main__':
    unittest.main()
