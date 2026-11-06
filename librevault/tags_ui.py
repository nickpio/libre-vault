"""Docked global tag tree and vault-wide tag renaming."""
from gi.repository import Gtk, Pango
from .vault import VaultError


class TagPane(Gtk.Box):
    def __init__(self, owner):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_border_width(12)
        self.get_style_context().add_class('tag-pane')
        self.owner = owner
        self.signature = None
        self.counts = None
        self.store = Gtk.TreeStore(str, str, int)
        self.tree = Gtk.TreeView(model=self.store, headers_visible=False)
        self.tree.get_accessible().set_name('Vault tags')
        self.tree.set_tooltip_column(1)
        tag_cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, xpad=6, ypad=7)
        tag_column = Gtk.TreeViewColumn('Tag', tag_cell, text=0)
        tag_column.set_expand(True)
        tag_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        tag_column.set_fixed_width(96)
        tag_column.set_min_width(48)
        count_cell = Gtk.CellRendererText(xalign=1, xpad=6, ypad=7)
        count_column = Gtk.TreeViewColumn('Notes', count_cell, text=2)
        count_column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        count_column.set_min_width(30)
        self.tree.append_column(tag_column)
        self.tree.append_column(count_column)
        self.tree.connect('row-activated', self.activate)
        self.content = Gtk.Stack()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree)
        self.content.add_named(scroll, 'tags')
        empty = Gtk.Label(label='No tags yet', xalign=0.5)
        empty.get_style_context().add_class('muted')
        empty.set_line_wrap(True)
        self.content.add_named(empty, 'empty')
        self.content.set_visible_child_name('empty')
        self.pack_start(self.content, True, True, 0)
        self.rename_button = Gtk.Button(label='Rename tag…')
        self.rename_button.set_tooltip_text('Rename the selected tag across the vault')
        self.rename_button.set_sensitive(False)
        self.rename_button.connect('clicked', self.rename)
        self.tree.get_selection().connect('changed', self.selection_changed)
        self.pack_start(self.rename_button, False, False, 0)

    def selection_changed(self, selection):
        self.rename_button.set_sensitive(selection.get_selected()[1] is not None)

    def refresh(self):
        vault = self.owner.vault
        signature = (id(vault), vault.index.generation) if vault else None
        if signature == self.signature:
            return
        self.signature = signature
        if vault:
            self.owner.vault_job('tags', lambda worker: worker.tag_counts(), self._apply_counts)
        else:
            self.owner.worker.cancel('tags')
            self._apply_counts({})

    def _apply_counts(self, counts):
        if counts == self.counts:
            return
        self.counts = counts
        self.content.set_visible_child_name('tags' if counts else 'empty')
        expanded = []
        self.tree.map_expanded_rows(lambda view, path: expanded.append(self.store[path][1]))
        self.store.clear()
        parents = {}
        for tag in sorted(counts):
            parent = tag.rpartition('/')[0]
            parents[tag] = self.store.append(parents.get(parent), [tag.rsplit('/', 1)[-1], tag, counts[tag]])
        for tag in expanded:
            if tag in parents:
                self.tree.expand_row(self.store.get_path(parents[tag]), False)

    def activate(self, _, path, column):
        self.owner.search_entry.set_text('tag:#' + self.store[path][1])

    def rename(self, *_):
        model, selected = self.tree.get_selection().get_selected()
        if selected is None:
            return
        old = model[selected][1]
        dialog = Gtk.Dialog(title='Rename tag across vault', transient_for=self.owner, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Rename', Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=old, activates_default=True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(16)
        content.pack_start(Gtk.Label(label='Rename #' + old + ' and its nested tags in all notes.'), False, False, 4)
        content.pack_start(entry, False, False, 4)
        problem = Gtk.Label(xalign=0)
        content.pack_start(problem, False, False, 4)
        dialog.show_all()
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                # Save open buffers first; existing conflict handling leaves
                # unsaved work intact and aborts the rename.
                for pane in self.owner.panes.values():
                    pane.save()
                    if pane.text().replace('\r\n', '\n') != self.owner.vault.read(pane.note.path).text.replace('\r\n', '\n'):
                        raise ValueError('Save open notes before renaming tags.')
                changed = self.owner.vault.rename_tag(old, entry.get_text().strip())
                self.owner.refresh()
                status = f'Renamed tag in {len(changed)} notes'
                if self.owner.vault.organization_notice:
                    status += ' · ' + self.owner.vault.organization_notice
                self.owner.status.set_text(status)
                break
            except (ValueError, VaultError, OSError) as error:
                message = str(error)
                if self.owner.vault.organization_notice:
                    message += ' · ' + self.owner.vault.organization_notice
                problem.set_text(message)
                self.owner.refresh()
        dialog.destroy()
