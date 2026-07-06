"""cascade-map analysis — the decision-useful layer over the reachability engine.

  * time-to-critical — the first essential service lost;
  * ride-through breaches — essential/regulated services that fail earlier than
    the minutes they are mandated to survive;
  * single points of failure — solo-injection blast radius per node;
  * NIS2 exposure — the headline: high-risk suppliers that are also structural
    single points of failure hitting an essential entity;
  * Monte-Carlo sensitivity — sample the [low, high] buffers so the modelled
    constants become distributions, not single invented numbers.

Everything here builds on ``engine.propagate``; no new failure semantics.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from cascade_map.engine import (
    Failure,
    Graph,
    _fmt_t,
    propagate,
    render_timeline,
)


# --------------------------------------------------------------------------- #
# Small graph helpers
# --------------------------------------------------------------------------- #
def _node(g: Graph, nid: str):
    return next(n for n in g.nodes if n.id == nid)


def _crit(g: Graph, nid: str) -> str:
    return _node(g, nid).criticality


def _blast(g: Graph, nid: str) -> set[str]:
    """The set of nodes that fail if `nid` alone is the initial failure."""
    return {f.node for f in propagate(g, [nid])}


# --------------------------------------------------------------------------- #
# Deterministic analyses (midpoint run)
# --------------------------------------------------------------------------- #
def time_to_critical(
    g: Graph, failures: list[Failure], injected: list[str]
) -> Failure | None:
    """First *essential* service lost, excluding the injected node(s)."""
    inj = set(injected)
    essential = [
        f for f in failures if f.node not in inj and _crit(g, f.node) == "essential"
    ]
    return min(essential, key=lambda f: (f.time, f.node)) if essential else None


def breaches(
    g: Graph, failures: list[Failure], injected: list[str]
) -> list[tuple[str, float, float, float]]:
    """(node, required, failed_at, minutes_early) for services that fail before
    their required ride-through. Injected nodes are excluded (they fail by fiat,
    not by breach)."""
    inj = set(injected)
    ftime = {f.node: f.time for f in failures}
    out = []
    for n in g.nodes:
        req = n.required_ride_through_min
        if req is None or n.id in inj or n.id not in ftime:
            continue
        if ftime[n.id] < req:
            out.append((n.id, float(req), ftime[n.id], float(req) - ftime[n.id]))
    out.sort(key=lambda r: (-r[3], r[0]))  # biggest shortfall first
    return out


def spof_scan(g: Graph) -> list[tuple[str, int]]:
    """(node, downstream_failure_count) for a solo injection of each node,
    ranked by blast radius. Downstream excludes the node itself."""
    rows = [(n.id, len(_blast(g, n.id)) - 1) for n in g.nodes]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def nis2_exposure(g: Graph) -> list[dict]:
    """For every node carrying a NIS2 vendor score, its structural exposure:
    solo blast radius, whether that blast reaches an essential entity, and the
    red-line flag (a high-risk supplier that is also a SPOF hitting essential
    services — the board slide)."""
    rows = []
    for n in g.nodes:
        if n.nis2_vendor_score is None:
            continue
        blast = _blast(g, n.id)
        downstream = len(blast) - 1
        hits_essential = any(m != n.id and _crit(g, m) == "essential" for m in blast)
        rows.append(
            {
                "node": n.id,
                "score": float(n.nis2_vendor_score),
                "downstream": downstream,
                "hits_essential": hits_essential,
                "red_line": downstream > 0 and hits_essential,
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["node"]))
    return rows


# --------------------------------------------------------------------------- #
# Monte-Carlo sensitivity over [low, high] buffers
# --------------------------------------------------------------------------- #
def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def monte_carlo(g: Graph, injected: list[str], runs: int = 2000, seed: int = 0) -> dict:
    # Non-cryptographic: sampling buffer ranges for sensitivity analysis, not
    # generating secrets. A seeded PRNG is exactly what reproducibility needs.
    rng = random.Random(seed)  # nosec B311
    ranged = [(n.id, n.autonomy_range) for n in g.nodes if n.autonomy_range]
    ttc: list[float] = []
    fail_ct: dict[str, int] = defaultdict(int)
    breach_ct: dict[str, int] = defaultdict(int)
    for _ in range(runs):
        override = {nid: rng.uniform(lo, hi) for nid, (lo, hi) in ranged}
        fs = propagate(g, injected, override)
        for f in fs:
            fail_ct[f.node] += 1
        crit = time_to_critical(g, fs, injected)
        if crit is not None:
            ttc.append(crit.time)
        for nid, *_ in breaches(g, fs, injected):
            breach_ct[nid] += 1
    return {
        "runs": runs,
        "ttc": {
            "n": len(ttc),
            "p10": _percentile(ttc, 0.10),
            "p50": _percentile(ttc, 0.50),
            "p90": _percentile(ttc, 0.90),
            "min": min(ttc) if ttc else None,
            "max": max(ttc) if ttc else None,
        },
        "fail_prob": {k: v / runs for k, v in fail_ct.items()},
        "breach_prob": {k: v / runs for k, v in breach_ct.items()},
    }


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def render_report(
    g: Graph,
    injected: list[str],
    runs: int = 2000,
    seed: int = 0,
    monte: bool = True,
) -> str:
    fs = propagate(g, injected)
    lines: list[str] = []
    lines.append("cascade-map — analysis report")
    lines.append(
        f"graph: {len(g.nodes)} nodes, {len(g.edges)} edges"
        f"   |   injected: {', '.join(injected)}"
    )
    lines.append("")

    lines.append("## Failure timeline")
    lines.append(render_timeline(fs, len(g.nodes)))
    lines.append("")

    lines.append("## Time to critical")
    crit = time_to_critical(g, fs, injected)
    lines.append(
        f"  first essential service lost: {crit.node} at t={_fmt_t(crit.time)}"
        if crit
        else "  no essential service lost"
    )
    lines.append("")

    lines.append("## Ride-through breaches")
    br = breaches(g, fs, injected)
    if br:
        for nid, req, tf, early in br:
            lines.append(
                f"  {nid:<22} failed t={_fmt_t(tf)}  "
                f"(required {_fmt_t(req)}; {_fmt_t(early)} early)"
            )
    else:
        lines.append("  none")
    lines.append("")

    lines.append("## Single points of failure (solo blast radius)")
    for nid, ds in spof_scan(g):
        mark = "   <-- SPOF" if ds >= 2 else ""
        lines.append(f"  {nid:<22} downstream failures: {ds}{mark}")
    lines.append("")

    lines.append("## NIS2 exposure")
    ex = nis2_exposure(g)
    if ex:
        for r in ex:
            flag = (
                "   <== RED LINE (SPOF hitting an essential entity)"
                if r["red_line"]
                else ""
            )
            lines.append(
                f"  {r['node']:<22} score {r['score']:.1f}  "
                f"downstream {r['downstream']}{flag}"
            )
    else:
        lines.append("  no NIS2 scores in graph")
    lines.append("")

    if monte:
        lines.append(f"## Monte-Carlo sensitivity ({runs} runs over [low,high] buffers)")
        mc = monte_carlo(g, injected, runs, seed)
        t = mc["ttc"]
        if t["n"]:
            lines.append(
                f"  time-to-critical: p10 {_fmt_t(t['p10'])}  "
                f"p50 {_fmt_t(t['p50'])}  p90 {_fmt_t(t['p90'])}  "
                f"(range {_fmt_t(t['min'])}–{_fmt_t(t['max'])})"
            )
        else:
            lines.append("  no essential service lost in any sampled scenario")
        bp = mc["breach_prob"]
        if bp:
            for nid in sorted(bp, key=lambda k: (-bp[k], k)):
                lines.append(f"  P(breach {nid}) = {bp[nid] * 100:.0f}%")
        else:
            lines.append("  no ride-through breaches across sampled scenarios")

    return "\n".join(lines)
