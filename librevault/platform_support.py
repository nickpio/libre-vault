"""OS boundaries for private storage, permissions, and recovery locks."""
import errno
import os
from pathlib import Path
import sys


def app_directory(kind):
    """Return an app directory, honoring XDG overrides on every platform."""
    override = os.environ.get(f'XDG_{kind.upper()}_HOME')
    if override:
        return Path(override) / 'libre-vault'
    home = Path.home()
    if sys.platform == 'win32':
        variable = 'APPDATA' if kind == 'config' else 'LOCALAPPDATA'
        fallback = home / 'AppData' / ('Roaming' if kind == 'config' else 'Local')
        base = Path(os.environ.get(variable) or fallback) / 'libre-vault'
        return base if kind == 'config' else base / kind
    if sys.platform == 'darwin':
        base = home / 'Library'
        if kind == 'cache':
            return base / 'Caches' / 'libre-vault'
        base = base / 'Application Support' / 'libre-vault'
        return base if kind == 'config' else base / kind
    return home / {'config': '.config', 'state': '.local/state', 'cache': '.cache'}[kind] / 'libre-vault'


def set_file_mode(handle, mode):
    """Preserve POSIX modes; Windows files inherit the parent directory ACL."""
    if sys.platform != 'win32':
        os.fchmod(handle.fileno(), mode)


def lock_session(handle, *, blocking=True):
    """Hold an exclusive lock until the handle closes, including on Windows."""
    if sys.platform == 'win32':
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(error.errno, str(error)) from error
            raise
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
