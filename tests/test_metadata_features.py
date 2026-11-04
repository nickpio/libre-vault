"""Boolean/scoped search, typed graph nodes, and source-safe tag operations."""
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.metadata import matches_query, note_metadata, query_terms, rename_tag
from librevault.graph import build_graph
from librevault.vault import Vault, VaultError


class AdvancedSearchTests(unittest.TestCase):
    text = ('---\nstatus: active\ntags: [work/project]\n---\n# First\nAlpha beta\n'
            'gamma\n\n- [ ] Ship release\n- [x] Write docs\n\n# Second\nDelta\n')

    def matches(self, query):
        return matches_query('Work/Plan.md', self.text, query_terms(query), note_metadata(self.text))

    def test_boolean_precedence_grouping_and_negation(self):
        for query in ('absent OR Alpha beta', '(absent OR Alpha) beta', '-(absent OR missing)',
                      '(tag:work OR tag:other) -property:status=done', 'Alpha AND beta',
                      'path:(Work OR Elsewhere)', 'match-case:(file:Plan)' ):
            with self.subTest(query=query):
                self.assertTrue(self.matches(query))
        for query in ('(Alpha OR beta) missing', '-(Alpha OR absent)', 'absent OR (Alpha missing)'):
            with self.subTest(query=query):
                self.assertFalse(self.matches(query))

    def test_regex_quotes_paths_and_property_spaces(self):
        for query in (r'/Alp\w+\s+beta/', 'content:/Alpha (beta|gamma)/', 'path:Work/Plan',
                      '"Alpha beta"', '[status: active]', 'path:"Work/Plan.md"'):
            with self.subTest(query=query):
                self.assertTrue(self.matches(query))
        self.assertFalse(self.matches('"/Alpha/"'))
        self.assertTrue(self.matches(r'/alpha/'))
        self.assertFalse(self.matches('match-case:/alpha/'))
        self.assertTrue(self.matches('match-case:(Alpha OR Missing)'))
        self.assertFalse(self.matches('match-case:alpha'))
        self.assertTrue(self.matches('ignore-case:ALPHA'))
        code = '```\n- [ ] Fake task\n```'
        self.assertFalse(matches_query('Code.md', code, query_terms('task:Fake'), note_metadata(code)))

    def test_content_scopes_require_terms_in_same_unit(self):
        for query in ('line:(Alpha beta)', 'block:(Alpha gamma)', 'section:(First gamma)',
                      'task:Ship', 'task-todo:(Ship release)', 'task-done:"Write docs"'):
            with self.subTest(query=query):
                self.assertTrue(self.matches(query))
        for query in ('line:(Alpha gamma)', 'block:(gamma Ship)', 'section:(First Delta)',
                      'task-done:Ship', 'task-todo:Write', 'line:Work/Plan'):
            with self.subTest(query=query):
                self.assertFalse(self.matches(query))

    def test_invalid_queries_are_safe_in_vault_and_snippets(self):
        with TemporaryDirectory() as directory:
            Path(directory, 'A.md').write_text(self.text)
            vault = Vault(directory)
            self.addCleanup(vault.close)
            for query in ('(', '"unclosed', '/[/', '/unclosed', 'Alpha OR', 'Alpha AND', ')', '[status'):
                with self.subTest(query=query):
                    self.assertTrue(query_terms(query).error)
                    self.assertEqual(vault.search(query), [])
            result = vault.search_results('/Alpha (beta|gamma)/ OR absent')[0]
            self.assertEqual(result.snippet, 'Alpha beta')
            self.assertEqual(result.highlights, ((0, 10),))
            self.assertEqual(vault.search_results('absent OR /(?i)alpha/')[0].highlights, ((0, 5),))
            self.assertEqual(vault.search_results('match-case:alpha'), [])
            # OR alternatives must never be ANDed by the trigram prefilter.
            self.assertEqual(vault.search('absent OR Alpha'), ['A.md'])


class ExtendedGraphTests(unittest.TestCase):
    def rows(self):
        return [('A.md', '[]', json.dumps([{'target': 'B'}, {'target': 'Missing'},
                                         {'target': 'image.png'}]), '#work/project Alpha'),
                ('B.md', '["Bee"]', json.dumps([{'target': 'A'}]), 'Beta')]

    def test_node_types_direction_and_full_filter(self):
        paths, edges = build_graph(self.rows(), tags=True, attachments=True, unresolved=True,
                                   directed=True, attachment_paths=['image.png'])
        pairs = {(paths[a], paths[b]) for a, b in edges}
        self.assertIn(('A.md', '#work/project'), pairs)
        self.assertIn(('A.md', 'ghost:Missing.md'), pairs)
        self.assertIn(('A.md', 'image.png'), pairs)
        self.assertIn(('A.md', 'B.md'), pairs)
        self.assertIn(('B.md', 'A.md'), pairs)
        paths, _ = build_graph(self.rows(), query='tag:work OR content:/bet./')
        self.assertEqual(paths, ['A.md', 'B.md'])
        paths, _ = build_graph(self.rows(), query='tag:work', tags=True, unresolved=True,
                               root='A.md', depth=1)
        self.assertEqual(paths, ['#work/project', 'A.md', 'ghost:Missing.md'])
        self.assertEqual(build_graph(self.rows(), query='file:image', attachments=True,
                                     attachment_paths=['image.png'])[0], ['image.png'])
        paths, _ = build_graph(self.rows())
        self.assertEqual(paths, ['A.md', 'B.md'])


