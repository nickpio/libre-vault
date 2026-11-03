"""Compact note switching and editor link navigation."""
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
from markdown_it.helpers import parseLinkDestination

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gtk, Pango

from .anchors import (block_id_insertion, extract_subpath, heading_links, normalize_anchor,
                      preview_snippet, reference_blocks, split_transclusion_target)
from .attachments import local_path
from .commands import CommandPalette
from .navigation import matching_notes
from .vault import VaultError


class QuickSwitcher(CommandPalette):
    max_width = 560

    def __init__(self, owner):
        super().__init__(owner)
        self.search.set_placeholder_text('Find a note…')
        self.search.get_accessible().set_name('Quick switcher')
        self.results_scroll.set_max_content_height(240)

    def filter_commands(self, *_):
        for row in self.results.get_children():
            row.destroy()
        vault = self.owner.vault
        if vault:
            aliases = {path: vault.metadata(path).aliases for path in vault.notes}
            for path in matching_notes(vault.notes, self.search.get_text(), aliases):
                row = Gtk.ListBoxRow()
                row.title = path
                row.action = lambda path=path: self.owner.open_note(path)
                label = Gtk.Label(label=path, xalign=0)
                label.set_ellipsize(3)
                label.set_margin_top(7)
                label.set_margin_bottom(7)
                row.add(label)
                self.results.add(row)
        self.results.show_all()
        first = self.results.get_row_at_index(0)
        if first:
            self.results.select_row(first)
        self.empty.set_visible(first is None)


def note_link_from_uri(uri):
    """Decode a reading-view ``note:source|target`` href, or return None."""
    if not uri or not uri.startswith('note:'):
        return None
    source, separator, destination = uri[5:].partition('|')
    if not separator or not destination:
        return None
    return unquote(source), unquote(destination)


