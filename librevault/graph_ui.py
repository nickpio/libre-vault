"""Native Cairo graph window, with an isolated, demand-driven layout worker."""
import math
import multiprocessing
import uuid
from pathlib import PurePosixPath

from gi.repository import Gdk, GLib, Gtk, Pango

from .graph import graph_worker


class GraphView(Gtk.Box):
    def __init__(self, owner, local=False, state=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.owner = owner
        self.workspace_id = uuid.uuid4().hex
        self.needs_reload = True
        self.closed = False
        owner.graph_views.add(self)
        self.process = self.pipe = None
        self.poll_source = self.reload_source = None
        self.paths, self.edges, self.positions = [], [], []
        self.adjacent = []
        self.hover = self.selected = None
        self.drag = None
        self.scale, self.pan_x, self.pan_y = 1., 0., 0.
        self.active = False
        self.signature = None
        pane = owner.current()
        self.root = pane.note.path if pane else None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        self.pack_start(box, True, True, 0)
        bar = Gtk.FlowBox()
        bar.set_selection_mode(Gtk.SelectionMode.NONE)
        bar.set_max_children_per_line(6)
        bar.set_min_children_per_line(1)
        bar.set_column_spacing(4)
        self.search = Gtk.SearchEntry(placeholder_text='Filter note paths and aliases')
        self.search.set_hexpand(True)
        box.pack_start(self.search, False, False, 0)
        box.pack_start(bar, False, False, 0)
        self.local = Gtk.CheckButton(label='Local graph')
        self.local.set_sensitive(self.root is not None)
        self.local.set_active(local and self.root is not None)
        self.local.set_tooltip_text('Show links around the note active when this graph was opened')
        bar.add(self.local)
        self.depth = Gtk.SpinButton.new_with_range(1, 5, 1)
        self.depth.set_tooltip_text('Local graph depth')
        self.depth.get_accessible().set_name('Local graph depth')
        bar.add(self.depth)
        self.orphans = Gtk.CheckButton(label='Orphans')
        self.orphans.set_active(True)
        bar.add(self.orphans)
        for title, action in [('Fit', self.fit), ('Refresh', self.reload)]:
            button = Gtk.Button(label=title)
            button.connect('clicked', action)
            bar.add(button)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_can_focus(True)
        self.canvas.get_accessible().set_name('Note graph. Arrow keys select notes; Enter opens; plus and minus zoom; F fits.')
        self.canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                               Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                               Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        box.pack_start(self.canvas, True, True, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        box.pack_start(self.status, False, False, 0)
        hint = Gtk.Label(label='Click to open · Drag to move · Scroll to zoom · F to fit', xalign=0)
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)
        self.canvas.connect('draw', self.draw)
        self.canvas.connect('button-press-event', self.press)
        self.canvas.connect('button-release-event', self.release)
        self.canvas.connect('motion-notify-event', self.motion)
        self.canvas.connect('scroll-event', self.scroll)
        self.canvas.connect('key-press-event', self.key)
        self.canvas.connect('leave-notify-event', self.leave)
        self.connect('destroy', self.cleanup)
        self.connect('map', self.mapped)
        self.connect('unmap', self.unmapped)
        self.search.connect('search-changed', self.schedule_reload)
        self.local.connect('toggled', self.schedule_reload)
        self.depth.connect('value-changed', self.schedule_reload)
        self.orphans.connect('toggled', self.schedule_reload)
        if state:
            self.restore_state(state)
        self.show_all()

    def schedule_reload(self, *_):
        if self.reload_source:
            GLib.source_remove(self.reload_source)
        self.reload_source = GLib.timeout_add(250, self.reload)

    def refresh_if_changed(self):
        signature = self.owner.vault.index.connection.total_changes
        if self.signature != signature:
            self.signature = signature
            self.schedule_reload()

    def stop_worker(self):
        if self.poll_source:
            GLib.source_remove(self.poll_source)
            self.poll_source = None
        if self.pipe:
            self.pipe.close()
            self.pipe = None
        if self.process:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=.2)
            self.process.close()
            self.process = None

    def snapshot(self):
        return dict(kind='graph', id=self.workspace_id, root=self.root,
                    local=self.local.get_active(), query=self.search.get_text(),
                    depth=self.depth.get_value_as_int(), orphans=self.orphans.get_active())

    def restore_state(self, state):
        if isinstance(state.get('id'), str):
            self.workspace_id = state['id']
        root = state.get('root')
        self.root = root if isinstance(root, str) and root in self.owner.vault.notes else None
        self.local.set_sensitive(self.root is not None)
        self.local.set_active(state.get('local') is True and self.root is not None)
        self.orphans.set_active(state.get('orphans') is not False)
        if isinstance(state.get('query'), str):
            self.search.set_text(state['query'])
        if type(state.get('depth')) is int:
            self.depth.set_value(max(1, min(5, state['depth'])))

    def mapped(self, *_):
        if self.needs_reload:
            self.schedule_reload()

    def unmapped(self, *_):
        self.needs_reload = self.needs_reload or not self.paths
        self.freeze()

    def freeze(self):
        self.stop_worker()
        self.active = False
        self.status.set_text(f'{len(self.paths):,} notes · {len(self.edges):,} links · Saved notes')

    def cleanup(self, *_):
        self.closed = True
        if self.reload_source:
            GLib.source_remove(self.reload_source)
            self.reload_source = None
        self.stop_worker()
        self.owner.graph_views.discard(self)

    def reload(self, *_):
        if self.reload_source:
            GLib.source_remove(self.reload_source)
            self.reload_source = None
        self.stop_worker()
        if self.closed:
            return False
        if not self.get_mapped():
            self.needs_reload = True
            return False
        self.needs_reload = False
        self.paths, self.edges, self.positions = [], [], []
        self.hover = self.selected = self.drag = None
        self.status.set_text('Loading saved-note graph…')
        self.canvas.queue_draw()
        self.signature = self.owner.vault.index.connection.total_changes
        context = multiprocessing.get_context('spawn')
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=graph_worker, args=(child, str(self.owner.vault.index.path),
            dict(query=self.search.get_text(), root=self.root if self.local.get_active() else None,
                 depth=self.depth.get_value_as_int(), orphans=self.orphans.get_active())), daemon=True)
        self.process.start()
        child.close()
        self.poll_source = GLib.timeout_add(33, self.poll)
        return False

    def poll(self):
        if not self.pipe.poll():
            if not self.process.is_alive():
                self.status.set_text('Graph worker stopped. Use Refresh to retry.')
                self.poll_source = None
                self.stop_worker()
                return False
            return True
        try:
            message = self.pipe.recv()
        except EOFError:
            self.status.set_text('Graph worker stopped. Use Refresh to retry.')
            self.poll_source = None
            self.stop_worker()
            return False
        if message[0] == 'error':
            self.status.set_text('Could not load graph: ' + message[1])
            self.poll_source = None
            self.stop_worker()
            return False
        if message[0] == 'graph':
            _, self.paths, self.edges, self.positions = message
            self.adjacent = [set() for _ in self.paths]
            for a, b in self.edges:
                self.adjacent[a].add(b)
                self.adjacent[b].add(a)
            self.active = bool(self.paths)
            self.fit()
        else:
            _, positions, self.active = message
            if self.drag is None:
                self.positions = positions
        self.status.set_text(f'{len(self.paths):,} notes · {len(self.edges):,} links · Saved notes' +
                             (' · Arranging…' if self.active else '') +
                             (' · No matching notes' if not self.paths else ''))
        self.canvas.queue_draw()
        if self.active and self.get_mapped():
            self.pipe.send('step')
            return True
        self.poll_source = None
        self.stop_worker()
        return False

    def fit(self, *_):
        if self.positions:
            xs, ys = zip(*self.positions)
            self.scale = max(.02, min(2., (self.canvas.get_allocated_width() - 100) / max(100, max(xs)-min(xs)),
                                      (self.canvas.get_allocated_height() - 100) / max(100, max(ys)-min(ys))))
            self.pan_x = -(max(xs) + min(xs)) / 2 * self.scale
            self.pan_y = -(max(ys) + min(ys)) / 2 * self.scale
        self.canvas.queue_draw()

    def screen(self, point):
        return (point[0] * self.scale + self.canvas.get_allocated_width()/2 + self.pan_x,
                point[1] * self.scale + self.canvas.get_allocated_height()/2 + self.pan_y)

    def hit(self, x, y):
        best, distance = None, 12 ** 2
        for i, point in enumerate(self.positions):
            px, py = self.screen(point)
            d = (px-x)**2 + (py-y)**2
            if d < distance:
                best, distance = i, d
        return best

    def draw(self, widget, cr):
        color = widget.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        focus = self.hover if self.hover is not None else self.selected
        points = [self.screen(p) for p in self.positions]
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_line_width(1)
        for highlighted in (False, True):
            cr.set_source_rgba(color.red, color.green, color.blue, .55 if highlighted else .12)
            for a, b in self.edges:
                if (focus in (a, b)) != highlighted:
                    continue
                x, y = points[a]
                ox, oy = points[b]
                if max(x, ox) < 0 or min(x, ox) > width or max(y, oy) < 0 or min(y, oy) > height:
                    continue
                cr.move_to(x, y)
                cr.line_to(ox, oy)
            cr.stroke()
        labels = 0
        cr.set_font_size(12)
        for i, (x, y) in enumerate(points):
            if not (-60 < x < width+60 and -20 < y < height+20):
                continue
            highlighted = i == focus or (focus is not None and i in self.adjacent[focus])
            if highlighted or self.paths[i] == self.root:
                cr.set_source_rgb(.48, .55, .95)
            else:
                cr.set_source_rgba(color.red, color.green, color.blue, .8 if focus is None else .3)
            radius = min(9, 3 + math.sqrt(len(self.adjacent[i]))) * min(1.5, max(.6, self.scale))
            cr.arc(x, y, radius, 0, math.tau)
            cr.fill()
            if i == focus or (self.scale >= .65 and labels < 150):
                cr.move_to(x + radius + 4, y + 4)
                cr.show_text(PurePosixPath(self.paths[i]).stem[:60])
                labels += 1
        return False

    def press(self, _, event):
        if event.button != 1:
            return False
        self.canvas.grab_focus()
        node = self.hit(event.x, event.y)
        self.selected = node
        self.drag = (node, event.x, event.y, self.pan_x, self.pan_y, False)
        return True

    def motion(self, _, event):
        if self.drag:
            node, x, y, px, py, moved = self.drag
            moved = moved or math.hypot(event.x-x, event.y-y) > 4
            if moved:
                # Freeze layout on first manipulation so a worker frame cannot undo a drag.
                self.freeze()
                if node is None:
                    self.pan_x, self.pan_y = px + event.x-x, py + event.y-y
                else:
                    self.positions[node] = [(event.x-self.canvas.get_allocated_width()/2-self.pan_x)/self.scale,
                                            (event.y-self.canvas.get_allocated_height()/2-self.pan_y)/self.scale]
            self.drag = (node, x, y, px, py, moved)
        else:
            node = self.hit(event.x, event.y)
            if node == self.hover:
                return False
            self.hover = node
            self.canvas.set_tooltip_text(self.paths[node] if node is not None else None)
        self.canvas.queue_draw()
        return True

    def release(self, _, event):
        if event.button == 1 and self.drag:
            node, _, _, _, _, moved = self.drag
            self.drag = None
            if node is not None and not moved:
                self.open_node(node)
        return True

    def open_node(self, node):
        self.owner.open_graph_note(self, self.paths[node])
        self.owner.present()

    def leave(self, *_):
        self.hover = None
        self.canvas.queue_draw()

    def zoom(self, factor, x, y):
        old = self.scale
        self.scale = max(.02, min(8, old * factor))
        ratio = self.scale / old
        cx, cy = self.canvas.get_allocated_width()/2, self.canvas.get_allocated_height()/2
        self.pan_x = x-cx-(x-cx-self.pan_x)*ratio
        self.pan_y = y-cy-(y-cy-self.pan_y)*ratio
        self.canvas.queue_draw()

    def scroll(self, _, event):
        dy = event.get_scroll_deltas()[2] if event.direction == Gdk.ScrollDirection.SMOOTH else (-1 if event.direction == Gdk.ScrollDirection.UP else 1)
        self.zoom(math.exp(-max(-4, min(4, dy))*.15), event.x, event.y)
        return True

    def key(self, _, event):
        key = Gdk.keyval_name(event.keyval)
        if key in ('Left', 'Up', 'Right', 'Down') and self.paths:
            self.selected = ((self.selected if self.selected is not None else -1) + (-1 if key in ('Left', 'Up') else 1)) % len(self.paths)
            self.hover = None
            x, y = self.positions[self.selected]
            self.pan_x, self.pan_y = -x * self.scale, -y * self.scale
            self.status.set_text(self.paths[self.selected] + ' · Enter to open')
            self.canvas.queue_draw()
        elif key in ('Return', 'KP_Enter') and self.selected is not None:
            self.open_node(self.selected)
        elif key in ('plus', 'equal', 'minus'):
            self.zoom(.8 if key == 'minus' else 1.25, self.canvas.get_allocated_width()/2, self.canvas.get_allocated_height()/2)
        elif key in ('f', 'F'):
            self.fit()
        elif key == 'Escape':
            return False
        else:
            return False
        return True


class GraphWindow(Gtk.Window):
    def __init__(self, owner, local=False):
        super().__init__(title='Graph view', transient_for=owner)
        self.set_default_size(1000, 720)
        self.set_destroy_with_parent(True)
        self.graph = GraphView(owner, local=local)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)
        button = Gtk.Button(label='Open as tab')
        button.set_halign(Gtk.Align.END)
        button.set_margin_top(12)
        button.set_margin_start(12)
        button.set_margin_end(12)
        button.connect('clicked', lambda *_: self.open_as_tab(owner))
        box.pack_start(button, False, False, 0)
        box.pack_start(self.graph, True, True, 0)
        self.connect('destroy', lambda *_: setattr(owner, 'graph_window', None))
        self.connect('key-press-event', self.key)
        self.connect('window-state-event', self.window_state)
        self.show_all()

    def open_as_tab(self, owner):
        owner.show_graph_tab(state=self.graph.snapshot())
        self.destroy()

    def window_state(self, _, event):
        if event.new_window_state & Gdk.WindowState.ICONIFIED:
            self.graph.freeze()
        return False

    def key(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False
