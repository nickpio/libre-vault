"""Application shortcuts and the editor's supported GTK keyboard controls."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Shortcut:
    accelerator: str
    description: str
    handler: str


APP_SHORTCUTS = (
    Shortcut('<Control><Alt>g', 'Open graph view', 'show_graph'),
    Shortcut('<Alt>p', 'Open command palette', 'show_command_palette'),
    Shortcut('<Control>o', 'Open a vault', 'choose_vault'),
    Shortcut('<Control>n', 'Create a note', 'new_note'),
    Shortcut('<Control>s', 'Save the active note', 'save_current'),
    Shortcut('<Control>w', 'Close the active note', 'close_tab'),
    Shortcut('<Control><Shift>d', 'Toggle note details panel', 'toggle_details'),
    Shortcut('<Control><Shift>e', 'Toggle file tree', 'toggle_file_tree'),
    Shortcut('<Control><Shift>o', 'Quick switcher', 'show_quick_switcher'),
    Shortcut('<Alt>Left', 'Navigate back', 'navigate_back'),
    Shortcut('<Alt>Right', 'Navigate forward', 'navigate_forward'),
    Shortcut('<Control><Shift>s', 'Toggle split editor', 'toggle_split'),
)

EDITOR_SHORTCUTS = (
    Shortcut('<Control>z', 'Undo last edit', 'undo'),
    Shortcut('<Control><Shift>z', 'Redo last edit', 'redo'),
    Shortcut('<Control>b', 'Toggle bold', 'bold'),
    Shortcut('<Control>i', 'Toggle italic', 'italic'),
    Shortcut('<Control>k', 'Insert a Markdown link', 'insert_link'),
    Shortcut('<Control>f', 'Find in the active note', 'show_find'),
    Shortcut('<Control>h', 'Find and replace in the active note', 'show_replace'),
    Shortcut('F3', 'Find next match', 'find_next'),
    Shortcut('<Control>g', 'Find next match', 'find_next'),
    Shortcut('<Shift>F3', 'Find previous match', 'find_previous'),
    Shortcut('<Control><Shift>g', 'Find previous match', 'find_previous'),
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
