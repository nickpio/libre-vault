"""Source-offset styles for live Markdown editing. Never rewrite note text.

Highlights, strikethrough, and Obsidian comments are recognized outside
fenced code, indented code, and inline code. Their markers are syntax spans
so live preview can conceal them away from the cursor.
"""
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from urllib.parse import unquote

from markdown_it.rules_inline import backticks, emphasis, link

from .anchors import image_alt_size, parse_image_size
from .markdown_parsers import new_parser
from .syntax import highlight_code
from .tables import find_tables
from .vault import wiki_rule

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tif', '.tiff'}
_IMAGE_SUFFIXES = IMAGE_SUFFIXES
CALLOUT_RE = re.compile(
    r'^[ \t]{0,3}(?:>[ \t]?)+[ \t]*\[!(?P<kind>[A-Za-z][A-Za-z0-9_-]*)\](?P<fold>[+-])?(?:[ \t]+(?P<title>[^\n]*))?$'
)
EMBED_RE = re.compile(r'^[ \t]*!\[\[(?P<body>[^\]\n]+)\]\][ \t]*$')


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    style: str


@dataclass(frozen=True)
class ListItem:
    start: int
    end: int
    marker_start: int
    content_start: int
    depth: int
    number: int | None
    task_offset: int | None
    checked: bool


@dataclass(frozen=True)
class Heading:
    start: int
    label: str
    level: int


@dataclass(frozen=True)
class Embed:
    start: int
    end: int
    target: str
    kind: str


@dataclass(frozen=True)
class MathSpan:
    start: int
    end: int
    expression: str
    display: bool


@dataclass(frozen=True)
class Callout:
    start: int
    end: int
    kind: str
    title: str
    header_start: int
    header_end: int


@dataclass(frozen=True)
class MarkdownLayout:
    spans: list[Span]
    items: list[ListItem]
    headings: list[Heading]
    images: list[tuple[int, int, str, int | None, int | None]]
    embeds: list[Embed] = field(default_factory=list)
    math: list[MathSpan] = field(default_factory=list)
    tables: list = field(default_factory=list)
    callouts: list[Callout] = field(default_factory=list)
    diagrams: list[tuple[int, int, str]] = field(default_factory=list)


_SAFE_HTML = re.compile(
    r'</(?P<close>u|mark|sub|sup|kbd)\s*>'
    r'|<(?P<open>u|mark|sub|sup|kbd)\s*>'
    r'|<(?P<break>br)\s*/?\s*>',
    re.IGNORECASE)
_HTML_STYLE = {'u': 'underline', 'mark': 'mark', 'sub': 'subscript', 'sup': 'superscript', 'kbd': 'kbd'}


def safe_html(state, silent):
    """Recognize a small set of inline tags without enabling raw HTML."""
    if state.pos >= state.posMax or state.src[state.pos] != '<':
        return False
    found = _SAFE_HTML.match(state.src, state.pos)
    if not found or found.start() != state.pos:
        return False
    end = found.end()
    if not silent:
        # Nesting stays 0. MarkdownIt treats -1 as a delimiter close and
        # crashes when a tag has no matching opener in the same paragraph.
        if found.group('break'):
            token = state.push('html_break', 'br', 0)
        elif found.group('open'):
            token = state.push('html_open', found.group('open').lower(), 0)
        else:
            token = state.push('html_close', found.group('close').lower(), 0)
        token.content = state.src[state.pos:end]
        token.meta['source'] = (state.pos, end)
    state.pos = end
    return True


def enable_safe_html(markdown):
    if 'safe_html' not in markdown.inline.ruler.get_all_rules():
        markdown.inline.ruler.before('html_inline', 'safe_html', safe_html)
    return markdown


