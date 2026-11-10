"""GTK-free bookkeeping that keeps live-preview updates incremental."""
from collections import defaultdict, deque
import re


_RUNS = re.compile(rb'[^\0]+')


def reconcile(previous, wanted):
    """Pair wanted descriptors with existing ``(descriptor, value)`` pairs.

    Descriptors omit source offsets, so a widget survives edits above it. Equal
    descriptors pair in note order. A None descriptor never matches.

    Returns one value or None per wanted descriptor, plus the unmatched values.
    """
    pool = defaultdict(deque)
    stale = []
    for descriptor, value in previous:
        if descriptor is None:
            stale.append(value)
        else:
            pool[descriptor].append(value)
    matched = [pool[descriptor].popleft() if descriptor is not None and pool.get(descriptor) else None
               for descriptor in wanted]
    for values in pool.values():
        stale.extend(values)
    return matched, stale


class Concealment:
    """The characters and paragraph-spacing tags one render wants concealed.

    Hide and reveal calls apply in order, as buffer tag operations would, so
    later math reveals can punch holes in earlier syntax concealment.
    """

    def __init__(self, length):
        self.hidden = bytearray(length)
        self.spacing = {}

    def hide(self, start, end):
        self._fill(start, end, b'\1')

    def reveal(self, start, end):
        self._fill(start, end, b'\0')

    def space(self, tag, start, end):
        self.spacing[tag] = (start, end)

    def _fill(self, start, end, value):
        # A slice assignment of another length would resize the array.
        start, end = max(start, 0), min(end, len(self.hidden))
        if end > start:
            self.hidden[start:end] = value * (end - start)


def hidden_changes(old, new):
    """Return ``(added, removed)`` ranges between two equal-length hidden maps."""
    before = int.from_bytes(old, 'little')
    after = int.from_bytes(new, 'little')
    return _runs(after & ~before, len(new)), _runs(before & ~after, len(new))


def _runs(bits, size):
    return [match.span() for match in _RUNS.finditer(bits.to_bytes(size, 'little'))]
