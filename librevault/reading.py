"""Native GTK reading view for Markdown, local media and note transclusions."""
from io import BytesIO
from pathlib import Path
import re
import sys
from urllib.parse import quote

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Gio, Pango
from markdown_it import MarkdownIt
from markdown_it.rules_inline.state_inline import Delimiter

from .anchors import conceal_block_id, extract_subpath, image_alt_size, split_transclusion_target
from .attachments import AUDIO_SUFFIXES, MEDIA_SUFFIXES, local_path
from .appearance import PALETTES
from .formatting import enable_safe_html, obsidian_comment_ranges
from .math_render import render_math_png
from .mermaid import render_mermaid
from .preferences import MIN_FONT_SIZE
from .navigation_ui import LinkPreview, note_link_from_uri
from .syntax import code_markup
from .vault import VaultError, fingerprint, wiki_rule


def _highlight_tokenize(state, silent):
    """Mark ==highlight== pairs the same way MarkdownIt marks strikethrough."""
    if silent or state.src[state.pos] != '=':
        return False
    scanned = state.scanDelims(state.pos, True)
    length = scanned.length
    if length < 2:
        return False
    if length % 2:
        token = state.push('text', '', 0)
        token.content = '='
        length -= 1
    index = 0
    while index < length:
        token = state.push('text', '', 0)
        token.content = '=='
        state.delimiters.append(Delimiter(
            marker=ord('='), length=0, token=len(state.tokens) - 1, end=-1,
            open=scanned.can_open, close=scanned.can_close))
        index += 2
    state.pos += scanned.length
    return True


def _highlight_pairs(state, delimiters):
    lone = []
    index = 0
    while index < len(delimiters):
        opener = delimiters[index]
        if opener.marker != ord('=') or opener.end == -1:
            index += 1
            continue
        closer = delimiters[opener.end]
        for token, nesting, kind in (
                (state.tokens[opener.token], 1, 'highlight_open'),
                (state.tokens[closer.token], -1, 'highlight_close')):
            token.type = kind
            token.tag = 'span'
            token.nesting = nesting
            token.markup = '=='
            token.content = ''
        if (closer.token > 0 and state.tokens[closer.token - 1].type == 'text'
                and state.tokens[closer.token - 1].content == '='):
            lone.append(closer.token - 1)
        index += 1
    while lone:
        current = lone.pop()
        following = current + 1
        while following < len(state.tokens) and state.tokens[following].type == 'highlight_close':
            following += 1
        following -= 1
        if current != following:
            state.tokens[current], state.tokens[following] = (
                state.tokens[following], state.tokens[current])


def _highlight_post(state):
    _highlight_pairs(state, state.delimiters)
    for meta in state.tokens_meta:
        if meta and 'delimiters' in meta:
            _highlight_pairs(state, meta['delimiters'])


MARKDOWN = MarkdownIt('commonmark', {'html': False}).enable('table').enable('strikethrough')
MARKDOWN.inline.ruler.before('emphasis', 'highlight', _highlight_tokenize)
MARKDOWN.inline.ruler2.before('emphasis', 'highlight', _highlight_post)
MARKDOWN.inline.ruler.before('link', 'wikilink', wiki_rule)
enable_safe_html(MARKDOWN)

CALLOUT_RE = re.compile(r'^\[!([^\]\n]+)\]([+-])?[ \t]*(.*)$')
CALLOUT_STYLES = {
    'note': ('📝', 'callout-note'),
    'info': ('ℹ', 'callout-info'),
    'tip': ('✦', 'callout-tip'),
    'warning': ('⚠', 'callout-warning'),
    'danger': ('⛔', 'callout-danger'),
    'example': ('✎', 'callout-example'),
    'quote': ('❝', 'callout-quote'),
    'todo': ('☑', 'callout-todo'),
    'bug': ('🐞', 'callout-bug'),
}
CALLOUT_ALIASES = {
    'hint': 'tip', 'important': 'tip',
    'caution': 'warning', 'attention': 'warning',
    'error': 'danger', 'failure': 'danger', 'fail': 'danger',
    'question': 'info', 'faq': 'info', 'help': 'info',
    'abstract': 'example', 'summary': 'example', 'tldr': 'example',
    'cite': 'quote',
    'check': 'todo', 'done': 'todo', 'success': 'todo',
}
_RICH_TEXT = re.compile(r'\$\$[^$]+\$\$|\$[^$\n]+\$|\[\^[^\]]+\]|\^\[[^\]]+\]')


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


