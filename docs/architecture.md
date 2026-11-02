# Architecture decisions

The initial target is Linux desktop, existing Markdown vaults, and eventual parity with Obsidian's core features. Running existing Obsidian plugins is outside the first version.

## Startup and distribution

The vault argument is optional. First launch shows the welcome screen. Later launches reopen the last successfully opened vault, recorded in settings-adjacent `session.json`. An explicit folder argument takes precedence. Missing or unavailable remembered folders leave the welcome screen available without a modal error. Choosing a folder switches the same window into its workspace. Close vault confirms unsaved notes, persists the workspace, cancels its watchers and callbacks, disposes its editors, and clears the remembered startup vault. The GTK application handles activation separately from file-open events, so each launch receives its own optional folder argument even when another instance is running.

`python3 -m librevault` is the application entry point. `run.sh` adds the source directory to Python's import path without changing the caller's working directory. Assets resolve relative to the package and preferences use the user's XDG configuration directory. AppImage is being considered for distribution; its launcher should retain these startup and storage contracts so the bundle can remain read-only and a vault path is never required.

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
| `librevault/monitor.py` | Recursive Gio directory watches, coalesced filesystem events, and watch cleanup |
| `librevault/app.py` | GTK windows, open tabs, dirty buffers, live text styles, user choices, and event-driven refresh |
| `librevault/formatting.py` | Pure source-offset formatting spans, syntax ranges, and list/task structure derived from Markdown tokens |
| `librevault/editing.py` | Pure source edits for list continuation, emphasis toggles, link construction, and literal match offsets |
| `librevault/history.py` | Per-buffer grouped source deltas, typing coalescence, selection restoration, and bounded undo/redo stacks |
| `librevault/findbar.py` | Active-note search highlights, navigation, and grouped replacements on original Markdown |
| `librevault/filetree.py` | Pure folder-first traversal of visible note paths and folders, respecting collapsed folders |
| `librevault/commands.py` | Searchable command palette overlay and grouped keybind cheatsheet dialog |
| `librevault/settings.py` | Category-based appearance/editor settings dialog, live controls, and save-error feedback |
| `librevault/preferences.py` | Preference defaults, configuration validation, and atomic storage outside the vault |
| `librevault/appearance.py` | Shared dark/light theme colors and generated typography styles |
| `librevault/panels.py` | Native sidebar reveal transitions synchronized with splitters and saved panel widths |
| `librevault/organization.py` | Move/trash validation, revision checks, link target rewrites, and rollback |
| `librevault/metadata.py` | Safe YAML properties, inline tags, aliases, search filters, and snippet offsets |
| `librevault/navigation.py` | Fuzzy path/alias ranking and bounded back/forward locations |
| `librevault/navigation_ui.py` | Quick switcher, wikilink completion, and Ctrl-click link navigation |
| `librevault/splitview.py` | Secondary shared-buffer editor with mirrored list and task controls |
| `librevault/workspace.py` | Atomic per-vault tab, cursor, scroll, folder, and split state outside vaults |
| `librevault/attachments.py` | Collision-safe attachment copies and confined local asset resolution |
| `librevault/reading.py` | Native Markdown reading widgets, local note/media embeds, mathtext, and optional Poppler PDF pages |
| `librevault/shortcuts.py` | Application shortcut registry shared by key handling and the cheatsheet, plus contextual GTK bindings |
| `tests/test_editing.py` | Exact Markdown and cursor positions for list/task keyboard actions |
| `tests/test_formatting.py` | Source offsets, nesting, escapes, literal code, and incomplete Markdown |
| `tests/test_vault.py` | Repeatable filesystem and parsing regression cases |
| `tests/smoke_gui.py` | The real GTK edit-to-save path and external-change behavior |

Disk files are authoritative. The index is disposable and rebuilt from files. Each open note owns its unsaved editor buffer and the revision it opened. Split views share that buffer and its edit history. The engine has no GTK dependency. The UI uses the same parser as the index.

Application preferences live in the user's XDG configuration directory, independently of vaults. Loading missing or malformed preferences uses defaults without writing files. Changes save through atomic replacement before applying to all open windows. CSS and Markdown text tags share the same theme colors. Appearance updates change widget styles and layout without rewriting note buffers or changing dirty state. A failed settings save retains the previous preferences and reports the error in the settings dialog.

A save writes a temporary file in the note's directory, flushes it, checks the source revision again, and atomically replaces the note. The temporary file is removed after a failure. This is optimistic conflict detection, not a filesystem transaction with other editors. Directory fsync, recovery versions, and a stronger concurrent-writer design remain release prerequisites.

## Live editing

Each `NotePane` owns one `Gtk.TextBuffer` containing the original Markdown. `markdown_layout(text)` returns style spans and list items without changing the text. CommonMark block maps identify headings, literal code, and actual list items; inline rules retain source offsets before delimiter balancing resolves nested emphasis. List items record nesting depth, displayed numbering, and the source offset of a task's state character. The GTK adapter applies text tags and conceals syntax outside the cursor's line and selected ranges. Copy, cut, indexing, and save explicitly include hidden characters.

Bullets, numbers, and native `Gtk.CheckButton` controls are positioned in the text view's widget window beside the list text. They are not buffer anchors, so they add no characters to the note. List margins keep wrapped lines aligned; scrolling and resizing reposition the controls. A task toggle replaces just its state character and follows the normal dirty-buffer/save path. Controls are rebuilt when source text changes. Stale controls cannot mutate unrelated source after a pending edit.

