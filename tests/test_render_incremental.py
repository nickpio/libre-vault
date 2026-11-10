"""Live-preview caches, widget reuse, and concealment updates without edits."""
from tests.gui_wait import wait_for_work
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from librevault.appearance import PALETTES
from librevault.math_render import render_math_png
from librevault.mermaid import render_mermaid
from librevault.render_plan import Concealment, hidden_changes, reconcile

try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    from librevault.editor_render import EditorRenderMixin, math_pixbuf
    from librevault.folding import FoldState
    from librevault.multicursor import ExtraCursors
    BUFFER_AVAILABLE = True
    from librevault.app import Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    BUFFER_AVAILABLE = GTK_AVAILABLE = False


class ReconcileTests(unittest.TestCase):
    def test_equal_descriptors_pair_in_order_and_the_rest_are_stale(self):
        matched, stale = reconcile([('a', 1), ('b', 2), ('a', 3), ('c', 4)], ['a', 'x', 'a', 'a', 'b'])
        self.assertEqual(matched, [1, None, 3, None, 2])
        self.assertEqual(stale, [4])

    def test_none_descriptors_never_match(self):
        matched, stale = reconcile([(None, 1)], [None])
        self.assertEqual(matched, [None])
        self.assertEqual(stale, [1])


class ConcealmentTests(unittest.TestCase):
    def test_operations_apply_in_order_and_clamp_to_the_text(self):
        plan = Concealment(6)
        plan.hide(-2, 3)
        plan.hide(4, 99)
        plan.reveal(1, 2)
        plan.hide(5, 2)
        self.assertEqual(bytes(plan.hidden), bytes([1, 0, 1, 0, 1, 1]))

    def test_changes_are_minimal_ranges_in_both_directions(self):
        self.assertEqual(hidden_changes(bytes([1, 1, 0, 0, 1]), bytes([0, 1, 1, 0, 1])), ([(2, 3)], [(0, 1)]))
        self.assertEqual(hidden_changes(b'', b''), ([], []))


class RenderCacheTests(unittest.TestCase):
    def test_math_png_is_cached_by_expression_color_and_dpi(self):
        first = render_math_png('a+b', '#eeeeee')
        self.assertIs(render_math_png('a+b', '#eeeeee'), first)
        self.assertIsNot(render_math_png('a+b', '#111111'), first)
        self.assertNotEqual(render_math_png('a+b', '#eeeeee', dpi=75), first)

    def test_mermaid_is_cached_by_source_and_palette(self):
        dark = render_mermaid('graph TD\nCached --> Dark\n', PALETTES['dark'])
        self.assertIs(render_mermaid('graph TD\nCached --> Dark\n', dict(PALETTES['dark'])), dark)
        self.assertNotEqual(render_mermaid('graph TD\nCached --> Dark\n', PALETTES['light']).png, dark.png)

    @unittest.skipUnless(BUFFER_AVAILABLE, 'GTK is unavailable')
    def test_math_pixbuf_only_renders_a_repeated_expression_once(self):
        with patch('matplotlib.mathtext.math_to_image', side_effect=ValueError) as draw:
            self.assertIsNone(math_pixbuf('once-only', '#123456'))
            self.assertIsNone(math_pixbuf('once-only', '#123456'))
        self.assertEqual(draw.call_count, 1)


class FakeWidget:
    def __init__(self, **attributes):
        self.visible = True
        self.destroyed = False
        self.preview_descriptor = None
        self.__dict__.update(attributes)

    def get_visible(self):
        return self.visible

    def set_visible(self, visible):
        self.visible = bool(visible)

    def hide(self):
        self.visible = False

    def has_focus(self):
        return False

    def destroy(self):
        self.destroyed = True


