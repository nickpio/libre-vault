"""Coalesced filesystem notifications, owned by one open vault window."""
from collections import deque
import os
from pathlib import Path
import sys
from threading import Lock
import weakref

from gi.repository import Gio, GLib

from .vault import VaultError


class VaultMonitor:
    def __init__(self, vault, changed, failed):
        self.vault = vault
        self.changed = changed
        self.failed = failed
        self.monitors = {}
        self.pending = set()
        self.source = None
        self.closed = False
        self._darwin = None
        # Watching the parent also detects replacement/removal of the root.
        self.parent = self.watch(vault.root.parent, self.parent_event)
        self.watch_tree('')
        # Close the gap between Vault's initial scan and watch installation.
        self.queue('')

    def watch(self, path, callback):
        # GLib's macOS backend is kqueue. A directory kqueue does not report
        # writes to files already in it, and an atomic rename often produces no
        # second directory event, so the replaced file never appears in the
        # callback. FSEvents reports the tree; the snapshot names the child
        # that changed. The parent watch stays on Gio: kqueue does report
        # creation, deletion, and rename of the vault directory itself, and a
        # recursive watch of the parent would observe every sibling.
        if sys.platform == 'darwin' and getattr(callback, '__func__', None) is VaultMonitor.event:
            if self._darwin is None:
                self._darwin = _DarwinWatchHub(self)
            return self._darwin.watch(path)
        try:
            monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            handler = monitor.connect('changed', callback)
            return monitor, handler
        except GLib.Error as error:
            self.failed(f'Could not monitor {path}: {error}. Reopen the vault to retry.')
            return None

    def watch_tree(self, relative):
        if self.vault.root.is_symlink() or not self.vault.root.is_dir():
            for watched in self.monitors.values():
                self.cancel(watched)
            self.monitors.clear()
            return
        try:
            path = self.vault._visible_path(relative) if relative else self.vault.root
        except VaultError:
            return
        if not path.is_dir():
            return
        if self.vault.root.is_symlink():
            return
        # Scans already discovered the folders. Installing watches must not walk
        # and stat every note again on the UI thread.
        keys = ['', *self.vault.folders] if not relative else [relative, *(
            name for name in self.vault.folders if name.startswith(relative + '/'))]
        for key in keys:
            if key not in self.monitors:
                directory = self.vault.root / key
                # FSEvents can report the parent when a nested directory is
                # removed. The folder list still names that directory until the
                # scan finishes, and installing a watch from the stale name puts
                # the monitor back. Linux inotify names the removed directory,
                # so flush cancels that key directly.
                if sys.platform == 'darwin' and key and not directory.is_dir():
                    continue
                watched = self.watch(directory, self.event)
                if watched:
                    self.monitors[key] = watched

    def parent_event(self, _, file, other, event):
        if any(item is not None and item.get_path() == str(self.vault.root) for item in (file, other)):
            self.queue('')

    def event(self, _, file, other, event):
        for item in (file, other):
            self._queue_disk_path(None if item is None else item.get_path())

    def _queue_disk_path(self, path):
        if not path:
            return
        try:
            relative = Path(path).relative_to(self.vault.root)
        except ValueError:
            return
        if not self.vault.path_allowed(relative.as_posix()):
            return
        self.queue('' if relative == Path('.') else relative.as_posix())

    def queue(self, relative):
        if self.closed:
            return
        self.pending.add(relative)
        if self.source is None:
            # Bound latency during continuous writes while coalescing bursts.
            self.source = GLib.timeout_add(100, self.flush)

    @staticmethod
    def cancel(watched):
        monitor, handler = watched
        monitor.disconnect(handler)
        monitor.cancel()

    def flush(self):
        self.source = None
        paths, self.pending = self.pending, set()
        if self.closed:
            return False
        for relative in paths:
            path = self.vault.root / relative
            # Reinstall subtree watches even for replacement at the same path:
            # an old watch may still refer to the directory's previous inode.
            if not relative or relative in self.monitors or path.is_dir():
                for key in list(self.monitors):
                    if not relative or key == relative or key.startswith(relative + '/'):
                        self.cancel(self.monitors.pop(key))
                self.watch_tree(relative)
        self.changed({''} if self.vault.options.follow_symlinks else paths)
        return False

    def close(self):
        self.closed = True
        if self.source is not None:
            GLib.source_remove(self.source)
            self.source = None
        self.pending.clear()
        for watched in self.monitors.values():
            self.cancel(watched)
        self.monitors.clear()
        if self.parent is not None:
            self.cancel(self.parent)
            self.parent = None
        if self._darwin is not None:
            self._darwin.close()
            self._darwin = None
        self.changed = self.failed = None


