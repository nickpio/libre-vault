"""Literal in-note find/replace on the complete Markdown source."""
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gtk

from .editing import TextEdit, find_matches


class FindBar(Gtk.Box):
    def __init__(self, pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.pane = pane
        self.matches = []
        self.cached = None
        self.get_style_context().add_class('find-bar')
        self.set_no_show_all(True)
        search_row = Gtk.Box(spacing=6)
        self.pack_start(search_row, False, False, 0)
        self.query = Gtk.SearchEntry(placeholder_text='Find in this note…')
        self.query.get_accessible().set_name('Find in note')
        self.query.connect('changed', lambda *_: self.refresh())
        self.query.connect('activate', lambda *_: self.step(True))
        search_row.pack_start(self.query, True, True, 0)
        self.count = Gtk.Label(label='')
        self.count.get_style_context().add_class('muted')
        search_row.pack_start(self.count, False, False, 4)
        self.match_case = Gtk.ToggleButton(label='Aa')
        self.match_case.set_tooltip_text('Match case')
        self.match_case.get_accessible().set_name('Match case')
        self.match_case.get_style_context().add_class('match-case')
        self.match_case.connect('toggled', lambda *_: self.refresh())
        search_row.pack_start(self.match_case, False, False, 0)
        for icon, tooltip, action in (
                ('go-up-symbolic', 'Previous match · Shift+F3', lambda *_: self.step(False)),
                ('go-down-symbolic', 'Next match · F3', lambda *_: self.step(True)),
                ('window-close-symbolic', 'Close find · Escape', lambda *_: self.close())):
            search_row.pack_start(pane.window.icon_button(icon, tooltip, action), False, False, 0)
        self.replace_row = Gtk.Box(spacing=6)
        self.pack_start(self.replace_row, False, False, 0)
        self.replacement = Gtk.Entry(placeholder_text='Replace with…')
        self.replacement.get_accessible().set_name('Replacement text')
        self.replacement.connect('activate', lambda *_: self.replace_current())
        self.replace_row.pack_start(self.replacement, True, True, 0)
        self.replace_button = Gtk.Button(label='Replace')
        self.replace_button.connect('clicked', lambda *_: self.replace_current())
        self.replace_row.pack_start(self.replace_button, False, False, 0)
        self.replace_all_button = Gtk.Button(label='Replace all')
        self.replace_all_button.connect('clicked', lambda *_: self.replace_all())
        self.replace_row.pack_start(self.replace_all_button, False, False, 0)
        self.tag = pane.buffer.create_tag(None)
        self.connect('key-press-event', self.key_press)

    def open(self, replace=False):
        selection = self.pane.buffer.get_selection_bounds()
        if selection:
            selected = self.pane.buffer.get_text(*selection, True)
            if '\n' not in selected:
                self.query.set_text(selected)
        self.set_no_show_all(False)
        self.show_all()
        self.set_no_show_all(True)
        self.replace_row.set_visible(replace)
        self.refresh()
        self.query.grab_focus()
        self.query.select_region(0, -1)

    def close(self):
        self.hide()
        self.pane.buffer.remove_tag(self.tag, *self.pane.buffer.get_bounds())
        self.cached = None
        self.matches = []
        self.pane.editor.grab_focus()

    def refresh(self):
        if not self.get_visible():
            return
        key = (self.pane.text(), self.query.get_text(), self.match_case.get_active())
        if key != self.cached:
            self.cached = key
            self.matches = find_matches(*key)
            self.pane.buffer.remove_tag(self.tag, *self.pane.buffer.get_bounds())
            found, background = self.pane.editor.get_style_context().lookup_color('selection_bg')
            if found:
                self.tag.set_property('background-rgba', background)
            for start, end in self.matches:
                self.pane.buffer.apply_tag(self.tag, self.pane.buffer.get_iter_at_offset(start),
                                           self.pane.buffer.get_iter_at_offset(end))
        selection = self.pane.buffer.get_selection_bounds()
        offsets = tuple(it.get_offset() for it in selection) if selection else None
        if offsets in self.matches:
            self.count.set_text(f'{self.matches.index(offsets) + 1} of {len(self.matches)}')
        else:
            self.count.set_text(f'{len(self.matches)} matches' if key[1] else '')
        self.replace_button.set_sensitive(bool(self.matches))
        self.replace_all_button.set_sensitive(bool(self.matches))

    def select_match(self, match):
        start, end = match
        buffer = self.pane.buffer
        buffer.select_range(buffer.get_iter_at_offset(end), buffer.get_iter_at_offset(start))
        self.pane.render()
        self.pane.editor.scroll_to_iter(buffer.get_iter_at_offset(start), 0.15, False, 0, 0)
        self.refresh()

    def step(self, forward):
        if not self.get_visible():
            self.open()
        self.refresh()
        if not self.matches:
            return
        selection = self.pane.buffer.get_selection_bounds()
        offsets = tuple(it.get_offset() for it in selection) if selection else None
        if offsets in self.matches:
            index = (self.matches.index(offsets) + (1 if forward else -1)) % len(self.matches)
        else:
            cursor = self.pane.buffer.get_iter_at_mark(self.pane.buffer.get_insert()).get_offset()
            if forward:
                index = next((i for i, match in enumerate(self.matches) if match[0] >= cursor), 0)
            else:
                index = next((i for i in range(len(self.matches) - 1, -1, -1)
                              if self.matches[i][1] <= cursor), len(self.matches) - 1)
        self.select_match(self.matches[index])

    def replace_current(self):
        self.refresh()
        if not self.matches:
            return
        selection = self.pane.buffer.get_selection_bounds()
        offsets = tuple(it.get_offset() for it in selection) if selection else None
        if offsets not in self.matches:
            self.step(True)
            offsets = tuple(it.get_offset() for it in self.pane.buffer.get_selection_bounds())
        replacement = self.replacement.get_text()
        self.pane.apply_edit(TextEdit(*offsets, replacement))
        self.refresh()
        self.step(True)

    def replace_all(self):
        self.refresh()
        if not self.matches:
            return
        replacement = self.replacement.get_text()
        buffer = self.pane.buffer
        self.pane.history.break_group()
        buffer.begin_user_action()
        try:
            for start, end in reversed(self.matches):
                self.pane.apply_edit(TextEdit(start, end, replacement), user_action=False)
        finally:
            buffer.end_user_action()
        self.refresh()

    def key_press(self, _, event):
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        key = Gdk.keyval_to_lower(event.keyval)
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if modifiers == Gdk.ModifierType.CONTROL_MASK and key in (Gdk.KEY_f, Gdk.KEY_h):
            self.replace_row.set_visible(key == Gdk.KEY_h)
            self.query.grab_focus()
            self.query.select_region(0, -1)
            return True
        if key == Gdk.KEY_F3 or (key == Gdk.KEY_g and modifiers & Gdk.ModifierType.CONTROL_MASK):
            self.step(not bool(modifiers & Gdk.ModifierType.SHIFT_MASK))
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and modifiers == Gdk.ModifierType.SHIFT_MASK:
            self.step(False)
            return True
        return False
