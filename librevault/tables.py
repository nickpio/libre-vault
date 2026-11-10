"""GFM Markdown tables: source ranges, one undoable edit, and a cell editor.

The buffer stays the note. Every change replaces one table span so undo
restores the previous Markdown in a single step. ``caret`` is the buffer
offset to select after that edit is applied.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Cell:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class Location:
    row: int
    column: int
    body_index: int | None


@dataclass(frozen=True)
class Table:
    """One GFM table. ``cells`` includes the header, delimiter, and body rows."""
    start: int
    end: int
    headers: list
    alignments: list
    rows: list
    cells: list
    line_starts: tuple


_ALIGNMENTS = ('left', 'center', 'right')
_SEPARATOR = re.compile(r':?-{3,}:?')


def _edit(start, end, replacement, caret):
    """A ``TextEdit`` plus the caret that should follow it."""
    from .editing import TextEdit

    if not hasattr(_edit, 'kind'):
        @dataclass(frozen=True)
        class TableChange(TextEdit):
            caret: int = 0

        _edit.kind = TableChange
    return _edit.kind(start, end, replacement, caret)


def line_bounds(text):
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return lines, offsets


def protected_lines(lines):
    """Frontmatter and fenced code stay literal, including pipe characters."""
    skip = set()
    index = 0
    if lines and lines[0].strip() == '---':
        for cursor in range(1, len(lines)):
            if lines[cursor].strip() in ('---', '...'):
                skip.update(range(cursor + 1))
                index = cursor + 1
                break
    fence = None
    while index < len(lines):
        match = re.match(r' {0,3}(`{3,}|~{3,})', lines[index])
        if fence is None and match:
            fence = (match.group(1)[0], len(match.group(1)))
            skip.add(index)
        elif fence is not None:
            skip.add(index)
            close = re.match(r' {0,3}(`{3,}|~{3,})\s*$', lines[index])
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= fence[1]:
                fence = None
        index += 1
    return skip


def parse_row(line, line_start):
    """Split one table row. Pipes inside code spans or escaped pipes stay put."""
    raw = line.rstrip('\r\n')
    if '|' not in raw:
        return None
    length = len(raw)
    index = 0
    while index < length and raw[index] in ' \t':
        index += 1
    if index >= length:
        return None
    saw_pipe = False
    if raw[index] == '|':
        saw_pipe = True
        index += 1
    cells = []
    while True:
        cell_start = index
        ticks = 0
        while index < length:
            if ticks:
                if raw.startswith('`' * ticks, index):
                    index += ticks
                    ticks = 0
                else:
                    index += 1
                continue
            char = raw[index]
            if char == '\\' and index + 1 < length:
                index += 2
                continue
            if char == '`':
                ticks = 1
                while index + ticks < length and raw[index + ticks] == '`':
                    ticks += 1
                index += ticks
                continue
            if char == '|':
                saw_pipe = True
                break
            index += 1
        left, right = cell_start, index
        while left < right and raw[left] in ' \t':
            left += 1
        while right > left and raw[right - 1] in ' \t':
            right -= 1
        cells.append(Cell(raw[left:right], line_start + left, line_start + right))
        if index < length and raw[index] == '|':
            index += 1
            if index >= length:
                break
            continue
        break
    if not saw_pipe or not cells:
        return None
    return cells


def _alignments(cells):
    found = []
    for cell in cells:
        if not _SEPARATOR.fullmatch(cell.text):
            return None
        left, right = cell.text.startswith(':'), cell.text.endswith(':')
        if left and right:
            found.append('center')
        elif right:
            found.append('right')
        else:
            found.append('left')
    return found or None


def _pad(cells, columns, edge):
    sized = list(cells[:columns])
    while len(sized) < columns:
        sized.append(Cell('', edge, edge))
    return sized


def find_tables(text):
    """Every GFM table in source order, excluding fenced code and frontmatter."""
    lines, offsets = line_bounds(text)
    skip = protected_lines(lines)
    tables = []
    index = 0
    while index < len(lines) - 1:
        if index in skip or index + 1 in skip or re.match(r'(?: {4}|\t)', lines[index]):
            index += 1
            continue
        header = parse_row(lines[index], offsets[index])
        separator = parse_row(lines[index + 1], offsets[index + 1])
        alignments = _alignments(separator) if separator else None
        if not header or not alignments:
            index += 1
            continue
        columns = max(len(header), len(alignments))
        if len(alignments) < columns:
            alignments = alignments + ['left'] * (columns - len(alignments))
        else:
            alignments = alignments[:columns]
        header_edge = offsets[index] + len(lines[index].rstrip('\r\n'))
        body_cells = []
        body_text = []
        line_indexes = [index, index + 1]
        cursor = index + 2
        while cursor < len(lines) and cursor not in skip and lines[cursor].strip():
            if re.match(r'(?: {4}|\t)', lines[cursor]):
                break
            row = parse_row(lines[cursor], offsets[cursor])
            if row is None:
                break
            edge = offsets[cursor] + len(lines[cursor].rstrip('\r\n'))
            sized = _pad(row, columns, edge)
            body_cells.append(sized)
            body_text.append([cell.text for cell in sized])
            line_indexes.append(cursor)
            cursor += 1
        last = line_indexes[-1]
        separator_edge = offsets[index + 1] + len(lines[index + 1].rstrip('\r\n'))
        tables.append(Table(
            offsets[index], offsets[last] + len(lines[last]),
            [cell.text for cell in _pad(header, columns, header_edge)],
            alignments, body_text,
            [_pad(header, columns, header_edge), _pad(separator, columns, separator_edge), *body_cells],
            tuple(offsets[number] for number in line_indexes)))
        index = last + 1
    return tables


def table_contains(text, table, cursor):
    if table.start <= cursor < table.end:
        return True
    return cursor == table.end == len(text) and not text.endswith('\n')


def table_at(text, cursor):
    """The table containing ``cursor``, or None when the caret is outside one."""
    if cursor < 0 or cursor > len(text):
        return None
    for table in find_tables(text):
        if table_contains(text, table, cursor):
            return table
    return None


def column_at(cells, cursor):
    if not cells:
        return 0
    if cursor <= cells[0].start:
        return 0
    for index, cell in enumerate(cells):
        following = cells[index + 1].start if index + 1 < len(cells) else None
        if cell.start <= cursor <= cell.end:
            return index
        if following is None or cursor < following:
            return index
    return len(cells) - 1


def locate(text, table, cursor):
    row_index = len(table.line_starts) - 1
    for index, start in enumerate(table.line_starts):
        end = table.line_starts[index + 1] if index + 1 < len(table.line_starts) else table.end
        if start <= cursor < end or (index == len(table.line_starts) - 1
                                     and cursor == end == len(text) and not text.endswith('\n')):
            row_index = index
            break
    column = column_at(table.cells[row_index], cursor)
    return Location(row_index, column, row_index - 2 if row_index >= 2 else None)


def escape_cell(text):
    # A physical newline terminates a GFM row. Keep pasted paragraphs in the cell.
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br>')
    pieces = []
    index = 0
    while index < len(text):
        if text[index] == '\\' and index + 1 < len(text):
            pieces.append(text[index:index + 2])
            index += 2
            continue
        if text[index] == '|':
            pieces.append('\\|')
        else:
            pieces.append(text[index])
        index += 1
    return ''.join(pieces)


def _widths(headers, alignments, rows):
    widths = []
    for column, alignment in enumerate(alignments):
        values = [headers[column], *(row[column] for row in rows)]
        minimum = {'center': 5, 'right': 4}.get(alignment, 4)
        widths.append(min(80, max([minimum, *(len(escape_cell(value)) for value in values)])))
    return widths


def _separator(width, alignment):
    if alignment == 'center':
        return ':' + '-' * (width - 2) + ':'
    if alignment == 'right':
        return '-' * (width - 1) + ':'
    return ':' + '-' * (width - 1)


def format_table(headers, alignments, rows):
    """Render a padded GFM table. ``rows`` are body rows and do not include the header."""
    columns = len(headers)
    if columns < 1:
        return ''
    alignments = list(alignments[:columns]) + ['left'] * max(0, columns - len(alignments))
    body = []
    for row in rows:
        values = list(row[:columns])
        values.extend('' for _ in range(columns - len(values)))
        body.append(values)
    widths = _widths(headers, alignments, body)

    def render(values):
        padded = []
        for column, value in enumerate(values):
            text = escape_cell(value)
            alignment = alignments[column]
            if alignment == 'right':
                text = text.rjust(widths[column])
            elif alignment == 'center':
                text = text.center(widths[column])
            else:
                text = text.ljust(widths[column])
            padded.append(text)
        return '| ' + ' | '.join(padded) + ' |'

    separator = '| ' + ' | '.join(_separator(widths[column], alignments[column])
                                  for column in range(columns)) + ' |'
    return '\n'.join([render(list(headers)), separator, *(render(row) for row in body)])


def _split_pipes(line):
    """Split a formatted row on unescaped pipes, preserving each source length."""
    parts, buf, index = [], [], 0
    while index < len(line):
        if line[index] == '\\' and index + 1 < len(line):
            buf.append(line[index:index + 2])
            index += 2
            continue
        if line[index] == '|':
            parts.append(''.join(buf))
            buf = []
            index += 1
            continue
        buf.append(line[index])
        index += 1
    parts.append(''.join(buf))
    return parts


def cell_offset(formatted, line_index, column):
    """Caret offset of a cell inside ``formatted`` (no extra trailing newline)."""
    lines = formatted.split('\n')
    line_index = max(0, min(line_index, len(lines) - 1))
    prefix = sum(len(line) + 1 for line in lines[:line_index])
    line = lines[line_index]
    parts = _split_pipes(line)
    inner = parts[1:-1] if line.startswith('|') and len(parts) > 2 else parts
    if not inner:
        return prefix
    column = max(0, min(column, len(inner) - 1))
    offset = 1 if line.startswith('|') else 0
    for index, part in enumerate(inner):
        if index == column:
            if not part.strip():
                return prefix + offset + (1 if part.startswith(' ') else 0)
            lead = len(part) - len(part.lstrip(' '))
            return prefix + offset + lead
        offset += len(part) + 1
    return prefix + len(line)


def default_table(rows=2, cols=2):
    columns = max(1, int(cols))
    body_rows = max(0, int(rows))
    headers = [f'Column {index + 1}' for index in range(columns)]
    alignments = ['left'] * columns
    body = [['' for _ in range(columns)] for _ in range(body_rows)]
    return headers, alignments, body


def insert_table(rows=2, cols=2):
    """Markdown for a new table, as an insertion at offset 0.

    ``rows`` is the number of empty body rows under the header. Move
    ``start``, ``end``, and ``caret`` together to insert at another offset.
    """
    headers, alignments, body = default_table(rows, cols)
    replacement = format_table(headers, alignments, body)
    return _edit(0, 0, replacement, cell_offset(replacement, 0, 0))


def _normalized(headers, alignments, rows):
    columns = len(headers)
    alignments = [('center' if item == 'center' else 'right' if item == 'right' else 'left')
                  for item in list(alignments[:columns])]
    alignments.extend('left' for _ in range(columns - len(alignments)))
    body = []
    for row in rows:
        values = [str(cell) for cell in list(row[:columns])]
        values.extend('' for _ in range(columns - len(values)))
        body.append(values)
    return list(headers), alignments, body


def _commit(text, table, headers, alignments, rows, line_index, column):
    headers, alignments, rows = _normalized(headers, alignments, rows)
    if not headers:
        return _edit(table.start, table.end, '', table.start)
    formatted = format_table(headers, alignments, rows)
    original = text[table.start:table.end]
    replacement = formatted + ('\n' if original.endswith('\n') else '')
    # line_index 0 is the header, 1 the delimiter, and 2+ body lines.
    target = 0 if line_index <= 0 else line_index if line_index == 1 else line_index
    if line_index >= 2:
        target = min(line_index, 1 + len(rows))
    caret = table.start + cell_offset(formatted, target if target < formatted.count('\n') + 1 else 0, column)
    return _edit(table.start, table.end, replacement, min(caret, table.start + len(formatted)))


def _with_table(text, cursor, mutator):
    table = table_at(text, cursor)
    if table is None:
        return None
    headers = list(table.headers)
    alignments = list(table.alignments)
    rows = [list(row) for row in table.rows]
    location = locate(text, table, cursor)
    result = mutator(headers, alignments, rows, location)
    if result is None:
        return None
    headers, alignments, rows, line_index, column = result
    return _commit(text, table, headers, alignments, rows, line_index, column)


def table_add_row_below(text, cursor):
    def mutate(headers, alignments, rows, location):
        blank = ['' for _ in headers]
        index = 0 if location.body_index is None else location.body_index + 1
        rows.insert(index, blank)
        return headers, alignments, rows, index + 2, location.column

    return _with_table(text, cursor, mutate)


def table_add_row_above(text, cursor):
    def mutate(headers, alignments, rows, location):
        blank = ['' for _ in headers]
        index = 0 if location.body_index is None else location.body_index
        rows.insert(index, blank)
        return headers, alignments, rows, index + 2, location.column

    return _with_table(text, cursor, mutate)


def table_delete_row(text, cursor):
    def mutate(headers, alignments, rows, location):
        if location.body_index is None or not rows:
            return None
        rows.pop(location.body_index)
        line = 0 if not rows else min(location.body_index, len(rows) - 1) + 2
        if not rows:
            line = 0
        return headers, alignments, rows, line, location.column

    return _with_table(text, cursor, mutate)


def _insert_column(offset):
    def mutate(headers, alignments, rows, location):
        column = min(max(location.column + offset, 0), len(headers))
        headers.insert(column, f'Column {len(headers) + 1}')
        alignments.insert(column, 'left')
        for row in rows:
            row.insert(column, '')
        line = location.row if location.row != 1 else 0
        return headers, alignments, rows, line, column

    return mutate


def table_add_column_right(text, cursor):
    return _with_table(text, cursor, _insert_column(1))


def table_add_column_left(text, cursor):
    return _with_table(text, cursor, _insert_column(0))


def table_delete_column(text, cursor):
    def mutate(headers, alignments, rows, location):
        if len(headers) <= 1:
            return [], [], [], 0, 0
        column = min(location.column, len(headers) - 1)
        del headers[column]
        del alignments[column]
        for row in rows:
            if column < len(row):
                del row[column]
        line = 0 if location.row <= 1 else location.row
        return headers, alignments, rows, line, min(column, len(headers) - 1)

    return _with_table(text, cursor, mutate)


def table_align_column(text, cursor, alignment):
    if alignment not in _ALIGNMENTS:
        return None

    def mutate(headers, alignments, rows, location):
        alignments[location.column] = alignment
        line = 0 if location.row <= 1 else location.row
        return headers, alignments, rows, line, location.column

    return _with_table(text, cursor, mutate)


def sort_rows(rows, column, ascending=True):
    """Sort body rows. Numbers and words sort separately; blank cells stay last."""
    numbers, words, empty = [], [], []
    for row in rows:
        value = row[column].strip()
        if not value:
            empty.append(row)
            continue
        try:
            float(value)
        except ValueError:
            words.append(row)
        else:
            numbers.append(row)
    numbers.sort(key=lambda row: float(row[column]), reverse=not ascending)
    words.sort(key=lambda row: row[column].casefold(), reverse=not ascending)
    return numbers + words + empty


def table_sort_column(text, cursor, ascending=True):
    def mutate(headers, alignments, rows, location):
        if not rows:
            return None
        ordered = sort_rows(rows, location.column, ascending)
        line = 0 if location.row <= 1 else location.row
        return headers, alignments, ordered, line, location.column

    return _with_table(text, cursor, mutate)


def table_move_row(text, cursor, delta):
    def mutate(headers, alignments, rows, location):
        if location.body_index is None or not rows:
            return None
        target = location.body_index + delta
        if target < 0 or target >= len(rows):
            return None
        rows[location.body_index], rows[target] = rows[target], rows[location.body_index]
        return headers, alignments, rows, target + 2, location.column

    return _with_table(text, cursor, mutate)


def table_move_column(text, cursor, delta):
    def mutate(headers, alignments, rows, location):
        target = location.column + delta
        if target < 0 or target >= len(headers):
            return None
        for collection in (headers, alignments, *rows):
            collection[location.column], collection[target] = collection[target], collection[location.column]
        line = 0 if location.row <= 1 else location.row
        return headers, alignments, rows, line, target

    return _with_table(text, cursor, mutate)


def _move_caret(text, table, row, column):
    cell = table.cells[row][column]
    caret = cell.start
    return _edit(caret, caret, '', caret)


def table_next_cell(text, cursor):
    """Move to the next cell. Tab on the last cell appends a body row."""
    table = table_at(text, cursor)
    if table is None:
        return None
    location = locate(text, table, cursor)
    columns = len(table.headers)
    column = min(location.column, columns - 1)
    if location.row == 0:
        if column + 1 < columns:
            return _move_caret(text, table, 0, column + 1)
        if table.rows:
            return _move_caret(text, table, 2, 0)
        return table_add_row_below(text, cursor)
    if location.row == 1:
        if table.rows:
            return _move_caret(text, table, 2, column)
        return table_add_row_below(text, cursor)
    if column + 1 < columns:
        return _move_caret(text, table, location.row, column + 1)
    if location.body_index + 1 < len(table.rows):
        return _move_caret(text, table, location.row + 1, 0)
    return table_add_row_below(text, cursor)


def table_break_after_row(text, cursor):
    """End the table after the complete current row, without splitting a cell."""
    table = table_at(text, cursor)
    if table is None:
        return None
    # Keep the header and its required delimiter together.
    row = max(1, locate(text, table, cursor).row)
    edge = table.line_starts[row + 1] if row + 1 < len(table.line_starts) else table.end
    line = text[table.line_starts[row]:edge]
    newline = '\r\n' if line.endswith('\r\n') else '\n'
    if line.endswith('\n'):
        return _edit(edge, edge, newline, edge)
    return _edit(edge, edge, newline * 2, edge + len(newline))


def table_previous_cell(text, cursor):
    """Move to the previous cell. The first cell keeps the caret there."""
    table = table_at(text, cursor)
    if table is None:
        return None
    location = locate(text, table, cursor)
    columns = len(table.headers)
    column = min(location.column, columns - 1)
    if location.row >= 2:
        if column > 0:
            return _move_caret(text, table, location.row, column - 1)
        if location.body_index > 0:
            return _move_caret(text, table, location.row - 1, columns - 1)
        return _move_caret(text, table, 0, columns - 1)
    if location.row == 1:
        return _move_caret(text, table, 0, column)
    if column > 0:
        return _move_caret(text, table, 0, column - 1)
    return _move_caret(text, table, 0, 0)


def table_from_model(text, cursor, headers, alignments, rows):
    """Replace the table around ``cursor`` with the edited model."""
    table = table_at(text, cursor)
    if table is None:
        return None
    location = locate(text, table, cursor)
    line = 0 if location.row <= 1 else min(location.row, 1 + max(len(rows), 1))
    column = min(location.column, max(len(headers) - 1, 0))
    return _commit(text, table, headers, alignments, rows, line, column)
