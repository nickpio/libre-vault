"""Workspace command palette and keyboard reference."""
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gtk

from .shortcuts import APP_SHORTCUTS, EDITOR_SHORTCUTS, KEYBIND_GROUPS, effective_shortcuts


class CommandPalette(Gtk.EventBox):
    max_width = 760

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.previous_focus = owner.get_focus()
        self.set_visible_window(False)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.card = Gtk.Frame()
        self.card.set_shadow_type(Gtk.ShadowType.NONE)
        self.card.set_halign(Gtk.Align.CENTER)
        self.card.set_valign(Gtk.Align.START)
        self.card.set_margin_top(32)
        self.card.get_style_context().add_class('command-palette')
        self.add(self.card)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_border_width(12)
        self.card.add(content)
        caption = Gtk.Label(label='Commands', xalign=0)
        caption.get_style_context().add_class('palette-label')
        content.pack_start(caption, False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text='Type a command…')
        self.search.get_accessible().set_name('Search commands')
        content.pack_start(self.search, False, False, 0)
        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results.set_activate_on_single_click(True)
        self.results.get_style_context().add_class('command-results')
        self.results_scroll = Gtk.ScrolledWindow()
        self.results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.results_scroll.set_max_content_height(176)
        self.results_scroll.set_propagate_natural_height(True)
        self.results_scroll.add(self.results)
        content.pack_start(self.results_scroll, False, False, 0)
        self.results.connect('row-selected', self.reveal_selected)
        # Keep the keyboard reference first as more commands are added.
        actions = [('Open keybind cheatsheet', owner.show_keybinds),
                   ('Open vault', owner.choose_vault), ('Open settings', owner.show_settings)]
        if owner.vault:
            actions.extend([('Open graph in tab', owner.show_graph_tab),
                            ('Open local graph in tab', owner.show_local_graph_tab),
                            ('Graph view', owner.show_graph),
                            ('Local graph', owner.show_local_graph),
                            ('Open Bases view', owner.show_bases),
                            ('Start slides presentation', owner.show_slides),
                            ('Format converter…', owner.show_converter),
                            ('Quick switcher', owner.show_quick_switcher),
                            ('New note', owner.new_note), ('New canvas', owner.new_canvas),
                            ('Open canvas', owner.open_canvas_command),
                            ('New folder', owner.new_folder),
                            ('Open daily note', owner.open_daily_note),
                            ('Insert template', owner.insert_template),
                            ('Insert table…', owner.insert_table),
                            ('Edit table…', owner.edit_table),
                            ('New note from template', owner.create_from_template),
                            ('New unique note', owner.new_unique_note),
                            ('Open random note', owner.open_random_note),
                            ('Save note', owner.save_current),
                            ('Toggle highlight', owner.toggle_highlight),
                            ('Toggle strikethrough', owner.toggle_strikethrough),
                            ('Version history…', owner.show_version_history),
                            ('Close note', owner.close_tab),
                            ('Close vault', owner.close_vault),
                            ('Navigate back', owner.navigate_back), ('Navigate forward', owner.navigate_forward),
                            ('Toggle split editor', owner.toggle_split),
                            ('Split right', owner.split_right), ('Split down', owner.split_down),
                            ('Open note in new window', owner.pop_out_note),
                            ('Use standard tabs', lambda: owner.set_tab_mode('tabs')),
                            ('Use stacked tabs', lambda: owner.set_tab_mode('stacked')),
                            ('Use sliding note panes', lambda: owner.set_tab_mode('sliding')),
                            ('Toggle file tree', owner.toggle_file_tree),
                            ('Toggle note details', owner.toggle_details)])
            note = owner.current()
            bookmarked = note is not None and owner.note_is_bookmarked(note.note.path)
            actions.append(('Remove bookmark' if bookmarked else 'Bookmark current note',
                            owner.remove_current_bookmark if bookmarked else owner.bookmark_current_note))
            actions.extend([('Bookmark current search', owner.bookmark_current_search),
                            ('Bookmark selected folder', owner.bookmark_selected_folder),
                            ('Bookmark current graph', owner.bookmark_current_graph),
                            ('Open bookmarks', owner.show_bookmarks),
                            ('Save workspace layout…', owner.save_workspace_layout),
                            ('Load workspace layout…', owner.load_workspace_layout),
                            ('Manage workspaces…', owner.manage_workspaces)])
            if note is not None:
                actions.extend([
                    ('Toggle fold at cursor', note.toggle_fold),
                    ('Fold all headings', note.fold_all_headings),
                    ('Unfold all', note.unfold_all),
                    ('Add next occurrence', note.add_next_occurrence),
                    ('Select all occurrences', note.select_all_occurrences),
                ])
        for title, action in actions:
            row = Gtk.ListBoxRow()
            row.action, row.title = action, title
            command = Gtk.Box(spacing=10)
            icon = Gtk.Image.new_from_icon_name('system-run-symbolic', Gtk.IconSize.MENU)
            icon.get_style_context().add_class('muted')
            command.pack_start(icon, False, False, 0)
            command.pack_start(Gtk.Label(label=title, xalign=0), True, True, 0)
            row.add(command)
            self.results.add(row)
        self.empty = Gtk.Label(label='No matching commands', xalign=0)
        self.empty.set_no_show_all(True)
        content.pack_start(self.empty, False, False, 0)
        hint = Gtk.Label(label='↑↓ Navigate     Enter Run     Esc Close', xalign=0)
        hint.get_style_context().add_class('palette-hint')
        content.pack_start(hint, False, False, 0)
        self.search.connect('changed', self.filter_commands)
        self.search.connect('activate', self.run_selected)
        self.search.connect('key-press-event', self.search_key_press)
        self.results.connect('row-activated', lambda *_: self.run_selected())
        self.connect('button-press-event', self.outside_click)
        self.connect('size-allocate', self.resize_card)
        self.connect('destroy', self.restore_focus)
        self.resize_card(self, owner.overlay.get_allocation())

    def resize_card(self, _, allocation):
        width = max(1, min(self.max_width, allocation.width - 32))
        if self.card.get_size_request().width != width:
            self.card.set_size_request(width, -1)

    def outside_click(self, _, event):
        source = Gtk.get_event_widget(event)
        position = source.translate_coordinates(self.card, int(event.x), int(event.y))
        if position and (0 <= position[0] < self.card.get_allocated_width()
                         and 0 <= position[1] < self.card.get_allocated_height()):
            return False
        self.destroy()
        return True

    def restore_focus(self, *_):
        if (self.previous_focus is not None and not self.owner.in_destruction()
                and self.previous_focus.get_mapped()):
            self.previous_focus.grab_focus()

    def filter_commands(self, *_):
        words = self.search.get_text().casefold().split()
        visible = []
        for row in self.results.get_children():
            matches = all(word in row.title.casefold() for word in words)
            row.set_visible(matches)
            if matches:
                visible.append(row)
        self.results.unselect_all()
        if visible:
            self.results.select_row(visible[0])
        self.empty.set_visible(not visible)

    def search_key_press(self, _, event):
        if event.keyval not in (Gdk.KEY_Up, Gdk.KEY_Down):
            return False
        rows = [row for row in self.results.get_children() if row.get_visible()]
        if rows:
            selected = self.results.get_selected_row()
            index = rows.index(selected) if selected in rows else 0
            index = max(0, min(len(rows) - 1, index + (1 if event.keyval == Gdk.KEY_Down else -1)))
            self.results.select_row(rows[index])
        return True

    def reveal_selected(self, _, row):
        if row is None:
            return
        adjustment = self.results_scroll.get_vadjustment()
        allocation = row.get_allocation()
        if allocation.y < adjustment.get_value():
            adjustment.set_value(allocation.y)
        elif allocation.y + allocation.height > adjustment.get_value() + adjustment.get_page_size():
            adjustment.set_value(allocation.y + allocation.height - adjustment.get_page_size())

    def run_selected(self, *_):
        row = self.results.get_selected_row()
        if row and row.get_visible():
            action = row.action
            self.destroy()
            action()

    def key_press(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self.search.grab_focus()
            return True
        return False


class KeybindCheatsheet(Gtk.Dialog):
    def __init__(self, owner):
        super().__init__(title='Keybind cheatsheet', transient_for=owner, modal=True,
                         use_header_bar=True)
        self.set_default_size(760, 640)
        self.get_style_context().add_class('keybind-cheatsheet')
        self.add_button('Close', Gtk.ResponseType.CLOSE)
        self.connect('response', lambda *_: self.destroy())
        self.connect('key-press-event', self.key_press)
        content = self.get_content_area()
        content.get_style_context().add_class('keybind-content')
        content.set_border_width(12)
        content.set_spacing(8)
        self.search = Gtk.SearchEntry(placeholder_text='Search keys, actions, or sections…')
        self.search.get_accessible().set_name('Search keybinds')
        content.pack_start(self.search, False, False, 0)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content.pack_start(self.scroll, True, True, 0)
        self.bindings = Gtk.ListBox()
        self.bindings.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.bindings.get_style_context().add_class('keybind-list')
        self.scroll.add(self.bindings)
        self.bindings.set_adjustment(self.scroll.get_vadjustment())
        self.empty = Gtk.Label(label='No matching shortcuts. Try another key or action.')
        self.empty.get_style_context().add_class('muted')
        self.empty.set_margin_top(24)
        self.empty.set_margin_bottom(24)
        self.bindings.set_placeholder(self.empty)
        key_widths = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        application = tuple((Gtk.accelerator_get_label(*Gtk.accelerator_parse(shortcut.accelerator)),
                             shortcut.description) for shortcut in effective_shortcuts(APP_SHORTCUTS, 'app', owner.hotkeys))
        editing = tuple((Gtk.accelerator_get_label(*Gtk.accelerator_parse(shortcut.accelerator)),
                         shortcut.description) for shortcut in effective_shortcuts(EDITOR_SHORTCUTS, 'editor', owner.hotkeys))
        self.groups = (('Application', application), ('Editor commands', editing), *KEYBIND_GROUPS)
        self.bindings.set_header_func(self.section_header)
        for section, (title, bindings) in enumerate(self.groups):
            for keys, description in bindings:
                row = Gtk.ListBoxRow()
                row.section = section
                row.search_text = f'{title} {keys} {description}'.casefold()
                row.get_accessible().set_name(f'{title}: {description}, {keys}')
                line = Gtk.Box(spacing=20)
                key_label = Gtk.Label(label=keys, xalign=0, yalign=0)
                key_label.get_style_context().add_class('keybind-keys')
                key_label.set_line_wrap(True)
                key_label.set_max_width_chars(29)
                key_label.set_halign(Gtk.Align.START)
                key_label.set_valign(Gtk.Align.START)
                key_column = Gtk.Box()
                key_column.pack_start(key_label, False, False, 0)
                key_widths.add_widget(key_column)
                line.pack_start(key_column, False, False, 0)
                description_label = Gtk.Label(label=description, xalign=0, yalign=0)
                description_label.set_line_wrap(True)
                description_label.set_max_width_chars(48)
                description_label.set_valign(Gtk.Align.CENTER)
                line.pack_start(description_label, True, True, 0)
                row.add(line)
                self.bindings.add(row)
        hint = Gtk.Label(label='↑↓ Browse     ←→ Sections     Ctrl+F Search     Esc Close', xalign=0)
        hint.get_style_context().add_class('palette-hint')
        content.pack_start(hint, False, False, 0)
        self.bindings.set_filter_func(self.matches_search)
        self.search.connect('changed', self.filter_bindings)
        self.search.connect('activate', self.focus_results)
        self.show_all()
        self.filter_bindings()
        self.search.grab_focus()

    def matches_search(self, row):
        return all(word in row.search_text for word in self.search.get_text().casefold().split())

    def visible_rows(self):
        return [row for row in self.bindings.get_children() if row.get_child_visible()]

    def filter_bindings(self, *_):
        self.bindings.invalidate_filter()
        self.bindings.invalidate_headers()
        rows = self.visible_rows()
        self.bindings.select_row(rows[0] if rows else None)
        self.scroll.get_vadjustment().set_value(0)

    def focus_results(self, *_):
        row = self.bindings.get_selected_row()
        if row is not None:
            self.select_binding(row.get_index())

    def section_header(self, row, previous):
        if previous is not None and previous.section == row.section:
            row.set_header(None)
            return
        heading = Gtk.Label(label=self.groups[row.section][0], xalign=0)
        heading.get_style_context().add_class('keybind-heading')
        row.set_header(heading)
        heading.show()

    def select_binding(self, index, focus=True):
        row = self.bindings.get_row_at_index(index)
        self.bindings.select_row(row)
        if focus:
            row.grab_focus()
        else:
            self.scroll.get_vadjustment().clamp_page(
                row.get_allocation().y, row.get_allocation().y + row.get_allocated_height())
        header = row.get_header()
        if index == 0:
            self.scroll.get_vadjustment().set_value(0)
        elif header is not None:
            self.scroll.get_vadjustment().clamp_page(
                header.get_allocation().y, row.get_allocation().y + row.get_allocated_height())

    def key_press(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_f and modifiers == Gdk.ModifierType.CONTROL_MASK:
            self.search.grab_focus()
            self.search.select_region(0, -1)
            return True
        if modifiers:
            return False
        search_focused = self.get_focus() is self.search
        if search_focused and event.keyval in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home, Gdk.KEY_End):
            return False
        if event.keyval not in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home, Gdk.KEY_End):
            return False
        rows = self.visible_rows()
        if not rows:
            return True
        selected = self.bindings.get_selected_row()
        index = rows.index(selected) if selected in rows else 0
        last = len(rows) - 1
        starts = [i for i, row in enumerate(rows) if i == 0 or rows[i - 1].section != row.section]
        if event.keyval == Gdk.KEY_Up:
            index = max(0, index - 1)
        elif event.keyval == Gdk.KEY_Down:
            index = min(last, index + 1)
        elif event.keyval == Gdk.KEY_Left:
            index = max((start for start in starts if start < index), default=0)
        elif event.keyval == Gdk.KEY_Right:
            index = next((start for start in starts if start > index), index)
        elif event.keyval == Gdk.KEY_Home:
            index = 0
        elif event.keyval == Gdk.KEY_End:
            index = last
        self.select_binding(rows[index].get_index(), focus=not search_focused)
        return True


