"""Optional modal editing. Pure cursor and text results; the pane applies them."""
from dataclasses import dataclass, replace

from .editing import TextEdit


@dataclass(frozen=True)
class Session:
    mode: str = 'insert'
    pending: str = ''
    register: str = ''
    linewise: bool = False
    column: int | None = None
    anchor: int = 0
    cursor: int = 0


@dataclass(frozen=True)
class Outcome:
    session: Session
    cursor: int
    anchor: int
    edit: TextEdit | None = None
    handled: bool = True
    undo: bool = False
    redo: bool = False


def line_bounds(text, cursor):
    cursor = max(0, min(cursor, len(text)))
    start = text.rfind('\n', 0, cursor) + 1
    end = text.find('\n', cursor)
    if end < 0:
        end = len(text)
    return start, end


def retreat(text, cursor):
    """Vim leaves insert mode one character left of the caret, except at column 0."""
    start, _ = line_bounds(text, cursor)
    if cursor > start:
        return cursor - 1
    return cursor


def _column(text, cursor):
    start, _ = line_bounds(text, cursor)
    return cursor - start


def _clamp_column(text, line_start, column):
    _, end = line_bounds(text, line_start)
    if end == line_start:
        return line_start
    return min(line_start + column, end - 1)


def _first_nonblank(text, line_start):
    _, end = line_bounds(text, line_start)
    index = line_start
    while index < end and text[index] in ' \t':
        index += 1
    return index


def _classify(char):
    if char.isspace():
        return 'space'
    if char.isalnum() or char == '_':
        return 'word'
    return 'punct'


def motion_h(text, cursor):
    start, _ = line_bounds(text, cursor)
    return max(start, cursor - 1)


def motion_l(text, cursor):
    start, end = line_bounds(text, cursor)
    if end == start:
        return cursor
    return min(end - 1, cursor + 1)


def motion_vertical(text, cursor, delta, column):
    start, end = line_bounds(text, cursor)
    column = _column(text, cursor) if column is None else column
    if delta < 0:
        if start == 0:
            return cursor, column
        previous = text.rfind('\n', 0, start - 1) + 1
        return _clamp_column(text, previous, column), column
    if end >= len(text):
        return cursor, column
    return _clamp_column(text, end + 1, column), column


def motion_w(text, cursor):
    size = len(text)
    if cursor >= size:
        return cursor
    index = cursor
    kind = _classify(text[index])
    if kind != 'space':
        while index < size and _classify(text[index]) == kind:
            index += 1
    while index < size and _classify(text[index]) == 'space':
        index += 1
    return index


def motion_b(text, cursor):
    if cursor <= 0:
        return 0
    index = cursor - 1
    while index > 0 and _classify(text[index]) == 'space':
        index -= 1
    kind = _classify(text[index])
    while index > 0 and _classify(text[index - 1]) == kind:
        index -= 1
    return index


def motion_e(text, cursor):
    size = len(text)
    if cursor >= size:
        return max(0, size - 1)
    index = min(size - 1, cursor + 1)
    while index < size and _classify(text[index]) == 'space':
        index += 1
    if index >= size:
        return max(cursor, size - 1)
    kind = _classify(text[index])
    while index + 1 < size and _classify(text[index + 1]) == kind:
        index += 1
    return index


def motion_zero(text, cursor):
    start, _ = line_bounds(text, cursor)
    return start


def motion_dollar(text, cursor):
    start, end = line_bounds(text, cursor)
    if end == start:
        return start
    return end - 1


def motion_caret(text, cursor):
    start, _ = line_bounds(text, cursor)
    return _first_nonblank(text, start)


def motion_gg():
    return 0


def motion_G(text):
    """First non-blank on the last line. A trailing newline does not add a line."""
    if not text:
        return 0
    end = len(text) - 1 if text.endswith('\n') else len(text)
    start = text.rfind('\n', 0, end) + 1
    return _first_nonblank(text, start)


