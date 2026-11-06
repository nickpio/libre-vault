"""Native GTK notebook prototype. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import difflib
import posixpath
from pathlib import Path
import re
from time import monotonic
from urllib.parse import unquote
import warnings
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, Pango

from .anchors import find_anchor_offset
from .vault import Conflict, VaultError, links
from .mentions import link_mention
from .monitor import VaultMonitor
from .editing import TextEdit
from .vim import mode_label
from .graph_ui import GraphView
from .bases_ui import BasesView
from .canvas_ui import CanvasView
from .converter import ConverterDialog
from .slides import SlidesWindow
from .filetree import tree_paths
from .metadata import note_metadata, property_text, with_properties
from .commands import CommandPalette, KeybindCheatsheet, NoteNameDialog, TemplatePicker, UniqueNoteDialog
from .shortcuts import APP_SHORTCUTS, HotkeyStore, effective_shortcuts, validate_hotkey, shortcut_key, shortcut_id
from .settings import SettingsDialog
from .preferences import PreferencesStore
from .plugins import PluginManager
from .appearance import stylesheet
from .gtk_runtime import install_window_header
from .panels import SIDEBAR_DEFAULT_RATIO, SIDEBAR_MAX, SIDEBAR_OPEN_LIMIT, SlidingPanel, sidebar_widths
from .tabs import WorkspaceTabs
from .navigation import Location, NavigationHistory
from .bookmarks import (BOOKMARK_ICONS, Bookmark, BookmarkManager, BookmarkStore, WorkspaceManager,
                        ask_text, confirm, graph_open_state, with_moved_root)
from .editor_ui import NotePane, line_hits, math_pixbuf, selection_overlaps
from .vault_lifecycle import VaultLifecycleMixin
from .worker import Snapshot, VaultWorker
from .widgets import count_summary, scrolled, text_buffer
from .workspace_ui import WorkspaceStateMixin
from .workspace import LastVaultStore, SavedWorkspacesStore, WorkspaceStore
from .templates import (
    create_note_from_template, create_unique_note, ensure_daily_note,
    expand_template, random_note_path, read_template, template_paths,
)


CSS = Path(__file__).with_name('style.css').read_bytes()


def format_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime('%Y-%m-%d %H:%M:%S')


def file_tree_icon(folder):
    """Draw compact outline icons that follow the current theme's text color."""
    icon = Gtk.DrawingArea()
    icon.set_size_request(16, 16)
    icon.get_style_context().add_class('muted')

    def draw(widget, context):
        color = widget.get_style_context().get_color(widget.get_state_flags())
        Gdk.cairo_set_source_rgba(context, color)
        context.set_line_width(1.2)
        if folder:
            context.move_to(1.8, 4.4)
            context.line_to(1.8, 12.5)
            context.line_to(14.2, 12.5)
            context.line_to(14.2, 5.7)
            context.line_to(7.3, 5.7)
            context.line_to(6.0, 4.4)
            context.close_path()
        else:
            context.move_to(3.6, 1.8)
            context.line_to(9.2, 1.8)
            context.line_to(12.4, 5.0)
            context.line_to(12.4, 14.2)
            context.line_to(3.6, 14.2)
            context.close_path()
            context.move_to(9.2, 1.8)
            context.line_to(9.2, 5.0)
            context.line_to(12.4, 5.0)
            context.move_to(5.6, 8.0)
            context.line_to(10.4, 8.0)
            context.move_to(5.6, 10.5)
            context.line_to(10.4, 10.5)
        context.stroke()
        return False

    icon.connect('draw', draw)
    return icon


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


