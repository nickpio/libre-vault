"""Native Linux notebook prototype. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import difflib
from io import BytesIO
import math
import posixpath
from pathlib import Path
import re
from urllib.parse import unquote
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, Pango

from .anchors import find_anchor_offset
from .vault import Vault, VaultError, links
from .mentions import link_mention
from .monitor import VaultMonitor
from .recovery import RecoveryStore, VersionHistoryStore
from .formatting import markdown_layout
from .editing import TextEdit, format_inline, list_enter, markdown_link
from .tables import (
    TableEditorDialog, cell_offset, format_table, table_add_column_left, table_add_column_right,
    table_add_row_above, table_add_row_below, table_align_column, table_at, table_delete_column,
    table_delete_row, table_from_model, table_move_column, table_move_row, table_next_cell,
    table_previous_cell, table_sort_column,
)
from .history import BufferHistory
from .folding import FoldState, compute_regions
from .slash import SlashMenu, builtin_commands, filter_commands, trigger_at
from .multicursor import (ExtraCursors, find_occurrences, next_occurrence, plan_backspace,
                          plan_delete, plan_format, plan_insert, word_bounds)
from .vim import Session, VimState, dispatch, display_selection, mode_label, retreat
from .findbar import FindBar
from .graph_ui import GraphView
from .bases_ui import BasesView
from .canvas_ui import CanvasView
from .converter import ConverterDialog
from .slides import SlidesWindow
from .filetree import tree_paths
from .metadata import note_metadata, property_text, with_properties
from .commands import CommandPalette, KeybindCheatsheet, NoteNameDialog, TemplatePicker, UniqueNoteDialog
from .shortcuts import APP_SHORTCUTS, EDITOR_SHORTCUTS
from .settings import SettingsDialog
from .preferences import PreferencesStore
from .appearance import PALETTES, stylesheet
from .syntax import TOKEN_STYLES
from .panels import SlidingPanel
from .tabs import WorkspaceTabs
from .attachments import attachment_markdown, local_path, store_attachment
from .mermaid import render_mermaid
from .reading import ReadingView, pixbuf_bytes, scaled_pixbuf
from .navigation_ui import EditorNavigation
from .navigation import Location, NavigationHistory
from .bookmarks import (BOOKMARK_ICONS, Bookmark, BookmarkManager, BookmarkStore, WorkspaceManager,
                        ask_text, confirm, graph_open_state, with_moved_root)
from .workspace import LastVaultStore, SavedWorkspacesStore, WorkspaceStore
from .templates import (
    create_note_from_template, create_unique_note, ensure_daily_note,
    expand_template, random_note_path, read_template, template_paths,
)


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


def count_summary(text, lines):
    """Words, characters, and lines for the active note."""
    words = len(text.split())
    characters = len(text)

    def unit(count, singular, plural):
        return f'{count} {singular if count == 1 else plural}'

    return (f'{unit(words, "word", "words")} · '
            f'{unit(characters, "character", "characters")} · '
            f'{unit(lines, "line", "lines")}')


def math_pixbuf(expression, color):
    """Render one mathtext expression. Unsupported math returns None."""
    if not expression or len(expression) > 400:
        return None
    try:
        from matplotlib import rc_context
        from matplotlib.mathtext import math_to_image
        output = BytesIO()
        with rc_context({'savefig.transparent': True}):
            math_to_image('$' + expression + '$', output, dpi=140, format='png', color=color)
        loader = GdkPixbuf.PixbufLoader()
        loader.write(output.getvalue())
        loader.close()
        pixbuf = loader.get_pixbuf()
    except Exception:
        return None
    if pixbuf is not None and pixbuf.get_width() > 640:
        scale = 640 / pixbuf.get_width()
        pixbuf = pixbuf.scale_simple(640, max(1, int(pixbuf.get_height() * scale)), GdkPixbuf.InterpType.BILINEAR)
    return pixbuf


def selection_overlaps(selected, start, end):
    return bool(selected and start < selected[1] and end > selected[0])


def line_hits(text, start, end, cursor):
    """True when the cursor's line intersects the source span."""
    if cursor < 0 or cursor > len(text) or end <= start:
        return False
    line_start = text.rfind('\n', 0, min(cursor, len(text))) + 1
    line_end = text.find('\n', min(cursor, len(text)))
    if line_end < 0:
        line_end = len(text)
    return line_start < end and line_end > start


def shortcut_key(keyval):
    """Compare accelerators even when Shift turns `[` into `{`."""
    lowered = Gdk.keyval_to_lower(keyval)
    if lowered == Gdk.KEY_braceleft:
        return Gdk.KEY_bracketleft
    if lowered == Gdk.KEY_braceright:
        return Gdk.KEY_bracketright
    return lowered


