"""Source edits for Markdown keyboard actions, independent of GTK."""
from dataclasses import dataclass
import re

from .formatting import markdown_layout


@dataclass(frozen=True)
class TextEdit:
    start: int
    end: int
    replacement: str


def list_enter(text: str, cursor: int) -> TextEdit | None:
    """Continue the current list, or leave an empty item on a blank line.

    Parse the current source rather than cached display widgets: consecutive
    key events can arrive before the idle formatting pass runs.
    """
    layout = markdown_layout(text)
    line_start = text.rfind('\n', 0, cursor) + 1
    line_end = text.find('\n', cursor)
    if line_end == -1:
        line_end = len(text)
    if any(span.style in ('code', 'metadata') and span.start <= cursor
           and (cursor < span.end or cursor == span.end and span.start <= line_start)
           for span in layout.spans):
        return None
    candidates = [item for item in layout.items
                  if item.start <= line_start < item.end and cursor >= item.content_start]
    if not candidates:
        return None
    item = max(candidates, key=lambda item: item.depth)
    prefix = text[item.start:item.content_start]
    # A newly typed nested "- " can parse as a setext underline until content
    # follows it. Inside an existing list, Enter still exits that empty draft.
    empty_draft = re.fullmatch(
        r'(?P<quote>(?:[ \t]*>[ \t]?)*)[ \t]*(?:[-+*]|\d+[.)])[ \t]*'
        r'(?:\[[ xX]\][ \t]*)?', text[line_start:line_end])
    if line_start != item.start and empty_draft:
        quote = empty_draft['quote']
        return TextEdit(line_start, line_end, quote.rstrip() + '\n' + quote)
    if line_start == item.start and not text[item.content_start:item.end].strip():
        # Keep quote context, but remove list indentation and leave a separator
        # so subsequent prose cannot become a CommonMark lazy list continuation.
        quote = text[item.start:item.marker_start]
        quote = quote if '>' in quote else ''
        return TextEdit(line_start, line_end, quote.rstrip() + '\n' + quote)
    if line_start != item.start and not text[line_start:line_end].strip():
        return None
    if item.task_offset is not None:
        state = item.task_offset - item.start
        prefix = prefix[:state] + ' ' + prefix[state + 1:]
    if item.number is not None:
        marker = item.marker_start - item.start
        number = re.match(r'\d+', prefix[marker:])
        prefix = prefix[:marker] + str(item.number + 1) + prefix[marker + number.end():]
    if not prefix.endswith((' ', '\t')):
        prefix += ' '
    return TextEdit(cursor, cursor, '\n' + prefix)
