"""Lifetime regressions using real GTK objects and weak references."""
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import weakref

try:
    from librevault.app import GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.MemoryTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Heading\n\n- [ ] Task\n')
        self.window = Window(self.app, self.root,
                             preferences_path=Path(self.directory.name) / 'settings.json')
        self.addCleanup(self.window.destroy)
        self.drain()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                break
            context.iteration(False)
        else:
            self.fail('GTK did not settle')
        gc.collect()

    def test_closed_tabs_are_collected(self):
        references = []
        for _ in range(10):
            pane = self.window.open_note('Note.md')
            self.drain()
            references.append(weakref.ref(pane))
            self.window.close_note(pane)
            del pane
            self.drain()
        self.assertTrue(all(reference() is None for reference in references))

    def test_switching_vault_releases_old_panes(self):
        pane = weakref.ref(self.window.current())
        self.window.load_vault(self.root)
        self.drain()
        self.assertIsNone(pane())

    def test_closed_split_is_collected(self):
        self.window.open_split(self.window.current())
        self.drain()
        view = weakref.ref(self.window.split_view)
        self.window.close_split()
        self.drain()
        self.assertIsNone(view())

    def test_leaving_reading_mode_releases_rendered_widgets(self):
        pane = self.window.current()
        pane.set_mode('reading')
        self.drain()
        children = [weakref.ref(child) for child in pane.reading.body.get_children()]
        self.assertTrue(children)
        pane.set_mode('live')
        self.drain()
        self.assertTrue(all(child() is None for child in children))
        self.assertIsNone(pane.reading.source)
        pane.set_mode('reading')
        self.assertTrue(pane.reading.body.get_children())

    def test_destroyed_window_is_collected(self):
        reference = weakref.ref(self.window)
        self.window.destroy()
        self.doCleanups()
        del self.window
        self.drain()
        self.assertIsNone(reference())

    def test_close_reading_tab_with_find_results_and_split(self):
        pane = self.window.current()
        pane.findbar.open()
        pane.findbar.query.set_text('a')
        pane.set_mode('reading')
        self.window.open_split(pane)
        self.drain()
        reference = weakref.ref(pane)
        self.window.close_note(pane)
        del pane
        self.drain()
        self.assertIsNone(reference())

    def test_rebuilt_context_widgets_are_collected(self):
        references = [weakref.ref(child) for child in self.window.context.get_children()]
        self.window.update_context()
        self.drain()
        self.assertTrue(all(reference() is None for reference in references))

    def test_undo_budget_preserves_valid_history_and_bounds_compound_edits(self):
        from librevault.history import BufferHistory
        buffer = Gtk.TextBuffer()
        history = BufferHistory(buffer, max_bytes=1800)
        self.addCleanup(history.close)
        for value in ('a' * 500, 'b' * 500, 'c' * 500):
            buffer.begin_user_action()
            buffer.insert_at_cursor(value)
            buffer.end_user_action()
            self.assertLessEqual(history.retained_bytes, history.max_bytes)
        self.assertTrue(history.undo())
        self.assertEqual(buffer.get_text(*buffer.get_bounds(), True), 'a' * 500 + 'b' * 500)
        self.assertTrue(history.redo())
        self.assertLessEqual(history.retained_bytes, history.max_bytes)
        buffer.begin_user_action()
        for _ in range(8):
            buffer.insert_at_cursor('x' * 500)
            self.assertLessEqual(sum(len(c.inserted) for c in history.pending), history.max_bytes)
        buffer.end_user_action()
        self.assertFalse(history.can_undo)
        self.assertFalse(history.can_redo)
        buffer.insert_at_cursor('end')
        self.assertTrue(history.undo())
        self.assertTrue(buffer.get_text(*buffer.get_bounds(), True).endswith('x' * 500))

    def test_reading_limits_repeated_embeds_and_large_note_previews(self):
        pane = self.window.current()
        (self.root / 'Embedded.md').write_text('small embedded content')
        self.window.vault.update_paths(['Embedded.md'])
        pane.buffer.set_text('![[Embedded]]\n' * 100)
        pane.set_mode('reading')
        self.assertEqual(pane.reading.embed_count, pane.reading.MAX_EMBEDS)
        self.assertLessEqual(pane.reading.embed_bytes, pane.reading.MAX_EMBED_BYTES)
        pane.reading.MAX_EMBED_BYTES = 8
        pane.reading.refresh(force=True)
        self.assertEqual(pane.reading.embed_bytes, 0)
