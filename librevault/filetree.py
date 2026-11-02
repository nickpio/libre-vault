"""Order visible vault paths into a folder-first tree."""
from pathlib import PurePosixPath


def tree_paths(notes, folders, collapsed):
    # A file can appear before its parent is recorded (a canvas created in a
    # folder the vault has not scanned yet). Keep that file under its folder.
    known = set(folders)
    for path in [*folders, *notes]:
        parent = PurePosixPath(path).parent
        while str(parent) not in ('.', ''):
            known.add(str(parent))
            parent = parent.parent
    children = {'': []}
    for folder in known:
        children[folder] = []
    for path in [*(folder + '/' for folder in known), *notes]:
        parent = str(PurePosixPath(path).parent)
        children['' if parent == '.' else parent].append(path)

    def visit(parent):
        for path in sorted(children[parent], key=lambda path: (
                not path.endswith('/'), PurePosixPath(path).name.casefold(), path)):
            if not parent and path == 'attachments/':
                continue
            yield path
            if path.endswith('/') and path[:-1] not in collapsed:
                yield from visit(path[:-1])

    return list(visit(''))
