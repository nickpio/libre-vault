"""Recursive GTK pane groups and shared-buffer note windows."""
from gi.repository import Gdk, Gtk
import math

from .splitview import SplitTextView
from .tabs import WorkspaceTabs


class NoteMirror(Gtk.Box):
    """A view, never a second owner of a note's history or disk revision."""
    def __init__(self, owner, pane):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.pane = pane
        self.title_bar = Gtk.Label(label=pane.note.path, xalign=0)
        self.title_bar.set_no_show_all(True)
        self.title_bar.set_margin_start(12)
        self.pack_start(self.title_bar, False, False, 6)
        self.editor = SplitTextView(pane)
        self.editor.connect('focus-in-event', lambda *_: owner.update_context() and False)
        self.connect('destroy', lambda *_: pane.navigation.leave())
        self.editor.connect('key-press-event', self.key_press)
        for signal, callback in [('copy-clipboard', pane.copy_selection), ('cut-clipboard', pane.cut_selection),
                                 ('populate-popup', pane.populate_edit_menu), ('paste-clipboard', pane.paste_attachment),
                                 ('button-release-event', pane.navigation.clicked),
                                 ('motion-notify-event', pane.navigation.motion), ('leave-notify-event', pane.navigation.leave)]:
            self.editor.connect(signal, callback)
        self.editor.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.editor.drag_dest_set(Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new('text/uri-list', 0, 1)], Gdk.DragAction.COPY)
        self.editor.connect('drag-data-received', pane.drop_attachments)
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.editor)
        scroll.get_vadjustment().connect('value-changed', self.editor.queue_sync)
        scroll.get_hadjustment().connect('value-changed', self.editor.queue_sync)
        self.pack_start(scroll, True, True, 0)

    def key_press(self, view, event):
        handled = self.pane.editor_key_press(view, event)
        from .shortcuts import EDITOR_SHORTCUTS, effective_shortcuts
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        opening_find = any(shortcut.handler in ('show_find', 'show_replace') and
                           Gtk.accelerator_parse(shortcut.accelerator) == (Gdk.keyval_to_lower(event.keyval), modifiers)
                           for shortcut in effective_shortcuts(EDITOR_SHORTCUTS, 'editor', self.pane.window.hotkeys))
        if handled and opening_find:
            owner = self.pane.window
            owner.main_stack.set_visible_child_name('tabs')
            owner.tabs.set_current_page(owner.tabs.page_num(self.pane))
            self.pane.findbar.query.grab_focus()
            owner.present()
        elif handled:
            view.grab_focus()
        return handled


class PaneGroup(Gtk.Box):
    def __init__(self, layout):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.layout = layout
        bar = Gtk.Box(spacing=4)
        chooser = Gtk.Button(label='Add active note')
        chooser.connect('clicked', lambda *_: self.add(layout.owner.tabs.get_nth_page(layout.owner.tabs.get_current_page())))
        bar.pack_start(chooser, True, True, 0)
        for label, orientation in [('Split right', Gtk.Orientation.HORIZONTAL), ('Split down', Gtk.Orientation.VERTICAL)]:
            button = Gtk.Button.new_from_icon_name('view-dual-symbolic' if orientation == Gtk.Orientation.HORIZONTAL else 'view-more-symbolic', Gtk.IconSize.MENU)
            button.set_tooltip_text(label)
            button.connect('clicked', lambda _, direction=orientation: layout.add_split(direction, self))
            bar.pack_start(button, False, False, 0)
        close = Gtk.Button(label='×')
        close.set_tooltip_text('Close pane group')
        close.connect('clicked', lambda *_: layout.remove_group(self))
        bar.pack_end(close, False, False, 0)
        self.pack_start(bar, False, False, 0)
        self.tabs = WorkspaceTabs()
        self.tabs.set_tab_pos(Gtk.PositionType.TOP)
        self.pack_start(self.tabs, True, True, 0)
        self.tabs.connect('switch-page', self.selected)

    def add(self, pane, state=None):
        if pane is None:
            return None
        content = self.layout.owner.make_mirror(pane, state)
        if content is None:
            return None
        content.canonical = pane
        label = Gtk.Box(spacing=4)
        title = Gtk.Label(label=getattr(getattr(pane, 'note', None), 'path', 'Graph'))
        title.set_ellipsize(3)
        title.set_max_width_chars(20)
        label.pack_start(title, True, True, 0)
        close = Gtk.Button(label='×', relief=Gtk.ReliefStyle.NONE)
        close.connect('clicked', lambda *_: self.close_page(content))
        label.pack_end(close, False, False, 0)
        label.show_all()
        self.tabs.append_page(content, label)
        content.show_all()
        self.tabs.set_current_page(self.tabs.page_num(content))
        self.layout.sync_legacy()
        return content

    def selected(self, _, page, index):
        self.layout.owner.split_pane = page.canonical
        self.layout.owner.split_view = page.editor if isinstance(page, NoteMirror) else page

    def close_page(self, page):
        self.tabs.remove_page(self.tabs.page_num(page))
        page.destroy()
        if not self.tabs.get_n_pages():
            self.layout.remove_group(self)
        else:
            self.layout.sync_legacy()

    def current(self):
        return self.tabs.get_nth_page(self.tabs.get_current_page()) if self.tabs.get_n_pages() else None

    def snapshot(self):
        return dict(kind='group', active=self.tabs.get_current_page(), tabs=[
            dict(path=page.pane.note.path) if isinstance(page, NoteMirror) else page.snapshot()
            for page in (self.tabs.get_nth_page(i) for i in range(self.tabs.get_n_pages()))])


