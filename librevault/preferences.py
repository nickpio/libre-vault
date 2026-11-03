"""Validated application preferences, stored separately from note vaults."""
from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import tempfile


OPTIONS = {
    'theme': (('dark', 'Dark'), ('light', 'Light'), ('lights_out', 'Lights Out')),
    'editor_font': (('sans', 'Sans serif'), ('serif', 'Serif'), ('monospace', 'Monospace')),
}
BOUNDS = {'interface_size': (8, 18), 'editor_size': (8, 32), 'line_width': (480, 1200)}
STRINGS = frozenset({
    'templates_folder', 'daily_notes_folder', 'daily_notes_format', 'daily_notes_template',
    'unique_notes_folder', 'unique_notes_format',
})
_FOLDERS = frozenset({'templates_folder', 'daily_notes_folder', 'unique_notes_folder'})
_FORMATS = frozenset({'daily_notes_format', 'unique_notes_format'})
_TEMPLATE_FILES = frozenset({'daily_notes_template'})
_MAX_SETTING = 240


def _safe_relative(text):
    if not text or '\\' in text or text.startswith('/') or '//' in text:
        return False
    return all(part and part not in ('.', '..') and not part.startswith('.') for part in text.split('/'))


def normalize_string_setting(key, value):
    """Return a stored path or format, or None when the value cannot be kept.

    Folders may be empty, which means the vault root. Date formats may contain
    slashes. Hidden segments and parent traversal are rejected.
    """
    if type(value) is not str or any(char in value for char in '\x00\r\n'):
        return None
    raw = value.strip()
    if len(raw) > _MAX_SETTING or raw.startswith('/') or '\\' in raw or '//' in raw:
        return None
    if key in _FOLDERS:
        text = raw.strip('/')
        if text and not _safe_relative(text):
            return None
        return text
    if key in _FORMATS:
        if not _safe_relative(raw):
            return None
        return raw
    if key in _TEMPLATE_FILES:
        text = raw.strip('/')
        if not text:
            return ''
        candidate = text if text.lower().endswith('.md') else f'{text}.md'
        if not _safe_relative(candidate):
            return None
        return text
    return None


@dataclass(frozen=True)
class Preferences:
    theme: str = 'dark'
    interface_size: int = 10
    editor_font: str = 'sans'
    editor_size: int = 11
    readable_length: bool = True
    line_width: int = 780
    wrap_lines: bool = True
    continue_lists: bool = True
    templates_folder: str = 'Templates'
    daily_notes_folder: str = 'Daily'
    daily_notes_format: str = 'YYYY-MM-DD'
    daily_notes_template: str = ''
    unique_notes_folder: str = ''
    unique_notes_format: str = 'YYYYMMDDHHmm'
    slash_commands: bool = True
    vim_mode: bool = False

    @classmethod
    def from_dict(cls, data):
        defaults = cls()
        if not isinstance(data, dict):
            return defaults
        values = {}
        for field in fields(cls):
            key = field.name
            value = data.get(key, getattr(defaults, key))
            if key in OPTIONS:
                valid = type(value) is str and value in dict(OPTIONS[key])
            elif key in BOUNDS:
                low, high = BOUNDS[key]
                valid = type(value) is int and low <= value <= high
            elif key in STRINGS:
                normalized = normalize_string_setting(key, value)
                valid = normalized is not None
                if valid:
                    value = normalized
            else:
                valid = type(value) is bool
            values[key] = value if valid else getattr(defaults, key)
        return cls(**values)


class PreferencesStore:
    def __init__(self, path=None):
        config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        self.path = Path(path) if path is not None else config / 'libre-vault' / 'settings.json'

    def load(self):
        try:
            return Preferences.from_dict(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError, UnicodeError):
            return Preferences()

    def save(self, preferences):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.settings-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(asdict(preferences), handle, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
