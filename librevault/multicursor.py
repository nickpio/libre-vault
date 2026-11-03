"""Multi-occurrence edits on plain text. The buffer applies one user action."""
from .editing import TextEdit, format_inline


def is_word_char(char):
    return char.isalnum() or char == '_'


def word_bounds(text, cursor):
    """The word touching the cursor, or None when the cursor is between words."""
    if not text:
        return None
    index = min(max(cursor, 0), len(text))
    if index >= len(text) or not is_word_char(text[index]):
        if index == 0 or not is_word_char(text[index - 1]):
            return None
        index -= 1
    start = index
    while start > 0 and is_word_char(text[start - 1]):
        start -= 1
    end = index + 1
    while end < len(text) and is_word_char(text[end]):
        end += 1
    return start, end


def find_occurrences(text, needle):
    if not needle:
        return []
    found = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return found
        found.append((index, index + len(needle)))
        start = index + max(1, len(needle))


def next_occurrence(spans, selected):
    """The next span after the last selection, wrapping once. Skips spans already taken."""
    taken = {tuple(span) for span in selected}
    pending = [span for span in spans if span not in taken]
    if not pending:
        return None
    last = max((end for _, end in selected), default=0)
    for span in pending:
        if span[0] >= last:
            return span
    return pending[0]


def merge_spans(spans):
    merged = []
    for start, end in sorted(spans):
        if start > end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _map_offset(offset, deletions):
    shift = 0
    for start, end in deletions:
        if end <= offset:
            shift += end - start
        elif start < offset:
            return start - shift
    return offset - shift


def plan_insert(ranges, chunk):
    """Replace every range with `chunk`. Empty ranges insert. Carets sit after each chunk.

    Edits use the original coordinates and run from the end of the buffer so
    earlier offsets stay valid. Carets are positions in the text after every edit.
    """
    ordered = sorted(ranges)
    edits = []
    carets = []
    shift = 0
    for start, end in ordered:
        carets.append(start + shift + len(chunk))
        edits.append(TextEdit(start, end, chunk))
        shift += len(chunk) - (end - start)
    edits.reverse()
    return edits, carets


def plan_backspace(text, ranges):
    deletions = []
    origins = []
    for start, end in sorted(ranges):
        if start != end:
            deletions.append((start, end))
            origins.append(start)
        elif start > 0:
            deletions.append((start - 1, start))
            origins.append(start - 1)
        else:
            origins.append(0)
    deletions = merge_spans(deletions)
    carets = [_map_offset(origin, deletions) for origin in origins]
    edits = [TextEdit(start, end, '') for start, end in reversed(deletions)]
    return edits, carets


def plan_delete(text, ranges):
    deletions = []
    origins = []
    for start, end in sorted(ranges):
        if start != end:
            deletions.append((start, end))
            origins.append(start)
        elif start < len(text):
            deletions.append((start, start + 1))
            origins.append(start)
        else:
            origins.append(start)
    deletions = merge_spans(deletions)
    carets = [_map_offset(origin, deletions) for origin in origins]
    edits = [TextEdit(start, end, '') for start, end in reversed(deletions)]
    return edits, carets


def plan_format(text, ranges, marker):
    """Toggle inline formatting at each range. Selections cover the inner content."""
    edits = []
    selections = []
    working = text
    for start, end in sorted(ranges, reverse=True):
        edit, selected = format_inline(working, start, end, marker)
        delta = len(edit.replacement) - (edit.end - edit.start)
        selections = [selected] + [(left + delta, right + delta) for left, right in selections]
        edits.append(edit)
        working = working[:edit.start] + edit.replacement + working[edit.end:]
    return edits, selections


class ExtraCursors:
    """Occurrences other than the buffer's primary selection."""

    def __init__(self):
        self.ranges = []

    def clear(self):
        self.ranges.clear()

    def __bool__(self):
        return bool(self.ranges)

    def combined(self, primary):
        ranges = list(self.ranges)
        if primary not in ranges:
            ranges.append(primary)
        return sorted(set(ranges))
