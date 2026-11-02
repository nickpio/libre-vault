"""Native Linux notebook prototype. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import argparse
import math
import posixpath
from pathlib import Path
import re
from urllib.parse import unquote
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, Pango

from .vault import Vault, VaultError, links
from .monitor import VaultMonitor
from .recovery import RecoveryStore
from .formatting import markdown_layout
from .editing import TextEdit, format_inline, list_enter, markdown_link
from .history import BufferHistory
from .findbar import FindBar
from .graph_ui import GraphView
from .filetree import tree_paths
from .metadata import note_metadata, property_text, with_properties
from .commands import CommandPalette, KeybindCheatsheet
from .shortcuts import APP_SHORTCUTS, EDITOR_SHORTCUTS
from .settings import SettingsDialog
from .preferences import PreferencesStore
from .appearance import PALETTES, stylesheet
from .panels import SlidingPanel
from .attachments import attachment_markdown, local_path, store_attachment
from .reading import ReadingView
from .navigation_ui import EditorNavigation
from .navigation import Location, NavigationHistory
from .workspace import LastVaultStore, WorkspaceStore


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
        self.autosave_source = None
        self.format_source = None
        self.formatted_text = None
        self.spans = []
        self.items = []
        self.headings = []
        self.list_tags = {}
        self.list_widgets = []
        self.image_widgets = []
        self.position_source = None
        self.mode = 'live'
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
        self.editor.connect('populate-popup', self.populate_edit_menu)
        self.editor.connect('paste-clipboard', self.paste_attachment)
        self.editor.drag_dest_set(Gtk.DestDefaults.ALL,
                                  [Gtk.TargetEntry.new('text/uri-list', 0, 1)], Gdk.DragAction.COPY)
        self.editor.connect('drag-data-received', self.drop_attachments)
        self.scroll = scrolled(self.editor)
        self.scroll.get_vadjustment().connect('value-changed', self.queue_positions)
        self.scroll.get_hadjustment().connect('value-changed', self.queue_positions)
        self.content_stack = Gtk.Stack()
        self.content_stack.add_named(self.scroll, 'editor')
        self.reading = ReadingView(self)
        self.content_stack.add_named(self.reading, 'reading')
        self.pack_start(self.content_stack, True, True, 0)
        toolbar = self.toolbar = Gtk.Box(spacing=8)
        toolbar.set_border_width(6)
        mode = self.mode_selector = Gtk.ComboBoxText()
        for value, title in [('live', 'Live preview'), ('source', 'Source'), ('reading', 'Reading')]:
            mode.append(value, title)
        mode.set_active_id('live')
        mode.connect('changed', lambda field: self.set_mode(field.get_active_id()))
        mode.get_accessible().set_name('Note view mode')
        toolbar.pack_start(mode, False, False, 0)
        attach = Gtk.Button.new_from_icon_name('mail-attachment-symbolic', Gtk.IconSize.BUTTON)
        attach.set_tooltip_text('Insert attachment')
        attach.connect('clicked', lambda *_: self.choose_attachment())
        toolbar.pack_end(attach, False, False, 0)
        self.tab_label = Gtk.Label(label=Path(note.path).stem)
        self.history = BufferHistory(self.buffer)
        self.findbar = FindBar(self)
        self.pack_start(self.findbar, False, False, 0)
        self.reorder_child(self.findbar, 0)
        self.buffer_handlers = [self.buffer.connect('changed', self.changed),
                                self.buffer.connect('mark-set', self.cursor_moved)]
        self.connect('destroy', self.cleanup)
        self.apply_preferences()
        self.render()
        self.navigation = EditorNavigation(self)

    def set_mode(self, mode):
        if mode not in ('live', 'source', 'reading'):
            return
        self.mode = mode
        if self.mode_selector.get_active_id() != mode:
            self.mode_selector.set_active_id(mode)
        self.content_stack.set_visible_child_name('reading' if mode == 'reading' else 'editor')
        self.render()
        if mode == 'reading':
            self.findbar.close()
            self.reading.refresh(force=True)
        else:
            self.reading.clear()
            self.editor.grab_focus()

    def insert_attachment_bytes(self, name, data):
        relative = store_attachment(self.window.vault, name, data)
        import posixpath
        target = posixpath.relpath(relative, str(Path(self.note.path).parent))
        selection = self.buffer.get_selection_bounds()
        if selection:
            start, end = (it.get_offset() for it in selection)
        else:
            start = end = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
        markup = attachment_markdown(target)
        if markup.startswith('!'):
            text = self.text()
            line_start = text.rfind('\n', 0, start) + 1
            line_end = text.find('\n', end)
            if line_end < 0:
                line_end = len(text)
            if text[line_start:start].strip():
                markup = '\n' + markup
            if text[end:line_end].strip():
                markup += '\n'
        self.apply_edit(TextEdit(start, end, markup))
        return relative

    def choose_attachment(self):
        dialog = Gtk.FileChooserDialog(title='Insert attachment', transient_for=self.window,
                                       action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Insert', Gtk.ResponseType.OK)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                path = Path(dialog.get_filename())
                self.window.try_action(lambda: self.insert_attachment_bytes(path.name, path.read_bytes()))
        finally:
            dialog.destroy()

    def paste_attachment(self, view):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        image = clipboard.wait_for_image()
        if image is None:
            return
        view.stop_emission_by_name('paste-clipboard')
        success, data = image.save_to_bufferv('png', [], [])
        if success:
            self.window.try_action(lambda: self.insert_attachment_bytes('Screenshot.png', data))

    def drop_attachments(self, _, context, x, y, selection, info, time):
        success = False
        try:
            for uri in selection.get_uris() or []:
                file = Gio.File.new_for_uri(uri)
                filename = file.get_path()
                if filename is None:
                    raise VaultError('Drop a local file to add an attachment.')
                path = Path(filename)
                self.insert_attachment_bytes(path.name, path.read_bytes())
                success = True
        except (OSError, VaultError) as error:
            self.window.error(error)
        Gtk.drag_finish(context, success, False, time)

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
        self.findbar.cached = None
        self.findbar.refresh()
        if self.mode == 'reading':
            self.reading.refresh()

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
        self.window.queue_reading_refresh()
        self.dirty = self.text() != self.note.text.replace('\r\n', '\n')
        self.tab_label.set_text(('• ' if self.dirty else '') + Path(self.note.path).stem)
        self.window.status.set_text('Unsaved changes · Ctrl+S to save' if self.dirty
                                    else f'No unsaved changes · {self.note.path}')
        if self.autosave_source is not None:
            GLib.source_remove(self.autosave_source)
            self.autosave_source = None
        try:
            if self.dirty:
                self.window.recovery.write(self.note, self.text())
            else:
                self.window.recovery.remove(self.note.path)
        except OSError as error:
            self.window.status.set_text(f'Could not write recovery draft: {error}')
        if self.dirty:
            self.autosave_source = GLib.timeout_add_seconds(2, self.autosave)
        self.queue_format()

    def autosave(self):
        self.autosave_source = None
        if self.window.confirming_panes:
            self.autosave_source = GLib.timeout_add_seconds(2, self.autosave)
            return False
        try:
            self.save()
        except (VaultError, OSError) as error:
            self.window.status.set_text(f'Autosave failed: {error}')
        return False

    def editor_key_press(self, _, event):
        if hasattr(self, 'navigation') and self.navigation.key_press(event):
            return True
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        for shortcut in EDITOR_SHORTCUTS:
            key, mask = Gtk.accelerator_parse(shortcut.accelerator)
            if Gdk.keyval_to_lower(event.keyval) == Gdk.keyval_to_lower(key) and modifiers == mask:
                getattr(self, shortcut.handler)()
                return True
        if event.keyval == Gdk.KEY_Escape and self.findbar.get_visible():
            self.findbar.close()
            return True
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
        self.findbar.refresh()
        if source_changed and self.window.current() is self:
            self.window.update_context()
        elif self.window.current() is self:
            self.window.update_outline_selection()
        return False

    def cleanup(self, *_):
        self.history.close()
        for handler in self.buffer_handlers:
            self.buffer.disconnect(handler)
        self.buffer_handlers.clear()
        self.toolbar.destroy()
        if self.autosave_source is not None:
            GLib.source_remove(self.autosave_source)
            self.autosave_source = None
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
            self.rebuild_image_widgets(layout.images)
        start, end = self.buffer.get_bounds()
        for tag in [*self.tags.values(), *self.list_tags.values(), self.hidden,
                    *(entry[3] for entry in self.image_widgets)]:
            self.buffer.remove_tag(tag, start, end)
        if self.mode == 'source':
            for _, widget in self.list_widgets:
                widget.hide()
            for _, _, widget, tag in self.image_widgets:
                widget.hide()
            return
        if self.mode == 'reading':
            self.reading.refresh()
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
        for image_start, image_end, widget, tag in self.image_widgets:
            editing_image = image_start <= cursor < image_end
            in_selection = selected and image_start < selected[1] and image_end > selected[0]
            visible = self.mode == 'live' and not editing_image and not in_selection
            widget.set_visible(bool(visible))
            if visible:
                left = self.buffer.get_iter_at_offset(image_start)
                right = self.buffer.get_iter_at_offset(image_end)
                self.buffer.apply_tag(self.hidden, left, right)
                self.buffer.apply_tag(tag, left, right)
        self.queue_positions()

    def rebuild_image_widgets(self, images):
        for _, _, widget, tag in self.image_widgets:
            widget.destroy()
            self.buffer.get_tag_table().remove(tag)
        self.image_widgets.clear()
        for start, end, target in images:
            try:
                path = local_path(self.window.vault, self.note.path, target)
                info = GdkPixbuf.Pixbuf.get_file_info(str(path))
                if not info or not info[1] or not info[2]:
                    continue
                scale = min(1, 600 / info[1], 360 / info[2])
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), max(1, int(info[1] * scale)), max(1, int(info[2] * scale)), True)
            except (OSError, GLib.Error, VaultError):
                continue
            widget = Gtk.Image.new_from_pixbuf(pixbuf)
            widget.set_tooltip_text('Place the cursor inside the image Markdown to edit it')
            self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
            tag = self.buffer.create_tag(None, pixels_above_lines=pixbuf.get_height() + 8)
            self.image_widgets.append((start, end, widget, tag))

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
        for start, _, widget, _ in self.image_widgets:
            if not widget.get_visible():
                continue
            position = self.buffer.get_iter_at_offset(start)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            height = widget.get_preferred_height().natural_height
            self.editor.move_child(widget, x, y - height - 4)
        return False

    def save(self):
        if self.dirty:
            self.note = self.window.vault.save(self.note, self.text())
            self.dirty = False
            self.tab_label.set_text(Path(self.note.path).stem)
            try:
                self.window.recovery.remove(self.note.path)
            except OSError as error:
                self.window.status.set_text(f'Saved, but could not remove recovery draft: {error}')
                return
        self.history.break_group()
        self.window.status.set_text(f'Saved · {self.note.path}')
        self.window.update_context()

    def undo(self):
        if self.history.undo():
            self.editor.grab_focus()
            self.editor.scroll_mark_onscreen(self.buffer.get_insert())

    def redo(self):
        if self.history.redo():
            self.editor.grab_focus()
            self.editor.scroll_mark_onscreen(self.buffer.get_insert())

    def apply_edit(self, edit, selection=None, user_action=True):
        self.history.break_group()
        if user_action:
            self.buffer.begin_user_action()
        try:
            if self.text()[edit.start:edit.end] != edit.replacement:
                start = self.buffer.get_iter_at_offset(edit.start)
                self.buffer.delete(start, self.buffer.get_iter_at_offset(edit.end))
                self.buffer.insert(start, edit.replacement)
            if selection is None:
                end = edit.start + len(edit.replacement)
                selection = (end, end)
            self.buffer.select_range(self.buffer.get_iter_at_offset(selection[1]),
                                     self.buffer.get_iter_at_offset(selection[0]))
        finally:
            if user_action:
                self.buffer.end_user_action()

    def emphasize(self, marker):
        selection = self.buffer.get_selection_bounds()
        if selection:
            start, end = (it.get_offset() for it in selection)
        else:
            start = end = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
        edit, selected = format_inline(self.text(), start, end, marker)
        self.apply_edit(edit, selected)
        self.editor.grab_focus()

    def bold(self):
        self.emphasize('**')

    def italic(self):
        self.emphasize('*')

    def insert_link(self):
        original = self.text()
        selection = self.buffer.get_selection_bounds()
        if selection:
            start, end = (it.get_offset() for it in selection)
        else:
            start = end = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
        dialog = Gtk.Dialog(title='Insert link', transient_for=self.window, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Insert', Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(8)
        label = Gtk.Entry(placeholder_text='Link text')
        label.set_text(original[start:end])
        label.get_accessible().set_name('Link text')
        target = Gtk.Entry(placeholder_text='URL or note path')
        target.get_accessible().set_name('Link target')
        target.set_size_request(400, -1)
        for field in (label, target):
            field.set_activates_default(True)
            content.pack_start(field, False, False, 0)
        target.connect('changed', lambda *_: dialog.set_response_sensitive(
            Gtk.ResponseType.OK, bool(target.get_text().strip())))
        dialog.set_response_sensitive(Gtk.ResponseType.OK, False)
        dialog.show_all()
        target.grab_focus()
        try:
            if dialog.run() == Gtk.ResponseType.OK and target.get_text().strip():
                if self.text() != original:
                    self.window.status.set_text('The note changed while the link dialog was open. Select text and try again.')
                    return
                source = markdown_link(label.get_text(), target.get_text().strip())
                self.apply_edit(TextEdit(start, end, source))
                self.editor.grab_focus()
        finally:
            dialog.destroy()

    def show_find(self):
        self.findbar.open()

    def show_replace(self):
        self.findbar.open(replace=True)

    def find_next(self):
        self.findbar.step(True)

    def find_previous(self):
        self.findbar.step(False)

    def populate_edit_menu(self, _, menu):
        menu.set_reserve_toggle_size(False)
        menu.get_style_context().add_class('editor-menu')
        items = []
        for title, action, enabled in (
                ('Undo', self.undo, self.history.can_undo), ('Redo', self.redo, self.history.can_redo),
                ('Bold', self.bold, True), ('Italic', self.italic, True),
                ('Insert link…', self.insert_link, True),
                ('Insert attachment…', self.choose_attachment, True),
                ('Live preview', lambda: self.set_mode('live'), True),
                ('Source mode', lambda: self.set_mode('source'), True),
                ('Reading mode', lambda: self.set_mode('reading'), True),
                ('Find…', self.show_find, True), ('Replace…', self.show_replace, True)):
            item = Gtk.MenuItem(label=title)
            item.set_sensitive(enabled)
            item.connect('activate', lambda _, action=action: action())
            items.append(item)
        items.append(Gtk.SeparatorMenuItem())
        for item in reversed(items):
            menu.prepend(item)
            item.show()

    def load_external(self, note):
        cursor = self.history.selection()
        self.note = note
        text = note.text.replace('\r\n', '\n')
        self.buffer.handler_block_by_func(self.changed)
        try:
            with self.history.suspended():
                self.buffer.set_text(text)
                self.buffer.select_range(self.buffer.get_iter_at_offset(min(cursor.cursor, len(text))),
                                         self.buffer.get_iter_at_offset(min(cursor.anchor, len(text))))
        finally:
            self.buffer.handler_unblock_by_func(self.changed)
        self.history.reset()
        self.dirty = False
        self.tab_label.set_text(Path(note.path).stem)
        self.render()
        self.findbar.refresh()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, folder=None, preferences_path=None):
        super().__init__(application=app, title='Libre Vault')
        self.set_default_size(1360, 850)
        self.vault = None
        self.recovery = None
        self.confirming_panes = False
        self.panes = {}
        self.outline_rows = []
        self.collapsed_folders = set()
        self.palette = None
        self.cheatsheet = None
        self.settings_dialog = None
        self.graph_window = None
        self.graph_views = set()
        self.preferences_store = PreferencesStore(preferences_path)
        self.preferences = self.preferences_store.load()
        self.workspace_store = WorkspaceStore(self.preferences_store.path)
        self.last_vault_store = LastVaultStore(self.preferences_store.path)
        self.navigation_history = NavigationHistory()
        self.restoring_workspace = False
        self.split_pane = None
        self.split_view = None
        self.workspace_source = None
        self.scroll_restore_sources = {}
        self.quick_switcher = None
        self.vault_monitor = None
        self.reading_source = None
        self.panel_widths_initialized = False
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
        self.save_button = self.icon_button('document-save-symbolic', 'Save note · Ctrl+S', self.save_current)
        header.pack_end(self.save_button)
        header.pack_start(self.icon_button('view-grid-symbolic', 'Graph view · Ctrl+Alt+G', self.show_graph))
        header.pack_start(self.icon_button('go-previous-symbolic', 'Navigate back · Alt+Left', self.navigate_back))
        header.pack_start(self.icon_button('go-next-symbolic', 'Navigate forward · Alt+Right', self.navigate_forward))
        header.pack_end(self.icon_button('view-dual-symbolic', 'Toggle split editor · Ctrl+Shift+S', self.toggle_split))
        self.overlay = Gtk.Overlay()
        self.add(self.overlay)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.overlay.add(root)
        self.workspace_stack = Gtk.Stack()
        self.workspace_stack.set_homogeneous(False)
        root.pack_start(self.workspace_stack, True, True, 0)
        welcome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        welcome.set_halign(Gtk.Align.CENTER)
        welcome.set_valign(Gtk.Align.CENTER)
        welcome.set_border_width(32)
        icon = Gtk.Image.new_from_icon_name('folder-documents-symbolic', Gtk.IconSize.DIALOG)
        icon.get_style_context().add_class('muted')
        welcome.pack_start(icon, False, False, 0)
        title = Gtk.Label(label='Welcome to Libre Vault')
        title.get_style_context().add_class('welcome-title')
        welcome.pack_start(title, False, False, 0)
        description = Gtk.Label(label='Your notes stay in ordinary Markdown files.\nOpen a vault or an empty folder to begin.')
        description.set_justify(Gtk.Justification.CENTER)
        description.set_line_wrap(True)
        description.set_max_width_chars(48)
        description.get_style_context().add_class('muted')
        welcome.pack_start(description, False, False, 0)
        self.open_vault_button = Gtk.Button(label='Open vault…')
        self.open_vault_button.set_halign(Gtk.Align.CENTER)
        self.open_vault_button.get_style_context().add_class('welcome-open')
        self.open_vault_button.connect('clicked', self.choose_vault)
        welcome.pack_start(self.open_vault_button, False, False, 4)
        hint = Gtk.Label(label='Ctrl+O to choose a folder')
        hint.get_style_context().add_class('welcome-hint')
        welcome.pack_start(hint, False, False, 0)
        self.workspace_stack.add_named(welcome, 'welcome')
        layout = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        layout.get_style_context().add_class('workspace-split')
        self.workspace_stack.add_named(layout, 'workspace')
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
        hint = Gtk.Button()
        hint.get_style_context().add_class('sidebar-command')
        hint.set_margin_top(12)
        hint.set_tooltip_text('Open command palette · Alt+P')
        hint.get_accessible().set_name('Open command palette')
        hint.connect('clicked', lambda *_: self.show_command_palette())
        hint_content = Gtk.Box(spacing=8)
        icon = Gtk.Image.new_from_icon_name('input-keyboard-symbolic', Gtk.IconSize.MENU)
        icon.get_style_context().add_class('muted')
        hint_content.pack_start(icon, False, False, 0)
        label = Gtk.Label(label='Commands', xalign=0)
        label.get_style_context().add_class('sidebar-command-label')
        hint_content.pack_start(label, True, True, 0)
        keys = Gtk.Label(label='Alt+P')
        keys.get_style_context().add_class('keybind-keys')
        hint_content.pack_end(keys, False, False, 0)
        hint.add(hint_content)
        sidebar.pack_start(hint, False, False, 0)
        self.notes_panel = SlidingPanel(sidebar, layout, at_start=True)
        layout.pack1(self.notes_panel, False, False)
        self.notes_toggle.connect('toggled', self.set_panel_open, self.notes_panel)
        center = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        center.get_style_context().add_class('workspace-split')
        self.tabs = Gtk.Notebook(scrollable=True)
        self.tabs.connect('switch-page', self.tab_switched)
        self.tabs.connect('page-removed', self.tab_removed)
        self.note_toolbars = Gtk.Stack()
        self.note_toolbars.set_homogeneous(False)
        self.note_toolbars.set_no_show_all(True)
        editor_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        editor_column.pack_start(self.note_toolbars, False, False, 0)
        editor_column.pack_start(self.tabs, True, True, 0)
        self.editor_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.editor_split.pack1(editor_column, True, False)
        self.split_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.split_box.set_no_show_all(True)
        self.editor_split.pack2(self.split_box, True, False)
        center.pack1(self.editor_split, True, False)
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
        center.connect('size-allocate', self.initialize_panel_widths)
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
        self.workspace_source = GLib.timeout_add_seconds(5, self.persist_workspace)
        self.connect('destroy', self.cleanup)
        self.show_all()
        self.update_workspace_state()
        if folder:
            self.try_action(lambda: self.load_vault(folder))
        else:
            last_vault = self.last_vault_store.load()
            if last_vault:
                try:
                    self.load_vault(last_vault)
                except (VaultError, OSError) as error:
                    self.status.set_text(f'Last vault unavailable · {error}')
            if self.vault is None:
                self.open_vault_button.grab_focus()

    def icon_button(self, icon, tooltip, action):
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
        button.get_style_context().add_class('icon-button')
        button.set_tooltip_text(tooltip)
        button.get_accessible().set_name(tooltip.split(' · ')[0])
        button.connect('clicked', action)
        return button

    def cleanup(self, *_):
        if self.graph_window:
            self.graph_window.destroy()
        self.persist_workspace()
        if self.reading_source is not None:
            GLib.source_remove(self.reading_source)
            self.reading_source = None
        # These widgets live in implicit GtkViewports. Explicitly dispose
        # them so native signal closures cannot retain the window.
        self.files.destroy()
        self.context.destroy()
        for source in self.scroll_restore_sources.values():
            GLib.source_remove(source)
        self.scroll_restore_sources.clear()
        if self.workspace_source:
            GLib.source_remove(self.workspace_source)
            self.workspace_source = None
        Gtk.StyleContext.remove_provider_for_screen(Gdk.Screen.get_default(), self.css_provider)
        if self.recovery:
            self.recovery.close()
        if self.vault_monitor:
            self.vault_monitor.close()
            self.vault_monitor = None
        if self.vault:
            self.vault.close()

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
        self.confirming_panes = True
        try:
            return self._confirm_panes(panes)
        finally:
            self.confirming_panes = False

    def _confirm_panes(self, panes):
        discarded = []
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
            elif result == Gtk.ResponseType.NO:
                discarded.append(pane)
            else:
                return False
        for pane in discarded:
            try:
                self.recovery.remove(pane.note.path)
            except OSError as error:
                self.error(error)
                return False
        return True

    def load_vault(self, folder):
        vault = Vault(folder)
        if not self.confirm_panes(list(self.panes.values())):
            vault.close()
            return
        self.persist_workspace()
        self.release_vault()
        self.vault = vault
        self.navigation_history = NavigationHistory()
        self.recovery = RecoveryStore(vault.root)
        self.update_workspace_state()
        self.collapsed_folders.clear()
        self.vault_label.set_text(vault.root.name)
        self.set_title(f'{vault.root.name} · Libre Vault')
        self.search_entry.set_text('')
        self.populate_files()
        self.status.set_text(f'{len(vault.notes)} notes · {vault.root}')
        self.offer_recovery()
        restored = self.restore_workspace()
        if vault.notes and not self.panes and not restored:
            first = 'Welcome.md' if 'Welcome.md' in vault.notes else next(iter(vault.notes))
            self.open_note(first)
        self.update_context()
        self.vault_monitor = VaultMonitor(vault, self.refresh, self.status.set_text)
        try:
            self.last_vault_store.save(vault.root)
        except OSError as error:
            self.status.set_text(f'Could not remember the opened vault: {error}')

    def release_vault(self):
        if self.graph_window:
            self.graph_window.destroy()
        if self.vault_monitor:
            self.vault_monitor.close()
            self.vault_monitor = None
        if self.reading_source is not None:
            GLib.source_remove(self.reading_source)
            self.reading_source = None
        for source in self.scroll_restore_sources.values():
            GLib.source_remove(source)
        self.scroll_restore_sources.clear()
        for overlay in (self.palette, self.quick_switcher):
            if overlay is not None:
                overlay.destroy()
        self.close_split()
        while self.tabs.get_n_pages():
            pane = self.tabs.get_nth_page(0)
            self.tabs.remove_page(0)
            pane.destroy()
        self.panes.clear()
        if self.recovery:
            self.recovery.close()
            self.recovery = None
        if self.vault:
            self.vault.close()

    def close_vault(self, *_):
        if self.vault is None:
            return True
        if not self.confirm_panes(list(self.panes.values())):
            return False
        try:
            self.last_vault_store.save(None)
        except OSError as error:
            self.error(error)
            return False
        self.persist_workspace()
        self.release_vault()
        self.vault = None
        self.navigation_history = NavigationHistory()
        self.collapsed_folders.clear()
        self.vault_label.set_text('No vault open')
        self.set_title('Libre Vault')
        self.search_entry.set_text('')
        self.populate_files()
        self.update_workspace_state()
        self.update_context()
        self.status.set_text('Open a vault folder to begin.')
        self.open_vault_button.grab_focus()
        return True

    def initialize_panel_widths(self, center, allocation):
        # The workspace can start hidden behind the welcome screen. Wait for
        # its real allocation before positioning the right-hand divider.
        if self.panel_widths_initialized or self.vault is None or allocation.width <= 1:
            return
        width = max(self.notes_panel.get_preferred_width().minimum_width,
                    self.details_panel.get_preferred_width().minimum_width)
        self.panel_widths_initialized = True
        self.notes_panel.position_width(width)
        self.details_panel.position_width(width)

    def update_workspace_state(self):
        opened = self.vault is not None
        self.workspace_stack.set_visible_child_name('workspace' if opened else 'welcome')
        self.notes_toggle.set_visible(opened)
        self.details_toggle.set_visible(opened)
        self.save_button.set_visible(opened)
        self.save_button.set_sensitive(opened and self.current() is not None)

    def populate_files(self):
        searching = bool(self.search_entry.get_text().strip())
        results = {result.path: result for result in self.vault.search_results(self.search_entry.get_text())} if self.vault else {}
        paths = list(results)
        if self.vault and not searching:
            paths = tree_paths(paths, self.vault.folders, self.collapsed_folders)
        rows = self.files.get_children()
        if [row.path for row in rows] == paths and all(
                row.search_result == searching and
                (not searching or getattr(row, 'search_signature', None) == results[row.path]) and
                (not row.path.endswith('/') or row.expanded == (row.path[:-1] not in self.collapsed_folders))
                for row in rows):
            return
        for row in rows:
            row.destroy()
        for path in paths:
            row = Gtk.ListBoxRow()
            row.path = path
            row.search_result = searching
            row.search_signature = results.get(path) if searching else None
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
            if searching and results[path].snippet:
                result = results[path]
                markup, start = [], 0
                for begin, end in result.highlights:
                    markup.append(GLib.markup_escape_text(result.snippet[start:begin]))
                    markup.append('<b>' + GLib.markup_escape_text(result.snippet[begin:end]) + '</b>')
                    start = end
                markup.append(GLib.markup_escape_text(result.snippet[start:]))
                snippet = Gtk.Label(xalign=0)
                snippet.set_markup(''.join(markup))
                snippet.set_ellipsize(Pango.EllipsizeMode.END)
                snippet.set_max_width_chars(24)
                snippet.get_style_context().add_class('note-folder')
                names.pack_start(snippet, False, False, 0)
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
        if row is not None:
            menu.append(Gtk.SeparatorMenuItem())
            for label, action in (
                    ('Rename…', lambda: self.rename_item(row.path)),
                    ('Move to…', lambda: self.move_item(row.path)),
                    ('Move to trash…', lambda: self.trash_item(row.path))):
                item = Gtk.MenuItem(label=label)
                item.connect('activate', lambda _, action=action: action())
                menu.append(item)
        menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label='Open vault…')
        item.connect('activate', self.choose_vault)
        menu.append(item)
        item = Gtk.MenuItem(label='Close vault')
        item.set_sensitive(self.vault is not None)
        item.connect('activate', self.close_vault)
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

    def rename_item(self, relative):
        self.organize_item(relative, rename=True)

    def move_item(self, relative):
        self.organize_item(relative, rename=False)

    def organize_item(self, relative, rename=False):
        relative = relative.rstrip('/')
        dialog = Gtk.Dialog(title='Rename' if rename else 'Move to', transient_for=self, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Rename' if rename else 'Move', Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(8)
        label = Gtk.Label(label='New name' if rename else 'Destination path inside the vault', xalign=0)
        content.pack_start(label, False, False, 0)
        entry = Gtk.Entry(text=Path(relative).name if rename else relative)
        entry.set_activates_default(True)
        content.pack_start(entry, False, False, 0)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        entry.grab_focus()
        accepted = dialog.run() == Gtk.ResponseType.OK
        value = entry.get_text().strip()
        dialog.destroy()
        if accepted:
            destination = (Path(relative).parent / value).as_posix() if rename else value
            if rename and ('/' in value or '\\' in value):
                self.error(VaultError('Use Move to for changing folders.'))
                return
            self.try_action(lambda: self.move_path(relative, destination))

    def move_path(self, relative, destination):
        """UI boundary for organization: resolve dirty editors before link rewrites."""
        if not self.confirm_panes(list(self.panes.values())):
            return False
        # Discard choices in confirm_panes must not leave stale dirty buffers
        # able to autosave over incoming-link updates.
        for pane in self.panes.values():
            if pane.dirty:
                pane.load_external(self.vault.read(pane.note.path))
        mapping = self.vault.move(relative, destination)
        renamed = {}
        for old, pane in self.panes.items():
            new = mapping.get(old, old)
            note = self.vault.read(new)
            if note.path != pane.note.path or note.revision != pane.note.revision:
                pane.load_external(note)
            pane.tab_label.set_text(Path(new).stem)
            tab = self.tabs.get_tab_label(pane)
            if tab:
                tab.set_tooltip_text(new)
            renamed[new] = pane
            if self.recovery and old != new:
                self.recovery.remove(old)
        self.panes = renamed
        old_folder, new_folder = relative.rstrip('/'), destination.rstrip('/')
        self.collapsed_folders = {new_folder + path[len(old_folder):]
                                 if path == old_folder or path.startswith(old_folder + '/') else path
                                 for path in self.collapsed_folders}
        self.collapsed_folders.intersection_update(self.vault.folders)
        self.remap_navigation(mapping)
        self.populate_files()
        self.update_context()
        self.status.set_text(f'Moved {relative} to {destination} · Incoming links updated')
        if self.vault.organization_notice:
            self.status.set_text(self.status.get_text() + ' · ' + self.vault.organization_notice)
        return True

    def trash_item(self, relative):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   text=f'Move {relative.rstrip("/")} to trash?')
        dialog.format_secondary_text('The item will be kept in the vault’s hidden .trash folder.')
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Move to trash', Gtk.ResponseType.OK)
        accepted = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if accepted:
            self.try_action(lambda: self.trash_path(relative))

    def trash_path(self, relative):
        relative = relative.rstrip('/')
        deleted = [path for path in self.vault.notes if path == relative or path.startswith(relative + '/')]
        panes = [pane for path, pane in self.panes.items() if path in deleted]
        if not self.confirm_panes(panes):
            return False
        target = self.vault.trash(relative)
        for pane in panes:
            if self.recovery:
                self.recovery.remove(pane.note.path)
            self.tabs.remove_page(self.tabs.page_num(pane))
            self.panes.pop(pane.note.path, None)
        self.queue_reading_refresh()
        self.remap_navigation({}, deleted=deleted)
        self.collapsed_folders.intersection_update(self.vault.folders)
        self.populate_files()
        self.update_context()
        self.status.set_text(f'Moved to trash · {target}')
        return True

    def edit_properties(self):
        pane = self.current()
        if pane is None:
            return
        dialog = Gtk.Dialog(title='Note properties', transient_for=self, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Apply', Gtk.ResponseType.OK)
        dialog.set_default_size(480, 360)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(8)
        caption = Gtk.Label(label='YAML properties · tags and aliases accept lists', xalign=0)
        content.pack_start(caption, False, False, 0)
        editor = Gtk.TextView(buffer=text_buffer(property_text(pane.text())))
        editor.set_monospace(True)
        editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        content.pack_start(scrolled(editor), True, True, 0)
        problem = Gtk.Label(xalign=0)
        problem.set_line_wrap(True)
        content.pack_start(problem, False, False, 0)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            source = editor.get_buffer().get_text(*editor.get_buffer().get_bounds(), True)
            try:
                self.set_note_properties(source)
            except ValueError as error:
                problem.set_text(str(error))
                continue
            break
        dialog.destroy()

    def set_note_properties(self, source):
        pane = self.current()
        if pane is None:
            return
        changed = with_properties(pane.text(), source)
        if changed != pane.text():
            pane.apply_edit(TextEdit(0, len(pane.text()), changed))
            self.update_context()

    def offer_recovery(self):
        for draft, note, text in self.recovery.drafts():
            if note.path in self.panes:
                continue
            try:
                self.vault._path(note.path)
            except VaultError:
                continue
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       text=f'Recover unsaved changes to {note.path}?')
            dialog.format_secondary_text('The draft stays available if you choose Later. If the file changed, saving will stop to protect both versions.')
            dialog.add_buttons('Later', Gtk.ResponseType.CANCEL, 'Discard draft', Gtk.ResponseType.NO, 'Recover', Gtk.ResponseType.YES)
            result = dialog.run()
            dialog.destroy()
            if result == Gtk.ResponseType.NO:
                try:
                    draft.unlink()
                except OSError as error:
                    self.error(error)
            elif result == Gtk.ResponseType.YES:
                self.open_note(note.path, recovered=note)
                pane = self.panes.get(note.path)
                if pane:
                    pane.buffer.set_text(text)
                    # Only remove the old copy after the new recovery draft is durable.
                    try:
                        if pane.dirty:
                            self.recovery.write(note, text)
                        draft.unlink()
                    except OSError as error:
                        self.error(error)

    def open_note(self, relative, fragment='', recovered=None):
        self.remember_location()
        if relative not in self.panes:
            def action():
                pane = NotePane(self, recovered if recovered is not None else self.vault.read(relative))
                self.panes[relative] = pane
                tab = Gtk.Box(spacing=6)
                tab.set_tooltip_text(relative)
                tab.pack_start(pane.tab_label, True, True, 0)
                close = self.icon_button('window-close-symbolic', 'Close note · Ctrl+W',
                                         lambda *_: self.close_note(pane))
                close.set_focus_on_click(False)
                tab.pack_end(close, False, False, 0)
                tab.show_all()
                self.note_toolbars.add(pane.toolbar)
                pane.toolbar.show_all()
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
        if not self.restoring_workspace:
            cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
            self.navigation_history.visit(Location(relative, cursor))
        return pane

    def active_page(self):
        focus = self.get_focus()
        if self.split_view is not None and focus and (focus is self.split_view or focus.is_ancestor(self.split_box)):
            return self.split_pane
        return self.tabs.get_nth_page(self.tabs.get_current_page()) if self.tabs.get_n_pages() else None

    def current(self):
        """The active note, if the focused workspace pane is a note editor."""
        page = self.active_page()
        return page if isinstance(page, NotePane) else None

    def save_current(self, *_):
        pane = self.current()
        if pane:
            self.try_action(pane.save)

    def close_tab(self, *_):
        self.close_note(self.active_page())

    def close_note(self, pane):
        if isinstance(pane, GraphView):
            self.tabs.remove_page(self.tabs.page_num(pane))
            self.update_context()
            return
        if pane and self.confirm_panes([pane]):
            if pane is self.split_pane:
                self.close_split()
            self.tabs.remove_page(self.tabs.page_num(pane))
            self.panes.pop(pane.note.path, None)
            self.queue_reading_refresh()
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
        from .attachments import open_attachment
        if open_attachment(self, source, target):
            return
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
            name = unquote(target.split('#', 1)[0].split('|', 1)[0]).strip()
            if name.startswith(('./', '../')):
                name = posixpath.normpath(str(Path(source).parent / name))
            if not name or Path(name).suffix.lower() not in ('', '.md'):
                self.status.set_text(f'Unresolved note or unsupported attachment: {target}')
                return
            dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                       message_type=Gtk.MessageType.QUESTION,
                                       text=f'Create note “{name}”?')
            dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Create note', Gtk.ResponseType.OK)
            result = dialog.run()
            dialog.destroy()
            if result == Gtk.ResponseType.OK:
                def create():
                    note = self.vault.create(name)
                    self.populate_files()
                    self.open_note(note.path, fragment)
                self.try_action(create)

    def update_context(self):
        if self.vault:
            for graph in tuple(self.graph_views):
                graph.refresh_if_changed()
        self.outline_rows = []
        for child in self.context.get_children():
            child.destroy()
        pane = self.current()
        self.save_button.set_sensitive(pane is not None)
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
        self.section('Properties')
        metadata = note_metadata(pane.text())
        self.context_button('Edit properties…', self.edit_properties)
        for key, value in metadata.properties.items():
            label = Gtk.Label(label=f'{key}: {value}', xalign=0)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_max_width_chars(24)
            label.set_selectable(True)
            label.get_style_context().add_class('outline-empty')
            self.context.pack_start(label, False, False, 0)
        if metadata.error:
            problem = Gtk.Label(label=metadata.error, xalign=0)
            problem.set_line_wrap(True)
            problem.set_max_width_chars(24)
            self.context.pack_start(problem, False, False, 0)
        if metadata.tags:
            self.section('Tags')
            for tag in metadata.tags:
                self.context_button('#' + tag, lambda tag=tag: self.search_entry.set_text('tag:#' + tag))
        self.context.show_all()
        return False

    def queue_reading_refresh(self):
        if self.reading_source is None and any(pane.mode == 'reading' for pane in self.panes.values()):
            self.reading_source = GLib.idle_add(self.refresh_reading, priority=GLib.PRIORITY_LOW)

    def refresh_reading(self):
        self.reading_source = None
        for pane in self.panes.values():
            if pane.mode == 'reading':
                pane.reading.refresh()
        return False

    def refresh(self, paths=None):
        if not self.vault:
            return True
        try:
            if paths is None:
                self.vault.refresh()
            else:
                self.vault.update_paths(paths)
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
                        pane.load_external(self.vault.indexed_note(path))
                if pane.mode == 'reading':
                    pane.reading.refresh()
            self.update_context()
        except (VaultError, OSError) as error:
            self.status.set_text(str(error))
        return True

    def key_press(self, _, event):
        if self.quick_switcher is not None:
            return self.quick_switcher.key_press(self.quick_switcher, event)
        if self.palette is not None:
            return self.palette.key_press(self.palette, event)
        pane, focus = self.current(), self.get_focus()
        if pane and focus and focus.is_ancestor(pane.findbar):
            if pane.findbar.key_press(pane.findbar, event):
                return True
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        for shortcut in APP_SHORTCUTS:
            key, mask = Gtk.accelerator_parse(shortcut.accelerator)
            if Gdk.keyval_to_lower(event.keyval) == key and modifiers == mask:
                getattr(self, shortcut.handler)()
                return True
        return False

    def toggle_details(self, *_):
        if self.vault is not None:
            self.details_toggle.set_active(not self.details_toggle.get_active())

    def set_panel_open(self, button, panel):
        opened = panel.set_open(button.get_active())
        if button.get_active() != opened:
            button.set_active(opened)

    def toggle_file_tree(self, *_):
        if self.vault is not None:
            self.notes_toggle.set_active(not self.notes_toggle.get_active())

    def apply_preferences(self):
        Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', self.preferences.theme != 'light')
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

    def show_graph(self, *_, local=False):
        if not self.vault:
            return
        if self.graph_window:
            self.graph_window.destroy()
        from .graph_ui import GraphWindow
        self.graph_window = GraphWindow(self, local=local)

    def show_graph_tab(self, *_, local=False, state=None):
        if not self.vault:
            return
        self.remember_location()
        graph = GraphView(self, local=local, state=state)
        tab = Gtk.Box(spacing=6)
        tab.pack_start(Gtk.Label(label='Local graph' if graph.local.get_active() else 'Graph'), True, True, 0)
        close = self.icon_button('window-close-symbolic', 'Close graph tab · Ctrl+W',
                                 lambda *_: self.close_note(graph))
        close.set_focus_on_click(False)
        tab.pack_end(close, False, False, 0)
        tab.show_all()
        self.tabs.append_page(graph, tab)
        self.tabs.set_current_page(self.tabs.page_num(graph))
        graph.canvas.grab_focus()
        self.update_context()
        return graph

    def show_local_graph_tab(self, *_):
        return self.show_graph_tab(local=True)

    def open_graph_note(self, graph, path):
        # Keep a graph in the main tab visible when its companion is a note.
        beside_graph = self.tabs.page_num(graph) >= 0 and isinstance(self.split_pane, NotePane)
        pane = self.open_note(path)
        if pane and beside_graph:
            self.open_split(pane)
            self.tabs.set_current_page(self.tabs.page_num(graph))
            self.split_view.grab_focus()
            self.update_context()

    def show_local_graph(self, *_):
        self.show_graph(local=True)

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

    def show_quick_switcher(self):
        from .navigation_ui import QuickSwitcher
        if self.vault is None:
            return
        if self.quick_switcher is None:
            self.quick_switcher = QuickSwitcher(self)
            self.quick_switcher.connect('destroy', lambda *_: setattr(self, 'quick_switcher', None))
            self.overlay.add_overlay(self.quick_switcher)
            self.quick_switcher.show_all()
        self.quick_switcher.filter_commands()
        self.quick_switcher.search.grab_focus()

    def remember_location(self):
        pane = self.current()
        if pane:
            cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
            self.navigation_history.update(Location(pane.note.path, cursor))

    def tab_switched(self, _, pane, index):
        if isinstance(pane, GraphView):
            self.note_toolbars.hide()
            GLib.idle_add(self.update_context)
            return
        self.note_toolbars.set_visible_child(pane.toolbar)
        self.note_toolbars.show()
        self.remember_location()
        if not self.restoring_workspace:
            self.navigation_history.visit(Location(pane.note.path,
                pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()))
        GLib.idle_add(self.update_context)

    def tab_removed(self, notebook, pane, index):
        if isinstance(pane, NotePane) and pane.toolbar.get_parent() is self.note_toolbars:
            self.note_toolbars.remove(pane.toolbar)
        self.note_toolbars.set_visible(isinstance(notebook.get_nth_page(notebook.get_current_page()), NotePane))
        if pane is self.split_pane:
            self.close_split()
        source = self.scroll_restore_sources.pop(pane, None)
        if source is not None:
            GLib.source_remove(source)
        pane.destroy()

    def navigate_back(self, *_):
        self.navigate_history(-1)

    def navigate_forward(self, *_):
        self.navigate_history(1)

    def navigate_history(self, direction):
        self.remember_location()
        location = self.navigation_history.step(direction)
        if location is None:
            return
        self.restoring_workspace = True
        try:
            pane = self.open_note(location.path)
            if pane:
                position = pane.buffer.get_iter_at_offset(min(location.cursor, pane.buffer.get_char_count()))
                pane.buffer.place_cursor(position)
                pane.editor.scroll_to_iter(position, 0.15, False, 0, 0)
        finally:
            self.restoring_workspace = False

    def close_split(self, *_):
        for child in self.split_box.get_children():
            child.destroy()
        self.split_box.hide()
        self.split_pane = self.split_view = None

    def toggle_split(self, *_):
        if self.split_view is not None:
            self.close_split()
            return
        pane = self.active_page()
        if pane:
            self.open_split(pane)

    def open_split(self, pane):
        from .splitview import SplitTextView
        self.close_split()
        self.split_pane = pane
        bar = Gtk.Box(spacing=6)
        title = Gtk.Label(label='Graph' if isinstance(pane, GraphView) else pane.note.path, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        bar.pack_start(title, True, True, 8)
        bar.pack_end(self.icon_button('window-close-symbolic', 'Close split editor', self.close_split), False, False, 0)
        self.split_box.pack_start(bar, False, False, 0)
        if isinstance(pane, GraphView):
            self.split_view = GraphView(self, state=pane.snapshot())
            self.split_box.pack_start(self.split_view, True, True, 0)
            self.show_split()
            return
        self.split_view = SplitTextView(pane)
        self.split_view.connect('key-press-event', self.split_key_press)
        self.split_view.connect('copy-clipboard', pane.copy_selection)
        self.split_view.connect('cut-clipboard', pane.cut_selection)
        self.split_view.connect('populate-popup', pane.populate_edit_menu)
        self.split_view.connect('paste-clipboard', pane.paste_attachment)
        self.split_view.drag_dest_set(Gtk.DestDefaults.ALL,
            [Gtk.TargetEntry.new('text/uri-list', 0, 1)], Gdk.DragAction.COPY)
        self.split_view.connect('drag-data-received', pane.drop_attachments)
        self.split_view.connect('button-release-event', pane.navigation.clicked)
        split_scroll = scrolled(self.split_view)
        split_scroll.get_vadjustment().connect('value-changed', self.split_view.queue_sync)
        split_scroll.get_hadjustment().connect('value-changed', self.split_view.queue_sync)
        self.split_box.pack_start(split_scroll, True, True, 0)
        self.show_split()

    def show_split(self):
        self.split_box.set_no_show_all(False)
        self.split_box.show_all()
        self.split_box.set_no_show_all(True)
        self.editor_split.set_position(max(1, self.editor_split.get_allocated_width() // 2))

    def split_key_press(self, view, event):
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if (modifiers == Gdk.ModifierType.CONTROL_MASK
                and Gdk.keyval_to_lower(event.keyval) in (Gdk.KEY_f, Gdk.KEY_h)):
            # Find controls belong to the canonical pane. Bring that pane into
            # view before its handler opens and focuses the search bar.
            self.tabs.set_current_page(self.tabs.page_num(self.split_pane))
        return self.split_pane.editor_key_press(view, event)

    def remap_navigation(self, mapping, deleted=()):
        self.navigation_history.remap(mapping, deleted)
        if isinstance(self.split_pane, NotePane) and self.split_pane.note.path in deleted:
            self.close_split()
        elif isinstance(self.split_pane, NotePane):
            self.open_split(self.split_pane)
        self.persist_workspace()

    def persist_workspace(self):
        if self.vault is None or self.restoring_workspace:
            return True
        pages = []
        for index in range(self.tabs.get_n_pages()):
            pane = self.tabs.get_nth_page(index)
            if isinstance(pane, GraphView):
                pages.append(pane.snapshot())
                continue
            pages.append({'path': pane.note.path,
                          'cursor': pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset(),
                          'scroll': pane.scroll.get_vadjustment().get_value()})
        index = self.tabs.get_current_page()
        active = self.tabs.get_nth_page(index) if index >= 0 else None
        active_key = {'graph': active.workspace_id} if isinstance(active, GraphView) else active.note.path if active else None
        split_key = ({'graph': self.split_pane.workspace_id, 'state': self.split_view.snapshot()}
                     if isinstance(self.split_pane, GraphView) else self.split_pane.note.path if self.split_pane else None)
        state = {'tabs': pages, 'active': active_key,
                 'collapsed_folders': sorted(self.collapsed_folders),
                 'split': split_key}
        try:
            self.workspace_store.save(self.vault.root, state)
        except OSError as error:
            self.status.set_text(f'Could not save workspace: {error}')
        return True

    def restore_workspace(self):
        state = self.workspace_store.load(self.vault.root)
        pages = state.get('tabs', [])
        if 'tabs' not in state or not isinstance(pages, list):
            return False
        self.restoring_workspace = True
        try:
            graphs = {}
            for saved in pages:
                if isinstance(saved, dict) and saved.get('kind') == 'graph':
                    graph = self.show_graph_tab(state=saved)
                    graphs[graph.workspace_id] = graph
                    continue
                if (not isinstance(saved, dict) or not isinstance(saved.get('path'), str)
                        or saved['path'] not in self.vault.notes):
                    continue
                pane = self.open_note(saved['path'])
                if pane is None:
                    continue
                cursor = saved.get('cursor', 0)
                if type(cursor) is int:
                    pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(max(0, min(cursor, pane.buffer.get_char_count()))))
                scroll = saved.get('scroll', 0)
                if type(scroll) in (int, float) and math.isfinite(scroll) and scroll >= 0:
                    previous = self.scroll_restore_sources.pop(pane, None)
                    if previous is not None:
                        GLib.source_remove(previous)
                    self.scroll_restore_sources[pane] = GLib.timeout_add(100, self.restore_scroll, pane, scroll)
            active = state.get('active')
            if isinstance(active, str) and active in self.panes:
                self.open_note(active)
            if isinstance(active, dict) and isinstance(active.get('graph'), str) and active['graph'] in graphs:
                self.tabs.set_current_page(self.tabs.page_num(graphs[active['graph']]))
            folders = state.get('collapsed_folders', [])
            if isinstance(folders, list):
                self.collapsed_folders = {path for path in folders if isinstance(path, str) and path in self.vault.folders}
            split = state.get('split')
            if isinstance(split, str) and split in self.panes:
                self.open_split(self.panes[split])
            elif isinstance(split, dict) and isinstance(split.get('graph'), str) and split['graph'] in graphs:
                self.open_split(graphs[split['graph']])
                if isinstance(split.get('state'), dict):
                    self.split_view.restore_state(split['state'])
        finally:
            self.restoring_workspace = False
        self.populate_files()
        pane = self.current()
        if pane:
            self.navigation_history.visit(Location(pane.note.path,
                pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()))
        return True

    def restore_scroll(self, pane, value):
        self.scroll_restore_sources.pop(pane, None)
        if pane.note.path in self.panes and self.panes[pane.note.path] is pane:
            pane.scroll.get_vadjustment().set_value(value)
        return False

    def show_keybinds(self):
        if self.cheatsheet is None:
            self.cheatsheet = KeybindCheatsheet(self)
            self.cheatsheet.set_destroy_with_parent(True)
            self.cheatsheet.connect('destroy', lambda *_: setattr(self, 'cheatsheet', None))
        else:
            self.cheatsheet.present()

    def on_delete(self, *_):
        return not self.confirm_panes(list(self.panes.values()))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Open a local Markdown vault in Libre Vault.')
    parser.add_argument('vault', nargs='?', help='Path to a vault folder')
    args = parser.parse_args(argv)
    app = Gtk.Application(application_id='org.librevault.Prototype', flags=Gio.ApplicationFlags.HANDLES_OPEN)
    app.connect('activate', lambda app: Window(app).present())
    def open_folders(app, files, count, hint):
        for folder in files:
            Window(app, folder.get_path()).present()
    app.connect('open', open_folders)
    paths = [str(Path(args.vault).expanduser().resolve())] if args.vault else []
    return app.run(['libre-vault', *paths])


if __name__ == '__main__':
    raise SystemExit(main())
