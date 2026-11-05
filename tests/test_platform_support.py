"""Storage and process boundaries exercised on each supported OS."""
import errno
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.platform_support import app_directory, lock_session, set_file_mode
from librevault.durable import sync_directory
from librevault.recovery import RecoveryStore, VersionHistoryStore
from librevault.vault import Note, Vault


class PlatformTests(unittest.TestCase):
    def test_native_directories_and_overrides(self):
        with patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.home', return_value=Path('/home/test')):
            with patch('sys.platform', 'linux'):
                self.assertEqual(app_directory('config'), Path('/home/test/.config/libre-vault'))
            with patch('sys.platform', 'darwin'):
                self.assertEqual(app_directory('cache'), Path('/home/test/Library/Caches/libre-vault'))
                self.assertEqual(app_directory('state'), Path('/home/test/Library/Application Support/libre-vault/state'))
            with patch('sys.platform', 'win32'), patch.dict(os.environ, {'APPDATA': '/roaming', 'LOCALAPPDATA': '/local'}):
                self.assertEqual(app_directory('config'), Path('/roaming/libre-vault'))
                self.assertEqual(app_directory('state'), Path('/local/libre-vault/state'))
                self.assertEqual(app_directory('cache'), Path('/local/libre-vault/cache'))
            for platform in ('linux', 'darwin', 'win32'):
                with patch('sys.platform', platform), patch.dict(os.environ, {'XDG_STATE_HOME': '/private'}):
                    self.assertEqual(app_directory('state'), Path('/private/libre-vault'))

    def test_windows_does_not_call_posix_file_operations(self):
        with patch('sys.platform', 'win32'), patch('os.open') as opening:
            sync_directory('.')
            set_file_mode(None, 0o600)
            opening.assert_not_called()

    def test_posix_directory_errors_are_not_suppressed(self):
        with patch('sys.platform', 'linux'), patch('os.O_DIRECTORY', 0, create=True), patch('os.open', side_effect=OSError(errno.EIO, 'disk error')):
            with self.assertRaises(OSError):
                sync_directory('.')

    def test_session_lock_excludes_other_process_and_releases_on_close(self):
        code = '''import sys
from librevault.platform_support import lock_session
with open(sys.argv[1], 'a') as handle:
    try:
        lock_session(handle, blocking=False)
    except BlockingIOError:
        sys.exit(23)
'''
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'lock'
            with path.open('w') as handle:
                lock_session(handle)
                result = subprocess.run([sys.executable, '-c', code, str(path)], timeout=15)
                self.assertEqual(result.returncode, 23)
            result = subprocess.run([sys.executable, '-c', code, str(path)], timeout=15)
            self.assertEqual(result.returncode, 0)

    def test_recovery_round_trip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RecoveryStore(root, root / 'recovery')
            other = RecoveryStore(root, root / 'recovery')
            note = Note('Note.md', 'original', 'revision', False)
            try:
                store.write(note, 'draft ü')
                self.assertEqual(other.drafts(), [])
                store.close()
                self.assertEqual(other.drafts()[0][2], 'draft ü')
            finally:
                store.close()
                other.close()

    def test_save_history_canvas_and_move_with_link_updates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'vault'
            root.mkdir()
            (root / 'Note.md').write_bytes(b'original\r\n')
            (root / 'Link.md').write_bytes(b'[[Note]]\n')
            vault = Vault(root)
            vault.version_history = VersionHistoryStore(root, Path(directory) / 'history')
            try:
                vault.save(vault.read('Note.md'), 'updated ü\n')
                self.assertEqual((root / 'Note.md').read_bytes(), 'updated ü\r\n'.encode())
                self.assertEqual(vault.version_history.versions('Note.md')[-1].text, 'updated ü\r\n')
                canvas = vault.create_canvas('Board.canvas')
                vault.save_canvas(canvas)
                vault.move('Note.md', 'Renamed.md')
                self.assertFalse((root / 'Note.md').exists())
                self.assertIn('[[Renamed]]', (root / 'Link.md').read_text(encoding='utf-8'))
            finally:
                vault.close()
