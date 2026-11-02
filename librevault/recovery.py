"""Private recovery data, separate from vault files.

Crash drafts live under libre-vault/recovery. Saved-version history lives
under libre-vault/history/<vault-hash>/ with mode 0700.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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


@dataclass(frozen=True, slots=True)
class Snapshot:
    timestamp: str
    path: str
    revision: str
    text: str


class VersionHistoryStore:
    """Timestamped copies of saved notes, capped per note.

    The first changed save also keeps the text it replaced, so that version
    can be restored. Saving the same text again does not append a copy.
    """

    MAX_VERSIONS = 50

    def __init__(self, root, directory=None):
        base = Path(directory) if directory else Path(
            os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'libre-vault' / 'history'
        self.directory = base / hashlib.sha256(str(root).encode()).hexdigest()

    def _file(self, path):
        return self.directory / (hashlib.sha256(path.encode()).hexdigest() + '.json')

    def _ensure(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def versions(self, path):
        try:
            data = json.loads(self._file(path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            values = [item.get(key) for key in ('timestamp', 'path', 'revision', 'text')]
            if not all(isinstance(value, str) for value in values):
                continue
            result.append(Snapshot(*values))
        if len(result) > self.MAX_VERSIONS:
            result = result[-self.MAX_VERSIONS:]
        return result

    def record(self, path, revision, text, previous=None, timestamp=None, previous_mtime=None):
        """Append the saved text when it differs from the latest snapshot."""
        versions = self.versions(path)
        original = list(versions)
        stamp = timestamp or datetime.now(timezone.utc).isoformat(timespec='microseconds')
        if previous is not None:
            previous_revision, previous_text = previous
            if previous_text != text and (not versions or versions[-1].text != previous_text):
                previous_stamp = stamp
                if previous_mtime is not None:
                    previous_stamp = datetime.fromtimestamp(previous_mtime, timezone.utc).isoformat(timespec='seconds')
                versions.append(Snapshot(previous_stamp, path, previous_revision, previous_text))
        if not versions or versions[-1].text != text:
            versions.append(Snapshot(stamp, path, revision, text))
        if versions == original:
            return versions
        if len(versions) > self.MAX_VERSIONS:
            versions = versions[-self.MAX_VERSIONS:]
        self._write(path, versions)
        return versions

    def _write(self, path, versions):
        self._ensure()
        target = self._file(path)
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix='.history-')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump([asdict(version) for version in versions], handle, ensure_ascii=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
