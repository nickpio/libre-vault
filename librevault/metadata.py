"""YAML note properties and searchable, source-preserving note metadata."""
from dataclasses import dataclass
import re
import json

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


class Literal(str):
    """A quoted phrase, including phrases that happen to resemble regexes."""


def _is_regex(value):
    return not isinstance(value, Literal) and value.startswith('/') and value.endswith('/')


class Query(list):
    """Iterable leaves for snippets, with a boolean expression for matching."""
    def __init__(self, leaves, expression, error=None):
        super().__init__(leaves)
        self.expression, self.error = expression, error
        self.complex = True


def query_terms(query):
    # Keep regex escapes and quoted whitespace intact; punctuation belongs to
    # the grammar only outside quotes and regex literals.
    tokens, token, quote, regex, escaped, bracket = [], '', False, False, False, False
    for char in query:
        if escaped:
            token += char
            escaped = False
            continue
        if char == '\\' and (quote or regex):
            token += char
            escaped = True
            continue
        if char == '"' and not regex:
            quote = not quote
        elif char == '/' and not quote and (regex or not token or token in ('-',) or token.endswith(':')):
            regex = not regex
        elif char == '[' and not quote and not regex and token in ('', '-'):
            bracket = True
        elif char == ']' and bracket:
            bracket = False
        elif not (quote or regex or bracket) and (char.isspace() or char in '()'):
            if token:
                tokens.append(token)
                token = ''
            if char in '()':
                tokens.append(char)
            continue
        token += char
    if token:
        tokens.append(token)
    if quote or regex or bracket:
        return Query([], ('or', []), 'Unclosed quote, regular expression, or property filter.')
    position, leaves = 0, []
    fields = {'path', 'file', 'tag', 'alias', 'content', 'property', 'line',
              'block', 'section', 'task', 'task-todo', 'task-done', 'match-case', 'ignore-case'}

    def atom():
        nonlocal position
        if position >= len(tokens):
            raise ValueError('Expected a search term.')
        token = tokens[position]
        position += 1
        if token in (')', 'OR', 'AND'):
            raise ValueError('Expected a search term.')
        if token == '-':
            return ('not', atom())
        if token == '(':
            value = expression()
            if position >= len(tokens) or tokens[position] != ')':
                raise ValueError('Missing closing parenthesis.')
            position += 1
            return value
        negative = token.startswith('-')
        if negative:
            token = token[1:]
        field, sep, value = token.partition(':')
        if not sep or field not in fields:
            field, value = '', token
            if token.startswith('[') and token.endswith(']'):
                field, value = 'property', token[1:-1]
        if field and not value and position < len(tokens) and tokens[position] == '(':
            node = ('scope', field, atom())
        else:
            if value.startswith('"') and value.endswith('"'):
                value = Literal(value[1:-1].replace('\\"', '"'))
            if _is_regex(value):
                re.compile(value[1:-1])
            leaves.append((negative, field, value))
            node = ('term', field, value)
        return ('not', node) if negative else node

    def conjunction():
        nonlocal position
        nodes = []
        while position < len(tokens) and tokens[position] not in (')', 'OR'):
            if tokens[position] == 'AND':
                position += 1
            nodes.append(atom())
        if not nodes:
            raise ValueError('Expected a search term.')
        return ('and', nodes)

    def expression():
        nonlocal position
        nodes = [conjunction()]
        while position < len(tokens) and tokens[position] == 'OR':
            position += 1
            nodes.append(conjunction())
        return ('or', nodes)

    try:
        tree = expression() if tokens else ('and', [])
        if position != len(tokens):
            raise ValueError('Unexpected closing parenthesis.')
        result = Query(leaves, tree)
        result.complex = any(token in ('OR', '(', ')', '-') for token in tokens) or any(
            _is_regex(value) or field in ('match-case', 'ignore-case') for _, field, value in leaves)
        return result
    except (ValueError, re.error) as error:
        return Query(leaves, ('or', []), str(error))


