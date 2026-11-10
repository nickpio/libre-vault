"""Filesystem policies, shared link resolution, and complete workspace flows."""
from tests.gui_wait import wait_for_work
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import time

from librevault.attachments import local_path, store_attachment
from librevault.graph import build_graph
from librevault.vault import Vault, VaultError
from librevault.vault_options import VaultOptions, VaultOptionsStore


class VaultCustomizationTests(unittest.TestCase):
    def test_case_insensitive_paths_basenames_aliases_and_graphs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Folder').mkdir()
            (root / 'Folder/Plan.MD').write_text('---\naliases: [RoadMap]\n---\n')
            (root / 'Home.md').write_text('[[folder/plan]] [[ROADMAP]]')
            vault = Vault(root)
            self.addCleanup(vault.close)
            for target in ('folder/plan', 'PLAN', 'roadmap', './folder/PLAN.md'):
                self.assertEqual(vault.resolve('Home.md', target), ['Folder/Plan.MD'])
            rows = vault.index.connection.execute('SELECT path,aliases,links,text FROM notes')
            paths, edges = build_graph(rows)
            self.assertEqual([(paths[a], paths[b]) for a,b in edges], [('Folder/Plan.MD', 'Home.md')])
            (root / 'Other').mkdir()
            (root / 'Other/PLAN.md').write_text('Other')
            vault.refresh()
            self.assertEqual(vault.resolve('Home.md', 'plan'), ['Folder/Plan.MD', 'Other/PLAN.md'])

    def test_hidden_files_internal_links_cycles_and_symlink_save(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            (root / '.notes').mkdir()
            (root / '.notes/Hidden.md').write_text('Hidden')
            (root / 'Real').mkdir()
            (root / 'Real/Note.md').write_text('Original')
            (root / 'Alias.md').symlink_to(root / 'Real/Note.md')
            (root / 'Linked').symlink_to(root / 'Real', target_is_directory=True)
            (root / 'Real/loop').symlink_to(root, target_is_directory=True)
            (root / 'Escape.md').symlink_to(Path(outside) / 'Outside.md')
            (Path(outside) / 'Outside.md').write_text('Outside')
            for reserved in ('.git', '.obsidian', '.trash', '.librevault'):
                (root / reserved).mkdir()
                (root / reserved / 'Never.md').write_text('Private')
            vault = Vault(root)
            self.addCleanup(vault.close)
            self.assertEqual(list(vault.notes), ['Real/Note.md'])
            vault.options = VaultOptions(show_hidden=True, follow_symlinks=True)
            vault.refresh()
            self.assertEqual(list(vault.notes), ['.notes/Hidden.md', 'Alias.md', 'Linked/Note.md', 'Real/Note.md'])
            vault.save(vault.read('Alias.md'), 'Changed')
            self.assertTrue((root / 'Alias.md').is_symlink())
            self.assertEqual((root / 'Real/Note.md').read_text(), 'Changed')
            vault.update_paths(['Real/loop'])
            self.assertFalse(any('/loop/' in path for path in vault.notes))
            vault.update_paths(['.notes/Hidden.md'])
            self.assertIn('.notes/Hidden.md', vault.notes)
            with self.assertRaises(VaultError):
                vault.read('Escape.md')

    def test_per_vault_settings_and_attachment_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = VaultOptionsStore(root / 'settings.json')
            options = VaultOptions(attachment_folder='Media/Images')
            store.save(root, options)
            self.assertEqual(store.load(root), options)
            self.assertEqual(store.load(root / 'other'), VaultOptions())
            vault = Vault(root, options=store.load(root))
            self.addCleanup(vault.close)
            path = store_attachment(vault, 'picture.png', b'picture')
            self.assertEqual(path, 'Media/Images/picture.png')
            self.assertEqual(local_path(vault, '', 'media/images/PICTURE.PNG'), root / path)
            self.assertEqual(store_attachment(vault, 'picture.png', b'picture'), path)
            vault.options = replace(options, attachment_folder='')
            self.assertEqual(store_attachment(vault, 'root.png', b'root'), 'root.png')


try:
    from librevault.app import Gtk, GLib, Gdk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display unavailable')
class WorkspaceCustomizationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.CustomizationTests', flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def drain(self):
        for _ in range(1000):
            if not GLib.MainContext.default().pending():
                return
            GLib.MainContext.default().iteration(False)
        self.fail('GTK did not settle')

    def test_recursive_splits_shared_windows_tab_modes_docks_and_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('[[B]]\nAlpha')
            (root / 'B.md').write_text('Beta')
            preferences = root / '.settings.json'
            window = Window(self.app, root, preferences_path=preferences)
            wait_for_work(window)
            self.addCleanup(window.destroy)
            a = window.open_note('A.md')
            window.split_right()
            first = window.workspace_layout.groups[0]
            first.current().editor.grab_focus()
            window.split_down()
            self.assertEqual(len(window.workspace_layout.groups), 2)
            self.assertIs(first.current().editor.get_buffer(), a.buffer)
            b = window.open_note('B.md')
            window.split_right()
            self.assertEqual(len(window.workspace_layout.groups), 3)
            first.add(b)
            self.assertEqual(first.tabs.get_n_pages(), 2)
            window.tabs.set_current_page(window.tabs.page_num(a))
            a.editor.grab_focus()
            popout = window.pop_out_note()
            self.assertIs(popout.mirror.editor.get_buffer(), a.buffer)
            popout.mirror.editor.get_buffer().insert_at_cursor(' shared')
            self.assertIn('shared', a.text())
            a.save()
            window.set_tab_mode('stacked')
            self.assertEqual(window.tabs.get_tab_pos(), Gtk.PositionType.LEFT)
            window.set_tab_mode('sliding')
            self.assertEqual(len(window.sliding_views), 2)
            self.assertIs(window.sliding_views[0].pane.buffer, a.buffer)
            window.restore_docks([['files'], ['details', 'tags']])
            self.assertIs(window.tag_pane.get_parent(), window.right_dock)
            state = window.capture_workspace_state()
            self.assertEqual(state['tab_mode'], 'sliding')
            self.assertEqual(len(state['note_windows']), 1)
            json.dumps(state)
            window.persist_workspace()
            window.destroy()
            restored = Window(self.app, root, preferences_path=preferences)
            wait_for_work(restored)
            self.addCleanup(restored.destroy)
            self.assertEqual(len(restored.workspace_layout.groups), 3)
            self.assertEqual(restored.tab_mode, 'sliding')
            self.assertEqual(len(restored.note_windows), 1)
            self.assertIs(restored.tag_pane.get_parent(), restored.right_dock)
            restored.workspace_layout.remove_group(restored.workspace_layout.groups[1])
            self.assertEqual(len(restored.workspace_layout.groups), 2)
            graph = restored.show_graph_tab()
            restored.set_tab_mode('tabs')
            restored.open_graph_note(graph, 'A.md')
            self.assertEqual(len(restored.workspace_layout.groups), 2)
            self.assertIs(restored.tabs.get_nth_page(restored.tabs.get_current_page()), graph)
            restored.close_split()
            self.assertFalse(restored.workspace_layout.groups)
            self.assertIsNone(restored.split_view)
            self.drain()

    def test_graph_pane_state_restores_with_named_application(self):
        from librevault.graph_ui import GraphView
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('[[B]]')
            (root / 'B.md').write_text('Body')
            preferences = root / '.settings.json'
            window = Window(self.app, root, preferences_path=preferences)
            wait_for_work(window)
            self.addCleanup(window.destroy)
            graph = window.show_graph_tab()
            window.open_split(graph)
            window.split_view.search.set_text('file:A')
            window.split_view.depth.set_value(2)
            window.persist_workspace()
            window.destroy()
            restored = Window(self.app, root, preferences_path=preferences)
            wait_for_work(restored)
            self.addCleanup(restored.destroy)
            self.assertIsInstance(restored.split_view, GraphView)
            self.assertEqual(restored.split_view.search.get_text(), 'file:A')
            self.assertEqual(restored.split_view.depth.get_value_as_int(), 2)
            restored.close_split()
            self.drain()

    def test_rebound_application_editor_shortcuts_settings_and_hover(self):
        from types import SimpleNamespace
        from librevault.shortcuts import APP_SHORTCUTS, EDITOR_SHORTCUTS, shortcut_id
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('[[B]]')
            (root / 'B.md').write_text('# Preview heading\nPreview body')
            window = Window(self.app, root, preferences_path=root / '.settings.json')
            wait_for_work(window)
            self.addCleanup(window.destroy)
            pane = window.open_note('A.md')
            self.assertIn(window, self.app.get_windows())
            save_id = shortcut_id('app', next(s for s in APP_SHORTCUTS if s.handler == 'save_current'))
            window.set_hotkey(save_id, '<Control><Alt>s')
            with self.assertRaises(ValueError):
                window.set_hotkey(save_id, '<Control>b')
            event = SimpleNamespace(keyval=Gdk.KEY_s, state=Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK)
            pane.buffer.insert_at_cursor(' changed')
            self.assertTrue(window.key_press(window, event))
            self.assertFalse(pane.dirty)
            self.assertEqual((root / 'A.md').read_text(), pane.text())
            self.assertFalse(window.key_press(window, SimpleNamespace(keyval=Gdk.KEY_s, state=Gdk.ModifierType.CONTROL_MASK)))
            bold_id = shortcut_id('editor', next(s for s in EDITOR_SHORTCUTS if s.handler == 'bold'))
            requested = '<Control><Alt>b'
            key, modifiers = Gtk.accelerator_parse(requested)
            window.set_hotkey(bold_id, requested)
            # GTK names the primary modifier Primary. That is Control on Linux
            # and Command on macOS, so a Control binding stays <Control> there.
            self.assertEqual(window.hotkey_store.load()[bold_id], Gtk.accelerator_name(key, modifiers))
            self.assertTrue(pane.editor_key_press(pane.editor, SimpleNamespace(keyval=Gdk.KEY_b,
                state=Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK)))
            window.show_settings()
            self.assertTrue(hasattr(window.settings_dialog, 'vault_attachment'))
            window.settings_dialog.destroy()
            preview = pane.navigation.link_preview
            rectangle = Gdk.Rectangle()
            preview.consider(pane.editor, 'A.md', 'B', False, rectangle, window)
            self.assertIsNotNone(preview.timer)
            deadline = time.monotonic() + 2
            while preview.popover is None and time.monotonic() < deadline:
                self.drain()
                time.sleep(.01)
            self.assertIsNotNone(preview.popover)
            self.assertEqual(preview.title_label.get_text(), 'B')
            self.assertIn('Preview body', preview.body_label.get_text())
            window.split_right()
            mirror = window.split_view
            pane.navigation.hover = (mirror, 'A.md', 'B', rectangle)
            pane.navigation.key_press(SimpleNamespace(keyval=Gdk.KEY_Control_L))
            self.assertIs(preview.popover.get_relative_to(), mirror)
            preview.hide()
            window.set_vault_options(VaultOptions(attachment_folder='Media'))
            self.assertEqual(window.vault_options_store.load(root).attachment_folder, 'Media')
