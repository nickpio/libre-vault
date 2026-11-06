"""Text views with safe GTK 3 hit-testing around concealed Markdown."""
from gi.repository import Gdk, Gtk


class EditorTextView(Gtk.TextView):
    def _hit_y(self, y):
        # GTK 3's layout hit-test uses a raw byte count as a *visible* index
        # below a paragraph. Hidden syntax then takes it past the line and
        # aborts the process. This also occurs below a trailing newline, even
        # with pixels-below-lines=0. Keep hits inside the chosen paragraph.
        iterator, _ = self.get_line_at_y(y)
        end = iterator.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        # Use a visible character on the last wrapped row, rather than a
        # concealed closing delimiter or the following paragraph.
        last = end.copy()
        if last.backward_visible_cursor_position() and last.get_line() == iterator.get_line():
            iterator = last
        # get_iter_location() passes the raw buffer byte index to Pango,
        # whose layout excludes hidden text. Its rectangle can be empty or
        # on the wrong wrapped row. Cursor locations use the visible index.
        rectangle, _ = self.get_cursor_locations(iterator)
        return min(y, rectangle.y + rectangle.height - 1)

    def get_iter_at_location(self, x, y):
        safe_y = self._hit_y(y)
        found, iterator = super().get_iter_at_location(x, safe_y)
        return found and safe_y == y, iterator

    def get_iter_at_position(self, x, y):
        safe_y = self._hit_y(y)
        found, iterator, trailing = super().get_iter_at_position(x, safe_y)
        return found and safe_y == y, iterator, trailing

    def do_event(self, event):
        # Native GTK tag dispatch and selection bypass the public methods.
        # Adjust their shared event too; navigation inspects the original
        # coordinates in the event signal before this default handler.
        if (event.type in (Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.BUTTON_PRESS,
                           Gdk.EventType.DOUBLE_BUTTON_PRESS, Gdk.EventType.TRIPLE_BUTTON_PRESS,
                           Gdk.EventType.BUTTON_RELEASE)
                and event.window == self.get_window(Gtk.TextWindowType.TEXT)):
            x, y = self.window_to_buffer_coords(Gtk.TextWindowType.TEXT, int(event.x), int(event.y))
            safe_y = self._hit_y(y)
            if safe_y != y:
                event.y += safe_y - y
        return Gtk.TextView.do_event(self, event)
