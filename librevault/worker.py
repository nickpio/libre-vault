"""Cancellable serial vault jobs. SQLite connections never cross threads."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from time import monotonic

from .vault import Vault


class Cancelled(Exception):
    pass


@dataclass
class Snapshot:
    root: object
    options: object
    notes: dict
    canvases: dict
    folders: list

    @classmethod
    def of(cls, vault):
        return cls(vault.root, vault.options, dict(vault.notes), dict(vault.canvases), list(vault.folders))

    def apply(self, vault):
        vault.notes, vault.canvases, vault.folders = self.notes, self.canvases, self.folders
        vault.text_cache.clear()


class VaultWorker:
    """Latest request wins per channel, including already queued UI callbacks.

    dispatch must enqueue on the UI thread (GLib.idle_add in the application).
    close never waits on the UI thread. The job's finally closes its connection.
    """
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='vault')
        self.requests = {}
        self.closed = False

    def cancel(self, channel):
        old = self.requests.pop(channel, None)
        if old:
            old.set()

    def cancel_all(self):
        for channel in list(self.requests):
            self.cancel(channel)

    def submit(self, channel, root, options, operation, done, failed, progress, *, snapshot=None):
        self.cancel(channel)
        token = Event()
        self.requests[channel] = token

        def deliver(callback, value, *, final=False):
            def on_ui():
                if not self.closed and self.requests.get(channel) is token and not token.is_set():
                    if final:
                        self.requests.pop(channel, None)
                    callback(value)
                return False
            self.dispatch(on_ui)

        def run():
            vault = None
            last_report, count = 0, 0

            def check():
                if token.is_set():
                    raise Cancelled()

            def report():
                nonlocal last_report, count
                check()
                count += 1
                now = monotonic()
                if now - last_report >= .1:
                    last_report = now
                    deliver(progress, count)

            try:
                check()
                deliver(progress, 0)
                vault = Vault(root, options=options, scan=False, verify_index=snapshot is None,
                              resolve_root=snapshot is None, cancelled=token.is_set)
                vault.background = True
                vault.check_work = check
                vault.report_work = report
                vault.index.connection.set_progress_handler(lambda: int(token.is_set()), 1000)
                if snapshot is not None:
                    snapshot.apply(vault)
                vault.index.connection.execute('BEGIN')
                value = operation(vault)
                check()
                deliver(done, value, final=True)
            except Exception as error:
                if not token.is_set() and not isinstance(error, Cancelled):
                    deliver(failed, error, final=True)
            finally:
                if vault is not None:
                    vault.close()

        self.executor.submit(run)
        return token

    def close(self):
        self.closed = True
        self.cancel_all()
        self.executor.shutdown(wait=False, cancel_futures=True)