class TemplatePicker(Gtk.Dialog):
    """Choose one template note. Enter or the confirm button accepts the selection."""

    def __init__(self, owner, paths, title='Insert template', confirm='Insert'):
        super().__init__(title=title, transient_for=owner, modal=True, use_header_bar=True)
        self.choice = None
        self.set_default_size(520, 420)
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.add_button(confirm, Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.connect('response', self._capture_choice)
        self.connect('key-press-event', self._key_press)
        content = self.get_content_area()
        content.set_border_width(12)
        content.set_spacing(8)
        self.search = Gtk.SearchEntry(placeholder_text='Find a template…')
        self.search.get_accessible().set_name('Search templates')
        self.search.set_activates_default(False)
        content.pack_start(self.search, False, False, 0)
        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results.set_activate_on_single_click(True)
        self.results.get_accessible().set_name('Templates')
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(220)
        scroll.add(self.results)
        content.pack_start(scroll, True, True, 0)
        for path in paths:
            row = Gtk.ListBoxRow()
            row.path = path
            label = Gtk.Label(label=path, xalign=0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_ellipsize(3)
            row.add(label)
            self.results.add(row)
        self.empty = Gtk.Label(label='No matching templates', xalign=0)
        self.empty.set_no_show_all(True)
        content.pack_start(self.empty, False, False, 0)
        self.search.connect('changed', self._filter)
        self.search.connect('activate', self._accept)
        self.search.connect('key-press-event', self._search_key)
        self.results.connect('row-activated', lambda *_: self._accept())
        self.show_all()
        self._filter()
        self.search.grab_focus()

    def _visible_rows(self):
        return [row for row in self.results.get_children() if row.get_visible()]

    def _filter(self, *_):
        words = self.search.get_text().casefold().split()
        visible = []
        for row in self.results.get_children():
            matches = all(word in row.path.casefold() for word in words)
            row.set_visible(matches)
            if matches:
                visible.append(row)
        self.results.unselect_all()
        if visible:
            self.results.select_row(visible[0])
        self.empty.set_visible(not visible)
        self.set_response_sensitive(Gtk.ResponseType.OK, bool(visible))

    def _selected(self):
        row = self.results.get_selected_row()
        if row is not None and row.get_visible():
            return row
        return None

    def _accept(self, *_):
        row = self._selected()
        if row is None:
            return
        self.choice = row.path
        self.response(Gtk.ResponseType.OK)

    def _capture_choice(self, _, response):
        if response == Gtk.ResponseType.OK and self.choice is None:
            row = self._selected()
            self.choice = row.path if row is not None else None

    def _search_key(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text('')
            else:
                self.response(Gtk.ResponseType.CANCEL)
            return True
        if event.keyval not in (Gdk.KEY_Up, Gdk.KEY_Down):
            return False
        rows = self._visible_rows()
        if rows:
            selected = self._selected()
            index = rows.index(selected) if selected in rows else 0
            index = max(0, min(len(rows) - 1, index + (1 if event.keyval == Gdk.KEY_Down else -1)))
            self.results.select_row(rows[index])
        return True

    def _key_press(self, _, event):
        if event.keyval == Gdk.KEY_Escape and not self.search.get_text():
            self.response(Gtk.ResponseType.CANCEL)
            return True
        return False


class NoteNameDialog(Gtk.Dialog):
    def __init__(self, owner):
        super().__init__(title='New note from template', transient_for=owner, modal=True, use_header_bar=True)
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.add_button('Create', Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_response_sensitive(Gtk.ResponseType.OK, False)
        self.connect('key-press-event', self._key_press)
        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(8)
        self.entry = Gtk.Entry(placeholder_text='Note name or folder/Note name')
        self.entry.set_size_request(420, -1)
        self.entry.get_accessible().set_name('Note name')
        self.entry.set_activates_default(True)
        self.entry.connect('changed', lambda *_: self.set_response_sensitive(
            Gtk.ResponseType.OK, bool(self.entry.get_text().strip())))
        content.pack_start(self.entry, False, False, 0)
        self.show_all()
        self.entry.grab_focus()

    def _key_press(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            self.response(Gtk.ResponseType.CANCEL)
            return True
        return False


class UniqueNoteDialog(Gtk.Dialog):
    def __init__(self, owner, templates):
        super().__init__(title='New unique note', transient_for=owner, modal=True, use_header_bar=True)
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.add_button('Create', Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.connect('key-press-event', self._key_press)
        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(8)
        content.pack_start(Gtk.Label(label='Optional title', xalign=0), False, False, 0)
        self.title_entry = Gtk.Entry(placeholder_text='Leave empty for a timestamp only')
        self.title_entry.set_size_request(420, -1)
        self.title_entry.get_accessible().set_name('Unique note title')
        self.title_entry.set_activates_default(True)
        content.pack_start(self.title_entry, False, False, 0)
        content.pack_start(Gtk.Label(label='Template', xalign=0), False, False, 0)
        self.template_choice = Gtk.ComboBoxText()
        self.template_choice.append('blank', 'Blank note')
        for path in templates:
            self.template_choice.append(path, path)
        self.template_choice.set_active_id('blank')
        self.template_choice.get_accessible().set_name('Unique note template')
        content.pack_start(self.template_choice, False, False, 0)
        self.show_all()
        self.title_entry.grab_focus()

    def template_path(self):
        selected = self.template_choice.get_active_id()
        return '' if selected in (None, '', 'blank') else selected

    def _key_press(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            self.response(Gtk.ResponseType.CANCEL)
            return True
        return False
