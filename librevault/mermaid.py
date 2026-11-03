"""Draw a useful Mermaid subset with Cairo and Pango.

Flowcharts, sequence diagrams, state diagrams, and class diagrams become
images. Anything else becomes a styled source card instead of a parser error.
"""
from dataclasses import dataclass, field
from io import BytesIO
import math
import re

import cairo
import gi
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo


MAX_SOURCE = 20000
MAX_LINES = 400
MAX_NODES = 200

_FLOW_HEADER = re.compile(r'^(?:graph|flowchart)(?:\s+(TD|TB|BT|LR|RL))?\s*$', re.I)
_SEQUENCE_HEADER = re.compile(r'^sequenceDiagram\s*$', re.I)
_STATE_HEADER = re.compile(r'^stateDiagram(?:-v2)?\s*$', re.I)
_CLASS_HEADER = re.compile(r'^classDiagram\s*$', re.I)
_DIRECTION = re.compile(r'^direction\s+(TD|TB|BT|LR|RL)\s*$', re.I)
_NODE_SHAPES = (
    (re.compile(r'^([A-Za-z_][\w]*)\s*\[([^\[\]]*)\]$'), 'rect', True),
    (re.compile(r'^([A-Za-z_][\w]*)\s*\(([^()]*)\)$'), 'round', True),
    (re.compile(r'^([A-Za-z_][\w]*)\s*\{([^{}]*)\}$'), 'diamond', True),
    (re.compile(r'^([A-Za-z_][\w]*)$'), 'rect', False),
)
_EDGE_PATTERNS = (
    (re.compile(r'-\.->\|([^|]*)\|'), 'dashed'),
    (re.compile(r'-->\|([^|]*)\|'), 'arrow'),
    (re.compile(r'---\|([^|]*)\|'), 'line'),
    (re.compile(r'-\.\s+(.+?)\s+\.->'), 'dashed'),
    (re.compile(r'--\s+(.+?)\s+-->'), 'arrow'),
    (re.compile(r'--\s+(.+?)\s+---'), 'line'),
    (re.compile(r'-\.->'), 'dashed'),
    (re.compile(r'-->'), 'arrow'),
    (re.compile(r'---'), 'line'),
)
_MESSAGE = re.compile(
    r'^([A-Za-z_][\w]*)\s*(-->>|->>|-->|->)\s*([A-Za-z_][\w]*)\s*:\s*(.*)$')
_PARTICIPANT = re.compile(
    r'^(?:participant|actor)\s+([A-Za-z_][\w]*)(?:\s+as\s+(.+))?$', re.I)
_STATE_EDGE = re.compile(r'^(.+?)\s+-->\s+(.+?)(?:\s*:\s*(.*))?$')
_STATE_LABEL = re.compile(r'^([A-Za-z_][\w]*)\s*:\s*(.+)$')
_STATE_ALIAS = re.compile(r'^state\s+"([^"]+)"\s+as\s+([A-Za-z_][\w]*)\s*$', re.I)
_CLASS_OPEN = re.compile(r'^class\s+([A-Za-z_][\w]*)\s*(\{)?\s*(.*)$', re.I)
_RELATION_OPS = ('<|--', '--|>', '<|..', '..|>', '*--', '--*', 'o--', '--o',
                 '-->', '<--', '<..', '..>', '--', '..')


@dataclass
class DiagramImage:
    png: bytes
    kind: str


@dataclass
class GraphNode:
    label: str
    shape: str


@dataclass
class GraphEdge:
    src: str
    dst: str
    style: str
    label: str


@dataclass
class GraphDiagram:
    kind: str
    direction: str = 'TD'
    nodes: dict = field(default_factory=dict)
    order: list = field(default_factory=list)
    edges: list = field(default_factory=list)


@dataclass
class SequenceDiagram:
    kind: str = 'sequence'
    participants: list = field(default_factory=list)
    messages: list = field(default_factory=list)


@dataclass
class ClassDiagram:
    kind: str = 'class'
    order: list = field(default_factory=list)
    members: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)


def render_mermaid(source, colors):
    """Return a theme-colored diagram, or a source card when Mermaid is unrecognized."""
    text = source if isinstance(source, str) else str(source)
    diagram = None
    if len(text) <= MAX_SOURCE and text.count('\n') <= MAX_LINES:
        diagram = parse_mermaid(text)
    if diagram is None:
        return DiagramImage(_png(paint_source(text, colors)), 'source')
    return DiagramImage(_png(paint_diagram(diagram, colors)), diagram.kind)


