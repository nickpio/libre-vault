"""Undo, formatting, and find/replace operate on real GTK Markdown buffers."""
from tests.gui_wait import wait_for_work
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.shortcuts import EDITOR_SHORTCUTS

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class EditorToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.EditorToolsTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Original\n\nOne café and one CAFE.\n', encoding='utf-8')
        self.window = Window(self.app, self.root,
                             preferences_path=Path(self.directory.name) / 'settings.json')
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.pane = self.window.current()
        self.drain()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def source(self, text):
        self.pane.buffer.set_text(text)
        self.pane.save()
        self.pane.history.reset()
        self.drain()

    def select(self, start, end):
        self.pane.buffer.select_range(self.pane.buffer.get_iter_at_offset(end),
                                      self.pane.buffer.get_iter_at_offset(start))

    def type_text(self, text):
        for char in text:
            self.pane.buffer.begin_user_action()
            self.pane.buffer.insert_at_cursor(char)
            self.pane.buffer.end_user_action()

    def key(self, keyval, modifiers=0, widget=None):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state = keyval, modifiers
        return (widget or self.pane.editor).emit('key-press-event', event)

    def test_typing_undo_redo_dirty_state_and_new_edit_clears_redo(self):
        original = self.pane.text()
        self.type_text('word')
        self.assertEqual(len(self.pane.history.undo_stack), 1)
        self.assertTrue(self.pane.dirty)
        self.key(Gdk.KEY_z, Gdk.ModifierType.CONTROL_MASK)
        self.assertEqual(self.pane.text(), original)
        self.assertFalse(self.pane.dirty)
        self.key(Gdk.KEY_Z, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertEqual(self.pane.text(), original + 'word')
        self.pane.undo()
        self.type_text('new')
        self.assertFalse(self.pane.history.can_redo)

    def test_autosave_writes_note_and_removes_recovery_draft(self):
        self.pane.buffer.insert_at_cursor(' autosaved')
        self.assertTrue(self.pane.dirty)
        self.window.recovery.flush()
        drafts = list(self.window.recovery.session.glob('*.json'))
        self.assertEqual(len(drafts), 1)
        GLib.source_remove(self.pane.autosave_source)
        self.pane.autosave()
        self.assertFalse(self.pane.dirty)
        self.assertEqual((self.root / 'Note.md').read_text(encoding='utf-8'), self.pane.text())
        self.assertEqual(list(self.window.recovery.session.glob('*.json')), [])

    def test_save_keeps_history_and_undoing_across_save_is_dirty(self):
        original = self.pane.text()
        self.type_text('saved')
        self.pane.save()
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.assertTrue(self.pane.dirty)
        self.key(Gdk.KEY_Z, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertFalse(self.pane.dirty)
        self.assertEqual((self.root / 'Note.md').read_text(), self.pane.text())

    def test_cursor_movement_breaks_typing_group_and_undo_restores_selection(self):
        original = self.pane.text()
        self.type_text('word')
        self.pane.buffer.place_cursor(self.pane.buffer.get_start_iter())
        self.pane.buffer.place_cursor(self.pane.buffer.get_end_iter())
        self.type_text('s')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original + 'word')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        start = original.index('café')
        self.select(start, start + 4)
        selection = self.pane.history.selection()
        self.key(Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK)
        self.assertIn('**café**', self.pane.text())
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.assertEqual(self.pane.history.selection(), selection)
        self.pane.redo()
        self.assertIn('**café**', self.pane.text())

    def test_bold_and_italic_toggle_without_removing_the_other_style(self):
        self.source('café')
        self.select(0, 4)
        self.pane.bold()
        self.assertEqual(self.pane.text(), '**café**')
        self.pane.italic()
        self.assertEqual(self.pane.text(), '***café***')
        self.pane.italic()
        self.assertEqual(self.pane.text(), '**café**')
        self.pane.bold()
        self.assertEqual(self.pane.text(), 'café')
        self.assertFalse(self.pane.dirty)

    def test_empty_selection_creates_markers_with_caret_inside(self):
        self.source('')
        self.key(Gdk.KEY_b, Gdk.ModifierType.CONTROL_MASK)
        self.assertEqual(self.pane.text(), '****')
        self.assertEqual(self.pane.history.selection().cursor, 2)
        self.pane.undo()
        self.assertEqual(self.pane.text(), '')
        self.assertEqual(self.pane.history.selection().cursor, 0)

    def test_list_enter_and_task_toggle_are_single_undo_actions(self):
        original = '- item\n- [ ] task'
        self.source(original)
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(6))
        self.key(Gdk.KEY_Return)
        self.assertEqual(self.pane.text(), '- item\n- \n- [ ] task')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.pane.render()
        checkbox = next(widget for item, widget in self.pane.list_widgets if item.task_offset is not None)
        checkbox.set_active(True)
        self.assertEqual(self.pane.text(), '- item\n- [x] task')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.pane.redo()
        self.assertIn('[x]', self.pane.text())

    def test_external_reload_clears_history_and_conflicting_local_edits_are_retained(self):
        self.type_text('local')
        self.pane.save()
        self.assertTrue(self.pane.history.can_undo)
        (self.root / 'Note.md').write_text('# External\n')
        self.window.refresh()
        wait_for_work(self.window)
        self.assertEqual(self.pane.text(), '# External\n')
        self.assertFalse(self.pane.history.can_undo)
        self.assertFalse(self.pane.history.can_redo)
        self.type_text('unsaved')
        local = self.pane.text()
        (self.root / 'Note.md').write_text('# Competing\n')
        self.window.refresh()
        wait_for_work(self.window)
        self.assertEqual(self.pane.text(), local)
        self.assertTrue(self.pane.history.can_undo)

    def test_histories_are_independent_per_tab(self):
        self.type_text('first')
        self.window.vault.create('Second')
        self.window.open_note('Second.md')
        second = self.window.current()
        second.buffer.insert_at_cursor('second')
        self.window.open_note('Note.md')
        self.pane.undo()
        self.assertNotIn('first', self.pane.text())
        self.assertEqual(second.text(), 'second')
        second.undo()
        self.assertEqual(second.text(), '')

    def test_render_and_cursor_selection_do_not_record_source_edits(self):
        self.pane.buffer.place_cursor(self.pane.buffer.get_start_iter())
        self.pane.render()
        self.window.update_context()
        wait_for_work(self.window)
        self.drain()
        self.assertFalse(self.pane.history.can_undo)
        self.assertFalse(self.pane.dirty)

    def test_find_literal_case_unicode_wrap_and_keyboard_close(self):
        self.source('One **one** café one CAFE.')
        self.key(Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK)
        bar = self.pane.findbar
        self.assertTrue(bar.get_visible())
        bar.query.set_text('one')
        self.assertEqual(len(bar.matches), 3)
        bar.step(True)
        self.assertEqual(tuple(it.get_offset() for it in self.pane.buffer.get_selection_bounds()), (0, 3))
        bar.step(False)
        self.assertEqual(bar.count.get_text(), '3 of 3')
        bar.match_case.set_active(True)
        self.assertEqual(len(bar.matches), 2)
        bar.match_case.set_active(False)
        bar.query.set_text('CAFÉ')
        self.assertEqual(len(bar.matches), 1)
        bar.query.set_text('.')
        self.assertEqual(len(bar.matches), 1)
        self.assertFalse(self.pane.dirty)
        self.key(Gdk.KEY_Escape, widget=self.window)
        self.assertFalse(bar.get_visible())
        self.assertIs(self.window.get_focus(), self.pane.editor)

    def test_replace_current_and_replace_all_are_undoable_and_do_not_loop_on_inserted_matches(self):
        original = 'one ONE one'
        self.source(original)
        self.pane.show_replace()
        bar = self.pane.findbar
        bar.query.set_text('one')
        bar.replacement.set_text('two')
        bar.replace_current()
        self.assertEqual(self.pane.text(), 'two ONE one')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        bar.replacement.set_text('oneone')
        bar.replace_all()
        self.assertEqual(self.pane.text(), 'oneone oneone oneone')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.assertFalse(self.pane.dirty)
        self.pane.redo()
        self.assertEqual(self.pane.text(), 'oneone oneone oneone')

    def test_find_empty_results_and_ctrl_h_from_search_do_not_edit_note(self):
        original = self.pane.text()
        self.pane.show_find()
        bar = self.pane.findbar
        self.key(Gdk.KEY_h, Gdk.ModifierType.CONTROL_MASK, self.window)
        self.assertTrue(bar.replace_row.get_visible())
        bar.query.set_text('no match here')
        self.assertFalse(bar.replace_all_button.get_sensitive())
        bar.replace_all()
        bar.step(True)
        self.assertEqual(self.pane.text(), original)
        self.assertFalse(self.pane.dirty)

    def test_link_insertion_is_one_undo_action_and_escapes_paths(self):
        self.source('café')
        self.select(0, 4)
        def answer():
            dialog = next(w for w in Gtk.Window.list_toplevels()
                          if isinstance(w, Gtk.Dialog) and w.get_title() == 'Insert link')
            target = next(child for child in dialog.get_content_area().get_children()
                          if isinstance(child, Gtk.Entry) and child.get_accessible().get_name() == 'Link target')
            target.set_text('Notes/My note (draft).md')
            dialog.response(Gtk.ResponseType.OK)
            return False
        GLib.idle_add(answer)
        self.key(Gdk.KEY_k, Gdk.ModifierType.CONTROL_MASK)
        self.assertEqual(self.pane.text(), '[café](Notes/My%20note%20%28draft%29.md)')
        self.pane.undo()
        self.assertEqual(self.pane.text(), 'café')
        self.assertFalse(self.pane.dirty)

    def test_editor_context_menu_exposes_undo_with_current_sensitivity(self):
        menu = Gtk.Menu()
        self.addCleanup(menu.destroy)
        self.pane.populate_edit_menu(self.pane.editor, menu)
        self.assertEqual(menu.get_children()[0].get_label(), 'Undo')
        self.assertFalse(menu.get_children()[0].get_sensitive())
        self.type_text('word')
        changed = Gtk.Menu()
        self.addCleanup(changed.destroy)
        self.pane.populate_edit_menu(self.pane.editor, changed)
        self.assertTrue(changed.get_children()[0].get_sensitive())
        changed.get_children()[0].activate()
        self.assertFalse(self.pane.dirty)

    def test_unicode_selection_replacement_and_backward_deletion_restore_source(self):
        original = '🙂 café 漢字'
        self.source(original)
        self.select(2, 6)
        before = self.pane.history.selection()
        self.pane.buffer.begin_user_action()
        self.pane.buffer.delete_selection(True, True)
        self.pane.buffer.insert_at_cursor('tea')
        self.pane.buffer.end_user_action()
        self.assertEqual(self.pane.text(), '🙂 tea 漢字')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.assertEqual(self.pane.history.selection(), before)
        self.pane.buffer.place_cursor(self.pane.buffer.get_end_iter())
        for _ in range(2):
            cursor = self.pane.buffer.get_iter_at_mark(self.pane.buffer.get_insert()).get_offset()
            self.pane.buffer.begin_user_action()
            self.pane.buffer.delete(self.pane.buffer.get_iter_at_offset(cursor - 1),
                                     self.pane.buffer.get_iter_at_offset(cursor))
            self.pane.buffer.end_user_action()
        self.assertEqual(self.pane.text(), '🙂 café ')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)

    def test_search_and_replace_cover_concealed_markdown_and_frontmatter(self):
        original = '---\ntitle: Hidden\n---\n\n**Hidden** and `Hidden`\n'
        self.source(original)
        self.pane.show_replace()
        bar = self.pane.findbar
        bar.query.set_text('Hidden')
        self.assertEqual(len(bar.matches), 3)
        bar.replacement.set_text('Found')
        bar.replace_all()
        self.assertEqual(self.pane.text(), original.replace('Hidden', 'Found'))
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.assertFalse(self.pane.dirty)

    def test_link_dialog_does_not_apply_stale_offsets_after_external_reload(self):
        def answer():
            dialog = next(w for w in Gtk.Window.list_toplevels()
                          if isinstance(w, Gtk.Dialog) and w.get_title() == 'Insert link')
            target = next(child for child in dialog.get_content_area().get_children()
                          if isinstance(child, Gtk.Entry) and child.get_accessible().get_name() == 'Link target')
            target.set_text('https://example.com')
            (self.root / 'Note.md').write_text('# External\n')
            self.window.refresh()
            wait_for_work(self.window)
            dialog.response(Gtk.ResponseType.OK)
            return False
        GLib.idle_add(answer)
        self.pane.insert_link()
        self.assertEqual(self.pane.text(), '# External\n')
        self.assertFalse(self.pane.history.can_undo)
        self.assertIn('note changed', self.window.status.get_text().lower())

    def test_history_is_bounded_without_storing_whole_document_snapshots(self):
        original = self.pane.text()
        for _ in range(501):
            self.pane.buffer.insert_at_cursor('xx')
        self.assertEqual(len(self.pane.history.undo_stack), 500)
        for _ in range(500):
            self.pane.undo()
        self.assertEqual(self.pane.text(), original + 'xx')
        self.assertFalse(self.pane.history.can_undo)

    def test_registered_editor_shortcuts_invoke_their_actions(self):
        for shortcut in EDITOR_SHORTCUTS:
            key, modifiers = Gtk.accelerator_parse(shortcut.accelerator)
            with self.subTest(shortcut=shortcut.accelerator), patch.object(self.pane, shortcut.handler) as action:
                self.assertTrue(self.key(key, modifiers))
                action.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
