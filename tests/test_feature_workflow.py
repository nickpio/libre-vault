"""Daily-use features cooperate across source buffers and real vault files."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from librevault.vault import links

try:
    from librevault.app import GLib, Gtk, Window
    from gi.repository import GdkPixbuf, Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK and a display are required')
class FeatureWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.FeatureWorkflowTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def settle(self):
        loop = GLib.MainLoop()
        def stop():
            loop.quit()
            return False
        GLib.timeout_add(150, stop)
        loop.run()

    def descendants(self, widget):
        yield widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                yield from self.descendants(child)

    def test_organize_search_read_split_and_restore_the_same_note(self):
        with TemporaryDirectory() as directory:
            environment = patch.dict(os.environ, {'XDG_STATE_HOME': str(Path(directory) / 'state')})
            environment.start()
            self.addCleanup(environment.stop)
            root = Path(directory) / 'vault'
            (root / 'Projects').mkdir(parents=True)
            (root / 'Archive' / 'Completed').mkdir(parents=True)
            (root / 'Reference.md').write_text('# Reference\n\nShared reference text.\n')
            (root / 'Index.md').write_text('# Index\n\n[[Projects/Draft]]\n')
            (root / 'Projects' / 'Draft.md').write_text(
                '---\naliases: [Blueprint]\ntags: [work]\nstatus: draft\n---\n'
                '# Draft\n\n| Name | Value |\n| --- | --- |\n| One | Two |\n\n![[Reference]]\n\n')
            preferences = Path(directory) / 'config' / 'settings.json'
            window = Window(self.app, preferences_path=preferences)
            self.addCleanup(window.destroy)
            self.assertEqual(window.workspace_stack.get_visible_child_name(), 'welcome')
            window.load_vault(root)
            window.show_quick_switcher()
            switcher = window.quick_switcher
            switcher.search.set_text('blueprint')
            switcher.run_selected()
            self.settle()
            pane = window.current()
            self.assertEqual(pane.note.path, 'Projects/Draft.md')

            image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 16, 12)
            image.fill(0x3070c0ff)
            _, data = image.save_to_bufferv('png', [], [])
            pane.buffer.place_cursor(pane.buffer.get_end_iter())
            attachment = pane.insert_attachment_bytes('Diagram.png', data)
            self.assertEqual((root / attachment).read_bytes(), data)
            window.set_note_properties('aliases: [Blueprint]\ntags: [work, done]\nstatus: complete\n')
            pane.save()
            self.assertEqual(window.vault.search('tag:done property:status=complete'), ['Projects/Draft.md'])

            window.open_split(pane)
            before = pane.text()
            window.split_view.get_buffer().insert_at_cursor('temporary split edit')
            self.assertIn('temporary split edit', pane.text())
            pane.undo()
            self.assertEqual(pane.text(), before)
            self.assertIs(window.split_view.get_buffer(), pane.buffer)

            destination = 'Archive/Completed/Final.md'
            self.assertTrue(window.move_path('Projects/Draft.md', destination))
            self.assertIs(window.panes[destination], pane)
            self.assertEqual(window.vault.resolve('Index.md', links(window.vault.read('Index.md').text)[0].target),
                             [destination])
            self.assertEqual(window.vault.search('alias:Blueprint tag:done'), [destination])
            pane.set_mode('reading')
            widgets = list(self.descendants(pane.reading.body))
            self.assertTrue(any(isinstance(widget, Gtk.Image) for widget in widgets))
            self.assertTrue(any(isinstance(widget, Gtk.Grid) for widget in widgets))
            self.assertTrue(any(isinstance(widget, Gtk.Label) and widget.get_text().strip() == 'Shared reference text.'
                                for widget in widgets))
            pane.set_mode('source')
            pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(7))
            window.open_note('Index.md')
            window.open_note(destination)
            window.persist_workspace()
            expected_tabs = [window.tabs.get_nth_page(i).note.path for i in range(window.tabs.get_n_pages())]
            window.destroy()

            restored = Window(self.app, preferences_path=preferences)
            self.addCleanup(restored.destroy)
            self.assertIsNone(restored.vault)
            restored.load_vault(root)
            self.settle()
            self.assertEqual([restored.tabs.get_nth_page(i).note.path for i in range(restored.tabs.get_n_pages())], expected_tabs)
            self.assertEqual(restored.current().note.path, destination)
            current = restored.panes[destination]
            self.assertEqual(current.buffer.get_iter_at_mark(current.buffer.get_insert()).get_offset(), 7)
            self.assertIs(restored.split_view.get_buffer(), current.buffer)
            self.assertTrue(restored.trash_path(destination))
            self.assertNotIn(destination, restored.panes)
            self.assertFalse((root / destination).exists())
            self.assertTrue(any(path.read_text().startswith('---\n') for path in (root / '.trash').iterdir()))
            self.assertTrue((root / attachment).exists())
            restored.destroy()

