<div align="center">

```text
╭────────────────────────────────────────────────────────────────────────────────╮
│                                                                                │
│  ██╗     ██╗██████╗ ██████╗ ███████╗  ██╗   ██╗ █████╗ ██╗   ██╗██╗  ████████╗ │
│  ██║     ██║██╔══██╗██╔══██╗██╔════╝  ██║   ██║██╔══██╗██║   ██║██║  ╚══██╔══╝ │
│  ██║     ██║██████╔╝██████╔╝█████╗    ██║   ██║███████║██║   ██║██║     ██║    │
│  ██║     ██║██╔══██╗██╔══██╗██╔══╝    ╚██╗ ██╔╝██╔══██║██║   ██║██║     ██║    │
│  ███████╗██║██████╔╝██║  ██║███████╗   ╚████╔╝ ██║  ██║╚██████╔╝███████╗██║    │
│  ╚══════╝╚═╝╚═════╝ ╚═╝  ╚═╝╚══════╝    ╚═══╝  ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝    │
│                                                                                │
│           LOCAL MARKDOWN  ·  NATIVE GTK  ·  YOUR NOTES, YOUR FILES             │
╰────────────────────────────────────────────────────────────────────────────────╯
```

**A free desktop editor for local Markdown vaults.**<br>
Ordinary files and folders. No accounts, no lock-in.

[![License: MIT](https://img.shields.io/badge/license-MIT-0f766e?style=for-the-badge)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](requirements.txt)
[![GTK 3](https://img.shields.io/badge/GTK-3-4A86CF?style=for-the-badge&logo=gtk&logoColor=white)](docs/platforms.md)
[![Status: Prototype](https://img.shields.io/badge/status-prototype-f97316?style=for-the-badge)](docs/compatibility.md)

[![Platform checks](https://img.shields.io/github/actions/workflow/status/nickpio/libre-vault/platforms.yml?style=flat-square&label=platform%20checks)](https://github.com/nickpio/libre-vault/actions/workflows/platforms.yml)
[![Last commit](https://img.shields.io/github/last-commit/nickpio/libre-vault?style=flat-square&color=6e40c9)](https://github.com/nickpio/libre-vault/commits)
[![Issues](https://img.shields.io/github/issues/nickpio/libre-vault?style=flat-square&color=6e40c9)](https://github.com/nickpio/libre-vault/issues)

[Features](#features) · [Installation](#installation) · [Usage](#usage) · [Documentation](#documentation)

</div>

> [!IMPORTANT]
> Libre Vault is a working prototype, not a verified Obsidian replacement. Start with the example vault or a copy of your notes. See the [compatibility target and limits](docs/compatibility.md).

## Features

- Edit in Live preview, Source, or Reading mode, with table editing, folding, and optional Vim bindings.
- Connect notes with wikilinks, backlinks, aliases, and global or local graphs.
- Search content, paths, tags, and properties; navigate with a quick switcher and command palette.
- Preview local attachments, embedded notes, callouts, math, and supported Mermaid diagrams.
- Organize work with tabs, split panes, canvas, templates, daily notes, and bookmarks.
- Autosave with revision checks, recovery drafts, and saved-version history. Notes stay Markdown; `.obsidian` is left untouched.

## Installation

Requires **Python 3.10+**, **GTK 3**, and the packages in [`requirements.txt`](requirements.txt). Linux is the validated platform; see [Windows and macOS setup](docs/platforms.md) for native dependencies and validation limits.

Install the runtime dependencies for your Linux distribution:

```sh
# Debian / Ubuntu
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-markdown-it python3-yaml python3-matplotlib gir1.2-poppler-0.18

# Arch Linux
sudo pacman -S python-gobject python-cairo gtk3 python-markdown-it-py python-yaml python-matplotlib poppler-glib

# Fedora
sudo dnf install python3-gobject gtk3 python3-markdown-it-py python3-pyyaml python3-matplotlib poppler-glib
```

Poppler is optional and enables PDF previews. GTK and its introspection bindings must come from your system package manager, even when installing Python packages with pip.

Clone and run:

```sh
git clone https://github.com/nickpio/libre-vault.git
cd libre-vault
./run.sh example-vault
```

Run `./run.sh /path/to/vault` to open your own folder, or `./run.sh` for the welcome screen or last-opened vault. The Python entry point is `python3 -m librevault [vault]`.

For a standalone executable or Linux AppImage, see [Building](docs/building.md).

## Usage

| Action | Shortcut |
| --- | --- |
| Open a vault | `Ctrl+O` |
| Create a note | `Ctrl+N` |
| Save | `Ctrl+S` |
| Find a note | `Ctrl+Shift+O` |
| Command palette | `Alt+P` |
| Find / replace | `Ctrl+F` / `Ctrl+H` |
| Toggle split panes | `Ctrl+Shift+S` |
| Open graph | `Ctrl+Alt+G` |

Type `[[` for link suggestions. Click rendered links to follow them; use `Ctrl+click` in Source mode. Right-click the explorer to create, rename, move, or trash notes and folders. Use **Close vault** in the command palette to return to welcome.

Open **Keybind cheatsheet** from the command palette for all shortcuts, or customize bindings in Settings. Shortcuts use Control on macOS too.

## Documentation

- [Desktop platforms](docs/platforms.md) — setup, storage locations, and platform caveats.
- [Building](docs/building.md) — standalone executables, AppImages, and build checks.
- [Compatibility](docs/compatibility.md) — supported formats, feature coverage, and acceptance gates.
- [Architecture](docs/architecture.md) — implementation, saves, indexing, and recovery.
- [Python plugins](docs/plugins.md) — experimental, opt-in commands from trusted scripts; no sandbox.
- [Roadmap](docs/roadmap.md) — remaining compatibility and release work.

## Development

Run from the checkout; restart after Python or CSS changes. No build is needed.

```sh
./check.sh                       # full test suite
./check.sh --smoke               # GUI smoke test
./check.sh --script probe.py     # standalone GUI probe
```

Always use `check.sh`: it isolates GUI windows on a background Hyprland workspace or private Xvfb display and uses temporary settings and recovery directories. If neither display option is available, GUI checks cannot run. Do not run GUI tests on the active desktop.

Issues and pull requests are welcome. Run the tests before submitting changes, preserve unsupported Markdown and properties, and update the relevant docs when behavior changes.

## License

[MIT](LICENSE). Dependencies retain their own licenses. Libre Vault is independently written and is not affiliated with Obsidian.

```text
(o) ─────────── your notes, your files, your machine ─────────── (o)
```
