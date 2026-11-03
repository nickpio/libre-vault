"""Compact saved-note graph and bounded force layout. No GTK dependencies."""
from collections import defaultdict
import json
import math
import posixpath
from pathlib import Path
import sqlite3
import time
from urllib.parse import unquote


def build_graph(rows, query='', root=None, depth=1, orphans=True,
                tags=False, attachments=False, unresolved=False, directed=False,
                attachment_paths=()):
    """Build optional typed nodes; all note filters use the vault search grammar."""
    from .metadata import NoteMetadata, note_metadata, query_terms, matches_query
    records, tag_values, matching = {}, {}, set()
    terms = query_terms(query)
    for row in rows:
        path, aliases, links, *text = row
        values = json.loads(aliases)
        content = text[0] if text else ''
        metadata = note_metadata(content) if content and (query or tags) else NoteMetadata({}, (), tuple(values))
        records[path] = (values, json.loads(links))
        if tags:
            tag_values[path] = metadata.tags
        if matches_query(path, content, terms, metadata):
            matching.add(path)
    names, aliases = defaultdict(list), defaultdict(list)
    assets = set(attachment_paths) if attachments else set()
    for path in list(records) + sorted(assets):
        names[posixpath.basename(path)].append(path)
    for path, (values, _) in records.items():
        for alias in values:
            aliases[alias.casefold()].append(path)
    edges, nodes = set(), set(records)
    available = set(records) | assets
    def edge(source, destination):
        if destination != source:
            nodes.add(destination)
            edges.add((source, destination) if directed else tuple(sorted((source, destination))))
    for source, (_, links) in records.items():
        if tags:
            for tag in tag_values[source]:
                edge(source, '#' + tag)
        for link in links:
            raw = unquote(link['target'].split('|', 1)[0].split('#', 1)[0]).strip().replace('\\', '/')
            if not raw or '://' in raw or raw.startswith(('mailto:', 'data:')):
                continue
            asset_local = posixpath.normpath(posixpath.join(posixpath.dirname(source), raw))
            is_asset = raw in assets or asset_local in assets or any(p in assets for p in names.get(raw, ()))
            target = raw if raw.lower().endswith('.md') or is_asset else raw + '.md'
            local = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
            if target.startswith(('./', '../')):
                candidates = [local] if local in available else []
            elif target in available:
                candidates = [target]
            elif local in available:
                candidates = [local]
            elif '/' not in target:
                candidates = names.get(target) or aliases.get(target.removesuffix('.md').casefold(), [])
            else:
                candidates = []
            candidates = set(candidates)
            if len(candidates) == 1:
                edge(source, next(iter(candidates)))
            elif not candidates and unresolved and (not posixpath.splitext(raw)[1] or raw.endswith('.md')):
                ghost = local if raw.startswith(('./', '../')) else target
                edge(source, 'ghost:' + ghost)
    adjacent = defaultdict(set)
    for a, b in edges:
        adjacent[a].add(b)
        adjacent[b].add(a)
    selected = set(nodes)
    if root is not None:
        selected = {root} if root in records else set()
        frontier = selected.copy()
        for _ in range(depth):
            frontier = {b for a in frontier for b in adjacent[a]} - selected
            selected.update(frontier)
    def matches_extra(path):
        metadata = NoteMetadata({}, (path[1:],) if path.startswith('#') else (), ())
        return matches_query(path, '', terms, metadata)
    # Extra nodes follow their visible notes, while an empty filter also shows
    # standalone attachments if the caller requests them.
    selected_notes = {p for p in selected if p in matching}
    selected = {p for p in selected if (p in selected_notes if p in records else
                (not query or matches_extra(p) or any(n in selected_notes for n in adjacent[p]))) and (orphans or adjacent[p])}
    if attachments and root is None and not query and orphans:
        selected.update(assets)
    paths = sorted(selected)
    indices = {p: i for i, p in enumerate(paths)}
    return paths, sorted((indices[a], indices[b]) for a, b in edges if a in selected and b in selected)


