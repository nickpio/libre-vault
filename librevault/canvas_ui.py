"""Canvas tab: pan, zoom, and edit a JSON Canvas file."""
import math
from pathlib import Path

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gtk, Pango

from .appearance import font_size_pixels
from .canvas import (
    SIDES, CanvasFile, add_edge, add_node, move_node, remove_edge, remove_node, resize_node, update_edge, update_node,
)
from .vault import VaultError


PRESET_COLORS = {
    '1': (0.86, 0.28, 0.24),
    '2': (0.90, 0.49, 0.13),
    '3': (0.86, 0.72, 0.18),
    '4': (0.24, 0.63, 0.36),
    '5': (0.16, 0.63, 0.71),
    '6': (0.55, 0.36, 0.78),
}
SIDE_VECTOR = {'top': (0, -1), 'right': (1, 0), 'bottom': (0, 1), 'left': (-1, 0)}
MIN_SIZE = 48
HANDLE = 8


def paint_color(value):
    if not isinstance(value, str) or not value:
        return None
    if value in PRESET_COLORS:
        return PRESET_COLORS[value]
    if value.startswith('#') and len(value) in (4, 7):
        text = value[1:]
        if len(text) == 3:
            text = ''.join(character * 2 for character in text)
        try:
            return tuple(int(text[index:index + 2], 16) / 255 for index in (0, 2, 4))
        except ValueError:
            return None
    return None


def side_anchor(node, side):
    if side == 'top':
        return node.x + node.width / 2, node.y
    if side == 'right':
        return node.x + node.width, node.y + node.height / 2
    if side == 'bottom':
        return node.x + node.width / 2, node.y + node.height
    return node.x, node.y + node.height / 2


def nearest_side(node, x, y):
    return min(SIDES, key=lambda side: math.hypot(side_anchor(node, side)[0] - x, side_anchor(node, side)[1] - y))


def facing(node, other):
    dx = (other.x + other.width / 2) - (node.x + node.width / 2)
    dy = (other.y + other.height / 2) - (node.y + node.height / 2)
    if abs(dx) > abs(dy):
        return 'right' if dx >= 0 else 'left'
    return 'bottom' if dy >= 0 else 'top'


def round_rect(cr, x, y, width, height, radius):
    radius = max(0, min(radius, width / 2, height / 2))
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, math.pi * 1.5)
    cr.close_path()


def segment_distance(x1, y1, x2, y2, x, y):
    length = (x2 - x1) ** 2 + (y2 - y1) ** 2
    if length == 0:
        return math.hypot(x - x1, y - y1)
    step = max(0, min(1, ((x - x1) * (x2 - x1) + (y - y1) * (y2 - y1)) / length))
    return math.hypot(x - (x1 + step * (x2 - x1)), y - (y1 + step * (y2 - y1)))


