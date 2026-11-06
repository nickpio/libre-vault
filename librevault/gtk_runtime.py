"""Configure macOS GTK defaults before loading the UI runtime."""
import os
from pathlib import Path
import sys


def install_window_header(window, header, content):
    """Keep native macOS decorations and place app actions below the title bar."""
    if sys.platform == 'darwin':
        header.set_show_close_button(False)
        content.pack_start(header, False, False, 0)
    else:
        header.set_show_close_button(True)
        window.set_titlebar(header)


def configure_scrollbars():
    """Avoid stale GTK overlay-indicator fade callbacks on macOS."""
    if sys.platform == 'darwin':
        os.environ.setdefault('GTK_OVERLAY_SCROLLING', '0')


def configure_data_dirs():
    """Add installed Homebrew data, preserving user and bundled search order."""
    if sys.platform != 'darwin' or getattr(sys, 'frozen', False):
        return
    # Framework Python and virtualenv prefixes are below the Homebrew prefix.
    # Walk both spellings to support opt symlinks and Cellar installations.
    prefixes = []
    if os.environ.get('HOMEBREW_PREFIX'):
        prefixes.append(Path(os.environ['HOMEBREW_PREFIX']))
    base = Path(sys.base_prefix)
    prefixes.extend([base, *base.parents, base.resolve(), *base.resolve().parents])
    prefixes.extend([Path('/opt/homebrew'), Path('/usr/local')])
    directories = (os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(os.pathsep)
    for prefix in prefixes:
        share = prefix / 'share'
        if ((share / 'glib-2.0' / 'schemas' / 'gschemas.compiled').is_file()
                or (share / 'icons' / 'hicolor' / 'index.theme').is_file()):
            if str(share) not in directories:
                directories.append(str(share))
    os.environ['XDG_DATA_DIRS'] = os.pathsep.join(directories)


def require_gtk_schemas():
    """Fail with an actionable message instead of a fatal GSettings abort."""
    from gi.repository import Gio

    source = Gio.SettingsSchemaSource.get_default()
    required = ('org.gtk.Settings.FileChooser', 'org.gtk.Settings.ColorChooser')
    missing = [name for name in required if source is None or source.lookup(name, True) is None]
    if missing:
        message = 'GTK settings schemas are missing: ' + ', '.join(missing) + '.'
        if sys.platform == 'darwin':
            message += (' Reinstall the Homebrew resources with `brew reinstall gtk+3 '
                        'adwaita-icon-theme hicolor-icon-theme`, then run '
                        '`glib-compile-schemas "$(brew --prefix)/share/glib-2.0/schemas"` '
                        'and restart Libre Vault.')
        else:
            message += ' Install the GTK 3 runtime data and compiled GSettings schemas.'
        raise RuntimeError(message)
