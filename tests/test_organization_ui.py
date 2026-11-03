"""Exercise organization, metadata editing, and filtered results through GTK."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

try:
    from librevault.app import GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class OrganizationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.OrganizationUiTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.root = base / 'vault'
        self.root.mkdir()
        (self.root / 'A.md').write_text('# A\n')
        (self.root / 'B.md').write_text('---\ntags: [work]\nstatus: active\n---\n[[A]] alpha result\n')
        self.window = Window(self.app, self.root, preferences_path=base / 'config/settings.json')
        self.addCleanup(self.window.destroy)
        self.drain()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def test_menu_routes_rename_move_and_trash_to_clicked_item(self):
        row = next(row for row in self.window.files.get_children() if row.path == 'B.md')
        menu = self.window.explorer_menu(row)
        self.addCleanup(menu.destroy)
        for label, method in [('Rename…', 'rename_item'), ('Move to…', 'move_item'), ('Move to trash…', 'trash_item')]:
            with self.subTest(label=label), patch.object(self.window, method) as action:
                next(item for item in menu.get_children() if isinstance(item, Gtk.MenuItem) and item.get_label() == label).activate()
                action.assert_called_once_with('B.md')

    def test_move_saves_dirty_incoming_links_and_updates_open_panes(self):
        self.window.open_note('B.md')
        pane = self.window.current()
        pane.buffer.insert_at_cursor('unsaved ')
        with patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.YES):
            self.assertTrue(self.window.move_path('A.md', 'Renamed.md'))
        self.assertIn('Renamed.md', self.window.panes)
        self.assertNotIn('A.md', self.window.panes)
        self.assertIn('[[Renamed]]', pane.text())
        self.assertIn('unsaved ', pane.text())
        self.assertFalse(pane.dirty)
        self.assertEqual((self.root / 'B.md').read_text(), pane.text())

    def test_cancel_dirty_move_retains_source_and_editor(self):
        pane = self.window.current()
        pane.buffer.insert_at_cursor('retained ')
        with patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.CANCEL):
            self.assertFalse(self.window.move_path('A.md', 'Renamed.md'))
        self.assertTrue((self.root / 'A.md').is_file())
        self.assertFalse((self.root / 'Renamed.md').exists())
        self.assertTrue(pane.dirty)
        self.assertIn('retained ', pane.text())

    def test_trash_closes_affected_tab_and_retains_durable_contents(self):
        pane = self.window.current()
        pane.buffer.insert_at_cursor('save before trash ')
        with patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.YES):
            self.assertTrue(self.window.trash_path('A.md'))
        self.assertNotIn('A.md', self.window.panes)
        self.assertNotIn('A.md', self.window.vault.notes)
        targets = list((self.root / '.trash').glob('A-*.md'))
        self.assertEqual(len(targets), 1)
        self.assertIn('save before trash ', targets[0].read_text())

    def test_properties_are_single_undoable_source_edit_and_tags_filter(self):
        pane = self.window.current()
        original = pane.text()
        self.window.set_note_properties('aliases: [Alternative]\ntags: [project/new]\nstatus: open\n')
        self.assertIn('aliases: [Alternative]', pane.text())
        self.assertTrue(pane.dirty)
        self.assertEqual((self.root / 'A.md').read_text(), original)
        pane.undo()
        self.assertEqual(pane.text(), original)
        self.assertFalse(pane.dirty)
        self.window.open_note('B.md')
        self.window.details_buttons['properties'].set_active(True)
        tags = [child for child in self.window.context.get_children() if isinstance(child, Gtk.Button)
                and child.get_child().get_text() == '#work']
        self.assertEqual(len(tags), 1)
        tags[0].clicked()
        self.window.populate_files()
        self.assertEqual(self.window.search_entry.get_text(), 'tag:#work')
        self.assertEqual([row.path for row in self.window.files.get_children()], ['B.md'])

    def test_search_renders_highlighted_snippet_and_updates_unchanged_path(self):
        self.window.search_entry.set_text('path:B alpha')
        self.window.populate_files()
        row = self.window.files.get_children()[0]
        self.assertEqual(row.path, 'B.md')
        labels = row.get_child().get_children()[1].get_children()
        self.assertIn('<b>alpha</b>', labels[-1].get_label())
        self.assertIn('result', labels[-1].get_text())
        note = self.window.vault.read('B.md')
        self.window.vault.save(note, note.text.replace('result', 'updated snippet'))
        self.window.populate_files()
        new_row = self.window.files.get_children()[0]
        self.assertIsNot(new_row, row)
        self.assertIn('updated snippet', new_row.search_signature.snippet)