def _line_span(text, cursor):
    """The line, including its newline, or the preceding newline on the last line."""
    start, end = line_bounds(text, cursor)
    if end < len(text):
        return start, end + 1, start
    if start > 0:
        return start - 1, end, max(0, start - 1)
    return start, end, start


def _change_word_end(text, cursor):
    """Exclusive end for `cw`: through the current word, not the trailing space."""
    if cursor >= len(text):
        return cursor
    if text[cursor] == '\n':
        return cursor
    if text[cursor].isspace():
        end = cursor
        while end < len(text) and text[end].isspace() and text[end] != '\n':
            end += 1
        return end
    kind = _classify(text[cursor])
    end = cursor
    while end < len(text) and _classify(text[end]) == kind:
        end += 1
    return end


def _visual_range(text, anchor, cursor, linewise):
    if linewise:
        first = min(anchor, cursor)
        last = max(anchor, cursor)
        start, _ = line_bounds(text, first)
        _, end = line_bounds(text, last)
        if end < len(text):
            end += 1
        return start, end
    return min(anchor, cursor), max(anchor, cursor)


def _move(session, text, cursor, column=None, keep_column=False):
    cursor = max(0, min(cursor, len(text)))
    anchor = session.anchor if session.mode in ('visual', 'visual_line') else cursor
    stored = column if keep_column else None
    return Outcome(replace(session, pending='', cursor=cursor, anchor=anchor, column=stored),
                   cursor, anchor)


def _delete_between(session, text, start, end, *, linewise, cursor, mode='normal'):
    """Delete `text[start:end]`. `cursor` is an offset in the text before the edit."""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    yanked = text[start:end]
    if linewise and yanked and not yanked.endswith('\n'):
        yanked += '\n'
    removed = end - start
    if cursor >= end:
        landed = cursor - removed
    elif cursor >= start:
        landed = start
    else:
        landed = cursor
    landed = max(0, min(landed, len(text) - removed))
    return Outcome(
        replace(session, mode=mode, pending='', register=yanked, linewise=linewise,
                cursor=landed, anchor=landed, column=None),
        landed, landed, edit=TextEdit(start, end, ''))


