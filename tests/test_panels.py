"""Real GTK transitions reclaim editor space and preserve splitter widths."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from librevault.app import GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.PanelTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / 'Note.md').write_text('# Original\n')
        settings = Gtk.Settings.get_default()
        animations = settings.get_property('gtk-enable-animations')
        self.addCleanup(settings.set_property, 'gtk-enable-animations', animations)
        settings.set_property('gtk-enable-animations', True)
        self.window = Window(self.app, root)
        self.addCleanup(self.window.destroy)
        self.wait(50)

    def wait(self, milliseconds):
        loop = GLib.MainLoop()
        def finish():
            loop.quit()
            return False
        GLib.timeout_add(milliseconds, finish)
        loop.run()

    def widths(self):
        return [panel.get_allocated_width() for panel in
                (self.window.notes_panel, self.window.details_panel)]

    def toggle(self, opened):
        self.window.notes_toggle.set_active(opened)
        self.window.details_toggle.set_active(opened)

    def test_close_and_open_have_intermediate_widths_and_reclaim_space(self):
        original = self.widths()
        editor_width = self.window.tabs.get_allocated_width()
        self.toggle(False)
        self.wait(60)
        for width, initial in zip(self.widths(), original):
            self.assertGreater(width, 1)
            self.assertLess(width, initial)
        self.wait(200)
        for panel in (self.window.notes_panel, self.window.details_panel):
            self.assertFalse(panel.get_visible())
            self.assertIsNone(panel.tick)
        self.assertGreater(self.window.tabs.get_allocated_width(), editor_width + sum(original) - 10)
        self.toggle(True)
        self.wait(60)
        for width, initial in zip(self.widths(), original):
            self.assertGreater(width, 1)
            self.assertLess(width, initial)
        self.wait(200)
        for width, initial in zip(self.widths(), original):
            self.assertAlmostEqual(width, initial, delta=2)
        self.assertFalse(self.window.current().dirty)

    def test_quick_repeated_toggles_end_in_the_requested_state(self):
        original = self.widths()
        for opened in (False, True, False, True):
            self.toggle(opened)
            self.wait(30)
        self.wait(220)
        for panel in (self.window.notes_panel, self.window.details_panel):
            self.assertTrue(panel.get_child_revealed())
            self.assertTrue(panel.get_visible())
            self.assertIsNone(panel.tick)
        for width, initial in zip(self.widths(), original):
            self.assertAlmostEqual(width, initial, delta=2)

    def test_splitter_widths_are_restored_after_animation(self):
        left = self.window.notes_panel.paned
        right = self.window.details_panel.paned
        left.set_position(left.get_position() + 40)
        right.set_position(right.get_position() - 30)
        self.wait(40)
        resized = self.widths()
        self.toggle(False)
        self.wait(220)
        self.toggle(True)
        self.wait(220)
        for width, expected in zip(self.widths(), resized):
            self.assertAlmostEqual(width, expected, delta=2)

    def test_disabled_animations_settle_without_frame_callbacks(self):
        Gtk.Settings.get_default().set_property('gtk-enable-animations', False)
        self.toggle(False)
        self.wait(30)
        for panel in (self.window.notes_panel, self.window.details_panel):
            self.assertFalse(panel.get_visible())
            self.assertIsNone(panel.tick)
        self.toggle(True)
        self.wait(40)
        for panel in (self.window.notes_panel, self.window.details_panel):
            self.assertTrue(panel.get_child_revealed())
            self.assertIsNone(panel.tick)

    def test_destroy_during_transition_removes_frame_callbacks(self):
        panels = (self.window.notes_panel, self.window.details_panel)
        self.toggle(False)
        self.wait(30)
        self.window.destroy()
        self.assertTrue(all(panel.tick is None for panel in panels))
