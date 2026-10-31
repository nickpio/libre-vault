# Architecture decisions

The initial target is Linux desktop, existing Markdown vaults, and eventual parity with Obsidian's core features. Running existing Obsidian plugins is outside the first version.

## Caller contract

The UI opens a directory and uses the vault engine directly:

```python
vault = Vault('/path/to/vault')
note = vault.read('Projects/Plan.md')
saved = vault.save(note, '# Revised plan\n')
```

`Note` records its relative path, text, SHA-256 revision, and UTF-8 byte-order mark. A save compares the revision against the current disk file. A mismatch raises `Conflict` and leaves the editor's text available.

`Vault.resolve(source, target)` returns a list of matching paths. An empty list means unresolved. Multiple paths mean ambiguous. The UI asks the user to choose a candidate rather than silently picking one.

## Ownership

| Module | Owns |
| --- | --- |
| `librevault/vault.py` | Path validation, UTF-8 reads, revision checks, atomic replacement, link parsing, resolution, search, and backlinks |
| `librevault/app.py` | GTK windows, open tabs, dirty buffers, live text styles, user choices, and periodic refresh |
| `librevault/formatting.py` | Pure source-offset formatting spans, syntax ranges, and list/task structure derived from Markdown tokens |
| `librevault/editing.py` | Pure source edits for Enter continuation and exiting empty list items |
| `librevault/filetree.py` | Pure folder-first traversal of visible note paths and folders, respecting collapsed folders |
| `librevault/commands.py` | Searchable command palette overlay and grouped keybind cheatsheet dialog |
| `librevault/settings.py` | Category-based appearance/editor settings dialog, live controls, and save-error feedback |
| `librevault/preferences.py` | Preference defaults, configuration validation, and atomic storage outside the vault |
| `librevault/appearance.py` | Shared dark/light theme colors and generated typography styles |
| `librevault/panels.py` | Native sidebar reveal transitions synchronized with splitters and saved panel widths |
| `librevault/shortcuts.py` | Application shortcut registry shared by key handling and the cheatsheet, plus contextual GTK bindings |
| `tests/test_editing.py` | Exact Markdown and cursor positions for list/task keyboard actions |
| `tests/test_formatting.py` | Source offsets, nesting, escapes, literal code, and incomplete Markdown |
| `tests/test_vault.py` | Repeatable filesystem and parsing regression cases |
| `tests/smoke_gui.py` | The real GTK edit-to-save path and external-change behavior |

Disk files are authoritative. The index is disposable and rebuilt from files. Each open tab owns its unsaved editor buffer and the revision it opened. The engine has no GTK dependency. The UI uses the same parser as the index.

Application preferences live in the user's XDG configuration directory, independently of vaults. Loading missing or malformed preferences uses defaults without writing files. Changes save through atomic replacement before applying to all open windows. CSS and Markdown text tags share the same theme colors. Appearance updates change widget styles and layout without rewriting note buffers or changing dirty state. A failed settings save retains the previous preferences and reports the error in the settings dialog.

A save writes a temporary file in the note's directory, flushes it, checks the source revision again, and atomically replaces the note. The temporary file is removed after a failure. This is optimistic conflict detection, not a filesystem transaction with other editors. Directory fsync, recovery versions, and a stronger concurrent-writer design remain release prerequisites.

## Live editing

Each `NotePane` owns one `Gtk.TextBuffer` containing the original Markdown. `markdown_layout(text)` returns style spans and list items without changing the text. CommonMark block maps identify headings, literal code, and actual list items; inline rules retain source offsets before delimiter balancing resolves nested emphasis. List items record nesting depth, displayed numbering, and the source offset of a task's state character. The GTK adapter applies text tags and conceals syntax outside the cursor's line and selected ranges. Copy, cut, indexing, and save explicitly include hidden characters.

Bullets, numbers, and native `Gtk.CheckButton` controls are positioned in the text view's widget window beside the list text. They are not buffer anchors, so they add no characters to the note. List margins keep wrapped lines aligned; scrolling and resizing reposition the controls. A task toggle replaces just its state character and follows the normal dirty-buffer/save path. Controls are rebuilt when source text changes. Stale controls cannot mutate unrelated source after a pending edit.

Edits and cursor movement schedule one idle formatting pass. Source spans are cached until the text changes. Applying styles does not change the dirty state. External reloads replace the same buffer and rerun formatting, retaining the existing revision checks.

The editor handles unmodified Enter with `list_enter(text, cursor)`. This pure function parses the current source and returns one replacement range, or no edit when GTK should handle the key normally. Bullets preserve their style and indentation, ordered lists advance their displayed number, and new task markers are unchecked. Empty items exit with a blank separator to prevent subsequent prose from becoming a lazy list continuation. The adapter applies the edit as one user action and places the caret after the replacement. It does not use cached widget positions, so consecutive Enter presses work before formatting runs.

A separate rendered editing buffer would require translating every cursor offset, selection, and edit back into source Markdown. Keeping the source buffer avoids that mapping and preserves existing save and navigation contracts. Embedded notes and attachments still have no rendering widgets.

## Alternatives considered

A browser application with a local HTTP server would make HTML previews easier but add a server lifecycle and local network boundary. This prototype uses GTK and direct filesystem calls so the storage contract can be exercised in one process.

An Electron editor with CodeMirror is another candidate for the final editing experience. It offers a stronger foundation for Obsidian-like live preview, at the cost of a browser runtime and additional JavaScript dependencies. GTK was selected for the runnable Linux prototype; that choice remains provisional until a live-preview editing spike establishes whether it can meet the required experience. The independently tested vault behavior should remain the reference if the shell changes.

A database as the authoritative note store would require import and export behavior and risk diverging from ordinary vault files. Only derived indexes may use a database in later versions.

## Risks that need evidence

- No tests have compared this implementation against a running Obsidian instance. Current fixtures test documented syntax and the prototype's stated behavior.
- Full-file polling and full backlink recomputation block the GTK thread. Large-vault support needs an incremental background index and measured latency.
- Live editing now has cursor reveal, selection, clipboard, and save checks. Embedded widgets, large-document latency, and IME interactions still need evidence before committing to GTK as the final editor.
- Rename-with-link-updates spans multiple files. It needs a recovery design before implementation.
- External editors do not participate in the prototype's revision protocol. Save races need a tested preservation policy before recommending use on primary vaults.
