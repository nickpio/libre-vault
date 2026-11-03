"""Foldable Markdown regions. Offsets only — never rewrites note text."""
from dataclasses import dataclass

from .formatting import markdown_layout


@dataclass(frozen=True)
class FoldRegion:
    """A collapsible body. `anchor` is the header line's first character.

    The header stays visible. `body_start:body_end` is the concealed range.
    `start_line` is the zero-based header line for that same revision.
    """
    kind: str
    level: int
    anchor: int
    body_start: int
    body_end: int
    start_line: int


def line_starts(text):
    starts = [0]
    for index, char in enumerate(text):
        if char == '\n':
            starts.append(index + 1)
    return starts


def merge_spans(spans):
    merged = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def remap_anchors(anchors, start, removed, inserted):
    """Move header anchors across a replacement of `text[start:start + removed]`.

    An anchor tracks the header's first character. An insertion at that
    character pushes it right. A deletion that covers the character drops the
    fold. Edits after the header leave it alone.
    """
    end = start + removed
    delta = inserted - removed
    updated = set()
    for anchor in anchors:
        if anchor >= end:
            updated.add(anchor + delta)
        elif anchor < start:
            updated.add(anchor)
    return updated


def _heading_end(text, layout, heading):
    style = f'h{heading.level}'
    for span in layout.spans:
        if span.style == style and span.start == heading.start:
            return span.end
    newline = text.find('\n', heading.start)
    return len(text) if newline < 0 else newline + 1


def _line_index(starts, offset):
    # starts is sorted and includes 0. The last start at or before offset wins.
    index = 0
    for candidate, origin in enumerate(starts):
        if origin <= offset:
            index = candidate
        else:
            break
    return index


def compute_regions(text, layout=None):
    """Heading, list, fenced-code, and frontmatter folds for one source snapshot."""
    layout = layout or markdown_layout(text)
    starts = line_starts(text)
    start_set = set(starts)
    regions = []

    for span in layout.spans:
        if span.style != 'metadata' or span.start != 0 or span.end <= span.start:
            continue
        close_line = _line_index(starts, max(span.end - 1, 0))
        if close_line > 1 and starts[1] < starts[close_line]:
            regions.append(FoldRegion('frontmatter', 0, 0, starts[1], starts[close_line], 0))
        break

    for span in layout.spans:
        if span.style != 'code' or span.start not in start_set:
            continue
        if span.end not in start_set and span.end != len(text):
            continue
        opening_end = text.find('\n', span.start)
        opening = text[span.start:] if opening_end < 0 else text[span.start:opening_end]
        fence = opening.lstrip()
        if len(fence) < 3 or fence[0] not in '`~' or fence[:3] != fence[0] * 3:
            continue
        body_start = span.start if opening_end < 0 else opening_end + 1
        body_end = span.end
        close_line = _line_index(starts, max(span.end - 1, 0))
        open_line = _line_index(starts, span.start)
        if close_line > open_line:
            closing = text[starts[close_line]:span.end].strip()
            if len(closing) >= 3 and closing == closing[0] * len(closing) and closing[0] == fence[0]:
                body_end = starts[close_line]
        if body_end > body_start:
            regions.append(FoldRegion('code', 0, span.start, body_start, body_end, open_line))

    headings = layout.headings
    for index, heading in enumerate(headings):
        header_end = _heading_end(text, layout, heading)
        body_end = len(text)
        for later in headings[index + 1:]:
            if later.level <= heading.level:
                body_end = later.start
                break
        if body_end > header_end:
            regions.append(FoldRegion(
                'heading', heading.level, heading.start, header_end, body_end,
                _line_index(starts, heading.start)))

    for item in layout.items:
        descendants = [child for child in layout.items
                       if child.depth > item.depth and item.start < child.start < item.end]
        if not descendants:
            continue
        newline = text.find('\n', item.start)
        first_end = len(text) if newline < 0 else newline + 1
        body_end = min(item.end, max(child.end for child in descendants))
        if body_end > first_end:
            regions.append(FoldRegion(
                'list', item.depth, item.start, first_end, body_end, _line_index(starts, item.start)))
    return regions


class FoldState:
    """Which header anchors are collapsed, remapped as the buffer is edited."""

    def __init__(self):
        self.anchors = set()

    def apply_edit(self, start, removed, inserted):
        self.anchors = remap_anchors(self.anchors, start, removed, inserted)

    def prune(self, regions):
        self.anchors &= {region.anchor for region in regions}

    def region_at(self, regions, text, cursor):
        """Innermost fold whose header line, or else whose body, holds the cursor."""
        if not regions:
            return None
        cursor = max(0, min(cursor, len(text)))
        line_start = text.rfind('\n', 0, cursor) + 1
        headers = [region for region in regions if region.anchor == line_start]
        if headers:
            return min(headers, key=lambda region: region.body_end - region.body_start)
        bodies = [region for region in regions if region.body_start <= cursor < region.body_end]
        if not bodies:
            return None
        return min(bodies, key=lambda region: region.body_end - region.body_start)

    def fold_at(self, regions, text, cursor):
        region = self.region_at(regions, text, cursor)
        if region is None:
            return None
        self.anchors.add(region.anchor)
        return region

    def unfold_at(self, regions, text, cursor):
        region = self.region_at(regions, text, cursor)
        if region is None or region.anchor not in self.anchors:
            return None
        self.anchors.discard(region.anchor)
        return region

    def toggle_at(self, regions, text, cursor):
        region = self.region_at(regions, text, cursor)
        if region is None:
            return None
        if region.anchor in self.anchors:
            self.anchors.discard(region.anchor)
        else:
            self.anchors.add(region.anchor)
        return region

    def fold_all_headings(self, regions):
        for region in regions:
            if region.kind == 'heading':
                self.anchors.add(region.anchor)

    def unfold_all(self):
        self.anchors.clear()

    def concealed(self, regions, reveal=()):
        """Body ranges that should be invisible. Offsets in `reveal` open their fold."""
        folded = [region for region in regions if region.anchor in self.anchors]
        revealed = set()
        for offset in reveal:
            for region in folded:
                if region.body_start <= offset < region.body_end:
                    revealed.add(region.anchor)
        spans = [(region.body_start, region.body_end)
                 for region in folded if region.anchor not in revealed]
        return merge_spans(spans)
