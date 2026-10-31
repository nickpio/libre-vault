"""Short native GTK slide transitions that preserve resizable panel widths."""
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk


class SlidingPanel(Gtk.Revealer):
    def __init__(self, child, paned, at_start):
        super().__init__()
        self.paned = paned
        self.at_start = at_start
        self.tick = None
        self.opened = True
        self.handle_width = paned.style_get_property('handle-size')
        self.original_request = child.get_size_request()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT if at_start
                                 else Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.set_transition_duration(160)
        self.set_reveal_child(True)
        self.add(child)
        self.connect_after('map', self.mapped)
        self.connect('destroy', self.cleanup)

    def set_open(self, opened):
        if opened == self.opened:
            return
        self.opened = opened
        self.show()
        if opened and not self.get_mapped():
            # A zero-width child of Gtk.Paned cannot map. Reserve one pixel
            # so GTK can map it before starting the opening transition.
            self.set_size_request(1, -1)
            self.position_width(1)
            return
        self.animate(opened)

    def mapped(self, *_):
        if self.opened and not self.get_reveal_child():
            self.animate(True)

    def animate(self, opened):
        if opened == self.get_reveal_child():
            if self.tick is None:
                self.finish()
            return
        if self.tick is None and self.get_child_revealed() and self.get_mapped():
            self.handle_width = max(0, self.paned.get_allocated_width()
                                    - self.paned.get_child1().get_allocated_width()
                                    - self.paned.get_child2().get_allocated_width())
            # Keep text/widgets at their full width while the revealer clips
            # them, and remember a width chosen by dragging the splitter.
            width = self.get_allocated_width()
            self.get_child().set_size_request(width, self.original_request.height)
            # Some themes add padding outside a scrolled window's request.
            excess = self.get_preferred_width().minimum_width - width
            if excess > 0:
                self.get_child().set_size_request(max(1, width - excess), self.original_request.height)
        self.set_reveal_child(opened)
        self.sync_width()
        if self.get_child_revealed() == opened:
            self.finish()
        elif self.tick is None:
            self.tick = self.add_tick_callback(self.advance)

    def sync_width(self):
        self.position_width(self.get_preferred_width().minimum_width)

    def position_width(self, width):
        if self.at_start:
            position = width
        else:
            position = self.paned.get_allocated_width() - self.handle_width - width
        self.paned.set_position(max(0, position))

    def advance(self, *_):
        self.sync_width()
        if self.get_child_revealed() != self.get_reveal_child():
            return True
        self.tick = None
        self.finish()
        return False

    def finish(self):
        if self.tick is not None:
            self.remove_tick_callback(self.tick)
            self.tick = None
        self.sync_width()
        self.set_size_request(-1, -1)
        if self.get_reveal_child():
            self.get_child().set_size_request(*self.original_request)
        else:
            # Hiding the collapsed wrapper also removes the splitter handle.
            self.hide()

    def cleanup(self, *_):
        if self.tick is not None:
            self.remove_tick_callback(self.tick)
            self.tick = None
