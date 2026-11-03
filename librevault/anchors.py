"""Heading sections and Obsidian block ids, independent of GTK.

Matching follows Obsidian's anchor rules: visible heading text, case folding,
collapsed whitespace (including non-breaking spaces), and ASCII punctuation
removed except apostrophes, hyphens, and underscores. ``#Parent#Child`` walks
nested sections. A block id is ``^[A-Za-z0-9-]+`` at the end of a block or on
the line immediately after it, including one blank line.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import unquote


# The 32 ASCII punctuation characters, minus the three Obsidian keeps.
_DROPPED_PUNCTUATION = set('!"#$%&()*+,./:;<=>?@[\\]^`{|}~')
_IMAGE_SIZE = re.compile(r'^(\d{1,5})(?:x(\d{1,5}))?$')
_OWN_BLOCK_ID = re.compile(r'^[ \t]*\^([A-Za-z0-9-]+)[ \t]*$')
_TRAILING_BLOCK_ID = re.compile(r'^(?P<body>.*\S)[ \t]+\^([A-Za-z0-9-]+)[ \t]*$')
_BLOCK_KINDS = {
    'heading_open': 'heading',
    'paragraph_open': 'paragraph',
    'fence': 'fence',
    'code_block': 'code',
    'blockquote_open': 'blockquote',
    'list_item_open': 'list_item',
    'bullet_list_open': 'list',
    'ordered_list_open': 'list',
    'table_open': 'table',
}
_PARSER = None


def parse_image_size(spec: str) -> tuple[int, int | None] | None:
    """Return ``(width, height)`` for ``300`` or ``300x200``.

    Height is ``None`` when only a width was given. Zero and non-numeric
    suffixes are display aliases, not sizes. Dimensions above 2000 pixels
    are clamped so a note cannot request an unbounded decode.
    """
    match = _IMAGE_SIZE.fullmatch((spec or '').strip())
    if not match:
        return None
    width = int(match.group(1))
    height = int(match.group(2)) if match.group(2) is not None else None
    if width < 1 or (height is not None and height < 1):
        return None
    return min(width, 2000), (None if height is None else min(height, 2000))


def image_alt_size(alt: str) -> tuple[int | None, int | None]:
    """Read ``![alt|300]`` / ``![alt|300x200]`` from image alt text."""
    if '|' not in (alt or ''):
        return None, None
    size = parse_image_size(alt.rpartition('|')[2])
    if size is None:
        return None, None
    return size


def split_transclusion_target(target: str) -> tuple[str, str, int | None, int | None]:
    """Split ``Note#Heading|alias`` or ``image.png|300x200``.

    Returns ``(path, fragment, width, height)``. A ``|suffix`` is a pixel
    size only when it is ``N`` or ``NxN``; any other suffix is display text
    and is ignored for resolution. The fragment keeps a leading ``^`` for
    block ids. Paths and fragments are percent-decoded.
    """
    base, separator, suffix = (target or '').partition('|')
    width = height = None
    if separator:
        size = parse_image_size(suffix.strip())
        if size is not None:
            width, height = size
    path, mark, fragment = base.partition('#')
    if mark:
        fragment = unquote(fragment).strip()
    else:
        fragment = ''
    return unquote(path).strip(), fragment, width, height


def normalize_anchor(value: str) -> str:
    """Fold a heading or link fragment the way Obsidian compares them.

    Dropped punctuation becomes a space, so ``a.b`` and ``a b`` are the same
    anchor. Apostrophes, hyphens, and underscores stay, so ``a_b`` does not
    match ``a b``.
    """
    kept = ''.join(' ' if character in _DROPPED_PUNCTUATION else character for character in value)
    return ' '.join(kept.split()).casefold()


def conceal_block_id(value: str) -> str:
    """Drop a trailing `` ^block-id`` from rendered text. Source stays unchanged."""
    if '\n' in value:
        head, separator, tail = value.rpartition('\n')
        return head + separator + conceal_block_id(tail)
    match = _TRAILING_BLOCK_ID.match(value)
    return match.group('body') if match else value


def preview_snippet(value: str, limit: int = 400) -> str:
    """One line of preview text, trimmed to ``limit`` characters."""
    compact = ' '.join((value or '').split())
    if len(compact) <= limit:
        return compact
    return compact[:max(0, limit - 1)] + '…'


def list_headings(text: str) -> list[tuple[int, str, int]]:
    """Return ``(level, visible text, character offset)`` for real headings."""
    return [(heading.level, heading.text, heading.start) for heading in _analyze(text).headings]


def list_block_ids(text: str) -> list[tuple[str, str, int]]:
    """Return ``(block id, preview snippet, character offset)``."""
    return [(block.block_id, block.snippet, block.start)
            for block in _analyze(text).blocks if block.block_id]


def heading_links(text: str) -> list[tuple[str, int, int]]:
    """Return ``(link fragment, level, offset)`` for heading completion.

    The fragment is the visible heading. When the same text occurs twice,
    ancestors are included (``Parent#Child``) so the link reaches that one.
    ``|``, ``]``, and ``#`` inside a heading are turned into spaces so the
    inserted wikilink still parses and still matches.
    """
    headings = _analyze(text).headings
    counts: dict[str, int] = {}
    for heading in headings:
        counts[heading.normalized] = counts.get(heading.normalized, 0) + 1
    links = []
    for index, heading in enumerate(headings):
        if counts[heading.normalized] == 1:
            fragment = _link_text(heading.text)
        else:
            fragment = '#'.join(_link_text(item.text) for item in _ancestor_path(headings, index))
        if fragment:
            links.append((fragment, heading.level, heading.start))
    return links


def reference_blocks(text: str) -> list[tuple[str | None, str, int, int, str]]:
    """Return ``(id or None, snippet, start, end, kind)`` for completion.

    Paragraphs inside list items are omitted because the item is the block
    Obsidian links to. Id-only lines and container lists are omitted unless
    that container itself carries the id.
    """
    analyzed = _analyze(text)
    items = [block for block in analyzed.blocks if block.kind == 'list_item']
    chosen = []
    for block in analyzed.blocks:
        if block.kind in ('list', 'blockquote') and not block.block_id:
            continue
        if _own_id_text(block.text):
            continue
        if block.kind == 'paragraph' and not block.block_id and any(
                item.start <= block.start and item.end >= block.end and item is not block
                for item in items):
            continue
        if not block.block_id and not block.snippet:
            continue
        chosen.append((block.block_id, block.snippet, block.start, block.end, block.kind))
    return chosen


def block_id_insertion(text: str, start: int, end: int, kind: str) -> tuple[str, int, str]:
    """Return ``(block id, insert offset, text to insert)``.

    The offset is ``-1`` and the text is empty when the block already has an
    id. New ids are a short hash of the block plus a suffix on collision.
    Paragraphs, headings, and list items get `` ^id`` on the marker line.
    Fences, tables, quotes, and indented code get an id line after the block
    so the id is not swallowed as another row or code line.
    """
    for block in _analyze(text).blocks:
        if block.start == start and block.block_id:
            return block.block_id, -1, ''
    existing = {block_id for block_id, _, _ in list_block_ids(text)}
    seed = text[start:end]
    block_id = _fresh_block_id(seed, existing)
    if kind == 'list_item':
        line_end = text.find('\n', start)
        if line_end < 0 or line_end > end:
            line_end = end
        elif line_end > start and text[line_end - 1] == '\r':
            line_end -= 1
        return block_id, line_end, f' ^{block_id}'
    if kind in ('fence', 'code', 'table', 'blockquote', 'list'):
        prefix = '\n' if kind == 'table' else ''
        if end > 0 and text[end - 1] != '\n':
            prefix = '\n' + prefix
        return block_id, end, f'{prefix}^{block_id}\n'
    line_end = end
    if line_end > start and text[line_end - 1] == '\n':
        line_end -= 1
    if line_end > start and text[line_end - 1] == '\r':
        line_end -= 1
    return block_id, line_end, f' ^{block_id}'


def find_anchor_offset(text: str, fragment: str) -> int | None:
    """Return the character offset of a heading or ``^block-id``, if present."""
    fragment = (fragment or '').strip()
    if not fragment:
        return None
    analyzed = _analyze(text)
    if fragment.startswith('^'):
        block = _match_block(analyzed.blocks, fragment[1:])
        return None if block is None else block.start
    heading = _match_heading(analyzed.headings, fragment)
    return None if heading is None else heading.start


def extract_subpath(text: str, fragment: str) -> str | None:
    """Return the Markdown for one heading section or one block.

    A heading slice runs from its line through the character before the next
    heading of the same or higher level. A block slice is that block only,
    without its `` ^id`` marker. ``None`` means the anchor is not in the note.
    """
    fragment = (fragment or '').strip()
    if not fragment:
        return None
    analyzed = _analyze(text)
    if fragment.startswith('^'):
        block = _match_block(analyzed.blocks, fragment[1:])
        return None if block is None else block.text
    heading = _match_heading(analyzed.headings, fragment)
    if heading is None:
        return None
    return text[heading.start:heading.end]


@dataclass
class _Heading:
    level: int
    text: str
    normalized: str
    start: int
    end: int
    start_line: int


@dataclass
class _Block:
    kind: str
    start_line: int
    end_line: int
    start: int = 0
    end: int = 0
    block_id: str | None = None
    extract_end_line: int | None = None
    text: str = ''
    snippet: str = ''

    @property
    def content_end_line(self) -> int:
        return self.end_line if self.extract_end_line is None else self.extract_end_line


@dataclass
class _Analysis:
    headings: list[_Heading]
    blocks: list[_Block]


def _parser():
    global _PARSER
    if _PARSER is None:
        from markdown_it import MarkdownIt
        from .vault import wiki_rule
        _PARSER = MarkdownIt('commonmark', {'html': False}).enable('table')
        _PARSER.inline.ruler.before('link', 'wikilink', wiki_rule)
    return _PARSER


def _body(lines: list[str]) -> int:
    """Return the first content line after a closed YAML frontmatter block."""
    if lines and lines[0].strip() == '---':
        for index in range(1, len(lines)):
            if lines[index].strip() in ('---', '...'):
                return index + 1
    return 0


def _visible_text(token) -> str:
    parts = []
    for child in token.children or []:
        if child.type in ('text', 'code_inline', 'wikilink'):
            parts.append(child.content)
        elif child.type == 'image':
            parts.append(child.content or '')
        elif child.type in ('softbreak', 'hardbreak'):
            parts.append(' ')
    return ''.join(parts).strip()


def _link_text(value: str) -> str:
    cleaned = ''.join(' ' if character in '[]|#' else character for character in value)
    return ' '.join(cleaned.split())


def _ancestor_path(headings: list[_Heading], index: int) -> list[_Heading]:
    chain: list[_Heading] = []
    for heading in headings[:index + 1]:
        while chain and chain[-1].level >= heading.level:
            chain.pop()
        chain.append(heading)
    return chain


def _fresh_block_id(seed: str, existing: set[str]) -> str:
    digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()
    candidate = digest[:6]
    serial = 0
    while candidate in existing:
        serial += 1
        if serial < 20:
            candidate = digest[serial:serial + 6]
        else:
            candidate = digest[:6] + str(serial)
    return candidate


def _strip_id(value: str, block_id: str) -> str:
    pattern = re.compile(r'[ \t]+\^' + re.escape(block_id) + r'[ \t]*\r?$', re.M)
    return pattern.sub('', value, count=1)


def _own_id_text(value: str) -> bool:
    return bool(_OWN_BLOCK_ID.match(value.strip()))


def _analyze(text: str) -> _Analysis:
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    body_line = _body(lines)
    body = ''.join(lines[body_line:])
    tokens = _parser().parse(body)
    headings: list[_Heading] = []
    blocks: list[_Block] = []
    for index, token in enumerate(tokens):
        kind = _BLOCK_KINDS.get(token.type)
        if kind is None or not token.map:
            continue
        start_line = token.map[0] + body_line
        end_line = token.map[1] + body_line
        if kind == 'heading':
            inline = next((item for item in tokens[index + 1:] if item.type == 'inline'), None)
            visible = _visible_text(inline) if inline is not None else ''
            headings.append(_Heading(int(token.tag[1:]), visible, normalize_anchor(visible),
                                     offsets[start_line], len(text), start_line))
        blocks.append(_Block(kind, start_line, end_line))
    for index, heading in enumerate(headings):
        for later in headings[index + 1:]:
            if later.level <= heading.level:
                heading.end = later.start
                break
    code_lines = set()
    for block in blocks:
        if block.kind in ('fence', 'code'):
            code_lines.update(range(block.start_line, block.end_line))
    seen: set[str] = set()
    for line_number, line in enumerate(lines):
        if line_number < body_line or line_number in code_lines:
            continue
        stripped = line.rstrip('\r\n')
        own = _OWN_BLOCK_ID.match(stripped)
        trailing = None if own else _TRAILING_BLOCK_ID.match(stripped)
        if own:
            block_id = own.group(1)
            picked = _block_before(blocks, lines, line_number)
            inside = picked is not None and picked.start_line < line_number < picked.end_line
        elif trailing:
            block_id = trailing.group(2)
            picked = _block_ending_on(blocks, line_number)
            inside = False
        else:
            continue
        if picked is None or picked.block_id is not None or block_id in seen:
            continue
        if _own_id_text(''.join(lines[picked.start_line:picked.end_line])):
            continue
        picked.block_id = block_id
        if inside:
            picked.extract_end_line = line_number
        seen.add(block_id)
    for block in blocks:
        block.start = offsets[block.start_line]
        block.end = offsets[min(block.content_end_line, len(offsets) - 1)]
        raw = text[block.start:block.end]
        block.text = _strip_id(raw, block.block_id) if block.block_id else raw
        block.snippet = preview_snippet(block.text, 80)
    return _Analysis(headings, blocks)


def _meaningful(block: _Block, lines: list[str]) -> bool:
    raw = ''.join(lines[block.start_line:block.end_line]).strip()
    return not _OWN_BLOCK_ID.match(raw)


def _block_ending_on(blocks: list[_Block], line_number: int) -> _Block | None:
    items = [block for block in blocks if block.kind == 'list_item' and block.start_line == line_number]
    if items:
        return max(items, key=lambda block: block.end_line - block.start_line)
    headings = [block for block in blocks if block.kind == 'heading' and block.start_line == line_number]
    if headings:
        return headings[0]
    ending = [block for block in blocks
              if block.end_line - 1 == line_number and block.kind not in ('list',)]
    if not ending:
        return None
    return min(ending, key=lambda block: (block.end_line - block.start_line, block.start_line))


def _block_before(blocks: list[_Block], lines: list[str], line_number: int) -> _Block | None:
    containing = [block for block in blocks
                  if block.start_line < line_number < block.end_line and _meaningful(block, lines)]
    if containing:
        return min(containing, key=lambda block: (block.end_line - block.start_line, block.start_line))
    candidates = [block for block in blocks
                  if block.end_line == line_number and block.start_line < line_number and _meaningful(block, lines)]
    if not candidates and line_number > 0 and not lines[line_number - 1].strip():
        boundary = line_number - 1
        candidates = [block for block in blocks
                      if block.end_line == boundary and block.start_line < boundary and _meaningful(block, lines)]
    if not candidates:
        return None
    return max(candidates, key=lambda block: (block.end_line - block.start_line, -block.start_line))


def _match_block(blocks: list[_Block], block_id: str) -> _Block | None:
    for block in blocks:
        if block.block_id == block_id:
            return block
    return None


def _match_heading(headings: list[_Heading], fragment: str) -> _Heading | None:
    parts = [normalize_anchor(part) for part in fragment.split('#')]
    parts = [part for part in parts if part]
    if not parts:
        return None
    if len(parts) == 1:
        for heading in headings:
            if heading.normalized == parts[0]:
                return heading
        return None
    found = _walk_headings(headings, parts)
    if found is not None:
        return found
    whole = normalize_anchor(fragment)
    for heading in headings:
        if heading.normalized == whole:
            return heading
    return None


def _walk_headings(headings: list[_Heading], parts: list[str]) -> _Heading | None:
    def search(start: int, part_index: int, limit: int) -> _Heading | None:
        for index in range(start, len(headings)):
            heading = headings[index]
            if heading.start >= limit:
                return None
            if heading.normalized != parts[part_index]:
                continue
            if part_index == len(parts) - 1:
                return heading
            found = search(index + 1, part_index + 1, heading.end)
            if found is not None:
                return found
        return None
    return search(0, 0, 2 ** 62)
