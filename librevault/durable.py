"""Publish a complete file, then make its directory entry durable.

Atomic replacement and persistence across power loss are different steps.
``os.replace`` publishes a complete file under the destination name, so a
crash leaves either the previous file or the new one. It does not make that
directory update durable. The following directory fsync does.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


class AtomicReplacementError(OSError):
    """``os.replace`` failed. The destination name is unchanged."""


class DirectorySyncError(OSError):
    """The destination already names the new file, and its directory was not synced.

    Readers can see the new file. A power loss can still restore the previous
    directory entry, so the replacement is not persistent yet.
    """


def sync_file(descriptor):
    """Flush file bytes and inode metadata to stable storage."""
    os.fsync(descriptor)


def replace_file(temporary, target):
    """Atomically publish ``temporary`` as ``target`` on the same filesystem."""
    os.replace(temporary, os.fspath(target))


def sync_directory(directory):
    """Flush a directory so its entries survive power loss."""
    descriptor = os.open(os.fspath(directory), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_synced_temporary(directory, write, *, prefix, binary=True):
    """Create a temporary sibling, let ``write(handle)`` fill it, and fsync it.

    The returned path is in ``directory``. Failures remove it and leave any
    destination file untouched. They raise the original ``OSError``.
    """
    descriptor, temporary = tempfile.mkstemp(prefix=prefix, dir=os.fspath(directory))
    finished = False
    try:
        options = {} if binary else {'encoding': 'utf-8'}
        with os.fdopen(descriptor, 'wb' if binary else 'w', **options) as handle:
            write(handle)
            handle.flush()
            sync_file(handle.fileno())
        finished = True
        return temporary
    finally:
        if not finished and os.path.exists(temporary):
            os.unlink(temporary)


def replace_durably(temporary, target):
    """Replace ``target`` with a fsynced temporary file, then fsync its directory.

    ``AtomicReplacementError`` means the destination still has its previous
    file. ``DirectorySyncError`` means the new file is already visible at
    ``target``. ``temporary`` must live in the destination directory.
    """
    try:
        replace_file(temporary, target)
    except OSError as error:
        raise AtomicReplacementError(f'Could not replace {target}: {error}') from error
    try:
        sync_directory(Path(target).parent)
    except OSError as error:
        raise DirectorySyncError(
            f'Replaced {target}, but could not sync its directory: {error}') from error


@contextmanager
def synced_temporary(directory, write, *, prefix, binary=True):
    """Yield a fsynced temporary path and remove it unless it was published."""
    temporary = write_synced_temporary(directory, write, prefix=prefix, binary=binary)
    try:
        yield temporary
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