def parse_mermaid(source):
    """Parse a supported diagram. Return None when the source should stay a card."""
    lines = [_clean_line(line) for line in source.replace('\r\n', '\n').split('\n')]
    index = 0
    while index < len(lines) and not lines[index]:
        index += 1
    if index >= len(lines):
        return None
    header = lines[index]
    body = lines[index + 1:]
    matched = _FLOW_HEADER.match(header)
    if matched:
        direction = (matched.group(1) or 'TD').upper()
        return _parse_flow(body, 'TD' if direction == 'TB' else direction)
    if _SEQUENCE_HEADER.match(header):
        return _parse_sequence(body)
    if _STATE_HEADER.match(header):
        return _parse_state(body)
    if _CLASS_HEADER.match(header):
        return _parse_class(body)
    return None


def _clean_line(line):
    text = line.strip()
    if text.endswith(';') and not text.startswith('%%'):
        text = text[:-1].strip()
    depth = 0
    for index, character in enumerate(text):
        if character in '[({':
            depth += 1
        elif character in '])}':
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith('%%', index):
            return text[:index].strip()
    return text


def _parse_flow(lines, direction):
    diagram = GraphDiagram('flowchart', direction)
    for line in lines:
        if not line or line.startswith('%%'):
            continue
        lowered = line.lower()
        if lowered.startswith(('style ', 'classdef ', 'linkstyle ', 'click ', 'class ')):
            continue
        matched = _DIRECTION.match(line)
        if matched:
            value = matched.group(1).upper()
            diagram.direction = 'TD' if value == 'TB' else value
            continue
        if lowered.startswith('subgraph') or lowered == 'end':
            return None
        if not _add_flow_line(diagram, line):
            return None
        if len(diagram.order) > MAX_NODES:
            return None
    return diagram


def _add_flow_line(diagram, line):
    parts = _tokenize_flow(line)
    if not parts:
        return False
    previous = _add_flow_node(diagram, parts[0][1])
    index = 1
    while index < len(parts):
        if (index + 1 >= len(parts) or parts[index][0] != 'edge'
                or parts[index + 1][0] != 'node'):
            return False
        target = _add_flow_node(diagram, parts[index + 1][1])
        if previous is None or target is None:
            return False
        _style, label = parts[index][1], parts[index][2]
        diagram.edges.append(GraphEdge(previous, target, _style, label))
        previous = target
        index += 2
    return previous is not None


def _tokenize_flow(line):
    parts = []
    index = 0
    length = len(line)
    while index < length:
        while index < length and line[index].isspace():
            index += 1
        if index >= length:
            break
        if parts and parts[-1][0] == 'node':
            edge = _match_edge(line, index)
            if edge is not None:
                style, label, size = edge
                parts.append(('edge', style, label))
                index += size
                continue
        start = index
        depth = 0
        while index < length:
            if depth == 0 and index > start and _match_edge(line, index):
                break
            character = line[index]
            if character in '[({':
                depth += 1
            elif character in '])}':
                depth = max(0, depth - 1)
            index += 1
        text = line[start:index].strip()
        if not text:
            return None
        parts.append(('node', text))
    if not parts or parts[0][0] != 'node' or parts[-1][0] != 'node':
        return None
    return parts


def _match_edge(line, index):
    rest = line[index:]
    for pattern, style in _EDGE_PATTERNS:
        found = pattern.match(rest)
        if found:
            label = found.group(1).strip() if found.lastindex else ''
            return style, label, found.end()
    return None


def _add_flow_node(diagram, text):
    parsed = _parse_node(text)
    if parsed is None:
        return None
    node_id, label, shape, explicit = parsed
    current = diagram.nodes.get(node_id)
    if current is None:
        diagram.nodes[node_id] = GraphNode(label or node_id, shape)
        diagram.order.append(node_id)
    elif explicit:
        diagram.nodes[node_id] = GraphNode(label or current.label, shape)
    return node_id


def _parse_node(text):
    for pattern, shape, explicit in _NODE_SHAPES:
        found = pattern.match(text.strip())
        if not found:
            continue
        label = found.group(2).strip() if explicit else found.group(1)
        if len(label) >= 2 and label[0] == label[-1] == '"':
            label = label[1:-1]
        return found.group(1), label.strip() or found.group(1), shape, explicit
    return None


