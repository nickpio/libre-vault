"""Templates, daily notes, unique notes, and random notes."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.preferences import Preferences, PreferencesStore
from librevault.shortcuts import APP_SHORTCUTS
from librevault.templates import (
    create_note_from_template, create_unique_note, daily_note_path, ensure_daily_note,
    expand_template, format_moment, random_note_path, template_paths, unique_note_path,
)
from librevault.vault import Vault, VaultError

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from librevault.commands import TemplatePicker
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


WHEN = datetime(2026, 11, 2, 8, 0, 0)
DAY = '# {{title}}\n{{date}} {{time}}\n{{date:dddd}} {{time:ss}}\n'


class TemplateLogicTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'vault'
        self.root.mkdir()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def open_vault(self):
        vault = Vault(self.root, cache_dir=Path(self.temporary.name) / 'cache')
        self.addCleanup(vault.close)
        return vault

    def test_moment_tokens_and_literal_runs(self):
        moment = datetime(2026, 11, 2, 14, 5, 9)
        cases = {
            'YYYY-MM-DD': '2026-11-02',
            'YY': '26',
            'HH:mm:ss': '14:05:09',
            'dddd': 'Monday',
            'ddd': 'Mon',
            'YYYYMMDDHHmm': '202611021405',
            'Note YYYY': 'Note 2026',
            'MMMM': 'MMMM',
            'YYYYYY': 'YYYYYY',
            'ddddd': 'ddddd',
            '': '',
        }
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(format_moment(moment, pattern), expected)

    def test_template_variables_expand_once(self):
        moment = datetime(2026, 11, 2, 14, 5, 9)
        text = '{{title}} {{date}} {{time}} {{ date : YYYY }} {{ time : HH }}\n{{title:kept}} {{created}}'
        expanded = expand_template(text, title='{{date}}', when=moment)
        self.assertEqual(expanded, '{{date}} 2026-11-02 14:05 2026 14\n{{title:kept}} {{created}}')
        self.assertEqual(expand_template('{{date:YYYY-MM-DD HH:mm:ss}}', title='Note', when=moment),
                         '2026-11-02 14:05:09')
        self.assertEqual(expand_template('{{date:}}', title='Note', when=moment), '2026-11-02')

    def test_preferences_accept_paths_and_reject_traversal(self):
        loaded = Preferences.from_dict({
            'theme': 'light',
            'templates_folder': '  Library/Templates  ',
            'daily_notes_folder': '',
            'daily_notes_format': 'YYYY/MM/DD',
            'daily_notes_template': 'Library/Templates/Day',
            'unique_notes_folder': 'Zettel',
            'unique_notes_format': 'YYYYMMDDHHmmss',
            'templates_folder_unused': '../nope',
        })
        self.assertEqual(loaded, replace(
            Preferences(), theme='light', templates_folder='Library/Templates', daily_notes_folder='',
            daily_notes_format='YYYY/MM/DD', daily_notes_template='Library/Templates/Day',
            unique_notes_folder='Zettel', unique_notes_format='YYYYMMDDHHmmss'))
        rejected = Preferences.from_dict({
            'templates_folder': '../Templates',
            'daily_notes_folder': '/etc',
            'daily_notes_format': '',
            'daily_notes_template': 'Hidden/.secret.md',
            'unique_notes_folder': 'a/../../b',
            'unique_notes_format': 'YYYY//MM',
        })
        self.assertEqual(rejected, Preferences())
        self.assertEqual(Preferences.from_dict({'templates_folder': None, 'daily_notes_format': False}).templates_folder,
                         'Templates')
        self.assertEqual(Preferences.from_dict({'daily_notes_format': 'Y' * 241}).daily_notes_format, 'YYYY-MM-DD')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            expected = replace(Preferences(), templates_folder='Snippets', daily_notes_folder='')
            PreferencesStore(path).save(expected)
            self.assertEqual(PreferencesStore(path).load(), expected)

    def test_shortcuts_are_registered_for_the_new_commands(self):
        handlers = {shortcut.handler: shortcut.description for shortcut in APP_SHORTCUTS}
        self.assertEqual(handlers['open_daily_note'], 'Open daily note')
        self.assertEqual(handlers['insert_template'], 'Insert template')
        self.assertEqual(handlers['new_unique_note'], 'New unique note')
        self.assertEqual(handlers['open_random_note'], 'Open random note')

    def test_template_discovery_stays_inside_the_folder(self):
        self.write('Templates/Day.md', 'day\n')
        self.write('Templates/Sub/Other.md', 'other\n')
        self.write('Templates-extra/Nope.md', 'nope\n')
        self.write('Projects/Nope.md', 'nope\n')
        self.write('Templates/Bad.md', 'bad\n')
        vault = self.open_vault()
        (self.root / 'Templates' / 'Bad.md').write_bytes(b'\xff')
        vault.refresh()
        self.assertEqual(template_paths(vault, 'Templates'), ['Templates/Day.md', 'Templates/Sub/Other.md'])
        self.assertEqual(template_paths(vault, 'Templates-extra'), ['Templates-extra/Nope.md'])
        self.assertNotIn('Templates/Bad.md', template_paths(vault, ''))
        self.assertIn('Projects/Nope.md', template_paths(vault, ''))
        self.assertEqual(template_paths(vault, '../Templates'), [])

    def test_daily_note_creates_nested_folders_and_applies_template_once(self):
        self.write('Templates/Day.md', DAY)
        vault = self.open_vault()
        preferences = replace(Preferences(), daily_notes_format='YYYY/MM/DD', daily_notes_template='Templates/Day')
        note, created = ensure_daily_note(vault, preferences, when=WHEN)
        self.assertTrue(created)
        self.assertEqual(note.path, 'Daily/2026/11/02.md')
        self.assertEqual(note.text, '# 02\n2026-11-02 08:00\nMonday 00\n')
        self.assertIn('Daily/2026/11', vault.folders)
        vault.save(note, 'keep\n')
        again, created = ensure_daily_note(vault, preferences, when=WHEN)
        self.assertFalse(created)
        self.assertEqual(again.text, 'keep\n')
        self.assertEqual((self.root / 'Daily' / '2026' / '11' / '02.md').read_text(encoding='utf-8'), 'keep\n')

    def test_daily_note_at_vault_root_and_missing_template_creates_nothing(self):
        vault = self.open_vault()
        note, created = ensure_daily_note(vault, replace(Preferences(), daily_notes_folder=''), when=WHEN)
        self.assertTrue(created)
        self.assertEqual(note.path, '2026-11-02.md')
        self.assertEqual(note.text, '')
        self.assertEqual(daily_note_path('Daily', 'YYYY-MM-DD', WHEN), 'Daily/2026-11-02.md')
        with self.assertRaises(VaultError):
            ensure_daily_note(vault, replace(Preferences(), daily_notes_template='Templates/Missing.md'), when=WHEN)
        self.assertFalse((self.root / 'Daily').exists())

    def test_existing_daily_file_is_opened_without_a_template(self):
        self.write('Daily/2026-11-02.md', 'keep\n')
        vault = self.open_vault()
        note, created = ensure_daily_note(vault, replace(Preferences(), daily_notes_template='Templates/Missing.md'), when=WHEN)
        self.assertFalse(created)
        self.assertEqual(note.text, 'keep\n')

    def test_unreadable_daily_note_is_not_replaced(self):
        self.write('Daily/2026-11-02.md', 'keep\n')
        vault = self.open_vault()
        (self.root / 'Daily' / '2026-11-02.md').write_bytes(b'\xff')
        vault.refresh()
        with self.assertRaises(VaultError):
            ensure_daily_note(vault, Preferences(), when=WHEN)
        self.assertEqual((self.root / 'Daily' / '2026-11-02.md').read_bytes(), b'\xff')

    def test_unique_note_title_template_folder_and_collision(self):
        self.write('Templates/Day.md', DAY)
        vault = self.open_vault()
        preferences = replace(Preferences(), unique_notes_folder='Zettel/Inbox', unique_notes_format='YYYYMMDDHHmmss')
        note = create_unique_note(vault, preferences, title='Plan', template='Templates/Day.md', when=WHEN)
        self.assertEqual(note.path, 'Zettel/Inbox/20261102080000 - Plan.md')
        self.assertEqual(note.text, '# 20261102080000 - Plan\n2026-11-02 08:00\nMonday 00\n')
        self.assertIn('Zettel/Inbox', vault.folders)
        self.assertEqual(unique_note_path('', 'YYYYMMDDHHmm', WHEN), '202611020800.md')
        self.assertEqual(unique_note_path('', 'YYYYMMDDHHmm', WHEN, 'Plan'), '202611020800 - Plan.md')
        first = create_unique_note(vault, Preferences(), when=WHEN)
        second = create_unique_note(vault, Preferences(), when=WHEN)
        self.assertEqual(first.path, '202611020800.md')
        self.assertEqual(second.path, '202611020800 1.md')
        before = set(vault.notes)
        with self.assertRaises(VaultError):
            create_unique_note(vault, Preferences(), title='../Secret', when=WHEN)
        self.assertEqual(set(vault.notes), before)
        with self.assertRaises(VaultError):
            create_unique_note(vault, Preferences(), template='Templates/Missing.md', when=datetime(2026, 11, 3, 8, 0, 0))
        self.assertNotIn('202611030800.md', vault.notes)

    def test_create_from_template_names_the_new_note(self):
        self.write('Templates/Day.md', DAY)
        vault = self.open_vault()
        note = create_note_from_template(vault, 'Templates/Day.md', 'Projects/café', when=WHEN)
        self.assertEqual(note.path, 'Projects/café.md')
        self.assertEqual(note.text, '# café\n2026-11-02 08:00\nMonday 00\n')
        self.assertTrue((self.root / 'Projects').is_dir())
        with self.assertRaises(VaultError):
            create_note_from_template(vault, 'Templates/Day.md', 'Projects/café', when=WHEN)
        with self.assertRaises(VaultError):
            create_note_from_template(vault, 'Templates/Day.md', '/etc/passwd', when=WHEN)
        self.assertFalse((self.root / 'etc').exists())

    def test_random_note_skips_unreadable_and_empty_vaults(self):
        self.write('Good.md', 'ok\n')
        self.write('Bad.md', 'nope\n')
        vault = self.open_vault()
        (self.root / 'Bad.md').write_bytes(b'\xff')
        vault.refresh()

        class Recorder:
            def choice(self, paths):
                self.paths = list(paths)
                return paths[-1]

        rng = Recorder()
        self.assertEqual(random_note_path(vault, rng), 'Good.md')
        self.assertEqual(rng.paths, ['Good.md'])
        empty_root = Path(self.temporary.name) / 'empty'
        empty_root.mkdir()
        empty = Vault(empty_root, cache_dir=Path(self.temporary.name) / 'empty-cache')
        self.addCleanup(empty.close)
        self.assertIsNone(random_note_path(empty))


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class TemplateWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.TemplateTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Original\n', encoding='utf-8')
        (self.root / 'Templates').mkdir()
        (self.root / 'Templates' / 'Day.md').write_text(DAY, encoding='utf-8')
        self.preferences_path = Path(self.directory.name) / 'settings.json'
        self.window = Window(self.app, self.root, preferences_path=self.preferences_path)
        self.addCleanup(self.window.destroy)
        self.drain()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def event(self, key, modifiers=0):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = key
        event.state = modifiers
        return event

    def dialog(self, title):
        return next(window for window in Gtk.Window.list_toplevels()
                    if isinstance(window, Gtk.Dialog) and window.get_title() == title and window.get_visible())

    def test_notes_settings_persist_and_reject_unsafe_paths(self):
        self.window.show_settings()
        dialog = self.window.settings_dialog
        self.assertEqual(dialog.categories.get_row_at_index(1).page_name, 'Editor')
        notes = next(row for row in dialog.categories.get_children() if row.page_name == 'Notes')
        dialog.categories.select_row(notes)
        self.assertEqual(dialog.stack.get_visible_child_name(), 'Notes')
        self.assertEqual(dialog.controls['templates_folder'].get_text(), 'Templates')
        self.assertEqual(dialog.controls['unique_notes_format'].get_text(), 'YYYYMMDDHHmm')
        dialog.controls['templates_folder'].set_text('Snippets')
        dialog.controls['daily_notes_format'].set_text('YYYY/MM/DD')
        self.assertEqual(self.window.preferences.templates_folder, 'Snippets')
        self.assertEqual(PreferencesStore(self.preferences_path).load().daily_notes_format, 'YYYY/MM/DD')
        dialog.controls['templates_folder'].set_text('../secret')
        self.assertEqual(self.window.preferences.templates_folder, 'Snippets')
        self.assertTrue(dialog.error.get_visible())
        dialog.controls['daily_notes_format'].set_text('')
        self.assertEqual(self.window.preferences.daily_notes_format, 'YYYY/MM/DD')
        dialog.controls['templates_folder'].set_text('Library')
        self.assertFalse(dialog.error.get_visible())
        dialog.save(Preferences())
        self.assertEqual(self.window.preferences, Preferences())
        self.assertEqual(dialog.controls['templates_folder'].get_text(), 'Templates')
        self.assertEqual(dialog.controls['daily_notes_template'].get_text(), '')

    def test_daily_note_shortcut_creates_and_reopens_without_replacing_text(self):
        self.window.set_preferences(replace(
            self.window.preferences, daily_notes_template='Templates/Day.md'))
        with patch('librevault.templates.current_time', return_value=WHEN):
            self.assertTrue(self.window.key_press(
                self.window, self.event(Gdk.KEY_d, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK)))
        pane = self.window.current()
        self.assertEqual(pane.note.path, 'Daily/2026-11-02.md')
        self.assertEqual(pane.text(), '# 2026-11-02\n2026-11-02 08:00\nMonday 00\n')
        self.assertIn('Daily/', [row.path for row in self.window.files.get_children()])
        self.assertIn('Daily/2026-11-02.md', [row.path for row in self.window.files.get_children()])
        pane.buffer.set_text('keep\n')
        pane.save()
        with patch('librevault.templates.current_time', return_value=WHEN):
            self.window.open_daily_note()
        self.assertEqual(self.window.current().text(), 'keep\n')
        self.assertEqual((self.root / 'Daily' / '2026-11-02.md').read_text(encoding='utf-8'), 'keep\n')
        self.assertIn('Opened daily note', self.window.status.get_text())

    def test_insert_template_is_one_undo_and_cancel_leaves_the_note(self):
        pane = self.window.current()
        original = pane.text()
        pane.buffer.place_cursor(pane.buffer.get_end_iter())

        def cancel():
            self.dialog('Insert template').response(Gtk.ResponseType.CANCEL)
            return False

        GLib.idle_add(cancel)
        self.window.insert_template()
        self.assertEqual(pane.text(), original)
        self.assertFalse(pane.dirty)

        def choose():
            picker = self.dialog('Insert template')
            self.assertIsInstance(picker, TemplatePicker)
            picker.search.set_text('day')
            self.assertEqual(picker.results.get_selected_row().path, 'Templates/Day.md')
            picker.search.emit('activate')
            return False

        with patch('librevault.templates.current_time', return_value=WHEN):
            GLib.idle_add(choose)
            self.window.insert_template()
        inserted = original + '# Note\n2026-11-02 08:00\nMonday 00\n'
        self.assertEqual(pane.text(), inserted)
        self.assertEqual(len(pane.history.undo_stack), 1)
        pane.undo()
        self.assertEqual(pane.text(), original)
        self.assertFalse(pane.dirty)
        pane.buffer.select_range(pane.buffer.get_start_iter(), pane.buffer.get_end_iter())
        with patch('librevault.templates.current_time', return_value=WHEN):
            self.window.insert_template(path='Templates/Day.md')
        self.assertEqual(pane.text(), '# Note\n2026-11-02 08:00\nMonday 00\n')
        self.window.insert_template(path='Templates/Day.md')
        self.assertEqual(len(pane.history.undo_stack), 2)
        pane.undo()
        self.assertEqual(pane.text(), '# Note\n2026-11-02 08:00\nMonday 00\n')

    def test_insert_without_a_note_or_templates_reports_status(self):
        self.window.close_tab()
        self.window.insert_template()
        self.assertIn('Open a note', self.window.status.get_text())
        self.window.open_note('Note.md')
        self.window.set_preferences(replace(self.window.preferences, templates_folder='Missing'))
        self.window.insert_template()
        self.assertIn('No templates', self.window.status.get_text())
        menu = Gtk.Menu()
        self.addCleanup(menu.destroy)
        self.window.current().populate_edit_menu(self.window.current().editor, menu)
        self.assertIn('Insert template…', [child.get_label() for child in menu.get_children() if hasattr(child, 'get_label')])

    def test_unique_note_dialog_and_direct_creation(self):
        def answer():
            dialog = self.dialog('New unique note')
            dialog.title_entry.set_text('Plan')
            dialog.template_choice.set_active_id('Templates/Day.md')
            dialog.response(Gtk.ResponseType.OK)
            return False

        with patch('librevault.templates.current_time', return_value=WHEN):
            GLib.idle_add(answer)
            self.window.new_unique_note()
        pane = self.window.current()
        self.assertEqual(pane.note.path, '202611020800 - Plan.md')
        self.assertEqual(pane.text(), '# 202611020800 - Plan\n2026-11-02 08:00\nMonday 00\n')
        with patch('librevault.templates.current_time', return_value=WHEN):
            self.window.new_unique_note(title='', template='')
        self.assertEqual(self.window.current().note.path, '202611020800.md')
        self.assertEqual(self.window.current().text(), '')

    def test_create_from_template_dialog_and_direct_path(self):
        with patch('librevault.templates.current_time', return_value=WHEN):
            self.window.create_from_template(template='Templates/Day.md', name='Projects/Idea')
        self.assertEqual(self.window.current().note.path, 'Projects/Idea.md')
        self.assertEqual(self.window.current().text(), '# Idea\n2026-11-02 08:00\nMonday 00\n')
        self.assertTrue((self.root / 'Projects' / 'Idea.md').is_file())

        def choose_template():
            picker = self.dialog('Choose a template')
            picker.search.set_text('Day')
            picker.response(Gtk.ResponseType.OK)
            GLib.idle_add(choose_name)
            return False

        def choose_name():
            matches = [window for window in Gtk.Window.list_toplevels()
                       if isinstance(window, Gtk.Dialog) and window.get_title() == 'New note from template'
                       and window.get_visible()]
            if not matches:
                return True
            matches[0].entry.set_text('Dialog note')
            matches[0].response(Gtk.ResponseType.OK)
            return False

        with patch('librevault.templates.current_time', return_value=WHEN):
            GLib.idle_add(choose_template)
            self.window.create_from_template()
        self.assertEqual(self.window.current().note.path, 'Dialog note.md')
        self.assertTrue((self.root / 'Dialog note.md').is_file())

    def test_palette_lists_commands_and_opens_a_random_note(self):
        (self.root / 'Beta.md').write_text('beta\n', encoding='utf-8')
        self.window.refresh()
        self.assertTrue(self.window.key_press(self.window, self.event(Gdk.KEY_p, Gdk.ModifierType.MOD1_MASK)))
        loop = GLib.MainLoop()
        GLib.timeout_add(30, lambda: loop.quit() or False)
        loop.run()
        self.drain()
        palette = self.window.palette
        titles = [row.title for row in palette.results.get_children()]
        for title in ('Open daily note', 'Insert template', 'New unique note', 'Open random note'):
            self.assertIn(title, titles)
        palette.search.set_text('random note')
        self.assertEqual(palette.results.get_selected_row().title, 'Open random note')
        with patch('librevault.templates.random.choice', return_value='Beta.md'):
            palette.search.emit('activate')
        self.drain()
        self.assertEqual(self.window.current().note.path, 'Beta.md')
        self.assertIn('Random note', self.window.status.get_text())


if __name__ == '__main__':
    unittest.main()
