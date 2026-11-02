"""Unlinked plain-text mentions of a note name or alias.

A mention is a whole-term occurrence outside YAML frontmatter, fenced code,
wikilinks, and Markdown links. Offsets refer to the text that was searched.
"""
from dataclasses import dataclass
import re

from .metadata import frontmatter_range


@dataclass(frozen=True, slots=True)
class Mention:
    source_path: str
    line_number: int
    snippet: str
    match_start: int
    match_end: int
    matched_term: str


def link_mention(text, match_start, match_end, target_name):
    """Replace one plain-text mention with a wikilink.

    The visible text stays in the alias position when it is not already the
    target name, including a different capitalization.
    """
    if not 0 <= match_start <= match_end <= len(text):
        raise ValueError('The mention is outside the note text.')
    matched = text[match_start:match_end]
    if matched == target_name:
        replacement = '[[' + target_name + ']]'
    else:
        replacement = '[[' + target_name + '|' + matched + ']]'
    return text[:match_start] + replacement + text[match_end:]


def find_mentions(source_path, text, terms):
    """Return unlinked mentions of terms in one note, in document order."""
    patterns = []
    seen = set()
    for term in terms:
        cleaned = term.strip()
        key = cleaned.casefold()
        if not cleaned or key in seen or not re.search(r'\w', cleaned):
            continue
        seen.add(key)
        patterns.append(re.compile(r'(?<!\w)' + re.escape(cleaned) + r'(?!\w)', re.IGNORECASE))
    if not patterns:
        return []
    blocked = _excluded_ranges(text)
    matches = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if _overlaps(match.start(), match.end(), blocked):
                continue
            matches.append(match)
    matches.sort(key=lambda match: (match.start(), -(match.end() - match.start())))
    chosen = []
    last_end = -1
    for match in matches:
        if match.start() < last_end:
            continue
        chosen.append(match)
        last_end = match.end()
    return [Mention(source_path, text.count('\n', 0, match.start()) + 1,
                    _snippet(text, match.start(), match.end()),
                    match.start(), match.end(), match.group()) for match in chosen]


def _snippet(text, start, end):
    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', end)
    if line_end < 0:
        line_end = len(text)
    left = max(line_start, start - 48)
    right = min(line_end, max(end + 48, left + 96))
    snippet = text[left:right].replace('\t', ' ').replace('\r', '')
    if left > line_start:
        snippet = '…' + snippet
    if right < line_end:
        snippet += '…'
    return snippet


def _overlaps(start, end, ranges):
    return any(start < stop and end > begin for begin, stop in ranges)


def _excluded_ranges(text):
    ranges = []
    bounds = frontmatter_range(text)
    body_start = bounds[2] if bounds else 0
    if bounds:
        ranges.append((0, body_start))
    offset = body_start
    fence = None
    fence_at = None
    for line in text[body_start:].splitlines(keepends=True):
        marker = re.match(r'\s{0,3}(`{3,}|~{3,})', line)
        if marker and (fence is None or (marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence))):
            if fence is None:
                fence = marker.group(1)
                fence_at = offset
            else:
                ranges.append((fence_at, offset + len(line)))
                fence = None
                fence_at = None
        offset += len(line)
    if fence_at is not None:
        ranges.append((fence_at, len(text)))
    ranges.extend(_link_ranges(text))
    return ranges


def _escaped(text, index):
    count = 0
    index -= 1
    while index >= 0 and text[index] == '\\':
        count += 1
        index -= 1
    return count % 2 == 1


def _link_ranges(text):
    """Wikilinks and inline Markdown links, including images and embeds."""
    ranges = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith('![[', index) or text.startswith('![', index) or text[index] == '[':
            if _escaped(text, index):
                index += 1
                continue
        if text.startswith('![[', index) or text.startswith('[[', index):
            end = text.find(']]', index + 2)
            newline = text.find('\n', index + 2)
            opening = 3 if text.startswith('![[', index) else 2
            if end > index + opening and (newline == -1 or end < newline):
                ranges.append((index, end + 2))
                index = end + 2
                continue
        image = text.startswith('![', index)
        if image or (index < length and text[index] == '['):
            label = index + (2 if image else 1)
            while label < length and text[label] != '\n':
                if text[label] == '\\' and label + 1 < length:
                    label += 2
                    continue
                if text[label] == ']':
                    break
                label += 1
            if (label < length and text[label] == ']' and label + 1 < length and text[label + 1] == '('):
                destination = text.find(')', label + 2)
                newline = text.find('\n', label + 2)
                if destination != -1 and (newline == -1 or destination < newline):
                    ranges.append((index, destination + 1))
                    index = destination + 1
                    continue
        index += 1
    return ranges