def _parse_sequence(lines):
    diagram = SequenceDiagram()
    for line in lines:
        if not line or line.startswith('%%'):
            continue
        participant = _PARTICIPANT.match(line)
        if participant:
            _upsert(diagram.participants, participant.group(1),
                    (participant.group(2) or participant.group(1)).strip())
            continue
        message = _MESSAGE.match(line)
        if not message:
            return None
        source, operator, target, text = message.groups()
        _upsert(diagram.participants, source, source)
        _upsert(diagram.participants, target, target)
        diagram.messages.append((source, target, operator, text.strip()))
        if len(diagram.participants) > MAX_NODES:
            return None
    return diagram


def _upsert(participants, name, label):
    for index, (identifier, current) in enumerate(participants):
        if identifier == name:
            if label and label != name and current == name:
                participants[index] = (identifier, label)
            return
    participants.append((name, label or name))


def _parse_state(lines):
    diagram = GraphDiagram('state', 'TD')
    for line in lines:
        if not line or line.startswith('%%'):
            continue
        alias = _STATE_ALIAS.match(line)
        if alias:
            _ensure_state(diagram, alias.group(2), alias.group(1), 'round')
            continue
        if '-->' in line:
            matched = _STATE_EDGE.match(line)
            if not matched:
                return None
            source = _state_ref(diagram, matched.group(1).strip(), 'src')
            target = _state_ref(diagram, matched.group(2).strip(), 'dst')
            if source is None or target is None:
                return None
            diagram.edges.append(GraphEdge(source, target, 'arrow', (matched.group(3) or '').strip()))
            continue
        label = _STATE_LABEL.match(line)
        if label:
            _ensure_state(diagram, label.group(1), label.group(2).strip(), 'round')
            continue
        if line.lower().startswith('note ') or line.lower().startswith('state '):
            return None
        return None
    return diagram


def _state_ref(diagram, token, role):
    if token == '[*]':
        key = '[*]start' if role == 'src' else '[*]end'
        _ensure_state(diagram, key, '', 'circle')
        return key
    if re.fullmatch(r'[A-Za-z_][\w]*', token):
        _ensure_state(diagram, token, token, 'round')
        return token
    return None


def _ensure_state(diagram, node_id, label, shape):
    current = diagram.nodes.get(node_id)
    if current is None:
        diagram.nodes[node_id] = GraphNode(label or node_id, shape)
        diagram.order.append(node_id)
        return
    if label and (current.label == node_id or node_id.startswith('[*]')):
        diagram.nodes[node_id] = GraphNode(label, current.shape)


def _parse_class(lines):
    diagram = ClassDiagram()
    current = None
    for line in lines:
        if current is not None:
            if line.startswith('}'):
                current = None
                continue
            if not line:
                continue
            diagram.members[current].append(line)
            continue
        if not line or line.startswith('%%'):
            continue
        opened = _CLASS_OPEN.match(line)
        if opened and not any(line.find(op) >= 0 for op in _RELATION_OPS):
            name = opened.group(1)
            _ensure_class(diagram, name)
            if opened.group(2):
                rest = opened.group(3).strip()
                if rest.endswith('}'):
                    inner = rest[:-1].strip()
                    if inner:
                        diagram.members[name].append(inner)
                else:
                    if rest:
                        diagram.members[name].append(rest)
                    current = name
            continue
        relation = _parse_relation(line)
        if relation is None:
            return None
        source, target, operator, label = relation
        _ensure_class(diagram, source)
        _ensure_class(diagram, target)
        diagram.edges.append((source, target, operator, label))
        if len(diagram.order) > MAX_NODES:
            return None
    if current is not None:
        return None
    return diagram


def _ensure_class(diagram, name):
    if name not in diagram.members:
        diagram.members[name] = []
        diagram.order.append(name)


def _parse_relation(line):
    found = re.match(r'^([A-Za-z_][\w]*)\s*', line)
    if not found:
        return None
    source = found.group(1)
    rest = line[found.end():]
    operator = next((op for op in _RELATION_OPS if rest.startswith(op)), None)
    if operator is None:
        return None
    rest = rest[len(operator):].strip()
    tail = re.match(r'^([A-Za-z_][\w]*)\s*(?::\s*(.*))?$', rest)
    if not tail:
        return None
    return source, tail.group(1), operator, (tail.group(2) or '').strip()


