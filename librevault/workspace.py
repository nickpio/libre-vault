"""Per-vault workspace state stored outside the vault."""
import copy
import json
from pathlib import Path

from .durable import write_json


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


def workspace_name(name):
    """A saved-layout label: visible text, at most 80 characters, no NULs."""
    if type(name) is not str:
        return None
    name = name.strip()
    if not name or '\x00' in name or len(name) > 80:
        return None
    return name


class SavedWorkspacesStore:
    """Named layouts for each vault, in saved-workspaces.json beside workspaces.json.

    A layout is the same document the live workspace stores: open tabs, the active
    tab, split state, collapsed folders, and the sidebar search query. It never
    includes note text, and it is not written into the vault.
    """

    def __init__(self, preferences_path):
        self.path = Path(preferences_path).with_name('saved-workspaces.json')

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def _key(self, root):
        return str(Path(root).resolve())

    def _vault(self, root):
        vault = self._read().get(self._key(root), {})
        return vault if isinstance(vault, dict) else {}

    def names(self, root):
        vault = self._vault(root)
        return [name for name in vault if workspace_name(name) and isinstance(vault[name], dict)]

    def get(self, root, name):
        state = self._vault(root).get(name)
        return copy.deepcopy(state) if isinstance(state, dict) else None

    def save(self, root, name, state):
        label = workspace_name(name)
        if label is None:
            raise ValueError('Enter a workspace name up to 80 characters.')
        if not isinstance(state, dict):
            raise ValueError('Workspace layout could not be saved.')
        data = self._read()
        key = self._key(root)
        vault = data.get(key)
        if not isinstance(vault, dict):
            vault = {}
        vault[label] = copy.deepcopy(state)
        data[key] = vault
        _write(self.path, data)

    def delete(self, root, name):
        data = self._read()
        key = self._key(root)
        vault = data.get(key)
        if not isinstance(vault, dict) or name not in vault:
            return False
        vault.pop(name, None)
        if vault:
            data[key] = vault
        else:
            data.pop(key, None)
        _write(self.path, data)
        return True


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(path, data, prefix='.workspace-', indent=2)
