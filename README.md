# Libre Vault

An MIT-licensed native Linux prototype for editing local Markdown vaults. Libre Vault is a working foundation for an Obsidian-compatible application. It does not yet provide near-identical core features.

## Run the app

Use Python 3.10 or newer with GTK 3, PyGObject, and markdown-it-py.

On Debian or Ubuntu, install the distribution packages:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 python3-markdown-it
```

From this project folder, open the example vault:

```sh
./run.sh example-vault
```

Pass an existing vault folder to open it directly:

```sh
./run.sh /path/to/vault
```

Use **Open vault** to select another folder. Use **New note** to create a note in an existing folder. Click a note to open a tab. Follow internal links from **Outgoing links** and **Backlinks**.

The gear icon at the bottom-left opens **Settings**, with a category sidebar and live preferences:

- **Appearance:** dark or light theme, interface font size, text font family, and text size.
- **Editor:** readable line length, writing column width, line wrapping, and list continuation on Enter.

Changes apply immediately to open notes and future tabs. **Restore defaults** resets all preferences. Settings are saved in `$XDG_CONFIG_HOME/libre-vault/settings.json`, or `~/.config/libre-vault/settings.json` when that variable is unset. Vault files and `.obsidian` settings are left untouched. The gear stays accessible when either sidebar is hidden.

Both sidebars open and close with a short slide transition, preserving widths set with the splitters. Repeated toggles are ignored during a transition and its brief settling period. GTK's system animation preference is respected.

Right-click the file explorer for **New note**, **New subfolder**, and **Open vault**. Right-clicking a folder creates inside it; right-clicking a note uses its parent folder. Right-click empty space to create at the vault root. Empty folders appear in the explorer immediately. The menu is also available with Shift+F10 or the Menu key when the explorer has focus.

Notes are grouped beneath their folders with indentation for each level. Click a folder to expand or collapse it. With the explorer focused, Right expands a folder or enters it; Left collapses it or selects its parent. Opening a note reveals any collapsed parent folders.

Use these keyboard shortcuts:

| Shortcut | Action |
| --- | --- |
| Alt+P | Open the command palette |
| Ctrl+O | Open a vault |
| Ctrl+N | Create a note |
| Ctrl+S | Save the active note |
| Ctrl+W | Close the active tab |
| Ctrl+Shift+D | Show or hide the note details panel |
| Ctrl+Shift+E | Show or hide the file tree |

Press **Alt+P** to open the compact command palette near the top of the workspace. Run **Open keybind cheatsheet** to see the full keyboard reference, grouped by application, editor, file explorer, tabs, and dialogs. Type to filter commands, press Enter to run the selected command, or press Escape or click outside to dismiss the palette and return to your previous control.

The cheatsheet opens with search focused. Type a key combination, action, or section name to filter shortcuts. Use Up/Down to browse matching results while continuing to type. Press Enter to focus the list, where Left/Right jumps between matching sections and Home/End reaches the first or last result. Ctrl+F returns to search. The selected row stays in view as you navigate. Press Escape to close.

## Current behavior

Notes remain ordinary `.md` files. The app has one live Markdown editor, tabs, plain-text search, an outline, wikilinks, local Markdown links, and backlinks.

The outline indents nested headings, wraps long titles, and highlights the section containing the editor cursor. Click a heading to jump to it.

Headings, emphasis, code, quotes, and link labels format in the writing area as you type. The line containing the cursor reveals its Markdown syntax. Move to another line to hide those markers again. Frontmatter is concealed until the cursor or selection enters it; Ctrl+Home reveals it. Copy, cut, and save retain the original Markdown.

Lists display bullets or numbers with nested indentation. Task lists display checkboxes; click one to toggle it, or focus it and press Space. Completed tasks appear crossed out. Toggling a checkbox updates the Markdown in the editor; use Ctrl+S to save. List markers stay rendered while typing. Select a marker's source range to reveal it for direct Markdown editing.

Press Enter to continue a list with the same indentation and bullet style. Numbered lists advance to the next number; new tasks start unchecked. On an empty item, Enter exits the list and leaves a blank line for normal writing. Shift+Enter keeps the standard line break behavior.

The app checks for external changes every three seconds. Clean tabs reload. Dirty tabs retain local edits, and conflicting saves stop. Closing a dirty tab or window offers Save, Discard, and Cancel.

Saves preserve a UTF-8 byte-order mark, uniform CRLF line endings, and Unix permission bits. Frontmatter stays in the source. The app leaves `.obsidian` settings untouched. Opening a vault does not create configuration files.

Search matches all whitespace-separated words against saved note paths and saved contents, without case sensitivity. Unsaved edits are not included in search or backlink indexing. Outgoing links use the active editor contents.

## Prototype limits

Start with the example vault or a copy of a small vault. This prototype has no recovery history. Save checks reduce accidental overwrites but cannot lock out unrelated programs. A narrow race remains between the final comparison and atomic replacement. Hard links, extended attributes, file ownership, and arbitrary mixed line endings are not preserved as a compatibility contract.

Live formatting handles a subset of CommonMark. Images and advanced constructs retain their source syntax. Note embeds show their link labels. Links are followed from the sidebar. Heading and block navigation uses a text match rather than Obsidian's full anchor rules. Search filters saved notes, hides folder entries, and displays parent paths beside results. Folder expansion lasts until another vault is opened or the app closes.

Symbolic links, dotfiles, and hidden folders are excluded. Non-UTF-8 files remain unchanged and report an error when opened. Link resolution is case-sensitive and reports ambiguous basenames. Alias properties are preserved but are not interpreted as link targets.

Graph, canvas, attachment rendering, property editing, rename and delete operations, full search syntax, plugins, sync, mobile support, and distribution packaging remain unimplemented. The app rereads visible Markdown files on each refresh and has not been optimized for large vaults.

See [the compatibility target](docs/compatibility.md) and [the architecture decisions](docs/architecture.md).

## Verify the implementation

Run the filesystem and parser tests:

```sh
./check.sh
```

To exercise the actual GTK window without a visible desktop, install Xvfb and run:

```sh
xvfb-run -a -s '-screen 0 1600x1000x24' env GDK_BACKEND=x11 PYTHONPATH=. python3 tests/smoke_gui.py
```

The GUI test opens a temporary copy of the example vault, follows a link, edits and saves, reads the resulting file, checks external reload and conflict handling, and exercises search. It does not modify the supplied example vault.

## License

The project source is available under the [MIT license](LICENSE). Runtime dependencies retain their own licenses. The application contains independently written code and no Obsidian implementation code or assets. Obsidian is a separate product; this project is unaffiliated.
