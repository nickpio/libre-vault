"""Disk index semantics and bounded resident note storage."""
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import tracemalloc
import unittest
from unittest.mock import patch

from librevault.metadata import matches_query, note_metadata, query_terms
from librevault.vault import Vault


class DiskIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / 'vault'
        self.root.mkdir()

    def vault(self, **kwargs):
        vault = Vault(self.root, cache_dir=self.base / 'cache', **kwargs)
        self.addCleanup(vault.close)
        return vault

    def test_index_and_search_do_not_retain_vault_text(self):
        for number in range(24):
            (self.root / f'{number:03}.md').write_text('large text body ' * 16000)
        gc.collect()
        tracemalloc.start()
        self.addCleanup(tracemalloc.stop)
        before = tracemalloc.get_traced_memory()[0]
        vault = self.vault(text_cache_bytes=4096)
        self.assertTrue(all(not hasattr(note, 'text') for note in vault.notes.values()))
        self.assertEqual(len(vault.search('text body')), 24)
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0] - before
        # More than 6 MB of source must not remain resident in Python. This
        # allows parser/module warmup without measuring allocator-specific RSS.
        self.assertLess(retained, 2 * 1024 * 1024)
        self.assertEqual(vault.text_cache.bytes, 0)
        vault.indexed_note('000.md')
        self.assertEqual(vault.text_cache.bytes, 0)  # oversized notes bypass cache

    def test_text_cache_eviction_and_external_invalidation(self):
        for name in ('A', 'B', 'C'):
            (self.root / f'{name}.md').write_text(name * 800)
        vault = self.vault(text_cache_bytes=1500)
        for name in ('A', 'B', 'C'):
            self.assertEqual(vault.indexed_note(f'{name}.md').text, name * 800)
            self.assertLessEqual(vault.text_cache.bytes, 1500)
        self.assertNotIn('A.md', vault.text_cache.entries)
        (self.root / 'C.md').write_text('changed')
        vault.update_paths(['C.md'])
        self.assertEqual(vault.indexed_note('C.md').text, 'changed')
        (self.root / 'C.md').unlink()
        vault.update_paths(['C.md'])
        self.assertNotIn('C.md', vault.text_cache.entries)
        self.assertIsNone(vault.index.revision('C.md'))

    def test_reopen_reuses_parses_and_corruption_rebuilds(self):
        (self.root / 'Note.md').write_text('---\naliases: [Other]\n---\n[[Target]]')
        vault = self.vault()
        path = vault.index.path
        self.assertFalse(path.is_relative_to(self.root))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        vault.close()
        with patch('librevault.vault.links', side_effect=AssertionError('Reparsed unchanged note')):
            reopened = self.vault()
        self.assertEqual(reopened.metadata('Note.md').aliases, ('Other',))
        reopened.close()
        path.write_bytes(b'broken cache')
        rebuilt = self.vault()
        self.assertEqual(rebuilt.search('alias:Other'), ['Note.md'])
        self.assertEqual(rebuilt.index.links('Note.md')[0]['target'], 'Target')

    def test_indexed_search_matches_existing_substring_and_filter_semantics(self):
        sources = {
            'A.md': '---\naliases: ["Launch Plan", "STRASSE"]\ntags: [work/project]\ndue: 2026-11-02\n---\nCafé Straße a.b [literal] Alpha\nBeta',
            'Other.md': 'ALPHABET alpha beta x "quotes"\n',
            'path-only.md': 'nothing',
            'nul.md': 'before\x00needle',
        }
        for path, text in sources.items():
            (self.root / path).write_text(text)
        (self.root / 'bad.md').write_bytes(b'\xff')
        sources['bad.md'] = ''
        vault = self.vault()
        queries = ['alpha', 'pha', 'a', '"alpha beta"', '"Alpha\nBeta"', 'content:Straße',
                   'strasse', 'café', '"launch plan"', 'alias:launch', 'tag:work',
                   'property:due=2026', 'path:only', '-alpha', 'file:bad', '[due]',
                   'needle', 'a.b', '"[literal]"', 'content:', '"x \\"quotes\\""', 'straße -tag:work', '']
        for query in queries:
            with self.subTest(query=query):
                terms = query_terms(query)
                expected = sorted(path for path, text in sources.items()
                                  if matches_query(path, text, terms, note_metadata(text)))
                self.assertEqual(vault.search(query), expected)
        vault.index.fts = False
        self.assertEqual(vault.search('pha'), ['A.md', 'Other.md'])

    def test_two_open_vaults_return_consistent_snapshot_revisions(self):
        (self.root / 'Note.md').write_text('old')
        first = self.vault()
        second = self.vault()
        saved = second.save(second.read('Note.md'), 'new')
        snapshot = first.indexed_note('Note.md')
        self.assertEqual(snapshot.text, 'new')
        self.assertEqual(snapshot.revision, saved.revision)

    def test_locked_index_is_reported_without_deleting_cache(self):
        import sqlite3
        from librevault.vault import VaultError
        (self.root / 'Note.md').write_text('Important text')
        vault = self.vault()
        path = vault.index.path
        inode = path.stat().st_ino
        connect = sqlite3.connect
        locker = connect(path)
        self.addCleanup(locker.close)
        locker.execute('BEGIN EXCLUSIVE')
        with patch('librevault.index.sqlite3.connect', side_effect=lambda path: connect(path, timeout=.01)):
            with self.assertRaisesRegex(VaultError, 'locked'):
                self.vault()
        locker.rollback()
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(vault.search('Important'), ['Note.md'])

    def test_newly_available_trigram_index_is_populated(self):
        (self.root / 'Note.md').write_text('Searchable existing note')
        vault = self.vault()
        vault.index.connection.execute('DROP TABLE search')
        vault.index.connection.commit()
        vault.close()
        reopened = self.vault()
        self.assertEqual(reopened.search('existing'), ['Note.md'])
