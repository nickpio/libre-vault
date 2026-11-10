"""Turn LaTeX that mathtext cannot parse into something it can draw.

Matplotlib renders one mathtext string at a time. This module expands macros
and lays out aligned, gathered, case, and matrix environments as a grid of
those strings.
"""
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import math
import re

import cairo


_ENVIRONMENTS = {
    'aligned': ('align', None),
    'align': ('align', None),
    'align*': ('align', None),
    'gather': ('gather', None),
    'gather*': ('gather', None),
    'cases': ('cases', ('{', '')),
    'matrix': ('matrix', None),
    'pmatrix': ('matrix', ('(', ')')),
    'bmatrix': ('matrix', ('[', ']')),
    'vmatrix': ('matrix', ('|', '|')),
    'Vmatrix': ('matrix', ('||', '||')),
}
_BRACED = {
    'textbf': lambda inner: r'\mathbf{' + inner + '}',
    'textit': lambda inner: r'\mathit{' + inner + '}',
    'texttt': lambda inner: r'\mathtt{' + inner + '}',
}
_SIMPLE = {
    'implies': r'\Rightarrow',
    'iff': r'\Leftrightarrow',
    'impliedby': r'\Leftarrow',
    'le': r'\leq',
    'ge': r'\geq',
}
_NAMES = sorted(set(_BRACED) | set(_SIMPLE), key=len, reverse=True)
_BEGIN = re.compile(r'\\begin\{([A-Za-z*]+)\}')
_NEWCOMMAND = re.compile(r'\\(?:re)?newcommand\s*\*?\s*')


@dataclass(frozen=True)
class MathLayout:
    mode: str
    rows: list
    delimiter: tuple | None
    prefix: str
    suffix: str