def paint_diagram(diagram, colors):
    if isinstance(diagram, SequenceDiagram):
        return paint_sequence(diagram, colors)
    if isinstance(diagram, ClassDiagram):
        return paint_class(diagram, colors)
    return paint_graph(diagram, colors)


def paint_graph(diagram, colors):
    ink = _rgb(colors.get('ink', '#d5d5d5'))
    bright = _rgb(colors.get('bright', '#eeeeee'))
    accent = _rgb(colors.get('accent', '#cccccc'))
    fill = _rgb(colors.get('hover', '#181818'))
    stroke = _rgb(colors.get('border', '#333333'))
    card = _rgb(colors.get('code_bg', '#1b1b1b'))
    edge_color = _rgb(colors.get('soft', '#aaaaaa'))
    sizes = {node_id: _node_size(node.label if node.shape != 'circle' or node.label else '',
                                 node.shape)
             for node_id, node in diagram.nodes.items()}
    links = [(edge.src, edge.dst) for edge in diagram.edges if edge.src != edge.dst]
    positions, width, height = _place(diagram.order, links, sizes, diagram.direction)
    surface = _canvas(width, height, card, stroke)
    context = cairo.Context(surface)
    placed = {
        node_id: _placed(node_id, diagram.nodes[node_id], *positions[node_id])
        for node_id in diagram.order if node_id in positions
    }
    for edge in diagram.edges:
        if edge.src not in placed or edge.dst not in placed:
            continue
        _draw_edge(context, placed[edge.src], placed[edge.dst], edge, edge_color, bright, card)
    for node_id, node in placed.items():
        _draw_node(context, node, fill, accent if node.shape != 'circle' else bright, ink, stroke)
    if not placed:
        _draw_empty(context, diagram.kind, ink, colors)
    return _limit(surface)


def paint_sequence(diagram, colors):
    ink = _rgb(colors.get('ink', '#d5d5d5'))
    muted = _rgb(colors.get('muted', '#999999'))
    bright = _rgb(colors.get('bright', '#eeeeee'))
    fill = _rgb(colors.get('hover', '#181818'))
    stroke = _rgb(colors.get('border', '#333333'))
    card = _rgb(colors.get('code_bg', '#1b1b1b'))
    if not diagram.participants:
        surface = _canvas(280, 96, card, stroke)
        _draw_empty(cairo.Context(surface), 'sequence', ink, colors)
        return surface
    widths = []
    for _, label in diagram.participants:
        text_w, _ = _measure(label, 'Sans 13', 180)
        widths.append(max(120, text_w + 28))
    gap = 56
    margin = 24
    header_h = 40
    xs = []
    cursor = margin
    for width in widths:
        xs.append(cursor)
        cursor += width + gap
    total_w = cursor - gap + margin
    row_h = 42
    total_h = margin + header_h + 30 + max(1, len(diagram.messages)) * row_h + margin
    surface = _canvas(total_w, total_h, card, stroke)
    context = cairo.Context(surface)
    centers = []
    bottom = total_h - margin
    for (identifier, label), x, width in zip(diagram.participants, xs, widths):
        _rounded(context, x, margin, width, header_h, 8)
        context.set_source_rgb(*fill)
        context.fill_preserve()
        context.set_source_rgb(*stroke)
        context.set_line_width(1.4)
        context.stroke()
        _paint_text(context, label, 'Sans 13', ink, x + 8, margin + 10, width - 16)
        center = x + width / 2
        centers.append(center)
        context.set_source_rgb(*muted)
        context.set_dash([3, 4], 0)
        context.move_to(center, margin + header_h)
        context.line_to(center, bottom)
        context.stroke()
        context.set_dash([], 0)
    lookup = {identifier: center for (identifier, _), center in zip(diagram.participants, centers)}
    y = margin + header_h + 30
    for source, target, operator, text in diagram.messages:
        _draw_message(context, lookup[source], lookup[target], y, operator, text,
                      bright, ink, card)
        y += row_h
    return _limit(surface)


