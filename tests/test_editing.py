"""Keyboard actions assert both final source and caret position."""
import unittest

from librevault.editing import list_enter


class ListEnterTests(unittest.TestCase):
    def apply_enter(self, text, cursor=None):
        cursor = len(text) if cursor is None else cursor
        edit = list_enter(text, cursor)
        self.assertIsNotNone(edit)
        return text[:edit.start] + edit.replacement + text[edit.end:], edit.start + len(edit.replacement)

    def test_continues_bullet_styles_and_nested_indent(self):
        for text, expected in (('- item', '- item\n- '), ('+ item', '+ item\n+ '), ('- parent\n  * child', '- parent\n  * child\n  * ')):
            with self.subTest(text=text):
                self.assertEqual(self.apply_enter(text), (expected, len(expected)))

    def test_numbered_items_advance_and_tasks_start_unchecked(self):
        for text, expected in (('9) item', '9) item\n10) '), ('- [x] done', '- [x] done\n- [ ] ')):
            with self.subTest(text=text):
                self.assertEqual(self.apply_enter(text), (expected, len(expected)))

    def test_enter_in_middle_splits_at_cursor(self):
        self.assertEqual(self.apply_enter('- first second', 7), ('- first\n-  second', 10))

    def test_empty_item_exits_with_separator(self):
        self.assertEqual(self.apply_enter('- previous\n- '), ('- previous\n\n', 12))
        self.assertEqual(self.apply_enter('- [ ] '), ('\n', 1))

    def test_consecutive_enters_use_latest_source(self):
        text, cursor = self.apply_enter('- item')
        self.assertEqual(self.apply_enter(text, cursor), ('- item\n\n', 8))

    def test_literal_code_frontmatter_and_prose_use_standard_enter(self):
        for text in ('plain prose', '```\n- literal', '---\nvalue: - item\n---', '`- inline`'):
            with self.subTest(text=text):
                self.assertIsNone(list_enter(text, len(text)))


if __name__ == '__main__':
    unittest.main()