def dispatch(text, cursor, session, key):
    """Apply one vim key. Unhandled insert-mode keys return `handled=False`."""
    cursor = max(0, min(cursor, len(text)))
    if session.mode == 'insert':
        if key == 'Escape':
            landed = retreat(text, cursor)
            return Outcome(replace(session, mode='normal', pending='', cursor=landed, anchor=landed, column=None),
                           landed, landed)
        return Outcome(session, cursor, session.anchor, handled=False)

    if key == 'ctrl-r':
        return Outcome(session, cursor, session.anchor, redo=True)
    if key == 'Escape':
        return Outcome(replace(session, mode='normal', pending='', cursor=cursor, anchor=cursor),
                       cursor, cursor)

    pending = session.pending
    if pending:
        session = replace(session, pending='')
        if pending == 'g' and key == 'g':
            return _move(session, text, motion_gg())
        if pending == 'd' and key == 'd':
            start, end, _ = _line_span(text, cursor)
            return _delete_between(session, text, start, end, linewise=True, cursor=cursor)
        if pending == 'd' and key == 'w':
            target = motion_w(text, cursor)
            if target == cursor:
                return _move(session, text, cursor)
            return _delete_between(session, text, cursor, target, linewise=False, cursor=cursor)
        if pending == 'y' and key == 'y':
            start, end, _ = _line_span(text, cursor)
            yanked = text[start:end]
            if yanked and not yanked.endswith('\n'):
                yanked += '\n'
            return Outcome(replace(session, pending='', register=yanked, linewise=True, cursor=cursor, anchor=cursor),
                           cursor, cursor)
        if pending == 'c' and key == 'w':
            end = _change_word_end(text, cursor)
            if end == cursor:
                updated = replace(session, mode='insert', pending='', cursor=cursor, anchor=cursor, column=None)
                return Outcome(updated, cursor, cursor)
            yanked = text[cursor:end]
            edit = TextEdit(cursor, end, '')
            return Outcome(replace(session, mode='insert', pending='', register=yanked, linewise=False,
                                   cursor=cursor, anchor=cursor, column=None),
                           cursor, cursor, edit=edit)
        # A cancelled operator still performs the key that followed it.

    if session.mode in ('visual', 'visual_line'):
        return _visual(text, cursor, session, key)

    motions = {
        'h': lambda: motion_h(text, cursor),
        'l': lambda: motion_l(text, cursor),
        'w': lambda: motion_w(text, cursor),
        'b': lambda: motion_b(text, cursor),
        'e': lambda: motion_e(text, cursor),
        '0': lambda: motion_zero(text, cursor),
        '$': lambda: motion_dollar(text, cursor),
        '^': lambda: motion_caret(text, cursor),
        'G': lambda: motion_G(text),
    }
    if key in motions:
        return _move(session, text, motions[key]())
    if key in ('j', 'k'):
        landed, column = motion_vertical(text, cursor, 1 if key == 'j' else -1, session.column)
        return _move(session, text, landed, column=column, keep_column=True)
    if key == 'g':
        return Outcome(replace(session, pending='g', cursor=cursor, anchor=cursor), cursor, cursor)
    if key == 'i':
        return Outcome(replace(session, mode='insert', pending='', cursor=cursor, anchor=cursor), cursor, cursor)
    if key == 'a':
        _, end = line_bounds(text, cursor)
        landed = min(end, cursor + 1)
        return Outcome(replace(session, mode='insert', pending='', cursor=landed, anchor=landed), landed, landed)
    if key == 'I':
        landed = motion_caret(text, cursor)
        return Outcome(replace(session, mode='insert', pending='', cursor=landed, anchor=landed), landed, landed)
    if key == 'A':
        _, end = line_bounds(text, cursor)
        return Outcome(replace(session, mode='insert', pending='', cursor=end, anchor=end), end, end)
    if key == 'o':
        _, end = line_bounds(text, cursor)
        return Outcome(replace(session, mode='insert', pending='', cursor=end + 1, anchor=end + 1, column=None),
                       end + 1, end + 1, edit=TextEdit(end, end, '\n'))
    if key == 'O':
        start, _ = line_bounds(text, cursor)
        return Outcome(replace(session, mode='insert', pending='', cursor=start, anchor=start, column=None),
                       start, start, edit=TextEdit(start, start, '\n'))
    if key == 'v':
        return Outcome(replace(session, mode='visual', pending='', anchor=cursor, cursor=cursor), cursor, cursor)
    if key == 'V':
        return Outcome(replace(session, mode='visual_line', pending='', anchor=cursor, cursor=cursor), cursor, cursor)
    if key == 'x':
        start, end = line_bounds(text, cursor)
        if cursor >= end:
            return _move(session, text, cursor)
        return _delete_between(session, text, cursor, cursor + 1, linewise=False, cursor=cursor)
    if key == 'd':
        return Outcome(replace(session, pending='d', cursor=cursor, anchor=cursor), cursor, cursor)
    if key == 'y':
        return Outcome(replace(session, pending='y', cursor=cursor, anchor=cursor), cursor, cursor)
    if key == 'c':
        return Outcome(replace(session, pending='c', cursor=cursor, anchor=cursor), cursor, cursor)
    if key == 'p':
        return _paste(text, cursor, session)
    if key == 'u':
        return Outcome(session, cursor, cursor, undo=True)
    return Outcome(replace(session, pending='', cursor=cursor, anchor=cursor), cursor, cursor)


