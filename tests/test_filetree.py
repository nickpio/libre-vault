"""The explorer keeps every note beneath its parent, including empty folders."""
import unittest

from librevault.filetree import tree_paths


class FileTreeTests(unittest.TestCase):
    def test_groups_folders_and_notes_by_parent(self):
        self.assertEqual(tree_paths(
            ['Root.md', 'Projects/Plan.md', 'Projects/Research/Notes.md', 'Archive/Old.md'],
            ['Projects/Research', 'Projects', 'Empty', 'Archive'], set()),
            ['Archive/', 'Archive/Old.md', 'Empty/', 'Projects/', 'Projects/Research/',
             'Projects/Research/Notes.md', 'Projects/Plan.md', 'Root.md'])

    def test_collapsing_a_folder_hides_only_its_descendants(self):
        self.assertEqual(tree_paths(
            ['Projects/Plan.md', 'Projects-old/Keep.md', 'Root.md'],
            ['Projects', 'Projects/Nested', 'Projects-old'], {'Projects'}),
            ['Projects/', 'Projects-old/', 'Projects-old/Keep.md', 'Root.md'])

    def test_root_attachment_storage_is_hidden_without_hiding_other_folders(self):
        self.assertEqual(tree_paths(
            ['attachments/Sub/Note.md', 'Projects/attachments/Note.md', 'Root.md'],
            ['attachments', 'attachments/Sub', 'Projects', 'Projects/attachments'], set()),
            ['Projects/', 'Projects/attachments/', 'Projects/attachments/Note.md', 'Root.md'])

    def test_empty_tree_and_case_insensitive_sibling_order(self):
        self.assertEqual(tree_paths([], [], set()), [])
        self.assertEqual(tree_paths(['Zebra.md', 'apple.md'], ['Zoo', 'alpha'], set()),
                         ['alpha/', 'Zoo/', 'apple.md', 'Zebra.md'])


if __name__ == '__main__':
    unittest.main()
