"""The taste graph drawn in the terminal: you in the middle, your artists around you, where they lead outside.

Edges are drawn with braille dots (2x4 sub-cells per character, so lines look like lines); an artist reached
from two of yours gets a second, fainter edge, which is what ties the picture together. Arrow keys move to the
nearest node in that direction. Pure functions over a list of nodes, so the shell can redraw on every key.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from rich.text import Text

from .matcher import norm

STYLE = {"you": "bold white", "seed": "bold red", "new": "bright_blue", "known": "green"}
GLYPH = {"you": "◉", "seed": "●", "new": "●", "known": "●"}
LABEL = 18  # characters of a name shown


@dataclass
class Node:
    key: str  # norm(artist); "" for you
    label: str
    kind: str  # you | seed | new | known
    score: float = 0.0
    owned: bool = False
    x: int = 0
    y: int = 0


Edge = tuple[int, int, float]  # node index, node index, strength (1 = main path, 0.5 = "also via")


def build(taste, owned: set[str] = frozenset(), seeds: int = 8, new_per_seed: int = 3,
          known_per_seed: int = 1) -> tuple[list[Node], list[Edge]]:
    """You -> your strongest artists -> the best artists each of them leads to (plus the second paths). Each of
    your artists shows mostly discoveries: known artists score higher (you play them), so they would fill it."""
    nodes = [Node("", "you", "you", 1.0)]
    edges: list[Edge] = []
    top = sorted(taste.seeds.items(), key=lambda kv: -kv[1])[:seeds]
    peak = top[0][1] if top else 1.0
    at: dict[str, int] = {}
    for name, w in top:
        at[norm(name)] = len(nodes)
        edges.append((0, len(nodes), 1.0))
        nodes.append(Node(norm(name), name, "seed", w / peak, norm(name) in owned))
    all_seeds = {norm(s) for s in taste.seeds}
    room = {"new": new_per_seed, "known": known_per_seed}
    taken: dict[tuple[str, str], int] = {}
    picks = []
    for a in sorted(taste.artist_score, key=lambda a: -taste.artist_score[a]):
        via = taste.why.get(a) or []
        kind = "known" if a in taste.known else "new"
        if a in all_seeds or not via or norm(via[0]) not in at or taken.get((norm(via[0]), kind), 0) >= room[kind]:
            continue
        taken[(norm(via[0]), kind)] = taken.get((norm(via[0]), kind), 0) + 1
        picks.append((a, via, kind))
    picks.sort(key=lambda p: p[2] != "new")  # discoveries first in each fan, then the known one
    for a, via, kind in picks:
        i = len(nodes)
        nodes.append(Node(a, taste.name(a), kind, taste.artist_score[a], a in owned))
        edges.append((at[norm(via[0])], i, 1.0))
        for other in via[1:]:
            if norm(other) in at:
                edges.append((at[norm(other)], i, 0.5))
    return nodes, edges


def layout(nodes: list[Node], edges: list[Edge], width: int, height: int) -> None:
    """Rings: you at the centre, your artists evenly round you, each one's picks fanned out behind it.
    Cells are about twice as tall as wide, so the x radii are doubled to look round."""
    cx, cy = width / 2, height / 2
    ry1, ry2 = height * 0.24, height * 0.45
    rx1, rx2 = width * 0.2, width * 0.37  # wider than round: names run sideways, so width is what they need
    nodes[0].x, nodes[0].y = round(cx), round(cy)
    seeds = [i for i, n in enumerate(nodes) if n.kind == "seed"]
    kids: dict[int, list[int]] = {s: [] for s in seeds}
    for a, b, w in edges:
        if w == 1.0 and a in kids:
            kids[a].append(b)
    for k, s in enumerate(seeds):
        ang = 2 * math.pi * k / max(len(seeds), 1) - math.pi / 2
        nodes[s].x, nodes[s].y = round(cx + rx1 * math.cos(ang)), round(cy + ry1 * math.sin(ang))
        fan = 2 * math.pi / max(len(seeds), 1) * 0.75
        ks = kids[s]
        for j, c in enumerate(ks):
            a2 = ang + (j - (len(ks) - 1) / 2) * fan / max(len(ks), 1)
            nodes[c].x = round(cx + rx2 * math.cos(a2))
            nodes[c].y = round(cy + ry2 * math.sin(a2))
    for n in nodes:
        n.x, n.y = max(0, min(width - 1, n.x)), max(0, min(height - 1, n.y))


_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # braille dot for sub-cell (row, column)


def render(nodes: list[Node], edges: list[Edge], width: int, height: int, selected: int | None = None) -> list[Text]:
    """One Text per row: braille edges (those of the selected node bright), then dots and names on top."""
    bits = [[0] * width for _ in range(height)]
    style: list[list[str]] = [["grey35"] * width for _ in range(height)]
    for a, b, w in sorted(edges, key=lambda e: selected in e[:2]):  # the selected node's edges drawn last
        hot = selected in (a, b)
        x0, y0, x1, y1 = nodes[a].x * 2 + 1, nodes[a].y * 4 + 2, nodes[b].x * 2 + 1, nodes[b].y * 4 + 2
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for s in range(steps + 1):
            if w < 1.0 and s % 3:  # second paths dotted
                continue
            x, y = round(x0 + (x1 - x0) * s / steps), round(y0 + (y1 - y0) * s / steps)
            cx, cy = x // 2, y // 4
            if 0 <= cx < width and 0 <= cy < height:
                bits[cy][cx] |= _BITS[y % 4][x % 2]
                if hot:
                    style[cy][cx] = "yellow"
                elif w < 1.0 and style[cy][cx] == "grey35":
                    style[cy][cx] = "grey27"
    chars = [[chr(0x2800 + v) if v else " " for v in row] for row in bits]
    taken = [[False] * width for _ in range(height)]  # cells already holding a name
    for n in nodes:  # every dot first, so no name covers one
        chars[n.y][n.x], style[n.y][n.x], taken[n.y][n.x] = GLYPH[n.kind], STYLE[n.kind], True
    order = sorted(range(len(nodes)), key=lambda i: (i != selected, nodes[i].kind != "you", nodes[i].kind != "seed",
                                                     -nodes[i].score))
    for i in order:  # the selected node, you and your artists get the first pick of free space
        n = nodes[i]
        name = (n.label if len(n.label) <= LABEL else n.label[: LABEL - 1] + "…") + (" ♪" if n.owned else "")
        st = STYLE[n.kind] + (" reverse" if i == selected else "")
        spot = _free_spot(taken, n, len(name), width, height)
        if spot is None:
            continue  # no room: the dot alone (its name shows in the side panel when selected)
        row, start = spot
        for k, ch in enumerate(name):
            chars[row][start + k], style[row][start + k], taken[row][start + k] = ch, st, True
    out = []
    for r in range(height):
        t = Text()
        run, run_style = "", style[r][0]
        for c in range(width):
            if style[r][c] != run_style:
                t.append(run, style=run_style)
                run, run_style = "", style[r][c]
            run += chars[r][c]
        t.append(run, style=run_style)
        out.append(t)
    return out


def _free_spot(taken: list[list[bool]], n: Node, length: int, width: int, height: int) -> tuple[int, int] | None:
    """Where to write a name: beside its dot (outward side first), else just above or below it."""
    right, left = (n.x + 2, n.x - length - 1)
    sides = (right, left) if n.x < width / 2 else (left, right)
    for row, starts in ((n.y, sides), (n.y - 1, (n.x - length // 2,)), (n.y + 1, (n.x - length // 2,))):
        for start in starts:
            start = max(0, min(width - length, start))
            if 0 <= row < height and not any(taken[row][start - (start > 0): start + length + 1]):
                return row, start
    return None


DIRECTIONS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}


def step(nodes: list[Node], current: int, key: str) -> int:
    """The nearest node roughly in the arrow's direction (rows count double: cells are tall)."""
    dx, dy = DIRECTIONS[key]
    here = nodes[current]
    best, best_cost = current, math.inf
    for i, n in enumerate(nodes):
        vx, vy = n.x - here.x, (n.y - here.y) * 2
        dist = math.hypot(vx, vy)
        if i == current or dist == 0:
            continue
        cos = (vx * dx + vy * dy) / dist
        if cos > 0.35:
            cost = dist / cos ** 2
            if cost < best_cost:
                best, best_cost = i, cost
    return best
