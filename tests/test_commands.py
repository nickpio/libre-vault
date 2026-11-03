"""Keyboard-to-palette-to-cheatsheet behavior on the real GTK widgets."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.shortcuts import APP_SHORTCUTS, shortcut_id

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class CommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.CommandTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        (Path(self.directory.name) / 'Note.md').write_text('# Original\n')
        self.window = Window(self.app, self.directory.name)
        self.addCleanup(self.window.destroy)
        self.drain_events()

    def drain_events(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def event(self, key, modifiers=0):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = key
        event.state = modifiers
        return event

    def open_palette(self):
        self.assertTrue(self.window.key_press(
            self.window, self.event(Gdk.KEY_p, Gdk.ModifierType.MOD1_MASK)))
        # Overlay allocation can run on the next frame, after idle callbacks.
        loop = GLib.MainLoop()
        def stop():
            loop.quit()
            return False
        GLib.timeout_add(30, stop)
        loop.run()
        self.drain_events()
        return self.window.palette

    def test_cheatsheet_shortcut_and_hint_follow_custom_binding(self):
        shortcut = next(item for item in APP_SHORTCUTS if item.handler == 'show_keybinds')
        self.assertEqual(shortcut.accelerator, '<Alt>k')
        self.assertEqual(self.window.keybind_hint_keys.get_text(),
                         Gtk.accelerator_get_label(*Gtk.accelerator_parse(shortcut.accelerator)))
        self.assertTrue(self.window.key_press(self.window, self.event(Gdk.KEY_k, Gdk.ModifierType.MOD1_MASK)))
        self.assertTrue(self.window.cheatsheet.get_visible())
        self.window.cheatsheet.destroy()
        self.window.keybind_hint.clicked()
        self.assertTrue(self.window.cheatsheet.get_visible())
        self.window.cheatsheet.destroy()
        with patch.object(self.window.hotkey_store, 'save'):
            self.window.set_hotkey(shortcut_id('app', shortcut), '<Control><Alt>k')
        self.assertEqual(self.window.keybind_hint_keys.get_text(),
                         Gtk.accelerator_get_label(*Gtk.accelerator_parse('<Control><Alt>k')))
        self.assertTrue(self.window.key_press(self.window, self.event(
            Gdk.KEY_k, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK)))

    def test_alt_p_search_enter_opens_cheatsheet_without_editing_note(self):
        pane = self.window.current()
        before = pane.text()
        palette = self.open_palette()
        self.assertEqual(palette.results.get_row_at_index(0).title, 'Open keybind cheatsheet')
        self.assertIs(self.window.get_focus(), palette.search)
        palette.search.set_text('KEYBIND cheat')
        self.assertIsNotNone(palette.results.get_selected_row())
        palette.search.emit('activate')
        self.drain_events()
        self.assertIsNone(self.window.palette)
        self.assertTrue(self.window.cheatsheet.get_visible())
        self.assertEqual(pane.text(), before)
        self.assertFalse(pane.dirty)
        sheet = self.window.cheatsheet
        self.assertTrue(sheet.key_press(sheet, self.event(Gdk.KEY_Escape)))
        self.assertIsNone(self.window.cheatsheet)

    def test_no_matches_and_escape_leave_note_and_tabs_unchanged(self):
        pane = self.window.current()
        palette = self.open_palette()
        palette.search.set_text('missing command')
        self.assertTrue(palette.empty.get_visible())
        palette.search.emit('activate')
        self.assertIs(self.window.palette, palette)
        self.assertIsNone(self.window.cheatsheet)
        palette.key_press(palette, self.event(Gdk.KEY_Escape))
        self.assertIsNone(self.window.palette)
        self.assertIs(self.window.current(), pane)
        self.assertFalse(pane.dirty)

    def test_command_click_and_repeated_open_do_not_duplicate_dialogs(self):
        palette = self.open_palette()
        self.window.show_command_palette()
        self.assertIs(self.window.palette, palette)
        palette.results.emit('row-activated', palette.results.get_row_at_index(0))
        sheet = self.window.cheatsheet
        self.window.show_keybinds()
        self.assertIs(self.window.cheatsheet, sheet)
        sheet.response(Gtk.ResponseType.CLOSE)
        self.assertIsNone(self.window.cheatsheet)

    def test_cheatsheet_uses_every_registered_shortcut_and_handlers_still_work(self):
        self.window.show_keybinds()
        descriptions = dict((description, keys) for keys, description
                            in self.window.cheatsheet.groups[0][1])
        for shortcut in APP_SHORTCUTS:
            self.assertIn(shortcut.description, descriptions)
            key, modifiers = Gtk.accelerator_parse(shortcut.accelerator)
            with patch.object(self.window, shortcut.handler) as action:
                self.assertTrue(self.window.key_press(self.window, self.event(key, modifiers)))
                action.assert_called_once_with()
        self.assertFalse(self.window.key_press(self.window, self.event(Gdk.KEY_p)))
        self.assertFalse(self.window.key_press(
            self.window, self.event(Gdk.KEY_p, Gdk.ModifierType.CONTROL_MASK)))

    def test_palette_works_without_a_vault(self):
        self.window.close_tab()
        self.window.vault = None
        self.open_palette().search.emit('activate')
        self.assertTrue(self.window.cheatsheet.get_visible())

    def test_documented_tab_shortcut_switches_notes(self):
        self.window.vault.create('Second')
        self.window.open_note('Second.md')
        self.assertEqual(self.window.current().note.path, 'Second.md')
        self.assertTrue(Gtk.bindings_activate(
            self.window.tabs, Gdk.KEY_Page_Up,
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK))
        self.assertEqual(self.window.current().note.path, 'Note.md')

    def test_palette_is_a_compact_overlay_near_the_top(self):
        palette = self.open_palette()
        self.assertIs(palette.get_parent(), self.window.overlay)
        self.assertIs(palette.get_toplevel(), self.window)
        card = palette.card.get_allocation()
        self.assertLess(card.y, 80)
        self.assertLess(card.height, 320)
        self.assertGreater(card.height, 220)
        self.assertAlmostEqual(card.width, min(760, palette.get_allocated_width() - 32), delta=2)
        self.assertAlmostEqual(card.x + card.width / 2,
                               palette.get_allocated_width() / 2, delta=2)
        self.assertTrue(self.window.key_press(self.window, self.event(Gdk.KEY_Tab)))
        self.assertIs(self.window.get_focus(), palette.search)
        with patch.object(self.window, 'new_note') as create:
            self.window.key_press(self.window, self.event(Gdk.KEY_n, Gdk.ModifierType.CONTROL_MASK))
            create.assert_not_called()

    def test_outside_click_dismisses_and_restores_previous_focus(self):
        previous = self.window.get_focus()
        palette = self.open_palette()
        event = Gdk.EventButton()
        event.type = Gdk.EventType.BUTTON_PRESS
        event.button = 1
        event.window = palette.get_window()
        source = Gtk.get_event_widget(event)
        event.x, event.y = palette.card.translate_coordinates(source, 20, 20)
        self.assertFalse(palette.outside_click(palette, event))
        self.assertIs(self.window.palette, palette)
        event.x, event.y = palette.translate_coordinates(source, 5, palette.get_allocated_height() - 5)
        self.assertTrue(palette.outside_click(palette, event))
        self.assertIsNone(self.window.palette)
        self.assertIs(self.window.get_focus(), previous)

    def test_cheatsheet_arrow_keys_navigate_shortcuts_and_sections(self):
        self.window.show_keybinds()
        sheet = self.window.cheatsheet
        sheet.focus_results()
        self.assertEqual(sheet.bindings.get_selected_row().get_index(), 0)
        for key, expected in ((Gdk.KEY_Down, 1), (Gdk.KEY_Up, 0), (Gdk.KEY_Up, 0)):
            self.assertTrue(sheet.emit('key-press-event', self.event(key)))
            self.assertEqual(sheet.bindings.get_selected_row().get_index(), expected)
        sheet.emit('key-press-event', self.event(Gdk.KEY_Right))
        self.assertEqual(sheet.bindings.get_selected_row().section, 1)
        sheet.emit('key-press-event', self.event(Gdk.KEY_Left))
        self.assertEqual(sheet.bindings.get_selected_row().get_index(), 0)
        headers = [row.get_header().get_text() for row in sheet.visible_rows() if row.get_header()]
        self.assertEqual(headers, [title for title, _ in sheet.groups])

    def test_cheatsheet_navigation_scrolls_to_selected_row(self):
        self.window.show_keybinds()
        sheet = self.window.cheatsheet
        sheet.focus_results()
        # Render the list before checking its scroll position.
        loop = GLib.MainLoop()
        def stop():
            loop.quit()
            return False
        GLib.timeout_add(30, stop)
        loop.run()
        sheet.emit('key-press-event', self.event(Gdk.KEY_End))
        row = sheet.bindings.get_selected_row()
        self.assertEqual(row.get_index(), len(sheet.bindings.get_children()) - 1)
        adjustment = sheet.scroll.get_vadjustment()
        self.assertGreater(adjustment.get_value(), 0)
        self.assertLessEqual(row.get_allocation().y + row.get_allocated_height(),
                             adjustment.get_value() + adjustment.get_page_size() + 1)
        sheet.emit('key-press-event', self.event(Gdk.KEY_Home))
        self.assertEqual(sheet.bindings.get_selected_row().get_index(), 0)
        self.assertEqual(adjustment.get_value(), 0)
        sheet.emit('key-press-event', self.event(Gdk.KEY_Escape))
        self.assertIsNone(self.window.cheatsheet)

    def test_cheatsheet_search_matches_actions_keys_and_sections(self):
        self.window.show_keybinds()
        sheet = self.window.cheatsheet
        total = len(sheet.visible_rows())
        self.assertIs(sheet.get_focus(), sheet.search)
        sheet.search.set_text('SAVE active')
        self.assertEqual(len(sheet.visible_rows()), 1)
        self.assertIn('Ctrl+S', sheet.visible_rows()[0].get_accessible().get_name())
        sheet.search.set_text('Ctrl+O')
        matches = [row.get_accessible().get_name() for row in sheet.visible_rows()]
        self.assertEqual(len(matches), 2)
        self.assertTrue(any('Open a vault' in name for name in matches))
        self.assertTrue(any('Quick switcher' in name for name in matches))
        sheet.search.set_text('FILE explorer')
        self.assertGreater(len(sheet.visible_rows()), 1)
        self.assertEqual([row.get_header().get_text() for row in sheet.visible_rows() if row.get_header()],
                         ['File explorer'])
        sheet.search.set_text('')
        self.assertEqual(len(sheet.visible_rows()), total)

    def test_cheatsheet_navigation_skips_hidden_results_and_preserves_typing(self):
        self.window.show_keybinds()
        sheet = self.window.cheatsheet
        sheet.search.set_text('tab')
        sheet.search.set_position(2)
        rows = sheet.visible_rows()
        sheet.emit('key-press-event', self.event(Gdk.KEY_Down))
        self.assertIs(sheet.bindings.get_selected_row(), rows[1])
        self.assertIs(sheet.get_focus(), sheet.search)
        self.assertEqual(sheet.search.get_position(), 2)
        self.assertFalse(sheet.key_press(sheet, self.event(Gdk.KEY_Left)))
        sheet.search.emit('activate')
        sheet.emit('key-press-event', self.event(Gdk.KEY_End))
        self.assertIs(sheet.bindings.get_selected_row(), rows[-1])
        sheet.emit('key-press-event', self.event(Gdk.KEY_Up))
        self.assertIs(sheet.bindings.get_selected_row(), rows[-2])
        sheet.emit('key-press-event', self.event(Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK))
        self.assertIs(sheet.get_focus(), sheet.search)
        self.assertEqual(sheet.search.get_selection_bounds(), (0, 3))

    def test_cheatsheet_empty_search_results_are_safe_to_navigate(self):
        self.window.show_keybinds()
        sheet = self.window.cheatsheet
        sheet.search.set_text('no-such-shortcut')
        self.assertEqual(sheet.visible_rows(), [])
        self.assertIsNone(sheet.bindings.get_selected_row())
        self.assertTrue(sheet.empty.get_child_visible())
        for key in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.assertTrue(sheet.emit('key-press-event', self.event(key)))
        sheet.search.emit('activate')
        self.assertIs(sheet.get_focus(), sheet.search)
        sheet.search.set_text('save')
        self.assertIsNotNone(sheet.bindings.get_selected_row())
        sheet.emit('key-press-event', self.event(Gdk.KEY_Escape))
        self.assertIsNone(self.window.cheatsheet)


if __name__ == '__main__':
    unittest.main()
