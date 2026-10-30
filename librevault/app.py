"""Native Linux notebook prototype. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

from .vault import Vault, VaultError, parse, links


CSS = b'''
window { background: #19191e; color: #dedee5; }
headerbar { background: #23232b; border-bottom: 1px solid #363641; }
button { background: #30303a; color: #e5e5ed; border-color: #444451; box-shadow: none; }
button:hover { background: #42424e; }
entry { background: #202027; color: #eeeef4; border-color: #41414d; }
.sidebar { background: #202027; }
list, list row { background: #202027; color: #d2d2dc; }
list row:selected { background: #41375b; color: #f3eefe; }
textview, textview text { background: #19191e; color: #dddde5; }
notebook header { background: #202027; }
notebook tab { color: #aaaab7; padding: 8px 14px; }
notebook tab:checked { color: #d7bdff; border-bottom: 2px solid #ad86ed; }
.status { background: #23232b; padding: 7px 12px; color: #b7b1c7; }
.caption { color: #b9a3df; font-weight: bold; padding: 12px 8px 6px; }
'''


def scrolled(widget, width=None):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add(widget)
    if width:
        scroll.set_size_request(width, -1)
    return scroll


def text_buffer(text):
    buffer = Gtk.TextBuffer()
    buffer.set_text(text)
    return buffer


class NotePane(Gtk.Paned):
    def __init__(self, window, note):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.window, self.note = window, note
        self.dirty = False
        self.buffer = text_buffer(note.text.replace('\r\n', '\n'))
        self.editor = Gtk.TextView(buffer=self.buffer)
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.set_left_margin(28)
        self.editor.set_right_margin(24)
        self.editor.set_top_margin(24)
        self.editor.set_bottom_margin(30)
        self.editor.set_monospace(True)
        self.preview = Gtk.TextView()
        self.preview.set_editable(False)
        self.preview.set_cursor_visible(False)
        self.preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.preview.set_left_margin(28)
        self.preview.set_right_margin(24)
        self.preview.set_top_margin(24)
        self.preview.set_bottom_margin(30)
        self.pack1(scrolled(self.editor), True, False)
        self.pack2(scrolled(self.preview), True, False)
        self.connect('size-allocate', self.balance_columns)
        self.last_width = 0
        self.tab_label = Gtk.Label(label=Path(note.path).stem)
        self.buffer.connect('changed', self.changed)
        self.render()

    def balance_columns(self, _, allocation):
        if allocation.width != self.last_width and allocation.width > 100:
            self.set_position(allocation.width // 2)
            self.last_width = allocation.width

    def text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def changed(self, *_):
        self.dirty = True
        self.tab_label.set_text('• ' + Path(self.note.path).stem)
        self.window.status.set_text('Unsaved changes · Ctrl+S to save')
        self.render()

    def render(self):
        buffer = Gtk.TextBuffer()
        tags = {
            'h1': buffer.create_tag(None, size_points=26, weight=Pango.Weight.BOLD, foreground='#eee6fc'),
            'h2': buffer.create_tag(None, size_points=21, weight=Pango.Weight.BOLD, foreground='#e2d4f8'),
            'h3': buffer.create_tag(None, size_points=17, weight=Pango.Weight.BOLD),
            'strong': buffer.create_tag(None, weight=Pango.Weight.BOLD),
            'em': buffer.create_tag(None, style=Pango.Style.ITALIC),
            'code': buffer.create_tag(None, family='monospace', background='#2a2933'),
            'quote': buffer.create_tag(None, foreground='#c4b2df', left_margin=12),
        }
        active = []
        list_level = 0

        def insert(text, extra=()):
            buffer.insert_with_tags(buffer.get_end_iter(), text, *(active + list(extra)))

        for token in parse(self.text()):
            kind = token.type
            if kind == 'heading_open':
                active.append(tags.get(token.tag, tags['h3']))
            elif kind == 'heading_close':
                active.pop()
                insert('\n\n')
            elif kind in ('bullet_list_open', 'ordered_list_open'):
                list_level += 1
            elif kind in ('bullet_list_close', 'ordered_list_close'):
                list_level -= 1
            elif kind == 'list_item_open':
                insert('  ' * max(0, list_level - 1) + '• ')
            elif kind == 'paragraph_close':
                insert('\n' if list_level else '\n\n')
            elif kind == 'blockquote_open':
                active.append(tags['quote'])
            elif kind == 'blockquote_close':
                active.pop()
            elif kind in ('fence', 'code_block'):
                insert(token.content + '\n', [tags['code']])
            elif kind == 'hr':
                insert('────────────────────\n\n')
            elif kind == 'inline':
                for child in token.children or []:
                    if child.type in ('strong_open', 'em_open'):
                        active.append(tags['strong' if child.type == 'strong_open' else 'em'])
                    elif child.type in ('strong_close', 'em_close'):
                        if active:
                            active.pop()
                    elif child.type == 'code_inline':
                        insert(child.content, [tags['code']])
                    elif child.type in ('softbreak', 'hardbreak'):
                        insert('\n')
                    elif child.type == 'wikilink':
                        # Embedded content is represented as a link in this prototype.
                        insert(('↳ ' if child.meta['embedded'] else '') + child.content)
                    elif child.type in ('text', 'image'):
                        insert(child.content)
        self.preview.set_buffer(buffer)

    def save(self):
        if self.dirty:
            self.note = self.window.vault.save(self.note, self.text())
            self.dirty = False
            self.tab_label.set_text(Path(self.note.path).stem)
        self.window.status.set_text(f'Saved · {self.note.path}')
        self.window.update_context()


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, folder=None):
        super().__init__(application=app, title='Libre Vault')
        self.set_default_size(1360, 850)
        self.vault = None
        self.panes = {}
        self.refresh_source = None
        self.connect('delete-event', self.on_delete)
        self.connect('key-press-event', self.key_press)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        header = Gtk.HeaderBar(title='Libre Vault', subtitle='Local Markdown notebooks')
        header.set_show_close_button(True)
        self.set_titlebar(header)
        for label, action in [('Open vault', self.choose_vault), ('New note', self.new_note)]:
            button = Gtk.Button(label=label)
            button.connect('clicked', action)
            header.pack_start(button)
        for label, action in [('Save', self.save_current), ('Close tab', self.close_tab)]:
            button = Gtk.Button(label=label)
            button.connect('clicked', action)
            header.pack_end(button)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        layout = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(layout, True, True, 0)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.get_style_context().add_class('sidebar')
        sidebar.set_border_width(10)
        self.vault_label = Gtk.Label(label='NO VAULT OPEN', xalign=0)
        self.vault_label.get_style_context().add_class('caption')
        sidebar.pack_start(self.vault_label, False, False, 0)
        self.search_entry = Gtk.SearchEntry(placeholder_text='Search notes and contents…')
        self.search_entry.connect('search-changed', lambda *_: self.populate_files())
        sidebar.pack_start(self.search_entry, False, False, 0)
        self.files = Gtk.ListBox()
        self.files.connect('row-activated', lambda _, row: self.open_note(row.path))
        sidebar.pack_start(scrolled(self.files, 235), True, True, 0)
        hint = Gtk.Label(label='Ctrl+O  Open vault\nCtrl+N  New note\nCtrl+S  Save\nCtrl+W  Close tab', xalign=0)
        hint.set_margin_top(12)
        sidebar.pack_start(hint, False, False, 0)
        layout.pack1(sidebar, False, False)
        center = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.tabs = Gtk.Notebook(scrollable=True)
        self.tabs.connect('switch-page', lambda *_: GLib.idle_add(self.update_context))
        center.pack1(self.tabs, True, False)
        self.context = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.context.set_border_width(12)
        self.context.set_size_request(235, -1)
        self.context.get_style_context().add_class('sidebar')
        center.pack2(scrolled(self.context), False, False)
        layout.pack2(center, True, False)
        layout.set_position(265)
        center.set_position(825)
        self.status = Gtk.Label(label='Open a vault folder to begin.', xalign=0)
        self.status.get_style_context().add_class('status')
        root.pack_end(self.status, False, False, 0)
        self.show_all()
        if folder:
            self.load_vault(folder)
        self.refresh_source = GLib.timeout_add_seconds(3, self.refresh)
        self.connect('destroy', self.cleanup)

    def cleanup(self, *_):
        if self.refresh_source:
            GLib.source_remove(self.refresh_source)
            self.refresh_source = None

    def error(self, error):
        self.status.set_text(str(error))
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.CLOSE, text=str(error))
        dialog.run()
        dialog.destroy()

    def try_action(self, action):
        try:
            return action()
        except (VaultError, OSError) as error:
            self.error(error)

    def choose_vault(self, *_):
        dialog = Gtk.FileChooserDialog(title='Open a Markdown vault', transient_for=self,
                                      action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Open', Gtk.ResponseType.OK)
        if dialog.run() == Gtk.ResponseType.OK:
            self.try_action(lambda: self.load_vault(dialog.get_filename()))
        dialog.destroy()

    def confirm_panes(self, panes):
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
            elif result != Gtk.ResponseType.NO:
                return False
        return True

    def load_vault(self, folder):
        vault = Vault(folder)
        if not self.confirm_panes(list(self.panes.values())):
            return
        while self.tabs.get_n_pages():
            self.tabs.remove_page(0)
        self.panes.clear()
        self.vault = vault
        self.vault_label.set_text(vault.root.name)
        self.set_title(f'{vault.root.name} · Libre Vault')
        self.search_entry.set_text('')
        self.populate_files()
        self.status.set_text(f'{len(vault.notes)} notes · {vault.root}')
        if vault.notes:
            first = 'Welcome.md' if 'Welcome.md' in vault.notes else next(iter(vault.notes))
            self.open_note(first)
        self.update_context()

    def populate_files(self):
        for row in self.files.get_children():
            self.files.remove(row)
        if not self.vault:
            return
        for path in self.vault.search(self.search_entry.get_text()):
            row = Gtk.ListBoxRow()
            row.path = path
            label = Gtk.Label(label=path.removesuffix('.md'), xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.set_margin_start(8)
            row.add(label)
            self.files.add(row)
        self.files.show_all()

    def open_note(self, relative, fragment=''):
        if relative not in self.panes:
            def action():
                pane = NotePane(self, self.vault.read(relative))
                self.panes[relative] = pane
                self.tabs.append_page(pane, pane.tab_label)
                pane.show_all()
            self.try_action(action)
        if relative not in self.panes:
            return
        pane = self.panes[relative]
        self.tabs.set_current_page(self.tabs.page_num(pane))
        pane.editor.grab_focus()
        if fragment:
            text = pane.text()
            needle = fragment.removeprefix('^')
            match = re.search(r'(?m)^.*' + re.escape(needle) + r'.*$', text, re.IGNORECASE)
            if match:
                position = pane.buffer.get_iter_at_offset(match.start())
                pane.buffer.place_cursor(position)
                pane.editor.scroll_to_iter(position, 0.15, False, 0, 0)
        self.update_context()

    def current(self):
        index = self.tabs.get_current_page()
        return self.tabs.get_nth_page(index) if index >= 0 else None

    def save_current(self, *_):
        pane = self.current()
        if pane:
            self.try_action(pane.save)

    def close_tab(self, *_):
        pane = self.current()
        if pane and self.confirm_panes([pane]):
            self.tabs.remove_page(self.tabs.page_num(pane))
            self.panes.pop(pane.note.path, None)
            self.update_context()

    def new_note(self, *_):
        if not self.vault:
            self.choose_vault()
            return
        dialog = Gtk.Dialog(title='New note', transient_for=self, modal=True)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Create', Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text='Note name or existing-folder/Note name')
        entry.set_size_request(430, -1)
        entry.set_margin_top(16)
        entry.set_margin_bottom(16)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            note = self.try_action(lambda: self.vault.create(entry.get_text()))
            if note:
                self.populate_files()
                self.open_note(note.path)
        dialog.destroy()

    def section(self, title):
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class('caption')
        self.context.pack_start(label, False, False, 0)

    def context_button(self, label, action, enabled=True):
        button = Gtk.Button(label=label)
        button.set_sensitive(enabled)
        button.connect('clicked', lambda *_: action())
        self.context.pack_start(button, False, False, 0)

    def follow_link(self, source, target):
        candidates = self.vault.resolve(source, target)
        fragment = target.partition('#')[2]
        if len(candidates) == 1:
            self.open_note(candidates[0], fragment)
        elif candidates:
            dialog = Gtk.Dialog(title='Choose a note', transient_for=self, modal=True)
            dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
            for i, path in enumerate(candidates):
                dialog.add_button(path, i + 1)
            result = dialog.run()
            dialog.destroy()
            if 1 <= result <= len(candidates):
                self.open_note(candidates[result - 1], fragment)
        else:
            self.status.set_text(f'Unresolved note or unsupported attachment: {target}')

    def update_context(self):
        for child in self.context.get_children():
            self.context.remove(child)
        pane = self.current()
        if not pane or not self.vault:
            self.section('OUTLINE · LINKS · BACKLINKS')
            self.context.show_all()
            return False
        self.section('OUTLINE')
        for match in re.finditer(r'(?m)^(#{1,6})\s+(.+?)\s*#*$', pane.text()):
            offset = match.start()
            def jump(offset=offset):
                position = pane.buffer.get_iter_at_offset(offset)
                pane.buffer.place_cursor(position)
                pane.editor.scroll_to_iter(position, 0.15, False, 0, 0)
            self.context_button(match[2], jump)
        self.section('OUTGOING LINKS')
        for link in links(pane.text()):
            self.context_button(('↳ ' if link.embedded else '') + link.label,
                                lambda target=link.target: self.follow_link(pane.note.path, target))
        self.section('BACKLINKS · SAVED FILES')
        for path in self.vault.backlinks(pane.note.path):
            self.context_button(path.removesuffix('.md'), lambda path=path: self.open_note(path))
        self.context.show_all()
        return False

    def refresh(self):
        if not self.vault:
            return True
        try:
            self.vault.refresh()
            self.populate_files()
            for path, pane in self.panes.items():
                disk_note = self.vault.notes.get(path)
                if disk_note is None:
                    self.status.set_text(f'{path} was removed or cannot be read. Open editor contents are retained.')
                elif disk_note.revision != pane.note.revision:
                    if pane.dirty:
                        self.status.set_text(f'External change to {path}. Your unsaved edits are retained; saving will stop on conflict.')
                    else:
                        pane.note = disk_note
                        pane.buffer.handler_block_by_func(pane.changed)
                        pane.buffer.set_text(disk_note.text.replace('\r\n', '\n'))
                        pane.buffer.handler_unblock_by_func(pane.changed)
                        pane.render()
            self.update_context()
        except (VaultError, OSError) as error:
            self.status.set_text(str(error))
        return True

    def key_press(self, _, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            action = {'s': self.save_current, 'o': self.choose_vault, 'n': self.new_note, 'w': self.close_tab}.get(Gdk.keyval_name(event.keyval).lower())
            if action:
                action()
                return True
        return False

    def on_delete(self, *_):
        return not self.confirm_panes(list(self.panes.values()))


def main():
    parser = argparse.ArgumentParser(description='Open a local Markdown vault in Libre Vault.')
    parser.add_argument('vault', nargs='?', help='Path to a vault folder')
    args = parser.parse_args()
    app = Gtk.Application(application_id='org.librevault.Prototype')
    app.connect('activate', lambda app: Window(app, args.vault).present())
    return app.run([])


if __name__ == '__main__':
    raise SystemExit(main())