def _block_id_paragraph(children):
    """True when a paragraph is only an Obsidian block id line."""
    if not children or any(child.type not in ('text', 'softbreak', 'hardbreak') for child in children):
        return False
    plain = ''.join(child.content for child in children if child.type == 'text').strip()
    return bool(re.fullmatch(r'\^[A-Za-z0-9-]+', plain))


def strip_obsidian_comments(source):
    """Drop %%comments%% outside fenced code, indented code, and inline code."""
    ranges = obsidian_comment_ranges(source)
    if not ranges:
        return source
    parts, cursor = [], 0
    for start, end in ranges:
        if start < cursor:
            continue
        parts.append(source[cursor:start])
        cursor = end
    parts.append(source[cursor:])
    return ''.join(parts)


def reading_source(source):
    """Keep footnote definitions outside the main block stream."""
    lines, definitions = [], []
    if source.startswith('---\n'):
        end = re.search(r'^\s*(?:---|\.\.\.)\s*$', source[4:], re.M)
        if end:
            source = source[4 + end.end():].lstrip('\n')
    source = strip_obsidian_comments(source)
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


def scaled_pixbuf(path, width, height, fallback):
    """Scale an image to an explicit Obsidian size, or use ``fallback``.

    ``|300`` keeps the aspect ratio. ``|300x200`` uses both dimensions.
    ``fallback`` preserves each caller's existing default box.
    """
    if not width and not height:
        return fallback()
    info = GdkPixbuf.Pixbuf.get_file_info(str(path))
    if not info or not info[1] or not info[2]:
        raise ValueError('Not an image.')
    pixel_width, pixel_height = info[1], info[2]
    if width and height:
        target_width, target_height, preserve = width, height, False
    elif width:
        target_width = width
        target_height = max(1, round(pixel_height * width / pixel_width))
        preserve = True
    else:
        target_height = height
        target_width = max(1, round(pixel_width * height / pixel_height))
        preserve = True
    return GdkPixbuf.Pixbuf.new_from_file_at_scale(
        str(path), max(1, target_width), max(1, target_height), preserve)


def pixbuf_bytes(data):
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    return loader.get_pixbuf()


def format_clock(seconds):
    """Format a media position. Unknown durations stay as --:--."""
    if seconds is None:
        return '--:--'
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def _wav_duration(path):
    import wave
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        if not rate:
            return None
        return handle.getnframes() / float(rate)


def probe_duration(path):
    """Best-effort duration in seconds. Missing metadata returns None."""
    import wave
    if path.suffix.lower() == '.wav':
        try:
            return _wav_duration(path)
        except (EOFError, OSError, wave.Error):
            pass
    try:
        if path.stat().st_size < 64:
            return None
    except OSError:
        return None
    try:
        gi.require_version('Gst', '1.0')
        gi.require_version('GstPbutils', '1.0')
        from gi.repository import Gst, GstPbutils
        Gst.init(None)
        discoverer = GstPbutils.Discoverer.new(Gst.SECOND)
        info = discoverer.discover_uri(path.as_uri())
        duration = info.get_duration()
        if duration is None or duration <= 0 or duration >= 2 ** 62:
            return None
        return duration / Gst.SECOND
    except (ImportError, ValueError, OSError, GLib.Error):
        return None