def positioned(rule, kind):
    """Retain source positions on tokens before MarkdownIt's delimiter balancing."""
    def wrapped(state, silent):
        start, count = state.pos, len(state.tokens)
        label_end = -1
        if kind == 'link' and not silent and state.src[start:start + 1] == '[':
            label_end = state.md.helpers.parseLinkLabel(state, start, True)
        matched = rule(state, silent)
        if not matched or silent:
            return matched
        if kind == 'emphasis':
            for offset, token in enumerate(state.tokens[-(state.pos - start):]):
                token.meta['offset'] = start + offset
        else:
            token_type = {'code': 'code_inline', 'wiki': 'wikilink', 'link': 'link_open'}[kind]
            for token in state.tokens[count:]:
                if token.type == token_type:
                    token.meta['source'] = (start, state.pos)
                    if kind == 'link':
                        token.meta['label_end'] = label_end
                    break
        return matched
    return wrapped


MARKDOWN = new_parser()
MARKDOWN.inline.ruler.at('emphasis', positioned(emphasis.tokenize, 'emphasis'))
MARKDOWN.inline.ruler.at('backticks', positioned(backticks.backtick, 'code'))
MARKDOWN.inline.ruler.at('link', positioned(link, 'link'))
MARKDOWN.inline.ruler.before('link', 'wikilink', positioned(wiki_rule, 'wiki'))
enable_safe_html(MARKDOWN)


def inline_spans(text, base, env):
    spans, stack, code_ranges = [], [], []

    def add(start, end, style):
        if end > start:
            spans.append(Span(base + start, base + end, style))

    for token in MARKDOWN.parseInline(text, env)[0].children or []:
        if token.type in ('em_open', 'strong_open'):
            start = token.meta['offset'] - (len(token.markup) - 1)
            stack.append((start, start + len(token.markup), token.tag))
        elif token.type in ('em_close', 'strong_close'):
            expected = 'em' if token.type == 'em_close' else 'strong'
            if not stack or stack[-1][2] != expected:
                continue
            start, content, style = stack.pop()
            end = token.meta['offset']
            add(content, end, style)
            add(start, content, 'syntax')
            add(end, end + len(token.markup), 'syntax')
        elif token.type == 'html_open':
            start, end = token.meta['source']
            stack.append((start, end, _HTML_STYLE[token.tag]))
        elif token.type == 'html_close':
            end, close_end = token.meta['source']
            style = _HTML_STYLE.get(token.tag)
            if stack and stack[-1][2] == style:
                start, content, style = stack.pop()
                add(content, end, style)
                add(start, content, 'syntax')
            add(end, close_end, 'syntax')
        elif token.type == 'html_break':
            start, end = token.meta['source']
            add(start, end, 'syntax')
        elif token.type == 'code_inline':
            start, end = token.meta['source']
            size = len(token.markup)
            add(start + size, end - size, 'code')
            add(start, start + size, 'syntax')
            add(end - size, end, 'syntax')
            code_ranges.append((base + start, base + end))
        elif token.type == 'wikilink':
            start, end = token.meta['source']
            opening = start + (3 if token.meta['embedded'] else 2)
            alias = text.find('|', opening, end - 2)
            label = alias + 1 if alias >= 0 else opening
            add(label, end - 2, 'link')
            add(start, label, 'syntax')
            add(end - 2, end, 'syntax')
        elif token.type == 'link_open':
            # Autolinks come from a separate rule and have no label delimiters.
            if 'source' not in token.meta:
                continue
            start, end = token.meta['source']
            label_end = token.meta['label_end']
            add(start + 1, label_end, 'link')
            add(start, start + 1, 'syntax')
            add(label_end, end, 'syntax')
    return spans, code_ranges


def _standalone_image(token):
    """Return ``(target, width, height)`` for a lone image line, or None.

    Markdown images read ``![alt|300]`` and ``![alt|300x200]``. Wikilink
    embeds read ``![[image.png|300]]`` the same way. Note embeds are not
    images, even when they carry a display alias.
    """
    if token.type == 'image':
        width, height = image_alt_size(token.content or '')
        return token.attrGet('src') or '', width, height
    if token.type == 'wikilink' and token.meta.get('embedded'):
        target = token.meta.get('target') or ''
        width = height = None
        label = token.content or ''
        if label != target:
            size = parse_image_size(label.strip())
            if size is not None:
                width, height = size
        path = unquote(target.split('#', 1)[0].strip())
        if PurePosixPath(path).suffix.lower() not in _IMAGE_SUFFIXES:
            return None
        return path, width, height
    return None


