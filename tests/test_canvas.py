"""JSON Canvas documents, vault files, and the canvas workspace tab."""
from tests.gui_wait import wait_for_work
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.canvas import (
    CanvasError, add_edge, add_node, canvas_path, move_node, parse_canvas, remove_edge, remove_node,
    resize_node, serialize_canvas, update_edge, update_node,
)
from librevault.vault import Conflict, Vault, VaultError


class CanvasDocumentTests(unittest.TestCase):
    def test_round_trip_preserves_unknown_fields_and_supported_cards(self):
        raw = {
            'plugin': {'version': 2},
            'nodes': [
                {'id': 'text-1', 'type': 'text', 'x': 1, 'y': 2, 'width': 30, 'height': 40,
                 'color': '1', 'text': 'Hello **canvas**', 'custom': [1, {'ok': True}]},
                {'id': 'file-1', 'type': 'file', 'x': 5, 'y': 6, 'width': 70, 'height': 80,
                 'file': 'Projects/Plan.md', 'subpath': '#Plan', 'color': '#0af'},
                {'id': 'link-1', 'type': 'link', 'x': -4, 'y': 0, 'width': 10, 'height': 12,
                 'url': 'https://example.com', 'color': '6'},
                {'id': 'group-1', 'type': 'group', 'x': 0, 'y': 0, 'width': 400, 'height': 300,
                 'label': 'Cluster', 'background': 'attachments/bg.png', 'backgroundStyle': 'cover',
                 'mystery': None},
                {'id': 'future-1', 'type': 'embed', 'x': 8, 'y': 9, 'width': 10, 'height': 11, 'payload': 'keep'},
            ],
            'edges': [
                {'id': 'edge-1', 'fromNode': 'text-1', 'fromSide': 'right', 'fromEnd': 'none',
                 'toNode': 'file-1', 'toSide': 'left', 'toEnd': 'arrow', 'label': 'cites', 'color': '4',
                 'extraFlag': True},
                {'id': 'edge-2', 'fromNode': 'missing', 'toNode': 'also-missing', 'fromSide': 'sideways'},
            ],
        }
        document = parse_canvas(json.dumps(raw))
        self.assertEqual(document.extra['plugin']['version'], 2)
        self.assertEqual(document.nodes[0].text, 'Hello **canvas**')
        self.assertEqual(document.nodes[0].extra['custom'], [1, {'ok': True}])
        self.assertEqual(document.nodes[1].file, 'Projects/Plan.md')
        self.assertEqual(document.nodes[1].subpath, '#Plan')
        self.assertEqual(document.nodes[2].url, 'https://example.com')
        self.assertEqual(document.nodes[3].background_style, 'cover')
        self.assertIsNone(document.nodes[3].extra['mystery'])
        self.assertEqual(document.nodes[4].type, 'embed')
        self.assertEqual(document.nodes[4].extra['payload'], 'keep')
        self.assertEqual(document.edges[0].label, 'cites')
        self.assertTrue(document.edges[0].extra['extraFlag'])
        self.assertIsNone(document.edges[1].from_side)
        self.assertEqual(document.edges[1].extra['fromSide'], 'sideways')
        self.assertEqual(document.edges[1].to_end, None)
        again = json.loads(serialize_canvas(document))
        self.assertEqual(again['plugin'], raw['plugin'])
        self.assertEqual(again['nodes'][0]['custom'], raw['nodes'][0]['custom'])
        self.assertEqual(again['nodes'][1]['subpath'], '#Plan')
        self.assertEqual(again['nodes'][3]['backgroundStyle'], 'cover')
        self.assertEqual(again['nodes'][4]['payload'], 'keep')
        self.assertEqual(again['edges'][0]['extraFlag'], True)
        self.assertEqual(again['edges'][1]['fromSide'], 'sideways')
        self.assertNotIn('toEnd', again['edges'][1])

    def test_empty_document_and_rejects_malformed_cards(self):
        document = parse_canvas('{"nodes": [], "edges": []}')
        self.assertEqual(json.loads(serialize_canvas(document)), {'nodes': [], 'edges': []})
        document = parse_canvas('{}')
        self.assertEqual((document.nodes, document.edges), ([], []))
        for text in ('[]', '{', '{"nodes": {}}', '{"nodes": [1]}',
                     '{"nodes": [{"id": "a", "type": "text", "x": 1.2, "y": 0, "width": 1, "height": 1, "text": "a"}]}',
                     '{"nodes": [{"id": "a", "type": "text", "x": true, "y": 0, "width": 1, "height": 1, "text": "a"}]}',
                     '{"nodes": [{"id": "", "type": "text", "x": 0, "y": 0, "width": 1, "height": 1, "text": "a"}]}',
                     '{"nodes": [{"id": "a", "type": "text", "x": 0, "y": 0, "width": 1, "height": 1}]}',
                     '{"nodes": [{"id": "a", "type": "file", "x": 0, "y": 0, "width": 1, "height": 1, "file": ""}]}',
                     '{"nodes": [{"id": "a", "type": "file", "x": 0, "y": 0, "width": 1, "height": 1, "file": "A.md", "subpath": "heading"}]}',
                     '{"nodes": [{"id": "a", "type": "link", "x": 0, "y": 0, "width": 1, "height": 1}]}',
                     '{"nodes": [{"id": "a", "type": "text", "x": 0, "y": 0, "width": 1, "height": 1, "text": "a"},'
                     ' {"id": "a", "type": "text", "x": 0, "y": 0, "width": 1, "height": 1, "text": "b"}]}'):
            with self.subTest(text=text), self.assertRaises(CanvasError):
                parse_canvas(text)

    def test_add_update_move_resize_and_remove_cards_and_edges(self):
        document = parse_canvas('{}')
        text = add_node(document, 'text', x=10, y=20, text='Note', id='card')
        group = add_node(document, 'group', label='Box', behind=True)
        file_card = add_node(document, 'file', file='A.md', subpath='#Title')
        link = add_node(document, 'link', url='https://example.com')
        self.assertEqual(document.nodes[0].id, group.id)
        self.assertEqual(text.text, 'Note')
        edge = add_edge(document, text.id, file_card.id, from_side='right', to_side='left', label='see')
        update_node(document, text.id, text='Revised', color='2')
        move_node(document, text.id, 30, 40)
        resize_node(document, text.id, 80, 50)
        update_edge(document, edge.id, label='updated', color='5')
        self.assertEqual((text.x, text.y, text.width, text.height, text.text, text.color), (30, 40, 80, 50, 'Revised', '2'))
        self.assertEqual(edge.label, 'updated')
        remove_edge(document, edge.id)
        self.assertEqual(document.edges, [])
        linked = add_edge(document, text.id, link.id)
        remove_node(document, text.id)
        self.assertIsNone(document.node(text.id))
        self.assertEqual(document.edges, [])
        self.assertIsNotNone(document.node(link.id))
        for call in (
                lambda: add_node(document, 'video'),
                lambda: add_node(document, 'file'),
                lambda: add_node(document, 'link'),
                lambda: add_node(document, 'text', id=link.id),
                lambda: add_node(document, 'file', file='A.md', subpath='heading'),
                lambda: update_node(document, link.id, type='text'),
                lambda: update_node(document, 'missing', text='x'),
                lambda: move_node(document, link.id, 1.5, 2),
                lambda: resize_node(document, link.id, 0, 10),
                lambda: add_edge(document, link.id, 'missing'),
                lambda: remove_edge(document, 'missing'),
                lambda: update_node(document, link.id, width=True)):
            with self.subTest(call=call), self.assertRaises(CanvasError):
                call()
        self.assertEqual(canvas_path(' Map '), 'Map.canvas')
        self.assertEqual(canvas_path('Projects/Map.CANVAS'), 'Projects/Map.CANVAS')


