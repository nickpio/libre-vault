"""Application shortcuts and the editor's supported GTK keyboard controls."""
from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class Shortcut:
    accelerator: str
    description: str
    handler: str


APP_SHORTCUTS = (
    Shortcut('<Control><Alt>g', 'Toggle graph view', 'show_graph'),
    Shortcut('<Alt>p', 'Open command palette', 'show_command_palette'),
    Shortcut('<Alt>k', 'Open keybind cheatsheet', 'show_keybinds'),
    Shortcut('<Control>o', 'Open a vault', 'choose_vault'),
    Shortcut('<Control>n', 'Create a note', 'new_note'),
    Shortcut('<Control><Alt>d', 'Open daily note', 'open_daily_note'),
    Shortcut('<Control><Alt>t', 'Insert template', 'insert_template'),
    Shortcut('<Control><Alt>u', 'New unique note', 'new_unique_note'),
    Shortcut('<Control><Alt>r', 'Open random note', 'open_random_note'),
    Shortcut('<Control>s', 'Save the active note', 'save_current'),
    Shortcut('<Control>w', 'Close the active note', 'close_tab'),
    Shortcut('<Control><Shift>d', 'Toggle note details panel', 'toggle_details'),
    Shortcut('<Control><Shift>e', 'Toggle file tree', 'toggle_file_tree'),
    Shortcut('<Control><Shift>o', 'Quick switcher', 'show_quick_switcher'),
    Shortcut('<Alt>Left', 'Navigate back', 'navigate_back'),
    Shortcut('<Alt>Right', 'Navigate forward', 'navigate_forward'),
    Shortcut('<Control><Shift>s', 'Toggle split editor', 'toggle_split'),
    Shortcut('<Control><Alt>Right', 'Split pane right', 'split_right'),
    Shortcut('<Control><Alt>Down', 'Split pane down', 'split_down'),
    Shortcut('<Control><Alt>n', 'Open note in new window', 'pop_out_note'),
)

EDITOR_SHORTCUTS = (
    Shortcut('<Control>z', 'Undo last edit', 'undo'),
    Shortcut('<Control><Shift>z', 'Redo last edit', 'redo'),
    Shortcut('<Control>b', 'Toggle bold', 'bold'),
    Shortcut('<Control>i', 'Toggle italic', 'italic'),
    Shortcut('<Control><Shift>h', 'Toggle highlight', 'highlight'),
    Shortcut('<Control><Shift>x', 'Toggle strikethrough', 'strikethrough'),
    Shortcut('<Control>k', 'Insert a Markdown link', 'insert_link'),
    Shortcut('<Control>f', 'Find in the active note', 'show_find'),
    Shortcut('<Control>h', 'Find and replace in the active note', 'show_replace'),
    Shortcut('F3', 'Find next match', 'find_next'),
    Shortcut('<Control>g', 'Find next match', 'find_next'),
    Shortcut('<Shift>F3', 'Find previous match', 'find_previous'),
    Shortcut('<Control><Shift>g', 'Find previous match', 'find_previous'),
    Shortcut('<Control><Shift>bracketleft', 'Fold the section at the cursor', 'fold_at_cursor'),
    Shortcut('<Control><Shift>bracketright', 'Unfold the section at the cursor', 'unfold_at_cursor'),
    Shortcut('<Control>d', 'Add the next occurrence', 'add_next_occurrence'),
    Shortcut('<Control><Shift>l', 'Select all occurrences', 'select_all_occurrences'),
)

