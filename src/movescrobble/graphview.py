"""The taste graph as a page you can drag around in the browser: your artists, what they lead to, and how strongly.

Self-contained HTML (d3 from a CDN), written next to the database and opened with the default browser.
"""
from __future__ import annotations

import json
import webbrowser
from pathlib import Path

from .config import config_dir

FILE = "taste-graph.html"
MAX_PICKS = 60


def graph_data(taste, owned: set[str] = frozenset(), n: int = MAX_PICKS) -> dict:
    """Nodes: your seed artists and the best-scoring other artists; links: which seeds lead to which pick.
    `owned`: norm(artist) with files in your library (drawn with a ring)."""
    from .matcher import norm

    seeds = {norm(a): w for a, w in taste.seeds.items()}
    top = max(seeds.values(), default=1.0)
    nodes = [{"id": a, "label": taste.name(a), "kind": "seed", "size": w / top, "owned": a in owned}
             for a, w in seeds.items()]
    picks = [a for a in sorted(taste.artist_score, key=lambda a: -taste.artist_score[a]) if a not in seeds][:n]
    links = []
    for a in picks:
        nodes.append({"id": a, "label": taste.name(a), "kind": "fresh" if a in taste.known else "new",
                      "size": taste.artist_score[a], "owned": a in owned})
        for rank, s in enumerate(taste.why.get(a, [])):
            if norm(s) in seeds:
                links.append({"source": norm(s), "target": a, "strength": 1.0 if rank == 0 else 0.5})
    return {"nodes": nodes, "links": links}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Taste graph</title>
<style>
:root { --bg: #fbfaf7; --ink: #1d1d1b; --muted: #77746c; --seed: #c2410c; --new: #2563eb; --fresh: #15803d; --edge: #b9b5aa; }
@media (prefers-color-scheme: dark) { :root { --bg: #16161a; --ink: #ecebe6; --muted: #9a978f; --seed: #fb923c;
  --new: #60a5fa; --fresh: #4ade80; --edge: #4a4842; } }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink); font: 13px system-ui, sans-serif; }
header { position: fixed; top: 0; left: 0; right: 0; padding: 12px 16px; pointer-events: none; }
header h1 { margin: 0 0 4px; font-size: 16px; } header p { margin: 0; color: var(--muted); }
.key span { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin: 0 4px 0 12px; vertical-align: -1px; }
svg { width: 100%; height: 100%; display: block; } text { fill: var(--ink); pointer-events: none; }
line { stroke: var(--edge); }
</style></head><body>
<header><h1>Your taste graph</h1>
<p class="key"><span style="background:var(--seed)"></span>your artists<span style="background:var(--new)"></span>new to you
<span style="background:var(--fresh)"></span>known, with unheard tracks
· ring = in your library · bigger = stronger · drag to explore</p></header>
<svg></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const data = __DATA__;
const svg = d3.select("svg"), g = svg.append("g");
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v);
const color = d => css(d.kind === "seed" ? "--seed" : d.kind === "new" ? "--new" : "--fresh");
const r = d => d.kind === "seed" ? 6 + 14 * d.size : 4 + 10 * d.size;
svg.call(d3.zoom().scaleExtent([0.2, 4]).on("zoom", e => g.attr("transform", e.transform)));
const sim = d3.forceSimulation(data.nodes)
  .force("link", d3.forceLink(data.links).id(d => d.id).distance(70).strength(l => 0.3 * l.strength))
  .force("charge", d3.forceManyBody().strength(-160))
  .force("center", d3.forceCenter(innerWidth / 2, innerHeight / 2))
  .force("collide", d3.forceCollide(d => r(d) + 3));
const link = g.append("g").selectAll("line").data(data.links).join("line").attr("stroke-width", l => 2 * l.strength);
const node = g.append("g").selectAll("circle").data(data.nodes).join("circle")
  .attr("r", r).attr("fill", color).attr("stroke", d => d.owned ? css("--ink") : "none").attr("stroke-width", 2)
  .call(d3.drag().on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = d.fy = null; }));
node.append("title").text(d => d.label);
const label = g.append("g").selectAll("text").data(data.nodes).join("text")
  .text(d => d.label).attr("font-size", d => d.kind === "seed" ? 13 : 11).attr("font-weight", d => d.kind === "seed" ? 600 : 400);
sim.on("tick", () => {
  link.attr("x1", l => l.source.x).attr("y1", l => l.source.y).attr("x2", l => l.target.x).attr("y2", l => l.target.y);
  node.attr("cx", d => d.x).attr("cy", d => d.y);
  label.attr("x", d => d.x + r(d) + 3).attr("y", d => d.y + 4);
});
</script></body></html>
"""


def write(taste, owned: set[str] = frozenset(), path: Path | None = None) -> Path:
    path = path or config_dir() / FILE
    path.write_text(PAGE.replace("__DATA__", json.dumps(graph_data(taste, owned)).replace("</", "<\\/")))
    return path


def open_in_browser(path: Path) -> None:
    webbrowser.open(path.resolve().as_uri())
