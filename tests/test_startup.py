"""Welcome startup, optional vault arguments, and working-directory-safe launch."""
from tests.gui_wait import wait_for_work
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

try:
    import librevault.app as application
    from gi.repository import Gio, GLib, Gtk
    GI_AVAILABLE = True
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GI_AVAILABLE = GTK_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_launcher_preserves_caller_directory_and_optional_argument(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            caller, fake_bin = base / 'caller', base / 'bin'
            caller.mkdir()
            fake_bin.mkdir()
            capture = base / 'capture.json'
            executable = fake_bin / 'python3'
            executable.write_text(f'#!{sys.executable}\n' +
                'import json, os, sys\nfrom pathlib import Path\n'
                "Path(os.environ['LIBREVAULT_LAUNCH_CAPTURE']).write_text(json.dumps({"
                "'cwd':os.getcwd(),'args':sys.argv[1:],'path':os.environ['PYTHONPATH']}))\n")
            executable.chmod(0o755)
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get('PATH', ''),
                       PYTHONPATH='prior-path', LIBREVAULT_LAUNCH_CAPTURE=str(capture))
            for args in ([], ['vault with spaces']):
                subprocess.run([str(ROOT / 'run.sh'), *args], cwd=caller, env=env, check=True)
                data = json.loads(capture.read_text())
                self.assertEqual(data['cwd'], str(caller))
                self.assertEqual(data['args'], ['-m', 'librevault', *args])
                self.assertEqual(data['path'].split(os.pathsep)[0], str(ROOT))

    @unittest.skipUnless(GI_AVAILABLE, 'GTK imports are required')
    def test_help_runs_from_outside_the_repository(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run([str(ROOT / 'run.sh'), '--help'], cwd=directory,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('[vault]', result.stdout)


@unittest.skipUnless(GI_AVAILABLE, 'GTK imports are required')
class ApplicationArgumentTests(unittest.TestCase):
    def test_no_arguments_activate_welcome_and_explicit_paths_use_open_events(self):
        with patch.object(application.Gtk, 'Application') as constructor, patch.object(application, 'Window') as window:
            app = constructor.return_value
            app.run.return_value = 0
            self.assertEqual(application.main([]), 0)
            app.run.assert_called_once_with(['libre-vault'])
            callbacks = dict((call.args[0], call.args[1]) for call in app.connect.call_args_list)
            callbacks['activate'](app)
            window.assert_called_once_with(app)
            window.reset_mock()
            with TemporaryDirectory() as directory:
                path = str(Path(directory) / 'second vault')
                callbacks['open'](app, [Gio.File.new_for_path(path)], 1, '')
                window.assert_called_once_with(app, path)
            self.assertEqual(constructor.call_args.kwargs['flags'], Gio.ApplicationFlags.HANDLES_OPEN)

    def test_vault_argument_is_forwarded_as_absolute_path(self):
        with patch.object(application.Gtk, 'Application') as constructor:
            application.main(['relative-vault'])
            constructor.return_value.run.assert_called_once_with(
                ['libre-vault', str(Path('relative-vault').resolve())])


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class WelcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.WelcomeTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        self.preferences = Path(self.directory.name) / 'config' / 'settings.json'
        self.window = application.Window(self.app, preferences_path=self.preferences)
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)

    def test_no_vault_has_welcome_and_no_filesystem_side_effects(self):
        self.assertIsNone(self.window.vault)
        self.assertIsNone(self.window.current())
        self.assertEqual(self.window.workspace_stack.get_visible_child_name(), 'welcome')
        self.assertTrue(self.window.open_vault_button.get_visible())
        self.assertFalse(self.window.save_button.get_sensitive())
        self.assertFalse(self.window.notes_toggle.get_visible())
        self.assertFalse(self.window.details_toggle.get_visible())
        self.assertTrue(self.window.refresh())
        self.window.toggle_file_tree()
        self.window.toggle_details()
        self.assertTrue(self.window.notes_toggle.get_active())
        self.assertTrue(self.window.details_toggle.get_active())
        self.assertFalse(self.window.on_delete())
        self.assertFalse(self.preferences.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_open_button_enters_workspace_after_folder_selection(self):
        (self.root / 'Note.md').write_text('# Note\n')
        with patch.object(application.Gtk, 'FileChooserDialog') as chooser:
            chooser.return_value.run.return_value = Gtk.ResponseType.OK
            chooser.return_value.get_filename.return_value = str(self.root)
            self.window.open_vault_button.clicked()
            wait_for_work(self.window)
        self.assertEqual(self.window.workspace_stack.get_visible_child_name(), 'workspace')
        self.assertEqual(self.window.current().note.path, 'Note.md')
        self.assertTrue(self.window.save_button.get_sensitive())
        self.assertTrue(self.window.notes_toggle.get_visible())
        self.assertFalse(self.preferences.exists())

    def test_cancel_folder_selection_keeps_welcome(self):
        with patch.object(application.Gtk, 'FileChooserDialog') as chooser:
            chooser.return_value.run.return_value = Gtk.ResponseType.CANCEL
            self.window.open_vault_button.clicked()
            wait_for_work(self.window)
        self.assertIsNone(self.window.vault)
        self.assertEqual(self.window.workspace_stack.get_visible_child_name(), 'welcome')

    def test_opening_from_welcome_initializes_matching_panel_widths_once(self):
        def settle():
            loop = GLib.MainLoop()
            GLib.timeout_add(100, lambda: (loop.quit(), False)[1])
            loop.run()

        def settle_sidebars():
            previous = None
            for _ in range(20):
                settle()
                width = self.window.get_allocated_width()
                left = self.window.notes_panel.get_allocated_width()
                right = self.window.details_panel.get_allocated_width()
                if (width == previous and width > 1 and not self.window._fitting_sidebars
                        and abs(left - right) <= 1 and left > 1):
                    return
                previous = width
            self.fail('sidebars did not settle')

        settle()
        self.assertFalse(self.window.panel_widths_initialized)
        (self.root / 'Note.md').write_text('# Note\n\n## Section\n')
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        settle_sidebars()
        left = self.window.notes_panel.get_allocated_width()
        right = self.window.details_panel.get_allocated_width()
        self.assertTrue(self.window.panel_widths_initialized)
        self.assertLessEqual(abs(left - right), 1)
        self.assertLessEqual(right, 300)  # Keep the initial Outline panel compact.

        # User adjustments must survive subsequent layouts and vault opens.
        self.window.details_panel.position_width(right + 60)
        settle()
        adjusted = self.window.details_panel.get_allocated_width()
        self.assertAlmostEqual(adjusted, right + 60, delta=1)
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        self.window.queue_resize()
        settle()
        self.assertAlmostEqual(self.window.details_panel.get_allocated_width(), adjusted, delta=1)

    def test_empty_folder_can_be_opened_before_creating_first_note(self):
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        self.assertEqual(self.window.workspace_stack.get_visible_child_name(), 'workspace')
        self.assertIsNone(self.window.current())
        self.assertFalse(self.window.save_button.get_sensitive())
        note = self.window.vault.create('First')
        self.window.open_note(note.path)
        self.assertTrue(self.window.save_button.get_sensitive())

    def test_settings_and_palette_work_before_opening_vault(self):
        self.window.settings_button.clicked()
        self.assertTrue(self.window.settings_dialog.get_visible())
        self.window.settings_dialog.destroy()
        self.window.show_command_palette()
        self.window.palette.search.emit('activate')
        self.assertTrue(self.window.cheatsheet.get_visible())
        self.assertIsNone(self.window.vault)
        self.assertFalse(self.preferences.exists())

    def test_invalid_startup_path_leaves_welcome_available(self):
        with patch.object(application.Window, 'error') as error:
            window = application.Window(self.app, self.root / 'missing', preferences_path=self.preferences)
            wait_for_work(window)
            self.addCleanup(window.destroy)
        error.assert_called_once()
        self.assertIsNone(window.vault)
        self.assertEqual(window.workspace_stack.get_visible_child_name(), 'welcome')

    def test_launch_reopens_last_vault_and_explicit_path_takes_precedence(self):
        (self.root / 'Note.md').write_text('# Note\n')
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        self.window.destroy()
        reopened = application.Window(self.app, preferences_path=self.preferences)
        wait_for_work(reopened)
        self.addCleanup(reopened.destroy)
        self.assertEqual(reopened.vault.root, self.root)
        self.assertEqual(reopened.current().note.path, 'Note.md')
        other = Path(self.directory.name) / 'other'
        other.mkdir()
        explicit = application.Window(self.app, other, preferences_path=self.preferences)
        wait_for_work(explicit)
        self.addCleanup(explicit.destroy)
        self.assertEqual(explicit.vault.root, other)
        self.assertEqual(explicit.last_vault_store.load(), str(other))

    def test_unavailable_last_vault_leaves_welcome_without_a_modal_error(self):
        missing = self.root / 'missing'
        self.window.last_vault_store.save(missing)
        with patch.object(application.Window, 'error') as error:
            window = application.Window(self.app, preferences_path=self.preferences)
            wait_for_work(window)
            self.addCleanup(window.destroy)
        error.assert_not_called()
        self.assertIsNone(window.vault)
        self.assertEqual(window.workspace_stack.get_visible_child_name(), 'welcome')
        self.assertIn('Last vault unavailable', window.status.get_text())
        self.assertFalse(missing.exists())

    def test_malformed_last_vault_is_ignored(self):
        path = self.window.last_vault_store.path
        path.parent.mkdir(parents=True)
        for contents in ('broken json', '[]', '{"last_vault": []}', '{"last_vault": "relative"}'):
            with self.subTest(contents=contents):
                path.write_text(contents)
                window = application.Window(self.app, preferences_path=self.preferences)
                wait_for_work(window)
                self.assertIsNone(window.vault)
                window.destroy()

    def test_close_vault_from_palette_returns_to_welcome_and_forgets_startup(self):
        (self.root / 'Note.md').write_text('# Note\n')
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.current()
        monitor = self.window.vault_monitor
        self.window.open_split(pane)
        self.window.show_command_palette()
        self.window.palette.search.set_text('close vault')
        self.window.palette.run_selected()
        self.assertIsNone(self.window.vault)
        self.assertTrue(monitor.closed)
        self.assertIsNone(self.window.recovery)
        self.assertIsNone(self.window.split_view)
        self.assertFalse(self.window.panes)
        self.assertEqual(self.window.tabs.get_n_pages(), 0)
        self.assertEqual(self.window.workspace_stack.get_visible_child_name(), 'welcome')
        self.assertIsNone(self.window.last_vault_store.load())
        self.assertEqual(self.window.workspace_store.load(self.root)['tabs'][0]['path'], 'Note.md')
        reopened = application.Window(self.app, preferences_path=self.preferences)
        wait_for_work(reopened)
        self.addCleanup(reopened.destroy)
        self.assertIsNone(reopened.vault)

    def test_cancel_close_retains_dirty_note_and_last_vault(self):
        (self.root / 'Note.md').write_text('# Note\n')
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.current()
        pane.buffer.insert_at_cursor('local change')
        with patch.object(application.Gtk, 'MessageDialog') as dialog:
            dialog.return_value.run.return_value = Gtk.ResponseType.CANCEL
            self.assertFalse(self.window.close_vault())
        self.assertIs(self.window.current(), pane)
        self.assertTrue(pane.dirty)
        self.assertEqual(self.window.last_vault_store.load(), str(self.root))

    def test_discard_close_cancels_autosave_and_save_close_preserves_edits(self):
        (self.root / 'Note.md').write_text('# Note\n')
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        pane = self.window.current()
        pane.buffer.insert_at_cursor('discard this')
        with patch.object(application.Gtk, 'MessageDialog') as dialog:
            dialog.return_value.run.return_value = Gtk.ResponseType.NO
            self.assertTrue(self.window.close_vault())
        self.assertIsNone(pane.autosave_source)
        self.assertEqual((self.root / 'Note.md').read_text(), '# Note\n')
        self.window.load_vault(self.root)
        wait_for_work(self.window)
        self.window.current().buffer.insert_at_cursor('save this')
        with patch.object(application.Gtk, 'MessageDialog') as dialog:
            dialog.return_value.run.return_value = Gtk.ResponseType.YES
            self.assertTrue(self.window.close_vault())
        self.assertIn('save this', (self.root / 'Note.md').read_text())