def paint_class(diagram, colors):
    ink = _rgb(colors.get('ink', '#d5d5d5'))
    bright = _rgb(colors.get('bright', '#eeeeee'))
    fill = _rgb(colors.get('hover', '#181818'))
    stroke = _rgb(colors.get('border', '#333333'))
    card = _rgb(colors.get('code_bg', '#1b1b1b'))
    muted = _rgb(colors.get('muted', '#999999'))
    sizes = {name: _class_size(name, diagram.members[name]) for name in diagram.order}
    links = [(source, target) for source, target, _, _ in diagram.edges if source != target]
    positions, width, height = _place(diagram.order, links, sizes, 'TD')
    surface = _canvas(width, height, card, stroke)
    context = cairo.Context(surface)
    placed = {}
    for name in diagram.order:
        if name not in positions:
            continue
        x, y, w, h = positions[name]
        placed[name] = (x, y, w, h)
        _rounded(context, x, y, w, h, 4)
        context.set_source_rgb(*fill)
        context.fill_preserve()
        context.set_source_rgb(*stroke)
        context.set_line_width(1.4)
        context.stroke()
        title_h = _measure(name, 'Sans Bold 14', w - 16)[1]
        _paint_text(context, name, 'Sans Bold 14', bright, x + 10, y + 8, w - 20)
        rule = y + title_h + 14
        context.set_source_rgb(*stroke)
        context.move_to(x, rule)
        context.line_to(x + w, rule)
        context.stroke()
        member_y = rule + 6
        for member in diagram.members[name]:
            _paint_text(context, member, 'Monospace 12', ink, x + 10, member_y, w - 20)
            member_y += _measure(member, 'Monospace 12', w - 20)[1] + 4
    for source, target, operator, label in diagram.edges:
        if source not in placed or target not in placed:
            continue
        _draw_relation(context, placed[source], placed[target], operator, label, muted, bright, card)
    if not placed:
        _draw_empty(context, 'class', ink, colors)
    return _limit(surface)


def paint_source(source, colors):
    ink = _rgb(colors.get('ink', '#d5d5d5'))
    muted = _rgb(colors.get('muted', '#999999'))
    card = _rgb(colors.get('code_bg', '#1b1b1b'))
    stroke = _rgb(colors.get('border', '#333333'))
    accent = _rgb(colors.get('accent', '#cccccc'))
    body = _card_text(source)
    _, text_h = _measure(body, 'Monospace 12', 600)
    width, height = 660, max(96, 58 + text_h + 20)
    surface = _canvas(width, height, card, stroke)
    context = cairo.Context(surface)
    context.set_source_rgb(*accent)
    context.rectangle(0, 0, 5, height)
    context.fill()
    _paint_text(context, 'Mermaid', 'Sans Bold 14', ink, 22, 14, 600)
    _paint_text(context, 'Unrecognized diagram', 'Sans 11', muted, 22, 34, 600)
    _paint_text(context, body, 'Monospace 12', ink, 22, 58, 610)
    return surface


def _card_text(source):
    lines = source.strip().splitlines() or ['(empty)']
    clipped = False
    if len(lines) > 32:
        lines = lines[:32]
        clipped = True
    text = '\n'.join(lines)
    if len(text) > 3500:
        text = text[:3500].rstrip()
        clipped = True
    if clipped:
        text += '\n…'
    return text


def _draw_empty(context, kind, ink, colors):
    _paint_text(context, kind, 'Sans Bold 14', ink, 24, 28, 400)
    muted = _rgb(colors.get('muted', '#999999'))
    _paint_text(context, 'Nothing to draw', 'Sans 12', muted, 24, 52, 400)


def _canvas(width, height, fill, stroke):
    width = max(80, int(math.ceil(width)))
    height = max(64, int(math.ceil(height)))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    _rounded(context, 1, 1, width - 2, height - 2, 10)
    context.set_source_rgb(*fill)
    context.fill_preserve()
    context.set_source_rgb(*stroke)
    context.set_line_width(1.5)
    context.stroke()
    return surface


def _placed(node_id, node, x, y, w, h):
    return type('Placed', (), {
        'id': node_id, 'label': node.label, 'shape': node.shape,
        'x': x, 'y': y, 'w': w, 'h': h,
    })()


def _node_size(label, shape):
    text_w, text_h = _measure(label or ' ', 'Sans 13', 220)
    if shape == 'circle':
        side = max(26, text_w + 18, text_h + 18)
        return side, side
    if shape == 'diamond':
        return max(96, text_w + 52), max(64, text_h + 36)
    return max(92, text_w + 32), max(42, text_h + 20)


