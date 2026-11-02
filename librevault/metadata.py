"""YAML note properties and searchable, source-preserving note metadata."""
from dataclasses import dataclass
import re
import shlex

import yaml


@dataclass(frozen=True)
class NoteMetadata:
    properties: dict
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class SearchResult:
    path: str
    snippet: str
    highlights: tuple[tuple[int, int], ...]
    line: int = 1


def frontmatter_range(text):
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return None
    end = len(lines[0])
    for line in lines[1:]:
        end += len(line)
        if line.strip() in ('---', '...'):
            return len(lines[0]), end - len(line), end
    return None


def property_mapping(source):
    try:
        properties = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise ValueError('Properties must contain valid YAML.') from error
    if properties is None:
        return {}
    if not isinstance(properties, dict) or any(not isinstance(key, str) for key in properties):
        raise ValueError('Properties must be a mapping with text keys.')
    return properties


def property_text(text):
    bounds = frontmatter_range(text)
    return text[bounds[0]:bounds[1]] if bounds else ''


def with_properties(text, source):
    """Replace only the frontmatter; the caller applies this as one editor action."""
    property_mapping(source)
    newline = '\r\n' if '\r\n' in text else '\n'
    bounds = frontmatter_range(text)
    body = text[bounds[2]:] if bounds else text
    if not source.strip():
        return body
    source = source.replace('\r\n', '\n').rstrip('\n').replace('\n', newline)
    return '---' + newline + source + newline + '---' + newline + body


def _values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int, float)) and not isinstance(item, bool)]
    return []


def note_metadata(text):
    try:
        properties = property_mapping(property_text(text))
        error = None
    except ValueError as problem:
        properties, error = {}, str(problem)
    tags = []
    for value in _values(properties.get('tags', properties.get('tag', []))):
        tags.extend(tag.lstrip('#') for tag in re.split(r'[\s,]+', value) if tag.lstrip('#'))
    # Inline code and fenced blocks do not contribute tags.
    body = text[frontmatter_range(text)[2]:] if frontmatter_range(text) else text
    fenced = None
    for line in body.splitlines():
        fence = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            marker = fence[1]
            if fenced is None:
                fenced = marker
            elif marker[0] == fenced[0] and len(marker) >= len(fenced):
                fenced = None
            continue
        if fenced is not None:
            continue
        line = re.sub(r'(`+).*?\1', '', line)
        line = re.sub(r'!?\[\[[^\]\n]+\]\]', '', line)
        line = re.sub(r'\]\([^\)\n]+\)', ']', line)
        tags.extend(match[1] for match in re.finditer(r'(?<![\w\\])#([\w/-]+)', line)
                    if not match[1].isdigit())
    aliases = _values(properties.get('aliases', properties.get('alias', [])))
    return NoteMetadata(properties, tuple(dict.fromkeys(tags)), tuple(dict.fromkeys(aliases)), error)


def query_terms(query):
    try:
        words = shlex.split(query)
    except ValueError:
        words = query.split()
    terms = []
    for word in words:
        negative = word.startswith('-') and len(word) > 1
        if negative:
            word = word[1:]
        field, separator, value = word.partition(':')
        if separator and field in ('path', 'file', 'tag', 'alias', 'content', 'property'):
            terms.append((negative, field, value))
        elif word.startswith('[') and word.endswith(']'):
            terms.append((negative, 'property', word[1:-1]))
        else:
            terms.append((negative, '', word))
    return terms


def matches_query(path, text, terms, metadata):
    folded = (path + '\n' + text).casefold()
    for negative, field, value in terms:
        value = value.casefold()
        if field == 'path':
            matched = value in path.casefold()
        elif field == 'file':
            matched = value in path.rsplit('/', 1)[-1].casefold()
        elif field == 'content':
            matched = value in text.casefold()
        elif field == 'tag':
            tag = value.lstrip('#')
            matched = any(item.casefold() == tag or item.casefold().startswith(tag + '/') for item in metadata.tags)
        elif field == 'alias':
            matched = any(value in alias.casefold() for alias in metadata.aliases)
        elif field == 'property':
            key, _, wanted = value.replace('=', ':', 1).partition(':')
            properties = {str(key).casefold(): item for key, item in metadata.properties.items()}
            matched = key in properties and (not wanted or wanted.strip() in str(properties[key]).casefold())
        else:
            matched = value in folded or any(value in alias.casefold() for alias in metadata.aliases)
        if matched == negative:
            return False
    return True


def search_result(path, text, terms):
    needles = [value for negative, field, value in terms if not negative and field in ('', 'content')]
    # re.IGNORECASE keeps offsets in the original Unicode string intact.
    pattern = re.compile('|'.join(re.escape(word) for word in needles if word), re.IGNORECASE) if any(needles) else None
    match = pattern.search(text) if pattern else None
    bounds = frontmatter_range(text)
    content_start = bounds[2] if bounds else 0
    first_content = re.search(r'\S', text[content_start:])
    offset = match.start() if match else content_start + (first_content.start() if first_content else 0)
    line_start = text.rfind('\n', 0, offset) + 1
    line_end = text.find('\n', offset)
    if line_end < 0:
        line_end = len(text)
    start = max(line_start, offset - 48)
    end = min(line_end, max(offset + 112, start + 160))
    snippet = text[start:end].replace('\t', ' ')
    prefix = '…' if start > line_start else ''
    suffix = '…' if end < line_end else ''
    highlights = tuple((m.start() + len(prefix), m.end() + len(prefix)) for m in pattern.finditer(snippet)) if pattern else ()
    return SearchResult(path, prefix + snippet + suffix, highlights, text.count('\n', 0, start) + 1)
