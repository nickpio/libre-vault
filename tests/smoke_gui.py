"""Exercise the real GTK window against temporary files; optionally capture it."""
import argparse
from pathlib import Path
import shutil
import tempfile
import time

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk
from librevault.app import Window
from librevault.vault import Conflict


def drain():
    for _ in range(15):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)


def smoke(screenshot):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / 'Example vault'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'example-vault', root)
        app = Gtk.Application(application_id='org.librevault.SmokeTest')
        app.register(None)
        window = Window(app, root)
        window.resize(1440, 900)
        drain()
        assert window.current().note.path == 'Welcome.md'
        preview = window.current().preview.get_buffer()
        assert 'Your notes, on your disk' in preview.get_text(preview.get_start_iter(), preview.get_end_iter(), True)
        window.follow_link('Welcome.md', 'Projects/Field notes#What matters')
        drain()
        assert window.current().note.path == 'Projects/Field notes.md'
        assert window.tabs.get_n_pages() == 2
        pane = window.current()
        pane.buffer.insert(pane.buffer.get_end_iter(), '\nGUI saved this line.\n')
        assert pane.dirty
        window.save_current()
        assert not pane.dirty
        path = root / 'Projects/Field notes.md'
        assert 'GUI saved this line.' in path.read_text()
        path.write_text(path.read_text() + '\nExternal clean edit.\n')
        window.refresh()
        assert 'External clean edit.' in pane.text()
        assert not pane.dirty
        pane.buffer.insert(pane.buffer.get_end_iter(), '\nLocal unsaved edit.\n')
        path.write_text(path.read_text() + '\nConcurrent external edit.\n')
        window.refresh()
        assert 'Local unsaved edit.' in pane.text()
        try:
            pane.save()
        except Conflict:
            pass
        else:
            raise AssertionError('Conflicting save should stop')
        assert 'Local unsaved edit.' not in path.read_text()
        assert pane.dirty
        # Restore the clean file for the screenshot without modifying the example vault.
        pane.note = window.vault.read(pane.note.path)
        pane.buffer.set_text(pane.note.text)
        pane.dirty = False
        pane.tab_label.set_text('Field notes')
        window.search_entry.set_text('observations')
        window.populate_files()
        assert [row.path for row in window.files.get_children()] == ['Projects/Field notes.md']
        window.search_entry.set_text('')
        window.populate_files()
        window.open_note('Welcome.md')
        window.status.set_text('Example vault · Local files · Ctrl+S to save')
        drain()
        if screenshot:
            allocation = window.get_allocation()
            pixels = Gdk.pixbuf_get_from_window(window.get_window(), 0, 0, allocation.width, allocation.height)
            pixels.savev(str(Path(screenshot).resolve()), 'png', [], [])
        window.destroy()
        drain()
    print('GTK smoke passed: open, preview, tabs, follow link, edit, save, disk verification, external reload, conflict, search.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshot')
    smoke(parser.parse_args().screenshot)
