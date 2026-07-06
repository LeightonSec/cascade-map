"""DOT (Graphviz) rendering — optional, text-first, zero dependencies.

Emits styled Graphviz DOT for the dependency graph. With ``--inject`` the nodes
are coloured by failure status; otherwise by sector. Rasterising to PNG/SVG uses
the system `dot` binary (`brew install graphviz`) — the tool itself carries no
Graphviz dependency, honouring the design's "never a hard dependency".

    cascade-map dot examples/region_x.yaml --inject grid_substation_12 \\
        | dot -Tpng -o cascade.png

Palette follows the validated data-viz reference instance: categorical hues for
sector identity, the reserved status palette (good/serious/critical) for failure
state. Identity is never colour-alone — every node carries a text sector tag and
a legend, and status carries a glyph + word (the sanctioned CVD mitigation).
"""

from __future__ import annotations

from cascade_map.analysis import nis2_exposure
from cascade_map.engine import Graph, Node, _fmt_t, propagate

# --- palette (data-viz reference instance, light surface) ------------------- #
_INK = "#0b0b0b"
_INK2 = "#52514e"
_SURFACE = "#fcfcfb"
_HAIRLINE = "#c3c2b7"
_MUTED = "#898781"

# Categorical hues (validated reference slots). Identity is also carried by the
# text sector tag on every node + the legend, so a CVD-adjacent pair is safe.
_SECTOR = {
    "power": "#eda100",    # yellow
    "telecom": "#4a3aa7",  # violet
    "water": "#2a78d6",    # blue
    "finance": "#008300",  # green
    "health": "#e34948",   # red
}
_SECTOR_FALLBACK = "#898781"

# Reserved status palette (pre-validated in the reference instance). Ships with
# a glyph + word, never colour alone.
_STATUS = {"injected": "#d03b3b", "failed": "#ec835a", "survived": "#0ca30c"}
_GLYPH = {"injected": "●", "failed": "✕", "survived": "✓"}


def _esc(s: str) -> str:
    """Escape for a quoted DOT string (ids, edge labels)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _esc_html(s: str) -> str:
    """Escape for an HTML-like DOT label."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fill(hue: str) -> str:
    return hue + "22"  # ~13% tint over the surface: readable dark text, colour kept


def _row(text: str, size: int, color: str, bold: bool = False) -> str:
    inner = f"<B>{text}</B>" if bold else text
    return (
        f'<TR><TD ALIGN="LEFT">'
        f'<FONT POINT-SIZE="{size}" COLOR="{color}">{inner}</FONT>'
        f"</TD></TR>"
    )


def _node_dot(
    n: Node, *, injected_run: bool, inj: set[str], failed: dict[str, float],
    red_lines: set[str],
) -> str:
    essential = n.criticality == "essential"
    title = ("★ " if essential else "") + _esc_html(n.label or n.id)

    if injected_run:
        status = "injected" if n.id in inj else "failed" if n.id in failed else "survived"
        hue = _STATUS[status]
    else:
        status = None
        hue = _SECTOR.get(n.sector, _SECTOR_FALLBACK)

    subline = " · ".join([t for t in (_esc_html(n.sector), n.criticality) if t])
    rows = [_row(title, 13, _INK, bold=True), _row(subline, 9, _INK2)]

    if status == "injected":
        rows.append(_row(f"{_GLYPH['injected']} injected · t=0", 11, _INK))
    elif status == "failed":
        rows.append(_row(f"{_GLYPH['failed']} failed · t={_fmt_t(failed[n.id])}", 11, _INK))
    elif status == "survived":
        rows.append(_row(f"{_GLYPH['survived']} survived", 11, _INK))

    meta = []
    if n.nis2_vendor_score is not None:
        meta.append(f"NIS2 {n.nis2_vendor_score:g}")
    if n.id in red_lines:
        meta.append("⚠ SPOF→essential")
    if meta:
        rows.append(_row(" · ".join(meta), 9, _INK2))

    table = (
        '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1">'
        + "".join(rows)
        + "</TABLE>"
    )
    penwidth = 3.0 if n.id in inj else 2.4 if essential else 1.6
    attrs = [
        f"label=<{table}>",
        f'fillcolor="{_fill(hue)}"',
        f'color="{hue}"',
        f"penwidth={penwidth}",
    ]
    return f'  "{_esc(n.id)}" [{", ".join(attrs)}];'


