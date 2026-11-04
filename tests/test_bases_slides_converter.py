"""Property tables, slide splitting, and Markdown format conversion."""
from tests.gui_wait import wait_for_work
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from librevault.bases import BaseRow, build_table, iter_note_text, set_property, sort_rows
from librevault.converter import conversion_edit, convert_text, selected_note_paths
from librevault.editing import TextEdit
from librevault.metadata import property_mapping, property_text
from librevault.slides import split_slides
from librevault.vault import Vault

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from librevault.bases_ui import BasesView
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


def properties(text):
    return property_mapping(property_text(text))


class BasesLogicTests(unittest.TestCase):
    def test_queries_match_vault_search_and_open_buffers_win(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                'Welcome.md': '# Welcome\n',
                'A.md': '# A\n',
                'b.md': '---\ntags: [personal]\nstatus: b\n---\n# b\n',
                'Work/Plan.md': '---\ntags: work\naliases: [Blueprint]\nstatus: active\ncount: 2\n---\n# Plan\n',
                'Work/Later.md': '---\ntags: work\nstatus: later\ncount: 10\n---\n# Later\n',
                'Work/Nested.md': '---\ntags: work/project\nmood: calm\n---\n# Nested\n',
                'Personal.md': '---\ntags: personal\nstatus: active\n---\n# Personal\n',
                'Homework.md': '---\ntags: homework\nstatus: active\n---\n# Homework\n',
                'C.md': '# Missing status\n',
            }
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            (root / 'Bad.md').write_bytes(b'\xff')
            vault = Vault(root)
            self.addCleanup(vault.close)

            def paths(query, sort_by='path', descending=False):
                table = build_table(iter_note_text(vault), query, sort_by, descending)
                return [row.path for row in table.rows]

            for query in ('', 'tag:work', 'path:Work', '[status]', '[status:active]', 'property:status=active'):
                self.assertEqual(paths(query), sorted(vault.search(query), key=str.casefold), query)
            self.assertIn('Bad.md', paths(''))
            self.assertNotIn('Bad.md', paths('tag:work'))
            self.assertEqual(paths('tag:work'), ['Work/Later.md', 'Work/Nested.md', 'Work/Plan.md'])
            self.assertNotIn('Homework.md', paths('tag:work'))
            self.assertEqual(paths('[status:active]'), ['Homework.md', 'Personal.md', 'Work/Plan.md'])
            personal = build_table(iter_note_text(vault), 'tag:personal')
            self.assertEqual(personal.columns, ('status', 'tags'))
            self.assertNotIn('mood', personal.columns)
            self.assertEqual(paths('path:Work', 'count'),
                             ['Work/Plan.md', 'Work/Later.md', 'Homework.md', 'Work/Nested.md'])
            self.assertEqual(paths('path:Work', 'count', True),
                             ['Work/Later.md', 'Work/Plan.md', 'Homework.md', 'Work/Nested.md'])
            names = paths('', 'name')
            self.assertLess(names.index('A.md'), names.index('b.md'))
            overridden = build_table(iter_note_text(vault, {'Work/Plan.md': '---\nstatus: draft\n---\n'}),
                                     '[status:draft]', 'path')
            self.assertEqual([row.path for row in overridden.rows], ['Work/Plan.md'])
            self.assertEqual(vault.search('[status:draft]'), [])

    def test_sort_keeps_missing_values_last_and_orders_types(self):
        rows = [
            BaseRow('A.md', 'A', ('work',), {'status': 'b', 'n': 2, 'flag': True}),
            BaseRow('B.md', 'b', ('personal',), {'status': 'a', 'n': 10, 'flag': False}),
            BaseRow('C.md', 'C', (), {}),
            BaseRow('D.md', 'D', ('work', 'personal'), {'status': '', 'n': None, 'label': 'm'}),
            BaseRow('E.md', 'E', (), {'label': 'A'}),
        ]
        self.assertEqual([row.path for row in sort_rows(rows, 'status')], ['B.md', 'A.md', 'C.md', 'D.md', 'E.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'status', True)], ['A.md', 'B.md', 'C.md', 'D.md', 'E.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'n')], ['A.md', 'B.md', 'C.md', 'D.md', 'E.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'n', True)], ['B.md', 'A.md', 'C.md', 'D.md', 'E.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'name')], ['A.md', 'B.md', 'C.md', 'D.md', 'E.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'name', True)], ['E.md', 'D.md', 'C.md', 'B.md', 'A.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'tags')], ['C.md', 'E.md', 'B.md', 'A.md', 'D.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'flag')], ['B.md', 'A.md', 'C.md', 'D.md', 'E.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'label')], ['E.md', 'D.md', 'A.md', 'B.md', 'C.md'])
        self.assertEqual([row.path for row in sort_rows(rows, 'label', True)], ['D.md', 'E.md', 'A.md', 'B.md', 'C.md'])
        notes = ((row.path, '') for row in rows)
        self.assertEqual([row.path for row in build_table(notes, sort_by='', descending=True).rows],
                         [row.path for row in sort_rows(rows, 'name', True)])

    def test_set_property_rewrites_only_frontmatter(self):
        original = '---\ntags: work\naliases:\n  - Blueprint\nstatus: active\ncount: 2\n---\nKeep this body.  \n'
        updated = set_property(original, 'status', 'later')
        self.assertEqual(properties(updated)['status'], 'later')
        self.assertEqual(properties(updated)['aliases'], ['Blueprint'])
        self.assertEqual(properties(updated)['count'], 2)
        self.assertTrue(updated.endswith('Keep this body.  \n'))
        cleared = set_property('---\nstatus: active\n---\n# Hi\n', 'status', '   ')
        self.assertEqual(cleared, '# Hi\n')
        added = set_property('# Hi\n', 'status', 'active')
        self.assertEqual(added, '---\nstatus: active\n---\n# Hi\n')
        typed = set_property('# Hi\n', 'flag', 'false')
        self.assertIs(properties(typed)['flag'], False)
        self.assertIn('flag: false\n', typed)
        numbered = set_property(typed, 'count', '3')
        self.assertEqual(properties(numbered)['count'], 3)
        listed = set_property(numbered, 'items', '[a, b]')
        self.assertEqual(properties(listed)['items'], ['a', 'b'])
        words = set_property('# Hi\n', 'status', 'yes')
        self.assertEqual(properties(words)['status'], 'yes')
        self.assertIn("'yes'", words)
        leading = set_property('# Hi\n', 'code', '01')
        self.assertEqual(properties(leading)['code'], '01')
        self.assertIn("'01'", leading)
        self.assertEqual(properties(set_property('# Hi\n', 'n', '+2'))['n'], 2)
        self.assertEqual(properties(set_property('# Hi\n', 'n', '-3'))['n'], -3)
        self.assertEqual(properties(set_property('# Hi\n', 'n', '1.5'))['n'], 1.5)
        crlf = '---\r\nstatus: a\r\n---\r\nBody  \r\n'
        rewritten = set_property(crlf, 'status', 'b')
        self.assertNotIn('\n', rewritten.replace('\r\n', ''))
        self.assertIn('Body  ', rewritten)
        broken = '---\ninvalid: [\n---\n# Broken\n'
        with self.assertRaises(ValueError):
            set_property(broken, 'status', 'x')
        for key in ('', '  ', 'bad\nkey', 'nul\x00'):
            with self.assertRaises(ValueError):
                set_property('# Hi\n', key, 'x')