def _class_size(name, members):
    title_w, title_h = _measure(name, 'Sans Bold 14', 240)
    member_sizes = [_measure(member, 'Monospace 12', 240) for member in members]
    width = max([title_w, *(item[0] for item in member_sizes), 120]) + 28
    height = title_h + 28 + sum(item[1] + 4 for item in member_sizes) + (8 if members else 4)
    return width, max(64, height)


def _place(order, edges, sizes, direction):
    if not order:
        return {}, 220, 96
    outgoing = {node: [] for node in order}
    indegree = {node: 0 for node in order}
    for source, target in edges:
        if source not in outgoing or target not in indegree or source == target:
            continue
        outgoing[source].append(target)
        indegree[target] += 1
    layer = {node: 0 for node in order}
    queue = [node for node in order if indegree[node] == 0]
    remaining = dict(indegree)
    seen = set()
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        seen.add(node)
        for target in outgoing[node]:
            layer[target] = max(layer[target], layer[node] + 1)
            remaining[target] -= 1
            if remaining[target] == 0:
                queue.append(target)
    if len(seen) != len(order):
        extra = max(layer.values(), default=0) + 1
        for node in order:
            if node not in seen:
                layer[node] = extra
                extra += 1
    if direction in ('BT', 'RL'):
        last = max(layer.values(), default=0)
        layer = {node: last - value for node, value in layer.items()}
    grouped = {}
    for node in order:
        grouped.setdefault(layer[node], []).append(node)
    horizontal = direction in ('LR', 'RL')
    gap_main, gap_cross, margin = 72, 28, 24
    keys = sorted(grouped)
    cross_totals = []
    main_sizes = []
    for key in keys:
        members = grouped[key]
        cross = sum(sizes[node][1] if horizontal else sizes[node][0] for node in members)
        cross += gap_cross * (len(members) - 1)
        cross_totals.append(cross)
        main_sizes.append(max(sizes[node][0] if horizontal else sizes[node][1] for node in members))
    max_cross = max(cross_totals, default=0)
    positions = {}
    cursor = margin
    for index, key in enumerate(keys):
        members = grouped[key]
        cross_pos = margin + max(0, (max_cross - cross_totals[index]) / 2)
        for node in members:
            width, height = sizes[node]
            if horizontal:
                positions[node] = (cursor, cross_pos, width, height)
                cross_pos += height + gap_cross
            else:
                positions[node] = (cross_pos, cursor, width, height)
                cross_pos += width + gap_cross
        cursor += main_sizes[index] + gap_main
    total_main = max(margin * 2, cursor - gap_main + margin)
    total_cross = max_cross + margin * 2
    if horizontal:
        return positions, total_main, total_cross
    return positions, total_cross, total_main


def _draw_node(context, node, fill, accent, ink, stroke):
    context.new_path()
    if node.shape == 'diamond':
        context.move_to(node.x + node.w / 2, node.y)
        context.line_to(node.x + node.w, node.y + node.h / 2)
        context.line_to(node.x + node.w / 2, node.y + node.h)
        context.line_to(node.x, node.y + node.h / 2)
        context.close_path()
    elif node.shape == 'circle':
        context.arc(node.x + node.w / 2, node.y + node.h / 2, min(node.w, node.h) / 2 - 1, 0, math.tau)
    else:
        radius = min(16, node.h / 2) if node.shape == 'round' else 3
        _rounded(context, node.x, node.y, node.w, node.h, radius)
    context.set_source_rgb(*fill)
    context.fill_preserve()
    context.set_source_rgb(*accent if node.shape == 'circle' else stroke)
    context.set_line_width(1.8 if node.shape == 'circle' else 1.4)
    context.stroke()
    if node.shape == 'circle' and not node.label:
        return
    _paint_text(context, node.label, 'Sans 13', ink, node.x + 8, node.y + (node.h - _measure(node.label, 'Sans 13', max(20, node.w - 16))[1]) / 2,
                max(20, node.w - 16))


