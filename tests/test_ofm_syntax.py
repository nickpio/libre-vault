"""Obsidian highlights, comments, strikethrough, and fenced code colors."""
from tests.gui_wait import wait_for_work
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from librevault.appearance import PALETTES
from librevault.editing import format_inline
from librevault.formatting import markdown_layout
from librevault.syntax import TOKEN_STYLES, canonical_language, code_markup, highlight_code

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from librevault.reading import MARKDOWN, reading_source
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False
    MARKDOWN = None
    reading_source = None


class InlineFormatTests(unittest.TestCase):
    def transform(self, text, start, end, marker):
        edit, selection = format_inline(text, start, end, marker)
        return text[:edit.start] + edit.replacement + text[edit.end:], selection

    def test_markers_wrap_trim_and_unwrap(self):
        self.assertEqual(self.transform(' café ', 0, 6, '=='), (' ==café== ', (3, 7)))
        self.assertEqual(self.transform('==café==', 2, 6, '=='), ('café', (0, 4)))
        self.assertEqual(self.transform('==café==', 0, 8, '=='), ('café', (0, 4)))
        self.assertEqual(self.transform('~~café~~', 2, 6, '~~'), ('café', (0, 4)))
        self.assertEqual(self.transform('café', 0, 4, '~~'), ('~~café~~', (2, 6)))
        self.assertEqual(self.transform('', 0, 0, '=='), ('====', (2, 2)))
        self.assertEqual(self.transform('   ', 0, 3, '~~'), ('   ', (0, 3)))

    def test_highlight_does_not_remove_bold_and_escaped_markers_stay(self):
        self.assertEqual(self.transform('**café**', 2, 6, '=='), ('**==café==**', (4, 8)))
        self.assertEqual(self.transform(r'\==café==', 3, 7, '==')[0], r'\====café====')
        self.assertEqual(self.transform('**café**', 2, 6, '**'), ('café', (0, 4)))
        self.assertEqual(self.transform('***café***', 3, 7, '*'), ('**café**', (2, 6)))


