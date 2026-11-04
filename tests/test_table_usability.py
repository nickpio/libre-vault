"""Table editing regressions, including bounded widgets for large tables."""
from types import SimpleNamespace
import unittest

from librevault.tables import TableEditorDialog, find_tables, format_table, table_from_model, table_break_after_row

try:
    from gi.repository import Gdk, Gtk
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class TableSerializationTests(unittest.TestCase):
    def test_pasted_paragraphs_stay_inside_one_cell(self):
        source = 'Before\n\n| A | B |\n| --- | --- |\n| x | y |\n\nAfter\n'
        edit = table_from_model(source, source.index('x'), ['A', 'B'], ['left'] * 2,
                                [['one\r\n\r\ntwo | three', 'four\rfive']])
        result = source[:edit.start] + edit.replacement + source[edit.end:]
        self.assertTrue(result.startswith('Before\n\n'))
        self.assertTrue(result.endswith('\n\nAfter\n'))
        self.assertEqual(find_tables(result)[0].rows,
                         [['one<br><br>two \\| three', 'four<br>five']])
        self.assertEqual(len(find_tables(result)), 1)

    def test_enter_keeps_the_entire_current_row(self):
        text = '| Name | Status |\n| --- | --- |\n| Alpha | In progress |\n| Beta | Done |\n'
        expected = text.replace('| Beta', '\n| Beta')
        for cursor in range(text.index('| Alpha'), text.index('| Beta')):
            edit = table_break_after_row(text, cursor)
            result = text[:edit.start] + edit.replacement + text[edit.end:]
            self.assertEqual(result, expected)
            self.assertEqual(find_tables(result)[0].rows, [['Alpha', 'In progress']])
            self.assertEqual(result[edit.caret:], '\n| Beta | Done |\n')

    def test_enter_at_end_and_in_header_preserves_table_structure(self):
        text = '| Name |\n| --- |\n| Alpha |'
        for cursor in (text.index('Alpha') + 2, len(text)):
            edit = table_break_after_row(text, cursor)
            self.assertEqual(text[:edit.start] + edit.replacement, text + '\n\n')
            self.assertEqual(edit.caret, len(text) + 1)
        for cursor in (text.index('Name'), text.index('---')):
            edit = table_break_after_row(text, cursor)
            result = text[:edit.start] + edit.replacement + text[edit.end:]
            self.assertEqual(result, '| Name |\n| --- |\n\n| Alpha |')
            self.assertEqual(find_tables(result)[0].headers, ['Name'])
        self.assertIsNone(table_break_after_row('ordinary text', 3))
        self.assertIsNone(table_break_after_row('```\n' + text + '\n```', 10))


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display unavailable')
class TableDialogTests(unittest.TestCase):
    def setUp(self):
        self.dialog = TableEditorDialog(None, ['Name', 'Value'], ['left', 'right'],
                                        [[str(i), str(i * 2)] for i in range(1000)])
        self.dialog.show_all()

    def tearDown(self):
        self.dialog.destroy()

    def test_pages_preserve_edits_and_bound_widget_count(self):
        d = self.dialog
        self.assertEqual(len(d.entries), 51)
        d.entries[1][0].set_text('changed first')
        d.change_page(1)
        self.assertEqual(d.entries[1][0].get_text(), '50')
        d.entries[1][0].set_text('changed second page')
        d.change_page(-1)
        self.assertEqual(d.entries[1][0].get_text(), 'changed first')
        rows = d.model()[2]
        self.assertEqual(rows[50][0], 'changed second page')
        self.assertEqual(rows[999], ['999', '1998'])
        self.assertEqual(len(find_tables(d.markdown())[0].rows), 1000)

    def test_navigation_crosses_pages_and_appends_only_at_end(self):
        d = self.dialog
        event = SimpleNamespace(keyval=Gdk.KEY_Tab, state=0)
        d.navigate_cell(d.entries[-1][-1], event, 50, 1)
        self.assertEqual(d.page_start, 50)
        self.assertEqual(d.focused_cell(), (51, 0))
        d.page_start = 950
        d._rebuild()
        d.navigate_cell(d.entries[-1][-1], event, 1000, 1)
        self.assertEqual(d.page_start, 1000)
        self.assertEqual(d.model()[2][-1], ['', ''])
        self.assertEqual(len(d.model()[2]), 1001)

    def test_actions_keep_selected_cell_when_toolbar_has_focus(self):
        d = self.dialog
        d.change_page(1)
        d.select_cell(None, None, 52, 1)
        d.next_page.grab_focus()
        d.remove_row()
        self.assertEqual(d.model()[2][51], ['52', '104'])
        d.move_column(-1)
        self.assertEqual(d.model()[0], ['Value', 'Name'])

    def test_width_change_preserves_text_and_survives_page_changes(self):
        d = self.dialog
        spin = Gtk.SpinButton.new_with_range(8, 80, 2)
        spin.set_value(32)
        d.resize_column(spin, 0)
        self.assertEqual(d.entries[1][0].get_width_chars(), 32)
        d.change_page(1)
        self.assertEqual(d.entries[1][0].get_width_chars(), 32)
        self.assertEqual(d.model()[2][50], ['50', '100'])
        spin.destroy()

    def test_selection_and_row_actions_identify_their_target(self):
        d = self.dialog
        d.focus_cell(0, 0)
        self.assertFalse(d.action_buttons['Remove row'].get_sensitive())
        d.focus_cell(2, 1)
        self.assertIn('Row 2, Column 2', d.summary.get_text())
        self.assertIn('table-editor-selected', d.entries[2][1].get_style_context().list_classes())
        self.assertEqual(d.row_labels[2].get_text(), '2')
        self.assertTrue(d.action_buttons['Move up'].get_sensitive())
        d.action_buttons['Move up'].clicked()
        self.assertEqual(d.model()[2][0], ['1', '2'])
        self.assertEqual(d.focused_cell(), (1, 1))
        self.assertFalse(d.action_buttons['Move up'].get_sensitive())

    def test_column_menu_updates_alignment_and_selected_column(self):
        d = self.dialog
        d.focus_cell(1, 0)
        d.column_buttons[1].set_active(True)
        self.assertEqual(d.focused_cell(), (1, 1))
        d.align_choices[1].set_active_id('center')
        self.assertEqual(d.entries[1][1].get_alignment(), 0.5)
        self.assertEqual(d.model()[1][1], 'center')
        d.column_buttons[1].set_active(False)
        d.change_page(1)
        self.assertEqual(d.row_labels[51].get_text(), '51')
        self.assertEqual(d.entries[1][1].get_alignment(), 0.5)

    def test_small_table_hides_unnecessary_paging_controls(self):
        d = TableEditorDialog(None, ['A'], ['left'], [['1'], ['2']])
        try:
            d.show_all()
            self.assertFalse(d.paging.get_visible())
            self.assertIn('table-editor-header', d.entries[0][0].get_style_context().list_classes())
            self.assertEqual(d.row_labels[0].get_text(), 'Header')
        finally:
            d.destroy()

    def test_dialog_fits_a_narrow_window(self):
        self.dialog.resize(400, 600)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        minimum, natural = self.dialog.get_preferred_width()
        self.assertLessEqual(minimum, 400)

    def test_preview_wraps_long_cells_and_opens_native_editor(self):
        from librevault.editor_render import EditorRenderMixin
        pane = EditorRenderMixin()
        pane.buffer = Gtk.TextBuffer()
        pane.editor = Gtk.TextView(buffer=pane.buffer)
        self.addCleanup(pane.editor.destroy)
        source = 'Intro\n' + format_table(['A'], ['left'], [['x' * 500]])
        pane.buffer.set_text(source)
        opened = []
        pane.prompt_edit_table = lambda: opened.append(
            pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset())
        table = find_tables(source)[0]
        grid = pane.table_preview(table)
        labels = [child for child in grid.get_children() if isinstance(child, Gtk.Label)]
        self.assertTrue(all(label.get_line_wrap() for label in labels))
        self.assertTrue(all(label.get_max_width_chars() == 32 for label in labels))
        button = next(child for child in grid.get_children() if isinstance(child, Gtk.Button))
        button.clicked()
        self.assertEqual(opened, [table.start])
        self.assertEqual(pane.buffer.get_text(*pane.buffer.get_bounds(), True), source)
        grid.destroy()

    def test_row_move_across_page_boundary_and_delete_last_page(self):
        d = self.dialog
        d.select_cell(None, None, 50, 0)
        d.move_row(1)
        self.assertEqual(d.page_start, 50)
        self.assertEqual(d.entries[1][0].get_text(), '49')
        self.assertEqual(d.model()[2][49][0], '50')
        d.page_start = 950
        d._selected = (1000, 0)
        d._rebuild()
        for _ in range(50):
            d.remove_row()
        self.assertEqual(d.page_start, 900)
        self.assertEqual(len(d.model()[2]), 950)


class TablePaddingTests(unittest.TestCase):
    def test_long_cell_does_not_pad_every_row_to_its_length(self):
        long = 'x' * 10000
        rendered = format_table(['A'], ['left'], [[long], *[['short'] for _ in range(1000)]])
        self.assertLess(len(rendered), 100000)
        self.assertEqual(find_tables(rendered)[0].rows[0][0], long)
