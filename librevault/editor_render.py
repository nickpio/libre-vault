"""Live-preview rendering for note buffers: spans, embeds, lists, and widgets."""
from functools import lru_cache
from io import BytesIO
import re

from gi.repository import GdkPixbuf, GLib, Gtk, Pango

from .appearance import PALETTES, TITLE_FONTS
from .attachments import local_path
from .folding import compute_regions
from .formatting import markdown_layout
from .mermaid import render_mermaid
from .preferences import MIN_FONT_SIZE
from .reading import pixbuf_bytes, scaled_pixbuf
from .render_plan import Concealment, hidden_changes, reconcile
from .syntax import TOKEN_STYLES
from .vault import VaultError


@lru_cache(maxsize=256)
def math_pixbuf(expression, color, dpi=140):
    """Render one mathtext expression. Unsupported math returns None."""
    if not expression or len(expression) > 400:
        return None
    try:
        from matplotlib import rc_context
        from matplotlib.mathtext import math_to_image
        output = BytesIO()
        with rc_context({'savefig.transparent': True}):
            math_to_image('$' + expression + '$', output, dpi=dpi, format='png', color=color)
        loader = GdkPixbuf.PixbufLoader()
        loader.write(output.getvalue())
        loader.close()
        pixbuf = loader.get_pixbuf()
    except Exception:
        return None
    if pixbuf is not None and pixbuf.get_width() > 640:
        scale = 640 / pixbuf.get_width()
        pixbuf = pixbuf.scale_simple(640, max(1, int(pixbuf.get_height() * scale)), GdkPixbuf.InterpType.BILINEAR)
    return pixbuf


def selection_overlaps(selected, start, end):
    return bool(selected and start < selected[1] and end > selected[0])


def line_hits(text, start, end, cursor):
    """True when the cursor's line intersects the source span."""
    if cursor < 0 or cursor > len(text) or end <= start:
        return False
    line_start = text.rfind('\n', 0, min(cursor, len(text))) + 1
    line_end = text.find('\n', min(cursor, len(text)))
    if line_end < 0:
        line_end = len(text)
    return line_start < end and line_end > start


