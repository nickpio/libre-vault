# Workspace views and note ownership

`Window.panes` owns one `NotePane` for each open note path. That pane owns the text buffer, undo history, autosave, recovery draft, and saved revision. `NoteMirror` presents the same buffer in a split group, sliding strip, or `NoteWindow`. A mirror never creates a second save owner.

`WorkspaceLayout.add_split(orientation)` divides the focused group, or the main tab area when it has focus. Its widget tree consists of `Gtk.Paned` branches and leaves containing either the main tabs or a `PaneGroup`. Each group owns its additional view tabs. Removing a group promotes its sibling and destroys only the removed views. Closing a canonical note removes every view of that note before destroying the buffer owner.

Workspace persistence records the recursive layout, branch ratios, group tabs, tab mode, pop-out window sizes, and sidebar dock positions. The original single-split field remains readable for existing saved workspaces. Restoring a tree requires exactly one main-tab leaf; missing notes are skipped. Note text stays in Markdown files and recovery drafts.

Separate `NotePane` instances in each pane would make independent navigation simpler, but would create competing histories, autosave timers, and disk revisions for the same note. Independent full application windows would also duplicate vault monitors and indexes. Shared views preserve one owner for these operations. The tradeoff is that GTK's insertion mark is shared between views of the same buffer.

`PathResolver` applies the same case-insensitive resolution rules in vault navigation and graph building. Exact paths retain priority; ambiguous names return multiple candidates. `VaultPaths` owns the filesystem policy used by scans, incremental updates, monitors, and attachment graph discovery. Hidden files and internal symbolic links are opt-in. Ancestor tracking prevents recursive link cycles, and resolved paths must remain inside the vault and outside app-managed folders.

Hotkey overrides are stored in `hotkeys.json` beside application preferences. Per-vault visibility and attachment settings live in `vault-settings.json` at the same location, keyed by vault root. The application and editor dispatchers use the same overrides as Settings and the cheatsheet. Settings validates a proposed binding before writing it.

Run `./check.sh discover -s tests -p test_workspace_customization.py -v` to exercise nested splits, shared buffers, note windows, tab modes, dock restoration, shortcut overrides, hover cards, case-insensitive links, and filesystem options through the real GTK and storage code.
