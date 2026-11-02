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
import sqlite3
import tempfile
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from .index import NoteIndex, TextCache
from .metadata import matches_query, note_metadata, query_terms, search_result


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


@dataclass(frozen=True, slots=True)
class NoteRecord:
    path: str
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
    def __init__(self, root: str | Path, *, cache_dir=None, text_cache_bytes=4 * 1024 * 1024):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise VaultError('Choose a folder as your vault.')
        self.notes: dict[str, NoteRecord | None] = {}
        try:
            self.index = NoteIndex(self.root, cache_dir)
        except (OSError, sqlite3.Error) as error:
            raise VaultError(f'Could not open the note index: {error}') from error
        self.text_cache = TextCache(text_cache_bytes)
        try:
            self.refresh()
        except Exception:
            self.close()
            raise

    def _visible_path(self, relative: str) -> Path:
        # No symlink traversal. A vault never grants access outside its root.
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or any(p in ('.', '..') or p.startswith('.') for p in path.parts):
            raise VaultError('Use a visible path inside the vault.')
        current = self.root
        if current.is_symlink():
            raise VaultError('The vault folder was replaced by a symbolic link.')
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise VaultError('Symbolic links are excluded from this prototype.')
        return current

    def _path(self, relative: str) -> Path:
        current = self._visible_path(relative)
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
        if self.root.is_symlink():
            self.notes.clear()
            self.folders = []
            self.index.prune({})
            self.index.connection.commit()
            self.text_cache.clear()
            return
        notes = {}
        visible_folders = []
        for directory, folders, files in os.walk(self.root, followlinks=False):
            folders[:] = sorted(f for f in folders if not f.startswith('.') and not (Path(directory) / f).is_symlink())
            visible_folders.extend((Path(directory) / name).relative_to(self.root).as_posix() for name in folders)
            for name in sorted(files):
                if name.startswith('.') or not name.lower().endswith('.md'):
                    continue
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                relative = path.relative_to(self.root).as_posix()
                try:
                    notes[relative] = self._index_note(self.read(relative))
                except (VaultError, OSError):
                    # Unreadable notes are still listed; opening one reports the error.
                    notes[relative] = None
        self.notes = dict(sorted(notes.items()))
        self.folders = sorted(visible_folders)
        self.index.prune({path for path, note in self.notes.items() if note is not None})
        self.index.connection.commit()
        for path in list(self.text_cache.entries):
            record = self.notes.get(path)
            if record is None or record.revision != self.text_cache.get(path).revision:
                self.text_cache.remove(path)

    def _index_note(self, note):
        if self.index.revision(note.path) != note.revision:
            self.index.store(note, note_metadata(note.text), links(note.text))
            self.text_cache.remove(note.path)
        record = NoteRecord(note.path, note.revision, note.bom)
        previous = self.notes.get(note.path)
        return previous if previous == record else record

    def indexed_note(self, relative):
        """Load a saved snapshot on demand; read() always checks the live file."""
        record = self.notes.get(relative)
        if record is None:
            raise VaultError(f'{relative} is not readable in the saved index.')
        cached = self.text_cache.get(relative)
        if cached is not None and cached.revision == record.revision:
            return cached
        snapshot = self.index.snapshot(relative)
        if snapshot is None:
            raise VaultError('The note index is unavailable. Reopen the vault to rebuild it.')
        text, revision, bom = snapshot
        note = Note(relative, text, revision, bool(bom))
        self.text_cache.put(relative, note)
        return note

    def close(self):
        self.text_cache.clear()
        self.index.close()

    def update_paths(self, paths):
        """Reconcile reported files/subtrees against disk without reading other notes.

        Notifications are hints: duplicate events and atomic replacements must
        converge on the current disk contents, not replay event operations.
        The empty path requests an explicit full reconciliation.
        """
        roots = []
        for relative in sorted(set(paths), key=lambda value: (value.count('/'), value)):
            if relative and any(part.startswith('.') for part in PurePosixPath(relative).parts):
                continue
            if not any(not parent or relative == parent or relative.startswith(parent + '/') for parent in roots):
                roots.append(relative)
        for relative in roots:
            if not relative:
                self.refresh()
                return
            old_paths = {name for name in self.notes if name == relative or name.startswith(relative + '/')}
            folders = {name for name in self.folders if name != relative and not name.startswith(relative + '/')}
            found = {}
            try:
                path = self._visible_path(relative)
                if path.is_dir():
                    folders.add(relative)
                    for directory, children, files in os.walk(path, followlinks=False):
                        children[:] = [name for name in children if not name.startswith('.')
                                       and not (Path(directory) / name).is_symlink()]
                        folders.update((Path(directory) / name).relative_to(self.root).as_posix()
                                       for name in children)
                        for name in files:
                            file = Path(directory) / name
                            if not name.startswith('.') and file.suffix.lower() == '.md' and not file.is_symlink():
                                found[file.relative_to(self.root).as_posix()] = None
                elif path.is_file() and path.suffix.lower() == '.md':
                    found[relative] = None
            except (VaultError, OSError):
                pass
            for name in found:
                try:
                    found[name] = self._index_note(self.read(name))
                except (VaultError, OSError):
                    # Keep unreadable Markdown listed, as in a full scan.
                    pass
            for name in old_paths - found.keys():
                self.notes.pop(name, None)
            self.notes.update(found)
            for name in old_paths | found.keys():
                note = self.notes.get(name)
                if note is None:
                    self.index.remove(name)
                    self.text_cache.remove(name)
            self.folders = sorted(folders)
        self.notes = dict(sorted(self.notes.items()))
        self.index.connection.commit()

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
        self.notes[note.path] = self._index_note(saved)
        self.index.connection.commit()
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
        self.notes[relative] = self._index_note(note)
        self.index.connection.commit()
        return note

    def create_folder(self, relative: str) -> str:
        path = self._visible_path(relative.strip())
        if not path.parent.is_dir():
            raise VaultError('Choose an existing parent folder for the new folder.')
        try:
            path.mkdir()
        except FileExistsError as error:
            raise VaultError('A file or folder already exists at that path.') from error
        relative = path.relative_to(self.root).as_posix()
        self.folders = sorted({*self.folders, relative})
        return relative

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
            candidates = sorted(path for path in self.notes if PurePosixPath(path).name == target)
            if candidates:
                return candidates
            alias = target[:-3].casefold()
            return sorted(path for path, note in self.notes.items() if note is not None and
                          any(value.casefold() == alias for value in self.metadata(path).aliases))
        return []

    def backlinks(self, relative: str) -> list[str]:
        return [path for path, note in self.notes.items() if note is not None and
                any(self.resolve(path, link.target) == [relative] for link in self._saved_links(note))]

    def _saved_links(self, note: NoteRecord) -> list[Link]:
        # Resolve against current paths, while syntax lives in the disk index.
        return [Link(**value) for value in self.index.links(note.path)]

    def search(self, query: str) -> list[str]:
        return [result.path for result in self.search_results(query)]

    def search_results(self, query: str):
        terms = query_terms(query)
        if not terms:
            from .metadata import SearchResult
            return [SearchResult(path, '', ()) for path in self.notes]
        candidates = set(self.index.candidates(terms))
        results = []
        for path, record in self.notes.items():
            if record is not None and path not in candidates:
                continue
            # Stream one document at a time; searching never fills the text cache.
            text = self.index.text(path) if record else ''
            if matches_query(path, text, terms, self.metadata(path)):
                results.append(search_result(path, text, terms))
        return results

    def metadata(self, relative: str):
        record = self.notes.get(relative)
        return self.index.metadata(record.path) if record is not None else note_metadata('')

    def move(self, relative: str, destination: str) -> dict[str, str]:
        """Rename or move a note/folder, updating unambiguous Markdown links."""
        from .organization import move
        return move(self, relative, destination)

    def trash(self, relative: str) -> Path:
        """Move an item to this vault's hidden .trash folder without deleting it."""
        from .organization import trash
        return trash(self, relative)
