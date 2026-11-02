"""Recovery drafts survive interrupted sessions and preserve conflict checks."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from librevault.recovery import RecoveryStore
from librevault.vault import Conflict, Vault


class RecoveryTests(unittest.TestCase):
    def test_recovery_survives_restart_and_conflicting_file_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'vault'
            root.mkdir()
            file = root / 'Note.md'
            file.write_text('original', encoding='utf-8')
            vault = Vault(root)
            store = RecoveryStore(root, Path(directory) / 'state')
            store.write(vault.read('Note.md'), 'unsaved')
            other = RecoveryStore(root, Path(directory) / 'state')
            self.assertEqual(other.drafts(), [])  # A live session is not a crash.
            store.close()
            draft, note, text = other.drafts()[0]
            self.assertEqual(text, 'unsaved')
            file.write_text('external', encoding='utf-8')
            with self.assertRaises(Conflict):
                vault.save(note, text)
            self.assertEqual(file.read_text(encoding='utf-8'), 'external')
            self.assertTrue(draft.exists())
            other.write(note, text)
            other.remove(note.path)
            other.close()

    def test_corrupt_draft_is_ignored(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'vault'
            root.mkdir()
            (root / 'Note.md').write_text('original', encoding='utf-8')
            store = RecoveryStore(root, Path(directory) / 'state')
            store.write(Vault(root).read('Note.md'), 'draft')
            next(store.session.glob('*.json')).write_text('{', encoding='utf-8')
            store.close()
            self.assertEqual(RecoveryStore(root, Path(directory) / 'state').drafts(), [])
