"""Shortcut recording and conflict feedback for Settings."""
from gi.repository import Gdk, Gtk
from .shortcuts import APP_SHORTCUTS, EDITOR_SHORTCUTS, shortcut_id, shortcut_key


def add_hotkey_settings(dialog):
    page = dialog.page('Hotkeys', 'Select a shortcut field and press the new keys. Apply saves it; Backspace clears it.')
    problem = Gtk.Label(xalign=0)
    problem.set_line_wrap(True)
    page.pack_start(problem, False, False, 6)
    for scope, shortcuts in (('app', APP_SHORTCUTS), ('editor', EDITOR_SHORTCUTS)):
        for shortcut in shortcuts:
            identifier = shortcut_id(scope, shortcut)
            row = Gtk.Box(spacing=8)
            label = Gtk.Label(label=shortcut.description, xalign=0, hexpand=True)
            label.set_line_wrap(True)
            row.pack_start(label, True, True, 0)
            entry = Gtk.Entry(editable=False, width_chars=20)
            entry.binding = dialog.owner.hotkeys.get(identifier, shortcut.accelerator)
            def display(field):
                field.set_text(Gtk.accelerator_get_label(*Gtk.accelerator_parse(field.binding)) if field.binding else 'Unbound')
            display(entry)
            def capture(field, event):
                if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_Escape):
                    return False
                if event.keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
                                     Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R):
                    return True
                field.binding = '' if event.keyval in (Gdk.KEY_BackSpace, Gdk.KEY_Delete) and not (event.state & Gtk.accelerator_get_default_mod_mask()) else Gtk.accelerator_name(
                    shortcut_key(event.keyval), event.state & Gtk.accelerator_get_default_mod_mask())
                display(field)
                return True
            entry.connect('key-press-event', capture)
            row.pack_start(entry, False, False, 0)
            apply = Gtk.Button(label='Apply')
            def save(_, field=entry, key=identifier):
                try:
                    dialog.owner.set_hotkey(key, field.binding)
                    problem.set_text('Shortcut saved.')
                except (ValueError, OSError) as error:
                    problem.set_text(str(error))
            apply.connect('clicked', save)
            row.pack_start(apply, False, False, 0)
            reset = Gtk.Button(label='Default')
            def restore(_, field=entry, default=shortcut.accelerator):
                field.binding = default
                display(field)
            reset.connect('clicked', restore)
            row.pack_start(reset, False, False, 0)
            page.pack_start(row, False, False, 4)
