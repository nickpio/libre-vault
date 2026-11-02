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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.workspace-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
