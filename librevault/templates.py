"""Template variables, daily notes, unique notes, and random note selection."""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
import random
import re

from .vault import VaultError


DEFAULT_DATE_FORMAT = 'YYYY-MM-DD'
DEFAULT_TIME_FORMAT = 'HH:mm'

_WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
_WEEKDAYS_SHORT = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_TOKENS = {
    'YYYY': lambda when: f'{when.year:04d}',
    'YY': lambda when: f'{when.year % 100:02d}',
    'MM': lambda when: f'{when.month:02d}',
    'DD': lambda when: f'{when.day:02d}',
    'dddd': lambda when: _WEEKDAYS[when.weekday()],
    'ddd': lambda when: _WEEKDAYS_SHORT[when.weekday()],
    'HH': lambda when: f'{when.hour:02d}',
    'mm': lambda when: f'{when.minute:02d}',
    'ss': lambda when: f'{when.second:02d}',
}
_VARIABLE = re.compile(r'\{\{\s*(title|date|time)(?:\s*:\s*([^{}]*?))?\s*\}\}')


def current_time():
    return datetime.now()


def format_moment(when: datetime, pattern: str) -> str:
    """Replace Obsidian moment tokens. A longer run of the same letter stays literal.

    Every supported token is a run of one letter, so `YYYY` is a year and `MMMM`
    stays text. Weekday names are English so a note does not change with the locale.
    """
    if not pattern:
        return ''
    parts = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        end = index + 1
        while end < length and pattern[end] == char:
            end += 1
        run = pattern[index:end]
        parts.append(_TOKENS[run](when) if run in _TOKENS else run)
        index = end
    return ''.join(parts)


def expand_template(text: str, *, title: str, when: datetime | None = None,
                    date_format: str = DEFAULT_DATE_FORMAT,
                    time_format: str = DEFAULT_TIME_FORMAT) -> str:
    """Expand {{title}}, {{date}}, {{date:FORMAT}}, {{time}}, and {{time:FORMAT}}."""
    moment = when or current_time()

    def replace(match):
        kind = match.group(1)
        custom = match.group(2)
        if kind == 'title':
            return match.group(0) if custom is not None else title
        fallback = date_format if kind == 'date' else time_format
        pattern = custom.strip() if custom is not None and custom.strip() else fallback
        return format_moment(moment, pattern)

    return _VARIABLE.sub(replace, text)


def _parts_safe(text: str) -> bool:
    if not text or '\\' in text or text.startswith('/') or '//' in text:
        return False
    return all(part and part not in ('.', '..') and not part.startswith('.') for part in text.split('/'))


def _clean_folder(folder: str) -> str:
    raw = (folder or '').strip()
    if not raw:
        return ''
    if raw.startswith('/') or '\\' in raw or '//' in raw:
        raise VaultError('Choose a folder inside the vault.')
    text = raw.strip('/')
    if text and not _parts_safe(text):
        raise VaultError('Choose a folder inside the vault.')
    return text


def _join_folder(folder: str, name: str) -> str:
    folder = (folder or '').strip().strip('/')
    name = (name or '').strip().strip('/')
    if folder and name:
        return f'{folder}/{name}'
    return folder or name


def note_relative(folder: str, stem: str) -> str:
    """Build a visible .md path. Slashes in the stem become folders."""
    folder = _clean_folder(folder)
    stem = (stem or '').strip()
    if stem.startswith('/') or '\\' in stem or '//' in stem:
        raise VaultError('Choose a note path inside the vault.')
    if stem.lower().endswith('.md'):
        stem = stem[:-3].rstrip()
    if not stem:
        raise VaultError('The note format did not produce a file name.')
    relative = _join_folder(folder, stem)
    if not relative.lower().endswith('.md'):
        relative += '.md'
    if not _parts_safe(relative):
        raise VaultError('Choose a note path inside the vault.')
    return relative


def _parent_folder(relative: str) -> str:
    parent = PurePosixPath(relative).parent
    if str(parent) == '.':
        return ''
    return parent.as_posix()


def template_file(template: str) -> str:
    raw = (template or '').strip()
    if not raw:
        return ''
    if raw.startswith('/') or '\\' in raw or '//' in raw:
        raise VaultError('Choose a template note inside the vault.')
    text = raw.strip('/')
    if not text.lower().endswith('.md'):
        text += '.md'
    if not _parts_safe(text):
        raise VaultError('Choose a template note inside the vault.')
    return text