if BUFFER_AVAILABLE:
    class CountingBuffer(Gtk.TextBuffer):
        """Record how much of the buffer each pass touches."""
        touched = 0

        def apply_tag(self, tag, start, end):
            self.touched += end.get_offset() - start.get_offset()
            super().apply_tag(tag, start, end)

        def remove_tag(self, tag, start, end):
            self.touched += end.get_offset() - start.get_offset()
            super().remove_tag(tag, start, end)


    class HeadlessPane(EditorRenderMixin):
        """The real render pipeline on a real buffer, with widgets stubbed out."""

        def __init__(self, text):
            self.window = SimpleNamespace(preferences=SimpleNamespace(theme='dark'), vault=None)
            self.note = SimpleNamespace(path='Note.md')
            self.mode = 'live'
            self.buffer = CountingBuffer()
            self.buffer.set_text(text)
            self.tags = {name: self.buffer.create_tag(None) for name in (
                'strong', 'em', 'code', 'quote', 'link', 'completed', 'metadata', 'highlight',
                'strikethrough', 'comment', 'underline', 'mark', 'subscript', 'superscript',
                'kbd', 'table', 'math', 'callout', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6')}
            self.hidden = self.buffer.create_tag(None, invisible=True)
            self.occurrence_tag = self.buffer.create_tag(None)
            self.list_tags = {}
            self.formatted_text = self.preview_theme = self._diagram_theme = self.fold_regions = None
            self.spans, self.items, self.headings, self.diagrams, self.math_spans, self.callouts = [], [], [], [], [], []
            self.list_widgets, self.image_widgets, self.diagram_widgets = [], [], []
            self.math_widgets, self.embed_widgets, self.table_widgets, self.callout_widgets = [], [], [], []
            self.folds, self.multicursor = FoldState(), ExtraCursors()
            self.reading = SimpleNamespace(refresh=lambda *_: None)
            self._preview_generation = 0
            self._tags_stale, self._tagged_mode = True, None
            self._applied_hidden, self._applied_spacing, self._applied_occurrences = bytearray(), {}, []
            self.built = 0
            self.toggles = []
            self.buffer.connect('changed', self._mark_tags_stale)

        def text(self):
            return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

        def _mark_tags_stale(self, *_):
            self._tags_stale = True

        def _fake(self, **attributes):
            self.built += 1
            return FakeWidget(**attributes)

        def _fake_entry(self, *_):
            return self._fake(), self.buffer.create_tag(None)

        image_widget = diagram_widget = _fake_entry

        def attach_preview(self, widget):
            return widget, self.buffer.create_tag(None)

        def _new_list_widget(self, item):
            return self._fake()

        def math_preview(self, expression, color):
            return self._fake()

        def embed_preview(self, embed):
            return self._fake()

        def table_preview(self, table):
            return self._fake()

        def callout_preview(self, callout):
            return self._fake()

        def _file_signature(self, target):
            return 'unchanged'

        def embed_excerpt(self, target):
            return 'excerpt'

        def toggle_task(self, widget, item, source):
            self.toggles.append((item.task_offset, source))

        def update_list_margins(self):
            pass

        def size_callout_preview(self, widget, tag):
            pass

        def queue_positions(self, *_):
            pass

        def _sync_fold_marks(self, concealed):
            pass

        def _sync_extra_carets(self):
            pass

        def place(self, offset, bound=None):
            start = self.buffer.get_iter_at_offset(offset)
            self.buffer.select_range(start, start if bound is None else self.buffer.get_iter_at_offset(bound))

        def insert(self, offset, text):
            self.buffer.insert(self.buffer.get_iter_at_offset(offset), text)

        def tag_map(self):
            """Every tag on every character, as the buffer actually holds them."""
            names = {tag: name for name, tag in {**self.tags, 'hidden': self.hidden,
                                                 'occurrence': self.occurrence_tag}.items()}
            entries = [*self.image_widgets, *self.diagram_widgets, *self.math_widgets, *self.embed_widgets,
                       *self.table_widgets, *self.callout_widgets]
            names.update({entry[3]: f'spacing{entry[0]}' for entry in entries})
            names.update({tag: f'list{depth}' for depth, tag in self.list_tags.items()})
            result = []
            for offset in range(len(self.text()) + 1):
                tags = self.buffer.get_iter_at_offset(offset).get_tags()
                result.append(frozenset(names.get(tag, 'unknown') for tag in tags))
            return result


    DOCUMENT = ('# Heading **bold**\n\nSome *inline* `code` and $x+y$ math.\n\n'
                '- item one\n- [ ] task\n  - nested\n\n$$\na^2\n$$\n\n'
                '| A | B |\n| --- | --- |\n| 1 | 2 |\n\nBetween\n\n'
                '> [!tip] Callout\n> Body\n\n![[Child]]\n\n'
                '```mermaid\ngraph TD\nA --> B\n```\n\n![pic](pic.png)\n\nThe end ==mark==\n')


@unittest.skipUnless(BUFFER_AVAILABLE, 'GTK is unavailable')
class IncrementalRenderTests(unittest.TestCase):
    def setUp(self):
        self.pane = HeadlessPane(DOCUMENT)
        self.pane.place(0)
        self.pane.render()

    def full_render_map(self):
        self.pane._tags_stale = True
        self.pane.render()
        return self.pane.tag_map()

    def assert_matches_full_render(self):
        incremental = self.pane.tag_map()
        self.assertEqual(incremental, self.full_render_map())

    def test_every_cursor_position_matches_a_full_render(self):
        for offset in range(len(DOCUMENT) + 1):
            self.pane.place(offset)
            self.pane.render()
            with self.subTest(offset=offset):
                self.assert_matches_full_render()

    def test_selections_match_a_full_render(self):
        for start, end in ((0, 40), (30, 75), (70, len(DOCUMENT)), (5, 6), (60, 110)):
            self.pane.place(start, end)
            self.pane.render()
            with self.subTest(selection=(start, end)):
                self.assert_matches_full_render()

    def test_cursor_move_touches_only_the_affected_ranges(self):
        pane = self.pane
        first = DOCUMENT.index('Some')
        pane.place(first)
        pane.render()
        pane.buffer.touched = 0
        pane.place(DOCUMENT.index('The end'))
        pane.render()
        self.assertGreater(pane.buffer.touched, 0)
        self.assertLess(pane.buffer.touched, 12)

    def test_repeating_a_pass_without_changes_touches_nothing(self):
        self.pane.buffer.touched = 0
        self.pane.render()
        self.assertEqual(self.pane.buffer.touched, 0)

    def test_text_returning_to_its_formatted_value_still_retags(self):
        pane = self.pane
        offset = DOCUMENT.index('bold')
        self.assertIn('strong', pane.tag_map()[offset])
        pane.buffer.set_text(DOCUMENT)
        pane.place(0)
        self.assertEqual(pane.text(), pane.formatted_text)
        pane.render()
        self.assertIn('strong', pane.tag_map()[offset])
        self.assert_matches_full_render()

    def test_fold_and_mode_changes_without_edits_match_a_full_render(self):
        pane = self.pane
        pane.folds.anchors.add(0)
        pane.place(len(DOCUMENT))
        pane.render()
        self.assert_matches_full_render()
        pane.folds.anchors.clear()
        pane.render()
        self.assert_matches_full_render()
        pane.mode = 'source'
        pane.render()
        self.assertFalse(any(pane.tag_map()))
        pane.mode = 'live'
        pane.render()
        self.assert_matches_full_render()

    def test_occurrences_follow_multicursor_ranges_without_edits(self):
        pane = self.pane
        pane.multicursor.ranges = [(0, 3)]
        pane.render()
        self.assertIn('occurrence', pane.tag_map()[1])
        pane.multicursor.ranges = []
        pane.render()
        self.assertNotIn('occurrence', pane.tag_map()[1])

    def widget_list(self):
        pane = self.pane
        return [*(entry[1] for entry in pane.list_widgets),
                *(entry[2] for entries in (pane.image_widgets, pane.diagram_widgets, pane.math_widgets,
                                           pane.embed_widgets, pane.table_widgets, pane.callout_widgets)
                  for entry in entries)]

    def test_edit_above_widgets_reuses_them_at_shifted_offsets(self):
        pane = self.pane
        before = self.widget_list()
        table_start = pane.table_widgets[0][0]
        anchor = pane.table_widgets[0][2].source_anchor
        self.assertEqual(len(before), 9)
        built = pane.built
        pane.insert(0, 'typed ')
        pane.render()
        after = self.widget_list()
        self.assertEqual(pane.built, built)
        self.assertEqual([id(widget) for widget in after], [id(widget) for widget in before])
        self.assertFalse(any(widget.destroyed for widget in after))
        self.assertEqual(pane.table_widgets[0][0], table_start + 6)
        self.assertEqual(pane.table_widgets[0][2].table_start, table_start + 6)
        self.assertEqual(pane.table_widgets[0][2].source_anchor, anchor + 6)

    def test_changed_block_is_the_only_widget_replaced(self):
        pane = self.pane
        before = pane.table_widgets[0][2]
        built = pane.built
        pane.insert(pane.table_widgets[0][1] - 3, 'x')
        pane.render()
        self.assertEqual(pane.built, built + 1)
        self.assertTrue(before.destroyed)
        self.assertIsNot(pane.table_widgets[0][2], before)

    def test_preference_change_replaces_every_widget(self):
        pane = self.pane
        before = self.widget_list()
        pane._preview_generation += 1
        pane.preview_theme = None
        pane.render()
        self.assertTrue(all(widget.destroyed for widget in before))
        self.assertFalse(any(widget.destroyed for widget in self.widget_list()))

    def test_theme_change_replaces_every_widget(self):
        before = self.widget_list()
        self.pane.window.preferences.theme = 'light'
        self.pane.render()
        self.assertTrue(all(widget.destroyed for widget in before))

    def test_reused_task_toggles_against_its_current_offset_and_source(self):
        pane = self.pane
        task = next(widget for item, widget in pane.list_widgets if item.task_offset is not None)
        offset = next(item.task_offset for item, _ in pane.list_widgets if item.task_offset is not None)
        pane.insert(0, 'typed ')
        pane.render()
        pane._task_toggled(task)
        self.assertEqual(pane.toggles, [(offset + 6, pane.formatted_text)])

    def test_toggled_task_is_never_reused_even_when_the_edit_is_declined(self):
        pane = self.pane
        task = next(widget for item, widget in pane.list_widgets if item.task_offset is not None)
        pane._task_toggled(task)
        pane.insert(0, 'typed ')
        pane.render()
        self.assertTrue(task.destroyed)

    def test_callouts_showing_other_notes_are_rebuilt(self):
        self.assertIsNone(self.pane._callout_descriptor('> [!note] A\n> ![[Other]]\n'))
        self.assertIsNone(self.pane._callout_descriptor('> [!note] A\n> Text[^1]\n'))
        self.assertIsNotNone(self.pane._callout_descriptor('> [!note] A\n> Text\n'))

    def test_embed_descriptor_tracks_other_note_content(self):
        pane = self.pane
        before = pane.embed_widgets[0][2]
        with patch.object(HeadlessPane, 'embed_excerpt', return_value='changed elsewhere'):
            pane.insert(0, 'x')
            pane.render()
        self.assertTrue(before.destroyed)
        self.assertIsNot(pane.embed_widgets[0][2], before)

    def test_changed_file_replaces_its_image(self):
        pane = self.pane
        before = pane.image_widgets[0][2]
        with patch.object(HeadlessPane, '_file_signature', return_value='rewritten'):
            pane.insert(0, 'x')
            pane.render()
        self.assertTrue(before.destroyed)


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display is unavailable')
class LiveWidgetReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application(application_id='test.librevault.renderincremental',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / 'vault'
        root.mkdir()
        self.source = ('Intro\n\n- [ ] Task\n- Plain\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n'
                       '> [!tip] Callout\n> Body\n\nAfter\n')
        (root / 'Note.md').write_text(self.source, encoding='utf-8')
        self.window = Window(self.app, root, preferences_path=Path(directory.name) / 'settings.json')
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.window.open_note('Note.md')
        self.pane = self.window.current()
        self.pane.render()

    def test_edit_above_keeps_widgets_and_controls_target_the_new_offsets(self):
        pane = self.pane
        table, callout = pane.table_widgets[0][2], pane.callout_widgets[0][2]
        task = next(widget for item, widget in pane.list_widgets if item.task_offset is not None)
        pane.buffer.insert(pane.buffer.get_start_iter(), 'New line\n')
        pane.render()
        self.assertIs(pane.table_widgets[0][2], table)
        self.assertIs(pane.callout_widgets[0][2], callout)
        self.assertIs(next(widget for item, widget in pane.list_widgets if item.task_offset is not None), task)
        self.assertEqual(table.table_start, pane.table_widgets[0][0])
        self.assertEqual(table.source_anchor, pane.text().rfind('\n', *pane.table_widgets[0][:2]))
        task.set_active(True)
        self.assertEqual(pane.text(), 'New line\n' + self.source.replace('- [ ] Task', '- [x] Task'))

    def test_cursor_move_reveals_and_conceals_a_reused_table(self):
        pane = self.pane
        start, end, widget, tag = pane.table_widgets[0]
        pane.buffer.place_cursor(pane.buffer.get_end_iter())
        pane.render()
        self.assertTrue(widget.get_visible())
        self.assertTrue(pane.buffer.get_iter_at_offset(start).has_tag(pane.hidden))
        self.assertTrue(pane.buffer.get_iter_at_offset(start).has_tag(tag))
        pane.buffer.place_cursor(pane.buffer.get_iter_at_offset(start + 1))
        pane.render()
        self.assertFalse(widget.get_visible())
        self.assertFalse(pane.buffer.get_iter_at_offset(start).has_tag(pane.hidden))
        self.assertFalse(pane.buffer.get_iter_at_offset(start).has_tag(tag))