def _draw_edge(context, source, target, edge, color, bright, card):
    if source.id == target.id:
        _self_loop(context, source, edge, color, bright, card)
        return
    x1, y1 = _boundary(source, target.x + target.w / 2, target.y + target.h / 2)
    x2, y2 = _boundary(target, source.x + source.w / 2, source.y + source.h / 2)
    context.set_source_rgb(*color)
    context.set_line_width(1.6)
    if edge.style == 'dashed':
        context.set_dash([7, 4], 0)
    context.move_to(x1, y1)
    context.line_to(x2, y2)
    context.stroke()
    context.set_dash([], 0)
    if edge.style != 'line':
        _arrow_head(context, x1, y1, x2, y2, bright)
    if edge.label:
        _edge_label(context, edge.label, (x1 + x2) / 2, (y1 + y2) / 2, bright, card)


def _self_loop(context, node, edge, color, bright, card):
    x = node.x + node.w
    y = node.y + node.h * 0.35
    context.set_source_rgb(*color)
    context.set_line_width(1.6)
    if edge.style == 'dashed':
        context.set_dash([7, 4], 0)
    context.move_to(x, y)
    context.curve_to(x + 42, y - 24, x + 42, y + 32, x, y + 16)
    context.stroke()
    context.set_dash([], 0)
    if edge.style != 'line':
        _arrow_head(context, x + 20, y + 20, x, y + 16, bright)
    if edge.label:
        _edge_label(context, edge.label, x + 46, y - 8, bright, card)


def _boundary(node, toward_x, toward_y):
    cx, cy = node.x + node.w / 2, node.y + node.h / 2
    dx, dy = toward_x - cx, toward_y - cy
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return cx, cy
    half_w, half_h = max(node.w / 2, 1), max(node.h / 2, 1)
    if node.shape == 'diamond':
        scale = 1 / (abs(dx) / half_w + abs(dy) / half_h)
    elif node.shape == 'circle':
        scale = min(half_w, half_h) / math.hypot(dx, dy)
    else:
        scale = _edge_scale(dx, dy, half_w, half_h)
    return cx + dx * scale, cy + dy * scale


def _edge_scale(dx, dy, half_w, half_h):
    """How far a ray from the center travels before it leaves the box."""
    limits = []
    if abs(dx) > 0.01:
        limits.append(half_w / abs(dx))
    if abs(dy) > 0.01:
        limits.append(half_h / abs(dy))
    return min(limits) if limits else 1


def _arrow_head(context, x1, y1, x2, y2, color):
    angle = math.atan2(y2 - y1, x2 - x1)
    length = 11
    context.set_source_rgb(*color)
    context.new_path()
    context.move_to(x2, y2)
    context.line_to(x2 - length * math.cos(angle - 0.38), y2 - length * math.sin(angle - 0.38))
    context.line_to(x2 - length * math.cos(angle + 0.38), y2 - length * math.sin(angle + 0.38))
    context.close_path()
    context.fill()


def _edge_label(context, label, x, y, ink, card):
    width, height = _measure(label, 'Sans 11', 180)
    left, top = x - width / 2, y - height - 6
    context.set_source_rgb(*card)
    _rounded(context, left - 4, top - 2, width + 8, height + 4, 4)
    context.fill()
    _paint_text(context, label, 'Sans 11', ink, left, top, 180)


def _draw_message(context, x1, x2, y, operator, text, bright, ink, card):
    dashed = '--' in operator
    context.set_source_rgb(*bright)
    context.set_line_width(1.5)
    if dashed:
        context.set_dash([6, 4], 0)
    if abs(x1 - x2) < 1:
        context.move_to(x1, y)
        context.line_to(x1 + 36, y)
        context.line_to(x1 + 36, y + 16)
        context.line_to(x1, y + 16)
        context.stroke()
        context.set_dash([], 0)
        _arrow_head(context, x1 + 16, y + 16, x1, y + 16, bright)
        if text:
            _paint_text(context, text, 'Sans 12', ink, x1 + 42, y - 2, 180)
        return
    context.move_to(x1, y)
    context.line_to(x2, y)
    context.stroke()
    context.set_dash([], 0)
    _arrow_head(context, x1, y, x2, y, bright)
    if text:
        _edge_label(context, text, (x1 + x2) / 2, y - 2, ink, card)


