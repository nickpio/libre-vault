"""Compact saved-note graph and bounded force layout. No GTK dependencies."""
from collections import defaultdict
import json
import math
import posixpath
from pathlib import Path
import sqlite3
import time
from urllib.parse import unquote


def build_graph(rows, query='', root=None, depth=1, orphans=True):
    """Resolve links with the same precedence as Vault.resolve, using name indexes."""
    records = {path: (json.loads(aliases), json.loads(links)) for path, aliases, links in rows}
    names, aliases = defaultdict(list), defaultdict(list)
    for path, (values, _) in records.items():
        names[posixpath.basename(path)].append(path)
        for alias in values:
            aliases[alias.casefold()].append(path)
    edges = set()
    for source, (_, links) in records.items():
        for link in links:
            target = unquote(link['target'].split('|', 1)[0].split('#', 1)[0]).strip().replace('\\', '/')
            if not target:
                continue
            if not target.lower().endswith('.md'):
                target += '.md'
            local = posixpath.join(posixpath.dirname(source), target)
            if target.startswith(('./', '../')):
                local = posixpath.normpath(local)
                candidates = [local] if local in records else []
            elif target in records:
                candidates = [target]
            elif local in records:
                candidates = [local]
            elif '/' not in target:
                candidates = names.get(target) or aliases.get(target[:-3].casefold(), [])
            else:
                candidates = []
            candidates = set(candidates)
            if len(candidates) == 1:
                destination = next(iter(candidates))
                if destination != source:
                    edges.add(tuple(sorted((source, destination))))
    adjacent = defaultdict(set)
    for a, b in edges:
        adjacent[a].add(b)
        adjacent[b].add(a)
    selected = set(records)
    if root is not None:
        selected = {root} if root in records else set()
        frontier = selected.copy()
        for _ in range(depth):
            frontier = {b for a in frontier for b in adjacent[a]} - selected
            selected.update(frontier)
    words = query.casefold().split()
    selected = {p for p in selected if all(w in (p + ' ' + ' '.join(records[p][0])).casefold() for w in words)
                and (orphans or adjacent[p])}
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
            paths, edges = build_graph(db.execute('SELECT path,aliases,links FROM notes'), **options)
        layout = Layout(len(paths), edges)
        connection.send(('graph', paths, edges, layout.positions))
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
