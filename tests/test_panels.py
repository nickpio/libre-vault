"""Real GTK transitions reclaim editor space and preserve splitter widths."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from librevault.app import Gdk, GLib, Gtk, Window
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

    def test_repeated_toggles_wait_for_the_current_transition(self):
        original = self.widths()
        for opened in (False, True, False, True):
            self.toggle(opened)
            self.wait(30)
        self.wait(260)
        for panel, button in ((self.window.notes_panel, self.window.notes_toggle),
                              (self.window.details_panel, self.window.details_toggle)):
            self.assertFalse(panel.get_visible())
            self.assertFalse(button.get_active())
            self.assertIsNone(panel.tick)
        self.toggle(True)
        self.wait(300)
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
        self.wait(300)
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

    def test_shortcut_spam_keeps_buttons_and_widths_consistent(self):
        original = self.widths()
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.state = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        for opened in (False, True):
            # Several presses before the next frame also cover the opening
            # gap when the panel has not mapped and has no tick callback yet.
            for _ in range(20):
                for key in (Gdk.KEY_E, Gdk.KEY_D):
                    event.keyval = key
                    self.assertTrue(self.window.emit('key-press-event', event))
            self.wait(300)
            for panel, button in ((self.window.notes_panel, self.window.notes_toggle),
                                  (self.window.details_panel, self.window.details_toggle)):
                self.assertEqual(panel.get_child_revealed(), opened)
                self.assertEqual(button.get_active(), opened)
                self.assertIsNone(panel.tick)
        for width, expected in zip(self.widths(), original):
            self.assertAlmostEqual(width, expected, delta=2)
        self.assertEqual(self.window.current().text(), '# Original\n')
        self.assertFalse(self.window.current().dirty)

    def test_short_settling_period_blocks_immediate_restart(self):
        self.toggle(False)
        # Inspect the actual end of the transition, before the next frame.
        for _ in range(100):
            if not (self.window.notes_panel.transitioning or self.window.details_panel.transitioning):
                break
            self.wait(5)
        self.assertFalse(self.window.notes_panel.transitioning)
        self.assertFalse(self.window.details_panel.transitioning)
        self.toggle(True)
        self.assertFalse(self.window.notes_toggle.get_active())
        self.assertFalse(self.window.details_toggle.get_active())
        self.wait(100)
        self.toggle(True)
        self.wait(300)
        self.assertTrue(self.window.notes_panel.get_child_revealed())
        self.assertTrue(self.window.details_panel.get_child_revealed())
