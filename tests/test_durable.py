"""Atomic replacement and directory persistence fail at different stages."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import librevault.durable as durable
from librevault.durable import (
    AtomicReplacementError,
    DirectorySyncError,
    replace_durably,
    synced_temporary,
    write_json,
    write_synced_temporary,
)


class DurableReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.destination = self.directory / 'note.md'
        self.destination.write_text('original', encoding='utf-8')

    def publish(self, text='revised'):
        with synced_temporary(
                self.directory, lambda handle: handle.write(text),
                prefix='.librevault-', binary=False) as temporary:
            replace_durably(temporary, self.destination)

    def test_file_sync_replacement_and_directory_sync_run_in_that_order(self):
        order = []
        real_sync_file = durable.sync_file
        real_replace = durable.replace_file
        real_sync_directory = durable.sync_directory

        def sync_file(descriptor):
            order.append('file')
            real_sync_file(descriptor)

        def replace(temporary, target):
            order.append('replace')
            self.assertEqual(self.destination.read_text(encoding='utf-8'), 'original')
            replace_result = real_replace(temporary, target)
            self.assertEqual(self.destination.read_text(encoding='utf-8'), 'revised')
            return replace_result

        def sync_directory(directory):
            order.append('directory')
            self.assertEqual(Path(directory).resolve(), self.directory.resolve())
            self.assertEqual(self.destination.read_text(encoding='utf-8'), 'revised')
            real_sync_directory(directory)

        with patch('librevault.durable.sync_file', sync_file), \
                patch('librevault.durable.replace_file', replace), \
                patch('librevault.durable.sync_directory', sync_directory):
            self.publish()
        self.assertEqual(order, ['file', 'replace', 'directory'])
        self.assertEqual(list(self.directory.glob('.librevault-*')), [])

    def test_write_failure_leaves_the_destination_and_removes_the_temporary(self):
        def fail(_handle):
            raise OSError('write failed')

        with patch('librevault.durable.replace_file') as replace, \
                patch('librevault.durable.sync_directory') as sync_directory:
            with self.assertRaises(OSError) as caught:
                write_synced_temporary(self.directory, fail, prefix='.librevault-', binary=False)
        self.assertNotIsInstance(caught.exception, (AtomicReplacementError, DirectorySyncError))
        self.assertEqual(str(caught.exception), 'write failed')
        replace.assert_not_called()
        sync_directory.assert_not_called()
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'original')
        self.assertEqual(list(self.directory.glob('.librevault-*')), [])

    def test_file_sync_failure_leaves_the_destination_and_removes_the_temporary(self):
        with patch('librevault.durable.sync_file', side_effect=OSError('file sync failed')), \
                patch('librevault.durable.replace_file') as replace, \
                patch('librevault.durable.sync_directory') as sync_directory:
            with self.assertRaises(OSError) as caught:
                self.publish()
        self.assertNotIsInstance(caught.exception, (AtomicReplacementError, DirectorySyncError))
        self.assertEqual(str(caught.exception), 'file sync failed')
        replace.assert_not_called()
        sync_directory.assert_not_called()
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'original')
        self.assertEqual(list(self.directory.glob('.librevault-*')), [])

    def test_atomic_replacement_failure_leaves_the_destination_and_removes_the_temporary(self):
        with patch('librevault.durable.replace_file', side_effect=OSError('disk failure')), \
                patch('librevault.durable.sync_directory') as sync_directory:
            with self.assertRaises(AtomicReplacementError) as caught:
                self.publish()
        self.assertIn('Could not replace', str(caught.exception))
        self.assertIn('disk failure', str(caught.exception))
        sync_directory.assert_not_called()
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'original')
        self.assertEqual(list(self.directory.glob('.librevault-*')), [])

    def test_directory_sync_failure_keeps_the_visible_replacement(self):
        with patch('librevault.durable.sync_directory', side_effect=OSError('power loss')):
            with self.assertRaises(DirectorySyncError) as caught:
                self.publish()
        self.assertIn('Replaced', str(caught.exception))
        self.assertIn('could not sync its directory', str(caught.exception))
        self.assertIn('power loss', str(caught.exception))
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'revised')
        self.assertEqual(list(self.directory.glob('.librevault-*')), [])

    def test_body_failure_after_file_sync_removes_the_temporary(self):
        with self.assertRaises(OSError) as caught:
            with synced_temporary(
                    self.directory, lambda handle: handle.write('revised'),
                    prefix='.librevault-', binary=False):
                self.assertEqual(len(list(self.directory.glob('.librevault-*'))), 1)
                raise OSError('stopped before replace')
        self.assertEqual(str(caught.exception), 'stopped before replace')
        self.assertEqual(self.destination.read_text(encoding='utf-8'), 'original')
        self.assertEqual(list(self.directory.glob('.librevault-*')), [])

    def test_write_json_round_trips_unicode_and_keeps_the_previous_file_on_failure(self):
        target = self.directory / 'state.json'
        write_json(target, {'name': 'café'}, prefix='.state-', indent=2)
        before = target.read_bytes()
        self.assertEqual(before.decode('utf-8'), '{\n  "name": "café"\n}\n')
        failures = (
            (TypeError, {'bad': object()}, patch('librevault.durable.sync_directory')),
            (AtomicReplacementError, {},
             patch('librevault.durable.replace_file', side_effect=OSError('disk failure'))),
        )
        for error_type, data, stage in failures:
            with self.subTest(error=error_type.__name__), stage:
                with self.assertRaises(error_type):
                    write_json(target, data, prefix='.state-')
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(self.directory.glob('.state-*')), [])


if __name__ == '__main__':
    unittest.main()
