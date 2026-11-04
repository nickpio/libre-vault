"""Opening, releasing, and closing a vault and its per-vault stores."""
from gi.repository import GLib, Gtk

from .monitor import VaultMonitor
from .navigation import NavigationHistory
from .recovery import RecoveryStore, VersionHistoryStore
from .vault import Vault, VaultError


class VaultLifecycleMixin:
    def choose_vault(self, *_):
        dialog = Gtk.FileChooserDialog(title='Open a Markdown vault', transient_for=self,
                                      action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Open', Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            self.try_action(lambda: self.load_vault(dialog.get_filename()))
        dialog.destroy()

    def editors(self):
        return [*self.panes.values(), *self.canvas_views]

    def confirm_panes(self, panes):
        self.confirming_panes = True
        try:
            return self._confirm_panes(panes)
        finally:
            self.confirming_panes = False

    def _confirm_panes(self, panes):
        discarded = []
        for pane in panes:
            if not pane.dirty:
                continue
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       text=f'Save changes to {pane.note.path}?')
            dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Discard', Gtk.ResponseType.NO, 'Save', Gtk.ResponseType.YES)
            result = dialog.run()
            dialog.destroy()
            if result == Gtk.ResponseType.YES:
                try:
                    pane.save()
                except (VaultError, OSError) as error:
                    self.error(error)
                    return False
            elif result == Gtk.ResponseType.NO:
                discarded.append(pane)
            else:
                return False
        for pane in discarded:
            try:
                self.recovery.remove(pane.note.path)
            except OSError as error:
                self.error(error)
                return False
        return True

    def load_vault(self, folder):
        vault = Vault(folder, options=self.vault_options_store.load(folder))
        if not self.confirm_panes(self.editors()):
            vault.close()
            return
        self.persist_workspace()
        self.release_vault()
        self.vault = vault
        self.bookmark_items = self.bookmark_store.load(vault.root)
        self.navigation_history = NavigationHistory()
        self.recovery = RecoveryStore(vault.root)
        self.version_history = VersionHistoryStore(vault.root)
        vault.version_history = self.version_history
        self.update_workspace_state()
        self.collapsed_folders.clear()
        self.vault_label.set_text(vault.root.name)
        self.set_title(f'{vault.root.name} · Libre Vault')
        self.search_entry.set_text('')
        self.populate_files()
        self.status.set_text(f'{len(vault.notes)} notes · {vault.root}')
        self.offer_recovery()
        restored = self.restore_workspace()
        if vault.notes and not self.panes and not restored:
            first = 'Welcome.md' if 'Welcome.md' in vault.notes else next(iter(vault.notes))
            self.open_note(first)
        self.update_context()
        self.refresh_bookmark_list()
        self.vault_monitor = VaultMonitor(vault, self.refresh, self.status.set_text)
        try:
            self.last_vault_store.save(vault.root)
        except OSError as error:
            self.status.set_text(f'Could not remember the opened vault: {error}')

    def clear_editors(self):
        for window in tuple(self.note_windows):
            window.destroy()
        self.set_tab_mode('tabs')
        self.close_split()
        for source in self.scroll_restore_sources.values():
            GLib.source_remove(source)
        self.scroll_restore_sources.clear()
        while self.tabs.get_n_pages():
            pane = self.tabs.get_nth_page(0)
            self.tabs.remove_page(0)
            pane.destroy()
        self.panes.clear()

    def release_vault(self):
        for window in (self.version_dialog, self.graph_window, self.slides_window,
                       self.converter_dialog, self.bookmark_manager, self.workspace_manager):
            if window is not None:
                window.destroy()
        if self.vault_monitor:
            self.vault_monitor.close()
            self.vault_monitor = None
        if self.reading_source is not None:
            GLib.source_remove(self.reading_source)
            self.reading_source = None
        for overlay in (self.palette, self.quick_switcher):
            if overlay is not None:
                overlay.destroy()
        self.clear_editors()
        self.version_history = None
        if self.recovery:
            self.recovery.close()
            self.recovery = None
        if self.vault:
            self.vault.close()
        self.bookmark_items = []
        self.refresh_bookmark_list()

    def close_vault(self, *_):
        if self.vault is None:
            return True
        if not self.confirm_panes(self.editors()):
            return False
        try:
            self.last_vault_store.save(None)
        except OSError as error:
            self.error(error)
            return False
        self.persist_workspace()
        self.release_vault()
        self.vault = None
        self.navigation_history = NavigationHistory()
        self.collapsed_folders.clear()
        self.vault_label.set_text('No vault open')
        self.set_title('Libre Vault')
        self.search_entry.set_text('')
        self.populate_files()
        self.update_workspace_state()
        self.update_context()
        self.status.set_text('Open a vault folder to begin.')
        self.open_vault_button.grab_focus()
        return True

    def offer_recovery(self):
        for draft, note, text in self.recovery.drafts():
            if note.path in self.panes:
                continue
            try:
                self.vault._path(note.path)
            except VaultError:
                continue
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       text=f'Recover unsaved changes to {note.path}?')
            dialog.format_secondary_text('The draft stays available if you choose Later. If the file changed, saving will stop to protect both versions.')
            dialog.add_buttons('Later', Gtk.ResponseType.CANCEL, 'Discard draft', Gtk.ResponseType.NO, 'Recover', Gtk.ResponseType.YES)
            result = dialog.run()
            dialog.destroy()
            if result == Gtk.ResponseType.NO:
                try:
                    draft.unlink()
                except OSError as error:
                    self.error(error)
            elif result == Gtk.ResponseType.YES:
                self.open_note(note.path, recovered=note)
                pane = self.panes.get(note.path)
                if pane:
                    pane.buffer.set_text(text)
                    # Only remove the old copy after the new recovery draft is durable.
                    try:
                        if pane.dirty:
                            self.recovery.write(note, text)
                        draft.unlink()
                    except OSError as error:
                        self.error(error)

