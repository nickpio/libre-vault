"""Per-vault bookmarks stored outside the vault, next to workspaces.json."""
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Pango

from .workspace import _write


BOOKMARK_TYPES = ('note', 'search', 'folder', 'graph')
BOOKMARK_ICONS = {
    'note': 'text-x-generic-symbolic',
    'folder': 'folder-symbolic',
    'search': 'edit-find-symbolic',
    'graph': 'view-grid-symbolic',
}
_INVALID = object()


def normalize_relative(value):
    """Vault-relative path with no absolute, empty, or parent segments."""
    if not isinstance(value, str) or '\x00' in value:
        return None
    text = value.strip().replace('\\', '/')
    if text.startswith('/'):
        return None
    text = text.rstrip('/')
    if not text:
        return None
    parts = text.split('/')
    if any(part in ('', '.', '..') for part in parts):
        return None
    return '/'.join(parts)


def _optional_text(value, limit):
    if value is None:
        return ''
    if type(value) is not str or '\x00' in value or len(value) > limit:
        return None
    return value


def _json_value(value, depth=0):
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is str:
        if '\x00' in value or len(value) > 10000:
            return _INVALID
        return value
    if type(value) is float:
        return value if math.isfinite(value) else _INVALID
    if isinstance(value, list):
        if depth > 6:
            return _INVALID
        items = []
        for item in value:
            parsed = _json_value(item, depth + 1)
            if parsed is _INVALID:
                return _INVALID
            items.append(parsed)
        return items
    if isinstance(value, dict):
        return _json_object(value, depth)
    return _INVALID


def _json_object(value, depth=0):
    if not isinstance(value, dict) or depth > 6:
        return None
    cleaned = {}
    for key, item in value.items():
        if type(key) is not str or '\x00' in key or not key or len(key) > 200:
            continue
        parsed = _json_value(item, depth + 1)
        if parsed is not _INVALID:
            cleaned[key] = parsed
    return cleaned


@dataclass(frozen=True)
class Bookmark:
    """A note, search, folder, or graph view saved outside the vault."""

    type: str
    path: str = ''
    fragment: str = ''
    query: str = ''
    title: str = ''
    state: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'state', dict(self.state) if isinstance(self.state, dict) else {})

    def to_dict(self):
        data = {'type': self.type}
        if self.title:
            data['title'] = self.title
        if self.type in ('note', 'folder'):
            data['path'] = self.path
        if self.type == 'note' and self.fragment:
            data['fragment'] = self.fragment
        if self.type == 'search':
            data['query'] = self.query
        if self.type == 'graph':
            data['state'] = self.state
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or data.get('type') not in BOOKMARK_TYPES:
            return None
        kind = data['type']
        title = _optional_text(data.get('title', ''), 200)
        if title is None:
            return None
        title = title.strip()
        path = ''
        if kind in ('note', 'folder'):
            path = normalize_relative(data.get('path', ''))
            if path is None:
                return None
        fragment = ''
        if kind == 'note':
            fragment = _optional_text(data.get('fragment', ''), 500)
            if fragment is None:
                return None
            fragment = fragment.strip()
        query = ''
        if kind == 'search':
            query = _optional_text(data.get('query', ''), 2000)
            if query is None or not query.strip():
                return None
        state = {}
        if kind == 'graph':
            state = _json_object(data.get('state', {}))
            if state is None:
                return None
        return cls(type=kind, path=path, fragment=fragment, query=query, title=title, state=state)

    def display_title(self):
        if self.title:
            return self.title
        if self.type == 'note':
            name = Path(self.path).stem or self.path
            return f'{name} · {self.fragment}' if self.fragment else name
        if self.type == 'folder':
            return self.path
        if self.type == 'search':
            return self.query
        if self.state.get('local'):
            return 'Local graph'
        return 'Graph'

    def detail(self):
        if self.type == 'note':
            return f'{self.path}#{self.fragment}' if self.fragment else self.path
        if self.type == 'search':
            return self.query
        if self.type == 'folder':
            return self.path
        root = self.state.get('root')
        if isinstance(root, str) and root:
            return f'Graph · {root}'
        return 'Graph view'


def _clean_mapping(mapping):
    cleaned = {}
    if not isinstance(mapping, dict):
        return cleaned
    for old, new in mapping.items():
        old_path, new_path = normalize_relative(old), normalize_relative(new)
        if old_path and new_path:
            cleaned[old_path] = new_path
    return cleaned


