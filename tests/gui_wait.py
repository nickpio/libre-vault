"""Wait for background results while allowing GTK to process events."""
import time


def wait_for_work(window, timeout=10):
    from gi.repository import GLib
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    ready_since = None
    while time.monotonic() < deadline:
        for _ in range(100):
            if not context.pending():
                break
            context.iteration(False)
        monitor = window.vault_monitor
        if (not window.worker.requests and not window.loading_vault
                and window.file_render_source is None
                and (monitor is None or monitor.source is None)):
            if ready_since is None:
                ready_since = time.monotonic()
            if time.monotonic() - ready_since >= .03:
                return
        else:
            ready_since = None
        time.sleep(.002)
    raise AssertionError('Background vault work did not finish')
