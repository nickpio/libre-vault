"""Reorganization preserves link meaning, source syntax and recoverable files."""
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.vault import Conflict, Vault, VaultError


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def test_rename_updates_wiki_markdown_and_reference_links_but_not_code(self):
        self.write('Old name.md', '# Heading\n')
        source = ('---\nexample: "[[Old name]]"\n---\n'
                  '[[Old name#Heading|label]] ![[Old name]] [text](Old%20name.md#Heading)\n'
                  '[reference][target]\n\n[target]: <Old name.md> "title"\n'
                  '`[[Old name]]`\n\n```md\n[[Old name]]\n```\n')
        self.write('Source.md', source)
        vault = Vault(self.root)
        self.assertEqual(vault.move('Old name.md', 'New name.md'), {'Old name.md': 'New name.md'})
        changed = vault.read('Source.md').text
        self.assertIn('[[New name#Heading|label]] ![[New name]]', changed)
        self.assertIn('[text](./New%20name.md#Heading)', changed)
        self.assertIn('[target]: <./New%20name.md> "title"', changed)
        self.assertIn('example: "[[Old name]]"', changed)
        self.assertIn('`[[Old name]]`', changed)
        self.assertIn('```md\n[[Old name]]\n```', changed)
        self.assertFalse((self.root / 'Old name.md').exists())

    def test_folder_move_preserves_outgoing_relative_links_and_attachments(self):
        self.write('Folder/Source.md', '[[../Outside]] ![pic](../picture.png) ![[inside.png]]\n')
        self.write('Outside.md', '![[Folder/inside.png]] [source](Folder/Source.md)\n')
        self.write('Folder/inside.png', 'image')
        self.write('picture.png', 'outside image')
        (self.root / 'Archive').mkdir()
        vault = Vault(self.root)
        mapping = vault.move('Folder', 'Archive/Folder')
        self.assertEqual(mapping['Folder/Source.md'], 'Archive/Folder/Source.md')
        self.assertEqual(mapping['Folder/inside.png'], 'Archive/Folder/inside.png')
        changed = vault.read('Archive/Folder/Source.md').text
        self.assertIn('[[Outside]]', changed)
        self.assertIn('![pic](../../picture.png)', changed)
        self.assertIn('![[Archive/Folder/inside.png]]', changed)
        self.assertIn('![[Archive/Folder/inside.png]]', vault.read('Outside.md').text)
        self.assertTrue((self.root / 'Archive/Folder/inside.png').is_file())

    def test_move_does_not_shadow_previously_resolved_link(self):
        self.write('A/Target.md', '# Original\n')
        self.write('B.md', '# Moved\n')
        self.write('Source.md', '[[Target]] [target](Target.md)\n')
        vault = Vault(self.root)
        vault.move('B.md', 'Target.md')
        self.assertIn('[[A/Target]]', vault.read('Source.md').text)
        self.assertIn('[target](./A/Target.md)', vault.read('Source.md').text)
        self.assertEqual(vault.resolve('Source.md', 'A/Target'), ['A/Target.md'])

    def test_renaming_to_reserved_url_characters_keeps_wiki_fragments_distinct(self):
        self.write('Old.md', '# Heading\n')
        self.write('Source.md', '[[Old#Heading]] [text](Old.md#Heading)\n')
        vault = Vault(self.root)
        vault.move('Old.md', 'New#50%20.md')
        changed = vault.read('Source.md').text
        self.assertIn('[[New%2350%2520#Heading]]', changed)
        self.assertIn('[text](./New%2350%2520.md#Heading)', changed)
        self.assertEqual(vault.resolve('Source.md', 'New%2350%2520#Heading'), ['New#50%20.md'])

    def test_failed_move_rolls_back_preceding_link_edits(self):
        self.write('Old.md', '')
        self.write('Source.md', '[[Old]]\n')
        vault = Vault(self.root)
        rename = Path.rename
        def fail_source(path, destination):
            if path == self.root / 'Old.md':
                raise OSError('move failed')
            return rename(path, destination)
        with patch.object(Path, 'rename', fail_source):
            with self.assertRaises(OSError):
                vault.move('Old.md', 'New.md')
        self.assertEqual(vault.read('Source.md').text, '[[Old]]\n')
        self.assertTrue((self.root / 'Old.md').exists())
        self.assertFalse((self.root / 'New.md').exists())
        self.assertEqual(list((self.root / '.trash/.librevault-moves').glob('*/manifest.json')), [])

    def test_failed_rollback_preserves_durable_original_bytes_and_manifest(self):
        self.write('Old.md', '')
        self.write('Source1.md', '[[Old]]\n')
        self.write('Source2.md', '[[Old]]\n')
        vault = Vault(self.root)
        save = vault.save
        calls = []
        def fail_during_second_save(note, text):
            calls.append(note.path)
            manifests = list((self.root / '.trash/.librevault-moves').glob('*/manifest.json'))
            self.assertEqual(len(manifests), 1, 'Backups must be complete before any mutation')
            if len(calls) == 2:
                (self.root / 'Source1.md').write_text('external intervening change')
                raise OSError('simulated write failure')
            return save(note, text)
        with patch.object(vault, 'save', side_effect=fail_during_second_save):
            with self.assertRaisesRegex(VaultError, 'Original note bytes.*preserved'):
                vault.move('Old.md', 'New.md')
        self.assertEqual((self.root / 'Source1.md').read_text(), 'external intervening change')
        self.assertEqual((self.root / 'Source2.md').read_text(), '[[Old]]\n')
        manifest = next((self.root / '.trash/.librevault-moves').glob('*/manifest.json'))
        data = json.loads(manifest.read_text())
        self.assertEqual(data['source'], 'Old.md')
        self.assertEqual(data['destination'], 'New.md')
        for note in data['notes']:
            self.assertEqual((manifest.parent / note['backup']).read_bytes(), b'[[Old]]\n')
        self.assertTrue((self.root / 'Old.md').is_file())
        self.assertFalse((self.root / 'New.md').exists())

    def test_external_changes_stop_move_before_any_link_write(self):
        old = self.write('Old.md', 'original')
        self.write('Source.md', '[[Old]]')
        vault = Vault(self.root)
        old.write_text('external')
        with self.assertRaises(Conflict):
            vault.move('Old.md', 'New.md')
        self.assertEqual((self.root / 'Source.md').read_text(), '[[Old]]')
        self.assertEqual(old.read_text(), 'external')

    def test_link_rewrite_preserves_bom_crlf_permissions_and_unrelated_source(self):
        self.write('Old.md', '# target\n')
        source = self.root / 'Source.md'
        source.write_bytes(b'\xef\xbb\xbf---\r\ncustom: yes\r\n---\r\n[[Old]]\r\nuntouched  \r\n')
        source.chmod(0o640)
        vault = Vault(self.root)
        vault.move('Old.md', 'New.md')
        self.assertEqual(source.read_bytes(), b'\xef\xbb\xbf---\r\ncustom: yes\r\n---\r\n[[New]]\r\nuntouched  \r\n')
        self.assertEqual(source.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list((self.root / '.trash/.librevault-moves').glob('*/manifest.json')), [])

    def test_rejects_overwrite_hidden_paths_symlinks_and_descendant_moves(self):
        self.write('Folder/Old.md', 'original')
        self.write('Existing.md', 'existing')
        (self.root / 'Linked').symlink_to(self.root / 'Folder', target_is_directory=True)
        vault = Vault(self.root)
        for source, destination in [('Folder/Old.md', 'Existing.md'), ('Folder/Old.md', '../Outside.md'),
                                    ('Folder/Old.md', '.hidden.md'), ('Folder/Old.md', 'Linked/New.md'),
                                    ('Folder', 'Folder/Child'), ('Folder/Old.md', 'Missing/New.md')]:
            with self.subTest(destination=destination), self.assertRaises(VaultError):
                vault.move(source, destination)
        self.assertEqual((self.root / 'Folder/Old.md').read_text(), 'original')

    def test_trash_keeps_files_in_vault_and_excludes_them_from_index(self):
        self.write('Folder/Note.md', 'recover me')
        self.write('Folder/photo.png', 'image')
        vault = Vault(self.root)
        target = vault.trash('Folder')
        self.assertEqual((target / 'Note.md').read_text(), 'recover me')
        self.assertEqual((target / 'photo.png').read_text(), 'image')
        self.assertEqual(target.parent, self.root / '.trash')
        self.assertEqual(vault.notes, {})
        self.assertEqual(vault.folders, [])

    def test_trash_rejects_external_symlink_directory(self):
        self.write('Note.md', 'keep')
        with TemporaryDirectory() as outside:
            (self.root / '.trash').symlink_to(outside, target_is_directory=True)
            vault = Vault(self.root)
            with self.assertRaises(VaultError):
                vault.trash('Note.md')
            self.assertTrue((self.root / 'Note.md').is_file())
            self.assertEqual(list(Path(outside).iterdir()), [])
