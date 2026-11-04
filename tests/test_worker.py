"""Thread ownership, cooperative cancellation, and queued-result invalidation."""
from pathlib import Path
from queue import Queue, Empty
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import time
import unittest

from librevault.vault import Vault
from librevault.worker import Snapshot, VaultWorker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'A.md').write_text('[[B]] B')
        (self.root / 'B.md').write_text('# B')
        self.vault = Vault(self.root)
        self.addCleanup(self.vault.close)
        self.callbacks = Queue()
        self.worker = VaultWorker(self.callbacks.put)
        self.addCleanup(self.worker.close)
        self.values, self.errors, self.progress = [], [], []

    def submit(self, operation, channel='test'):
        return self.worker.submit(channel, self.root, self.vault.options, operation,
                                  self.values.append, self.errors.append, self.progress.append,
                                  snapshot=Snapshot.of(self.vault))

    def drain_until(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            try:
                self.callbacks.get(timeout=.01)()
            except Empty:
                pass
        self.assertTrue(condition(), 'Worker timed out')

    def test_connection_owned_and_closed_on_worker_thread(self):
        caller = get_ident()
        def query(vault):
            self.assertIsNot(vault.index.connection, self.vault.index.connection)
            return get_ident(), vault.search('B'), vault.backlinks('B.md'), vault.unlinked_mentions('B.md')
        self.submit(query)
        self.drain_until(lambda: bool(self.values or self.errors))
        self.assertFalse(self.errors)
        thread, search, backlinks, mentions = self.values[0]
        self.assertNotEqual(thread, caller)
        self.assertEqual(search, ['A.md', 'B.md'])
        self.assertEqual(backlinks, ['A.md'])
        self.assertEqual([m.source_path for m in mentions], ['A.md'])
        self.assertTrue(self.progress)
        self.assertEqual(self.vault.index.connection.execute('SELECT count(*) FROM notes').fetchone()[0], 2)

    def test_superseded_running_job_stops_and_cannot_deliver(self):
        started, stopped = Event(), Event()
        def slow(vault):
            started.set()
            try:
                while True:
                    vault.check_work()
                    time.sleep(.001)
            finally:
                stopped.set()
        self.submit(slow)
        self.assertTrue(started.wait(2))
        self.submit(lambda vault: 'new')
        self.drain_until(lambda: bool(self.values))
        self.assertTrue(stopped.is_set())
        self.assertEqual(self.values, ['new'])
        self.assertFalse(self.errors)

    def test_already_queued_result_and_error_are_rejected_after_cancel(self):
        for operation in (lambda vault: 'stale', lambda vault: 1 / 0):
            finished = Event()
            def job(vault):
                try:
                    return operation(vault)
                finally:
                    finished.set()
            self.submit(job)
            self.assertTrue(finished.wait(2))
            self.worker.executor.submit(lambda: None).result(timeout=2)
            self.worker.cancel('test')
            while not self.callbacks.empty():
                self.callbacks.get()()
        self.assertFalse(self.values)
        self.assertFalse(self.errors)

    def test_close_cancels_without_waiting_and_suppresses_callbacks(self):
        started = Event()
        def slow(vault):
            started.set()
            while True:
                vault.check_work()
                time.sleep(.001)
        self.submit(slow)
        self.assertTrue(started.wait(2))
        start = time.monotonic()
        self.worker.close()
        self.assertLess(time.monotonic() - start, .1)
        while not self.callbacks.empty():
            self.callbacks.get()()
        self.assertFalse(self.values)
        self.assertFalse(self.errors)


try:
    from librevault.app import GLib, Gtk, Window
    from gi.repository import Gio
    GTK_AVAILABLE = Gtk.init_check()[0]
except (ImportError, ValueError):
    GTK_AVAILABLE = False


@unittest.skipUnless(GTK_AVAILABLE, 'GTK display unavailable')
class AsyncWindowTests(unittest.TestCase):
    def setUp(self):
        from tests.gui_wait import wait_for_work
        self.wait = wait_for_work
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'A.md').write_text('alpha')
        (self.root / 'B.md').write_text('beta')
        self.app = Gtk.Application.get_default()
        if self.app is None:
            self.app = Gtk.Application(application_id='org.librevault.AsyncTests',
                                       flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.app.register(None)
        self.window = Window(self.app, preferences_path=self.root / '.settings.json')
        self.addCleanup(self.window.destroy)

    def test_cancel_pending_open_keeps_welcome_and_ui_responsive(self):
        from unittest.mock import patch
        started, release = Event(), Event()
        read = Vault.read
        def slow(vault, path):
            started.set()
            release.wait(3)
            return read(vault, path)
        with patch.object(Vault, 'read', slow):
            start = time.monotonic()
            self.window.load_vault(self.root)
            self.assertLess(time.monotonic() - start, .1)
            self.assertTrue(started.wait(2))
            self.assertIsNone(self.window.vault)
            self.assertTrue(self.window.work_cancel.get_visible())
            self.window.close_vault()
            release.set()
            self.window.worker.executor.submit(lambda: None).result(timeout=3)
            self.wait(self.window)
        self.assertIsNone(self.window.vault)
        self.assertEqual(self.window.workspace_stack.get_visible_child_name(), 'welcome')

    def test_rapid_query_changes_never_display_old_results(self):
        from unittest.mock import patch
        self.window.load_vault(self.root)
        self.wait(self.window)
        started, release = Event(), Event()
        search = Vault.search_results
        def slow(vault, query):
            if query == 'alpha':
                started.set()
                release.wait(3)
            return search(vault, query)
        with patch.object(Vault, 'search_results', slow):
            self.window.search_entry.set_text('alpha')
            self.assertTrue(started.wait(2))
            self.window.search_entry.set_text('beta')
            release.set()
            self.wait(self.window)
        self.assertEqual([row.path for row in self.window.files.get_children()], ['B.md'])
        self.assertEqual(self.window.files_heading.get_text(), 'Search results (1)')

    def test_switching_vaults_rejects_old_open_result(self):
        from unittest.mock import patch
        second = self.root / 'second'
        second.mkdir()
        (second / 'New.md').write_text('new')
        started, release = Event(), Event()
        refresh = Vault.refresh
        def slow(vault):
            if vault.root == self.root:
                started.set()
                release.wait(3)
            return refresh(vault)
        with patch.object(Vault, 'refresh', slow):
            self.window.load_vault(self.root)
            self.assertTrue(started.wait(2))
            self.window.load_vault(second)
            release.set()
            self.wait(self.window)
        self.assertEqual(self.window.vault.root, second)
        self.assertEqual(self.window.current().note.path, 'New.md')

    def test_file_pages_keep_all_paths_available(self):
        for i in range(205):
            (self.root / f'Note {i:03}.md').write_text('note')
        self.window.load_vault(self.root)
        self.wait(self.window)
        self.assertEqual(len(self.window.files.get_children()), 200)
        self.assertTrue(self.window.more_files.get_visible())
        self.window.more_files.clicked()
        self.wait(self.window)
        self.assertEqual(len(self.window.files.get_children()), 207)
        self.assertFalse(self.window.more_files.get_visible())

    def test_foreground_save_invalidates_an_inflight_refresh_snapshot(self):
        from unittest.mock import patch
        self.window.load_vault(self.root)
        self.wait(self.window)
        started, release = Event(), Event()
        update = Vault.update_paths
        first = True
        def slow(vault, paths):
            nonlocal first
            update(vault, paths)
            if first:
                first = False
                started.set()
                release.wait(3)
        with patch.object(Vault, 'update_paths', slow):
            self.window.refresh({'A.md'})
            self.assertTrue(started.wait(2))
            pane = self.window.open_note('A.md')
            pane.buffer.set_text('saved during refresh')
            pane.save()
            release.set()
            self.wait(self.window)
        self.assertEqual(pane.text(), 'saved during refresh')
        self.assertEqual(self.window.vault.notes['A.md'].revision, pane.note.revision)
        self.assertEqual(self.window.vault.indexed_note('A.md').text, pane.text())