class SlidesLogicTests(unittest.TestCase):
    def test_split_ignores_frontmatter_fences_and_non_breaks(self):
        self.assertEqual(split_slides('# One\n\n```\n---\n```\n\n---\n\n# Two\n'),
                         ['# One\n\n```\n---\n```', '# Two'])
        self.assertEqual(split_slides('---\ntitle: Talk\n---\n# Only\n'), ['# Only'])
        self.assertEqual(split_slides('---\ntitle: Talk\n---\n'), [''])
        self.assertEqual(split_slides(''), [''])
        self.assertEqual(split_slides('---\n# A\n---\n'), [''])
        self.assertEqual(split_slides('---\n\n# A\n'), ['# A'])
        self.assertEqual(split_slides('# A\n---\n'), ['# A'])
        self.assertEqual(split_slides('# A\n---\n---\n# B\n'), ['# A', '', '# B'])
        self.assertEqual(split_slides('before\n    ---\nafter\n'), ['before\n    ---\nafter'])
        self.assertEqual(split_slides('a\n----\nb\n***\nc\n'), ['a\n----\nb\n***\nc'])
        self.assertEqual(split_slides('Left\r\n---\r\nRight\r\n'), ['Left', 'Right'])
        self.assertEqual(split_slides('~~~\n---\n~~~\n\n---\n\n# Next\n'), ['~~~\n---\n~~~', '# Next'])


