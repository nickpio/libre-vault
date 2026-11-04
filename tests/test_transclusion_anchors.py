"""Heading sections, block ids, transclusion, completion, and link previews."""
from tests.gui_wait import wait_for_work
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest

from librevault.anchors import (
    block_id_insertion, extract_subpath, find_anchor_offset, heading_links,
    list_block_ids, list_headings, normalize_anchor, parse_image_size,
    reference_blocks, split_transclusion_target,
)
from librevault.formatting import markdown_layout
from librevault.vault import find_anchor_offset as vault_find_anchor_offset


NOTE = """---
# Fake
---

```python
# Also fake
^inside
```

Title with **emphasis**
=======================

  ## Second `heading`

> ### Quoted

# Parent

intro

## Child

child body

### Deep

deep

## Other

other

# Next

next

# **Bold** `code` [[Other|Alias]]

# a.b

# a_b

# it's

# Hello\u00a0World

Intro mentions Beta

# Beta

beta body

# A # heading

hash body

# Parent

## Note

note one

# Other

## Note

note two
"""

BLOCKS = """See alpha early.

Alpha line ^alpha

Paragraph one.
^pid

- item ^item
- beta
  nested

> quoted line

^qid

> one
> two
^lazy

```js
code
^inside
```
^cid

| h |
| --- |
| c |
^tid

[^footnote]: not a block id

Real mention stays above.

See beta early.

Real ^beta
"""