def _scopes(text, field):
    bounds = frontmatter_range(text)
    body = text[bounds[2]:] if bounds else text
    if field == 'line':
        return body.splitlines()
    if field in ('task', 'task-todo', 'task-done'):
        # A checkbox shown as code is not a Markdown task.
        lines, fence = [], None
        for line in body.splitlines():
            marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
            if marker:
                if fence is None:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                    fence = None
            elif fence is None:
                lines.append(line)
        tasks = re.findall(r'^\s*(?:[-*+]|\d+[.)])\s+\[([ xX])\]\s*(.*)$', '\n'.join(lines), re.M)
        return [line for mark, line in tasks if field == 'task' or
                (mark == ' ') == (field == 'task-todo')]
    if field == 'section':
        return re.split(r'(?m)(?=^#{1,6}\s)', body)
    return re.split(r'\n\s*\n', body)


def matches_query(path, text, terms, metadata):
    def evaluate(node, content=text, case=False, scoped=False):
        kind, *args = node
        if kind == 'and':
            return all(evaluate(child, content, case, scoped) for child in args[0])
        if kind == 'or':
            return any(evaluate(child, content, case, scoped) for child in args[0])
        if kind == 'not':
            return not evaluate(args[0], content, case, scoped)
        field, value = args
        if kind == 'scope' and field not in ('match-case', 'ignore-case', 'line', 'block', 'section', 'task', 'task-todo', 'task-done'):
            return evaluate(_bind_field(value, field), content, case, scoped)
        if field in ('match-case', 'ignore-case'):
            child = value if kind == 'scope' else ('term', '', value)
            return evaluate(child, content, field == 'match-case', scoped)
        if field in ('line', 'block', 'section', 'task', 'task-todo', 'task-done'):
            child = value if kind == 'scope' else ('term', 'content', value)
            return any(evaluate(child, part, case, True) for part in _scopes(content, field))
        def contains(haystack, needle=value):
            if _is_regex(needle):
                return re.search(needle[1:-1], str(haystack), 0 if case else re.I) is not None
            return needle in str(haystack) if case else needle.casefold() in str(haystack).casefold()
        if field == 'path':
            return contains(path)
        if field == 'file':
            return contains(path.rsplit('/', 1)[-1])
        if field == 'content':
            return contains(content)
        if field == 'tag':
            tag = value.lstrip('#')
            return any(contains(item, tag) if tag.startswith('/') else
                       (item if case else item.casefold()) == (tag if case else tag.casefold()) or
                       (item if case else item.casefold()).startswith((tag if case else tag.casefold()) + '/')
                       for item in metadata.tags)
        if field == 'alias':
            return any(contains(alias) for alias in metadata.aliases)
        if field == 'property':
            key, _, wanted = value.replace('=', ':', 1).partition(':')
            return any((name == key if case else name.casefold() == key.casefold()) and
                       (not wanted or contains(item, wanted.strip())) for name, item in metadata.properties.items())
        return contains(content) if scoped else contains(path + '\n' + content) or any(contains(alias) for alias in metadata.aliases)
    if isinstance(terms, Query):
        try:
            return not terms.error and evaluate(terms.expression)
        except re.error:
            return False
    return all(matches_query(path, text, query_terms(('-' if neg else '') +
               (field + ':' if field else '') + '"' + value + '"'), metadata) for neg, field, value in terms)


def _bind_field(node, field):
    kind, *args = node
    if kind in ('and', 'or'):
        return (kind, [_bind_field(child, field) for child in args[0]])
    if kind == 'not':
        return ('not', _bind_field(args[0], field))
    if kind == 'term' and not args[0]:
        return ('term', field, args[1])
    return node