class EditorRenderMixin:
    def set_mode(self, mode):
        if mode not in ('live', 'source', 'reading'):
            return
        self.mode = mode
        if self.mode_selector.get_active_id() != mode:
            self.mode_selector.set_active_id(mode)
        self.content_stack.set_visible_child_name('reading' if mode == 'reading' else 'editor')
        self.render()
        if mode == 'reading':
            self.findbar.close()
            self.slash.dismiss()
            self.reading.refresh(force=True)
        else:
            self.reading.clear()
            self.editor.grab_focus()

    def apply_preferences(self):
        preferences = self.window.preferences
        colors = PALETTES[preferences.theme]
        self.tags['code'].set_property('background', colors['code_bg'])
        self.tags['code'].set_property('size-points', max(MIN_FONT_SIZE, preferences.editor_size - 1))
        self.tags['metadata'].set_property('size-points', max(MIN_FONT_SIZE, preferences.editor_size - 1))
        for name in ('quote', 'completed', 'metadata'):
            self.tags[name].set_property('foreground', colors['muted'])
        self.tags['link'].set_property('foreground', colors['link'])
        self.tags['highlight'].set_property('background', colors['highlight_bg'])
        self.tags['highlight'].set_property('foreground', colors['highlight_fg'])
        self.tags['comment'].set_property('foreground', colors['muted'])
        for style in TOKEN_STYLES:
            self.tags[style].set_property('foreground', colors[style])
        self.tags['mark'].set_property('background', colors['mark_bg'])
        self.tags['mark'].set_property('foreground', colors['mark_fg'])
        self.tags['kbd'].set_property('background', colors['code_bg'])
        self.tags['kbd'].set_property('foreground', colors['ink'])
        self.tags['callout'].set_property('foreground', colors['bright'])
        self.tags['table'].set_property('size-points', max(MIN_FONT_SIZE, preferences.editor_size - 1))
        self.tags['math'].set_property('foreground', colors['bright'])
        for name in ('subscript', 'superscript'):
            self.tags[name].set_property('size-points', max(MIN_FONT_SIZE, preferences.editor_size * 0.8))
        for level, size in enumerate((25, 18, 16, 14, 12, 11), 1):
            self.tags[f'h{level}'].set_property('size-points', size * max(MIN_FONT_SIZE, preferences.editor_size) / 11)
            self.tags[f'h{level}'].set_property('foreground', colors['bright'])
        self.tags['h1'].set_property('family', ', '.join(TITLE_FONTS) if preferences.editor_font == 'sans' else None)
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if preferences.wrap_lines else Gtk.WrapMode.NONE)
        self.size_editor(self.editor, self.editor.get_allocation())
        self.update_list_margins()
        self.findbar.cached = None
        self.findbar.refresh()
        self.occurrence_tag.set_property('background', colors['selection_bg'])
        self._sync_vim_enabled()
        if not self.window.preferences.slash_commands:
            self.slash.dismiss()
        if self.mode == 'reading':
            self.reading.refresh()
        # Preview widgets measure themselves against fonts and theme colors,
        # so a preference change must not reuse any of them.
        self._preview_generation += 1
        self.preview_theme = None
        self.render()

    def size_editor(self, _, allocation):
        preferences = self.window.preferences
        margin = max(32, (allocation.width - preferences.line_width) // 2) if preferences.readable_length else 32
        if self.editor.get_left_margin() != margin:
            self.editor.set_left_margin(margin)
            self.editor.set_right_margin(margin)
            self.tags['quote'].set_property('left-margin', margin + 18)
            self.update_list_margins()
        for _start, _end, widget, tag in self.table_widgets:
            self.size_table_preview(widget)
            tag.set_property('pixels-above-lines', widget.get_preferred_height().natural_height + 8)
        for _start, _end, widget, tag in self.callout_widgets:
            self.size_callout_preview(widget, tag)
        self.queue_positions()

    def render(self):
        text = self.text()
        theme = self.window.preferences.theme
        rebuilt = False
        if text != self.formatted_text or theme != self.preview_theme:
            layout = markdown_layout(text)
            self.spans, self.items, self.headings = layout.spans, layout.items, layout.headings
            self.diagrams = layout.diagrams
            self.math_spans, self.callouts = layout.math, layout.callouts
            self.formatted_text = text
            self.preview_theme = theme
            self.fold_regions = compute_regions(text, layout)
            self.folds.prune(self.fold_regions)
            self.rebuild_list_widgets()
            self.rebuild_image_widgets(layout.images)
            self.rebuild_diagram_widgets(layout.diagrams)
            self.rebuild_preview_widgets(layout)
            rebuilt = True
        else:
            if self._diagram_theme != self.window.preferences.theme:
                self.rebuild_diagram_widgets(self.diagrams)
                rebuilt = True
            if self.fold_regions is None:
                self.fold_regions = compute_regions(text)
                self.folds.prune(self.fold_regions)
        # Without a buffer edit, every tag from the last pass is still in place
        # and only the concealment can differ.
        if rebuilt or self._tags_stale or self._tagged_mode != self.mode:
            self._reset_tags(len(text))
            if self.mode != 'source':
                self._apply_static_tags(text)
        concealed = self._concealed_ranges()
        plan = Concealment(len(text))
        if self.mode == 'source':
            self._finish_render(plan, concealed)
            self.hide_source_widgets()
            return
        if self.mode == 'reading':
            self.reading.refresh()
        cursor = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
        line_start = text.rfind('\n', 0, cursor) + 1
        line_end = text.find('\n', cursor)
        if line_end < 0:
            line_end = len(text)
        selection = self.buffer.get_selection_bounds()
        selected = tuple(it.get_offset() for it in selection) if selection else None
        editing_math = self._math_being_edited(cursor, selected)
        for span in self.spans:
            if span.style != 'syntax':
                continue
            on_line = span.start <= line_end and span.end > line_start
            in_selection = selection_overlaps(selected, span.start, span.end)
            in_edited_math = any(item.start <= span.start and span.end <= item.end for item in editing_math)
            if not on_line and not in_selection and not in_edited_math:
                plan.hide(span.start, span.end)

        def covered(offset):
            return any(span_start <= offset < span_end for span_start, span_end in concealed)

        for item, widget in self.list_widgets:
            # Lists stay rendered on the active line; a selection can expose
            # their source markers for deliberate Markdown editing.
            if covered(item.start):
                widget.hide()
            else:
                reveal = selected and item.start < selected[1] and item.content_start > selected[0]
                widget.set_visible(not reveal)
                if not reveal:
                    plan.hide(item.start, item.content_start)
        for image_start, image_end, widget, tag in self.image_widgets:
            if covered(image_start):
                widget.hide()
                continue
            editing_image = image_start <= cursor < image_end
            in_selection = selection_overlaps(selected, image_start, image_end)
            visible = self.mode == 'live' and not editing_image and not in_selection
            widget.set_visible(bool(visible))
            if visible:
                self.conceal_block(plan, image_start, image_end, tag)
        for diagram_start, diagram_end, widget, tag in self.diagram_widgets:
            if covered(diagram_start):
                widget.hide()
                continue
            editing_diagram = diagram_start <= cursor < diagram_end
            in_selection = selected and diagram_start < selected[1] and diagram_end > selected[0]
            visible = self.mode == 'live' and not editing_diagram and not in_selection
            widget.set_visible(bool(visible))
            if visible:
                newline = text.find('\n', diagram_start)
                first_end = diagram_end if newline < 0 or newline >= diagram_end else newline + 1
                plan.hide(diagram_start, diagram_end)
                plan.space(tag, diagram_start, first_end)
        self.preview_blocks(plan, text, cursor, selected, covered)
        self._finish_render(plan, concealed)

    def _reset_tags(self, length):
        start, end = self.buffer.get_bounds()
        spacing = [*(entry[3] for entry in self.image_widgets),
                   *(entry[3] for entry in self.diagram_widgets),
                   *(entry[3] for entry in self.preview_entries())]
        for tag in [*self.tags.values(), *self.list_tags.values(), self.hidden, self.occurrence_tag, *spacing]:
            self.buffer.remove_tag(tag, start, end)
        self._tags_stale = False
        self._tagged_mode = self.mode
        self._applied_hidden = bytearray(length)
        self._applied_spacing = {}
        self._applied_occurrences = []

    def _apply_static_tags(self, text):
        """Apply the tags that depend only on the source text."""
        for span in self.spans:
            if span.style != 'syntax' and span.style in self.tags:
                self.buffer.apply_tag(self.tags[span.style], self.buffer.get_iter_at_offset(span.start),
                                      self.buffer.get_iter_at_offset(span.end))
        for item, _widget in self.list_widgets:
            self.buffer.apply_tag(self.list_tags[item.depth], self.buffer.get_iter_at_offset(item.start),
                                  self.buffer.get_iter_at_offset(item.end))
            if item.checked:
                line_end = text.find('\n', item.content_start)
                stop = len(text) if line_end < 0 else line_end
                self.buffer.apply_tag(self.tags['completed'], self.buffer.get_iter_at_offset(item.content_start),
                                      self.buffer.get_iter_at_offset(stop))

    def _finish_render(self, plan, concealed):
        for start, end in concealed:
            plan.hide(start, end)
        self._commit_concealment(plan)
        self._highlight_occurrences()
        self._sync_fold_marks(concealed)
        self._sync_extra_carets()
        self.queue_positions()

    def _commit_concealment(self, plan):
        """Touch only the ranges whose concealment differs from the last pass."""
        offset = self.buffer.get_iter_at_offset
        added, removed = hidden_changes(self._applied_hidden, plan.hidden)
        for start, end in removed:
            self.buffer.remove_tag(self.hidden, offset(start), offset(end))
        for start, end in added:
            self.buffer.apply_tag(self.hidden, offset(start), offset(end))
        self._applied_hidden = plan.hidden
        for tag, (start, end) in self._applied_spacing.items():
            if plan.spacing.get(tag) != (start, end):
                self.buffer.remove_tag(tag, offset(start), offset(end))
        for tag, (start, end) in plan.spacing.items():
            if self._applied_spacing.get(tag) != (start, end):
                self.buffer.apply_tag(tag, offset(start), offset(end))
        self._applied_spacing = plan.spacing

    def _descriptor(self, kind, *source):
        """Identify what a widget shows. Source offsets are deliberately absent."""
        return (self._preview_generation, self.window.preferences.theme, self.note.path, kind, *source)

    def _file_signature(self, target):
        try:
            found = local_path(self.window.vault, self.note.path, target).stat()
        except (OSError, VaultError, ValueError):
            return None
        return found.st_mtime_ns, found.st_size

    def _sync_entries(self, entries, wanted, build):
        """Update ``entries`` from ``(start, end, descriptor, source)`` values.

        Matching widgets stay alive with their new offsets. ``build`` returns
        ``(widget, tag)`` for the rest, or None to leave the block unpreviewed.
        """
        previous = [(entry[2].preview_descriptor, entry) for entry in entries]
        matched, stale = reconcile(previous, [item[2] for item in wanted])
        for _, _, widget, tag in stale:
            widget.destroy()
            self.buffer.get_tag_table().remove(tag)
        entries.clear()
        for (start, end, descriptor, source), entry in zip(wanted, matched):
            if entry is not None:
                entries.append((start, end, entry[2], entry[3]))
                continue
            built = build(source)
            if built is None:
                continue
            widget, tag = built
            widget.preview_descriptor = descriptor
            entries.append((start, end, widget, tag))

    def rebuild_image_widgets(self, images):
        self._sync_entries(self.image_widgets, [
            (start, end, self._descriptor('image', target, width, height, self._file_signature(target)),
             (target, width, height))
            for start, end, target, width, height in images], self.image_widget)

    def image_widget(self, source):
        target, width, height = source
        try:
            path = local_path(self.window.vault, self.note.path, target)
            info = GdkPixbuf.Pixbuf.get_file_info(str(path))
            if not info or not info[1] or not info[2]:
                return None

            def fallback(path=path, info=info):
                scale = min(1, 600 / info[1], 360 / info[2])
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), max(1, int(info[1] * scale)), max(1, int(info[2] * scale)), True)

            pixbuf = scaled_pixbuf(path, width, height, fallback)
        except (OSError, GLib.Error, VaultError, ValueError):
            return None
        widget = Gtk.Image.new_from_pixbuf(pixbuf)
        widget.set_tooltip_text('Place the cursor inside the image Markdown to edit it')
        self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
        return widget, self.buffer.create_tag(None, pixels_above_lines=pixbuf.get_height() + 8)

    def rebuild_diagram_widgets(self, diagrams):
        self._diagram_theme = self.window.preferences.theme
        self._sync_entries(self.diagram_widgets, [
            (start, end, self._descriptor('diagram', source), source)
            for start, end, source in diagrams], self.diagram_widget)

    def diagram_widget(self, source):
        try:
            rendered = render_mermaid(source, PALETTES[self._diagram_theme])
            pixbuf = pixbuf_bytes(rendered.png)
        except (OSError, ValueError, RuntimeError):
            return None
        widget = Gtk.Image.new_from_pixbuf(pixbuf)
        widget.set_tooltip_text('Place the cursor inside the Mermaid block to edit it')
        self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
        return widget, self.buffer.create_tag(None, pixels_above_lines=pixbuf.get_height() + 12)

    def preview_entries(self):
        return [*self.math_widgets, *self.embed_widgets, *self.table_widgets, *self.callout_widgets]

    def hide_source_widgets(self):
        for _, widget in self.list_widgets:
            widget.hide()
        for _, _, widget, _tag in (*self.image_widgets, *self.diagram_widgets, *self.preview_entries()):
            widget.hide()

    def attach_preview(self, widget):
        widget.set_halign(Gtk.Align.START)
        self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
        widget.show_all()
        height = max(widget.get_preferred_height().natural_height, 18)
        widget.hide()
        widget.set_no_show_all(True)
        return widget, self.buffer.create_tag(None, pixels_above_lines=height + 8)

    def rebuild_preview_widgets(self, layout):
        text = self.formatted_text
        color = PALETTES[self.window.preferences.theme]['bright']
        self._sync_entries(self.math_widgets, [
            (item.start, item.end, self._descriptor('math', item.expression), item.expression)
            for item in layout.math if self.math_uses_widget(text, item)],
            lambda expression: self.attach_preview(self.math_preview(expression, color)))

        def attach_embed(embed):
            widget = self.embed_preview(embed)
            return None if widget is None else self.attach_preview(widget)

        self._sync_entries(self.embed_widgets, [
            (embed.start, embed.end, self._descriptor('embed', embed.kind, embed.target, self._embed_signature(embed)),
             embed)
            for embed in layout.embeds], attach_embed)
        self._sync_entries(self.table_widgets, [
            (table.start, table.end, self._descriptor('table', text[table.start:table.end]), table)
            for table in layout.tables], lambda table: self.attach_preview(self.table_preview(table)))
        for start, end, widget, _tag in self.table_widgets:
            widget.table_start = start
            # Keep one real newline as the table's layout slot. Locations for
            # invisible characters do not include paragraph spacing in GTK.
            widget.source_anchor = text.rfind('\n', start, end)
        # Share the safety budget across this rebuild, never across edits.
        self.reading.embed_count = self.reading.embed_bytes = 0
        self.reading.embed_limit_shown = False
        self._sync_entries(self.callout_widgets, [
            (callout.start, callout.end, self._callout_descriptor(text[callout.start:callout.end]), callout)
            for callout in layout.callouts], lambda callout: self.attach_preview(self.callout_preview(callout)))
        for start, end, widget, _tag in self.callout_widgets:
            widget.source_anchor = max(start, text.rfind('\n', start, end))

    def _embed_signature(self, embed):
        """Detect changes outside this note, which never alter its source."""
        if embed.kind == 'image':
            signature = self._file_signature(embed.target.split('#', 1)[0])
            if signature is not None:
                return signature
        return self.embed_excerpt(embed.target)

    def _callout_descriptor(self, source):
        # Transclusions show other files, and footnote links register with the
        # reading view, which forgets them. Either must be rebuilt each edit.
        if '![' in source or '[^' in source:
            return None
        return self._descriptor('callout', source)

    def math_uses_widget(self, text, item):
        if '\n' in text[item.start:item.end]:
            return True
        line_start = text.rfind('\n', 0, item.start) + 1
        line_end = text.find('\n', item.end)
        if line_end < 0:
            line_end = len(text)
        return not text[line_start:item.start].strip() and not text[item.end:line_end].strip()

    def math_preview(self, expression, color):
        pixbuf = math_pixbuf(expression, color)
        if pixbuf is None:
            label = Gtk.Label(label=expression or 'Math', xalign=0)
            label.set_tooltip_text('This expression stays in the note. Move the cursor here to edit it.')
            label.get_style_context().add_class('math-preview')
            return label
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.set_tooltip_text(expression)
        image.get_style_context().add_class('math-preview')
        return image

    def embed_preview(self, embed):
        if embed.kind == 'image':
            image = self.preview_image(embed.target)
            if image is not None:
                image.set_tooltip_text('Place the cursor on this line to edit the embed')
                return image
        frame = Gtk.Frame()
        frame.get_style_context().add_class('live-embed')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        title = Gtk.Label(label=embed.target, xalign=0)
        title.get_style_context().add_class('live-embed-title')
        excerpt = Gtk.Label(label=self.embed_excerpt(embed.target), xalign=0)
        excerpt.set_line_wrap(True)
        excerpt.set_max_width_chars(72)
        excerpt.get_style_context().add_class('muted')
        box.pack_start(title, False, False, 0)
        box.pack_start(excerpt, False, False, 0)
        frame.add(box)
        frame.set_tooltip_text('Place the cursor on this line to edit the embed')
        if embed.kind == 'note':
            target = embed.target
            frame.connect('button-press-event', lambda *_args, target=target: self.open_embed(target))
        return frame

    def preview_image(self, target):
        try:
            path = local_path(self.window.vault, self.note.path, target.split('#', 1)[0])
            info = GdkPixbuf.Pixbuf.get_file_info(str(path))
            if not info or not info[1] or not info[2]:
                return None
            scale = min(1, 600 / info[1], 360 / info[2])
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path), max(1, int(info[1] * scale)), max(1, int(info[2] * scale)), True)
        except (OSError, GLib.Error, VaultError):
            return None
        return Gtk.Image.new_from_pixbuf(pixbuf)

    def embed_excerpt(self, target):
        note_target = target.split('#', 1)[0].split('|', 1)[0].strip()
        try:
            candidates = self.window.vault.resolve(self.note.path, note_target)
        except (OSError, VaultError):
            return 'Embedded note'
        if len(candidates) != 1:
            return 'Unresolved embed' if not candidates else 'Several notes match this embed'
        relative = candidates[0]
        try:
            opened = self.window.panes.get(relative)
            source = opened.text() if opened is not None else self.window.vault.read(relative).text
        except (OSError, VaultError, AttributeError):
            return relative
        body = source
        if body.startswith('---\n'):
            end = re.search(r'\n(?:---|\.\.\.)\s*\n', body)
            if end:
                body = body[end.end():]
        for line in body.splitlines():
            stripped = line.strip().lstrip('#').strip()
            if stripped and not stripped.startswith('![['):
                return stripped[:80]
        return 'Empty note'

    def open_embed(self, target):
        self.window.follow_link(self.note.path, target)
        return True

    def table_preview(self, table):
        grid = Gtk.Grid(column_spacing=14, row_spacing=4)
        grid.table_start = table.start
        grid.table_columns = [[] for _ in table.headers]
        grid.get_style_context().add_class('live-table')
        grid.set_tooltip_text('Use Edit table to edit cells, resize columns, or browse all rows')
        shown = table.rows[:12]
        for column, header in enumerate(table.headers):
            label = Gtk.Label(label=self.cell_label(header), xalign=self.cell_xalign(table.alignments[column]))
            label.get_style_context().add_class('live-table-heading')
            self.wrap_table_label(label)
            grid.table_columns[column].append(label)
            grid.attach(label, column, 1, 1, 1)
        for row_index, row in enumerate(shown, 2):
            for column, cell in enumerate(row):
                alignment = table.alignments[column] if column < len(table.alignments) else 'left'
                label = Gtk.Label(label=self.cell_label(cell), xalign=self.cell_xalign(alignment))
                self.wrap_table_label(label)
                grid.table_columns[column].append(label)
                grid.attach(label, column, row_index, 1, 1)
        if len(table.rows) > len(shown):
            more = Gtk.Label(label=f'{len(table.rows) - len(shown)} more rows', xalign=0)
            more.get_style_context().add_class('muted')
            grid.attach(more, 0, len(shown) + 2, max(len(table.headers), 1), 1)
        edit = Gtk.Button(label='Edit table…')
        edit.set_halign(Gtk.Align.END)
        edit.get_style_context().add_class('live-table-edit')
        edit.connect('clicked', lambda button: self.edit_preview_table(button.get_parent().table_start))
        grid.attach(edit, 0, 0, max(len(table.headers), 1), 1)
        self.size_table_preview(grid)
        return grid

    def size_table_preview(self, grid):
        # TextView overlay children receive their minimum width, not the
        # natural width of wrapping labels. Reserve a measured width per
        # column so ordinary values do not collapse into narrow strips.
        desired = [min(360, max(40, max(label.create_pango_layout(label.get_text()).get_pixel_size()[0]
                                         for label in column)))
                   for column in grid.table_columns]
        available = (self.editor.get_allocated_width() - self.editor.get_left_margin()
                     - self.editor.get_right_margin())
        budget = max(80 * len(desired), available - 22 - 14 * (len(desired) - 1))
        widths = list(desired)
        # Keep compact columns intact and share the remaining room among
        # longer columns. Cap long prose at 360px even in a wide note.
        while sum(widths) > budget:
            largest = max(widths)
            if largest <= 80:
                break
            widths = [width - 1 if width == largest else width for width in widths]
        for column, width in zip(grid.table_columns, widths):
            for label in column:
                if label.get_size_request().width != width:
                    label.set_size_request(width, -1)

    def edit_preview_table(self, start):
        self.buffer.place_cursor(self.buffer.get_iter_at_offset(start))
        self.prompt_edit_table()

    def wrap_table_label(self, label):
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(32)
        label.set_selectable(True)

    def callout_preview(self, callout):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class('editor-preview')
        self.reading.blocks(box, self.formatted_text[callout.start:callout.end],
                            self.note.path, {self.note.path})
        def prepare(widget):
            if isinstance(widget, Gtk.Label) and widget.get_line_wrap():
                widget.set_max_width_chars(1)
                widget.set_hexpand(True)
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    prepare(child)
        prepare(box)
        box.set_tooltip_text('Place the cursor in the callout to edit it')
        return box

    def size_callout_preview(self, widget, tag):
        width = max(120, self.editor.get_allocated_width() - 2 * self.editor.get_left_margin())
        if widget.get_size_request().width != width:
            widget.set_size_request(width, -1)
        height = widget.get_preferred_height_for_width(width).natural_height
        widget.preview_height = height
        widget.preview_x = self.editor.get_left_margin()
        tag.set_property('pixels-above-lines', height + 8)

    def cell_label(self, text):
        return re.sub(r'\\([\\|])', r'\1', text)

    def cell_xalign(self, alignment):
        return {'center': 0.5, 'right': 1}.get(alignment, 0)

    def _math_being_edited(self, cursor, selected):
        """Math whose delimiters stay visible on every line while it is edited."""
        widget_starts = {entry[0] for entry in self.math_widgets}
        return [item for item in self.math_spans
                if item.start <= cursor < item.end or selection_overlaps(selected, item.start, item.end)
                or (item.start in widget_starts and line_hits(self.formatted_text, item.start, item.end, cursor))]

    def conceal_block(self, plan, start, end, tag, first_line_only=False):
        text = self.formatted_text
        hide_end = end
        newline = text.find('\n', start)
        if first_line_only:
            hide_end = newline if 0 <= newline < end else end
        elif hide_end > start and text[hide_end - 1:hide_end] == '\n':
            hide_end -= 1
        spacing_end = hide_end
        if 0 <= newline < hide_end:
            spacing_end = newline
        plan.hide(start, hide_end)
        if spacing_end > start:
            plan.space(tag, start, spacing_end)

    def preview_blocks(self, plan, text, cursor, selected, covered=lambda _offset: False):
        if self.mode != 'live':
            self.hide_source_widgets()
            return
        for start, end, widget, tag in self.table_widgets:
            if covered(start):
                widget.hide()
                continue
            revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, start, end)
            widget.set_visible(not revealed)
            if not revealed:
                self._conceal_around_anchor(plan, start, end, widget.source_anchor, tag)
        for start, end, widget, tag in self.embed_widgets:
            if covered(start):
                widget.hide()
                continue
            revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, start, end)
            widget.set_visible(not revealed)
            if not revealed:
                self.conceal_block(plan, start, end, tag)
        for start, end, widget, tag in self.callout_widgets:
            if covered(start):
                widget.hide()
                continue
            revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, start, end)
            widget.set_visible(not revealed)
            if not revealed:
                self.size_callout_preview(widget, tag)
                self._conceal_around_anchor(plan, start, end, widget.source_anchor, tag)
        widget_starts = {start for start, _end, _widget, _tag in self.math_widgets}
        for item in self.math_spans:
            opener = 2 if text.startswith('$$', item.start) else 1
            closer = 2 if item.end - item.start >= 4 and text.startswith('$$', max(item.end - 2, item.start)) else 1
            if item.start in widget_starts:
                start, end, widget, tag = next(entry for entry in self.math_widgets if entry[0] == item.start)
                if covered(start):
                    widget.hide()
                    continue
                revealed = line_hits(text, start, end, cursor) or selection_overlaps(selected, item.start, item.end)
                widget.set_visible(not revealed)
                if revealed:
                    plan.reveal(start, end)
                else:
                    self.conceal_block(plan, start, end, tag)
                continue
            if item.start <= cursor < item.end or selection_overlaps(selected, item.start, item.end):
                plan.reveal(item.start, item.start + opener)
                plan.reveal(item.end - closer, item.end)
            else:
                plan.hide(item.start, item.start + opener)
                plan.hide(item.end - closer, item.end)

    def _conceal_around_anchor(self, plan, start, end, anchor, tag):
        plan.hide(start, anchor)
        plan.hide(anchor + 1, end)
        plan.space(tag, start, anchor + 1)

    def rebuild_list_widgets(self):
        focused = next((item.task_offset for item, widget in self.list_widgets if widget.has_focus()), None)
        wanted = [self._list_descriptor(item) for item in self.items]
        matched, stale = reconcile([(widget.preview_descriptor, widget) for _, widget in self.list_widgets], wanted)
        for widget in stale:
            widget.destroy()
        self.list_widgets.clear()
        for depth in sorted({item.depth for item in self.items}):
            if depth not in self.list_tags:
                self.list_tags[depth] = self.buffer.create_tag(None)
        for item, descriptor, widget in zip(self.items, wanted, matched):
            created = widget is None
            if created:
                widget = self._new_list_widget(item)
                widget.preview_descriptor = descriptor
            if item.task_offset is not None:
                widget.task_item, widget.task_source = item, self.formatted_text
            self.list_widgets.append((item, widget))
            if created and focused is not None and focused == item.task_offset:
                widget.grab_focus()
        self.update_list_margins()

    def _list_label(self, item):
        if item.task_offset is not None:
            return self.formatted_text[item.content_start:item.end].split('\n', 1)[0]
        return str(item.number) + '.' if item.number is not None else ['•', '◦', '▪'][item.depth % 3]

    def _list_descriptor(self, item):
        kind = 'task' if item.task_offset is not None else 'marker'
        return self._descriptor(kind, self._list_label(item), item.checked)

    def _new_list_widget(self, item):
        label = self._list_label(item)
        if item.task_offset is not None:
            widget = Gtk.CheckButton()
            widget.get_style_context().add_class('task-checkbox')
            widget.set_active(item.checked)
            widget.get_accessible().set_name(label or 'Task')
            widget.set_tooltip_text('Mark task incomplete' if item.checked else 'Mark task complete')
            widget.connect('toggled', self._task_toggled)
        else:
            widget = Gtk.Label(label=label)
            widget.get_style_context().add_class('list-marker')
        self.editor.add_child_in_window(widget, Gtk.TextWindowType.WIDGET, 0, 0)
        widget.show()
        return widget

    def update_list_margins(self):
        # Reserve enough gutter for long ordered-list numbers as well as nesting.
        widths = {}
        for item, widget in self.list_widgets:
            widths[item.depth] = max(widths.get(item.depth, 28), widget.get_preferred_width().natural_width + 10)
        for depth, tag in self.list_tags.items():
            gutter = sum(widths.get(level, 28) for level in range(depth + 1))
            tag.set_property('left-margin', self.editor.get_left_margin() + gutter)
            tag.set_property('indent', 0)

    def _task_toggled(self, widget):
        # The control now disagrees with the source it was built from, even
        # when toggle_task declines a stale edit, so it can never be reused.
        widget.preview_descriptor = None
        self.toggle_task(widget, widget.task_item, widget.task_source)

    def toggle_task(self, widget, item, source):
        # A pending edit may invalidate a widget's source offset. Never toggle
        # unrelated text; the next formatting pass replaces stale controls.
        if self.text() != source:
            self.queue_format()
            return
        start = self.buffer.get_iter_at_offset(item.task_offset)
        end = self.buffer.get_iter_at_offset(item.task_offset + 1)
        self.buffer.begin_user_action()
        self.buffer.delete(start, end)
        self.buffer.insert(start, 'x' if widget.get_active() else ' ')
        self.buffer.end_user_action()

    def queue_positions(self, *_):
        if self.position_source is None:
            self.position_source = GLib.idle_add(self.position_list_widgets)

    def position_list_widgets(self):
        self.position_source = None
        for item, widget in self.list_widgets:
            if not widget.get_visible():
                continue
            # GTK reports positions within hidden prefixes inconsistently. The
            # line start gives the first displayed position and survives wrapping.
            position = self.buffer.get_iter_at_offset(item.start)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            width = widget.get_preferred_width().natural_width
            height = widget.get_preferred_height().natural_height
            y += (rect.height - height) // 2
            widget.set_child_visible(y >= 0 and y + height <= self.editor.get_allocated_height())
            self.editor.move_child(widget, x - width - 8, y)
        for start, _, widget, _ in (*self.image_widgets, *self.diagram_widgets, *self.preview_entries()):
            if not widget.get_visible():
                continue
            position = self.buffer.get_iter_at_offset(getattr(widget, 'source_anchor', start))
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            height = getattr(widget, 'preview_height', widget.get_preferred_height().natural_height)
            self.editor.move_child(widget, getattr(widget, 'preview_x', x), y - height - 4)
        for button in self.fold_widgets:
            if not button.get_visible():
                continue
            position = self.buffer.get_iter_at_offset(button.fold_anchor)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            width = button.get_preferred_width().natural_width
            height = button.get_preferred_height().natural_height
            y += max(0, (rect.height - height) // 2)
            button.set_child_visible(y >= 0 and y + height <= self.editor.get_allocated_height())
            self.editor.move_child(button, max(0, x - width - 2), y)
        for marker in self.extra_carets:
            position = self.buffer.get_iter_at_offset(marker.caret_offset)
            rect = self.editor.get_iter_location(position)
            x, y = self.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rect.x, rect.y)
            height = marker.get_preferred_height().natural_height
            marker.set_child_visible(y >= 0 and y + height <= self.editor.get_allocated_height())
            self.editor.move_child(marker, x, y)
        return False

    def _concealed_ranges(self):
        reveal = []
        findbar = getattr(self, 'findbar', None)
        if findbar is not None and findbar.get_visible():
            reveal = [item.get_offset() for item in self.buffer.get_selection_bounds()]
        return self.folds.concealed(self.fold_regions or [], reveal)

    def _highlight_occurrences(self):
        ranges = [(start, end) for start, end in self.multicursor.ranges if end > start]
        if ranges == self._applied_occurrences:
            return
        if self._applied_occurrences:
            self.buffer.remove_tag(self.occurrence_tag, *self.buffer.get_bounds())
        for start, end in ranges:
            self.buffer.apply_tag(self.occurrence_tag, self.buffer.get_iter_at_offset(start),
                                  self.buffer.get_iter_at_offset(end))
        self._applied_occurrences = ranges

    def _sync_fold_marks(self, concealed):
        if self.mode == 'reading':
            for button in self.fold_widgets:
                button.hide()
            return

        def covered(offset):
            return any(start <= offset < end for start, end in concealed)

        visible = [region for region in self.fold_regions or [] if not covered(region.anchor)]
        signature = tuple((region.anchor, region.anchor in self.folds.anchors, region.kind) for region in visible)
        if signature != self._fold_signature:
            self._fold_signature = signature
            for button in self.fold_widgets:
                button.destroy()
            self.fold_widgets = []
            for region in visible:
                folded = region.anchor in self.folds.anchors
                button = Gtk.Button(label='▸' if folded else '▾')
                button.set_relief(Gtk.ReliefStyle.NONE)
                button.set_can_focus(False)
                button.get_style_context().add_class('fold-chevron')
                if folded:
                    button.get_style_context().add_class('folded')
                button.fold_anchor = region.anchor
                button.set_tooltip_text('Unfold section' if folded else 'Fold section')
                button.get_accessible().set_name(button.get_tooltip_text())
                button.connect('clicked', lambda _, anchor=region.anchor: self.toggle_fold_anchor(anchor))
                self.editor.add_child_in_window(button, Gtk.TextWindowType.WIDGET, 0, 0)
                button.show()
                self.fold_widgets.append(button)

    def _sync_extra_carets(self):
        carets = [start for start, end in self.multicursor.ranges if start == end]
        if carets != self._caret_signature:
            self._caret_signature = list(carets)
            for marker in self.extra_carets:
                marker.destroy()
            self.extra_carets = []
            for offset in carets:
                marker = Gtk.Label(label='▏')
                marker.get_style_context().add_class('extra-cursor')
                marker.caret_offset = offset
                self.editor.add_child_in_window(marker, Gtk.TextWindowType.WIDGET, 0, 0)
                marker.show()
                self.extra_carets.append(marker)