def prepare_math(expression):
    """Expand macros. Return a string, or a layout when the expression is an environment."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError('Empty math expression')
    text = apply_macros(expand_newcommands(expression.strip()))
    found = _find_environment(text)
    if found is None:
        return text
    name, inner, prefix, suffix = found
    mode, delimiter = _ENVIRONMENTS[name]
    rows = []
    for raw in _split_rows(inner):
        cells = [cell.strip() for cell in _split_columns(raw)]
        if any(cells):
            rows.append(cells)
    if not rows:
        raise ValueError(f'Empty math environment: {name}')
    return MathLayout(mode, rows, delimiter, prefix, suffix)


@lru_cache(maxsize=256)
def render_math_png(expression, color, dpi=150):
    """Render one expression to a transparent PNG, using the active theme color."""
    prepared = prepare_math(expression)
    if isinstance(prepared, str):
        return _render_simple(prepared, color, dpi)
    return _png(_layout_surface(prepared, color, dpi))


def expand_newcommands(expression):
    definitions = []
    index_by_name = {}
    output = []
    position = 0
    while position < len(expression):
        if expression.startswith('\\newcommand', position) or expression.startswith('\\renewcommand', position):
            parsed = _take_newcommand(expression, position)
            if parsed is not None:
                name, nargs, body, end = parsed
                if name in index_by_name:
                    definitions[index_by_name[name]] = (name, nargs, body)
                else:
                    index_by_name[name] = len(definitions)
                    definitions.append((name, nargs, body))
                position = end
                continue
        output.append(expression[position])
        position += 1
    text = ''.join(output)
    for _ in range(30):
        if len(text) > 20000:
            raise ValueError('Math macro expansion is too large')
        changed = False
        for name, nargs, body in definitions:
            text, did = _substitute(text, name, nargs, body)
            changed = changed or did
        if not changed:
            break
    if len(text) > 20000:
        raise ValueError('Math macro expansion is too large')
    return text


def apply_macros(text):
    """Rewrite commands mathtext rejects. Leave \\mathbb, \\text, and native symbols alone."""
    output = []
    position = 0
    while position < len(text):
        if text[position] != '\\':
            output.append(text[position])
            position += 1
            continue
        matched = None
        for name in _NAMES:
            if not text.startswith(name, position + 1):
                continue
            after = position + 1 + len(name)
            if after < len(text) and text[after].isalpha():
                continue
            matched = name
            break
        if matched is None:
            output.append(text[position])
            position += 1
            continue
        after = position + 1 + len(matched)
        if matched in _SIMPLE:
            output.append(_SIMPLE[matched])
            position = after
            continue
        if after < len(text) and text[after] == '{':
            inner, after = _read_brace(text, after)
            output.append(_BRACED[matched](inner))
            position = after
            continue
        output.append(text[position:after])
        position = after
    return ''.join(output)


def _take_newcommand(expression, start):
    found = _NEWCOMMAND.match(expression, start)
    if not found:
        return None
    position = found.end()
    if position >= len(expression) or expression[position] != '{':
        return None
    name_text, position = _read_brace(expression, position)
    name = name_text.strip()
    if name.startswith('\\'):
        name = name[1:]
    if not re.fullmatch(r'[A-Za-z]+', name):
        return None
    nargs = 0
    if position < len(expression) and expression[position] == '[':
        close = expression.find(']', position)
        if close < 0:
            raise ValueError('Unclosed newcommand argument count')
        nargs = int(expression[position + 1:close].strip() or '0')
        position = close + 1
    while position < len(expression) and expression[position].isspace():
        position += 1
    if position >= len(expression) or expression[position] != '{':
        raise ValueError('newcommand is missing a replacement')
    body, position = _read_brace(expression, position)
    return name, nargs, body, position


def _substitute(expression, name, nargs, body):
    needle = '\\' + name
    output = []
    position = 0
    changed = False
    while position < len(expression):
        found = expression.find(needle, position)
        if found < 0:
            output.append(expression[position:])
            break
        after = found + len(needle)
        if after < len(expression) and expression[after].isalpha():
            output.append(expression[position:after])
            position = after
            continue
        arguments = []
        cursor = after
        complete = True
        for _ in range(nargs):
            while cursor < len(expression) and expression[cursor].isspace():
                cursor += 1
            if cursor >= len(expression) or expression[cursor] != '{':
                complete = False
                break
            argument, cursor = _read_brace(expression, cursor)
            arguments.append(argument)
        if not complete:
            output.append(expression[position:after])
            position = after
            continue
        replacement = body
        for index, argument in enumerate(arguments, 1):
            replacement = replacement.replace(f'#{index}', argument)
        output.append(expression[position:found])
        output.append(replacement)
        position = cursor
        changed = True
    return ''.join(output), changed


def _read_brace(text, open_index):
    if open_index >= len(text) or text[open_index] != '{':
        raise ValueError('Expected a brace group in math expression')
    depth = 0
    position = open_index
    while position < len(text):
        if text[position] == '\\':
            position += 2
            continue
        if text[position] == '{':
            depth += 1
        elif text[position] == '}':
            depth -= 1
            if depth == 0:
                return text[open_index + 1:position], position + 1
        position += 1
    raise ValueError('Unclosed brace in math expression')


def _find_environment(text):
    found = _BEGIN.search(text)
    if not found:
        return None
    name = found.group(1)
    if name not in _ENVIRONMENTS:
        raise ValueError(f'Unsupported math environment: {name}')
    closer = '\\end{' + name + '}'
    end = text.find(closer, found.end())
    if end < 0:
        raise ValueError(f'Unclosed math environment: {name}')
    return name, text[found.end():end], text[:found.start()].strip(), text[end + len(closer):].strip()


def _split_rows(text):
    return _split_outside(text, '\\\\', True)


def _split_columns(text):
    return _split_outside(text, '&', False)


def _split_outside(text, separator, skip_space):
    parts = []
    current = []
    depth = 0
    position = 0
    size = len(separator)
    while position < len(text):
        if depth == 0 and text.startswith(separator, position):
            parts.append(''.join(current))
            current = []
            position += size
            if skip_space and position < len(text) and text[position] == '[':
                close = text.find(']', position)
                if close >= 0:
                    position = close + 1
            continue
        if text[position] == '\\':
            current.append(text[position:position + 2] if position + 1 < len(text) else text[position])
            position += 2 if position + 1 < len(text) else 1
            continue
        if text[position] == '{':
            depth += 1
        elif text[position] == '}':
            depth = max(0, depth - 1)
        current.append(text[position])
        position += 1
    parts.append(''.join(current))
    return parts


def _render_simple(expression, color, dpi):
    expression = expression.strip()
    if not expression:
        raise ValueError('Empty math expression')
    from matplotlib import rc_context
    from matplotlib.mathtext import math_to_image
    output = BytesIO()
    with rc_context({'savefig.transparent': True}):
        math_to_image('$' + expression + '$', output, dpi=dpi, format='png', color=color)
    return output.getvalue()


def _layout_surface(layout, color, dpi):
    rows = [[_piece_surface(cell, color, dpi) for cell in row] for row in layout.rows]
    columns = max(len(row) for row in rows)
    gap = {'align': 2, 'gather': 0, 'matrix': 18, 'cases': 16}[layout.mode]
    row_gap = 8
    column_widths = [0] * columns
    row_heights = []
    for row in rows:
        height = 1
        for index, surface in enumerate(row):
            column_widths[index] = max(column_widths[index], surface.get_width())
            height = max(height, surface.get_height())
        row_heights.append(height)
    grid_w = sum(column_widths) + gap * max(0, columns - 1)
    grid_h = sum(row_heights) + row_gap * max(0, len(row_heights) - 1)
    prefix = _piece_surface(layout.prefix, color, dpi) if layout.prefix else None
    suffix = _piece_surface(layout.suffix, color, dpi) if layout.suffix else None
    left, right = layout.delimiter or ('', '')
    left_w = _delimiter_width(left)
    right_w = _delimiter_width(right)
    pad = 8
    prefix_gap = 8 if prefix is not None else 0
    suffix_gap = 8 if suffix is not None else 0
    width = pad * 2 + (prefix.get_width() if prefix else 0) + prefix_gap + left_w + grid_w + right_w + suffix_gap + (suffix.get_width() if suffix else 0)
    height = pad * 2 + max(grid_h, prefix.get_height() if prefix else 0, suffix.get_height() if suffix else 0)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(math.ceil(width))), max(1, int(math.ceil(height))))
    context = cairo.Context(surface)
    ink = _rgb(color)
    top = pad + max(0, (height - pad * 2 - grid_h) / 2)
    cursor = pad
    if prefix is not None:
        context.set_source_surface(prefix, cursor, pad + (height - pad * 2 - prefix.get_height()) / 2)
        context.paint()
        cursor += prefix.get_width() + prefix_gap
    if left:
        _stroke_delimiter(context, left, cursor, top, grid_h, ink, True)
        cursor += left_w
    y = top
    for row, row_h in zip(rows, row_heights):
        x = cursor
        for index in range(columns):
            piece = row[index] if index < len(row) else None
            if piece is not None:
                context.set_source_surface(piece, x + _align_x(layout.mode, index, column_widths[index], piece.get_width()),
                                           y + (row_h - piece.get_height()) / 2)
                context.paint()
            x += column_widths[index] + (gap if index < columns - 1 else 0)
        y += row_h + row_gap
    cursor += grid_w
    if right:
        _stroke_delimiter(context, right, cursor + right_w - 2, top, grid_h, ink, False)
        cursor += right_w
    if suffix is not None:
        context.set_source_surface(suffix, cursor + suffix_gap, pad + (height - pad * 2 - suffix.get_height()) / 2)
        context.paint()
    return surface


def _piece_surface(expression, color, dpi):
    expression = expression.strip()
    if not expression:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        return surface
    if '\\begin{' in expression:
        prepared = prepare_math(expression)
        if not isinstance(prepared, str):
            return _layout_surface(prepared, color, dpi)
        expression = prepared
    return cairo.ImageSurface.create_from_png(BytesIO(_render_simple(expression, color, dpi)))


def _align_x(mode, column, column_width, piece_width):
    if mode == 'align':
        return column_width - piece_width if column % 2 == 0 else 0
    if mode in ('gather', 'matrix'):
        return (column_width - piece_width) / 2
    return 0


def _delimiter_width(kind):
    if not kind:
        return 0
    if kind == '|':
        return 10
    if kind == '||':
        return 14
    return 18


def _stroke_delimiter(context, kind, x, top, height, color, left):
    context.set_source_rgb(*color)
    context.set_line_width(1.7)
    bottom = top + height
    if kind == '|':
        context.move_to(x, top)
        context.line_to(x, bottom)
        context.stroke()
        return
    if kind == '||':
        shift = -4 if left else 4
        context.move_to(x, top)
        context.line_to(x, bottom)
        context.move_to(x + shift, top)
        context.line_to(x + shift, bottom)
        context.stroke()
        return
    if kind in '[]':
        arm = 8
        if left:
            context.move_to(x + arm, top)
            context.line_to(x, top)
            context.line_to(x, bottom)
            context.line_to(x + arm, bottom)
        else:
            context.move_to(x - arm, top)
            context.line_to(x, top)
            context.line_to(x, bottom)
            context.line_to(x - arm, bottom)
        context.stroke()
        return
    if kind in '()':
        sign = 1 if left else -1
        context.move_to(x + sign * 10, top)
        context.curve_to(x - sign * 6, top + height * 0.25, x - sign * 6, bottom - height * 0.25, x + sign * 10, bottom)
        context.stroke()
        return
    mid = (top + bottom) / 2
    context.move_to(x + 11, top)
    context.curve_to(x + 2, top + 2, x + 2, mid - 8, x, mid)
    context.curve_to(x + 2, mid + 8, x + 2, bottom - 2, x + 11, bottom)
    context.stroke()


def _rgb(color):
    text = str(color).strip()
    if len(text) == 7 and text.startswith('#'):
        return tuple(int(text[index:index + 2], 16) / 255 for index in (1, 3, 5))
    return (0.93, 0.93, 0.93)


def _png(surface):
    output = BytesIO()
    surface.write_to_png(output)
    return output.getvalue()
