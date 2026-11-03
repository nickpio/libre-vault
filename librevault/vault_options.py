"""Per-vault filesystem and attachment preferences, stored outside the vault."""
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .workspace import WorkspaceStore


@dataclass(frozen=True)
class VaultOptions:
    show_hidden: bool = False
    follow_symlinks: bool = False
    attachment_folder: str = 'attachments'

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        folder = data.get('attachment_folder', 'attachments')
        if not isinstance(folder, str) or '\x00' in folder or '\\' in folder or any(char in folder for char in '\r\n') or PurePosixPath(folder).is_absolute() or '..' in PurePosixPath(folder).parts:
            folder = 'attachments'
        return cls(data.get('show_hidden') is True, data.get('follow_symlinks') is True, folder.strip('/'))


class VaultOptionsStore(WorkspaceStore):
    def __init__(self, preferences_path):
        self.path = Path(preferences_path).with_name('vault-settings.json')

    def load(self, root):
        return VaultOptions.from_dict(super().load(root))

    def save(self, root, options):
        super().save(root, asdict(options))


class VaultPaths:
    def __init__(self, root, options):
        self.root, self.options = Path(root), options

    def path_allowed(self, relative):
        parts = PurePosixPath(relative).parts
        reserved = {'.git', '.obsidian', '.trash', '.librevault'}
        return not any(part in reserved or (part.startswith('.') and not self.options.show_hidden) for part in parts)

    def _visible_path(self, relative: str) -> Path:
        from .vault import VaultError
        path = PurePosixPath(relative)
        if '\x00' in relative or path.is_absolute() or not path.parts or '..' in path.parts or not self.path_allowed(relative):
            raise VaultError('Use an enabled path inside the vault.')
        current = self.root
        if current.is_symlink():
            raise VaultError('The vault folder was replaced by a symbolic link.')
        for part in path.parts:
            current = current / part
            if current.is_symlink() and not self.options.follow_symlinks:
                raise VaultError("Enable symbolic links in this vault's settings first.")
        try:
            resolved = current.resolve()
            if not resolved.is_relative_to(self.root):
                raise VaultError('Symbolic links must point inside the vault.')
            if not self.path_allowed(resolved.relative_to(self.root).as_posix()):
                raise VaultError('The symbolic link points to an excluded path.')
        except (RuntimeError, OSError) as error:
            raise VaultError('The symbolic link cannot be resolved.') from error
        return current

    def casefold_path(self, relative):
        """Find an existing local path, retaining exact component precedence."""
        from .vault import VaultError
        self._visible_path(relative)
        current = self.root
        for part in PurePosixPath(relative).parts:
            exact = current / part
            if exact.exists():
                current = exact
            else:
                matches = [path for path in current.iterdir() if path.name.casefold() == part.casefold()]
                if len(matches) != 1:
                    raise VaultError('File path is missing or ambiguous: ' + relative)
                current = matches[0]
            self._visible_path(current.relative_to(self.root).as_posix())
        return current

    def walk(self, relative=''):
        """Walk enabled paths, following internal links without ancestor cycles."""
        from .vault import VaultError
        if self.root.is_symlink():
            return
        def visit(path, ancestors):
            try:
                real = path.resolve()
                if real in ancestors:
                    return
                children, files = [], []
                for child in sorted(path.iterdir()):
                    listed = child.relative_to(self.root).as_posix()
                    try:
                        self._visible_path(listed)
                        if child.is_dir():
                            if child.resolve() not in ancestors | {real}:
                                children.append(child.name)
                        elif child.is_file():
                            files.append(child.name)
                    except (VaultError, OSError, RuntimeError):
                        continue
                yield str(path), children, files
                for name in children:
                    yield from visit(path / name, ancestors | {real})
            except (OSError, RuntimeError):
                return
        start = self._visible_path(relative) if relative else self.root
        ancestors = set()
        if relative:
            parent = start.parent
            while parent.is_relative_to(self.root):
                ancestors.add(parent.resolve())
                if parent == self.root:
                    break
                parent = parent.parent
        yield from visit(start, ancestors)

