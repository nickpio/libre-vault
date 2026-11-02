"""Private crash recovery drafts, separate from vault files."""
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from .vault import Note


class RecoveryStore:
    def __init__(self, root, directory=None):
        base = Path(directory) if directory else Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'libre-vault' / 'recovery'
        self.directory = base / hashlib.sha256(str(root).encode()).hexdigest()
        self.session = self.directory / uuid.uuid4().hex
        self.lock = None

    def _start(self):
        if self.lock is None:
            self.session.mkdir(mode=0o700, parents=True)
            self.lock = (self.session / 'lock').open('w')
            fcntl.flock(self.lock, fcntl.LOCK_EX)

    def write(self, note, text):
        self._start()
        target = self.session / (hashlib.sha256(note.path.encode()).hexdigest() + '.json')
        fd, temporary = tempfile.mkstemp(dir=self.session, prefix='.draft-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump({'note': asdict(note), 'text': text}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove(self, path):
        target = self.session / (hashlib.sha256(path.encode()).hexdigest() + '.json')
        target.unlink(missing_ok=True)

    def drafts(self):
        if not self.directory.exists():
            return []
        result = []
        for session in self.directory.iterdir():
            if not session.is_dir() or session == self.session:
                continue
            try:
                with (session / 'lock').open('a') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    for path in sorted(session.glob('*.json')):
                        try:
                            data = json.loads(path.read_text(encoding='utf-8'))
                            note = Note(**data['note'])
                            if all(type(value) is str for value in (note.path, note.text, note.revision, data['text'])) and type(note.bom) is bool:
                                result.append((path, note, data['text']))
                        except (OSError, ValueError, TypeError, KeyError):
                            continue
            except OSError:
                continue
        return result

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None
