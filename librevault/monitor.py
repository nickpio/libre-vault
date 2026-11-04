"""Coalesced filesystem notifications, owned by one open vault window."""
import os
from pathlib import Path

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
        # Watching the parent also detects replacement/removal of the root.
        self.parent = self.watch(vault.root.parent, self.parent_event)
        self.watch_tree('')
        # Close the gap between Vault's initial scan and watch installation.
        self.queue('')

    def watch(self, path, callback):
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
                watched = self.watch(self.vault.root / key, self.event)
                if watched:
                    self.monitors[key] = watched

    def parent_event(self, _, file, other, event):
        if any(item is not None and item.get_path() == str(self.vault.root) for item in (file, other)):
            self.queue('')

    def event(self, _, file, other, event):
        for item in (file, other):
            path = item.get_path() if item is not None else None
            if path is None:
                continue
            try:
                relative = Path(path).relative_to(self.vault.root)
            except ValueError:
                continue
            if not self.vault.path_allowed(relative.as_posix()):
                continue
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
        self.changed = self.failed = None