class CanvasView(Gtk.Box):
    def __init__(self, owner, record):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.owner = owner
        self.path = record.path
        self.record = record
        self.document = record.document
        self.dirty = False
        self.scale = 1.0
        self.pan_x = 48.0
        self.pan_y = 48.0
        self.selection = None
        self.drag = None
        self.autosave_source = None
        self.tab_label = Gtk.Label(label=Path(record.path).stem)
        self.set_border_width(8)
        bar = Gtk.FlowBox()
        bar.set_selection_mode(Gtk.SelectionMode.NONE)
        bar.set_max_children_per_line(8)
        bar.set_column_spacing(4)
        bar.set_row_spacing(4)
        self.pack_start(bar, False, False, 0)
        actions = (
            ('New Text Card', lambda *_: self.add_text_card()),
            ('New Note Card', self.add_note_card),
            ('New Link Card', self.prompt_link_card),
            ('New Group', lambda *_: self.add_group_card()),
            ('Edit Selected', self.edit_selected),
            ('Delete Selected', lambda *_: self.delete_selected()),
            ('Zoom In', lambda *_: self.zoom_at(1.2, *self.view_center())),
            ('Zoom Out', lambda *_: self.zoom_at(1 / 1.2, *self.view_center())),
            ('Fit', self.fit),
            ('Save', lambda *_: self.owner.try_action(self.save)),
        )
        self.buttons = {}
        for label, callback in actions:
            button = Gtk.Button(label=label)
            button.connect('clicked', callback)
            button.get_accessible().set_name(label)
            self.buttons[label] = button
            bar.add(button)
        self.drawing = Gtk.DrawingArea()
        self.drawing.set_hexpand(True)
        self.drawing.set_vexpand(True)
        self.drawing.set_can_focus(True)
        self.drawing.get_accessible().set_name('JSON canvas. Drag cards to move them. Scroll to zoom. F fits the view.')
        self.drawing.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                                Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                                Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self.pack_start(self.drawing, True, True, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.pack_start(self.status, False, False, 0)
        hint = Gtk.Label(label='Drag a card to move it · Drag the corner to resize · Drag a side dot to connect · '
                         'Double-click a note card to open it · Other cards edit · Autosave after changes', xalign=0)
        hint.set_line_wrap(True)
        self.pack_start(hint, False, False, 0)
        self.drawing.connect('draw', self.draw)
        self.drawing.connect('button-press-event', self.on_press)
        self.drawing.connect('button-release-event', self.on_release)
        self.drawing.connect('motion-notify-event', self.on_motion)
        self.drawing.connect('scroll-event', self.on_scroll)
        self.drawing.connect('key-press-event', self.on_key)
        self.connect('destroy', self.cleanup)
        owner.canvas_views.add(self)
        self._retitle()
        self._status()
        self.show_all()

    @property
    def note(self):
        """Path carrier so the window can confirm unsaved canvas edits."""
        return self.record

    def snapshot(self):
        return {'kind': 'canvas', 'path': self.path}

    def cleanup(self, *_):
        self.cancel_autosave()
        views = getattr(self.owner, 'canvas_views', None)
        if views is not None:
            views.discard(self)

    def cancel_autosave(self):
        if self.autosave_source is not None:
            GLib.source_remove(self.autosave_source)
            self.autosave_source = None

    def _retitle(self):
        self.tab_label.set_text(('• ' if self.dirty else '') + Path(self.path).stem)

    def _status(self):
        state = 'Unsaved changes · autosave' if self.dirty else 'Saved'
        detail = ''
        if self.selection and self.selection[0] == 'node':
            node = self.document.node(self.selection[1])
            detail = f' · {node.type} card' if node else ''
        elif self.selection and self.selection[0] == 'edge':
            detail = ' · connection'
        self.status.set_text(
            f'{state} · {len(self.document.nodes)} cards · {len(self.document.edges)} connections · '
            f'{round(self.scale * 100)}%{detail}')

    def mark_dirty(self):
        self.dirty = True
        self._retitle()
        self._status()
        self.cancel_autosave()
        self.autosave_source = GLib.timeout_add_seconds(2, self.autosave)
        self.drawing.queue_draw()

    def autosave(self):
        self.autosave_source = None
        if getattr(self.owner, 'confirming_panes', False):
            self.autosave_source = GLib.timeout_add_seconds(2, self.autosave)
            return False
        try:
            self.save()
        except (VaultError, OSError) as error:
            self.owner.status.set_text(f'Autosave failed: {error}')
        return False

    def save(self):
        if not self.dirty:
            self._status()
            return self.record
        saved = self.owner.vault.save_canvas(self.record)
        self.record = saved
        self.document = saved.document
        self.dirty = False
        self.cancel_autosave()
        self._retitle()
        self._status()
        self.owner.status.set_text(f'Saved · {self.path}')
        self.drawing.queue_draw()
        return saved

    def reload_from_disk(self):
        self.record = self.owner.vault.read_canvas(self.path)
        self.document = self.record.document
        self.dirty = False
        self.selection = None
        self.drag = None
        self.cancel_autosave()
        self._retitle()
        self._status()
        self.drawing.queue_draw()

    def retarget(self, path):
        self.path = path
        self.record = CanvasFile(path, self.document, self.record.revision, self.record.bom, self.record.newline)
        self._retitle()
        tab = self.owner.tabs.get_tab_label(self)
        if tab is not None:
            tab.set_tooltip_text(path)

    def view_center(self):
        return self.drawing.get_allocated_width() / 2, self.drawing.get_allocated_height() / 2

    def to_screen(self, x, y):
        return x * self.scale + self.pan_x, y * self.scale + self.pan_y

    def to_world(self, x, y):
        return (x - self.pan_x) / self.scale, (y - self.pan_y) / self.scale

    def node_rect(self, node):
        x, y = self.to_screen(node.x, node.y)
        return x, y, node.width * self.scale, node.height * self.scale

    def _spawn(self):
        center_x, center_y = self.to_world(*self.view_center())
        offset = 28 * (len(self.document.nodes) % 6)
        return int(round(center_x - 130 + offset)), int(round(center_y - 70 + offset))

    def add_text_card(self, text=''):
        x, y = self._spawn()
        node = add_node(self.document, 'text', x=x, y=y, text=text)
        self.selection = ('node', node.id)
        self.mark_dirty()
        return node

    def add_file_card(self, file, subpath=None):
        x, y = self._spawn()
        node = add_node(self.document, 'file', x=x, y=y, file=file, subpath=subpath)
        self.selection = ('node', node.id)
        self.mark_dirty()
        return node

    def add_link_card(self, url):
        x, y = self._spawn()
        node = add_node(self.document, 'link', x=x, y=y, url=url)
        self.selection = ('node', node.id)
        self.mark_dirty()
        return node

    def add_group_card(self, label='Group'):
        x, y = self._spawn()
        node = add_node(self.document, 'group', x=x, y=y, label=label, behind=True)
        self.selection = ('node', node.id)
        self.mark_dirty()
        return node

    def add_note_card(self, *_):
        path = self.choose_note()
        if path:
            self.add_file_card(path)

    def prompt_link_card(self, *_):
        dialog = Gtk.Dialog(title='New link card', transient_for=self.owner, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Add', Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text='https://')
        entry.set_activates_default(True)
        entry.set_margin_top(12)
        entry.set_margin_bottom(12)
        entry.set_margin_start(12)
        entry.set_margin_end(12)
        dialog.get_content_area().add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        accepted = dialog.run() == Gtk.ResponseType.OK
        url = entry.get_text().strip()
        dialog.destroy()
        if accepted and url:
            self.add_link_card(url)

    def choose_note(self):
        notes = list(self.owner.vault.notes)
        if not notes:
            self.owner.status.set_text('This vault has no notes to place on the canvas.')
            return None
        dialog = Gtk.Dialog(title='Choose a note', transient_for=self.owner, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Add', Gtk.ResponseType.OK)
        dialog.set_default_size(460, 380)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        search = Gtk.SearchEntry(placeholder_text='Filter notes')
        content.pack_start(search, False, False, 0)
        listing = Gtk.ListBox()
        listing.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(220)
        scroll.add(listing)
        content.pack_start(scroll, True, True, 0)
        for path in notes:
            row = Gtk.ListBoxRow()
            row.note_path = path
            label = Gtk.Label(label=path, xalign=0)
            label.set_margin_start(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            row.add(label)
            listing.add(row)

        def refilter(*_):
            words = search.get_text().casefold().split()
            visible = []
            for row in listing.get_children():
                matched = all(word in row.note_path.casefold() for word in words)
                row.set_visible(matched)
                if matched:
                    visible.append(row)
            listing.select_row(visible[0] if visible else None)

        search.connect('search-changed', refilter)
        dialog.show_all()
        refilter()
        accepted = dialog.run() == Gtk.ResponseType.OK
        selected = listing.get_selected_row()
        path = selected.note_path if accepted and selected is not None and selected.get_visible() else None
        dialog.destroy()
        return path

    def delete_selected(self):
        if not self.selection:
            return
        kind, identifier = self.selection
        if kind == 'node':
            remove_node(self.document, identifier)
        else:
            remove_edge(self.document, identifier)
        self.selection = None
        self.drag = None
        self.mark_dirty()

    def change_node(self, node_id, **fields):
        node = update_node(self.document, node_id, **fields)
        self.mark_dirty()
        return node

    def edit_selected(self, *_):
        if not self.selection:
            return
        if self.selection[0] == 'node':
            node = self.document.node(self.selection[1])
            if node is not None:
                self.edit_node(node)
        else:
            self.edit_edge()

    def edit_node(self, node):
        dialog = Gtk.Dialog(title=f'Edit {node.type} card', transient_for=self.owner, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Apply', Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        fields = {}
        if node.type == 'text':
            view = Gtk.TextView()
            view.get_buffer().set_text(node.text)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            scroll = Gtk.ScrolledWindow()
            scroll.set_min_content_height(160)
            scroll.set_min_content_width(380)
            scroll.add(view)
            content.pack_start(scroll, True, True, 0)
            fields['text'] = view.get_buffer()
        elif node.type == 'file':
            fields['file'] = self._labeled_entry(content, 'Vault path', node.file)
            fields['subpath'] = self._labeled_entry(content, 'Subpath, starting with #', node.subpath or '')
        elif node.type == 'link':
            fields['url'] = self._labeled_entry(content, 'URL', node.url)
        elif node.type == 'group':
            fields['label'] = self._labeled_entry(content, 'Label', node.label or '')
        colors = Gtk.ComboBoxText()
        options = [('default', 'Default'), ('1', 'Red'), ('2', 'Orange'), ('3', 'Yellow'),
                   ('4', 'Green'), ('5', 'Cyan'), ('6', 'Purple')]
        current = node.color or 'default'
        if all(current != value for value, _title in options):
            options.append((current, current))
        for value, title in options:
            colors.append(value, title)
        colors.set_active_id(current)
        content.pack_start(Gtk.Label(label='Color', xalign=0), False, False, 0)
        content.pack_start(colors, False, False, 0)
        dialog.show_all()
        accepted = dialog.run() == Gtk.ResponseType.OK
        changes = {}
        if accepted:
            if node.type == 'text':
                buffer = fields['text']
                changes['text'] = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            elif node.type == 'file':
                changes['file'] = fields['file'].get_text().strip()
                subpath = fields['subpath'].get_text().strip()
                changes['subpath'] = subpath or None
            elif node.type == 'link':
                changes['url'] = fields['url'].get_text().strip()
            elif node.type == 'group':
                changes['label'] = fields['label'].get_text()
            color = colors.get_active_id()
            changes['color'] = None if color in (None, '', 'default') else color
        dialog.destroy()
        if not accepted:
            return
        try:
            self.change_node(node.id, **changes)
        except VaultError as error:
            self.owner.status.set_text(str(error))

    def edit_edge(self):
        edge = self.document.edge(self.selection[1]) if self.selection else None
        if edge is None:
            return
        dialog = Gtk.Dialog(title='Edit connection', transient_for=self.owner, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Apply', Gtk.ResponseType.OK)
        entry = self._labeled_entry(dialog.get_content_area(), 'Label', edge.label or '')
        dialog.get_content_area().set_border_width(12)
        dialog.show_all()
        accepted = dialog.run() == Gtk.ResponseType.OK
        label = entry.get_text()
        dialog.destroy()
        if not accepted:
            return
        update_edge(self.document, edge.id, label=label or None)
        self.mark_dirty()

    def _labeled_entry(self, content, caption, text):
        content.pack_start(Gtk.Label(label=caption, xalign=0), False, False, 0)
        entry = Gtk.Entry(text=text)
        entry.set_activates_default(True)
        content.pack_start(entry, False, False, 0)
        return entry

    def open_file_node(self, node):
        target = node.file.replace('\\', '/').strip()
        fragment = node.subpath[1:] if node.subpath and node.subpath.startswith('#') else ''
        vault = self.owner.vault
        if target in vault.notes:
            self.owner.open_note(target, fragment)
            return
        matches = vault.resolve(self.path, target)
        if len(matches) == 1:
            self.owner.open_note(matches[0], fragment)
            return
        self.owner.status.set_text(f'No note for {node.file}')

    def zoom_at(self, factor, x, y):
        old = self.scale
        self.scale = max(0.15, min(6.0, old * factor))
        world_x, world_y = (x - self.pan_x) / old, (y - self.pan_y) / old
        self.pan_x = x - world_x * self.scale
        self.pan_y = y - world_y * self.scale
        self.drawing.queue_draw()
        self._status()

    def fit(self, *_):
        width = max(1, self.drawing.get_allocated_width())
        height = max(1, self.drawing.get_allocated_height())
        nodes = [node for node in self.document.nodes if node.width > 0 and node.height > 0]
        if not nodes:
            self.scale, self.pan_x, self.pan_y = 1.0, 48.0, 48.0
        else:
            min_x = min(node.x for node in nodes)
            min_y = min(node.y for node in nodes)
            max_x = max(node.x + node.width for node in nodes)
            max_y = max(node.y + node.height for node in nodes)
            usable_w = max(40, width - 64)
            usable_h = max(40, height - 64)
            self.scale = max(0.15, min(4.0, min(usable_w / max(1, max_x - min_x), usable_h / max(1, max_y - min_y))))
            self.pan_x = (width - (max_x - min_x) * self.scale) / 2 - min_x * self.scale
            self.pan_y = (height - (max_y - min_y) * self.scale) / 2 - min_y * self.scale
        self.drawing.queue_draw()
        self._status()

    def handle_at(self, x, y):
        if not self.selection or self.selection[0] != 'node':
            return None
        node = self.document.node(self.selection[1])
        if node is None:
            return None
        left, top, width, height = self.node_rect(node)
        if math.hypot(x - (left + width), y - (top + height)) <= HANDLE:
            return ('resize', node.id)
        for side in SIDES:
            anchor_x, anchor_y = self.to_screen(*side_anchor(node, side))
            if math.hypot(x - anchor_x, y - anchor_y) <= HANDLE:
                return ('side', node.id, side)
        return None

    def node_at(self, x, y):
        for node in reversed(self.document.nodes):
            left, top, width, height = self.node_rect(node)
            if left <= x <= left + width and top <= y <= top + height:
                return node
        return None

    def curve_points(self, edge):
        start = self.document.node(edge.from_node)
        end = self.document.node(edge.to_node)
        if start is None or end is None:
            return []
        from_side = edge.from_side or facing(start, end)
        to_side = edge.to_side or facing(end, start)
        x1, y1 = self.to_screen(*side_anchor(start, from_side))
        x2, y2 = self.to_screen(*side_anchor(end, to_side))
        direction_x, direction_y = SIDE_VECTOR[from_side]
        back_x, back_y = SIDE_VECTOR[to_side]
        span = max(48, abs(x2 - x1), abs(y2 - y1)) * 0.45
        controls = (x1 + direction_x * span, y1 + direction_y * span, x2 + back_x * span, y2 + back_y * span)
        points = []
        for step in range(17):
            t = step / 16
            inverse = 1 - t
            points.append((
                inverse ** 3 * x1 + 3 * inverse ** 2 * t * controls[0] + 3 * inverse * t ** 2 * controls[2] + t ** 3 * x2,
                inverse ** 3 * y1 + 3 * inverse ** 2 * t * controls[1] + 3 * inverse * t ** 2 * controls[3] + t ** 3 * y2,
            ))
        return points

    def edge_at(self, x, y):
        for edge in reversed(self.document.edges):
            points = self.curve_points(edge)
            if any(segment_distance(*points[index], *points[index + 1], x, y) <= 6 for index in range(len(points) - 1)):
                return edge
        return None

    def pointer_down(self, x, y, event_type=None):
        self.drawing.grab_focus()
        if event_type == Gdk.EventType._2BUTTON_PRESS:
            self.drag = None
            node = self.node_at(x, y)
            if node is not None:
                self.selection = ('node', node.id)
                if node.type == 'file':
                    self.open_file_node(node)
                else:
                    self.edit_node(node)
            self.drawing.queue_draw()
            return
        handle = self.handle_at(x, y)
        if handle and handle[0] == 'resize':
            self.selection = ('node', handle[1])
            self.drag = {'kind': 'resize', 'id': handle[1], 'moved': False}
            return
        if handle and handle[0] == 'side':
            self.selection = ('node', handle[1])
            self.drag = {'kind': 'connect', 'id': handle[1], 'side': handle[2], 'x': x, 'y': y}
            return
        node = self.node_at(x, y)
        if node is not None:
            self.selection = ('node', node.id)
            self.drag = {'kind': 'move', 'id': node.id, 'x': x, 'y': y, 'node_x': node.x, 'node_y': node.y, 'moved': False}
            self.drawing.queue_draw()
            return
        edge = self.edge_at(x, y)
        if edge is not None:
            self.selection = ('edge', edge.id)
            self.drag = None
            self.drawing.queue_draw()
            self._status()
            return
        self.selection = None
        self.drag = {'kind': 'pan', 'x': x, 'y': y, 'pan_x': self.pan_x, 'pan_y': self.pan_y, 'moved': False}
        self.drawing.queue_draw()
        self._status()

    def pointer_move(self, x, y):
        if not self.drag:
            node = self.node_at(x, y)
            tip = None
            if node is not None:
                tip = node.text or node.file or node.url or node.label or node.type
            self.drawing.set_tooltip_text(tip[:240] if tip else None)
            return
        kind = self.drag['kind']
        if kind == 'pan':
            if math.hypot(x - self.drag['x'], y - self.drag['y']) > 3:
                self.drag['moved'] = True
            if self.drag['moved']:
                self.pan_x = self.drag['pan_x'] + (x - self.drag['x'])
                self.pan_y = self.drag['pan_y'] + (y - self.drag['y'])
        elif kind == 'move':
            if math.hypot(x - self.drag['x'], y - self.drag['y']) > 3:
                self.drag['moved'] = True
            if self.drag['moved']:
                dx = (x - self.drag['x']) / self.scale
                dy = (y - self.drag['y']) / self.scale
                move_node(self.document, self.drag['id'], int(round(self.drag['node_x'] + dx)),
                          int(round(self.drag['node_y'] + dy)))
                self.mark_dirty()
        elif kind == 'resize':
            node = self.document.node(self.drag['id'])
            if node is not None:
                world_x, world_y = self.to_world(x, y)
                resize_node(self.document, node.id, max(MIN_SIZE, int(round(world_x - node.x))),
                            max(MIN_SIZE, int(round(world_y - node.y))))
                self.drag['moved'] = True
                self.mark_dirty()
        elif kind == 'connect':
            self.drag['x'], self.drag['y'] = x, y
        self.drawing.queue_draw()

    def pointer_up(self, x, y):
        drag = self.drag
        self.drag = None
        if not drag:
            return
        if drag['kind'] == 'connect':
            target = self.node_at(x, y)
            if target is not None and target.id != drag['id']:
                world_x, world_y = self.to_world(x, y)
                edge = add_edge(self.document, drag['id'], target.id, from_side=drag['side'],
                                to_side=nearest_side(target, world_x, world_y))
                self.selection = ('edge', edge.id)
                self.mark_dirty()
        self.drawing.queue_draw()
        self._status()

    def on_press(self, _, event):
        if event.button != 1:
            return False
        self.pointer_down(event.x, event.y, event.type)
        return True

    def on_release(self, _, event):
        if event.button == 1:
            self.pointer_up(event.x, event.y)
        return True

    def on_motion(self, _, event):
        self.pointer_move(event.x, event.y)
        return True

    def on_scroll(self, _, event):
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            dy = event.get_scroll_deltas()[2]
        elif event.direction == Gdk.ScrollDirection.UP:
            dy = -1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            dy = 1
        else:
            return False
        self.zoom_at(math.exp(-max(-4, min(4, dy)) * 0.15), event.x, event.y)
        return True

    def on_key(self, _, event):
        name = Gdk.keyval_name(event.keyval)
        if name in ('Delete', 'BackSpace'):
            self.delete_selected()
        elif name in ('plus', 'equal', 'KP_Add'):
            self.zoom_at(1.2, *self.view_center())
        elif name in ('minus', 'KP_Subtract'):
            self.zoom_at(1 / 1.2, *self.view_center())
        elif name in ('f', 'F'):
            self.fit()
        elif name in ('Return', 'KP_Enter'):
            self.edit_selected()
        elif name == 'Escape':
            self.selection = None
            self.drag = None
            self.drawing.queue_draw()
            self._status()
        else:
            return False
        return True

    def draw(self, widget, cr):
        color = widget.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        dark = color.red + color.green + color.blue > 1.5
        paper = (0.17, 0.18, 0.20) if dark else (0.99, 0.99, 0.98)
        ink = (0.93, 0.93, 0.94) if dark else (0.15, 0.16, 0.18)
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        selected_edge = self.selection[1] if self.selection and self.selection[0] == 'edge' else None
        for edge in self.document.edges:
            points = self.curve_points(edge)
            if len(points) < 2:
                continue
            tint = paint_color(edge.color) or (color.red, color.green, color.blue)
            highlighted = edge.id == selected_edge
            cr.set_source_rgba(*tint, 0.95 if highlighted else 0.45)
            cr.set_line_width(2.5 if highlighted else 1.4)
            cr.move_to(*points[0])
            for point in points[1:]:
                cr.line_to(*point)
            cr.stroke()
            end = self.document.node(edge.to_node)
            start = self.document.node(edge.from_node)
            if end is not None and edge.to_end != 'none':
                self._arrow(cr, points[-1], points[-2])
            if start is not None and edge.from_end == 'arrow':
                self._arrow(cr, points[0], points[1])
            if edge.label:
                mid = points[len(points) // 2]
                cr.set_source_rgba(*ink, 0.9)
                cr.set_font_size(font_size_pixels(self))
                cr.move_to(mid[0] + 6, mid[1] - 6)
                cr.show_text(edge.label[:80])
        if self.drag and self.drag['kind'] == 'connect':
            node = self.document.node(self.drag['id'])
            if node is not None:
                cr.set_source_rgba(color.red, color.green, color.blue, 0.8)
                cr.set_line_width(1.5)
                cr.move_to(*self.to_screen(*side_anchor(node, self.drag['side'])))
                cr.line_to(self.drag['x'], self.drag['y'])
                cr.stroke()
        for node in self.document.nodes:
            left, top, node_width, node_height = self.node_rect(node)
            if node_width < 1 or node_height < 1 or left > width or top > height or left + node_width < 0 or top + node_height < 0:
                continue
            tint = paint_color(node.color)
            fill = paper
            if tint:
                fill = tuple(channel * 0.72 + dye * 0.28 for channel, dye in zip(paper, tint))
            cr.set_source_rgb(*fill)
            round_rect(cr, left, top, node_width, node_height, 8)
            cr.fill()
            selected = self.selection == ('node', node.id)
            if tint:
                cr.set_source_rgb(*tint)
            elif selected:
                cr.set_source_rgb(0.36, 0.48, 0.92)
            else:
                cr.set_source_rgba(*ink, 0.28)
            cr.set_line_width(2.4 if selected else 1)
            round_rect(cr, left, top, node_width, node_height, 8)
            cr.stroke()
            if node.type == 'text':
                body = node.text
            elif node.type == 'file':
                body = node.file + (f' {node.subpath}' if node.subpath else '')
            elif node.type == 'link':
                body = node.url
            elif node.type == 'group':
                body = node.label or 'Group'
            else:
                body = node.type
            cr.set_source_rgba(*ink, 0.95)
            self._show_wrapped(cr, body, left + 10, top + 8, max(8, node_width - 20), max(8, node_height - 16))
            if selected:
                cr.set_source_rgb(0.36, 0.48, 0.92)
                for side in SIDES:
                    anchor_x, anchor_y = self.to_screen(*side_anchor(node, side))
                    cr.arc(anchor_x, anchor_y, 4.5, 0, math.tau)
                    cr.fill()
                cr.rectangle(left + node_width - 5, top + node_height - 5, 10, 10)
                cr.fill()
        return False

    def _arrow(self, cr, tip, origin):
        angle = math.atan2(tip[1] - origin[1], tip[0] - origin[0])
        length = 11
        cr.move_to(*tip)
        cr.line_to(tip[0] - length * math.cos(angle - 0.4), tip[1] - length * math.sin(angle - 0.4))
        cr.line_to(tip[0] - length * math.cos(angle + 0.4), tip[1] - length * math.sin(angle + 0.4))
        cr.close_path()
        cr.fill()

    def _show_wrapped(self, cr, text, x, y, width, height):
        cr.save()
        cr.rectangle(x, y, max(1, width), max(1, height))
        cr.clip()
        font_size = font_size_pixels(self)
        cr.set_font_size(font_size)
        line_height = max(17, font_size * 1.3)
        lines = []
        for paragraph in str(text).split('\n') or ['']:
            current = ''
            words = paragraph.split(' ') if paragraph else ['']
            for word in words:
                trial = word if not current else f'{current} {word}'
                advance = cr.text_extents(trial)[4]
                if current and advance > width:
                    lines.append(current)
                    current = word
                else:
                    current = trial
            lines.append(current)
        for index, line in enumerate(lines):
            baseline = y + font_size + index * line_height
            if baseline > y + height:
                break
            cr.move_to(x, baseline)
            cr.show_text(line)
        cr.restore()