def _draw_relation(context, source, target, operator, label, muted, bright, card):
    sx, sy, sw, sh = source
    tx, ty, tw, th = target
    x1, y1 = _box_boundary(sx, sy, sw, sh, tx + tw / 2, ty + th / 2)
    x2, y2 = _box_boundary(tx, ty, tw, th, sx + sw / 2, sy + sh / 2)
    context.set_source_rgb(*muted)
    context.set_line_width(1.5)
    if '.' in operator:
        context.set_dash([5, 4], 0)
    context.move_to(x1, y1)
    context.line_to(x2, y2)
    context.stroke()
    context.set_dash([], 0)
    parent_at_target = operator.endswith('|>') or operator.endswith('>')
    parent_at_source = operator.startswith('<|') or operator.startswith('<')
    if '|>' in operator or '<|' in operator:
        point = (x2, y2) if parent_at_target else (x1, y1)
        origin = (x1, y1) if parent_at_target else (x2, y2)
        _triangle(context, origin[0], origin[1], point[0], point[1], bright, filled=False)
    elif '>' in operator or '<' in operator:
        if parent_at_target or operator.endswith('>'):
            _arrow_head(context, x1, y1, x2, y2, bright)
        else:
            _arrow_head(context, x2, y2, x1, y1, bright)
    if operator.startswith('*') or operator.endswith('*'):
        point = (x1, y1) if operator.startswith('*') else (x2, y2)
        _diamond_marker(context, point[0], point[1], bright, filled=True)
    elif operator.startswith('o') or operator.endswith('o'):
        point = (x1, y1) if operator.startswith('o') else (x2, y2)
        _diamond_marker(context, point[0], point[1], bright, filled=False)
    if label:
        _edge_label(context, label, (x1 + x2) / 2, (y1 + y2) / 2, bright, card)


def _box_boundary(x, y, w, h, toward_x, toward_y):
    cx, cy = x + w / 2, y + h / 2
    dx, dy = toward_x - cx, toward_y - cy
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return cx, cy
    scale = _edge_scale(dx, dy, max(w / 2, 1), max(h / 2, 1))
    return cx + dx * scale, cy + dy * scale


def _triangle(context, x1, y1, x2, y2, color, filled):
    angle = math.atan2(y2 - y1, x2 - x1)
    length = 12
    context.set_source_rgb(*color)
    context.new_path()
    context.move_to(x2, y2)
    context.line_to(x2 - length * math.cos(angle - 0.4), y2 - length * math.sin(angle - 0.4))
    context.line_to(x2 - length * math.cos(angle + 0.4), y2 - length * math.sin(angle + 0.4))
    context.close_path()
    if filled:
        context.fill()
    else:
        context.set_line_width(1.3)
        context.stroke()


def _diamond_marker(context, x, y, color, filled):
    context.set_source_rgb(*color)
    context.new_path()
    context.move_to(x, y - 6)
    context.line_to(x + 7, y)
    context.line_to(x, y + 6)
    context.line_to(x - 7, y)
    context.close_path()
    if filled:
        context.fill()
    else:
        context.set_line_width(1.3)
        context.stroke()


def _rounded(context, x, y, w, h, radius):
    radius = max(0, min(radius, w / 2, h / 2))
    context.new_sub_path()
    context.arc(x + w - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + w - radius, y + h - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + h - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()


def _measure(text, font, wrap):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
    context = cairo.Context(surface)
    layout = PangoCairo.create_layout(context)
    layout.set_font_description(Pango.FontDescription.from_string(font))
    layout.set_width(int(max(20, wrap) * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_text(text or '', -1)
    return layout.get_pixel_size()


def _paint_text(context, text, font, color, x, y, wrap):
    layout = PangoCairo.create_layout(context)
    layout.set_font_description(Pango.FontDescription.from_string(font))
    layout.set_width(int(max(20, wrap) * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.CENTER)
    layout.set_text(text or '', -1)
    context.set_source_rgb(*color)
    context.move_to(x, y)
    PangoCairo.show_layout(context, layout)


def _rgb(value):
    text = str(value).strip()
    if len(text) == 7 and text.startswith('#'):
        return tuple(int(text[index:index + 2], 16) / 255 for index in (1, 3, 5))
    return (0.8, 0.8, 0.8)


def _limit(surface, max_w=720, max_h=880):
    width, height = surface.get_width(), surface.get_height()
    scale = min(1.0, max_w / max(width, 1), max_h / max(height, 1))
    if scale >= 0.999:
        return surface
    scaled = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(width * scale)), max(1, int(height * scale)))
    context = cairo.Context(scaled)
    context.scale(scale, scale)
    context.set_source_surface(surface, 0, 0)
    context.paint()
    return scaled


def _png(surface):
    output = BytesIO()
    surface.write_to_png(output)
    return output.getvalue()
