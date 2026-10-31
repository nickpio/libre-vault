"""Explorer creation actions use the clicked folder and preserve vault bounds."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.vault import Vault, VaultError

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class FolderTests(unittest.TestCase):
    def test_create_folder_lists_empty_folders_and_accepts_notes(self):
        with TemporaryDirectory() as directory:
            vault = Vault(directory)
            self.assertEqual(vault.create_folder(' Projects '), 'Projects')
            vault.create_folder('Projects/Empty')
            self.assertEqual(vault.folders, ['Projects', 'Projects/Empty'])
            vault.create('Projects/New note')
            vault.refresh()
            self.assertEqual(vault.folders, ['Projects', 'Projects/Empty'])
            self.assertIn('Projects/New note.md', vault.notes)

    def test_rejects_hidden_outside_missing_parent_and_existing_paths(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            vault = Vault(directory)
            vault.create_folder('Existing')
            vault.create('Note')
            (Path(directory) / 'Link').symlink_to(outside, target_is_directory=True)
            for name in ('', '.', '../escape', outside, '.hidden', 'Existing/.hidden',
                         'Missing/Child', 'Existing', 'Note.md', 'Link/Child'):
                with self.subTest(name=name), self.assertRaises(VaultError):
                    vault.create_folder(name)
            vault.refresh()
            self.assertEqual(vault.folders, ['Existing'])
            self.assertEqual(list(Path(outside).iterdir()), [])


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class ExplorerMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.ExplorerMenuTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'Projects').mkdir()
        (self.root / 'Projects' / 'Existing.md').write_text('# Existing\n')
        self.window = Window(self.app, str(self.root))
        self.addCleanup(self.window.destroy)
        self.drain_events()

    def drain_events(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def row(self, path):
        return next(row for row in self.window.files.get_children() if row.path == path)

    def activate_creation(self, row, item_index, name, expected_prefix):
        menu = self.window.explorer_menu(row)
        self.addCleanup(menu.destroy)
        prefixes = []

        def answer_dialog():
            dialog = next(window for window in Gtk.Window.list_toplevels()
                          if isinstance(window, Gtk.Dialog) and window.get_visible())
            entry = next(child for child in dialog.get_content_area().get_children()
                         if isinstance(child, Gtk.Entry))
            prefixes.append(entry.get_text())
            entry.set_text(name)
            dialog.response(Gtk.ResponseType.OK)
            return False

        GLib.idle_add(answer_dialog)
        menu.get_children()[item_index].activate()
        self.drain_events()
        self.assertEqual(prefixes, [expected_prefix])

    def test_menu_creates_subfolder_then_note_inside_it(self):
        self.activate_creation(self.row('Projects/'), 1, 'Projects/Research', 'Projects/')
        self.assertTrue((self.root / 'Projects' / 'Research').is_dir())
        self.assertEqual(self.window.files.get_selected_row().path, 'Projects/Research/')
        self.activate_creation(self.row('Projects/Research/'), 0,
                               'Projects/Research/Ideas', 'Projects/Research/')
        self.assertTrue((self.root / 'Projects' / 'Research' / 'Ideas.md').is_file())
        self.assertEqual(self.window.current().note.path, 'Projects/Research/Ideas.md')

    def test_note_row_uses_parent_and_empty_space_uses_vault_root(self):
        self.activate_creation(self.row('Projects/Existing.md'), 0, 'Projects/Sibling', 'Projects/')
        self.activate_creation(None, 0, 'Root note', '')
        self.assertTrue((self.root / 'Projects' / 'Sibling.md').is_file())
        self.assertTrue((self.root / 'Root note.md').is_file())

    def test_right_click_opens_menu_without_opening_note(self):
        event = Gdk.EventButton()
        event.type = Gdk.EventType.BUTTON_PRESS
        event.button = 3
        event.window = self.window.files.get_window()
        event.x = 10
        event.y = self.row('Projects/').get_allocation().y + 2
        with patch.object(Gtk.Menu, 'popup_at_pointer') as popup:
            self.assertTrue(self.window.explorer_button_press(self.window.files, event))
            popup.assert_called_once_with(event)
        self.assertEqual(self.window.files.get_selected_row().path, 'Projects/')
        self.assertEqual(self.window.current().note.path, 'Projects/Existing.md')
        self.window.files.emit('row-activated', self.row('Projects/'))
        self.assertEqual(self.window.current().note.path, 'Projects/Existing.md')

    def test_cancel_and_existing_name_do_not_create_files(self):
        with patch.object(Gtk.Dialog, 'run', return_value=Gtk.ResponseType.CANCEL):
            self.window.new_folder(parent='Projects')
        self.assertEqual(self.window.vault.folders, ['Projects'])
        with patch.object(self.window, 'error') as error:
            self.activate_creation(None, 1, 'Projects', '')
            error.assert_called_once()
        self.assertEqual(self.window.vault.folders, ['Projects'])

    def test_tree_indentation_collapse_and_refresh(self):
        self.window.vault.create_folder('Projects/Research')
        self.window.vault.create('Projects/Research/Ideas')
        self.window.populate_files()
        self.assertEqual([row.path for row in self.window.files.get_children()],
                         ['Projects/', 'Projects/Research/', 'Projects/Research/Ideas.md',
                          'Projects/Existing.md'])
        margins = [self.row(path).get_child().get_margin_start() for path in (
            'Projects/', 'Projects/Research/', 'Projects/Research/Ideas.md')]
        self.assertEqual(margins, [0, 18, 36])
        self.window.files.emit('row-activated', self.row('Projects/'))
        self.assertEqual([row.path for row in self.window.files.get_children()], ['Projects/'])
        self.window.refresh()
        self.assertEqual([row.path for row in self.window.files.get_children()], ['Projects/'])
        self.window.open_note('Projects/Research/Ideas.md')
        self.assertEqual(self.window.files.get_selected_row().path, 'Projects/Research/Ideas.md')

    def test_search_and_creation_reveal_notes_in_collapsed_folders(self):
        self.window.toggle_folder('Projects/')
        self.window.search_entry.set_text('Existing')
        self.window.populate_files()
        self.assertEqual([row.path for row in self.window.files.get_children()], ['Projects/Existing.md'])
        self.assertTrue(self.row('Projects/Existing.md').search_result)
        self.window.search_entry.set_text('')
        self.window.populate_files()
        self.activate_creation(self.row('Projects/'), 0, 'Projects/New note', 'Projects/')
        self.assertEqual(self.window.files.get_selected_row().path, 'Projects/New note.md')

    def test_arrow_keys_expand_collapse_and_select_parent(self):
        self.window.files.select_row(self.row('Projects/'))
        event = Gdk.EventKey()
        event.keyval = Gdk.KEY_Left
        event.state = 0
        self.assertTrue(self.window.files_key_press(self.window.files, event))
        self.assertFalse(self.row('Projects/').expanded)
        event.keyval = Gdk.KEY_Right
        self.assertTrue(self.window.files_key_press(self.window.files, event))
        self.assertTrue(self.row('Projects/').expanded)
        self.assertTrue(self.window.files_key_press(self.window.files, event))
        self.assertEqual(self.window.files.get_selected_row().path, 'Projects/Existing.md')
        event.keyval = Gdk.KEY_Left
        self.assertTrue(self.window.files_key_press(self.window.files, event))
        self.assertEqual(self.window.files.get_selected_row().path, 'Projects/')


if __name__ == '__main__':
    unittest.main()
