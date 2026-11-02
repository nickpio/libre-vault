"""Confined, collision-safe attachment storage and local embed resolution."""
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, unquote, urlsplit

from .vault import VaultError


def local_path(vault, source, target):
    """Resolve a local asset without allowing symlinks or vault escapes."""
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise VaultError('Only attachments inside this vault can be displayed.')
    relative = unquote(parsed.path).replace('\\', '/')
    if not relative:
        raise VaultError('Choose a file inside this vault.')
    candidates = [PurePosixPath(source).parent / relative, PurePosixPath(relative)]
    for candidate in candidates:
        # Normalize relative links, then validate every actual filesystem part.
        import posixpath
        normalized = posixpath.normpath(str(candidate))
        try:
            path = vault._visible_path(normalized)
        except VaultError:
            continue
        if path.is_file():
            return path
    raise VaultError(f'Attachment not found: {relative}')


def store_attachment(vault, name, data):
    """Reuse matching attachment bytes or copy them without overwriting."""
    name = Path(name).name
    name = re.sub(r'[\x00-\x1f/\\]', '_', name).lstrip('.') or 'attachment'
    directory = vault._visible_path('attachments')
    directory.mkdir(exist_ok=True)
    if not directory.is_dir():
        raise VaultError('The attachments path is not a folder.')
    original = Path(name)
    # Compare only the same extension so the reused file keeps its display and
    # open behavior. Skip symlinks and unreadable entries in the vault.
    for candidate in sorted(directory.iterdir()):
        if candidate.name.startswith('.') or candidate.suffix.lower() != original.suffix.lower():
            continue
        try:
            path = vault._visible_path('attachments/' + candidate.name)
            if not path.is_file() or path.stat().st_size != len(data):
                continue
            with path.open('rb') as handle:
                offset = 0
                while offset < len(data):
                    chunk = handle.read(min(65536, len(data) - offset))
                    if not chunk or chunk != data[offset:offset + len(chunk)]:
                        break
                    offset += len(chunk)
                if offset == len(data) and not handle.read(1):
                    return path.relative_to(vault.root).as_posix()
        except (OSError, VaultError):
            continue
    for number in range(10000):
        filename = name if number == 0 else f'{original.stem} {number}{original.suffix}'
        path = vault._visible_path('attachments/' + filename)
        try:
            with path.open('xb') as handle:
                handle.write(data)
            return path.relative_to(vault.root).as_posix()
        except FileExistsError:
            continue
    raise VaultError('Too many attachments have this name.')


def attachment_markdown(relative):
    image = Path(relative).suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg')
    label = Path(relative).name.replace(']', '\\]')
    return ('!' if image else '') + f'[{label}]({quote(relative, safe="/")})'


def open_attachment(window, source, target):
    """Handle known local non-note links before unresolved-note creation."""
    try:
        path = local_path(window.vault, source, target)
    except VaultError:
        return False
    if path.suffix.lower() == '.md':
        return False
    from gi.repository import Gio
    window.try_action(lambda: Gio.AppInfo.launch_default_for_uri(path.as_uri(), None))
    return True
