"""Fuzzy note matching and bounded navigation history, independent of GTK."""
from dataclasses import dataclass


def fuzzy_score(query, text):
    query, text = query.casefold().strip(), text.casefold()
    if not query:
        return 0
    index, score, previous = 0, 0, -2
    for character in query:
        found = text.find(character, index)
        if found < 0:
            return None
        score += 8 if found == previous + 1 else 1
        score += 5 if found == 0 or text[found - 1] in '/ _-.' else 0
        previous, index = found, found + 1
    return score - len(text) / 1000


def matching_notes(paths, query, aliases=None, limit=40):
    aliases = aliases or {}
    ranked = []
    for path in paths:
        scores = [fuzzy_score(query, value) for value in [path, *aliases.get(path, [])]]
        scores = [score for score in scores if score is not None]
        if scores:
            ranked.append((-max(scores), path.casefold(), path))
    return [path for _, _, path in sorted(ranked)[:limit]]


@dataclass(frozen=True)
class Location:
    path: str
    cursor: int = 0


class NavigationHistory:
    def __init__(self):
        self.locations = []
        self.index = -1

    def visit(self, location):
        if self.index >= 0 and self.locations[self.index].path == location.path:
            self.locations[self.index] = location
            return
        del self.locations[self.index + 1:]
        self.locations.append(location)
        self.locations = self.locations[-100:]
        self.index = len(self.locations) - 1

    def update(self, location):
        if self.index >= 0 and self.locations[self.index].path == location.path:
            self.locations[self.index] = location

    def step(self, direction):
        target = self.index + direction
        if not 0 <= target < len(self.locations):
            return None
        self.index = target
        return self.locations[target]

    def remap(self, mapping, deleted=()):
        current = self.locations[self.index] if self.index >= 0 else None
        self.locations = [Location(mapping.get(item.path, item.path), item.cursor)
                          for item in self.locations if item.path not in deleted]
        path = mapping.get(current.path, current.path) if current else None
        self.index = next((i for i, item in reversed(list(enumerate(self.locations)))
                           if item.path == path), len(self.locations) - 1)
