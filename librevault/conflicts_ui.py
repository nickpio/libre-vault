"""Compare a note's saved baseline, editor buffer, and current disk contents."""
from pathlib import Path

from gi.repository import Gtk

from .vault import Conflict, VaultError
from .widgets import scrolled, text_buffer


class ConflictDialog(Gtk.Dialog):
    USE_DISK, REPLACE_DISK, SAVE_COPY = 1, 2, 3

    def __init__(self, pane):
        super().__init__(title=f'Resolve conflict: {pane.note.path}',
                         transient_for=pane.window, modal=True)
        self.pane = pane
        self.editor_text = pane.text()
        self.disk = None
        self.set_default_size(1000, 600)
        self.add_buttons('Keep editing', Gtk.ResponseType.CANCEL,
                         'Use disk version', self.USE_DISK,
                         'Replace disk with editor', self.REPLACE_DISK,
                         'Save as another note', self.SAVE_COPY)
        self.set_default_response(Gtk.ResponseType.CANCEL)
        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(10)
        explanation = Gtk.Label(xalign=0, wrap=True, label=(
            'Compare the versions below. Use disk version discards your editor edits. '
            'Replace disk with editor overwrites the disk version shown here. '
            'Saving another note keeps this editor open with its edits.'))
        content.pack_start(explanation, False, False, 0)
        try:
            self.disk = pane.window.vault.read(pane.note.path)
            disk_text = self.disk.text
        except (VaultError, OSError) as error:
            disk_text = f'Disk version unavailable: {error}'
        columns = Gtk.Box(spacing=10, homogeneous=True)
        self.views = []
        for title, text in (('Original', pane.note.text), ('Editor', self.editor_text),
                            ('Disk', disk_text)):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            column.pack_start(Gtk.Label(label=title, xalign=0), False, False, 0)
            view = Gtk.TextView(buffer=text_buffer(text.replace('\r\n', '\n')))
            view.set_editable(False)
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.get_accessible().set_name(title + ' version')
            self.views.append(view)
            column.pack_start(scrolled(view), True, True, 0)
            columns.pack_start(column, True, True, 0)
        content.pack_start(columns, True, True, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label='New note path', xalign=0), False, False, 0)
        path = Path(pane.note.path)
        self.copy_path = Gtk.Entry(text=str(path.with_name(path.stem + ' (conflict copy).md')))
        row.pack_start(self.copy_path, True, True, 0)
        content.pack_start(row, False, False, 0)
        self.message = Gtk.Label(xalign=0, wrap=True)
        content.pack_start(self.message, False, False, 0)
        for response in (self.USE_DISK, self.REPLACE_DISK):
            self.set_response_sensitive(response, self.disk is not None)

    def resolve(self, response):
        """Apply only the displayed versions; failures leave the editor intact."""
        pane = self.pane
        vault = pane.window.vault
        if pane.text() != self.editor_text:
            raise Conflict('The editor changed. Close this comparison and open it again.')
        if response == self.SAVE_COPY:
            note = vault.create(self.copy_path.get_text())
            vault.save(note, self.editor_text)
            pane.window.populate_files()
            pane.window.open_note(note.path)
            return True
        if response not in (self.USE_DISK, self.REPLACE_DISK) or self.disk is None:
            return False
        if response == self.REPLACE_DISK:
            note = vault.save(self.disk, self.editor_text)
        else:
            note = vault.read(self.disk.path)
            if note.revision != self.disk.revision:
                raise Conflict('The disk version changed again. Close this comparison and open it again.')
        pane.load_external(note)
        try:
            pane.window.recovery.remove(note.path)
        except OSError as error:
            pane.window.status.set_text(f'Resolved, but could not remove recovery draft: {error}')
        pane.window.update_context()
        return True


def compare_conflict(pane):
    if pane.resolving_conflict:
        return
    pane.resolving_conflict = True
    dialog = None
    try:
        dialog = ConflictDialog(pane)
        dialog.show_all()
        while True:
            response = dialog.run()
            if response not in (dialog.USE_DISK, dialog.REPLACE_DISK, dialog.SAVE_COPY):
                break
            try:
                if dialog.resolve(response):
                    break
            except (VaultError, OSError) as error:
                dialog.message.set_text(str(error))
    finally:
        if dialog is not None:
            dialog.destroy()
        pane.resolving_conflict = False
