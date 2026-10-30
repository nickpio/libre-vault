"""Regression tests for the prototype's documented compatibility subset."""
import os
from pathlib import Path
import stat
import tempfile
import unittest

from librevault.vault import Conflict, Vault, VaultError, links


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'Projects').mkdir()
        (self.root / 'Welcome.md').write_text('# Welcome\n\n[[Projects/Plan|Plan]]\n', encoding='utf-8')
        (self.root / 'Projects/Plan.md').write_text('# Plan\n\n[[Welcome#Welcome]]\n', encoding='utf-8')
        self.vault = Vault(self.root)

    def test_open_does_not_modify_files(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        Vault(self.root)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_wikilinks_and_backlinks(self):
        self.assertEqual(self.vault.resolve('Welcome.md', 'Projects/Plan#Details|Label'), ['Projects/Plan.md'])
        self.assertEqual(self.vault.backlinks('Projects/Plan.md'), ['Welcome.md'])
        self.assertEqual(self.vault.resolve('Projects/Plan.md', '#Plan'), ['Projects/Plan.md'])

    def test_relative_and_encoded_links(self):
        (self.root / 'Projects/Note one.md').write_text('Hello')
        self.vault.refresh()
        self.assertEqual(self.vault.resolve('Projects/Plan.md', '../Welcome.md#Welcome'), ['Welcome.md'])
        self.assertEqual(self.vault.resolve('Projects/Plan.md', './Note%20one.md'), ['Projects/Note one.md'])
        self.assertEqual(self.vault.resolve('Welcome.md', '../../outside'), [])

    def test_ambiguous_basename(self):
        (self.root / 'Other').mkdir()
        (self.root / 'Other/Plan.md').write_text('# Other')
        self.vault.refresh()
        self.assertEqual(self.vault.resolve('Welcome.md', 'Plan'), ['Other/Plan.md', 'Projects/Plan.md'])
        self.assertEqual(self.vault.resolve('Projects/Another.md', 'Plan'), ['Projects/Plan.md'])
        self.assertNotIn('Welcome.md', self.vault.backlinks('Other/Plan.md'))

    def test_markdown_parser_excludes_code_and_frontmatter(self):
        source = '''---
example: "[[Metadata]]"
---
[[Real#Heading|Name]] ![[Picture.png]]
`[[Inline code]]`
```md
[[Fenced code]]
```
[local](Projects/Plan.md) [web](https://example.org) \\[[Escaped]]
'''
        found = links(source)
        self.assertEqual([x.target for x in found], ['Real#Heading', 'Picture.png', 'Projects/Plan.md'])
        self.assertEqual(found[0].label, 'Name')
        self.assertTrue(found[1].embedded)

    def test_reference_markdown_links(self):
        self.assertEqual([x.target for x in links('[Plan][p]\n\n[p]: Projects/Plan.md')], ['Projects/Plan.md'])

    def test_search(self):
        self.assertEqual(self.vault.search('PLAN projects'), ['Projects/Plan.md', 'Welcome.md'])
        self.assertEqual(self.vault.search('nonexistent'), [])

    def test_save_updates_disk_and_index(self):
        note = self.vault.read('Welcome.md')
        saved = self.vault.save(note, '# Changed\n\n[[Projects/Plan]]\n')
        self.assertNotEqual(note.revision, saved.revision)
        self.assertEqual((self.root / 'Welcome.md').read_text(), saved.text)
        self.assertEqual(self.vault.search('Changed'), ['Welcome.md'])

    def test_preserves_utf8_bom_crlf_frontmatter_and_mode(self):
        path = self.root / 'Welcome.md'
        original = b'\xef\xbb\xbf---\r\ncustom: yes\r\n---\r\n# Caf\xc3\xa9\r\n'
        path.write_bytes(original)
        path.chmod(0o640)
        note = self.vault.read('Welcome.md')
        self.vault.save(note, note.text.replace('\r\n', '\n') + 'Added\n')
        self.assertEqual(path.read_bytes(), original + b'Added\r\n')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_conflict_retains_external_contents(self):
        note = self.vault.read('Welcome.md')
        path = self.root / 'Welcome.md'
        path.write_text('Edited by another program')
        with self.assertRaises(Conflict):
            self.vault.save(note, 'My edits')
        self.assertEqual(path.read_text(), 'Edited by another program')
        self.assertFalse(list(self.root.glob('.librevault-*')))

    def test_deleted_note_is_not_recreated_on_save(self):
        note = self.vault.read('Welcome.md')
        (self.root / 'Welcome.md').unlink()
        with self.assertRaises(Conflict):
            self.vault.save(note, 'My edits')
        self.assertFalse((self.root / 'Welcome.md').exists())

    def test_create_no_overwrite(self):
        note = self.vault.create('Projects/New note')
        self.assertEqual(note.path, 'Projects/New note.md')
        with self.assertRaises(VaultError):
            self.vault.create('Welcome')
        self.assertTrue((self.root / 'Welcome.md').read_text().startswith('# Welcome'))

    def test_hidden_settings_and_symlinks_untouched(self):
        (self.root / '.obsidian').mkdir()
        settings = self.root / '.obsidian/settings.json'
        settings.write_text('{"unknown":true}')
        (self.root / '.obsidian/Hidden.md').write_text('Hidden')
        (self.root / 'Shortcut.md').symlink_to(self.root / 'Welcome.md')
        (self.root / 'Linked').symlink_to(self.root / 'Projects', target_is_directory=True)
        self.vault.refresh()
        self.assertEqual(set(self.vault.notes), {'Welcome.md', 'Projects/Plan.md'})
        self.assertEqual(settings.read_text(), '{"unknown":true}')
        for path in ['Shortcut.md', 'Linked/Plan.md', '../outside.md', '/tmp/outside.md', '.obsidian/Hidden.md']:
            with self.subTest(path=path), self.assertRaises(VaultError):
                self.vault.read(path)

    def test_invalid_utf8_listed_but_never_overwritten(self):
        path = self.root / 'Legacy.md'
        path.write_bytes(b'\xff\xfeBad')
        self.vault.refresh()
        self.assertIn('Legacy.md', self.vault.notes)
        with self.assertRaises(VaultError):
            self.vault.read('Legacy.md')
        self.assertEqual(path.read_bytes(), b'\xff\xfeBad')

    def test_refresh_tracks_external_create_edit_delete(self):
        path = self.root / 'External.md'
        path.write_text('Created externally')
        self.vault.refresh()
        self.assertEqual(self.vault.search('externally'), ['External.md'])
        path.write_text('Edited externally')
        self.vault.refresh()
        self.assertEqual(self.vault.notes['External.md'].text, 'Edited externally')
        path.unlink()
        self.vault.refresh()
        self.assertNotIn('External.md', self.vault.notes)


if __name__ == '__main__':
    unittest.main()
