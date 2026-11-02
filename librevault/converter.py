"""Convert Markdown links, wikilinks, and Roam/Logseq markers in note source."""
import re
from urllib.parse import unquote, urlsplit
from pathlib import PurePosixPath

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from .editing import TextEdit, markdown_link
from .organization import _protected_ranges
from .vault import MARKDOWN, VaultError


OPERATIONS = (
    ('wikilinks', 'Markdown links to wikilinks'),
    ('markdown', 'Wikilinks to Markdown links'),
    ('obsidian', 'Roam/Logseq to Obsidian Markdown'),
)

_WIKILINK = re.compile(r'(?<![\\\[])!?\[\[([^\]\n]+)\]\]')
_TASK_LINE = re.compile(
    r'^(?P<indent>[ \t]*)(?:[-+*][ \t]+)?\{\{\s*\[\[(?P<state>TODO|DONE)\]\]\s*\}\}',
    re.IGNORECASE | re.MULTILINE)
_TASK_INLINE = re.compile(r'\{\{\s*\[\[(TODO|DONE)\]\]\s*\}\}', re.IGNORECASE)
_TAG = re.compile(r'(?<![\w\\])#\[\[([^\]\n]+)\]\]')


def protected_ranges(text):
    # Same code, HTML, and frontmatter ranges used when rewriting links.
    return _protected_ranges(text, MARKDOWN)


def _visible(start, end, protected):
    return not any(start < stop and end > begin for begin, stop in protected)


