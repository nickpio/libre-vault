"""Table editor dialog: edit headers, cells, alignment, order, and shape, then apply once.

Kept apart from ``tables.py`` so the Markdown table logic stays importable without GTK.
"""
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gtk

from .tables import default_table, format_table, sort_rows


class TableEditorDialog(Gtk.Dialog):
    """Edit headers, cells, alignment, order, and shape, then apply once."""

    PAGE_SIZE = 50

    def __init__(self, parent, headers=None, alignments=None, rows=None, title=None):
        if headers is None:
            headers, alignments, rows = default_table()
        editing = title == 'Edit table' or title is None and any(any(cell for cell in row) for row in rows)
        super().__init__(title=title or ('Edit table' if editing else 'Insert table'),
                         transient_for=parent, modal=True)
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.add_button('Apply', Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(780, 520)
        self.get_style_context().add_class('table-editor')
        self.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class('suggested-action')
        self.entries = []
        self.align_choices = []
        self.page_start = 0
        self._selected = (0, 0)
        self._headers = [str(cell) for cell in headers]
        self._alignments = list(alignments or ['left'] * len(self._headers))
        self._rows = [[str(cell) for cell in row] for row in rows]
        self._column_widths = [18] * len(self._headers)
        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        self.summary = Gtk.Label(xalign=0)
        self.summary.set_line_wrap(True)
        self.summary.get_style_context().add_class('table-editor-summary')
        content.pack_start(self.summary, False, False, 0)
        tools = Gtk.FlowBox()
        tools.set_selection_mode(Gtk.SelectionMode.NONE)
        tools.set_min_children_per_line(1)
        tools.set_max_children_per_line(4)
        content.pack_start(tools, False, False, 0)
        self.action_buttons = {}
        for label, action in (('Add row', self.add_row), ('Add column', self.add_column)):
            button = Gtk.Button(label=label)
            button.connect('clicked', lambda _, action=action: action())
            tools.add(button)
        for title, actions in (
            ('Row actions', (('Move up', lambda: self.move_row(-1)),
                             ('Move down', lambda: self.move_row(1)),
                             ('Remove row', self.remove_row))),
            ('Column actions', (('Move left', lambda: self.move_column(-1)),
                                ('Move right', lambda: self.move_column(1)),
                                ('Sort ascending', lambda: self.sort(True)),
                                ('Sort descending', lambda: self.sort(False)),
                                ('Remove column', self.remove_column))),
        ):
            menu = Gtk.MenuButton(label=title + ' ▾')
            popover = Gtk.Popover.new(menu)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_border_width(8)
            for label, action in actions:
                button = Gtk.Button(label=label)
                button.connect('clicked', self.run_action, popover, action)
                box.pack_start(button, False, False, 0)
                self.action_buttons[label] = button
            popover.add(box)
            box.show_all()
            menu.set_popover(popover)
            tools.add(menu)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        content.pack_start(self.scroll, True, True, 0)
        self.scroll.set_shadow_type(Gtk.ShadowType.IN)
        self.grid = Gtk.Grid(column_spacing=0, row_spacing=0)
        self.grid.set_valign(Gtk.Align.START)
        self.grid.set_hexpand(True)
        self.grid.get_style_context().add_class('table-editor-grid')
        self.scroll.add(self.grid)
        paging = Gtk.Box(spacing=6)
        self.previous_page = Gtk.Button(label='Previous rows')
        self.next_page = Gtk.Button(label='Next rows')
        self.page_label = Gtk.Label()
        self.previous_page.connect('clicked', lambda _: self.change_page(-1))
        self.next_page.connect('clicked', lambda _: self.change_page(1))
        for widget in (self.previous_page, self.page_label, self.next_page):
            paging.pack_start(widget, False, False, 0)
        content.pack_start(paging, False, False, 0)
        self.paging = paging
        paging.show_all()
        paging.set_no_show_all(True)
        hint = Gtk.Label(label='Tab: next cell · Enter: next row\nColumn menus: alignment and editing width. Apply saves your changes.', xalign=0)
        hint.set_line_wrap(True)
        content.pack_start(hint, False, False, 0)
        self._rebuild()

    def run_action(self, button, popover, action):
        popover.popdown()
        action()

    def change_page(self, delta):
        self._capture()
        self.page_start = max(0, min(self.page_start + delta * self.PAGE_SIZE,
                                    max(0, (len(self._rows) - 1) // self.PAGE_SIZE * self.PAGE_SIZE)))
        self._selected = (self.page_start + 1, self._selected[1])
        self._rebuild()

    def _capture(self):
        if not self.entries:
            return
        self._headers = [entry.get_text() for entry in self.entries[0]]
        self._alignments = [choice.get_active_id() or 'left' for choice in self.align_choices]
        visible = [[entry.get_text() for entry in row] for row in self.entries[1:]]
        self._rows[self.page_start:self.page_start + len(visible)] = visible

    def _rebuild(self):
        focus = self._selected
        self.page_start = min(self.page_start, max(0, (len(self._rows) - 1) // self.PAGE_SIZE * self.PAGE_SIZE))
        for child in self.grid.get_children():
            child.destroy()
        self.entries = []
        self.align_choices = []
        columns = len(self._headers)
        self.row_labels = {}
        self.column_buttons = []
        header_label = Gtk.Label(label='Header')
        header_label.get_style_context().add_class('table-editor-axis')
        self.grid.attach(header_label, 0, 1, 1, 1)
        self.row_labels[0] = header_label
        for column in range(columns):
            choice = Gtk.ComboBoxText()
            for value, label in (('left', 'Align left'), ('center', 'Align center'), ('right', 'Align right')):
                choice.append(value, label)
            choice.set_active_id(self._alignments[column] if column < len(self._alignments) else 'left')
            choice.get_accessible().set_name(f'Column {column + 1} alignment')
            self.align_choices.append(choice)
            menu = Gtk.MenuButton(label=f'Column {column + 1} ▾')
            menu.get_style_context().add_class('table-editor-column')
            menu.connect('toggled', self.select_column, column)
            self.column_buttons.append(menu)
            popover = Gtk.Popover.new(menu)
            controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            controls.set_border_width(12)
            controls.pack_start(Gtk.Label(label='Text alignment', xalign=0), False, False, 0)
            controls.pack_start(choice, False, False, 0)
            choice.connect('changed', self.align_cells, column)
            controls.pack_start(Gtk.Label(label='Editing width (characters)', xalign=0), False, False, 0)
            width = Gtk.SpinButton.new_with_range(8, 80, 2)
            width.set_value(self._column_widths[column])
            width.get_accessible().set_name(f'Column {column + 1} editing width')
            width.set_tooltip_text('Column width in characters, for this editing session')
            width.connect('value-changed', self.resize_column, column)
            controls.pack_start(width, False, False, 0)
            note = Gtk.Label(label='Width applies to this editing session.', xalign=0)
            note.get_style_context().add_class('muted')
            controls.pack_start(note, False, False, 0)
            popover.add(controls)
            controls.show_all()
            menu.set_popover(popover)
            self.grid.attach(menu, column + 1, 0, 1, 1)
        header = []
        for column, text in enumerate(self._headers):
            entry = self._entry(text, f'Header {column + 1}', 0, column)
            header.append(entry)
            self.grid.attach(entry, column + 1, 1, 1, 1)
        self.entries.append(header)
        for row_index in range(self.page_start, min(len(self._rows), self.page_start + self.PAGE_SIZE)):
            row = self._rows[row_index]
            widgets = []
            row_label = Gtk.Label(label=str(row_index + 1))
            row_label.get_style_context().add_class('table-editor-axis')
            self.row_labels[row_index + 1] = row_label
            self.grid.attach(row_label, 0, row_index - self.page_start + 2, 1, 1)
            for column in range(columns):
                entry = self._entry(row[column] if column < len(row) else '',
                                    f'Row {row_index + 1} column {column + 1}', row_index + 1, column)
                widgets.append(entry)
                self.grid.attach(entry, column + 1, row_index - self.page_start + 2, 1, 1)
            self.entries.append(widgets)
        self.grid.show_all()
        end = min(len(self._rows), self.page_start + self.PAGE_SIZE)
        self.page_label.set_text(f'Rows {self.page_start + 1 if end else 0}–{end} of {len(self._rows)}')
        self.previous_page.set_sensitive(self.page_start > 0)
        self.next_page.set_sensitive(end < len(self._rows))
        self.paging.set_visible(len(self._rows) > self.PAGE_SIZE)
        self.focus_cell(*focus)

    def focus_cell(self, row, column):
        row = max(0, min(row, len(self._rows)))
        column = max(0, min(column, len(self._headers) - 1))
        self._selected = (row, column)
        self.refresh_selection()
        visible = row - self.page_start if row else 0
        if row and visible <= 0:
            return
        if 0 <= visible < len(self.entries):
            self.entries[visible][column].grab_focus()

    def resize_column(self, spin, column):
        self._column_widths[column] = spin.get_value_as_int()
        for row in self.entries:
            row[column].set_width_chars(self._column_widths[column])

    def _entry(self, text, name, row, column):
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_width_chars(self._column_widths[column])
        entry.set_text(text)
        entry.set_alignment({'left': 0, 'center': 0.5, 'right': 1}.get(self._alignments[column], 0))
        entry.get_style_context().add_class('table-editor-cell')
        if row == 0:
            entry.get_style_context().add_class('table-editor-header')
        elif row % 2 == 0:
            entry.get_style_context().add_class('table-editor-stripe')
        entry.set_activates_default(False)
        entry.get_accessible().set_name(name)
        entry.connect('focus-in-event', self.select_cell, row, column)
        entry.connect('key-press-event', self.navigate_cell, row, column)
        return entry

    def select_cell(self, entry, event, row, column):
        self._selected = (row, column)
        self.refresh_selection()
        return False

    def select_column(self, menu, column):
        if menu.get_active():
            self.select_cell(None, None, self._selected[0], column)

    def align_cells(self, choice, column):
        alignment = {'left': 0, 'center': 0.5, 'right': 1}.get(choice.get_active_id(), 0)
        for row in self.entries:
            row[column].set_alignment(alignment)

    def refresh_selection(self):
        row, column = self._selected
        location = 'Header' if row == 0 else f'Row {row}'
        self.summary.set_text(f'{len(self._rows)} rows × {len(self._headers)} columns · {location}, Column {column + 1}')
        for visible_row, entries in enumerate(self.entries):
            source_row = visible_row + self.page_start if visible_row else 0
            for col, entry in enumerate(entries):
                style = entry.get_style_context()
                if (source_row, col) == (row, column):
                    style.add_class('table-editor-selected')
                else:
                    style.remove_class('table-editor-selected')
        for index, label in self.row_labels.items():
            style = label.get_style_context()
            if index == row:
                style.add_class('table-editor-axis-selected')
            else:
                style.remove_class('table-editor-axis-selected')
        for index, menu in enumerate(self.column_buttons):
            style = menu.get_style_context()
            if index == column:
                style.add_class('table-editor-axis-selected')
            else:
                style.remove_class('table-editor-axis-selected')
        enabled = {
            'Move up': row > 1, 'Move down': 0 < row < len(self._rows),
            'Remove row': row > 0, 'Move left': column > 0,
            'Move right': column + 1 < len(self._headers),
            'Remove column': len(self._headers) > 1,
            'Sort ascending': len(self._rows) > 1, 'Sort descending': len(self._rows) > 1,
        }
        for label, button in self.action_buttons.items():
            button.set_sensitive(enabled[label])

    def navigate_cell(self, entry, event, row, column):
        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        backward = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            position = row * len(self._headers) + column
            position = max(0, position + (-1 if backward or event.keyval == Gdk.KEY_ISO_Left_Tab else 1))
            row, column = divmod(position, len(self._headers))
        elif event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            row = max(0, row + (-1 if backward else 1))
        else:
            return False
        self._capture()
        if row > len(self._rows):
            self._rows.append([''] * len(self._headers))
        target_page = (row - 1) // self.PAGE_SIZE * self.PAGE_SIZE if row else self.page_start
        self._selected = (row, column)
        if target_page != self.page_start or row - self.page_start >= len(self.entries):
            self.page_start = target_page
            self._rebuild()
        self.focus_cell(row, column)
        return True

    def focused_cell(self):
        return self._selected

    def model(self):
        self._capture()
        return list(self._headers), list(self._alignments), [list(row) for row in self._rows]

    def markdown(self):
        headers, alignments, rows = self.model()
        return format_table(headers, alignments, rows)

    def add_row(self):
        self._capture()
        row_index, column = self.focused_cell() or (1, 0)
        insert_at = len(self._rows) if row_index == 0 else row_index
        self._rows.insert(insert_at, ['' for _ in self._headers])
        self.page_start = insert_at // self.PAGE_SIZE * self.PAGE_SIZE
        self._selected = (insert_at + 1, column)
        self._rebuild()

    def remove_row(self):
        self._capture()
        if not self._rows:
            return
        row_index, _column = self.focused_cell() or (1, 0)
        index = len(self._rows) - 1 if row_index == 0 else min(row_index - 1, len(self._rows) - 1)
        self._rows.pop(max(0, index))
        self._rebuild()

    def add_column(self):
        self._capture()
        _row, column = self.focused_cell() or (0, len(self._headers) - 1)
        column = min(column + 1, len(self._headers))
        self._headers.insert(column, f'Column {len(self._headers) + 1}')
        self._alignments.insert(column, 'left')
        self._column_widths.insert(column, 18)
        for row in self._rows:
            row.insert(column, '')
        self._rebuild()

    def remove_column(self):
        self._capture()
        if len(self._headers) <= 1:
            return
        _row, column = self.focused_cell() or (0, 0)
        column = min(column, len(self._headers) - 1)
        del self._headers[column]
        del self._alignments[column]
        del self._column_widths[column]
        for row in self._rows:
            del row[column]
        self._rebuild()

    def move_row(self, delta):
        self._capture()
        row_index, column = self.focused_cell() or (1, 0)
        if row_index < 1:
            return
        target = row_index - 1 + delta
        if target < 0 or target >= len(self._rows):
            return
        current = row_index - 1
        self._rows[current], self._rows[target] = self._rows[target], self._rows[current]
        self.page_start = target // self.PAGE_SIZE * self.PAGE_SIZE
        self._selected = (target + 1, column)
        self._rebuild()

    def move_column(self, delta):
        self._capture()
        row_index, column = self.focused_cell() or (0, 0)
        target = column + delta
        if target < 0 or target >= len(self._headers):
            return
        self._headers[column], self._headers[target] = self._headers[target], self._headers[column]
        self._alignments[column], self._alignments[target] = self._alignments[target], self._alignments[column]
        self._column_widths[column], self._column_widths[target] = self._column_widths[target], self._column_widths[column]
        for row in self._rows:
            row[column], row[target] = row[target], row[column]
        self._rebuild()
        self.focus_cell(row_index, target)

    def sort(self, ascending):
        self._capture()
        if not self._rows:
            return
        _row, column = self.focused_cell() or (0, 0)
        self._rows = sort_rows(self._rows, column, ascending)
        self._rebuild()
