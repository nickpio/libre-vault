"""Native GTK reading view for Markdown, local media and note transclusions."""
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import quote

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GdkPixbuf, GLib, Gio, Pango
from markdown_it import MarkdownIt

from .attachments import local_path
from .appearance import PALETTES
from .vault import VaultError, wiki_rule


MARKDOWN = MarkdownIt('commonmark', {'html': False}).enable('table').enable('strikethrough')
MARKDOWN.inline.ruler.before('link', 'wikilink', wiki_rule)


def dollar_math(state, start, end, silent):
    begin = state.bMarks[start] + state.tShift[start]
    first = state.src[begin:state.eMarks[start]].strip()
    if first != '$$':
        return False
    stop = start + 1
    while stop < end:
        content = state.src[state.bMarks[stop] + state.tShift[stop]:state.eMarks[stop]].strip()
        if content == '$$':
            break
        stop += 1
    if stop == end:
        return False
    if silent:
        return True
    token = state.push('math_block', '', 0)
    token.block = True
    token.content = state.getLines(start + 1, stop, state.blkIndent, False)
    token.map = [start, stop + 1]
    state.line = stop + 1
    return True


MARKDOWN.block.ruler.before('fence', 'dollar_math', dollar_math,
                            {'alt': ['paragraph', 'reference', 'blockquote', 'list']})


def reading_source(source):
    """Keep footnote definitions outside the main block stream."""
    lines, definitions = [], []
    if source.startswith('---\n'):
        end = re.search(r'^\s*(?:---|\.\.\.)\s*$', source[4:], re.M)
        if end:
            source = source[4 + end.end():].lstrip('\n')
    fenced = False
    continuing = False
    for line in source.splitlines():
        if re.match(r'^\s*(```|~~~)', line):
            fenced = not fenced
        match = re.match(r'^\[\^([^]]+)\]:\s*(.*)', line) if not fenced else None
        if match:
            definitions.append((match[1], match[2]))
            continuing = True
        elif continuing and line.startswith(('    ', '\t')):
            key, text = definitions[-1]
            definitions[-1] = (key, text + '\n' + line.lstrip())
        else:
            continuing = False
            lines.append(line)
    return '\n'.join(lines), definitions


def pixbuf_bytes(data):
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    return loader.get_pixbuf()


class PdfView(Gtk.Box):
    """One bounded-resolution PDF page, with native page controls."""
    def __init__(self, path):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        gi.require_version('Poppler', '0.18')
        from gi.repository import Poppler
        self.document = Poppler.Document.new_from_file(path.as_uri(), None)
        self.page = 0
        bar = Gtk.Box(spacing=8)
        for title, delta in [('Previous', -1), ('Next', 1)]:
            button = Gtk.Button(label=title)
            button.connect('clicked', lambda _, delta=delta: self.show_page(delta))
            bar.pack_start(button, False, False, 0)
        self.counter = Gtk.Label()
        bar.pack_start(self.counter, False, False, 0)
        self.pack_start(bar, False, False, 0)
        self.image = Gtk.Image()
        self.pack_start(self.image, False, False, 0)
        self.show_page(0)

    def show_page(self, delta):
        import cairo
        count = self.document.get_n_pages()
        if count == 0:
            return
        self.page = max(0, min(count - 1, self.page + delta))
        page = self.document.get_page(self.page)
        width, height = page.get_size()
        scale = min(1.5, 680 / max(width, 1))
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(width * scale)), max(1, int(height * scale)))
        context = cairo.Context(surface)
        context.set_source_rgb(1, 1, 1)
        context.paint()
        context.scale(scale, scale)
        page.render(context)
        output = BytesIO()
        surface.write_to_png(output)
        self.image.set_from_pixbuf(pixbuf_bytes(output.getvalue()))
        self.counter.set_text(f'{self.page + 1} / {count}')


