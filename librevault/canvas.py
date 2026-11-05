"""JSON Canvas 1.0 documents stored as vault ``.canvas`` files.

The model is independent of GTK. Unknown JSON fields are kept so an external
canvas can be saved without dropping data this version does not use.
Specification: https://jsoncanvas.org/spec/1.0/
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import os
from .platform_support import set_file_mode
from pathlib import PurePosixPath
import stat
import tempfile
import uuid

from .vault import Conflict, VaultError, fingerprint


class CanvasError(VaultError):
    """A canvas document the UI can report without discarding open edits."""


NODE_TYPES = ('text', 'file', 'link', 'group')
SIDES = ('top', 'right', 'bottom', 'left')
ENDS = ('none', 'arrow')
BACKGROUND_STYLES = ('cover', 'ratio', 'repeat')
DEFAULT_SIZE = {'text': (260, 140), 'file': (260, 90), 'link': (260, 90), 'group': (340, 220)}
_NODE_GEOMETRY = ('id', 'type', 'x', 'y', 'width', 'height', 'color')
_EDGE_FIELDS = ('id', 'fromNode', 'fromSide', 'fromEnd', 'toNode', 'toSide', 'toEnd', 'color', 'label')


@dataclass
class CanvasNode:
    id: str
    type: str
    x: int
    y: int
    width: int
    height: int
    color: str | None = None
    text: str = ''
    file: str = ''
    subpath: str | None = None
    url: str = ''
    label: str | None = None
    background: str | None = None
    background_style: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class CanvasEdge:
    id: str
    from_node: str
    to_node: str
    from_side: str | None = None
    to_side: str | None = None
    from_end: str | None = None
    to_end: str | None = None
    color: str | None = None
    label: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class CanvasDocument:
    nodes: list[CanvasNode] = field(default_factory=list)
    edges: list[CanvasEdge] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def node(self, node_id: str) -> CanvasNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def edge(self, edge_id: str) -> CanvasEdge | None:
        return next((edge for edge in self.edges if edge.id == edge_id), None)


@dataclass(frozen=True)
class CanvasFile:
    path: str
    document: CanvasDocument
    revision: str
    bom: bool
    newline: str


def parse_canvas(text: str) -> CanvasDocument:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise CanvasError(f'Canvas file is not valid JSON: {error}') from error
    if not isinstance(raw, dict):
        raise CanvasError('Canvas file must contain a JSON object.')
    nodes_raw = raw.get('nodes', [])
    edges_raw = raw.get('edges', [])
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        raise CanvasError('Canvas nodes and edges must be arrays.')
    document = CanvasDocument(
        nodes=[_parse_node(item) for item in nodes_raw],
        edges=[_parse_edge(item) for item in edges_raw],
        extra={key: copy.deepcopy(value) for key, value in raw.items() if key not in ('nodes', 'edges')},
    )
    _require_unique([node.id for node in document.nodes], 'Node')
    _require_unique([edge.id for edge in document.edges], 'Edge')
    return document


def serialize_canvas(document: CanvasDocument) -> str:
    payload = {
        'nodes': [_node_json(node) for node in document.nodes],
        'edges': [_edge_json(edge) for edge in document.edges],
    }
    payload.update(copy.deepcopy(document.extra))
    return json.dumps(payload, ensure_ascii=False, indent=2)


def add_node(document: CanvasDocument, node_type: str, *, x=0, y=0, width=None, height=None,
             behind=False, **fields) -> CanvasNode:
    if node_type not in NODE_TYPES:
        raise CanvasError('Canvas cards must be text, file, link, or group.')
    if node_type == 'file' and not fields.get('file'):
        raise CanvasError('File nodes require file.')
    if node_type == 'link' and 'url' not in fields:
        raise CanvasError('Link nodes require url.')
    default_width, default_height = DEFAULT_SIZE[node_type]
    node = CanvasNode(
        id=_new_identifier(document, fields.pop('id', None)),
        type=node_type,
        x=_coordinate(x, 'Node x'),
        y=_coordinate(y, 'Node y'),
        width=_coordinate(default_width if width is None else width, 'Node width'),
        height=_coordinate(default_height if height is None else height, 'Node height'),
    )
    _apply_node_fields(node, fields, creating=True)
    _validate_node(node)
    if behind:
        document.nodes.insert(0, node)
    else:
        document.nodes.append(node)
    return node


def update_node(document: CanvasDocument, node_id: str, **fields) -> CanvasNode:
    node = _require_node(document, node_id)
    if 'id' in fields or 'type' in fields:
        raise CanvasError('Canvas card ids and types stay fixed. Add a new card instead.')
    if 'x' in fields:
        node.x = _coordinate(fields.pop('x'), 'Node x')
    if 'y' in fields:
        node.y = _coordinate(fields.pop('y'), 'Node y')
    if 'width' in fields:
        node.width = _coordinate(fields.pop('width'), 'Node width')
    if 'height' in fields:
        node.height = _coordinate(fields.pop('height'), 'Node height')
    _apply_node_fields(node, fields, creating=False)
    _validate_node(node)
    return node


def remove_node(document: CanvasDocument, node_id: str) -> None:
    _require_node(document, node_id)
    document.nodes = [node for node in document.nodes if node.id != node_id]
    document.edges = [edge for edge in document.edges if edge.from_node != node_id and edge.to_node != node_id]


def move_node(document: CanvasDocument, node_id: str, x, y) -> CanvasNode:
    return update_node(document, node_id, x=x, y=y)


def resize_node(document: CanvasDocument, node_id: str, width, height) -> CanvasNode:
    width = _coordinate(width, 'Node width')
    height = _coordinate(height, 'Node height')
    if width < 1 or height < 1:
        raise CanvasError('Node size must be positive.')
    return update_node(document, node_id, width=width, height=height)


def add_edge(document: CanvasDocument, from_node: str, to_node: str, *, from_side=None, to_side=None,
             from_end=None, to_end=None, label=None, color=None, edge_id=None, **extra) -> CanvasEdge:
    if document.node(from_node) is None or document.node(to_node) is None:
        raise CanvasError('Connect two cards that are on this canvas.')
    edge = CanvasEdge(
        id=_new_identifier(document, edge_id),
        from_node=from_node,
        to_node=to_node,
        from_side=_optional_choice(from_side, SIDES, 'From side'),
        to_side=_optional_choice(to_side, SIDES, 'To side'),
        from_end=_optional_choice(from_end, ENDS, 'From end'),
        to_end=_optional_choice(to_end, ENDS, 'To end'),
        color=_optional_string(color, 'Edge color'),
        label=_optional_string(label, 'Edge label'),
        extra=_extra_fields(extra),
    )
    document.edges.append(edge)
    return edge


def update_edge(document: CanvasDocument, edge_id: str, **fields) -> CanvasEdge:
    edge = _require_edge(document, edge_id)
    if 'id' in fields:
        raise CanvasError('Canvas connection ids stay fixed.')
    if 'from_node' in fields or 'fromNode' in fields:
        edge.from_node = _required_string(fields.pop('from_node', fields.pop('fromNode', None)), 'From node')
    if 'to_node' in fields or 'toNode' in fields:
        edge.to_node = _required_string(fields.pop('to_node', fields.pop('toNode', None)), 'To node')
    for key, choices, label in (
            ('from_side', SIDES, 'From side'), ('fromSide', SIDES, 'From side'),
            ('to_side', SIDES, 'To side'), ('toSide', SIDES, 'To side'),
            ('from_end', ENDS, 'From end'), ('fromEnd', ENDS, 'From end'),
            ('to_end', ENDS, 'To end'), ('toEnd', ENDS, 'To end')):
        if key in fields:
            value = _optional_choice(fields.pop(key), choices, label)
            setattr(edge, _edge_attr(key), value)
    if 'color' in fields:
        edge.color = _optional_string(fields.pop('color'), 'Edge color')
    if 'label' in fields:
        edge.label = _optional_string(fields.pop('label'), 'Edge label')
    edge.extra.update(_extra_fields(fields))
    return edge


def remove_edge(document: CanvasDocument, edge_id: str) -> None:
    _require_edge(document, edge_id)
    document.edges = [edge for edge in document.edges if edge.id != edge_id]


def canvas_path(relative: str) -> str:
    relative = relative.strip().replace('\\', '/')
    if not relative.lower().endswith('.canvas'):
        relative += '.canvas'
    return relative


def read_canvas(vault, relative: str) -> CanvasFile:
    path = vault._canvas_path(relative)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise VaultError(f'{relative} was removed outside the app.') from error
    except OSError as error:
        raise VaultError(f'Could not read {relative}: {error}') from error
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise VaultError(f'{relative} is not UTF-8. Its contents were left unchanged.') from error
    newline = '\r\n' if '\r\n' in text and '\n' not in text.replace('\r\n', '') else '\n'
    return CanvasFile(relative, parse_canvas(text), fingerprint(raw), raw.startswith(b'\xef\xbb\xbf'), newline)


def save_canvas(vault, canvas: CanvasFile, document: CanvasDocument | None = None) -> CanvasFile:
    """Replace a canvas only when its SHA-256 still matches the opened revision."""
    document = canvas.document if document is None else document
    path = vault._canvas_path(canvas.path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise Conflict('The canvas was removed outside the app. Your edits are still open.') from error
    if fingerprint(raw) != canvas.revision:
        raise Conflict('The file changed outside the app. Save was stopped; your edits are still open. Copy them before reloading.')
    output = _encode(document, bom=canvas.bom, newline=canvas.newline)
    descriptor, temporary = tempfile.mkstemp(prefix='.librevault-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            set_file_mode(handle, stat.S_IMODE(path.stat().st_mode))
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        if fingerprint(path.read_bytes()) != canvas.revision:
            raise Conflict('The file changed during save. Your edits remain open.')
        vault._canvas_path(canvas.path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    saved = read_canvas(vault, canvas.path)
    vault.canvases[saved.path] = saved.revision
    vault.canvases = dict(sorted(vault.canvases.items()))
    return saved


def create_canvas(vault, relative: str) -> CanvasFile:
    relative = canvas_path(relative)
    path = vault._canvas_path(relative)
    if not path.parent.is_dir():
        raise VaultError('Choose an existing folder for the new canvas.')
    output = _encode(CanvasDocument(), bom=False, newline='\n')
    try:
        with path.open('xb') as handle:
            handle.write(output)
    except FileExistsError as error:
        raise VaultError('A canvas already exists at that path.') from error
    except OSError as error:
        raise VaultError(f'Could not create the canvas: {error}') from error
    saved = read_canvas(vault, relative)
    vault.canvases[saved.path] = saved.revision
    vault.canvases = dict(sorted(vault.canvases.items()))
    _remember_parent_folders(vault, saved.path)
    return saved


def _remember_parent_folders(vault, relative: str) -> None:
    parent = PurePosixPath(relative).parent
    added = []
    while str(parent) not in ('.', ''):
        folder = str(parent)
        if folder not in vault.folders:
            added.append(folder)
        parent = parent.parent
    if added:
        vault.folders = sorted({*vault.folders, *added})


def _encode(document: CanvasDocument, *, bom: bool, newline: str) -> bytes:
    text = serialize_canvas(document)
    if not text.endswith('\n'):
        text += '\n'
    if newline == '\r\n':
        text = text.replace('\n', '\r\n')
    output = text.encode('utf-8')
    return b'\xef\xbb\xbf' + output if bom else output


def _parse_node(raw) -> CanvasNode:
    raw = _object(raw, 'Node')
    node_type = _required_string(raw.get('type'), 'Node type')
    node = CanvasNode(
        id=_required_string(raw.get('id'), 'Node id'),
        type=node_type,
        x=_json_int(raw.get('x'), 'Node x'),
        y=_json_int(raw.get('y'), 'Node y'),
        width=_json_int(raw.get('width'), 'Node width'),
        height=_json_int(raw.get('height'), 'Node height'),
        color=_optional_present(raw, 'color', 'Node color'),
    )
    consumed = set(_NODE_GEOMETRY)
    if node_type == 'text':
        if 'text' not in raw or not isinstance(raw.get('text'), str):
            raise CanvasError('Text nodes require text.')
        node.text = raw['text']
        consumed.add('text')
    elif node_type == 'file':
        if 'file' not in raw or not isinstance(raw.get('file'), str) or not raw['file']:
            raise CanvasError('File nodes require file.')
        node.file = raw['file']
        consumed.add('file')
        if 'subpath' in raw and raw['subpath'] is not None:
            node.subpath = _required_string(raw['subpath'], 'Subpath')
            if not node.subpath.startswith('#'):
                raise CanvasError('File subpath must start with #.')
            consumed.add('subpath')
    elif node_type == 'link':
        if 'url' not in raw or not isinstance(raw.get('url'), str):
            raise CanvasError('Link nodes require url.')
        node.url = raw['url']
        consumed.add('url')
    elif node_type == 'group':
        if 'label' in raw and raw['label'] is not None:
            node.label = _optional_string(raw['label'], 'Group label')
            if node.label is None:
                raise CanvasError('Group label must be a string.')
            consumed.add('label')
        if 'background' in raw and raw['background'] is not None:
            node.background = _required_string(raw['background'], 'Group background')
            consumed.add('background')
        if 'backgroundStyle' in raw and raw['backgroundStyle'] is not None:
            style = _required_string(raw['backgroundStyle'], 'Group background style')
            if style in BACKGROUND_STYLES:
                node.background_style = style
            else:
                node.extra['backgroundStyle'] = style
            consumed.add('backgroundStyle')
    node.extra.update({key: copy.deepcopy(value) for key, value in raw.items() if key not in consumed})
    return node


def _parse_edge(raw) -> CanvasEdge:
    raw = _object(raw, 'Edge')
    edge = CanvasEdge(
        id=_required_string(raw.get('id'), 'Edge id'),
        from_node=_required_string(raw.get('fromNode'), 'From node'),
        to_node=_required_string(raw.get('toNode'), 'To node'),
        color=_optional_present(raw, 'color', 'Edge color'),
        label=_optional_present(raw, 'label', 'Edge label'),
    )
    consumed = set(_EDGE_FIELDS)
    for key, choices, label, attr in (
            ('fromSide', SIDES, 'From side', 'from_side'),
            ('toSide', SIDES, 'To side', 'to_side'),
            ('fromEnd', ENDS, 'From end', 'from_end'),
            ('toEnd', ENDS, 'To end', 'to_end')):
        if key not in raw or raw[key] is None:
            continue
        value = _required_string(raw[key], label)
        if value in choices:
            setattr(edge, attr, value)
        else:
            edge.extra[key] = value
    edge.extra.update({key: copy.deepcopy(value) for key, value in raw.items() if key not in consumed})
    return edge


def _node_json(node: CanvasNode) -> dict:
    payload = {'id': node.id, 'type': node.type, 'x': node.x, 'y': node.y, 'width': node.width, 'height': node.height}
    if node.color is not None:
        payload['color'] = node.color
    if node.type == 'text':
        payload['text'] = node.text
    elif node.type == 'file':
        payload['file'] = node.file
        if node.subpath is not None:
            payload['subpath'] = node.subpath
    elif node.type == 'link':
        payload['url'] = node.url
    elif node.type == 'group':
        if node.label is not None:
            payload['label'] = node.label
        if node.background is not None:
            payload['background'] = node.background
        if node.background_style is not None:
            payload['backgroundStyle'] = node.background_style
    payload.update(copy.deepcopy(node.extra))
    return payload


def _edge_json(edge: CanvasEdge) -> dict:
    payload = {'id': edge.id, 'fromNode': edge.from_node, 'toNode': edge.to_node}
    if edge.from_side is not None:
        payload['fromSide'] = edge.from_side
    if edge.from_end is not None:
        payload['fromEnd'] = edge.from_end
    if edge.to_side is not None:
        payload['toSide'] = edge.to_side
    if edge.to_end is not None:
        payload['toEnd'] = edge.to_end
    if edge.color is not None:
        payload['color'] = edge.color
    if edge.label is not None:
        payload['label'] = edge.label
    payload.update(copy.deepcopy(edge.extra))
    return payload


def _apply_node_fields(node: CanvasNode, fields: dict, *, creating: bool) -> None:
    if 'color' in fields:
        node.color = _optional_string(fields.pop('color'), 'Node color')
    if node.type == 'text' and 'text' in fields:
        node.text = _required_string(fields.pop('text'), 'Text')
    elif node.type == 'text' and creating:
        node.text = ''
    if node.type == 'file':
        if 'file' in fields:
            node.file = _required_string(fields.pop('file'), 'File')
        if 'subpath' in fields:
            subpath = fields.pop('subpath')
            if subpath is None or subpath == '':
                node.subpath = None
            else:
                node.subpath = _required_string(subpath, 'Subpath')
                if not node.subpath.startswith('#'):
                    raise CanvasError('File subpath must start with #.')
    if node.type == 'link' and 'url' in fields:
        node.url = _required_string(fields.pop('url'), 'URL')
    if node.type == 'group':
        if 'label' in fields:
            label = fields.pop('label')
            node.label = None if label is None else _required_string(label, 'Group label')
        if 'background' in fields:
            background = fields.pop('background')
            node.background = None if background is None else _required_string(background, 'Group background')
        if 'background_style' in fields or 'backgroundStyle' in fields:
            style = fields.pop('background_style', fields.pop('backgroundStyle', None))
            if style is None:
                node.background_style = None
            else:
                node.background_style = _optional_choice(style, BACKGROUND_STYLES, 'Group background style')
    node.extra.update(_extra_fields(fields))


def _validate_node(node: CanvasNode) -> None:
    if node.type == 'text' and not isinstance(node.text, str):
        raise CanvasError('Text nodes require text.')
    if node.type == 'file' and not node.file:
        raise CanvasError('File nodes require file.')
    if node.type == 'link' and not isinstance(node.url, str):
        raise CanvasError('Link nodes require url.')


def _require_node(document: CanvasDocument, node_id: str) -> CanvasNode:
    node = document.node(node_id)
    if node is None:
        raise CanvasError(f'No canvas card has id {node_id}.')
    return node


def _require_edge(document: CanvasDocument, edge_id: str) -> CanvasEdge:
    edge = document.edge(edge_id)
    if edge is None:
        raise CanvasError(f'No canvas connection has id {edge_id}.')
    return edge


def _used_ids(document: CanvasDocument) -> set[str]:
    return {node.id for node in document.nodes} | {edge.id for edge in document.edges}


def _new_identifier(document: CanvasDocument, requested) -> str:
    if requested is None:
        used = _used_ids(document)
        while True:
            candidate = uuid.uuid4().hex[:16]
            if candidate not in used:
                return candidate
    identifier = _required_string(requested, 'Id')
    if identifier in _used_ids(document):
        raise CanvasError(f'Canvas id {identifier} is already used.')
    return identifier


def _require_unique(values, label) -> None:
    if len(values) != len(set(values)):
        raise CanvasError(f'{label} ids must be unique.')


def _object(raw, label):
    if not isinstance(raw, dict):
        raise CanvasError(f'{label} must be an object.')
    return raw


def _required_string(value, label) -> str:
    if not isinstance(value, str) or not value:
        raise CanvasError(f'{label} must be a non-empty string.')
    return value


def _optional_string(value, label) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CanvasError(f'{label} must be a string.')
    return value


def _optional_present(raw, key, label) -> str | None:
    if key not in raw or raw[key] is None:
        return None
    return _required_string(raw[key], label) if key in ('id',) else _optional_string(raw[key], label)


def _optional_choice(value, choices, label) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        raise CanvasError(f'{label} must be one of: {", ".join(choices)}.')
    return value


def _json_int(value, label) -> int:
    if type(value) is not int:
        raise CanvasError(f'{label} must be an integer.')
    return value


def _coordinate(value, label) -> int:
    if type(value) is not int:
        raise CanvasError(f'{label} must be an integer.')
    return value


def _extra_fields(fields: dict) -> dict:
    extra = {}
    for key, value in fields.items():
        if not isinstance(key, str):
            raise CanvasError('Canvas field names must be strings.')
        try:
            json.dumps(value)
        except TypeError as error:
            raise CanvasError(f'{key} must be JSON data.') from error
        extra[key] = copy.deepcopy(value)
    return extra


def _edge_attr(key: str) -> str:
    return {'from_side': 'from_side', 'fromSide': 'from_side', 'to_side': 'to_side', 'toSide': 'to_side',
            'from_end': 'from_end', 'fromEnd': 'from_end', 'to_end': 'to_end', 'toEnd': 'to_end'}[key]
