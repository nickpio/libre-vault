"""Draggable workspace tabs with a brief settling animation."""
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk


class WorkspaceTabs(Gtk.Notebook):
    def __init__(self):
        super().__init__(scrollable=True)
        self._animation = None
        self.connect('page-added', self.page_added)
        self.connect('page-reordered', self.page_reordered)
        self.connect('page-removed', self.stop_animation)
        self.connect('unmap', self.stop_animation)
        self.connect('destroy', self.stop_animation)

    def page_added(self, _, page, index):
        self.set_tab_reorderable(page, True)

    def stop_animation(self, *_):
        if self._animation is not None:
            label, callback = self._animation
            label.remove_tick_callback(callback)
            label.set_opacity(1.)
            self._animation = None

    def page_reordered(self, _, page, index):
        self.stop_animation()
        if not self.get_mapped() or not self.get_settings().get_property('gtk-enable-animations'):
            return
        label = self.get_tab_label(page)
        start = self.get_frame_clock().get_frame_time()
        label.set_opacity(.6)

        def settle(widget, clock):
            progress = min(1., (clock.get_frame_time() - start) / 180_000)
            widget.set_opacity(1. - .4 * (1. - progress) ** 3)
            if progress >= 1.:
                self._animation = None
                return False
            return True

        self._animation = label, label.add_tick_callback(settle)