def _swatch(hue: str, text: str) -> str:
    return (
        f'<TR><TD BGCOLOR="{hue}" WIDTH="14"> </TD>'
        f'<TD ALIGN="LEFT"><FONT POINT-SIZE="10" COLOR="{_INK}">{text}</FONT></TD></TR>'
    )


def _marker(sym: str, text: str) -> str:
    return (
        f'<TR><TD ALIGN="RIGHT"><FONT POINT-SIZE="10" COLOR="{_INK}">{sym}</FONT></TD>'
        f'<TD ALIGN="LEFT"><FONT POINT-SIZE="10" COLOR="{_INK}">{text}</FONT></TD></TR>'
    )


def _heading(text: str) -> str:
    return (
        f'<TR><TD COLSPAN="2" ALIGN="LEFT">'
        f'<FONT POINT-SIZE="9" COLOR="{_INK2}"><B>{text}</B></FONT></TD></TR>'
    )


def _legend(injected_run: bool, sectors_used: list[str]) -> list[str]:
    rows = []
    if injected_run:
        rows.append(_heading("STATUS"))
        rows.append(_swatch(_STATUS["injected"], f"{_GLYPH['injected']} injected"))
        rows.append(_swatch(_STATUS["failed"], f"{_GLYPH['failed']} failed"))
        rows.append(_swatch(_STATUS["survived"], f"{_GLYPH['survived']} survived"))
    else:
        rows.append(_heading("SECTOR"))
        for s in sectors_used:
            rows.append(_swatch(_SECTOR.get(s, _SECTOR_FALLBACK), _esc_html(s)))
    rows.append(_heading("MARKERS"))
    rows.append(_marker("★", "essential entity"))
    if injected_run:
        rows.append(_marker("⚠", "SPOF hitting essential"))
    table = (
        '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="4" CELLPADDING="2">'
        + "".join(rows)
        + "</TABLE>"
    )
    return [
        "  subgraph cluster_legend {",
        f'    label=""; style="rounded"; color="{_HAIRLINE}"; bgcolor="#ffffff"; margin=8;',
        f"    __legend [shape=plaintext, fillcolor=\"#ffffff00\", label=<{table}>];",
        "  }",
    ]


def render_dot(g: Graph, injected: list[str] | None = None) -> str:
    """Return styled Graphviz DOT source. If ``injected`` is given, nodes are
    coloured by the resulting failure run; otherwise by sector."""
    inj = set(injected or [])
    failed: dict[str, float] = {}
    red_lines: set[str] = set()
    if injected:
        failed = {f.node: f.time for f in propagate(g, injected)}
        red_lines = {r["node"] for r in nis2_exposure(g) if r["red_line"]}
    injected_run = bool(injected)

    subtitle = (
        "injected failure: " + ", ".join(injected)
        if injected
        else "topology, coloured by sector"
    )
    graph_label = (
        f'<<FONT POINT-SIZE="18" COLOR="{_INK}"><B>cascade-map</B></FONT>'
        f'<BR/><FONT POINT-SIZE="11" COLOR="{_INK2}">{_esc_html(subtitle)}</FONT>>'
    )

    lines = [
        "digraph cascade_map {",
        f'  graph [bgcolor="{_SURFACE}", rankdir=LR, nodesep=0.45, ranksep=1.0,'
        f' fontname="Helvetica", label={graph_label}, labelloc="t", labeljust="l"];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica"];',
        f'  edge [color="{_MUTED}", penwidth=1.2, arrowsize=0.8, fontname="Helvetica",'
        f' fontsize=9, fontcolor="{_INK2}"];',
    ]
    for n in g.nodes:
        lines.append(
            _node_dot(
                n, injected_run=injected_run, inj=inj, failed=failed, red_lines=red_lines
            )
        )
    for e in g.edges:
        lines.append(f'  "{_esc(e.dst)}" -> "{_esc(e.src)}" [label="{_esc(e.type)}"];')

    sectors_used = list(dict.fromkeys(n.sector for n in g.nodes if n.sector))
    lines += _legend(injected_run, sectors_used)
    lines.append("}")
    return "\n".join(lines)
