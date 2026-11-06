"""Real GTK transitions reclaim editor space and preserve splitter widths."""
from tests.gui_wait import wait_for_work
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from librevault.panels import (
        EDITOR_MIN, SIDEBAR_DEFAULT_RATIO, SIDEBAR_MAX, SIDEBAR_OPEN_LIMIT, sidebar_widths,
    )
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False
    sidebar_widths = None


class SidebarWidthTests(unittest.TestCase):
    def setUp(self):
        if sidebar_widths is None:
            self.skipTest('GTK is required to import panel sizing')

    def test_default_widths_track_the_workspace_until_the_outline_cap(self):
        narrow = sidebar_widths(900, SIDEBAR_DEFAULT_RATIO, SIDEBAR_DEFAULT_RATIO, limit=SIDEBAR_OPEN_LIMIT)
        wide = sidebar_widths(1800, SIDEBAR_DEFAULT_RATIO, SIDEBAR_DEFAULT_RATIO, limit=SIDEBAR_OPEN_LIMIT)
        self.assertEqual(narrow, (180, 180))
        self.assertEqual(wide, (SIDEBAR_OPEN_LIMIT, SIDEBAR_OPEN_LIMIT))
        self.assertLess(narrow[0], wide[0])

    def test_dragged_ratios_scale_and_then_leave_the_editor_room(self):
        self.assertEqual(sidebar_widths(1400, 0.31, 0.18), (434, 252))
        squeezed = sidebar_widths(640, 0.31, 0.18)
        self.assertLess(sum(squeezed), 434 + 252)
        self.assertLessEqual(sum(squeezed) + EDITOR_MIN + 10, 640)
        self.assertGreater(squeezed[0], squeezed[1])

    def test_closed_sidebar_adds_no_width(self):
        self.assertEqual(sidebar_widths(1000, 0.2, 0.25, details_open=False, limit=SIDEBAR_MAX), (200, 0))
        self.assertEqual(sidebar_widths(0, 0.2, 0.2), (0, 0))


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
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.wait_for_sidebars()

    def wait_for_sidebars(self):
        previous = None
        for _ in range(40):
            self.wait(40)
            width = self.window.get_allocated_width()
            panels = self.widths()
            if (width == previous and width > 1 and not self.window._fitting_sidebars
                    and self.window.panel_widths_initialized and panels[0] > 1
                    and abs(panels[0] - panels[1]) <= 1):
                return
            previous = width
        self.fail(f'sidebars did not settle: {self.widths()}')

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

    def assert_sidebar_contents_fit(self):
        for panel in (self.window.notes_panel, self.window.details_panel):
            child = panel.get_child()
            self.assertAlmostEqual(child.get_allocated_width(), panel.get_allocated_width(), delta=1)
            self.assertAlmostEqual(child.get_allocated_height(), panel.get_allocated_height(), delta=1)
            self.assertEqual(child.translate_coordinates(panel, 0, 0), (0, 0))
        # The search field and shortcut button must stay inside the file dock.
        for widget in (self.window.search_entry, self.window.file_views, self.window.keybind_hint):
            x, y = widget.translate_coordinates(self.window.left_dock, 0, 0)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + widget.get_allocated_width(), self.window.left_dock.get_allocated_width())
            self.assertLessEqual(y + widget.get_allocated_height(), self.window.left_dock.get_allocated_height())

    def test_details_selector_stays_usable_when_compact_and_keeps_its_state(self):
        self.window.resize(1000, 760)
        self.wait(180)
        self.assertTrue(self.window.details_combo.get_visible())
        self.assertFalse(self.window.details_selector.get_visible())
        self.window.details_combo.set_active_id('properties')
        self.assertEqual(self.window.details_view, 'properties')
        self.assertTrue(self.window.details_buttons['properties'].get_active())
        self.window.resize(1440, 900)
        self.wait(180)
        self.assertFalse(self.window.details_combo.get_visible())
        self.assertTrue(self.window.details_selector.get_visible())
        self.window.details_buttons['links'].set_active(True)
        self.assertEqual(self.window.details_combo.get_active_id(), 'links')
        self.assertTrue(self.window.right_dock.get_show_tabs())

    def test_sidebar_contents_follow_small_window_allocations(self):
        # Shrink after a large allocation to catch revealers retaining their
        # children's natural width, which clips the search field and file tree.
        for width, height in ((1024, 600), (800, 600), (640, 480), (1360, 850)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height)
                self.wait(150)
                self.assertEqual(tuple(self.window.get_size()), (width, height))
                self.assert_sidebar_contents_fit()
                self.assertGreaterEqual(self.window.tabs.get_allocated_width(), EDITOR_MIN)

    def test_sidebar_contents_fit_after_reopening_in_a_smaller_window(self):
        self.toggle(False)
        self.wait(300)
        self.window.resize(800, 600)
        self.wait(150)
        self.toggle(True)
        self.wait(300)
        self.assert_sidebar_contents_fit()

    def test_outline_inset_keeps_panel_background_when_scrollbar_appears(self):
        pane = self.window.current()
        pane.buffer.set_text('\n\n'.join(f'# Heading {index}' for index in range(12)))
        self.window.update_context()
        self.wait(150)
        scroll = self.window.details_scroll
        viewport = scroll.get_child()
        found, color = self.window.context.get_style_context().lookup_color('panel')
        self.assertTrue(found)
        expected = tuple(round(channel * 255) for channel in (color.red, color.green, color.blue))

        for height, scrollable in ((900, False), (480, True), (900, False)):
            with self.subTest(height=height):
                self.window.resize(1000, height)
                self.wait(200)
                adjustment = scroll.get_vadjustment()
                self.assertEqual(adjustment.get_upper() > adjustment.get_page_size(), scrollable)
                adjustment.set_value(0)
                self.wait(50)
                # Capture the native viewport, including the 12px inset that
                # used to turn black when its contents needed scrolling.
                width, height = viewport.get_allocated_width(), viewport.get_allocated_height()
                pixels = Gdk.pixbuf_get_from_window(viewport.get_window(), 0, 0, width, height)
                self.assertIsNotNone(pixels)
                data = pixels.get_pixels()
                for x, y in ((6, 6), (6, height // 2)):
                    offset = y * pixels.get_rowstride() + x * pixels.get_n_channels()
                    self.assertEqual(tuple(data[offset:offset + 3]), expected)

    def test_file_tree_scrolls_to_last_note_in_a_short_window(self):
        from librevault.bookmarks import Bookmark
        root = self.window.vault.root
        for index in range(40):
            (root / f'Note {index:02}.md').write_text('# Note\n')
        self.window.refresh()
        wait_for_work(self.window)
        self.window.bookmark_items = [Bookmark('note', path=f'Note {index:02}.md') for index in range(20)]
        self.window.refresh_bookmark_list()
        self.window.resize(800, 480)
        self.wait(150)
        self.assertEqual(tuple(self.window.get_size()), (800, 480))
        self.assert_sidebar_contents_fit()
        scroll = self.window.file_views.get_visible_child()
        adjustment = scroll.get_vadjustment()
        self.assertGreater(adjustment.get_page_size(), 0)
        self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        self.wait(40)
        last = self.window.files.get_children()[-1]
        self.assertLessEqual(last.get_allocation().y + last.get_allocated_height(),
                             adjustment.get_value() + adjustment.get_page_size() + 1)
        last.activate()
        self.assertEqual(self.window.current().note.path, last.path)

    def test_destroy_cancels_pending_sidebar_callbacks(self):
        self.window.fit_sidebars(self.window.notes_panel.paned.get_allocated_width())
        fit_source = self.window._sidebar_fit_source
        self.window._sidebar_dragging = True
        self.window._sidebar_drag_end(None, type('Event', (), {'button': 1})())
        ratios_source = self.window._sidebar_ratios_source
        self.assertIsNotNone(GLib.MainContext.default().find_source_by_id(fit_source))
        self.assertIsNotNone(GLib.MainContext.default().find_source_by_id(ratios_source))
        self.window.destroy()
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(fit_source))
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(ratios_source))
        self.wait(20)

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

    def test_sidebars_follow_workspace_share_and_a_dragged_ratio(self):
        workspace = self.window.notes_panel.paned.get_allocated_width()
        expected = sidebar_widths(workspace, SIDEBAR_DEFAULT_RATIO, SIDEBAR_DEFAULT_RATIO,
                                  limit=SIDEBAR_OPEN_LIMIT)
        for actual, target in zip(self.widths(), expected):
            self.assertAlmostEqual(actual, target, delta=2)
        self.window.notes_ratio = 0.3
        self.window.details_ratio = 0.3
        self.window.sidebar_custom = True
        self.window.sidebar_span = 0
        self.window.fit_sidebars(workspace)
        self.wait_for_sidebars()
        grown = sidebar_widths(workspace, 0.3, 0.3)
        for actual, target in zip(self.widths(), grown):
            self.assertAlmostEqual(actual, target, delta=2)
        self.assertGreater(grown[0], expected[0])
        self.window.notes_panel.paned.set_position(self.window.notes_panel.paned.get_position() + 36)
        self.wait(40)
        self.window._sidebar_dragging = True
        self.window._sidebar_drag_end(None, type('Event', (), {'button': 1})())
        self.wait(40)
        self.assertTrue(self.window.sidebar_custom)
        self.assertAlmostEqual(self.window.notes_ratio * workspace,
                               self.window.notes_panel.get_allocated_width(), delta=2)
