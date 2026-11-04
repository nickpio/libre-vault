"""Unlinked mentions, saved-version history, and live note counts."""
from tests.gui_wait import wait_for_work
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.durable import AtomicReplacementError, DirectorySyncError
from librevault.mentions import find_mentions, link_mention
from librevault.recovery import VersionHistoryStore
from librevault.vault import Vault, VaultError

try:
    from librevault.app import Gtk, Window, count_summary
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


WELCOME = """---
aliases:
  - Hello World
---

# Welcome
"""

OTHER = """---
title: Welcome
---

See Welcome soon.
Hello World appears here.
[[Welcome]] is already linked.
[Welcome](Welcome.md) is a markdown link.

```
Welcome
```
NotWelcome stays plain.
"""


class MentionTests(unittest.TestCase):
    def test_plain_names_and_aliases_ignore_links_frontmatter_and_fences(self):
        mentions = find_mentions('Other.md', OTHER, ['Welcome', 'Hello World'])
        self.assertEqual([(item.line_number, item.matched_term) for item in mentions],
                         [(5, 'Welcome'), (6, 'Hello World')])
        welcome = mentions[0]
        self.assertEqual(OTHER[welcome.match_start:welcome.match_end], 'Welcome')
        self.assertIn('See Welcome soon.', welcome.snippet)
        self.assertNotIn('NotWelcome', [item.matched_term for item in mentions])

    def test_case_alias_images_embeds_and_closed_fences(self):
        text = '![Welcome](pic.png) and ![[Welcome]] and plain welcome.'
        mentions = find_mentions('Other.md', text, ['Welcome'])
        self.assertEqual([item.matched_term for item in mentions], ['welcome'])
        self.assertGreater(mentions[0].match_start, text.index('plain'))
        fenced = 'Before\n```\nWelcome\n```\nAfter Welcome\n'
        found = find_mentions('Other.md', fenced, ['Welcome'])
        self.assertEqual([item.line_number for item in found], [5])
        self.assertEqual(find_mentions('Other.md', 'Before\n```\nWelcome\n', ['Welcome']), [])
        phrase = 'See Note (draft) today.'
        special = find_mentions('Other.md', phrase, ['Note (draft)'])
        self.assertEqual(special[0].matched_term, 'Note (draft)')
        self.assertEqual(find_mentions('Other.md', 'Noted (draft)', ['Note (draft)']), [])

    def test_longer_term_wins_and_crlf_offsets_stay_in_the_searched_text(self):
        text = 'Note book\nNote\n'
        mentions = find_mentions('Other.md', text, ['Note', 'Note book'])
        self.assertEqual([item.matched_term for item in mentions], ['Note book', 'Note'])
        crlf = 'See Welcome\r\nnext\r\n'
        found = find_mentions('Other.md', crlf, ['Welcome'])
        self.assertEqual(found[0].line_number, 1)
        self.assertEqual(crlf[found[0].match_start:found[0].match_end], 'Welcome')

    def test_link_mention_uses_a_plain_wikilink_or_an_alias(self):
        self.assertEqual(link_mention('See Welcome soon.', 4, 11, 'Welcome'),
                         'See [[Welcome]] soon.')
        self.assertEqual(link_mention('see welcome soon.', 4, 11, 'Welcome'),
                         'see [[Welcome|welcome]] soon.')
        self.assertEqual(link_mention('Say Hello World.', 4, 15, 'Welcome'),
                         'Say [[Welcome|Hello World]].')
        with self.assertRaises(ValueError):
            link_mention('abc', 0, 5, 'Welcome')

    def test_vault_searches_saved_notes_and_open_text_overrides(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Welcome.md').write_text(WELCOME, encoding='utf-8')
            (root / 'Other.md').write_text(OTHER, encoding='utf-8')
            (root / 'Projects').mkdir()
            (root / 'Projects' / 'Field notes.md').write_text('Nothing here.\n', encoding='utf-8')
            vault = Vault(root)
            self.addCleanup(vault.close)
            mentions = vault.unlinked_mentions('Welcome.md')
            self.assertEqual([(item.source_path, item.matched_term) for item in mentions],
                             [('Other.md', 'Welcome'), ('Other.md', 'Hello World')])
            self.assertEqual(vault.unlinked_mentions('Other.md'), [])
            overridden = vault.unlinked_mentions('Welcome.md', {'Other.md': 'No names here.\n'})
            self.assertEqual(overridden, [])
            self.assertEqual(vault.unlinked_mentions('Missing.md'), [])


class VersionHistoryTests(unittest.TestCase):
    def test_changed_saves_are_kept_deduped_and_bounded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'vault'
            root.mkdir()
            (root / 'Note.md').write_text('one\n', encoding='utf-8')
            state = Path(directory) / 'history'
            vault = Vault(root)
            self.addCleanup(vault.close)
            store = VersionHistoryStore(root, state)
            vault.version_history = store
            note = vault.read('Note.md')
            saved = vault.save(note, 'two\n')
            versions = store.versions('Note.md')
            self.assertEqual([item.text for item in versions], ['one\n', 'two\n'])
            self.assertEqual(versions[-1].revision, saved.revision)
            self.assertEqual(versions[-1].path, 'Note.md')
            self.assertNotEqual(versions[0].timestamp, '')
            again = vault.save(saved, 'two\n')
            self.assertEqual([item.text for item in store.versions('Note.md')], ['one\n', 'two\n'])
            vault.save(again, 'three\n')
            self.assertEqual([item.text for item in store.versions('Note.md')], ['one\n', 'two\n', 'three\n'])
            self.assertEqual(list(store.directory.glob('.history-*')), [])

    def test_directory_is_private_under_xdg_state_and_corrupt_files_reset(self):
        previous = os.umask(0)
        try:
            with TemporaryDirectory() as directory:
                root = Path(directory) / 'vault'
                root.mkdir()
                state = Path(directory) / 'state'
                with patch.dict(os.environ, {'XDG_STATE_HOME': str(state)}):
                    store = VersionHistoryStore(root)
                self.assertEqual(store.directory.parent, state / 'libre-vault' / 'history')
                store.record('Note.md', 'r1', 'v1', previous=('r0', 'v0'),
                             timestamp='2026-11-02T00:00:01+00:00', previous_mtime=None)
                self.assertEqual(stat.S_IMODE(store.directory.stat().st_mode), 0o700)
                saved = next(store.directory.glob('*.json'))
                self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
                self.assertEqual([item.text for item in store.versions('Note.md')], ['v0', 'v1'])
                store.record('Note.md', 'r1', 'v1', previous=('r1', 'v1'), timestamp='2026-11-02T00:00:02+00:00')
                self.assertEqual([item.text for item in store.versions('Note.md')], ['v0', 'v1'])
                saved.write_text('{', encoding='utf-8')
                self.assertEqual(store.versions('Note.md'), [])
                store.record('Note.md', 'r2', 'fresh', timestamp='2026-11-02T00:00:03+00:00')
                self.assertEqual(store.versions('Note.md')[0].text, 'fresh')
        finally:
            os.umask(previous)

    def test_history_keeps_the_newest_fifty_versions(self):
        with TemporaryDirectory() as directory:
            store = VersionHistoryStore(Path(directory) / 'vault', Path(directory) / 'history')
            previous = ('r0', 'v0')
            for index in range(1, 61):
                store.record('Note.md', f'r{index}', f'v{index}', previous=previous,
                             timestamp=f'2026-11-02T00:00:{index:02d}+00:00')
                previous = (f'r{index}', f'v{index}')
            versions = store.versions('Note.md')
            self.assertEqual(len(versions), VersionHistoryStore.MAX_VERSIONS)
            self.assertEqual(versions[0].text, 'v11')
            self.assertEqual(versions[-1].text, 'v60')

    def test_history_failure_leaves_the_note_unchanged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'vault'
            root.mkdir()
            note_path = root / 'Note.md'
            note_path.write_text('one\n', encoding='utf-8')
            vault = Vault(root)
            self.addCleanup(vault.close)
            blocker = Path(directory) / 'blocked'
            blocker.write_text('not a directory', encoding='utf-8')
            vault.version_history = VersionHistoryStore(root, blocker)
            note = vault.read('Note.md')
            with self.assertRaises(VaultError):
                vault.save(note, 'two\n')
            self.assertEqual(note_path.read_text(encoding='utf-8'), 'one\n')

    def test_history_write_stages_leave_the_note_unchanged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'vault'
            root.mkdir()
            note_path = root / 'Note.md'
            note_path.write_text('one\n', encoding='utf-8')
            vault = Vault(root)
            self.addCleanup(vault.close)
            store = VersionHistoryStore(root, Path(directory) / 'history')
            vault.version_history = store
            note = vault.read('Note.md')
            vault.save(note, 'two\n')
            saved = vault.read('Note.md')
            stages = (
                ('write failed', OSError,
                 patch('librevault.recovery.json.dump', side_effect=OSError('write failed'))),
                ('disk failure', AtomicReplacementError,
                 patch('librevault.durable.replace_file', side_effect=OSError('disk failure'))),
            )
            for label, error_type, stage in stages:
                with self.subTest(stage=label):
                    with stage:
                        with self.assertRaises(VaultError) as caught:
                            vault.save(saved, 'three\n')
                    self.assertIsInstance(caught.exception.__cause__, error_type)
                    if error_type is OSError:
                        self.assertNotIsInstance(
                            caught.exception.__cause__, (AtomicReplacementError, DirectorySyncError))
                    self.assertEqual(note_path.read_text(encoding='utf-8'), 'two\n')
                    self.assertEqual([item.text for item in store.versions('Note.md')], ['one\n', 'two\n'])
                    self.assertEqual(list(store.directory.glob('.history-*')), [])
                    self.assertEqual(list(root.glob('.librevault-*')), [])

    def test_history_record_distinguishes_each_write_stage(self):
        with TemporaryDirectory() as directory:
            store = VersionHistoryStore(Path(directory) / 'vault', Path(directory) / 'history')
            store.record('Note.md', 'r1', 'one', timestamp='2026-11-04T00:00:01+00:00')
            stages = (
                ('mode', OSError, patch('librevault.recovery.os.fchmod', side_effect=OSError('mode'))),
                ('file sync failed', OSError,
                 patch('librevault.durable.sync_file', side_effect=OSError('file sync failed'))),
                ('disk failure', AtomicReplacementError,
                 patch('librevault.durable.replace_file', side_effect=OSError('disk failure'))),
            )
            for label, error_type, stage in stages:
                with self.subTest(stage=label):
                    with stage:
                        with self.assertRaises(error_type) as caught:
                            store.record('Note.md', 'r2', 'two', previous=('r1', 'one'),
                                         timestamp='2026-11-04T00:00:02+00:00')
                    if error_type is OSError:
                        self.assertNotIsInstance(
                            caught.exception, (AtomicReplacementError, DirectorySyncError))
                    self.assertEqual([item.text for item in store.versions('Note.md')], ['one'])
                    self.assertEqual(list(store.directory.glob('.history-*')), [])
            with patch('librevault.durable.sync_directory', side_effect=OSError('power loss')):
                with self.assertRaises(DirectorySyncError):
                    store.record('Note.md', 'r2', 'two', previous=('r1', 'one'),
                                 timestamp='2026-11-04T00:00:02+00:00')
            self.assertEqual([item.text for item in store.versions('Note.md')], ['one', 'two'])
            self.assertEqual(list(store.directory.glob('.history-*')), [])

    def test_history_directory_sync_failure_publishes_history_and_leaves_the_note(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'vault'
            root.mkdir()
            note_path = root / 'Note.md'
            note_path.write_text('one\n', encoding='utf-8')
            vault = Vault(root)
            self.addCleanup(vault.close)
            store = VersionHistoryStore(root, Path(directory) / 'history')
            vault.version_history = store
            note = vault.read('Note.md')
            with patch('librevault.durable.sync_directory', side_effect=OSError('power loss')):
                with self.assertRaises(VaultError) as caught:
                    vault.save(note, 'two\n')
            self.assertIsInstance(caught.exception.__cause__, DirectorySyncError)
            self.assertEqual(note_path.read_text(encoding='utf-8'), 'one\n')
            self.assertEqual([item.text for item in store.versions('Note.md')], ['one\n', 'two\n'])
            self.assertEqual(list(store.directory.glob('.history-*')), [])
            self.assertEqual(list(root.glob('.librevault-*')), [])


def mention_buttons(window):
    buttons = []

    def walk(widget):
        if isinstance(widget, Gtk.Button) and getattr(widget, 'mention_action', None):
            buttons.append(widget)
        if hasattr(widget, 'get_children'):
            for child in widget.get_children():
                walk(child)

    walk(window.context)
    return buttons


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class MentionsHistoryWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.MentionsHistoryTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'Welcome.md').write_text(WELCOME, encoding='utf-8')
        (self.root / 'Other.md').write_text(OTHER, encoding='utf-8')
        self.window = Window(self.app, str(self.root))
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.drain_events()

    def drain_events(self):
        wait_for_work(self.window)
        from librevault.app import GLib
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not become idle')

    def section_labels(self):
        return [child.get_text() for child in self.window.context.get_children()
                if isinstance(child, Gtk.Label)]

    def test_unlinked_mentions_can_be_opened_or_linked_from_the_details_panel(self):
        self.window.details_buttons['links'].set_active(True)
        wait_for_work(self.window)
        labels = self.section_labels()
        self.assertLess(labels.index('Backlinks · Saved files'), labels.index('Unlinked mentions'))
        buttons = mention_buttons(self.window)
        self.assertEqual(sorted((button.mention.matched_term, button.mention_action) for button in buttons),
                         [('Hello World', 'link'), ('Hello World', 'open'),
                          ('Welcome', 'link'), ('Welcome', 'open')])
        open_hello = next(button for button in buttons
                          if button.mention_action == 'open' and button.mention.matched_term == 'Hello World')
        open_hello.clicked()
        pane = self.window.current()
        self.assertEqual(pane.note.path, 'Other.md')
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        self.assertEqual(pane.text()[cursor:cursor + len('Hello World')], 'Hello World')
        self.window.open_note('Welcome.md')
        wait_for_work(self.window)
        self.window.open_note('Other.md')
        self.window.open_note('Welcome.md')
        wait_for_work(self.window)
        link = next(button for button in mention_buttons(self.window)
                    if button.mention_action == 'link' and button.mention.matched_term == 'Welcome')
        link.clicked()
        other = self.window.panes['Other.md']
        self.assertIn('See [[Welcome]] soon.', other.text())
        disk = (self.root / 'Other.md').read_text(encoding='utf-8')
        self.assertIn('See Welcome soon.', disk)
        self.assertNotIn('See [[Welcome]] soon.', disk)
        self.assertTrue(other.history.undo())
        self.assertIn('See Welcome soon.', other.text())
        other.dirty = False
        self.window.open_note('Welcome.md')
        wait_for_work(self.window)
        self.window.close_note(other)
        wait_for_work(self.window)
        link = next(button for button in mention_buttons(self.window)
                    if button.mention_action == 'link' and button.mention.matched_term == 'Welcome')
        link.clicked()
        self.drain_events()
        self.assertIn('See [[Welcome]] soon.', (self.root / 'Other.md').read_text(encoding='utf-8'))
        self.assertIn('Other', [child.get_label() for child in self.window.context.get_children()
                                if isinstance(child, Gtk.Button)])
        remaining = [button.mention.matched_term for button in mention_buttons(self.window)]
        self.assertIn('Hello World', remaining)
        self.assertNotIn('Welcome', remaining)
        self.window.open_note('Other.md')
        wait_for_work(self.window)
        self.assertIn('No unlinked mentions.', self.section_labels())

    def test_version_history_command_restores_one_undoable_edit(self):
        pane = self.window.current()
        self.assertIn('Version history…', [child.get_label() for child in self.window.context.get_children()
                                           if isinstance(child, Gtk.Button)])
        self.window.show_command_palette()
        titles = [row.title for row in self.window.palette.results.get_children()]
        self.assertIn('Version history…', titles)
        self.window.palette.destroy()
        dialog = self.window.show_version_history()
        self.assertTrue(dialog.get_modal())
        self.assertEqual(dialog.version_list.get_children(), [])
        self.assertFalse(dialog.restore_button.get_sensitive())
        preview = dialog.preview.get_buffer().get_text(*dialog.preview.get_buffer().get_bounds(), True)
        self.assertIn('No saved versions', preview)
        dialog.destroy()
        pane.buffer.set_text('first version\n')
        pane.save()
        pane.buffer.set_text('second version\n')
        pane.save()
        dialog = self.window.show_version_history()
        rows = dialog.version_list.get_children()
        self.assertEqual([row.snapshot.text for row in rows],
                         ['second version\n', 'first version\n', WELCOME])
        original = next(row for row in rows if row.snapshot.text == 'first version\n')
        dialog.version_list.select_row(original)
        shown = dialog.preview.get_buffer().get_text(*dialog.preview.get_buffer().get_bounds(), True)
        self.assertIn('first version', shown)
        dialog.diff_toggle.set_active(True)
        diff = dialog.preview.get_buffer().get_text(*dialog.preview.get_buffer().get_bounds(), True)
        self.assertIn('Saved version', diff)
        self.assertIn('+first version', diff)
        self.assertIn('-second version', diff)
        before = pane.text()
        depth = len(pane.history.undo_stack)
        dialog.restore()
        self.assertEqual(pane.text(), 'first version\n')
        self.assertEqual(len(pane.history.undo_stack), depth + 1)
        self.assertTrue(pane.history.undo())
        self.assertEqual(pane.text(), before)
        dialog.response(Gtk.ResponseType.CLOSE)
        self.assertIsNone(self.window.version_dialog)

    def test_word_and_character_counts_follow_the_active_note(self):
        pane = self.window.current()
        pane.buffer.set_text('alpha beta')
        self.assertEqual(pane.buffer.get_line_count(), 1)
        self.assertEqual(self.window.counts.get_text(), count_summary('alpha beta', 1))
        self.assertEqual(self.window.counts.get_text(), '2 words · 10 characters · 1 line')
        pane.buffer.insert_at_cursor(' gamma')
        self.assertEqual(self.window.counts.get_text(), '3 words · 16 characters · 1 line')
        pane.buffer.set_text('alpha beta\nthird')
        self.assertEqual(self.window.counts.get_text(),
                         count_summary('alpha beta\nthird', pane.buffer.get_line_count()))
        self.assertIn('3 words', self.window.counts.get_text())
        self.assertIn('2 lines', self.window.counts.get_text())
        pane.dirty = False
        self.window.close_tab()
        self.assertEqual(self.window.counts.get_text(), '')


if __name__ == '__main__':
    unittest.main()