def _covering_end(ranges, index):
    end = None
    for start, stop in ranges:
        if start <= index < stop and (end is None or stop > end):
            end = stop
    return end


def _contained(start, end, ranges):
    return any(bound_start <= start and end <= bound_end for bound_start, bound_end in ranges)


def _meaningful(content, marker):
    stripped = content.strip()
    return bool(stripped) and any(char not in marker for char in stripped)


def _find_closer(text, begin, limit, marker, blockers, skippable, allow_blank):
    """Return the closer index, or None when a code block or blank line interrupts it."""
    size = len(marker)
    index = begin
    while index < limit:
        blocked = _covering_end(blockers, index)
        if blocked is not None and blocked > index:
            return None
        skipped = _covering_end(skippable, index)
        if skipped is not None and skipped > index:
            index = min(skipped, limit)
            continue
        if text[index] == '\\' and index + 1 < limit:
            index += 2
            continue
        if not allow_blank and text[index] == '\n':
            line_end = text.find('\n', index + 1, limit)
            if line_end < 0:
                line_end = limit
            if not text[index + 1:line_end].strip():
                return None
        if index + size <= limit and text.startswith(marker, index):
            return index
        index += 1
    return None


def _comment_spans(text, blockers, skippable):
    spans, ranges = [], []
    limit = len(text)
    index = 0
    while index < limit:
        blocked = _covering_end(blockers, index)
        if blocked is not None and blocked > index:
            index = blocked
            continue
        skipped = _covering_end(skippable, index)
        if skipped is not None and skipped > index:
            index = skipped
            continue
        if text[index] == '\\' and index + 1 < limit:
            index += 2
            continue
        if text.startswith('%%', index):
            closer = _find_closer(text, index + 2, limit, '%%', blockers, skippable, True)
            if closer is not None:
                spans.append(Span(index, closer + 2, 'comment'))
                spans.append(Span(index, index + 2, 'syntax'))
                spans.append(Span(closer, closer + 2, 'syntax'))
                ranges.append((index, closer + 2))
                index = closer + 2
                continue
        index += 1
    return spans, ranges


def _paired_spans(text, blockers, skippable):
    spans = []

    def scan(begin, limit):
        index = begin
        while index < limit:
            blocked = _covering_end(blockers, index)
            if blocked is not None and blocked > index:
                index = min(blocked, limit)
                continue
            skipped = _covering_end(skippable, index)
            if skipped is not None and skipped > index:
                index = min(skipped, limit)
                continue
            if text[index] == '\\' and index + 1 < limit:
                index += 2
                continue
            matched = False
            for marker, style in (('==', 'highlight'), ('~~', 'strikethrough')):
                if not (index + len(marker) <= limit and text.startswith(marker, index)):
                    continue
                closer = _find_closer(
                    text, index + len(marker), limit, marker, blockers, skippable, False)
                if closer is None or not _meaningful(text[index + len(marker):closer], marker):
                    continue
                spans.append(Span(index + len(marker), closer, style))
                spans.append(Span(index, index + len(marker), 'syntax'))
                spans.append(Span(closer, closer + len(marker), 'syntax'))
                scan(index + len(marker), closer)
                index = closer + len(marker)
                matched = True
                break
            if not matched:
                index += 1

    scan(0, len(text))
    return spans


def obsidian_comment_ranges(text):
    """Character ranges of %%comments%%, including their markers."""
    return [(span.start, span.end) for span in markdown_layout(text).spans if span.style == 'comment']


def _escaped(text, index):
    slashes = 0
    while index - slashes > 0 and text[index - slashes - 1] == '\\':
        slashes += 1
    return slashes % 2 == 1


