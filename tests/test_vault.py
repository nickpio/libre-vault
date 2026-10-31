"""Storage and link contracts, exercised against disposable vaults."""
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.vault import Conflict, Vault, VaultError, links


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_open_does_not_modify_vault_and_excludes_hidden_and_symlinks(self):
        self.write('Visible.MD', 'hello')
        self.write('.obsidian/settings.json', '{}')
        self.write('.private.md', 'private')
        self.write('.hidden/secret.md', 'secret')
        (self.root / 'alias.md').symlink_to('Visible.MD')
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        vault = Vault(self.root)
        self.assertEqual(list(vault.notes), ['Visible.MD'])
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))
        with self.assertRaises(VaultError):
            vault.read('alias.md')

    def test_rejects_paths_outside_visible_markdown_files(self):
        vault = Vault(self.root)
        for path in ('../outside.md', '/outside.md', '.secret.md', 'folder/.secret.md', 'file.txt'):
            with self.subTest(path=path), self.assertRaises(VaultError):
                vault.read(path)

    def test_non_utf8_is_listed_but_not_rewritten(self):
        path = self.root / 'binary.md'
        path.write_bytes(b'\xff\xfe')
        vault = Vault(self.root)
        self.assertIn('binary.md', vault.notes)
        self.assertIsNone(vault.notes['binary.md'])
        with self.assertRaises(VaultError):
            vault.read('binary.md')
        self.assertEqual(path.read_bytes(), b'\xff\xfe')

    def test_save_preserves_bom_crlf_permissions_and_frontmatter(self):
        path = self.root / 'Note.md'
        path.write_bytes(b'\xef\xbb\xbf---\r\ntitle: Example\r\n---\r\nOld\r\n')
        path.chmod(0o640)
        vault = Vault(self.root)
        note = vault.read('Note.md')
        saved = vault.save(note, '---\ntitle: Example\n---\nNew\n')
        self.assertEqual(path.read_bytes(), b'\xef\xbb\xbf---\r\ntitle: Example\r\n---\r\nNew\r\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertNotEqual(saved.revision, note.revision)
        self.assertEqual(vault.notes['Note.md'], saved)

    def test_external_edits_and_deletions_stop_saves(self):
        path = self.write('Note.md', 'original')
        vault = Vault(self.root)
        note = vault.read('Note.md')
        path.write_text('external', encoding='utf-8')
        with self.assertRaises(Conflict):
            vault.save(note, 'local')
        self.assertEqual(path.read_text(), 'external')
        path.unlink()
        with self.assertRaises(Conflict):
            vault.save(note, 'local')
        self.assertFalse(path.exists())

    def test_failed_atomic_replace_keeps_original_and_removes_temporary(self):
        path = self.write('Note.md', 'original')
        vault = Vault(self.root)
        with patch('librevault.vault.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                vault.save(vault.read('Note.md'), 'new')
        self.assertEqual(path.read_text(), 'original')
        self.assertEqual(list(self.root.glob('.librevault-*')), [])

    def test_create_does_not_overwrite_or_create_missing_folders(self):
        vault = Vault(self.root)
        self.assertEqual(vault.create(' New note ').path, 'New note.md')
        with self.assertRaises(VaultError):
            vault.create('New note')
        with self.assertRaises(VaultError):
            vault.create('missing/New note')
        self.assertFalse((self.root / 'missing').exists())

    def test_relative_encoded_and_ambiguous_links(self):
        for path in ('Source.md', 'Folder/Source.md', 'Folder/Local note.md', 'One/Duplicate.md', 'Two/Duplicate.md'):
            self.write(path, '')
        vault = Vault(self.root)
        self.assertEqual(vault.resolve('Folder/Source.md', './Local%20note.md#Heading'), ['Folder/Local note.md'])
        self.assertEqual(vault.resolve('Folder/Source.md', '../Source'), ['Source.md'])
        self.assertEqual(vault.resolve('Source.md', 'Duplicate'), ['One/Duplicate.md', 'Two/Duplicate.md'])
        self.assertEqual(vault.resolve('Source.md', '#Heading'), ['Source.md'])
        self.assertEqual(vault.resolve('Source.md', 'missing'), [])

    def test_search_requires_all_words_and_refreshes_saved_contents(self):
        path = self.write('Folder/Note.md', 'Café ALPHA beta')
        vault = Vault(self.root)
        self.assertEqual(vault.search('folder café alpha'), ['Folder/Note.md'])
        self.assertEqual(vault.search('alpha missing'), [])
        path.write_text('replacement', encoding='utf-8')
        vault.refresh()
        self.assertEqual(vault.search('alpha'), [])
        self.assertEqual(vault.search('replacement'), ['Folder/Note.md'])

    def test_backlinks_exclude_ambiguous_and_literal_links(self):
        self.write('Target.md', '')
        self.write('Source.md', '[[Target]] [[Duplicate]] `[[Ignored]]`\n')
        self.write('One/Duplicate.md', '')
        self.write('Two/Duplicate.md', '')
        self.write('Other.md', '```\n[[Target]]\n```\n')
        vault = Vault(self.root)
        self.assertEqual(vault.backlinks('Target.md'), ['Source.md'])
        self.assertEqual(vault.backlinks('One/Duplicate.md'), [])
        self.assertEqual(vault.backlinks('Two/Duplicate.md'), [])


class LinkParsingTests(unittest.TestCase):
    def test_frontmatter_code_and_external_links_are_ignored(self):
        source = '---\nvalue: "[[Metadata]]"\n---\n`[[Inline]]`\n\n```\n[[Fence]]\n```\n[external](https://example.com) [[Note|Label]] ![[Embed]] [local](Folder/Note.md)'
        parsed = links(source)
        self.assertEqual([link.target for link in parsed], ['Note', 'Embed', 'Folder/Note.md'])
        self.assertEqual(parsed[0].label, 'Label')
        self.assertTrue(parsed[1].embedded)


if __name__ == '__main__':
    unittest.main()