class AnchorTests(unittest.TestCase):
    def test_vault_reexports_anchor_resolution(self):
        self.assertIs(vault_find_anchor_offset, find_anchor_offset)

    def test_headings_ignore_frontmatter_and_fences_and_keep_offsets(self):
        headings = list_headings(NOTE)
        labels = [(level, text) for level, text, _offset in headings]
        self.assertEqual(labels[:4], [
            (1, 'Title with emphasis'),
            (2, 'Second heading'),
            (3, 'Quoted'),
            (1, 'Parent'),
        ])
        self.assertEqual(headings[0][2], NOTE.index('Title with **emphasis**'))
        self.assertEqual(headings[1][2], NOTE.index('  ## Second'))
        self.assertEqual(headings[2][2], NOTE.index('> ### Quoted'))
        self.assertNotIn('Fake', [text for _, text, _ in headings])
        self.assertNotIn('Also fake', [text for _, text, _ in headings])
        visible = next(text for _, text, _ in headings if text.startswith('Bold'))
        self.assertEqual(visible, 'Bold code Alias')

    def test_heading_match_folds_case_whitespace_and_punctuation(self):
        self.assertEqual(normalize_anchor('a.b'), 'a b')
        self.assertEqual(normalize_anchor('a_b'), 'a_b')
        self.assertEqual(normalize_anchor("it's"), "it's")
        self.assertEqual(find_anchor_offset(NOTE, 'a b'), NOTE.index('# a.b'))
        self.assertEqual(find_anchor_offset(NOTE, 'a_b'), NOTE.index('# a_b'))
        self.assertIsNone(find_anchor_offset(NOTE, 'its'))
        self.assertEqual(find_anchor_offset(NOTE, "IT'S"), NOTE.index("# it's"))
        self.assertEqual(find_anchor_offset(NOTE, 'hello world'), NOTE.index('# Hello'))
        self.assertEqual(find_anchor_offset(NOTE, 'Bold code Alias'), NOTE.index('# **Bold**'))

    def test_sections_stop_at_the_next_heading_of_the_same_or_higher_level(self):
        parent = extract_subpath(NOTE, 'Parent')
        self.assertIn('intro', parent)
        self.assertIn('## Child', parent)
        self.assertIn('child body', parent)
        self.assertIn('## Other', parent)
        self.assertNotIn('# Next', parent)
        self.assertNotIn('\nnext', parent)
        child = extract_subpath(NOTE, 'Parent#Child')
        self.assertTrue(child.startswith('## Child'))
        self.assertIn('child body', child)
        self.assertIn('### Deep', child)
        self.assertIn('deep', child)
        self.assertNotIn('## Other', child)
        self.assertNotIn('other', child)
        nxt = extract_subpath(NOTE, 'Next')
        self.assertIn('next', nxt)
        self.assertNotIn('Parent', nxt)

    def test_duplicate_headings_use_the_first_match_unless_the_path_differs(self):
        first = extract_subpath(NOTE, 'Note')
        self.assertIn('note one', first)
        self.assertNotIn('note two', first)
        second = extract_subpath(NOTE, 'Other#Note')
        self.assertIn('note two', second)
        self.assertNotIn('note one', second)
        self.assertEqual(find_anchor_offset(NOTE, 'Note'), NOTE.index('## Note'))
        self.assertEqual(find_anchor_offset(NOTE, 'Other#Note'), NOTE.rindex('## Note'))
        links = [fragment for fragment, _level, _offset in heading_links(NOTE) if fragment.endswith('Note')]
        self.assertEqual(links, ['Parent#Note', 'Other#Note'])

    def test_hash_inside_a_heading_matches_the_whole_fragment(self):
        self.assertEqual(find_anchor_offset(NOTE, 'A # heading'), NOTE.index('# A # heading'))
        section = extract_subpath(NOTE, 'A # heading')
        self.assertIn('hash body', section)
        self.assertNotIn('note one', section)
        self.assertEqual(find_anchor_offset(NOTE, 'Beta'), NOTE.index('# Beta'))
        self.assertNotIn('Intro mentions', extract_subpath(NOTE, 'Beta'))

    def test_block_ids_attach_to_the_block_and_ignore_code_and_footnotes(self):
        found = {block_id: (snippet, offset) for block_id, snippet, offset in list_block_ids(BLOCKS)}
        for block_id in ('alpha', 'pid', 'item', 'qid', 'lazy', 'cid', 'tid', 'beta'):
            self.assertIn(block_id, found)
        self.assertNotIn('inside', found)
        self.assertNotIn('footnote', found)
        self.assertEqual(found['beta'][1], BLOCKS.index('Real ^beta'))
        self.assertNotEqual(found['alpha'][1], found['beta'][1])
        crlf = 'See beta early.\r\n\r\nReal ^beta\r\n'
        self.assertEqual(find_anchor_offset(crlf, '^beta'), crlf.index('Real'))

        alpha = extract_subpath(BLOCKS, '^alpha')
        self.assertIn('Alpha line', alpha)
        self.assertNotIn('^alpha', alpha)
        self.assertNotIn('Paragraph one', alpha)
        paragraph = extract_subpath(BLOCKS, '^pid')
        self.assertIn('Paragraph one', paragraph)
        self.assertNotIn('^pid', paragraph)
        item = extract_subpath(BLOCKS, '^item')
        self.assertIn('item', item)
        self.assertNotIn('nested', item)
        self.assertNotIn('^item', item)
        quote = extract_subpath(BLOCKS, '^qid')
        self.assertIn('quoted line', quote)
        self.assertNotIn('^qid', quote)
        lazy = extract_subpath(BLOCKS, '^lazy')
        self.assertIn('one', lazy)
        self.assertIn('two', lazy)
        self.assertNotIn('^lazy', lazy)
        fence = extract_subpath(BLOCKS, '^cid')
        self.assertIn('code', fence)
        self.assertIn('```', fence)
        self.assertIn('^inside', fence)
        self.assertNotIn('^cid', fence)
        table = extract_subpath(BLOCKS, '^tid')
        self.assertIn('| c |', table)
        self.assertNotIn('^tid', table)
        self.assertIsNone(extract_subpath(BLOCKS, '^missing'))
        self.assertIsNone(find_anchor_offset(BLOCKS, '^'))

    def test_image_size_suffixes_are_not_aliases(self):
        self.assertEqual(parse_image_size('300'), (300, None))
        self.assertEqual(parse_image_size('300x200'), (300, 200))
        self.assertEqual(parse_image_size('99999'), (2000, None))
        self.assertIsNone(parse_image_size('0'))
        self.assertIsNone(parse_image_size('Alias'))
        self.assertEqual(split_transclusion_target('image.png|300x200'), ('image.png', '', 300, 200))
        self.assertEqual(split_transclusion_target('image.png|300'), ('image.png', '', 300, None))
        self.assertEqual(split_transclusion_target('Note#Heading|Alias'), ('Note', 'Heading', None, None))
        self.assertEqual(split_transclusion_target('Note#^block'), ('Note', '^block', None, None))
        self.assertEqual(split_transclusion_target('Note%20name#My%20heading'), ('Note name', 'My heading', None, None))

    def test_generated_block_ids_round_trip_for_each_block_kind(self):
        samples = {
            'paragraph': 'Lonely paragraph\n',
            'list_item': '- alpha\n',
            'fence': '```js\ncode\n```\n',
            'table': '| h |\n| --- |\n| c |\n',
        }
        for kind, text in samples.items():
            matches = [block for block in reference_blocks(text) if block[4] == kind]
            self.assertEqual(len(matches), 1, kind)
            _block_id, _snippet, start, end, found_kind = matches[0]
            block_id, at, insertion = block_id_insertion(text, start, end, found_kind)
            self.assertRegex(block_id, r'^[A-Za-z0-9-]+$')
            self.assertGreaterEqual(at, 0)
            updated = text[:at] + insertion + text[at:]
            extracted = extract_subpath(updated, '^' + block_id)
            self.assertIsNotNone(extracted, kind)
            self.assertNotIn('^' + block_id, extracted)
            again = next(block for block in reference_blocks(updated) if block[0] == block_id)
            self.assertEqual(block_id_insertion(updated, again[2], again[3], again[4])[:2], (block_id, -1))

    def test_layout_records_image_sizes_and_skips_note_embeds(self):
        sized = markdown_layout('![alt|300x200](image.png)\n')
        self.assertEqual(sized.images, [(0, len('![alt|300x200](image.png)'), 'image.png', 300, 200)])
        wiki = markdown_layout('![[photo.png|80]]\n')
        self.assertEqual(wiki.images[0][2:], ('photo.png', 80, None))
        self.assertEqual(markdown_layout('![[Note#Heading]]\n').images, [])
        self.assertEqual(markdown_layout('![[Note#^block]]\n').images, [])