class ConverterLogicTests(unittest.TestCase):
    def test_links_wikilinks_and_roam_markers(self):
        cases = {
            '[Label](Note.md)': '[[Note|Label]]',
            '[Plan](Plan.md)': '[[Plan]]',
            '[Later](Work/Later.md)': '[[Work/Later]]',
            '[see](Plan.md#Intro)': '[[Plan#Intro|see]]',
            '[Intro](#Intro)': '[[#Intro]]',
            '[See](#Intro)': '[[#Intro|See]]',
            '[Label](My%20Note.md)': '[[My Note|Label]]',
            '[Plan](Plan.md#My%20Heading)': '[[Plan#My Heading]]',
            '[Plan](<Plan.md#My Heading>)': '[[Plan#My Heading]]',
            '[Plan](Plan.md "Title")': '[[Plan]]',
            '[Plan](<Plan.md>)': '[[Plan]]',
            '[Plan](./Plan.md)': '[[Plan]]',
            '[web](https://example.com)': '[web](https://example.com)',
            '[x](pic.png)': '[x](pic.png)',
            '![x](Note.md)': '![x](Note.md)',
            '[x](Note)': '[x](Note)',
            '[x](Note.md?q=1)': '[x](Note.md?q=1)',
            '[label][Note.md]': '[label][Note.md]',
            '[a|b](Note.md)': '[a|b](Note.md)',
            '\\[Plan](Plan.md)': '\\[Plan](Plan.md)',
        }
        for source, expected in cases.items():
            self.assertEqual(convert_text(source, 'wikilinks'), expected, source)
        self.assertEqual(convert_text('[[My Note|Label]]', 'markdown'), '[Label](My%20Note.md)')
        self.assertEqual(convert_text('[[Plan#My Heading]]', 'markdown'), '[Plan](Plan.md#My%20Heading)')
        self.assertEqual(convert_text('[[Note]]', 'markdown'), '[Note](Note.md)')
        self.assertEqual(convert_text('![[Note]] [[pic.png]]', 'markdown'), '![[Note]] [[pic.png]]')
        protected = '---\nsee: [Plan](Plan.md)\n---\n`[Plan](Plan.md)`\n```\n[Plan](Plan.md)\n```\n[Plan](Plan.md)\n'
        converted = convert_text(protected, 'wikilinks')
        self.assertIn('see: [Plan](Plan.md)', converted)
        self.assertIn('`[Plan](Plan.md)`', converted)
        self.assertIn('```\n[Plan](Plan.md)\n```', converted)
        self.assertTrue(converted.endswith('[[Plan]]\n'))
        self.assertEqual(convert_text('See [Plan](Plan.md)\r\n', 'wikilinks'), 'See [[Plan]]\r\n')
        roam = '''---
tag: #[[keep]]
---
#[[my tag]]
#[[work/project]]
{{[[TODO]]}} write
- {{[[DONE]]}} shipped
  {{[[done]]}} indented
please {{[[TODO]]}} now
#[[TODO]]
`#[[keep]]`
```
{{[[TODO]]}}
```
#[[ship]]
'''
        obs = convert_text(roam, 'obsidian')
        self.assertIn('#my-tag', obs)
        self.assertIn('#work/project', obs)
        self.assertIn('- [ ] write', obs)
        self.assertIn('- [x] shipped', obs)
        self.assertIn('  - [x] indented', obs)
        self.assertIn('please [ ] now', obs)
        self.assertIn('#TODO', obs)
        self.assertNotIn('- [ ] #TODO', obs)
        self.assertIn('tag: #[[keep]]', obs)
        self.assertIn('`#[[keep]]`', obs)
        self.assertIn('```\n{{[[TODO]]}}\n```', obs)
        self.assertTrue(obs.rstrip().endswith('#ship'))
        source = 'See [Plan](Plan.md) and [Later](Later.md)\n'
        edit = conversion_edit(source, 'wikilinks')
        self.assertEqual(edit, TextEdit(0, len(source), 'See [[Plan]] and [[Later]]\n'))
        self.assertIsNone(conversion_edit(edit.replacement, 'wikilinks'))
        with self.assertRaises(ValueError):
            convert_text('x', 'nope')

    def test_selected_notes_follow_the_file_tree(self):
        notes = {'Imported/A.md': object(), 'Imported/B.md': object(), 'Plain.md': object()}

        def owner(path):
            return SimpleNamespace(files=SimpleNamespace(get_selected_row=lambda: SimpleNamespace(path=path)),
                                   vault=SimpleNamespace(notes=notes))

        self.assertEqual(selected_note_paths(owner('Imported/')), ['Imported/A.md', 'Imported/B.md'])
        self.assertEqual(selected_note_paths(owner('Plain.md')), ['Plain.md'])
        self.assertEqual(selected_note_paths(owner('Missing.md')), [])
        self.assertEqual(selected_note_paths(SimpleNamespace(files=None, vault=None)), [])
        empty = owner('Plain.md')
        empty.files = SimpleNamespace(get_selected_row=lambda: None)
        self.assertEqual(selected_note_paths(empty), [])


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class PluginUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application(application_id='org.librevault.PluginUITests',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        files = {
            'Welcome.md': '# Welcome\n',
            'Work/Plan.md': '---\ntags: work\naliases:\n  - Blueprint\nstatus: active\ncount: 2\n---\n# Plan\nKeep this body.  \n',
            'Work/Later.md': '---\ntags: work\nstatus: later\ncount: 10\n---\n# Later\n',
            'Personal.md': '---\ntags: personal\nstatus: active\n---\n# Personal\n',
            'Talk.md': '---\ntitle: Talk\n---\n# One\n\n```\n---\n```\n\n---\n\n# Two\n\n---\n\n# Three\n',
            'Links.md': 'See [Plan](Work/Plan.md) and [Later](Work/Later.md).\n\n`[Keep](Keep.md)` stays.\n\n[web](https://example.com)\n',
            'Plain.md': '#[[nope]]\n',
            'Imported/A.md': '{{[[TODO]]}} write\n#[[ship it]]\n',
            'Imported/B.md': '{{[[DONE]]}} shipped\n\n```\n#[[keep]]\n```\n\n#[[ship]]\n',
            'Broken.md': '---\ninvalid: [\n---\n# Broken\n',
        }
        for relative, text in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.window = self.make_window()

    def make_window(self):
        window = Window(self.app, self.root, preferences_path=self.root / '.settings.json')
        wait_for_work(window)
        self.addCleanup(window.destroy)
        return window

    def drain(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not settle')

    def press(self, widget, key):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = key
        event.state = 0
        if widget.get_window() is not None:
            event.window = widget.get_window()
        return widget.do_key_press_event(event)

    def reading_text(self, slides):
        texts = []

        def walk(widget):
            if isinstance(widget, Gtk.Label):
                texts.append(widget.get_text())
            if hasattr(widget, 'get_children'):
                for child in widget.get_children():
                    walk(child)

        walk(slides.reading)
        return '\n'.join(texts)

    def column(self, bases, key):
        return next(column for column in bases.tree.get_columns() if column.key == key)

    def select_path(self, path):
        for row in self.window.files.get_children():
            if row.path == path:
                self.window.files.select_row(row)
                return row
        self.fail(f'Missing file-tree row {path}')

    def palette_titles(self):
        self.window.show_command_palette()
        palette = self.window.palette
        return palette, [row.title for row in palette.results.get_children()]

    def test_palette_opens_each_plugin(self):
        palette, titles = self.palette_titles()
        self.assertEqual(titles[0], 'Open keybind cheatsheet')
        for title in ('Open Bases view', 'Start slides presentation', 'Format converter…'):
            self.assertIn(title, titles)
        palette.search.set_text('bases')
        palette.search.emit('activate')
        self.drain()
        bases = self.window.tabs.get_nth_page(self.window.tabs.get_current_page())
        self.assertIsInstance(bases, BasesView)
        self.assertIs(self.window.show_bases(), bases)
        self.window.open_note('Welcome.md')
        palette, _ = self.palette_titles()
        palette.search.set_text('slides')
        palette.search.emit('activate')
        self.drain()
        self.assertIsNotNone(self.window.slides_window)
        self.assertEqual(self.window.slides_window.counter.get_text(), 'Slide 1 of 1')
        self.assertTrue(self.press(self.window.slides_window, Gdk.KEY_Escape))
        self.assertIsNone(self.window.slides_window)
        palette, _ = self.palette_titles()
        palette.search.set_text('format converter')
        palette.search.emit('activate')
        self.drain()
        dialog = self.window.converter_dialog
        self.assertTrue(dialog.get_visible())
        dialog.response(Gtk.ResponseType.CANCEL)
        self.assertIsNone(self.window.converter_dialog)

    def test_bases_filters_sorts_columns_and_opens_notes(self):
        first = self.window.show_bases()
        self.assertIs(self.window.show_bases(), first)
        self.window.open_split(first)
        self.assertIsNone(self.window.split_view)
        pane = self.window.open_note('Plain.md')
        pane.buffer.set_text('---\nstatus: draft\n---\n# Plain\n')
        first.search.set_text('[status:draft]')
        first.reload()
        self.assertEqual([row.path for row in first.table.rows], ['Plain.md'])
        self.assertEqual((self.root / 'Plain.md').read_text(), '#[[nope]]\n')
        second = self.window.show_bases(state={'id': 'restore-me', 'query': 'tag:work', 'sort': 'path',
                                               'descending': True, 'hidden': ['status']})
        self.assertIsNot(first, second)
        self.assertEqual(second.workspace_id, 'restore-me')
        self.assertEqual([row.path for row in second.table.rows], ['Work/Plan.md', 'Work/Later.md'])
        self.assertNotIn('status', second.visible_keys())
        self.assertIn('name', second.visible_keys())
        self.assertIn('path', second.visible_keys())
        self.assertFalse(second.column_checks['name'].get_sensitive())
        self.assertFalse(second.column_checks['path'].get_sensitive())
        second.search.set_text('path:Work')
        second.reload()
        self.column(second, 'count').emit('clicked')
        self.drain()
        self.assertEqual(self.column(second, 'count').get_title(), 'count  ↑')
        self.assertEqual([row.path for row in second.table.rows], ['Work/Plan.md', 'Work/Later.md'])
        self.column(second, 'count').emit('clicked')
        self.drain()
        self.assertEqual(self.column(second, 'count').get_title(), 'count  ↓')
        self.assertEqual([row.path for row in second.table.rows], ['Work/Later.md', 'Work/Plan.md'])
        second.column_checks['status'].set_active(False)
        self.drain()
        self.assertNotIn('status', second.visible_keys())
        second.search.set_text('tag:missing')
        second.reload()
        self.assertEqual(second.stack.get_visible_child_name(), 'empty')
        self.assertEqual(second.empty.get_text(), 'No matching notes')
        self.assertEqual(second.summary.get_text(), '0 notes · 0 properties')
        second.search.set_text('path:Work')
        second.reload()
        second.tree.emit('row-activated', second.row_path('Work/Later.md'), second.name_column)
        self.assertEqual(self.window.current().note.path, 'Work/Later.md')
        self.window.show_bases()
        self.window.close_note(second)
        self.assertNotIn(second, self.window.bases_views)
        self.window.persist_workspace()

    def test_bases_edits_are_undoable_and_invalid_yaml_is_left_alone(self):
        pane = self.window.open_note('Work/Plan.md')
        original = pane.text()
        disk = (self.root / 'Work/Plan.md').read_bytes()
        bases = self.window.show_bases()
        bases.search.set_text('path:Work')
        bases.reload()
        renderer = bases.property_renderers['status']
        renderer.emit('edited', bases.row_path('Work/Plan.md').to_string(), 'later')
        self.drain()
        self.assertIn('status: later', pane.text())
        self.assertIn('Blueprint', pane.text())
        self.assertIn('Keep this body.  ', pane.text())
        self.assertEqual(self.window.status.get_text(), 'Updated status · Work/Plan.md')
        self.assertEqual((self.root / 'Work/Plan.md').read_bytes(), disk)
        self.assertTrue(pane.dirty)
        pane.undo()
        self.assertEqual(pane.text(), original)
        bases.reload()
        plan = next(row for row in bases.table.rows if row.path == 'Work/Plan.md')
        self.assertEqual(plan.properties['status'], 'active')
        self.assertFalse(pane.dirty)
        bases.commit_edit('Work/Plan.md', 'status', '  active  ')
        self.assertEqual(pane.text(), original)
        self.assertFalse(pane.dirty)
        personal = self.root / 'Personal.md'
        before = personal.read_bytes()
        bases.commit_edit('Personal.md', 'status', 'paused')
        self.assertIn(b'paused', personal.read_bytes())
        self.assertNotEqual(personal.read_bytes(), before)
        self.assertEqual(self.window.status.get_text(), 'Updated status · Personal.md')
        self.assertEqual((self.root / 'Work/Plan.md').read_bytes(), disk)
        broken = (self.root / 'Broken.md').read_bytes()
        bases.commit_edit('Broken.md', 'status', 'nope')
        self.assertEqual((self.root / 'Broken.md').read_bytes(), broken)
        self.assertIn('YAML', self.window.status.get_text())

    def test_workspace_restores_the_bases_tab(self):
        bases = self.window.show_bases()
        bases.search.set_text('path:Work')
        bases.set_sort('count')
        bases.column_checks['status'].set_active(False)
        self.drain()
        self.window.persist_workspace()
        saved = self.window.workspace_store.load(self.root)
        stored = next(page for page in saved['tabs'] if isinstance(page, dict) and page.get('kind') == 'bases')
        self.assertEqual(stored['query'], 'path:Work')
        self.assertEqual(stored['sort'], 'count')
        self.assertFalse(stored['descending'])
        self.assertIn('status', stored['hidden'])
        self.assertEqual(saved['active'], {'bases': bases.workspace_id})
        self.window.destroy()
        restored = self.make_window()
        page = restored.tabs.get_nth_page(restored.tabs.get_current_page())
        self.assertIsInstance(page, BasesView)
        self.assertEqual(page.search.get_text(), 'path:Work')
        self.assertEqual(page.sort_column, 'count')
        self.assertFalse(page.descending)
        self.assertIn('status', page.hidden_columns)
        self.assertEqual([row.path for row in page.table.rows], ['Work/Plan.md', 'Work/Later.md'])
        self.assertNotIn('status', page.visible_keys())
        restored.persist_workspace()

    def test_slides_follow_the_editor_buffer_and_keyboard(self):
        pane = self.window.open_note('Talk.md')
        slides = self.window.show_slides()
        self.assertEqual(slides.counter.get_text(), 'Slide 1 of 3')
        self.assertIn('One', self.reading_text(slides))
        self.assertNotIn('Two', self.reading_text(slides))
        self.assertNotIn('Talk', self.reading_text(slides))
        self.assertFalse(slides.previous.get_sensitive())
        self.assertTrue(self.press(slides, Gdk.KEY_Right))
        self.assertEqual(slides.counter.get_text(), 'Slide 2 of 3')
        self.assertIn('Two', self.reading_text(slides))
        self.assertNotIn('One', self.reading_text(slides))
        self.assertTrue(self.press(slides, Gdk.KEY_Left))
        self.assertTrue(self.press(slides, Gdk.KEY_space))
        self.assertEqual(slides.counter.get_text(), 'Slide 2 of 3')
        self.assertTrue(self.press(slides, Gdk.KEY_Page_Down))
        self.assertEqual(slides.counter.get_text(), 'Slide 3 of 3')
        self.assertFalse(slides.next.get_sensitive())
        self.assertTrue(self.press(slides, Gdk.KEY_Right))
        self.assertEqual(slides.counter.get_text(), 'Slide 3 of 3')
        self.assertTrue(self.press(slides, Gdk.KEY_Home))
        self.assertEqual(slides.counter.get_text(), 'Slide 1 of 3')
        self.assertTrue(self.press(slides, Gdk.KEY_End))
        self.assertEqual(slides.counter.get_text(), 'Slide 3 of 3')
        self.assertTrue(self.press(slides, Gdk.KEY_Page_Up))
        self.assertEqual(slides.counter.get_text(), 'Slide 2 of 3')
        self.assertTrue(self.press(slides, Gdk.KEY_KP_Left))
        self.assertEqual(slides.counter.get_text(), 'Slide 1 of 3')
        self.assertTrue(self.press(slides, Gdk.KEY_KP_End))
        self.assertEqual(slides.counter.get_text(), 'Slide 3 of 3')
        slides.previous.emit('clicked')
        self.assertEqual(slides.counter.get_text(), 'Slide 2 of 3')
        slides.next.emit('clicked')
        self.assertEqual(slides.counter.get_text(), 'Slide 3 of 3')
        self.assertTrue(self.press(slides, Gdk.KEY_F11))
        self.assertTrue(slides.fullscreen_on)
        self.assertTrue(self.press(slides, Gdk.KEY_F11))
        self.assertFalse(slides.fullscreen_on)
        self.assertTrue(self.press(slides, Gdk.KEY_Escape))
        self.assertIsNone(self.window.slides_window)
        pane.buffer.insert(pane.buffer.get_end_iter(), '\n---\n# Extra\n')
        slides = self.window.show_slides()
        self.assertEqual(slides.counter.get_text(), 'Slide 1 of 4')
        self.assertNotIn('Talk', self.reading_text(slides))
        slides.go(3)
        self.assertEqual(slides.counter.get_text(), 'Slide 4 of 4')
        self.assertIn('Extra', self.reading_text(slides))
        self.assertNotIn('Three', self.reading_text(slides))
        slides.destroy()
        self.window.show_bases()
        self.assertIsNone(self.window.current())
        self.assertIsNone(self.window.show_slides())
        self.assertEqual(self.window.status.get_text(), 'Open a note to start the slides presentation.')
        self.assertIsNone(self.window.slides_window)

    def test_converter_edits_the_active_note_or_selected_folder(self):
        from librevault.converter import ConverterDialog
        pane = self.window.open_note('Links.md')
        original = pane.text()
        dialog = self.window.show_converter()
        dialog.operation.set_active_id('wikilinks')
        dialog.scope.set_active_id('active')
        dialog.response(Gtk.ResponseType.OK)
        self.assertIsNone(self.window.converter_dialog)
        self.assertEqual(self.window.status.get_text(), 'Converted 1 note')
        self.assertIn('[[Work/Plan]]', pane.text())
        self.assertIn('[[Work/Later]]', pane.text())
        self.assertIn('`[Keep](Keep.md)`', pane.text())
        self.assertIn('[web](https://example.com)', pane.text())
        pane.undo()
        self.assertEqual(pane.text(), original)
        imported = self.window.open_note('Imported/A.md')
        imported.buffer.insert(imported.buffer.get_end_iter(), '\nEXTRA')
        disk_a = (self.root / 'Imported/A.md').read_bytes()
        disk_plain = (self.root / 'Plain.md').read_bytes()
        self.select_path('Imported/')
        dialog = ConverterDialog(self.window)
        dialog.show_all()
        dialog.operation.set_active_id('obsidian')
        dialog.scope.set_active_id('selected')
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(self.window.status.get_text(), 'Converted 2 notes')
        self.assertEqual(imported.text(), '- [ ] write\n#ship-it\n\nEXTRA')
        self.assertEqual((self.root / 'Imported/A.md').read_bytes(), disk_a)
        self.assertEqual((self.root / 'Imported/B.md').read_text(),
                         '- [x] shipped\n\n```\n#[[keep]]\n```\n\n#ship\n')
        self.assertEqual((self.root / 'Plain.md').read_bytes(), disk_plain)
        imported.undo()
        self.assertEqual(imported.text(), '{{[[TODO]]}} write\n#[[ship it]]\n\nEXTRA')
        self.window.show_bases()
        dialog = self.window.show_converter()
        dialog.scope.set_active_id('active')
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(dialog.problem.get_text(), 'Open a note to convert it.')
        self.assertTrue(dialog.get_visible())
        self.assertIs(self.window.converter_dialog, dialog)
        dialog.response(Gtk.ResponseType.CANCEL)
        self.assertIsNone(self.window.converter_dialog)
        self.window.files.unselect_all()
        dialog = ConverterDialog(self.window)
        dialog.show_all()
        dialog.scope.set_active_id('selected')
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(dialog.problem.get_text(), 'Select a note or folder in the file tree.')
        self.assertTrue(dialog.get_visible())
        dialog.response(Gtk.ResponseType.CANCEL)