def _occupied(ranges, start, end):
    return any(not (end <= opening or start >= closing) for opening, closing in ranges)


def callout_at(lines, offsets, start, end):
    if start >= len(lines):
        return None
    raw = lines[start].rstrip('\r\n')
    match = CALLOUT_RE.match(raw)
    if not match:
        return None
    return Callout(offsets[start], offsets[end], match['kind'], (match['title'] or '').strip(),
                   offsets[start], offsets[start] + len(raw))


def callout_spans(line, line_start):
    raw = line.rstrip('\r\n')
    marker = raw.find('[!')
    if marker < 0:
        return []
    kind_end = raw.find(']', marker + 2)
    if kind_end < 0:
        return []
    spans = [Span(line_start + marker, line_start + marker + 2, 'syntax')]
    kind = raw[marker + 2:kind_end]
    if kind:
        spans.append(Span(line_start + marker + 2, line_start + kind_end, 'callout'))
    syntax_end = kind_end + 1
    if syntax_end < len(raw) and raw[syntax_end] in '+-':
        syntax_end += 1
    spans.append(Span(line_start + kind_end, line_start + syntax_end, 'syntax'))
    title = raw[syntax_end:].strip()
    if title:
        title_at = raw.find(title, syntax_end)
        if title_at >= 0:
            spans.append(Span(line_start + title_at, line_start + title_at + len(title), 'callout'))
    return spans


def find_embeds(lines, offsets, skip):
    found = []
    for index, line in enumerate(lines):
        if index in skip:
            continue
        raw = line.rstrip('\r\n')
        match = EMBED_RE.match(raw)
        if not match:
            continue
        body = match['body']
        target = body.split('|', 1)[0].strip()
        begin = raw.find('![[')
        end = begin + raw[begin:].find(']]') + 2
        suffix = PurePosixPath(target.split('#', 1)[0]).suffix.lower()
        kind = 'image' if suffix in IMAGE_SUFFIXES else 'note'
        found.append(Embed(offsets[index] + begin, offsets[index] + end, target, kind))
    return found


def find_math(text, lines, offsets, skip, code_ranges):
    """Display ``$$`` blocks and inline ``$...$`` outside code and metadata."""
    found = []
    covered = []
    index = 0
    while index < len(lines):
        if index in skip:
            index += 1
            continue
        raw = lines[index].rstrip('\r\n')
        opening = raw.find('$$')
        if raw.strip() == '$$' and opening >= 0 and not _escaped(raw, opening):
            close = None
            for follower in range(index + 1, len(lines)):
                if follower in skip:
                    break
                follower_raw = lines[follower].rstrip('\r\n')
                marker = follower_raw.find('$$')
                if follower_raw.strip() == '$$' and marker >= 0 and not _escaped(follower_raw, marker):
                    close = follower
                    break
            if close is not None:
                start = offsets[index] + opening
                end = offsets[close] + lines[close].rstrip('\r\n').find('$$') + 2
                if not _occupied(code_ranges, start, end):
                    expression = text[offsets[index + 1]:offsets[close]].strip('\n')
                    found.append(MathSpan(start, end, expression, True))
                    covered.append((start, end))
                index = close + 1
                continue
        index += 1
    for index, line in enumerate(lines):
        if index in skip:
            continue
        raw = line.rstrip('\r\n')
        position = 0
        while position < len(raw):
            absolute = offsets[index] + position
            if raw[position] != '$' or _escaped(raw, position) or _occupied(covered, absolute, absolute + 1):
                position += 1
                continue
            if raw.startswith('$$', position):
                close = raw.find('$$', position + 2)
                if close > position + 2 and not _occupied(code_ranges, absolute, offsets[index] + close + 2):
                    expression = raw[position + 2:close]
                    if expression and expression == expression.strip():
                        found.append(MathSpan(absolute, offsets[index] + close + 2, expression, True))
                        position = close + 2
                        continue
                position += 2
                continue
            close = position + 1
            while close < len(raw):
                if raw[close] == '$' and not _escaped(raw, close):
                    break
                close += 1
            else:
                position += 1
                continue
            expression = raw[position + 1:close]
            end = offsets[index] + close + 1
            if expression and expression == expression.strip() and not _occupied(code_ranges, absolute, end):
                found.append(MathSpan(absolute, end, expression, False))
                position = close + 1
                continue
            position += 1
    found.sort(key=lambda item: item.start)
    return found


