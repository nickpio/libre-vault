"""Live preview for tables, callouts, math, and wiki embeds, plus table edits."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from librevault.editing import TextEdit
from librevault.formatting import MarkdownLayout, markdown_layout
from librevault.tables import (
    find_tables, format_table, insert_table, table_add_column_left, table_add_column_right,
    table_add_row_above, table_add_row_below, table_align_column, table_at, table_delete_column,
    table_delete_row, table_from_model, table_move_column, table_move_row, table_next_cell,
    table_previous_cell, table_sort_column,
)

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from librevault.attachments import store_attachment
    from gi.repository import GdkPixbuf, Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


def applied(text, edit):
    updated = text[:edit.start] + edit.replacement + text[edit.end:]
    return updated, edit.caret


class TableEngineTests(unittest.TestCase):
    def test_insert_table_is_one_padded_edit_with_the_caret_in_the_header(self):
        edit = insert_table()
        self.assertIsInstance(edit, TextEdit)
        self.assertEqual(edit.start, 0)
        self.assertEqual(edit.end, 0)
        self.assertEqual(edit.replacement, (
            '| Column 1 | Column 2 |\n'
            '| :------- | :------- |\n'
            '|          |          |\n'
            '|          |          |'
        ))
        self.assertEqual(edit.replacement[edit.caret], 'C')
        narrower = insert_table(rows=1, cols=2)
        self.assertEqual(narrower.replacement.count('\n'), 2)

    def test_alignments_padding_and_source_range(self):
        text = 'Before\n| Name | Qty |\n| :--- | ---: |\n| Ada | 2 |\nAfter\n'
        table = table_at(text, text.index('Ada'))
        self.assertEqual(table.headers, ['Name', 'Qty'])
        self.assertEqual(table.alignments, ['left', 'right'])
        self.assertEqual(table.rows, [['Ada', '2']])
        self.assertEqual(text[table.start:table.end], '| Name | Qty |\n| :--- | ---: |\n| Ada | 2 |\n')
        edit = table_align_column(text, text.index('Qty'), 'center')
        updated, _caret = applied(text, edit)
        self.assertEqual(find_tables(updated)[0].alignments, ['left', 'center'])
        self.assertTrue(updated.startswith('Before\n'))
        self.assertTrue(updated.endswith('After\n'))
        self.assertIsNone(table_align_column(text, text.index('Qty'), 'justify'))
        self.assertIsNone(table_at(text, 0))

    def test_row_and_column_edits_keep_one_undoable_span(self):
        text = 'Before\n| A | B |\n| --- | --- |\n| 1 | 2 |\nAfter\n'
        cursor = text.index('1')
        added, caret = applied(text, table_add_row_below(text, cursor))
        self.assertIn('|      |      |', added)
        self.assertEqual(len(find_tables(added)[0].rows), 2)
        self.assertEqual(added[caret], ' ')
        above, _caret = applied(text, table_add_row_above(text, cursor))
        self.assertEqual(find_tables(above)[0].rows[0], ['', ''])
        self.assertEqual(find_tables(above)[0].rows[1][0], '1')
        deleted, _caret = applied(text, table_delete_row(text, cursor))
        self.assertEqual(find_tables(deleted)[0].rows, [])
        self.assertIsNone(table_delete_row(text, text.index('A')))
        column = text.index('| B |') + 2
        wider, _caret = applied(text, table_add_column_right(text, column))
        self.assertEqual(find_tables(wider)[0].headers[1:], ['B', 'Column 3'])
        left, _caret = applied(text, table_add_column_left(text, column))
        self.assertEqual(find_tables(left)[0].headers[:2], ['A', 'Column 3'])
        removed, _caret = applied(text, table_delete_column(text, column))
        self.assertNotIn('B', find_tables(removed)[0].headers)
        gone, _caret = applied(removed, table_delete_column(removed, removed.index('A')))
        self.assertEqual(gone, 'Before\nAfter\n')
        self.assertEqual(find_tables(gone), [])

    def test_sort_move_and_bounds(self):
        text = '| N | T |\n| ---: | :--- |\n| 10 | b |\n| 2 | a |\n|  | c |\n'
        ascending, _caret = applied(text, table_sort_column(text, text.index('10'), True))
        self.assertEqual(find_tables(ascending)[0].rows, [['2', 'a'], ['10', 'b'], ['', 'c']])
        descending, _caret = applied(text, table_sort_column(text, text.index('10'), False))
        self.assertEqual(find_tables(descending)[0].rows, [['10', 'b'], ['2', 'a'], ['', 'c']])
        words, _caret = applied(text, table_sort_column(text, text.index('b'), False))
        self.assertEqual([row[1] for row in find_tables(words)[0].rows], ['c', 'b', 'a'])
        moved, _caret = applied(text, table_move_row(text, text.index('10'), 1))
        self.assertEqual(find_tables(moved)[0].rows[0][0], '2')
        self.assertIsNone(table_move_row(text, text.index('10'), -1))
        self.assertIsNone(table_move_row(text, text.index('N'), 1))
        shifted, _caret = applied(text, table_move_column(text, text.index('N'), 1))
        self.assertEqual(find_tables(shifted)[0].headers, ['T', 'N'])
        self.assertIsNone(table_move_column(text, text.index('T'), 1))
        self.assertIsNone(table_sort_column('| A |\n| --- |\n', 0, True))

    def test_cell_navigation_moves_the_caret_and_appends_on_the_last_cell(self):
        text = '| A | B |\n| --- | --- |\n| 1 | 2 |\n'
        first = table_next_cell(text, text.index('1'))
        self.assertEqual(first.replacement, '')
        self.assertEqual(first.start, first.end)
        _same, caret = applied(text, first)
        self.assertEqual(text[caret], '2')
        previous = table_previous_cell(text, caret)
        self.assertEqual(applied(text, previous)[1], text.index('1'))
        appended = table_next_cell(text, text.index('2'))
        updated, caret = applied(text, appended)
        self.assertEqual(len(find_tables(updated)[0].rows), 2)
        self.assertGreater(caret, text.index('2'))
        stay = table_previous_cell(text, text.index('A'))
        self.assertEqual(stay.caret, text.index('A'))
        self.assertIsNone(table_next_cell('no table', 0))
        self.assertIsNone(table_previous_cell('no table', 0))

    def test_pipes_code_frontmatter_and_indented_lines_do_not_split_or_match(self):
        piped = '| a \\| b | c |\n| --- | --- |\n| 1 | 2 |\n'
        table = find_tables(piped)[0]
        self.assertEqual(table.headers, ['a \\| b', 'c'])
        edited, _caret = applied(piped, table_add_row_below(piped, piped.index('1')))
        self.assertEqual(find_tables(edited)[0].headers, ['a \\| b', 'c'])
        coded = '| `b|c` | d |\n| --- | --- |\n'
        self.assertEqual(find_tables(coded)[0].headers, ['`b|c`', 'd'])
        fenced = '```\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```\n'
        self.assertEqual(find_tables(fenced), [])
        matter = '---\n| A | B |\n| --- | --- |\n---\n| C | D |\n| --- | --- |\n| 3 | 4 |\n'
        found = find_tables(matter)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].headers, ['C', 'D'])
        indented = '    | A | B |\n    | --- | --- |\n    | 1 | 2 |\n'
        self.assertEqual(find_tables(indented), [])
        loose = 'A | B\n--- | ---\n1 | 2\n'
        self.assertEqual(find_tables(loose)[0].rows, [['1', '2']])
        replaced = table_from_model(piped, piped.index('1'), ['a | b', 'c'], ['right', 'left'], [['9', '8']])
        updated, _caret = applied(piped, replaced)
        self.assertIn('\\|', updated)
        self.assertEqual(find_tables(updated)[0].headers, ['a \\| b', 'c'])
        self.assertEqual(find_tables(updated)[0].rows, [['9', '8']])
        self.assertEqual(find_tables(updated)[0].alignments, ['right', 'left'])

    def test_formatter_uses_alignment_markers(self):
        rendered = format_table(['Name', 'Qty'], ['left', 'right'], [['Ada', '2']])
        self.assertEqual(rendered, '| Name |  Qty |\n| :--- | ---: |\n| Ada  |    2 |')
        centered = format_table(['Hi'], ['center'], [])
        self.assertIn(':---:', centered)


class LivePreviewLayoutTests(unittest.TestCase):
    def test_constructor_stays_compatible(self):
        layout = MarkdownLayout([], [], [], [])
        self.assertEqual(layout.embeds, [])
        self.assertEqual(layout.math, [])
        self.assertEqual(layout.tables, [])
        self.assertEqual(layout.callouts, [])

    def test_wiki_embeds_math_callouts_and_tables_keep_source_offsets(self):
        text = '\n'.join([
            'See [[Note]] and ![[inline]] here.',
            '![[Child|alias]]',
            '![[pic.PNG]]',
            'Energy $x^2$ grows, but $20 and $30 do not, nor does \\$x$.',
            '$$',
            '\\frac{a}{b}',
            '$$',
            'A sentence with $$y$$ in it.',
            '> [!WARNING]+ Careful',
            '> stay ready',
            '| H | I |',
            '| --- | :---: |',
            '| 1 | 2 |',
            '```',
            '$z$ and ![[Hidden]]',
            '| A | B |',
            '| --- | --- |',
            '```',
            '',
        ])
        layout = markdown_layout(text)
        embeds = {embed.target: embed for embed in layout.embeds}
        self.assertEqual(set(embeds), {'Child', 'pic.PNG'})
        self.assertEqual(embeds['Child'].kind, 'note')
        self.assertEqual(embeds['pic.PNG'].kind, 'image')
        self.assertEqual(text[embeds['Child'].start:embeds['Child'].end], '![[Child|alias]]')
        self.assertNotIn('inline', embeds)
        expressions = [(item.expression, item.display) for item in layout.math]
        self.assertIn(('x^2', False), expressions)
        self.assertIn(('\\frac{a}{b}', True), expressions)
        self.assertIn(('y', True), expressions)
        self.assertNotIn(('20 and ', False), expressions)
        self.assertFalse(any('z' == item.expression for item in layout.math))
        self.assertEqual(layout.callouts[0].kind, 'WARNING')
        self.assertEqual(layout.callouts[0].title, 'Careful')
        self.assertEqual(text[layout.callouts[0].header_start:layout.callouts[0].header_end],
                         '> [!WARNING]+ Careful')
        kinds = {text[span.start:span.end] for span in layout.spans if span.style == 'callout'}
        self.assertIn('WARNING', kinds)
        self.assertIn('Careful', kinds)
        syntax = [text[span.start:span.end] for span in layout.spans if span.style == 'syntax']
        self.assertTrue(any('[!' in piece for piece in syntax))
        self.assertEqual(layout.tables[0].headers, ['H', 'I'])
        self.assertEqual(layout.tables[0].alignments, ['left', 'center'])
        self.assertTrue(any(span.style == 'table' for span in layout.spans))
        self.assertEqual([span.style for span in layout.spans if text[span.start:span.end] == 'x^2'], ['math'])
        for span in layout.spans:
            self.assertTrue(0 <= span.start < span.end <= len(text))

    def test_incomplete_constructs_stay_inside_the_source(self):
        for text in ('$', '$$', '$20 and $30', '\\$x$', '> [!NOTE]', '|', '![[', '```\n$x$\n',
                     '---\n$x$\n---\n'):
            with self.subTest(text=text):
                layout = markdown_layout(text)
                self.assertEqual(layout.math, [])
                for span in layout.spans:
                    self.assertTrue(0 <= span.start <= span.end <= len(text))

    def test_emphasis_still_uses_source_offsets_beside_a_table(self):
        text = '**bold**\n\n| A |\n| --- |\n| 1 |\n'
        styled = [text[span.start:span.end] for span in markdown_layout(text).spans if span.style == 'strong']
        self.assertEqual(styled, ['bold'])


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class LivePreviewTableGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.LivePreviewTableTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Note\n', encoding='utf-8')
        (self.root / 'Child.md').write_text('Saved excerpt line\n', encoding='utf-8')
        self.window = Window(self.app, self.root,
                             preferences_path=Path(self.directory.name) / 'settings.json')
        self.addCleanup(self.window.destroy)
        self.window.open_note('Note.md')
        self.pane = self.window.current()
        self.drain()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def show(self, text, cursor=0):
        self.pane.buffer.set_text(text)
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(cursor))
        self.pane.history.reset()
        self.pane.render()
        self.drain()

    def labels(self, widget):
        found = []
        if isinstance(widget, Gtk.Label):
            found.append(widget)
        for child in widget.get_children() if hasattr(widget, 'get_children') else []:
            found.extend(self.labels(child))
        return found

    def menu_item(self, menu, label):
        return next(child for child in menu.get_children()
                    if hasattr(child, 'get_label') and child.get_label() == label)

    def test_table_widget_hides_source_until_the_cursor_enters(self):
        text = 'Before\n| Name | Qty |\n| :--- | ---: |\n| Ada | 2 |\nAfter\n'
        self.show(text, 0)
        self.assertEqual(len(self.pane.table_widgets), 1)
        start, end, widget, tag = self.pane.table_widgets[0]
        self.assertTrue(widget.get_visible())
        self.assertIn('live-table', widget.get_style_context().list_classes())
        shown = {label.get_text(): label.get_xalign() for label in self.labels(widget)}
        self.assertEqual(shown['Qty'], 1)
        self.assertEqual(shown['Ada'], 0)
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(tag))
        body = text.index('Ada')
        self.assertFalse(self.pane.buffer.get_iter_at_offset(body).has_tag(tag))
        self.assertEqual(self.pane.text(), text)
        self.show(text, body)
        self.assertFalse(widget.get_visible())
        self.assertFalse(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.pane.set_mode('source')
        self.assertFalse(widget.get_visible())
        self.assertEqual(self.pane.text(), text)

    def test_long_tables_stop_after_twelve_rows(self):
        rows = '\n'.join(f'| {index} |' for index in range(14))
        text = f'Intro\n| H |\n| --- |\n{rows}\n'
        self.show(text, 0)
        labels = [label.get_text() for label in self.labels(self.pane.table_widgets[0][2])]
        self.assertIn('2 more rows', labels)
        self.assertNotIn('13', labels)

    def test_wiki_image_and_note_embeds_preview_off_the_cursor_line(self):
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 24, 12)
        image.fill(0xff0000ff)
        _status, data = image.save_to_bufferv('png', [], [])
        relative = store_attachment(self.window.vault, 'image.png', data)
        self.window.vault.refresh()
        text = f'Before\n![[{relative}]]\n![[Child]]\nAfter\n'
        self.show(text, 0)
        images = [widget for _start, _end, widget, _tag in self.pane.embed_widgets
                  if isinstance(widget, Gtk.Image)]
        frames = [widget for _start, _end, widget, _tag in self.pane.embed_widgets
                  if not isinstance(widget, Gtk.Image)]
        self.assertEqual(len(images), 1)
        self.assertEqual(len(frames), 1)
        self.assertTrue(images[0].get_visible())
        card = [label.get_text() for label in self.labels(frames[0])]
        self.assertIn('Child', card)
        self.assertIn('Saved excerpt line', card)
        note = next(embed for embed in self.pane.embed_widgets if embed[2] is frames[0])
        self.assertTrue(self.pane.buffer.get_iter_at_offset(note[0]).has_tag(self.pane.hidden))
        self.show(text, text.index('![[Child]]') + 2)
        self.assertFalse(frames[0].get_visible())
        self.assertFalse(self.pane.buffer.get_iter_at_offset(note[0]).has_tag(self.pane.hidden))
        self.assertEqual(self.pane.text(), text)

    def test_callout_widget_reveals_the_header_on_its_block(self):
        text = 'Intro\n> [!NOTE] Title\n> body\n'
        self.show(text, 0)
        self.assertEqual(len(self.pane.callout_widgets), 1)
        start, _end, widget, _tag = self.pane.callout_widgets[0]
        self.assertTrue(widget.get_visible())
        self.assertIn('callout-note', widget.get_style_context().list_classes())
        shown = [label.get_text() for label in self.labels(widget)]
        self.assertIn('NOTE', shown)
        self.assertIn('Title', shown)
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        body = text.index('body')
        self.assertFalse(self.pane.buffer.get_iter_at_offset(body).has_tag(self.pane.hidden))
        self.show(text, text.index('[!NOTE]'))
        self.assertFalse(widget.get_visible())
        self.assertFalse(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.assertIn('[!NOTE]', self.pane.text())

    def test_inline_math_hides_delimiters_and_display_math_uses_a_widget(self):
        inline = 'Energy $x^2$ grows'
        self.show(inline, 0)
        start = inline.index('$')
        end = inline.index('^')
        closer = inline.rindex('$')
        self.assertEqual(self.pane.math_widgets, [])
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.assertFalse(self.pane.buffer.get_iter_at_offset(end).has_tag(self.pane.hidden))
        self.assertTrue(self.pane.buffer.get_iter_at_offset(end).has_tag(self.pane.tags['math']))
        self.show(inline, start + 1)
        self.assertFalse(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.assertFalse(self.pane.buffer.get_iter_at_offset(closer).has_tag(self.pane.hidden))
        block = 'Before\n$$\n\\frac{a}{b}\n$$\nAfter\n'
        self.show(block, 0)
        self.assertEqual(len(self.pane.math_widgets), 1)
        start, end, widget, _tag = self.pane.math_widgets[0]
        self.assertTrue(widget.get_visible())
        self.assertIsInstance(widget, Gtk.Image)
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.show(block, block.index('frac'))
        self.assertFalse(widget.get_visible())
        self.assertFalse(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.assertEqual(self.pane.text(), block)

    def test_context_menu_and_palette_expose_table_commands(self):
        menu = Gtk.Menu()
        self.addCleanup(menu.destroy)
        self.pane.populate_edit_menu(self.pane.editor, menu)
        self.assertEqual(menu.get_children()[0].get_label(), 'Undo')
        self.assertTrue(self.menu_item(menu, 'Insert table…').get_sensitive())
        self.assertFalse(self.menu_item(menu, 'Edit table…').get_sensitive())
        text = '| A | B |\n| --- | --- |\n| 1 | 2 |\n'
        self.show(text, text.index('1'))
        armed = Gtk.Menu()
        self.addCleanup(armed.destroy)
        self.pane.populate_edit_menu(self.pane.editor, armed)
        self.assertTrue(self.menu_item(armed, 'Edit table…').get_sensitive())
        self.menu_item(armed, 'Add row below').activate()
        self.drain()
        self.assertEqual(len(find_tables(self.pane.text())[0].rows), 2)
        self.pane.undo()
        self.assertEqual(self.pane.text(), text)
        self.window.show_command_palette()
        palette = self.window.palette
        titles = [row.title for row in palette.results.get_children()]
        self.assertEqual(titles[0], 'Open keybind cheatsheet')
        self.assertIn('Insert table…', titles)
        self.assertIn('Edit table…', titles)
        palette.search.set_text('edit table')
        self.assertEqual(palette.results.get_selected_row().title, 'Edit table…')

        def cancel():
            dialog = next(window for window in Gtk.Window.list_toplevels()
                          if isinstance(window, Gtk.Dialog) and window.get_title() == 'Edit table')
            dialog.response(Gtk.ResponseType.CANCEL)
            return False

        GLib.idle_add(cancel)
        palette.search.emit('activate')
        self.assertEqual(self.pane.text(), text)

    def test_tab_navigates_cells_and_appends_a_row_as_one_undo(self):
        text = '| A | B |\n| --- | --- |\n| 1 | 2 |\n'
        self.show(text, text.index('1'))
        self.assertTrue(self.key(Gdk.KEY_Tab))
        self.assertEqual(self.pane.text(), text)
        self.assertEqual(self.pane.text()[self.pane.cursor_offset()], '2')
        self.assertFalse(self.pane.history.can_undo)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state = Gdk.KEY_Tab, Gdk.ModifierType.SHIFT_MASK
        self.assertTrue(self.pane.editor_key_press(self.pane.editor, event))
        self.assertEqual(self.pane.text()[self.pane.cursor_offset()], '1')
        self.show(text, text.index('2'))
        self.assertTrue(self.key(Gdk.KEY_ISO_Left_Tab))
        self.assertEqual(self.pane.text()[self.pane.cursor_offset()], '1')
        self.show(text, text.index('2'))
        self.assertTrue(self.key(Gdk.KEY_Tab))
        self.assertEqual(len(find_tables(self.pane.text())[0].rows), 2)
        self.pane.undo()
        self.assertEqual(self.pane.text(), text)
        self.show('plain', 0)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state = Gdk.KEY_Tab, 0
        self.assertFalse(self.pane.editor_key_press(self.pane.editor, event))
        self.show(text, text.index('1'))
        self.pane.buffer.select_range(self.pane.buffer.get_iter_at_offset(text.index('1') + 1),
                                      self.pane.buffer.get_iter_at_offset(text.index('1')))
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state = Gdk.KEY_Tab, 0
        self.assertFalse(self.pane.editor_key_press(self.pane.editor, event))

    def test_table_editor_dialog_edits_and_inserts_in_one_undo(self):
        text = 'Before\n| A | B |\n| --- | --- |\n| 1 | 2 |\nAfter\n'
        self.show(text, text.index('1'))

        def edit():
            dialog = next(window for window in Gtk.Window.list_toplevels()
                          if isinstance(window, Gtk.Dialog) and window.get_title() == 'Edit table')
            cell = next(entry for entry in self.entries(dialog)
                        if entry.get_accessible().get_name() == 'Row 1 column 1')
            cell.set_text('9')
            dialog.align_choices[1].set_active_id('right')
            dialog.response(Gtk.ResponseType.OK)
            return False

        GLib.idle_add(edit)
        self.pane.prompt_edit_table()
        updated = self.pane.text()
        self.assertIn('9', updated)
        self.assertIn('---:', updated)
        self.assertTrue(updated.startswith('Before\n'))
        self.assertTrue(updated.endswith('After\n'))
        self.pane.undo()
        self.assertEqual(self.pane.text(), text)
        self.show('Hello world', 5)

        def insert():
            dialog = next(window for window in Gtk.Window.list_toplevels()
                          if isinstance(window, Gtk.Dialog) and window.get_title() == 'Insert table')
            dialog.add_row()
            dialog.response(Gtk.ResponseType.OK)
            return False

        GLib.idle_add(insert)
        self.window.insert_table()
        inserted = self.pane.text()
        self.assertTrue(inserted.startswith('Hello\n| Column 1 | Column 2 |\n'))
        self.assertIn('\n world', inserted)
        self.assertEqual(find_tables(inserted)[0].rows, [['', ''], ['', ''], ['', '']])
        self.assertEqual(inserted[self.pane.cursor_offset()], 'C')
        self.pane.undo()
        self.assertEqual(self.pane.text(), 'Hello world')

    def entries(self, dialog):
        found = []
        content = dialog.get_content_area()
        stack = list(content.get_children())
        while stack:
            widget = stack.pop()
            if isinstance(widget, Gtk.Entry):
                found.append(widget)
            if hasattr(widget, 'get_children'):
                stack.extend(widget.get_children())
        return found

    def key(self, keyval, modifiers=0):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state = keyval, modifiers
        return self.pane.editor.emit('key-press-event', event)


if __name__ == '__main__':
    unittest.main()
