"""Settings controls change the running editor and persist outside the vault."""
from tests.gui_wait import wait_for_work
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.preferences import Preferences, PreferencesStore, normalize_editor_font

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import Gio, Pango
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
        wait_for_work(self.window)
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

    def test_gear_clears_stale_hover_after_modal_open_and_close(self):
        self.dialog.destroy()
        gear = self.window.settings_button
        hover = Gtk.StateFlags.PRELIGHT | Gtk.StateFlags.ACTIVE
        def cross(kind):
            event = Gdk.Event.new(kind)
            event.window = gear.get_event_window()
            event.detail = Gdk.NotifyType.NONLINEAR
            event.set_device(gear.get_display().get_default_seat().get_pointer())
            gear.event(event)
        def drain():
            context = GLib.MainContext.default()
            for _ in range(1000):
                if not context.pending():
                    return
                context.iteration(False)
            self.fail('GTK did not settle')
        cross(Gdk.EventType.ENTER_NOTIFY)
        gear.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
        gear.emit('pressed')
        gear.emit('released')
        self.assertTrue(self.window.settings_dialog.get_visible())
        # The release handler runs after clicked and can restore stale hover.
        self.assertTrue(gear.get_state_flags() & Gtk.StateFlags.PRELIGHT)
        drain()
        self.assertFalse(gear.get_state_flags() & hover)
        gear.set_state_flags(hover, False)
        self.window.settings_dialog.response(Gtk.ResponseType.CLOSE)
        drain()
        self.assertIsNone(self.window.settings_dialog)
        self.assertFalse(gear.get_state_flags() & hover)
        # Subsequent pointer/button processing still highlights normally.
        cross(Gdk.EventType.ENTER_NOTIFY)
        gear.emit('pressed')
        self.assertTrue(gear.get_state_flags() & Gtk.StateFlags.PRELIGHT)
        cross(Gdk.EventType.LEAVE_NOTIFY)
        gear.emit('released')
        self.assertFalse(gear.get_state_flags() & hover)
        self.assertIsNone(self.window.settings_dialog)

    def test_small_editor_styles_and_font_controls_respect_minimum_size(self):
        for field in ('interface_size', 'editor_size'):
            self.assertEqual(self.dialog.controls[field].get_adjustment().get_lower(), 11)
        pane = self.window.current()
        for size in (11, 16):
            self.dialog.controls['editor_size'].set_value(size)
            for name in ('code', 'metadata', 'table', 'subscript', 'superscript'):
                with self.subTest(size=size, tag=name):
                    self.assertGreaterEqual(pane.tags[name].get_property('size-points'), 11)
                    self.assertEqual(pane.tags[name].get_property('scale'), 1)

    def test_page_headers_only_keep_actionable_descriptions(self):
        for title in ('Appearance', 'Editor', 'Notes', 'Vault', 'Plugins', 'Hotkeys'):
            with self.subTest(page=title):
                scroll = self.dialog.stack.get_child_by_name(title)
                body = scroll.get_child().get_child()
                children = body.get_children()
                self.assertEqual(children[0].get_text(), title)
                subtitles = [child for child in children
                             if child.get_style_context().has_class('settings-subtitle')]
                self.assertEqual(len(subtitles), int(title in ('Plugins', 'Hotkeys')))

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

    def test_lights_out_uses_black_surfaces_and_persists_from_theme_control(self):
        pane = self.window.current()
        before = pane.text()
        self.dialog.controls['theme'].set_active_id('lights_out')
        self.assertEqual(PreferencesStore(self.preferences_path).load().theme, 'lights_out')
        self.assertTrue(Gtk.Settings.get_default().get_property('gtk-application-prefer-dark-theme'))
        for name in ('canvas', 'panel', 'raised', 'button_bg', 'code_bg'):
            found, color = self.window.get_style_context().lookup_color(name)
            self.assertTrue(found)
            self.assertEqual((color.red, color.green, color.blue), (0, 0, 0))
        text = pane.tags['h1'].get_property('foreground-rgba')
        self.assertEqual((text.red, text.green, text.blue), (1, 1, 1))
        self.assertEqual(pane.text(), before)
        self.assertFalse(pane.dirty)

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
        wait_for_work(reopened)
        self.addCleanup(reopened.destroy)
        self.assertEqual(reopened.preferences, expected)
        self.assertEqual(reopened.current().editor.get_wrap_mode(), Gtk.WrapMode.NONE)
        reopened.show_settings()
        reopened.settings_dialog.save(Preferences())
        self.assertEqual(reopened.preferences, Preferences())
        self.assertEqual(PreferencesStore(self.preferences_path).load(), Preferences())
        self.assertEqual(reopened.settings_dialog.controls['editor_size'].get_value_as_int(), Preferences().editor_size)

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
        wait_for_work(other)
        self.addCleanup(other.destroy)
        expected = replace(Preferences(), editor_size=18, wrap_lines=False)
        self.dialog.save(expected)
        self.assertEqual(other.preferences, expected)
        self.assertEqual(other.current().editor.get_wrap_mode(), Gtk.WrapMode.NONE)
        self.assertAlmostEqual(other.current().tags['h1'].get_property('size-points'), 25 * 18 / 11, places=2)
        self.assertFalse(other.current().dirty)

    def test_custom_font_choice_applies_to_notes_and_can_be_cancelled(self):
        families = [family.get_name() for family in self.dialog.get_pango_context().list_families()]
        family = next(name for name in sorted(families) if normalize_editor_font(name) == name)
        chooser = Gtk.FontChooserDialog(title='Choose text font', transient_for=self.dialog, modal=True)
        chooser.set_level(Gtk.FontChooserLevel.FAMILY)
        chooser.set_font(family)
        self.assertEqual(chooser.get_font_desc().get_family(), family)
        chooser.destroy()
        chosen = {}

        class Description:
            def get_family(self):
                return family

        class Chooser:
            def __init__(self, *args, **kwargs):
                chosen['dialog'] = self

            def set_destroy_with_parent(self, _value):
                pass

            def set_level(self, level):
                chosen['level'] = level

            def set_preview_text(self, text):
                chosen['preview'] = text

            def set_font(self, font):
                chosen['font'] = font

            def run(self):
                return chosen['response']

            def get_font_desc(self):
                return Description()

            def destroy(self):
                chosen['destroyed'] = True

        control = self.dialog.controls['editor_font']
        with patch('librevault.settings.Gtk.FontChooserDialog', Chooser):
            chosen['response'] = Gtk.ResponseType.CANCEL
            control.set_active_id('choose-font')
            self.drain()
            self.assertTrue(chosen['destroyed'])
            self.assertEqual(self.window.preferences.editor_font, 'sans')
            self.assertEqual(control.get_active_id(), 'sans')
            self.assertFalse(self.preferences_path.exists())

            chosen['destroyed'] = False
            chosen['response'] = Gtk.ResponseType.OK
            control.set_active_id('choose-font')
            self.drain()

        self.assertEqual(chosen['level'], Gtk.FontChooserLevel.FAMILY)
        self.assertEqual(chosen['preview'], 'A space for your thoughts')
        self.assertTrue(chosen['destroyed'])
        self.assertEqual(self.window.preferences.editor_font, family)
        self.assertEqual(control.get_active_id(), f'font:{family}')
        self.assertEqual(PreferencesStore(self.preferences_path).load().editor_font, family)
        self.assertEqual(self.font_family(self.window.current().editor), family)
        self.assertEqual(self.font_family(self.preview_label()), family)
        self.dialog.change('editor_font', 'Bad;Font')
        self.assertEqual(self.window.preferences.editor_font, family)
        self.assertEqual(control.get_active_id(), f'font:{family}')
        self.assertIn('font family', self.dialog.error.get_text())
        self.dialog.save(Preferences())
        self.assertEqual(self.window.preferences.editor_font, 'sans')
        self.assertEqual(control.get_active_id(), 'sans')
        ids = [row[1] for row in control.get_model()]
        self.assertEqual(ids, ['sans', 'serif', 'monospace', 'choose-font'])

    def test_language_font_is_used_for_its_script_on_open_editors(self):
        families = {family.get_name() for family in self.dialog.get_pango_context().list_families()}
        needed = {'Noto Sans Arabic', 'Noto Sans CJK JP', 'Noto Sans CJK SC', 'Noto Sans Hebrew',
                  'Noto Serif CJK JP', 'Noto Naskh Arabic'}
        if not needed <= families:
            self.skipTest('Noto Arabic, Hebrew, and CJK fonts are not installed')
        sample = '日本語 مرحبا שלום'
        pane = self.window.current()
        pane.buffer.set_text(sample)
        pane.set_mode('reading')
        self.drain()
        reading = next(label for label in self.labels(pane) if sample in label.get_text())
        self.dialog.change('editor_font', 'Noto Sans Arabic')
        self.drain()
        for widget in (pane.editor, self.preview_label(), reading):
            faces = self.font_faces(widget, sample)
            self.assertIn('Noto Sans Arabic', faces)
            self.assertIn('Noto Sans CJK JP', faces)
            self.assertIn('Noto Sans Hebrew', faces)
            self.assertNotIn('WenQuanYi Zen Hei', faces)
            self.assertNotIn('DejaVu Sans', faces)
        self.dialog.change('editor_font', 'Noto Sans CJK SC')
        self.drain()
        self.assertEqual(self.font_faces(pane.editor, '日本語'), ['Noto Sans CJK SC'])
        self.assertEqual(self.font_faces(reading, '日本語'), ['Noto Sans CJK SC'])
        self.dialog.change('editor_font', 'serif')
        self.drain()
        serif_faces = self.font_faces(pane.editor, '日本語 مرحبا')
        self.assertIn('Noto Serif CJK JP', serif_faces)
        self.assertIn('Noto Naskh Arabic', serif_faces)
        self.assertNotIn('WenQuanYi Zen Hei', serif_faces)

    def font_faces(self, widget, text):
        # Reading PangoAnalysis.font through PyGObject is None on newer Pango and
        # segfaults on Ubuntu 24.04. Ask the fontset Pango itemizes each script with.
        context = widget.get_pango_context()
        description = context.get_font_description()
        language = context.get_language()
        faces = []
        for char in text:
            if char.isspace():
                continue
            script = Pango.script_for_unichar(char)
            derived = language if language.includes_script(script) else Pango.script_get_sample_language(script)
            fontset = context.load_fontset(description, derived or language)
            family = fontset.get_font(ord(char)).describe().get_family()
            if family not in faces:
                faces.append(family)
        return faces

    def labels(self, widget):
        found = []
        if isinstance(widget, Gtk.Label):
            found.append(widget)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                found.extend(self.labels(child))
        return found

    def font_family(self, widget):
        description = widget.get_style_context().get_property('font', Gtk.StateFlags.NORMAL)
        family = description.get_family() or ''
        return family.split(',')[0]

    def preview_label(self):
        def walk(widget):
            classes = widget.get_style_context().list_classes()
            if 'settings-preview' in classes:
                return widget
            if hasattr(widget, 'get_children'):
                for child in widget.get_children():
                    found = walk(child)
                    if found is not None:
                        return found
            return None
        found = walk(self.dialog)
        self.assertIsNotNone(found)
        return found

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                break
            context.iteration(False)


if __name__ == '__main__':
    unittest.main()