def _visual(text, cursor, session, key):
    linewise = session.mode == 'visual_line'
    anchor = session.anchor
    if key == 'v' and session.mode == 'visual':
        return Outcome(replace(session, mode='normal', pending='', cursor=cursor, anchor=cursor), cursor, cursor)
    if key == 'V' and session.mode == 'visual_line':
        return Outcome(replace(session, mode='normal', pending='', cursor=cursor, anchor=cursor), cursor, cursor)
    if key == 'v':
        return Outcome(replace(session, mode='visual', pending='', anchor=anchor, cursor=cursor), cursor, anchor)
    if key == 'V':
        return Outcome(replace(session, mode='visual_line', pending='', anchor=anchor, cursor=cursor), cursor, anchor)
    motions = {
        'h': lambda: motion_h(text, cursor),
        'l': lambda: motion_l(text, cursor),
        'w': lambda: motion_w(text, cursor),
        'b': lambda: motion_b(text, cursor),
        'e': lambda: motion_e(text, cursor),
        '0': lambda: motion_zero(text, cursor),
        '$': lambda: motion_dollar(text, cursor),
        '^': lambda: motion_caret(text, cursor),
        'G': lambda: motion_G(text),
        'g': lambda: motion_gg(),
    }
    if key in motions:
        if key == 'g' and session.pending != 'g':
            # `gg` in visual mode: the first g is stored, matching normal mode.
            return Outcome(replace(session, pending='g', cursor=cursor, anchor=anchor), cursor, anchor)
        landed = motions[key]()
        return Outcome(replace(session, pending='', cursor=landed, anchor=anchor), landed, anchor)
    if key in ('j', 'k'):
        landed, column = motion_vertical(text, cursor, 1 if key == 'j' else -1, session.column)
        return Outcome(replace(session, pending='', cursor=landed, anchor=anchor, column=column), landed, anchor)
    if key in ('x', 'd', 'y'):
        start, end = _visual_range(text, anchor, cursor, linewise)
        if start == end:
            return Outcome(replace(session, mode='normal', pending='', cursor=cursor, anchor=cursor), cursor, cursor)
        if key == 'y':
            yanked = text[start:end]
            return Outcome(replace(session, mode='normal', pending='', register=yanked, linewise=linewise,
                                   cursor=start, anchor=start, column=None),
                           start, start)
        return _delete_between(session, text, start, end, linewise=linewise, cursor=start)
    return Outcome(replace(session, pending='', cursor=cursor, anchor=anchor), cursor, anchor)


def _paste(text, cursor, session):
    if not session.register:
        return Outcome(replace(session, pending='', cursor=cursor, anchor=cursor), cursor, cursor)
    if session.linewise:
        _, end = line_bounds(text, cursor)
        insert_at = end + 1 if end < len(text) else end
        payload = session.register if session.register.endswith('\n') else session.register + '\n'
        if end >= len(text):
            payload = '\n' + session.register.rstrip('\n')
            insert_at = end
        landed = insert_at if end < len(text) else end + 1
        return Outcome(replace(session, pending='', cursor=landed, anchor=landed, column=None),
                       landed, landed, edit=TextEdit(insert_at, insert_at, payload))
    _, end = line_bounds(text, cursor)
    insert_at = cursor + 1 if cursor < end else cursor
    payload = session.register
    landed = insert_at + max(0, len(payload) - 1)
    return Outcome(replace(session, pending='', cursor=landed, anchor=landed, column=None),
                   landed, landed, edit=TextEdit(insert_at, insert_at, payload))


class VimState:
    """Per-note mode. `active` follows the vim_mode preference."""

    def __init__(self):
        self.active = False
        self.session = Session()


def mode_label(session):
    return {'normal': 'NORMAL', 'insert': 'INSERT', 'visual': 'VISUAL', 'visual_line': 'VISUAL'}.get(
        session.mode, 'NORMAL')


def display_selection(text, session):
    """GTK selection for the session. Visual ranges cover the moved cursor."""
    if session.mode == 'visual':
        return _visual_range(text, session.anchor, session.cursor, False)
    if session.mode == 'visual_line':
        return _visual_range(text, session.anchor, session.cursor, True)
    return session.cursor, session.cursor
