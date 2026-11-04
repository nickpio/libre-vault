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

from .vault_options import VaultPaths
from .index import NoteIndex, TextCache
from .mentions import Mention, find_mentions
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


class Vault(VaultPaths):
    def __init__(self, root: str | Path, *, cache_dir=None, text_cache_bytes=4 * 1024 * 1024, options=None):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise VaultError('Choose a folder as your vault.')
        from .vault_options import VaultOptions
        self.options = options or VaultOptions()
        self.notes: dict[str, NoteRecord | None] = {}
        self.canvases: dict[str, str | None] = {}
        self.version_history = None
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

    def _path(self, relative: str) -> Path:
        current = self._visible_path(relative)
        if current.suffix.lower() != '.md':
            raise VaultError('This editor opens Markdown (.md) notes.')
        return current

    def _canvas_path(self, relative: str) -> Path:
        current = self._visible_path(relative)
        if current.suffix.lower() != '.canvas':
            raise VaultError('Canvas files use the .canvas extension.')
        return current

    def _canvas_fingerprint(self, path: Path):
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            from .canvas import parse_canvas
            parse_canvas(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError, VaultError):
            return None
        return fingerprint(raw)

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
            self.canvases.clear()
            self.folders = []
            self.index.prune({})
            self.index.connection.commit()
            self.text_cache.clear()
            return
        notes = {}
        canvases = {}
        visible_folders = []
        for directory, folders, files in self.walk():
            visible_folders.extend((Path(directory) / name).relative_to(self.root).as_posix() for name in folders)
            for name in sorted(files):
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                lower = name.lower()
                if lower.endswith('.md'):
                    try:
                        notes[relative] = self._index_note(self.read(relative))
                    except (VaultError, OSError):
                        # Unreadable notes are still listed; opening one reports the error.
                        notes[relative] = None
                elif lower.endswith('.canvas'):
                    canvases[relative] = self._canvas_fingerprint(path)
        self.notes = dict(sorted(notes.items()))
        self.canvases = dict(sorted(canvases.items()))
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
            if relative and not self.path_allowed(relative):
                continue
            if not any(not parent or relative == parent or relative.startswith(parent + '/') for parent in roots):
                roots.append(relative)
        for relative in roots:
            if not relative:
                self.refresh()
                return
            old_paths = {name for name in self.notes if name == relative or name.startswith(relative + '/')}
            old_canvases = {name for name in self.canvases if name == relative or name.startswith(relative + '/')}
            folders = {name for name in self.folders if name != relative and not name.startswith(relative + '/')}
            found = {}
            found_canvases = {}
            try:
                path = self._visible_path(relative)
                if path.is_dir():
                    folders.add(relative)
                    for directory, children, files in self.walk(relative):
                        folders.update((Path(directory) / name).relative_to(self.root).as_posix()
                                       for name in children)
                        for name in files:
                            file = Path(directory) / name
                            listed = file.relative_to(self.root).as_posix()
                            if file.suffix.lower() == '.md':
                                found[listed] = None
                            elif file.suffix.lower() == '.canvas':
                                found_canvases[listed] = self._canvas_fingerprint(file)
                elif path.is_file() and path.suffix.lower() == '.md':
                    found[relative] = None
                elif path.is_file() and path.suffix.lower() == '.canvas':
                    found_canvases[relative] = self._canvas_fingerprint(path)
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
            for name in old_canvases - found_canvases.keys():
                self.canvases.pop(name, None)
            self.canvases.update(found_canvases)
        self.notes = dict(sorted(self.notes.items()))
        self.canvases = dict(sorted(self.canvases.items()))
        self.index.connection.commit()

    def save(self, note: Note, text: str) -> Note:
        path = self._path(note.path)
        if path.is_symlink():
            path = path.resolve()
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
            # Record history before replacing the note. A history failure leaves
            # the saved file untouched, so the editor revision stays valid.
            self._remember_version(note, fingerprint(output), text, path.stat().st_mtime)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        saved = self.read(note.path)
        self.notes[note.path] = self._index_note(saved)
        self.index.connection.commit()
        return saved

    def _remember_version(self, note, revision, text, previous_mtime):
        store = self.version_history
        if store is None or text == note.text:
            return
        try:
            store.record(note.path, revision, text, previous=(note.revision, note.text),
                         previous_mtime=previous_mtime)
        except OSError as error:
            raise VaultError(f'Could not record version history: {error}') from error

    def unlinked_mentions(self, relative: str, texts: dict[str, str] | None = None) -> list[Mention]:
        """Plain-text mentions of this note's name or aliases in other notes.

        texts replaces indexed text for those paths, so an open editor is
        searched instead of the last saved copy. Offsets match that text.
        """
        if relative not in self.notes:
            return []
        terms = (PurePosixPath(relative).stem, *self.metadata(relative).aliases)
        overrides = texts or {}
        found = []
        for path, record in self.notes.items():
            if path == relative or record is None:
                continue
            text = overrides.get(path)
            if text is None:
                text = self.index.text(path)
            if text is None:
                continue
            found.extend(find_mentions(path, text, terms))
        found.sort(key=lambda mention: (mention.source_path, mention.match_start))
        return found

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

    def list_canvases(self) -> list[str]:
        return list(self.canvases)

    def read_canvas(self, relative: str):
        from .canvas import read_canvas
        return read_canvas(self, relative)

    def save_canvas(self, canvas, document=None):
        from .canvas import save_canvas
        return save_canvas(self, canvas, document)

    def create_canvas(self, relative: str):
        from .canvas import create_canvas
        return create_canvas(self, relative)

    def resolve(self, source: str, target: str) -> list[str]:
        """Resolve paths and aliases without silently choosing ambiguous names."""
        from .link_resolution import resolve_paths
        return resolve_paths(self.notes, lambda path: self.metadata(path).aliases, source, target)

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
        if terms.error:
            return []
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

    def tag_counts(self):
        import json
        counts = {}
        for (tags,) in self.index.connection.execute('SELECT tags FROM notes'):
            seen = set()
            for tag in json.loads(tags):
                parts = tag.split('/')
                seen.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
            for tag in seen:
                counts[tag] = counts.get(tag, 0) + 1
        return counts

    def rename_tag(self, old, new):
        from .metadata import rename_tag
        from .organization import _discard_backup, _prepare_tag_backup
        self.organization_notice = ''
        rename_tag('', old, new)  # Validate even when the vault is empty.
        # Prepare every edit before writing, so malformed YAML cannot cause a
        # partially applied rename. save() checks disk revisions for each file.
        edits = []
        originals = {}
        for path, record in self.notes.items():
            if record is None:
                continue
            note = self.read(path)
            changed = rename_tag(note.text, old, new)
            if changed != note.text:
                originals[path] = (b'\xef\xbb\xbf' if note.bom else b'') + note.text.encode('utf-8')
                edits.append((note, changed))
        # As with moves, publish the complete manifest first so a crash or a
        # failed rollback always leaves recoverable original bytes behind.
        backup = _prepare_tag_backup(self, old, new, originals)
        written = []
        try:
            for note, text in edits:
                written.append((note, self.save(note, text)))
        except Exception as error:
            rollback_errors = []
            restored = []
            for original, saved in reversed(written):
                # Never overwrite an intervening external edit during rollback.
                try:
                    self.save(saved, original.text)
                    restored.append(original.path)
                except (Conflict, OSError) as rollback_error:
                    rollback_errors.append((original.path, rollback_error))
            self.refresh()
            if rollback_errors:
                still_changed = ', '.join(path for path, _ in rollback_errors)
                restored_text = ', '.join(restored) or 'none'
                raise VaultError(
                    f'Tag rename stopped with partial completion. Restored: {restored_text}. '
                    f'Could not roll back: {still_changed}. Original note bytes and the tag rename '
                    f'manifest are preserved in {backup}. {rollback_errors[0][1]}'
                ) from error
            if backup:
                try:
                    _discard_backup(backup)
                except OSError:
                    raise VaultError(
                        f'Tag rename stopped. Original notes were restored; a recovery copy '
                        f'remains in {backup}. {error}'
                    ) from error
            self.organization_notice = 'All earlier tag changes were rolled back.'
            raise
        if backup:
            try:
                _discard_backup(backup)
            except OSError:
                self.organization_notice = f'Recovery copies retained in {backup}'
        return [note.path for note, _ in edits]

    def move(self, relative: str, destination: str) -> dict[str, str]:
        """Rename or move a note/folder, updating unambiguous Markdown links."""
        from .organization import move
        return move(self, relative, destination)

    def trash(self, relative: str) -> Path:
        """Move an item to this vault's hidden .trash folder without deleting it."""
        from .organization import trash
        return trash(self, relative)


# Heading and block anchors live in a GTK-free module. Re-export them so the
# vault engine remains the public place to resolve a link target.
from .anchors import extract_subpath, find_anchor_offset, list_block_ids, list_headings  # noqa: E402
