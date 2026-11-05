# Desktop platforms

Libre Vault uses GTK 3 and Python 3.10+. Linux is tested locally. Windows and
macOS have portable storage code and source setup instructions, but native GUI
and packaged-artifact validation on those systems is still pending. Mobile
Windows devices and iOS are outside this desktop scope.

## Windows

Install [MSYS2](https://www.msys2.org/) and open its **UCRT64** terminal.
Use its Python and GTK together, rather than mixing them with a python.org
Python installation. Install dependencies with:

```sh
pacman -S --needed mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-gtk3 mingw-w64-ucrt-x86_64-python-cairo mingw-w64-ucrt-x86_64-python-markdown-it-py mingw-w64-ucrt-x86_64-python-yaml mingw-w64-ucrt-x86_64-python-matplotlib mingw-w64-ucrt-x86_64-adwaita-icon-theme
cd /c/path/to/libre-vault
python -m librevault
python -m librevault 'C:/path/to/vault'
```

This follows the [GTK Windows setup](https://www.gtk.org/docs/installations/windows/),
using GTK 3 for this application. Optional PDF previews need Poppler GLib's
introspection bindings in the same environment.

## macOS

Install [Homebrew](https://brew.sh/), then install GTK 3 and PyGObject:

```sh
brew install pygobject3 gtk+3 adwaita-icon-theme
```

Use the Homebrew Python version that `pygobject3` depends on. Create a virtual
environment with that interpreter so it can see the installed GTK bindings:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install markdown-it-py PyYAML matplotlib
.venv/bin/python -m librevault
.venv/bin/python -m librevault /path/to/vault
```

If `import gi` fails, check that `python3` is the interpreter used by Homebrew's
`pygobject3`, rather than an Apple, pyenv, or unrelated Python installation.
See [PyGObject setup](https://pygobject.gnome.org/getting_started.html).
The existing application shortcuts use Control, including on macOS.

## Storage and durability

Explicit `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_CACHE_HOME` overrides work
on all platforms. Without overrides:

| Data | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Settings | `~/.config/libre-vault` | `%APPDATA%/libre-vault` | `~/Library/Application Support/libre-vault` |
| Recovery and history | `~/.local/state/libre-vault` | `%LOCALAPPDATA%/libre-vault/state` | `~/Library/Application Support/libre-vault/state` |
| Index | `~/.cache/libre-vault` | `%LOCALAPPDATA%/libre-vault/cache` | `~/Library/Caches/libre-vault` |

Windows recovery sessions use OS byte-range locks. POSIX systems use `flock`.
Closing a session or exiting the process releases its lock. Live sessions are
excluded from crash recovery.

Saves flush file contents and replace files atomically. POSIX systems also sync
the parent directory. Windows does not expose directory fsync through Python,
so the same directory persistence guarantee after power loss is unavailable.
Windows private files inherit their directory's ACL; POSIX mode bits do not
configure Windows ACLs. Vaults moved between systems must use filenames supported
by the destination filesystem, avoiding Windows reserved names and case-only
duplicates.

## Native builds and checks

Install `requirements-build.txt` in an environment with working GTK bindings,
then run `python scripts/build.py` on the target OS. It produces
`dist/libre-vault.exe` on Windows or `dist/libre-vault` on Linux and macOS.
PyInstaller must run on the target OS and architecture. AppImage is Linux-only.
Signed Windows installers and signed/notarized macOS app bundles are not provided.
These native build paths still need artifact validation on Windows and macOS.

Run tests through `./check.sh`, using `PYTHON=/path/to/python ./check.sh` when
needed. Windows can use the MSYS2 shell. The platform CI matrix exercises native
locking and recovery on Linux, Windows, and macOS. It does not certify GTK or
frozen builds. GUI probes require a background workspace or private Xvfb display;
the runner refuses native GUI probes without one and disables native backends
for unit tests. No test should open windows on the active desktop.
