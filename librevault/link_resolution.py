"""Case-insensitive note resolution shared by navigation and graphs."""
from collections import defaultdict
import posixpath
from urllib.parse import unquote


class PathResolver:
    def __init__(self, paths, aliases):
        self.paths = set(paths)
        self.aliases = aliases
        self.folded, self.names = defaultdict(list), defaultdict(list)
        self.alias_index = None
        for path in paths:
            self.folded[path.casefold()].append(path)
            self.names[posixpath.basename(path).casefold()].append(path)

    def resolve(self, source, target):
        target = unquote(target.split('|', 1)[0].split('#', 1)[0]).strip().replace('\\', '/')
        if not target:
            return [source] if source in self.paths else []
        if not target.lower().endswith('.md'):
            target += '.md'
        def candidates(name):
            return [name] if name in self.paths else sorted(self.folded.get(name.casefold(), []))
        local = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
        if target.startswith(('./', '../')):
            return candidates(local)
        found = candidates(target) or candidates(local)
        if found:
            return found
        if '/' not in target:
            found = sorted(self.names.get(target.casefold(), []))
            if found:
                return found
            if self.alias_index is None:
                self.alias_index = defaultdict(list)
                for path in self.paths:
                    for value in self.aliases(path):
                        self.alias_index[value.casefold()].append(path)
            return sorted(set(self.alias_index.get(target[:-3].casefold(), [])))
        return []


def resolve_paths(paths, aliases, source, target):
    return PathResolver(paths, aliases).resolve(source, target)