try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import GdkPixbuf, Gio
    from librevault.attachments import store_attachment
    from librevault.navigation_ui import note_link_from_uri
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK 3 and a display are required')
class TransclusionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.TransclusionTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'A.md').write_text('# Host\n')
        self.window = Window(self.app, str(self.root))
        wait_for_work(self.window)
        self.addCleanup(self.window.destroy)
        self.pane = self.window.current()
        self.drain_events()

    def drain_events(self):
        context = GLib.MainContext.default()
        for _ in range(1000):
            if not context.pending():
                return
            context.iteration(False)
        self.fail('GTK did not become idle')

    def descendants(self, widget):
        yield widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                yield from self.descendants(child)

    def labels(self, widget):
        return [child.get_text() for child in self.descendants(widget) if isinstance(child, Gtk.Label)]

    def frame_named(self, name):
        return next(child for child in self.descendants(self.pane.reading.body)
                    if isinstance(child, Gtk.Frame) and child.get_label() == name)

    def show_reading(self, source):
        self.pane.buffer.set_text(source)
        self.pane.set_mode('reading')
        self.drain_events()

    def complete(self, source):
        self.pane.buffer.set_text(source)
        self.pane.buffer.place_cursor(self.pane.buffer.get_end_iter())
        self.drain_events()

    def test_reading_transcludes_a_heading_or_block_and_reports_a_missing_anchor(self):
        (self.root / 'Child.md').write_text(
            '# Keep\n\nkeep me\n\n# Later\n\nlater body\n\nAlpha mention of beta\n\nParagraph two ^beta\n')
        self.window.vault.refresh()
        self.show_reading('![[Child#Keep]]\n\n![[Child#Missing]]\n\n![[Child#^beta]]\n')
        kept = self.frame_named('Child.md#Keep')
        kept_labels = self.labels(kept)
        self.assertTrue(any('keep me' in label for label in kept_labels))
        self.assertFalse(any('later body' in label for label in kept_labels))
        missing = self.frame_named('Child.md#Missing')
        self.assertTrue(any(label == 'Heading or block not found: #Missing' for label in self.labels(missing)))
        block = self.frame_named('Child.md#^beta')
        block_labels = self.labels(block)
        self.assertTrue(any(label.strip() == 'Paragraph two' for label in block_labels))
        self.assertFalse(any('Alpha mention' in label for label in block_labels))

    def test_same_note_section_embed_cycles_only_when_the_section_embeds_itself(self):
        self.show_reading('# Keep\n\nkeep me\n\n# Other\n\n![[#Keep]]\n')
        frame = self.frame_named('A.md#Keep')
        frame_labels = self.labels(frame)
        self.assertTrue(any('keep me' in label for label in frame_labels))
        self.assertFalse(any('cycle' in label for label in frame_labels))
        self.assertFalse(any(label == 'Other' for label in frame_labels))
        self.show_reading('# Keep\n\nkeep me\n\n![[#Keep]]\n')
        labels = self.labels(self.pane.reading.body)
        self.assertTrue(any('cycle' in label for label in labels))
        self.assertFalse(any('Preview limit reached' in label for label in labels))

    def test_image_dimensions_apply_in_reading_and_live_preview(self):
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 40, 20)
        image.fill(0xff0000ff)
        _success, data = image.save_to_bufferv('png', [], [])
        relative = store_attachment(self.window.vault, 'wide.png', data)
        source = f'![[{relative}|10]]\n\n![alt|30x10]({relative})\n'
        self.show_reading(source)
        sizes = sorted((widget.get_pixbuf().get_width(), widget.get_pixbuf().get_height())
                       for widget in self.descendants(self.pane.reading.body) if isinstance(widget, Gtk.Image))
        self.assertEqual(sizes, [(10, 5), (30, 10)])
        self.pane.set_mode('live')
        self.pane.buffer.place_cursor(self.pane.buffer.get_end_iter())
        self.pane.render()
        self.drain_events()
        live = sorted((widget.get_pixbuf().get_width(), widget.get_pixbuf().get_height())
                      for _start, _end, widget, _tag in self.pane.image_widgets)
        self.assertEqual(live, [(10, 5), (30, 10)])
        self.assertEqual(self.pane.text(), source)

    def test_open_note_lands_on_the_heading_or_block_not_an_earlier_mention(self):
        text = 'Intro mentions Gamma\n\nonly-in-prose sits here\n\n# Gamma\n\nbody\n\nReal ^beta\n'
        (self.root / 'Target.md').write_text(text)
        self.window.vault.refresh()
        pane = self.window.open_note('Target.md', 'Gamma')
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        self.assertEqual(cursor, text.index('# Gamma'))
        self.window.follow_link('A.md', 'Target.md#^beta')
        pane = self.window.current()
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        self.assertEqual(cursor, text.index('Real ^beta'))
        pane.buffer.place_cursor(pane.buffer.get_end_iter())
        end = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        self.window.open_note('Target.md', '^')
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        self.assertEqual(cursor, end)
        self.window.open_note('Target.md', 'only-in-prose')
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        self.assertEqual(cursor, text.index('only-in-prose sits here'))

    def test_heading_and_block_completion_insert_the_anchor(self):
        (self.root / 'Other.md').write_text('# Alpha\n\nalpha\n\n# Beta\n\nbeta\n')
        self.window.vault.refresh()
        self.complete('[[Other#')
        self.assertEqual(self.pane.navigation.paths, [])
        self.assertEqual([choice.insert for choice in self.pane.navigation.choices], ['Other#Alpha', 'Other#Beta'])
        self.complete('[[Other#Be')
        self.assertEqual([choice.insert for choice in self.pane.navigation.choices], ['Other#Beta'])
        self.pane.navigation.accept()
        self.assertEqual(self.pane.text(), '[[Other#Beta]]')

        self.complete('# Parent\n\n## Note\n\none\n\n# Other\n\n## Note\n\ntwo\n\n[[#Note')
        self.assertEqual([choice.insert for choice in self.pane.navigation.choices],
                         ['#Parent#Note', '#Other#Note'])
        self.pane.navigation.rows.select_row(self.pane.navigation.rows.get_row_at_index(1))
        self.pane.navigation.accept()
        self.assertTrue(self.pane.text().endswith('[[#Other#Note]]'))

        self.complete('Paragraph one. ^pid\n\n[[#^')
        self.assertEqual([choice.insert for choice in self.pane.navigation.choices], ['#^pid'])
        self.pane.navigation.accept()
        self.assertEqual(self.pane.text(), 'Paragraph one. ^pid\n\n[[#^pid]]')

    def test_generated_block_id_is_one_undo_and_a_closed_note_is_saved(self):
        original = 'Lonely paragraph\n\n[[#^'
        self.complete(original)
        self.assertEqual([choice.kind for choice in self.pane.navigation.choices], ['block-new'])
        self.pane.navigation.accept()
        text = self.pane.text()
        ids = re.findall(r'\^([A-Za-z0-9-]+)', text)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(text, f'Lonely paragraph ^{ids[0]}\n\n[[#^{ids[0]}]]')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)

        (self.root / 'Zed.md').write_text('Saved paragraph\n')
        self.window.vault.refresh()
        self.assertNotIn('Zed.md', self.window.panes)
        self.complete('[[Zed#^')
        self.assertEqual(self.pane.navigation.choices[0].kind, 'block-new')
        self.pane.navigation.accept()
        saved = (self.root / 'Zed.md').read_text()
        saved_ids = re.findall(r'\^([A-Za-z0-9-]+)', saved)
        self.assertEqual(len(saved_ids), 1)
        self.assertIn(f'[[Zed#^{saved_ids[0]}]]', self.pane.text())
        self.assertTrue(saved.startswith(f'Saved paragraph ^{saved_ids[0]}'))

    def test_hover_preview_shows_the_targeted_section_immediately_with_ctrl(self):
        (self.root / 'Child.md').write_text('# Keep\n\nkeep me\n\n# Later\n\nlater body\n')
        self.window.vault.refresh()
        from urllib.parse import quote
        self.assertEqual(note_link_from_uri('note:' + quote('A.md') + '|' + quote('Child#Keep')),
                         ('A.md', 'Child#Keep'))
        self.assertIsNone(note_link_from_uri('https://example.com'))
        preview = self.pane.navigation.link_preview
        rectangle = Gdk.Rectangle()
        rectangle.x = rectangle.y = 0
        rectangle.width = rectangle.height = 8
        preview.show(self.pane.editor, 'A.md', 'Child#Keep', rectangle, self.window)
        self.assertIn('Keep', preview.title_label.get_text())
        self.assertIn('keep me', preview.body_label.get_text())
        self.assertNotIn('later body', preview.body_label.get_text())
        preview.hide()
        preview.consider(self.pane.editor, 'A.md', 'Child#^missing', False, rectangle, self.window)
        self.assertIsNone(preview.popover)
        self.assertIsNotNone(preview.timer)
        preview.consider(self.pane.editor, 'A.md', 'Child#^missing', True, rectangle, self.window)
        self.assertIsNotNone(preview.popover)
        self.assertIsNone(preview.timer)
        self.assertIn('Heading or block not found', preview.body_label.get_text())
        preview.hide()
