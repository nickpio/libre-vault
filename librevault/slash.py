"""Slash-command snippets and the editor popup that inserts them."""
from dataclasses import dataclass, field

import gi

gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gtk

from .templates import current_time, format_moment


@dataclass(frozen=True)
class SlashCommand:
    title: str
    snippet: str
    cursor: int
    keywords: str = ''
    kind: str = 'snippet'


def builtin_commands(when=None):
    """Commands offered by `/`. Dates use the same moment tokens as templates."""
    moment = when or current_time()
    date = format_moment(moment, 'YYYY-MM-DD')
    clock = format_moment(moment, 'HH:mm')
    note = '> [!NOTE]\n> '
    tip = '> [!TIP]\n> '
    warning = '> [!WARNING]\n> '
    table = '| Column | Column |\n| --- | --- |\n|  |  |\n'
    return (
        SlashCommand('Heading 1', '# ', 2, 'h1'),
        SlashCommand('Heading 2', '## ', 3, 'h2'),
        SlashCommand('Heading 3', '### ', 4, 'h3'),
        SlashCommand('Heading 4', '#### ', 5, 'h4'),
        SlashCommand('Bullet list', '- ', 2, 'ul unordered'),
        SlashCommand('Numbered list', '1. ', 3, 'ol ordered'),
        SlashCommand('Task item', '- [ ] ', 6, 'todo checkbox task'),
        SlashCommand('Code block', '```\n\n```\n', 4, 'fence'),
        SlashCommand('Math block', '$$\n\n$$\n', 3, 'latex tex'),
        SlashCommand('Callout', note, len(note), 'note admonition'),
        SlashCommand('Callout tip', tip, len(tip), 'tip hint'),
        SlashCommand('Callout warning', warning, len(warning), 'warning caution'),
        SlashCommand('Table', table, 2, 'grid'),
        SlashCommand('Horizontal rule', '---\n', 4, 'hr divider'),
        SlashCommand('Link', '[]()', 1, 'url'),
        SlashCommand('Embed', '![[]]', 3, 'transclusion image'),
        SlashCommand('Current date', date, len(date), 'today'),
        SlashCommand('Current time', clock, len(clock), 'now clock'),
        SlashCommand('Template', '', 0, 'insert snippet', kind='template'),
    )


def trigger_at(text, cursor):
    """Return `(slash_offset, query)` when `/` starts a command at the cursor.

    The slash must be at the beginning of the line or after whitespace.
    The query is everything after it on that line, up to the cursor.
    """
    if cursor < 0 or cursor > len(text):
        return None
    line_start = text.rfind('\n', 0, cursor) + 1
    prefix = text[line_start:cursor]
    slash = prefix.rfind('/')
    if slash < 0:
        return None
    if slash > 0 and not prefix[slash - 1].isspace():
        return None
    query = prefix[slash + 1:]
    if any(char in query for char in '\r\n'):
        return None
    return line_start + slash, query


def filter_commands(commands, query):
    words = query.casefold().split()
    if not words:
        return list(commands)
    matched = []
    for command in commands:
        haystack = f'{command.title} {command.keywords}'.casefold()
        if all(word in haystack for word in words):
            matched.append(command)
    return matched


@dataclass
class SlashMenu:
    """Non-modal completion popup. The text view keeps the keyboard."""

    pane: object
    trigger_start: int = 0
    commands: list = field(default_factory=list)
    index: int = 0
    open: bool = False

    def __post_init__(self):
        self.popover = Gtk.Popover.new(self.pane.editor)
        self.popover.set_modal(False)
        self.popover.set_position(Gtk.PositionType.BOTTOM)
        self.popover.get_style_context().add_class('slash-popover')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(6)
        self.rows = Gtk.ListBox()
        self.rows.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.rows.set_activate_on_single_click(True)
        self.rows.get_style_context().add_class('command-results')
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_width(240)
        scroll.set_max_content_height(220)
        scroll.set_propagate_natural_height(True)
        scroll.add(self.rows)
        box.pack_start(scroll, True, True, 0)
        hint = Gtk.Label(label='↑↓ Navigate    Enter Apply    Esc Dismiss', xalign=0)
        hint.get_style_context().add_class('palette-hint')
        box.pack_start(hint, False, False, 0)
        self.popover.add(box)
        self.rows.connect('row-activated', self._activated)
        self.popover.connect('closed', self._closed)
        box.show_all()

    def _closed(self, *_):
        self.open = False

    def _activated(self, _, row):
        if row is not None:
            self.index = row.get_index()
            self.apply_current()

    def dismiss(self):
        self.open = False
        self.commands = []
        if self.popover.get_visible():
            self.popover.popdown()

    def update(self, start, commands):
        self.trigger_start = start
        titles = [command.title for command in commands]
        previous = [command.title for command in self.commands]
        self.commands = list(commands)
        if titles != previous:
            self.index = 0 if not previous else min(self.index, len(commands) - 1)
            for child in self.rows.get_children():
                child.destroy()
            for command in commands:
                row = Gtk.ListBoxRow()
                label = Gtk.Label(label=command.title, xalign=0)
                label.set_margin_top(4)
                label.set_margin_bottom(4)
                label.set_margin_start(8)
                label.set_margin_end(8)
                row.add(label)
                self.rows.add(row)
            self.rows.show_all()
        self.index = max(0, min(self.index, len(self.commands) - 1))
        row = self.rows.get_row_at_index(self.index)
        if row is not None:
            self.rows.select_row(row)
        if not self.popover.get_visible():
            self._popup()
        self.open = True

    def _popup(self):
        iterator = self.pane.buffer.get_iter_at_offset(self.trigger_start)
        rect = self.pane.editor.get_iter_location(iterator)
        x, y = self.pane.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
        self.popover.set_pointing_to(Gdk.Rectangle(x, y, max(rect.width, 1), max(rect.height, 1)))
        self.popover.popup()

    def move(self, delta):
        if not self.commands:
            return
        self.index = max(0, min(len(self.commands) - 1, self.index + delta))
        row = self.rows.get_row_at_index(self.index)
        if row is not None:
            self.rows.select_row(row)

    def handle_key(self, event):
        if not self.open:
            return False
        if event.keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self.move(-1)
            return True
        if event.keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self.move(1)
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            self.apply_current()
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.dismiss()
            return True
        return False

    def apply_current(self):
        if not self.commands:
            self.dismiss()
            return
        command = self.commands[self.index]
        start = self.trigger_start
        self.dismiss()
        self.pane.apply_slash_command(command, start)

    def selected_title(self):
        if not self.commands:
            return ''
        return self.commands[self.index].title
