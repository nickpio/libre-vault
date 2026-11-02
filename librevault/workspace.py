"""Per-vault workspace state stored outside the vault."""
import json
import os
from pathlib import Path
import tempfile


class WorkspaceStore:
    def __init__(self, preferences_path):
        self.path = Path(preferences_path).with_name('workspaces.json')

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def load(self, root):
        data = self._read().get(str(Path(root).resolve()), {})
        return data if isinstance(data, dict) else {}

    def save(self, root, state):
        data = self._read()
        data[str(Path(root).resolve())] = state
        _write(self.path, data)


class LastVaultStore:
    def __init__(self, preferences_path):
        self.path = Path(preferences_path).with_name('session.json')

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            path = data.get('last_vault') if isinstance(data, dict) else None
            return path if isinstance(path, str) and '\x00' not in path and Path(path).is_absolute() else None
        except (OSError, ValueError, UnicodeError):
            return None

    def save(self, root):
        _write(self.path, {'last_vault': str(root) if root is not None else None})


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.workspace-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
