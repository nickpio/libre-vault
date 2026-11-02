"""Bookmarks and named workspace layouts, stored outside the vault."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.bookmarks import Bookmark, BookmarkStore, remap_bookmarks, with_moved_root
from librevault.workspace import SavedWorkspacesStore, WorkspaceStore

try:
    from librevault.app import Window
    from librevault.graph_ui import GraphView
    from gi.repository import Gdk, Gio, GLib, Gtk
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class BookmarkModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'vault'
        self.other = self.base / 'other'
        self.root.mkdir()
        self.other.mkdir()
        self.preferences = self.base / 'config' / 'settings.json'
        self.store = BookmarkStore(self.preferences)

    def test_store_sits_beside_workspaces_and_roundtrips_every_type(self):
        self.assertEqual(self.store.path, WorkspaceStore(self.preferences).path.with_name('bookmarks.json'))
        self.assertFalse(self.store.path.exists())
        note = Bookmark(type='note', path='Projects/A.md', fragment='Heading', title='Pin')
        search = Bookmark(type='search', query='tag:#work')
        folder = Bookmark(type='folder', path='Projects/Plans')
        graph = Bookmark(type='graph', title='Map', state={'local': True, 'root': 'Projects/A.md', 'depth': 2})
        self.store.add(self.root, note)
        self.store.add(self.root, search)
        self.store.add(self.root, folder)
        saved = self.store.add(self.root, graph)
        self.assertEqual(saved, [note, search, folder, graph])
        self.assertEqual(self.store.load(self.root), saved)
        self.assertEqual(self.store.load(self.other), [])
        raw = json.loads(self.store.path.read_text(encoding='utf-8'))
        self.assertEqual(set(raw), {str(self.root.resolve())})
        self.assertTrue(self.store.path.read_text(encoding='utf-8').endswith('\n'))
        self.assertEqual(list(self.store.path.parent.glob('.workspace-*')), [])
        self.assertFalse(any(self.root.rglob('bookmarks.json')))
        self.store.add(self.other, Bookmark(type='note', path='Other.md'))
        self.assertEqual([item.path for item in self.store.load(self.root) if item.type == 'note'], ['Projects/A.md'])

    def test_malformed_entries_are_ignored_and_corruption_reads_empty(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text(json.dumps({str(self.root.resolve()): [
            {'type': 'note'},
            {'type': 'missing', 'path': 'A.md'},
            {'type': 'note', 'path': '../Secret.md'},
            {'type': 'note', 'path': '/etc/passwd'},
            {'type': 'search', 'query': ''},
            {'type': 'graph', 'state': 'nope'},
            {'type': 'note', 'path': 'A.md', 'fragment': 'H', 'title': 'T'},
            'bad',
        ]}), encoding='utf-8')
        self.assertEqual(self.store.load(self.root), [Bookmark(type='note', path='A.md', fragment='H', title='T')])
        self.store.path.write_text('{', encoding='utf-8')
        self.assertEqual(self.store.load(self.root), [])

    def test_add_remove_reorder_and_rename(self):
        self.store.add(self.root, Bookmark(type='note', path='A.md'))
        self.store.add(self.root, Bookmark(type='note', path='B.md'))
        self.store.add(self.root, Bookmark(type='search', query='alpha'))
        self.assertEqual([item.path for item in self.store.reorder(self.root, 0, 2)], ['B.md', '', 'A.md'])
        self.assertEqual([item.type for item in self.store.load(self.root)], ['note', 'search', 'note'])
        renamed = self.store.rename(self.root, 2, '  Alpha note  ')
        self.assertEqual(renamed[2].title, 'Alpha note')
        self.assertEqual(renamed[2].display_title(), 'Alpha note')
        self.assertEqual(Bookmark(type='note', path='A.md').display_title(), 'A')
        self.assertEqual(Bookmark(type='note', path='A.md', fragment='H').display_title(), 'A · H')
        self.assertEqual(Bookmark(type='folder', path='Plans').display_title(), 'Plans')
        self.assertEqual(Bookmark(type='search', query='tag:#a').display_title(), 'tag:#a')
        self.assertEqual(Bookmark(type='graph', state={'local': True}).display_title(), 'Local graph')
        self.store.remove(self.root, 0)
        self.store.remove(self.root, 0)
        self.store.remove(self.root, 0)
        self.assertEqual(self.store.load(self.root), [])
        self.assertNotIn(str(self.root.resolve()), json.loads(self.store.path.read_text(encoding='utf-8')))
        with self.assertRaises(IndexError):
            self.store.remove(self.root, 0)
        with self.assertRaises(ValueError):
            self.store.add(self.root, Bookmark(type='note', path='../no.md'))

    def test_remap_moves_notes_and_folders_and_drops_deleted_paths(self):
        bookmarks = [
            Bookmark(type='note', path='Projects/A.md', fragment='Heading', title='Keep'),
            Bookmark(type='folder', path='Projects'),
            Bookmark(type='folder', path='Projects/Sub'),
            Bookmark(type='note', path='Other.md'),
            Bookmark(type='search', query='tag:#work'),
            Bookmark(type='graph', state={'local': True, 'root': 'Projects/A.md', 'depth': 2}),
        ]
        mapping = {
            'Projects': 'Archive/Projects',
            'Projects/A.md': 'Archive/Projects/A.md',
            'Projects/Sub/B.md': 'Archive/Projects/Sub/B.md',
        }
        moved = remap_bookmarks(bookmarks, mapping)
        self.assertEqual(moved[0].path, 'Archive/Projects/A.md')
        self.assertEqual(moved[0].fragment, 'Heading')
        self.assertEqual(moved[0].title, 'Keep')
        self.assertEqual(moved[1].path, 'Archive/Projects')
        self.assertEqual(moved[2].path, 'Archive/Projects/Sub')
        self.assertEqual(moved[3].path, 'Other.md')
        self.assertEqual(moved[4].query, 'tag:#work')
        self.assertEqual(moved[5].state['root'], 'Archive/Projects/A.md')
        # Renaming one file must not drag its old folder along with it.
        stayed = remap_bookmarks(
            [Bookmark(type='folder', path='Projects'), Bookmark(type='note', path='Projects/A.md')],
            {'Projects/A.md': 'Inbox/A.md'})
        self.assertEqual([item.path for item in stayed], ['Projects', 'Inbox/A.md'])
        deleted = remap_bookmarks(moved, {}, ['Archive/Projects', 'Other.md'])
        self.assertEqual([item.type for item in deleted], ['search', 'graph'])
        self.assertIsNone(deleted[1].state['root'])
        self.store.save(self.root, bookmarks)
        self.store.save(self.other, [Bookmark(type='note', path='Other.md')])
        updated = self.store.remap(self.root, mapping, ['Other.md'])
        self.assertEqual(updated[3].type, 'search')
        self.assertNotIn('Other.md', [item.path for item in updated])
        self.assertEqual(self.store.load(self.other)[0].path, 'Other.md')
        self.assertEqual(self.store.remap(self.root, {'Archive/Projects/A.md': 'Archive/Projects/A.md'}), updated)

    def test_moved_root_records_a_folder_the_file_map_left_out(self):
        mapping = with_moved_root({'Projects/A.md': 'Archive/Projects/A.md'}, 'Projects', 'Archive/Projects')
        self.assertEqual(mapping['Projects'], 'Archive/Projects')
        empty = with_moved_root({}, 'Empty', 'Archive/Empty')
        self.assertEqual(empty, {'Empty': 'Archive/Empty'})
        renamed = with_moved_root({'A.md': 'B.md'}, 'A.md', 'B.md')
        self.assertEqual(renamed, {'A.md': 'B.md'})


class NamedWorkspaceModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'vault'
        self.other = self.base / 'other'
        self.root.mkdir()
        self.other.mkdir()
        self.preferences = self.base / 'config' / 'settings.json'
        self.store = SavedWorkspacesStore(self.preferences)
        self.layout = {'tabs': [{'path': 'A.md', 'cursor': 3, 'scroll': 0}], 'active': 'A.md',
                       'collapsed_folders': ['Plans'], 'split': None, 'search': 'tag:#work'}

    def test_named_layouts_are_per_vault_and_do_not_replace_the_live_workspace(self):
        self.assertEqual(self.store.path, WorkspaceStore(self.preferences).path.with_name('saved-workspaces.json'))
        self.assertEqual(self.store.names(self.root), [])
        self.assertIsNone(self.store.get(self.root, 'Writing'))
        self.store.save(self.root, 'Writing', self.layout)
        self.store.save(self.root, 'Research', {'tabs': [], 'search': ''})
        self.store.save(self.other, 'Writing', {'tabs': [{'path': 'Other.md'}], 'search': 'elsewhere'})
        self.assertEqual(self.store.names(self.root), ['Writing', 'Research'])
        loaded = self.store.get(self.root, 'Writing')
        self.assertEqual(loaded['search'], 'tag:#work')
        loaded['search'] = 'changed'
        self.assertEqual(self.store.get(self.root, 'Writing')['search'], 'tag:#work')
        self.store.save(self.root, '  Writing  ', {'tabs': [], 'active': None, 'search': 'replaced'})
        self.assertEqual(self.store.get(self.root, 'Writing')['search'], 'replaced')
        self.assertEqual(self.store.names(self.root), ['Writing', 'Research'])
        live = WorkspaceStore(self.preferences)
        live.save(self.root, {'tabs': [{'path': 'Live.md'}]})
        self.assertEqual(live.load(self.root)['tabs'][0]['path'], 'Live.md')
        self.assertTrue(self.store.delete(self.root, 'Research'))
        self.assertFalse(self.store.delete(self.root, 'Research'))
        self.assertEqual(self.store.names(self.root), ['Writing'])
        self.assertEqual(self.store.names(self.other), ['Writing'])
        self.assertEqual(list(self.store.path.parent.glob('.workspace-*')), [])

    def test_invalid_names_and_corruption_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save(self.root, '   ', self.layout)
        with self.assertRaises(ValueError):
            self.store.save(self.root, 'bad\x00name', self.layout)
        with self.assertRaises(ValueError):
            self.store.save(self.root, 'x' * 81, self.layout)
        with self.assertRaises(ValueError):
            self.store.save(self.root, 'Writing', ['not', 'a', 'layout'])
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text('{', encoding='utf-8')
        self.assertEqual(self.store.names(self.root), [])
        self.assertIsNone(self.store.get(self.root, 'Writing'))


@unittest.skipUnless(GTK_AVAILABLE, 'GTK and a display are required')
class BookmarkWorkspaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.BookmarkTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'vault'
        self.root.mkdir()
        (self.root / 'Folder').mkdir()
        (self.root / 'A.md').write_text('# Alpha\n\nBody\n\n## Details\n\nMore\n')
        (self.root / 'Folder' / 'B.md').write_text('---\naliases: [Launch]\n---\n# Beta\n')
        self.preferences = self.base / 'config' / 'settings.json'
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state')})
        env.start()
        self.addCleanup(env.stop)
        self.windows = []
        self.addCleanup(self.destroy_windows)
        self.window = self.make_window()

    def make_window(self):
        window = Window(self.app, preferences_path=self.preferences)
        self.windows.append(window)
        return window

    def destroy_windows(self):
        for window in self.windows:
            if not window.in_destruction():
                window.destroy()
        self.settle(40)

    def settle(self, milliseconds=80):
        loop = GLib.MainLoop()
        GLib.timeout_add(milliseconds, lambda: (loop.quit(), False)[1])
        loop.run()

    def open_palette(self):
        self.assertTrue(self.window.key_press(
            self.window, self.event(Gdk.KEY_p, Gdk.ModifierType.MOD1_MASK)))
        self.settle(30)
        return self.window.palette

    def event(self, key, modifiers=0):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = key
        event.state = modifiers
        return event

    def titles(self, palette):
        return [row.title for row in palette.results.get_children()]

    def test_star_sidebar_and_commands_open_and_remove_a_note(self):
        self.window.load_vault(self.root)
        pane = self.window.open_note('A.md')
        self.assertEqual(pane.bookmark_button.get_tooltip_text(), 'Bookmark current note')
        self.assertTrue(self.window.bookmarks_empty.get_visible())
        pane.bookmark_button.clicked()
        self.assertTrue(self.window.note_is_bookmarked('A.md'))
        self.assertEqual(pane.bookmark_button.get_tooltip_text(), 'Remove bookmark')
        self.assertFalse(str(self.window.bookmark_store.path).startswith(str(self.root)))
        self.assertEqual(self.window.bookmark_list.get_children()[0].title, 'A')
        self.window.open_note('Folder/B.md')
        self.window.bookmark_list.emit('row-activated', self.window.bookmark_list.get_row_at_index(0))
        self.assertEqual(self.window.current().note.path, 'A.md')
        palette = self.open_palette()
        self.assertIn('Remove bookmark', self.titles(palette))
        self.assertNotIn('Bookmark current note', self.titles(palette))
        palette.destroy()
        self.window.remove_current_bookmark()
        self.assertEqual(self.window.bookmark_items, [])
        self.assertFalse(self.window.note_is_bookmarked('A.md'))
        self.assertIn('Bookmark current note', self.titles(self.open_palette()))

    def test_heading_search_folder_and_graph_bookmarks_open(self):
        self.window.load_vault(self.root)
        self.window.add_note_bookmark('A.md', 'Details')
        self.window.open_note('Folder/B.md')
        self.window.open_bookmark(self.window.bookmark_items[0])
        pane = self.window.current()
        self.assertEqual(pane.note.path, 'A.md')
        offset = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        line_end = pane.text().find('\n', offset)
        self.assertIn('Details', pane.text()[offset:line_end])
        self.window.search_entry.set_text('tag:#missing')
        self.window.bookmark_current_search()
        self.window.search_entry.set_text('')
        self.window.open_bookmark(next(item for item in self.window.bookmark_items if item.type == 'search'))
        self.assertEqual(self.window.search_entry.get_text(), 'tag:#missing')
        self.window.search_entry.set_text('')
        folder = next(row for row in self.window.files.get_children() if row.path == 'Folder/')
        self.window.files.select_row(folder)
        self.window.bookmark_selected_folder()
        self.window.collapsed_folders.add('Folder')
        self.window.populate_files()
        self.window.open_bookmark(next(item for item in self.window.bookmark_items if item.type == 'folder'))
        self.assertNotIn('Folder', self.window.collapsed_folders)
        self.assertEqual(self.window.files.get_selected_row().path, 'Folder/')
        self.window.search_entry.set_text('')
        graph = self.window.show_graph_tab()
        graph.local.set_active(False)
        self.window.bookmark_current_graph()
        self.window.close_note(graph)
        saved = next(item for item in self.window.bookmark_items if item.type == 'graph')
        self.assertNotIn('id', saved.state)
        self.window.open_bookmark(saved)
        page = self.window.tabs.get_nth_page(self.window.tabs.get_current_page())
        self.assertIsInstance(page, GraphView)
        self.assertFalse(page.local.get_active())
        self.window.close_note(page)
        self.settle(40)

    def test_move_and_trash_remap_bookmarks_outside_the_vault(self):
        self.window.load_vault(self.root)
        self.window.add_note_bookmark('A.md')
        self.window.add_note_bookmark('Folder/B.md', 'Beta')
        self.window.bookmark_items.append(Bookmark(type='folder', path='Folder'))
        self.window.bookmark_items.append(Bookmark(type='search', query='keep'))
        self.window.bookmark_items.append(Bookmark(type='graph', state={'root': 'A.md', 'local': True}))
        self.window.persist_bookmarks()
        self.window.move_path('A.md', 'Renamed.md')
        self.assertEqual(self.window.bookmark_items[0].path, 'Renamed.md')
        self.assertTrue(self.window.note_is_bookmarked('Renamed.md'))
        self.window.vault.create_folder('Archive')
        self.window.move_path('Folder', 'Archive/Folder')
        paths = [item.path for item in self.window.bookmark_items if item.type in ('note', 'folder')]
        self.assertIn('Archive/Folder', paths)
        self.assertIn('Archive/Folder/B.md', paths)
        self.assertEqual(next(item for item in self.window.bookmark_items if item.type == 'graph').state['root'], 'Renamed.md')
        self.window.trash_path('Renamed.md')
        self.assertFalse(any(item.type == 'note' and item.path == 'Renamed.md' for item in self.window.bookmark_items))
        self.assertTrue(any(item.type == 'search' for item in self.window.bookmark_items))
        self.window.trash_path('Archive/Folder')
        kinds = [item.type for item in self.window.bookmark_store.load(self.root)]
        self.assertNotIn('folder', kinds)
        self.assertNotIn('note', kinds)
        self.assertIn('search', kinds)
        self.assertFalse(any(self.root.rglob('bookmarks.json')))

    def test_manager_renames_reorders_and_deletes(self):
        self.window.load_vault(self.root)
        self.window.add_note_bookmark('A.md')
        self.window.add_note_bookmark('Folder/B.md')
        self.window.show_bookmarks()
        manager = self.window.bookmark_manager
        self.assertEqual([row.title for row in manager.list.get_children()], ['A', 'B'])
        with patch('librevault.bookmarks.ask_text', return_value='Pinned'):
            manager.rename_button.clicked()
        self.assertEqual(self.window.bookmark_items[0].title, 'Pinned')
        self.assertEqual(self.window.bookmark_list.get_row_at_index(0).title, 'Pinned')
        manager.down_button.clicked()
        self.assertEqual([item.path for item in self.window.bookmark_items], ['Folder/B.md', 'A.md'])
        self.assertEqual(manager.list.get_selected_row().index, 1)
        manager.remove_button.clicked()
        self.assertEqual([item.path for item in self.window.bookmark_items], ['Folder/B.md'])
        manager.list.emit('row-activated', manager.list.get_row_at_index(0))
        self.assertIsNone(self.window.bookmark_manager)
        self.assertEqual(self.window.current().note.path, 'Folder/B.md')

    def test_named_workspace_saves_loads_and_deletes_layout(self):
        self.window.load_vault(self.root)
        alpha = self.window.open_note('A.md')
        beta = self.window.open_note('Folder/B.md')
        alpha.buffer.place_cursor(alpha.buffer.get_iter_at_offset(4))
        self.window.open_note('A.md')
        self.window.collapsed_folders.add('Folder')
        self.window.search_entry.set_text('Alpha')
        self.window.open_split(beta)
        self.assertTrue(self.window.save_named_workspace('Writing'))
        self.window.search_entry.set_text('Beta')
        self.window.close_split()
        self.window.collapsed_folders.clear()
        self.window.close_note(self.window.panes['Folder/B.md'])
        self.assertTrue(self.window.load_named_workspace('Writing'))
        self.settle(240)
        self.assertEqual(list(self.window.panes), ['A.md', 'Folder/B.md'])
        self.assertEqual(self.window.current().note.path, 'A.md')
        restored = self.window.panes['A.md']
        self.assertEqual(restored.buffer.get_iter_at_mark(restored.buffer.get_insert()).get_offset(), 4)
        self.assertIn('Folder', self.window.collapsed_folders)
        self.assertEqual(self.window.split_pane.note.path, 'Folder/B.md')
        self.assertEqual(self.window.search_entry.get_text(), 'Alpha')
        self.window.manage_workspaces()
        manager = self.window.workspace_manager
        self.assertEqual(manager.selected_name(), 'Writing')
        with patch('librevault.bookmarks.confirm', return_value=False):
            manager.delete_button.clicked()
        self.assertEqual(self.window.saved_workspace_names(), ['Writing'])
        with patch('librevault.bookmarks.confirm', return_value=True):
            manager.delete_button.clicked()
        self.assertEqual(self.window.saved_workspace_names(), [])
        self.assertIsNone(self.window.saved_workspaces.get(self.root, 'Writing'))
        manager.destroy()
        self.assertIsNone(self.window.workspace_manager)

    def test_save_command_prompts_and_palette_lists_layout_commands(self):
        self.window.load_vault(self.root)
        self.window.open_note('A.md')
        with patch('librevault.app.ask_text', return_value='Research') as prompt:
            self.window.save_workspace_layout()
        prompt.assert_called_once()
        self.assertEqual(self.window.saved_workspaces.get(self.root, 'Research')['active'], 'A.md')
        with patch('librevault.app.ask_text', return_value='Research'), \
             patch('librevault.app.confirm', return_value=False) as confirm:
            self.window.save_workspace_layout()
        confirm.assert_called_once()
        self.assertEqual(self.window.saved_workspaces.get(self.root, 'Research')['active'], 'A.md')
        palette = self.open_palette()
        titles = self.titles(palette)
        for title in ('Bookmark current note', 'Bookmark current search', 'Bookmark selected folder',
                      'Bookmark current graph', 'Open bookmarks', 'Save workspace layout…',
                      'Load workspace layout…', 'Manage workspaces…'):
            self.assertIn(title, titles)
        palette.search.set_text('workspace layout')
        visible = [row.title for row in palette.results.get_children() if row.get_visible()]
        self.assertEqual(visible, ['Save workspace layout…', 'Load workspace layout…'])
        self.window.close_vault()
        closed = self.titles(self.open_palette())
        self.assertFalse(any('bookmark' in title.casefold() or 'workspace layout' in title.casefold() for title in closed))

    def test_loading_a_layout_can_be_cancelled_while_a_note_is_dirty(self):
        self.window.load_vault(self.root)
        self.window.open_note('A.md')
        self.window.open_note('Folder/B.md')
        self.window.save_named_workspace('Writing')
        pane = self.window.current()
        pane.buffer.insert_at_cursor('dirty')
        self.assertTrue(pane.dirty)
        with patch('librevault.app.Gtk.MessageDialog') as constructor:
            constructor.return_value.run.return_value = Gtk.ResponseType.CANCEL
            self.assertFalse(self.window.load_named_workspace('Writing'))
        self.assertIn('dirty', self.window.current().text())
        self.assertEqual(self.window.current().note.path, 'Folder/B.md')
