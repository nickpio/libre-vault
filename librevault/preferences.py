"""Validated application preferences, stored separately from note vaults."""
from dataclasses import asdict, dataclass, fields
import json
from .durable import write_json
from .platform_support import app_directory
from pathlib import Path
import unicodedata


OPTIONS = {
    'theme': (('dark', 'Dark'), ('light', 'Light'), ('lights_out', 'Lights Out')),
}
FONT_PRESETS = (('sans', 'Sans serif'), ('serif', 'Serif'), ('monospace', 'Monospace'))
FONT_PRESET_IDS = frozenset(item[0] for item in FONT_PRESETS)
_FONT_EXTRA = set(' .+_-')
_FONT_NAME_LIMIT = 120
MIN_FONT_SIZE = 11
BOUNDS = {'interface_size': (MIN_FONT_SIZE, 18), 'editor_size': (MIN_FONT_SIZE, 32), 'line_width': (480, 1200)}
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


def normalize_editor_font(value):
    """Return a preset or one family name, or None when it cannot be stored.

    Custom names are quoted into the stylesheet, so quotes, controls, and other
    characters that could change the CSS are rejected.
    """
    if type(value) is not str:
        return None
    text = value.strip()
    if text in FONT_PRESET_IDS:
        return text
    if not text or len(text) > _FONT_NAME_LIMIT:
        return None
    if not any(unicodedata.category(char).startswith(('L', 'N')) for char in text):
        return None
    for char in text:
        if char in _FONT_EXTRA:
            continue
        if not unicodedata.category(char).startswith(('L', 'N')):
            return None
    return text


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
    interface_size: int = MIN_FONT_SIZE
    editor_font: str = 'sans'
    editor_size: int = 12
    readable_length: bool = True
    line_width: int = 680
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
    python_plugins: bool = False

    @classmethod
    def from_dict(cls, data):
        defaults = cls()
        if not isinstance(data, dict):
            return defaults
        values = {}
        for field in fields(cls):
            key = field.name
            value = data.get(key, getattr(defaults, key))
            if key == 'editor_font':
                normalized = normalize_editor_font(value)
                valid = normalized is not None
                if valid:
                    value = normalized
            elif key in OPTIONS:
                valid = type(value) is str and value in dict(OPTIONS[key])
            elif key in BOUNDS:
                low, high = BOUNDS[key]
                if key in ('interface_size', 'editor_size') and type(value) is int and 0 < value < low:
                    value = low
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
        config = app_directory('config')
        self.path = Path(path) if path is not None else config / 'settings.json'

    def load(self):
        try:
            return Preferences.from_dict(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError, UnicodeError):
            return Preferences()

    def save(self, preferences):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(self.path, asdict(preferences), prefix='.settings-', indent=2)
