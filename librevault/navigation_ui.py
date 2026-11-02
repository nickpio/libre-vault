"""Compact note switching and editor link navigation."""
import re
from urllib.parse import urlsplit
from markdown_it.helpers import parseLinkDestination

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gtk

from .commands import CommandPalette
from .navigation import matching_notes


class QuickSwitcher(CommandPalette):
    max_width = 560

    def __init__(self, owner):
        super().__init__(owner)
        self.search.set_placeholder_text('Find a note…')
        self.search.get_accessible().set_name('Quick switcher')
        self.results_scroll.set_max_content_height(240)

    def filter_commands(self, *_):
        for row in self.results.get_children():
            row.destroy()
        vault = self.owner.vault
        if vault:
            aliases = {path: vault.metadata(path).aliases for path in vault.notes}
            for path in matching_notes(vault.notes, self.search.get_text(), aliases):
                row = Gtk.ListBoxRow()
                row.title = path
                row.action = lambda path=path: self.owner.open_note(path)
                label = Gtk.Label(label=path, xalign=0)
                label.set_ellipsize(3)
                label.set_margin_top(7)
                label.set_margin_bottom(7)
                row.add(label)
                self.results.add(row)
        self.results.show_all()
        first = self.results.get_row_at_index(0)
        if first:
            self.results.select_row(first)
        self.empty.set_visible(first is None)


class EditorNavigation:
    def __init__(self, pane):
        self.pane = pane
        self.pending = None
        self.popover = None
        self.paths = []
        self.start = 0
        self.stop = 0
        self.handlers = [pane.buffer.connect('changed', self.queue_update),
                         pane.buffer.connect('mark-set', self.queue_update)]
        pane.editor.connect('button-release-event', self.clicked)
        pane.connect('destroy', self.cleanup)

    def cleanup(self, *_):
        for handler in self.handlers:
            self.pane.buffer.disconnect(handler)
        self.handlers.clear()
        if self.pending is not None:
            GLib.source_remove(self.pending)
            self.pending = None
        self.close()

    def close(self):
        if self.popover:
            self.popover.destroy()
            self.popover = None
        self.paths = []

    def queue_update(self, *_):
        if self.pending is None:
            self.pending = GLib.idle_add(self.update)

    def update(self):
        self.pending = None
        pane = self.pane
        text = pane.text()
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        line = text[text.rfind('\n', 0, cursor) + 1:cursor]
        opening = line.rfind('[[')
        if opening < 0 or ']]' in line[opening:] or '|' in line[opening:]:
            self.close()
            return False
        query = line[opening + 2:]
        vault = pane.window.vault
        aliases = {path: vault.metadata(path).aliases for path in vault.notes}
        self.paths = matching_notes(vault.notes, query, aliases, limit=8)
        self.start, self.stop = cursor - len(query), cursor
        if not self.paths:
            self.close()
            return False
        if self.popover:
            self.popover.destroy()
        self.popover = Gtk.Popover.new(pane.editor)
        self.popover.set_modal(False)
        self.rows = Gtk.ListBox()
        self.rows.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.rows.set_activate_on_single_click(True)
        self.rows.connect('row-activated', lambda *_: self.accept())
        self.popover.add(self.rows)
        for path in self.paths:
            label = Gtk.Label(label=path, xalign=0)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            self.rows.add(label)
        self.rows.select_row(self.rows.get_row_at_index(0))
        rectangle = pane.editor.get_iter_location(pane.buffer.get_iter_at_offset(cursor))
        rectangle.x, rectangle.y = pane.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET,
                                                                       rectangle.x, rectangle.y)
        self.popover.set_pointing_to(rectangle)
        self.popover.show_all()
        return False

    def accept(self):
        row = self.rows.get_selected_row()
        if row is None:
            return
        path = self.paths[row.get_index()]
        pane = self.pane
        text = pane.text()
        suffix = '' if text[self.stop:self.stop + 2] == ']]' else ']]'
        start, stop = self.start, self.stop
        self.close()
        pane.buffer.begin_user_action()
        pane.buffer.delete(pane.buffer.get_iter_at_offset(start), pane.buffer.get_iter_at_offset(stop))
        pane.buffer.insert(pane.buffer.get_iter_at_offset(start), path.removesuffix('.md') + suffix)
        pane.buffer.end_user_action()

    def key_press(self, event):
        if not self.popover:
            return False
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
            self.accept()
            return True
        if event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            row = self.rows.get_selected_row()
            index = row.get_index() if row else 0
            index = max(0, min(len(self.paths) - 1, index + (1 if event.keyval == Gdk.KEY_Down else -1)))
            self.rows.select_row(self.rows.get_row_at_index(index))
            return True
        return False

    def clicked(self, view, event):
        if event.button != 1 or not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        x, y = view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        found, iterator = view.get_iter_at_location(x, y)
        if not found:
            return False
        offset = iterator.get_offset()
        if any(span.style == 'code' and span.start <= offset < span.end for span in self.pane.spans):
            return False
        text = self.pane.text()
        pattern = r'!?\[\[([^\]\n]+)\]\]|!?\[[^\]\n]*\]\(([^)\n]+)\)'
        for match in re.finditer(pattern, text):
            if match.start() <= offset < match.end():
                if not any(span.style == 'link' and span.start < match.end()
                           and span.end > match.start() for span in self.pane.spans):
                    continue
                target = (match.group(1) or match.group(2)).split('|', 1)[0]
                if match.group(2):
                    result = parseLinkDestination(text, match.start(2), len(text))
                    if not result.ok:
                        continue
                    target = result.str
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    if url.scheme in ('http', 'https', 'mailto'):
                        Gtk.show_uri_on_window(self.pane.window, target, Gdk.CURRENT_TIME)
                else:
                    self.pane.window.follow_link(self.pane.note.path, target)
                return True
        return False