def markdown_layout(text):
    """Return source styles and list items without modifying the Markdown.

    Block maps come from CommonMark; inline token offsets survive its emphasis
    balancing, including constructs spanning several lines of a paragraph.
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    spans, skip, items, lists, headings, images, callouts, diagrams = [], set(), [], [], [], [], [], []
    block_ranges, code_ranges = [], []
    first = 0
    if lines and lines[0].strip() == '---':
        for index, line in enumerate(lines[1:], 1):
            if line.strip() in ('---', '...'):
                first = index + 1
                spans.append(Span(0, offsets[first], 'metadata'))
                spans.append(Span(0, offsets[first], 'syntax'))
                block_ranges.append((0, offsets[first]))
                skip.update(range(first))
                break
    env = {}
    tokens = MARKDOWN.parse(''.join(lines[first:]), env)
    for token_index, token in enumerate(tokens):
        if token.type in ('bullet_list_open', 'ordered_list_open'):
            lists.append(int(token.attrGet('start') or 1) if token.type == 'ordered_list_open' else None)
        elif token.type in ('bullet_list_close', 'ordered_list_close'):
            lists.pop()
        if token.map is None:
            continue
        start, end = (line + first for line in token.map)
        if token.type == 'list_item_open':
            # CommonMark decides whether this is a real list, excluding code,
            # escaped markers, and thematic breaks before we examine its prefix.
            marker = re.match(
                r'(?:(?:[ \t]*>[ \t]?)*[ \t]*)'
                r'(?P<bullet>[-+*]|\d+[.)])(?:[ \t]+|$)'
                r'(?:\[(?P<state>[ xX])\](?:[ \t]+|$))?', lines[start].rstrip('\n'))
            if marker:
                number = lists[-1]
                state = marker.start('state')
                items.append(ListItem(
                    offsets[start], offsets[end], offsets[start] + marker.start('bullet'),
                    offsets[start] + marker.end(), len(lists) - 1, number,
                    offsets[start] + state if state >= 0 else None,
                    state >= 0 and marker['state'].lower() == 'x'))
                if number is not None:
                    lists[-1] += 1
        elif token.type in ('fence', 'code_block'):
            spans.append(Span(offsets[start], offsets[end], 'code'))
            skip.update(range(start, end))
            block_ranges.append((offsets[start], offsets[end]))
            if token.type == 'fence':
                spans.append(Span(offsets[start], offsets[start + 1], 'syntax'))
                closed = end > start + 1 and re.match(
                    r'^\s*' + re.escape(token.markup[0]) + '{' + str(len(token.markup)) + r',}\s*$',
                    lines[end - 1])
                if closed:
                    spans.append(Span(offsets[end - 1], offsets[end], 'syntax'))
                content_end = end - 1 if closed else end
                language = (token.info or '').split(None, 1)[0] if token.info else ''
                if language.lower() == 'mermaid':
                    diagrams.append((offsets[start], offsets[end], token.content))
                if content_end > start + 1 and language.lower() not in ('math', 'latex'):
                    code = ''.join(lines[start + 1:content_end])
                    base = offsets[start + 1]
                    limit = offsets[content_end]
                    for token_span in highlight_code(language, code):
                        token_start = base + token_span.start
                        token_end = base + token_span.end
                        if base <= token_start < token_end <= limit:
                            spans.append(Span(token_start, token_end, token_span.style))
        elif token.type == 'heading_open':
            inline = tokens[token_index + 1]
            label = ''.join('\n' if child.type in ('softbreak', 'hardbreak') else child.content
                            for child in inline.children or []
                            if child.type in ('text', 'code_inline', 'wikilink', 'image', 'softbreak', 'hardbreak'))
            headings.append(Heading(offsets[start], label, int(token.tag[1:])))
            spans.append(Span(offsets[start], offsets[end], token.tag))
            prefix = re.match(r' {0,3}#{1,6}\s+', lines[start])
            if prefix:
                spans.append(Span(offsets[start], offsets[start] + prefix.end(), 'syntax'))
                closing = re.search(r'\s+#+\s*$', lines[start].rstrip('\n'))
                if closing:
                    spans.append(Span(offsets[start] + closing.start(), offsets[start] + closing.end(), 'syntax'))
            elif end > start + 1:
                spans.append(Span(offsets[end - 1], offsets[end], 'syntax'))
        elif token.type == 'blockquote_open':
            spans.append(Span(offsets[start], offsets[end], 'quote'))
            callout = callout_at(lines, offsets, start, end)
            if callout is not None:
                callouts.append(callout)
                spans.extend(callout_spans(lines[start], offsets[start]))
        elif token.type == 'inline':
            # Recognize standalone image lines, including lines adjacent to text
            # in the same paragraph. Block tokens exclude fenced/code content.
            for line_number in range(start, end):
                raw = lines[line_number].rstrip('\r\n')
                if not raw.lstrip().startswith('!['):
                    continue
                children = MARKDOWN.parseInline(raw, env)[0].children or []
                if len(children) == 1:
                    image = _standalone_image(children[0])
                    if image is not None:
                        source, width, height = image
                        images.append((offsets[line_number], offsets[line_number] + len(raw),
                                       source, width, height))
            # Parse the source block rather than token.content, which has already
            # lost quote/list prefixes and would shift the original offsets.
            source = ''.join(lines[start:end]).rstrip('\n')
            inline, codes = inline_spans(source, offsets[start], env)
            spans.extend(inline)
            code_ranges.extend(codes)
    for index, line in enumerate(lines):
        if index in skip:
            continue
        # Quote/list markers are styled independently of inline content.
        prefix = re.match(r' {0,3}(?:>\s?)+', line)
        if prefix:
            spans.append(Span(offsets[index], offsets[index] + prefix.end(), 'syntax'))
    comment_spans, comments = _comment_spans(text, block_ranges, code_ranges)
    if comments:
        spans = [span for span in spans if not _contained(span.start, span.end, comments)]
        items = [item for item in items if not _contained(item.start, item.end, comments)]
        headings = [heading for heading in headings if not any(
            start <= heading.start < stop for start, stop in comments)]
        images = [image for image in images if not _contained(image[0], image[1], comments)]
    spans.extend(comment_spans)
    spans.extend(_paired_spans(text, block_ranges, [*code_ranges, *comments]))
    all_code_ranges = [*block_ranges, *code_ranges, *comments]
    embeds = [embed for embed in find_embeds(lines, offsets, skip)
              if not _contained(embed.start, embed.end, comments)]
    math = find_math(text, lines, offsets, skip, all_code_ranges)
    for item in math:
        opener = 2 if text.startswith('$$', item.start) else 1
        closer = 2 if text.endswith('$$', item.start, item.end) or text.startswith('$$', item.end - 2) else 1
        if item.start + opener <= item.end - closer:
            spans.append(Span(item.start, item.start + opener, 'syntax'))
            spans.append(Span(item.end - closer, item.end, 'syntax'))
            if item.start + opener < item.end - closer:
                spans.append(Span(item.start + opener, item.end - closer, 'math'))
    tables = []
    for table in find_tables(text):
        header_line = text.count('\n', 0, table.start)
        if any(line in skip for line in range(header_line, header_line + len(table.line_starts))):
            continue
        tables.append(table)
        if table.end > table.start:
            spans.append(Span(table.start, table.end, 'table'))
    return MarkdownLayout(spans, items, headings, images, embeds, math, tables, callouts, diagrams)