Edits and cursor movement schedule one idle formatting pass. Source spans are cached until the text changes. Applying styles does not change the dirty state. External reloads replace the same buffer and rerun formatting, retaining the existing revision checks.

The editor handles unmodified Enter with `list_enter(text, cursor)`. This pure function parses the current source and returns one replacement range, or no edit when GTK should handle the key normally. Bullets preserve their style and indentation, ordered lists advance their displayed number, and new task markers are unchecked. Empty items exit with a blank separator to prevent subsequent prose from becoming a lazy list continuation. The adapter applies the edit as one user action and places the caret after the replacement. It does not use cached widget positions, so consecutive Enter presses work before formatting runs.

Each `NotePane` owns a `BufferHistory`. Insert/delete signals record character-offset source deltas and before/after selections. GTK user-action boundaries make compound edits atomic for undo; adjacent typing and deletion coalesce for up to one second, ending at whitespace boundaries or cursor movement. History holds at most 500 actions and excludes text tags. Replay uses buffer edits without recording itself. Dirty state compares the current source with the saved note, so undo/redo can cross a save checkpoint without changing its disk revision. External reload suspends recording and clears history before adopting the new source.

The in-note find bar searches original source, including invisible characters. Literal matches use source offsets rather than offsets in rendered text. Replacements modify those ranges; Replace All walks matches backwards inside one user action. Search highlights use a separate text tag and never enter edit history. Editor shortcuts are scoped to the editor; find-field keyboard controls are routed before GTK entry defaults so Escape closes the bar and text-entry editing cannot format the note.

A separate rendered editing buffer would require translating every cursor offset, selection, and edit back into source Markdown. Keeping the source buffer avoids that mapping and preserves existing save and navigation contracts. Source mode removes concealed syntax, while Reading builds separate widgets from the same original text. Reading widgets never modify the source or enter undo history. Local images and PDF pages are scaled, note transclusion is bounded and detects cycles, and remote images are not fetched. Math uses Matplotlib mathtext, with source and an error message when an expression is unsupported.

Split editors share a canonical note's `Gtk.TextBuffer`, so changes use the same revision, autosave, and undo history. Workspace state stores paths and positions, not draft text, in a settings-adjacent `workspaces.json`. Restoring a vault ignores missing notes and malformed positions. Last-vault startup state lives separately in `session.json`, so closing a vault preserves its workspace while returning future launches to welcome.

Organization resolves link targets against the old index before computing their new paths. The move also checks links that could be shadowed by a new local filename. Before rewriting links, it saves original bytes and a move manifest under `.trash/.librevault-moves`. Successful moves and complete rollbacks remove their backup. Interrupted moves or incomplete rollbacks leave it available for manual recovery. The UI confirms dirty buffers before changing files and updates open pane paths, recovery drafts, and navigation history afterward. Search and backlinks index saved files; property edits use the normal undoable source-edit path before autosave updates that index.

## Alternatives considered

A browser application with a local HTTP server would make HTML previews easier but add a server lifecycle and local network boundary. This prototype uses GTK and direct filesystem calls so the storage contract can be exercised in one process.

An Electron editor with CodeMirror is another candidate for the final editing experience. It offers a stronger foundation for Obsidian-like live preview, at the cost of a browser runtime and additional JavaScript dependencies. GTK was selected for the runnable Linux prototype; that choice remains provisional until a live-preview editing spike establishes whether it can meet the required experience. The independently tested vault behavior should remain the reference if the shell changes.

A database as the authoritative note store would require import and export behavior and risk diverging from ordinary vault files. Only derived indexes may use a database in later versions.

## Risks that need evidence

- No tests have compared this implementation against a running Obsidian instance. Current fixtures test documented syntax and the prototype's stated behavior.
- Filesystem events reconcile only changed files or subtrees. Index updates, initial/explicit scans, and backlink recomputation still run on the GTK thread. Large-vault support needs measured latency and background work.
- Live editing now has cursor reveal, selection, clipboard, and save checks. Embedded widgets, large-document latency, and IME interactions still need evidence before committing to GTK as the final editor.
- Rename-with-link-updates spans multiple files. Revision checks and rollback handle ordinary failures; multi-file moves are not crash-atomic.
- External editors do not participate in the prototype's revision protocol. Save races need a tested preservation policy before recommending use on primary vaults.

## Filesystem monitoring

Each open vault owns Gio directory monitors for its visible, non-symlink directories and a parent watch for root replacement. Notifications are coalesced for 100 ms, then reconciled against current disk state. File changes read only the affected Markdown notes; directory changes scan that subtree and reinstall its watches. Both sides of moves are reconciled. Non-Markdown events also refresh reading dependencies, including attachments. Buffer changes independently queue an idle reading refresh so unsaved transclusions remain current.

An initial reconciliation after watch installation closes the scan/watch startup gap. Explicit `Window.refresh()` and `Vault.refresh()` remain full rescans for callers that need them, including organization operations. Switching vaults or destroying the window disconnects and cancels watches and pending callbacks. Watch-installation failures appear in the status bar; reopening the vault retries them. There is no periodic polling fallback. Gio backend support and OS watch limits therefore constrain live monitoring; unsupported filesystems or dropped notifications may require an explicit rescan or reopening the vault.