class CanvasFileTests(unittest.TestCase):
    def test_create_list_save_revision_and_ignored_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Projects').mkdir()
            (root / 'A.md').write_text('# A\n')
            (root / '.hidden.canvas').write_text('{}\n')
            (root / 'Projects' / 'Link.canvas').symlink_to(root / 'A.md')
            outside = root.parent / f'{root.name}-outside.canvas'
            outside.write_text('{}\n')
            self.addCleanup(outside.unlink, missing_ok=True)
            (root / 'Linked.canvas').symlink_to(outside)
            vault = Vault(root)
            self.addCleanup(vault.close)
            self.assertEqual(list(vault.notes), ['A.md'])
            self.assertEqual(vault.list_canvases(), [])
            created = vault.create_canvas(' Projects/Board ')
            self.assertEqual(created.path, 'Projects/Board.canvas')
            self.assertEqual(vault.list_canvases(), ['Projects/Board.canvas'])
            self.assertNotIn('Projects/Board.canvas', vault.notes)
            note = vault.create('Projects/Note')
            self.assertEqual(note.path, 'Projects/Note.md')
            self.assertEqual(json.loads(created.document and (root / 'Projects' / 'Board.canvas').read_text()),
                             {'nodes': [], 'edges': []})
            loaded = vault.read_canvas(created.path)
            text = add_node(loaded.document, 'text', text='Keep', id='card')
            text.extra['vendor'] = {'kept': True}
            loaded.document.extra['meta'] = 'external'
            saved = vault.save_canvas(loaded)
            disk = json.loads((root / saved.path).read_text())
            self.assertEqual(disk['nodes'][0]['text'], 'Keep')
            self.assertEqual(disk['nodes'][0]['vendor'], {'kept': True})
            self.assertEqual(disk['meta'], 'external')
            self.assertEqual(vault.canvases[saved.path], saved.revision)
            self.assertFalse(list((root / 'Projects').glob('.librevault-*')))
            reloaded = vault.read_canvas(saved.path)
            update_node(reloaded.document, 'card', text='Changed')
            (root / saved.path).write_bytes((root / saved.path).read_bytes() + b'\n')
            with self.assertRaises(Conflict):
                vault.save_canvas(reloaded)
            self.assertIn('"Keep"', (root / saved.path).read_text())
            self.assertEqual(reloaded.document.node('card').text, 'Changed')
            with self.assertRaises(VaultError):
                vault.create_canvas('Projects/Board')
            with self.assertRaises(VaultError):
                vault.create_canvas('../escape')
            with self.assertRaises(VaultError):
                vault.create_canvas('.secret')
            vault.refresh()
            self.assertEqual(vault.list_canvases(), ['Projects/Board.canvas'])
            self.assertIn('Projects/Note.md', vault.notes)

    def test_bom_crlf_invalid_json_and_atomic_replace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'Board.canvas'
            body = '{\r\n  "nodes": [],\r\n  "edges": [],\r\n  "kept": 1\r\n}\r\n'
            path.write_bytes(b'\xef\xbb\xbf' + body.encode())
            (root / 'Broken.canvas').write_text('{"nodes":', encoding='utf-8')
            vault = Vault(root)
            self.addCleanup(vault.close)
            self.assertIsNone(vault.canvases['Broken.canvas'])
            loaded = vault.read_canvas('Board.canvas')
            self.assertTrue(loaded.bom)
            self.assertEqual(loaded.newline, '\r\n')
            self.assertEqual(loaded.document.extra['kept'], 1)
            add_node(loaded.document, 'group', label='Area')
            saved = vault.save_canvas(loaded)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
            self.assertIn(b'\r\n', raw)
            self.assertNotIn(b'\n{', raw.replace(b'\r\n', b''))
            self.assertEqual(saved.revision, vault.canvases['Board.canvas'])
            update_node(saved.document, saved.document.nodes[0].id, label='Moved')
            with patch('librevault.canvas.os.replace', side_effect=OSError('full')):
                with self.assertRaises(OSError):
                    vault.save_canvas(saved)
            self.assertFalse(list(root.glob('.librevault-*')))
            self.assertNotIn(b'Moved', path.read_bytes())
            (root / 'Fresh.canvas').write_text('{"nodes": [], "edges": []}\n')
            vault.update_paths(['Fresh.canvas', 'Broken.canvas'])
            self.assertIn('Fresh.canvas', vault.canvases)
            self.assertNotIn('Fresh.canvas', vault.notes)
            path.unlink()
            vault.update_paths(['Board.canvas'])
            self.assertNotIn('Board.canvas', vault.canvases)
            self.assertIn('Fresh.canvas', vault.canvases)

    def test_folder_move_keeps_canvas_with_notes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Projects').mkdir()
            vault = Vault(root)
            self.addCleanup(vault.close)
            vault.create('Projects/Note')
            vault.create_canvas('Projects/Board')
            mapping = vault.move('Projects', 'Archive')
            self.assertEqual(mapping['Projects/Board.canvas'], 'Archive/Board.canvas')
            self.assertIn('Archive/Note.md', vault.notes)
            self.assertIn('Archive/Board.canvas', vault.canvases)
            self.assertTrue((root / 'Archive' / 'Board.canvas').is_file())


