"""Native Linux notebook prototype. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
from urllib.parse import unquote
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

from .vault import Vault, VaultError, links
from .formatting import markdown_layout
from .editing import list_enter
from .filetree import tree_paths
from .commands import CommandPalette, KeybindCheatsheet
from .shortcuts import APP_SHORTCUTS
from .settings import SettingsDialog
from .preferences import PreferencesStore
from .appearance import PALETTES, stylesheet
from .panels import SlidingPanel


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
        self.headings = []
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
                foreground='#e4e4e4', pixels_above_lines=20)
        # This tag has highest priority, including inside headings and code.
        self.hidden = self.buffer.create_tag(None, invisible=True)
        self.editor = Gtk.TextView(buffer=self.buffer)
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.set_left_margin(40)
        self.editor.set_right_margin(40)
        self.editor.set_top_margin(30)
        self.editor.set_bottom_margin(80)
        # GTK 3's hit-testing converts a raw line byte count to a visible
        # index in bottom padding, which aborts when syntax is invisible.
        # Keep paragraph spacing above lines, where hit-testing starts at zero.
        self.editor.set_pixels_above_lines(5)
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
        self.apply_preferences()
        self.render()

    def apply_preferences(self):
        preferences = self.window.preferences
        colors = PALETTES[preferences.theme]
        self.tags['code'].set_property('background', colors['code_bg'])
        self.tags['code'].set_property('size-points', max(8, preferences.editor_size - 1))
        self.tags['metadata'].set_property('size-points', max(8, preferences.editor_size - 1))
        for name in ('quote', 'completed', 'metadata'):
            self.tags[name].set_property('foreground', colors['muted'])
        self.tags['link'].set_property('foreground', colors['soft'])
        for level, size in enumerate((25, 21, 18, 16, 14, 12), 1):
            self.tags[f'h{level}'].set_property('size-points', size * preferences.editor_size / 11)
            self.tags[f'h{level}'].set_property('foreground', colors['bright'])
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if preferences.wrap_lines else Gtk.WrapMode.NONE)
        self.size_editor(self.editor, self.editor.get_allocation())
        self.update_list_margins()

    def size_editor(self, _, allocation):
        preferences = self.window.preferences
        margin = max(32, (allocation.width - preferences.line_width) // 2) if preferences.readable_length else 32
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
        if not self.window.preferences.continue_lists:
            return False
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
        source_changed = self.text() != self.formatted_text
        self.render()
        if source_changed and self.window.current() is self:
            self.window.update_context()
        elif self.window.current() is self:
            self.window.update_outline_selection()
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
            self.spans, self.items, self.headings = layout.spans, layout.items, layout.headings
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
    def __init__(self, app, folder=None, preferences_path=None):
        super().__init__(application=app, title='Libre Vault')
        self.set_default_size(1360, 850)
        self.vault = None
        self.panes = {}
        self.outline_rows = []
        self.collapsed_folders = set()
        self.palette = None
        self.cheatsheet = None
        self.settings_dialog = None
        self.preferences_store = PreferencesStore(preferences_path)
        self.preferences = self.preferences_store.load()
        self.refresh_source = None
        self.connect('delete-event', self.on_delete)
        self.connect('key-press-event', self.key_press)
        self.css_provider = Gtk.CssProvider()
        self.apply_preferences()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
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
        self.notes_toggle.set_tooltip_text('Toggle file tree · Ctrl+Shift+E')
        self.notes_toggle.get_accessible().set_name('Toggle file tree')
        self.notes_toggle.set_active(True)
        self.notes_toggle.get_style_context().add_class('icon-button')
        header.pack_start(self.notes_toggle)
        self.details_toggle = Gtk.ToggleButton()
        self.details_toggle.set_image(Gtk.Image.new_from_icon_name('sidebar-show-right-symbolic', Gtk.IconSize.MENU))
        self.details_toggle.set_tooltip_text('Toggle note details · Ctrl+Shift+D')
        self.details_toggle.get_accessible().set_name('Toggle note details')
        self.details_toggle.set_active(True)
        self.details_toggle.get_style_context().add_class('icon-button')
        header.pack_end(self.details_toggle)
        header.pack_end(self.icon_button('document-save-symbolic', 'Save note · Ctrl+S', self.save_current))
        self.overlay = Gtk.Overlay()
        self.add(self.overlay)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.overlay.add(root)
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
        files_heading = Gtk.Label(label='Files', xalign=0)
        files_heading.get_style_context().add_class('caption')
        sidebar.pack_start(files_heading, False, False, 0)
        self.files = Gtk.ListBox()
        self.files.get_style_context().add_class('note-list')
        self.files.set_activate_on_single_click(True)
        self.files.connect('row-activated', self.activate_file)
        self.files.connect('key-press-event', self.files_key_press)
        self.files.connect('button-press-event', self.explorer_button_press)
        self.files.connect('popup-menu', self.explorer_popup_menu)
        files_scroll = scrolled(self.files, 212)
        files_scroll.connect('button-press-event', self.explorer_button_press)
        sidebar.pack_start(files_scroll, True, True, 0)
        hint = Gtk.Label(label='Alt+P  Commands\nCtrl+O  Open vault\nCtrl+N  New note\nCtrl+S  Save\nCtrl+W  Close tab', xalign=0)
        hint.get_style_context().add_class('shortcuts')
        hint.set_margin_top(12)
        sidebar.pack_start(hint, False, False, 0)
        self.notes_panel = SlidingPanel(sidebar, layout, at_start=True)
        layout.pack1(self.notes_panel, False, False)
        self.notes_toggle.connect('toggled', self.set_panel_open, self.notes_panel)
        center = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        center.get_style_context().add_class('workspace-split')
        self.tabs = Gtk.Notebook(scrollable=True)
        self.tabs.connect('switch-page', lambda *_: GLib.idle_add(self.update_context))
        center.pack1(self.tabs, True, False)
        self.context = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.context.set_border_width(12)
        self.context.set_size_request(240, -1)
        self.context.get_style_context().add_class('sidebar')
        self.details_scroll = scrolled(self.context)
        self.details_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.details_scroll.set_min_content_width(240)
        self.details_panel = SlidingPanel(self.details_scroll, center, at_start=False)
        center.pack2(self.details_panel, False, False)
        self.details_toggle.connect('toggled', self.set_panel_open, self.details_panel)
        layout.pack2(center, True, False)
        layout.set_position(232)
        center.set_position(900)
        self.status = Gtk.Label(label='Open a vault folder to begin.', xalign=0)
        self.status.get_style_context().add_class('status')
        statusbar = Gtk.Box()
        statusbar.get_style_context().add_class('statusbar')
        self.settings_button = self.icon_button('emblem-system-symbolic', 'Settings', self.show_settings)
        self.settings_button.get_style_context().add_class('settings-button')
        statusbar.pack_start(self.settings_button, False, False, 0)
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
        Gtk.StyleContext.remove_provider_for_screen(Gdk.Screen.get_default(), self.css_provider)
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
        self.collapsed_folders.clear()
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
        searching = bool(self.search_entry.get_text().strip())
        paths = self.vault.search(self.search_entry.get_text()) if self.vault else []
        if self.vault and not searching:
            paths = tree_paths(paths, self.vault.folders, self.collapsed_folders)
        rows = self.files.get_children()
        if [row.path for row in rows] == paths and all(
                row.search_result == searching and
                (not row.path.endswith('/') or row.expanded == (row.path[:-1] not in self.collapsed_folders))
                for row in rows):
            return
        for row in rows:
            self.files.remove(row)
        for path in paths:
            row = Gtk.ListBoxRow()
            row.path = path
            row.search_result = searching
            content = Gtk.Box(spacing=6)
            is_folder = path.endswith('/')
            if not searching:
                content.set_margin_start(path.rstrip('/').count('/') * 18)
                row.get_style_context().add_class('tree-row')
                if is_folder:
                    row.expanded = path[:-1] not in self.collapsed_folders
                    indicator = Gtk.Image.new_from_icon_name(
                        'pan-down-symbolic' if row.expanded else 'pan-end-symbolic', Gtk.IconSize.MENU)
                    row.get_accessible().set_name(
                        f"{Path(path).name}, folder, {'expanded' if row.expanded else 'collapsed'}")
                else:
                    indicator = Gtk.Box()
                indicator.set_size_request(16, -1)
                content.pack_start(indicator, False, False, 0)
            icon = Gtk.Image.new_from_icon_name(
                'folder-symbolic' if is_folder else 'text-x-generic-symbolic', Gtk.IconSize.MENU)
            icon.get_style_context().add_class('muted')
            content.pack_start(icon, False, False, 0)
            names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            label = Gtk.Label(label=Path(path).name if is_folder else Path(path).stem, xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            if is_folder:
                label.get_style_context().add_class('tree-folder')
            names.pack_start(label, False, False, 0)
            if searching and Path(path).parent != Path('.'):
                folder = Gtk.Label(label=str(Path(path).parent), xalign=0)
                folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                folder.get_style_context().add_class('note-folder')
                names.pack_start(folder, False, False, 0)
            content.pack_start(names, True, True, 0)
            row.add(content)
            row.set_tooltip_text(path)
            self.files.add(row)
        pane = self.current()
        if pane:
            for row in self.files.get_children():
                if row.path == pane.note.path:
                    self.files.select_row(row)
                    break
        self.files.show_all()

    def activate_file(self, _, row):
        if row.path.endswith('/'):
            self.toggle_folder(row.path)
        else:
            self.open_note(row.path)

    def toggle_folder(self, path):
        folder = path.rstrip('/')
        if folder in self.collapsed_folders:
            self.collapsed_folders.remove(folder)
        else:
            self.collapsed_folders.add(folder)
        self.populate_files()
        for row in self.files.get_children():
            if row.path == path:
                self.files.select_row(row)
                row.grab_focus()
                break

    def files_key_press(self, _, event):
        if self.search_entry.get_text().strip() or event.state & Gtk.accelerator_get_default_mod_mask():
            return False
        row = self.files.get_selected_row()
        if not row or event.keyval not in (Gdk.KEY_Left, Gdk.KEY_Right):
            return False
        if row.path.endswith('/'):
            if (event.keyval == Gdk.KEY_Right) != row.expanded:
                self.toggle_folder(row.path)
                return True
            if event.keyval == Gdk.KEY_Right:
                child = self.files.get_row_at_index(row.get_index() + 1)
                if child and child.path.startswith(row.path):
                    self.files.select_row(child)
                    child.grab_focus()
                return True
        parent = str(Path(row.path).parent) + '/'
        if event.keyval == Gdk.KEY_Left:
            for candidate in self.files.get_children():
                if candidate.path == parent:
                    self.files.select_row(candidate)
                    candidate.grab_focus()
                    return True
        return False

    def explorer_menu(self, row=None):
        parent = ''
        if row:
            parent = row.path.rstrip('/') if row.path.endswith('/') else str(Path(row.path).parent)
            if parent == '.':
                parent = ''
        menu = Gtk.Menu()
        menu.set_reserve_toggle_size(False)
        menu.get_style_context().add_class('explorer-menu')
        menu.attach_to_widget(self.files, None)
        for label, action in (
                ('New note…', lambda: self.new_note(parent=parent)),
                ('New subfolder…', lambda: self.new_folder(parent=parent))):
            item = Gtk.MenuItem(label=label)
            item.set_sensitive(self.vault is not None)
            item.connect('activate', lambda _, action=action: action())
            menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label='Open vault…')
        item.connect('activate', self.choose_vault)
        menu.append(item)
        menu.connect('selection-done', lambda menu: menu.destroy())
        menu.show_all()
        return menu

    def explorer_button_press(self, _, event):
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        source = Gtk.get_event_widget(event)
        position = source.translate_coordinates(self.files, int(event.x), int(event.y))
        row = self.files.get_row_at_y(position[1]) if position else None
        if row:
            self.files.select_row(row)
        self.explorer_menu(row).popup_at_pointer(event)
        return True

    def explorer_popup_menu(self, *_):
        self.explorer_menu(self.files.get_selected_row()).popup_at_widget(
            self.files, Gdk.Gravity.NORTH_WEST, Gdk.Gravity.NORTH_WEST, None)
        return True

    def open_note(self, relative, fragment=''):
        if relative not in self.panes:
            def action():
                pane = NotePane(self, self.vault.read(relative))
                self.panes[relative] = pane
                tab = Gtk.Box(spacing=6)
                tab.set_tooltip_text(relative)
                tab.pack_start(pane.tab_label, True, True, 0)
                close = self.icon_button('window-close-symbolic', 'Close note · Ctrl+W',
                                         lambda *_: self.close_note(pane))
                close.set_focus_on_click(False)
                tab.pack_end(close, False, False, 0)
                tab.show_all()
                self.tabs.append_page(pane, tab)
                pane.show_all()
            self.try_action(action)
        if relative not in self.panes:
            return
        pane = self.panes[relative]
        ancestors = {str(parent) for parent in Path(relative).parents}
        if self.collapsed_folders & ancestors:
            self.collapsed_folders.difference_update(ancestors)
            self.populate_files()
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
        self.close_note(self.current())

    def close_note(self, pane):
        if pane and self.confirm_panes([pane]):
            self.tabs.remove_page(self.tabs.page_num(pane))
            self.panes.pop(pane.note.path, None)
            self.update_context()

    def new_note(self, *_, parent=''):
        self.new_item('note', parent)

    def new_folder(self, *_, parent=''):
        self.new_item('folder', parent)

    def new_item(self, kind, parent):
        if not self.vault:
            self.choose_vault()
            return
        dialog = Gtk.Dialog(title=f'New {kind}', transient_for=self, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Create', Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text=f'{kind.capitalize()} name or existing-folder/{kind.capitalize()} name')
        entry.set_text(parent + '/' if parent else '')
        entry.set_position(-1)
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry.set_size_request(430, -1)
        entry.set_margin_top(16)
        entry.set_margin_bottom(16)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            create = self.vault.create if kind == 'note' else self.vault.create_folder
            created = self.try_action(lambda: create(entry.get_text()))
            if created is not None:
                path = created.path if kind == 'note' else created
                self.collapsed_folders.difference_update(str(parent) for parent in Path(path).parents)
                self.collapsed_folders.discard(path)
                self.search_entry.set_text('')
                self.populate_files()
                if kind == 'note':
                    self.open_note(created.path)
                else:
                    for row in self.files.get_children():
                        if row.path == created + '/':
                            self.files.select_row(row)
                            break
                    self.status.set_text(f'Created folder · {created}')
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
        return button

    def update_outline_selection(self):
        pane = self.current()
        if not pane:
            return
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        active = None
        for offset, button in self.outline_rows:
            if offset <= cursor:
                active = button
        for _, button in self.outline_rows:
            style = button.get_style_context()
            if button is active:
                style.add_class('current-heading')
            else:
                style.remove_class('current-heading')

    def follow_link(self, source, target):
        candidates = self.vault.resolve(source, target)
        fragment = unquote(target.partition('#')[2])
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
        self.outline_rows = []
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
        if pane.text() != pane.formatted_text:
            pane.render()
        ancestors = []
        for heading in pane.headings:
            while ancestors and ancestors[-1] >= heading.level:
                ancestors.pop()
            depth = len(ancestors)
            ancestors.append(heading.level)
            offset = heading.start
            def jump(offset=offset):
                position = pane.buffer.get_iter_at_offset(offset)
                pane.buffer.place_cursor(position)
                pane.editor.scroll_to_iter(position, 0.15, False, 0, 0)
                self.update_outline_selection()
            button = self.context_button(heading.label, jump)
            button.set_margin_start(depth * 12)
            button.get_style_context().add_class('outline-heading')
            if depth == 0:
                button.get_style_context().add_class('outline-root')
                button.set_margin_top(4)
            label = button.get_child()
            label.set_ellipsize(Pango.EllipsizeMode.NONE)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_max_width_chars(24)
            button.get_accessible().set_name(f'Heading {heading.level}: {heading.label}')
            self.outline_rows.append((offset, button))
        if not pane.headings:
            empty = Gtk.Label(label='No headings in this note.', xalign=0)
            empty.get_style_context().add_class('outline-empty')
            self.context.pack_start(empty, False, False, 0)
        self.update_outline_selection()
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
            self.collapsed_folders.intersection_update(self.vault.folders)
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
        if self.palette is not None:
            return self.palette.key_press(self.palette, event)
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        for shortcut in APP_SHORTCUTS:
            key, mask = Gtk.accelerator_parse(shortcut.accelerator)
            if Gdk.keyval_to_lower(event.keyval) == key and modifiers == mask:
                getattr(self, shortcut.handler)()
                return True
        return False

    def toggle_details(self, *_):
        self.details_toggle.set_active(not self.details_toggle.get_active())

    def set_panel_open(self, button, panel):
        opened = panel.set_open(button.get_active())
        if button.get_active() != opened:
            button.set_active(opened)

    def toggle_file_tree(self, *_):
        self.notes_toggle.set_active(not self.notes_toggle.get_active())

    def apply_preferences(self):
        Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', self.preferences.theme == 'dark')
        self.css_provider.load_from_data(stylesheet(CSS.decode('utf-8'), self.preferences).encode('utf-8'))
        for pane in self.panes.values():
            pane.apply_preferences()

    def set_preferences(self, preferences):
        self.preferences_store.save(preferences)
        for window in self.get_application().get_windows():
            if isinstance(window, Window):
                window.preferences = preferences
                window.apply_preferences()
                if window.settings_dialog is not None:
                    window.settings_dialog.sync_controls()

    def show_settings(self, *_):
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self)
            self.settings_dialog.set_destroy_with_parent(True)
            self.settings_dialog.connect('destroy', lambda *_: setattr(self, 'settings_dialog', None))
        else:
            self.settings_dialog.present()

    def show_command_palette(self):
        if self.palette is None:
            self.palette = CommandPalette(self)
            self.palette.connect('destroy', lambda *_: setattr(self, 'palette', None))
            self.overlay.add_overlay(self.palette)
            self.palette.show_all()
            self.palette.filter_commands()
            self.palette.search.grab_focus()
        else:
            self.palette.search.grab_focus()

    def show_keybinds(self):
        if self.cheatsheet is None:
            self.cheatsheet = KeybindCheatsheet(self)
            self.cheatsheet.set_destroy_with_parent(True)
            self.cheatsheet.connect('destroy', lambda *_: setattr(self, 'cheatsheet', None))
        else:
            self.cheatsheet.present()

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
