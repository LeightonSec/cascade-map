"""cascade-map engine — dependency-reachability model for critical infrastructure.

Given a dependency graph and an injected initial failure, compute *what fails,
in what order, and how fast* — a threshold + time-buffer reachability front.

This is NOT a load-redistribution cascade simulator and NOT a recovery model;
both are out of scope by design (see DESIGN.md). A node is either up or failed
at a given time; each failure additionally carries a *kind* (physical /
control_loss / capacity_degraded) derived from the edge that caused it, so
"lost telemetry" is no longer conflated with "physically stopped".
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
    purdue_level: float | None = None  # Purdue/ISA-95 level; None = unclassified (Gate 6)


@dataclass
class Edge:
    src: str  # this node depends on...
    dst: str  # ...this target, to operate
    type: str = "generic"
    redundancy: int = 0  # independent equivalents; 0 = no backup
    failure_offset: str = "none"  # per-edge failure characterization: none|partial|full
    note: str = ""


# Failure kinds — what losing a dependency actually does to the dependent node.
KIND_PHYSICAL = "physical"  # node stops functioning
KIND_CONTROL_LOSS = "control_loss"  # node keeps running but loses visibility/control
KIND_CAPACITY_DEGRADED = "capacity_degraded"  # node keeps running at reduced capacity

FAILURE_KINDS = {KIND_PHYSICAL, KIND_CONTROL_LOSS, KIND_CAPACITY_DEGRADED}
FAILURE_OFFSETS = {"none", "partial", "full"}


def derive_failure_kind(edge_type: str, failure_offset: str = "none") -> str:
    """The kind of failure an unmet edge inflicts on its dependent node.

    Derived from the edge's dependency type — never assigned freely per node:
      * ``data`` -> control_loss (telemetry/control lost; the node keeps running)
      * everything else (power, physical_route, supply, fuel, staff, generic,
        ...) -> physical. Unmapped dependency types default to physical
        (conservative worst-case) pending explicit classification — a risk
        tool must not quietly downgrade an impact it does not understand.
    A ``partial`` failure_offset overrides the base mapping: the dependency is
    partially backed up, so the node degrades instead of taking the full hit.

    ``full`` never reaches this function: fully backed-up edges are excluded
    from the unmet computation in ``propagate`` and can never drive a failure,
    so being asked to derive a kind for one is a caller bug — fail loudly.
    """
    if failure_offset == "full":
        raise ValueError(
            "a fully backed-up edge cannot drive a failure; derive_failure_kind "
            "must not be called with failure_offset='full'"
        )
    if failure_offset == "partial":
        return KIND_CAPACITY_DEGRADED
    if edge_type == "data":
        return KIND_CONTROL_LOSS
    return KIND_PHYSICAL


# --------------------------------------------------------------------------- #
# Purdue / IT–OT boundary overlay (Gate 6)
# --------------------------------------------------------------------------- #
# A classification overlay only — never touches failure timing. Nodes may carry
# a Purdue/ISA-95 level; each edge's boundary facts are *derived* from its two
# endpoints' zones (never assigned in the graph). The IDMZ (level 3.5) is a
# first-class level, so a DMZ broker/jump-host/historian is a real node, not an
# artefact inferred from edge geometry.
#
# IMPORTANT — purdue_level is a CATEGORICAL label that merely happens to be typed
# as a number in YAML. Treat it as an enum: compare by exact membership/equality
# only, NEVER do arithmetic on it. 3.5 is exactly representable in float so
# `== 3.5` is safe forever, but `level - 3.5 < epsilon`, averaging levels, or
# "distance to the IDMZ" are the float-equality bugs that pass clean tests here
# and fail on someone else's messy graph. Any future gate that wants ordinal
# math must first map the level through an explicit lookup, not compute on it.
PURDUE_LEVELS = frozenset({0, 1, 2, 3, 3.5, 4, 5})
_OT_LEVELS = frozenset({0, 1, 2, 3})
_DMZ_LEVEL = 3.5
# levels 4, 5 are IT

ZONE_OT = "OT"
ZONE_DMZ = "DMZ"
ZONE_IT = "IT"
ZONE_UNKNOWN = "unknown"

# Tri-state for derived boundary facts: a missing level yields UNKNOWN, which is
# surfaced explicitly — never silently downgraded to FALSE ("unknown != safe").
TRI_TRUE = "true"
TRI_FALSE = "false"
TRI_UNKNOWN = "unknown"


def zone(level: float | None) -> str:
    """OT (0–3) / DMZ (3.5) / IT (4–5) / unknown (None). Levels are validated to
    ``PURDUE_LEVELS`` at load time, so the final branch is exactly {4, 5}."""
    if level is None:
        return ZONE_UNKNOWN
    if level in _OT_LEVELS:
        return ZONE_OT
    if level == _DMZ_LEVEL:
        return ZONE_DMZ
    return ZONE_IT


def derive_touches_dmz(zsrc: str, zdst: str) -> str:
    """An endpoint is a DMZ node: the designed, brokered path (or a lateral hop
    inside the DMZ). Not called ``crosses_idmz`` — a DMZ↔DMZ edge never leaves
    the zone, so 'crossing' would be a false positive."""
    if zsrc == ZONE_DMZ or zdst == ZONE_DMZ:
        return TRI_TRUE
    if zsrc == ZONE_UNKNOWN or zdst == ZONE_UNKNOWN:
        return TRI_UNKNOWN  # an unknown endpoint might itself be a DMZ node
    return TRI_FALSE


def derive_violates_purdue_direct(zsrc: str, zdst: str) -> str:
    """A raw IT↔OT edge with no DMZ endpoint: IT talking straight to OT with no
    intermediary — the Stuxnet-shaped hole. A DMZ endpoint means a brokered leg,
    never a direct bypass."""
    if zsrc == ZONE_DMZ or zdst == ZONE_DMZ:
        return TRI_FALSE
    if {zsrc, zdst} == {ZONE_OT, ZONE_IT}:
        return TRI_TRUE
    if zsrc == ZONE_UNKNOWN or zdst == ZONE_UNKNOWN:
        return TRI_UNKNOWN
    return TRI_FALSE


def derive_crosses_sector(ssrc: str, sdst: str) -> str:
    """True iff both sectors are known and differ; unknown if either is blank."""
    if not ssrc or not sdst:
        return TRI_UNKNOWN
    return TRI_TRUE if ssrc != sdst else TRI_FALSE


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
        lvl = n.get("purdue_level")
        # Enforce the exact literal domain — this is the load-bearing guarantee
        # zone() relies on so its fallback branch is exactly {4, 5}. bool is an
        # int subclass, so reject it before the membership test (True == 1).
        if lvl is not None and (
            isinstance(lvl, bool) or lvl not in PURDUE_LEVELS
        ):
            raise ValueError(
                f"node {n['id']}: purdue_level must be one of "
                f"{sorted(PURDUE_LEVELS)} or omitted, got {lvl!r}"
            )
    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            raise ValueError(f"edge #{i} must be a mapping")
        for key in ("from", "to"):
            if not isinstance(e.get(key), str) or not e[key]:
                raise ValueError(f"edge #{i} needs a non-empty string '{key}'")
        red = e.get("redundancy", 0)
        if isinstance(red, bool) or not isinstance(red, int) or red < 0:
            raise ValueError(f"edge #{i}: redundancy must be a non-negative integer")
        if "failure_kind" in e:
            raise ValueError(
                f"edge #{i}: failure_kind is derived from the edge type and cannot "
                "be assigned in the graph — remove it"
            )
        for derived in ("touches_dmz", "violates_purdue_direct", "crosses_sector"):
            if derived in e:
                raise ValueError(
                    f"edge #{i}: {derived} is derived from the endpoints' Purdue "
                    "levels/sectors and cannot be assigned in the graph — remove it"
                )
        off = e.get("failure_offset", "none")
        if off not in FAILURE_OFFSETS:
            raise ValueError(
                f"edge #{i}: failure_offset must be one of {sorted(FAILURE_OFFSETS)}"
            )


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
                purdue_level=n.get("purdue_level"),
            )
        )
    edges = [
        Edge(
            src=e["from"],
            dst=e["to"],
            type=e.get("type", "generic"),
            redundancy=int(e.get("redundancy", 0)),
            failure_offset=e.get("failure_offset", "none"),
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
    kind: str  # physical | control_loss | capacity_degraded; no default — a
    # construction site that forgets to derive it must fail loudly, not
    # silently report worst-case
    driver: str | None = None  # the target node whose failure drove this one;
    # None for injected nodes. Structured so the Gate 6 boundary analysis can
    # classify the driving edge without parsing the human-readable reason string.


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
      * An edge with ``failure_offset: full`` is fully backed up outside the
        model: its target failing can never make the dependency unmet.
      * Each failure carries a *kind* derived from the driving edge
        (``derive_failure_kind``); ``partial`` offsets degrade instead.
    """
    ids = g.node_ids()
    for i in injected:
        if i not in ids:
            raise ValueError(f"cannot inject unknown node: {i}")

    deps: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    red: dict[tuple[str, str], int] = {}
    offset: dict[tuple[str, str, str], str] = {}
    _protection = {"none": 0, "partial": 1, "full": 2}
    for e in g.edges:
        key3 = (e.src, e.type, e.dst)
        # conservative: if duplicate edges disagree, assume the least protection
        if key3 not in offset or _protection[e.failure_offset] < _protection[offset[key3]]:
            offset[key3] = e.failure_offset
    for e in g.edges:
        if offset[(e.src, e.type, e.dst)] == "full":
            continue  # fully backed up: this edge can never drive an unmet dependency
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
    kind: dict[str, str] = {}
    drivers: dict[str, str] = {}  # node -> target whose failure drove it (Gate 6)
    injected_set = set(injected)
    for i in injected:
        fail[i] = 0.0
        reason[i] = "injected"
        kind[i] = KIND_PHYSICAL  # injected = the node itself is down, by fiat

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
                kind[n.id] = derive_failure_kind(typ, offset[(n.id, typ, driver)])
                drivers[n.id] = driver
                changed = True
        if not changed:
            break
    else:
        raise RuntimeError("propagation did not converge (unexpected)")

    failures = [
        Failure(
            node=nid,
            time=fail[nid],
            reason=reason[nid],
            kind=kind[nid],
            driver=drivers.get(nid),  # None for injected nodes
        )
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
        lines.append(f"  {tok:<10} {f.node:<22} {f.kind:<18} {f.reason}")
    lines.append("")
    n_failed = len(failures)
    survivors = total_nodes - n_failed
    tail = "none" if survivors == 0 else f"{survivors}"
    lines.append(f"{n_failed} of {total_nodes} nodes failed.  Survivors: {tail}.")
    return "\n".join(lines)