def template_paths(vault, folder: str) -> list[str]:
    """Readable notes inside the templates folder. An empty folder searches the vault."""
    try:
        folder = _clean_folder(folder)
    except VaultError:
        return []
    prefix = f'{folder}/' if folder else ''
    found = [path for path, record in vault.notes.items()
             if record is not None and (not prefix or path.startswith(prefix))]
    return sorted(found)


def daily_note_path(folder: str, date_format: str, when: datetime) -> str:
    pattern = (date_format or '').strip()
    if not pattern:
        raise VaultError('Set a daily note date format.')
    return note_relative(folder, format_moment(when, pattern).strip())


def unique_note_path(folder: str, note_format: str, when: datetime, title: str = '') -> str:
    pattern = (note_format or '').strip()
    if not pattern:
        raise VaultError('Set a unique note format.')
    rendered = format_moment(when, pattern).strip()
    title = (title or '').strip()
    if title:
        if '\\' in title or '/' in title or title in ('.', '..') or title.startswith('.') or '\x00' in title:
            raise VaultError('Use a title without folders or hidden names.')
        rendered = f'{rendered} - {title}'
    return note_relative(folder, rendered)


def _note_taken(vault, relative: str) -> bool:
    if relative in vault.notes:
        return True
    try:
        return vault._path(relative).exists()
    except VaultError:
        return True


def unused_note_path(vault, relative: str) -> str:
    if not _note_taken(vault, relative):
        return relative
    path = PurePosixPath(relative)
    parent = '' if str(path.parent) == '.' else path.parent.as_posix()
    stem = path.stem
    for number in range(1, 1000):
        candidate = note_relative(parent, f'{stem} {number}')
        if not _note_taken(vault, candidate):
            return candidate
    raise VaultError('Could not choose an unused note name.')


def _ensure_folder(vault, folder: str) -> None:
    folder = _clean_folder(folder)
    if not folder:
        return
    built = []
    for part in folder.split('/'):
        built.append(part)
        current = '/'.join(built)
        if current in vault.folders:
            continue
        try:
            vault.create_folder(current)
        except VaultError:
            vault.update_paths([current])
            if current not in vault.folders:
                raise


def _create_note(vault, relative: str, content: str):
    _ensure_folder(vault, _parent_folder(relative))
    note = vault.create(relative)
    if content:
        note = vault.save(note, content)
    return note


def read_template(vault, template: str) -> str:
    relative = template_file(template)
    if not relative:
        raise VaultError('Choose a template note.')
    if relative not in vault.notes or vault.notes[relative] is None:
        raise VaultError(f'Template not found: {relative}')
    return vault.read(relative).text


def _rendered_template(vault, template: str, title: str, when: datetime) -> str:
    if not (template or '').strip():
        return ''
    return expand_template(read_template(vault, template), title=title, when=when)


def ensure_daily_note(vault, preferences, when: datetime | None = None):
    """Return today's daily note and whether this call created it.

    An existing note is opened unchanged. A configured template is expanded only
    for a note created here. The folder is created when it is missing.
    """
    moment = when or current_time()
    relative = daily_note_path(preferences.daily_notes_folder, preferences.daily_notes_format, moment)
    if _note_taken(vault, relative):
        if relative not in vault.notes:
            vault.update_paths([relative])
        if vault.notes.get(relative) is None:
            raise VaultError(f'{relative} is not readable.')
        return vault.read(relative), False
    content = _rendered_template(vault, preferences.daily_notes_template, PurePosixPath(relative).stem, moment)
    return _create_note(vault, relative, content), True


def create_unique_note(vault, preferences, title: str = '', template: str = '', when: datetime | None = None):
    """Create a Zettelkasten note, suffixing the name if that timestamp is taken."""
    moment = when or current_time()
    relative = unused_note_path(vault, unique_note_path(
        preferences.unique_notes_folder, preferences.unique_notes_format, moment, title))
    content = _rendered_template(vault, template, PurePosixPath(relative).stem, moment)
    return _create_note(vault, relative, content)


def create_note_from_template(vault, template: str, name: str, when: datetime | None = None):
    """Create a named note whose body is the expanded template."""
    moment = when or current_time()
    relative = note_relative('', name)
    if relative in vault.notes or _note_taken(vault, relative):
        raise VaultError('A note already exists at that path.')
    content = _rendered_template(vault, template, PurePosixPath(relative).stem, moment)
    return _create_note(vault, relative, content)


def random_note_path(vault, rng=None) -> str | None:
    """Pick one readable note. Unreadable paths and an empty vault return nothing."""
    paths = [path for path, record in vault.notes.items() if record is not None]
    if not paths:
        return None
    return (rng or random).choice(paths)
