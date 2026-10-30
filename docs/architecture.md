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
| `librevault/app.py` | GTK windows, open tabs, dirty buffers, preview formatting, user choices, and periodic refresh |
| `tests/test_vault.py` | Repeatable filesystem and parsing regression cases |
| `tests/smoke_gui.py` | The real GTK edit-to-save path and external-change behavior |

Disk files are authoritative. The index is disposable and rebuilt from files. Each open tab owns its unsaved editor buffer and the revision it opened. The engine has no GTK dependency. The UI uses the same parser as the index.

A save writes a temporary file in the note's directory, flushes it, checks the source revision again, and atomically replaces the note. The temporary file is removed after a failure. This is optimistic conflict detection, not a filesystem transaction with other editors. Directory fsync, recovery versions, and a stronger concurrent-writer design remain release prerequisites.

## Alternatives considered

A browser application with a local HTTP server would make HTML previews easier but add a server lifecycle and local network boundary. This prototype uses GTK and direct filesystem calls so the storage contract can be exercised in one process.

An Electron editor with CodeMirror is another candidate for the final editing experience. It offers a stronger foundation for Obsidian-like live preview, at the cost of a browser runtime and additional JavaScript dependencies. GTK was selected for the runnable Linux prototype; that choice remains provisional until a live-preview editing spike establishes whether it can meet the required experience. The independently tested vault behavior should remain the reference if the shell changes.

A database as the authoritative note store would require import and export behavior and risk diverging from ordinary vault files. Only derived indexes may use a database in later versions.

## Risks that need evidence

- No tests have compared this implementation against a running Obsidian instance. Current fixtures test documented syntax and the prototype's stated behavior.
- Full-file polling and full backlink recomputation block the GTK thread. Large-vault support needs an incremental background index and measured latency.
- GTK's text preview does not implement Obsidian's live editing experience. A prototype of cursor behavior, selection, embedded widgets, and IME input is needed before committing to the final editor.
- Rename-with-link-updates spans multiple files. It needs a recovery design before implementation.
- External editors do not participate in the prototype's revision protocol. Save races need a tested preservation policy before recommending use on primary vaults.
