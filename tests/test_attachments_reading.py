"""Attachment bytes, native reading widgets and source fidelity."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from librevault.attachments import attachment_markdown, local_path, store_attachment
from librevault.reading import reading_source
from librevault.vault import Vault, VaultError

try:
    from librevault.app import Gdk, GLib, Gtk, Window
    from gi.repository import GdkPixbuf, Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


class AttachmentStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'Note.md').write_text('# Note')
        self.vault = Vault(self.root)

    def test_collision_preserves_bytes_and_markdown_encodes_spaces(self):
        first = store_attachment(self.vault, 'Image one.png', b'original')
        second = store_attachment(self.vault, 'Image one.png', b'other')
        self.assertEqual((self.root / first).read_bytes(), b'original')
        self.assertEqual((self.root / second).read_bytes(), b'other')
        self.assertEqual(second, 'attachments/Image one 1.png')
        self.assertEqual(attachment_markdown(first), '![Image one.png](attachments/Image%20one.png)')
        self.assertEqual(attachment_markdown('attachments/report.pdf'), '[report.pdf](attachments/report.pdf)')

    def test_identical_attachments_are_reused_without_overwriting_distinct_files(self):
        first = store_attachment(self.vault, 'image.png', b'first')
        second = store_attachment(self.vault, 'image.png', b'other')
        self.assertEqual(store_attachment(self.vault, 'image.png', b'other'), second)
        self.assertEqual(store_attachment(self.vault, 'renamed.PNG', b'first'), first)
        self.assertEqual(len(list((self.root / 'attachments').iterdir())), 2)
        self.assertNotEqual(store_attachment(self.vault, 'image.txt', b'first'), first)

    def test_duplicate_check_does_not_follow_symlinks(self):
        directory = self.root / 'attachments'
        directory.mkdir()
        with TemporaryDirectory() as outside:
            external = Path(outside) / 'external.png'
            external.write_bytes(b'image')
            (directory / 'alias.png').symlink_to(external)
            stored = store_attachment(self.vault, 'new.png', b'image')
            self.assertEqual(stored, 'attachments/new.png')

    def test_local_assets_refuse_remote_escape_and_symlinks(self):
        relative = store_attachment(self.vault, 'file.png', b'image')
        (self.root / 'Folder').mkdir()
        self.assertEqual(local_path(self.vault, 'Folder/Note.md', '../' + relative), self.root / relative)
        for target in ('https://example.com/a.png', '../../outside.png', '/etc/passwd'):
            with self.assertRaises(VaultError):
                local_path(self.vault, 'Note.md', target)
        (self.root / 'evil.png').symlink_to(self.root / relative)
        with self.assertRaises(VaultError):
            local_path(self.vault, 'Note.md', 'evil.png')

    def test_attachment_directory_cannot_be_a_symlink(self):
        with TemporaryDirectory() as outside:
            (self.root / 'attachments').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(VaultError):
                store_attachment(self.vault, 'file.png', b'image')
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_footnote_continuations_stop_at_next_block(self):
        text = 'Body[^one]\n\n[^one]: First\n    continuation\n\nOther\n\n    code\n'
        body, notes = reading_source(text)
        self.assertEqual(notes, [('one', 'First\ncontinuation')])
        self.assertIn('    code', body)


@unittest.skipUnless(GTK_AVAILABLE, 'GTK and a display are required')
class ReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application.get_default()
        if cls.app is None:
            cls.app = Gtk.Application(application_id='org.librevault.ReadingTests',
                                      flags=Gio.ApplicationFlags.NON_UNIQUE)
            cls.app.register(None)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'vault'
        self.root.mkdir()
        (self.root / 'Note.md').write_text('# Note\n\n**bold**\n')
        self.window = Window(self.app, self.root, preferences_path=Path(self.directory.name) / 'settings.json')
        self.addCleanup(self.window.destroy)
        self.pane = self.window.current()

    def descendants(self, widget):
        yield widget
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                yield from self.descendants(child)

    def test_attachment_insert_and_undo_preserve_source_and_copied_bytes(self):
        original = self.pane.text()
        relative = self.pane.insert_attachment_bytes('a b.pdf', b'%PDF-fixture')
        self.assertIn('[a b.pdf](attachments/a%20b.pdf)', self.pane.text())
        self.assertEqual((self.root / relative).read_bytes(), b'%PDF-fixture')
        self.pane.undo()
        self.assertEqual(self.pane.text(), original)
        self.assertTrue((self.root / relative).exists())

    def test_removing_and_reinserting_attachment_reuses_stored_file(self):
        first = self.pane.insert_attachment_bytes('image.png', b'image')
        self.pane.buffer.set_text('')
        second = self.pane.insert_attachment_bytes('image.png', b'image')
        self.assertEqual(second, first)
        self.assertEqual(self.pane.text(), '![image.png](attachments/image.png)')
        self.assertEqual([path.name for path in (self.root / 'attachments').iterdir()], ['image.png'])

    def test_nested_note_attachment_markup_cannot_resolve_to_shadowing_local_asset(self):
        (self.root / 'Folder' / 'attachments').mkdir(parents=True)
        (self.root / 'Folder' / 'attachments' / 'image.png').write_bytes(b'shadow')
        (self.root / 'Folder' / 'Nested.md').write_text('Nested\n')
        self.window.vault.refresh()
        self.window.open_note('Folder/Nested.md')
        pane = self.window.current()
        relative = pane.insert_attachment_bytes('image.png', b'imported')
        self.assertIn('(../attachments/image.png)', pane.text())
        self.assertEqual(local_path(self.window.vault, pane.note.path, '../attachments/image.png').read_bytes(), b'imported')
        self.assertEqual(relative, 'attachments/image.png')

    def test_source_reading_and_live_modes_keep_source_and_history(self):
        original = self.pane.text()
        self.pane.set_mode('source')
        self.assertEqual(self.pane.content_stack.get_visible_child_name(), 'editor')
        self.assertEqual(self.pane.buffer.get_text(*self.pane.buffer.get_bounds(), False), original)
        self.pane.set_mode('reading')
        self.assertEqual(self.pane.content_stack.get_visible_child_name(), 'reading')
        self.pane.set_mode('live')
        self.assertEqual(self.pane.text(), original)
        self.assertFalse(self.pane.dirty)
        self.assertFalse(self.pane.history.can_undo)

    def test_view_toolbar_is_above_tabs_and_follows_the_selected_note(self):
        self.pane.set_mode('source')
        second = self.window.vault.create('Second')
        self.window.open_note(second.path)
        other = self.window.current()
        self.assertIs(self.window.note_toolbars.get_visible_child(), other.toolbar)
        other.mode_selector.set_active_id('reading')
        self.assertEqual(other.mode, 'reading')
        self.window.open_note(self.pane.note.path)
        self.assertIs(self.window.note_toolbars.get_visible_child(), self.pane.toolbar)
        self.assertEqual(self.pane.mode_selector.get_active_id(), 'source')
        loop = GLib.MainLoop()
        GLib.timeout_add(100, lambda: (loop.quit(), False)[1])
        loop.run()
        bar = self.window.note_toolbars
        self.assertIs(bar.get_parent(), self.window.tabs.get_parent())
        self.assertLess(bar.get_allocation().y, self.window.tabs.get_allocation().y)
        self.window.close_note(self.pane)
        self.assertIs(bar.get_visible_child(), other.toolbar)
        self.window.close_note(other)
        self.assertFalse(bar.get_visible())

    def test_tables_callouts_footnotes_math_and_note_embeds_render(self):
        (self.root / 'Child.md').write_text('## Embedded heading\n\nEmbedded body.\n![[Note]]')
        self.window.vault.refresh()
        source = ('# Main\n\n| Name | Value |\n| --- | --- |\n| One | Two |\n\n'
                  '> [!NOTE] Remember\n> Callout text.\n\n'
                  'Footnote[^one]\n\n[^one]: Footnote body\n\n'
                  '**bold $x^2$ text**\n\n$$\n\\frac{a}{b}\n$$\n\n![[Child]]\n')
        self.pane.buffer.set_text(source)
        self.pane.set_mode('reading')
        widgets = list(self.descendants(self.pane.reading.body))
        labels = [widget.get_text() for widget in widgets if isinstance(widget, Gtk.Label)]
        self.assertTrue(any(isinstance(widget, Gtk.Grid) for widget in widgets))
        self.assertIn('Remember', labels)
        self.assertIn('[one] Footnote body', labels)
        self.assertIn('Embedded body.', [label.strip() for label in labels])
        self.assertTrue(any('cycle' in label for label in labels))
        self.assertTrue(any(isinstance(widget, Gtk.Image) for widget in widgets))
        self.assertEqual(self.pane.text(), source)
        self.assertFalse(any('Could not render' in label for label in labels))
        self.assertTrue(any('bold ' == label for label in labels))
        self.assertTrue(any(' text' == label for label in labels))

    def test_math_image_has_transparent_background_in_dark_theme(self):
        self.pane.buffer.set_text('$x^2 + y^2$')
        self.pane.set_mode('reading')
        image = next(widget for widget in self.descendants(self.pane.reading.body)
                     if isinstance(widget, Gtk.Image))
        pixbuf = image.get_pixbuf()
        self.assertTrue(pixbuf.get_has_alpha())
        self.assertEqual(pixbuf.get_pixels()[3], 0)

    def test_reading_refresh_tracks_nested_note_changes_and_preserves_unchanged_widgets(self):
        (self.root / 'Child.md').write_text('![[Grandchild]]')
        child = self.root / 'Grandchild.md'
        child.write_text('Before external edit')
        self.window.vault.refresh()
        self.pane.buffer.set_text('![[Child]]')
        self.pane.set_mode('reading')
        original = self.pane.reading.body.get_children()
        self.pane.reading.refresh()
        self.assertEqual(original, self.pane.reading.body.get_children())
        child.write_text('After external edit, longer content')
        self.window.refresh()
        labels = [widget.get_text() for widget in self.descendants(self.pane.reading.body)
                  if isinstance(widget, Gtk.Label)]
        self.assertIn('After external edit, longer content', labels)
        self.assertNotEqual(original, self.pane.reading.body.get_children())
        refreshed = self.pane.reading.body.get_children()
        self.window.refresh()
        self.assertEqual(refreshed, self.pane.reading.body.get_children())

    def test_reading_refresh_tracks_assets_unsaved_embeds_and_theme(self):
        from dataclasses import replace
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 20, 10)
        image.fill(0xff0000ff)
        _, data = image.save_to_bufferv('png', [], [])
        relative = store_attachment(self.window.vault, 'changing.png', data)
        self.pane.buffer.set_text(f'![]({relative})\n\n$x$')
        self.pane.set_mode('reading')
        original = self.pane.reading.body.get_children()
        image.fill(0x00ff00ff)
        image.savev(str(self.root / relative), 'png', [], [])
        self.pane.reading.refresh()
        self.assertNotEqual(original, self.pane.reading.body.get_children())
        original = self.pane.reading.body.get_children()
        self.window.preferences = replace(self.window.preferences, theme='light')
        self.pane.apply_preferences()
        self.assertNotEqual(original, self.pane.reading.body.get_children())

        (self.root / 'Child.md').write_text('Saved embedded content')
        self.window.vault.refresh()
        self.window.open_note('Child.md')
        child = self.window.current()
        self.pane.buffer.set_text('![[Child]]')
        self.pane.reading.refresh()
        child.buffer.set_text('Unsaved embedded content')
        self.pane.reading.refresh()
        labels = [widget.get_text() for widget in self.descendants(self.pane.reading.body)
                  if isinstance(widget, Gtk.Label)]
        self.assertIn('Unsaved embedded content', labels)

    def test_malformed_math_preserves_source_and_shows_rendering_error(self):
        source = '$\\unknowncommand{x}$'
        self.pane.buffer.set_text(source)
        self.pane.set_mode('reading')
        labels = [widget.get_text() for widget in self.descendants(self.pane.reading.body)
                  if isinstance(widget, Gtk.Label)]
        self.assertTrue(any('Could not render this expression' in label for label in labels))
        self.assertEqual(self.pane.text(), source)

    def test_live_image_preview_preserves_source_and_reveals_active_line(self):
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 40, 20)
        image.fill(0xff0000ff)
        _, data = image.save_to_bufferv('png', [], [])
        relative = store_attachment(self.window.vault, 'image.png', data)
        source = f'Before\n![image]({relative})\nAfter\n\n- [ ] Task\n'
        self.pane.buffer.set_text(source)
        self.pane.buffer.place_cursor(self.pane.buffer.get_end_iter())
        self.pane.render()
        self.assertEqual(len(self.pane.image_widgets), 1)
        start, end, widget, tag = self.pane.image_widgets[0]
        self.assertTrue(widget.get_visible())
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.assertTrue(self.pane.buffer.get_iter_at_offset(start).has_tag(tag))
        self.assertEqual(self.pane.text(), source)
        self.assertTrue(any(item.task_offset is not None for item, _ in self.pane.list_widgets))
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(end))
        self.pane.render()
        self.assertTrue(widget.get_visible())
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(start + 1))
        self.pane.render()
        self.assertFalse(widget.get_visible())
        self.assertFalse(self.pane.buffer.get_iter_at_offset(start).has_tag(self.pane.hidden))
        self.pane.set_mode('source')
        self.assertFalse(widget.get_visible())
        self.assertEqual(self.pane.text(), source)

    def test_inserted_image_in_text_gets_a_previewable_line(self):
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 20, 10)
        image.fill(0xff0000ff)
        _, data = image.save_to_bufferv('png', [], [])
        self.pane.buffer.set_text('before after')
        self.pane.buffer.place_cursor(self.pane.buffer.get_iter_at_offset(6))
        self.pane.insert_attachment_bytes('image.png', data)
        self.pane.render()
        self.assertEqual(self.pane.text(), 'before\n![image.png](attachments/image.png)\n after')
        self.assertEqual(len(self.pane.image_widgets), 1)
        self.assertTrue(self.pane.image_widgets[0][2].get_visible())
        self.pane.undo()
        self.assertEqual(self.pane.text(), 'before after')

    def test_live_image_preview_ignores_code_and_missing_images(self):
        self.pane.buffer.set_text('```md\n![fake](missing.png)\n```\n\n![missing](missing.png)\n')
        self.pane.render()
        self.assertEqual(self.pane.image_widgets, [])

    def test_local_image_embed_is_scaled_and_remote_image_is_not_fetched(self):
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 20, 10)
        image.fill(0xff0000ff)
        _, data = image.save_to_bufferv('png', [], [])
        relative = store_attachment(self.window.vault, 'image.png', data)
        self.pane.buffer.set_text(f'![]({relative})\n\n![](https://example.com/image.png)')
        self.pane.set_mode('reading')
        widgets = list(self.descendants(self.pane.reading.body))
        self.assertTrue(any(isinstance(widget, Gtk.Image) for widget in widgets))
        self.assertTrue(any(isinstance(widget, Gtk.Label) and 'Only attachments' in widget.get_text()
                            for widget in widgets))

    def test_pdf_preview_has_real_page_count_and_navigation(self):
        try:
            import cairo
            from librevault.reading import PdfView
            import gi
            gi.require_version('Poppler', '0.18')
        except (ImportError, ValueError):
            self.skipTest('Poppler and Cairo are required for inline PDFs')
        path = self.root / 'two.pdf'
        surface = cairo.PDFSurface(str(path), 200, 100)
        context = cairo.Context(surface)
        context.show_text('First')
        context.show_page()
        context.show_text('Second')
        context.show_page()
        surface.finish()
        view = PdfView(path)
        self.addCleanup(view.destroy)
        self.assertEqual(view.counter.get_text(), '1 / 2')
        view.show_page(1)
        self.assertEqual(view.counter.get_text(), '2 / 2')
        self.assertIsNotNone(view.image.get_pixbuf())

    def test_pasting_screenshot_and_dropping_file_copy_real_bytes(self):
        image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 20, 10)
        image.fill(0x00ff00ff)
        with patch.object(Gtk.Clipboard, 'get') as clipboard:
            clipboard.return_value.wait_for_image.return_value = image
            self.pane.editor.emit('paste-clipboard')
        path = self.root / 'attachments' / 'Screenshot.png'
        self.assertTrue(path.read_bytes().startswith(b'\x89PNG'))
        self.assertIn('![Screenshot.png]', self.pane.text())
        from unittest.mock import Mock
        external = Path(self.directory.name) / 'drop.txt'
        external.write_bytes(b'dropped contents')
        selection = Mock()
        selection.get_uris.return_value = [external.as_uri()]
        context = Mock()
        with patch.object(Gtk, 'drag_finish') as finish:
            self.pane.drop_attachments(None, context, 0, 0, selection, 1, 123)
        finish.assert_called_once_with(context, True, False, 123)
        self.assertEqual((self.root / 'attachments' / 'drop.txt').read_bytes(), b'dropped contents')
        self.assertIn('[drop.txt](attachments/drop.txt)', self.pane.text())
