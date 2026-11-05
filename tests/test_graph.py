"""Graph resolution, filtering, bounded layout and real GTK worker lifecycle."""
from tests.gui_wait import wait_for_work
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest

from librevault.graph import build_graph, Layout
from librevault.vault import Vault


class GraphTests(unittest.TestCase):
    def test_saved_links_match_vault_resolution_without_loading_text(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sub').mkdir()
            (root / 'A.md').write_text('[[B]] [[Bee]] [[sub/C]] [[missing]] [[B]] [web](https://example.com)')
            (root / 'B.md').write_text('---\naliases: [Bee]\n---\n[[A]]')
            (root / 'sub/C.md').write_text('[[../A]] [[A#heading]]')
            (root / 'Alone.md').write_text('isolated')
            vault = Vault(root, cache_dir=root / '.cache')
            self.addCleanup(vault.close)
            rows = list(vault.index.connection.execute('SELECT path,aliases,links FROM notes'))
            paths, edges = build_graph(rows)
            actual = {tuple(sorted((paths[a], paths[b]))) for a, b in edges}
            expected = set()
            for source, note in vault.notes.items():
                for link in vault._saved_links(note):
                    targets = vault.resolve(source, link.target)
                    if len(targets) == 1 and targets[0] != source:
                        expected.add(tuple(sorted((source, targets[0]))))
            self.assertEqual(actual, expected)
            self.assertEqual(build_graph(rows, root='B.md', depth=1)[0], ['A.md', 'B.md'])
            self.assertEqual(build_graph(rows, root='B.md', depth=2)[0], ['A.md', 'B.md', 'sub/C.md'])
            self.assertNotIn('Alone.md', build_graph(rows, orphans=False)[0])
            self.assertEqual(build_graph(rows, query='bee')[0], ['B.md'])
            self.assertEqual(build_graph(rows, query='nothing'), ([], []))

    def test_ambiguous_names_and_aliases_do_not_create_edges(self):
        rows = [(p, json.dumps(a), json.dumps([{'target': t} for t in targets])) for p, a, targets in (
            ('A.md', [], ['Twin', 'alias', 'folder/Twin']),
            ('folder/Twin.md', ['alias'], []), ('other/Twin.md', ['alias'], []))]
        paths, edges = build_graph(rows)
        self.assertEqual([(paths[a], paths[b]) for a, b in edges], [('A.md', 'folder/Twin.md')])

    def test_layout_is_finite_and_stops(self):
        layout = Layout(1000, [(i, i+1) for i in range(999)])
        for _ in range(161):
            if not layout.step():
                break
        else:
            self.fail('layout did not settle within its work budget')
        self.assertTrue(all(math.isfinite(v) for p in layout.positions for v in p))
        self.assertFalse(Layout(0, []).step())


try:
    from librevault.app import Gdk, Gtk, GLib, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display unavailable')
class GraphUITests(unittest.TestCase):
    def pump_until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while GLib.MainContext.default().pending():
                GLib.MainContext.default().iteration(False)
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for graph worker')

    def test_graph_worker_navigation_filter_refresh_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('[[B]]')
            (root / 'B.md').write_text('hello')
            (root / 'Unreadable.md').write_bytes(b'\xff')
            app = Gtk.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
            app.register(None)
            window = Window(app, root, preferences_path=root / '.settings.json')
            wait_for_work(window)
            self.addCleanup(window.destroy)
            window.show_all()
            window.open_note('A.md')
            window.show_graph()
            graph = window.graph_window.graph
            self.pump_until(lambda: bool(graph.paths))
            self.assertEqual(graph.paths, ['A.md', 'B.md'])
            self.assertEqual(len(graph.edges), 1)
            graph.open_node(graph.paths.index('B.md'))
            self.assertEqual(window.current().note.path, 'B.md')
            graph.search.set_text('A')
            self.pump_until(lambda: graph.paths == ['A.md'])
            graph.search.set_text('')
            (root / 'C.md').write_text('[[A]]')
            window.refresh()
            wait_for_work(window)
            self.pump_until(lambda: len(graph.paths) == 3)
            self.pump_until(lambda: graph.process is None)
            self.assertIsNone(graph.poll_source)
            old_scale = graph.scale
            graph.zoom(1.25, 200, 200)
            self.assertGreater(graph.scale, old_scale)
            point = graph.screen(graph.positions[0])
            self.assertEqual(graph.hit(*point), 0)
            original = tuple(graph.positions[0])
            graph.press(None, SimpleNamespace(button=1, x=point[0], y=point[1]))
            graph.motion(None, SimpleNamespace(x=point[0]+50, y=point[1]+30))
            graph.release(None, SimpleNamespace(button=1))
            self.assertNotEqual(tuple(graph.positions[0]), original)
            self.assertEqual(window.current().note.path, 'B.md')
            pane = window.current()
            pane.buffer.set_text('[[C]]')
            pane.save()
            self.pump_until(lambda: graph.edges and len(graph.edges) == 3)
            window.graph_window.destroy()
            self.assertIsNone(window.graph_window)
            self.assertIsNone(graph.poll_source)
            self.assertIsNone(graph.reload_source)
            self.assertIsNone(graph.process)
            window.show_local_graph()
            self.pump_until(lambda: bool(window.graph_window.graph.paths))
            self.assertEqual(window.graph_window.graph.paths, ['A.md', 'B.md', 'C.md'])
            window.release_vault()
            self.assertIsNone(window.graph_window)

    def make_window(self, root):
        app = Gtk.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        window = Window(app, root, preferences_path=root / '.settings.json')
        wait_for_work(window)
        self.addCleanup(window.destroy)
        window.show_all()
        return window

    def test_graph_tabs_split_navigation_and_workspace_restore(self):
        from librevault.graph_ui import GraphView
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('[[B]]')
            (root / 'B.md').write_text('[[C]]')
            (root / 'C.md').write_text('end')
            window = self.make_window(root)
            note = window.open_note('A.md')
            graph = window.show_local_graph_tab()
            self.pump_until(lambda: len(graph.paths) == 2)
            self.assertIsNone(window.current())
            self.assertIs(note.toolbar.get_parent(), note)
            self.assertFalse(note.toolbar.get_mapped())
            window.save_current()  # Graph tabs must not enter the note save path.
            window.toggle_split()
            split = window.split_view
            self.assertIsInstance(split, GraphView)
            window.open_note('A.md')
            self.pump_until(lambda: len(split.paths) == 2)
            self.assertIsNone(graph.process)  # Hidden tabs stop their layout worker.
            split.open_node(split.paths.index('B.md'))
            self.assertEqual(window.current().note.path, 'B.md')
            self.assertIs(window.split_view, split)
            split.depth.set_value(2)
            self.pump_until(lambda: len(split.paths) == 3)
            graph.search.set_text('A')
            window.persist_workspace()
            saved = window.workspace_store.load(root)
            self.assertEqual(saved['split']['graph'], graph.workspace_id)
            self.assertEqual(saved['split']['state']['depth'], 2)
            window.destroy()
            self.assertFalse(window.graph_views)
            self.assertIsNone(split.process)
            restored = self.make_window(root)
            self.assertIsInstance(restored.split_view, GraphView)
            self.pump_until(lambda: len(restored.split_view.paths) == 3)
            self.assertEqual(restored.current().note.path, 'B.md')
            restored_graph = restored.split_pane
            self.assertEqual(restored_graph.search.get_text(), 'A')
            restored.split_view.canvas.grab_focus()
            restored.close_tab()
            self.assertIsNone(restored.split_view)
            self.assertNotIn(restored_graph, restored.graph_views)
            self.assertEqual(len(restored.graph_views), 0)
            self.assertIn('A.md', restored.panes)

    def test_ctrl_alt_g_toggles_the_graph_window_like_the_header_button(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('[[B]]')
            (root / 'B.md').write_text('hello')
            window = self.make_window(root)
            event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
            event.keyval = Gdk.KEY_g
            event.state = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
            self.assertTrue(window.emit('key-press-event', event))
            self.assertIsNotNone(window.graph_window)
            # The canvas does not consume the shortcut, and the graph window
            # closes itself because it now has keyboard focus.
            self.assertFalse(window.graph_window.graph.canvas.emit('key-press-event', event))
            self.assertIsNotNone(window.graph_window)
            self.assertTrue(window.graph_window.emit('key-press-event', event))
            self.assertIsNone(window.graph_window)
            self.assertTrue(window.emit('key-press-event', event))
            graph = window.graph_window
            # The shortcut still toggles when the graph's search field is focused.
            graph.graph.search.grab_focus()
            self.assertTrue(graph.emit('key-press-event', event))
            self.assertIsNone(window.graph_window)

    def test_main_graph_keeps_clicked_note_in_split_and_window_can_be_tabbed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'A.md').write_text('[[B]]')
            (root / 'B.md').write_text('hello')
            window = self.make_window(root)
            note = window.open_note('A.md')
            window.toggle_split()
            window.show_graph()
            floating = window.graph_window
            floating.open_as_tab(window)
            self.assertIsNone(window.graph_window)
            graph = window.tabs.get_nth_page(window.tabs.get_current_page())
            self.pump_until(lambda: len(graph.paths) == 2)
            graph.open_node(graph.paths.index('B.md'))
            self.assertIs(window.tabs.get_nth_page(window.tabs.get_current_page()), graph)
            self.assertEqual(window.split_pane.note.path, 'B.md')
            self.assertEqual(window.current().note.path, 'B.md')
            self.assertIs(note.toolbar.get_parent(), note)
            self.assertFalse(note.toolbar.get_mapped())
            graph.canvas.grab_focus()
            window.persist_workspace()
            self.assertEqual(window.workspace_store.load(root)['active'], {'graph': graph.workspace_id})
            window.destroy()
            restored = self.make_window(root)
            current = restored.tabs.get_nth_page(restored.tabs.get_current_page())
            self.assertEqual(current.workspace_id, graph.workspace_id)
            self.assertEqual(restored.split_pane.note.path, 'B.md')
            restored.release_vault()
            self.assertFalse(restored.graph_views)
            self.assertIsNone(current.process)