try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from librevault.canvas_ui import CanvasView
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class CanvasUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            from gi.repository import Gio
            cls.app = Gtk.Application(application_id='org.librevault.CanvasTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'Note.md').write_text('# Note\n\nBody\n')
        (self.root / 'Other.md').write_text('Other\n')
        self.window = Window(self.app, self.root, preferences_path=self.root / 'settings.json')
        wait_for_work(self.window)
        self.addCleanup(self.safe_destroy, self.window)
        self.drain()

    def safe_destroy(self, window):
        if not window.in_destruction():
            window.destroy()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(400):
            if not context.pending():
                return
            context.iteration(False)

    def open_board(self):
        created = self.window.vault.create_canvas('Board')
        self.window.populate_files()
        wait_for_work(self.window)
        view = self.window.open_canvas(created.path)
        self.drain()
        view.drawing.set_size_request(800, 600)
        self.drain()
        return view

    def test_tab_tools_draw_save_and_reopen_note(self):
        view = self.open_board()
        self.assertIsInstance(view, CanvasView)
        self.assertEqual(view.snapshot(), {'kind': 'canvas', 'path': 'Board.canvas'})
        self.assertIsNone(self.window.current())
        for label in ('New Text Card', 'New Note Card', 'New Group', 'Delete Selected', 'Zoom In', 'Zoom Out', 'Fit', 'Save'):
            self.assertIn(label, view.buttons)
        text = view.add_text_card('Hello canvas')
        group = view.add_group_card('Cluster')
        link = view.add_link_card('https://example.com')
        note = view.add_file_card('Note.md', subpath='#Note')
        self.assertEqual(view.document.nodes[0].id, group.id)
        self.assertTrue(view.dirty)
        self.assertTrue(view.tab_label.get_text().startswith('•'))
        move_node(view.document, text.id, 100, 80)
        move_node(view.document, note.id, 420, 80)
        move_node(view.document, link.id, 420, 240)
        view.scale, view.pan_x, view.pan_y = 1, 0, 0
        view.pointer_down(120, 100)
        view.pointer_move(170, 140)
        view.pointer_up(170, 140)
        moved = view.document.node(text.id)
        self.assertEqual((moved.x, moved.y), (150, 120))
        view.pointer_down(150 + 20, 120 + 20)
        view.pointer_up(170, 140)
        view.pointer_down(150 + moved.width, 120 + moved.height)
        view.pointer_move(150 + 300, 120 + 180)
        view.pointer_up(450, 300)
        resized = view.document.node(text.id)
        self.assertGreaterEqual(resized.width, 200)
        self.assertGreaterEqual(resized.height, 80)
        view.pointer_down(resized.x + 20, resized.y + 20)
        view.pointer_up(resized.x + 20, resized.y + 20)
        view.pointer_down(resized.x + resized.width, resized.y + resized.height / 2)
        view.pointer_up(440, 110)
        self.assertEqual(len(view.document.edges), 1)
        self.assertEqual(view.document.edges[0].from_node, resized.id)
        self.assertEqual(view.document.edges[0].to_node, note.id)
        view.change_node(link.id, url='https://example.org/path', color='4')
        view.drawing.queue_draw()
        self.drain()
        view.save()
        self.assertFalse(view.dirty)
        disk = json.loads((self.root / 'Board.canvas').read_text())
        self.assertEqual(disk['nodes'][0]['type'], 'group')
        kinds = {node['type'] for node in disk['nodes']}
        self.assertEqual(kinds, {'text', 'file', 'link', 'group'})
        stored_link = next(node for node in disk['nodes'] if node['type'] == 'link')
        self.assertEqual(stored_link['url'], 'https://example.org/path')
        self.assertEqual(stored_link['color'], '4')
        self.assertEqual(len(disk['edges']), 1)
        self.window.save_current()
        view.pointer_down(440, 120, Gdk.EventType._2BUTTON_PRESS)
        self.drain()
        self.assertEqual(self.window.current().note.path, 'Note.md')
        self.window.tabs.set_current_page(self.window.tabs.page_num(view))
        self.drain()
        previous = view.scale
        view.buttons['Zoom In'].emit('clicked')
        self.assertGreater(view.scale, previous)
        view.buttons['Fit'].emit('clicked')
        self.assertGreater(view.scale, 0)
        edge_id = view.document.edges[0].id
        view.selection = ('edge', edge_id)
        view.delete_selected()
        self.assertEqual(view.document.edges, [])
        view.autosave()
        self.assertFalse(view.dirty)
        self.assertEqual(json.loads((self.root / 'Board.canvas').read_text())['edges'], [])
        self.assertEqual([row.path for row in self.window.files.get_children() if row.path.endswith('.canvas')],
                         ['Board.canvas'])
        self.window.files.emit('row-activated', next(row for row in self.window.files.get_children()
                                                     if row.path == 'Note.md'))
        self.assertEqual(self.window.current().note.path, 'Note.md')

    def test_dialogs_commands_workspace_conflict_and_external_reload(self):
        view = self.open_board()
        text = view.add_text_card('Before')

        def apply_text(dialog):
            editor = next(widget for widget in self.walk(dialog) if isinstance(widget, Gtk.TextView))
            editor.get_buffer().set_text('After edit')
            colors = next(widget for widget in self.walk(dialog) if isinstance(widget, Gtk.ComboBoxText))
            colors.set_active_id('2')
            return Gtk.ResponseType.OK

        with patch.object(Gtk.Dialog, 'run', apply_text):
            view.edit_node(view.document.node(text.id))
        self.assertEqual(view.document.node(text.id).text, 'After edit')
        self.assertEqual(view.document.node(text.id).color, '2')
        view.save()

        def answer_note(dialog):
            if dialog.get_title() != 'Choose a note':
                return Gtk.ResponseType.CANCEL
            listing = next(widget for widget in self.walk(dialog) if isinstance(widget, Gtk.ListBox))
            listing.select_row(next(row for row in listing.get_children() if row.note_path == 'Other.md'))
            return Gtk.ResponseType.OK

        with patch.object(Gtk.Dialog, 'run', answer_note):
            view.buttons['New Note Card'].emit('clicked')
        self.assertTrue(any(node.file == 'Other.md' for node in view.document.nodes))

        palette = self.window.palette
        self.window.show_command_palette()
        palette = self.window.palette
        titles = [row.title for row in palette.results.get_children()]
        self.assertIn('New canvas', titles)
        self.assertIn('Open canvas', titles)
        palette.destroy()
        self.drain()

        def name_canvas(dialog):
            entry = next(widget for widget in dialog.get_content_area().get_children() if isinstance(widget, Gtk.Entry))
            entry.set_text('Projects/Map')
            return Gtk.ResponseType.OK

        (self.root / 'Projects').mkdir()
        with patch.object(Gtk.Dialog, 'run', name_canvas):
            self.window.new_canvas(parent='Projects')
        self.drain()
        self.assertIn('Projects/Map.canvas', self.window.vault.canvases)
        self.assertIsInstance(self.window.active_page(), CanvasView)
        self.assertEqual(self.window.active_page().path, 'Projects/Map.canvas')

        self.window.open_canvas('Board.canvas')
        board = self.window.active_page()
        board.add_text_card('Race')
        (self.root / 'Board.canvas').write_text('{"nodes": [], "edges": []}\n')
        with self.assertRaises(Conflict):
            board.save()
        self.assertTrue(board.dirty)
        board.dirty = False
        board.cancel_autosave()
        self.window.refresh()
        wait_for_work(self.window)
        self.drain()
        self.assertEqual(board.document.nodes, [])

        def open_listed(dialog):
            self.assertEqual(dialog.get_title(), 'Open canvas')
            return Gtk.ResponseType.OK

        with patch.object(Gtk.Dialog, 'run', open_listed):
            self.window.open_canvas_command()
        self.drain()
        self.assertIn(self.window.active_page().path, self.window.vault.canvases)
        self.window.persist_workspace()
        self.safe_destroy(self.window)
        restored = Window(self.app, self.root, preferences_path=self.root / 'settings.json')
        wait_for_work(restored)
        self.addCleanup(self.safe_destroy, restored)
        self.drain()
        paths = [restored.tabs.get_nth_page(index).path
                 for index in range(restored.tabs.get_n_pages())
                 if isinstance(restored.tabs.get_nth_page(index), CanvasView)]
        self.assertIn('Board.canvas', paths)
        self.assertIn('Projects/Map.canvas', paths)

    def walk(self, widget):
        yield widget
        if hasattr(widget, 'get_children'):
            for child in widget.get_children():
                yield from self.walk(child)
        content = getattr(widget, 'get_content_area', None)
        if content is not None and content() is not widget:
            yield from self.walk(content())


if __name__ == '__main__':
    unittest.main()
