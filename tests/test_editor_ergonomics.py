"""Folding, slash commands, multiple cursors, and optional Vim mode."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.folding import FoldState, compute_regions, remap_anchors
from librevault.multicursor import (
    ExtraCursors, find_occurrences, next_occurrence, plan_backspace, plan_delete,
    plan_format, plan_insert, word_bounds,
)
from librevault.preferences import Preferences, PreferencesStore
from librevault.shortcuts import EDITOR_SHORTCUTS
from librevault.slash import builtin_commands, filter_commands, trigger_at
from librevault.vim import Session, dispatch, display_selection, mode_label, retreat

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class FoldingTests(unittest.TestCase):
    def test_headings_stop_at_the_next_same_or_higher_level(self):
        text = '# A\nbody\n## B\nstill a\n# C\nend\n'
        regions = [region for region in compute_regions(text) if region.kind == 'heading']
        first = next(region for region in regions if region.level == 1)
        self.assertEqual(text[first.body_start:first.body_end], 'body\n## B\nstill a\n')
        self.assertEqual([region.anchor for region in regions if region.level == 1], [0, text.index('# C')])
        nested = next(region for region in regions if region.level == 2)
        self.assertEqual(text[nested.body_start:nested.body_end], 'still a\n')
        self.assertEqual(nested.start_line, 2)
        self.assertEqual(nested.body_start, text.index('still'))

    def test_setext_heading_body_starts_after_the_underline(self):
        text = 'Title\n=====\nbody\n'
        region = compute_regions(text)[0]
        self.assertEqual(region.kind, 'heading')
        self.assertEqual(region.level, 1)
        self.assertTrue(text[region.body_start:].startswith('body'))
        self.assertNotIn('=====', text[region.body_start:region.body_end])

    def test_lists_fold_only_when_they_have_nested_children(self):
        nested = '- parent\n  - child\n  - child2\n- sibling\n'
        regions = compute_regions(nested)
        self.assertEqual(len(regions), 1)
        region = regions[0]
        self.assertEqual(region.kind, 'list')
        self.assertEqual(text_body(nested, region), '  - child\n  - child2\n')
        self.assertNotIn('parent', text_body(nested, region))
        self.assertEqual(compute_regions('- only\n- two\n'), [])
        tasks = '- parent\n  - [ ] child\n'
        task_region = compute_regions(tasks)[0]
        self.assertIn('child', text_body(tasks, task_region))
        self.assertNotIn('parent', text_body(tasks, task_region))

    def test_fenced_code_and_frontmatter_fold_their_bodies_only(self):
        code = '```py\nline\nline2\n```\n\nafter\n'
        region = compute_regions(code)[0]
        self.assertEqual(region.kind, 'code')
        self.assertEqual(text_body(code, region), 'line\nline2\n')
        self.assertTrue(code[region.anchor:].startswith('```py'))
        tilde = '~~~\nbody\n~~~\n'
        self.assertEqual(text_body(tilde, compute_regions(tilde)[0]), 'body\n')
        indented = '    code\n    more\n'
        self.assertEqual(compute_regions(indented), [])
        front = '---\ntitle: x\ntags: a\n---\n# H\nbody\n'
        matter = next(region for region in compute_regions(front) if region.kind == 'frontmatter')
        self.assertEqual(text_body(front, matter), 'title: x\ntags: a\n')
        self.assertEqual(matter.anchor, 0)
        self.assertEqual(matter.start_line, 0)
        self.assertEqual(compute_regions('---\n---\n# H\n'), [])

    def test_anchors_follow_edits_and_drop_when_the_header_character_is_deleted(self):
        self.assertEqual(remap_anchors({10}, 0, 0, 5), {15})
        self.assertEqual(remap_anchors({10}, 10, 0, 3), {13})
        self.assertEqual(remap_anchors({10}, 12, 0, 4), {10})
        self.assertEqual(remap_anchors({10}, 0, 2, 0), {8})
        self.assertEqual(remap_anchors({10}, 8, 2, 0), {8})
        self.assertEqual(remap_anchors({10}, 8, 5, 1), set())
        text = '# A\nbody\n'
        state = FoldState()
        state.fold_all_headings(compute_regions(text))
        self.assertEqual(state.anchors, {0})
        state.apply_edit(0, 4, 0)
        state.prune(compute_regions('body\n'))
        self.assertEqual(state.anchors, set())

    def test_fold_toggle_and_concealment_keep_the_source_offsets(self):
        text = '# A\n## B\ndeep\n# C\nend\n'
        regions = compute_regions(text)
        state = FoldState()
        trailed = '# A\nbody\n'
        self.assertIsNone(FoldState().region_at(compute_regions(trailed), trailed, len(trailed)))
        outer = state.region_at(regions, text, 0)
        self.assertEqual(outer.level, 1)
        deep = text.index('deep')
        self.assertEqual(state.region_at(regions, text, deep).anchor, text.index('## B'))
        state.fold_all_headings(regions)
        self.assertTrue(all(region.anchor in state.anchors for region in regions if region.kind == 'heading'))
        concealed = state.concealed(regions)
        self.assertIn((regions[0].body_start, regions[0].body_end),
                      [(start, end) for start, end in concealed if start == regions[0].body_start])
        revealed = state.concealed(regions, reveal=[deep])
        self.assertTrue(all(not (start <= deep < end) for start, end in revealed))
        self.assertIsNotNone(state.toggle_at(regions, text, 0))
        self.assertNotIn(0, state.anchors)
        self.assertIsNotNone(state.fold_at(regions, text, 0))
        self.assertIsNotNone(state.unfold_at(regions, text, 0))
        self.assertNotIn(0, state.anchors)
        state.unfold_all()
        self.assertEqual(state.anchors, set())
        self.assertEqual(state.concealed(regions), [])


class SlashCommandTests(unittest.TestCase):
    def test_trigger_requires_a_slash_at_the_line_start_or_after_whitespace(self):
        self.assertEqual(trigger_at('/he', 3), (0, 'he'))
        self.assertEqual(trigger_at('hello /li', len('hello /li')), (6, 'li'))
        self.assertEqual(trigger_at('a\n/code', len('a\n/code')), (2, 'code'))
        self.assertIsNone(trigger_at('word/li', len('word/li')))
        self.assertIsNone(trigger_at('http://', len('http://')))
        self.assertIsNone(trigger_at('no slash', 3))

    def test_builtin_snippets_filter_and_place_the_cursor(self):
        when = datetime(2026, 11, 2, 7, 5)
        commands = builtin_commands(when)
        by_title = {command.title: command for command in commands}
        self.assertEqual(by_title['Heading 1'].snippet, '# ')
        self.assertEqual(by_title['Heading 1'].cursor, 2)
        self.assertEqual(by_title['Heading 4'].cursor, 5)
        self.assertEqual(by_title['Bullet list'].snippet, '- ')
        self.assertEqual(by_title['Numbered list'].snippet, '1. ')
        self.assertEqual(by_title['Numbered list'].cursor, 3)
        self.assertEqual(by_title['Task item'].snippet, '- [ ] ')
        self.assertEqual(by_title['Task item'].cursor, 6)
        self.assertEqual(by_title['Code block'].snippet, '```\n\n```\n')
        self.assertEqual(by_title['Code block'].cursor, 4)
        self.assertEqual(by_title['Math block'].snippet, '$$\n\n$$\n')
        self.assertEqual(by_title['Math block'].cursor, 3)
        self.assertEqual(by_title['Callout'].snippet, '> [!NOTE]\n> ')
        self.assertEqual(by_title['Callout tip'].snippet, '> [!TIP]\n> ')
        self.assertEqual(by_title['Callout warning'].snippet, '> [!WARNING]\n> ')
        self.assertTrue(by_title['Table'].snippet.startswith('| Column | Column |'))
        self.assertEqual(by_title['Horizontal rule'].snippet, '---\n')
        self.assertEqual(by_title['Link'].snippet, '[]()')
        self.assertEqual(by_title['Link'].cursor, 1)
        self.assertEqual(by_title['Embed'].snippet, '![[]]')
        self.assertEqual(by_title['Embed'].cursor, 3)
        self.assertEqual(by_title['Current date'].snippet, '2026-11-02')
        self.assertEqual(by_title['Current time'].snippet, '07:05')
        self.assertEqual(by_title['Template'].kind, 'template')
        self.assertEqual([command.title for command in filter_commands(commands, '')],
                         [command.title for command in commands])
        self.assertEqual([command.title for command in filter_commands(commands, 'heading 2')], ['Heading 2'])
        self.assertEqual([command.title for command in filter_commands(commands, 'TIP')], ['Callout tip'])
        self.assertEqual(filter_commands(commands, 'zzzz'), [])

    def test_new_preference_flags_default_and_reject_non_booleans(self):
        self.assertTrue(Preferences().slash_commands)
        self.assertFalse(Preferences().vim_mode)
        self.assertEqual(Preferences.from_dict({'slash_commands': 'yes', 'vim_mode': 1}), Preferences())
        self.assertEqual(Preferences.from_dict({'slash_commands': False, 'vim_mode': True}),
                         replace(Preferences(), slash_commands=False, vim_mode=True))
        self.assertEqual(Preferences.from_dict({'theme': 'light'}), replace(Preferences(), theme='light'))


class MultiCursorTests(unittest.TestCase):
    def test_word_bounds_and_occurrence_search_are_exact_and_wrap_once(self):
        self.assertEqual(word_bounds('hello world', 0), (0, 5))
        self.assertEqual(word_bounds('hello world', 5), (0, 5))
        self.assertEqual(word_bounds('hello world', 6), (6, 11))
        self.assertEqual(word_bounds('hello_1', 3), (0, 7))
        self.assertIsNone(word_bounds('...', 1))
        spans = find_occurrences('cat cat cat', 'cat')
        self.assertEqual(spans, [(0, 3), (4, 7), (8, 11)])
        self.assertEqual(find_occurrences('Cat cat', 'cat'), [(4, 7)])
        self.assertEqual(next_occurrence(spans, [(0, 3)]), (4, 7))
        self.assertEqual(next_occurrence(spans, [(0, 3), (4, 7)]), (8, 11))
        self.assertEqual(next_occurrence(spans, [(8, 11)]), (0, 3))
        self.assertIsNone(next_occurrence(spans, spans))

    def test_insert_backspace_delete_and_format_plan_one_pass_from_the_end(self):
        edits, carets = plan_insert([(0, 3), (4, 7)], 'X')
        self.assertEqual([(edit.start, edit.end, edit.replacement) for edit in edits],
                         [(4, 7, 'X'), (0, 3, 'X')])
        self.assertEqual(apply_edits('aaa bbb', edits), 'X X')
        self.assertEqual(carets, [1, 3])
        edits, carets = plan_backspace('abcdef', [(2, 4), (5, 5)])
        self.assertEqual(apply_edits('abcdef', edits), 'abf')
        self.assertEqual(carets, [2, 2])
        edits, carets = plan_delete('abcdef', [(1, 1), (4, 6)])
        self.assertEqual(apply_edits('abcdef', edits), 'acd')
        self.assertEqual(carets, [1, 3])
        edits, selections = plan_format('aa bb', [(0, 2), (3, 5)], '**')
        self.assertEqual(apply_edits('aa bb', edits), '**aa** **bb**')
        self.assertEqual(selections, [(2, 4), (9, 11)])
        edits, selections = plan_format('**aa** bb', [(2, 4), (7, 9)], '*')
        rewritten = apply_edits('**aa** bb', edits)
        self.assertIn('aa', rewritten)
        self.assertIn('*bb*', rewritten)

    def test_extra_cursors_combine_with_the_primary_selection(self):
        extra = ExtraCursors()
        self.assertFalse(extra)
        extra.ranges.append((4, 7))
        self.assertEqual(extra.combined((0, 3)), [(0, 3), (4, 7)])
        self.assertEqual(extra.combined((4, 7)), [(4, 7)])
        extra.clear()
        self.assertEqual(extra.ranges, [])


class VimTests(unittest.TestCase):
    def test_motions_stay_on_their_lines_and_remember_a_column(self):
        self.assertEqual(play('ab\ncd', 0, ['l']).cursor, 1)
        self.assertEqual(play('ab\ncd', 1, ['l']).cursor, 1)
        self.assertEqual(play('ab\ncd', 3, ['h']).cursor, 3)
        self.assertEqual(play('ab\ncd', 1, ['h']).cursor, 0)
        self.assertEqual(play('ab\ncd', 1, ['0']).cursor, 0)
        self.assertEqual(play('ab\n', 0, ['$']).cursor, 1)
        self.assertEqual(play('  ab', 0, ['^']).cursor, 2)
        self.assertEqual(play('hello world', 0, ['w']).cursor, 6)
        self.assertEqual(play('hello world', 0, ['e']).cursor, 4)
        self.assertEqual(play('hello world', 6, ['b']).cursor, 0)
        self.assertEqual(play('short\nlonger line\nx\n', 4, ['j']).cursor, 10)
        landed = play('short\nlonger line\nx\n', 4, ['j', 'j', 'k', 'k'])
        self.assertEqual(landed.cursor, 4)
        self.assertEqual(landed.session.column, 4)
        self.assertEqual(play('ab\ncd', 3, ['g', 'g']).cursor, 0)
        self.assertEqual(play('ab\n  cd\n', 0, ['G']).cursor, 5)
        moved = play('abcdef\n123456\n', 5, ['j', 'h'])
        self.assertIsNone(moved.session.column)

    def test_mode_changes_and_operators_are_single_edits(self):
        self.assertEqual(mode_label(Session(mode='normal')), 'NORMAL')
        self.assertEqual(mode_label(Session(mode='insert')), 'INSERT')
        self.assertEqual(mode_label(Session(mode='visual_line')), 'VISUAL')
        self.assertEqual(retreat('ab', 0), 0)
        self.assertEqual(retreat('ab', 2), 1)
        entered = play('ab', 0, ['i'])
        self.assertEqual(entered.session.mode, 'insert')
        self.assertFalse(dispatch('ab', 1, entered.session, 'h').handled)
        escaped = dispatch('ab', 2, Session(mode='insert', cursor=2, anchor=2), 'Escape')
        self.assertEqual(escaped.session.mode, 'normal')
        self.assertEqual(escaped.cursor, 1)
        self.assertEqual(play('  hi\n', 3, ['I']).cursor, 2)
        self.assertEqual(play('  hi\n', 3, ['A']).cursor, 4)
        self.assertEqual(play('ab', 1, ['a']).cursor, 2)
        opened = play('ab', 0, ['o'])
        self.assertEqual(opened.text, 'ab\n')
        self.assertEqual(opened.session.mode, 'insert')
        self.assertEqual(opened.cursor, 3)
        above = play('ab\ncd', 3, ['O'])
        self.assertEqual(above.text, 'ab\n\ncd')
        self.assertEqual(above.cursor, 3)
        deleted = play('ab\ncd\n', 0, ['d', 'd'])
        self.assertEqual(deleted.text, 'cd\n')
        self.assertEqual(deleted.session.register, 'ab\n')
        self.assertTrue(deleted.session.linewise)
        yanked = play('ab\ncd\n', 0, ['y', 'y', 'p'])
        self.assertEqual(yanked.text, 'ab\nab\ncd\n')
        self.assertEqual(yanked.cursor, 3)
        word = play('hello world', 0, ['d', 'w'])
        self.assertEqual(word.text, 'world')
        changed = play('hello world', 0, ['c', 'w'])
        self.assertEqual(changed.text, ' world')
        self.assertEqual(changed.session.mode, 'insert')
        cut = play('ab', 0, ['x', 'p'])
        self.assertEqual(cut.text, 'ba')
        visual = play('abcd', 0, ['v', 'l', 'd'])
        self.assertEqual(visual.text, 'bcd')
        self.assertEqual(visual.session.mode, 'normal')
        yanked_visual = play('abcd', 0, ['v', 'l', 'y'])
        self.assertEqual(yanked_visual.text, 'abcd')
        self.assertEqual(yanked_visual.session.register, 'a')
        lines = play('ab\ncd\nef\n', 0, ['V', 'j', 'd'])
        self.assertEqual(lines.text, 'ef\n')
        self.assertEqual(lines.session.mode, 'normal')
        visual_move = play('ab\ncd', 3, ['v', 'g', 'g'])
        self.assertEqual(visual_move.session.mode, 'visual')
        self.assertEqual(visual_move.cursor, 0)
        self.assertEqual(visual_move.session.anchor, 3)
        self.assertEqual(display_selection('abcd', visual_move.session), (0, 3))
        swallowed = play('ab', 0, ['q'])
        self.assertTrue(swallowed.outcomes[0].handled)
        self.assertIsNone(swallowed.outcomes[0].edit)
        self.assertEqual(swallowed.text, 'ab')
        undo = dispatch('ab', 0, Session(mode='normal'), 'u')
        redo = dispatch('ab', 0, Session(mode='normal'), 'ctrl-r')
        self.assertTrue(undo.undo)
        self.assertTrue(redo.redo)
        self.assertIsNone(undo.edit)
        empty = play('ab', 0, ['p'])
        self.assertEqual(empty.text, 'ab')

    def test_editor_shortcuts_keep_their_handlers(self):
        handlers = {shortcut.accelerator: shortcut.handler for shortcut in EDITOR_SHORTCUTS}
        self.assertEqual(handlers['<Control><Shift>bracketleft'], 'fold_at_cursor')
        self.assertEqual(handlers['<Control><Shift>bracketright'], 'unfold_at_cursor')
        self.assertEqual(handlers['<Control>d'], 'add_next_occurrence')
        self.assertEqual(handlers['<Control><Shift>l'], 'select_all_occurrences')
        self.assertEqual(handlers['<Control>b'], 'bold')
        self.assertEqual(handlers['<Control>z'], 'undo')


def text_body(text, region):
    return text[region.body_start:region.body_end]


def apply_edits(text, edits):
    for edit in edits:
        text = text[:edit.start] + edit.replacement + text[edit.end:]
    return text


class VimPlay:
    def __init__(self, text, session, cursor, outcomes):
        self.text = text
        self.session = session
        self.cursor = cursor
        self.outcomes = outcomes


def play(text, cursor, keys, mode='normal'):
    session = Session(mode=mode, cursor=cursor, anchor=cursor)
    point = cursor
    outcomes = []
    for key in keys:
        current = point if session.mode == 'insert' else session.cursor
        outcome = dispatch(text, current, session, key)
        outcomes.append(outcome)
        if outcome.edit is not None:
            edit = outcome.edit
            text = text[:edit.start] + edit.replacement + text[edit.end:]
        session = outcome.session
        point = outcome.cursor
    return VimPlay(text, session, point, outcomes)


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class EditorErgonomicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.EditorErgonomicsTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Original\n', encoding='utf-8')
        self.preferences_path = Path(self.directory.name) / 'settings.json'
        self.window = Window(self.app, self.root, preferences_path=self.preferences_path)
        self.addCleanup(self.window.destroy)
        self.pane = self.window.current()
        self.drain()

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def source(self, text):
        self.pane.buffer.set_text(text)
        self.pane.save()
        self.pane.history.reset()
        self.drain()

    def key(self, keyval, modifiers=0):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = keyval
        event.state = modifiers
        value = Gdk.keyval_to_unicode(keyval)
        if value and not (modifiers & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
                                       | Gdk.ModifierType.SUPER_MASK)):
            event.string = chr(value)
        else:
            event.string = ''
        window = self.pane.editor.get_window(Gtk.TextWindowType.WIDGET)
        if window is not None:
            event.window = window
        return self.pane.editor.emit('key-press-event', event)

    def visible(self):
        return self.pane.buffer.get_text(self.pane.buffer.get_start_iter(),
                                         self.pane.buffer.get_end_iter(), False)

    def hidden_at(self, offset):
        return self.pane.hidden in self.pane.buffer.get_iter_at_offset(offset).get_tags()

    def place(self, offset):
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(offset))

    def test_folds_conceal_bodies_and_save_copy_and_search_keep_the_source(self):
        text = '# A\nsecret\n# B\nmore\n'
        self.source(text)
        self.place(0)
        self.assertTrue(any(button.get_label() == '▾' for button in self.pane.fold_widgets))
        self.assertIn('secret', self.visible())
        self.key(Gdk.KEY_braceleft, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.drain()
        self.assertIn(0, self.pane.folds.anchors)
        self.assertNotIn('secret', self.visible())
        self.assertTrue(self.hidden_at(text.index('secret')))
        self.assertEqual(self.pane.text(), text)
        self.assertTrue(any(button.get_label() == '▸' for button in self.pane.fold_widgets))
        self.pane.show_find()
        self.pane.findbar.query.set_text('secret')
        self.pane.findbar.refresh()
        self.assertEqual(self.pane.findbar.matches, [(text.index('secret'), text.index('secret') + len('secret'))])
        self.pane.find_next()
        self.drain()
        self.assertIn('secret', self.visible())
        self.pane.findbar.close()
        self.pane.render()
        self.assertNotIn('secret', self.visible())
        buffer = self.pane.buffer
        buffer.select_range(buffer.get_end_iter(), buffer.get_start_iter())
        with patch.object(Gtk.Clipboard, 'get') as clipboard:
            self.pane.editor.emit('copy-clipboard')
        self.assertEqual(clipboard.return_value.set_text.call_args[0][0], text)
        self.assertEqual(self.pane.text(), text)
        self.place(len(text))
        self.pane.buffer.insert_at_cursor('Z')
        self.pane.save()
        self.assertEqual((self.root / 'Note.md').read_text(encoding='utf-8'), text + 'Z')
        self.assertIn('secret', (self.root / 'Note.md').read_text(encoding='utf-8'))
        self.place(0)
        self.key(Gdk.KEY_braceright, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.drain()
        self.assertNotIn(0, self.pane.folds.anchors)
        self.assertIn('secret', self.visible())

    def test_list_code_and_frontmatter_chevrons_and_nested_heading_folds(self):
        code = '```\nsecret\n```\n'
        self.source(code)
        self.place(0)
        self.assertIn('secret', self.visible())
        self.pane.fold_at_cursor()
        self.assertNotIn('secret', self.visible())
        self.assertIn('secret', self.pane.text())
        self.assertEqual(self.pane.fold_widgets[0].get_label(), '▸')
        nested = '- parent\n  - child\n'
        self.source(nested)
        self.place(0)
        self.pane.fold_at_cursor()
        self.assertIn('parent', self.visible())
        self.assertNotIn('child', self.visible())
        self.assertTrue(any(not widget.get_visible() for _, widget in self.pane.list_widgets))
        front = '---\ntitle: hidden\n---\n# H\n'
        self.source(front)
        self.place(0)
        self.drain()
        self.assertIn('hidden', self.visible())
        self.pane.toggle_fold()
        self.assertNotIn('hidden', self.visible())
        self.assertEqual(self.pane.text(), front)
        headings = '# A\n## B\ntext\n'
        self.source(headings)
        self.place(headings.index('text'))
        self.window.show_command_palette()
        palette = self.window.palette
        palette.search.set_text('Fold all headings')
        palette.search.emit('activate')
        self.drain()
        self.assertNotIn('text', self.visible())
        self.assertEqual(self.pane.text(), headings)
        self.assertLess(self.pane._cursor(), headings.index('text'))
        self.assertEqual(len(self.pane.fold_widgets), 1)
        self.pane.fold_widgets[0].emit('clicked')
        self.drain()
        self.assertNotIn(0, self.pane.folds.anchors)
        self.assertNotIn('text', self.visible())
        self.window.show_command_palette()
        palette = self.window.palette
        palette.search.set_text('Unfold all')
        palette.search.emit('activate')
        self.drain()
        self.assertIn('text', self.visible())
        self.assertGreater(len(self.pane.fold_widgets), 1)
        self.pane.set_mode('reading')
        self.drain()
        self.assertTrue(self.pane.fold_widgets)
        self.assertTrue(all(not button.get_visible() for button in self.pane.fold_widgets))
        self.pane.set_mode('source')
        self.pane.fold_all_headings()
        self.assertNotIn('text', self.visible())
        self.assertTrue(any(button.get_visible() for button in self.pane.fold_widgets))

    def test_edits_before_a_folded_heading_keep_the_body_concealed(self):
        text = '# A\nsecret\n# B\n'
        self.source(text)
        self.place(0)
        self.pane.fold_at_cursor()
        self.place(0)
        self.pane.buffer.insert_at_cursor('Note\n')
        self.drain()
        revised = 'Note\n' + text
        self.assertEqual(self.pane.text(), revised)
        self.assertEqual(self.pane.folds.anchors, {len('Note\n')})
        self.assertNotIn('secret', self.visible())
        self.pane.undo()
        self.drain()
        self.assertEqual(self.pane.text(), text)
        self.assertEqual(self.pane.folds.anchors, {0})
        self.assertNotIn('secret', self.visible())

    def test_slash_menu_inserts_a_snippet_as_one_undo_and_dismisses(self):
        self.source('')
        self.assertTrue(self.key(Gdk.KEY_slash))
        self.drain()
        self.assertTrue(self.pane.slash.open)
        self.assertEqual(self.pane.slash.selected_title(), 'Heading 1')
        self.assertTrue(self.key(Gdk.KEY_Down))
        self.assertEqual(self.pane.slash.selected_title(), 'Heading 2')
        self.assertTrue(self.key(Gdk.KEY_Return))
        self.drain()
        self.assertFalse(self.pane.slash.open)
        self.assertEqual(self.pane.text(), '## ')
        self.assertEqual(self.pane._cursor(), 3)
        self.pane.undo()
        self.assertEqual(self.pane.text(), '/')
        self.source('')
        self.key(Gdk.KEY_slash)
        self.key(Gdk.KEY_h)
        self.key(Gdk.KEY_e)
        self.key(Gdk.KEY_a)
        self.drain()
        self.assertTrue(self.pane.slash.open)
        self.assertTrue(all(command.title.startswith('Heading') for command in self.pane.slash.commands))
        self.key(Gdk.KEY_z)
        self.drain()
        self.assertFalse(self.pane.slash.open)
        self.assertEqual(self.pane.text(), '/heaz')
        self.source('word')
        self.place(len('word'))
        self.key(Gdk.KEY_slash)
        self.drain()
        self.assertFalse(self.pane.slash.open)
        self.assertEqual(self.pane.text(), 'word/')
        self.source('')
        self.key(Gdk.KEY_slash)
        self.assertTrue(self.pane.slash.open)
        self.assertTrue(self.key(Gdk.KEY_Escape))
        self.assertFalse(self.pane.slash.open)
        self.assertEqual(self.pane.text(), '/')
        self.source('/Template')
        self.place(len('/Template'))
        self.drain()
        self.assertEqual(self.pane.slash.selected_title(), 'Template')
        self.key(Gdk.KEY_Return)
        self.drain()
        self.assertEqual(self.pane.text(), '/Template')
        self.assertIn('No templates', self.window.status.get_text())
        self.window.set_preferences(replace(self.window.preferences, slash_commands=False))
        self.source('')
        self.key(Gdk.KEY_slash)
        self.drain()
        self.assertFalse(self.pane.slash.open)

    def test_multi_cursor_edits_are_one_undo_and_escape_clears_the_extras(self):
        text = 'one cat and one cat'
        self.source(text)
        self.place(text.index('cat'))
        control = Gdk.ModifierType.CONTROL_MASK
        self.assertTrue(self.key(Gdk.KEY_d, control))
        bounds = self.pane.buffer.get_selection_bounds()
        self.assertEqual(self.pane.buffer.get_text(*bounds, True), 'cat')
        self.assertFalse(self.pane.multicursor)
        self.key(Gdk.KEY_d, control)
        self.drain()
        self.assertEqual(self.pane.multicursor.ranges, [(text.rindex('cat'), text.rindex('cat') + 3)])
        extra = self.pane.multicursor.ranges[0][0]
        self.assertIn(self.pane.occurrence_tag, self.pane.buffer.get_iter_at_offset(extra).get_tags())
        self.assertTrue(self.key(Gdk.KEY_x))
        self.drain()
        self.assertEqual(self.pane.text(), 'one x and one x')
        self.assertEqual(len(self.pane.history.undo_stack), 1)
        self.pane.undo()
        self.assertEqual(self.pane.text(), text)
        self.place(text.index('cat'))
        self.key(Gdk.KEY_d, control)
        self.key(Gdk.KEY_d, control)
        self.key(Gdk.KEY_Escape)
        self.assertFalse(self.pane.multicursor)
        self.key(Gdk.KEY_q)
        self.drain()
        self.assertEqual(self.pane.text(), 'one q and one cat')
        self.source('cat cat')
        self.place(0)
        self.key(Gdk.KEY_L, control | Gdk.ModifierType.SHIFT_MASK)
        self.drain()
        self.assertEqual(len(self.pane.multicursor.ranges), 1)
        self.key(Gdk.KEY_b, control)
        self.drain()
        self.assertEqual(self.pane.text(), '**cat** **cat**')
        self.assertEqual(len(self.pane.history.undo_stack), 1)
        self.pane.undo()
        self.assertEqual(self.pane.text(), 'cat cat')
        self.pane.redo()
        self.assertEqual(self.pane.text(), '**cat** **cat**')

    def test_vim_mode_shows_its_status_and_keeps_existing_undo(self):
        self.source('alpha\n')
        self.place(len(self.pane.text()))
        self.key(Gdk.KEY_h)
        self.drain()
        self.assertEqual(self.pane.text(), 'alpha\nh')
        self.assertEqual(self.window.vim_status.get_text(), '')
        self.source('alpha\nbeta\n')
        self.place(0)
        self.window.set_preferences(replace(self.window.preferences, vim_mode=True))
        self.drain()
        self.assertEqual(self.window.vim_status.get_text(), 'NORMAL')
        before = self.pane.text()
        self.key(Gdk.KEY_h)
        self.assertEqual(self.pane.text(), before)
        self.key(Gdk.KEY_i)
        self.assertEqual(self.window.vim_status.get_text(), 'INSERT')
        self.key(Gdk.KEY_z)
        self.drain()
        self.assertEqual(self.pane.text(), 'zalpha\nbeta\n')
        self.key(Gdk.KEY_Escape)
        self.assertEqual(self.window.vim_status.get_text(), 'NORMAL')
        self.key(Gdk.KEY_d)
        self.key(Gdk.KEY_d)
        self.assertEqual(self.pane.text(), 'beta\n')
        self.key(Gdk.KEY_u)
        self.assertEqual(self.pane.text(), 'zalpha\nbeta\n')
        self.key(Gdk.KEY_r, Gdk.ModifierType.CONTROL_MASK)
        self.assertEqual(self.pane.text(), 'beta\n')
        self.key(Gdk.KEY_v)
        self.assertEqual(self.window.vim_status.get_text(), 'VISUAL')
        self.key(Gdk.KEY_v)
        self.assertEqual(self.window.vim_status.get_text(), 'NORMAL')
        self.place(0)
        self.key(Gdk.KEY_slash)
        self.drain()
        self.assertEqual(self.window.vim_status.get_text(), 'INSERT')
        self.assertTrue(self.pane.text().startswith('/'))
        self.assertTrue(self.pane.slash.open)
        self.window.set_preferences(replace(self.window.preferences, vim_mode=False))
        self.drain()
        self.assertEqual(self.window.vim_status.get_text(), '')

    def test_settings_persist_slash_commands_and_vim_mode(self):
        self.window.show_settings()
        dialog = self.window.settings_dialog
        dialog.categories.select_row(dialog.categories.get_row_at_index(1))
        self.assertEqual(dialog.stack.get_visible_child_name(), 'Editor')
        dialog.controls['slash_commands'].set_active(False)
        dialog.controls['vim_mode'].set_active(True)
        self.drain()
        stored = PreferencesStore(self.preferences_path).load()
        self.assertFalse(stored.slash_commands)
        self.assertTrue(stored.vim_mode)
        self.assertFalse(self.window.preferences.slash_commands)
        self.assertTrue(self.window.preferences.vim_mode)
        self.assertEqual(self.window.vim_status.get_text(), 'NORMAL')
        dialog.controls['vim_mode'].set_active(False)
        self.assertEqual(self.window.vim_status.get_text(), '')
        self.assertFalse(PreferencesStore(self.preferences_path).load().vim_mode)


if __name__ == '__main__':
    unittest.main()
