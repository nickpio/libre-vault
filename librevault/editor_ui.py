"""The note editor: source buffer, live preview, and editing behavior."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from gi.repository import Gtk, Gdk, Gio, GLib, Pango

from .attachments import attachment_markdown, store_attachment
from .editing import TextEdit, format_inline, list_enter, markdown_link
from .editor_render import EditorRenderMixin, line_hits, math_pixbuf, selection_overlaps
from .findbar import FindBar
from .folding import FoldState, compute_regions
from .history import BufferHistory
from .multicursor import (ExtraCursors, find_occurrences, next_occurrence, plan_backspace,
                          plan_delete, plan_format, plan_insert, word_bounds)
from .navigation_ui import EditorNavigation
from .reading import ReadingView
from .shortcuts import EDITOR_SHORTCUTS, effective_shortcuts, shortcut_key
from .slash import SlashMenu, builtin_commands, filter_commands, trigger_at
from .syntax import TOKEN_STYLES
from .tables import (
    TableEditorDialog, cell_offset, format_table, table_add_column_left, table_add_column_right,
    table_add_row_above, table_add_row_below, table_align_column, table_at, table_delete_column,
    table_delete_row, table_from_model, table_move_column, table_move_row, table_next_cell,
    table_previous_cell, table_sort_column, table_break_after_row,
)
from .textview import EditorTextView
from .vault import Conflict, VaultError
from .vim import Session, VimState, dispatch, display_selection, retreat
from .widgets import scrolled, text_buffer


class NotePane(EditorRenderMixin, Gtk.Box):
    def __init__(self, window, note):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window, self.note = window, note
        self.dirty = False
        self.save_error = None
        self.save_conflict = False
        self.resolving_conflict = False
        self.autosave_source = None
        self.format_source = None
        self.formatted_text = None
        self.spans = []
        self.items = []
        self.headings = []
        self.list_tags = {}
        self.list_widgets = []
        self.image_widgets = []
        self.diagram_widgets = []
        self.diagrams = []
        self._diagram_theme = None
        self.math_widgets = []
        self.embed_widgets = []
        self.table_widgets = []
        self.callout_widgets = []
        self.math_spans = []
        self.callouts = []
        self.preview_theme = None
        self.position_source = None
        self.mode = 'live'
        self.buffer = text_buffer(note.text.replace('\r\n', '\n'))
        self.tags = {
            'strong': self.buffer.create_tag(None, weight=Pango.Weight.BOLD),
            'em': self.buffer.create_tag(None, style=Pango.Style.ITALIC),
            'code': self.buffer.create_tag(None, family='monospace', background='#1b1b1b', size_points=11),
            'quote': self.buffer.create_tag(None, foreground='#aaaaaa', left_margin=18),
            'link': self.buffer.create_tag(None, foreground='#8cc8ff', underline=Pango.Underline.SINGLE),
            'completed': self.buffer.create_tag(None, foreground='#999999', strikethrough=True),
            'metadata': self.buffer.create_tag(None, foreground='#888888', family='monospace', size_points=11),
            'highlight': self.buffer.create_tag(None, background='#5c4b16', foreground='#fff4cc'),
            'strikethrough': self.buffer.create_tag(None, strikethrough=True),
            'comment': self.buffer.create_tag(None, foreground='#999999'),
            'underline': self.buffer.create_tag(None, underline=Pango.Underline.SINGLE),
            'mark': self.buffer.create_tag(None, background='#6b5420', foreground='#fff8e0'),
            'subscript': self.buffer.create_tag(None, rise=-3500, size_points=11),
            'superscript': self.buffer.create_tag(None, rise=5000, size_points=11),
            'kbd': self.buffer.create_tag(None, family='monospace', background='#1b1b1b'),
            'table': self.buffer.create_tag(None, family='monospace'),
            'math': self.buffer.create_tag(None, style=Pango.Style.ITALIC, family='serif'),
            'callout': self.buffer.create_tag(None, weight=Pango.Weight.BOLD),
        }
        for style in TOKEN_STYLES:
            self.tags[style] = self.buffer.create_tag(None, foreground='#d5d5d5')
        for level, size in enumerate([25, 21, 18, 16, 14, 12], 1):
            self.tags[f'h{level}'] = self.buffer.create_tag(
                None, size_points=size, weight=Pango.Weight.BOLD,
                foreground='#e4e4e4', pixels_above_lines=10)
        # This tag has highest priority, including inside headings and code.
        self.hidden = self.buffer.create_tag(None, invisible=True)
        self.editor = EditorTextView(buffer=self.buffer)
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.set_left_margin(40)
        self.editor.set_right_margin(40)
        self.editor.set_top_margin(38)
        self.editor.set_bottom_margin(80)
        # GTK 3's hit-testing converts a raw line byte count to a visible
        # index in bottom padding, which aborts when syntax is invisible.
        # Keep paragraph spacing above lines, where hit-testing starts at zero.
        self.editor.set_pixels_above_lines(5)
        self.editor.get_style_context().add_class('live-editor')
        self.folds = FoldState()
        self.fold_regions = None
        self.fold_widgets = []
        self._fold_signature = None
        self.multicursor = ExtraCursors()
        self.extra_carets = []
        self._caret_signature = None
        self.vim = VimState()
        self._preserving_cursors = 0
        self._applying_vim = False
        self._applying_slash = False
        self.occurrence_tag = self.buffer.create_tag(None, background='#3a5f8a')
        self.slash = SlashMenu(self)
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
        self.pack_start(toolbar, False, False, 0)
        self.reorder_child(toolbar, 0)
        toolbar.get_style_context().add_class('note-toolbar')
        mode = self.mode_selector = Gtk.ComboBoxText()
        for value, title in [('live', 'Live preview'), ('source', 'Source'), ('reading', 'Reading')]:
            mode.append(value, title)
        mode.set_active_id('live')
        mode.connect('changed', lambda field: self.set_mode(field.get_active_id()))
        mode.get_accessible().set_name('Note view mode')
        path_label = self.path_label = Gtk.Label(label=Path(note.path).parent.as_posix() if '/' in note.path else 'Notes', xalign=0)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_label.get_style_context().add_class('note-path')
        toolbar.pack_start(path_label, True, True, 0)
        toolbar.pack_end(mode, False, False, 0)
        attach = Gtk.Button.new_from_icon_name('mail-attachment-symbolic', Gtk.IconSize.BUTTON)
        attach.get_style_context().add_class('icon-button')
        attach.set_tooltip_text('Insert attachment')
        attach.connect('clicked', lambda *_: self.choose_attachment())
        toolbar.pack_end(attach, False, False, 0)
        self.bookmark_button = Gtk.Button.new_from_icon_name('non-starred-symbolic', Gtk.IconSize.BUTTON)
        self.bookmark_button.get_style_context().add_class('icon-button')
        self.bookmark_button.set_tooltip_text('Bookmark current note')
        self.bookmark_button.get_accessible().set_name('Bookmark current note')
        self.bookmark_button.connect('clicked', lambda *_: self.window.toggle_note_bookmark(self))
        toolbar.pack_end(self.bookmark_button, False, False, 0)
        self.tab_label = Gtk.Label(label=Path(note.path).stem)
        self.history = BufferHistory(self.buffer)
        self.findbar = FindBar(self)
        self.pack_start(self.findbar, False, False, 0)
        self.reorder_child(self.findbar, 1)
        self.buffer_handlers = [self.buffer.connect('changed', self.changed),
                                self.buffer.connect('mark-set', self.cursor_moved),
                                self.buffer.connect('insert-text', self._remap_folds_insert),
                                self.buffer.connect('delete-range', self._remap_folds_delete)]
        self.connect('destroy', self.cleanup)
        self.apply_preferences()
        self.render()
        self.navigation = EditorNavigation(self)
        self.sync_bookmark_button()

    def sync_bookmark_button(self):
        if self.in_destruction():
            return
        marked = self.window.note_is_bookmarked(self.note.path)
        icon = 'starred-symbolic' if marked else 'non-starred-symbolic'
        self.bookmark_button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
        label = 'Remove bookmark' if marked else 'Bookmark current note'
        self.bookmark_button.set_tooltip_text(label)
        self.bookmark_button.get_accessible().set_name(label)
        style = self.bookmark_button.get_style_context()
        if marked:
            style.add_class('bookmarked')
        else:
            style.remove_class('bookmarked')

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
        if not self.dirty:
            self.save_error = None
        self.window.update_save_status()
        if self.autosave_source is not None:
            GLib.source_remove(self.autosave_source)
            self.autosave_source = None
        if self.dirty:
            self.window.recovery.schedule_write(self.note, self.text())
        else:
            self.window.recovery.schedule_remove(self.note.path)
        if self.dirty:
            self.autosave_source = GLib.timeout_add_seconds(2, self.autosave)
        if not self._preserving_cursors and self.multicursor:
            self.multicursor.clear()
        self.queue_format()
        self.sync_slash_menu()
        if not self.window.in_destruction():
            self.window.update_counts()

    def autosave(self):
        self.autosave_source = None
        if self.window.confirming_panes or self.resolving_conflict:
            self.autosave_source = GLib.timeout_add_seconds(2, self.autosave)
            return False
        try:
            self.save()
        except (VaultError, OSError):
            # save() retains the failure in the dedicated indicator.
            pass
        return False

    def editor_key_press(self, view, event):
        if hasattr(self, 'navigation') and self.navigation.key_press(event):
            return True
        if self.slash.handle_key(event):
            return True
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        pressed = shortcut_key(event.keyval)
        for shortcut in effective_shortcuts(EDITOR_SHORTCUTS, 'editor', self.window.hotkeys):
            key, mask = Gtk.accelerator_parse(shortcut.accelerator)
            if pressed == Gdk.keyval_to_lower(key) and modifiers == mask:
                getattr(self, shortcut.handler)()
                return True
        if event.keyval == Gdk.KEY_Escape:
            if getattr(self, 'findbar', None) is not None and self.findbar.get_visible():
                self.findbar.close()
                return True
            if self.multicursor:
                self.clear_extra_cursors()
                return True
        if self._vim_key(event, modifiers):
            return True
        if self._multicursor_key(event, modifiers):
            return True
        if (event.keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab, Gdk.KEY_ISO_Left_Tab)
                and not (modifiers & ~Gdk.ModifierType.SHIFT_MASK)
                and not self.buffer.get_has_selection() and self.navigate_table(view, event)):
            return True
        if (event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
                and not modifiers and not self.buffer.get_has_selection()):
            edit = table_break_after_row(self.text(), self.cursor_offset())
            if edit is not None:
                self.apply_table_change(edit)
                view.scroll_mark_onscreen(self.buffer.get_insert())
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

    def cursor_moved(self, _, iterator, mark):
        if mark.get_name() not in ('insert', 'selection_bound'):
            return
        if not self._preserving_cursors and not self._applying_vim and self.multicursor:
            self.multicursor.clear()
        if (self.vim.active and not self._applying_vim and not self._preserving_cursors
                and self.window.preferences.vim_mode and mark.get_name() == 'insert'):
            offset = iterator.get_offset()
            session = self.vim.session
            if session.mode == 'normal' and offset != session.cursor:
                self.vim.session = replace(session, cursor=offset, anchor=offset, pending='', column=None)
                self.window.update_vim_status()
            elif session.mode in ('visual', 'visual_line'):
                self.vim.session = replace(session, mode='normal', cursor=offset, anchor=offset,
                                           pending='', column=None)
                self.window.update_vim_status()
        self.queue_format()
        self.sync_slash_menu()

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
        # The popover and gutter widgets hold callbacks back to this pane.
        # Drop them before the widget tree finishes destroying, or the note
        # stays reachable after its tab is closed.
        self.slash.dismiss()
        self.slash.popover.destroy()
        self.slash.pane = None
        for button in self.fold_widgets:
            button.destroy()
        self.fold_widgets = []
        for marker in self.extra_carets:
            marker.destroy()
        self.extra_carets = []
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

    def _cursor(self):
        return self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()

    def _primary_range(self):
        selection = self.buffer.get_selection_bounds()
        if selection:
            start, end = (item.get_offset() for item in selection)
            return (min(start, end), max(start, end))
        offset = self._cursor()
        return offset, offset

    def _current_regions(self):
        text = self.text()
        if self.fold_regions is not None and text == self.formatted_text:
            return self.fold_regions
        return compute_regions(text)

    def _remap_folds_insert(self, _, location, text, length):
        self.folds.apply_edit(location.get_offset(), 0, len(text))

    def _remap_folds_delete(self, _, start, end):
        self.folds.apply_edit(start.get_offset(), end.get_offset() - start.get_offset(), 0)

    def _park_outside(self, region):
        cursor = self._cursor()
        if region is None or not (region.body_start <= cursor < region.body_end):
            return
        text = self.text()
        target = region.body_start
        if target > region.anchor and text[target - 1] == '\n':
            target -= 1
        self.buffer.place_cursor(self.buffer.get_iter_at_offset(max(region.anchor, target)))

    def _park_outside_folded(self):
        cursor = self._cursor()
        covering = [region for region in self.fold_regions or []
                    if region.anchor in self.folds.anchors and region.body_start <= cursor < region.body_end]
        if covering:
            outer = max(covering, key=lambda region: region.body_end - region.body_start)
            self._park_outside(outer)

    def fold_at_cursor(self):
        regions = self._current_regions()
        region = self.folds.fold_at(regions, self.text(), self._cursor())
        if region is not None:
            self._park_outside(region)
            self.render()

    def unfold_at_cursor(self):
        regions = self._current_regions()
        if self.folds.unfold_at(regions, self.text(), self._cursor()) is not None:
            self.render()

    def toggle_fold(self):
        regions = self._current_regions()
        before = set(self.folds.anchors)
        region = self.folds.toggle_at(regions, self.text(), self._cursor())
        if region is None:
            return
        if region.anchor in self.folds.anchors and region.anchor not in before:
            self._park_outside(region)
        self.render()

    def toggle_fold_anchor(self, anchor):
        region = next((item for item in self._current_regions() if item.anchor == anchor), None)
        if region is None:
            return
        if anchor in self.folds.anchors:
            self.folds.anchors.discard(anchor)
        else:
            self.folds.anchors.add(anchor)
            self._park_outside(region)
        self.render()
        self.editor.grab_focus()

    def fold_all_headings(self):
        regions = self._current_regions()
        self.fold_regions = regions
        self.folds.fold_all_headings(regions)
        self._park_outside_folded()
        self.render()

    def unfold_all(self):
        if self.folds.anchors:
            self.folds.unfold_all()
            self.render()





    def sync_slash_menu(self):
        if self._applying_slash or not hasattr(self, 'slash'):
            return
        if not self.window.preferences.slash_commands or self.mode == 'reading':
            self.slash.dismiss()
            return
        found = trigger_at(self.text(), self._cursor())
        if not found:
            self.slash.dismiss()
            return
        start, query = found
        matches = filter_commands(builtin_commands(), query)
        if not matches:
            self.slash.dismiss()
            return
        self.slash.update(start, matches)

    def apply_slash_command(self, command, start):
        end = self._cursor()
        text = self.text()
        if start < 0 or end < start or end > len(text) or not text[start:end].startswith('/'):
            return
        self._applying_slash = True
        try:
            self.slash.dismiss()
            if command.kind == 'template':
                self.buffer.select_range(self.buffer.get_iter_at_offset(end),
                                         self.buffer.get_iter_at_offset(start))
                self.window.insert_template(pane=self)
                return
            cursor = min(start + len(command.snippet), max(start, start + command.cursor))
            self.apply_edit(TextEdit(start, end, command.snippet), (cursor, cursor))
        finally:
            self._applying_slash = False
        self.editor.grab_focus()

    def clear_extra_cursors(self):
        if not self.multicursor:
            return
        self.multicursor.clear()
        self.render()

    def _sorted_ranges(self):
        primary = self._primary_range()
        ordered = self.multicursor.combined(primary)
        try:
            index = ordered.index(primary)
        except ValueError:
            index = 0
        return ordered, index

    def add_next_occurrence(self):
        text = self.text()
        start, end = self._primary_range()
        if start == end and not self.multicursor:
            bounds = word_bounds(text, start)
            if bounds is None:
                return
            self._preserving_cursors += 1
            try:
                self.buffer.select_range(self.buffer.get_iter_at_offset(bounds[1]),
                                         self.buffer.get_iter_at_offset(bounds[0]))
            finally:
                self._preserving_cursors -= 1
            self.render()
            return
        if start == end:
            return
        nxt = next_occurrence(find_occurrences(text, text[start:end]), self.multicursor.combined((start, end)))
        if nxt is None:
            return
        self.multicursor.ranges.append(nxt)
        self.render()

    def select_all_occurrences(self):
        text = self.text()
        start, end = self._primary_range()
        self._preserving_cursors += 1
        try:
            if start == end:
                bounds = word_bounds(text, start)
                if bounds is None:
                    return
                start, end = bounds
                self.buffer.select_range(self.buffer.get_iter_at_offset(end),
                                         self.buffer.get_iter_at_offset(start))
        finally:
            self._preserving_cursors -= 1
        if start == end:
            return
        self.multicursor.ranges = [span for span in find_occurrences(text, text[start:end]) if span != (start, end)]
        self.render()

    def _apply_cursor_edits(self, edits, carets, primary_index, collapsed=True):
        self._preserving_cursors += 1
        self.history.break_group()
        self.buffer.begin_user_action()
        try:
            for edit in edits:
                if self.text()[edit.start:edit.end] != edit.replacement:
                    point = self.buffer.get_iter_at_offset(edit.start)
                    self.buffer.delete(point, self.buffer.get_iter_at_offset(edit.end))
                    self.buffer.insert(point, edit.replacement)
            points = [(caret, caret) for caret in carets] if collapsed else list(carets)
            if not points:
                return
            primary_index = max(0, min(primary_index, len(points) - 1))
            primary = points[primary_index]
            self.multicursor.ranges = [item for index, item in enumerate(points) if index != primary_index]
            self.buffer.select_range(self.buffer.get_iter_at_offset(primary[1]),
                                     self.buffer.get_iter_at_offset(primary[0]))
        finally:
            self.buffer.end_user_action()
            self._preserving_cursors -= 1
        self.render()

    def _multicursor_key(self, event, modifiers):
        if not self.multicursor:
            return False
        if modifiers & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        ranges, index = self._sorted_ranges()
        if event.keyval == Gdk.KEY_BackSpace:
            edits, carets = plan_backspace(self.text(), ranges)
            self._apply_cursor_edits(edits, carets, index)
            return True
        if event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            edits, carets = plan_delete(self.text(), ranges)
            self._apply_cursor_edits(edits, carets, index)
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            edits, carets = plan_insert(ranges, '\n')
            self._apply_cursor_edits(edits, carets, index)
            return True
        typed = event.string or ''
        if not typed:
            value = Gdk.keyval_to_unicode(event.keyval)
            typed = chr(value) if value else ''
        if typed and typed.isprintable():
            edits, carets = plan_insert(ranges, typed)
            self._apply_cursor_edits(edits, carets, index)
            return True
        return False

    def _vim_key_name(self, event, modifiers):
        control = Gdk.ModifierType.CONTROL_MASK
        if modifiers & control:
            extra = modifiers & ~control & ~Gdk.ModifierType.SHIFT_MASK
            if extra == 0 and Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_r:
                return 'ctrl-r'
            return None
        if modifiers & (Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK):
            return None
        if event.keyval == Gdk.KEY_Escape:
            return 'Escape'
        if event.keyval in (Gdk.KEY_BackSpace, Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_Return,
                            Gdk.KEY_KP_Enter, Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            return 'swallow'
        value = Gdk.keyval_to_unicode(event.keyval)
        if not value or value < 32:
            return None
        return chr(value)

    def _vim_point(self):
        if self.vim.session.mode == 'insert':
            return self._cursor()
        return max(0, min(self.vim.session.cursor, len(self.text())))

    def _show_vim_cursor(self):
        text = self.text()
        start, end = display_selection(text, self.vim.session)
        start = max(0, min(start, len(text)))
        end = max(0, min(end, len(text)))
        if self.vim.session.cursor >= self.vim.session.anchor:
            insert, bound = end, start
        else:
            insert, bound = start, end
        self.buffer.select_range(self.buffer.get_iter_at_offset(insert),
                                 self.buffer.get_iter_at_offset(bound))

    def _apply_vim(self, outcome):
        self._applying_vim = True
        try:
            if outcome.undo:
                self.undo()
                cursor = self._cursor()
                self.vim.session = replace(outcome.session, mode='normal', pending='', cursor=cursor, anchor=cursor)
            elif outcome.redo:
                self.redo()
                cursor = self._cursor()
                self.vim.session = replace(outcome.session, mode='normal', pending='', cursor=cursor, anchor=cursor)
            elif outcome.edit is not None:
                self.vim.session = outcome.session
                self.apply_edit(outcome.edit, (outcome.anchor, outcome.cursor))
            else:
                self.vim.session = outcome.session
                self._show_vim_cursor()
        finally:
            self._applying_vim = False
        self.window.update_vim_status()
        if not self.in_destruction():
            self.editor.scroll_mark_onscreen(self.buffer.get_insert())

    def _vim_key(self, event, modifiers):
        if not self.window.preferences.vim_mode or not self.vim.active:
            return False
        key = self._vim_key_name(event, modifiers)
        if self.vim.session.mode == 'insert':
            if key == 'Escape':
                self._apply_vim(dispatch(self.text(), self._cursor(), self.vim.session, 'Escape'))
                return True
            return False
        if key is None:
            return False
        if (key == '/' and not self.vim.session.pending and self.vim.session.mode == 'normal'
                and self.window.preferences.slash_commands):
            point = self._vim_point()
            self.vim.session = replace(self.vim.session, mode='insert', pending='', cursor=point, anchor=point)
            self.window.update_vim_status()
            return False
        self._apply_vim(dispatch(self.text(), self._vim_point(), self.vim.session, key))
        return True

    def _sync_vim_enabled(self):
        enabled = bool(self.window.preferences.vim_mode)
        if enabled and not self.vim.active:
            landed = retreat(self.text(), self._cursor())
            self.vim.active = True
            self.vim.session = Session(mode='normal', cursor=landed, anchor=landed)
            self._applying_vim = True
            try:
                self.buffer.place_cursor(self.buffer.get_iter_at_offset(landed))
            finally:
                self._applying_vim = False
        elif not enabled and self.vim.active:
            self.vim.active = False
            self.vim.session = Session(mode='insert')
        if not self.window.in_destruction():
            self.window.update_vim_status()


























    def save(self):
        if self.dirty:
            try:
                self.note = self.window.vault.save(self.note, self.text())
            except (VaultError, OSError) as error:
                self.save_error = str(error)
                self.save_conflict = isinstance(error, Conflict)
                self.window.update_save_status()
                raise
            self.dirty = False
            self.save_error = None
            self.window.update_save_status()
            self.tab_label.set_text(Path(self.note.path).stem)
            try:
                self.window.recovery.remove(self.note.path)
            except OSError as error:
                self.window.status.set_text(f'Saved, but could not remove recovery draft: {error}')
        self.history.break_group()
        self.window.update_save_status()
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
        ranges, index = self._sorted_ranges()
        if len(ranges) > 1:
            edits, selections = plan_format(self.text(), ranges, marker)
            self._apply_cursor_edits(edits, selections, index, collapsed=False)
            self.editor.grab_focus()
            return
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

    def highlight(self):
        self.emphasize('==')

    def strikethrough(self):
        self.emphasize('~~')

    def insert_template(self):
        self.window.insert_template(pane=self)

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

    def cursor_offset(self):
        return self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()

    def cursor_in_table(self):
        return table_at(self.text(), self.cursor_offset()) is not None

    def navigate_table(self, view, event):
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        backward = event.keyval == Gdk.KEY_ISO_Left_Tab or bool(modifiers & Gdk.ModifierType.SHIFT_MASK)
        edit = (table_previous_cell if backward else table_next_cell)(self.text(), self.cursor_offset())
        if edit is None:
            return False
        self.apply_table_change(edit)
        view.scroll_mark_onscreen(self.buffer.get_insert())
        return True

    def apply_table_change(self, edit):
        caret = getattr(edit, 'caret', None)
        if caret is None:
            self.apply_edit(edit)
            return
        new_length = len(self.text()) - (edit.end - edit.start) + len(edit.replacement)
        caret = max(0, min(caret, new_length))
        self.apply_edit(edit, (caret, caret))

    def run_table(self, operation, *args):
        cursor = self.cursor_offset()
        if table_at(self.text(), cursor) is None:
            self.window.status.set_text('Place the cursor inside a Markdown table.')
            return False
        edit = operation(self.text(), cursor, *args)
        if edit is None:
            self.window.status.set_text('That table change does not apply here.')
            return False
        self.apply_table_change(edit)
        self.editor.grab_focus()
        return True

    def place_block(self, block, caret_in_block):
        text = self.text()
        cursor = self.cursor_offset()
        if block and not block.endswith('\n'):
            block += '\n'
        prefix = '\n' if cursor and text[cursor - 1] != '\n' else ''
        suffix = '\n' if cursor < len(text) and text[cursor] != '\n' and not block.endswith('\n') else ''
        replacement = prefix + block + suffix
        caret = cursor + len(prefix) + min(caret_in_block, len(block))
        self.apply_edit(TextEdit(cursor, cursor, replacement), (caret, caret))

    def prompt_insert_table(self):
        if TableEditorDialog is None:
            return
        self.commit_table_dialog(TableEditorDialog(self.window, title='Insert table'), replace=False)

    def prompt_edit_table(self):
        if TableEditorDialog is None:
            return
        text = self.text()
        cursor = self.cursor_offset()
        table = table_at(text, cursor)
        if table is None:
            self.window.status.set_text('Place the cursor inside a Markdown table.')
            return
        dialog = TableEditorDialog(self.window, table.headers, table.alignments, table.rows, title='Edit table')
        self.commit_table_dialog(dialog, replace=True, original=text, cursor=cursor)

    def commit_table_dialog(self, dialog, replace, original=None, cursor=None):
        dialog.show_all()
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return
            if original is not None and self.text() != original:
                self.window.status.set_text('The note changed while the table editor was open.')
                return
            headers, alignments, rows = dialog.model()
            if replace:
                edit = table_from_model(self.text(), cursor, headers, alignments, rows)
                if edit is None:
                    self.window.status.set_text('Place the cursor inside a Markdown table.')
                    return
                self.apply_table_change(edit)
            else:
                block = format_table(headers, alignments, rows)
                self.place_block(block, cell_offset(block, 0, 0))
        finally:
            dialog.destroy()
        self.editor.grab_focus()

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
                ('Highlight', self.highlight, True), ('Strikethrough', self.strikethrough, True),
                ('Toggle fold', self.toggle_fold, True),
                ('Fold all headings', self.fold_all_headings, True),
                ('Unfold all', self.unfold_all, True),
                ('Add next occurrence', self.add_next_occurrence, True),
                ('Select all occurrences', self.select_all_occurrences, True),
                ('Insert link…', self.insert_link, True),
                ('Insert template…', self.insert_template, True),
                ('Insert attachment…', self.choose_attachment, True),
                ('Insert table…', self.prompt_insert_table, True),
                ('Edit table…', self.prompt_edit_table, self.cursor_in_table()),
                ('Add row below', lambda: self.run_table(table_add_row_below), self.cursor_in_table()),
                ('Add row above', lambda: self.run_table(table_add_row_above), self.cursor_in_table()),
                ('Delete row', lambda: self.run_table(table_delete_row), self.cursor_in_table()),
                ('Add column right', lambda: self.run_table(table_add_column_right), self.cursor_in_table()),
                ('Add column left', lambda: self.run_table(table_add_column_left), self.cursor_in_table()),
                ('Delete column', lambda: self.run_table(table_delete_column), self.cursor_in_table()),
                ('Align column left', lambda: self.run_table(table_align_column, 'left'), self.cursor_in_table()),
                ('Align column center', lambda: self.run_table(table_align_column, 'center'), self.cursor_in_table()),
                ('Align column right', lambda: self.run_table(table_align_column, 'right'), self.cursor_in_table()),
                ('Sort column ascending', lambda: self.run_table(table_sort_column, True), self.cursor_in_table()),
                ('Sort column descending', lambda: self.run_table(table_sort_column, False), self.cursor_in_table()),
                ('Move row up', lambda: self.run_table(table_move_row, -1), self.cursor_in_table()),
                ('Move row down', lambda: self.run_table(table_move_row, 1), self.cursor_in_table()),
                ('Move column left', lambda: self.run_table(table_move_column, -1), self.cursor_in_table()),
                ('Move column right', lambda: self.run_table(table_move_column, 1), self.cursor_in_table()),
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
        self.save_error = None
        self.window.update_save_status()
        self.tab_label.set_text(Path(note.path).stem)
        self.render()
        self.findbar.refresh()

