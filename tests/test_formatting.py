"""Source offsets must always refer to the original Markdown."""
import unittest

from librevault.formatting import markdown_layout


class FormattingTests(unittest.TestCase):
    def styled(self, text, style):
        return [text[span.start:span.end] for span in markdown_layout(text).spans if span.style == style]

    def test_unicode_and_nested_emphasis_offsets(self):
        text = 'Café **bold *nested* end**.'
        self.assertEqual(self.styled(text, 'strong'), ['bold *nested* end'])
        self.assertEqual(self.styled(text, 'em'), ['nested'])

    def test_link_alias_preserves_source_offsets(self):
        text = 'Before [[Folder/Note|Label]] after'
        self.assertEqual(self.styled(text, 'link'), ['Label'])
        self.assertEqual(self.styled(text, 'syntax'), ['[[Folder/Note|', ']]'])

    def test_frontmatter_and_fences_are_literal(self):
        text = '---\nvalue: "**metadata**"\n---\n```\n**literal**\n```\n'
        self.assertEqual(self.styled(text, 'strong'), [])
        self.assertEqual(self.styled(text, 'metadata'), ['---\nvalue: "**metadata**"\n---\n'])
        self.assertEqual(self.styled(text, 'code'), ['```\n**literal**\n```\n'])

    def test_lists_track_nesting_numbering_and_task_state(self):
        text = '3. parent\n   - [X] nested\n4. next\n'
        items = markdown_layout(text).items
        self.assertEqual([item.depth for item in items], [0, 1, 0])
        self.assertEqual([item.number for item in items], [3, None, 4])
        self.assertEqual(text[items[1].task_offset], 'X')
        self.assertTrue(items[1].checked)
        self.assertEqual(text[items[1].content_start:items[1].end], 'nested\n')

    def test_escaped_and_code_markers_do_not_create_lists(self):
        for text in ('\\- escaped\n', '```\n- literal\n```\n', '    - code\n', '---\n'):
            with self.subTest(text=text):
                self.assertEqual(markdown_layout(text).items, [])

    def test_incomplete_markdown_stays_within_source_bounds(self):
        for text in ('', '*', '**unfinished', '[[unfinished', '[label](', '```\ncode', '- ['):
            with self.subTest(text=text):
                layout = markdown_layout(text)
                for span in layout.spans:
                    self.assertTrue(0 <= span.start < span.end <= len(text), span)


if __name__ == '__main__':
    unittest.main()
