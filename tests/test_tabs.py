"""Workspace tab order, restoration, and animation lifecycle."""
from tests.gui_wait import wait_for_work
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from librevault.app import Window
    from gi.repository import Gio, GLib, Gtk
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display unavailable')
class TabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application(application_id='org.librevault.TabTests',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'A.md').write_text('Alpha')
        (self.root / 'B.md').write_text('Beta')
        (self.root / 'Map.canvas').write_text('{"nodes": [], "edges": []}')
        self.window = self.make_window()

    def make_window(self):
        window = Window(self.app, self.root, preferences_path=self.root / '.settings.json')
        wait_for_work(window)
        self.addCleanup(window.destroy)
        window.show_all()
        return window

    def settle(self, milliseconds=240):
        loop = GLib.MainLoop()
        GLib.timeout_add(milliseconds, lambda: (loop.quit(), False)[1])
        loop.run()

    def test_mixed_tabs_reorder_and_restore_without_changing_active_page(self):
        window = self.window
        alpha = window.open_note('A.md')
        beta = window.open_note('B.md')
        graph = window.show_graph_tab()
        bases = window.show_bases()
        canvas = window.open_canvas('Map.canvas')
        for page in (alpha, beta, graph, bases, canvas):
            self.assertTrue(window.tabs.get_tab_reorderable(page))
        window.tabs.reorder_child(canvas, 0)
        window.tabs.reorder_child(graph, 1)
        window.tabs.reorder_child(beta, 2)
        expected = [canvas, graph, beta, alpha, bases]
        self.assertEqual([window.tabs.get_nth_page(i) for i in range(5)], expected)
        self.assertIs(window.active_page(), canvas)
        saved = window.workspace_store.load(self.root)
        self.assertEqual(saved['tabs'], window.capture_workspace_state()['tabs'])
        window.destroy()
        restored = self.make_window()
        actual = restored.capture_workspace_state()
        self.assertEqual(actual['tabs'], saved['tabs'])
        self.assertEqual(actual['active'], saved['active'])
        for index in range(restored.tabs.get_n_pages()):
            self.assertTrue(restored.tabs.get_tab_reorderable(restored.tabs.get_nth_page(index)))

    def test_animation_settles_and_cancels_on_repeated_moves_and_close(self):
        window = self.window
        alpha = window.open_note('A.md')
        beta = window.open_note('B.md')
        self.settle()
        settings = window.tabs.get_settings()
        original = settings.get_property('gtk-enable-animations')
        self.addCleanup(settings.set_property, 'gtk-enable-animations', original)
        settings.set_property('gtk-enable-animations', True)
        label = window.tabs.get_tab_label(beta)
        window.tabs.reorder_child(beta, 0)
        self.assertLess(label.get_opacity(), 1.)
        self.settle(60)
        self.assertGreater(label.get_opacity(), .6)
        self.assertLess(label.get_opacity(), 1.)
        self.settle()
        self.assertEqual(label.get_opacity(), 1.)
        self.assertIsNone(window.tabs._animation)
        window.tabs.reorder_child(beta, 1)
        window.tabs.reorder_child(alpha, 1)
        self.assertEqual(label.get_opacity(), 1.)
        window.close_note(alpha)
        self.assertIsNone(window.tabs._animation)
        settings.set_property('gtk-enable-animations', False)
        alpha = window.open_note('A.md')
        window.tabs.reorder_child(alpha, 0)
        self.assertIsNone(window.tabs._animation)
        self.assertEqual(window.tabs.get_tab_label(alpha).get_opacity(), 1.)