class WorkspaceLayout(Gtk.Box):
    def __init__(self, owner, main):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.owner, self.main = owner, main
        self.groups = []
        self.connect('destroy', self.disposing)
        self.pack_start(main, True, True, 0)

    def disposing(self, *_):
        # Child notebook removal signals during native teardown must not try
        # to restructure a tree whose parents GTK is already disposing.
        self.groups.clear()

    def focused(self, focus):
        return next((group for group in self.groups if focus is group or focus and focus.is_ancestor(group)), None)

    def replace(self, old, new):
        parent = old.get_parent()
        first = isinstance(parent, Gtk.Paned) and parent.get_child1() is old
        parent.remove(old)
        if isinstance(parent, Gtk.Paned):
            (parent.pack1 if first else parent.pack2)(new, True, False)
        else:
            parent.pack_start(new, True, True, 0)

    def add_split(self, orientation=Gtk.Orientation.HORIZONTAL, group=None, pane=None):
        pane = pane or (group.current().canonical if group and group.current() else self.owner.active_page())
        if pane is None:
            return None
        group = group or self.focused(self.owner.get_focus())
        target = group or self.main
        added = PaneGroup(self)
        if added.add(pane) is None:
            added.destroy()
            return None
        branch = Gtk.Paned(orientation=orientation)
        self.replace(target, branch)
        branch.pack1(target, True, False)
        branch.pack2(added, True, False)
        self.groups.append(added)
        branch.show_all()
        def center(widget, allocation):
            if allocation.width > 1 and allocation.height > 1:
                extent = allocation.width if orientation == Gtk.Orientation.HORIZONTAL else allocation.height
                widget.set_position(extent // 2)
                widget.disconnect(widget.initial_allocation)
        branch.initial_allocation = branch.connect('size-allocate', center)
        self.sync_legacy()
        return added

    def remove_group(self, group):
        if group not in self.groups:
            return
        parent = group.get_parent()
        sibling = parent.get_child2() if parent.get_child1() is group else parent.get_child1()
        parent.remove(sibling)
        self.replace(parent, sibling)
        self.groups.remove(group)
        parent.destroy()
        self.sync_legacy()

    def sync_legacy(self):
        page = self.groups[0].current() if self.groups else None
        self.owner.split_pane = page.canonical if page else None
        self.owner.split_view = page.editor if isinstance(page, NoteMirror) else page

    def clear(self):
        for group in list(self.groups):
            self.remove_group(group)

    def remove_pane(self, pane):
        for group in list(self.groups):
            for index in reversed(range(group.tabs.get_n_pages())):
                page = group.tabs.get_nth_page(index)
                if page.canonical is pane:
                    group.close_page(page)

    def snapshot(self):
        def visit(widget):
            if widget is self.main:
                return dict(kind='main')
            if isinstance(widget, PaneGroup):
                return widget.snapshot()
            extent = widget.get_allocated_width() if widget.get_orientation() == Gtk.Orientation.HORIZONTAL else widget.get_allocated_height()
            return dict(kind='split', orientation='horizontal' if widget.get_orientation() == Gtk.Orientation.HORIZONTAL else 'vertical',
                        ratio=widget.get_position() / max(1, extent), first=visit(widget.get_child1()), second=visit(widget.get_child2()))
        return visit(self.get_children()[0])

    def restore(self, state):
        # Parse the persisted tree before detaching the main workspace.
        def valid(node, depth=0):
            if not isinstance(node, dict) or depth > 20:
                return False
            if node.get('kind') == 'main':
                return True
            if node.get('kind') == 'group':
                return isinstance(node.get('tabs'), list)
            return node.get('kind') == 'split' and valid(node.get('first'), depth+1) and valid(node.get('second'), depth+1)
        def mains(node):
            return int(node.get('kind') == 'main') if node.get('kind') != 'split' else mains(node['first']) + mains(node['second'])
        if not valid(state) or mains(state) != 1:
            return
        self.clear()
        Gtk.Box.remove(self, self.main)
        def build(node):
            if node['kind'] == 'main':
                return self.main
            if node['kind'] == 'group':
                group = PaneGroup(self)
                self.groups.append(group)
                for tab in node['tabs']:
                    if not isinstance(tab, dict):
                        continue
                    pane = self.owner.panes.get(tab['path']) if isinstance(tab.get('path'), str) else None
                    if pane is None and tab.get('kind') == 'graph':
                        pane = next((view for view in self.owner.tabs.get_children() if getattr(view, 'workspace_id', None) == tab.get('id')), None)
                    if pane is not None:
                        group.add(pane, tab)
                if not group.tabs.get_n_pages():
                    self.groups.remove(group)
                    group.destroy()
                    return None
                if type(node.get('active')) is int:
                    group.tabs.set_current_page(node['active'])
                return group
            orientation = Gtk.Orientation.VERTICAL if node.get('orientation') == 'vertical' else Gtk.Orientation.HORIZONTAL
            first, second = build(node['first']), build(node['second'])
            if first is None or second is None:
                return first if first is not None else second
            branch = Gtk.Paned(orientation=orientation)
            branch.pack1(first, True, False)
            branch.pack2(second, True, False)
            ratio = node.get('ratio', .5)
            ratio = max(.1, min(.9, ratio)) if type(ratio) in (int, float) and math.isfinite(ratio) else .5
            def allocate(widget, allocation):
                extent = allocation.width if orientation == Gtk.Orientation.HORIZONTAL else allocation.height
                if extent > 1:
                    widget.set_position(int(extent * ratio))
                    widget.disconnect(widget.restore_allocation)
            branch.restore_allocation = branch.connect('size-allocate', allocate)
            return branch
        self.pack_start(build(state), True, True, 0)
        self.show_all()
        self.sync_legacy()


class NoteWindow(Gtk.Window):
    def __init__(self, owner, pane):
        super().__init__(title=pane.note.path + ' · Libre Vault', transient_for=owner)
        self.owner, self.pane = owner, pane
        self.set_destroy_with_parent(True)
        self.set_default_size(720, 600)
        self.mirror = NoteMirror(owner, pane)
        self.add(self.mirror)
        self.connect('key-press-event', self.key_press)
        self.connect('destroy', lambda *_: owner.note_windows.discard(self))
        owner.note_windows.add(self)
        self.show_all()
        self.mirror.editor.grab_focus()

    def key_press(self, _, event):
        from .shortcuts import APP_SHORTCUTS, effective_shortcuts, shortcut_key
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        for shortcut in effective_shortcuts(APP_SHORTCUTS, 'app', self.owner.hotkeys):
            key, mask = Gtk.accelerator_parse(shortcut.accelerator)
            if shortcut_key(event.keyval) == shortcut_key(key) and modifiers == mask:
                if shortcut.handler == 'save_current':
                    self.owner.try_action(self.pane.save)
                elif shortcut.handler == 'close_tab':
                    self.destroy()
                else:
                    self.owner.tabs.set_current_page(self.owner.tabs.page_num(self.pane))
                    getattr(self.owner, shortcut.handler)()
                return True
        return False