KEYBIND_GROUPS = (
    ('Writing and editing', (
        ('Enter', 'Continue a list or task; exit an empty item; otherwise insert a line break'),
        ('Shift+Enter', 'Insert a line break without continuing the list'),
        ('Tab / Shift+Tab', 'Move between cells in a Markdown table; otherwise insert a tab'),
        ('Space', 'Toggle a focused task checkbox'),
        ('Ctrl+C / Ctrl+Insert', 'Copy selected Markdown, including hidden syntax'),
        ('Ctrl+X / Shift+Delete', 'Cut selected Markdown, including hidden syntax'),
        ('Ctrl+V / Shift+Insert', 'Paste from the clipboard'),
        ('Ctrl+A / Ctrl+/', 'Select all text'),
        ('Ctrl+Shift+A / Ctrl+\\', 'Clear the selection'),
        ('Backspace / Shift+Backspace', 'Delete before the cursor'),
        ('Delete', 'Delete after the cursor'),
        ('Ctrl+Backspace / Ctrl+Delete', 'Delete to the previous or next word boundary'),
        ('Ctrl+Shift+Backspace / Ctrl+Shift+Delete', 'Delete to the previous or next paragraph boundary'),
        ('Insert', 'Toggle overwrite mode'),
        ('Ctrl+. / Ctrl+;', 'Open the emoji picker'),
        ('F7', 'Toggle cursor visibility'),
    )),
    ('Find and replace', (
        ('Enter / Shift+Enter', 'Find the next or previous match from the find field'),
        ('Enter in replacement field', 'Replace the selected match'),
        ('Escape', 'Close the find bar and return to the editor'),
    )),
    ('Moving through a note', (
        ('Ctrl+click', 'Follow an editor link'),
        ('Arrow keys', 'Move the cursor'),
        ('Ctrl+Left / Ctrl+Right', 'Move by word'),
        ('Ctrl+Up / Ctrl+Down', 'Move by paragraph'),
        ('Home / End', 'Move to the start or end of the displayed line'),
        ('Ctrl+Home / Ctrl+End', 'Move to the start or end of the note; Ctrl+Home reveals frontmatter'),
        ('Page Up / Page Down', 'Move by page'),
        ('Ctrl+Page Up / Ctrl+Page Down', 'Move horizontally by page'),
        ('Shift + movement keys', 'Extend the selection, including Ctrl and page movements'),
        ('Numeric keypad navigation', 'With Num Lock off, corresponding movement and editing keys use the same shortcuts'),
    )),
    ('File explorer', (
        ('Up / Down', 'Select the previous or next entry'),
        ('Home / End', 'Select the first or last entry'),
        ('Page Up / Page Down', 'Move through entries by page'),
        ('Enter / Space', 'Open the selected note or toggle the selected folder'),
        ('Right', 'Expand the selected folder or enter its first child'),
        ('Left', 'Collapse the selected folder or select its parent'),
        ('Shift+F10 / Menu', 'Open the context menu for the selected entry'),
    )),
    ('Note tabs', (
        ('Ctrl+Alt+Page Up / Ctrl+Alt+Page Down', 'Switch to the previous or next note'),
        ('Ctrl+Page Up / Ctrl+Page Down', 'Switch notes when the tab strip has focus'),
        ('Left / Right', 'Move between tabs when the tab strip has focus'),
        ('Home / End', 'Focus the first or last tab when the tab strip has focus'),
        ('Space', 'Activate the focused tab'),
    )),
    ('Keybind cheatsheet', (
        ('Ctrl+F', 'Focus and select the shortcut search text'),
        ('Up / Down', 'Select the previous or next shortcut and scroll it into view'),
        ('Enter', 'Move focus from shortcut search into its results'),
        ('Left / Right', 'Jump between sections when the shortcut list has focus'),
        ('Home / End', 'Select the first or last result when the shortcut list has focus'),
        ('Escape', 'Close the cheatsheet'),
    )),
    ('Slash commands and multiple cursors', (
        ('/', 'Open slash commands at the start of a line or after a space'),
        ('Up / Down, Enter / Tab, Escape', 'Choose a slash command, insert it, or dismiss the menu'),
        ('Escape', 'Clear extra cursors'),
    )),
    ('Vim mode', (
        ('Escape', 'Return to Normal mode when Vim keybindings are enabled'),
        ('h j k l, w b e, 0 $ ^, gg G', 'Move in Normal and Visual mode'),
        ('i a I A o O', 'Enter Insert mode'),
        ('v / V', 'Enter character or line Visual mode'),
        ('x, dd, yy, p, dw, cw', 'Delete, yank, paste, or change text'),
        ('u / Ctrl+R', 'Undo or redo'),
    )),
    ('Palette, dialogs, and focus', (
        ('Up / Down, Enter / Tab', 'Choose and insert a [[link]] completion'),
        ('Up / Down', 'Select a command in the palette'),
        ('Enter', 'Run the selected command or confirm a creation dialog'),
        ('Escape', 'Close the palette, cheatsheet, dialog, or context menu'),
        ('Tab / Shift+Tab', 'Move between controls outside the editor'),
        ('Ctrl+Tab / Ctrl+Shift+Tab', 'Move focus out of the editor, forward or backward'),
        ('Space / Enter', 'Activate a focused button'),
        ('Shift+F10 / Menu', "Open the focused control's context menu"),
    )),
)