class Layout:
    """O(nodes + edges) steps with bounded local repulsion and cooling."""
    def __init__(self, count, edges):
        self.edges = edges
        self.positions = [[math.cos(i * 2.399963) * 14 * math.sqrt(i + 1),
                           math.sin(i * 2.399963) * 14 * math.sqrt(i + 1)] for i in range(count)]
        self.iteration = 0

    def step(self):
        cells = defaultdict(list)
        for i, (x, y) in enumerate(self.positions):
            cells[(int(x // 90), int(y // 90))].append(i)
        centers = {cell: (sum(self.positions[j][0] for j in members),
                          sum(self.positions[j][1] for j in members)) for cell, members in cells.items()}
        forces = [[-x * .002, -y * .002] for x, y in self.positions]
        for i, (x, y) in enumerate(self.positions):
            cx, cy = int(x // 90), int(y // 90)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cell = (cx + dx, cy + dy)
                    members = cells.get(cell, ())
                    if len(members) > 8:
                        count = len(members)
                        sx, sy = centers[cell]
                        if cell == (cx, cy):
                            sx, sy, count = sx-x, sy-y, count-1
                        targets = ((sx/count, sy/count, count),)
                    else:
                        targets = ((self.positions[j][0], self.positions[j][1], 1) for j in members if i != j)
                    for ox, oy, weight in targets:
                        vx, vy = x - ox, y - oy
                        distance = max(4, vx * vx + vy * vy)
                        factor = 90 * weight / distance
                        forces[i][0] += vx * factor
                        forces[i][1] += vy * factor
        for a, b in self.edges:
            x, y = self.positions[a]
            ox, oy = self.positions[b]
            vx, vy = ox - x, oy - y
            distance = max(1, math.hypot(vx, vy))
            factor = .018 * (distance - 65) / distance
            forces[a][0] += vx * factor
            forces[a][1] += vy * factor
            forces[b][0] -= vx * factor
            forces[b][1] -= vy * factor
        cooling = max(.03, 1 - self.iteration / 160)
        movement = 0
        for point, (fx, fy) in zip(self.positions, forces):
            dx, dy = max(-8, min(8, fx)) * cooling, max(-8, min(8, fy)) * cooling
            point[0] += dx
            point[1] += dy
            movement = max(movement, abs(dx), abs(dy))
        self.iteration += 1
        return self.iteration < 160 and movement > .04


def graph_worker(connection, index_path, options):
    """Read only indexed metadata, then compute off the GTK process."""
    try:
        with sqlite3.connect(Path(index_path).as_uri() + '?mode=ro', uri=True) as db:
            from .metadata import matches_query, note_metadata, query_terms
            options = dict(options)
            vault_root = options.pop('vault_root', None)
            groups = options.pop('groups', [])
            if options.get('attachments') and vault_root:
                import os
                assets = []
                for directory, folders, files in os.walk(vault_root, followlinks=False):
                    folders[:] = [name for name in folders if not name.startswith('.') and not (Path(directory) / name).is_symlink()]
                    assets.extend((Path(directory) / name).relative_to(vault_root).as_posix()
                                  for name in files if not name.startswith('.') and not name.lower().endswith('.md')
                                  and not (Path(directory) / name).is_symlink())
                options['attachment_paths'] = assets
            colors = {}
            compiled = [(query_terms(group['query']), group['color']) for group in groups]
            def indexed_rows():
                # Stream note text instead of retaining a second vault-sized
                # copy in the layout process.
                for row in db.execute('SELECT path,aliases,links,text FROM notes'):
                    path, aliases, links, text = row
                    if compiled:
                        metadata = note_metadata(text)
                        for terms, color in compiled:
                            if matches_query(path, text, terms, metadata):
                                colors[path] = color
                                break
                    yield row
            paths, edges = build_graph(indexed_rows(), **options)
            colors = {path: colors[path] for path in paths if path in colors}
        layout = Layout(len(paths), edges)
        connection.send(('graph', paths, edges, layout.positions, colors))
        # Backpressure: only compute/send a new frame when the UI asks for it.
        started = time.monotonic()
        while connection.recv() == 'step':
            active = True
            for _ in range(1 if len(paths) > 2000 else 4):
                active = layout.step()
                if not active:
                    break
            active = active and time.monotonic() - started < 8
            connection.send(('positions', layout.positions, active))
            if not active:
                break
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        connection.send(('error', str(error)))
    finally:
        connection.close()