class LinkPreview:
    """A non-modal card with a note title and a short slice of its text."""

    delay_ms = 450

    def __init__(self):
        self.popover = None
        self.timer = None
        self.key = None
        self.title_label = None
        self.body_label = None

    def cleanup(self):
        self.hide()

    def hide(self):
        self._cancel_timer()
        if self.popover is not None:
            self.popover.destroy()
            self.popover = None
        self.key = None
        self.title_label = None
        self.body_label = None

    def consider(self, widget, source, target, ctrl, rectangle, window):
        if not target:
            self.hide()
            return
        key = (widget, source, target)
        if self.popover is not None and self.key == key:
            return
        if self.timer is not None and self.key == key:
            if ctrl:
                self.show(widget, source, target, rectangle, window)
            return
        self._cancel_timer()
        if self.popover is not None:
            self.popover.destroy()
            self.popover = None
            self.title_label = None
            self.body_label = None
        self.key = key
        if ctrl:
            self.show(widget, source, target, rectangle, window)
        else:
            self.timer = GLib.timeout_add(self.delay_ms, self._fire, widget, source, target, rectangle, window)

    def _fire(self, widget, source, target, rectangle, window):
        self.timer = None
        self.show(widget, source, target, rectangle, window)
        return False

    def _cancel_timer(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None

    def show(self, widget, source, target, rectangle, window):
        self._cancel_timer()
        if self.popover is not None:
            self.popover.destroy()
            self.popover = None
        title, body = preview_card(window, source, target)
        self.popover = Gtk.Popover.new(widget)
        self.popover.set_modal(False)
        self.popover.set_position(Gtk.PositionType.BOTTOM)
        self.popover.get_style_context().add_class('link-preview')
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(12)
        box.set_size_request(280, -1)
        self.title_label = Gtk.Label(label=title, xalign=0)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_label.set_max_width_chars(42)
        self.title_label.get_style_context().add_class('link-preview-title')
        self.body_label = Gtk.Label(label=body, xalign=0)
        self.body_label.set_line_wrap(True)
        self.body_label.set_max_width_chars(42)
        self.body_label.set_lines(8)
        self.body_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.body_label.get_style_context().add_class('link-preview-body')
        box.pack_start(self.title_label, False, False, 0)
        box.pack_start(self.body_label, False, False, 0)
        self.popover.add(box)
        self.popover.set_pointing_to(rectangle)
        self.key = (widget, source, target)
        self.popover.show_all()


def preview_card(window, source, target):
    """Title and snippet for an internal link, heading, or block."""
    path, fragment, _, _ = split_transclusion_target(target)
    vault = window.vault
    if vault is None:
        return path or target, 'Open a vault to preview this link.'
    candidates = vault.resolve(source, path)
    if len(candidates) != 1:
        asset = _preview_asset(vault, source, path)
        if asset is not None:
            return asset.name, 'Local attachment'
        if len(candidates) > 1:
            return path or 'Unresolved link', 'Several notes match this link.'
        return path or 'Unresolved link', 'This note does not exist yet.'
    relative = candidates[0]
    opened = window.panes.get(relative)
    try:
        text = opened.text() if opened is not None else vault.read(relative).text
    except (OSError, VaultError) as error:
        return Path(relative).stem, str(error)
    title = Path(relative).stem + (f' #{fragment}' if fragment else '')
    if fragment:
        section = extract_subpath(text, fragment)
        if section is None:
            return title, f'Heading or block not found: #{fragment}'
        text = section
    return title, preview_snippet(text, 400) or 'Empty note.'


def _preview_asset(vault, source, target):
    if not target:
        return None
    try:
        return local_path(vault, source, target)
    except (OSError, VaultError):
        return None


class _Choice:
    def __init__(self, kind, label, insert='', path=None, block=None):
        self.kind = kind
        self.label = label
        self.insert = insert
        self.path = path
        self.block = block


class EditorNavigation:
    def __init__(self, pane):
        self.pane = pane
        self.pending = None
        self.popover = None
        self.paths = []
        self.choices = []
        self.start = 0
        self.stop = 0
        self.hover = None
        self.link_preview = LinkPreview()
        self.buffer_handlers = [pane.buffer.connect('changed', self.queue_update),
                                pane.buffer.connect('mark-set', self.queue_update)]
        self.editor_handlers = [pane.editor.connect('button-release-event', self.clicked),
                                pane.editor.connect('motion-notify-event', self.motion),
                                pane.editor.connect('leave-notify-event', self.leave)]
        pane.connect('destroy', self.cleanup)

    def cleanup(self, *_):
        for handler in self.buffer_handlers:
            self.pane.buffer.disconnect(handler)
        self.buffer_handlers.clear()
        for handler in self.editor_handlers:
            self.pane.editor.disconnect(handler)
        self.editor_handlers.clear()
        if self.pending is not None:
            GLib.source_remove(self.pending)
            self.pending = None
        self.close()
        self.link_preview.cleanup()

    def close(self):
        if self.popover:
            self.popover.destroy()
            self.popover = None
        self.paths = []
        self.choices = []

    def queue_update(self, *_):
        if self.pending is None:
            self.pending = GLib.idle_add(self.update)

    def update(self):
        self.pending = None
        pane = self.pane
        text = pane.text()
        cursor = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        line = text[text.rfind('\n', 0, cursor) + 1:cursor]
        opening = line.rfind('[[')
        if opening < 0 or ']]' in line[opening:] or '|' in line[opening:]:
            self.close()
            return False
        query = line[opening + 2:]
        self.start, self.stop = cursor - len(query), cursor
        if '#' in query:
            self._anchor_choices(query)
        else:
            self._note_choices(query)
        return False

    def _note_choices(self, query):
        vault = self.pane.window.vault
        aliases = {path: vault.metadata(path).aliases for path in vault.notes}
        self.paths = matching_notes(vault.notes, query, aliases, limit=8)
        self.choices = []
        if not self.paths:
            self.close()
            return
        self._show_rows(self.paths)

    def _anchor_choices(self, query):
        note_part, _, rest = query.partition('#')
        paths = self._resolve_note(note_part)
        if len(paths) != 1:
            self._note_choices(note_part)
            return
        text = self._note_text(paths[0])
        if text is None:
            self.close()
            return
        prefix = '' if note_part.strip() == '' else paths[0].removesuffix('.md')
        if rest.startswith('^'):
            choices = self._block_choices(paths[0], text, prefix, rest[1:])
        else:
            choices = self._heading_choices(text, prefix, rest)
        self.paths = []
        self.choices = choices[:12]
        if not self.choices:
            self.close()
            return
        self._show_rows([choice.label for choice in self.choices])

    def _resolve_note(self, note_part):
        if note_part.strip() == '':
            return [self.pane.note.path]
        return self.pane.window.vault.resolve(self.pane.note.path, note_part.strip())

    def _note_text(self, path):
        opened = self.pane.window.panes.get(path)
        if opened is not None:
            return opened.text()
        try:
            return self.pane.window.vault.read(path).text
        except (OSError, VaultError):
            return None

    def _heading_choices(self, text, prefix, typed):
        folded = typed.casefold()
        folded_anchor = normalize_anchor(typed)
        choices = []
        for fragment, level, _offset in heading_links(text):
            if folded and folded not in fragment.casefold() and folded_anchor not in normalize_anchor(fragment):
                continue
            insert = f'{prefix}#{fragment}' if prefix else f'#{fragment}'
            choices.append(_Choice('heading', f'{"#" * level} {fragment}', insert))
        return choices

    def _block_choices(self, path, text, prefix, typed):
        folded = typed.casefold()
        blocks = sorted(reference_blocks(text), key=lambda item: item[0] is None)
        choices = []
        for block_id, snippet, start, end, kind in blocks:
            # Offsets match the editor only for the note being typed in.
            if path == self.pane.note.path and start < self.stop and end > self.start:
                continue
            if folded and folded not in f'{block_id or ""} {snippet}'.casefold():
                continue
            if block_id:
                insert = f'{prefix}#^{block_id}' if prefix else f'#^{block_id}'
                choices.append(_Choice('block', f'^{block_id}  {snippet}', insert))
            else:
                choices.append(_Choice('block-new', snippet or kind, block=(path, start, end, kind, prefix)))
        return choices

    def _show_rows(self, labels):
        if self.popover:
            self.popover.destroy()
        self.link_preview.hide()
        pane = self.pane
        self.popover = Gtk.Popover.new(pane.editor)
        self.popover.set_modal(False)
        self.rows = Gtk.ListBox()
        self.rows.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.rows.set_activate_on_single_click(True)
        self.rows.connect('row-activated', lambda *_: self.accept())
        self.popover.add(self.rows)
        for text in labels:
            label = Gtk.Label(label=text, xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_max_width_chars(48)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            self.rows.add(label)
        self.rows.select_row(self.rows.get_row_at_index(0))
        rectangle = pane.editor.get_iter_location(pane.buffer.get_iter_at_offset(self.stop))
        rectangle.x, rectangle.y = pane.editor.buffer_to_window_coords(Gtk.TextWindowType.WIDGET,
                                                                       rectangle.x, rectangle.y)
        self.popover.set_pointing_to(rectangle)
        self.popover.show_all()

    def accept(self):
        row = self.rows.get_selected_row()
        if row is None:
            return
        index = row.get_index()
        if self.choices:
            self._accept_choice(self.choices[index])
            return
        path = self.paths[index]
        self._insert_link(path.removesuffix('.md'))

    def _accept_choice(self, choice):
        if choice.kind == 'block-new':
            self._accept_new_block(choice)
            return
        self._insert_link(choice.insert)

    def _accept_new_block(self, choice):
        path, start, end, kind, prefix = choice.block
        pane = self.pane
        target = pane.window.panes.get(path)
        original = target.text() if target is not None else self._note_text(path)
        if original is None:
            return
        block_id, at, insertion = block_id_insertion(original, start, end, kind)
        link = f'{prefix}#^{block_id}' if prefix else f'#^{block_id}'
        suffix = '' if pane.text()[self.stop:self.stop + 2] == ']]' else ']]'
        link_text = link + suffix
        link_start, link_stop = self.start, self.stop
        self.close()
        if target is pane and at >= 0 and insertion:
            edits = [(link_start, link_stop, link_text), (at, at, insertion)]
            edits.sort(key=lambda item: item[0], reverse=True)
            pane.buffer.begin_user_action()
            for begin, finish, replacement in edits:
                pane.buffer.delete(pane.buffer.get_iter_at_offset(begin), pane.buffer.get_iter_at_offset(finish))
                pane.buffer.insert(pane.buffer.get_iter_at_offset(begin), replacement)
            pane.buffer.end_user_action()
            return
        if at >= 0 and insertion:
            if target is not None:
                self._splice(target.buffer, at, at, insertion)
            else:
                self._splice_file(path, original, at, insertion)
        self._splice(pane.buffer, link_start, link_stop, link_text)

    def _insert_link(self, body):
        pane = self.pane
        text = pane.text()
        suffix = '' if text[self.stop:self.stop + 2] == ']]' else ']]'
        start, stop = self.start, self.stop
        self.close()
        self._splice(pane.buffer, start, stop, body + suffix)

    def _splice(self, buffer, start, stop, replacement):
        buffer.begin_user_action()
        buffer.delete(buffer.get_iter_at_offset(start), buffer.get_iter_at_offset(stop))
        buffer.insert(buffer.get_iter_at_offset(start), replacement)
        buffer.end_user_action()

    def _splice_file(self, path, original, start, insertion):
        vault = self.pane.window.vault
        try:
            note = vault.read(path)
        except (OSError, VaultError):
            return
        if note.text != original or start > len(note.text):
            return
        try:
            vault.save(note, note.text[:start] + insertion + note.text[start:])
        except (OSError, VaultError) as error:
            self.pane.window.status.set_text(f'Could not add a block id: {error}')

    def key_press(self, event):
        if self.popover:
            if event.keyval == Gdk.KEY_Escape:
                self.close()
                return True
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab):
                self.accept()
                return True
            if event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
                row = self.rows.get_selected_row()
                index = row.get_index() if row else 0
                count = len(self.choices) if self.choices else len(self.paths)
                index = max(0, min(count - 1, index + (1 if event.keyval == Gdk.KEY_Down else -1)))
                self.rows.select_row(self.rows.get_row_at_index(index))
                return True
            return False
        if event.keyval == Gdk.KEY_Escape and self.link_preview.popover:
            self.link_preview.hide()
            return True
        if event.keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R) and self.hover:
            widget, source, target, rectangle = self.hover
            self.link_preview.consider(widget, source, target, True, rectangle, self.pane.window)
        return False

    def leave(self, *_):
        self.hover = None
        self.link_preview.hide()
        return False

    def motion(self, view, event):
        if self.popover:
            return False
        x, y = view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        found, iterator = view.get_iter_at_location(x, y)
        target = self.link_at(iterator.get_offset()) if found else None
        if target is None:
            self.hover = None
            self.link_preview.hide()
            return False
        rectangle = view.get_iter_location(iterator)
        rectangle.x, rectangle.y = view.buffer_to_window_coords(Gtk.TextWindowType.WIDGET, rectangle.x, rectangle.y)
        self.hover = (view, self.pane.note.path, target, rectangle)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        self.link_preview.consider(view, self.pane.note.path, target, ctrl, rectangle, self.pane.window)
        return False

    def link_at(self, offset):
        if any(span.style == 'code' and span.start <= offset < span.end for span in self.pane.spans):
            return None
        text = self.pane.text()
        pattern = r'!?\[\[([^\]\n]+)\]\]|!?\[[^\]\n]*\]\(([^)\n]+)\)'
        for match in re.finditer(pattern, text):
            if match.start() <= offset < match.end():
                if not any(span.style == 'link' and span.start < match.end()
                           and span.end > match.start() for span in self.pane.spans):
                    continue
                target = (match.group(1) or match.group(2)).split('|', 1)[0]
                if match.group(2):
                    result = parseLinkDestination(text, match.start(2), len(text))
                    if not result.ok:
                        continue
                    target = result.str
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    return None
                return target
        return None

    def clicked(self, view, event):
        if event.button != 1 or not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        x, y = view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(event.x), int(event.y))
        found, iterator = view.get_iter_at_location(x, y)
        if not found:
            return False
        target = self.link_at(iterator.get_offset())
        if target is None:
            # External links are not note targets. Open those directly.
            return self._open_external(view, iterator.get_offset())
        self.pane.window.follow_link(self.pane.note.path, target)
        return True

    def _open_external(self, view, offset):
        if any(span.style == 'code' and span.start <= offset < span.end for span in self.pane.spans):
            return False
        text = self.pane.text()
        pattern = r'!?\[\[([^\]\n]+)\]\]|!?\[[^\]\n]*\]\(([^)\n]+)\)'
        for match in re.finditer(pattern, text):
            if not (match.start() <= offset < match.end()):
                continue
            if not any(span.style == 'link' and span.start < match.end()
                       and span.end > match.start() for span in self.pane.spans):
                continue
            target = (match.group(1) or match.group(2)).split('|', 1)[0]
            if match.group(2):
                result = parseLinkDestination(text, match.start(2), len(text))
                if not result.ok:
                    continue
                target = result.str
            url = urlsplit(target)
            if url.scheme in ('http', 'https', 'mailto'):
                Gtk.show_uri_on_window(self.pane.window, target, Gdk.CURRENT_TIME)
                return True
        return False