class TagManagementTests(unittest.TestCase):
    def test_rename_preserves_comments_code_and_nested_tag_boundaries(self):
        text = ('---\n# Keep this comment\ntags: [work, work/project, worker]\n'
                'title: work\n---\n#work #work/project #worker `#work` \\#work\n'
                '[[#work]] [x](#work)\n```\n#work\n```\n')
        changed = rename_tag(text, 'work', 'office')
        self.assertIn('tags: [office, office/project, worker]', changed)
        self.assertIn('#office #office/project #worker `#work` \\#work', changed)
        self.assertIn('[[#work]] [x](#work)', changed)
        self.assertIn('```\n#work\n```', changed)
        self.assertIn('# Keep this comment', changed)
        self.assertIn('title: work', changed)
        self.assertEqual(rename_tag(changed, 'office', 'office'), changed)
        multiline = '---\ntags: >\n  work work/project\nstatus: active\n---\nBody'
        renamed = rename_tag(multiline, 'work', 'office')
        self.assertEqual(note_metadata(renamed).tags, ('office', 'office/project'))
        self.assertEqual(note_metadata(renamed).properties['status'], 'active')

    def test_bulk_rename_counts_and_index_refresh(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('---\ntags: "work, work/project"\n---\n#work\n')
            (root / 'B.md').write_text('#work/project\n#work/project\n')
            vault = Vault(root)
            self.addCleanup(vault.close)
            self.assertEqual(vault.tag_counts(), {'work': 2, 'work/project': 2})
            self.assertEqual(vault.rename_tag('#work', 'office'), ['A.md', 'B.md'])
            self.assertEqual(vault.search('tag:office'), ['A.md', 'B.md'])
            self.assertEqual(vault.search('tag:work'), [])
            self.assertEqual(vault.tag_counts(), {'office': 2, 'office/project': 2})
            for tag in ('', 'bad tag', 'a//b', '../outside', '42'):
                with self.assertRaises(ValueError):
                    vault.rename_tag('office', tag)

    def test_failed_tag_rename_rolls_back_preceding_edits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('#work\n')
            (root / 'B.md').write_text('#work\n')
            vault = Vault(root)
            self.addCleanup(vault.close)
            save = vault.save
            calls = []
            def fail_during_second_save(note, text):
                calls.append(note.path)
                manifests = list((root / '.trash/.librevault-tag-renames').glob('*/manifest.json'))
                self.assertEqual(len(manifests), 1, 'Backups must be complete before any mutation')
                if len(calls) == 2:
                    raise OSError('simulated write failure')
                return save(note, text)
            with patch.object(vault, 'save', side_effect=fail_during_second_save):
                with self.assertRaises(OSError):
                    vault.rename_tag('work', 'office')
            self.assertEqual((root / 'A.md').read_text(), '#work\n')
            self.assertEqual((root / 'B.md').read_text(), '#work\n')
            self.assertEqual(list((root / '.trash/.librevault-tag-renames').glob('*/manifest.json')), [])
            self.assertEqual(vault.search('tag:work'), ['A.md', 'B.md'])
            self.assertIn('rolled back', vault.organization_notice)

    def test_failed_tag_rollback_preserves_manifest_and_reports_partial_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('---\ntags: [work]\n---\n#work\n')
            (root / 'B.md').write_text('#work\n')
            vault = Vault(root)
            self.addCleanup(vault.close)
            save = vault.save
            calls = []
            def fail_during_second_save(note, text):
                calls.append(note.path)
                if len(calls) == 2:
                    (root / 'A.md').write_text('external intervening change')
                    raise OSError('simulated write failure')
                return save(note, text)
            with patch.object(vault, 'save', side_effect=fail_during_second_save):
                with self.assertRaisesRegex(VaultError, 'partial completion.*Original note bytes.*preserved') as raised:
                    vault.rename_tag('work', 'office')
            self.assertEqual((root / 'A.md').read_text(), 'external intervening change')
            self.assertEqual((root / 'B.md').read_text(), '#work\n')
            manifest = next((root / '.trash/.librevault-tag-renames').glob('*/manifest.json'))
            data = json.loads(manifest.read_text())
            self.assertEqual(data['operation'], 'tag-rename')
            self.assertEqual(data['old'], 'work')
            self.assertEqual(data['new'], 'office')
            backups = {note['path']: (manifest.parent / note['backup']).read_bytes() for note in data['notes']}
            self.assertEqual(backups['A.md'], b'---\ntags: [work]\n---\n#work\n')
            self.assertEqual(backups['B.md'], b'#work\n')
            self.assertIn('A.md', str(raised.exception))
