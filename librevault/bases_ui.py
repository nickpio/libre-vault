"""Table of note properties filtered with the vault search syntax."""
import uuid

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gtk, Pango

from .bases import build_table, display_value, iter_note_text, set_property
from .editing import TextEdit
from .metadata import note_metadata
from .vault import VaultError


class BasesView(Gtk.Box):
    def __init__(self, owner, state=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.owner = owner
        self.workspace_id = uuid.uuid4().hex
        self.sort_column = 'name'
        self.descending = False
        self.hidden_columns = set()
        self.table = build_table(())
        self.store = None
        self.name_column = None
        self.property_renderers = {}
        self.column_checks = {}
        self.signature = None
        self.reload_source = None
        self.editing = False
        self.reloading = False
        self._loading_columns = False
        self.closed = False
        self.needs_reload = False
        owner.bases_views.add(self)
        self.set_border_width(12)
        self.get_style_context().add_class('bases-view')
        bar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text='Filter with tag:work, path:Projects, [status]…')
        self.search.set_hexpand(True)
        self.search.get_accessible().set_name('Filter notes')
        self.columns_button = Gtk.MenuButton(label='Columns')
        self.columns_button.get_accessible().set_name('Choose columns')
        self.popover = Gtk.Popover()
        self.column_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.column_box.set_border_width(10)
        self.popover.add(self.column_box)
        self.columns_button.set_popover(self.popover)
        bar.pack_start(self.search, True, True, 0)
        bar.pack_start(self.columns_button, False, False, 0)
        self.pack_start(bar, False, False, 0)
        self.tree = Gtk.TreeView()
        self.tree.set_headers_visible(True)
        self.tree.set_enable_search(False)
        self.tree.set_activate_on_single_click(False)
        self.tree.get_accessible().set_name('Note properties')
        self.tree.connect('row-activated', self.on_row_activated)
        self.tree.connect('button-press-event', self.on_press)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroll.add(self.tree)
        self.empty = Gtk.Label(label='No matching notes', xalign=0, yalign=0)
        self.empty.set_margin_top(12)
        self.empty.get_style_context().add_class('muted')
        self.stack = Gtk.Stack()
        self.stack.add_named(self.scroll, 'table')
        self.stack.add_named(self.empty, 'empty')
        self.pack_start(self.stack, True, True, 0)
        self.summary = Gtk.Label(xalign=0)
        self.summary.set_ellipsize(Pango.EllipsizeMode.END)
        self.summary.get_style_context().add_class('muted')
        self.pack_start(self.summary, False, False, 0)
        if state:
            self.apply_state(state)
        self.search.connect('search-changed', self.schedule_reload)
        self.connect('destroy', self.cleanup)
        self.connect('map', self.on_map)
        self.reload()
        self.show_all()

    def apply_state(self, state):
        if not isinstance(state, dict):
            return
        if isinstance(state.get('id'), str):
            self.workspace_id = state['id']
        if isinstance(state.get('query'), str):
            self.search.set_text(state['query'])
        if isinstance(state.get('sort'), str) and state['sort']:
            self.sort_column = state['sort']
        self.descending = state.get('descending') is True
        hidden = state.get('hidden')
        if isinstance(hidden, list):
            self.hidden_columns = {item for item in hidden if isinstance(item, str)}

    def snapshot(self):
        return {'kind': 'bases', 'id': self.workspace_id, 'query': self.search.get_text(),
                'sort': self.sort_column, 'descending': self.descending,
                'hidden': sorted(self.hidden_columns)}

    def cleanup(self, *_):
        self.closed = True
        if self.reload_source:
            GLib.source_remove(self.reload_source)
            self.reload_source = None
        self.owner.bases_views.discard(self)

    def schedule_reload(self, *_):
        if self.reload_source:
            GLib.source_remove(self.reload_source)
        self.reload_source = GLib.timeout_add(200, self._reload_scheduled)

    def _reload_scheduled(self):
        self.reload_source = None
        self.reload()
        return False

    def on_map(self, *_):
        if self.needs_reload and not self.closed:
            self.reload()

    def content_signature(self):
        vault = self.owner.vault
        if vault is None:
            return None
        opened = tuple((path, hash(pane.text())) for path, pane in sorted(self.owner.panes.items()))
        return (vault.index.generation, self.search.get_text(), opened)

    def refresh_if_changed(self):
        if self.closed or self.editing or self.reloading or self.owner.vault is None:
            return
        signature = self.content_signature()
        if signature == self.signature:
            return
        if not self.get_mapped():
            self.needs_reload = True
            return
        self.reload()

    def note_sources(self):
        buffers = {path: pane.text() for path, pane in self.owner.panes.items()}
        return iter_note_text(self.owner.vault, buffers)

    def reload(self, *_):
        if self.reloading:
            return False
        if self.reload_source:
            GLib.source_remove(self.reload_source)
            self.reload_source = None
        if self.closed or self.owner.vault is None:
            return False
        self.reloading = True
        self.needs_reload = False
        try:
            self.table = build_table(self.note_sources(), self.search.get_text(),
                                     self.sort_column, self.descending)
            self.rebuild()
            self.signature = self.content_signature()
        finally:
            self.reloading = False
        return False

    def visible_keys(self):
        return [column.key for column in self.tree.get_columns()]

    def heading(self, key, title):
        if self.sort_column != key:
            return title
        return title + ('  ↓' if self.descending else '  ↑')

    def rebuild(self):
        self.property_renderers = {}
        for column in self.tree.get_columns():
            self.tree.remove_column(column)
        visible_properties = [key for key in self.table.columns if key not in self.hidden_columns]
        show_tags = 'tags' not in self.hidden_columns
        types = [str, str, str, *([str] * len(visible_properties))]
        self.store = Gtk.ListStore(*types)
        self.tree.set_model(self.store)
        self.add_column('Name', 1, 'name')
        self.add_column('Path', 0, 'path')
        if show_tags:
            self.add_column('Tags', 2, 'tags')
        for offset, key in enumerate(visible_properties):
            self.add_column(key, 3 + offset, key, editable=True)
        for row in self.table.rows:
            values = [row.path, row.name, ', '.join(row.tags)]
            for key in visible_properties:
                values.append(display_value(row.properties[key]) if key in row.properties else '')
            self.store.append(values)
        self.stack.set_visible_child_name('table' if self.table.rows else 'empty')
        noun = 'note' if len(self.table.rows) == 1 else 'notes'
        self.summary.set_text(f'{len(self.table.rows)} {noun} · {len(self.table.columns)} properties')
        self.rebuild_columns_popover()

    def add_column(self, title, model_index, key, editable=False):
        renderer = Gtk.CellRendererText()
        renderer.set_property('ellipsize', Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn(self.heading(key, title), renderer, text=model_index)
        column.set_resizable(True)
        column.set_min_width(140 if key == 'name' else 96)
        column.set_clickable(True)
        column.key = key
        column.connect('clicked', self.on_column_clicked)
        if key == 'name':
            column.set_expand(True)
            self.name_column = column
        if editable:
            renderer.set_property('editable', True)
            renderer.connect('edited', self.on_edited, key)
            renderer.connect('editing-started', self.edit_started)
            renderer.connect('editing-canceled', self.edit_canceled)
            self.property_renderers[key] = renderer
        self.tree.append_column(column)
        return column

    def rebuild_columns_popover(self):
        self._loading_columns = True
        self.column_checks = {}
        for child in self.column_box.get_children():
            child.destroy()
        entries = [('name', 'Name', True), ('path', 'Path', True), ('tags', 'Tags', False)]
        entries.extend((key, key, False) for key in self.table.columns)
        for key, label, fixed in entries:
            check = Gtk.CheckButton(label=label)
            check.set_active(fixed or key not in self.hidden_columns)
            check.set_sensitive(not fixed)
            check.connect('toggled', self.toggle_column, key)
            self.column_box.pack_start(check, False, False, 0)
            self.column_checks[key] = check
        self.column_box.show_all()
        self._loading_columns = False

    def toggle_column(self, check, key):
        if self._loading_columns or key in ('name', 'path'):
            return
        if check.get_active():
            self.hidden_columns.discard(key)
        else:
            self.hidden_columns.add(key)
        if self.reload_source:
            GLib.source_remove(self.reload_source)
        self.reload_source = GLib.idle_add(self._reload_scheduled)

    def on_column_clicked(self, column):
        key = column.key
        if key == self.sort_column:
            self.descending = not self.descending
        else:
            self.sort_column = key
            self.descending = False
        if self.reload_source:
            GLib.source_remove(self.reload_source)
        self.reload_source = GLib.idle_add(self._reload_scheduled)

    def set_sort(self, key):
        if key == self.sort_column:
            self.descending = not self.descending
        else:
            self.sort_column = key
            self.descending = False
        self.reload()

    def edit_started(self, *_):
        self.editing = True

    def edit_canceled(self, *_):
        self.editing = False

    def on_edited(self, _renderer, path, text, key):
        self.editing = False
        self.commit_edit(self.store[path][0], key, text)

    def row_path(self, note_path):
        for row in self.store:
            if row[0] == note_path:
                return row.path
        return None

    def commit_edit(self, path, key, raw):
        try:
            if path in self.owner.panes:
                source = self.owner.panes[path].text()
                note = None
            else:
                note = self.owner.vault.read(path)
                source = note.text
            metadata = note_metadata(source)
            shown = display_value(metadata.properties[key]) if key in metadata.properties else ''
            if str(raw).strip() == shown.strip():
                return
            updated = set_property(source, key, raw)
        except (ValueError, VaultError, OSError) as error:
            self.owner.status.set_text(str(error))
            return
        try:
            pane = self.owner.panes.get(path)
            if pane is not None:
                if updated != pane.text():
                    pane.apply_edit(TextEdit(0, len(pane.text()), updated))
            elif note is not None and updated != note.text:
                self.owner.vault.save(note, updated)
                self.owner.populate_files()
        except (VaultError, OSError) as error:
            self.owner.status.set_text(str(error))
            return
        self.reload()
        self.owner.status.set_text(f'Updated {key} · {path}')

    def on_row_activated(self, _tree, path, _column):
        self.owner.open_note(self.store[path][0])

    def on_press(self, tree, event):
        if getattr(event, 'button', 1) != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        found = tree.get_path_at_pos(int(event.x), int(event.y))
        if not found or found[1] is not self.name_column:
            return False
        self.owner.open_note(self.store[found[0]][0])
        return True
