"""Source-offset styles for live Markdown editing. Never rewrite note text."""
from dataclasses import dataclass
import re

from markdown_it import MarkdownIt
from markdown_it.rules_inline import backticks, emphasis, link

from .vault import wiki_rule


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
class MarkdownLayout:
    spans: list[Span]
    items: list[ListItem]
    headings: list[Heading]
    images: list[tuple[int, int, str]]


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


MARKDOWN = MarkdownIt('commonmark', {'html': False})
MARKDOWN.inline.ruler.at('emphasis', positioned(emphasis.tokenize, 'emphasis'))
MARKDOWN.inline.ruler.at('backticks', positioned(backticks.backtick, 'code'))
MARKDOWN.inline.ruler.at('link', positioned(link, 'link'))
MARKDOWN.inline.ruler.before('link', 'wikilink', positioned(wiki_rule, 'wiki'))


def inline_spans(text, base, env):
    spans, stack = [], []

    def add(start, end, style):
        if end > start:
            spans.append(Span(base + start, base + end, style))

    for token in MARKDOWN.parseInline(text, env)[0].children or []:
        if token.type in ('em_open', 'strong_open'):
            start = token.meta['offset'] - (len(token.markup) - 1)
            stack.append((start, start + len(token.markup), token.tag))
        elif token.type in ('em_close', 'strong_close'):
            start, content, style = stack.pop()
            end = token.meta['offset']
            add(content, end, style)
            add(start, content, 'syntax')
            add(end, end + len(token.markup), 'syntax')
        elif token.type == 'code_inline':
            start, end = token.meta['source']
            size = len(token.markup)
            add(start + size, end - size, 'code')
            add(start, start + size, 'syntax')
            add(end - size, end, 'syntax')
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
    return spans


def markdown_layout(text):
    """Return source styles and list items without modifying the Markdown.

    Block maps come from CommonMark; inline token offsets survive its emphasis
    balancing, including constructs spanning several lines of a paragraph.
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    spans, skip, items, lists, headings, images = [], set(), [], [], [], []
    first = 0
    if lines and lines[0].strip() == '---':
        for index, line in enumerate(lines[1:], 1):
            if line.strip() in ('---', '...'):
                first = index + 1
                spans.append(Span(0, offsets[first], 'metadata'))
                spans.append(Span(0, offsets[first], 'syntax'))
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
            if token.type == 'fence':
                spans.append(Span(offsets[start], offsets[start + 1], 'syntax'))
                if end > start + 1 and re.match(r'^\s*' + re.escape(token.markup[0]) + '{' + str(len(token.markup)) + r',}\s*$', lines[end - 1]):
                    spans.append(Span(offsets[end - 1], offsets[end], 'syntax'))
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
        elif token.type == 'inline':
            # Recognize standalone image lines, including lines adjacent to text
            # in the same paragraph. Block tokens exclude fenced/code content.
            for line_number in range(start, end):
                raw = lines[line_number].rstrip('\r\n')
                if not raw.lstrip().startswith('!['):
                    continue
                children = MARKDOWN.parseInline(raw, env)[0].children or []
                if len(children) == 1 and children[0].type == 'image':
                    images.append((offsets[line_number], offsets[line_number] + len(raw),
                                   children[0].attrGet('src')))
            # Parse the source block rather than token.content, which has already
            # lost quote/list prefixes and would shift the original offsets.
            source = ''.join(lines[start:end]).rstrip('\n')
            spans.extend(inline_spans(source, offsets[start], env))
    for index, line in enumerate(lines):
        if index in skip:
            continue
        # Quote/list markers are styled independently of inline content.
        prefix = re.match(r' {0,3}(?:>\s?)+', line)
        if prefix:
            spans.append(Span(offsets[index], offsets[index] + prefix.end(), 'syntax'))
    return MarkdownLayout(spans, items, headings, images)