def _deleted_paths(deleted):
    paths = []
    for item in deleted or ():
        path = normalize_relative(item)
        if path and path not in paths:
            paths.append(path)
    return paths


def _is_deleted(path, deleted):
    return any(path == item or path.startswith(item + '/') for item in deleted)


def _mapped_path(path, mapping):
    if path in mapping:
        return mapping[path]
    best_old, best_new = '', None
    for old, new in mapping.items():
        if path.startswith(old + '/') and len(old) > len(best_old):
            best_old, best_new = old, new
    if best_new is None:
        return path
    return best_new + path[len(best_old):]


def remap_bookmarks(bookmarks, mapping, deleted=()):
    """Move bookmark paths with a rename map and drop ones that were trashed."""
    mapping = _clean_mapping(mapping)
    deleted = _deleted_paths(deleted)
    updated = []
    for bookmark in bookmarks:
        if bookmark.type in ('note', 'folder'):
            if _is_deleted(bookmark.path, deleted):
                continue
            updated.append(replace(bookmark, path=_mapped_path(bookmark.path, mapping)))
            continue
        if bookmark.type == 'graph':
            state = dict(bookmark.state)
            root = state.get('root')
            if isinstance(root, str) and root:
                root_path = normalize_relative(root)
                if root_path and _is_deleted(root_path, deleted):
                    state['root'] = None
                elif root_path:
                    state['root'] = _mapped_path(root_path, mapping)
            updated.append(replace(bookmark, state=state))
            continue
        updated.append(bookmark)
    return updated


def with_moved_root(mapping, source, destination):
    """Record the moved folder itself when the engine only mapped files inside it."""
    mapping = dict(mapping or {})
    root = normalize_relative(source)
    if not root or root in mapping:
        return mapping
    prefix = root + '/'
    mapped_root = None
    for old, new in mapping.items():
        if isinstance(old, str) and isinstance(new, str) and old.startswith(prefix):
            suffix = old[len(root):]
            if new.endswith(suffix):
                mapped_root = new[:-len(suffix)].rstrip('/')
                break
    if not mapped_root and isinstance(destination, str):
        mapped_root = normalize_relative(destination)
    if mapped_root and mapped_root != root:
        mapping[root] = mapped_root
    return mapping


def graph_open_state(bookmark):
    """Graph bookmark state for a new tab, without a previous workspace id."""
    state = dict(bookmark.state)
    state.pop('id', None)
    return state


class BookmarkStore:
    """Ordered bookmarks for each vault, in bookmarks.json beside workspaces.json."""

    def __init__(self, preferences_path):
        self.path = Path(preferences_path).with_name('bookmarks.json')

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def _key(self, root):
        return str(Path(root).resolve())

    def load(self, root):
        entries = self._read().get(self._key(root), [])
        if not isinstance(entries, list):
            return []
        bookmarks = []
        for entry in entries:
            bookmark = Bookmark.from_dict(entry)
            if bookmark is not None:
                bookmarks.append(bookmark)
        return bookmarks

    def save(self, root, bookmarks):
        data = self._read()
        key = self._key(root)
        if bookmarks:
            data[key] = [bookmark.to_dict() for bookmark in bookmarks]
        else:
            data.pop(key, None)
        _write(self.path, data)

    def add(self, root, bookmark):
        if not isinstance(bookmark, Bookmark):
            raise TypeError('expected a Bookmark')
        stored = Bookmark.from_dict(bookmark.to_dict())
        if stored is None:
            raise ValueError('Bookmark is missing a note, folder, search, or graph.')
        items = self.load(root)
        items.append(stored)
        self.save(root, items)
        return items

    def remove(self, root, index):
        items = self.load(root)
        if not 0 <= index < len(items):
            raise IndexError('bookmark index out of range')
        items.pop(index)
        self.save(root, items)
        return items

    def reorder(self, root, source, destination):
        items = self.load(root)
        if not 0 <= source < len(items):
            raise IndexError('bookmark index out of range')
        destination = max(0, min(len(items) - 1, destination))
        item = items.pop(source)
        items.insert(destination, item)
        self.save(root, items)
        return items

    def rename(self, root, index, title):
        if type(title) is not str or '\x00' in title:
            raise ValueError('Enter a bookmark title.')
        title = title.strip()
        if len(title) > 200:
            raise ValueError('Enter a shorter bookmark title.')
        items = self.load(root)
        if not 0 <= index < len(items):
            raise IndexError('bookmark index out of range')
        items[index] = replace(items[index], title=title)
        self.save(root, items)
        return items

    def remap(self, root, mapping, deleted=()):
        current = self.load(root)
        updated = remap_bookmarks(current, mapping, deleted)
        if updated != current:
            self.save(root, updated)
        return updated


