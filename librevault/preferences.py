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