class LayoutTests(unittest.TestCase):
    def pieces(self, text, style):
        return [text[span.start:span.end] for span in markdown_layout(text).spans if span.style == style]

    def test_highlight_strikethrough_and_comments_keep_source_offsets(self):
        text = 'Use ==literal== and ~~gone~~.\nA %%secret%% B\n'
        self.assertEqual(self.pieces(text, 'highlight'), ['literal'])
        self.assertEqual(self.pieces(text, 'strikethrough'), ['gone'])
        self.assertEqual(self.pieces(text, 'comment'), ['%%secret%%'])
        self.assertEqual(self.pieces(text, 'syntax').count('=='), 2)
        self.assertEqual(self.pieces(text, 'syntax').count('~~'), 2)
        self.assertEqual(self.pieces(text, 'syntax').count('%%'), 2)
        secret = next(span for span in markdown_layout(text).spans
                      if span.style == 'comment')
        self.assertEqual(secret.start, text.index('%%secret%%'))

    def test_nested_markers_multiline_comments_and_breaks(self):
        self.assertEqual(self.pieces('==hi ~~there~~ friend==\n', 'highlight'), ['hi ~~there~~ friend'])
        self.assertEqual(self.pieces('==hi ~~there~~ friend==\n', 'strikethrough'), ['there'])
        self.assertEqual(self.pieces('==one\ntwo==\n', 'highlight'), ['one\ntwo'])
        self.assertEqual(self.pieces('==one\n\ntwo==\n', 'highlight'), [])
        block = 'Before\n%%\nsecret\n%%\nAfter\n'
        self.assertEqual(self.pieces(block, 'comment'), ['%%\nsecret\n%%'])
        self.assertEqual(self.pieces('See \\==no== please\n', 'highlight'), [])
        self.assertEqual(self.pieces('==a \\== b==\n', 'highlight'), ['a \\== b'])

    def test_code_fences_inline_code_and_frontmatter_stay_literal(self):
        fenced = '```\n==no==\n%%no%%\n~~no~~\n```\n==yes==\n%%hide%%\n'
        self.assertEqual(self.pieces(fenced, 'highlight'), ['yes'])
        self.assertEqual(self.pieces(fenced, 'comment'), ['%%hide%%'])
        self.assertEqual(self.pieces(fenced, 'strikethrough'), [])
        self.assertEqual(self.pieces('A `==no==` ==yes==\n', 'highlight'), ['yes'])
        self.assertEqual(self.pieces('`~~no~~` ~~yes~~\n', 'strikethrough'), ['yes'])
        self.assertEqual(self.pieces('Say `%%keep%%` and %%hide%%.\n', 'comment'), ['%%hide%%'])
        self.assertIn('%%keep%%', ''.join(self.pieces('Say `%%keep%%` and %%hide%%.\n', 'code')))
        front = '---\nnote: %%keep%%\n---\nBody %%hide%%\n'
        self.assertEqual(self.pieces(front, 'comment'), ['%%hide%%'])
        self.assertIn('%%keep%%', self.pieces(front, 'metadata')[0])

    def test_comments_hide_inner_markup_from_the_outline(self):
        text = '%%\n# Secret\n%%\n\n# Real\n'
        self.assertEqual([heading.label for heading in markdown_layout(text).headings], ['Real'])
        self.assertEqual(self.pieces(text, 'highlight'), [])
        self.assertIn('# Secret', self.pieces(text, 'comment')[0])

    def test_python_fence_tokens_use_source_offsets(self):
        text = '```python\ndef f():\n    return "hi"  # note\n```\n'
        layout = markdown_layout(text)
        keywords = [text[span.start:span.end] for span in layout.spans if span.style == 'code_keyword']
        self.assertIn('def', keywords)
        self.assertIn('return', keywords)
        def_span = next(span for span in layout.spans
                        if span.style == 'code_keyword' and text[span.start:span.end] == 'def')
        self.assertEqual(def_span.start, text.index('def'))
        strings = [text[span.start:span.end] for span in layout.spans if span.style == 'code_string']
        self.assertEqual(strings, ['"hi"'])
        comments = [text[span.start:span.end] for span in layout.spans if span.style == 'code_comment']
        self.assertEqual(comments, ['# note'])
        self.assertTrue(any(piece.startswith('```python') for piece in self.pieces(text, 'code')))

    def test_spans_stay_inside_incomplete_source(self):
        samples = ('', '==', '%%', '~~', '```python\ndef', '==a', '%%\nopen', '\\==a==',
                   '===text===', '~~~~', '```\n%%')
        for text in samples:
            with self.subTest(text=text):
                for span in markdown_layout(text).spans:
                    self.assertTrue(0 <= span.start < span.end <= len(text), span)
                    self.assertLessEqual(span.end, len(text))


