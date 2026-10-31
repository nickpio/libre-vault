"""Category-based appearance and editor preferences."""
from dataclasses import replace
import gi

gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, Gtk

from .preferences import BOUNDS, OPTIONS, Preferences


class SettingsDialog(Gtk.Dialog):
    def __init__(self, owner):
        super().__init__(title='Settings', transient_for=owner, modal=True, use_header_bar=True)
        self.owner = owner
        self.controls = {}
        self.updating = False
        self.set_default_size(860, 620)
        self.get_style_context().add_class('settings-dialog')
        self.add_button('Done', Gtk.ResponseType.CLOSE)
        self.connect('response', lambda *_: self.destroy())
        self.connect('key-press-event', self.key_press)
        layout = Gtk.Box()
        self.get_content_area().pack_start(layout, True, True, 0)
        navigation = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        navigation.set_size_request(192, -1)
        navigation.get_style_context().add_class('settings-navigation')
        navigation_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        navigation_content.set_margin_start(12)
        navigation_content.set_margin_end(12)
        navigation_content.set_margin_top(20)
        navigation_content.set_margin_bottom(16)
        navigation.pack_start(navigation_content, True, True, 0)
        heading = Gtk.Label(label='Options', xalign=0)
        heading.get_style_context().add_class('settings-heading')
        navigation_content.pack_start(heading, False, False, 0)
        self.categories = Gtk.ListBox()
        self.categories.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.categories.get_style_context().add_class('settings-categories')
        navigation_content.pack_start(self.categories, False, False, 0)
        reset = Gtk.Button(label='Restore defaults')
        reset.get_style_context().add_class('settings-reset')
        reset.connect('clicked', lambda *_: self.save(Preferences()))
        navigation_content.pack_end(reset, False, False, 0)
        layout.pack_start(navigation, False, False, 0)
        self.stack = Gtk.Stack()
        layout.pack_start(self.stack, True, True, 0)
        appearance = self.page('Appearance', 'Make Libre Vault feel like your workspace.')
        self.combo(appearance, 'theme', 'Base theme', 'Choose a dark or light workspace.')
        self.spin(appearance, 'interface_size', 'Interface font size', 'Size of text in menus and controls.', 'pt')
        self.combo(appearance, 'editor_font', 'Text font', 'Font family used when writing and reading notes.')
        self.spin(appearance, 'editor_size', 'Text font size', 'Size of your note text. Headings scale with it.', 'pt')
        preview = Gtk.Label(label='A space for your thoughts', xalign=0)
        preview.get_style_context().add_class('editor-preview')
        preview.get_style_context().add_class('settings-preview')
        appearance.pack_start(preview, False, False, 16)
        editor = self.page('Editor', 'Choose how notes behave while you write.')
        self.switch(editor, 'readable_length', 'Readable line length', 'Keep long lines in a centered writing column.')
        self.spin(editor, 'line_width', 'Writing column width', 'Maximum width when readable line length is enabled.', 'px')
        self.switch(editor, 'wrap_lines', 'Wrap long lines', 'Wrap text to the editor width instead of scrolling horizontally.')
        self.switch(editor, 'continue_lists', 'Continue lists on Enter', 'Continue bullets, numbers, and tasks. Enter on an empty item exits the list.')
        self.error = Gtk.Label(xalign=0)
        self.error.set_line_wrap(True)
        self.error.set_no_show_all(True)
        self.error.get_style_context().add_class('settings-error')
        self.get_content_area().pack_end(self.error, False, False, 8)
        self.categories.connect('row-selected', self.select_category)
        self.sync_controls()
        self.show_all()
        self.categories.select_row(self.categories.get_row_at_index(0))

    def page(self, title, description):
        row = Gtk.ListBoxRow()
        row.page_name = title
        row.add(Gtk.Label(label=title, xalign=0))
        self.categories.add(row)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.set_border_width(24)
        scroll.add(body)
        self.stack.add_named(scroll, title)
        heading = Gtk.Label(label=title, xalign=0)
        heading.get_style_context().add_class('settings-page-title')
        body.pack_start(heading, False, False, 0)
        subtitle = Gtk.Label(label=description, xalign=0)
        subtitle.set_line_wrap(True)
        subtitle.get_style_context().add_class('settings-subtitle')
        body.pack_start(subtitle, False, False, 8)
        return body

    def select_category(self, _, row):
        if row:
            self.stack.set_visible_child_name(row.page_name)

    def setting_row(self, page, key, title, description, control, unit=None):
        self.controls[key] = control
        control.get_accessible().set_name(title)
        row = Gtk.Box(spacing=20)
        row.get_style_context().add_class('setting-row')
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        labels.pack_start(Gtk.Label(label=title, xalign=0), False, False, 0)
        detail = Gtk.Label(label=description, xalign=0)
        detail.set_line_wrap(True)
        detail.set_max_width_chars(42)
        detail.get_style_context().add_class('settings-description')
        labels.pack_start(detail, False, False, 0)
        row.pack_start(labels, True, True, 0)
        value = Gtk.Box(spacing=6)
        value.set_valign(Gtk.Align.CENTER)
        value.pack_start(control, False, False, 0)
        if unit:
            suffix = Gtk.Label(label=unit)
            suffix.get_style_context().add_class('muted')
            value.pack_start(suffix, False, False, 0)
        row.pack_end(value, False, False, 0)
        page.pack_start(row, False, False, 0)

    def combo(self, page, key, title, description):
        control = Gtk.ComboBoxText()
        for value, label in OPTIONS[key]:
            control.append(value, label)
        control.connect('changed', lambda *_: self.change(key, control.get_active_id()))
        self.setting_row(page, key, title, description, control)

    def spin(self, page, key, title, description, unit):
        low, high = BOUNDS[key]
        control = Gtk.SpinButton.new_with_range(low, high, 1 if unit == 'pt' else 20)
        control.set_numeric(True)
        control.set_width_chars(4)
        control.connect('value-changed', lambda *_: self.change(key, control.get_value_as_int()))
        self.setting_row(page, key, title, description, control, unit)

    def switch(self, page, key, title, description):
        control = Gtk.Switch()
        control.connect('notify::active', lambda *_: self.change(key, control.get_active()))
        self.setting_row(page, key, title, description, control)

    def sync_controls(self):
        self.updating = True
        try:
            for key, control in self.controls.items():
                value = getattr(self.owner.preferences, key)
                if key in OPTIONS:
                    control.set_active_id(value)
                elif key in BOUNDS:
                    control.set_value(value)
                else:
                    control.set_active(value)
            self.controls['line_width'].set_sensitive(self.owner.preferences.readable_length)
        finally:
            self.updating = False

    def change(self, key, value):
        if not self.updating:
            self.save(replace(self.owner.preferences, **{key: value}))

    def save(self, preferences):
        try:
            self.owner.set_preferences(preferences)
        except OSError as error:
            self.error.set_text(f'Could not save settings: {error}')
            self.error.show()
            self.sync_controls()
        else:
            self.error.hide()

    def key_press(self, _, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False
