"""Saved-link caching must preserve backlinks as files and targets change."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import librevault.vault as engine


class LinkCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.write('Source.md', '[[Target]]\n')
        self.write('Target.md', '# Target\n')
        self.vault = engine.Vault(self.root)

    def write(self, path, text):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding='utf-8')

    def test_repeated_backlink_queries_reuse_parses_even_after_refresh(self):
        with patch.object(engine, 'parse', wraps=engine.parse) as parse:
            self.assertEqual(self.vault.backlinks('Target.md'), ['Source.md'])
            initial_count = parse.call_count
            self.assertEqual(initial_count, 2)
            self.vault.refresh()
            self.assertEqual(self.vault.backlinks('Target.md'), ['Source.md'])
            self.assertEqual(self.vault.backlinks('Source.md'), [])
            self.assertEqual(parse.call_count, initial_count)

    def test_save_reparses_only_changed_note(self):
        self.vault.backlinks('Target.md')
        with patch.object(engine, 'parse', wraps=engine.parse) as parse:
            self.vault.save(self.vault.notes['Source.md'], 'No links\n')
            self.assertEqual(self.vault.backlinks('Target.md'), [])
            self.assertEqual(parse.call_count, 1)
            self.vault.backlinks('Target.md')
            self.assertEqual(parse.call_count, 1)

    def test_external_edit_reparses_only_changed_note(self):
        self.vault.backlinks('Target.md')
        with patch.object(engine, 'parse', wraps=engine.parse) as parse:
            self.write('Source.md', 'No links\n')
            self.vault.refresh()
            self.assertEqual(self.vault.backlinks('Target.md'), [])
            self.assertEqual(parse.call_count, 1)

    def test_unreadable_note_loses_cached_links_and_can_recover(self):
        self.assertEqual(self.vault.backlinks('Target.md'), ['Source.md'])
        (self.root / 'Source.md').write_bytes(b'\xff')
        self.vault.refresh()
        self.assertEqual(self.vault.backlinks('Target.md'), [])
        self.assertNotIn('Source.md', self.vault._link_cache)
        self.write('Source.md', '[[Target]]\n')
        self.vault.refresh()
        self.assertEqual(self.vault.backlinks('Target.md'), ['Source.md'])

    def test_deleted_note_is_removed_from_cache(self):
        self.vault.backlinks('Target.md')
        (self.root / 'Source.md').unlink()
        self.vault.refresh()
        self.assertEqual(self.vault.backlinks('Target.md'), [])
        self.assertNotIn('Source.md', self.vault._link_cache)

    def test_create_resolves_previously_missing_target_without_reparse(self):
        self.vault.save(self.vault.notes['Source.md'], '[[New]]\n')
        self.assertEqual(self.vault.backlinks('New.md'), [])
        self.vault.create('New.md')
        with patch.object(engine, 'parse', wraps=engine.parse) as parse:
            self.assertEqual(self.vault.backlinks('New.md'), ['Source.md'])
            # Only the new empty note needs a parse.
            self.assertEqual(parse.call_count, 1)

    def test_path_changes_update_ambiguity_without_reparsing_source(self):
        (self.root / 'Target.md').unlink()
        self.write('One/Target.md', '')
        self.vault.refresh()
        self.assertEqual(self.vault.backlinks('One/Target.md'), ['Source.md'])
        self.write('Two/Target.md', '')
        self.vault.refresh()
        self.assertEqual(self.vault.backlinks('One/Target.md'), [])
        self.assertEqual(self.vault.backlinks('Two/Target.md'), [])
        (self.root / 'Two/Target.md').unlink()
        self.vault.refresh()
        self.assertEqual(self.vault.backlinks('One/Target.md'), ['Source.md'])


if __name__ == '__main__':
    unittest.main()