def _directory_snapshot(path):
    entries = {}
    try:
        with os.scandir(path) as scan:
            for entry in scan:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries[entry.name] = (info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode)
    except OSError:
        return None
    return entries


def _reconcile_directory(monitor, path, previous):
    """Queue direct children whose directory entry changed since the last snapshot."""
    current = _directory_snapshot(path)
    if current is None:
        if previous is not None:
            monitor._queue_disk_path(path)
        return None
    if previous is None:
        monitor._queue_disk_path(path)
        return current
    for name in set(previous) | set(current):
        if previous.get(name) != current.get(name):
            monitor._queue_disk_path(Path(path) / name)
    return current


class _DarwinRegistration:
    """One directory entry in VaultMonitor.monitors. The stream itself is shared."""

    def __init__(self, hub, path):
        self._hub = hub
        self._path = path

    def disconnect(self, handler):
        return None

    def cancel(self):
        hub = self._hub
        self._hub = None
        if hub is not None:
            hub.unregister(self._path)


class _DarwinWatchHub:
    """One FSEvents stream for a vault, plus a snapshot of each watched directory.

    Event paths are only a wake-up. macOS may report the hidden temporary name
    from an atomic replace and omit the file that was replaced; the snapshot
    diff records the path whose inode or mtime actually changed.
    """

    def __init__(self, monitor):
        self.monitor_ref = weakref.ref(monitor)
        self.snapshots = {}
        self._lock = Lock()
        self._pending = deque()
        self._running = False
        self._timer = None
        self._stream = None
        self._callback = None
        self._context = None
        self._paths = None
        self._cfpath = None
        self._error = None

    def watch(self, path):
        key = os.path.normpath(str(path))
        self.snapshots[key] = _directory_snapshot(key)
        if not self._ensure_started():
            self.snapshots.pop(key, None)
            return None
        return _DarwinRegistration(self, key), 0

    def unregister(self, path):
        self.snapshots.pop(os.path.normpath(str(path)), None)
        if not self.snapshots:
            self._stop()

    def close(self):
        self.snapshots.clear()
        self._stop()
        self.monitor_ref = lambda: None

    def _root(self):
        monitor = self.monitor_ref()
        if monitor is None or monitor.vault is None:
            return None
        return os.path.normpath(str(monitor.vault.root))

    def _ensure_started(self):
        if self._stream is not None:
            return True
        root = self._root()
        if root is None:
            return False
        try:
            api = _fsevents_api()
        except OSError as error:
            self._fail(f'Could not monitor {root}: {error}. Reopen the vault to retry.')
            return False
        callback_type = api['callback_type']
        hub = weakref.ref(self)

        def callback(stream, info, count, paths, flags, ids):
            owner = hub()
            if owner is None:
                return
            try:
                copied = []
                for index in range(count):
                    raw = paths[index]
                    if raw:
                        copied.append(os.fsdecode(raw))
                with owner._lock:
                    if owner._running and copied:
                        owner._pending.append(copied)
            except Exception as error:
                owner._error = error

        self._callback = callback_type(callback)
        self._context = api['context']()
        cfpath = api['cfstring'](os.fsencode(root))
        if not cfpath:
            self._fail(f'Could not monitor {root}: FSEvents did not start. Reopen the vault to retry.')
            self._callback = None
            return False
        array = api['cfarray'](cfpath)
        self._cfpath = cfpath
        self._paths = array
        stream = api['create'](self._callback, self._context, array)
        if not stream:
            self._release_cf(api)
            self._callback = None
            self._fail(f'Could not monitor {root}: FSEvents did not start. Reopen the vault to retry.')
            return False
        api['schedule'](stream)
        with self._lock:
            self._running = True
        if self._timer is None:
            self._timer = GLib.timeout_add(50, self._drain)
        if not api['start'](stream):
            # Start never succeeded, so Stop would be invalid. Drop the stream
            # and the timer without going through the running-stream shutdown.
            api['release'](stream)
            self._callback = None
            self._release_cf(api)
            with self._lock:
                self._running = False
                self._pending.clear()
            if self._timer is not None:
                GLib.source_remove(self._timer)
                self._timer = None
            self._fail(f'Could not monitor {root}: FSEvents did not start. Reopen the vault to retry.')
            return False
        self._stream = stream
        return True

    def _drain(self):
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        if not self._running:
            self._timer = None
            return False
        with self._lock:
            batches = list(self._pending)
            self._pending.clear()
        paths = [path for batch in batches for path in batch]
        if paths:
            self._handle(paths)
        return self._running

    def _handle(self, event_paths):
        monitor = self.monitor_ref()
        if monitor is None or monitor.closed:
            return
        nearest_keys = []
        for raw in event_paths:
            path = Path(os.path.normpath(raw))
            parent = os.path.normpath(str(path.parent))
            key = os.path.normpath(str(path))
            # A child created before its directory watch exists still has to be
            # named. Registered directories are diffed instead, so a hidden
            # temporary from an atomic replace is not treated as the edit.
            if parent not in self.snapshots and key not in self.snapshots:
                monitor._queue_disk_path(path)
            nearest = self._nearest(path)
            if nearest is not None and nearest not in nearest_keys:
                nearest_keys.append(nearest)
        for key in nearest_keys:
            if key not in self.snapshots:
                continue
            self.snapshots[key] = _reconcile_directory(monitor, key, self.snapshots.get(key))
        # Ancestor mtimes move when a nested directory is removed. Refresh those
        # snapshots without queueing the ancestor, or flush reinstalls watches
        # from the folder list that the removal has not updated yet.
        for key in nearest_keys:
            self._refresh_ancestors(key, skip=set(nearest_keys))

    def _nearest(self, path):
        current = Path(os.path.normpath(str(path)))
        if os.path.normpath(str(current)) not in self.snapshots:
            current = current.parent
        while True:
            key = os.path.normpath(str(current))
            if key in self.snapshots:
                return key
            if current.parent == current:
                return None
            current = current.parent

    def _refresh_ancestors(self, start, skip):
        current = Path(start).parent
        root = self._root()
        while True:
            key = os.path.normpath(str(current))
            if key in self.snapshots and key not in skip:
                self.snapshots[key] = _directory_snapshot(key)
            if current.parent == current or key == root:
                break
            current = current.parent

    def _stop(self):
        with self._lock:
            self._running = False
            self._pending.clear()
        timer = self._timer
        self._timer = None
        if timer is not None:
            GLib.source_remove(timer)
        stream = self._stream
        self._stream = None
        if stream is not None:
            api = _fsevents_api()
            api['stop'](stream)
            api['invalidate'](stream)
            api['release'](stream)
            # A callback already running on the dispatch queue can outlast
            # invalidation, so its native thunk must never be freed. It holds
            # only a weak reference to this hub.
            _RETIRED_CALLBACKS.append(self._callback)
        self._callback = None
        self._release_cf()

    def _release_cf(self, api=None):
        paths, cfpath = self._paths, self._cfpath
        self._paths = self._cfpath = None
        if paths is None and cfpath is None:
            return
        api = api or _fsevents_api()
        if paths is not None:
            api['release_cf'](paths)
        if cfpath is not None:
            api['release_cf'](cfpath)

    def _fail(self, message):
        monitor = self.monitor_ref() if callable(self.monitor_ref) else None
        if monitor is not None and not monitor.closed and monitor.failed is not None:
            monitor.failed(message)


