"""Conflict decisions preserve edits and recheck the compared disk revision."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.gui_wait import wait_for_work

try:
    from librevault.app import Window
    from librevault.conflicts_ui import ConflictDialog
    from librevault.vault import Conflict, VaultError
    from gi.repository import Gio, Gtk
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display unavailable')
class ConflictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application(application_id='org.librevault.ConflictTests',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / 'Note.md'
        self.file.write_text('original\n')
        self.window = Window(self.app, self.root, preferences_path=self.root / '.settings.json')
        self.addCleanup(self.window.destroy)
        wait_for_work(self.window)
        self.pane = self.window.open_note('Note.md')
        self.pane.buffer.set_text('editor\n')
        self.file.write_text('disk\n')
        self.pane.autosave()

    def dialog(self):
        dialog = ConflictDialog(self.pane)
        self.addCleanup(dialog.destroy)
        return dialog

    def test_comparison_and_cancel_preserve_all_versions(self):
        self.assertTrue(self.window.compare_button.get_visible())
        dialog = self.dialog()
        texts = []
        for view in dialog.views:
            buffer = view.get_buffer()
            texts.append(buffer.get_text(*buffer.get_bounds(), True))
            self.assertFalse(view.get_editable())
        self.assertEqual(texts, ['original\n', 'editor\n', 'disk\n'])
        self.assertFalse(dialog.resolve(Gtk.ResponseType.CANCEL))
        self.assertEqual(self.file.read_text(), 'disk\n')
        self.assertEqual(self.pane.text(), 'editor\n')
        self.assertTrue(self.pane.dirty)

    def test_replace_uses_compared_disk_revision(self):
        self.assertTrue(self.dialog().resolve(ConflictDialog.REPLACE_DISK))
        self.assertEqual(self.file.read_text(), 'editor\n')
        self.assertFalse(self.pane.dirty)
        self.assertIsNone(self.pane.save_error)
        self.assertFalse(self.window.compare_button.get_visible())

    def test_both_destructive_actions_reject_new_disk_change(self):
        dialog = self.dialog()
        self.file.write_text('new disk\n')
        for response in (dialog.REPLACE_DISK, dialog.USE_DISK):
            with self.assertRaises(Conflict):
                dialog.resolve(response)
            self.assertEqual(self.file.read_text(), 'new disk\n')
            self.assertEqual(self.pane.text(), 'editor\n')
            self.assertEqual(self.pane.note.text, 'original\n')
            self.assertTrue(self.pane.dirty)

    def test_use_disk_clears_recovery_and_dirty_state(self):
        self.window.recovery.write(self.pane.note, self.pane.text())
        self.dialog().resolve(ConflictDialog.USE_DISK)
        self.assertEqual(self.pane.text(), 'disk\n')
        self.assertFalse(self.pane.dirty)
        self.assertEqual(list(self.window.recovery.session.glob('*.json')), [])

    def test_copy_preserves_original_and_editor(self):
        dialog = self.dialog()
        dialog.resolve(dialog.SAVE_COPY)
        self.assertEqual((self.root / 'Note (conflict copy).md').read_text(), 'editor\n')
        self.assertEqual(self.file.read_text(), 'disk\n')
        self.assertEqual(self.pane.text(), 'editor\n')
        self.assertTrue(self.pane.dirty)
        self.assertEqual(self.window.current().note.path, 'Note (conflict copy).md')
        with self.assertRaises(VaultError):
            dialog.resolve(dialog.SAVE_COPY)

    def test_deleted_file_can_be_saved_as_copy(self):
        self.file.unlink()
        dialog = self.dialog()
        self.assertIsNone(dialog.disk)
        self.assertFalse(dialog.get_widget_for_response(dialog.REPLACE_DISK).get_sensitive())
        dialog.resolve(dialog.SAVE_COPY)
        self.assertEqual((self.root / 'Note (conflict copy).md').read_text(), 'editor\n')
        self.assertFalse(self.file.exists())

    def test_manual_save_opens_comparison(self):
        with patch('librevault.conflicts_ui.compare_conflict') as compare:
            self.window.save_current()
        compare.assert_called_once_with(self.pane)

    def test_save_failure_keeps_edits_and_recovery(self):
        dialog = self.dialog()
        with patch.object(self.window.vault, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                dialog.resolve(dialog.REPLACE_DISK)
        self.assertEqual(self.pane.note.text, 'original\n')
        self.assertEqual(self.pane.text(), 'editor\n')
        self.assertTrue(self.pane.dirty)

    def test_close_save_can_resolve_conflict(self):
        def resolve(pane):
            self.dialog().resolve(ConflictDialog.USE_DISK)

        with patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.YES), \
                patch('librevault.conflicts_ui.compare_conflict', side_effect=resolve):
            self.assertTrue(self.window.confirm_panes([self.pane]))
        self.assertFalse(self.pane.dirty)

    def test_close_save_cancel_keeps_note_open(self):
        with patch.object(Gtk.MessageDialog, 'run', return_value=Gtk.ResponseType.YES), \
                patch('librevault.conflicts_ui.compare_conflict'):
            self.assertFalse(self.window.confirm_panes([self.pane]))
        self.assertTrue(self.pane.dirty)

    def test_dialog_cancel_releases_autosave_pause(self):
        from librevault.conflicts_ui import compare_conflict

        def cancel(dialog):
            self.assertTrue(self.pane.resolving_conflict)
            return Gtk.ResponseType.CANCEL

        with patch.object(ConflictDialog, 'run', cancel):
            compare_conflict(self.pane)
        self.assertFalse(self.pane.resolving_conflict)
        self.assertTrue(self.pane.dirty)