def shortcut_label(accelerator):
    """Use readable modifier names instead of tiny macOS keyboard symbols."""
    from gi.repository import Gdk, Gtk
    if not accelerator:
        return 'Unbound'
    key, modifiers = Gtk.accelerator_parse(accelerator)
    if sys.platform != 'darwin':
        return Gtk.accelerator_get_label(key, modifiers)
    names = (
        (Gdk.ModifierType.SHIFT_MASK, 'Shift'),
        (Gdk.ModifierType.CONTROL_MASK, 'Control'),
        (Gdk.ModifierType.MOD1_MASK, 'Option'),
        (Gdk.ModifierType.SUPER_MASK, 'Super'),
        (Gdk.ModifierType.HYPER_MASK, 'Hyper'),
        (Gdk.ModifierType.META_MASK, 'Command'),
        (Gdk.ModifierType.MOD2_MASK, 'Mod2'),
        (Gdk.ModifierType.MOD3_MASK, 'Mod3'),
        (Gdk.ModifierType.MOD4_MASK, 'Mod4'),
        (Gdk.ModifierType.MOD5_MASK, 'Mod5'),
    )
    return ' + '.join([name for mask, name in names if modifiers & mask]
                      + [Gtk.accelerator_get_label(key, 0)])


def shortcut_key(keyval):
    """Normalize shifted bracket keys consistently for recording and dispatch."""
    from gi.repository import Gdk
    lowered = Gdk.keyval_to_lower(keyval)
    return {Gdk.KEY_braceleft: Gdk.KEY_bracketleft, Gdk.KEY_braceright: Gdk.KEY_bracketright}.get(lowered, lowered)


def shortcut_id(scope, shortcut):
    return scope + ':' + shortcut.handler + ':' + shortcut.accelerator


def effective_shortcuts(shortcuts, scope, overrides):
    from dataclasses import replace
    return tuple(replace(shortcut, accelerator=overrides.get(shortcut_id(scope, shortcut), shortcut.accelerator))
                 for shortcut in shortcuts if overrides.get(shortcut_id(scope, shortcut), shortcut.accelerator))


class HotkeyStore:
    def __init__(self, preferences_path):
        from pathlib import Path
        self.path = Path(preferences_path).with_name('hotkeys.json')

    def load(self):
        import json
        try:
            data = json.loads(self.path.read_text())
            return {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, bindings):
        from .workspace import _write
        _write(self.path, bindings)


def validate_hotkey(bindings, identifier, accelerator):
    from gi.repository import Gdk, Gtk
    groups = (('app', APP_SHORTCUTS), ('editor', EDITOR_SHORTCUTS))
    known = {shortcut_id(scope, shortcut) for scope, shortcuts in groups for shortcut in shortcuts}
    if identifier not in known:
        raise ValueError('Unknown command.')
    if accelerator:
        key, mask = Gtk.accelerator_parse(accelerator)
        if not key or not Gtk.accelerator_valid(key, mask) or (not mask and Gdk.keyval_to_unicode(key)):
            raise ValueError('Use a modifier key or a function key for this shortcut.')
        canonical = Gtk.accelerator_name(shortcut_key(key), mask)
        for scope, shortcuts in groups:
            for shortcut in shortcuts:
                if shortcut_id(scope, shortcut) == identifier:
                    continue
                other = bindings.get(shortcut_id(scope, shortcut), shortcut.accelerator)
                if other and (shortcut_key(Gtk.accelerator_parse(other)[0]), Gtk.accelerator_parse(other)[1]) == (shortcut_key(key), mask):
                    raise ValueError('Already assigned to: ' + shortcut.description)
        return canonical
    return ''
