"""Mermaid diagrams, math environments, callouts, footnotes, HTML, and media."""
from tests.gui_wait import wait_for_work
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
import wave

import cairo

from librevault.appearance import PALETTES
from librevault.attachments import attachment_markdown
from librevault.formatting import markdown_layout
from librevault.math_render import prepare_math, render_math_png
from librevault.mermaid import parse_mermaid, render_mermaid

try:
    from librevault.app import Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


def _png_size(data):
    surface = cairo.ImageSurface.create_from_png(BytesIO(data))
    return surface.get_width(), surface.get_height()


class DiagramMathFormattingTests(unittest.TestCase):
    def test_flowcharts_keep_direction_shapes_labels_and_chains(self):
        diagram = parse_mermaid(
            'graph TD\n'
            'A[Start] --> B(Next)\n'
            'B -->|yes| C{Stop}\n'
            'A --- D\n'
            'D -.-> E\n'
            'X --> Y --> Z\n'
            'P -- hello --> Q\n'
            'direction LR\n')
        self.assertEqual(diagram.kind, 'flowchart')
        self.assertEqual(diagram.direction, 'LR')
        self.assertEqual((diagram.nodes['A'].label, diagram.nodes['A'].shape), ('Start', 'rect'))
        self.assertEqual(diagram.nodes['B'].shape, 'round')
        self.assertEqual(diagram.nodes['C'].shape, 'diamond')
        edges = {(edge.src, edge.dst, edge.style, edge.label) for edge in diagram.edges}
        self.assertIn(('B', 'C', 'arrow', 'yes'), edges)
        self.assertIn(('A', 'D', 'line', ''), edges)
        self.assertIn(('D', 'E', 'dashed', ''), edges)
        self.assertIn(('X', 'Y', 'arrow', ''), edges)
        self.assertIn(('Y', 'Z', 'arrow', ''), edges)
        self.assertIn(('P', 'Q', 'arrow', 'hello'), edges)
        self.assertEqual(parse_mermaid('graph TB\nA-->B\n').direction, 'TD')
        self.assertEqual(parse_mermaid('flowchart RL\nA-->B\n').direction, 'RL')

    def test_rendered_diagrams_are_pngs_and_follow_direction_and_theme(self):
        dark = PALETTES['dark']
        down = render_mermaid('graph TD\nA --> B --> C\n', dark)
        across = render_mermaid('graph LR\nA --> B --> C\n', dark)
        self.assertEqual(down.kind, 'flowchart')
        self.assertTrue(down.png.startswith(b'\x89PNG'))
        self.assertGreater(_png_size(across.png)[0], _png_size(down.png)[0])
        self.assertNotEqual(render_mermaid('graph TD\nA --> B\n', PALETTES['light']).png, down.png)
        sequence = render_mermaid(
            'sequenceDiagram\nparticipant Alice as Alice A\nBob->>Alice: Hello\nAlice-->>Bob: Hi\n', dark)
        state = render_mermaid('stateDiagram-v2\n[*] --> Idle\nIdle --> [*]\n', dark)
        classes = render_mermaid('classDiagram\nclass Animal {\n  +int age\n}\nAnimal <|-- Dog\n', dark)
        self.assertEqual((sequence.kind, state.kind, classes.kind), ('sequence', 'state', 'class'))
        for image in (sequence, state, classes, render_mermaid('graph TD\nA --> A\n', dark)):
            self.assertTrue(image.png.startswith(b'\x89PNG'))
            self.assertGreater(_png_size(image.png)[0], 20)

    def test_sequence_state_and_class_diagrams_keep_their_structure(self):
        sequence = parse_mermaid(
            'sequenceDiagram\nparticipant Alice as Alice A\nBob->>Alice: Hello\nAlice-->>Bob: Hi\n')
        self.assertEqual(sequence.participants[0], ('Alice', 'Alice A'))
        self.assertEqual(sequence.messages[0][2:], ('->>', 'Hello'))
        self.assertEqual(sequence.messages[1][2], '-->>')
        state = parse_mermaid(
            'stateDiagram-v2\n[*] --> Idle\nIdle --> [*]: done\nstate "Long name" as Busy\n')
        self.assertEqual(state.kind, 'state')
        self.assertEqual(state.nodes['[*]start'].shape, 'circle')
        self.assertEqual(state.nodes['[*]end'].shape, 'circle')
        self.assertNotEqual('[*]start', '[*]end')
        self.assertEqual(state.nodes['Busy'].label, 'Long name')
        self.assertEqual(state.edges[0].dst, 'Idle')
        self.assertEqual(state.edges[1].label, 'done')
        classes = parse_mermaid('classDiagram\nclass Animal {\n  +int age\n}\nAnimal <|-- Dog : pet\n')
        self.assertEqual(classes.members['Animal'], ['+int age'])
        self.assertEqual(classes.edges[0][:3], ('Animal', 'Dog', '<|--'))
        self.assertEqual(classes.edges[0][3], 'pet')

    def test_unrecognized_mermaid_falls_back_to_a_source_card(self):
        samples = (
            'pie\n"A" : 1\n',
            'graph TD\nsubgraph one\nA --> B\nend\n',
            'flowchart TD\nA ==> B\n',
            'classDiagram\nclass Foo {\n',
            'stateDiagram-v2\nnote right of Idle: hi\n',
            'sequenceDiagram\nautonumber\n',
        )
        for source in samples:
            with self.subTest(source=source):
                self.assertIsNone(parse_mermaid(source))
                image = render_mermaid(source, PALETTES['dark'])
                self.assertEqual(image.kind, 'source')
                self.assertTrue(image.png.startswith(b'\x89PNG'))
        self.assertEqual(render_mermaid('graph TD\n', PALETTES['dark']).kind, 'flowchart')

    def test_macros_and_environments_prepare_for_mathtext(self):
        rendered = prepare_math(r'\newcommand{\R}{\mathbb{R}} \R')
        self.assertEqual(rendered.strip(), r'\mathbb{R}')
        self.assertNotIn('newcommand', rendered)
        self.assertEqual(
            prepare_math(r'\newcommand{\abs}[1]{\left|#1\right|}\abs{x}'),
            r'\left|x\right|')
        self.assertEqual(prepare_math(r'\newcommand{\R}{A}\renewcommand{\R}{\mathbb{R}}\R'), r'\mathbb{R}')
        symbols = prepare_math(r'\textbf{bold} \textit{i} \texttt{t} \implies \iff \neq \leq \le \geq \ge \to \mapsto')
        self.assertIn(r'\mathbf{bold}', symbols)
        self.assertIn(r'\mathit{i}', symbols)
        self.assertIn(r'\mathtt{t}', symbols)
        self.assertIn(r'\Rightarrow', symbols)
        self.assertIn(r'\Leftrightarrow', symbols)
        self.assertNotIn('implies', symbols)
        self.assertNotIn('textbf', symbols)
        self.assertEqual(symbols.count(r'\leq'), 2)
        self.assertEqual(symbols.count(r'\geq'), 2)
        self.assertIn(r'\to', symbols)
        self.assertIn(r'\mapsto', symbols)
        self.assertEqual(prepare_math(r'\text{hello world}'), r'\text{hello world}')
        aligned = prepare_math(r'\begin{align*} a &= b \\ c &= d \end{align*}')
        self.assertEqual(aligned.mode, 'align')
        self.assertEqual(aligned.rows, [['a', '= b'], ['c', '= d']])
        matrix = prepare_math(r'A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}')
        self.assertEqual(matrix.mode, 'matrix')
        self.assertEqual(matrix.delimiter, ('(', ')'))
        self.assertEqual(matrix.prefix, 'A =')
        self.assertEqual(matrix.rows, [['1', '2'], ['3', '4']])
        cases = prepare_math(r'\begin{cases} a & x>0 \\ b & \text{else} \end{cases}')
        self.assertEqual(cases.delimiter, ('{', ''))
        self.assertEqual(cases.rows[1][1], r'\text{else}')
        self.assertEqual(prepare_math(r'\begin{bmatrix}1&2\end{bmatrix}').delimiter, ('[', ']'))
        self.assertEqual(prepare_math(r'\begin{vmatrix}a\end{vmatrix}').delimiter, ('|', '|'))
        self.assertEqual(prepare_math(r'\begin{gather*}a \\ b\end{gather*}').mode, 'gather')
        self.assertEqual(prepare_math(r'\begin{aligned}a&=b\end{aligned}').mode, 'align')

    def test_math_pngs_cover_environments_and_reject_unknown_commands(self):
        simple = render_math_png(r'a', '#eeeeee')
        samples = (
            r'\mathbb{R}',
            r'\textbf{bold} \implies \text{hello world}',
            r'\newcommand{\R}{\mathbb{R}}\R',
            r'\begin{aligned} a &= b \\ c &= d \\ e &= f \end{aligned}',
            r'\begin{align} a &= b \\ c &= d \end{align}',
            r'\begin{align*} a &= b \end{align*}',
            r'\begin{gather} a \\ b \end{gather}',
            r'\begin{cases} 0 & x<0 \\ 1 & x \ge 0 \end{cases}',
            r'\begin{matrix} a & b \\ c & d \end{matrix}',
            r'\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}',
            r'\begin{bmatrix} 1 & 2 \\ 3 & 4 \end{bmatrix}',
            r'\begin{vmatrix} a & b \\ c & d \end{vmatrix}',
            r'A = \begin{Vmatrix} a \\ b \end{Vmatrix}',
        )
        for expression in samples:
            with self.subTest(expression=expression):
                image = render_math_png(expression, '#eeeeee')
                self.assertTrue(image.startswith(b'\x89PNG'))
        self.assertGreater(
            _png_size(render_math_png(r'\begin{aligned} a &= b \\ c &= d \\ e &= f \end{aligned}', '#eeeeee'))[1],
            _png_size(simple)[1])
        for expression in (r'\unknowncommand{x}', r'\begin{align} a', r'\begin{equation}x\end{equation}', ''):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    render_math_png(expression, '#eeeeee')

    def test_safe_html_and_mermaid_fences_keep_source_offsets(self):
        text = ('Before\n```mermaid\ngraph TD\nA-->B\n```\n'
                'After <u>yes</u> <U>Yes</U> <mark>m</mark> <sub>s</sub> <sup>p</sup> '
                '<kbd>k</kbd><br><br/> <br /> `<u>no</u>` <script>x</script> <b>lit</b> '
                '<u class="x">skip</u>\n')
        layout = markdown_layout(text)
        self.assertEqual(len(layout.diagrams), 1)
        start, end, content = layout.diagrams[0]
        self.assertEqual(content, 'graph TD\nA-->B\n')
        self.assertTrue(text[start:end].startswith('```mermaid'))
        styled = {}
        for span in layout.spans:
            self.assertTrue(0 <= span.start < span.end <= len(text), span)
            styled.setdefault(span.style, []).append(text[span.start:span.end])
        self.assertEqual(styled['underline'], ['yes', 'Yes'])
        self.assertEqual(styled['mark'], ['m'])
        self.assertEqual(styled['subscript'], ['s'])
        self.assertEqual(styled['superscript'], ['p'])
        self.assertEqual(styled['kbd'], ['k'])
        self.assertIn('<u>', styled['syntax'])
        self.assertIn('<br>', styled['syntax'])
        self.assertIn('<br/>', styled['syntax'])
        self.assertIn('<u>no</u>', styled['code'])
        self.assertNotIn('skip', styled['underline'])
        self.assertNotIn('x', [item for item in styled['underline']])

    def test_audio_and_video_attachments_are_embeds_and_pdfs_stay_links(self):
        for name in ('clip.mp3', 'tone.wav', 'a.ogg', 'a.flac', 'a.m4a', 'a.mp4', 'a.webm', 'a.mkv'):
            markdown = attachment_markdown('attachments/' + name)
            self.assertTrue(markdown.startswith('!['), markdown)
            self.assertIn(name, markdown)
        self.assertEqual(attachment_markdown('attachments/report.pdf'),
                         '[report.pdf](attachments/report.pdf)')


