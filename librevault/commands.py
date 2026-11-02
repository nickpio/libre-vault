"""Workspace command palette and keyboard reference."""
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gtk

from .shortcuts import APP_SHORTCUTS, EDITOR_SHORTCUTS, KEYBIND_GROUPS


class CommandPalette(Gtk.EventBox):
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
        self.results_scroll.set_max_content_height(88)
        self.results_scroll.set_propagate_natural_height(True)
        self.results_scroll.add(self.results)
        content.pack_start(self.results_scroll, False, False, 0)
        self.results.connect('row-selected', self.reveal_selected)
        # Keep the keyboard reference first as more commands are added.
        actions = [('Open keybind cheatsheet', owner.show_keybinds),
                   ('Open vault', owner.choose_vault), ('Open settings', owner.show_settings)]
        if owner.vault:
            actions.extend([('Quick switcher', owner.show_quick_switcher),
                            ('New note', owner.new_note), ('New folder', owner.new_folder),
                            ('Save note', owner.save_current), ('Close note', owner.close_tab),
                            ('Close vault', owner.close_vault),
                            ('Navigate back', owner.navigate_back), ('Navigate forward', owner.navigate_forward),
                            ('Toggle split editor', owner.toggle_split),
                            ('Toggle file tree', owner.toggle_file_tree),
                            ('Toggle note details', owner.toggle_details)])
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

    def resize_card(self, _, allocation):
        width = max(1, min(560, allocation.width - 32))
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
                             shortcut.description) for shortcut in APP_SHORTCUTS)
        editing = tuple((Gtk.accelerator_get_label(*Gtk.accelerator_parse(shortcut.accelerator)),
                         shortcut.description) for shortcut in EDITOR_SHORTCUTS)
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