def _highlight_patterns(terms):
    def walk(node, negative=False, case=False, field=''):
        kind, *args = node
        if kind in ('and', 'or'):
            for child in args[0]:
                yield from walk(child, negative, case, field)
        elif kind == 'not':
            yield from walk(args[0], not negative, case, field)
        else:
            name, value = args
            if name in ('match-case', 'ignore-case'):
                yield from walk(value if kind == 'scope' else ('term', '', value),
                                negative, name == 'match-case', field)
            elif kind == 'scope':
                yield from walk(value, negative, case, name)
            elif not negative and (name or field) in ('', 'content', 'line', 'block', 'section', 'task', 'task-todo', 'task-done') and value:
                pattern = value[1:-1] if _is_regex(value) else re.escape(value)
                yield re.compile(pattern, 0 if case else re.I)
    if isinstance(terms, Query):
        return list(walk(terms.expression)) if not terms.error else []
    return [re.compile(re.escape(value), re.I) for negative, field, value in terms
            if not negative and field in ('', 'content') and value]


def search_result(path, text, terms):
    patterns = _highlight_patterns(terms)
    matches = [match for pattern in patterns if (match := pattern.search(text)) is not None]
    match = min(matches, key=lambda item: item.start()) if matches else None
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
    highlights = tuple(sorted(set((m.start() + len(prefix), m.end() + len(prefix))
                                  for pattern in patterns for m in pattern.finditer(snippet))))
    return SearchResult(path, prefix + snippet + suffix, highlights, text.count('\n', 0, start) + 1)


def rename_tag(text, old, new):
    """Rename a tag and descendants, preserving code and unrelated source."""
    old, new = old.lstrip('#'), new.lstrip('#')
    if not all(re.fullmatch(r'[\w-]+(?:/[\w-]+)*', tag) and not tag.isdigit() for tag in (old, new)):
        raise ValueError('Tags must contain letters, numbers, underscores, hyphens, or nested / segments.')
    def renamed(value):
        prefix = '#' if value.startswith('#') else ''
        tag = value.lstrip('#')
        return prefix + new + tag[len(old):] if tag == old or tag.startswith(old + '/') else value
    edits = []
    bounds = frontmatter_range(text)
    if bounds:
        source = property_text(text)
        try:
            root = yaml.compose(source)
        except yaml.YAMLError as error:
            raise ValueError('Fix invalid frontmatter before renaming tags.') from error
        if isinstance(root, yaml.MappingNode):
            for key, node in root.value:
                if key.value not in ('tags', 'tag'):
                    continue
                nodes = node.value if isinstance(node, yaml.SequenceNode) else [node]
                for item in nodes:
                    if not isinstance(item, yaml.ScalarNode):
                        continue
                    changed = re.sub(r'[^\s,]+', lambda m: renamed(m[0]), item.value)
                    if changed != item.value:
                        # Serialize only the changed scalar, preserving all other
                        # frontmatter comments, ordering, and formatting.
                        original = source[item.start_mark.index:item.end_mark.index]
                        replacement = (json.dumps(changed, ensure_ascii=False) if '\n' in changed else
                                       yaml.safe_dump(changed, default_flow_style=True, allow_unicode=True).removesuffix('...\n').rstrip('\n'))
                        if original.endswith('\n'):
                            replacement += '\r\n' if original.endswith('\r\n') else '\n'
                        edits.append((bounds[0] + item.start_mark.index, bounds[0] + item.end_mark.index, replacement))
    offset, fence = bounds[2] if bounds else 0, None
    for line in text[offset:].splitlines(keepends=True):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        elif fence is None:
            masked = re.sub(r'(`+).*?\1|!?\[\[[^\]\n]+\]\]|\]\([^\)\n]+\)',
                            lambda m: ' ' * len(m[0]), line)
            for match in re.finditer(r'(?<![\w\\])#([\w/-]+)', masked):
                changed = renamed(match[0])
                if changed != match[0]:
                    edits.append((offset + match.start(), offset + match.end(), changed))
        offset += len(line)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text
