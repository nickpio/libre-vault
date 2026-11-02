# Libre Vault

An MIT-licensed native Linux prototype for editing local Markdown vaults. Libre Vault is a working foundation for an Obsidian-compatible application. It does not yet provide near-identical core features.

## Run the app

Use Python 3.10 or newer with GTK 3, PyGObject, markdown-it-py, PyYAML, and Matplotlib.

On Debian or Ubuntu, install the distribution packages:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 python3-markdown-it python3-yaml python3-matplotlib gir1.2-poppler-0.18
```

Launch the app without opening a vault:

```sh
./run.sh
```

First launch shows the welcome screen with **Open vault**. Press Ctrl+O or choose that button to select a vault or an empty folder. Later launches reopen the last opened vault. If its folder is unavailable, the welcome screen remains available. Settings and the command palette also work before a vault is opened.

Use **Close vault** in the explorer's right-click menu or the command palette to return to welcome. Unsaved notes offer Save, Discard, and Cancel. Closing a vault clears the startup choice, so the next launch stays on welcome until another vault is opened.

To open the example vault directly:

```sh
./run.sh example-vault
```

Pass an existing vault folder to open it directly:

```sh
./run.sh /path/to/vault
```

The launcher also works from outside the project folder and resolves relative vault paths from your current directory. The Python entry point is `python3 -m librevault`, with the same optional vault argument.

Use **Open vault** to select another folder. Use **New note** to create a note in an existing folder. Click a note to open a tab. Follow internal links from **Outgoing links** and **Backlinks**.

The gear icon at the bottom-left opens **Settings**, with a category sidebar and live preferences:

- **Appearance:** Dark, Light, or Lights Out theme, interface font size, text font family, and text size. Lights Out uses pure-black surfaces with white text and brighter controls for OLED displays.
- **Editor:** readable line length, writing column width, line wrapping, and list continuation on Enter.

Changes apply immediately to open notes and future tabs. **Restore defaults** resets all preferences. Settings are saved in `$XDG_CONFIG_HOME/libre-vault/settings.json`, or `~/.config/libre-vault/settings.json` when that variable is unset. Vault files and `.obsidian` settings are left untouched. The gear stays accessible when either sidebar is hidden.

Both sidebars open and close with a short slide transition, preserving widths set with the splitters. Repeated toggles are ignored during a transition and its brief settling period. GTK's system animation preference is respected.

Right-click the file explorer for **New note**, **New subfolder**, and **Open vault**. Right-clicking a folder creates inside it; right-clicking a note uses its parent folder. Right-click empty space to create at the vault root. Empty folders appear in the explorer immediately. The menu is also available with Shift+F10 or the Menu key when the explorer has focus.

The same menu offers **Rename**, **Move**, and **Move to trash** for notes and folders. Rename and move update resolved incoming links and relative links inside moved notes, including local attachments. Unsaved editors offer Save, Discard, and Cancel before files change. Trashed items remain in the vault's hidden `.trash` folder and can be moved back with your file manager.

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
| Ctrl+Shift+O | Open the fuzzy note switcher |
| Alt+Left / Alt+Right | Navigate back / forward |
| Ctrl+Shift+S | Open or close a split editor |

Press **Alt+P** to open the compact command palette near the top of the workspace. Run **Open keybind cheatsheet** to see the full keyboard reference, grouped by application, editor, file explorer, tabs, and dialogs. Type to filter commands, press Enter to run the selected command, or press Escape or click outside to dismiss the palette and return to your previous control.

The cheatsheet opens with search focused. Type a key combination, action, or section name to filter shortcuts. Use Up/Down to browse matching results while continuing to type. Press Enter to focus the list, where Left/Right jumps between matching sections and Home/End reaches the first or last result. Ctrl+F returns to search. The selected row stays in view as you navigate. Press Escape to close.

The quick switcher matches note paths and frontmatter aliases. Type `[[` in an editor for note suggestions, then use Up/Down and Enter or Tab to complete the link. Ctrl-click an editor link to follow it. Following an unresolved note link offers to create the note. The command palette also exposes navigation, note creation, saving, settings, and panel controls.

Opening a vault restores its tabs, cursor and scroll positions, collapsed folders, and split editor. A split shares the original note's source buffer and undo history. Keep one note in the split while selecting another tab to compare them. Workspace state is saved beside application settings in `workspaces.json`; `session.json` remembers which vault to reopen. An explicit vault argument takes precedence over the remembered vault.

These shortcuts apply when the note editor has focus:

| Shortcut | Action |
| --- | --- |
| Ctrl+Z | Undo |
| Ctrl+Shift+Z | Redo |
| Ctrl+B | Toggle bold on selected text, or insert paired bold markers |
| Ctrl+I | Toggle italics on selected text, or insert paired italic markers |
| Ctrl+K | Insert a Markdown link to a URL or note path |
| Ctrl+F | Find in the active note |
| Ctrl+H | Find and replace in the active note |
| F3 or Ctrl+G | Next match |
| Shift+F3 or Ctrl+Shift+G | Previous match |

Right-click the editor for the same editing commands. Find searches the complete Markdown source, including concealed syntax and frontmatter. Searches are literal and case-insensitive by default; use **Aa** to match case. Enter in the find field advances to the next match, Shift+Enter goes back, and Escape closes the bar. Replace All applies to the active note and undoes as one action.

Each tab keeps up to 500 undo actions within an estimated 8 MiB memory budget. Oldest actions are discarded as needed; an edit larger than the budget clears undo history and cannot itself be undone. Adjacent typing and deletion are grouped; task toggles, list continuation, formatting, and replacements form individual actions. Undo restores the cursor and selection. Saving preserves the history, and returning to the saved text clears the unsaved indicator. Reloading a clean note after an external change clears that tab's history.

## Current behavior

Notes remain ordinary `.md` files. Each note has **Live preview**, **Source**, and **Reading** modes in its toolbar. Source exposes every Markdown character. Reading renders tables, callouts, footnotes, math, images, PDF pages, and embedded notes without changing the source.

Use the attachment button to copy a file into the vault's `attachments` folder and insert its link. Pasting a screenshot or dropping local files into the editor uses the same storage. Existing files are preserved by choosing a new filename when necessary. Undo removes inserted Markdown while keeping the copied attachment. Reading displays local images and `![[note]]` embeds. PDFs embedded with `![[file.pdf]]` have page controls when Poppler is installed; an Open button uses the system viewer as a fallback. Remote images are not fetched.

The outline indents nested headings, wraps long titles, and highlights the section containing the editor cursor. Click a heading to jump to it.

Headings, emphasis, code, quotes, and link labels format in the writing area as you type. The line containing the cursor reveals its Markdown syntax. Move to another line to hide those markers again. Frontmatter is concealed until the cursor or selection enters it; Ctrl+Home reveals it. Copy, cut, and save retain the original Markdown.

Lists display bullets or numbers with nested indentation. Task lists display checkboxes; click one to toggle it, or focus it and press Space. Completed tasks appear crossed out. Toggling a checkbox updates the Markdown in the editor and schedules an autosave. List markers stay rendered while typing. Select a marker's source range to reveal it for direct Markdown editing.

Press Enter to continue a list with the same indentation and bullet style. Numbered lists advance to the next number; new tasks start unchecked. On an empty item, Enter exits the list and leaves a blank line for normal writing. Shift+Enter keeps the standard line break behavior.

Edits autosave after two seconds of inactivity. Ctrl+S saves immediately. While a note has unsaved edits, the app keeps a private recovery draft under `$XDG_STATE_HOME/libre-vault/recovery`, or `~/.local/state/libre-vault/recovery` when that variable is unset. After an interrupted session, reopening the vault offers to recover or discard each draft. A recovered draft retains its original file revision, so an external edit or deletion prevents autosave from overwriting it. Recovery drafts contain note text; protect your user account and backups accordingly.

The app checks for external changes every three seconds. Clean tabs reload. Dirty tabs retain local edits, and conflicting saves stop. Closing a dirty tab or window offers Save, Discard, and Cancel.

Saves preserve a UTF-8 byte-order mark, uniform CRLF line endings, and Unix permission bits. Frontmatter stays in the source. The app leaves `.obsidian` settings untouched. Opening a vault does not create configuration files.

A rebuildable SQLite cache stores note content, metadata and links under `$XDG_CACHE_HOME/libre-vault` or `~/.cache/libre-vault`, outside the vault. Unopened notes do not retain their full text in memory; loaded saved-note text uses a 4 MiB cache. Delete the cache with the app closed to rebuild it on the next launch.

Reading mode limits each render to 32 embedded files and 4 MiB of embedded note source. Open files separately when a preview reaches the limit.

Search shows saved-note snippets with highlighted matches. Plain words match paths and contents without case sensitivity. Quoted phrases, `path:`, `file:`, `content:`, `tag:`, `alias:`, and `property:` filters narrow results. Prefix a term with `-` to exclude it. For example, `tag:work property:status=done -path:Archive` finds completed work outside Archive. `[status]` matches notes with that property. Unsaved edits are not included in search or backlink indexing. Outgoing links use the active editor contents.

The details panel displays tags, aliases, and properties. **Edit properties** edits YAML frontmatter as one undoable action. Tags come from frontmatter lists or inline `#tags`, including nested tags. Aliases participate in link resolution and the quick switcher; ambiguous names still require a choice.

## Prototype limits

Start with the example vault or a copy of a small vault. Recovery keeps the latest unsaved draft, not a history of saved versions. Save checks reduce accidental overwrites but cannot lock out unrelated programs. A narrow race remains between the final comparison and atomic replacement. Hard links, extended attributes, file ownership, and arbitrary mixed line endings are not preserved as a compatibility contract.

Live formatting handles a subset of CommonMark; richer media and block rendering use Reading mode. Math uses Matplotlib's mathtext subset rather than a full LaTeX engine. Unsupported expressions display their source and a rendering message. Note embeds show whole notes; section and block transclusion remain incomplete. Heading and block navigation uses a text match rather than Obsidian's full anchor rules. Search supports the documented filters above, rather than every Obsidian query operator.

Symbolic links, dotfiles, and hidden folders are excluded from browsing. Non-UTF-8 files remain unchanged and report an error when opened. Link resolution is case-sensitive and reports ambiguous basenames and aliases.

Graph, canvas, unlinked mentions, saved-version history, full search syntax, plugins, sync, mobile support, and distribution packaging remain unimplemented. Filesystem notifications update changed notes and directory subtrees without periodic full-vault reads. Initial indexing, explicit rescans, and multi-file organization operations still scan the vault; large-vault latency has not been optimized. Multi-file moves use revision checks and rollback after ordinary failures; they are not a crash-atomic filesystem transaction. Interrupted moves retain original rewritten-note bytes and a manifest under `.trash/.librevault-moves` for manual recovery.

See [the compatibility target](docs/compatibility.md) and [the architecture decisions](docs/architecture.md).

## Verify the implementation

Run the test suite:

```sh
./check.sh
```

On Hyprland, GUI checks open silently on the named `librevault-tests` workspace and do not take focus. This includes dialogs and child-process probes. The rule applies only to the current test run and is disabled afterward. On other desktops, install Xvfb to run GUI checks on a private display. If neither option is available, the runner refuses to open GUI windows on your desktop unless you explicitly use `--foreground`. Without a display, existing GUI tests are skipped.

Run the GUI smoke check with the same background routing:

```sh
./check.sh --smoke
```

Use `./check.sh --foreground` for interactive debugging. To run a standalone Python GUI probe in the background, use `./check.sh --script /path/to/probe.py`. Test runs use temporary configuration and recovery directories, leaving your real settings and last-opened vault untouched.

The GUI test opens a temporary copy of the example vault, follows a link, edits, exercises undo/redo and find/replace, saves, reads the resulting file, checks external reload and conflict handling, and exercises search. It does not modify the supplied example vault.

## License

The project source is available under the [MIT license](LICENSE). Runtime dependencies retain their own licenses. The application contains independently written code and no Obsidian implementation code or assets. Obsidian is a separate product; this project is unaffiliated.