def format_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime('%Y-%m-%d %H:%M:%S')


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
            'code': self.buffer.create_tag(None, family='monospace', background='#1b1b1b', size_points=10),
            'quote': self.buffer.create_tag(None, foreground='#aaaaaa', left_margin=18),
            'link': self.buffer.create_tag(None, foreground='#cccccc', underline=Pango.Underline.SINGLE),
            'completed': self.buffer.create_tag(None, foreground='#999999', strikethrough=True),
            'metadata': self.buffer.create_tag(None, foreground='#888888', family='monospace', size_points=10),
            'highlight': self.buffer.create_tag(None, background='#5c4b16', foreground='#fff4cc'),
            'strikethrough': self.buffer.create_tag(None, strikethrough=True),
            'comment': self.buffer.create_tag(None, foreground='#999999'),
            'underline': self.buffer.create_tag(None, underline=Pango.Underline.SINGLE),
            'mark': self.buffer.create_tag(None, background='#6b5420', foreground='#fff8e0'),
            'subscript': self.buffer.create_tag(None, rise=-3500, scale=0.8),
            'superscript': self.buffer.create_tag(None, rise=5000, scale=0.8),
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
        self.reorder_child(self.findbar, 0)
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
            self.slash.dismiss()
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
        self.tags['highlight'].set_property('background', colors['highlight_bg'])
        self.tags['highlight'].set_property('foreground', colors['highlight_fg'])
        self.tags['comment'].set_property('foreground', colors['muted'])
        for style in TOKEN_STYLES:
            self.tags[style].set_property('foreground', colors[style])
        if preferences.theme == 'light':
            self.tags['mark'].set_property('background', '#ffe08a')
            self.tags['mark'].set_property('foreground', '#1a1400')
        else:
            self.tags['mark'].set_property('background', '#6b5420')
            self.tags['mark'].set_property('foreground', colors['bright'])
        self.tags['kbd'].set_property('background', colors['code_bg'])
        self.tags['kbd'].set_property('foreground', colors['ink'])
        self.tags['callout'].set_property('foreground', colors['bright'])
        self.tags['table'].set_property('size-points', max(8, preferences.editor_size - 1))
        self.tags['math'].set_property('foreground', colors['bright'])
        for level, size in enumerate((25, 21, 18, 16, 14, 12), 1):
            self.tags[f'h{level}'].set_property('size-points', size * preferences.editor_size / 11)
            self.tags[f'h{level}'].set_property('foreground', colors['bright'])
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if preferences.wrap_lines else Gtk.WrapMode.NONE)
        self.size_editor(self.editor, self.editor.get_allocation())
        self.update_list_margins()
        self.findbar.cached = None
        self.findbar.refresh()
        self.occurrence_tag.set_property('background', colors['selection_bg'])
        self._sync_vim_enabled()
        if not self.window.preferences.slash_commands:
            self.slash.dismiss()
        if self.mode == 'reading':
            self.reading.refresh()
        # Math images use the theme color, so a theme change rebuilds them.
        self.preview_theme = None
        self.render()

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
        if not self._preserving_cursors and self.multicursor:
            self.multicursor.clear()
        self.queue_format()
        self.sync_slash_menu()
        if not self.window.in_destruction():
            self.window.update_counts()

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

    def editor_key_press(self, view, event):
        if hasattr(self, 'navigation') and self.navigation.key_press(event):
            return True
        if self.slash.handle_key(event):
            return True
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        pressed = shortcut_key(event.keyval)
        for shortcut in EDITOR_SHORTCUTS:
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

    def _concealed_ranges(self):
        reveal = []
        findbar = getattr(self, 'findbar', None)
        if findbar is not None and findbar.get_visible():
            reveal = [item.get_offset() for item in self.buffer.get_selection_bounds()]
        return self.folds.concealed(self.fold_regions or [], reveal)

    def _conceal(self, spans):
        for start, end in spans:
            self.buffer.apply_tag(self.hidden, self.buffer.get_iter_at_offset(start),
                                  self.buffer.get_iter_at_offset(end))

    def _highlight_occurrences(self):
        for start, end in self.multicursor.ranges:
            if end > start:
                self.buffer.apply_tag(self.occurrence_tag, self.buffer.get_iter_at_offset(start),
                                      self.buffer.get_iter_at_offset(end))

    def _sync_fold_marks(self, concealed):
        if self.mode == 'reading':
            for button in self.fold_widgets:
                button.hide()
            return

        def covered(offset):
            return any(start <= offset < end for start, end in concealed)

        visible = [region for region in self.fold_regions or [] if not covered(region.anchor)]
        signature = tuple((region.anchor, region.anchor in self.folds.anchors, region.kind) for region in visible)
        if signature != self._fold_signature:
            self._fold_signature = signature
            for button in self.fold_widgets:
                button.destroy()
            self.fold_widgets = []
            for region in visible:
                folded = region.anchor in self.folds.anchors
                button = Gtk.Button(label='▸' if folded else '▾')
                button.set_relief(Gtk.ReliefStyle.NONE)
                button.set_can_focus(False)
                button.get_style_context().add_class('fold-chevron')
                button.fold_anchor = region.anchor
                button.set_tooltip_text('Unfold section' if folded else 'Fold section')
                button.get_accessible().set_name(button.get_tooltip_text())
                button.connect('clicked', lambda _, anchor=region.anchor: self.toggle_fold_anchor(anchor))
                self.editor.add_child_in_window(button, Gtk.TextWindowType.WIDGET, 0, 0)
                button.show()
                self.fold_widgets.append(button)

    def _sync_extra_carets(self):
        carets = [start for start, end in self.multicursor.ranges if start == end]
        if carets != self._caret_signature:
            self._caret_signature = list(carets)
            for marker in self.extra_carets:
                marker.destroy()
            self.extra_carets = []
            for offset in carets:
                marker = Gtk.Label(label='▏')
                marker.get_style_context().add_class('extra-cursor')
                marker.caret_offset = offset
                self.editor.add_child_in_window(marker, Gtk.TextWindowType.WIDGET, 0, 0)
                marker.show()
                self.extra_carets.append(marker)

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

    def render(self):
        text = self.text()
        theme = self.window.preferences.theme
        if text != self.formatted_text or theme != self.preview_theme:
            layout = markdown_layout(text)
            self.spans, self.items, self.headings = layout.spans, layout.items, layout.headings
            self.diagrams = layout.diagrams
            self.math_spans, self.callouts = layout.math, layout.callouts
            self.formatted_text = text
            self.preview_theme = theme
            self.fold_regions = compute_regions(text, layout)
            self.folds.prune(self.fold_regions)
            self.rebuild_list_widgets()
            self.rebuild_image_widgets(layout.images)
            self.rebuild_diagram_widgets(layout.diagrams)
            self.rebuild_preview_widgets(layout)
        else:
            if self._diagram_theme != self.window.preferences.theme:
                self.rebuild_diagram_widgets(self.diagrams)
            if self.fold_regions is None:
                self.fold_regions = compute_regions(text)
                self.folds.prune(self.fold_regions)
        start, end = self.buffer.get_bounds()
        spacing = [*(entry[3] for entry in self.image_widgets),
                   *(entry[3] for entry in self.diagram_widgets),
                   *(entry[3] for entry in self.preview_entries())]
        for tag in [*self.tags.values(), *self.list_tags.values(), self.hidden, self.occurrence_tag, *spacing]:
            self.buffer.remove_tag(tag, start, end)
        concealed = self._concealed_ranges()
        if self.mode == 'source':
            self._conceal(concealed)
            self._highlight_occurrences()
            self._sync_fold_marks(concealed)
            self._sync_extra_carets()
            self.hide_source_widgets()
            self.queue_positions()
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
                in_selection = selection_overlaps(selected, span.start, span.end)
                if not on_line and not in_selection and not self.syntax_kept_visible(span, cursor, selected):
                    self.buffer.apply_tag(self.hidden, left, right)
            elif span.style in self.tags:
                self.buffer.apply_tag(self.tags[span.style], left, right)

        concealed_offsets = self._concealed_ranges()

        def covered(offset):
            return any(span_start <= offset < span_end for span_start, span_end in concealed_offsets)

        for item, widget in self.list_widgets:
            left = self.buffer.get_iter_at_offset(item.start)
            end = self.buffer.get_iter_at_offset(item.end)
            content = self.buffer.get_iter_at_offset(item.content_start)
            self.buffer.apply_tag(self.list_tags[item.depth], left, end)
            # Lists stay rendered on the active line; a selection can expose
            # their source markers for deliberate Markdown editing.
            if covered(item.start):
                widget.hide()
            else:
                reveal = selected and item.start < selected[1] and item.content_start > selected[0]
                widget.set_visible(not reveal)
                if not reveal:
                    self.buffer.apply_tag(self.hidden, left, content)
            if item.checked:
                line_end = text.find('\n', item.content_start)
                stop = len(text) if line_end < 0 else line_end
                self.buffer.apply_tag(self.tags['completed'], content, self.buffer.get_iter_at_offset(stop))
        for image_start, image_end, widget, tag in self.image_widgets:
            if covered(image_start):
                widget.hide()
                continue
            editing_image = image_start <= cursor < image_end
            in_selection = selection_overlaps(selected, image_start, image_end)
            visible = self.mode == 'live' and not editing_image and not in_selection
            widget.set_visible(bool(visible))
            if visible:
                self.conceal_block(image_start, image_end, tag)
        for diagram_start, diagram_end, widget, tag in self.diagram_widgets:
            if covered(diagram_start):
                widget.hide()
                continue
            editing_diagram = diagram_start <= cursor < diagram_end
            in_selection = selected and diagram_start < selected[1] and diagram_end > selected[0]
            visible = self.mode == 'live' and not editing_diagram and not in_selection
            widget.set_visible(bool(visible))
            if visible:
                newline = text.find('\n', diagram_start)
                first_end = diagram_end if newline < 0 or newline >= diagram_end else newline + 1
                left = self.buffer.get_iter_at_offset(diagram_start)
                right = self.buffer.get_iter_at_offset(diagram_end)
                self.buffer.apply_tag(self.hidden, left, right)
                self.buffer.apply_tag(tag, left, self.buffer.get_iter_at_offset(first_end))
        self.preview_blocks(text, cursor, selected, covered)
        self._conceal(concealed_offsets)
        self._highlight_occurrences()
        self._sync_fold_marks(concealed_offsets)
        self._sync_extra_carets()
        self.queue_positions()

    def rebuild_image_widgets(self, images):
        for _, _, widget, tag in self.image_widgets:
            widget.destroy()
            self.buffer.get_tag_table().remove(tag)
        self.image_widgets.clear()
        for start, end, target, width, height in images:
            try:
                path = local_path(self.window.vault, self.note.path, target)
                info = GdkPixbuf.Pixbuf.get_file_info(str(path))
                if not info or not info[1] or not info[2]:
                    continue

                def fallback(path=path, info=info):
                    scale = min(1, 600 / info[1], 360 / info[2])
                    return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        str(path), max(1, int(info[1] * scale)), max(1, int(info[2] * scale)), True)

                pixbuf = scaled_pixbuf(path, width, height, fallback)
            except (OSError, GLib.Error, VaultError, ValueError):
                continue
            widget = Gtk.Image.new_from_pixbuf(pixbuf)
            widget.set_tooltip_text('Place the cursor inside the image Markdown to edit it')
            self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
            tag = self.buffer.create_tag(None, pixels_above_lines=pixbuf.get_height() + 8)
            self.image_widgets.append((start, end, widget, tag))

    def rebuild_diagram_widgets(self, diagrams):
        for _, _, widget, tag in self.diagram_widgets:
            widget.destroy()
            self.buffer.get_tag_table().remove(tag)
        self.diagram_widgets.clear()
        self._diagram_theme = self.window.preferences.theme
        colors = PALETTES[self._diagram_theme]
        for start, end, source in diagrams:
            try:
                rendered = render_mermaid(source, colors)
                pixbuf = pixbuf_bytes(rendered.png)
            except (OSError, ValueError, RuntimeError):
                continue
            widget = Gtk.Image.new_from_pixbuf(pixbuf)
            widget.set_tooltip_text('Place the cursor inside the Mermaid block to edit it')
            self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
            tag = self.buffer.create_tag(None, pixels_above_lines=pixbuf.get_height() + 12)
            self.diagram_widgets.append((start, end, widget, tag))

    def preview_entries(self):
        return [*self.math_widgets, *self.embed_widgets, *self.table_widgets, *self.callout_widgets]

    def hide_source_widgets(self):
        for _, widget in self.list_widgets:
            widget.hide()
        for _, _, widget, _tag in (*self.image_widgets, *self.diagram_widgets, *self.preview_entries()):
            widget.hide()

    def clear_preview_list(self, entries):
        for _, _, widget, tag in entries:
            widget.destroy()
            self.buffer.get_tag_table().remove(tag)
        entries.clear()

    def attach_preview(self, widget, anchor):
        widget.set_halign(Gtk.Align.START)
        self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
        widget.show_all()
        height = max(widget.get_preferred_height().natural_height, 18)
        widget.hide()
        widget.set_no_show_all(True)
        tag = self.buffer.create_tag(None, pixels_above_lines=height + 8)
        return anchor, widget, tag

    def rebuild_preview_widgets(self, layout):
        for entries in (self.math_widgets, self.embed_widgets, self.table_widgets, self.callout_widgets):
            self.clear_preview_list(entries)
        text = self.formatted_text
        color = PALETTES[self.window.preferences.theme]['bright']
        for item in layout.math:
            if not self.math_uses_widget(text, item):
                continue
            widget = self.math_preview(item.expression, color)
            anchor, widget, tag = self.attach_preview(widget, item.start)
            self.math_widgets.append((item.start, item.end, widget, tag))
        for embed in layout.embeds:
            widget = self.embed_preview(embed)
            if widget is None:
                continue
            _anchor, widget, tag = self.attach_preview(widget, embed.start)
            self.embed_widgets.append((embed.start, embed.end, widget, tag))
        for table in layout.tables:
            _anchor, widget, tag = self.attach_preview(self.table_preview(table), table.start)
            self.table_widgets.append((table.start, table.end, widget, tag))
        for callout in layout.callouts:
            _anchor, widget, tag = self.attach_preview(self.callout_preview(callout), callout.start)
            self.callout_widgets.append((callout.start, callout.end, widget, tag))

    def math_uses_widget(self, text, item):
        if '\n' in text[item.start:item.end]:
            return True
        line_start = text.rfind('\n', 0, item.start) + 1
        line_end = text.find('\n', item.end)
        if line_end < 0:
            line_end = len(text)
        return not text[line_start:item.start].strip() and not text[item.end:line_end].strip()

    def math_preview(self, expression, color):
        pixbuf = math_pixbuf(expression, color)
        if pixbuf is None:
            label = Gtk.Label(label=expression or 'Math', xalign=0)
            label.set_tooltip_text('This expression stays in the note. Move the cursor here to edit it.')
            label.get_style_context().add_class('math-preview')
            return label
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.set_tooltip_text(expression)
        image.get_style_context().add_class('math-preview')
        return image

    def embed_preview(self, embed):
        if embed.kind == 'image':
            image = self.preview_image(embed.target)
            if image is not None:
                image.set_tooltip_text('Place the cursor on this line to edit the embed')
                return image
        frame = Gtk.Frame()
        frame.get_style_context().add_class('live-embed')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        title = Gtk.Label(label=embed.target, xalign=0)
        title.get_style_context().add_class('live-embed-title')
        excerpt = Gtk.Label(label=self.embed_excerpt(embed.target), xalign=0)
        excerpt.set_line_wrap(True)
        excerpt.set_max_width_chars(72)
        excerpt.get_style_context().add_class('muted')
        box.pack_start(title, False, False, 0)
        box.pack_start(excerpt, False, False, 0)
        frame.add(box)
        frame.set_tooltip_text('Place the cursor on this line to edit the embed')
        if embed.kind == 'note':
            target = embed.target
            frame.connect('button-press-event', lambda *_args, target=target: self.open_embed(target))
        return frame

    def preview_image(self, target):
        try:
            path = local_path(self.window.vault, self.note.path, target.split('#', 1)[0])
            info = GdkPixbuf.Pixbuf.get_file_info(str(path))
            if not info or not info[1] or not info[2]:
                return None
            scale = min(1, 600 / info[1], 360 / info[2])
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path), max(1, int(info[1] * scale)), max(1, int(info[2] * scale)), True)
        except (OSError, GLib.Error, VaultError):
            return None
        return Gtk.Image.new_from_pixbuf(pixbuf)

    def embed_excerpt(self, target):
        note_target = target.split('#', 1)[0].split('|', 1)[0].strip()
        try:
            candidates = self.window.vault.resolve(self.note.path, note_target)
        except (OSError, VaultError):
            return 'Embedded note'
        if len(candidates) != 1:
            return 'Unresolved embed' if not candidates else 'Several notes match this embed'
        relative = candidates[0]
        try:
            opened = self.window.panes.get(relative)
            source = opened.text() if opened is not None else self.window.vault.read(relative).text
        except (OSError, VaultError, AttributeError):
            return relative
        body = source
        if body.startswith('---\n'):
            end = re.search(r'\n(?:---|\.\.\.)\s*\n', body)
            if end:
                body = body[end.end():]
        for line in body.splitlines():
            stripped = line.strip().lstrip('#').strip()
            if stripped and not stripped.startswith('![['):
                return stripped[:80]
        return 'Empty note'

    def open_embed(self, target):
        self.window.follow_link(self.note.path, target)
        return True

    def table_preview(self, table):
        grid = Gtk.Grid(column_spacing=14, row_spacing=4)
        grid.get_style_context().add_class('live-table')
        grid.set_tooltip_text('Place the cursor inside the table to edit the Markdown')
        shown = table.rows[:12]
        for column, header in enumerate(table.headers):
            label = Gtk.Label(label=self.cell_label(header), xalign=self.cell_xalign(table.alignments[column]))
            label.get_style_context().add_class('live-table-heading')
            grid.attach(label, column, 0, 1, 1)
        for row_index, row in enumerate(shown, 1):
            for column, cell in enumerate(row):
                alignment = table.alignments[column] if column < len(table.alignments) else 'left'
                label = Gtk.Label(label=self.cell_label(cell), xalign=self.cell_xalign(alignment))
                grid.attach(label, column, row_index, 1, 1)
        if len(table.rows) > len(shown):
            more = Gtk.Label(label=f'{len(table.rows) - len(shown)} more rows', xalign=0)
            more.get_style_context().add_class('muted')
            grid.attach(more, 0, len(shown) + 1, max(len(table.headers), 1), 1)
        return grid

    def callout_preview(self, callout):
        box = Gtk.Box(spacing=8)
        box.get_style_context().add_class('callout')
        box.get_style_context().add_class('callout-' + callout.kind.lower())
        kind = Gtk.Label(label=callout.kind.upper())
        kind.get_style_context().add_class('callout-kind')
        box.pack_start(kind, False, False, 0)
        if callout.title:
            title = Gtk.Label(label=callout.title, xalign=0)
            title.get_style_context().add_class('callout-title')
            box.pack_start(title, False, False, 0)
        box.set_tooltip_text('Place the cursor in the callout to edit it')
        return box

    def cell_label(self, text):
        return re.sub(r'\\([\\|])', r'\1', text)

    def cell_xalign(self, alignment):
        return {'center': 0.5, 'right': 1}.get(alignment, 0)

    def syntax_kept_visible(self, span, cursor, selected):
        """Keep math delimiters visible on every line of a math block being edited."""
        for item in self.math_spans:
            if not (item.start <= span.start and span.end <= item.end):
                continue
            if item.start <= cursor < item.end or selection_overlaps(selected, item.start, item.end):
                return True
            if any(start == item.start for start, _end, _widget, _tag in self.math_widgets):
                if line_hits(self.formatted_text, item.start, item.end, cursor):
                    return True
        return False

    def conceal_block(self, start, end, tag, first_line_only=False):
        text = self.formatted_text
        hide_end = end
        newline = text.find('\n', start)
        if first_line_only:
            hide_end = newline if 0 <= newline < end else end
        elif hide_end > start and text[hide_end - 1:hide_end] == '\n':
            hide_end -= 1
        spacing_end = hide_end
        if 0 <= newline < hide_end:
            spacing_end = newline
        if hide_end > start:
            self.buffer.apply_tag(self.hidden, self.buffer.get_iter_at_offset(start),
                                  self.buffer.get_iter_at_offset(hide_end))
        if spacing_end > start:
            self.buffer.apply_tag(tag, self.buffer.get_iter_at_offset(start),
                                  self.buffer.get_iter_at_offset(spacing_end))

    def preview_blocks(self, text, cursor, selected, covered=lambda _offset: False):
        if self.mode != 'live':
            self.hide_source_widgets()
            return
        for start, end, widget, tag in self.table_widgets:
            if covered(start):
                widget.hide()
                continue
            revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, start, end)
            widget.set_visible(not revealed)
            if not revealed:
                self.conceal_block(start, end, tag)
        for start, end, widget, tag in self.embed_widgets:
            if covered(start):
                widget.hide()
                continue
            revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, start, end)
            widget.set_visible(not revealed)
            if not revealed:
                self.conceal_block(start, end, tag)
        for start, end, widget, tag in self.callout_widgets:
            if covered(start):
                widget.hide()
                continue
            revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, start, end)
            widget.set_visible(not revealed)
            if not revealed:
                self.conceal_block(start, end, tag, first_line_only=True)
        widget_starts = {start for start, _end, _widget, _tag in self.math_widgets}
        for item in self.math_spans:
            opener = 2 if text.startswith('$$', item.start) else 1
            closer = 2 if item.end - item.start >= 4 and text.startswith('$$', max(item.end - 2, item.start)) else 1
            if item.start in widget_starts:
                start, end, widget, tag = next(entry for entry in self.math_widgets if entry[0] == item.start)
                if covered(start):
                    widget.hide()
                    continue
                revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, item.start, item.end)
                widget.set_visible(not revealed)
                if revealed:
                    self.buffer.remove_tag(self.hidden, self.buffer.get_iter_at_offset(start),
                                           self.buffer.get_iter_at_offset(end))
                else:
                    self.conceal_block(start, end, tag)
                continue
            widget = None
            revealed = item.start <= cursor < item.end or selection_overlaps(selected, item.start, item.end)
            if revealed:
                self.buffer.remove_tag(self.hidden, self.buffer.get_iter_at_offset(item.start),
                                       self.buffer.get_iter_at_offset(item.start + opener))
                self.buffer.remove_tag(self.hidden, self.buffer.get_iter_at_offset(item.end - closer),
                                       self.buffer.get_iter_at_offset(item.end))
            else:
                self.buffer.apply_tag(self.hidden, self.buffer.get_iter_at_offset(item.start),
                                     self.buffer.get_iter_at_offset(item.start + opener))
                self.buffer.apply_tag(self.hidden, self.buffer.get_iter_at_offset(item.end - closer),
                                     self.buffer.get_iter_at_offset(item.end))

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
        for start, _, widget, _ in (*self.image_widgets, *self.diagram_widgets, *self.preview_entries()):
            if not widget.get_visible():
                continue
            position = self.buffer.get_iter_at_offset(start)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            height = widget.get_preferred_height().natural_height
            self.editor.move_child(widget, x, y - height - 4)
        for button in self.fold_widgets:
            if not button.get_visible():
                continue
            position = self.buffer.get_iter_at_offset(button.fold_anchor)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            width = button.get_preferred_width().natural_width
            height = button.get_preferred_height().natural_height
            y += max(0, (rect.height - height) // 2)
            button.set_child_visible(y >= 0 and y + height <= self.editor.get_allocated_height())
            self.editor.move_child(button, max(0, x - width - 2), y)
        for marker in self.extra_carets:
            position = self.buffer.get_iter_at_offset(marker.caret_offset)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            height = marker.get_preferred_height().natural_height
            marker.set_child_visible(y >= 0 and y + height <= self.editor.get_allocated_height())
            self.editor.move_child(marker, x, y)
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
        self.tab_label.set_text(Path(note.path).stem)
        self.render()
        self.findbar.refresh()


class VersionHistoryDialog(Gtk.Dialog):
    """Saved snapshots for one note, with a single undoable restore."""

    def __init__(self, window, path):
        super().__init__(title='Version history', transient_for=window, modal=True, use_header_bar=True)
        self.owner = window
        self.path = path
        self.store = window.version_history
        self.set_default_size(780, 560)
        self.set_destroy_with_parent(True)
        self.restore_button = self.add_button('Restore version', Gtk.ResponseType.APPLY)
        self.add_button('Close', Gtk.ResponseType.CLOSE)
        self.connect('response', self._respond)
        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        heading = Gtk.Label(label=path, xalign=0)
        heading.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        heading.set_selectable(True)
        heading.get_style_context().add_class('muted')
        content.pack_start(heading, False, False, 0)
        body = Gtk.Box(spacing=12)
        content.pack_start(body, True, True, 0)
        self.version_list = Gtk.ListBox()
        self.version_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.empty_history = Gtk.Label(label='No saved versions yet.', xalign=0, wrap=True)
        self.empty_history.get_style_context().add_class('muted')
        self.version_list.set_placeholder(self.empty_history)
        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroll.set_size_request(240, -1)
        list_scroll.add(self.version_list)
        body.pack_start(list_scroll, False, False, 0)
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.pack_start(preview_box, True, True, 0)
        self.diff_toggle = Gtk.CheckButton.new_with_label('Show differences from the current note')
        self.diff_toggle.connect('toggled', self._show_preview)
        preview_box.pack_start(self.diff_toggle, False, False, 0)
        self.preview = Gtk.TextView()
        self.preview.set_editable(False)
        self.preview.set_cursor_visible(False)
        self.preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.preview.set_monospace(True)
        self.preview.set_left_margin(8)
        self.preview.set_right_margin(8)
        self.preview.set_top_margin(8)
        preview_box.pack_start(scrolled(self.preview), True, True, 0)
        self.version_list.connect('row-selected', self._show_preview)
        self._reload()

    def _reload(self):
        for row in self.version_list.get_children():
            row.destroy()
        snapshots = [] if self.store is None else self.store.versions(self.path)
        for snapshot in reversed(snapshots):
            self._add_row(snapshot)
        rows = self.version_list.get_children()
        if not rows:
            self.restore_button.set_sensitive(False)
            self.preview.get_buffer().set_text('No saved versions yet.')
            return
        current = self._current_text()
        chosen = rows[0]
        for row in rows:
            if row.snapshot.text.replace('\r\n', '\n') != current:
                chosen = row
                break
        self.version_list.select_row(chosen)
        self._show_preview()

    def _add_row(self, snapshot):
        row = Gtk.ListBoxRow()
        row.snapshot = snapshot
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        when = Gtk.Label(label=format_timestamp(snapshot.timestamp), xalign=0)
        excerpt_source = snapshot.text.replace('\r\n', '\n').strip()
        excerpt = excerpt_source.split('\n', 1)[0][:72] if excerpt_source else '(empty)'
        summary = Gtk.Label(label=excerpt, xalign=0)
        summary.set_ellipsize(Pango.EllipsizeMode.END)
        summary.get_style_context().add_class('muted')
        box.pack_start(when, False, False, 0)
        box.pack_start(summary, False, False, 0)
        row.add(box)
        row.get_accessible().set_name(f'Saved version {format_timestamp(snapshot.timestamp)}')
        self.version_list.add(row)

    def _current_text(self):
        pane = self.owner.panes.get(self.path)
        if pane is not None:
            return pane.text()
        try:
            return self.owner.vault.read(self.path).text.replace('\r\n', '\n')
        except (OSError, VaultError):
            return ''

    def _show_preview(self, *_):
        row = self.version_list.get_selected_row()
        self.restore_button.set_sensitive(row is not None)
        buffer = self.preview.get_buffer()
        if row is None:
            buffer.set_text('No saved versions yet.')
            return
        saved = row.snapshot.text.replace('\r\n', '\n')
        if self.diff_toggle.get_active():
            diff = ''.join(difflib.unified_diff(
                self._current_text().splitlines(keepends=True), saved.splitlines(keepends=True),
                fromfile='Current note', tofile='Saved version', n=3))
            buffer.set_text(diff or 'No differences from the current note.')
        else:
            buffer.set_text(saved)

    def restore(self):
        row = self.version_list.get_selected_row()
        pane = self.owner.panes.get(self.path)
        if row is None or pane is None:
            self.owner.status.set_text('Open this note to restore a version.')
            return
        restored = row.snapshot.text.replace('\r\n', '\n')
        current = pane.text()
        if restored != current:
            pane.apply_edit(TextEdit(0, len(current), restored))
        self.owner.status.set_text('Restored a saved version. Undo returns to the text you were editing.')
        self._show_preview()

    def _respond(self, _, response):
        if response == Gtk.ResponseType.APPLY:
            self.restore()
            return
        self.destroy()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, folder=None, preferences_path=None):
        super().__init__(application=app, title='Libre Vault')
        self.set_default_size(1360, 850)
        self.vault = None
        self.recovery = None
        self.version_history = None
        self.version_dialog = None
        self.confirming_panes = False
        self.panes = {}
        self.outline_rows = []
        self.collapsed_folders = set()
        self.palette = None
        self.cheatsheet = None
        self.settings_dialog = None
        self.graph_window = None
        self.graph_views = set()
        self.bases_views = set()
        self.canvas_views = set()
        self.slides_window = None
        self.converter_dialog = None
        self.preferences_store = PreferencesStore(preferences_path)
        self.preferences = self.preferences_store.load()
        self.workspace_store = WorkspaceStore(self.preferences_store.path)
        self.saved_workspaces = SavedWorkspacesStore(self.preferences_store.path)
        self.bookmark_store = BookmarkStore(self.preferences_store.path)
        self.bookmark_items = []
        self.bookmark_manager = None
        self.workspace_manager = None
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
        self.bookmarks_heading = Gtk.Label(label='Bookmarks', xalign=0)
        self.bookmarks_heading.get_style_context().add_class('caption')
        sidebar.pack_start(self.bookmarks_heading, False, False, 0)
        self.bookmarks_empty = Gtk.Label(label='No bookmarks yet', xalign=0)
        self.bookmarks_empty.get_style_context().add_class('bookmark-empty')
        self.bookmarks_empty.set_no_show_all(True)
        sidebar.pack_start(self.bookmarks_empty, False, False, 0)
        self.bookmark_list = Gtk.ListBox()
        self.bookmark_list.get_style_context().add_class('note-list')
        self.bookmark_list.get_style_context().add_class('bookmark-list')
        self.bookmark_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.bookmark_list.set_activate_on_single_click(True)
        self.bookmark_list.connect('row-activated', self.activate_bookmark)
        self.bookmarks_scroll = Gtk.ScrolledWindow()
        self.bookmarks_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.bookmarks_scroll.set_propagate_natural_height(True)
        self.bookmarks_scroll.set_max_content_height(160)
        self.bookmarks_scroll.set_no_show_all(True)
        self.bookmarks_scroll.add(self.bookmark_list)
        sidebar.pack_start(self.bookmarks_scroll, False, False, 0)
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
        self.tabs = WorkspaceTabs()
        self.tabs.connect('switch-page', self.tab_switched)
        self.tabs.connect('page-removed', self.tab_removed)
        self.tabs.connect('page-reordered', lambda *_: self.persist_workspace())
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
        self.counts = Gtk.Label(xalign=1)
        self.counts.get_style_context().add_class('status')
        self.counts.get_accessible().set_name('Word and character count')
        self.counts.set_tooltip_text('Words, characters, and lines in the active note')
        self.vim_status = Gtk.Label(label='')
        self.vim_status.get_style_context().add_class('vim-status')
        self.vim_status.get_accessible().set_name('Vim mode')
        statusbar.pack_end(format_label, False, False, 0)
        statusbar.pack_end(self.counts, False, False, 0)
        statusbar.pack_end(self.vim_status, False, False, 0)
        root.pack_end(statusbar, False, False, 0)
        self.workspace_source = GLib.timeout_add_seconds(5, self.persist_workspace)
        self.connect('destroy', self.cleanup)
        self.show_all()
        self.refresh_bookmark_list()
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
        for view in tuple(getattr(self, 'canvas_views', ())):
            view.cancel_autosave()
        for window in (self.version_dialog, self.graph_window, self.slides_window,
                       self.converter_dialog, self.bookmark_manager, self.workspace_manager):
            if window is not None:
                window.destroy()
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

    def editors(self):
        return [*self.panes.values(), *self.canvas_views]

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
        if not self.confirm_panes(self.editors()):
            vault.close()
            return
        self.persist_workspace()
        self.release_vault()
        self.vault = vault
        self.bookmark_items = self.bookmark_store.load(vault.root)
        self.navigation_history = NavigationHistory()
        self.recovery = RecoveryStore(vault.root)
        self.version_history = VersionHistoryStore(vault.root)
        vault.version_history = self.version_history
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
        self.refresh_bookmark_list()
        self.vault_monitor = VaultMonitor(vault, self.refresh, self.status.set_text)
        try:
            self.last_vault_store.save(vault.root)
        except OSError as error:
            self.status.set_text(f'Could not remember the opened vault: {error}')

    def clear_editors(self):
        self.close_split()
        for source in self.scroll_restore_sources.values():
            GLib.source_remove(source)
        self.scroll_restore_sources.clear()
        while self.tabs.get_n_pages():
            pane = self.tabs.get_nth_page(0)
            self.tabs.remove_page(0)
            pane.destroy()
        self.panes.clear()

    def release_vault(self):
        for window in (self.version_dialog, self.graph_window, self.slides_window,
                       self.converter_dialog, self.bookmark_manager, self.workspace_manager):
            if window is not None:
                window.destroy()
        if self.vault_monitor:
            self.vault_monitor.close()
            self.vault_monitor = None
        if self.reading_source is not None:
            GLib.source_remove(self.reading_source)
            self.reading_source = None
        for overlay in (self.palette, self.quick_switcher):
            if overlay is not None:
                overlay.destroy()
        self.clear_editors()
        self.version_history = None
        if self.recovery:
            self.recovery.close()
            self.recovery = None
        if self.vault:
            self.vault.close()
        self.bookmark_items = []
        self.refresh_bookmark_list()

    def close_vault(self, *_):
        if self.vault is None:
            return True
        if not self.confirm_panes(self.editors()):
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
            paths = tree_paths([*paths, *self.vault.canvases], self.vault.folders, self.collapsed_folders)
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
            icon_name = 'folder-symbolic' if is_folder else 'x-office-drawing-symbolic' if path.lower().endswith('.canvas') else 'text-x-generic-symbolic'
            icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
            icon.get_style_context().add_class('muted')
            content.pack_start(icon, False, False, 0)
            names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            show_name = is_folder or path.lower().endswith('.canvas')
            label = Gtk.Label(label=Path(path).name if show_name else Path(path).stem, xalign=0)
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
        selected_path = pane.note.path if pane else None
        if selected_path is None:
            page = self.active_page()
            if isinstance(page, CanvasView):
                selected_path = page.path
        if selected_path:
            for row in self.files.get_children():
                if row.path == selected_path:
                    self.files.select_row(row)
                    break
        self.files.show_all()

    def activate_file(self, _, row):
        if row.path.endswith('/'):
            self.toggle_folder(row.path)
        elif row.path.lower().endswith('.canvas'):
            self.open_canvas(row.path)
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
        if not self.confirm_panes(self.editors()):
            return False
        # Discard choices in confirm_panes must not leave stale dirty buffers
        # able to autosave over incoming-link updates.
        for pane in self.panes.values():
            if pane.dirty:
                pane.load_external(self.vault.read(pane.note.path))
        for view in tuple(self.canvas_views):
            if view.dirty:
                view.reload_from_disk()
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
        for view in tuple(self.canvas_views):
            moved = mapping.get(view.path)
            if moved and moved != view.path:
                view.retarget(moved)
        old_folder, new_folder = relative.rstrip('/'), destination.rstrip('/')
        self.collapsed_folders = {new_folder + path[len(old_folder):]
                                 if path == old_folder or path.startswith(old_folder + '/') else path
                                 for path in self.collapsed_folders}
        self.collapsed_folders.intersection_update(self.vault.folders)
        mapping = with_moved_root(mapping, relative, destination)
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
        if relative and relative not in deleted:
            deleted.append(relative)
        panes = [pane for path, pane in self.panes.items() if path in deleted]
        canvases = [view for view in self.canvas_views
                    if view.path == relative or view.path.startswith(relative + '/')]
        if not self.confirm_panes([*panes, *canvases]):
            return False
        for view in canvases:
            view.cancel_autosave()
            view.dirty = False
        target = self.vault.trash(relative)
        for pane in panes:
            if self.recovery:
                self.recovery.remove(pane.note.path)
            self.tabs.remove_page(self.tabs.page_num(pane))
            self.panes.pop(pane.note.path, None)
        for view in canvases:
            page_number = self.tabs.page_num(view)
            if page_number >= 0:
                self.tabs.remove_page(page_number)
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
            offset = find_anchor_offset(text, fragment)
            if offset is None:
                # Keep the older line search when the fragment is not an exact
                # heading or block id, so partial and hand-written links still land.
                needle = fragment[1:] if fragment.startswith('^') else fragment
                if needle:
                    match = re.search(r'(?m)^.*' + re.escape(needle) + r'.*$', text, re.IGNORECASE)
                    offset = match.start() if match else None
            if offset is not None:
                position = pane.buffer.get_iter_at_offset(offset)
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
        page = self.active_page()
        if isinstance(page, CanvasView):
            self.try_action(page.save)
            return
        pane = self.current()
        if pane:
            self.try_action(pane.save)

    def toggle_highlight(self, *_):
        pane = self.current()
        if pane is not None and pane.mode != 'reading':
            pane.highlight()

    def toggle_strikethrough(self, *_):
        pane = self.current()
        if pane is not None and pane.mode != 'reading':
            pane.strikethrough()

    def close_tab(self, *_):
        self.close_note(self.active_page())

    def close_note(self, pane):
        if not isinstance(pane, NotePane):
            if pane is not None:
                page = self.tabs.page_num(pane)
                if page >= 0:
                    self.tabs.remove_page(page)
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

    def new_canvas(self, *_, parent=''):
        self.new_item('canvas', parent)

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
            if kind == 'note':
                create = self.vault.create
            elif kind == 'canvas':
                create = self.vault.create_canvas
            else:
                create = self.vault.create_folder
            created = self.try_action(lambda: create(entry.get_text()))
            if created is not None:
                path = created if kind == 'folder' else created.path
                self.collapsed_folders.difference_update(str(parent) for parent in Path(path).parents)
                self.collapsed_folders.discard(path)
                self.search_entry.set_text('')
                self.populate_files()
                if kind == 'note':
                    self.open_note(created.path)
                elif kind == 'canvas':
                    self.open_canvas(created.path)
                else:
                    for row in self.files.get_children():
                        if row.path == created + '/':
                            self.files.select_row(row)
                            break
                    self.status.set_text(f'Created folder · {created}')
        dialog.destroy()

    def reveal_note(self, path):
        self.collapsed_folders.difference_update(str(parent) for parent in Path(path).parents)
        self.collapsed_folders.discard(path)
        if self.search_entry.get_text():
            self.search_entry.set_text('')
        else:
            self.populate_files()
        return self.open_note(path)

    def open_daily_note(self, *_):
        if not self.vault:
            self.choose_vault()
            return

        def action():
            note, created = ensure_daily_note(self.vault, self.preferences)
            pane = self.reveal_note(note.path)
            if pane:
                verb = 'Created daily note' if created else 'Opened daily note'
                self.status.set_text(f'{verb} · {note.path}')
        self.try_action(action)

    def insert_table(self, *_):
        pane = self.current()
        if pane is None:
            self.status.set_text('Open a note to insert a table.')
            return
        pane.prompt_insert_table()

    def edit_table(self, *_):
        pane = self.current()
        if pane is None:
            self.status.set_text('Open a note to edit a table.')
            return
        pane.prompt_edit_table()

    def insert_template(self, *_, path=None, pane=None):
        if not self.vault:
            self.choose_vault()
            return
        pane = pane or self.current()
        if pane is None:
            self.status.set_text('Open a note before inserting a template.')
            return
        original = pane.text()
        bounds = pane.buffer.get_selection_bounds()
        if bounds:
            start, end = (item.get_offset() for item in bounds)
        else:
            start = end = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        if path is None:
            paths = template_paths(self.vault, self.preferences.templates_folder)
            if not paths:
                folder = self.preferences.templates_folder or 'the vault root'
                self.status.set_text(f'No templates in {folder}.')
                return
            dialog = TemplatePicker(self, paths)
            try:
                accepted = dialog.run() == Gtk.ResponseType.OK and bool(dialog.choice)
                path = dialog.choice
            finally:
                dialog.destroy()
            if not accepted:
                return

        def action():
            if pane.text() != original:
                self.status.set_text('The note changed while choosing a template. Try again.')
                return
            replacement = expand_template(read_template(self.vault, path), title=Path(pane.note.path).stem)
            pane.apply_edit(TextEdit(start, end, replacement))
            pane.editor.grab_focus()
            self.status.set_text(f'Inserted template · {path}')
        self.try_action(action)

    def create_from_template(self, *_, template=None, name=None):
        if not self.vault:
            self.choose_vault()
            return
        if template is None:
            paths = template_paths(self.vault, self.preferences.templates_folder)
            if not paths:
                folder = self.preferences.templates_folder or 'the vault root'
                self.status.set_text(f'No templates in {folder}.')
                return
            dialog = TemplatePicker(self, paths, title='Choose a template', confirm='Choose')
            try:
                accepted = dialog.run() == Gtk.ResponseType.OK and bool(dialog.choice)
                template = dialog.choice
            finally:
                dialog.destroy()
            if not accepted:
                return
        if name is None:
            dialog = NoteNameDialog(self)
            try:
                if dialog.run() != Gtk.ResponseType.OK:
                    return
                name = dialog.entry.get_text().strip()
            finally:
                dialog.destroy()
            if not name:
                return

        def action():
            note = create_note_from_template(self.vault, template, name)
            if self.reveal_note(note.path):
                self.status.set_text(f'Created note · {note.path}')
        self.try_action(action)

    def new_unique_note(self, *_, title=None, template=None):
        if not self.vault:
            self.choose_vault()
            return
        if title is None and template is None:
            paths = template_paths(self.vault, self.preferences.templates_folder)
            dialog = UniqueNoteDialog(self, paths)
            try:
                if dialog.run() != Gtk.ResponseType.OK:
                    return
                title = dialog.title_entry.get_text()
                template = dialog.template_path()
            finally:
                dialog.destroy()

        def action():
            note = create_unique_note(self.vault, self.preferences, title=title or '', template=template or '')
            if self.reveal_note(note.path):
                self.status.set_text(f'Unique note · {note.path}')
        self.try_action(action)

    def open_random_note(self, *_):
        if not self.vault:
            self.choose_vault()
            return
        path = random_note_path(self.vault)
        if not path:
            self.status.set_text('No readable notes to open.')
            return
        if self.open_note(path):
            self.status.set_text(f'Random note · {path}')

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

    def mention_target(self, relative):
        stem = Path(relative).stem
        filename = Path(relative).name
        matches = [path for path in self.vault.notes if Path(path).name == filename]
        if len(matches) == 1:
            return stem
        if relative.lower().endswith('.md'):
            return relative[:-3]
        return relative

    def add_unlinked_mention(self, mention):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row.set_margin_bottom(6)
        location = Gtk.Label(label=f'{mention.source_path}:{mention.line_number}', xalign=0)
        location.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        location.set_tooltip_text(mention.source_path)
        location.get_style_context().add_class('outline-empty')
        snippet = Gtk.Label(label=mention.snippet, xalign=0)
        snippet.set_line_wrap(True)
        snippet.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        snippet.set_max_width_chars(28)
        snippet.set_selectable(True)
        actions = Gtk.Box(spacing=4)
        open_button = Gtk.Button(label='Open')
        open_button.mention = mention
        open_button.mention_action = 'open'
        open_button.get_style_context().add_class('context-link')
        open_button.get_accessible().set_name(f'Open mention {mention.source_path}:{mention.line_number}')
        open_button.set_tooltip_text(f'Open {mention.source_path} at this mention')
        open_button.connect('clicked', lambda *_: self.open_mention(mention))
        link_button = Gtk.Button(label='Link')
        link_button.mention = mention
        link_button.mention_action = 'link'
        link_button.get_style_context().add_class('context-link')
        link_button.get_accessible().set_name(f'Link mention {mention.source_path}:{mention.line_number}')
        link_button.set_tooltip_text(f'Link “{mention.matched_term}” to this note')
        link_button.connect('clicked', lambda *_: self.link_unlinked_mention(mention))
        actions.pack_start(open_button, False, False, 0)
        actions.pack_start(link_button, False, False, 0)
        row.pack_start(location, False, False, 0)
        row.pack_start(snippet, False, False, 0)
        row.pack_start(actions, False, False, 0)
        self.context.pack_start(row, False, False, 0)

    def open_mention(self, mention):
        pane = self.open_note(mention.source_path)
        if pane is None:
            return
        text = pane.text()
        offset = mention.match_start
        if text[offset:offset + len(mention.matched_term)] != mention.matched_term:
            offset = text.find(mention.matched_term)
            if offset < 0:
                offset = 0
        position = pane.buffer.get_iter_at_offset(min(offset, len(text)))
        pane.buffer.place_cursor(position)
        pane.editor.scroll_to_iter(position, 0.15, False, 0, 0)
        pane.editor.grab_focus()

    def link_unlinked_mention(self, mention):
        target = self.current()
        if target is None or self.vault is None:
            return
        target_name = self.mention_target(target.note.path)

        def replacement_for(text):
            if text[mention.match_start:mention.match_end] != mention.matched_term:
                return None
            updated = link_mention(text, mention.match_start, mention.match_end, target_name)
            delta = len(updated) - len(text)
            return updated[mention.match_start:mention.match_end + delta]

        source = self.panes.get(mention.source_path)
        if source is not None:
            replacement = replacement_for(source.text())
            if replacement is None:
                self.status.set_text(f'The mention in {mention.source_path} changed. It was not linked.')
                self.update_context()
                return
            source.apply_edit(TextEdit(mention.match_start, mention.match_end, replacement))
            self.status.set_text(f'Linked “{mention.matched_term}” in {mention.source_path}')
            self.update_context()
            return

        def action():
            note = self.vault.read(mention.source_path)
            if replacement_for(note.text) is None:
                raise VaultError(f'The mention in {mention.source_path} changed. It was not linked.')
            return self.vault.save(note, link_mention(
                note.text, mention.match_start, mention.match_end, target_name))

        if self.try_action(action) is not None:
            self.status.set_text(f'Linked “{mention.matched_term}” in {mention.source_path}')
            self.update_context()

    def show_version_history(self, *_):
        pane = self.current()
        if pane is None or self.vault is None or self.version_history is None:
            self.status.set_text('Open a note to see its version history.')
            return None
        if self.version_dialog is not None:
            self.version_dialog.destroy()
        self.version_dialog = VersionHistoryDialog(self, pane.note.path)
        self.version_dialog.connect('destroy', self._forget_version_dialog)
        self.version_dialog.show_all()
        return self.version_dialog

    def _forget_version_dialog(self, dialog):
        if self.version_dialog is dialog:
            self.version_dialog = None

    def update_counts(self):
        if self.in_destruction():
            return
        pane = self.current()
        if pane is None:
            self.counts.set_text('')
            return
        self.counts.set_text(count_summary(pane.text(), pane.buffer.get_line_count()))

    def update_vim_status(self):
        label = getattr(self, 'vim_status', None)
        if label is None or self.in_destruction():
            return
        pane = self.current()
        if self.preferences.vim_mode and isinstance(pane, NotePane) and pane.vim.active:
            label.set_text(mode_label(pane.vim.session))
        else:
            label.set_text('')

    def update_context(self):
        self.update_vim_status()
        if self.vault:
            for graph in tuple(self.graph_views):
                graph.refresh_if_changed()
            for bases in tuple(self.bases_views):
                bases.refresh_if_changed()
        self.outline_rows = []
        for child in self.context.get_children():
            child.destroy()
        page = self.active_page()
        if isinstance(page, CanvasView) and self.vault:
            self.save_button.set_sensitive(True)
            self.location_label.set_text(f'{self.vault.root.name}  /  {page.path}')
            self.location_label.set_tooltip_text(str(self.vault.root / page.path))
            for row in self.files.get_children():
                if row.path == page.path:
                    self.files.select_row(row)
                    break
            self.section('Canvas')
            summary = Gtk.Label(label=f'{len(page.document.nodes)} cards · {len(page.document.edges)} connections', xalign=0)
            summary.set_line_wrap(True)
            summary.get_style_context().add_class('outline-empty')
            self.context.pack_start(summary, False, False, 0)
            self.context.show_all()
            return False
        pane = self.current()
        self.save_button.set_sensitive(pane is not None)
        if not pane or not self.vault:
            self.location_label.set_text(self.vault.root.name if self.vault else 'Local workspace')
            self.section('Outline · Links · Backlinks')
            self.context.show_all()
            self.update_counts()
            return False
        self.location_label.set_text(f'{self.vault.root.name}  /  {pane.note.path}')
        self.location_label.set_tooltip_text(str(self.vault.root / pane.note.path))
        pane.sync_bookmark_button()
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
        self.section('Unlinked mentions')
        texts = {path: item.text() for path, item in self.panes.items()}
        mentions = self.vault.unlinked_mentions(pane.note.path, texts)
        if not mentions:
            empty = Gtk.Label(label='No unlinked mentions.', xalign=0)
            empty.get_style_context().add_class('outline-empty')
            self.context.pack_start(empty, False, False, 0)
        else:
            shown = mentions[:100]
            for mention in shown:
                self.add_unlinked_mention(mention)
            if len(mentions) > len(shown):
                more = Gtk.Label(
                    label=f'{len(shown)} of {len(mentions)} unlinked mentions are shown.', xalign=0)
                more.set_line_wrap(True)
                more.set_max_width_chars(28)
                more.get_style_context().add_class('outline-empty')
                self.context.pack_start(more, False, False, 0)
        self.context_button('Version history…', self.show_version_history)
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
        self.update_counts()
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
            for view in tuple(self.canvas_views):
                revision = self.vault.canvases.get(view.path)
                if view.path not in self.vault.canvases or revision is None:
                    self.status.set_text(f'{view.path} was removed or cannot be read. Open canvas edits are retained.')
                elif revision != view.record.revision and view.dirty:
                    self.status.set_text(f'External change to {view.path}. Your unsaved edits are retained; saving will stop on conflict.')
                elif revision != view.record.revision:
                    try:
                        view.reload_from_disk()
                    except (VaultError, OSError) as error:
                        self.status.set_text(str(error))
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
            return
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

    def show_bases(self, *_, state=None):
        if not self.vault:
            return None
        if state is None:
            for index in range(self.tabs.get_n_pages()):
                page = self.tabs.get_nth_page(index)
                if isinstance(page, BasesView):
                    self.tabs.set_current_page(index)
                    page.search.grab_focus()
                    return page
        self.remember_location()
        bases = BasesView(self, state=state)
        tab = Gtk.Box(spacing=6)
        tab.pack_start(Gtk.Label(label='Bases'), True, True, 0)
        close = self.icon_button('window-close-symbolic', 'Close bases tab · Ctrl+W',
                                 lambda *_: self.close_note(bases))
        close.set_focus_on_click(False)
        tab.pack_end(close, False, False, 0)
        tab.show_all()
        self.tabs.append_page(bases, tab)
        self.tabs.set_current_page(self.tabs.page_num(bases))
        bases.search.grab_focus()
        self.update_context()
        return bases

    def show_slides(self, *_):
        if not self.vault:
            return None
        pane = self.current()
        if pane is None:
            self.status.set_text('Open a note to start the slides presentation.')
            return None
        if self.slides_window is not None:
            self.slides_window.destroy()
        self.slides_window = SlidesWindow(self, pane.note.path, pane.text())
        return self.slides_window

    def show_converter(self, *_):
        if not self.vault:
            return None
        if self.converter_dialog is None:
            self.converter_dialog = ConverterDialog(self)
            self.converter_dialog.connect('destroy', lambda *_: setattr(self, 'converter_dialog', None))
            self.converter_dialog.show_all()
        else:
            self.converter_dialog.present()
        return self.converter_dialog

    def open_canvas(self, relative):
        if not self.vault:
            return None
        for view in self.canvas_views:
            if view.path == relative and self.tabs.page_num(view) >= 0:
                self.tabs.set_current_page(self.tabs.page_num(view))
                view.drawing.grab_focus()
                self.update_context()
                return view
        opened = []

        def action():
            record = self.vault.read_canvas(relative)
            view = CanvasView(self, record)
            opened.append(view)
            tab = Gtk.Box(spacing=6)
            tab.set_tooltip_text(relative)
            tab.pack_start(view.tab_label, True, True, 0)
            close = self.icon_button('window-close-symbolic', 'Close canvas · Ctrl+W',
                                     lambda *_: self.close_note(view))
            close.set_focus_on_click(False)
            tab.pack_end(close, False, False, 0)
            tab.show_all()
            self.tabs.append_page(view, tab)
            self.tabs.set_current_page(self.tabs.page_num(view))
            view.drawing.grab_focus()

        self.remember_location()
        self.try_action(action)
        if not opened:
            return None
        self.update_context()
        return opened[0]

    def open_canvas_command(self, *_):
        if not self.vault:
            return
        paths = [path for path, revision in self.vault.canvases.items() if isinstance(revision, str)]
        if not paths:
            self.status.set_text('No canvas files in this vault.')
            return
        if len(paths) == 1:
            self.open_canvas(paths[0])
            return
        dialog = Gtk.Dialog(title='Open canvas', transient_for=self, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Open', Gtk.ResponseType.OK)
        dialog.set_default_size(440, 320)
        listing = Gtk.ListBox()
        listing.set_selection_mode(Gtk.SelectionMode.BROWSE)
        for path in paths:
            row = Gtk.ListBoxRow()
            row.canvas_path = path
            label = Gtk.Label(label=path, xalign=0)
            label.set_margin_start(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            row.add(label)
            listing.add(row)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(200)
        scroll.add(listing)
        dialog.get_content_area().set_border_width(12)
        dialog.get_content_area().add(scroll)
        dialog.show_all()
        listing.select_row(listing.get_row_at_index(0))
        accepted = dialog.run() == Gtk.ResponseType.OK
        selected = listing.get_selected_row()
        dialog.destroy()
        if accepted and selected is not None:
            self.open_canvas(selected.canvas_path)

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
        if not isinstance(pane, NotePane):
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
        if isinstance(pane, CanvasView):
            self.status.set_text('Canvas files stay in their own tab.')
            return
        if pane:
            self.open_split(pane)

    def open_split(self, pane):
        if isinstance(pane, CanvasView):
            self.status.set_text('Canvas files stay in their own tab.')
            return
        if not isinstance(pane, (NotePane, GraphView)):
            return
        from .splitview import SplitTextView
        self.close_split()
        self.split_pane = pane
        bar = Gtk.Box(spacing=6)
        title = Gtk.Label(label='Graph' if isinstance(pane, GraphView) else pane.path if isinstance(pane, CanvasView) else pane.note.path, xalign=0)
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

    def note_is_bookmarked(self, path, fragment=''):
        return any(item.type == 'note' and item.path == path and item.fragment == fragment
                   for item in self.bookmark_items)

    def persist_bookmarks(self):
        if self.vault is None:
            return
        try:
            self.bookmark_store.save(self.vault.root, self.bookmark_items)
        except OSError as error:
            self.status.set_text(f'Could not save bookmarks: {error}')

    def refresh_bookmark_list(self, select=0):
        for row in self.bookmark_list.get_children():
            row.destroy()
        for index, bookmark in enumerate(self.bookmark_items):
            row = Gtk.ListBoxRow()
            row.index = index
            row.title = bookmark.display_title()
            content = Gtk.Box(spacing=6)
            icon_name = BOOKMARK_ICONS.get(bookmark.type, 'emblem-favorite-symbolic')
            icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
            icon.get_style_context().add_class('muted')
            content.pack_start(icon, False, False, 0)
            label = Gtk.Label(label=bookmark.display_title(), xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            content.pack_start(label, True, True, 0)
            row.add(content)
            row.set_tooltip_text(bookmark.detail())
            row.get_accessible().set_name(bookmark.display_title())
            self.bookmark_list.add(row)
        if self.bookmark_items:
            self.bookmarks_empty.hide()
            self.bookmark_list.show_all()
            self.bookmarks_scroll.show()
        else:
            self.bookmarks_scroll.hide()
            self.bookmarks_empty.show()
        if self.bookmark_manager is not None:
            self.bookmark_manager.reload(select)
        self.sync_bookmark_buttons()

    def sync_bookmark_buttons(self):
        for pane in self.panes.values():
            pane.sync_bookmark_button()

    def activate_bookmark(self, _, row):
        if 0 <= row.index < len(self.bookmark_items):
            self.open_bookmark(self.bookmark_items[row.index])

    def open_bookmark(self, bookmark):
        if self.vault is None:
            return
        if bookmark.type == 'note':
            if bookmark.path not in self.vault.notes:
                self.status.set_text(f'Bookmarked note is missing · {bookmark.path}')
                return
            self.open_note(bookmark.path, bookmark.fragment)
            self.status.set_text(f'Opened bookmark · {bookmark.display_title()}')
            return
        if bookmark.type == 'search':
            self.search_entry.set_text(bookmark.query)
            self.search_entry.grab_focus()
            self.status.set_text(f'Search · {bookmark.display_title()}')
            return
        if bookmark.type == 'folder':
            self.reveal_folder(bookmark.path)
            return
        if bookmark.type == 'graph':
            self.show_graph_tab(state=graph_open_state(bookmark))
            self.status.set_text(f'Opened bookmark · {bookmark.display_title()}')

    def reveal_folder(self, path):
        folder = path.strip().rstrip('/')
        if folder not in self.vault.folders:
            self.status.set_text(f'Bookmarked folder is missing · {folder}')
            return
        ancestors = {str(parent) for parent in Path(folder).parents if str(parent) not in ('.', '')}
        self.collapsed_folders.difference_update(ancestors)
        self.collapsed_folders.discard(folder)
        if self.search_entry.get_text():
            self.search_entry.set_text('')
        else:
            self.populate_files()
        target = folder + '/'
        for row in self.files.get_children():
            if row.path == target:
                self.files.select_row(row)
                row.grab_focus()
                break
        self.status.set_text(f'Folder · {folder}')

    def toggle_note_bookmark(self, pane=None):
        if not isinstance(pane, NotePane):
            pane = self.current()
        if pane is None:
            self.status.set_text('Open a note to bookmark it.')
            return
        if self.note_is_bookmarked(pane.note.path):
            self.remove_note_bookmark(pane.note.path)
        else:
            self.add_note_bookmark(pane.note.path)

    def bookmark_current_note(self, *_):
        pane = self.current()
        if pane is None:
            self.status.set_text('Open a note to bookmark it.')
            return
        self.add_note_bookmark(pane.note.path)

    def remove_current_bookmark(self, *_):
        pane = self.current()
        if pane is None:
            self.status.set_text('Open a note to remove its bookmark.')
            return
        self.remove_note_bookmark(pane.note.path)

    def add_note_bookmark(self, path, fragment=''):
        if self.vault is None:
            return
        if self.note_is_bookmarked(path, fragment):
            self.status.set_text('That note is already bookmarked.')
            return
        self.bookmark_items.append(Bookmark(type='note', path=path, fragment=fragment))
        self.persist_bookmarks()
        self.refresh_bookmark_list()
        self.status.set_text(f'Bookmarked · {path}')

    def remove_note_bookmark(self, path, fragment=''):
        before = len(self.bookmark_items)
        self.bookmark_items = [item for item in self.bookmark_items
                               if not (item.type == 'note' and item.path == path and item.fragment == fragment)]
        if len(self.bookmark_items) == before:
            self.status.set_text('That note is not bookmarked.')
            return
        self.persist_bookmarks()
        self.refresh_bookmark_list()
        self.status.set_text(f'Removed bookmark · {path}')

    def bookmark_current_search(self, *_):
        if self.vault is None:
            return
        query = self.search_entry.get_text()
        if not query.strip():
            self.status.set_text('Enter a search before bookmarking it.')
            return
        if any(item.type == 'search' and item.query == query for item in self.bookmark_items):
            self.status.set_text('That search is already bookmarked.')
            return
        self.bookmark_items.append(Bookmark(type='search', query=query))
        self.persist_bookmarks()
        self.refresh_bookmark_list()
        self.status.set_text('Bookmarked search')

    def bookmark_selected_folder(self, *_):
        if self.vault is None:
            return
        row = self.files.get_selected_row()
        if row is None or not row.path.endswith('/'):
            self.status.set_text('Select a folder to bookmark it.')
            return
        path = row.path.rstrip('/')
        if any(item.type == 'folder' and item.path == path for item in self.bookmark_items):
            self.status.set_text('That folder is already bookmarked.')
            return
        self.bookmark_items.append(Bookmark(type='folder', path=path))
        self.persist_bookmarks()
        self.refresh_bookmark_list()
        self.status.set_text(f'Bookmarked folder · {path}')

    def bookmark_current_graph(self, *_):
        page = self.active_page()
        if not isinstance(page, GraphView):
            self.status.set_text('Open a graph tab to bookmark it.')
            return
        state = page.snapshot()
        state.pop('id', None)
        self.bookmark_items.append(Bookmark(type='graph', state=state))
        self.persist_bookmarks()
        self.refresh_bookmark_list()
        self.status.set_text('Bookmarked graph')

    def show_bookmarks(self, *_):
        if self.vault is None:
            return
        if self.bookmark_manager is None:
            self.bookmark_manager = BookmarkManager(self)
            self.bookmark_manager.connect('destroy', lambda *_: setattr(self, 'bookmark_manager', None))
        else:
            self.bookmark_manager.present()

    def rename_bookmark(self, index, title):
        if not 0 <= index < len(self.bookmark_items):
            return
        if type(title) is not str or '\x00' in title or len(title.strip()) > 200:
            self.status.set_text('Enter a shorter bookmark title.')
            return
        self.bookmark_items[index] = replace(self.bookmark_items[index], title=title.strip())
        self.persist_bookmarks()
        self.refresh_bookmark_list(select=index)

    def move_bookmark(self, index, delta):
        target = index + delta
        if not 0 <= index < len(self.bookmark_items) or not 0 <= target < len(self.bookmark_items):
            return
        item = self.bookmark_items.pop(index)
        self.bookmark_items.insert(target, item)
        self.persist_bookmarks()
        self.refresh_bookmark_list(select=target)

    def remove_bookmark_at(self, index):
        if not 0 <= index < len(self.bookmark_items):
            return
        removed = self.bookmark_items.pop(index)
        self.persist_bookmarks()
        self.refresh_bookmark_list(select=index)
        self.status.set_text(f'Removed bookmark · {removed.display_title()}')

    def saved_workspace_names(self):
        if self.vault is None:
            return []
        return self.saved_workspaces.names(self.vault.root)

    def save_workspace_layout(self, *_):
        if self.vault is None:
            return
        name = ask_text(self, 'Save workspace layout', 'Name this layout', accept='Save')
        if name is None:
            return
        if not name:
            self.status.set_text('Enter a workspace name.')
            return
        if name in self.saved_workspaces.names(self.vault.root):
            if not confirm(self, f'Replace the saved layout “{name}”?', 'Replace'):
                return
        self.save_named_workspace(name)

    def save_named_workspace(self, name):
        if self.vault is None:
            return False
        try:
            self.saved_workspaces.save(self.vault.root, name, self.capture_workspace_state())
        except (OSError, ValueError) as error:
            self.status.set_text(str(error))
            return False
        self.status.set_text(f'Saved workspace · {name.strip()}')
        if self.workspace_manager is not None:
            self.workspace_manager.reload()
        return True

    def load_workspace_layout(self, *_):
        self.manage_workspaces()

    def manage_workspaces(self, *_):
        if self.vault is None:
            return
        if self.workspace_manager is None:
            self.workspace_manager = WorkspaceManager(self)
            self.workspace_manager.connect('destroy', lambda *_: setattr(self, 'workspace_manager', None))
        else:
            self.workspace_manager.present()

    def load_named_workspace(self, name):
        if self.vault is None:
            return False
        state = self.saved_workspaces.get(self.vault.root, name)
        if not isinstance(state, dict) or not isinstance(state.get('tabs'), list):
            self.status.set_text('That workspace layout could not be read.')
            return False
        if not self.confirm_panes(self.editors()):
            return False
        self.restoring_workspace = True
        try:
            self.clear_editors()
            self.navigation_history = NavigationHistory()
            self.collapsed_folders.clear()
            applied = self.restore_workspace(state)
        finally:
            self.restoring_workspace = False
        self.persist_workspace()
        self.sync_bookmark_buttons()
        if applied:
            self.status.set_text(f'Loaded workspace · {name}')
        return bool(applied)

    def delete_named_workspace(self, name):
        if self.vault is None:
            return False
        try:
            removed = self.saved_workspaces.delete(self.vault.root, name)
        except OSError as error:
            self.status.set_text(f'Could not delete workspace: {error}')
            return False
        if removed:
            self.status.set_text(f'Deleted workspace · {name}')
        if self.workspace_manager is not None:
            self.workspace_manager.reload()
        return removed

    def remap_navigation(self, mapping, deleted=()):
        self.navigation_history.remap(mapping, deleted)
        if self.vault is not None:
            try:
                self.bookmark_items = self.bookmark_store.remap(self.vault.root, mapping, deleted)
            except OSError as error:
                self.status.set_text(f'Could not update bookmarks: {error}')
            else:
                self.refresh_bookmark_list()
        if isinstance(self.split_pane, NotePane) and self.split_pane.note.path in deleted:
            self.close_split()
        elif isinstance(self.split_pane, NotePane):
            self.open_split(self.split_pane)
        self.persist_workspace()

    def capture_workspace_state(self):
        pages = []
        for index in range(self.tabs.get_n_pages()):
            pane = self.tabs.get_nth_page(index)
            if isinstance(pane, (GraphView, BasesView, CanvasView)):
                pages.append(pane.snapshot())
            elif isinstance(pane, NotePane):
                pages.append({'path': pane.note.path,
                              'cursor': pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset(),
                              'scroll': pane.scroll.get_vadjustment().get_value()})
        index = self.tabs.get_current_page()
        active = self.tabs.get_nth_page(index) if index >= 0 else None
        if isinstance(active, GraphView):
            active_key = {'graph': active.workspace_id}
        elif isinstance(active, BasesView):
            active_key = {'bases': active.workspace_id}
        elif isinstance(active, CanvasView):
            active_key = {'canvas': active.path}
        elif isinstance(active, NotePane):
            active_key = active.note.path
        else:
            active_key = None
        if isinstance(self.split_pane, GraphView):
            split_key = {'graph': self.split_pane.workspace_id, 'state': self.split_view.snapshot()}
        elif isinstance(self.split_pane, NotePane):
            split_key = self.split_pane.note.path
        else:
            split_key = None
        return {'tabs': pages, 'active': active_key,
                'collapsed_folders': sorted(self.collapsed_folders),
                'split': split_key,
                'search': self.search_entry.get_text()}

    def persist_workspace(self):
        if self.vault is None or self.restoring_workspace:
            return True
        try:
            self.workspace_store.save(self.vault.root, self.capture_workspace_state())
        except OSError as error:
            self.status.set_text(f'Could not save workspace: {error}')
        return True

    def restore_workspace(self, state=None):
        if state is None:
            state = self.workspace_store.load(self.vault.root)
        pages = state.get('tabs', [])
        if 'tabs' not in state or not isinstance(pages, list):
            return False
        own_flag = not self.restoring_workspace
        if own_flag:
            self.restoring_workspace = True
        try:
            graphs, bases, canvases = {}, {}, {}
            for saved in pages:
                if isinstance(saved, dict) and saved.get('kind') == 'graph':
                    graph = self.show_graph_tab(state=saved)
                    graphs[graph.workspace_id] = graph
                    continue
                if isinstance(saved, dict) and saved.get('kind') == 'bases':
                    view = self.show_bases(state=saved)
                    if view is not None:
                        bases[view.workspace_id] = view
                    continue
                if isinstance(saved, dict) and saved.get('kind') == 'canvas':
                    path = saved.get('path')
                    if isinstance(path, str) and isinstance(self.vault.canvases.get(path), str):
                        canvas = self.open_canvas(path)
                        if canvas is not None:
                            canvases[path] = canvas
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
            if isinstance(active, dict) and isinstance(active.get('bases'), str) and active['bases'] in bases:
                self.tabs.set_current_page(self.tabs.page_num(bases[active['bases']]))
            if isinstance(active, dict) and isinstance(active.get('canvas'), str) and active['canvas'] in canvases:
                self.tabs.set_current_page(self.tabs.page_num(canvases[active['canvas']]))
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
            search = state.get('search')
            if isinstance(search, str):
                self.search_entry.set_text(search)
        finally:
            if own_flag:
                self.restoring_workspace = False
        self.populate_files()
        pane = self.current()
        if pane:
            self.navigation_history.visit(Location(pane.note.path,
                pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()))
        self.sync_bookmark_buttons()
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
        return not self.confirm_panes(self.editors())


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