_FS_EVENTS = None
_RETIRED_CALLBACKS = []


def _fsevents_api():
    global _FS_EVENTS
    if _FS_EVENTS is not None:
        return _FS_EVENTS
    import ctypes
    core_services = ctypes.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
    core_foundation = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    system = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    kFSEventStreamCreateFlagNoDefer = 0x00000002
    kFSEventStreamCreateFlagWatchRoot = 0x00000004
    kFSEventStreamCreateFlagFileEvents = 0x00000010
    kFSEventStreamEventIdSinceNow = ctypes.c_uint64(-1).value
    kCFStringEncodingUTF8 = 0x08000100
    callback_type = ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint64))

    class _StreamContext(ctypes.Structure):
        _fields_ = [
            ('version', ctypes.c_long),
            ('info', ctypes.c_void_p),
            ('retain', ctypes.c_void_p),
            ('release', ctypes.c_void_p),
            ('copyDescription', ctypes.c_void_p),
        ]

    system.dlsym.restype = ctypes.c_void_p
    system.dlsym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    system.dispatch_queue_create.restype = ctypes.c_void_p
    system.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    callbacks = system.dlsym(ctypes.c_void_p(-2), b'kCFTypeArrayCallBacks')
    if not callbacks:
        raise OSError('kCFTypeArrayCallBacks is unavailable')
    queue = system.dispatch_queue_create(b'org.librevault.fsevents', None)
    if not queue:
        raise OSError('Could not create the FSEvents queue')
    core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
    core_foundation.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    core_foundation.CFArrayCreateMutable.restype = ctypes.c_void_p
    core_foundation.CFArrayCreateMutable.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    core_foundation.CFArrayAppendValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_services.FSEventStreamCreate.restype = ctypes.c_void_p
    core_services.FSEventStreamCreate.argtypes = [
        ctypes.c_void_p, callback_type, ctypes.POINTER(_StreamContext),
        ctypes.c_void_p, ctypes.c_uint64, ctypes.c_double, ctypes.c_uint32]
    core_services.FSEventStreamSetDispatchQueue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    core_services.FSEventStreamStart.argtypes = [ctypes.c_void_p]
    core_services.FSEventStreamStart.restype = ctypes.c_bool
    core_services.FSEventStreamStop.argtypes = [ctypes.c_void_p]
    core_services.FSEventStreamInvalidate.argtypes = [ctypes.c_void_p]
    core_services.FSEventStreamRelease.argtypes = [ctypes.c_void_p]
    flags = (kFSEventStreamCreateFlagNoDefer | kFSEventStreamCreateFlagWatchRoot
             | kFSEventStreamCreateFlagFileEvents)

    def cfstring(encoded):
        return core_foundation.CFStringCreateWithCString(None, encoded, kCFStringEncodingUTF8)

    def cfarray(cfpath):
        array = core_foundation.CFArrayCreateMutable(None, 1, callbacks)
        core_foundation.CFArrayAppendValue(array, cfpath)
        return array

    def create(callback, context, array):
        return core_services.FSEventStreamCreate(
            None, callback, ctypes.byref(context), array,
            kFSEventStreamEventIdSinceNow, 0.05, flags)

    def schedule(stream):
        core_services.FSEventStreamSetDispatchQueue(stream, queue)

    def start(stream):
        return bool(core_services.FSEventStreamStart(stream))

    _FS_EVENTS = {
        'callback_type': callback_type,
        'context': _StreamContext,
        'cfstring': cfstring,
        'cfarray': cfarray,
        'create': create,
        'schedule': schedule,
        'start': start,
        'stop': core_services.FSEventStreamStop,
        'invalidate': core_services.FSEventStreamInvalidate,
        'release': core_services.FSEventStreamRelease,
        'release_cf': core_foundation.CFRelease,
    }
    return _FS_EVENTS
