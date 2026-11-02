"""Incremental indexing and real Gio notifications, including watch teardown."""
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
import weakref

from librevault.vault import Conflict, Vault

try:
    from librevault.app import GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class IncrementalIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'A.md').write_text('[[B]]')
        (self.root / 'B.md').write_text('---\ntags: old\n---\nBody')
        self.vault = Vault(self.root)

    def test_only_changed_file_is_read_and_caches_are_invalidated(self):
        self.vault.backlinks('B.md')
        self.vault.metadata('B.md')
        original = self.vault.notes['A.md']
        (self.root / 'B.md').write_text('New body')
        with patch.object(self.vault, 'read', wraps=self.vault.read) as read:
            self.vault.update_paths(['B.md', 'B.md'])
            self.assertEqual([call.args[0] for call in read.call_args_list], ['B.md'])
        self.assertIs(self.vault.notes['A.md'], original)
        self.assertEqual(self.vault.metadata('B.md').tags, ())
        self.assertEqual(self.vault.index.links('B.md'), [])
        self.assertEqual(self.vault.backlinks('B.md'), ['A.md'])

    def test_directory_move_delete_and_exclusions(self):
        folder = self.root / 'Folder'
        folder.mkdir()
        (folder / 'Nested').mkdir()
        (folder / 'Nested' / 'C.md').write_text('C')
        (folder / '.hidden.md').write_text('hidden')
        (folder / 'alias.md').symlink_to(self.root / 'A.md')
        with patch.object(self.vault, 'read', wraps=self.vault.read) as read:
            self.vault.update_paths(['Folder', 'Folder/Nested/C.md'])
            self.assertEqual([call.args[0] for call in read.call_args_list], ['Folder/Nested/C.md'])
        self.assertEqual(self.vault.folders, ['Folder', 'Folder/Nested'])
        folder.rename(self.root / 'Moved')
        self.vault.update_paths(['Folder', 'Moved'])
        self.assertNotIn('Folder/Nested/C.md', self.vault.notes)
        self.assertIn('Moved/Nested/C.md', self.vault.notes)
        self.assertNotIn('Moved/alias.md', self.vault.notes)
        (self.root / 'Moved' / 'Nested' / 'C.md').unlink()
        self.vault.update_paths(['Moved/Nested/C.md'])
        self.assertNotIn('Moved/Nested/C.md', self.vault.notes)

    def test_unreadable_recovery_and_symlink_replacement(self):
        self.vault.metadata('B.md')
        (self.root / 'B.md').write_bytes(b'\xff')
        self.vault.update_paths(['B.md'])
        self.assertIsNone(self.vault.notes['B.md'])
        self.assertEqual(self.vault.metadata('B.md').tags, ())
        (self.root / 'B.md').write_text('Recovered')
        self.vault.update_paths(['B.md'])
        self.assertEqual(self.vault.indexed_note('B.md').text, 'Recovered')
        (self.root / 'B.md').unlink()
        (self.root / 'B.md').symlink_to(self.root / 'A.md')
        self.vault.update_paths(['B.md'])
        self.assertNotIn('B.md', self.vault.notes)


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.MonitorTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'A.md').write_text('A')
        (self.root / 'B.md').write_text('B')
        self.window = Window(self.app, self.root,
                             preferences_path=Path(self.directory.name) / 'settings.json')
        self.addCleanup(self.window.destroy)
        self.wait_for(lambda: self.window.vault_monitor.source is None)

    def wait_for(self, condition, timeout=3):
        context = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if condition():
                return
            time.sleep(.01)
        self.fail('Filesystem event did not propagate')

    def test_external_atomic_edit_reads_only_affected_file_and_protects_dirty_note(self):
        pane = self.window.open_note('A.md')
        with patch.object(self.window.vault, 'refresh', side_effect=AssertionError('Unexpected full scan')):
            with patch.object(self.window.vault, 'read', wraps=self.window.vault.read) as read:
                temporary = self.root / '.replacement'
                temporary.write_text('External')
                temporary.replace(self.root / 'A.md')
                self.wait_for(lambda: pane.text() == 'External')
                self.assertTrue(read.called)
                self.assertEqual({call.args[0] for call in read.call_args_list}, {'A.md'})
            pane.buffer.insert_at_cursor(' draft')
            (self.root / 'A.md').write_text('Competing')
            self.wait_for(lambda: self.window.vault.indexed_note('A.md').text == 'Competing')
            self.assertIn('draft', pane.text())
            with self.assertRaises(Conflict):
                pane.save()

    def test_nested_directory_creation_rename_and_removal_update_watches(self):
        nested = self.root / 'Folder' / 'Nested'
        nested.mkdir(parents=True)
        (nested / 'C.md').write_text('C')
        self.wait_for(lambda: 'Folder/Nested/C.md' in self.window.vault.notes)
        (self.root / 'Folder').rename(self.root / 'Moved')
        self.wait_for(lambda: 'Moved/Nested/C.md' in self.window.vault.notes
                      and 'Folder/Nested/C.md' not in self.window.vault.notes)
        (self.root / 'Moved' / 'Nested' / 'C.md').write_text('Updated')
        self.wait_for(lambda: self.window.vault.indexed_note('Moved/Nested/C.md').text == 'Updated')
        (self.root / 'Moved' / 'Nested' / 'C.md').unlink()
        (self.root / 'Moved' / 'Nested').rmdir()
        self.wait_for(lambda: 'Moved/Nested' not in self.window.vault.folders)
        self.assertNotIn('Moved/Nested', self.window.vault_monitor.monitors)

    def test_asset_and_unsaved_embed_changes_refresh_reading(self):
        pane = self.window.open_note('A.md')
        pane.buffer.set_text('![[B]]\n![asset](asset.png)')
        pane.save()
        pane.set_mode('reading')
        original = pane.reading.body.get_children()
        (self.root / 'asset.png').write_bytes(b'invalid image but changed dependency')
        self.wait_for(lambda: original != pane.reading.body.get_children())
        other = self.window.open_note('B.md')
        original = pane.reading.body.get_children()
        other.buffer.insert_at_cursor(' unsaved')
        self.wait_for(lambda: original != pane.reading.body.get_children())
        self.assertTrue(other.dirty)
        # Formatting synchronously before the idle pass must not suppress the
        # dependent view refresh.
        original = pane.reading.body.get_children()
        other.buffer.insert_at_cursor(' more')
        other.render()
        self.wait_for(lambda: original != pane.reading.body.get_children())
        original = pane.reading.body.get_children()
        with patch.object(self.window, 'confirm_panes', return_value=True):
            self.window.close_note(other)
        self.wait_for(lambda: original != pane.reading.body.get_children())

    def test_switch_and_destroy_cancel_pending_work_and_release_monitor(self):
        old = self.window.vault_monitor
        old.queue('A.md')
        self.window.load_vault(self.root)
        self.assertTrue(old.closed)
        self.assertIsNone(old.source)
        self.assertFalse(old.monitors)
        reference = weakref.ref(old)
        del old
        gc.collect()
        self.assertIsNone(reference())
        current = self.window.vault_monitor
        self.window.destroy()
        self.assertTrue(current.closed)
        self.assertIsNone(current.source)
        self.assertFalse(current.monitors)

    def test_root_replacement_symlink_is_excluded_and_recreated_root_is_watched(self):
        old = self.root.with_name('old')
        self.root.rename(old)
        outside = self.root.with_name('outside')
        outside.mkdir()
        (outside / 'Secret.md').write_text('outside')
        self.root.symlink_to(outside, target_is_directory=True)
        self.wait_for(lambda: not self.window.vault.notes)
        self.assertNotIn('Secret.md', self.window.vault.notes)
        self.assertFalse(self.window.vault_monitor.monitors)
        self.root.unlink()
        self.root.mkdir()
        (self.root / 'Fresh.md').write_text('Fresh')
        self.wait_for(lambda: 'Fresh.md' in self.window.vault.notes)
        (self.root / 'Fresh.md').write_text('Changed')
        self.wait_for(lambda: self.window.vault.indexed_note('Fresh.md').text == 'Changed')

    def test_idle_does_not_read_notes_or_rebuild_ui(self):
        deadline = time.monotonic() + 4.2
        with patch.object(self.window.vault, 'read', side_effect=AssertionError('Idle read')):
            with patch.object(self.window, 'populate_files', side_effect=AssertionError('Idle UI rebuild')):
                self.wait_for(lambda: time.monotonic() >= deadline, timeout=6)