class SyntaxTests(unittest.TestCase):
    def tokens(self, language, source):
        found = highlight_code(language, source)
        cursor = 0
        for token in found:
            self.assertGreaterEqual(token.start, cursor)
            self.assertLess(token.start, token.end)
            self.assertLessEqual(token.end, len(source))
            self.assertIn(token.style, TOKEN_STYLES)
            cursor = token.end
        return found

    def style_at(self, language, source, word):
        index = source.index(word)
        return [token.style for token in self.tokens(language, source) if token.start <= index < token.end]

    def test_languages_and_aliases_cover_the_requested_set(self):
        names = (
            'python', 'py', 'javascript', 'js', 'typescript', 'ts', 'json', 'yaml', 'yml',
            'sh', 'bash', 'shell', 'html', 'css', 'sql', 'rust', 'go', 'c', 'cpp',
            'markdown', 'md',
        )
        for name in names:
            with self.subTest(language=name):
                self.tokens(name, 'value = 1  # note\n"text"\n')
        self.assertEqual(canonical_language('Py'), 'python')
        self.assertEqual(canonical_language('c++'), 'cpp')
        self.assertEqual(canonical_language('JS'), 'javascript')

    def test_python_tokens_ignore_words_inside_strings(self):
        source = 'def greet(name):\n    return f"Hello {name} def"  # hi\nvalue = 42\n'
        self.assertEqual(self.style_at('python', source, 'def greet'), ['code_keyword'])
        self.assertEqual(self.style_at('py', source, 'return'), ['code_keyword'])
        self.assertEqual(self.style_at('python', source, '42'), ['code_number'])
        string = next(token for token in self.tokens('python', source) if token.style == 'code_string')
        self.assertEqual(source[string.start:string.end], 'f"Hello {name} def"')
        self.assertEqual(self.style_at('python', source, '# hi'), ['code_comment'])
        literal = 'text = "def 42"\n'
        self.assertEqual(self.style_at('python', literal, 'def'), ['code_string'])
        self.assertEqual(self.style_at('python', literal, '42'), ['code_string'])

    def test_common_languages_mark_keywords_strings_and_comments(self):
        samples = (
            ('javascript', 'const value = 1; // note\nfunction hi() { return "ok"; }\n', 'const', 'code_keyword'),
            ('js', 'console.log("ok")\n', 'console', 'code_builtin'),
            ('typescript', 'interface Item { name: string }\n', 'interface', 'code_keyword'),
            ('ts', 'const item: string = "x";\n', 'string', 'code_keyword'),
            ('json', '{ "ok": true, "n": 2 }\n', 'true', 'code_keyword'),
            ('yaml', 'name: Ada\ncount: 2\n# note\n', '# note', 'code_comment'),
            ('yml', 'enabled: true\n', 'true', 'code_keyword'),
            ('sh', 'echo "hi"\n# note\n', 'echo', 'code_builtin'),
            ('bash', 'if true; then echo "$HOME"; fi\n', 'if', 'code_keyword'),
            ('shell', 'echo $#\n', '$#', 'code_builtin'),
            ('html', '<p class="x">Hi</p> <!-- c -->\n', 'class', 'code_builtin'),
            ('css', 'p { color: #336699; margin: 2px; } /* c */\n', 'color', 'code_builtin'),
            ('sql', 'SELECT name FROM users WHERE id = 3; -- tail\n', 'SELECT', 'code_keyword'),
            ('rust', 'fn main() { let value = 1; // note\n}\n', 'fn', 'code_keyword'),
            ('go', 'package main\nfunc main() { println("hi") }\n', 'func', 'code_keyword'),
            ('c', '#include <stdio.h>\nint main(void) { return 0; }\n', 'int', 'code_keyword'),
            ('cpp', 'class Item { public: int value; };\n', 'class', 'code_keyword'),
            ('markdown', '# Title\n`code`\n', '# Title', 'code_keyword'),
            ('md', 'See [a](b.md)\n', '[a](b.md)', 'code_builtin'),
        )
        for language, source, word, style in samples:
            with self.subTest(language=language, word=word):
                self.assertIn(style, self.style_at(language, source, word))

    def test_markup_escapes_text_and_uses_the_palette(self):
        markup = code_markup('def f():\n', 'python', PALETTES['dark'])
        self.assertIn(PALETTES['dark']['code_keyword'], markup)
        self.assertIn('>def</span>', markup)
        escaped = code_markup('a < b & c', 'text', PALETTES['light'])
        self.assertIn('&lt;', escaped)
        self.assertIn('&amp;', escaped)
        self.assertNotIn('< b', escaped)

    def test_every_theme_defines_highlight_and_syntax_colors(self):
        required = ('highlight_bg', 'highlight_fg', *TOKEN_STYLES)
        for theme, colors in PALETTES.items():
            for key in required:
                self.assertRegex(colors[key], r'#[0-9a-fA-F]{6}')


