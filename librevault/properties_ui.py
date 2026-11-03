"""Typed GTK frontmatter form, shared by the inspector and properties dialog."""
from datetime import date, datetime
import json
import math
import re
import tempfile
import os
from pathlib import Path

import yaml
from gi.repository import Gtk, Pango


TYPES = ('text', 'number', 'checkbox', 'date', 'datetime', 'list', 'yaml')


def inferred_type(value):
    if isinstance(value, bool):
        return 'checkbox'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, datetime):
        return 'datetime'
    if isinstance(value, date):
        return 'date'
    if isinstance(value, list):
        return 'list'
    if isinstance(value, dict) or value is None:
        return 'yaml'
    return 'text'


class PropertyForm(Gtk.Box):
    def __init__(self, properties, root):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.registry_path = Path(root) / '.librevault' / 'property-types.json'
        try:
            if self.registry_path.parent.is_symlink() or self.registry_path.is_symlink():
                raise OSError('Property type registry must be inside the vault.')
            self.registry = json.loads(self.registry_path.read_text())
            if not isinstance(self.registry, dict):
                self.registry = {}
        except (OSError, ValueError):
            self.registry = {}
        self.rows = []
        self.connect('destroy', lambda *_: self.rows.clear())
        for key, value in properties.items():
            self.add_property(key, value)
        add = Gtk.Button(label='Add property')
        add.connect('clicked', lambda *_: self.add_property('', ''))
        self.pack_end(add, False, False, 0)

    def add_property(self, key, value):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header = Gtk.Box(spacing=4)
        name = Gtk.Entry(text=key, placeholder_text='Property name', hexpand=True, width_chars=8)
        name.get_accessible().set_name('Property name')
        kind = Gtk.ComboBoxText()
        for item in TYPES:
            kind.append(item, item.capitalize())
        selected = self.registry.get(key, inferred_type(value))
        kind.set_active_id(selected if selected in TYPES else inferred_type(value))
        remove = Gtk.Button(label='−')
        remove.set_tooltip_text('Remove property')
        kind.get_accessible().set_name('Property type')
        header.pack_start(name, True, True, 0)
        header.pack_start(kind, False, False, 0)
        header.pack_start(remove, False, False, 0)
        row.pack_start(header, False, False, 0)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        row.pack_start(holder, False, False, 0)
        record = [row, name, kind, None]
        self.rows.append(record)
        def rebuild(*_):
            current = value
            if record[3]:
                try:
                    current = record[3]()
                except ValueError:
                    pass
            for child in holder.get_children():
                child.destroy()
            widget, getter = self.control(kind.get_active_id(), current)
            widget.get_accessible().set_name(key or 'Property value')
            holder.pack_start(widget, False, False, 0)
            record[3] = getter
            holder.show_all()
        kind.connect('changed', rebuild)
        remove.connect('clicked', lambda *_: (self.rows.remove(record), row.destroy()))
        rebuild()
        self.pack_start(row, False, False, 0)
        self.show_all()

    def control(self, kind, value):
        if kind == 'checkbox':
            widget = Gtk.CheckButton(label='Enabled')
            widget.set_active(value is True)
            return widget, widget.get_active
        if kind == 'list':
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            pills = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE)
            pills.set_max_children_per_line(4)
            values = list(value) if isinstance(value, list) else ([value] if value else [])
            def refresh():
                for child in pills.get_children():
                    child.destroy()
                for index, item in enumerate(values):
                    button = Gtk.Button(label=str(item) + ' ×')
                    button.get_child().set_ellipsize(Pango.EllipsizeMode.END)
                    button.get_child().set_max_width_chars(18)
                    button.set_tooltip_text('Remove item: ' + str(item))
                    button.connect('clicked', lambda _, i=index: (values.pop(i), refresh()))
                    pills.add(button)
                pills.show_all()
            entry = Gtk.Entry(placeholder_text='Add item, then press Enter')
            def append(*_):
                if entry.get_text().strip():
                    values.append(entry.get_text().strip())
                    entry.set_text('')
                    refresh()
            entry.connect('activate', append)
            box.pack_start(pills, False, False, 0)
            box.pack_start(entry, False, False, 0)
            refresh()
            def get():
                append()
                return list(values)
            return box, get
        if kind == 'number':
            entry = Gtk.Entry(text=str(value), input_purpose=Gtk.InputPurpose.NUMBER)
            def number():
                raw = entry.get_text().strip()
                try:
                    parsed = int(raw) if re.fullmatch(r'[+-]?\d+', raw) else float(raw)
                except ValueError as error:
                    raise ValueError('Enter a valid number.') from error
                if isinstance(parsed, float) and not math.isfinite(parsed):
                    raise ValueError('Enter a finite number.')
                return parsed
            return entry, number
        entry = Gtk.Entry(text=yaml.safe_dump(value, default_flow_style=True).strip().removesuffix('\n...')
                          if kind == 'yaml' else str(value) if value is not None else '')
        if kind in ('date', 'datetime'):
            box = Gtk.Box(spacing=4)
            box.pack_start(entry, True, True, 0)
            entry.set_width_chars(10)
            button = Gtk.MenuButton()
            button.set_image(Gtk.Image.new_from_icon_name('x-office-calendar-symbolic', Gtk.IconSize.BUTTON))
            button.set_tooltip_text('Choose date; double-click a day to select it')
            popover = Gtk.Popover.new(button)
            calendar = Gtk.Calendar()
            try:
                current = date.fromisoformat(str(value)[:10])
                calendar.select_month(current.month - 1, current.year)
                calendar.select_day(current.day)
            except ValueError:
                pass
            def choose(*_):
                year, month, day = calendar.get_date()
                chosen = date(year, month + 1, day).isoformat()
                entry.set_text(chosen + ('T00:00:00' if kind == 'datetime' else ''))
                popover.popdown()
            calendar.connect('day-selected-double-click', choose)
            popover.add(calendar)
            calendar.show()
            button.set_popover(popover)
            box.pack_start(button, False, False, 0)
            def get_date():
                try:
                    return (datetime if kind == 'datetime' else date).fromisoformat(entry.get_text())
                except ValueError as error:
                    raise ValueError('Enter an ISO date (YYYY-MM-DD) or date and time.') from error
            return box, get_date
        if kind == 'yaml':
            def get_yaml():
                try:
                    return yaml.safe_load(entry.get_text())
                except yaml.YAMLError as error:
                    raise ValueError('Invalid YAML value.') from error
            return entry, get_yaml
        return entry, entry.get_text

    def source(self):
        properties = {}
        for _, name, kind, getter in self.rows:
            key = name.get_text().strip()
            if not key or key in properties:
                raise ValueError('Property names must be nonempty and unique.')
            properties[key] = getter()
        return yaml.safe_dump(properties, allow_unicode=True, sort_keys=False) if properties else ''

    def save_registry(self):
        self.registry.update({name.get_text().strip(): kind.get_active_id() for _, name, kind, _ in self.rows})
        if self.registry_path.parent.is_symlink() or self.registry_path.is_symlink():
            raise OSError('Property type registry must be inside the vault.')
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.property-types-', dir=self.registry_path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w') as handle:
                json.dump(self.registry, handle, indent=2)
            temporary.replace(self.registry_path)
        finally:
            temporary.unlink(missing_ok=True)
