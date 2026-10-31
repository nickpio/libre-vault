"""Application shortcuts and the editor's supported GTK keyboard controls."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Shortcut:
    accelerator: str
    description: str
    handler: str


APP_SHORTCUTS = (
    Shortcut('<Alt>p', 'Open command palette', 'show_command_palette'),
    Shortcut('<Control>o', 'Open a vault', 'choose_vault'),
    Shortcut('<Control>n', 'Create a note', 'new_note'),
    Shortcut('<Control>s', 'Save the active note', 'save_current'),
    Shortcut('<Control>w', 'Close the active note', 'close_tab'),
    Shortcut('<Control><Shift>d', 'Toggle note details panel', 'toggle_details'),
    Shortcut('<Control><Shift>e', 'Toggle file tree', 'toggle_file_tree'),
)

KEYBIND_GROUPS = (
    ('Writing and editing', (
        ('Enter', 'Continue a list or task; exit an empty item; otherwise insert a line break'),
        ('Shift+Enter', 'Insert a line break without continuing the list'),
        ('Tab / Shift+Tab', 'Insert a tab in the editor'),
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
    ('Moving through a note', (
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
    ('Palette, dialogs, and focus', (
        ('Up / Down', 'Select a command in the palette'),
        ('Enter', 'Run the selected command or confirm a creation dialog'),
        ('Escape', 'Close the palette, cheatsheet, dialog, or context menu'),
        ('Tab / Shift+Tab', 'Move between controls outside the editor'),
        ('Ctrl+Tab / Ctrl+Shift+Tab', 'Move focus out of the editor, forward or backward'),
        ('Space / Enter', 'Activate a focused button'),
        ('Shift+F10 / Menu', "Open the focused control's context menu"),
    )),
)
