"""Regression checks for the outline, link navigation, and unchanged UI state."""
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
        self.addCleanup(self.window.destroy)
        self.drain_events()

    def drain_events(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not become idle')

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
        self.assertEqual(self.window.files.get_children(), rows)
        self.assertEqual(self.window.files.get_selected_row(), selected)
        (self.root / 'C.md').write_text('New note\n')
        self.window.refresh()
        self.assertEqual([row.path for row in self.window.files.get_children()],
                         ['A.md', 'B.md', 'C.md'])

    def test_search_and_vault_switch_still_update_rows(self):
        self.window.search_entry.set_text('Source')
        self.window.populate_files()
        self.assertEqual([row.path for row in self.window.files.get_children()], ['A.md'])
        (self.root / 'B.md').write_text('Source now matches\n')
        self.window.refresh()
        self.assertEqual([row.path for row in self.window.files.get_children()], ['A.md', 'B.md'])
        with TemporaryDirectory() as other:
            (Path(other) / 'Different.md').write_text('# Different\n')
            self.window.load_vault(other)
            self.assertEqual([row.path for row in self.window.files.get_children()], ['Different.md'])

    def test_switching_vault_with_same_paths_reuses_rows_and_selects_new_active_note(self):
        self.window.open_note('B.md')
        rows = self.window.files.get_children()
        self.assertEqual(self.window.files.get_selected_row().path, 'B.md')
        with TemporaryDirectory() as other:
            (Path(other) / 'A.md').write_text('# Different source\n')
            (Path(other) / 'B.md').write_text('# Different target\n')
            self.window.load_vault(other)
            self.assertEqual(self.window.files.get_children(), rows)
            self.assertEqual(self.window.files.get_selected_row().path, 'A.md')
            self.assertEqual(self.window.current().text(), '# Different source\n')

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


if __name__ == '__main__':
    unittest.main()
