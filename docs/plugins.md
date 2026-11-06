# Python plugins (experimental)

Libre Vault can load trusted single-file Python plugins that add commands to the
command palette. This is a small, versioned API for Libre Vault, **not** support
for Obsidian's JavaScript plugins. It is disabled by default.

## Install and run

1. Open **Settings → Plugins** and note the displayed plugin folder. It is a
   `plugins` directory beside the application's `settings.json`, never inside
   the open vault. Create the directory if it does not exist yet.
2. Review a script's source before copying it there. Try the bundled
   `examples/plugins/insert_timestamp.py` to insert a local timestamp.
3. Turn on **Enable trusted Python plugins** and accept the warning. This loads
   **all eligible scripts** in that folder and remembers the opt-in for future
   launches. There is no per-plugin enable switch in this first version.
4. Open a Markdown note and find **insert_timestamp: Insert timestamp** in the
   command palette. It replaces the selection, or inserts at the cursor, and
   uses the normal editor undo, dirty tracking, recovery, and autosave behavior.
5. After adding, editing, or removing scripts, use **Reload Python plugins** in
   Settings or the command palette. Reload applies to all open windows.

Common default plugin locations (the Settings page is authoritative):

- Linux: `~/.config/libre-vault/plugins`, or
  `$XDG_CONFIG_HOME/libre-vault/plugins` when configured.
- macOS: `~/Library/Application Support/libre-vault/plugins`.
- Windows: `%APPDATA%/libre-vault/plugins`.

Only direct `.py` files with Python-identifier filenames are loaded, in filename
order. Names beginning with `_`, hidden files, symlinks, and directories are
ignored. Use `my_plugin.py`, not `my-plugin.py`. Packages, sibling imports,
dependency installation, and automatic file watching are not supported. The
plugin directory is not added to Python's import path; standard-library modules
and dependencies already available to the running interpreter can be imported.

## Trust and recovery

**Plugins are not sandboxed.** Importing a script executes Python code with the
same filesystem, network, and process access as Libre Vault. The API is a
convenience, not a security boundary. Never enable scripts from an untrusted
source. Vaults and their `.obsidian` folders are never searched for code.

Turning plugins off removes their commands and calls their registered cleanup
handlers. It cannot reverse arbitrary code execution or guarantee that a plugin
stops its own threads, timers, or other side effects. Restart after disabling
misbehaving code. If a plugin prevents startup, close Libre Vault, remove its
file from the plugin folder, or set `"python_plugins": false` in `settings.json`
before restarting.

Ordinary load, command, and cleanup exceptions (including `SystemExit`) are
reported in the status bar and application logs. **Settings → Plugins** shows
loaded plugin IDs and the most recent 20 errors for that window. Reopen Settings
to refresh command-error details. One failing plugin does not stop others from
loading; failed setup discards its commands and runs already-registered cleanup
handlers. Errors do not roll back arbitrary file writes or edits a plugin made.
Infinite loops, native crashes, `os._exit`, and malicious behavior cannot be
contained by this in-process loader.

## API version 1

Every script must declare the integer `API_VERSION = 1` and define `setup(api)`:

```python
API_VERSION = 1


def setup(api):
    def uppercase_selection():
        api.replace_selection(api.get_selection().upper())

    api.register_command('uppercase', 'Uppercase selection', uppercase_selection)
```

| API | Behavior |
| --- | --- |
| `api.version` | Supported API version (`1`). |
| `api.register_command(id, title, callback)` | Register during `setup`. Callback takes no arguments. IDs are Python identifiers, unique within this plugin. Palette titles are prefixed with the plugin filename stem. |
| `api.vault_root` | Current vault's `pathlib.Path`, or `None` if no vault is open. |
| `api.current_note_path` | Current Markdown note's vault-relative path string, or `None`. |
| `api.get_text()` | Active note's current buffer text, including unsaved changes. |
| `api.get_selection()` | Active note's selected text, or an empty string. |
| `api.replace_selection(text)` | Replace the selection or insert at the cursor as one undoable edit. |
| `api.show_status(message)` | Display a non-modal status message. |
| `api.on_unload(callback)` | Register a no-argument cleanup callback, such as disconnecting a signal or removing a timer. |

Text and selection methods raise `RuntimeError` when no Markdown editor is
active (including graph/canvas tabs). They resolve the current note each time;
do not cache a vault or note at setup if your command should follow navigation.
`setup` may run before a startup vault finishes opening, so defer note access
until a command runs. Direct writes through Python bypass the editor's conflict,
undo, and save protections; prefer the editor API for the active note.

Each main window loads its own module and calls `setup` once per enable/reload.
Plugins remain loaded when switching or closing vaults. Commands and setup run
synchronously on the GTK thread: keep them short and do not block the UI.
Background tasks must marshal editor operations back to the GTK thread and
stop during cleanup. Unload callbacks run in reverse registration order on
disable, reload, failed setup, or window destruction; register cleanup as soon
as you acquire a resource. The API is inactive during/after unload, so cleanup
must release its own resources without editing notes or registering commands.
Stale command callbacks obtained before a reload/disable will not run.

This first version intentionally omits event subscriptions, custom panels,
per-plugin hotkeys, a marketplace, and dependency management. Use only the
documented API; Libre Vault's internal widgets and modules are not stable APIs.
