"""Regression checks for the outline, link navigation, and unchanged UI state."""
from tests.gui_wait import wait_for_work
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.formatting import markdown_layout

try:
    from librevault.app import GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class OutlineTests(unittest.TestCase):
    def test_headings_follow_markdown_blocks_and_keep_source_offsets(self):
        text = ('---\n# metadata\n---\n\n```python\n# comment\n```\n\n'
                'Title with **emphasis**\n=======================\n\n'
                '  ## Second `heading` ##\n\n> ### Quoted\n')
        headings = markdown_layout(text).headings
        self.assertEqual([(h.label, h.level) for h in headings],
                         [('Title with emphasis', 1), ('Second heading', 2), ('Quoted', 3)])
        self.assertEqual([h.start for h in headings],
                         [text.index('Title with'), text.index('  ##'), text.index('> ###')])


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class WindowImprovementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.UiRegressionTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'A.md').write_text('# Source\n\n[[B]]\n')
        (self.root / 'B.md').write_text('Introduction\n\n# My heading\n\nBody\n')
        self.window = Window(self.app, str(self.root))
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.drain_events()

    def drain_events(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not become idle')

    def test_autosave_indicator_leaves_operation_message_alone(self):
        pane = self.window.current()
        self.window.status.set_text('Inserted template')
        self.assertEqual(self.window.save_status.get_text(), 'Saved')
        pane.buffer.insert_at_cursor('updated ')
        expected = pane.text()
        self.assertEqual(self.window.save_status.get_text(), 'Saving…')
        self.assertEqual(self.window.status.get_text(), 'Inserted template')
        self.assertIsNotNone(pane.autosave_source)
        # Exercise the real debounce callback and check the file it writes.
        loop = GLib.MainLoop()
        GLib.timeout_add_seconds(4, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertFalse(pane.dirty)
        self.assertIsNone(pane.autosave_source)
        self.assertEqual((self.root / pane.note.path).read_text(), expected)
        self.assertEqual(self.window.save_status.get_text(), 'Saved')
        self.assertEqual(self.window.status.get_text(), 'Inserted template')

    def test_failed_save_persists_until_manual_retry_succeeds(self):
        pane = self.window.current()
        self.window.status.set_text('Inserted template')
        pane.buffer.insert_at_cursor('updated ')
        with patch.object(self.window.vault, 'save', side_effect=OSError('Disk full')):
            GLib.source_remove(pane.autosave_source)
            pane.autosave()
            self.assertEqual(self.window.save_status.get_text(), 'Couldn’t save')
            self.assertIn('Disk full', self.window.save_status.get_tooltip_text())
            self.assertTrue(self.window.save_status.get_style_context().has_class('save-error'))
            pane.buffer.insert_at_cursor('more ')
            self.assertEqual(self.window.save_status.get_text(), 'Couldn’t save')
            self.window.save_current()
            self.assertEqual(self.window.save_status.get_text(), 'Couldn’t save')
        self.assertTrue(pane.dirty)
        self.window.save_current()
        self.assertEqual((self.root / pane.note.path).read_text(), pane.text())
        self.assertEqual(self.window.save_status.get_text(), 'Saved')
        self.assertFalse(self.window.save_status.get_style_context().has_class('save-error'))
        self.assertNotIn('Disk full', self.window.save_status.get_tooltip_text())
        self.assertEqual(self.window.status.get_text(), 'Inserted template')

    def test_save_indicator_follows_active_note(self):
        first = self.window.current()
        first.buffer.insert_at_cursor('updated ')
        with patch.object(self.window.vault, 'save', side_effect=OSError('Read only')):
            GLib.source_remove(first.autosave_source)
            first.autosave()
        self.window.open_note('B.md')
        self.drain_events()
        self.assertEqual(self.window.save_status.get_text(), 'Saved')
        self.window.open_note(first.note.path)
        self.drain_events()
        self.assertEqual(self.window.save_status.get_text(), 'Couldn’t save')
        self.window.open_note('B.md')
        self.drain_events()
        self.window.current().buffer.insert_at_cursor('second ')
        first.save()
        self.assertEqual(self.window.save_status.get_text(), 'Saving…')
        self.window.open_note(first.note.path)
        self.drain_events()
        self.assertEqual(self.window.save_status.get_text(), 'Saved')
        self.window.show_graph_tab()
        self.drain_events()
        self.assertEqual(self.window.save_status.get_text(), '')

    def test_saved_indicator_survives_recovery_cleanup_failure(self):
        pane = self.window.current()
        pane.buffer.insert_at_cursor('updated ')
        with patch.object(self.window.recovery, 'remove', side_effect=OSError('Read only')):
            self.window.save_current()
        self.assertEqual(self.window.save_status.get_text(), 'Saved')
        self.assertIn('could not remove recovery draft', self.window.status.get_text())
        self.assertEqual((self.root / pane.note.path).read_text(), pane.text())

    def test_cursor_movement_does_not_rebuild_context_but_edits_do(self):
        pane = self.window.current()
        with patch.object(self.window, 'update_context', wraps=self.window.update_context) as update:
            pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(2))
            self.drain_events()
            update.assert_not_called()
            pane.buffer.insert_at_cursor('updated ')
            self.drain_events()
            update.assert_called_once()

    def test_encoded_heading_fragment_and_encoded_hash_filename(self):
        self.window.follow_link('A.md', 'B.md#My%20heading')
        pane = self.window.current()
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        self.assertEqual(pane.note.path, 'B.md')
        self.assertEqual(cursor, pane.text().index('# My heading'))
        (self.root / 'B#draft.md').write_text('Start\n\n# A # heading\n')
        self.window.vault.refresh()
        self.window.follow_link('A.md', 'B%23draft.md#A%20%23%20heading')
        pane = self.window.current()
        self.assertEqual(pane.note.path, 'B#draft.md')
        self.assertEqual(pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset(),
                         pane.text().index('# A # heading'))

    def test_unchanged_refresh_preserves_rows_and_selection(self):
        rows = self.window.files.get_children()
        selected = self.window.files.get_selected_row()
        self.window.refresh()
        wait_for_work(self.window)
        self.assertEqual(self.window.files.get_children(), rows)
        self.assertEqual(self.window.files.get_selected_row(), selected)
        (self.root / 'C.md').write_text('New note\n')
        self.window.refresh()
        wait_for_work(self.window)
        self.assertEqual([row.path for row in self.window.files.get_children()],
                         ['A.md', 'B.md', 'C.md'])

    def test_search_and_vault_switch_still_update_rows(self):
        self.window.search_entry.set_text('Source')
        self.window.populate_files()
        wait_for_work(self.window)
        self.assertEqual([row.path for row in self.window.files.get_children()], ['A.md'])
        (self.root / 'B.md').write_text('Source now matches\n')
        self.window.refresh()
        wait_for_work(self.window)
        self.assertEqual([row.path for row in self.window.files.get_children()], ['A.md', 'B.md'])
        with TemporaryDirectory() as other:
            (Path(other) / 'Different.md').write_text('# Different\n')
            self.window.load_vault(other)
            wait_for_work(self.window)
            self.assertEqual([row.path for row in self.window.files.get_children()], ['Different.md'])

    def test_switching_vault_with_same_paths_reuses_rows_and_selects_new_active_note(self):
        self.window.open_note('B.md')
        rows = self.window.files.get_children()
        self.assertEqual(self.window.files.get_selected_row().path, 'B.md')
        with TemporaryDirectory() as other:
            (Path(other) / 'A.md').write_text('# Different source\n')
            (Path(other) / 'B.md').write_text('# Different target\n')
            self.window.load_vault(other)
            wait_for_work(self.window)
            self.assertEqual(self.window.files.get_children(), rows)
            self.assertEqual(self.window.files.get_selected_row().path, 'A.md')
            self.assertEqual(self.window.current().text(), '# Different source\n')

    def test_details_views_show_readable_properties_and_restore_selection(self):
        pane = self.window.current()
        pane.buffer.set_text('---\nstatus: open\ncount: 3\n---\n# Heading\n\n[[B]]\n')
        self.drain_events()
        self.window.details_buttons['properties'].set_active(True)
        labels = [child.get_text() for child in self.window.context.get_children()
                  if isinstance(child, Gtk.Label)]
        self.assertIn('status', labels)
        self.assertIn('open', labels)
        self.assertIn('3', labels)
        self.assertFalse(any(isinstance(child, Gtk.Entry) for child in self.window.context.get_children()))
        state = self.window.capture_workspace_state()
        self.assertEqual(state['details_view'], 'properties')
        self.window.details_buttons['links'].set_active(True)
        buttons = [child.get_label() for child in self.window.context.get_children()
                   if isinstance(child, Gtk.Button)]
        self.assertIn('B', buttons)
        self.assertNotIn('Edit properties…', buttons)
        self.window.restore_workspace(state)
        self.assertEqual(self.window.details_view, 'properties')
        self.assertTrue(self.window.details_buttons['properties'].get_active())
        pane.dirty = False

    def test_outline_buttons_jump_to_real_headings(self):
        pane = self.window.current()
        text = '```\n# Fake\n```\n\nReal title\n==========\n'
        pane.buffer.set_text(text)
        self.drain_events()
        labels = [child.get_label() for child in self.window.context.get_children()
                  if isinstance(child, Gtk.Button)]
        self.assertIn('Real title', labels)
        self.assertNotIn('Fake', labels)
        button = next(child for child in self.window.context.get_children()
                      if isinstance(child, Gtk.Button) and child.get_label() == 'Real title')
        button.clicked()
        self.assertEqual(pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset(),
                         text.index('Real title'))
        # Avoid a save prompt if a future test adds a vault switch.
        pane.dirty = False

    def test_outline_hierarchy_wraps_long_titles_and_tracks_cursor_without_rebuild(self):
        pane = self.window.current()
        long_title = 'A detailed heading that should remain fully readable in a narrow outline'
        text = ('Preface\n\n# Overview\n\nBody\n\n## First section\n\n### ' + long_title
                + '\n\nDetails\n\n## Second section\n\nMore text\n\n#### Nested section\n')
        pane.buffer.set_text(text)
        self.drain_events()
        rows = self.window.outline_rows
        self.assertEqual([button.get_margin_start() for _, button in rows], [0, 12, 24, 12, 24])
        long_button = rows[2][1]
        self.assertEqual(long_button.get_label(), long_title)
        self.assertTrue(long_button.get_child().get_line_wrap())
        self.assertEqual(long_button.get_tooltip_text(), long_title)
        self.assertTrue(rows[0][1].get_style_context().has_class('outline-root'))
        with patch.object(self.window, 'update_context', wraps=self.window.update_context) as update:
            pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(text.index('More text')))
            self.drain_events()
            update.assert_not_called()
        active = [button.get_label() for _, button in rows
                  if button.get_style_context().has_class('current-heading')]
        self.assertEqual(active, ['Second section'])
        pane.buffer.place_cursor(pane.buffer.get_start_iter())
        self.drain_events()
        self.assertFalse(any(button.get_style_context().has_class('current-heading') for _, button in rows))

    def test_outline_explains_when_note_has_no_headings(self):
        pane = self.window.current()
        pane.buffer.set_text('Just a paragraph.\n')
        self.drain_events()
        self.assertEqual(self.window.outline_rows, [])
        labels = [child.get_text() for child in self.window.context.get_children()
                  if isinstance(child, Gtk.Label)]
        self.assertIn('No headings in this note.', labels)


if __name__ == '__main__':
    unittest.main()