@unittest.skipUnless(GTK_AVAILABLE and reading_source is not None, 'GTK and a display are required')
class ReadingSyntaxTests(unittest.TestCase):
    def test_reading_source_hides_comments_outside_code(self):
        body, notes = reading_source(
            'Visible %%hidden%% end\n\n`%%keep%%`\n\n```\n%%keep%%\n```\n\n[^n]: shown\n')
        self.assertNotIn('hidden', body)
        self.assertIn('Visible', body)
        self.assertIn('end', body)
        self.assertEqual(body.count('%%keep%%'), 2)
        self.assertEqual(notes, [('n', 'shown')])
        hidden, visible = reading_source('%%\n[^n]: hidden\n%%\n\n[^y]: shown\n')
        self.assertNotIn('hidden', hidden)
        self.assertEqual(visible, [('y', 'shown')])

    def test_highlight_tokens_nest_inside_emphasis_and_skip_code(self):
        tokens = MARKDOWN.parse('Say ==hello **there**==.')
        kinds = [child.type for token in tokens if token.type == 'inline' for child in token.children or []]
        self.assertEqual(kinds.count('highlight_open'), 1)
        self.assertIn('strong_open', kinds)
        coded = MARKDOWN.parse('`==no==` ==yes==')
        text = ''.join(child.content for token in coded if token.type == 'inline'
                       for child in token.children or [] if child.type == 'text')
        self.assertIn('yes', text)
        self.assertNotIn('no', text)
        struck = MARKDOWN.parse('~~gone~~')
        struck_kinds = [child.type for token in struck if token.type == 'inline' for child in token.children or []]
        self.assertIn('s_open', struck_kinds)