@unittest.skipUnless(GTK_AVAILABLE, 'GTK and a display are required')
class DiagramReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.DiagramTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Note\n')
        self.window = Window(self.app, self.root, preferences_path=Path(self.directory.name) / 'settings.json')
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.pane = self.window.current()

    def descendants(self, widget):
        yield widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                yield from self.descendants(child)

    def labels(self):
        return [widget.get_text() for widget in self.descendants(self.pane.reading.body)
                if isinstance(widget, Gtk.Label)]

    def test_reading_renders_diagrams_math_callouts_footnotes_and_html(self):
        from librevault.reading import CALLOUT_STYLES
        callouts = []
        for kind in ('note', 'info', 'tip', 'warning', 'danger', 'example', 'quote', 'todo', 'bug'):
            sign = '-' if kind == 'warning' else '+'
            callouts.append(f'> [!{kind}]{sign} {kind.title()} title\n> Body for {kind}.\n')
        source = (
            '\n'.join(callouts)
            + '\n> [!NOTE] Remember\n> Callout text.\n\n'
            'See[^one] and a side ^[side note].\n\n'
            '[^one]: The definition\n'
            '    continues\n\n'
            'Water H<sub>2</sub>O <u>under</u> <mark>here</mark> <kbd>Ctrl</kbd><br>next '
            '<script>no</script> <b>lit</b>\n\n'
            'Set $\\mathbb{R}$ $\\implies$ $\\textbf{v}$ $\\text{hello world}$.\n\n'
            '$$\n\\begin{align*}\na &= b \\\\\nc &= d\n\\end{align*}\n$$\n\n'
            '```math\n\\begin{cases}\n0 & x < 0 \\\\\n1 & x \\ge 0\n\\end{cases}\n```\n\n'
            '```mermaid\ngraph TD\nA[Start] --> B(Next)\n```\n\n'
            '```mermaid\npie\n"A" : 1\n```\n'
        )
        self.pane.buffer.set_text(source)
        self.pane.set_mode('reading')
        widgets = list(self.descendants(self.pane.reading.body))
        labels = self.labels()
        self.assertEqual(self.pane.text(), source)
        self.assertFalse(any('Could not render' in label for label in labels))
        frames = [widget for widget in widgets if isinstance(widget, Gtk.Frame)]
        for kind in CALLOUT_STYLES:
            self.assertTrue(any(CALLOUT_STYLES[kind] in widget.get_style_context().list_classes()
                                for widget in frames), kind)
            self.assertIn(f'{kind.title()} title', labels)
        expanders = {widget.get_accessible().get_name(): widget
                     for widget in widgets if isinstance(widget, Gtk.Expander)}
        self.assertFalse(expanders['Warning title'].get_expanded())
        self.assertTrue(expanders['Tip title'].get_expanded())
        self.assertTrue(expanders['Remember'].get_expanded())
        self.assertIn('Body for warning.', [label.strip() for label in labels])
        self.assertIn('[one] The definition\ncontinues', labels)
        self.assertIn('[i1] side note', labels)
        reference = next(widget for widget in widgets
                         if isinstance(widget, Gtk.Label) and 'footnote-ref' in widget.get_style_context().list_classes())
        self.assertIn('The definition\ncontinues', reference.get_tooltip_text())
        self.assertIn('side note', reference.get_tooltip_text())
        hrefs = re.findall(r'href="([^"]+)"', reference.get_label())
        footnote_href = next(href for href in hrefs if href.startswith('footnote:'))
        self.assertIn(footnote_href, self.pane.reading.link_targets)
        self.assertTrue(self.pane.reading.activate_link(reference, footnote_href))
        back = next(widget for widget in widgets
                    if isinstance(widget, Gtk.Label) and widget.get_text() == '↩')
        self.assertIn('footnoteback:', back.get_label())
        self.assertEqual(back.get_tooltip_text(), 'Back to reference')
        rich = next(widget for widget in widgets
                    if isinstance(widget, Gtk.Label) and 'under' in widget.get_text())
        markup = rich.get_label()
        self.assertIn('rise="-3500"', markup)
        self.assertIn(f'size="{11 * 1024}"', markup)
        self.assertIn('<u>', markup)
        self.assertIn('background=', markup)
        self.assertIn('monospace', markup)
        self.assertIn('\n', rich.get_text())
        self.assertIn('<script>no</script>', rich.get_text())
        self.assertIn('<b>lit</b>', rich.get_text())
        images = [widget for widget in widgets if isinstance(widget, Gtk.Image)]
        classes = [set(widget.get_style_context().list_classes()) for widget in images]
        self.assertTrue(any('mermaid-flowchart' in item for item in classes))
        self.assertTrue(any('mermaid-source' in item for item in classes))
        self.assertGreaterEqual(sum('mermaid-diagram' not in item for item in classes), 3)

    def test_live_mermaid_preview_and_html_tags_follow_the_cursor(self):
        source = ('```mermaid\ngraph LR\nA[Start] --> B(End)\n```\n\n'
                  '<u>yes</u> <mark>m</mark> <sub>s</sub> <sup>p</sup> <kbd>k</kbd>\n\nTail\n')
        self.pane.buffer.set_text(source)
        self.pane.buffer.place_cursor(self.pane.buffer.get_end_iter())
        self.pane.render()
        self.assertEqual(len(self.pane.diagram_widgets), 1)
        start, end, widget, tag = self.pane.diagram_widgets[0]
        self.assertIn('graph LR', self.pane.diagrams[0][2])
        self.assertTrue(widget.get_visible())
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(tag))
        self.assertEqual(self.pane.text(), source)
        html = '<u>yes</u> <mark>m</mark> <sub>s</sub> <sup>p</sup> <kbd>k</kbd>'
        base = source.index(html)
        for word, style in (('yes', 'underline'), ('m', 'mark'), ('s', 'subscript'), ('p', 'superscript'), ('k', 'kbd')):
            offset = base + html.index(f'>{word}<') + 1
            self.assertTrue(self.pane.buffer.get_iter_at_offset(offset).has_tag(self.pane.tags[style]), style)
        self.assertTrue(self.pane.buffer.get_iter_at_offset(base).has_tag(self.pane.hidden))
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(start + 1))
        self.pane.render()
        self.assertFalse(widget.get_visible())
        self.assertFalse(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.pane.set_mode('source')
        self.assertFalse(widget.get_visible())
        self.assertEqual(self.pane.text(), source)

    def test_audio_and_video_cards_show_metadata_and_controls(self):
        from librevault.attachments import store_attachment
        from librevault.reading import MediaCard
        audio_buffer = BytesIO()
        with wave.open(audio_buffer, 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b'\x00\x00' * 8000)
        audio = store_attachment(self.window.vault, 'tone.wav', audio_buffer.getvalue())
        video = store_attachment(self.window.vault, 'clip.mp4', b'not-a-real-video')
        self.pane.buffer.set_text(f'{attachment_markdown(audio)}\n\n{attachment_markdown(video)}\n')
        self.pane.set_mode('reading')
        cards = [widget for widget in self.descendants(self.pane.reading.body) if isinstance(widget, MediaCard)]
        self.assertEqual(len(cards), 2)
        tone, clip = cards
        self.assertIn('media-audio', tone.get_style_context().list_classes())
        self.assertIn('media-video', clip.get_style_context().list_classes())
        tone_labels = [widget.get_text() for widget in self.descendants(tone) if isinstance(widget, Gtk.Label)]
        clip_labels = [widget.get_text() for widget in self.descendants(clip) if isinstance(widget, Gtk.Label)]
        self.assertIn('tone.wav', tone_labels)
        self.assertTrue(any(label.startswith('Audio · wav ·') and label.endswith('bytes') for label in tone_labels))
        self.assertTrue(any('0:01' in label and '/' in label for label in tone_labels))
        self.assertIn('clip.mp4', clip_labels)
        self.assertTrue(any(label.startswith('Video · mp4 ·') and label.endswith('bytes') for label in clip_labels))
        self.assertTrue(any('--:--' in label for label in clip_labels))
        opened = []
        tone.on_open = lambda: opened.append('audio')
        clip.on_open = lambda: opened.append('video')
        self.assertEqual(tone.play.get_label(), 'Play')
        tone.play.clicked()
        if tone.play.get_label() == 'Pause':
            self.assertEqual(tone.play.get_accessible().get_name(), 'Pause')
            tone.play.clicked()
            self.assertEqual(tone.play.get_label(), 'Play')
        else:
            self.assertIn('audio', opened)
        clip.play.clicked()
        self.assertEqual(opened[-1], 'video')
        external = next(widget for widget in self.descendants(tone)
                        if isinstance(widget, Gtk.Button) and widget.get_label() == 'Open externally')
        external.clicked()
        self.assertIn('audio', opened)
        for card in cards:
            card.shutdown()


if __name__ == '__main__':
    unittest.main()