class MediaCard(Gtk.Box):
    """Local audio or video with metadata, a clock, and playback or an external opener."""

    def __init__(self, path, on_open):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.path = path
        self.on_open = on_open
        self.audio = path.suffix.lower() in AUDIO_SUFFIXES
        self.duration = probe_duration(path)
        self.position = 0.0
        self.playing = False
        self.pipeline = None
        self._gst = None
        self._tick = None
        self.get_style_context().add_class('media-card')
        self.get_style_context().add_class('media-audio' if self.audio else 'media-video')
        self.set_border_width(10)
        title = Gtk.Label(label=path.name, xalign=0)
        title.get_style_context().add_class('media-title')
        title.set_selectable(True)
        self.pack_start(title, False, False, 0)
        kind = 'Audio' if self.audio else 'Video'
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        meta = Gtk.Label(label=f'{kind} · {path.suffix.lower().lstrip(".")} · {size} bytes', xalign=0)
        meta.get_style_context().add_class('media-meta')
        self.pack_start(meta, False, False, 0)
        controls = Gtk.Box(spacing=8)
        self.play = Gtk.Button(label='Play')
        self.play.get_accessible().set_name('Play')
        self.play.connect('clicked', self.toggle_playback)
        controls.pack_start(self.play, False, False, 0)
        self.time = Gtk.Label(label=self.time_text(), xalign=0)
        self.time.get_style_context().add_class('media-time')
        self.time.get_accessible().set_name('Playback time')
        controls.pack_start(self.time, False, False, 0)
        external = Gtk.Button(label='Open externally')
        external.connect('clicked', lambda *_: self.on_open())
        controls.pack_end(external, False, False, 0)
        self.pack_start(controls, False, False, 0)
        self.connect('destroy', lambda *_: self.shutdown())

    def time_text(self):
        return f'{format_clock(self.position)} / {format_clock(self.duration)}'

    def _refresh_time(self):
        self.time.set_text(self.time_text())

    def toggle_playback(self, *_):
        if not self.audio:
            self.on_open()
            return
        try:
            self._toggle_audio()
        except (ImportError, ValueError, RuntimeError, OSError, GLib.Error):
            self.shutdown()
            self.play.set_label('Play')
            self.play.get_accessible().set_name('Play')
            self.playing = False
            self.on_open()

    def _toggle_audio(self):
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        if self.pipeline is None:
            pipeline = Gst.ElementFactory.make('playbin', None)
            if pipeline is None:
                raise RuntimeError('Audio playback is unavailable')
            pipeline.set_property('uri', self.path.as_uri())
            pipeline.set_property('flags', 0x02)
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self._on_bus)
            self.pipeline = pipeline
            self._gst = Gst
        if self.playing:
            self.pipeline.set_state(self._gst.State.PAUSED)
            self.playing = False
            self.play.set_label('Play')
            self.play.get_accessible().set_name('Play')
            self._stop_ticks()
            return
        self.pipeline.set_state(self._gst.State.PLAYING)
        self.playing = True
        self.play.set_label('Pause')
        self.play.get_accessible().set_name('Pause')
        if self._tick is None:
            self._tick = GLib.timeout_add(200, self._tick_playback)

    def _on_bus(self, _bus, message):
        gst = self._gst
        if gst is None:
            return
        if message.type == gst.MessageType.EOS:
            if self.pipeline is not None:
                self.pipeline.set_state(gst.State.NULL)
            self.pipeline = None
            self.playing = False
            self.position = 0
            self.play.set_label('Play')
            self.play.get_accessible().set_name('Play')
            self._stop_ticks()
            self._refresh_time()
        elif message.type == gst.MessageType.ERROR:
            self.shutdown()
            self.play.set_label('Play')
            self.play.get_accessible().set_name('Play')

    def _tick_playback(self):
        if not self.playing or self.pipeline is None or self._gst is None:
            self._tick = None
            return False
        ok, position = self.pipeline.query_position(self._gst.Format.TIME)
        if ok and position >= 0:
            self.position = position / self._gst.SECOND
        ok, duration = self.pipeline.query_duration(self._gst.Format.TIME)
        if ok and 0 < duration < 2 ** 62:
            self.duration = duration / self._gst.SECOND
        self._refresh_time()
        return True

    def _stop_ticks(self):
        if self._tick is not None:
            GLib.source_remove(self._tick)
            self._tick = None

    def shutdown(self):
        self._stop_ticks()
        self.playing = False
        pipeline, gst = self.pipeline, self._gst
        self.pipeline = None
        if pipeline is not None and gst is not None:
            pipeline.set_state(gst.State.NULL)


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
        scale = min(1.5, 680 / max(width, 1), 900 / max(height, 1))
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
    MAX_EMBEDS = 32
    MAX_EMBED_BYTES = 4 * 1024 * 1024

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
        self.link_preview = LinkPreview()
        self.add_events(Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect('leave-notify-event', self.preview_leave)
        self.link_targets = {}
        self._footnote_context = []
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
                              fingerprint(opened.text().encode('utf-8')) if opened is not None else None))
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
        self.link_preview.hide()
        for child in self.body.get_children():
            child.destroy()
        self.source = None
        self.references.clear()
        self.dependency_state = None
        self.link_targets.clear()
        self._footnote_context.clear()

    def refresh(self, force=False):
        source = self.pane.text()
        if source == self.source and self.dependencies() == self.dependency_state and not force:
            return
        self.link_preview.hide()
        self.source = source
        self.references.clear()
        self.link_targets.clear()
        for child in self.body.get_children():
            child.destroy()
        self.embed_count = 0
        self.embed_bytes = 0
        self.embed_limit_shown = False
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
        if markup and '<a href=' in text:
            label.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
            label.connect('motion-notify-event', self.preview_motion)
            label.connect('leave-notify-event', self.preview_leave)
        parent.pack_start(label, False, False, 0)
        return label

    def preview_motion(self, label, event):
        parsed = note_link_from_uri(label.get_current_uri())
        if not parsed:
            self.link_preview.hide()
            return False
        rectangle = Gdk.Rectangle()
        rectangle.x, rectangle.y = int(event.x), int(event.y)
        rectangle.width, rectangle.height = 1, 1
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        self.link_preview.consider(label, parsed[0], parsed[1], ctrl, rectangle, self.pane.window)
        return False

    def preview_leave(self, *_):
        self.link_preview.hide()
        return False

    def activate_link(self, _, target):
        if target.startswith('footnote:') or target.startswith('footnoteback:'):
            self.scroll_to_widget(self.link_targets.get(target))
            return True
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

    def scroll_to_widget(self, widget):
        if widget is None or widget.get_parent() is None:
            return
        coords = widget.translate_coordinates(self.body, 0, 0)
        if coords is None:
            return
        adjustment = self.get_vadjustment()
        limit = max(0, adjustment.get_upper() - adjustment.get_page_size())
        adjustment.set_value(max(0, min(coords[1], limit)))

    def open_asset(self, target):
        try:
            path = local_path(self.pane.window.vault, self.pane.note.path, target)
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)
        except (OSError, GLib.Error, VaultError) as error:
            self.pane.window.error(error)

    def inline(self, parent, children, source, visited, expanded_self=False):
        pieces = []
        opening, closing = [], []
        pending = []
        context = self._footnote_context[-1] if self._footnote_context else None

        def flush():
            if not pieces:
                return
            label = self.label(parent, ''.join(pieces) + ''.join('</' + tag + '>' for tag in reversed(closing)), True)
            if pending:
                label.set_tooltip_text('\n\n'.join(text for _key, text in pending))
                label.get_style_context().add_class('footnote-ref')
                if context is not None:
                    for key, _text in pending:
                        context['refs'].setdefault(key, label)
                pending.clear()
            pieces.clear()
            pieces.extend(opening)

        for token in children or []:
            kind = token.type
            if kind in ('text', 'code_inline'):
                text = conceal_block_id(token.content) if kind == 'text' else token.content
                if kind == 'code_inline':
                    pieces.append('<tt>' + GLib.markup_escape_text(text) + '</tt>')
                    continue
                # Math stays a separate image widget, avoiding altered source.
                position = 0
                for match in _RICH_TEXT.finditer(text):
                    if match.start() > position:
                        pieces.append(GLib.markup_escape_text(text[position:match.start()]))
                    token_text = match.group()
                    if token_text.startswith('$'):
                        flush()
                        self.math(parent, token_text.strip('$'))
                    elif token_text.startswith('[^'):
                        self._append_footnote(pieces, pending, context, token_text[2:-1])
                    else:
                        key = self._add_inline_footnote(context, token_text[2:-1])
                        self._append_footnote(pieces, pending, context, key)
                    position = match.end()
                if position < len(text):
                    pieces.append(GLib.markup_escape_text(text[position:]))
            elif kind in ('softbreak', 'hardbreak', 'html_break'):
                pieces.append('\n')
            elif kind in ('strong_open', 'em_open', 's_open', 'highlight_open'):
                if kind == 'highlight_open':
                    colors = PALETTES[self.pane.window.preferences.theme]
                    value = (f'<span background="{colors["highlight_bg"]}" '
                             f'foreground="{colors["highlight_fg"]}">')
                    pieces.append(value)
                    opening.append(value)
                    closing.append('span')
                else:
                    tag = {'strong_open': 'b', 'em_open': 'i', 's_open': 's'}[kind]
                    pieces.append('<' + tag + '>')
                    opening.append('<' + tag + '>')
                    closing.append(tag)
            elif kind in ('strong_close', 'em_close', 's_close', 'highlight_close'):
                if closing:
                    pieces.append('</' + closing.pop() + '>')
                    opening.pop()
            elif kind == 'html_open':
                value, tag = self._html_markup(token.tag)
                pieces.append(value)
                opening.append(value)
                closing.append(tag)
            elif kind == 'html_close':
                tag = 'span' if token.tag in ('mark', 'kbd', 'sub', 'sup') else token.tag
                if closing and closing[-1] == tag:
                    pieces.append('</' + closing.pop() + '>')
                    opening.pop()
                else:
                    pieces.append(GLib.markup_escape_text(token.content or '</' + token.tag + '>'))
            elif kind == 'link_open':
                target = token.attrGet('href') or ''
                if not re.match(r'^(https?://|mailto:)', target):
                    target = 'note:' + quote(source, safe='') + '|' + quote(target, safe='')
                value = '<a href="' + GLib.markup_escape_text(target) + '">'
                pieces.append(value)
                opening.append(value)
                closing.append('a')
            elif kind == 'link_close':
                if closing:
                    pieces.append('</a>')
                    closing.pop()
                    opening.pop()
            elif kind == 'wikilink':
                target = token.meta['target']
                if token.meta['embedded']:
                    flush()
                    alias = token.content
                    if alias != target:
                        target = f'{target}|{alias}'
                    self.embed(parent, target, source, visited, expanded_self)
                else:
                    href = 'note:' + quote(source, safe='') + '|' + quote(target, safe='')
                    pieces.append('<a href="' + GLib.markup_escape_text(href) + '">' + GLib.markup_escape_text(token.content) + '</a>')
            elif kind == 'image':
                flush()
                src = token.attrGet('src') or ''
                width, height = image_alt_size(token.content or '')
                if width:
                    src = f'{src}|{width}' + (f'x{height}' if height else '')
                self.embed(parent, src, source, visited, expanded_self)
        flush()

    def code_block(self, parent, token):
        language = (token.info or '').split(None, 1)[0] if token.info else ''
        displayed = token.content[:-1] if token.content.endswith('\n') else token.content
        colors = PALETTES[self.pane.window.preferences.theme]
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        frame.get_style_context().add_class('reading-code')
        header = Gtk.Box(spacing=8)
        header.get_style_context().add_class('reading-code-header')
        badge = Gtk.Label(label=language or 'text', xalign=0)
        badge.get_style_context().add_class('reading-code-badge')
        badge.get_accessible().set_name(f'Code language {language or "text"}')
        header.pack_start(badge, False, False, 0)
        copy = Gtk.Button(label='Copy code')
        copy.set_halign(Gtk.Align.END)
        copy.get_accessible().set_name('Copy code')
        copy.connect('clicked', lambda _button, text=displayed: self.copy_code(text))
        header.pack_end(copy, False, False, 0)
        frame.pack_start(header, False, False, 0)
        label = self.label(frame, '<tt>' + code_markup(displayed, language, colors) + '</tt>', True)
        label.set_line_wrap(False)
        label.get_style_context().add_class('reading-code-body')
        parent.pack_start(frame, False, False, 0)

    def copy_code(self, text):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        if not self.pane.window.in_destruction():
            self.pane.window.status.set_text('Copied code')

    def _html_markup(self, tag):
        colors = PALETTES[self.pane.window.preferences.theme]
        if tag == 'mark':
            if self.pane.window.preferences.theme == 'light':
                return '<span background="#ffe08a" foreground="#1a1400">', 'span'
            return f'<span background="#6b5420" foreground="{colors["bright"]}">', 'span'
        if tag in ('sub', 'sup'):
            size = round(max(MIN_FONT_SIZE, self.pane.window.preferences.editor_size * 0.8) * Pango.SCALE)
            rise = -3500 if tag == 'sub' else 5000
            return f'<span rise="{rise}" size="{size}">', 'span'
        if tag == 'kbd':
            return (f'<span font_family="monospace" background="{colors["code_bg"]}" '
                    f'foreground="{colors["ink"]}">', 'span')
        return f'<{tag}>', tag

    def _append_footnote(self, pieces, pending, context, key):
        superscript, _ = self._html_markup('sup')
        if context is None:
            pieces.append(superscript + '[' + GLib.markup_escape_text(key) + ']</span>')
            return
        if key not in context['definitions']:
            context['definitions'][key] = 'Missing footnote.'
            context['order'].append(key)
        definition = context['definitions'][key]
        href = 'footnote:' + quote(context['prefix'] + ':' + key, safe='')
        pieces.append('<a href="' + GLib.markup_escape_text(href) + '">' + superscript + '['
                      + GLib.markup_escape_text(key) + ']</span></a>')
        pending.append((key, definition))

    def _add_inline_footnote(self, context, text):
        if context is None:
            return 'i1'
        number = 1
        while f'i{number}' in context['definitions']:
            number += 1
        key = f'i{number}'
        context['definitions'][key] = text
        context['order'].append(key)
        return key

    def math(self, parent, expression):
        try:
            color = PALETTES[self.pane.window.preferences.theme]['bright']
            image = Gtk.Image.new_from_pixbuf(pixbuf_bytes(render_math_png(expression, color)))
            image.set_halign(Gtk.Align.START)
            image.set_tooltip_text(expression)
            parent.pack_start(image, False, False, 0)
        except (ImportError, ValueError, RuntimeError, OSError) as error:
            self.label(parent, f'Math: {expression}\nCould not render this expression: {error}')

    def diagram(self, parent, source):
        try:
            colors = PALETTES[self.pane.window.preferences.theme]
            rendered = render_mermaid(source, colors)
            image = Gtk.Image.new_from_pixbuf(pixbuf_bytes(rendered.png))
            image.set_halign(Gtk.Align.START)
            image.get_style_context().add_class('mermaid-diagram')
            image.get_style_context().add_class('mermaid-' + rendered.kind)
            image.set_tooltip_text(rendered.kind)
            parent.pack_start(image, False, False, 0)
        except (OSError, ValueError, RuntimeError) as error:
            self.label(parent, f'Diagram:\n{source}\nCould not render this diagram: {error}')

    def embed(self, parent, target, source, visited, expanded_self=False):
        if self.embed_count >= self.MAX_EMBEDS:
            if not self.embed_limit_shown:
                self.label(parent, 'Preview limit reached. Open embedded files separately to view more.')
                self.embed_limit_shown = True
            return
        self.embed_count += 1
        path, fragment, width, height = split_transclusion_target(target)
        reference = path + (f'#{fragment}' if fragment else '')
        vault = self.pane.window.vault
        self.references.add((source, reference))
        candidates = vault.resolve(source, path)
        if len(candidates) == 1:
            relative = candidates[0]
            # A section of the note that contains the embed is allowed once.
            # Embedding that same note again, or a chain of five notes, is a cycle.
            self_section = bool(fragment) and relative == source
            if (relative in visited and not (self_section and not expanded_self)) or len(visited) >= 5:
                self.label(parent, f'Embedded note cycle: {relative}')
                return
            frame = Gtk.Frame(label=relative if not fragment else f'{relative}#{fragment}')
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content.set_border_width(12)
            frame.add(content)
            parent.pack_start(frame, False, False, 0)
            try:
                opened = self.pane.window.panes.get(relative)
                remaining = self.MAX_EMBED_BYTES - self.embed_bytes
                if opened is None and vault._path(relative).stat().st_size > remaining:
                    self.label(content, 'Note exceeds the preview budget. Open it separately to read it.')
                    return
                text = opened.text() if opened is not None else vault.read(relative).text
                size = sys.getsizeof(text)
                if size > remaining:
                    self.label(content, 'Note exceeds the preview budget. Open it separately to read it.')
                    return
                self.embed_bytes += size
                if fragment:
                    section = extract_subpath(text, fragment)
                    if section is None:
                        self.label(content, f'Heading or block not found: #{fragment}')
                        return
                    text = section
                self.blocks(content, text, relative, visited | {relative}, expanded_self or self_section)
            except (OSError, VaultError) as error:
                self.label(content, str(error))
            return
        try:
            asset = local_path(vault, source, path)
            suffix = asset.suffix.lower()
            if suffix in MEDIA_SUFFIXES:
                parent.pack_start(MediaCard(asset, lambda asset=asset: self._open_path(asset)), False, False, 0)
                return
            if suffix == '.pdf':
                try:
                    parent.pack_start(PdfView(asset), False, False, 0)
                except (ImportError, ValueError):
                    self.label(parent, 'PDF preview needs Poppler. Open the PDF with its system application below.')
            else:
                pixbuf = scaled_pixbuf(asset, width, height, lambda: GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(asset), 680, 520, True))
                image = Gtk.Image.new_from_pixbuf(pixbuf)
                image.set_halign(Gtk.Align.START)
                parent.pack_start(image, False, False, 0)
            button = Gtk.Button(label='Open ' + asset.name)
            button.set_halign(Gtk.Align.START)
            button.connect('clicked', lambda *_: self.pane.window.try_action(
                lambda: Gio.AppInfo.launch_default_for_uri(asset.as_uri(), None)))
            parent.pack_start(button, False, False, 0)
        except (OSError, GLib.Error, VaultError, ValueError) as error:
            self.label(parent, str(error))

    def _open_path(self, path):
        self.pane.window.try_action(lambda: Gio.AppInfo.launch_default_for_uri(path.as_uri(), None))

    def _install_callout(self, frame, box, matched):
        raw = matched.group(1).strip()
        kind = CALLOUT_ALIASES.get(raw.lower(), raw.lower())
        icon, css = CALLOUT_STYLES.get(kind, ('•', 'callout-generic'))
        title = matched.group(3).strip() or raw.title()
        header = Gtk.Box(spacing=8)
        icon_label = Gtk.Label(label=icon)
        icon_label.get_style_context().add_class('callout-icon')
        icon_label.get_accessible().set_name(f'{kind} callout')
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.get_style_context().add_class('callout-title')
        attributes = Pango.AttrList()
        attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
        title_label.set_attributes(attributes)
        header.pack_start(icon_label, False, False, 0)
        header.pack_start(title_label, False, False, 0)
        expander = Gtk.Expander()
        expander.set_expanded(matched.group(2) != '-')
        expander.set_label_widget(header)
        expander.get_accessible().set_name(title)
        style = frame.get_style_context()
        style.add_class('callout')
        style.add_class(css)
        frame.set_shadow_type(Gtk.ShadowType.NONE)
        frame.remove(box)
        expander.add(box)
        frame.add(expander)

    def _render_footnotes(self, parent, context):
        if not context['order']:
            return
        parent.pack_start(Gtk.Separator(), False, False, 0)
        for key in context['order']:
            content = context['definitions'].get(key, 'Missing footnote.')
            row = Gtk.Box(spacing=8)
            row.get_style_context().add_class('footnote-definition')
            back_href = 'footnoteback:' + quote(context['prefix'] + ':' + key, safe='')
            self.link_targets[back_href] = context['refs'].get(key)
            back = self.label(row, f'<a href="{GLib.markup_escape_text(back_href)}">↩</a>', True)
            back.get_style_context().add_class('footnote-back')
            back.set_tooltip_text('Back to reference')
            self.label(row, f'[{key}] {content}')
            parent.pack_start(row, False, False, 0)
            self.link_targets['footnote:' + quote(context['prefix'] + ':' + key, safe='')] = row

    def blocks(self, parent, text, source, visited, expanded_self=False):
        text, footnotes = reading_source(text)
        context = {
            'prefix': f'{source}:{len(self.link_targets)}:{len(self._footnote_context)}',
            'definitions': {},
            'order': [],
            'refs': {},
        }
        for key, content in footnotes:
            if key not in context['definitions']:
                context['definitions'][key] = content
                context['order'].append(key)
        self._footnote_context.append(context)
        try:
            self._render_blocks(parent, text, source, visited, expanded_self)
            self._render_footnotes(parent, context)
        finally:
            self._footnote_context.pop()

    def _render_blocks(self, parent, text, source, visited, expanded_self=False):
        tokens = MARKDOWN.parse(text)
        containers = [parent]
        metas = [None]
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
                if metas[-1] and metas[-1].get('callout') == 'pending':
                    metas[-1]['callout'] = 'done'
                frame = Gtk.Frame()
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                box.set_border_width(10)
                frame.add(box)
                containers[-1].pack_start(frame, False, False, 0)
                containers.append(box)
                if kind == 'blockquote_open':
                    metas.append({'callout': 'pending', 'frame': frame, 'box': box})
                else:
                    metas.append(None)
            elif kind in ('blockquote_close', 'list_item_close'):
                containers.pop()
                metas.pop()
            elif kind == 'heading_open':
                heading = int(token.tag[1:])
            elif kind == 'heading_close':
                heading = None
            elif kind == 'inline':
                destination = cell if cell is not None else containers[-1]
                children = list(token.children or [])
                if not heading and _block_id_paragraph(children):
                    continue
                if heading:
                    previous = set(destination.get_children())
                    self.inline(destination, children, source, visited, expanded_self)
                    for child in destination.get_children():
                        if child not in previous and isinstance(child, Gtk.Label):
                            attributes = Pango.AttrList()
                            attributes.insert(Pango.attr_size_new((25 - heading * 2) * 1024))
                            attributes.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
                            child.set_attributes(attributes)
                else:
                    meta = metas[-1]
                    if meta and meta.get('callout') == 'pending':
                        meta['callout'] = 'done'
                        if children and children[0].type == 'text':
                            matched = CALLOUT_RE.match(children[0].content.lstrip())
                            if matched:
                                children[0].content = ''
                                self._install_callout(meta['frame'], meta['box'], matched)
                    self.inline(destination, children, source, visited, expanded_self)
            elif kind in ('fence', 'code_block'):
                info = token.info.strip() if getattr(token, 'info', None) else ''
                language = info.split(None, 1)[0].lower() if info else ''
                if language == 'mermaid':
                    self.diagram(containers[-1], token.content)
                elif language in ('math', 'latex'):
                    self.math(containers[-1], token.content.strip())
                else:
                    self.code_block(containers[-1], token)
            elif kind == 'math_block':
                self.math(containers[-1], token.content.strip())
            elif kind == 'hr':
                containers[-1].pack_start(Gtk.Separator(), False, False, 0)
