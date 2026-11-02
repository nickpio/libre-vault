"""A second view of a canonical note buffer, including native list controls."""
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk


class SplitTextView(Gtk.TextView):
    def __init__(self, pane):
        super().__init__(buffer=pane.buffer)
        self.pane = pane
        self.markers = []
        self.signature = None
        self.pending = None
        self.closed = False
        self.set_wrap_mode(pane.editor.get_wrap_mode())
        self.set_left_margin(pane.editor.get_left_margin())
        self.set_right_margin(24)
        self.set_top_margin(30)
        self.set_bottom_margin(80)
        self.set_pixels_above_lines(5)
        self.get_style_context().add_class('live-editor')
        self.handlers = [(pane.buffer, pane.buffer.connect('changed', self.queue_sync)),
                         (pane.buffer, pane.buffer.connect('mark-set', self.queue_sync)),
                         (pane.mode_selector, pane.mode_selector.connect('changed', self.queue_sync))]
        self.connect('size-allocate', self.queue_sync)
        self.connect('destroy', self.cleanup)
        self.queue_sync()

    def queue_sync(self, *_):
        if not self.closed and self.pending is None:
            # The canonical pane formats first, then this view mirrors its
            # controls without creating another history or formatting owner.
            self.pending = GLib.idle_add(self.sync, priority=GLib.PRIORITY_LOW)

    def sync(self):
        self.pending = None
        if self.closed:
            return False
        signature = (self.pane.formatted_text, self.pane.mode)
        if signature != self.signature:
            self.signature = signature
            for _, widget in self.markers:
                widget.destroy()
            self.markers.clear()
            if self.pane.mode != 'source':
                for item, original in self.pane.list_widgets:
                    if item.task_offset is not None:
                        widget = Gtk.CheckButton()
                        widget.get_style_context().add_class('task-checkbox')
                        widget.set_active(item.checked)
                        widget.get_accessible().set_name(original.get_accessible().get_name())
                        widget.set_tooltip_text(original.get_tooltip_text())
                        widget.task_handler = widget.connect('toggled', self.pane.toggle_task,
                                                             item, self.pane.formatted_text)
                    else:
                        widget = Gtk.Label(label=original.get_text())
                        widget.get_style_context().add_class('list-marker')
                    self.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
                    self.markers.append((item, widget))
        self.set_left_margin(self.pane.editor.get_left_margin())
        self.set_wrap_mode(self.pane.editor.get_wrap_mode())
        for (item, widget), (_, original) in zip(self.markers, self.pane.list_widgets):
            if item.task_offset is not None and widget.get_active() != item.checked:
                widget.handler_block(widget.task_handler)
                widget.set_active(item.checked)
                widget.handler_unblock(widget.task_handler)
            widget.set_visible(original.get_visible())
            if not widget.get_visible():
                continue
            # As in the canonical editor, positions start at the line, avoiding
            # GTK 3's invalid byte indices inside invisible Markdown prefixes.
            position = self.pane.buffer.get_iter_at_offset(item.start)
            rectangle = self.get_iter_location(position)
            x, y = self.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rectangle.x, rectangle.y)
            width = widget.get_preferred_width().natural_width
            height = widget.get_preferred_height().natural_height
            y += (rectangle.height - height) // 2
            widget.set_child_visible(y >= 0 and y + height <= self.get_allocated_height())
            self.move_child(widget, x - width - 8, y)
        return False

    def cleanup(self, *_):
        self.closed = True
        if self.pending is not None:
            GLib.source_remove(self.pending)
            self.pending = None
        for source, handler in self.handlers:
            if source.handler_is_connected(handler):
                source.disconnect(handler)
        self.handlers.clear()
