"""Query notes and edit one frontmatter value without touching the note body."""
from dataclasses import dataclass
from pathlib import PurePosixPath
import re

import yaml

from .metadata import matches_query, note_metadata, property_mapping, property_text, query_terms, with_properties


@dataclass(frozen=True)
class BaseRow:
    path: str
    name: str
    tags: tuple
    properties: dict


@dataclass(frozen=True)
class BaseTable:
    rows: tuple
    columns: tuple


def note_name(path):
    return PurePosixPath(path).stem


def iter_note_text(vault, buffers=None):
    """Saved note text, with open buffers winning over the index."""
    buffers = buffers or {}
    for path, record in vault.notes.items():
        if path in buffers:
            yield path, buffers[path]
            continue
        if record is None:
            yield path, ''
            continue
        text = vault.index.text(path)
        if text is None:
            text = vault.read(path).text
        yield path, text


def display_value(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, dict)):
        return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, sort_keys=False).strip()
    return str(value)


def _scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def coerce_property_input(raw):
    """Interpret a table cell. Plain words stay text, including YAML 1.1 yes/no."""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    folded = text.casefold()
    if folded == 'true':
        return True
    if folded == 'false':
        return False
    if re.fullmatch(r'[+-]?\d+', text):
        number = int(text)
        canonical = str(number)
        if text == canonical or (text.startswith('+') and canonical == text[1:]):
            return number
    if re.fullmatch(r'[+-]?(?:\d+\.\d+|\d+\.|\.\d+)', text):
        return float(text)
    if text.startswith('[') and text.endswith(']'):
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError:
            return text
        if isinstance(value, list) and all(_scalar(item) for item in value):
            return value
    return text


def set_property(text, key, raw):
    """Replace one mapping entry. Invalid YAML raises and the caller keeps the note."""
    if not isinstance(key, str) or not key.strip() or any(char in key for char in '\n\r\x00'):
        raise ValueError('Property names must be a single line of text.')
    key = key.strip()
    properties = property_mapping(property_text(text))
    if raw is None or (isinstance(raw, str) and raw.strip() == ''):
        properties.pop(key, None)
    else:
        properties[key] = coerce_property_input(raw)
    if not properties:
        return with_properties(text, '')
    dumped = yaml.safe_dump(properties, sort_keys=False, allow_unicode=True).strip()
    if dumped in ('{}', '...'):
        dumped = ''
    return with_properties(text, dumped)


def _property_sort_key(value):
    # Booleans are ints, so they have to be separated before the numeric branch.
    if isinstance(value, bool):
        return (0, int(value), '')
    if isinstance(value, (int, float)):
        return (1, float(value), '')
    return (2, 0, display_value(value).casefold())


def sort_rows(rows, sort_by, descending=False):
    """Absent properties stay at the bottom in either direction."""
    def path_key(row):
        return row.path.casefold()

    if sort_by == 'name':
        return sorted(rows, key=lambda row: (row.name.casefold(), path_key(row)), reverse=descending)
    if sort_by == 'path':
        return sorted(rows, key=path_key, reverse=descending)
    if sort_by == 'tags':
        return sorted(rows, key=lambda row: (tuple(tag.casefold() for tag in row.tags), path_key(row)),
                      reverse=descending)
    present, missing = [], []
    for row in rows:
        value = row.properties.get(sort_by)
        if sort_by in row.properties and value is not None and value != '':
            present.append(row)
        else:
            missing.append(row)
    present.sort(key=lambda row: (_property_sort_key(row.properties[sort_by]), path_key(row)), reverse=descending)
    missing.sort(key=path_key)
    return present + missing


def build_table(notes, query='', sort_by='name', descending=False):
    """notes is an iterable of (path, text). Filters use the vault search syntax."""
    terms = query_terms(query)
    rows = []
    keys = set()
    for path, text in notes:
        text = text or ''
        metadata = note_metadata(text)
        if not matches_query(path, text, terms, metadata):
            continue
        properties = {key: value for key, value in metadata.properties.items() if isinstance(key, str)}
        keys.update(properties)
        rows.append(BaseRow(path, note_name(path), metadata.tags, properties))
    if not isinstance(sort_by, str) or not sort_by:
        sort_by = 'name'
    ordered = sort_rows(rows, sort_by, bool(descending))
    columns = tuple(sorted(keys, key=str.casefold))
    return BaseTable(tuple(ordered), columns)