@unittest.skipUnless(GTK_AVAILABLE, 'GTK and a display are required')
class LivePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.OfmSyntaxTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('Start\n', encoding='utf-8')
        self.window = Window(self.app, self.root,
                             preferences_path=Path(self.directory.name) / 'settings.json')
        wait_for_work(self.window)
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
        self.pane.render()

    def select(self, start, end):
        self.pane.buffer.select_range(self.pane.buffer.get_iter_at_offset(end),
                                      self.pane.buffer.get_iter_at_offset(start))

    def key(self, keyval, modifiers=0):
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval, event.state = keyval, modifiers
        return self.pane.editor.emit('key-press-event', event)

    def tagged(self, offset, tag):
        return self.pane.buffer.get_iter_at_offset(offset).has_tag(tag)

    def test_live_preview_conceals_markers_and_styles_content(self):
        text = 'Before\n==Hi== ~~gone~~ %%secret%%\nAfter\n'
        self.source(text)
        self.pane.buffer.place_cursor(self.pane.buffer.get_start_iter())
        self.drain()
        self.pane.render()
        hi = text.index('Hi')
        gone = text.index('gone')
        secret = text.index('secret')
        self.assertTrue(self.tagged(hi, self.pane.tags['highlight']))
        self.assertTrue(self.tagged(gone, self.pane.tags['strikethrough']))
        self.assertTrue(self.tagged(secret, self.pane.tags['comment']))
        self.assertFalse(self.tagged(hi, self.pane.hidden))
        for marker in (text.index('=='), text.index('~~'), text.index('%%')):
            self.assertTrue(self.tagged(marker, self.pane.hidden))
        line = text.index('==Hi==')
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(line))
        self.drain()
        self.pane.render()
        self.assertFalse(self.tagged(text.index('=='), self.pane.hidden))
        self.assertTrue(self.tagged(hi, self.pane.tags['highlight']))

    def test_shortcuts_and_commands_toggle_highlight_and_strikethrough(self):
        self.source('word')
        self.select(0, 4)
        self.key(Gdk.KEY_H, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertEqual(self.pane.text(), '==word==')
        self.select(0, len(self.pane.text()))
        self.pane.highlight()
        self.assertEqual(self.pane.text(), 'word')
        self.select(0, 4)
        self.key(Gdk.KEY_X, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertEqual(self.pane.text(), '~~word~~')
        self.pane.strikethrough()
        self.assertEqual(self.pane.text(), 'word')
        self.select(0, 4)
        self.window.show_command_palette()
        palette = self.window.palette
        palette.search.set_text('toggle highlight')
        self.assertEqual(palette.results.get_selected_row().title, 'Toggle highlight')
        palette.search.emit('activate')
        self.assertEqual(self.pane.text(), '==word==')
        self.window.show_command_palette()
        palette = self.window.palette
        palette.search.set_text('toggle strikethrough')
        palette.search.emit('activate')
        self.assertIn('~~', self.pane.text())

    def test_fenced_code_uses_syntax_tags_in_the_editor(self):
        text = 'Intro\n```python\ndef f():\n    return 1\n```\n'
        self.source(text)
        self.pane.buffer.place_cursor(self.pane.buffer.get_start_iter())
        self.drain()
        self.pane.render()
        def_at = text.index('def')
        self.assertTrue(self.tagged(def_at, self.pane.tags['code_keyword']))
        self.assertTrue(self.tagged(def_at, self.pane.tags['code']))
        self.assertTrue(self.tagged(text.index('```'), self.pane.hidden))
        self.assertTrue(self.tagged(text.index('return'), self.pane.tags['code_keyword']))
        self.assertTrue(self.tagged(text.index('1'), self.pane.tags['code_number']))

    def descendants(self, widget):
        yield widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                yield from self.descendants(child)

    def test_reading_mode_renders_highlights_comments_and_code_blocks(self):
        source = (
            'Shown %%hidden words%% tail\n\n'
            'Say ==hello== and ~~gone~~.\n\n'
            '```python\ndef hi():\n    return 1\n```\n'
        )
        self.pane.buffer.set_text(source)
        self.pane.set_mode('reading')
        widgets = list(self.descendants(self.pane.reading.body))
        labels = [widget for widget in widgets if isinstance(widget, Gtk.Label)]
        plain = '\n'.join(widget.get_text() for widget in labels)
        self.assertIn('Shown', plain)
        self.assertIn('tail', plain)
        self.assertNotIn('hidden', plain)
        self.assertNotIn('%%', plain)
        hello = next(widget for widget in labels if 'hello' in widget.get_text())
        self.assertNotIn('==', hello.get_label())
        self.assertIn(PALETTES['dark']['highlight_bg'], hello.get_label())
        self.assertIn('<s>', next(widget.get_label() for widget in labels if 'gone' in widget.get_text()))
        self.assertTrue(any(widget.get_text() == 'python' for widget in labels))
        button = next(widget for widget in widgets
                      if isinstance(widget, Gtk.Button) and widget.get_label() == 'Copy code')
        board = Mock()
        with patch.object(Gtk.Clipboard, 'get', return_value=board):
            button.clicked()
        copied = board.set_text.call_args[0][0]
        self.assertIn('def hi():', copied)
        self.assertIn('return 1', copied)
        self.assertEqual(self.window.status.get_text(), 'Copied code')
        self.window.set_preferences(replace(self.window.preferences, theme='light'))
        self.drain()
        labels = [widget for widget in self.descendants(self.pane.reading.body) if isinstance(widget, Gtk.Label)]
        hello = next(widget for widget in labels if 'hello' in widget.get_text())
        self.assertIn(PALETTES['light']['highlight_bg'], hello.get_label())
        color = self.pane.tags['highlight'].get_property('background-rgba')
        rendered = '#{:02x}{:02x}{:02x}'.format(
            round(color.red * 255), round(color.green * 255), round(color.blue * 255))
        self.assertEqual(rendered, PALETTES['light']['highlight_bg'])


if __name__ == '__main__':
    unittest.main()