def ask_text(parent, title, message, accept='OK', text=''):
    dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, accept, Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    content = dialog.get_content_area()
    content.set_border_width(16)
    content.set_spacing(8)
    label = Gtk.Label(label=message, xalign=0)
    label.set_line_wrap(True)
    content.pack_start(label, False, False, 0)
    entry = Gtk.Entry(text=text)
    entry.set_activates_default(True)
    entry.set_width_chars(36)
    content.pack_start(entry, False, False, 0)
    dialog.show_all()
    entry.grab_focus()
    accepted = dialog.run() == Gtk.ResponseType.OK
    value = entry.get_text().strip()
    dialog.destroy()
    if not accepted:
        return None
    return value


def confirm(parent, text, accept='Replace'):
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION, text=text)
    dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, accept, Gtk.ResponseType.OK)
    accepted = dialog.run() == Gtk.ResponseType.OK
    dialog.destroy()
    return accepted


class BookmarkManager(Gtk.Dialog):
    """Open, rename, reorder, or remove bookmarks for the current vault."""

    def __init__(self, owner):
        super().__init__(title='Bookmarks', transient_for=owner, modal=True)
        self.owner = owner
        self.set_destroy_with_parent(True)
        self.set_default_size(520, 420)
        self.add_button('Close', Gtk.ResponseType.CLOSE)
        self.connect('response', lambda *_: self.destroy())
        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        hint = Gtk.Label(label='Open a bookmark, or rename, reorder, and remove it.', xalign=0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class('muted')
        content.pack_start(hint, False, False, 0)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.set_activate_on_single_click(False)
        self.list.connect('row-activated', lambda *_: self.open_selected())
        self.list.connect('row-selected', lambda *_: self.update_buttons())
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(220)
        scroll.add(self.list)
        content.pack_start(scroll, True, True, 0)
        buttons = Gtk.Box(spacing=8)
        self.open_button = Gtk.Button(label='Open')
        self.rename_button = Gtk.Button(label='Rename')
        self.up_button = Gtk.Button(label='Up')
        self.down_button = Gtk.Button(label='Down')
        self.remove_button = Gtk.Button(label='Remove')
        self.open_button.connect('clicked', lambda *_: self.open_selected())
        self.rename_button.connect('clicked', lambda *_: self.rename_selected())
        self.up_button.connect('clicked', lambda *_: self.move_selected(-1))
        self.down_button.connect('clicked', lambda *_: self.move_selected(1))
        self.remove_button.connect('clicked', lambda *_: self.remove_selected())
        for button in (self.open_button, self.rename_button, self.up_button, self.down_button):
            buttons.pack_start(button, False, False, 0)
        buttons.pack_end(self.remove_button, False, False, 0)
        content.pack_start(buttons, False, False, 0)
        self.empty = Gtk.Label(label='No bookmarks yet.')
        self.empty.get_style_context().add_class('muted')
        self.list.set_placeholder(self.empty)
        self.empty.show()
        self.reload(0)
        self.show_all()

    def reload(self, select=0):
        if self.in_destruction():
            return
        for row in self.list.get_children():
            row.destroy()
        for index, bookmark in enumerate(self.owner.bookmark_items):
            row = Gtk.ListBoxRow()
            row.index = index
            row.title = bookmark.display_title()
            line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label(label=bookmark.display_title(), xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            detail = Gtk.Label(label=bookmark.detail(), xalign=0)
            detail.set_ellipsize(Pango.EllipsizeMode.END)
            detail.get_style_context().add_class('muted')
            line.pack_start(title, False, False, 0)
            line.pack_start(detail, False, False, 0)
            row.add(line)
            row.get_accessible().set_name(f'{bookmark.display_title()}, {bookmark.detail()}')
            self.list.add(row)
        self.list.show_all()
        rows = self.list.get_children()
        if rows:
            self.list.select_row(rows[max(0, min(select, len(rows) - 1))])
        self.update_buttons()

    def update_buttons(self):
        row = self.list.get_selected_row()
        index = row.index if row is not None else None
        count = len(self.owner.bookmark_items)
        chosen = index is not None
        self.open_button.set_sensitive(chosen)
        self.rename_button.set_sensitive(chosen)
        self.remove_button.set_sensitive(chosen)
        self.up_button.set_sensitive(chosen and index > 0)
        self.down_button.set_sensitive(chosen and index < count - 1)

    def open_selected(self):
        row = self.list.get_selected_row()
        if row is None:
            return
        bookmark = self.owner.bookmark_items[row.index]
        self.destroy()
        self.owner.open_bookmark(bookmark)

    def rename_selected(self):
        row = self.list.get_selected_row()
        if row is None:
            return
        current = self.owner.bookmark_items[row.index]
        title = ask_text(self, 'Rename bookmark', 'Bookmark title', accept='Rename', text=current.display_title())
        if title is None:
            return
        self.owner.rename_bookmark(row.index, title)

    def move_selected(self, delta):
        row = self.list.get_selected_row()
        if row is None:
            return
        self.owner.move_bookmark(row.index, delta)

    def remove_selected(self):
        row = self.list.get_selected_row()
        if row is None:
            return
        self.owner.remove_bookmark_at(row.index)


class WorkspaceManager(Gtk.Dialog):
    """Load or delete named workspace layouts for the current vault."""

    def __init__(self, owner):
        super().__init__(title='Workspaces', transient_for=owner, modal=True)
        self.owner = owner
        self.set_destroy_with_parent(True)
        self.set_default_size(420, 360)
        self.add_button('Close', Gtk.ResponseType.CLOSE)
        self.connect('response', lambda *_: self.destroy())
        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        hint = Gtk.Label(label='Saved layouts restore tabs, the active note, the split, folders, and search.', xalign=0)
        hint.set_line_wrap(True)
        hint.set_max_width_chars(48)
        hint.get_style_context().add_class('muted')
        content.pack_start(hint, False, False, 0)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.set_activate_on_single_click(False)
        self.list.connect('row-activated', lambda *_: self.load_selected())
        self.list.connect('row-selected', lambda *_: self.update_buttons())
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(180)
        scroll.add(self.list)
        content.pack_start(scroll, True, True, 0)
        buttons = Gtk.Box(spacing=8)
        self.load_button = Gtk.Button(label='Load')
        self.delete_button = Gtk.Button(label='Delete')
        self.load_button.connect('clicked', lambda *_: self.load_selected())
        self.delete_button.connect('clicked', lambda *_: self.delete_selected())
        buttons.pack_start(self.load_button, False, False, 0)
        buttons.pack_end(self.delete_button, False, False, 0)
        content.pack_start(buttons, False, False, 0)
        self.empty = Gtk.Label(label='No saved layouts.')
        self.empty.get_style_context().add_class('muted')
        self.list.set_placeholder(self.empty)
        self.empty.show()
        self.reload()
        self.show_all()

    def reload(self):
        if self.in_destruction():
            return
        for row in self.list.get_children():
            row.destroy()
        names = self.owner.saved_workspace_names()
        for name in names:
            row = Gtk.ListBoxRow()
            row.workspace_name = name
            label = Gtk.Label(label=name, xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            row.add(label)
            row.get_accessible().set_name(name)
            self.list.add(row)
        self.list.show_all()
        rows = self.list.get_children()
        if rows:
            self.list.select_row(rows[0])
        self.update_buttons()

    def update_buttons(self):
        chosen = self.selected_name() is not None
        self.load_button.set_sensitive(chosen)
        self.delete_button.set_sensitive(chosen)

    def selected_name(self):
        row = self.list.get_selected_row()
        return row.workspace_name if row is not None else None

    def load_selected(self):
        name = self.selected_name()
        if name and self.owner.load_named_workspace(name):
            self.destroy()

    def delete_selected(self):
        name = self.selected_name()
        if not name:
            return
        if not confirm(self, f'Delete workspace layout “{name}”?', 'Delete'):
            return
        self.owner.delete_named_workspace(name)
        self.reload()
