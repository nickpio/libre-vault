"""Pins which GFM extensions each shared Markdown parser reads."""
import unittest

from librevault import anchors, formatting, vault
from librevault.markdown_parsers import new_parser

try:
    from librevault import reading
    READING_AVAILABLE = True
except (ImportError, ValueError):
    READING_AVAILABLE = False


TABLE = '| a | b |\n| - | - |\n| 1 | 2 |\n'
STRIKE = '~~gone~~\n'


def _types(parser, text):
    return {token.type for token in parser.parse(text)}


def _inline_types(parser, text):
    return {child.type for token in parser.parse(text) for child in token.children or []}


class MarkdownParserTests(unittest.TestCase):
    def test_default_parser_has_no_raw_html_and_no_gfm_extensions(self):
        parser = new_parser()
        self.assertFalse(parser.options['html'])
        self.assertNotIn('table_open', _types(parser, TABLE))
        self.assertNotIn('s_open', _inline_types(parser, STRIKE))

    def test_index_reads_tables_and_editor_reads_neither_extension(self):
        self.assertIn('table_open', _types(vault.MARKDOWN, TABLE))
        self.assertNotIn('table_open', _types(formatting.MARKDOWN, TABLE))

    def test_index_does_not_link_a_wikilink_split_by_a_table_pipe(self):
        text = '| a | b |\n| - | - |\n| [[Note|Alias]] | x |\n'
        self.assertEqual(vault.links(text), [])
        self.assertEqual([link.target for link in vault.links('[[Note|Alias]]\n')], ['Note'])

    def test_anchor_parser_reads_tables_but_not_strikethrough(self):
        parser = anchors._parser()
        self.assertIn('table_open', _types(parser, TABLE))
        self.assertNotIn('s_open', _inline_types(parser, STRIKE))

    @unittest.skipUnless(READING_AVAILABLE, 'GTK is required for the reading view parser')
    def test_reading_parser_reads_tables_and_strikethrough(self):
        self.assertIn('table_open', _types(reading.MARKDOWN, TABLE))
        self.assertIn('s_open', _inline_types(reading.MARKDOWN, STRIKE))


if __name__ == '__main__':
    unittest.main()
