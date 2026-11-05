"""Navigation, shared-buffer splits, and real workspace restoration."""
from tests.gui_wait import wait_for_work
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.navigation import Location, NavigationHistory, matching_notes
from librevault.workspace import WorkspaceStore

try:
    from librevault.app import Window
    from gi.repository import Gdk, Gio, GLib, Gtk
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class NavigationTests(unittest.TestCase):
    def test_fuzzy_matching_aliases_and_rank(self):
        paths = ['Projects/Long Project.md', 'LP.md', 'Other.md']
        self.assertEqual(matching_notes(paths, 'lp')[0], 'LP.md')
        self.assertEqual(matching_notes(paths, 'launch', {'Other.md': ['Launch plans']}), ['Other.md'])
        self.assertEqual(matching_notes(paths, 'xyz'), [])

    def test_history_branch_cursor_and_remap(self):
        history = NavigationHistory()
        history.visit(Location('A.md', 2))
        history.visit(Location('B.md', 5))
        history.update(Location('B.md', 9))
        self.assertEqual(history.step(-1), Location('A.md', 2))
        self.assertEqual(history.step(1), Location('B.md', 9))
        history.step(-1)
        history.visit(Location('C.md'))
        self.assertIsNone(history.step(1))
        history.remap({'A.md': 'Renamed.md'}, ['C.md'])
        self.assertEqual(history.locations, [Location('Renamed.md', 2)])
        self.assertEqual(history.index, 0)

    def test_store_is_per_vault_and_handles_corruption(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = WorkspaceStore(root / 'config' / 'settings.json')
            self.assertEqual(store.load(root / 'one'), {})
            self.assertFalse(store.path.exists())
            store.save(root / 'one', {'tabs': [{'path': 'A.md', 'cursor': 3}]})
            store.save(root / 'two', {'tabs': []})
            self.assertEqual(store.load(root / 'one')['tabs'][0]['cursor'], 3)
            store.path.write_text('broken json')
            self.assertEqual(store.load(root / 'one'), {})


@unittest.skipUnless(GTK_AVAILABLE, 'GTK and a display are required')
class WorkspaceFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.NavigationTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'vault'
        self.root.mkdir()
        (self.root / 'Folder').mkdir()
        (self.root / 'A.md').write_text('# Alpha\n\n' + 'Long paragraph\n' * 100)
        (self.root / 'Folder' / 'B.md').write_text('---\naliases: [Launch plan]\n---\n# Beta\n')
        self.preferences = self.base / 'config' / 'settings.json'
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state')})
        env.start()
        self.addCleanup(env.stop)
        self.windows = []
        self.addCleanup(self.destroy_windows)
        self.window = self.make_window()

    def make_window(self):
        window = Window(self.app, preferences_path=self.preferences)
        wait_for_work(window)
        self.windows.append(window)
        return window

    def destroy_windows(self):
        for window in self.windows:
            if not window.in_destruction():
                window.destroy()

    def settle(self, milliseconds=120):
        loop = GLib.MainLoop()
        GLib.timeout_add(milliseconds, lambda: (loop.quit(), False)[1])
        loop.run()

    def test_destroy_cancels_pending_workspace_scroll_restoration(self):
        self.window.workspace_store.save(self.root, {'tabs': [{'path': 'A.md', 'scroll': 10}]})
        with patch.object(self.window, 'restore_scroll', wraps=self.window.restore_scroll) as restore:
            self.window.load_vault(self.root)
            # Stop as soon as installation schedules restoration, before its
            # timeout runs; the test intentionally destroys an unsettled UI.
            import time
            deadline = time.monotonic() + 5
            context = GLib.MainContext.default()
            while self.window.loading_vault and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(.001)
            self.assertTrue(self.window.scroll_restore_sources)
            self.window.destroy()
            self.settle()
            restore.assert_not_called()
        self.assertFalse(self.window.scroll_restore_sources)

    def test_switcher_uses_alias_and_opens_selected_note(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        self.window.show_quick_switcher()
        switcher = self.window.quick_switcher
        switcher.search.set_text('launch')
        self.settle()
        self.assertEqual(switcher.results.get_selected_row().title, 'Folder/B.md')
        switcher.search.emit('activate')
        self.assertIsNone(self.window.quick_switcher)
        self.assertEqual(self.window.current().note.path, 'Folder/B.md')

    def test_back_forward_restores_cursor_and_split_shares_edits(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        alpha = self.window.open_note('A.md')
        alpha.buffer.place_cursor(alpha.buffer.get_iter_at_offset(5))
        self.window.open_note('Folder/B.md')
        self.window.navigate_back()
        self.assertIs(self.window.current(), alpha)
        self.assertEqual(alpha.buffer.get_iter_at_mark(alpha.buffer.get_insert()).get_offset(), 5)
        self.window.navigate_forward()
        self.assertEqual(self.window.current().note.path, 'Folder/B.md')
        self.window.open_split(alpha)
        self.assertIs(self.window.split_view.get_buffer(), alpha.buffer)
        alpha.buffer.insert_at_cursor('edit')
        self.assertTrue(alpha.dirty)
        self.assertIn('edit', self.window.split_view.get_buffer().get_text(*alpha.buffer.get_bounds(), True))
        alpha.undo()
        self.assertNotIn('edit', alpha.text())

    def test_reopen_restores_tabs_cursor_scroll_folders_and_split(self):
        self.assertEqual(self.window.workspace_stack.get_visible_child_name(), 'welcome')
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        alpha = self.window.open_note('A.md')
        beta = self.window.open_note('Folder/B.md')
        alpha.buffer.place_cursor(alpha.buffer.get_iter_at_offset(80))
        self.window.open_note('A.md')
        self.settle()
        alpha.scroll.get_vadjustment().set_value(200)
        self.window.collapsed_folders.add('Folder')
        self.window.open_split(beta)
        self.window.persist_workspace()
        self.window.destroy()
        self.windows.remove(self.window)
        reopened = self.make_window()
        self.assertEqual(reopened.vault.root, self.root)
        self.settle(240)
        self.assertEqual(list(reopened.panes), ['A.md', 'Folder/B.md'])
        self.assertEqual(reopened.current().note.path, 'A.md')
        restored = reopened.panes['A.md']
        self.assertEqual(restored.buffer.get_iter_at_mark(restored.buffer.get_insert()).get_offset(), 80)
        self.assertAlmostEqual(restored.scroll.get_vadjustment().get_value(), 200, delta=2)
        self.assertIn('Folder', reopened.collapsed_folders)
        self.assertEqual(reopened.split_pane.note.path, 'Folder/B.md')
        self.assertIs(reopened.split_view.get_buffer(), reopened.split_pane.buffer)

    def test_link_completion_inserts_as_one_undo_action(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.open_note('A.md')
        before = pane.text()
        pane.buffer.place_cursor(pane.buffer.get_end_iter())
        pane.buffer.insert_at_cursor('\n[[launch')
        self.settle()
        self.assertEqual(pane.navigation.paths, ['Folder/B.md'])
        pane.navigation.accept()
        self.assertTrue(pane.text().endswith('[[Folder/B]]'))
        pane.undo()
        self.assertTrue(pane.text().endswith('[[launch'))
        self.assertTrue(pane.text().startswith(before))

    def test_empty_workspace_stays_empty_and_invalid_entries_are_ignored(self):
        store = self.window.workspace_store
        store.save(self.root, {'tabs': []})
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        self.assertIsNone(self.window.current())
        # Malformed persisted entries must not reach GTK or dict lookup.
        store.save(self.root, {'tabs': [{'path': []}, {'path': {}}, {'path': 'A.md',
                         'cursor': 'invalid', 'scroll': float('inf')}],
                         'active': [], 'collapsed_folders': [{}, 4, 'Folder'], 'split': {}})
        self.window.vault = None  # Preserve the deliberately malformed fixture on load.
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        self.settle()
        self.assertEqual(list(self.window.panes), ['A.md'])
        self.assertEqual(self.window.panes['A.md'].scroll.get_vadjustment().get_value(), 0)
        self.assertIn('Folder', self.window.collapsed_folders)

    def test_unresolved_link_creation_is_explicit_and_cancellable(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        with patch('librevault.app.Gtk.MessageDialog') as constructor:
            constructor.return_value.run.return_value = Gtk.ResponseType.CANCEL
            self.window.follow_link('A.md', 'Missing')
        self.assertFalse((self.root / 'Missing.md').exists())
        with patch('librevault.app.Gtk.MessageDialog') as constructor:
            constructor.return_value.run.return_value = Gtk.ResponseType.OK
            self.window.follow_link('A.md', 'Missing')
        self.assertTrue((self.root / 'Missing.md').is_file())
        self.assertEqual(self.window.current().note.path, 'Missing.md')
        with patch('librevault.app.Gtk.MessageDialog') as constructor:
            constructor.return_value.run.return_value = Gtk.ResponseType.OK
            self.window.follow_link('Folder/B.md', './Sibling%20note')
        self.assertTrue((self.root / 'Folder' / 'Sibling note.md').is_file())
        self.assertEqual(self.window.current().note.path, 'Folder/Sibling note.md')
        with patch('librevault.app.Gtk.MessageDialog') as constructor:
            constructor.return_value.run.return_value = Gtk.ResponseType.OK
            self.window.follow_link('Folder/B.md', '../Root note')
        self.assertTrue((self.root / 'Root note.md').is_file())

    def test_ctrl_click_follows_real_markdown_and_wiki_links(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.open_note('A.md')
        for source, label, target in (
                ('[label](<Folder/B.md> "title")', 'label', 'Folder/B.md'),
                ('[[Folder/B|label]]', 'label', 'Folder/B'),
                ('`[label](Folder/B.md)`', 'label', None)):
            pane.buffer.set_text(source)
            pane.render()
            self.settle()
            iterator = pane.buffer.get_iter_at_offset(source.index(label))
            rectangle = pane.editor.get_iter_location(iterator)
            event = Gdk.EventButton()
            event.button = 1
            event.state = Gdk.ModifierType.CONTROL_MASK
            event.x, event.y = pane.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET,
                rectangle.x + 2, rectangle.y + max(1, rectangle.height // 2))
            with patch.object(self.window, 'follow_link') as follow:
                pane.navigation.clicked(pane.editor, event)
                if target:
                    follow.assert_called_once_with('A.md', target)
                else:
                    follow.assert_not_called()

    def click_editor_label(self, pane, view, label, modifiers=None):
        if modifiers is None:
            modifiers = Gdk.ModifierType.CONTROL_MASK
        start = pane.text().index(label)
        iterator = pane.buffer.get_iter_at_offset(start)
        rectangle = view.get_iter_location(iterator)
        top, height = view.get_line_yrange(iterator)
        y = rectangle.y + max(1, rectangle.height // 2) if rectangle.height else top + height // 2
        # GTK's character rectangle can be shifted by hidden alias prefixes.
        # Find the visible label through the same hit-testing as the mouse.
        visible = view.get_visible_rect()
        hits = []
        for x in range(visible.x, visible.x + visible.width):
            found, position = view.get_iter_at_location(x, y)
            if found and start <= position.get_offset() < start + len(label):
                hits.append(x)
        self.assertTrue(hits, f'No visible hit target for {label!r}')
        x, y = view.buffer_to_window_coords(Gtk.TextWindowType.TEXT, hits[len(hits) // 2], y)
        for kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            event = Gdk.Event.new(kind)
            event.window = view.get_window(Gtk.TextWindowType.TEXT)
            event.send_event = True
            event.time = Gdk.CURRENT_TIME
            event.x, event.y = x, y
            event.button = 1
            event.state = modifiers
            event.set_device(view.get_display().get_default_seat().get_pointer())
            view.event(event)
            self.settle()

    def test_plain_and_ctrl_click_dispatch_open_note_in_editor_and_split(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        from itertools import product
        for split, modifiers in product((False, True), (Gdk.ModifierType(0), Gdk.ModifierType.CONTROL_MASK)):
            for source in ('[[Folder/B|label]]', '[label](Folder/B.md)'):
                with self.subTest(split=split, modifiers=modifiers, source=source):
                    pane = self.window.open_note('A.md')
                    pane.buffer.set_text('Before\n' + source + '\nAfter')
                    pane.buffer.place_cursor(pane.buffer.get_start_iter())
                    pane.set_mode('live')
                    if split:
                        self.window.open_split(pane)
                    self.settle()
                    view = self.window.split_view if split else pane.editor
                    self.click_editor_label(pane, view, 'label', modifiers)
                    self.assertEqual(self.window.current().note.path, 'Folder/B.md')

    def test_editor_external_link_dispatch_and_plain_click_editing(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.open_note('A.md')
        source = 'Before\n[label](https://example.com/path?a=1&b=2 "title")\nAfter'
        pane.buffer.set_text(source)
        pane.buffer.place_cursor(pane.buffer.get_start_iter())
        pane.set_mode('live')
        self.settle()
        with patch('librevault.navigation_ui.Gtk.show_uri_on_window') as launch:
            self.click_editor_label(pane, pane.editor, 'label', Gdk.ModifierType(0))
            launch.assert_called_once_with(self.window, 'https://example.com/path?a=1&b=2', Gdk.CURRENT_TIME)
            self.assertEqual(self.window.current().note.path, 'A.md')
            pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(source.index('label')))
            self.settle()
            for mode in ('live', 'source'):
                pane.set_mode(mode)
                self.settle()
                previous_calls = launch.call_count
                self.click_editor_label(pane, pane.editor, 'label', Gdk.ModifierType(0))
                self.assertEqual(launch.call_count, previous_calls)
                self.click_editor_label(pane, pane.editor, 'label')
                launch.assert_called_with(self.window, 'https://example.com/path?a=1&b=2', Gdk.CURRENT_TIME)
            self.assertEqual(launch.call_count, 3)
        self.assertEqual(pane.text(), source)

    def test_live_link_shift_click_selects_and_blank_space_does_not_open(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.open_note('A.md')
        pane.buffer.set_text('Before\n[label](https://example.com)')
        pane.buffer.place_cursor(pane.buffer.get_start_iter())
        pane.set_mode('live')
        self.settle()
        with patch('librevault.navigation_ui.Gtk.show_uri_on_window') as launch:
            self.click_editor_label(pane, pane.editor, 'label', Gdk.ModifierType.SHIFT_MASK)
            self.assertTrue(pane.buffer.get_has_selection())
            launch.assert_not_called()
            pane.buffer.place_cursor(pane.buffer.get_start_iter())
            self.settle()
            top, height = pane.editor.get_line_yrange(pane.buffer.get_end_iter())
            event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
            event.window = pane.editor.get_window(Gtk.TextWindowType.TEXT)
            event.send_event = True
            event.time = Gdk.CURRENT_TIME
            event.button = 1
            event.state = Gdk.ModifierType(0)
            event.x, event.y = pane.editor.buffer_to_window_coords(
                Gtk.TextWindowType.TEXT, pane.editor.get_left_margin() + 5, top + height + 30)
            event.set_device(pane.editor.get_display().get_default_seat().get_pointer())
            pane.editor.event(event)
            launch.assert_not_called()

    def test_reading_links_activate_rendered_destinations(self):
        import html
        import re

        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.open_note('A.md')
        pane.buffer.set_text('[[Folder/B|Note]] and [Web](https://example.com/path?a=1&b=2)')
        pane.set_mode('reading')
        self.settle()

        def labels(widget):
            if isinstance(widget, Gtk.Label):
                yield widget
            elif isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    yield from labels(child)

        links = [(label, html.unescape(uri)) for label in labels(pane.reading)
                 for uri in re.findall(r'<a href="([^"<>]+)"', label.get_label())]
        self.assertEqual(len(links), 2)
        with patch('librevault.reading.Gio.AppInfo.launch_default_for_uri') as launch:
            external = next((label, uri) for label, uri in links if uri.startswith('https:'))
            self.assertTrue(external[0].emit('activate-link', external[1]))
            launch.assert_called_once_with('https://example.com/path?a=1&b=2', None)
        internal = next((label, uri) for label, uri in links if uri.startswith('note:'))
        self.assertTrue(internal[0].emit('activate-link', internal[1]))
        self.assertEqual(self.window.current().note.path, 'Folder/B.md')

    def test_split_renders_task_controls_and_cleans_up_pending_layout(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.open_note('A.md')
        pane.buffer.set_text('# List\n\n- [ ] Task\n- Bullet\n')
        self.window.open_split(pane)
        self.settle()
        view = self.window.split_view
        self.assertEqual(len(view.markers), 2)
        self.assertIsInstance(view.markers[0][1], Gtk.CheckButton)
        view.markers[0][1].set_active(True)
        self.assertIn('- [x] Task', pane.text())
        pane.undo()
        self.settle()
        self.assertIn('- [ ] Task', pane.text())
        self.assertFalse(view.markers[0][1].get_active())
        pane.set_mode('source')
        self.settle()
        self.assertEqual(view.markers, [])
        pane.set_mode('live')
        self.settle()
        self.assertEqual(len(view.markers), 2)
        view.queue_sync()
        self.assertIsNotNone(view.pending)
        self.window.close_split()
        self.assertTrue(view.closed)
        self.assertIsNone(view.pending)
        self.assertEqual(view.handlers, [])
        self.window.open_split(pane)
        self.settle()
        self.assertEqual(len(self.window.split_view.markers), 2)
