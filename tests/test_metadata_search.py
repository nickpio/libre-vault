"""Structured metadata, filter semantics, Unicode snippets and source preservation."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from librevault.metadata import note_metadata, property_mapping, property_text, with_properties
from librevault.vault import Vault


class MetadataTests(unittest.TestCase):
    def test_properties_aliases_and_nested_tags_ignore_code(self):
        text = ('---\naliases: [First, Second]\ntags:\n  - work/project\nstatus: active\ncount: 2\n---\n'
                '# Heading\n#inline #42 `#ignored` [[#Section]] [link](#fragment)\n```\n#ignored-too\n```\n')
        metadata = note_metadata(text)
        self.assertEqual(metadata.aliases, ('First', 'Second'))
        self.assertEqual(metadata.tags, ('work/project', 'inline'))
        self.assertEqual(metadata.properties['count'], 2)
        self.assertIsNone(metadata.error)

    def test_invalid_properties_are_reported_without_losing_content(self):
        text = '---\ninvalid: [\n---\nbody\n'
        self.assertTrue(note_metadata(text).error)
        with self.assertRaises(ValueError):
            with_properties(text, 'invalid: [')
        with self.assertRaises(ValueError):
            property_mapping('- list\n- items\n')
        self.assertEqual(property_text(text), 'invalid: [\n')

    def test_property_edit_preserves_body_newlines_and_original_yaml_comments(self):
        text = '---\r\ntags: [old]\r\n---\r\n# Body\r\n  untouched  \r\n'
        changed = with_properties(text, '# Comment\ntags: [new]\nnumber: 12\n')
        self.assertEqual(changed, '---\r\n# Comment\r\ntags: [new]\r\nnumber: 12\r\n---\r\n# Body\r\n  untouched  \r\n')
        self.assertEqual(with_properties(text, ''), '# Body\r\n  untouched  \r\n')


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / 'Work').mkdir()
        (root / 'Work/Plan.md').write_text('---\naliases: [Roadmap]\ntags: [work/project]\nstatus: active\n---\nCafé alpha beta\n')
        (root / 'Other.md').write_text('---\nstatus: done\n---\nAlpha gamma #personal\n')
        self.vault = Vault(root)

    def test_combines_filters_phrases_negation_and_properties(self):
        for query in ('path:Work tag:#work "alpha beta"', 'alias:roadmap -tag:personal',
                      '[status:active]', 'property:status=active', 'file:plan content:café',
                      'Roadmap -gamma'):
            with self.subTest(query=query):
                self.assertEqual(self.vault.search(query), ['Work/Plan.md'])
        self.assertEqual(self.vault.search('alpha -property:status=active'), ['Other.md'])
        self.assertEqual(self.vault.search('tag:absent'), [])

    def test_snippets_use_original_unicode_offsets_and_real_line_numbers(self):
        result = self.vault.search_results('CAFÉ alpha')[0]
        self.assertEqual(result.path, 'Work/Plan.md')
        self.assertEqual(result.snippet, 'Café alpha beta')
        self.assertEqual([result.snippet[start:end] for start, end in result.highlights], ['Café', 'alpha'])
        self.assertEqual(result.line, 6)
        self.assertEqual(self.vault.search_results('tag:work')[0].snippet, 'Café alpha beta')

    def test_alias_resolution_keeps_exact_paths_first_and_reports_ambiguity(self):
        self.assertEqual(self.vault.resolve('Other.md', 'Roadmap'), ['Work/Plan.md'])
        self.vault.create('Roadmap')
        self.assertEqual(self.vault.resolve('Other.md', 'Roadmap'), ['Roadmap.md'])
        note = self.vault.create('Duplicate')
        self.vault.save(note, '---\naliases: [Same]\n---\n')
        self.vault.save(self.vault.read('Other.md'), '---\naliases: [Same]\n---\n')
        self.assertEqual(self.vault.resolve('Work/Plan.md', 'Same'), ['Duplicate.md', 'Other.md'])
