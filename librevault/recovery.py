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
import queue
import threading
import uuid

from .durable import replace_durably, synced_temporary
from .vault import Note


class _DraftJob:
    """One recovery operation, optionally awaitable by a synchronous caller."""
    __slots__ = ('kind', 'note', 'text', 'event', 'error')

    def __init__(self, kind, note=None, text=None, event=None):
        self.kind = kind
        self.note = note
        self.text = text
        self.event = event
        self.error = None


class RecoveryStore:
    """Crash drafts with coalesced, ordered background writes.

    Editor changes call ``schedule_write``/``schedule_remove``. They only
    update the latest pending operation for each note, so bursts of typing
    collapse into one draft write after a short bounded delay. A single
    worker performs serialization, file fsync, atomic replacement, and
    directory fsync away from the UI thread, and ``flush``/``close`` wait
    for pending work before shutdown.
    """

    COALESCE_SECONDS = 0.25

    def __init__(self, root, directory=None):
        base = Path(directory) if directory else Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'libre-vault' / 'recovery'
        self.directory = base / hashlib.sha256(str(root).encode()).hexdigest()
        self.session = self.directory / uuid.uuid4().hex
        self.lock = None
        self.on_error = None
        self._pending = {}
        self._pending_condition = threading.Condition()
        self._timer = None
        self._jobs = queue.Queue()
        self._worker = None
        self._worker_lock = threading.Lock()
        self._outstanding = 0
        self._closed = False

    def _start(self):
        if self.lock is None:
            self.session.mkdir(mode=0o700, parents=True)
            self.lock = (self.session / 'lock').open('w')
            fcntl.flock(self.lock, fcntl.LOCK_EX)

    def _write_now(self, note, text):
        self._start()
        target = self.session / (hashlib.sha256(note.path.encode()).hexdigest() + '.json')

        def write(handle):
            json.dump({'note': asdict(note), 'text': text}, handle, ensure_ascii=False)

        with synced_temporary(self.session, write, prefix='.draft-', binary=False) as temporary:
            replace_durably(temporary, target)

    def _remove_now(self, path):
        target = self.session / (hashlib.sha256(path.encode()).hexdigest() + '.json')
        target.unlink(missing_ok=True)

    def _ensure_worker(self):
        with self._worker_lock:
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name='librevault-recovery', daemon=True)
                self._worker.start()

    def _take_pending(self):
        with self._pending_condition:
            timer, self._timer = self._timer, None
            jobs = list(self._pending.items())
            self._pending.clear()
        if timer is not None:
            timer.cancel()
        return jobs

    def _enqueue(self, jobs):
        if not jobs:
            return
        self._ensure_worker()
        for path, job in jobs:
            self._jobs.put((path, job))

    def _schedule(self, path, job):
        with self._pending_condition:
            if self._closed:
                return
            if path not in self._pending:
                self._outstanding += 1
            self._pending[path] = job
            if self._timer is None:
                self._timer = threading.Timer(self.COALESCE_SECONDS, self._timer_fired)
                self._timer.daemon = True
                self._timer.start()

    def _timer_fired(self):
        with self._pending_condition:
            if self._closed:
                return
        self._enqueue(self._take_pending())

    def _submit(self, path, job):
        with self._pending_condition:
            if self._closed:
                raise OSError('Recovery store is closed')
            if path in self._pending:
                self._pending.pop(path)
            else:
                self._outstanding += 1
            job.event = threading.Event()
        self._enqueue([(path, job)])
        job.event.wait()
        if job.error is not None:
            raise job.error

    def _work(self):
        while True:
            item = self._jobs.get()
            if item is None:
                return
            path, job = item
            try:
                if job.kind == 'write':
                    self._write_now(job.note, job.text)
                else:
                    self._remove_now(path)
            except Exception as error:
                job.error = error
            awaited = job.event is not None
            if awaited:
                job.event.set()
            with self._pending_condition:
                self._outstanding -= 1
                self._pending_condition.notify_all()
            if job.error is not None and not awaited and self.on_error is not None:
                try:
                    self.on_error(job.error)
                except Exception:
                    pass

    def write(self, note, text):
        """Write a draft now, ordered after any work already queued."""
        self._submit(note.path, _DraftJob('write', note, text))

    def remove(self, path):
        """Remove a draft now, cancelling any pending write for that note."""
        self._submit(path, _DraftJob('remove'))

    def schedule_write(self, note, text):
        self._schedule(note.path, _DraftJob('write', note, text))

    def schedule_remove(self, path):
        self._schedule(path, _DraftJob('remove'))

    def flush(self):
        """Finish all pending draft operations."""
        self._enqueue(self._take_pending())
        with self._pending_condition:
            while self._outstanding:
                self._pending_condition.wait()

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
        with self._pending_condition:
            if self._closed:
                return
            self._closed = True
        self.flush()
        with self._worker_lock:
            worker, self._worker = self._worker, None
        if worker is not None:
            self._jobs.put(None)
            worker.join()
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
        if versions and versions[-1].revision == revision and versions[-1].text == text:
            return versions
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

        def write(handle):
            os.fchmod(handle.fileno(), 0o600)
            json.dump([asdict(version) for version in versions], handle, ensure_ascii=False)
            handle.write('\n')

        with synced_temporary(self.directory, write, prefix='.history-', binary=False) as temporary:
            replace_durably(temporary, target)
        os.chmod(target, 0o600)
