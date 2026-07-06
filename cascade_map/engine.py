"""cascade-map engine — dependency-reachability model for critical infrastructure.

Given a dependency graph and an injected initial failure, compute *what fails,
in what order, and how fast* — a threshold + time-buffer reachability front.

This is NOT a load-redistribution cascade simulator and NOT a recovery model;
both are out of scope by design (see DESIGN.md). Failure is binary — a node is
either up or failed; "degraded" states are not modelled.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import yaml

INF = float("inf")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    id: str
    label: str = ""
    sector: str = ""
    criticality: str = "supporting"
    autonomy_minutes: float = 0.0  # midpoint of a [low, high] range, or a point
    autonomy_range: tuple[float, float] | None = None  # raw range, for Monte-Carlo
    nis2_vendor_score: float | None = None
    required_ride_through_min: float | None = None  # min minutes it must survive


@dataclass
class Edge:
    src: str  # this node depends on...
    dst: str  # ...this target, to operate
    type: str = "generic"
    redundancy: int = 0  # independent equivalents; 0 = no backup
    note: str = ""


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def node_ids(self) -> set[str]:
        return {n.id for n in self.nodes}


def _midpoint(v) -> float:
    """A time buffer may be a point value or a [low, high] range; use the mid."""
    if isinstance(v, (list, tuple)):
        if len(v) != 2:
            raise ValueError(f"range must be [low, high], got {v!r}")
        return (float(v[0]) + float(v[1])) / 2.0
    return float(v)


def _resolve_autonomy(v) -> tuple[float, tuple[float, float] | None]:
    """Return (deterministic midpoint, raw range or None) for a buffer value."""
    mid = _midpoint(v)
    rng = (float(v[0]), float(v[1])) if isinstance(v, (list, tuple)) else None
    return mid, rng


_CRITICALITIES = {"essential", "important", "supporting"}


def _check_buffer(v, nid: str) -> None:
    if isinstance(v, (list, tuple)):
        if len(v) != 2 or not all(isinstance(x, (int, float)) for x in v):
            raise ValueError(f"node {nid}: autonomy range must be [low, high] numbers")
        if v[0] > v[1]:
            raise ValueError(f"node {nid}: autonomy range has low > high")
    elif not isinstance(v, (int, float)):
        raise ValueError(f"node {nid}: autonomy_minutes must be a number or [low, high]")


def _validate_schema(raw) -> None:
    """Validate the *shape* of a loaded graph document before trusting it.
    External YAML is untrusted input; fail loudly with a clear message rather
    than deep inside a comprehension."""
    if not isinstance(raw, dict):
        raise ValueError("graph document must be a mapping with 'nodes' and 'edges'")
    nodes = raw.get("nodes", [])
    edges = raw.get("edges", [])
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("'nodes' and 'edges' must be lists")
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            raise ValueError(f"node #{i} must be a mapping")
        if not isinstance(n.get("id"), str) or not n["id"]:
            raise ValueError(f"node #{i} needs a non-empty string 'id'")
        crit = n.get("criticality", "supporting")
        if crit not in _CRITICALITIES:
            raise ValueError(f"node {n['id']}: criticality must be one of {_CRITICALITIES}")
        _check_buffer(n.get("autonomy_minutes", 0), n["id"])
        for fld in ("nis2_vendor_score", "required_ride_through_min"):
            val = n.get(fld)
            if val is not None and not isinstance(val, (int, float)):
                raise ValueError(f"node {n['id']}: {fld} must be a number")
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            raise ValueError(f"edge #{i} must be a mapping")
        for key in ("from", "to"):
            if not isinstance(e.get(key), str) or not e[key]:
                raise ValueError(f"edge #{i} needs a non-empty string '{key}'")
        red = e.get("redundancy", 0)
        if isinstance(red, bool) or not isinstance(red, int) or red < 0:
            raise ValueError(f"edge #{i}: redundancy must be a non-negative integer")


def load_graph(path: str) -> Graph:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    _validate_schema(raw)  # validate untrusted input shape before consuming it
    nodes = []
    for n in raw.get("nodes", []):
        mid, rng = _resolve_autonomy(n.get("autonomy_minutes", 0))
        nodes.append(
            Node(
                id=n["id"],
                label=n.get("label", n["id"]),
                sector=n.get("sector", ""),
                criticality=n.get("criticality", "supporting"),
                autonomy_minutes=mid,
                autonomy_range=rng,
                nis2_vendor_score=n.get("nis2_vendor_score"),
                required_ride_through_min=n.get("required_ride_through_min"),
            )
        )
    edges = [
        Edge(
            src=e["from"],
            dst=e["to"],
            type=e.get("type", "generic"),
            redundancy=int(e.get("redundancy", 0)),
            note=e.get("note", ""),
        )
        for e in raw.get("edges", [])
    ]
    g = Graph(nodes=nodes, edges=edges)
    _validate(g)
    return g


def _validate(g: Graph) -> None:
    ids = g.node_ids()
    if len(ids) != len(g.nodes):
        raise ValueError("duplicate node ids in graph")
    for e in g.edges:
        if e.src not in ids:
            raise ValueError(f"edge references unknown node: {e.src}")
        if e.dst not in ids:
            raise ValueError(f"edge references unknown node: {e.dst}")


# --------------------------------------------------------------------------- #
# Propagation engine
# --------------------------------------------------------------------------- #
@dataclass
class Failure:
    node: str
    time: float
    reason: str


def propagate(
    g: Graph,
    injected: list[str],
    autonomy_override: dict[str, float] | None = None,
) -> list[Failure]:
    """Compute each node's failure time via monotone relaxation (Bellman-Ford
    style). Times only ever decrease and are bounded below by 0 (buffers are
    non-negative), so the fixpoint converges; cycles without an injected feed
    simply stay at INF (never fail).

    ``autonomy_override`` replaces individual nodes' buffers for a single run
    (used by the Monte-Carlo sampler); unlisted nodes keep their midpoint.

    Semantics (DESIGN.md §2):
      * A dependency *type* T of node N is UNMET when (redundancy + 1) of its
        targets have failed; the unmet time is the (redundancy+1)-th earliest
        target failure.
      * Different types are independently required (AND); redundant targets
        within a type are the OR/backup.
      * N fails at (earliest unmet-time across its required types) + autonomy.
    """
    ids = g.node_ids()
    for i in injected:
        if i not in ids:
            raise ValueError(f"cannot inject unknown node: {i}")

    deps: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    red: dict[tuple[str, str], int] = {}
    for e in g.edges:
        deps[e.src][e.type].append(e.dst)
        key = (e.src, e.type)
        # conservative: if edges disagree, assume the fewest backups
        red[key] = min(red.get(key, e.redundancy), e.redundancy)

    if autonomy_override:
        autonomy = {
            n.id: autonomy_override.get(n.id, n.autonomy_minutes) for n in g.nodes
        }
    else:
        autonomy = {n.id: n.autonomy_minutes for n in g.nodes}
    fail = {n.id: INF for n in g.nodes}
    reason: dict[str, str] = {}
    injected_set = set(injected)
    for i in injected:
        fail[i] = 0.0
        reason[i] = "injected"

    for _ in range(len(g.nodes) + 1):  # bounded; converges within |V| passes
        changed = False
        for n in g.nodes:
            if n.id in injected_set:
                continue
            best_unmet = INF
            best_reason = None
            for typ, targets in deps[n.id].items():
                r = red[(n.id, typ)]
                ordered = sorted(targets, key=lambda t: fail[t])
                if len(ordered) < r + 1:
                    continue  # redundancy can never be exhausted -> type stays met
                driver = ordered[r]
                unmet = fail[driver]
                if unmet < best_unmet:
                    best_unmet = unmet
                    best_reason = (typ, driver, unmet)
            if best_unmet is INF:
                continue
            cand = best_unmet + autonomy[n.id]
            if cand < fail[n.id]:
                fail[n.id] = cand
                typ, driver, unmet = best_reason
                reason[n.id] = (
                    f"{typ} dependency unmet ({driver} failed at t={_fmt_t(unmet)})"
                )
                changed = True
        if not changed:
            break
    else:
        raise RuntimeError("propagation did not converge (unexpected)")

    failures = [
        Failure(node=nid, time=fail[nid], reason=reason[nid])
        for nid in fail
        if fail[nid] < INF
    ]
    failures.sort(key=lambda f: (f.time, f.node))  # stable, byte-deterministic
    return failures


# --------------------------------------------------------------------------- #
# Rendering (text-first; deterministic for byte-checking)
# --------------------------------------------------------------------------- #
def _fmt_t(v: float) -> str:
    return f"{int(v)}min" if v == int(v) else f"{v:.1f}min"


def render_timeline(failures: list[Failure], total_nodes: int) -> str:
    lines = []
    for f in failures:
        tok = f"t={_fmt_t(f.time)}"
        lines.append(f"  {tok:<10} {f.node:<22} {f.reason}")
    lines.append("")
    n_failed = len(failures)
    survivors = total_nodes - n_failed
    tail = "none" if survivors == 0 else f"{survivors}"
    lines.append(f"{n_failed} of {total_nodes} nodes failed.  Survivors: {tail}.")
    return "\n".join(lines)
