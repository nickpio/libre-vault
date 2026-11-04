"""Typed property controls, tag tree, graph options, and worker integration."""
from tests.gui_wait import wait_for_work
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

try:
    from librevault.app import Gtk, GLib, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display unavailable')
class PropertyTagUITests(unittest.TestCase):
    def test_controls_registry_tag_tree_and_extended_graph_worker(self):
        from librevault.properties_ui import PropertyForm
        from librevault.metadata import property_mapping
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('---\ntags: [work/project]\n---\n[[Missing]] ![[image.png]]')
            (root / 'B.md').write_text('Other note')
            (root / 'image.png').write_bytes(b'placeholder')
            form = PropertyForm({'title': 'A', 'count': 12345678901234567890,
                                 'done': True, 'due': date(2026, 11, 2),
                                 'items': ['a', 2], 'nested': {'a': [1, 2]}}, root)
            expected = {'title': 'A', 'count': 12345678901234567890, 'done': True,
                        'due': date(2026, 11, 2), 'items': ['a', 2], 'nested': {'a': [1, 2]}}
            self.assertEqual(property_mapping(form.source()), expected)
            form.save_registry()
            self.assertTrue((root / '.librevault/property-types.json').exists())
            row = form.rows[0]
            row[2].set_active_id('list')
            self.assertEqual(property_mapping(form.source())['title'], ['A'])
            form.save_registry()
            restored = PropertyForm({'title': 'B'}, root)
            self.assertEqual(restored.rows[0][2].get_active_id(), 'list')
            form.destroy()
            restored.destroy()
            app = Gtk.Application(application_id='org.librevault.FeatureTests', flags=Gio.ApplicationFlags.NON_UNIQUE)
            app.register(None)
            window = Window(app, root, preferences_path=root / '.settings.json')
            wait_for_work(window)
            self.addCleanup(window.destroy)
            window.show_all()
            window.open_note('A.md')
            window.tag_pane.refresh()
            model = window.tag_pane.store
            first = model.get_iter_first()
            self.assertEqual(model[first][1:], ['work', 1])
            child = model.iter_children(first)
            self.assertEqual(model[child][1:], ['work/project', 1])
            window.tag_pane.activate(None, model.get_path(first), None)
            self.assertEqual(window.search_entry.get_text(), 'tag:#work')
            # Invalid frontmatter must remain available in the YAML view.
            pane = window.current()
            original = pane.text()
            pane.buffer.set_text('---\ninvalid: [\n---\nBody')
            errors = []
            def inspect_dialog():
                dialog = next(widget for widget in Gtk.Window.list_toplevels()
                              if isinstance(widget, Gtk.Dialog) and widget.get_title() == 'Note properties')
                try:
                    def descendants(widget):
                        yield widget
                        if isinstance(widget, Gtk.Container):
                            for child in widget.get_children():
                                yield from descendants(child)
                    notebook = next(widget for widget in descendants(dialog) if isinstance(widget, Gtk.Notebook))
                    self.assertEqual(notebook.get_current_page(), 1)
                    editor = next(widget for widget in descendants(notebook.get_nth_page(1)) if isinstance(widget, Gtk.TextView))
                    buffer = editor.get_buffer()
                    self.assertEqual(buffer.get_text(*buffer.get_bounds(), True), 'invalid: [\n')
                    notebook.set_current_page(0)
                    self.assertEqual(notebook.get_current_page(), 1)
                    buffer.set_text('title: Fixed\n')
                    notebook.set_current_page(0)
                    self.assertEqual(notebook.get_current_page(), 0)
                except Exception as error:
                    errors.append(error)
                finally:
                    dialog.response(Gtk.ResponseType.CANCEL)
                return False
            GLib.idle_add(inspect_dialog)
            window.edit_properties()
            if errors:
                raise errors[0]
            pane.buffer.set_text(original)
            graph = window.show_graph_tab()
            graph.tags.set_active(True)
            graph.attachments.set_active(True)
            graph.unresolved.set_active(True)
            graph.groups = [{'query': 'tag:work', 'color': '#ff0000'}]
            graph.search.set_text('tag:work OR content:absent')
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                while GLib.MainContext.default().pending():
                    GLib.MainContext.default().iteration(False)
                if len(graph.paths) == 4:
                    break
                time.sleep(.01)
            self.assertEqual(graph.paths, ['#work/project', 'A.md', 'ghost:Missing.md', 'image.png'])
            self.assertEqual(graph.colors, {'A.md': '#ff0000'})
            state = graph.snapshot()
            self.assertTrue(state['tags'])
            self.assertTrue(state['attachments'])
            self.assertTrue(state['unresolved'])
            self.assertEqual(state['groups'], graph.groups)
            # Drawing all node variants and arrows must work with Cairo.
            window.show_all()
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            graph.freeze()

            # Bulk rename saves dirty editors and reloads them with renamed tags.
            window.tag_pane.tree.get_selection().select_iter(window.tag_pane.store.get_iter_first())
            def rename_dialog():
                dialog = next(widget for widget in Gtk.Window.list_toplevels()
                              if isinstance(widget, Gtk.Dialog) and widget.get_title() == 'Rename tag across vault')
                entry = next(widget for widget in dialog.get_content_area().get_children() if isinstance(widget, Gtk.Entry))
                entry.set_text('office')
                dialog.response(Gtk.ResponseType.OK)
                return False
            GLib.idle_add(rename_dialog)
            window.tag_pane.rename()
            wait_for_work(window)
            self.assertEqual(window.vault.search('tag:office'), ['A.md'])
            self.assertIn('office/project', pane.text())
