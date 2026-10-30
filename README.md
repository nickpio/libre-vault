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

Use these keyboard shortcuts:

| Shortcut | Action |
| --- | --- |
| Ctrl+O | Open a vault |
| Ctrl+N | Create a note |
| Ctrl+S | Save the active note |
| Ctrl+W | Close the active tab |

## Current behavior

Notes remain ordinary `.md` files. The app supports source editing, a basic live side-by-side Markdown preview, tabs, plain-text search, an outline, wikilinks, local Markdown links, and backlinks.

The app checks for external changes every three seconds. Clean tabs reload. Dirty tabs retain local edits, and conflicting saves stop. Closing a dirty tab or window offers Save, Discard, and Cancel.

Saves preserve a UTF-8 byte-order mark, uniform CRLF line endings, and Unix permission bits. Frontmatter stays in the source. The app leaves `.obsidian` settings untouched. Opening a vault does not create configuration files.

Search matches all whitespace-separated words against saved note paths and saved contents, without case sensitivity. Unsaved edits are not included in search or backlink indexing. Outgoing links use the active editor contents.

## Prototype limits

Start with the example vault or a copy of a small vault. This prototype has no recovery history. Save checks reduce accidental overwrites but cannot lock out unrelated programs. A narrow race remains between the final comparison and atomic replacement. Hard links, extended attributes, file ownership, and arbitrary mixed line endings are not preserved as a compatibility contract.

The preview handles basic CommonMark formatting. Images and note embeds appear as link labels. Links are followed from the sidebar, not by clicking preview text. Heading and block navigation uses a text match rather than Obsidian's full anchor rules. The file browser lists relative paths rather than expandable folders.

Symbolic links, dotfiles, and hidden folders are excluded. Non-UTF-8 files remain unchanged and report an error when opened. Link resolution is case-sensitive and reports ambiguous basenames. Alias properties are preserved but are not interpreted as link targets.

Graph, canvas, attachment rendering, property editing, live-preview editing, rename and delete operations, full search syntax, plugins, sync, mobile support, and distribution packaging remain unimplemented. The app rereads visible Markdown files on each refresh and has not been optimized for large vaults.

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