def _apply(text, spans):
    for start, end, replacement in sorted(spans, key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _escaped(text, index):
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == '\\':
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _unescape(value):
    chars = []
    index = 0
    while index < len(value):
        if value[index] == '\\' and index + 1 < len(value):
            chars.append(value[index + 1])
            index += 2
        else:
            chars.append(value[index])
            index += 1
    return ''.join(chars)


def _destination(text, start):
    length = len(text)
    if start < length and text[start] == '<':
        end = text.find('>', start + 1)
        if end < 0 or '\n' in text[start:end]:
            return None, None
        cursor = end + 1
        while cursor < length and text[cursor] in ' \t':
            cursor += 1
        if cursor >= length or text[cursor] != ')':
            return None, None
        return text[start + 1:end].strip(), cursor + 1
    cursor, depth = start, 0
    while cursor < length:
        char = text[cursor]
        if char == '\\' and cursor + 1 < length:
            cursor += 2
            continue
        if char == '(':
            depth += 1
        elif char == ')':
            if depth == 0:
                return text[start:cursor].strip(), cursor + 1
            depth -= 1
        elif char.isspace():
            break
        cursor += 1
    if cursor >= length or not text[cursor].isspace():
        return None, None
    target = text[start:cursor].strip()
    while cursor < length and text[cursor] in ' \t':
        cursor += 1
    if cursor < length and text[cursor] in '"\'':
        quote_char = text[cursor]
        cursor += 1
        while cursor < length and text[cursor] != '\n':
            if text[cursor] == '\\' and cursor + 1 < length:
                cursor += 2
                continue
            if text[cursor] == quote_char:
                cursor += 1
                break
            cursor += 1
        else:
            return None, None
    while cursor < length and text[cursor] in ' \t':
        cursor += 1
    if cursor >= length or text[cursor] != ')':
        return None, None
    return target, cursor + 1


def iter_markdown_links(text):
    """Yield inline links, not images, reference links, or autolinks."""
    index, length = 0, len(text)
    while index < length:
        if text[index] != '[' or _escaped(text, index):
            index += 1
            continue
        if index > 0 and text[index - 1] == '!' and not _escaped(text, index - 1):
            index += 1
            continue
        label_start = index + 1
        cursor = label_start
        closed = False
        while cursor < length:
            if text[cursor] == '\\' and cursor + 1 < length:
                cursor += 2
                continue
            if text[cursor] in '\n[':
                break
            if text[cursor] == ']':
                closed = True
                break
            cursor += 1
        if not closed:
            index += 1
            continue
        label = _unescape(text[label_start:cursor])
        cursor += 1
        if cursor >= length or text[cursor] != '(':
            index += 1
            continue
        target, close = _destination(text, cursor + 1)
        if target is None:
            index += 1
            continue
        yield index, close, label, target
        index = close


def _encode_wiki(value):
    for char, encoded in (('%', '%25'), ('#', '%23'), ('|', '%7C'), (']', '%5D'), ('[', '%5B')):
        value = value.replace(char, encoded)
    return value


def _note_target(target):
    """Return (path without .md, fragment) for an internal note link."""
    target = target.strip()
    if not target or target.startswith('//'):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or parts.query:
        return None
    path = unquote(parts.path).replace('\\', '/')
    fragment = unquote(parts.fragment)
    if path.startswith('./'):
        path = path[2:]
    if not path and fragment:
        return '', fragment
    if path.lower().endswith('.md'):
        return path[:-3], fragment
    return None


def _to_wikilink(label, path, fragment):
    encoded_path = _encode_wiki(path)
    encoded_fragment = _encode_wiki(fragment) if fragment else ''
    if encoded_path and encoded_fragment:
        target = f'{encoded_path}#{encoded_fragment}'
    elif encoded_fragment:
        target = f'#{encoded_fragment}'
    else:
        target = encoded_path
    if not target:
        return None
    stem = path.rsplit('/', 1)[-1]
    bare = f'{path}#{fragment}' if path and fragment else (f'#{fragment}' if fragment else path)
    equivalents = {bare, path, stem, fragment, path + '.md' if path else '', stem + '.md' if stem else ''}
    equivalents.discard('')
    if label in equivalents:
        return f'[[{target}]]'
    if any(token in label for token in (']]', '|', '\n')):
        return None
    return f'[[{target}|{label}]]'


def markdown_links_to_wikilinks(text):
    protected = protected_ranges(text)
    spans = []
    for start, end, label, target in iter_markdown_links(text):
        if not _visible(start, end, protected):
            continue
        parsed = _note_target(target)
        if parsed is None:
            continue
        replacement = _to_wikilink(label, *parsed)
        if replacement:
            spans.append((start, end, replacement))
    return _apply(text, spans)


def _wikilink_to_markdown(body):
    target, separator, alias = body.partition('|')
    target = target.strip()
    alias = alias.strip() if separator else None
    if not target:
        return None
    path, _, fragment = target.partition('#')
    path = unquote(path).replace('\\', '/')
    fragment = unquote(fragment)
    suffix = PurePosixPath(path).suffix.lower() if path else ''
    if suffix and suffix != '.md':
        return None
    if path.lower().endswith('.md'):
        href_path = path
        stem = PurePosixPath(path).stem
    elif path:
        href_path = path + '.md'
        stem = path.rsplit('/', 1)[-1]
    else:
        href_path = ''
        stem = ''
    if fragment and href_path:
        href = href_path + '#' + fragment
    elif fragment:
        href = '#' + fragment
    else:
        href = href_path
    if not href:
        return None
    if alias:
        label = alias
    elif fragment and not path:
        label = fragment
    else:
        label = stem or fragment
    if not label:
        return None
    return markdown_link(label, href)


def iter_wikilinks(text):
    for match in _WIKILINK.finditer(text):
        if match.group(0).startswith('!'):
            continue
        yield match.start(), match.end(), match.group(1)


def wikilinks_to_markdown_links(text):
    protected = protected_ranges(text)
    spans = []
    for start, end, body in iter_wikilinks(text):
        if not _visible(start, end, protected):
            continue
        replacement = _wikilink_to_markdown(body)
        if replacement:
            spans.append((start, end, replacement))
    return _apply(text, spans)


def _sanitize_tag(value):
    value = re.sub(r'\s+', '-', value.strip().strip('#'))
    value = re.sub(r'[^\w/-]+', '', value, flags=re.UNICODE)
    return re.sub(r'-{2,}', '-', value).strip('-/')


def _replace_visible(text, pattern, replacer):
    protected = protected_ranges(text)

    def replace(match):
        if not _visible(*match.span(), protected):
            return match.group(0)
        return replacer(match)

    return pattern.sub(replace, text)


def _replace_task_line(match):
    mark = 'x' if match.group('state').upper() == 'DONE' else ' '
    return f"{match.group('indent')}- [{mark}]"


def _replace_task_inline(match):
    return '[x]' if match.group(1).upper() == 'DONE' else '[ ]'


def _replace_tag(match):
    tag = _sanitize_tag(match.group(1))
    if not tag:
        return match.group(0)
    return '#' + tag


def roam_to_obsidian(text):
    text = _replace_visible(text, _TASK_LINE, _replace_task_line)
    text = _replace_visible(text, _TASK_INLINE, _replace_task_inline)
    return _replace_visible(text, _TAG, _replace_tag)


def convert_text(text, operation):
    if operation == 'wikilinks':
        return markdown_links_to_wikilinks(text)
    if operation == 'markdown':
        return wikilinks_to_markdown_links(text)
    if operation == 'obsidian':
        return roam_to_obsidian(text)
    raise ValueError(f'Unknown conversion: {operation}')


def conversion_edit(text, operation):
    """One whole-note edit, or None when the text is already in that form."""
    converted = convert_text(text, operation)
    if converted == text:
        return None
    return TextEdit(0, len(text), converted)


def selected_note_paths(owner):
    """The file-tree selection. A folder contributes every note inside it."""
    files = getattr(owner, 'files', None)
    vault = getattr(owner, 'vault', None)
    if files is None or vault is None:
        return []
    row = files.get_selected_row()
    if row is None:
        return []
    path = row.path
    if path.endswith('/'):
        return sorted(note for note in vault.notes if note.startswith(path))
    if path in vault.notes:
        return [path]
    return []


class ConverterDialog(Gtk.Dialog):
    def __init__(self, owner):
        super().__init__(title='Format converter', transient_for=owner, modal=True, use_header_bar=True)
        self.owner = owner
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.add_button('Convert', Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(520, 260)
        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(8)
        intro = Gtk.Label(label='Convert note syntax in place. The active note stays one undo.', xalign=0)
        intro.set_line_wrap(True)
        content.pack_start(intro, False, False, 0)
        self.operation = Gtk.ComboBoxText()
        self.operation.get_accessible().set_name('Conversion')
        for identifier, label in OPERATIONS:
            self.operation.append(identifier, label)
        self.operation.set_active_id('wikilinks')
        content.pack_start(self.operation, False, False, 0)
        self.scope = Gtk.ComboBoxText()
        self.scope.get_accessible().set_name('Notes to convert')
        self.scope.append('active', 'Active note')
        self.scope.append('selected', 'Selected notes')
        self.scope.set_active_id('active')
        content.pack_start(self.scope, False, False, 0)
        hint = Gtk.Label(xalign=0, label='Selected notes follow the file tree. A folder includes the notes inside it.')
        hint.set_line_wrap(True)
        hint.get_style_context().add_class('muted')
        content.pack_start(hint, False, False, 0)
        self.problem = Gtk.Label(xalign=0)
        self.problem.set_line_wrap(True)
        self.problem.set_max_width_chars(48)
        content.pack_start(self.problem, False, False, 0)
        self.connect('response', self.on_response)

    def target_paths(self):
        if self.scope.get_active_id() == 'selected':
            return selected_note_paths(self.owner)
        pane = self.owner.current()
        return [pane.note.path] if pane is not None else []

    def apply(self):
        operation = self.operation.get_active_id()
        paths = self.target_paths()
        if not paths:
            if self.scope.get_active_id() == 'selected':
                raise ValueError('Select a note or folder in the file tree.')
            raise ValueError('Open a note to convert it.')
        changed = 0
        wrote_disk = False
        for path in paths:
            pane = self.owner.panes.get(path)
            if pane is not None:
                edit = conversion_edit(pane.text(), operation)
                if edit is not None:
                    pane.apply_edit(edit)
                    changed += 1
                continue
            record = self.owner.vault.notes.get(path)
            if record is None:
                raise VaultError(f'{path} cannot be read.')
            note = self.owner.vault.read(path)
            edit = conversion_edit(note.text, operation)
            if edit is not None:
                self.owner.vault.save(note, edit.replacement)
                changed += 1
                wrote_disk = True
        if wrote_disk:
            self.owner.populate_files()
            self.owner.update_context()
        noun = 'note' if changed == 1 else 'notes'
        return f'Converted {changed} {noun}'

    def on_response(self, _, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        try:
            summary = self.apply()
        except (ValueError, VaultError, OSError) as error:
            self.problem.set_text(str(error))
            return
        self.owner.status.set_text(summary)
        self.destroy()
