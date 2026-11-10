"""Stacking and floating desktops, where the window manager honours GTK.

Tiling compositors replace requested sizes, maximization, and dialog stacking.
The Hyprland test path forces every window to float, so it does not exercise
that. These tests cover display selection when Hyprland cannot be used, and
the sizes, transience, and layout Libre Vault asks a stacking window manager
to apply. They assert those requests, not the size a display server ends up
with: Xvfb has no window manager, and a tiling compositor would ignore them.
"""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import test_runner
from tests.gui_wait import wait_for_work

try:
    from librevault.panels import EDITOR_MIN, SPLITTER_HANDLE, sidebar_widths
except (ImportError, ValueError):
    sidebar_widths = None


class NonTilingDisplayTests(unittest.TestCase):
    """`scripts/test_runner.py` when Hyprland is absent or only partly available.

    Broadway, a working Hyprland workspace, generic Xvfb selection, and the
    Linux error when a display exists but neither Hyprland nor xvfb-run does
    are covered in `tests/test_test_runner.py`.
    """

    def launch(self, argv, *, platform='linux', env=None, programs=(), returncode=0):
        available = {name: f'/usr/bin/{name}' for name in programs}
        stdout, stderr = StringIO(), StringIO()
        with patch.object(test_runner.sys, 'platform', platform), \
                patch.dict(os.environ, env or {}, clear=True), \
                patch.object(test_runner.shutil, 'which', side_effect=available.get), \
                patch.object(test_runner.subprocess, 'run',
                             return_value=SimpleNamespace(returncode=returncode)) as run, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = test_runner.main(list(argv))
        return code, run, stdout.getvalue(), stderr.getvalue()

    def assert_xvfb(self, run, output):
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['xvfb-run', '-a', '-s', '-screen 0 1600x1000x24'])
        child = run.call_args.kwargs['env']
        self.assertEqual(child['GDK_BACKEND'], 'x11')
        self.assertNotIn('WAYLAND_DISPLAY', child)
        self.assertNotIn('LIBREVAULT_TEST_CLASS', child)
        self.assertEqual(run.call_args.kwargs['cwd'], test_runner.ROOT)
        self.assertEqual(run.call_count, 1)
        self.assertIn('private Xvfb display', output)
        self.assertNotIn('background workspace', output)

    def test_xvfb_is_used_when_hyprland_has_no_hyprctl(self):
        code, run, output, _error = self.launch(
            [], programs=('xvfb-run',),
            env={'HYPRLAND_INSTANCE_SIGNATURE': 'session', 'DISPLAY': ':0',
                 'WAYLAND_DISPLAY': 'wayland-0', 'XDG_CONFIG_HOME': '/real/config'})
        self.assertEqual(code, 0)
        self.assert_xvfb(run, output)
        self.assertNotEqual(run.call_args.kwargs['env']['XDG_CONFIG_HOME'], '/real/config')

    def test_xvfb_is_used_when_hyprland_has_no_xwayland_display(self):
        # The Hyprland branch also requires DISPLAY. A Wayland-only session,
        # even with hyprctl installed, is not a place to map test windows.
        code, run, output, _error = self.launch(
            [], programs=('hyprctl', 'xvfb-run'),
            env={'HYPRLAND_INSTANCE_SIGNATURE': 'session', 'WAYLAND_DISPLAY': 'wayland-0'})
        self.assertEqual(code, 0)
        self.assert_xvfb(run, output)
        self.assertNotEqual(run.call_args.args[0][0], 'hyprctl')

    def test_native_hosts_run_gui_tests_on_the_desktop_display(self):
        for platform, backend in (('darwin', 'quartz'), ('win32', 'win32')):
            with self.subTest(platform=platform):
                code, run, output, _error = self.launch(
                    ['tests.test_non_tiling_wm'], platform=platform, returncode=9,
                    env={'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0',
                         'GDK_BACKEND': backend, 'XDG_CONFIG_HOME': '/real/config'})
                self.assertEqual(code, 9)
                self.assertEqual(run.call_count, 1)
                command = run.call_args.args[0]
                self.assertEqual(command[:3], [sys.executable, '-m', 'unittest'])
                self.assertIn('tests.test_non_tiling_wm', command)
                child = run.call_args.kwargs['env']
                self.assertEqual(child['GDK_BACKEND'], backend)
                self.assertEqual(child['DISPLAY'], ':0')
                self.assertNotIn('LIBREVAULT_TEST_CLASS', child)
                self.assertNotEqual(child['XDG_CONFIG_HOME'], '/real/config')
                self.assertIn('GUI checks use the floating desktop.', output)

    def test_native_gui_probes_run_on_the_desktop_display(self):
        for platform in ('darwin', 'win32'):
            for argv in (['--smoke'], ['--script', 'tests/smoke_gui.py'], ['--script', 'probe.py', '--flag']):
                with self.subTest(platform=platform, argv=argv):
                    code, run, output, error = self.launch(argv, platform=platform)
                    self.assertEqual((code, error), (0, ''))
                    self.assertEqual(run.call_count, 1)
                    self.assertIn('GUI checks use the floating desktop.', output)

    def test_linux_floating_desktops_run_on_their_display(self):
        for desktop in ('GNOME', 'KDE', 'ubuntu:GNOME', 'XFCE', 'X-Cinnamon'):
            with self.subTest(desktop=desktop):
                code, run, output, error = self.launch(
                    ['--smoke'], env={'DISPLAY': ':0', 'XDG_CURRENT_DESKTOP': desktop})
                self.assertEqual((code, error), (0, ''))
                self.assertEqual(run.call_args.kwargs['env']['DISPLAY'], ':0')
                self.assertIn('GUI checks use the floating desktop.', output)

    def test_tiling_or_unknown_linux_desktops_still_need_isolation(self):
        for desktop in ('sway', 'i3', 'Hyprland', 'niri', ''):
            with self.subTest(desktop=desktop), self.assertRaises(SystemExit) as raised:
                self.launch([], env={'DISPLAY': ':0', 'XDG_CURRENT_DESKTOP': desktop})
            self.assertEqual(raised.exception.code, 2)

    def test_floating_name_without_a_display_skips_gui_tests(self):
        _code, _run, output, _error = self.launch([], env={'XDG_CURRENT_DESKTOP': 'GNOME'})
        self.assertIn('No desktop display; GUI tests will be skipped.', output)

    def test_xvfb_outranks_the_desktop_and_allows_gui_probes(self):
        for platform in ('darwin', 'win32'):
            with self.subTest(platform=platform):
                code, run, output, error = self.launch(
                    ['--smoke'], platform=platform, programs=('xvfb-run',),
                    env={'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'GDK_BACKEND': 'quartz'})
                self.assertEqual(code, 0)
                self.assertEqual(error, '')
                self.assert_xvfb(run, output)
                self.assertTrue(str(run.call_args.args[0][-1]).endswith('smoke_gui.py'))
                self.assertNotIn('floating desktop', output)

    def test_linux_without_a_display_skips_without_forcing_a_backend(self):
        code, run, output, _error = self.launch(
            [], returncode=4, env={'XDG_CONFIG_HOME': '/real/config'})
        self.assertEqual(code, 4)
        self.assertEqual(run.call_count, 1)
        child = run.call_args.kwargs['env']
        self.assertNotIn('GDK_BACKEND', child)
        self.assertNotIn('DISPLAY', child)
        self.assertNotIn('WAYLAND_DISPLAY', child)
        self.assertNotEqual(child['XDG_CONFIG_HOME'], '/real/config')
        self.assertIn('No desktop display; GUI tests will be skipped.', output)
        self.assertNotIn('floating desktop', output)

    def test_foreground_on_a_native_host_keeps_the_desktop_display(self):
        code, run, output, error = self.launch(
            ['--foreground', '--smoke'], platform='darwin',
            env={'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0', 'GDK_BACKEND': 'quartz'})
        self.assertEqual(code, 0)
        self.assertEqual(error, '')
        self.assertEqual(output, '')
        child = run.call_args.kwargs['env']
        self.assertEqual(child['DISPLAY'], ':0')
        self.assertEqual(child['WAYLAND_DISPLAY'], 'wayland-0')
        self.assertEqual(child['GDK_BACKEND'], 'quartz')
        self.assertNotIn('LIBREVAULT_TEST_CLASS', child)
        self.assertTrue(str(run.call_args.args[0][1]).endswith('smoke_gui.py'))
        self.assertNotEqual(run.call_args.args[0][0], 'xvfb-run')


class NarrowWorkspaceTests(unittest.TestCase):
    """Sidebar pixels for a window the user was able to resize freely."""

    def setUp(self):
        if sidebar_widths is None:
            self.skipTest('GTK is required to import panel sizing')

    def test_a_window_at_or_below_the_editor_minimum_hides_both_sidebars(self):
        # Two splitter handles sit outside the editor reservation. At or below
        # that total, open sidebars are given nothing so they cannot cover the note.
        limit = EDITOR_MIN + 2 * SPLITTER_HANDLE
        for width in (1, 200, EDITOR_MIN, limit):
            with self.subTest(width=width):
                self.assertEqual(sidebar_widths(width, 0.5, 0.5), (0, 0))
                self.assertEqual(sidebar_widths(width, 0.45, 0.4), (0, 0))

    def test_the_first_spare_pixel_goes_to_a_sidebar_and_the_editor_keeps_its_minimum(self):
        width = EDITOR_MIN + 2 * SPLITTER_HANDLE + 1
        self.assertEqual(sidebar_widths(width, 0.5, 0.5), (1, 0))
        left, right = sidebar_widths(width, 0.45, 0.4)
        self.assertEqual(left + right, 1)
        self.assertGreaterEqual(width - left - right - 2 * SPLITTER_HANDLE, EDITOR_MIN)
        # Once there is a real margin, both open sidebars stay in proportion.
        left, right = sidebar_widths(480, 0.45, 0.4)
        self.assertEqual((left, right), (80, 70))
        self.assertGreaterEqual(480 - left - right - 2 * SPLITTER_HANDLE, EDITOR_MIN)


try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from librevault.commands import CommandPalette
    from librevault.vault import VaultError
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class FloatingDesktopGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.NonTilingTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Note\n', encoding='utf-8')
        (self.root / 'Other.md').write_text('Other\n', encoding='utf-8')
        self.window = Window(self.app, self.root,
                             preferences_path=Path(self.directory.name) / 'settings.json')
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.window.open_note('Note.md')
        self.pane = self.window.current()
        self.drain()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def test_the_main_window_requests_a_size_and_the_platform_titlebar(self):
        # get_default_size is the request a stacking window manager applies.
        # It is not the size Xvfb or a tiling compositor realizes.
        self.assertEqual(tuple(self.window.get_default_size()), (1360, 850))
        self.assertTrue(self.window.get_resizable())
        if sys.platform == 'darwin':
            self.assertIsNone(self.window.get_titlebar())
        else:
            titlebar = self.window.get_titlebar()
            self.assertIsInstance(titlebar, Gtk.HeaderBar)
            self.assertTrue(titlebar.get_show_close_button())

    def test_popped_out_notes_are_transient_and_remember_their_requested_size(self):
        first = self.window.pop_out_note()
        self.window.open_note('Other.md')
        second = self.window.pop_out_note()
        self.addCleanup(first.destroy)
        self.addCleanup(second.destroy)
        for note_window in (first, second):
            self.assertIs(note_window.get_transient_for(), self.window)
            self.assertTrue(note_window.get_destroy_with_parent())
            self.assertFalse(note_window.get_modal())
            self.assertEqual(tuple(note_window.get_default_size()), (720, 600))
        first.get_size = lambda: SimpleNamespace(width=880, height=640)
        second.get_size = lambda: SimpleNamespace(width=410, height=280)
        saved = {(item['path'], item['width'], item['height'])
                 for item in self.window.capture_workspace_state()['note_windows']}
        self.assertEqual(saved, {('Note.md', 880, 640), ('Other.md', 410, 280)})

    def test_restored_note_windows_request_sizes_inside_the_floating_bounds(self):
        from librevault.layout_ui import NoteWindow
        self.window.open_note('Other.md')
        requested = []
        original = NoteWindow.resize

        def spy(window, width, height):
            requested.append((window.pane.note.path, width, height))
            return original(window, width, height)

        state = {
            'tabs': [{'path': 'Note.md', 'cursor': 0, 'scroll': 0},
                     {'path': 'Other.md', 'cursor': 0, 'scroll': 0}],
            'active': 'Note.md',
            'note_windows': [
                {'path': 'Note.md', 'width': 12, 'height': 5},
                {'path': 'Note.md', 'width': 9000, 'height': 8000},
                {'path': 'Other.md', 'width': 300, 'height': 200},
                {'path': 'Other.md', 'width': 4000, 'height': 3000},
                {'path': 'Note.md', 'width': 500, 'height': 3.5},
                {'path': 'Note.md', 'width': True, 'height': 400},
                {'path': 'Missing.md', 'width': 800, 'height': 600},
                'not-a-window',
            ],
        }
        with patch.object(NoteWindow, 'resize', spy):
            self.assertTrue(self.window.restore_workspace(state))
        self.drain()
        self.assertEqual(requested, [
            ('Note.md', 300, 200),
            ('Note.md', 4000, 3000),
            ('Other.md', 300, 200),
            ('Other.md', 4000, 3000),
        ])

    def test_restore_opens_at_most_thirty_note_windows(self):
        created = []

        class RecordingWindow:
            def __init__(self, owner, pane):
                self.owner, self.pane = owner, pane
                self.requested = None
                owner.note_windows.add(self)
                created.append(self)

            def resize(self, width, height):
                self.requested = (width, height)

            def get_size(self):
                width, height = self.requested or (720, 600)
                return SimpleNamespace(width=width, height=height)

            def destroy(self):
                self.owner.note_windows.discard(self)

        entries = [{'path': 'Note.md', 'width': 500, 'height': 400} for _ in range(30)]
        entries.append({'path': 'Note.md', 'width': 1111, 'height': 1111})
        try:
            with patch('librevault.layout_ui.NoteWindow', RecordingWindow):
                self.assertTrue(self.window.restore_workspace({
                    'tabs': [{'path': 'Note.md', 'cursor': 0, 'scroll': 0}],
                    'note_windows': entries,
                }))
            self.assertEqual(len(created), 30)
            self.assertTrue(all(window.requested == (500, 400) for window in created))
        finally:
            for window in created:
                self.window.note_windows.discard(window)

    def assert_transient(self, dialog, *, modal, size, destroy_with_parent=None):
        self.assertIs(dialog.get_transient_for(), self.window)
        self.assertEqual(dialog.get_modal(), modal)
        self.assertEqual(tuple(dialog.get_default_size()), size)
        if destroy_with_parent is not None:
            self.assertEqual(dialog.get_destroy_with_parent(), destroy_with_parent)

    def test_utility_windows_stay_attached_to_the_main_window(self):
        cases = [
            (lambda: self.window.show_settings() or self.window.settings_dialog,
             True, (860, 620), True),
            (self.window.show_version_history, True, (780, 560), True),
            (lambda: self.window.show_bookmarks() or self.window.bookmark_manager,
             True, (520, 420), True),
            (lambda: self.window.manage_workspaces() or self.window.workspace_manager,
             True, (420, 360), True),
            (lambda: self.window.show_keybinds() or self.window.cheatsheet,
             True, (760, 640), True),
            (self.window.show_converter, True, (520, 260), None),
            (self.window.show_slides, False, (1100, 760), True),
            (lambda: self.window.show_graph() or self.window.graph_window,
             False, (1000, 720), True),
        ]
        for open_dialog, modal, size, destroy_with_parent in cases:
            dialog = open_dialog()
            with self.subTest(title=dialog.get_title()):
                self.assertIsNotNone(dialog)
                self.assert_transient(dialog, modal=modal, size=size, destroy_with_parent=destroy_with_parent)
            dialog.destroy()
            self.drain()

    def test_blocking_dialogs_are_modal_for_the_main_window(self):
        seen = {}

        def close_error():
            dialog = next(window for window in Gtk.Window.list_toplevels()
                          if isinstance(window, Gtk.MessageDialog)
                          and window.get_transient_for() is self.window)
            seen['error_parent'] = dialog.get_transient_for()
            seen['error_modal'] = dialog.get_modal()
            dialog.response(Gtk.ResponseType.CLOSE)
            return False

        GLib.idle_add(close_error)
        self.window.error(VaultError('Could not save'))
        self.assertIs(seen['error_parent'], self.window)
        self.assertTrue(seen['error_modal'])

        def cancel_new_note():
            dialog = next(window for window in Gtk.Window.list_toplevels()
                          if isinstance(window, Gtk.Dialog) and window.get_title() == 'New note')
            entry = next(child for child in dialog.get_content_area().get_children()
                         if isinstance(child, Gtk.Entry))
            seen['note_parent'] = dialog.get_transient_for()
            seen['note_modal'] = dialog.get_modal()
            seen['entry_width'] = entry.get_size_request().width
            dialog.response(Gtk.ResponseType.CANCEL)
            return False

        GLib.idle_add(cancel_new_note)
        self.window.new_note()
        self.assertIs(seen['note_parent'], self.window)
        self.assertTrue(seen['note_modal'])
        self.assertEqual(seen['entry_width'], 430)
        self.assertFalse((self.root / 'Untitled.md').exists())

    def column_widths(self, widget):
        return [column[0].get_size_request().width for column in widget.table_columns]

    def allocate_editor(self, width):
        """Deliver a width as a size-allocate would, without asking the display to resize."""
        editor = self.pane.editor
        with patch.object(editor, 'get_allocated_width', return_value=width):
            self.pane.size_editor(editor, SimpleNamespace(width=width))

    def test_readable_width_and_table_columns_follow_the_editor_allocation(self):
        from librevault.tables import format_table
        prose = 'A longer description with several words. ' * 12
        self.pane.window.preferences = replace(self.window.preferences, readable_length=True, line_width=680)
        self.show_table(format_table(['One', 'Two', 'Three'], ['left', 'left', 'left'], [[prose, prose, prose]]))
        widget = self.pane.table_widgets[0][2]
        for label in widget.table_columns[0]:
            self.assertTrue(label.get_line_wrap())
            self.assertEqual(label.get_max_width_chars(), 32)

        # Wider than the writing column: margins center it, and the three
        # capped 360px columns share line_width minus the preview gaps.
        # available = 1600 - 2 * ((1600 - 680) // 2) = 680
        # budget = 680 - 22 - 14 * 2 = 630, so each column lands on 210.
        self.allocate_editor(1600)
        self.assertEqual(self.pane.editor.get_left_margin(), 460)
        self.assertEqual(self.pane.editor.get_right_margin(), 460)
        self.assertEqual(self.pane.tags['quote'].get_property('left-margin'), 478)
        self.assertEqual(self.column_widths(widget), [210, 210, 210])

        # Narrower than the column: margins stay at the 32px floor and the
        # same columns share the editor width that remains.
        # budget = (420 - 64) - 22 - 28 = 306, so each column lands on 102.
        self.allocate_editor(420)
        self.assertEqual(self.pane.editor.get_left_margin(), 32)
        self.assertEqual(self.pane.editor.get_right_margin(), 32)
        self.assertEqual(self.column_widths(widget), [102, 102, 102])

        self.pane.window.preferences = replace(self.window.preferences, readable_length=False)
        self.allocate_editor(1600)
        self.assertEqual(self.pane.editor.get_left_margin(), 32)
        self.assertEqual(self.column_widths(widget), [360, 360, 360])

    def test_narrow_editor_keeps_the_table_column_floor(self):
        from librevault.tables import format_table
        prose = 'A longer description with several words. ' * 12
        self.show_table(format_table(['One', 'Two', 'Three'], ['left'] * 3, [[prose, prose, prose]]))
        widget = self.pane.table_widgets[0][2]
        # 80px times three columns is the budget floor, which is wider than this editor.
        self.allocate_editor(200)
        self.assertEqual(self.column_widths(widget), [80, 80, 80])

    def test_a_short_table_column_stays_put_while_prose_shrinks(self):
        from librevault.tables import format_table
        prose = 'A longer description with several words. ' * 12
        self.show_table(format_table(['ID', 'Notes'], ['left', 'left'], [['7', prose]]))
        widget = self.pane.table_widgets[0][2]
        self.pane.editor.set_left_margin(32)
        self.pane.editor.set_right_margin(32)
        with patch.object(self.pane.editor, 'get_allocated_width', return_value=2000):
            self.pane.size_table_preview(widget)
        short, long = self.column_widths(widget)
        self.assertEqual(long, 360)
        self.assertGreaterEqual(short, 40)
        self.assertLess(short, 80)
        # Ask for a budget of short + 160. Only the wider column is reduced.
        gaps = 22 + 14
        allocated = (short + 160) + gaps + 64
        with patch.object(self.pane.editor, 'get_allocated_width', return_value=allocated):
            self.pane.size_table_preview(widget)
        self.assertEqual(self.column_widths(widget), [short, 160])

    def show_table(self, text):
        self.pane.buffer.set_text('Before\n' + text + '\nAfter\n')
        self.pane.buffer.place_cursor(self.pane.buffer.get_start_iter())
        self.pane.render()
        self.drain()
        self.assertEqual(len(self.pane.table_widgets), 1)

    def test_callout_preview_tracks_the_writing_column_and_its_minimum(self):
        self.pane.window.preferences = replace(self.window.preferences, readable_length=True, line_width=680)
        self.pane.buffer.set_text('Intro\n> [!NOTE] Title\n> body\n')
        self.pane.buffer.place_cursor(self.pane.buffer.get_start_iter())
        self.pane.render()
        self.drain()
        widget = self.pane.callout_widgets[0][2]
        self.allocate_editor(1600)
        self.assertEqual(widget.get_size_request().width, 680)
        self.allocate_editor(200)
        self.assertEqual(widget.get_size_request().width, 136)
        self.allocate_editor(100)
        self.assertEqual(self.pane.editor.get_left_margin(), 32)
        self.assertEqual(widget.get_size_request().width, 120)

    def test_details_switch_to_a_menu_when_the_sidebar_is_narrower_than_its_buttons(self):
        required = sum(button.get_preferred_width().natural_width
                       for button in self.window.details_buttons.values()) + 46
        self.window.size_details_selector(None, SimpleNamespace(width=required - 1))
        self.assertTrue(self.window.details_combo.get_visible())
        self.assertFalse(self.window.details_selector.get_visible())
        self.window.size_details_selector(None, SimpleNamespace(width=required))
        self.assertFalse(self.window.details_combo.get_visible())
        self.assertTrue(self.window.details_selector.get_visible())
        self.assertEqual(self.window.details_view, 'outline')

    def test_command_palette_tracks_the_overlay_width_up_to_its_cap(self):
        self.window.show_command_palette()
        palette = self.window.palette
        self.addCleanup(palette.destroy)
        palette.resize_card(palette, SimpleNamespace(width=1600))
        self.assertEqual(palette.card.get_size_request().width, CommandPalette.max_width)
        palette.resize_card(palette, SimpleNamespace(width=300))
        self.assertEqual(palette.card.get_size_request().width, 268)
        palette.resize_card(palette, SimpleNamespace(width=16))
        self.assertEqual(palette.card.get_size_request().width, 1)

    def test_minimizing_the_graph_window_freezes_it(self):
        self.window.show_graph()
        graph = self.window.graph_window
        self.addCleanup(graph.destroy)
        view = graph.graph
        view.active = True
        view.paths = ['Note.md']
        view.edges = []
        focused = SimpleNamespace(new_window_state=Gdk.WindowState.FOCUSED)
        self.assertFalse(graph.window_state(graph, focused))
        self.assertTrue(view.active)
        iconified = SimpleNamespace(new_window_state=Gdk.WindowState.ICONIFIED)
        self.assertFalse(graph.window_state(graph, iconified))
        self.assertFalse(view.active)
        self.assertIn('nodes', view.status.get_text())

    def test_slides_request_fullscreen_without_relying_on_the_display(self):
        slides = self.window.show_slides()
        self.addCleanup(slides.destroy)
        self.assertIs(slides.get_transient_for(), self.window)
        self.assertFalse(slides.get_modal())
        calls = []
        with patch.object(slides, 'fullscreen', lambda: calls.append('on')), \
                patch.object(slides, 'unfullscreen', lambda: calls.append('off')):
            slides.toggle_fullscreen()
            self.assertTrue(slides.fullscreen_on)
            slides.toggle_fullscreen()
            self.assertFalse(slides.fullscreen_on)
        self.assertEqual(calls, ['on', 'off'])