class Window(VaultLifecycleMixin, WorkspaceStateMixin, Gtk.ApplicationWindow):
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
        self.plugins = PluginManager(self, self.preferences_store.path.parent / 'plugins')
        self.hotkey_store = HotkeyStore(self.preferences_store.path)
        self.hotkeys = self.hotkey_store.load()
        from .vault_options import VaultOptionsStore
        self.vault_options_store = VaultOptionsStore(self.preferences_store.path)
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
        self.note_windows = set()
        self.closed = False
        self.worker = VaultWorker(GLib.idle_add)
        self.loading_vault = False
        self.refresh_paths = set()
        self.file_render_source = None
        self.pending_file_selection = None
        self.file_row_limit = 200
        self.file_query = None
        self.sliding_views = []
        self.tab_mode = 'tabs'
        self.workspace_source = None
        self.scroll_restore_sources = {}
        self.quick_switcher = None
        self.vault_monitor = None
        self.reading_source = None
        self.panel_widths_initialized = False
        self.sidebar_span = 0
        self.sidebar_refit = False
        self.sidebar_custom = False
        self._fitting_sidebars = False
        self._sidebar_fit_source = None
        self._sidebar_ratios_source = None
        self._sidebar_dragging = False
        self.notes_ratio = SIDEBAR_DEFAULT_RATIO
        self.details_ratio = SIDEBAR_DEFAULT_RATIO
        self.connect('delete-event', self.on_delete)
        self.connect('key-press-event', self.key_press)
        self.css_provider = Gtk.CssProvider()
        self.brand = Gtk.Image()
        self.brand.get_style_context().add_class('brand')
        self.brand.get_accessible().set_name('Libre Vault')
        self.brand.set_tooltip_text('Libre Vault')
        self.apply_preferences()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        header = Gtk.HeaderBar()
        header.pack_start(self.brand)
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
        install_window_header(self, header, root)
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
        description = Gtk.Label(label='Open a vault or an empty folder to begin.')
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
        # Let the file tree become narrower than the entry's default width.
        self.search_entry.set_width_chars(8)
        self.search_entry.connect('changed', lambda *_: self.populate_files())
        sidebar.pack_start(self.search_entry, False, False, 0)
        self.bookmarks_heading = Gtk.Label(label='Bookmarks', xalign=0)
        self.bookmarks_heading.get_style_context().add_class('caption')
        sidebar.pack_start(self.bookmarks_heading, False, False, 0)
        self.bookmarks_empty = Gtk.Label(label='No bookmarks yet', xalign=0)
        self.bookmarks_empty.set_ellipsize(Pango.EllipsizeMode.END)
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
        from .tags_ui import TagPane
        self.tag_pane = TagPane(self)
        sidebar.pack_start(self.tag_pane, False, False, 0)
        self.files_heading = Gtk.Label(label='Files', xalign=0)
        self.files_heading.get_style_context().add_class('caption')
        sidebar.pack_start(self.files_heading, False, False, 0)
        self.file_views = Gtk.Stack()
        self.file_lists = {}
        for name in ('tree', 'search'):
            file_list = Gtk.ListBox()
            file_list.get_style_context().add_class('note-list')
            file_list.set_activate_on_single_click(True)
            file_list.connect('row-activated', self.activate_file)
            file_list.connect('key-press-event', self.files_key_press)
            file_list.connect('button-press-event', self.explorer_button_press)
            file_list.connect('popup-menu', self.explorer_popup_menu)
            files_scroll = scrolled(file_list)
            files_scroll.connect('button-press-event', self.explorer_button_press)
            self.file_lists[name] = file_list
            self.file_views.add_named(files_scroll, name)
        self.files = self.file_lists['tree']
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        empty.set_valign(Gtk.Align.START)
        self.search_empty_label = Gtk.Label(xalign=0)
        self.search_empty_label.set_line_wrap(True)
        self.search_empty_label.set_max_width_chars(28)
        empty.pack_start(self.search_empty_label, False, False, 0)
        clear_search = Gtk.Button(label='Clear search')
        clear_search.set_halign(Gtk.Align.START)
        clear_search.connect('clicked', lambda *_: self.search_entry.set_text(''))
        empty.pack_start(clear_search, False, False, 0)
        self.file_views.add_named(empty, 'empty')
        sidebar.pack_start(self.file_views, True, True, 0)
        self.more_files = Gtk.Button(label='Show more files')
        self.more_files.set_no_show_all(True)
        self.more_files.connect('clicked', self.show_more_files)
        sidebar.pack_start(self.more_files, False, False, 0)
        hint = self.keybind_hint = Gtk.Button()
        hint.get_style_context().add_class('sidebar-command')
        hint.set_margin_top(12)
        hint.get_accessible().set_name('Open keybind cheatsheet')
        hint.connect('clicked', lambda *_: self.show_keybinds())
        hint_content = Gtk.Box(spacing=8)
        icon = Gtk.Image.new_from_icon_name('input-keyboard-symbolic', Gtk.IconSize.MENU)
        icon.get_style_context().add_class('muted')
        hint_content.pack_start(icon, False, False, 0)
        label = Gtk.Label(label='Keyboard shortcuts', xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.get_style_context().add_class('sidebar-command-label')
        hint_content.pack_start(label, True, True, 0)
        keys = self.keybind_hint_keys = Gtk.Label()
        keys.get_style_context().add_class('keybind-keys')
        hint_content.pack_end(keys, False, False, 0)
        hint.add(hint_content)
        self.update_keybind_hint()
        sidebar.pack_start(hint, False, False, 0)
        self.left_dock = Gtk.Notebook(scrollable=True)
        self.right_dock = Gtk.Notebook(scrollable=True)
        for dock in (self.left_dock, self.right_dock):
            dock.get_style_context().add_class('sidebar-dock')
            dock.set_group_name('librevault-panels-' + str(id(self)))
        sidebar.remove(self.tag_pane)
        self.tag_pane.set_expanded(True)
        for dock, child, title in [(self.left_dock, sidebar, 'Files'), (self.left_dock, self.tag_pane, 'Tags')]:
            child.dock_id = title.lower()
            dock.append_page(child, Gtk.Label(label=title))
            dock.set_tab_reorderable(child, True)
            dock.set_tab_detachable(child, True)
        self.notes_panel = SlidingPanel(self.left_dock, layout, at_start=True)
        layout.pack1(self.notes_panel, False, False)
        self.notes_toggle.connect('toggled', self.set_panel_open, self.notes_panel)
        center = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        center.get_style_context().add_class('workspace-split')
        self.tabs = WorkspaceTabs()
        self.tabs.connect('switch-page', self.tab_switched)
        self.tabs.connect('page-removed', self.tab_removed)
        self.tabs.connect('page-reordered', lambda *_: self.persist_workspace())
        editor_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.main_stack = Gtk.Stack()
        self.main_stack.add_named(self.tabs, 'tabs')
        self.sliding_strip = Gtk.Box(spacing=8)
        sliding_scroll = Gtk.ScrolledWindow()
        sliding_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        sliding_scroll.add(self.sliding_strip)
        self.main_stack.add_named(sliding_scroll, 'sliding')
        editor_column.pack_start(self.main_stack, True, True, 0)
        self.tabs.connect('page-added', self.rebuild_sliding_tabs)
        self.tabs.connect('page-reordered', self.rebuild_sliding_tabs)
        from .layout_ui import WorkspaceLayout
        self.workspace_layout = WorkspaceLayout(self, editor_column)
        self.editor_split = self.workspace_layout
        center.pack1(self.workspace_layout, True, False)
        self.context = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.context.get_style_context().add_class('sidebar')
        self.context.get_style_context().add_class('details-content')
        self.details_scroll = scrolled(self.context)
        self.details_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.details_view = 'outline'
        self.details_buttons = {}
        details = self.details_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        selector = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE)
        selector.set_min_children_per_line(1)
        selector.set_max_children_per_line(3)
        selector.set_column_spacing(6)
        selector.set_row_spacing(6)
        selector.set_margin_start(8)
        selector.set_margin_end(8)
        selector.set_margin_top(6)
        selector.set_margin_bottom(4)
        group = None
        for name in ('outline', 'links', 'properties'):
            button = Gtk.RadioButton.new_with_label_from_widget(group, name.capitalize())
            button.set_mode(False)
            button.get_style_context().add_class('details-view-button')
            if group is None:
                group = button
            self.details_buttons[name] = button
            selector.add(button)
            button.connect('toggled', self.select_details_view, name)
        details.pack_start(selector, False, False, 0)
        details.pack_start(self.details_scroll, True, True, 0)
        details.dock_id = 'details'
        self.right_dock.append_page(details, Gtk.Label(label='Details'))
        self.right_dock.set_tab_reorderable(details, True)
        self.right_dock.set_tab_detachable(details, True)
        self.details_panel = SlidingPanel(self.right_dock, center, at_start=False)
        center.pack2(self.details_panel, False, False)
        self.details_toggle.connect('toggled', self.set_panel_open, self.details_panel)
        layout.pack2(center, True, False)
        layout.connect('size-allocate', self.allocate_sidebars)
        for paned in (layout, center):
            paned.connect('button-press-event', self._sidebar_drag_begin)
            paned.connect('button-release-event', self._sidebar_drag_end)
        self.status = Gtk.Label(label='Open a vault folder to begin.', xalign=0)
        self.status.get_style_context().add_class('status')
        statusbar = Gtk.Box()
        statusbar.get_style_context().add_class('statusbar')
        self.settings_button = self.icon_button('emblem-system-symbolic', 'Settings', self.show_settings)
        self.settings_button.get_style_context().add_class('settings-button')
        statusbar.pack_start(self.settings_button, False, False, 0)
        self.status.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        statusbar.pack_start(self.status, True, True, 0)
        self.work_status = Gtk.Label(xalign=0)
        self.work_status.get_style_context().add_class('status')
        self.work_status.get_accessible().set_name('Background work progress')
        statusbar.pack_start(self.work_status, False, False, 0)
        self.work_cancel = Gtk.Button(label='Cancel work')
        self.work_cancel.set_no_show_all(True)
        self.work_cancel.connect('clicked', lambda *_: self.cancel_work(user=True))
        statusbar.pack_start(self.work_cancel, False, False, 0)
        self.compare_button = Gtk.Button(label='Compare versions…')
        self.compare_button.set_no_show_all(True)
        self.compare_button.connect('clicked', self.compare_current_conflict)
        statusbar.pack_start(self.compare_button, False, False, 0)
        self.save_status = Gtk.Label(xalign=1)
        self.save_status.set_width_chars(13)
        self.save_status.get_style_context().add_class('status')
        self.save_status.get_accessible().set_name('Note save status')
        statusbar.pack_start(self.save_status, False, False, 0)
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
        self.plugins.set_enabled(self.preferences.python_plugins)

    def icon_button(self, icon, tooltip, action):
        button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
        button.get_style_context().add_class('icon-button')
        button.set_tooltip_text(tooltip)
        button.get_accessible().set_name(tooltip.split(' · ')[0])
        button.connect('clicked', action)
        return button

    def cleanup(self, *_):
        self.closed = True
        self.plugins.set_enabled(False)
        self.worker.close()
        self.cancel_file_render()
        for name in ('_sidebar_fit_source', '_sidebar_ratios_source'):
            source = getattr(self, name)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, name, None)
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









    def allocate_sidebars(self, _layout, allocation):
        # The workspace can start hidden behind the welcome screen. Wait for
        # its real allocation before positioning the dividers.
        width = allocation.width
        if self.closed or self.vault is None or width <= 1 or self._fitting_sidebars:
            return
        if self.notes_panel.transitioning or self.details_panel.transitioning:
            # Remember a resize that arrives mid-slide, but let the animation
            # keep a dragged width when the window itself did not change.
            if self.panel_widths_initialized and abs(width - self.sidebar_span) > 1:
                self.sidebar_refit = True
            return
        unchanged = self.panel_widths_initialized and abs(width - self.sidebar_span) <= 1
        if unchanged and not self.sidebar_refit:
            return
        self.sidebar_refit = False
        self.fit_sidebars(width)

    def fit_sidebars(self, width=None):
        """Place open sidebars from their share of the workspace width."""
        if width is None:
            width = self.notes_panel.paned.get_allocated_width()
        if width <= 1:
            return
        limit = SIDEBAR_MAX if self.sidebar_custom else SIDEBAR_OPEN_LIMIT
        # Measure both as open so a hidden sidebar reopens at the adapted width.
        left, right = sidebar_widths(width, self.notes_ratio, self.details_ratio, limit=limit)
        if self._sidebar_fit_source is not None:
            GLib.source_remove(self._sidebar_fit_source)
            self._sidebar_fit_source = None
        self._fitting_sidebars = True
        self.sidebar_span = width
        self.panel_widths_initialized = True
        self._place_sidebar(self.notes_panel, left)
        # The details divider sits in the center pane, whose width changes
        # after the file tree moves. Apply it once that allocation settles.
        self._sidebar_fit_source = GLib.idle_add(self._finish_sidebar_fit, right)

    def _place_sidebar(self, panel, width):
        if width < 1:
            return
        if panel.get_visible() and panel.get_child_revealed():
            panel.position_width(width)
        elif not panel.get_visible():
            panel.get_child().set_size_request(width, -1)

    def _finish_sidebar_fit(self, right):
        self._sidebar_fit_source = None
        self._place_sidebar(self.details_panel, right)
        self._sidebar_fit_source = GLib.idle_add(self._release_sidebar_fit)
        return False

    def _release_sidebar_fit(self):
        self._sidebar_fit_source = None
        self._fitting_sidebars = False
        if self.sidebar_refit and not (self.notes_panel.transitioning or self.details_panel.transitioning):
            self.sidebar_refit = False
            self.fit_sidebars()
        return False

    def _sidebar_drag_begin(self, _paned, event):
        if event.button == 1:
            self._sidebar_dragging = True
        return False

    def _sidebar_drag_end(self, _paned, event):
        if event.button == 1 and self._sidebar_dragging:
            self._sidebar_dragging = False
            if self._sidebar_ratios_source is not None:
                GLib.source_remove(self._sidebar_ratios_source)
            self._sidebar_ratios_source = GLib.idle_add(self._store_sidebar_ratios)
        return False

    def _store_sidebar_ratios(self):
        self._sidebar_ratios_source = None
        if self._fitting_sidebars:
            return False
        workspace = self.notes_panel.paned.get_allocated_width()
        if workspace <= 1:
            return False
        changed = False
        if self.notes_panel.get_child_revealed():
            changed = self._remember_ratio('notes_ratio', self.notes_panel.get_allocated_width(), workspace) or changed
        if self.details_panel.get_child_revealed():
            changed = self._remember_ratio('details_ratio', self.details_panel.get_allocated_width(), workspace) or changed
        if changed:
            self.sidebar_custom = True
        return False

    def _remember_ratio(self, name, width, workspace):
        ratio = width / workspace
        if abs(ratio - getattr(self, name)) <= 1 / workspace:
            return False
        setattr(self, name, ratio)
        return True


    def cancel_file_render(self):
        if self.file_render_source is not None:
            GLib.source_remove(self.file_render_source)
            self.file_render_source = None

    def cancel_work(self, *, user=False):
        self.cancel_file_render()
        self.worker.cancel_all()
        self.tag_pane.signature = None
        self.loading_vault = False
        self.refresh_paths.clear()
        self.work_cancel.hide()
        self.work_status.set_text('Cancelled' if user else '')
        if user and self.search_entry.get_text().strip():
            self.files_heading.set_text('Search cancelled')
            self.search_empty_label.set_text('Search cancelled. Edit the query to search again.')
            self.file_views.set_visible_child_name('empty')
            self.more_files.hide()

    def vault_job(self, channel, operation, done):
        """Capture UI-owned state before dispatch; never pass widgets to a job."""
        vault = self.vault
        if vault is None or self.closed:
            return
        changes = vault.index.connection.total_changes
        snapshot = Snapshot.of(vault)

        def completed(value):
            if self.vault is not vault:
                return
            if changes != vault.index.connection.total_changes:
                # A foreground save/create/move happened while the job ran.
                if channel == 'refresh':
                    self.refresh()
                else:
                    self.vault_job(channel, operation, done)
                return
            self.work_cancel.set_visible(bool(self.worker.requests))
            if not self.worker.requests:
                self.work_status.set_text('')
            done(value)

        def failed(error):
            self.work_cancel.set_visible(bool(self.worker.requests))
            if not self.worker.requests:
                self.work_status.set_text('')
            self.status.set_text(str(error))

        self.work_cancel.show()
        self.worker.submit(channel, vault.root, vault.options, operation, completed,
                           failed,
                           lambda count: self.work_status.set_text(f'{channel.capitalize()} · {count} files…'),
                           snapshot=snapshot)

    def show_more_files(self, *_):
        self.file_row_limit += 200
        self.populate_files()

    def populate_files(self):
        from .metadata import query_terms
        self.cancel_file_render()
        query = self.search_entry.get_text()
        if query != self.file_query:
            self.file_query = query
            self.file_row_limit = 200
        error = query_terms(query).error
        self.search_entry.set_tooltip_text('Invalid search: ' + error if error else 'Search saved notes')
        if hasattr(self, 'tag_pane'):
            self.tag_pane.refresh()
        self.worker.cancel('search')
        if self.vault and query.strip() and not error:
            self.more_files.hide()
            self.files_heading.set_text('Searching…')
            # Clear old results immediately so they cannot be mistaken for this query.
            self.file_views.set_visible_child_name('empty')
            self.search_empty_label.set_text('Searching…')
            self.vault_job('search', lambda vault: vault.search_results(query),
                           lambda results: self._populate_files({result.path: result for result in results}))
            return
        if self.vault and not query.strip():
            collapsed = set(self.collapsed_folders)
            self.vault_job('search', lambda vault: tree_paths(
                [*vault.notes, *vault.canvases], vault.folders, collapsed),
                lambda paths: self._populate_files({}, paths))
        else:
            self._populate_files({})

    def _populate_files(self, results, tree=None):
        searching = bool(self.search_entry.get_text().strip())
        paths = list(results) if tree is None else tree
        self.files = self.file_lists['search' if searching else 'tree']
        if searching:
            count = len(paths)
            self.files_heading.set_text(f'Search results ({count})')
            self.search_empty_label.set_text(f'No notes match ‘{self.search_entry.get_text().strip()}’')
            self.file_views.set_visible_child_name('search' if count else 'empty')
        else:
            self.files_heading.set_text('Files')
            self.file_views.set_visible_child_name('tree')
        total = len(paths)
        self.more_files.set_visible(total > self.file_row_limit)
        self.more_files.set_label(f'Show 200 more · {min(total, self.file_row_limit)} of {total}')
        paths = paths[:self.file_row_limit]
        rows = self.files.get_children()
        if [row.path for row in rows] == paths and all(
                row.search_result == searching and
                (not searching or getattr(row, 'search_signature', None) == results[row.path]) and
                (not row.path.endswith('/') or row.expanded == (row.path[:-1] not in self.collapsed_folders))
                for row in rows):
            if self.pending_file_selection:
                for row in rows:
                    if row.path == self.pending_file_selection:
                        self.files.select_row(row)
                        break
                self.pending_file_selection = None
            return
        self.cancel_file_render()
        # GTK row construction also needs bounded slices for large result sets.
        file_list = self.files
        def rows_in_batches():
            for row in rows:
                row.destroy()
                yield
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
                if path.lower().endswith('.canvas') and not is_folder:
                    icon = Gtk.Image.new_from_icon_name('x-office-drawing-symbolic', Gtk.IconSize.MENU)
                    icon.get_style_context().add_class('muted')
                else:
                    icon = file_tree_icon(is_folder)
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
                file_list.add(row)
                row.show_all()
                yield
            pane = self.current()
            selected_path = self.pending_file_selection or (pane.note.path if pane else None)
            self.pending_file_selection = None
            if selected_path is None:
                page = self.active_page()
                if isinstance(page, CanvasView):
                    selected_path = page.path
            if selected_path:
                for row in file_list.get_children():
                    if row.path == selected_path:
                        file_list.select_row(row)
                        break
            file_list.show_all()

        pending = rows_in_batches()
        def render_batch():
            deadline = monotonic() + .006
            while monotonic() < deadline:
                try:
                    next(pending)
                except StopIteration:
                    self.file_render_source = None
                    return False
            return True
        self.file_render_source = GLib.idle_add(render_batch, priority=GLib.PRIORITY_LOW)

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
        self.pending_file_selection = path
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
        from .properties_ui import PropertyForm
        metadata = note_metadata(pane.text())
        notebook = Gtk.Notebook()
        form = PropertyForm(metadata.properties, self.vault.root)
        notebook.append_page(scrolled(form), Gtk.Label(label='Properties'))
        editor = Gtk.TextView(buffer=text_buffer(property_text(pane.text())))
        editor.set_monospace(True)
        notebook.append_page(scrolled(editor), Gtk.Label(label='YAML'))
        # Synchronize both views when switching, preserving raw YAML until the
        # structured form is actually edited and applied.
        def switch(_, page, index):
            try:
                if index == 1:
                    editor.get_buffer().set_text(form.source())
                else:
                    from .metadata import property_mapping
                    values = property_mapping(editor.get_buffer().get_text(*editor.get_buffer().get_bounds(), True))
                    for record in list(form.rows):
                        form.rows.remove(record)
                        record[0].destroy()
                    for key, value in values.items():
                        form.add_property(key, value)
                problem.set_text('')
            except ValueError as error:
                problem.set_text(str(error))
                notebook.handler_block_by_func(switch)
                notebook.set_current_page(1 if index == 0 else 0)
                notebook.handler_unblock_by_func(switch)
        content.pack_start(notebook, True, True, 0)
        problem = Gtk.Label(xalign=0)
        problem.set_line_wrap(True)
        content.pack_start(problem, False, False, 0)
        dialog.show_all()
        if metadata.error:
            notebook.set_current_page(1)
        notebook.connect_after('switch-page', switch)
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                source = (form.source() if notebook.get_current_page() == 0 else
                          editor.get_buffer().get_text(*editor.get_buffer().get_bounds(), True))
                self.set_note_properties(source)
                if notebook.get_current_page() == 0:
                    form.save_registry()
            except (ValueError, OSError) as error:
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
        mirror = next((view for view in self.sliding_views if view.canonical is pane), None) if self.tab_mode == 'sliding' else None
        (mirror.editor if mirror is not None else pane.editor).grab_focus()
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
        group = self.workspace_layout.focused(focus)
        if group and group.current():
            return group.current().canonical
        for view in self.sliding_views:
            if focus is view or focus and focus.is_ancestor(view):
                return view.canonical
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
            try:
                pane.save()
            except Conflict:
                self.compare_current_conflict()
            except (VaultError, OSError):
                # The save indicator keeps the error visible with retry details.
                pass

    def compare_current_conflict(self, *_):
        from .conflicts_ui import compare_conflict
        pane = self.current()
        if pane and pane.save_error and pane.save_conflict:
            compare_conflict(pane)

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
                    self.pending_file_selection = created + '/'
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
        label.set_ellipsize(Pango.EllipsizeMode.END)
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

    def recovery_failed(self, error):
        """Receives background recovery failures without touching GTK off-thread."""
        if not self.closed:
            GLib.idle_add(self._show_recovery_error, str(error))

    def _show_recovery_error(self, message):
        if not self.in_destruction():
            self.status.set_text(f'Could not write recovery draft: {message}')
        return False

    def update_save_status(self):
        if self.in_destruction():
            return
        pane = self.current()
        self.compare_button.set_visible(bool(pane and pane.save_error and pane.save_conflict))
        label = self.save_status
        style = label.get_style_context()
        style.remove_class('save-error')
        if pane is None:
            label.set_text('')
            label.set_tooltip_text(None)
        elif pane.save_error is not None:
            label.set_text('Couldn’t save')
            hint = 'Compare versions to resolve this conflict, or press Ctrl+S.' if pane.save_conflict else 'Retry with Ctrl+S.'
            label.set_tooltip_text(f'{pane.note.path}: {pane.save_error}\n{hint}')
            style.add_class('save-error')
        else:
            label.set_text('Saving…' if pane.dirty else 'Saved')
            label.set_tooltip_text('Changes save automatically after you stop typing.'
                                   if pane.dirty else f'All changes saved · {pane.note.path}')

    def update_vim_status(self):
        label = getattr(self, 'vim_status', None)
        if label is None or self.in_destruction():
            return
        pane = self.current()
        if self.preferences.vim_mode and isinstance(pane, NotePane) and pane.vim.active:
            label.set_text(mode_label(pane.vim.session))
        else:
            label.set_text('')

    def select_details_view(self, button, name):
        if not button.get_active() or self.details_view == name:
            return
        self.details_view = name
        self.details_scroll.get_vadjustment().set_value(0)
        self.update_context()
        self.persist_workspace()

    def update_context(self):
        if self.closed:
            return False
        self.worker.cancel('links')
        self.update_vim_status()
        self.update_save_status()
        if self.vault:
            self.tag_pane.refresh()
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
            self.section('Open a note to see its details.')
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
        if self.details_view == 'outline':
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
                empty.set_ellipsize(Pango.EllipsizeMode.END)
                empty.get_style_context().add_class('outline-empty')
                self.context.pack_start(empty, False, False, 0)
            self.update_outline_selection()
            self.context_button('Version history…', self.show_version_history)
        elif self.details_view == 'links':
            self.section('Outgoing links')
            for link in links(pane.text()):
                self.context_button(('↳ ' if link.embedded else '') + link.label,
                                    lambda target=link.target: self.follow_link(pane.note.path, target))
            path = pane.note.path
            texts = {name: item.text() for name, item in self.panes.items()}
            def scan(vault):
                return vault.backlinks(path), vault.unlinked_mentions(path, texts)
            self.vault_job('links', scan, lambda result: self._show_links(pane, result, texts))
        else:
            self.section('Properties')
            metadata = note_metadata(pane.text())
            self.context_button('Edit properties…', self.edit_properties)
            if metadata.error:
                problem = Gtk.Label(label=metadata.error, xalign=0)
                problem.set_line_wrap(True)
                problem.set_max_width_chars(24)
                self.context.pack_start(problem, False, False, 0)
            elif not metadata.properties:
                empty = Gtk.Label(label='No properties in this note.', xalign=0)
                empty.get_style_context().add_class('outline-empty')
                self.context.pack_start(empty, False, False, 0)
            else:
                from .bases import display_value
                for key, value in metadata.properties.items():
                    name = Gtk.Label(label=str(key), xalign=0)
                    name.get_style_context().add_class('caption')
                    name.set_line_wrap(True)
                    self.context.pack_start(name, False, False, 0)
                    text = display_value(value) or ('null' if value is None else 'Empty')
                    label = Gtk.Label(label=text, xalign=0)
                    label.set_line_wrap(True)
                    label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                    label.set_selectable(True)
                    label.set_max_width_chars(28)
                    self.context.pack_start(label, False, False, 0)
            if metadata.tags:
                self.section('Tags')
                for tag in metadata.tags:
                    self.context_button('#' + tag, lambda tag=tag: self.search_entry.set_text('tag:#' + tag))
        self.context.show_all()
        self.update_counts()
        return False

    def _show_links(self, pane, result, texts):
        if self.current() is not pane or self.details_view != 'links':
            return
        if texts != {name: item.text() for name, item in self.panes.items()}:
            self.update_context()
            return
        backlinks, mentions = result
        self.section('Backlinks · Saved files')
        for path in backlinks:
            self.context_button(path.removesuffix('.md'), lambda path=path: self.open_note(path))
        self.section('Unlinked mentions')
        if not mentions:
            empty = Gtk.Label(label='No unlinked mentions.', xalign=0)
            empty.set_ellipsize(Pango.EllipsizeMode.END)
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
        self.context.show_all()

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
        for channel in ('search', 'links', 'tags'):
            self.worker.cancel(channel)
        self.tag_pane.signature = None
        self.refresh_paths.update({''} if paths is None else paths)
        requested = set(self.refresh_paths)
        def scan(vault):
            vault.update_paths(requested)
            return Snapshot.of(vault)
        def completed(snapshot):
            self.refresh_paths.clear()
            snapshot.apply(self.vault)
            if self.vault_monitor:
                self.vault_monitor.watch_tree('')
            self._refresh_views()
        self.vault_job('refresh', scan, completed)
        return True

    def _refresh_views(self):
        selected = self.files.get_selected_row()
        if selected and selected.path.endswith('/'):
            self.pending_file_selection = selected.path
        try:
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
        for shortcut in effective_shortcuts(APP_SHORTCUTS, 'app', self.hotkeys):
            key, mask = Gtk.accelerator_parse(shortcut.accelerator)
            if shortcut_key(event.keyval) == shortcut_key(key) and modifiers == mask:
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
        logo_theme = 'light' if self.preferences.theme == 'light' else 'dark'
        logo_path = Path(__file__).with_name('assets') / f'logo-{logo_theme}.png'
        self.brand.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(logo_path), 40, 40, True))
        for pane in self.panes.values():
            pane.apply_preferences()
        self.reload_shown_fonts()

    def reload_shown_fonts(self):
        """Draw the editor font on widgets that are already on screen.

        Loading a new stylesheet updates the CSS font property, but a realized
        widget keeps its previous Pango font until its style context is invalidated.
        """
        roots = [self, self.settings_dialog, self.slides_window, *tuple(self.note_windows)]
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            for root in roots:
                if root is not None:
                    self._invalidate_fonts(root)

    def _invalidate_fonts(self, widget):
        # GTK 3.24 still needs this deprecated call. Replacing the provider,
        # toggling the CSS class, and reset_style() leave the old face in place.
        widget.get_style_context().invalidate()
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._invalidate_fonts(child)

    def update_keybind_hint(self):
        shortcut = next(item for item in APP_SHORTCUTS if item.handler == 'show_keybinds')
        accelerator = self.hotkeys.get(shortcut_id('app', shortcut), shortcut.accelerator)
        label = Gtk.accelerator_get_label(*Gtk.accelerator_parse(accelerator)) if accelerator else 'Unbound'
        self.keybind_hint_keys.set_text(label)
        self.keybind_hint.set_tooltip_text(f'Open keybind cheatsheet · {label}')

    def set_hotkey(self, identifier, accelerator):
        accelerator = validate_hotkey(self.hotkeys, identifier, accelerator)
        bindings = dict(self.hotkeys, **{identifier: accelerator})
        self.hotkey_store.save(bindings)
        for window in self.get_application().get_windows():
            if isinstance(window, Window):
                window.hotkeys = bindings
                window.update_keybind_hint()
                if window.cheatsheet is not None:
                    window.cheatsheet.destroy()

    def set_vault_options(self, options):
        if self.vault is None:
            return
        folder = options.attachment_folder
        if folder:
            # Validate against reserved paths and the proposed visibility policy.
            previous = self.vault.options
            self.vault.options = options
            try:
                self.vault._visible_path(folder)
            finally:
                self.vault.options = previous
        self.vault_options_store.save(self.vault.root, options)
        if self.vault_monitor:
            self.vault_monitor.close()
        self.vault.options = options
        self.refresh()
        self.vault_monitor = VaultMonitor(self.vault, self.refresh, self.status.set_text)

    def set_preferences(self, preferences):
        self.preferences_store.save(preferences)
        for window in self.get_application().get_windows():
            if isinstance(window, Window):
                window.preferences = preferences
                window.apply_preferences()
                window.plugins.set_enabled(preferences.python_plugins)
                if window.palette is not None:
                    window.palette.destroy()
                if window.settings_dialog is not None:
                    window.settings_dialog.sync_controls()

    def reload_plugins(self, *_):
        for window in self.get_application().get_windows():
            if isinstance(window, Window):
                if window.palette is not None:
                    window.palette.destroy()
                window.plugins.reload()
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
        companion = next((group for group in self.workspace_layout.groups
                          if group.current() and isinstance(group.current().canonical, NotePane)), None)
        beside_graph = self.tabs.page_num(graph) >= 0 and companion is not None
        pane = self.open_note(path)
        if pane and beside_graph:
            previous = companion.current()
            added = companion.add(pane)
            companion.close_page(previous)
            self.tabs.set_current_page(self.tabs.page_num(graph))
            added.editor.grab_focus()
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
        if self.closed:
            return
        if self.tab_mode == 'sliding':
            self.main_stack.set_visible_child_name('sliding' if isinstance(pane, (NotePane, GraphView)) else 'tabs')
        if not isinstance(pane, NotePane):
            GLib.idle_add(self.update_context)
            return
        self.remember_location()
        if not self.restoring_workspace:
            self.navigation_history.visit(Location(pane.note.path,
                pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()))
        GLib.idle_add(self.update_context)

    def tab_removed(self, notebook, pane, index):
        self.workspace_layout.remove_pane(pane)
        for window in tuple(self.note_windows):
            if window.pane is pane:
                window.destroy()
        self.rebuild_sliding_tabs()
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

    def make_mirror(self, pane, state=None):
        from .layout_ui import NoteMirror
        if isinstance(pane, NotePane):
            return NoteMirror(self, pane)
        if isinstance(pane, GraphView):
            return GraphView(self, state=state or pane.snapshot())
        self.status.set_text('This view stays in its own tab.')
        return None

    def close_split(self, *_):
        self.workspace_layout.clear()

    def toggle_split(self, *_):
        if self.workspace_layout.groups:
            self.close_split()
        else:
            self.open_split(self.active_page())

    def open_split(self, pane):
        if not isinstance(pane, (NotePane, GraphView)):
            return
        self.close_split()
        self.workspace_layout.add_split(pane=pane)

    def split_right(self, *_):
        self.workspace_layout.add_split(Gtk.Orientation.HORIZONTAL)

    def split_down(self, *_):
        self.workspace_layout.add_split(Gtk.Orientation.VERTICAL)

    def pop_out_note(self, *_):
        pane = self.current()
        if pane is not None:
            from .layout_ui import NoteWindow
            return NoteWindow(self, pane)

    def set_tab_mode(self, mode):
        self.tab_mode = mode
        self.tabs.set_tab_pos(Gtk.PositionType.LEFT if mode == 'stacked' else Gtk.PositionType.TOP)
        if mode == 'sliding':
            self.rebuild_sliding_tabs()
        else:
            self.main_stack.set_visible_child_name('tabs')
            for view in self.sliding_views:
                view.destroy()
            self.sliding_views.clear()

    def rebuild_sliding_tabs(self, *_):
        if self.closed or self.tab_mode != 'sliding':
            return
        for view in self.sliding_views:
            view.destroy()
        self.sliding_views.clear()
        for index in range(self.tabs.get_n_pages()):
            pane = self.tabs.get_nth_page(index)
            view = self.make_mirror(pane)
            if view is not None:
                view.canonical = pane
                if hasattr(view, 'title_bar'):
                    view.title_bar.set_no_show_all(False)
                view.set_size_request(480, -1)
                self.sliding_strip.pack_start(view, False, False, 0)
                self.sliding_views.append(view)
        self.sliding_strip.show_all()
        selected = self.tabs.get_nth_page(self.tabs.get_current_page()) if self.tabs.get_n_pages() else None
        self.main_stack.set_visible_child_name('sliding' if isinstance(selected, (NotePane, GraphView)) else 'tabs')

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
        self.pending_file_selection = folder + '/'
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
    from .gtk_runtime import require_gtk_schemas
    try:
        require_gtk_schemas()
    except RuntimeError as error:
        parser.exit(1, f'Libre Vault: {error}\n')
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
