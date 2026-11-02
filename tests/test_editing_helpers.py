"""Source transformations preserve Unicode, delimiters, and literal queries."""
import unittest

from librevault.editing import find_matches, format_inline, markdown_link
from librevault.history import Action, Change, Selection, merge_actions


class FormattingTests(unittest.TestCase):
    def transform(self, text, start, end, marker):
        edit, selection = format_inline(text, start, end, marker)
        return text[:edit.start] + edit.replacement + text[edit.end:], selection

    def test_wrap_trim_and_empty_selection(self):
        self.assertEqual(self.transform(' café ', 0, 6, '**'), (' **café** ', (3, 7)))
        self.assertEqual(self.transform('', 0, 0, '*'), ('**', (1, 1)))
        self.assertEqual(self.transform('   ', 0, 3, '**'), ('   ', (0, 3)))

    def test_unwrap_selected_delimiters_and_content(self):
        self.assertEqual(self.transform('**café**', 0, 8, '**'), ('café', (0, 4)))
        self.assertEqual(self.transform('**café**', 2, 6, '**'), ('café', (0, 4)))
        self.assertEqual(self.transform('***café***', 3, 7, '*'), ('**café**', (2, 6)))
        self.assertEqual(self.transform('***café***', 3, 7, '**'), ('*café*', (1, 5)))

    def test_italic_does_not_remove_existing_bold(self):
        self.assertEqual(self.transform('**word**', 2, 6, '*'), ('***word***', (3, 7)))
        self.assertEqual(self.transform('**word**', 0, 8, '*')[0], '***word***')

    def test_escaped_literal_delimiters_are_retained(self):
        self.assertEqual(self.transform(r'\*word*', 2, 6, '*')[0], r'\**word**')
        self.assertEqual(self.transform(r'\*word*', 1, 7, '*')[0], r'\**word**')

    def test_links_escape_labels_and_encode_destination_delimiters(self):
        self.assertEqual(markdown_link('A [label]', 'Folder/My note (1).md#Résumé'),
                         '[A \\[label\\]](Folder/My%20note%20%281%29.md#R%C3%A9sum%C3%A9)')
        self.assertEqual(markdown_link('', 'https://example.com?a=1&b=2'),
                         '[https://example.com?a=1&b=2](https://example.com?a=1&b=2)')
        self.assertEqual(markdown_link(r'\*literal\* and \[bracket\]', 'note.md'),
                         r'[\*literal\* and \[bracket\]](note.md)')


class SearchTests(unittest.TestCase):
    def test_literal_and_unicode_offsets(self):
        self.assertEqual(find_matches('🙂 café CAFÉ', 'CAFÉ'), [(2, 6), (7, 11)])
        self.assertEqual(find_matches('a.b [c] a*b', '.'), [(1, 2)])
        self.assertEqual(find_matches('a.b [c] a*b', '[c]'), [(4, 7)])
        self.assertEqual(find_matches('One one ONE', 'one', True), [(4, 7)])
        self.assertEqual(find_matches('anything', ''), [])


class GroupingTests(unittest.TestCase):
    def test_adjacent_typing_merges_but_not_after_cursor_movement(self):
        first = Action((Change(0, '', 'a'),), Selection(0, 0), Selection(1, 1))
        second = Action((Change(1, '', 'b'),), Selection(1, 1), Selection(2, 2))
        self.assertEqual(merge_actions(first, second, 'typing').changes, (Change(0, '', 'ab'),))
        moved = Action(second.changes, Selection(0, 0), second.after)
        self.assertIsNone(merge_actions(first, moved, 'typing'))

    def test_backward_deletion_keeps_removed_source_in_order(self):
        first = Action((Change(2, 'c', ''),), Selection(3, 3), Selection(2, 2))
        second = Action((Change(1, 'b', ''),), Selection(2, 2), Selection(1, 1))
        self.assertEqual(merge_actions(first, second, 'backspace').changes, (Change(1, 'bc', ''),))


if __name__ == '__main__':
    unittest.main()
