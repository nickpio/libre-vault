"""Collapsible global tag tree and vault-wide tag renaming."""
from gi.repository import Gtk, Pango
from .vault import VaultError


class TagPane(Gtk.Expander):
    def __init__(self, owner):
        super().__init__(label='Tags')
        self.owner = owner
        self.signature = None
        self.counts = None
        self.store = Gtk.TreeStore(str, str, int)
        self.tree = Gtk.TreeView(model=self.store, headers_visible=False)
        tag_cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        tag_column = Gtk.TreeViewColumn('Tag', tag_cell, text=0)
        tag_column.set_expand(True)
        tag_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        tag_column.set_fixed_width(96)
        tag_column.set_min_width(48)
        count_column = Gtk.TreeViewColumn('Notes', Gtk.CellRendererText(), text=2)
        count_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        count_column.set_fixed_width(28)
        self.tree.append_column(tag_column)
        self.tree.append_column(count_column)
        self.tree.connect('row-activated', self.activate)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(220)
        scroll.set_propagate_natural_height(True)
        scroll.add(self.tree)
        box.pack_start(scroll, True, True, 0)
        button = Gtk.Button(label='Rename selected tag…')
        button.connect('clicked', self.rename)
        box.pack_start(button, False, False, 0)
        self.add(box)

    def refresh(self):
        vault = self.owner.vault
        signature = (id(vault), vault.index.connection.total_changes) if vault else None
        if signature == self.signature:
            return
        self.signature = signature
        counts = vault.tag_counts() if vault else {}
        if counts == self.counts:
            return
        self.counts = counts
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
