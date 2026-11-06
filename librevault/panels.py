"""Sidebar widths that follow the window, plus slide transitions for the splitters."""
import time

import cairo
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

# Share of the workspace used until someone drags a splitter.
SIDEBAR_DEFAULT_RATIO = 0.2
# The outline stays compact on a large window until the splitter is dragged.
SIDEBAR_OPEN_LIMIT = 300
SIDEBAR_MAX = 520
# Keep this much of the workspace for the note when both sidebars are open.
EDITOR_MIN = 320
SPLITTER_HANDLE = 5


def sidebar_widths(workspace, notes_ratio, details_ratio, notes_open=True, details_open=True,
                   limit=SIDEBAR_MAX):
    """Pixel widths for the file tree and the details sidebar.

    Ratios are fractions of the workspace. Open sidebars shrink together when
    the window is too narrow to keep the editor readable.
    """
    if workspace <= 0:
        return (0, 0)
    slots = []
    if notes_open:
        slots.append(notes_ratio)
    if details_open:
        slots.append(details_ratio)
    if not slots:
        return (0, 0)
    chosen = [min(limit, max(0, int(round(max(0.0, ratio) * workspace)))) for ratio in slots]
    room = max(0, workspace - SPLITTER_HANDLE * len(slots) - min(EDITOR_MIN, max(0, workspace)))
    total = sum(chosen)
    if total > room:
        if room <= 0 or total <= 0:
            chosen = [0 for _ in chosen]
        else:
            scaled = [int(width * room / total) for width in chosen]
            spare = room - sum(scaled)
            for index in range(spare):
                scaled[index % len(scaled)] += 1
            chosen = scaled
    widths = [0, 0]
    cursor = 0
    if notes_open:
        widths[0] = chosen[cursor]
        cursor += 1
    if details_open:
        widths[1] = chosen[cursor]
    return tuple(widths)


class SlidingPanel(Gtk.Revealer):
    def __init__(self, child, paned, at_start):
        super().__init__()
        self.paned = paned
        self.at_start = at_start
        self.tick = None
        self.opened = True
        self.transitioning = False
        self.accept_after = 0
        self.handle_width = paned.style_get_property('handle-size')
        self.original_request = child.get_size_request()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT if at_start
                                 else Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.set_transition_duration(160)
        self.set_reveal_child(True)
        self.add(child)
        self.connect_after('draw', self.draw_inner_border)
        self.connect_after('map', self.mapped)
        self.connect('destroy', self.cleanup)

    def do_size_allocate(self, allocation):
        Gtk.Revealer.do_size_allocate(self, allocation)
        if self.transitioning or not self.get_child_revealed():
            return
        # GtkRevealer keeps its child at least as large as its natural size,
        # even when fully open. Let the dock use the available space instead,
        # so entries shrink and scrolled lists scroll on small windows.
        child = self.get_child()
        if self.get_realized():
            # The revealer's bin window also retains the natural-width slide
            # offset. Resize and align it after GTK's default allocation.
            child.get_parent_window().move_resize(0, 0, allocation.width, allocation.height)
        child_allocation = child.get_allocation()
        if (child_allocation.width != allocation.width
                or child_allocation.height != allocation.height):
            # Slide-right revealers right-align an oversized natural child.
            # Reset that offset when constraining its width, or the leading
            # edge remains outside the panel after a window resize.
            child_allocation.x = 0
            child_allocation.y = 0
            child_allocation.width = allocation.width
            child_allocation.height = allocation.height
            child.size_allocate(child_allocation)

    def draw_inner_border(self, _, context):
        width, height = self.get_allocated_width(), self.get_allocated_height()
        _, color = self.get_style_context().lookup_color('panel_border')
        gradient = cairo.LinearGradient(0, 0, 0, height)
        gradient.add_color_stop_rgba(0, color.red, color.green, color.blue, 0)
        gradient.add_color_stop_rgba(1, color.red, color.green, color.blue, 0.12)
        context.set_source(gradient)
        context.rectangle(width - 1 if self.at_start else 0, 0, 1, height)
        context.fill()
        return False

    def set_open(self, opened):
        if opened == self.opened:
            return self.opened
        if self.transitioning or time.monotonic() < self.accept_after:
            return self.opened
        self.transitioning = True
        self.opened = opened
        self.show()
        if opened and not self.get_mapped():
            # A zero-width child of Gtk.Paned cannot map. Reserve one pixel
            # so GTK can map it before starting the opening transition.
            self.set_size_request(1, -1)
            self.position_width(1)
            return self.opened
        self.animate(opened)
        return self.opened

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
        self.finish(animated=True)
        return False

    def finish(self, animated=False):
        if self.tick is not None:
            animated = True
            self.remove_tick_callback(self.tick)
            self.tick = None
        self.sync_width()
        self.set_size_request(-1, -1)
        if self.get_reveal_child():
            self.get_child().set_size_request(*self.original_request)
        else:
            # Hiding the collapsed wrapper also removes the splitter handle.
            self.hide()
        self.transitioning = False
        self.queue_resize()
        if animated:
            # Let the final allocation settle before capturing a width for
            # another transition. Auto-repeat must not restart it mid-layout.
            self.accept_after = time.monotonic() + 0.06

    def cleanup(self, *_):
        if self.tick is not None:
            self.remove_tick_callback(self.tick)
            self.tick = None
        self.transitioning = False
