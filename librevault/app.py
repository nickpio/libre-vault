"""Native Linux notebook prototype. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

from .vault import Vault, VaultError, links
from .formatting import markdown_layout
from .editing import list_enter


CSS = Path(__file__).with_name('style.css').read_bytes()


def scrolled(widget, width=None):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add(widget)
    if width:
        scroll.set_size_request(width, -1)
    return scroll


def text_buffer(text):
    buffer = Gtk.TextBuffer()
    buffer.set_text(text)
    return buffer


class NotePane(Gtk.Box):
    def __init__(self, window, note):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.note = window, note
        self.dirty = False
        self.format_source = None
        self.formatted_text = None
        self.spans = []
        self.items = []
        self.list_tags = {}
        self.list_widgets = []
        self.position_source = None
        self.buffer = text_buffer(note.text.replace('\r\n', '\n'))
        self.tags = {
            'strong': self.buffer.create_tag(None, weight=Pango.Weight.BOLD),
            'em': self.buffer.create_tag(None, style=Pango.Style.ITALIC),
            'code': self.buffer.create_tag(None, family='monospace', background='#1b1b1b', size_points=10),
            'quote': self.buffer.create_tag(None, foreground='#aaaaaa', left_margin=18),
            'link': self.buffer.create_tag(None, foreground='#cccccc', underline=Pango.Underline.SINGLE),
            'completed': self.buffer.create_tag(None, foreground='#999999', strikethrough=True),
            'metadata': self.buffer.create_tag(None, foreground='#888888', family='monospace', size_points=10),
        }
        for level, size in enumerate([25, 21, 18, 16, 14, 12], 1):
            self.tags[f'h{level}'] = self.buffer.create_tag(
                None, size_points=size, weight=Pango.Weight.BOLD,
                foreground='#e4e4e4', pixels_above_lines=12, pixels_below_lines=8)
        # This tag has highest priority, including inside headings and code.
        self.hidden = self.buffer.create_tag(None, invisible=True)
        self.editor = Gtk.TextView(buffer=self.buffer)
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.set_left_margin(40)
        self.editor.set_right_margin(40)
        self.editor.set_top_margin(30)
        self.editor.set_bottom_margin(80)
        self.editor.set_pixels_below_lines(5)
        self.editor.get_style_context().add_class('live-editor')
        self.editor.connect('size-allocate', self.size_editor)
        self.editor.connect('copy-clipboard', self.copy_selection)
        self.editor.connect('cut-clipboard', self.cut_selection)
        self.editor.connect('key-press-event', self.editor_key_press)
        self.scroll = scrolled(self.editor)
        self.scroll.get_vadjustment().connect('value-changed', self.queue_positions)
        self.scroll.get_hadjustment().connect('value-changed', self.queue_positions)
        self.pack_start(self.scroll, True, True, 0)
        self.tab_label = Gtk.Label(label=Path(note.path).stem)
        self.buffer.connect('changed', self.changed)
        self.buffer.connect('mark-set', self.cursor_moved)
        self.connect('destroy', self.cleanup)
        self.render()

    def size_editor(self, _, allocation):
        margin = max(32, (allocation.width - 780) // 2)
        if self.editor.get_left_margin() != margin:
            self.editor.set_left_margin(margin)
            self.editor.set_right_margin(margin)
            self.tags['quote'].set_property('left-margin', margin + 18)
            self.update_list_margins()
        self.queue_positions()

    def text(self):
        # Hidden Markdown is still authoritative, including during save and copy.
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def copy_selection(self, view):
        view.stop_emission_by_name('copy-clipboard')
        selection = self.buffer.get_selection_bounds()
        if selection:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
                self.buffer.get_text(*selection, True), -1)

    def cut_selection(self, view):
        view.stop_emission_by_name('cut-clipboard')
        selection = self.buffer.get_selection_bounds()
        if selection:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
                self.buffer.get_text(*selection, True), -1)
            self.buffer.begin_user_action()
            self.buffer.delete_interactive(*selection, True)
            self.buffer.end_user_action()

    def changed(self, *_):
        self.dirty = True
        self.tab_label.set_text('• ' + Path(self.note.path).stem)
        self.window.status.set_text('Unsaved changes · Ctrl+S to save')
        self.queue_format()

    def editor_key_press(self, _, event):
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        # Modified Enter and selected text keep GTK's normal editing behavior.
        modifiers = Gtk.accelerator_get_default_mod_mask()
        if event.state & modifiers or self.buffer.get_has_selection():
            return False
        cursor = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
        edit = list_enter(self.text(), cursor)
        if edit is None:
            return False
        self.buffer.begin_user_action()
        start = self.buffer.get_iter_at_offset(edit.start)
        end = self.buffer.get_iter_at_offset(edit.end)
        self.buffer.delete(start, end)
        self.buffer.insert(start, edit.replacement)
        self.buffer.place_cursor(start)
        self.buffer.end_user_action()
        self.editor.scroll_mark_onscreen(self.buffer.get_insert())
        return True

    def cursor_moved(self, _, __, mark):
        if mark.get_name() in ('insert', 'selection_bound'):
            self.queue_format()

    def queue_format(self):
        if self.format_source is None:
            self.format_source = GLib.idle_add(self.format_pending)

    def format_pending(self):
        self.format_source = None
        self.render()
        if self.window.current() is self:
            self.window.update_context()
        return False

    def cleanup(self, *_):
        if self.position_source is not None:
            GLib.source_remove(self.position_source)
            self.position_source = None
        if self.format_source is not None:
            GLib.source_remove(self.format_source)
            self.format_source = None

    def render(self):
        text = self.text()
        if text != self.formatted_text:
            layout = markdown_layout(text)
            self.spans, self.items = layout.spans, layout.items
            self.formatted_text = text
            self.rebuild_list_widgets()
        start, end = self.buffer.get_bounds()
        for tag in [*self.tags.values(), *self.list_tags.values(), self.hidden]:
            self.buffer.remove_tag(tag, start, end)
        cursor = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
        line_start = text.rfind('\n', 0, cursor) + 1
        line_end = text.find('\n', cursor)
        if line_end < 0:
            line_end = len(text)
        selection = self.buffer.get_selection_bounds()
        selected = tuple(it.get_offset() for it in selection) if selection else None
        for span in self.spans:
            left = self.buffer.get_iter_at_offset(span.start)
            right = self.buffer.get_iter_at_offset(span.end)
            if span.style == 'syntax':
                on_line = span.start <= line_end and span.end > line_start
                in_selection = selected and span.start < selected[1] and span.end > selected[0]
                if not on_line and not in_selection:
                    self.buffer.apply_tag(self.hidden, left, right)
            else:
                self.buffer.apply_tag(self.tags[span.style], left, right)

        for item, widget in self.list_widgets:
            left = self.buffer.get_iter_at_offset(item.start)
            end = self.buffer.get_iter_at_offset(item.end)
            content = self.buffer.get_iter_at_offset(item.content_start)
            self.buffer.apply_tag(self.list_tags[item.depth], left, end)
            # Lists stay rendered on the active line; a selection can expose
            # their source markers for deliberate Markdown editing.
            reveal = selected and item.start < selected[1] and item.content_start > selected[0]
            widget.set_visible(not reveal)
            if not reveal:
                self.buffer.apply_tag(self.hidden, left, content)
            if item.checked:
                line_end = text.find('\n', item.content_start)
                stop = len(text) if line_end < 0 else line_end
                self.buffer.apply_tag(self.tags['completed'], content, self.buffer.get_iter_at_offset(stop))
        self.queue_positions()

    def rebuild_list_widgets(self):
        focused = next((item.task_offset for item, widget in self.list_widgets if widget.has_focus()), None)
        for _, widget in self.list_widgets:
            widget.destroy()
        self.list_widgets.clear()
        for depth in sorted({item.depth for item in self.items}):
            if depth not in self.list_tags:
                self.list_tags[depth] = self.buffer.create_tag(None)
        for item in self.items:
            if item.task_offset is not None:
                widget = Gtk.CheckButton()
                widget.get_style_context().add_class('task-checkbox')
                widget.set_active(item.checked)
                label = self.formatted_text[item.content_start:item.end].split('\n', 1)[0]
                widget.get_accessible().set_name(label or 'Task')
                widget.set_tooltip_text('Mark task incomplete' if item.checked else 'Mark task complete')
                widget.connect('toggled', self.toggle_task, item, self.formatted_text)
            else:
                label = str(item.number) + '.' if item.number is not None else ['•', '◦', '▪'][item.depth % 3]
                widget = Gtk.Label(label=label)
                widget.get_style_context().add_class('list-marker')
            self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
            widget.show()
            self.list_widgets.append((item, widget))
            if focused is not None and focused == item.task_offset:
                widget.grab_focus()
        self.update_list_margins()

    def update_list_margins(self):
        # Reserve enough gutter for long ordered-list numbers as well as nesting.
        widths = {}
        for item, widget in self.list_widgets:
            widths[item.depth] = max(widths.get(item.depth, 28), widget.get_preferred_width().natural_width + 10)
        for depth, tag in self.list_tags.items():
            gutter = sum(widths.get(level, 28) for level in range(depth + 1))
            tag.set_property('left-margin', self.editor.get_left_margin() + gutter)
            tag.set_property('indent', 0)

    def toggle_task(self, widget, item, source):
        # A pending edit may invalidate a widget's source offset. Never toggle
        # unrelated text; the next formatting pass replaces stale controls.
        if self.text() != source:
            self.queue_format()
            return
        start = self.buffer.get_iter_at_offset(item.task_offset)
        end = self.buffer.get_iter_at_offset(item.task_offset + 1)
        self.buffer.begin_user_action()
        self.buffer.delete(start, end)
        self.buffer.insert(start, 'x' if widget.get_active() else ' ')
        self.buffer.end_user_action()

    def queue_positions(self, *_):
        if self.position_source is None:
            self.position_source = GLib.idle_add(self.position_list_widgets)

    def position_list_widgets(self):
        self.position_source = None
        for item, widget in self.list_widgets:
            if not widget.get_visible():
                continue
            # GTK reports positions within hidden prefixes inconsistently. The
            # line start gives the first displayed position and survives wrapping.
            position = self.buffer.get_iter_at_offset(item.start)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            width = widget.get_preferred_width().natural_width
            height = widget.get_preferred_height().natural_height
            y += (rect.height - height) // 2
            widget.set_child_visible(y >= 0 and y + height <= self.editor.get_allocated_height())
            self.editor.move_child(widget, x - width - 8, y)
        return False

    def save(self):
        if self.dirty:
            self.note = self.window.vault.save(self.note, self.text())
            self.dirty = False
            self.tab_label.set_text(Path(self.note.path).stem)
        self.window.status.set_text(f'Saved · {self.note.path}')
        self.window.update_context()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, folder=None):
        super().__init__(application=app, title='Libre Vault')
        self.set_default_size(1360, 850)
        self.vault = None
        self.panes = {}
        self.refresh_source = None
        self.connect('delete-event', self.on_delete)
        self.connect('key-press-event', self.key_press)
        Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', True)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        self.set_titlebar(header)
        brand = Gtk.Label(label='Libre Vault')
        brand.get_style_context().add_class('brand')
        header.pack_start(brand)
        self.location_label = Gtk.Label(label='Local workspace')
        self.location_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.location_label.get_style_context().add_class('breadcrumb')
        header.set_custom_title(self.location_label)
        self.notes_toggle = Gtk.ToggleButton()
        self.notes_toggle.set_image(Gtk.Image.new_from_icon_name('view-list-symbolic', Gtk.IconSize.MENU))
        self.notes_toggle.set_tooltip_text('Show notes sidebar')
        self.notes_toggle.get_accessible().set_name('Show notes sidebar')
        self.notes_toggle.set_active(True)
        self.notes_toggle.get_style_context().add_class('icon-button')
        header.pack_start(self.notes_toggle)
        self.details_toggle = Gtk.ToggleButton()
        self.details_toggle.set_image(Gtk.Image.new_from_icon_name('sidebar-show-right-symbolic', Gtk.IconSize.MENU))
        self.details_toggle.set_tooltip_text('Show note details')
        self.details_toggle.get_accessible().set_name('Show note details')
        self.details_toggle.set_active(True)
        self.details_toggle.get_style_context().add_class('icon-button')
        header.pack_end(self.details_toggle)
        header.pack_end(self.icon_button('window-close-symbolic', 'Close tab · Ctrl+W', self.close_tab))
        header.pack_end(self.icon_button('document-save-symbolic', 'Save note · Ctrl+S', self.save_current))
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        layout = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        layout.get_style_context().add_class('workspace-split')
        root.pack_start(layout, True, True, 0)
        sidebar = self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.get_style_context().add_class('sidebar')
        sidebar.set_border_width(14)
        vault_heading = Gtk.Box(spacing=4)
        vault_heading.get_style_context().add_class('vault-heading')
        self.vault_label = Gtk.Label(label='No vault open', xalign=0)
        self.vault_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.vault_label.get_style_context().add_class('vault-title')
        vault_heading.pack_start(self.vault_label, True, True, 0)
        vault_heading.pack_end(self.icon_button('document-new-symbolic', 'New note · Ctrl+N', self.new_note), False, False, 0)
        vault_heading.pack_end(self.icon_button('folder-open-symbolic', 'Open vault · Ctrl+O', self.choose_vault), False, False, 0)
        sidebar.pack_start(vault_heading, False, False, 0)
        self.search_entry = Gtk.SearchEntry(placeholder_text='Search notes…')
        self.search_entry.connect('search-changed', lambda *_: self.populate_files())
        sidebar.pack_start(self.search_entry, False, False, 0)
        files_heading = Gtk.Label(label='Notes', xalign=0)
        files_heading.get_style_context().add_class('caption')
        sidebar.pack_start(files_heading, False, False, 0)
        self.files = Gtk.ListBox()
        self.files.get_style_context().add_class('note-list')
        self.files.set_activate_on_single_click(True)
        self.files.connect('row-activated', lambda _, row: self.open_note(row.path))
        sidebar.pack_start(scrolled(self.files, 212), True, True, 0)
        hint = Gtk.Label(label='Ctrl+O  Open vault\nCtrl+N  New note\nCtrl+S  Save\nCtrl+W  Close tab', xalign=0)
        hint.get_style_context().add_class('shortcuts')
        hint.set_margin_top(12)
        sidebar.pack_start(hint, False, False, 0)
        layout.pack1(sidebar, False, False)
        self.notes_toggle.connect('toggled', lambda button: sidebar.set_visible(button.get_active()))
        center = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        center.get_style_context().add_class('workspace-split')
        self.tabs = Gtk.Notebook(scrollable=True)
        self.tabs.connect('switch-page', lambda *_: GLib.idle_add(self.update_context))
        center.pack1(self.tabs, True, False)
        self.context = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.context.set_border_width(12)
        self.context.set_size_request(208, -1)
        self.context.get_style_context().add_class('sidebar')
        self.details_scroll = scrolled(self.context)
        center.pack2(self.details_scroll, False, False)
        self.details_toggle.connect('toggled', lambda button: self.details_scroll.set_visible(button.get_active()))
        layout.pack2(center, True, False)
        layout.set_position(232)
        center.set_position(900)
        self.status = Gtk.Label(label='Open a vault folder to begin.', xalign=0)
        self.status.get_style_context().add_class('status')
        statusbar = Gtk.Box()
        statusbar.get_style_context().add_class('statusbar')
        self.status.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        statusbar.pack_start(self.status, True, True, 0)
        format_label = Gtk.Label(label='Markdown   ·   UTF-8')
        format_label.get_style_context().add_class('status')
        statusbar.pack_end(format_label, False, False, 0)
        root.pack_end(statusbar, False, False, 0)
        self.show_all()
        if folder:
            self.load_vault(folder)
        self.refresh_source = GLib.timeout_add_seconds(3, self.refresh)
        self.connect('destroy', self.cleanup)

    def icon_button(self, icon, tooltip, action):
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
        button.get_style_context().add_class('icon-button')
        button.set_tooltip_text(tooltip)
        button.get_accessible().set_name(tooltip.split(' · ')[0])
        button.connect('clicked', action)
        return button

    def cleanup(self, *_):
        if self.refresh_source:
            GLib.source_remove(self.refresh_source)
            self.refresh_source = None

    def error(self, error):
        self.status.set_text(str(error))
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text=str(error))
        dialog.run()
        dialog.destroy()

    def try_action(self, action):
        try:
            return action()
        except (VaultError, OSError) as error:
            self.error(error)

    def choose_vault(self, *_):
        dialog = Gtk.FileChooserDialog(title='Open a Markdown vault', transient_for=self,
                                      action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Open', Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            self.try_action(lambda: self.load_vault(dialog.get_filename()))
        dialog.destroy()

    def confirm_panes(self, panes):
        for pane in panes:
            if not pane.dirty:
                continue
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       text=f'Save changes to {pane.note.path}?')
            dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Discard', Gtk.ResponseType.NO, 'Save', Gtk.ResponseType.YES)
            result = dialog.run()
            dialog.destroy()
            if result == Gtk.ResponseType.YES:
                try:
                    pane.save()
                except (VaultError, OSError) as error:
                    self.error(error)
                    return False
            elif result != Gtk.ResponseType.NO:
                return False
        return True

    def load_vault(self, folder):
        vault = Vault(folder)
        if not self.confirm_panes(list(self.panes.values())):
            return
        while self.tabs.get_n_pages():
            self.tabs.remove_page(0)
        self.panes.clear()
        self.vault = vault
        self.vault_label.set_text(vault.root.name)
        self.set_title(f'{vault.root.name} · Libre Vault')
        self.search_entry.set_text('')
        self.populate_files()
        self.status.set_text(f'{len(vault.notes)} notes · {vault.root}')
        if vault.notes:
            first = 'Welcome.md' if 'Welcome.md' in vault.notes else next(iter(vault.notes))
            self.open_note(first)
        self.update_context()

    def populate_files(self):
        for row in self.files.get_children():
            self.files.remove(row)
        if not self.vault:
            return
        for path in self.vault.search(self.search_entry.get_text()):
            row = Gtk.ListBoxRow()
            row.path = path
            content = Gtk.Box(spacing=10)
            icon = Gtk.Image.new_from_icon_name('text-x-generic-symbolic', Gtk.IconSize.MENU)
            icon.get_style_context().add_class('muted')
            content.pack_start(icon, False, False, 0)
            names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            label = Gtk.Label(label=Path(path).stem, xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            names.pack_start(label, False, False, 0)
            if Path(path).parent != Path('.'):
                folder = Gtk.Label(label=str(Path(path).parent), xalign=0)
                folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                folder.get_style_context().add_class('note-folder')
                names.pack_start(folder, False, False, 0)
            content.pack_start(names, True, True, 0)
            row.add(content)
            self.files.add(row)
        pane = self.current()
        if pane:
            for row in self.files.get_children():
                if row.path == pane.note.path:
                    self.files.select_row(row)
                    break
        self.files.show_all()

    def open_note(self, relative, fragment=''):
        if relative not in self.panes:
            def action():
                pane = NotePane(self, self.vault.read(relative))
                self.panes[relative] = pane
                self.tabs.append_page(pane, pane.tab_label)
                pane.show_all()
            self.try_action(action)
        if relative not in self.panes:
            return
        pane = self.panes[relative]
        self.tabs.set_current_page(self.tabs.page_num(pane))
        pane.editor.grab_focus()
        if fragment:
            text = pane.text()
            needle = fragment.removeprefix('^')
            match = re.search(r'(?m)^.*' + re.escape(needle) + r'.*$', text, re.IGNORECASE)
            if match:
                position = pane.buffer.get_iter_at_offset(match.start())
                pane.buffer.place_cursor(position)
                pane.editor.scroll_to_iter(position, 0.15, False, 0, 0)
        self.update_context()

    def current(self):
        index = self.tabs.get_current_page()
        return self.tabs.get_nth_page(index) if index >= 0 else None

    def save_current(self, *_):
        pane = self.current()
        if pane:
            self.try_action(pane.save)

    def close_tab(self, *_):
        pane = self.current()
        if pane and self.confirm_panes([pane]):
            self.tabs.remove_page(self.tabs.page_num(pane))
            self.panes.pop(pane.note.path, None)
            self.update_context()

    def new_note(self, *_):
        if not self.vault:
            self.choose_vault()
            return
        dialog = Gtk.Dialog(title='New note', transient_for=self, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Create', Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text='Note name or existing-folder/Note name')
        entry.set_size_request(430, -1)
        entry.set_margin_top(16)
        entry.set_margin_bottom(16)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            note = self.try_action(lambda: self.vault.create(entry.get_text()))
            if note:
                self.populate_files()
                self.open_note(note.path)
        dialog.destroy()

    def section(self, title):
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class('caption')
        self.context.pack_start(label, False, False, 0)

    def context_button(self, label, action, enabled=True):
        button = Gtk.Button(label=label)
        button.set_sensitive(enabled)
        button.get_style_context().add_class('context-link')
        text = button.get_child()
        text.set_xalign(0)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        button.set_tooltip_text(label)
        button.connect('clicked', lambda *_: action())
        self.context.pack_start(button, False, False, 0)

    def follow_link(self, source, target):
        candidates = self.vault.resolve(source, target)
        fragment = target.partition('#')[2]
        if len(candidates) == 1:
            self.open_note(candidates[0], fragment)
        elif candidates:
            dialog = Gtk.Dialog(title='Choose a note', transient_for=self, modal=True)
            dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
            for i, path in enumerate(candidates):
                dialog.add_button(path, i + 1)
            result = dialog.run()
            dialog.destroy()
            if 1 <= result <= len(candidates):
                self.open_note(candidates[result - 1], fragment)
        else:
            self.status.set_text(f'Unresolved note or unsupported attachment: {target}')

    def update_context(self):
        for child in self.context.get_children():
            self.context.remove(child)
        pane = self.current()
        if not pane or not self.vault:
            self.location_label.set_text(self.vault.root.name if self.vault else 'Local workspace')
            self.section('Outline · Links · Backlinks')
            self.context.show_all()
            return False
        self.location_label.set_text(f'{self.vault.root.name}  /  {pane.note.path}')
        self.location_label.set_tooltip_text(str(self.vault.root / pane.note.path))
        for row in self.files.get_children():
            if row.path == pane.note.path:
                self.files.select_row(row)
                break
        self.section('Outline')
        for match in re.finditer(r'(?m)^(#{1,6})\s+(.+?)\s*#*$', pane.text()):
            offset = match.start()
            def jump(offset=offset):
                position = pane.buffer.get_iter_at_offset(offset)
                pane.buffer.place_cursor(position)
                pane.editor.scroll_to_iter(position, 0.15, False, 0, 0)
            self.context_button(match[2], jump)
        self.section('Outgoing links')
        for link in links(pane.text()):
            self.context_button(('↳ ' if link.embedded else '') + link.label,
                                lambda target=link.target: self.follow_link(pane.note.path, target))
        self.section('Backlinks · Saved files')
        for path in self.vault.backlinks(pane.note.path):
            self.context_button(path.removesuffix('.md'), lambda path=path: self.open_note(path))
        self.context.show_all()
        return False

    def refresh(self):
        if not self.vault:
            return True
        try:
            self.vault.refresh()
            self.populate_files()
            for path, pane in self.panes.items():
                disk_note = self.vault.notes.get(path)
                if disk_note is None:
                    self.status.set_text(f'{path} was removed or cannot be read. Open editor contents are retained.')
                elif disk_note.revision != pane.note.revision:
                    if pane.dirty:
                        self.status.set_text(f'External change to {path}. Your unsaved edits are retained; saving will stop on conflict.')
                    else:
                        pane.note = disk_note
                        pane.buffer.handler_block_by_func(pane.changed)
                        pane.buffer.set_text(disk_note.text.replace('\r\n', '\n'))
                        pane.buffer.handler_unblock_by_func(pane.changed)
                        pane.render()
            self.update_context()
        except (VaultError, OSError) as error:
            self.status.set_text(str(error))
        return True

    def key_press(self, _, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            action = {'s': self.save_current, 'o': self.choose_vault, 'n': self.new_note, 'w': self.close_tab}.get(Gdk.keyval_name(event.keyval).lower())
            if action:
                action()
                return True
        return False

    def on_delete(self, *_):
        return not self.confirm_panes(list(self.panes.values()))


def main():
    parser = argparse.ArgumentParser(description='Open a local Markdown vault in Libre Vault.')
    parser.add_argument('vault', nargs='?', help='Path to a vault folder')
    args = parser.parse_args()
    app = Gtk.Application(application_id='org.librevault.Prototype')
    app.connect('activate', lambda app: Window(app, args.vault).present())
    return app.run([])


if __name__ == '__main__':
    raise SystemExit(main())
