"""Exercise GTK against a temporary copy of the supplied example vault.

Run from the repository root with PYTHONPATH=., optionally under xvfb-run.
"""
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import gi

gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk

from librevault.app import Window
from librevault.vault import Conflict


def drain_events():
    context = GLib.MainContext.default()
    # Formatting and geometry can schedule more idle callbacks. Bound the loop
    # so a broken callback fails the smoke instead of hanging indefinitely.
    for _ in range(1000):
        if not context.pending():
            return
        context.iteration(False)
    raise AssertionError('GTK event queue did not settle')


def main():
    if not Gtk.init_check()[0]:
        raise SystemExit('GTK needs a display. Run this script under xvfb-run.')
    app = Gtk.Application(application_id='org.librevault.Smoke')
    app.register(None)
    with TemporaryDirectory(prefix='librevault-smoke-') as folder:
        root = Path(folder) / 'vault'
        example = Path(__file__).resolve().parents[1] / 'example-vault'
        shutil.copytree(example, root)
        window = Window(app, preferences_path=Path(folder) / 'settings.json')
        try:
            assert window.vault is None
            assert window.workspace_stack.get_visible_child_name() == 'welcome'
            window.load_vault(root)
            drain_events()
            assert window.current().note.path == 'Welcome.md'
            window.follow_link('Welcome.md', 'Projects/Field notes')
            drain_events()
            pane = window.current()
            assert pane.note.path == 'Projects/Field notes.md'
            original = pane.text()
            pane.buffer.insert(pane.buffer.get_end_iter(), '\nSmoke saved text\n')
            drain_events()
            assert pane.dirty
            pane.undo()
            assert pane.text() == original
            assert not pane.dirty
            pane.redo()
            assert pane.text() == original + '\nSmoke saved text\n'
            pane.save()
            path = root / pane.note.path
            assert path.read_text(encoding='utf-8') == original + '\nSmoke saved text\n'
            assert not pane.dirty

            # A clean editor must reload an external edit.
            path.write_text('# External revision\n', encoding='utf-8')
            window.refresh()
            drain_events()
            assert pane.text() == '# External revision\n'
            assert not pane.dirty
            assert not pane.history.can_undo

            # Find/replace updates original Markdown and undoes as one action.
            pane.show_replace()
            pane.findbar.query.set_text('External')
            pane.findbar.replacement.set_text('Revised')
            pane.findbar.replace_all()
            assert pane.text() == '# Revised revision\n'
            pane.undo()
            assert pane.text() == '# External revision\n'
            assert not pane.dirty
            pane.findbar.close()

            # A dirty editor must retain its buffer and refuse a conflicting save.
            pane.buffer.insert(pane.buffer.get_end_iter(), 'Local unsaved text\n')
            local = pane.text()
            path.write_text('# Competing external revision\n', encoding='utf-8')
            window.refresh()
            drain_events()
            assert pane.text() == local
            assert pane.dirty
            try:
                pane.save()
            except Conflict:
                pass
            else:
                raise AssertionError('Conflicting save unexpectedly succeeded')
            assert path.read_text(encoding='utf-8') == '# Competing external revision\n'
            assert pane.text() == local

            window.search_entry.set_text('competing external')
            window.populate_files()
            drain_events()
            assert [row.path for row in window.files.get_children()] == ['Projects/Field notes.md']
            window.search_entry.set_text('does-not-exist')
            window.populate_files()
            assert window.files.get_children() == []
            print('GTK smoke passed: link, edit, undo/redo, find/replace, save, external reload, conflict, search')
        finally:
            # Destroy directly so teardown cannot open a dirty-buffer dialog.
            window.destroy()
            drain_events()


if __name__ == '__main__':
    main()
