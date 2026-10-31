"""Order visible vault paths into a folder-first tree."""
from pathlib import PurePosixPath


def tree_paths(notes, folders, collapsed):
    children = {'': []}
    for folder in folders:
        children[folder] = []
    for path in [*(folder + '/' for folder in folders), *notes]:
        parent = str(PurePosixPath(path).parent)
        children['' if parent == '.' else parent].append(path)

    def visit(parent):
        for path in sorted(children[parent], key=lambda path: (
                not path.endswith('/'), PurePosixPath(path).name.casefold(), path)):
            yield path
            if path.endswith('/') and path[:-1] not in collapsed:
                yield from visit(path[:-1])

    return list(visit(''))
