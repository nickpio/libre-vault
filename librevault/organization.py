"""Recoverable vault organization and source-preserving link updates."""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
from urllib.parse import quote, unquote, urlsplit

from .metadata import frontmatter_range


def _trash_directory(vault):
    from .vault import VaultError
    directory = vault.root / '.trash'
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise VaultError('The vault trash path must be a real folder.')
    directory.mkdir(exist_ok=True)
    return directory


def _prepare_backup(vault, source, destination, originals, mapping):
    """Publish a complete recovery manifest before changing any note bytes."""
    from .vault import VaultError
    if not originals:
        return None
    parent = _trash_directory(vault) / '.librevault-moves'
    if parent.is_symlink() or parent.exists() and not parent.is_dir():
        raise VaultError('The move recovery path must be a real folder.')
    parent.mkdir(mode=0o700, exist_ok=True)
    backup = parent / uuid.uuid4().hex
    backup.mkdir(mode=0o700)
    notes = []
    try:
        for path, raw in originals.items():
            filename = hashlib.sha256(path.encode()).hexdigest() + '.bin'
            with (backup / filename).open('xb') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            notes.append({'path': path, 'backup': filename, 'revision': hashlib.sha256(raw).hexdigest()})
        manifest = {'source': source, 'destination': destination, 'mapping': mapping, 'notes': notes}
        with (backup / 'manifest.tmp').open('x', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        (backup / 'manifest.tmp').rename(backup / 'manifest.json')
        for directory in (backup, parent, parent.parent):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return backup
    except Exception:
        shutil.rmtree(backup)
        raise


def _protected_ranges(text, parser):
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    bounds = frontmatter_range(text)
    protected = [(0, bounds[2])] if bounds else []
    # Parse the full source after blanking properties, keeping source offsets.
    body = ('\n' * text[:bounds[2]].count('\n') + text[bounds[2]:]) if bounds else text
    for token in parser.parse(body):
        if token.type in ('fence', 'code_block', 'html_block') and token.map:
            protected.append((offsets[token.map[0]], offsets[token.map[1]]))
    for match in re.finditer(r'(?<!\\)(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)', text):
        protected.append(match.span())
    return protected


def _destinations(text, parser):
    protected = _protected_ranges(text, parser)

    def visible(start, end):
        return not any(start < stop and end > begin for begin, stop in protected)

    for match in re.finditer(r'(?<![\\\[])!?\[\[([^\]\n]+)\]\]', text):
        if visible(*match.span()):
            start = match.start(1)
            target = match[1].split('|', 1)[0]
            yield start, start + len(target), target, True
    for match in re.finditer(r'(?<!\\)!?\[(?:\\.|[^\]\n])*\]\(\s*', text):
        start = match.end()
        if start >= len(text) or not visible(match.start(), start):
            continue
        if text[start] == '<':
            start += 1
            end = text.find('>', start)
            if end >= 0 and '\n' not in text[start:end]:
                yield start, end, text[start:end], False
            continue
        end, depth = start, 0
        while end < len(text):
            char = text[end]
            if char == '\\' and end + 1 < len(text):
                end += 2
                continue
            if char == '(':
                depth += 1
            elif char == ')':
                if depth == 0:
                    break
                depth -= 1
            elif char.isspace():
                break
            end += 1
        if end > start:
            yield start, end, text[start:end], False
    for match in re.finditer(r'(?m)^ {0,3}\[(?:\\.|[^\]\n])+\]:[ \t]*(?:<([^>\n]+)>|(\S+))', text):
        if visible(*match.span()):
            group = 1 if match[1] is not None else 2
            yield match.start(group), match.end(group), match[group], False


def rewrite_links(vault, note, mapping, future=None, files=None):
    """Resolve links against the old index, then encode an unambiguous new path."""
    from .vault import MARKDOWN, VaultError
    from .attachments import local_path
    if future is None:
        future = copy.copy(vault)
        future.notes = {mapping.get(path, path): value for path, value in vault.notes.items()}
    new_source = mapping.get(note.path, note.path)
    edits = []
    for start, end, target, wiki in _destinations(note.text, MARKDOWN):
        if urlsplit(target).scheme or target.startswith('//'):
            continue
        candidates = vault.resolve(note.path, target)
        is_note = len(candidates) == 1
        if is_note:
            old_target = candidates[0]
        else:
            try:
                old_target = local_path(vault, note.path, target).relative_to(vault.root).as_posix()
            except (VaultError, OSError):
                continue
        destination = mapping.get(old_target, old_target)
        if is_note:
            if future.resolve(new_source, target) == [destination]:
                continue
        elif old_target not in mapping and note.path == new_source:
            # Detect a moved asset shadowing a previously root-relative one.
            local = os.path.normpath(str(PurePosixPath(new_source).parent / unquote(urlsplit(target).path)))
            if not files or local not in files or local == destination:
                continue
        fragment = '#' + target.partition('#')[2] if '#' in target else ''
        if wiki:
            replacement = destination
            if is_note and not unquote(target.partition('#')[0]).lower().endswith('.md'):
                replacement = destination[:-3]
            for character, encoded in (('%', '%25'), ('#', '%23'), ('|', '%7C'), ('[', '%5B'), (']', '%5D')):
                replacement = replacement.replace(character, encoded)
        else:
            replacement = os.path.relpath(destination, str(PurePosixPath(new_source).parent)).replace(os.sep, '/')
            if not replacement.startswith('../'):
                replacement = './' + replacement
            replacement = quote(replacement, safe='/.-_~')
        if not target.partition('#')[0] and destination == new_source:
            replacement = ''
        replacement += fragment
        if replacement != target:
            edits.append((start, end, replacement))
    text = note.text
    for start, end, replacement in sorted(set(edits), reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def move(vault, relative, destination):
    from .vault import Conflict, VaultError, fingerprint
    source = vault._visible_path(relative.rstrip('/'))
    target = vault._visible_path(destination.rstrip('/').strip())
    old = source.relative_to(vault.root).as_posix()
    new = target.relative_to(vault.root).as_posix()
    if source == target:
        return {}
    vault.organization_notice = ''
    if not source.exists():
        raise VaultError('The selected note or folder no longer exists.')
    if target.exists():
        raise VaultError('A file or folder already exists at that destination.')
    if not target.parent.is_dir():
        raise VaultError('Choose an existing destination folder.')
    if source.is_dir() and source in target.parents:
        raise VaultError('A folder cannot be moved inside itself.')
    if source.is_file() and source.suffix.lower() == '.md' and target.suffix.lower() != '.md':
        raise VaultError('Markdown notes must keep the .md extension.')
    if source.is_dir():
        for directory, folders, files in os.walk(source, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in folders + files):
                raise VaultError('Remove symbolic links before moving this folder.')
    mapping = {path: new + path[len(old):] for path in vault.notes if path == old or path.startswith(old + '/')}
    files = set()
    for directory, folders, names in os.walk(vault.root, followlinks=False):
        folders[:] = [name for name in folders if not name.startswith('.') and not (Path(directory) / name).is_symlink()]
        for name in names:
            item = Path(directory) / name
            if name.startswith('.') or item.is_symlink():
                continue
            path = item.relative_to(vault.root).as_posix()
            files.add(path)
            if path == old or path.startswith(old + '/'):
                mapping[path] = new + path[len(old):]
    future = copy.copy(vault)
    future.notes = {mapping.get(path, path): note for path, note in vault.notes.items()}
    future_files = {mapping.get(path, path) for path in files}
    updates, originals = [], {}
    for path, note in vault.notes.items():
        if note is None:
            raise VaultError(f'{path} cannot be read. Resolve this before updating links.')
        raw = vault._path(path).read_bytes()
        if fingerprint(raw) != note.revision:
            raise Conflict(f'{path} changed outside the app. Refresh before moving notes.')
        note = vault.indexed_note(path)
        text = rewrite_links(vault, note, mapping, future, future_files)
        if text != note.text:
            originals[path] = raw
            updates.append((note, text))
    backup = _prepare_backup(vault, old, new, originals, mapping)
    written = []
    try:
        for note, text in updates:
            saved = vault.save(note, text)
            written.append((note, saved))
        # Validate the complete snapshot again immediately before moving.
        for path, note in vault.notes.items():
            if fingerprint(vault._path(path).read_bytes()) != note.revision:
                raise Conflict(f'{path} changed during the move. Try again after refreshing.')
        vault._visible_path(old)
        vault._visible_path(new)
        if target.exists():
            raise VaultError('The destination appeared during the move.')
        source.rename(target)
    except Exception as error:
        rollback_errors = []
        for original, saved in reversed(written):
            # Never overwrite an intervening external edit during rollback.
            try:
                vault.save(saved, original.text)
            except (Conflict, OSError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        vault.refresh()
        if rollback_errors:
            raise VaultError(f'Move stopped and some link updates could not be rolled back. '
                             f'Original note bytes and the move manifest are preserved in {backup}. '
                             f'{rollback_errors[0]}') from error
        if backup:
            try:
                shutil.rmtree(backup)
            except OSError:
                raise VaultError(f'Move stopped. Original notes were restored; a recovery copy remains in {backup}. {error}') from error
        raise
    vault.refresh()
    if backup:
        try:
            shutil.rmtree(backup)
        except OSError:
            vault.organization_notice = f'Recovery copies retained in {backup}'
    return mapping


def trash(vault, relative):
    from .vault import Conflict, VaultError, fingerprint
    source = vault._visible_path(relative.rstrip('/'))
    if not source.exists():
        raise VaultError('The selected note or folder no longer exists.')
    for path, note in vault.notes.items():
        if path == relative.rstrip('/') or path.startswith(relative.rstrip('/') + '/'):
            if note is None or fingerprint(vault._path(path).read_bytes()) != note.revision:
                raise Conflict(f'{path} changed outside the app. Refresh before moving it to trash.')
    directory = _trash_directory(vault)
    target = directory / f'{source.stem}-{uuid.uuid4().hex[:12]}{source.suffix}'
    source.rename(target)
    vault.refresh()
    return target
