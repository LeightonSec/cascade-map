"""DOT (Graphviz) rendering — optional, text-first, zero dependencies.

Emits Graphviz DOT source describing the dependency graph, optionally annotated
with a failure run: failed nodes coloured, the injected node and any NIS2
red-line SPOF highlighted, essential entities shaped distinctly. Rasterising to
PNG/SVG uses the system `dot` binary (`brew install graphviz`) — the tool itself
carries no Graphviz dependency, honouring the design's "never a hard dependency".

    cascade-map dot examples/region_x.yaml --inject grid_substation_12 \\
        | dot -Tpng -o cascade.png
"""

from __future__ import annotations

from cascade_map.analysis import nis2_exposure
from cascade_map.engine import Graph, _fmt_t, propagate

# Fill colours by sector, used when no failure is injected.
_SECTOR_COLORS = {
    "power": "#f6c85f",
    "telecom": "#6f9fd8",
    "water": "#6dc0b3",
    "finance": "#b39ddb",
    "health": "#e57373",
}
_DEFAULT_SECTOR = "#cccccc"

# Fill colours by failure status, used when a failure is injected.
_INJECTED = "#c0392b"
_FAILED = "#e8956b"
_SURVIVOR = "#7fc07f"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def render_dot(g: Graph, injected: list[str] | None = None) -> str:
    """Return Graphviz DOT source for the graph. If ``injected`` is given, the
    nodes are annotated with the resulting failure run rather than by sector."""
    inj = set(injected or [])
    failed: dict[str, float] = {}
    red_lines: set[str] = set()
    if injected:
        failed = {f.node: f.time for f in propagate(g, injected)}
        red_lines = {r["node"] for r in nis2_exposure(g) if r["red_line"]}

    lines = [
        "digraph cascade_map {",
        "  rankdir=LR;",
        '  graph [fontname="Helvetica", labelloc="t", label="cascade-map"];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica",'
        ' fontcolor="#111111"];',
        '  edge [fontname="Helvetica", fontsize=10, color="#888888"];',
    ]

    for n in g.nodes:
        parts = [_esc(n.label or n.id)]
        if n.sector:
            parts.append(f"[{n.sector}]")
        if injected:
            if n.id in inj:
                fill = _INJECTED
                parts.append("injected t=0")
            elif n.id in failed:
                fill = _FAILED
                parts.append(f"failed t={_fmt_t(failed[n.id])}")
            else:
                fill = _SURVIVOR
                parts.append("survived")
        else:
            fill = _SECTOR_COLORS.get(n.sector, _DEFAULT_SECTOR)
        if n.nis2_vendor_score is not None:
            parts.append(f"NIS2 {n.nis2_vendor_score:g}")
        if n.id in red_lines:
            parts.append("⚠ SPOF → essential")

        label = "\\n".join(parts)
        attrs = [f'label="{label}"', f'fillcolor="{fill}"']
        if n.criticality == "essential":
            attrs.append("shape=doubleoctagon")
        if n.id in red_lines:
            attrs += ['color="#8e44ad"', "penwidth=3"]
        elif n.id in inj:
            attrs += ['color="#7b241c"', "penwidth=3"]
        else:
            attrs.append('color="#555555"')
        lines.append(f'  "{_esc(n.id)}" [{", ".join(attrs)}];')

    # Draw the "supplies" direction (dst -> src) so failure flows along arrows.
    for e in g.edges:
        lines.append(f'  "{_esc(e.dst)}" -> "{_esc(e.src)}" [label="{_esc(e.type)}"];')

    lines.append("}")
    return "\n".join(lines)