class ReadingView(Gtk.ScrolledWindow):
    def __init__(self, pane):
        super().__init__()
        self.pane = pane
        self.get_style_context().add_class('reading-view')
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.body.set_border_width(32)
        self.add(self.body)
        self.source = None
        self.references = set()
        self.dependency_state = None
        self.connect('destroy', lambda *_: self.clear())

    def dependencies(self):
        """Compare rendered inputs without rebuilding stable GTK widgets."""
        vault = self.pane.window.vault
        state = [self.pane.note.path, self.pane.window.preferences.theme]
        for source, target in sorted(self.references):
            candidates = tuple(vault.resolve(source, target))
            if len(candidates) == 1:
                relative = candidates[0]
                try:
                    stat = vault._path(relative).stat()
                    disk = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                except (OSError, VaultError):
                    disk = None
                saved = vault.notes.get(relative)
                opened = self.pane.window.panes.get(relative)
                state.append((source, target, 'note', candidates, disk,
                              saved.revision if saved is not None else None,
                              opened.text() if opened is not None else None))
            else:
                try:
                    path = local_path(vault, source, target)
                    stat = path.stat()
                    asset = (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
                except (OSError, VaultError):
                    asset = None
                state.append((source, target, 'asset', candidates, asset))
        return tuple(state)

    def clear(self):
        for child in self.body.get_children():
            child.destroy()
        self.source = None
        self.references.clear()
        self.dependency_state = None

    def refresh(self, force=False):
        source = self.pane.text()
        if source == self.source and self.dependencies() == self.dependency_state and not force:
            return
        self.source = source
        self.references.clear()
        for child in self.body.get_children():
            child.destroy()
        self.blocks(self.body, source, self.pane.note.path, {self.pane.note.path})
        self.body.show_all()
        self.dependency_state = self.dependencies()

    def label(self, parent, text, markup=False):
        label = Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(2)
        label.set_max_width_chars(100)
        label.set_selectable(True)
        if markup:
            label.set_markup(text)
        else:
            label.set_text(text)
        label.connect('activate-link', self.activate_link)
        parent.pack_start(label, False, False, 0)
        return label

    def activate_link(self, _, target):
        if target.startswith('note:'):
            from urllib.parse import unquote
            source, _, destination = target[5:].partition('|')
            source, destination = unquote(source), unquote(destination)
            try:
                asset = local_path(self.pane.window.vault, source, destination)
            except VaultError:
                asset = None
            if asset is not None and asset.suffix.lower() != '.md':
                self.pane.window.try_action(lambda: Gio.AppInfo.launch_default_for_uri(asset.as_uri(), None))
            else:
                self.pane.window.follow_link(source, destination)
        elif target.startswith('asset:'):
            from urllib.parse import unquote
            self.open_asset(unquote(target[6:]))
        elif target.startswith(('https://', 'http://', 'mailto:')):
            self.pane.window.try_action(lambda: Gio.AppInfo.launch_default_for_uri(target, None))
        return True

    def open_asset(self, target):
        try:
            path = local_path(self.pane.window.vault, self.pane.note.path, target)
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except (OSError, GLib.Error, VaultError) as error:
            self.pane.window.error(error)

    def inline(self, parent, children, source, visited):
        pieces = []
        opening, closing = [], []
        def flush():
            if pieces:
                self.label(parent, ''.join(pieces) + ''.join('</' + tag + '>' for tag in reversed(closing)), True)
                pieces.clear()
                pieces.extend(opening)
        for token in children or []:
            kind = token.type
            if kind in ('text', 'code_inline'):
                text = token.content
                if kind == 'code_inline':
                    pieces.append('<tt>' + GLib.markup_escape_text(text) + '</tt>')
                    continue
                # Math stays a separate image widget, avoiding altered source.
                for part in re.split(r'(\$\$[^$]+\$\$|\$[^$\n]+\$)', text):
                    if part.startswith('$') and part.endswith('$') and len(part) > 2:
                        flush()
                        self.math(parent, part.strip('$'))
                    else:
                        escaped = GLib.markup_escape_text(part)
                        escaped = re.sub(r'\[\^([^]]+)\]', r'<sup>[\1]</sup>', escaped)
                        pieces.append(escaped)
            elif kind in ('softbreak', 'hardbreak'):
                pieces.append('\n')
            elif kind in ('strong_open', 'em_open', 's_open'):
                tag = {'strong_open': 'b', 'em_open': 'i', 's_open': 's'}[kind]
                pieces.append('<' + tag + '>')
                opening.append('<' + tag + '>')
                closing.append(tag)
            elif kind in ('strong_close', 'em_close', 's_close'):
                pieces.append('</' + closing.pop() + '>')
                opening.pop()
            elif kind == 'link_open':
                target = token.attrGet('href') or ''
                if not re.match(r'^(https?://|mailto:)', target):
                    target = 'note:' + quote(source, safe='') + '|' + quote(target, safe='')
                value = '<a href="' + GLib.markup_escape_text(target) + '">'
                pieces.append(value)
                opening.append(value)
                closing.append('a')
            elif kind == 'link_close':
                pieces.append('</a>')
                closing.pop()
                opening.pop()
            elif kind == 'wikilink':
                target = token.meta['target']
                if token.meta['embedded']:
                    flush()
                    self.embed(parent, target, source, visited)
                else:
                    href = 'note:' + quote(source, safe='') + '|' + quote(target, safe='')
                    pieces.append('<a href="' + GLib.markup_escape_text(href) + '">' + GLib.markup_escape_text(token.content) + '</a>')
            elif kind == 'image':
                flush()
                self.embed(parent, token.attrGet('src') or '', source, visited)
        flush()

    def math(self, parent, expression):
        try:
            from matplotlib import rc_context
            from matplotlib.mathtext import math_to_image
            output = BytesIO()
            color = PALETTES[self.pane.window.preferences.theme]['bright']
            with rc_context({'savefig.transparent': True}):
                math_to_image('$' + expression + '$', output, dpi=150, format='png', color=color)
            image = Gtk.Image.new_from_pixbuf(pixbuf_bytes(output.getvalue()))
            image.set_halign(Gtk.Align.START)
            image.set_tooltip_text(expression)
            parent.pack_start(image, False, False, 0)
        except (ImportError, ValueError, RuntimeError) as error:
            self.label(parent, f'Math: {expression}\nCould not render this expression: {error}')

    def embed(self, parent, target, source, visited):
        vault = self.pane.window.vault
        self.references.add((source, target))
        candidates = vault.resolve(source, target)
        if len(candidates) == 1:
            relative = candidates[0]
            if relative in visited or len(visited) >= 5:
                self.label(parent, f'Embedded note cycle: {relative}')
                return
            frame = Gtk.Frame(label=relative)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content.set_border_width(12)
            frame.add(content)
            parent.pack_start(frame, False, False, 0)
            try:
                note = vault.read(relative)
                text = self.pane.window.panes[relative].text() if relative in self.pane.window.panes else note.text
                self.blocks(content, text, relative, visited | {relative})
            except (OSError, VaultError) as error:
                self.label(content, str(error))
            return
        try:
            path = local_path(vault, source, target)
            if path.suffix.lower() == '.pdf':
                try:
                    parent.pack_start(PdfView(path), False, False, 0)
                except (ImportError, ValueError):
                    self.label(parent, 'PDF preview needs Poppler. Open the PDF with its system application below.')
            else:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 680, 520, True)
                image = Gtk.Image.new_from_pixbuf(pixbuf)
                image.set_halign(Gtk.Align.START)
                parent.pack_start(image, False, False, 0)
            button = Gtk.Button(label='Open ' + path.name)
            button.set_halign(Gtk.Align.START)
            button.connect('clicked', lambda *_: self.pane.window.try_action(
                lambda: Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)))
            parent.pack_start(button, False, False, 0)
        except (OSError, GLib.Error, VaultError) as error:
            self.label(parent, str(error))

    def blocks(self, parent, text, source, visited):
        text, footnotes = reading_source(text)
        tokens = MARKDOWN.parse(text)
        containers = [parent]
        grid, row, column, cell = None, -1, -1, None
        heading = None
        for index, token in enumerate(tokens):
            kind = token.type
            if kind == 'table_open':
                grid = Gtk.Grid(column_spacing=18, row_spacing=8)
                grid.get_style_context().add_class('reading-table')
                parent.pack_start(grid, False, False, 0)
                row = -1
            elif kind == 'table_close':
                grid = None
            elif kind == 'tr_open':
                row += 1
                column = -1
            elif kind in ('th_open', 'td_open'):
                column += 1
                cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                if kind == 'th_open':
                    cell.get_style_context().add_class('reading-table-heading')
                grid.attach(cell, column, row, 1, 1)
            elif kind in ('th_close', 'td_close'):
                cell = None
            elif kind in ('blockquote_open', 'list_item_open'):
                frame = Gtk.Frame()
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                box.set_border_width(10)
                frame.add(box)
                containers[-1].pack_start(frame, False, False, 0)
                containers.append(box)
            elif kind in ('blockquote_close', 'list_item_close'):
                containers.pop()
            elif kind == 'heading_open':
                heading = int(token.tag[1:])
            elif kind == 'heading_close':
                heading = None
            elif kind == 'inline':
                destination = cell if cell is not None else containers[-1]
                if heading:
                    previous = set(destination.get_children())
                    self.inline(destination, token.children, source, visited)
                    for child in destination.get_children():
                        if child not in previous and isinstance(child, Gtk.Label):
                            attributes = Pango.AttrList()
                            attributes.insert(Pango.attr_size_new((25 - heading * 2) * 1024))
                            attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
                            child.set_attributes(attributes)
                else:
                    children = token.children or []
                    if children and children[0].type == 'text':
                        callout = re.match(r'^\[!([^]]+)\][+-]?\s*(.*)', children[0].content)
                        if callout:
                            self.label(destination, '<b>' + GLib.markup_escape_text(callout[2] or callout[1].title()) + '</b>', True)
                            children[0].content = ''
                    self.inline(destination, children, source, visited)
            elif kind in ('fence', 'code_block'):
                if token.info.strip() in ('math', 'latex'):
                    self.math(containers[-1], token.content.strip())
                else:
                    label = self.label(containers[-1], '<tt>' + GLib.markup_escape_text(token.content.rstrip()) + '</tt>', True)
                    label.set_line_wrap(False)
            elif kind == 'math_block':
                self.math(containers[-1], token.content.strip())
            elif kind == 'hr':
                containers[-1].pack_start(Gtk.Separator(), False, False, 0)
        if footnotes:
            parent.pack_start(Gtk.Separator(), False, False, 0)
            for key, content in footnotes:
                self.label(parent, f'[{key}] {content}')
