"""Vault storage and link indexing, independent of GTK.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


class VaultError(Exception):
    """A vault operation the UI can report without losing editor contents."""


class Conflict(VaultError):
    pass


@dataclass(frozen=True)
class Note:
    path: str
    text: str
    revision: str
    bom: bool


@dataclass(frozen=True)
class Link:
    target: str
    label: str
    embedded: bool = False


def wiki_rule(state, silent):
    start = state.pos
    embedded = state.src.startswith('![[', start)
    opening = 3 if embedded else 2
    if not embedded and not state.src.startswith('[[', start):
        return False
    end = state.src.find(']]', start + opening)
    if end < 0:
        return False
    body = state.src[start + opening:end]
    if '\n' in body or not body:
        return False
    if not silent:
        target, separator, label = body.partition('|')
        token = state.push('wikilink', '', 0)
        token.content = label if separator else target
        token.meta = {'target': target.strip(), 'embedded': embedded}
    state.pos = end + 2
    return True


MARKDOWN = MarkdownIt('commonmark', {'html': False})
MARKDOWN.inline.ruler.before('link', 'wikilink', wiki_rule)


def parse(text: str):
    # Frontmatter remains verbatim in storage but is not interpreted as content.
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == '---':
        for i in range(1, len(lines)):
            if lines[i].strip() in ('---', '...'):
                text = ''.join(lines[i + 1:])
                break
    return MARKDOWN.parse(text)


def links(text: str) -> list[Link]:
    found = []
    for token in parse(text):
        for child in token.children or []:
            if child.type == 'wikilink':
                found.append(Link(child.meta['target'], child.content, child.meta['embedded']))
            elif child.type in ('link_open', 'image'):
                target = child.attrGet('href' if child.type == 'link_open' else 'src') or ''
                url = urlsplit(target)
                if not url.scheme and not url.netloc:
                    found.append(Link(target, child.content or target, child.type == 'image'))
    return found


def fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Vault:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise VaultError('Choose a folder as your vault.')
        self.notes: dict[str, Note | None] = {}
        self._link_cache: dict[str, tuple[str, list[Link]]] = {}
        self.refresh()

    def _path(self, relative: str) -> Path:
        # No symlink traversal. A vault never grants access outside its root.
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or any(p in ('.', '..') or p.startswith('.') for p in path.parts):
            raise VaultError('Use a visible path inside the vault.')
        current = self.root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise VaultError('Symbolic links are excluded from this prototype.')
        if current.suffix.lower() != '.md':
            raise VaultError('This editor opens Markdown (.md) notes.')
        return current

    def read(self, relative: str) -> Note:
        raw = self._path(relative).read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError as error:
            raise VaultError(f'{relative} is not UTF-8. Its contents were left unchanged.') from error
        return Note(relative, text, fingerprint(raw), raw.startswith(b'\xef\xbb\xbf'))

    def refresh(self):
        notes = {}
        for directory, folders, files in os.walk(self.root, followlinks=False):
            folders[:] = sorted(f for f in folders if not f.startswith('.') and not (Path(directory) / f).is_symlink())
            for name in sorted(files):
                if name.startswith('.') or not name.lower().endswith('.md'):
                    continue
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                relative = path.relative_to(self.root).as_posix()
                try:
                    notes[relative] = self.read(relative)
                except (VaultError, OSError):
                    # Unreadable notes are still listed; opening one reports the error.
                    notes[relative] = None
        self.notes = dict(sorted(notes.items()))
        self._link_cache = {
            path: cached for path, cached in self._link_cache.items()
            if (note := self.notes.get(path)) is not None and note.revision == cached[0]
        }

    def save(self, note: Note, text: str) -> Note:
        path = self._path(note.path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as error:
            raise Conflict('The note was removed outside the app. Your edits are still in the editor.') from error
        if fingerprint(raw) != note.revision:
            raise Conflict('The file changed outside the app. Save was stopped; your edits are still in the editor. Copy them before reloading.')
        # Gtk.TextBuffer uses LF. Preserve the file's existing uniform CRLF convention.
        newline = '\r\n' if '\r\n' in note.text and '\n' not in note.text.replace('\r\n', '') else '\n'
        text = text.replace('\r\n', '\n')
        if newline == '\r\n':
            text = text.replace('\n', '\r\n')
        output = (b'\xef\xbb\xbf' if note.bom else b'') + text.encode('utf-8')
        descriptor, temporary = tempfile.mkstemp(prefix='.librevault-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                os.fchmod(handle.fileno(), stat.S_IMODE(path.stat().st_mode))
                handle.write(output)
                handle.flush()
                os.fsync(handle.fileno())
            # Recheck after writing the temporary file to catch intervening edits.
            if fingerprint(path.read_bytes()) != note.revision:
                raise Conflict('The file changed during save. Your edits remain in the editor.')
            self._path(note.path)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        saved = self.read(note.path)
        self.notes[note.path] = saved
        return saved

    def create(self, relative: str) -> Note:
        relative = relative.strip()
        if not relative.lower().endswith('.md'):
            relative += '.md'
        path = self._path(relative)
        if not path.parent.is_dir():
            raise VaultError('Choose an existing folder for the new note.')
        try:
            with path.open('xb') as handle:
                handle.write(b'')
        except FileExistsError as error:
            raise VaultError('A note already exists at that path.') from error
        note = self.read(relative)
        self.notes[relative] = note
        return note

    def resolve(self, source: str, target: str) -> list[str]:
        """Return candidates. Never silently choose between ambiguous basenames."""
        target = unquote(target.split('|', 1)[0].split('#', 1)[0]).strip()
        if not target:
            return [source] if source in self.notes else []
        target = target.replace('\\', '/')
        if not target.lower().endswith('.md'):
            target += '.md'
        parent = PurePosixPath(source).parent
        # Explicit relative paths are resolved from the current note.
        if target.startswith('./') or target.startswith('../'):
            normalized = os.path.normpath(str(parent / target)).replace(os.sep, '/')
            return [normalized] if normalized in self.notes else []
        if target in self.notes:
            return [target]
        local = str(parent / target)
        if local in self.notes:
            return [local]
        if '/' not in target:
            return sorted(path for path in self.notes if PurePosixPath(path).name == target)
        return []

    def backlinks(self, relative: str) -> list[str]:
        return [path for path, note in self.notes.items() if note is not None and
                any(self.resolve(path, link.target) == [relative] for link in self._saved_links(note))]

    def _saved_links(self, note: Note) -> list[Link]:
        # Cache syntax only: resolution must reflect newly created/deleted notes
        # and basename ambiguity even when the linking note has not changed.
        cached = self._link_cache.get(note.path)
        if cached is None or cached[0] != note.revision:
            cached = (note.revision, links(note.text))
            self._link_cache[note.path] = cached
        return cached[1]

    def search(self, query: str) -> list[str]:
        words = query.casefold().split()
        return [path for path, note in self.notes.items() if all(
            word in (path + '\n' + (note.text if note else '')).casefold() for word in words)]
