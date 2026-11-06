"""Plugin discovery, lifecycle, and editor API without a display."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from librevault.editing import TextEdit
from librevault.plugins import Plugin, PluginAPI, PluginManager
from librevault.preferences import Preferences, PreferencesStore


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.window = SimpleNamespace(closed=False, status=Mock(), vault=None, current=Mock(return_value=None))
        self.manager = PluginManager(self.window, self.root / 'plugins')
        self.addCleanup(self.manager.unload)

    def write_plugin(self, name='sample', source=None):
        self.manager.directory.mkdir(exist_ok=True)
        path = self.manager.directory / f'{name}.py'
        path.write_text(source or '''API_VERSION = 1
def setup(api):
    api.register_command('hello', 'Say hello', lambda: api.show_status('Hello'))
''', encoding='utf-8')
        return path

    def test_disabled_plugins_are_not_discovered_or_executed(self):
        self.manager.reload()
        self.assertFalse(self.manager.directory.exists())
        self.write_plugin(source='raise AssertionError("must not execute")')
        self.manager.reload()
        self.assertEqual(self.manager.plugins, {})
        self.assertEqual(self.manager.errors, [])

    def test_preferences_require_boolean_opt_in_and_persist_it(self):
        for value in (None, 'true', 1, [], {}):
            self.assertFalse(Preferences.from_dict({'python_plugins': value}).python_plugins)
        self.assertFalse(Preferences().python_plugins)
        store = PreferencesStore(self.root / 'settings.json')
        enabled = Preferences(python_plugins=True)
        store.save(enabled)
        self.assertEqual(store.load(), enabled)

    def test_discovery_uses_only_visible_single_files_and_sorted_order(self):
        self.write_plugin('zebra')
        self.write_plugin('alpha')
        for name in ('_private', '.hidden', 'not-a-python-id'):
            self.write_plugin(name, 'raise AssertionError("must not execute")')
        (self.manager.directory / 'package.py').mkdir()
        (self.manager.directory / 'package.py' / 'nested.py').write_text('raise AssertionError')
        (self.manager.directory / 'link.py').symlink_to(self.manager.directory / 'alpha.py')
        self.manager.set_enabled(True)
        self.assertEqual(list(self.manager.plugins), ['alpha', 'zebra'])
        self.assertEqual(self.manager.errors, [])
        actions = self.manager.command_actions()
        self.assertEqual([title for title, _ in actions], ['alpha: Say hello', 'zebra: Say hello'])
        actions[0][1]()
        self.window.status.set_text.assert_called_once_with('Hello')

    def test_reload_reads_fresh_source_and_invalidates_old_actions(self):
        path = self.write_plugin()
        self.manager.set_enabled(True)
        old = self.manager.plugins['sample']
        old_action = self.manager.command_actions()[0][1]
        path.write_text(path.read_text().replace('Hello', 'Fresh'))
        self.manager.reload()
        self.assertNotIn(old.module.__name__, sys.modules)
        old_action()
        self.window.status.set_text.assert_not_called()
        self.manager.command_actions()[0][1]()
        self.window.status.set_text.assert_called_once_with('Fresh')
        self.assertFalse((self.manager.directory / '__pycache__').exists())
        path.unlink()
        self.manager.reload()
        self.assertEqual(self.manager.command_actions(), [])

    def test_setup_failure_rolls_back_commands_and_runs_cleanup(self):
        self.write_plugin(source='''API_VERSION = 1
def setup(api):
    api.register_command('partial', 'Partial', lambda: None)
    api.on_unload(lambda: open(__file__ + '.cleaned', 'w').close())
    raise RuntimeError('setup failed')
''')
        self.write_plugin('working')
        before = set(sys.modules)
        with self.assertLogs('librevault.plugins', level='ERROR'):
            self.manager.set_enabled(True)
        self.assertEqual(list(self.manager.plugins), ['working'])
        self.assertTrue((self.manager.directory / 'sample.py.cleaned').exists())
        self.assertIn('setup failed', self.manager.errors[0])
        added = {name for name in set(sys.modules) - before if name.startswith('_librevault_plugin_')}
        self.assertEqual(added, {self.manager.plugins['working'].module.__name__})

    def test_load_errors_do_not_prevent_other_plugins_loading(self):
        for name, source in {
            'syntax': 'this is invalid python',
            'import_error': 'raise ImportError("missing dependency")',
            'version': 'API_VERSION = 2',
            'bool_version': 'API_VERSION = True',
            'missing_setup': 'API_VERSION = 1',
            'exit': 'raise SystemExit(1)',
            'duplicate': '''API_VERSION = 1
def setup(api):
    api.register_command('same', 'First', lambda: None)
    api.register_command('same', 'Second', lambda: None)
''',
        }.items():
            self.write_plugin(name, source)
        self.write_plugin('working')
        with self.assertLogs('librevault.plugins', level='ERROR'):
            self.manager.set_enabled(True)
        self.assertEqual(list(self.manager.plugins), ['working'])
        self.assertEqual(len(self.manager.errors), 7)

    def test_command_errors_are_reported_and_bounded(self):
        self.write_plugin(source='''API_VERSION = 1
def fail():
    raise SystemExit('command failed')
def setup(api):
    api.register_command('fail', 'Fail', fail)
''')
        self.manager.set_enabled(True)
        with self.assertLogs('librevault.plugins', level='ERROR'):
            for _ in range(25):
                self.manager.command_actions()[0][1]()
        self.assertEqual(len(self.manager.errors), 20)
        self.assertIn('sample (command fail): SystemExit: command failed', self.manager.errors[-1])

    def test_disable_cleans_up_in_reverse_order_even_when_cleanup_fails(self):
        self.write_plugin(source='''API_VERSION = 1
events = []
def fail():
    events.append('fail')
    raise RuntimeError('cleanup failed')
def setup(api):
    global context
    context = api
    api.on_unload(lambda: events.append('first'))
    api.on_unload(fail)
    api.on_unload(lambda: events.append('last'))
    api.register_command('hello', 'Hello', lambda: api.show_status('hello'))
''')
        self.manager.set_enabled(True)
        plugin = self.manager.plugins['sample']
        action = self.manager.command_actions()[0][1]
        with self.assertLogs('librevault.plugins', level='ERROR'):
            self.manager.set_enabled(False)
        self.assertEqual(plugin.module.events, ['last', 'fail', 'first'])
        self.assertNotIn(plugin.module.__name__, sys.modules)
        self.assertEqual(self.manager.command_actions(), [])
        with self.assertRaisesRegex(RuntimeError, 'unloaded'):
            plugin.module.context.show_status('stale')
        self.window.status.reset_mock()
        action()
        self.window.status.set_text.assert_not_called()
        self.manager.unload()
        self.assertEqual(plugin.module.events, ['last', 'fail', 'first'])

    def test_window_managers_have_independent_modules(self):
        self.write_plugin(source='''from dataclasses import dataclass
API_VERSION = 1
@dataclass
class State:
    count: int = 0
state = State()
def setup(api):
    state.count += 1
''')
        other = PluginManager(self.window, self.manager.directory)
        self.addCleanup(other.unload)
        self.manager.set_enabled(True)
        other.set_enabled(True)
        first = self.manager.plugins['sample'].module
        second = other.plugins['sample'].module
        self.assertIsNot(first, second)
        self.assertEqual(first.state.count, 1)
        self.assertEqual(second.state.count, 1)
        self.manager.set_enabled(True)
        self.assertIs(self.manager.plugins['sample'].module, first)

    def test_unreadable_directory_is_reported(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('no access')):
            with self.assertLogs('librevault.plugins', level='ERROR'):
                self.manager.set_enabled(True)
        self.assertIn('discovery', self.manager.errors[0])
        self.assertEqual(self.manager.plugins, {})


class PluginAPITests(unittest.TestCase):
    def setUp(self):
        self.pane = Mock()
        self.pane.note.path = 'Note.md'
        self.pane.text.return_value = 'hello world'
        self.pane.buffer.get_selection_bounds.return_value = ()
        self.pane.buffer.get_iter_at_mark.return_value.get_offset.return_value = 5
        self.window = SimpleNamespace(vault=SimpleNamespace(root=Path('/vault')),
                                      current=Mock(return_value=self.pane), status=Mock())
        self.plugin = Plugin('sample', ModuleType('sample'))
        self.api = PluginAPI(self.window, self.plugin)

    def test_selection_edits_use_editor_undo_path(self):
        self.assertEqual(self.api.get_selection(), '')
        self.api.replace_selection('!')
        self.pane.apply_edit.assert_called_once_with(TextEdit(5, 5, '!'))
        self.pane.buffer.get_selection_bounds.return_value = (
            Mock(get_offset=Mock(return_value=6)), Mock(get_offset=Mock(return_value=11)))
        self.assertEqual(self.api.get_selection(), 'world')
        self.api.replace_selection('everyone')
        self.pane.apply_edit.assert_called_with(TextEdit(6, 11, 'everyone'))
        with self.assertRaises(TypeError):
            self.api.replace_selection(None)

    def test_context_tracks_active_note_and_vault(self):
        self.assertEqual(self.api.get_text(), 'hello world')
        self.assertEqual(self.api.current_note_path, 'Note.md')
        self.assertEqual(self.api.vault_root, Path('/vault'))
        self.window.current.return_value = None
        self.window.vault = None
        self.assertIsNone(self.api.current_note_path)
        self.assertIsNone(self.api.vault_root)
        for action in (self.api.get_text, self.api.get_selection, lambda: self.api.replace_selection('text')):
            with self.assertRaisesRegex(RuntimeError, 'Open a Markdown note'):
                action()

    def test_registration_validation_and_setup_only_commands(self):
        for args in (('', 'Title', lambda: None), ('ok', ' ', lambda: None), ('ok', 'Title', None)):
            with self.assertRaises((ValueError, TypeError)):
                self.api.register_command(*args)
        with self.assertRaises(TypeError):
            self.api.on_unload(None)
        self.plugin.registering = False
        with self.assertRaisesRegex(RuntimeError, 'during setup'):
            self.api.register_command('ok', 'Title', lambda: None)
