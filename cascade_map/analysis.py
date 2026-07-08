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
    TRI_TRUE,
    TRI_UNKNOWN,
    ZONE_OT,
    Failure,
    Graph,
    _fmt_t,
    derive_crosses_sector,
    derive_touches_dmz,
    derive_violates_purdue_direct,
    propagate,
    render_timeline,
    zone,
)


# --------------------------------------------------------------------------- #
# Small graph helpers
# --------------------------------------------------------------------------- #
def _node(g: Graph, nid: str):
    return next(n for n in g.nodes if n.id == nid)


def _crit(g: Graph, nid: str) -> str:
    return _node(g, nid).criticality


def _zone(g: Graph, nid: str) -> str:
    return zone(_node(g, nid).purdue_level)


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


# Worst-first: a physical stop outranks lost control outranks reduced capacity.
_KIND_SEVERITY = {"capacity_degraded": 0, "control_loss": 1, "physical": 2}


def _worst_kind(kinds: list[str]) -> str | None:
    return max(kinds, key=lambda k: _KIND_SEVERITY[k]) if kinds else None


def nis2_exposure(g: Graph) -> list[dict]:
    """For every node carrying a NIS2 vendor score, its structural exposure:
    solo blast radius, whether that blast reaches an essential entity, the
    red-line flag (a high-risk supplier that is also a SPOF hitting essential
    services — the board slide), and the worst failure kind in the blast —
    a red line whose downstream impact is physical reads very differently
    from one that is control_loss.

    Uses ``propagate`` directly (not ``_blast``) because it needs the failure
    *kinds*, which ``_blast`` discards; the blast set is identical."""
    rows = []
    for n in g.nodes:
        if n.nis2_vendor_score is None:
            continue
        fs = propagate(g, [n.id])
        blast = {f.node for f in fs}
        downstream = len(blast) - 1
        hits_essential = any(m != n.id and _crit(g, m) == "essential" for m in blast)
        rows.append(
            {
                "node": n.id,
                "score": float(n.nis2_vendor_score),
                "downstream": downstream,
                "hits_essential": hits_essential,
                "red_line": downstream > 0 and hits_essential,
                "impact": _worst_kind([f.kind for f in fs if f.node != n.id]),
                # Gate 6 composition: this vendor's solo blast reaches an
                # essential OT node via a direct Purdue violation (no IDMZ on the
                # path) — the worst finding the tool can produce.
                "bypass_to_essential_ot": bool(bypass_red_lines(g, fs)),
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["node"]))
    return rows


# --------------------------------------------------------------------------- #
# Purdue / IT–OT boundary overlay (Gate 6)
# --------------------------------------------------------------------------- #
def boundary_crossings(g: Graph, failures: list[Failure]) -> list[dict]:
    """Classify the *driving* dependency of each failure against the Purdue
    boundary. One row per failure that has a driver; the boundary facts are
    derived by the same engine functions the truth table was verified against,
    so this can never drift from ``propagate``'s own reason string.

    Injected root causes (``driver is None``) are intentionally absent — they are
    not crossings (no driving edge exists) and are reported explicitly in the
    failure timeline with reason 'injected', so this is exclusion-by-definition,
    not silent dropping. Edge direction is failure-flow: driver → dependent.
    ``derive_*`` are symmetric in their two zone args, so classification does not
    depend on argument order."""
    rows = []
    for f in failures:
        if f.driver is None:
            continue
        zf = _zone(g, f.node)  # the dependent node that failed
        zd = _zone(g, f.driver)  # the dependency whose failure drove it
        rows.append(
            {
                "node": f.node,
                "driver": f.driver,
                "flow": f"{zd}->{zf}",
                "touches_dmz": derive_touches_dmz(zf, zd),
                "violates_purdue_direct": derive_violates_purdue_direct(zf, zd),
                "crosses_sector": derive_crosses_sector(
                    _node(g, f.node).sector, _node(g, f.driver).sector
                ),
            }
        )
    rows.sort(key=lambda r: (r["node"], r["driver"]))
    return rows


def bypass_red_lines(g: Graph, failures: list[Failure]) -> list[dict]:
    """The headline: essential OT nodes that failed because a dependency crossed
    the IT/OT boundary *directly* (no IDMZ on the path) — an IT-side failure
    reaching essential OT. Bound to ``violates_purdue_direct`` on the driving
    edge, never to a string assembled from zones elsewhere, so it cannot inherit
    the conflated 'touches DMZ' ambiguity."""
    return [
        c
        for c in boundary_crossings(g, failures)
        if c["violates_purdue_direct"] == TRI_TRUE
        and _zone(g, c["node"]) == ZONE_OT
        and _crit(g, c["node"]) == "essential"
    ]


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
            if r.get("bypass_to_essential_ot"):
                flag += "  [IT→OT BYPASS: reaches essential OT with no IDMZ]"
            impact = f"  [worst impact: {r['impact']}]" if r["impact"] else ""
            lines.append(
                f"  {r['node']:<22} score {r['score']:.1f}  "
                f"downstream {r['downstream']}{flag}{impact}"
            )
    else:
        lines.append("  no NIS2 scores in graph")
    lines.append("")

    lines.append("## Purdue / IT–OT boundary")
    bc = boundary_crossings(g, fs)
    brl = bypass_red_lines(g, fs)
    if brl:
        for c in brl:
            lines.append(
                f"  {c['node']:<22} <== IT→OT BYPASS red line "
                f"(driven by {c['driver']}, no IDMZ on path)"
            )
    else:
        lines.append("  no IT→OT bypass reaches an essential OT node")
    viol = [c for c in bc if c["violates_purdue_direct"] == TRI_TRUE]
    if viol:
        lines.append("  direct Purdue violations on failure path:")
        for c in viol:
            lines.append(f"    {c['driver']} -> {c['node']}  ({c['flow']})")
    dmz = [c for c in bc if c["touches_dmz"] == TRI_TRUE]
    if dmz:
        lines.append("  IDMZ-routed dependencies on failure path (designed):")
        for c in dmz:
            lines.append(f"    {c['driver']} -> {c['node']}  ({c['flow']})")
    xsec = [c for c in bc if c["crosses_sector"] == TRI_TRUE]
    if xsec:
        lines.append("  cross-sector dependencies on failure path:")
        for c in xsec:
            lines.append(f"    {c['driver']} -> {c['node']}  ({c['flow']})")
    unk = [
        c for c in bc if TRI_UNKNOWN in (c["touches_dmz"], c["violates_purdue_direct"])
    ]
    if unk:
        lines.append(
            f"  {len(unk)} driving dependency(ies) unclassified (Purdue levels missing)"
        )
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
