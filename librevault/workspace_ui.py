"""Saving, restoring, and managing named workspace layouts."""
import math

from gi.repository import GLib, Gtk

from .bases_ui import BasesView
from .bookmarks import WorkspaceManager, ask_text, confirm
from .canvas_ui import CanvasView
from .editor_ui import NotePane
from .graph_ui import GraphView
from .navigation import Location, NavigationHistory


def _prompt(*args, **kwargs):
    """Resolve prompts through the window module so existing patch points hold."""
    from . import app
    return getattr(app, 'ask_text', ask_text)(*args, **kwargs)


def _confirm(*args, **kwargs):
    from . import app
    return getattr(app, 'confirm', confirm)(*args, **kwargs)


class WorkspaceStateMixin:
    def update_workspace_state(self):
        opened = self.vault is not None
        self.workspace_stack.set_visible_child_name('workspace' if opened else 'welcome')
        self.notes_toggle.set_visible(opened)
        self.details_toggle.set_visible(opened)
        self.save_button.set_visible(opened)
        self.save_button.set_sensitive(opened and self.current() is not None)

    def saved_workspace_names(self):
        if self.vault is None:
            return []
        return self.saved_workspaces.names(self.vault.root)

    def save_workspace_layout(self, *_):
        if self.vault is None:
            return
        name = _prompt(self, 'Save workspace layout', 'Name this layout', accept='Save')
        if name is None:
            return
        if not name:
            self.status.set_text('Enter a workspace name.')
            return
        if name in self.saved_workspaces.names(self.vault.root):
            if not _confirm(self, f'Replace the saved layout “{name}”?', 'Replace'):
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
        for group in list(self.workspace_layout.groups):
            for index in reversed(range(group.tabs.get_n_pages())):
                page = group.tabs.get_nth_page(index)
                if isinstance(page.canonical, NotePane):
                    path = page.canonical.note.path
                    if path in deleted:
                        group.close_page(page)
                    else:
                        group.tabs.get_tab_label(page).get_children()[0].set_text(path)
        for window in tuple(self.note_windows):
            if window.pane.note.path in deleted:
                window.destroy()
            else:
                window.set_title(window.pane.note.path + ' · Libre Vault')
        self.rebuild_sliding_tabs()
        self.persist_workspace()

    def restore_docks(self, state):
        if not isinstance(state, list) or len(state) != 2 or not all(isinstance(items, list) for items in state):
            return
        known = {'files': self.sidebar, 'tags': self.tag_pane, 'details': self.details_container}
        if any(not isinstance(item, str) for items in state for item in items) or sorted(item for items in state for item in items) != sorted(known):
            return
        for dock, identifiers in zip((self.left_dock, self.right_dock), state):
            for identifier in identifiers:
                widget = known[identifier]
                widget.get_parent().remove(widget)
                dock.append_page(widget, Gtk.Label(label=identifier.capitalize()))
                dock.set_tab_reorderable(widget, True)
                dock.set_tab_detachable(widget, True)
        self.left_dock.show_all()
        self.right_dock.show_all()

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
                'layout': self.workspace_layout.snapshot(),
                'tab_mode': self.tab_mode,
                'details_view': self.details_view,
                'note_windows': [dict(path=window.pane.note.path, width=window.get_size().width, height=window.get_size().height) for window in self.note_windows],
                'docks': [[dock.get_nth_page(i).dock_id for i in range(dock.get_n_pages())] for dock in (self.left_dock, self.right_dock)],
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
            if isinstance(state.get('layout'), dict):
                self.workspace_layout.restore(state['layout'])
            elif isinstance(split, str) and split in self.panes:
                self.open_split(self.panes[split])
            elif isinstance(split, dict) and isinstance(split.get('graph'), str) and split['graph'] in graphs:
                self.open_split(graphs[split['graph']])
                if isinstance(split.get('state'), dict):
                    self.split_view.restore_state(split['state'])
            mode = state.get('tab_mode', 'tabs')
            self.set_tab_mode(mode if mode in ('tabs', 'stacked', 'sliding') else 'tabs')
            windows = state.get('note_windows', [])
            if isinstance(windows, list):
                for saved in windows[:30]:
                    if isinstance(saved, dict) and isinstance(saved.get('path'), str) and saved['path'] in self.panes:
                        from .layout_ui import NoteWindow
                        window = NoteWindow(self, self.panes[saved['path']])
                        width, height = saved.get('width', 720), saved.get('height', 600)
                        if type(width) is int and type(height) is int:
                            window.resize(max(300, min(4000, width)), max(200, min(3000, height)))
            details_view = state.get('details_view', 'outline')
            if not isinstance(details_view, str) or details_view not in self.details_buttons:
                details_view = 'outline'
            self.details_buttons[details_view].set_active(True)
            self.restore_docks(state.get('docks'))
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

