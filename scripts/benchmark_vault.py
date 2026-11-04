"""Reproducible large-vault latency budgets; run via ./check.sh --script.

Generated vaults/indexes are temporary. No thresholds depend on disk warmup.
JSON output includes hardware context, operation times and UI heartbeat delay.
"""
import argparse
import gc
import threading
import json
import os
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import time

from gi.repository import GLib, Gtk, Gio
from librevault.vault_options import VaultOptions
from librevault.worker import Snapshot, VaultWorker


BUDGETS = {'submit_ms': 50, 'heartbeat_p99_ms': 50, 'heartbeat_max_ms': 250,
           'cancel_ms': 250}


def benchmark(count, app):
    with TemporaryDirectory(prefix='librevault-bench-') as directory:
        root = Path(directory) / 'vault'
        root.mkdir()
        # Nested folders, YAML, aliases, tags, Unicode, prose, linked and plain mentions.
        for i in range(count):
            folder = root / f'folder-{i // 1000:03}'
            folder.mkdir(exist_ok=True)
            text = (f'---\ntags: [project/topic{i % 20}]\naliases: [Alias {i}]\nrank: {i}\n---\n'
                    f'# Note {i}\n\n[[Note {(i + 1) % count:06}]]\n\n'
                    + ('Representative prose café research findings. ' * (16 + i % 48))
                    + ('\nNote 000000 appears here. needle-rare\n' if i % 100 == 1 else '\n'))
            (folder / f'Note {i:06}.md').write_text(text)
        worker = VaultWorker(GLib.idle_add)
        context = GLib.MainContext.default()
        gaps = []
        phase = 'idle'
        stalls = []
        gc_started = {}
        gc_pauses = []
        def gc_timing(event, info):
            key = threading.get_ident()
            if event == 'start':
                gc_started[key] = time.monotonic()
            elif key in gc_started:
                gc_pauses.append((time.monotonic() - gc_started.pop(key)) * 1000)
        gc.callbacks.append(gc_timing)
        previous = time.monotonic()
        def tick():
            nonlocal previous
            now = time.monotonic()
            interval = (now - previous) * 1000
            gaps.append(interval)
            if interval > 100:
                stalls.append((phase, round(interval, 2)))
            previous = now
            return True
        source = GLib.timeout_add(10, tick)
        results = {'notes': count, 'approx_text_mib': round(sum(p.stat().st_size for p in root.rglob('*.md')) / 2**20, 2)}
        snapshot = None
        options = VaultOptions()
        submissions = []

        def run(name, operation, budget):
            nonlocal phase
            phase = name
            values, errors = [], []
            started = time.monotonic()
            worker.submit(name, root, options, operation, values.append, errors.append,
                          lambda _: None, snapshot=snapshot)
            submissions.append((time.monotonic() - started) * 1000)
            while not values and not errors:
                context.iteration(True)
                if time.monotonic() - started > budget:
                    raise AssertionError(f'{name} exceeded {budget}s')
            if errors:
                raise errors[0]
            results[name + '_s'] = round(time.monotonic() - started, 3)
            assert results[name + '_s'] <= budget, (name, results)
            return values[0]

        try:
            def scan(vault):
                vault.refresh()
                return Snapshot.of(vault)
            snapshot = run('cold_open', scan, count * .008 + 10)
            assert len(snapshot.notes) == count
            snapshot = run('warm_refresh', scan, count * .0005 + 5)
            run('search_rare', lambda v: v.search_results('content:needle-rare'), count * .0001 + 1)
            run('search_broad', lambda v: v.search_results('content:research'), count * .0003 + 2)
            run('backlinks', lambda v: v.backlinks('folder-000/Note 000000.md'), count * .0002 + 2)
            run('mentions', lambda v: v.unlinked_mentions('folder-000/Note 000000.md'), count * .0003 + 2)
            # Measure cancel-to-next-result: proves the obsolete scan releases the worker.
            worker.submit('cancelled', root, options, lambda v: v.refresh(), lambda _: None,
                          lambda e: None, lambda _: None, snapshot=snapshot)
            # Let the job start before cancelling; keep the main loop alive.
            start = time.monotonic()
            while time.monotonic() - start < .02:
                while context.pending():
                    context.iteration(False)
                time.sleep(.001)
            start = time.monotonic()
            worker.cancel('cancelled')
            run('after_cancel', lambda v: True, 1)
            results['cancel_ms'] = round((time.monotonic() - start) * 1000, 2)
            results['submit_ms'] = round(max(submissions), 2)
            ordered = sorted(gaps)
            results['heartbeat_p99_ms'] = round(ordered[int((len(ordered) - 1) * .99)], 2)
            results['heartbeat_max_ms'] = round(max(gaps), 2)
            # Exercise the actual window too, including opening, monitor setup,
            # the initial file page, a broad search and GTK layout/frame work.
            from librevault.app import Window
            window = Window(app, preferences_path=Path(directory) / 'settings.json')
            gaps.clear()
            previous = time.monotonic()
            start = previous
            try:
                window.load_vault(root)
                for query in ('', 'content:research'):
                    phase = 'window_search' if query else 'window_open'
                    if query:
                        window.search_entry.set_text(query)
                    deadline = time.monotonic() + count * .0008 + 10
                    ready_since = None
                    while True:
                        context.iteration(True)
                        monitor = window.vault_monitor
                        ready = (window.vault and not window.worker.requests
                                 and window.file_render_source is None and monitor and monitor.source is None)
                        if ready:
                            ready_since = ready_since or time.monotonic()
                            if time.monotonic() - ready_since > .05:
                                break
                        else:
                            ready_since = None
                        assert time.monotonic() < deadline, 'Window did not settle'
                results['window_open_and_search_s'] = round(time.monotonic() - start, 3)
                results['window_rows'] = len(window.files.get_children())
                ordered = sorted(gaps)
                results['window_heartbeat_p99_ms'] = round(ordered[int((len(ordered) - 1) * .99)], 2)
                results['window_heartbeat_max_ms'] = round(max(gaps), 2)
                results['stalls_over_100ms'] = stalls
                results['max_gc_pause_ms'] = round(max(gc_pauses, default=0), 2)
                assert results['window_rows'] == 200
                assert results['window_heartbeat_p99_ms'] <= BUDGETS['heartbeat_p99_ms'], results
                assert results['window_heartbeat_max_ms'] <= BUDGETS['heartbeat_max_ms'], results
            finally:
                window.destroy()
                window.worker.executor.shutdown(wait=True)
            for key, budget in BUDGETS.items():
                assert results[key] <= budget, (key, budget, results)
            return results
        finally:
            gc.callbacks.remove(gc_timing)
            GLib.source_remove(source)
            worker.close()
            worker.executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notes', nargs='+', type=int, default=[10000, 50000])
    args = parser.parse_args()
    print(json.dumps({'platform': platform.platform(), 'python': platform.python_version(),
                      'cpu': platform.processor(), 'budgets_ms': BUDGETS}), flush=True)
    app = Gtk.Application(application_id='org.librevault.LatencyBenchmark', flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    with TemporaryDirectory(prefix='librevault-bench-cache-') as cache:
        os.environ['XDG_CACHE_HOME'] = cache
        for count in args.notes:
            print(json.dumps(benchmark(count, app)), flush=True)


if __name__ == '__main__':
    main()
