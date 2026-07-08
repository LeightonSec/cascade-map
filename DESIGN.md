# cascade-map — DESIGN (Gate 1: LOCKED 2026-07-06)

Status: **Gate 1 locked.** Design reviewed via LLM Council (2026-07-06) and
revised below. No code until Gate 2. This document is the contract.
**Gate 6 (Purdue / IT–OT boundary overlay) LOCKED 2026-07-08** — see §7.

> **Naming & scope declaration (read first).** This tool models
> **dependency-reachability with time buffers** — "function A stops because a
> function it needs stopped, after A's local buffer drains." It does **not**
> model **load-redistribution cascades** (where a failed node's load shifts to
> neighbours that then trip from overload — the 2003 Northeast blackout
> mechanism). Real grid collapses are often the redistribution kind, and this
> model structurally cannot represent them. The name "cascade-map" is kept for
> the *effect* it traces (one failure reaching many services over time), not as
> a claim about the *mechanism*. Stating this limit precisely is deliberate:
> the honesty is the point, and naming a model's boundary is the OT/ICS
> maturity this artifact is meant to demonstrate.

## 1. What it is (and what it is not)

A **dependency-reachability model** for critical infrastructure, with a NIS2
risk overlay as its spine. You describe a sector topology as a graph of
"function A depends on function B"; you attach NIS2 vendor-risk scores to the
nodes; you inject an initial failure (a substation drops, a DNS provider dies, a
Carrington-class transformer loss); the tool propagates unmet dependencies
through the graph respecting redundancy and each node's time buffer, and reports
**what fails, in what order, how fast, which regulated entities it drags below
tolerated downtime, and where the irreplaceable bottlenecks sit.**

- It is a **modelling / GRC tool** using public or illustrative topology.
- It is **not** a real-time monitor, not a scanner, not a flow/physics
  simulator, and it ingests **no classified or live operational data.** That
  boundary is deliberate so the artifact is unambiguously defensive.

### Why this, why now

The falsifiable core of the "grid is the real target" scenario is a claim you
can model: *interdependencies between power, telecoms, water, and finance are
unmanaged, and a small initial failure reaches essential services faster than
they can be restored.* This tool makes that claim concrete and testable instead
of rhetorical — the OT/ICS + GRC lane.

### The spine: composition with the NIS2 vendor-risk framework

**This is the tool's headline, not a side feature.** The NIS2 project scores a
single third party. cascade-map imports those scores **as node attributes** and
answers the question a board actually asks: *"which of our highest-risk
suppliers also sit at a single point of failure that pulls an essential entity
below its tolerated downtime?"* A high-NIS2-risk vendor that is also a
structural SPOF is the exact thing a regulator and a CISO both lose sleep over —
and a working NIS2 framework to plug in is the differentiator no other
job-hunt portfolio has.

## 2. Conceptual model

A **directed graph.** Nodes are infrastructure *functions or assets*; edges are
*dependencies* ("source needs target to operate").

### Node

Time-buffer and restore fields take **either a point value or a `[low, high]`
range.** Ranges are what make the invented constants defensible — see Gate 3
(sensitivity analysis) — while a point value (or the range midpoint) drives the
deterministic Gate 2 run.

```yaml
- id: mobile_core_region_x
  label: "Mobile core network (Region X)"
  sector: telecom            # power | telecom | water | finance | health | ...
  criticality: essential     # essential | important | supporting  (NIS2-aligned)
  autonomy_minutes: [120, 360]     # buffer with all deps gone (battery/genset); point value also allowed
  nis2_vendor_score: 7.5           # imported from the NIS2 framework; optional
  required_ride_through_min: 60    # min minutes it must stay up in an incident; failing earlier = breach
```

### Edge (dependency)

```yaml
- from: mobile_core_region_x
  to: grid_substation_12
  type: power                # power | data | fuel | water | staff | ...
  redundancy: 1              # independent equivalents (0 = no backup)
  failure_offset: none       # none | partial | full — per-edge failure characterization (Gate 5)
  note: "on-site diesel; edge unmet only when both mains AND fuel gone"
```

`redundancy` (integer) and `failure_offset` (enum) are deliberately separate
concepts: `redundancy` counts independent supplying sources at the node and
composes dynamically (the node survives until all N are gone), while
`failure_offset` is a static per-edge category — `full` means this edge is
completely backed up outside the model and can never propagate failure;
`partial` means the dependent node degrades (`capacity_degraded`) instead of
taking the full hit; `none` is an ordinary edge.

### Failure kinds (Gate 5)

Every failure carries a **kind**, *derived from the edge type* — never
assigned freely per node:

- `data` unmet → `control_loss` — the node keeps running but loses
  visibility/control (a water plant does not physically halt when it only
  loses telemetry);
- everything else (`power`, `physical_route`, `supply`, …) → `physical`.

Unmapped dependency types default to `physical` (conservative worst-case)
pending explicit classification — a risk tool must not quietly downgrade an
impact it does not understand. A `partial` failure_offset overrides the base
mapping to `capacity_degraded`.

**Known limitation — kinds are edge-local, not inherited.** A node's failure
kind derives solely from its own unmet edge; upstream severity is not
consulted. Nodes downstream of a merely *degraded* node therefore report full
severity, so a chain behind a `capacity_degraded` node can read more severe
than reality. Severity inheritance/capping is deliberately deferred to a
future gate.

### Propagation semantics (specified before coded)

1. Start with an **initial failed set** F₀ (the injected event).
2. A required dependency of a given `type` is **unmet** when all edges of that
   type point to failed nodes and `redundancy` is exhausted. Edges with
   `failure_offset: full` never count — they are fully backed up.
3. A node with an unmet required dependency does **not** fail instantly — it
   fails after its `autonomy_minutes` buffer drains. This yields a
   **time-ordered** reachability front, not a flat set.
4. Each failure is tagged with the kind derived from its driving edge
   (see *Failure kinds*); timing is unaffected by kinds.
5. Repeat until no new failures. Output is a **timeline**.

**Restoration modelling is explicitly deferred** (see Gate plan). v1 traces
failure propagation only; it does not model recovery. This halves the semantics
for a feature the demo barely exercises.

This is a **threshold + time-buffer reachability** model. Load redistribution is
out of scope (see scope declaration). Both limits are stated, not hidden.

## 3. Outputs (NIS2 lens first, text-first rendering)

- **NIS2 exposure (headline):** which `essential`/`important` nodes fail, and
  the ranked list of failed nodes carrying high `nis2_vendor_score` — i.e.
  high-risk suppliers sitting on the failure path.
- **Ride-through breaches:** nodes that fail *before* their
  `required_ride_through_min` — a mandated survival time missed. (Corrected
  during Gate 3: the earlier `tolerated_downtime_min` framing was an *outage
  duration* metric, which needs a recovery model we deferred; ride-through is
  the equivalent that failure-times alone can actually compute.)
- **Failure timeline:** ordered (time, node, "failed because …").
- **Single points of failure:** nodes whose individual injection drives the
  largest downstream failure set.
- **Time-to-critical:** minutes until the first `essential` node fails.

Rendering is **text-first.** A Graphviz/`dot` render is optional, behind a
clearly-marked install extra — never a hard dependency, to stay inside the
fleet's lockfile/CI discipline.

## 4. Worked example (ships as the Gate 2 acceptance test)

```
grid_substation_12  --(power)-->  mobile_core, water_pump_station, bank_datacentre
mobile_core         --(data)-->   card_payment_switch
water_pump_station  --(power)-->  (autonomy 60 min: rooftop tank buffer)
bank_datacentre     --(power)-->  (autonomy 240 min: UPS + diesel)
card_payment_switch --(data)-->   (needs mobile_core; autonomy 0)
```

Inject: `grid_substation_12` fails at t=0. Expected deterministic timeline
(midpoint inputs), which is the byte-checked acceptance fixture:

- t=0    substation down
- t=0    card_payment_switch degraded (mobile_core now on battery)
- t=60m  water_pump_station fails → **essential**, time-to-critical = 60m
- t=240m mobile_core fails → card payments hard-down
- t=240m bank_datacentre fails

NIS2 headline the tool should surface: `grid_substation_12` (the grid operator,
highest `nis2_vendor_score`) is the single point of failure whose loss breaches
the essential `water_pump_station`'s required ride-through — that is the
board-slide line.

## 5. Non-goals / scope guards

- **Load-redistribution / overload cascades — out of scope** (reachability only).
- **Restoration/recovery — deferred to a later gate** (v1 = failure only).
- No live data, no scanning, no real utility topologies — illustrative only.
- Single-region scale; not a national digital-twin.

## 6. Gate plan (gate-build doctrine)

- **Gate 1 — this document. LOCKED.** Model, schema, semantics, scope
  declaration, worked example, and NIS2-as-spine all fixed.
- **Gate 2 — engine. SHIPPED 2026-07-06.** `cascade_map/engine.py` parses the
  YAML graph (point values / range midpoints), runs the deterministic
  failure-timeline propagation, and renders text only. The §4 example is the
  byte-checked golden fixture; correctness derived by hand from §4, plus golden
  + redundancy + survivor + CLI + input-schema validation. Python, stdlib +
  PyYAML.
- **Gate 3 — analysis + the reframe. SHIPPED 2026-07-06.**
  `cascade_map/analysis.py` adds time-to-critical, ride-through breaches, SPOF
  blast-radius scan, the **NIS2 exposure headline** (red-line = high-risk
  supplier that is also a SPOF hitting an essential entity), and **Monte-Carlo
  over the `[low, high]` ranges** so invented constants become sensitivity
  analysis (e.g. water breaches its ride-through in ~75% of sampled scenarios).
  Text report; hand-derived values + closed-form Monte-Carlo band. **Graphviz
  render SHIPPED 2026-07-06** (`cascade_map/render.py` + `cascade-map dot`):
  emits DOT with zero Python dependency — failure status, the injected node and
  the NIS2 red-line highlighted, essential entities shaped distinctly; the
  system `dot` binary rasterizes it.
- **Gate 4 — ship. SHIPPED 2026-07-06.** Fleet-standard package layout,
  two-job CI (test matrix 3.10–3.12 + bandit + ruff; pip-audit + security-gate
  pinned to the fleet SHA), hashed universal lockfiles at the 3.10 floor, gate
  green (one HIGH `missing_validation` resolved with real input validation, not
  a waiver), 25 tests. Published to `LeightonSec/cascade-map`.
- **NIS2 import pipeline — SHIPPED 2026-07-06.** `cascade-map import-nis2`
  (`cascade_map/nis2_import.py`) turns the composition claim from a manual
  convention into a demonstrable pipeline: it parses the vendor-risk
  framework's filled assessment report (its real deliverable), converts the
  overall compliance percentage onto the 0–10 risk scale
  (`risk = (100 − pct) / 10`, recorded in a provenance header), and merges it
  onto a named node. Fail-closed: unfilled template, ambiguous/missing rating,
  rating inconsistent with the framework's own score bands, out-of-range score,
  and unknown node are all hard errors. 15 tests incl. a round-trip through
  `load_graph` and the analysis layer; illustrative filled report in
  `examples/`.
- **Gate 5 — failure severity & redundancy typing. SHIPPED 2026-07-08**
  (v0.2.0). Per-edge `failure_offset` (none/partial/full) and derived failure
  *kinds* (physical / control_loss / capacity_degraded) — see §2. A
  classification overlay: timing/propagation unchanged.
- **Gate 6 — Purdue / IT–OT boundary overlay. LOCKED 2026-07-08.** See §7.
  Optional `purdue_level`; derived `touches_dmz`, `violates_purdue_direct`,
  `crosses_sector`; the IT→OT bypass red line. Another classification overlay —
  no new failure semantics.
- **Future (deferred) — restoration modelling.** Reverse propagation,
  restore-time bottlenecks (the 12-month HV transformer). Not scheduled.

**Locked decisions:** Python (matches fleet CI/gate/lockfile patterns; graph
work is trivial there; Rust buys nothing at hand-authored scale). Text-first
output; Graphviz optional. NIS2 overlay is the spine. Restoration deferred.

## 7. Gate 6 — Purdue / IT–OT boundary overlay (LOCKED 2026-07-08)

A **classification overlay** in the exact mould of Gate 5: it labels nodes with
their Purdue level and derives per-edge boundary facts. **No new failure
semantics** — timing and propagation are byte-identical to Gate 5 for graphs
without the new fields. Theme: *where does the cascade cross a trust boundary,
and is that crossing designed or a bypass?* Reviewed manually (Leighton,
2026-07-08); the LLM-Council pass was deliberately skipped — the gap council
exists to catch (a spec that silently conflates two findings) was found and
closed in that review (the `touches_dmz` / `violates_purdue_direct` split).

### 7.1 Why

The repo's thesis (§1) is unmanaged interdependency. Gate 6 makes the *most
security-relevant* one legible: the dependency spanning the IT/OT boundary — and
distinguishes one **routed through the IDMZ as designed** from one that
**bypasses it**. In the Purdue/ISA-95 model, OT (levels 0–3) and IT (levels 4–5)
are separated by an Industrial DMZ (level 3.5). A raw IT→OT edge with no DMZ node
on the path is the coupling that turns an IT incident (breached vendor SaaS,
ransomwared ERP) into an OT consequence (a plant loses its setpoints) — "the grid
is the real target," made concrete.

### 7.2 Node — optional `purdue_level` (DMZ is a real level)

```yaml
- id: water_treatment_plant
  sector: water
  purdue_level: 2       # OPTIONAL. Purdue/ISA-95 level.
- id: dmz_historian
  purdue_level: 3.5     # IDMZ is a first-class level — jump boxes / DMZ historians
                        # are real nodes, not inferred from edge geometry.
```

- **Domain: the discrete set `{0, 1, 2, 3, 3.5, 4, 5}`.** Optional; absent ⇒
  unclassified (`null`). Any other value (e.g. `2.5`) is a hard error. Not
  mandatory — that would break every existing example and force junk back-fill;
  rigor belongs in a later lint layer, not the data model.
- `3.5` is **numeric, not a `"dmz"` string** — keeps range checks and sort order
  as plain arithmetic, and matches how ISA-95/Purdue already names Level 3.5.
- Canonical meanings: 0 process, 1 basic control (PLC/RTU), 2 supervisory
  (SCADA/HMI), 3 site operations (historian/MES), 3.5 IDMZ (brokers/jump hosts),
  4 site business (ERP), 5 enterprise.
- **Zone (derived, never assigned):** OT `{0,1,2,3}` · DMZ `{3.5}` · IT `{4,5}`.

### 7.3 Edge — two derived boundary fields + `crosses_sector`

All **derived, never assigned** — same principle as Gate 5's `failure_kind`;
assigning any in YAML is a hard error. Edge is `from` (dependent) → `to`
(dependency).

- **`touches_dmz ∈ {true,false,unknown}`** — an endpoint is a DMZ node. The
  expected, brokered path (or a lateral hop inside the DMZ). Named `touches_dmz`,
  **not** `crosses_idmz`: a DMZ↔DMZ edge never leaves the zone, so "crossing"
  would be a false positive.
- **`violates_purdue_direct ∈ {true,false,unknown}`** — the edge spans IT `{4,5}`
  ↔ OT `{0,1,2,3}` **directly, no DMZ endpoint**: IT talking straight to OT, the
  Stuxnet-shaped hole. Kept separate from `touches_dmz` so a well-architected
  network (historian in place) and a badly-architected one (raw L5→L1 pipe) never
  produce the same signal, and the red-line analysis (7.4) can't inherit that
  ambiguity.
- **`crosses_sector ∈ {true,false,unknown}`** — `true` iff both `sector` known
  and differ.

**Derivation truth table** (endpoints unordered for classification; direction
kept for reporting only):

| endpoint zones | `touches_dmz` | `violates_purdue_direct` |
|---|---|---|
| OT–OT | false | false |
| IT–IT | false | false |
| **OT–IT** | false | **true** |
| OT–DMZ | true | false |
| IT–DMZ | true | false |
| DMZ–DMZ | true | false |
| DMZ–UNK | true | false |
| OT–UNK | unknown | unknown |
| IT–UNK | unknown | unknown |
| UNK–UNK | unknown | unknown |

By construction: a brokered path OT→DMZ→IT is **two edges, each
`touches_dmz=true`, zero violations**; a raw IT→OT pipe is **one edge,
`violates_purdue_direct=true`, `touches_dmz=false`**. **Unknown ≠ safe** — a
missing level yields `unknown`, surfaced explicitly, never silently `false`
(same conservative stance as Gate 5's "unmapped types default to physical").

### 7.4 Analysis (the headline) — built on `propagate`, no new semantics

1. **Boundary crossings on the failure path** — of the edges that *drove* a
   failure (reason edges), report `violates_purdue_direct` ones (bypasses),
   `touches_dmz` ones (designed DMZ), and `crosses_sector` ones — as distinct
   lines, never merged.
2. **IT→OT bypass red line (headline)** — an essential OT node that fails because
   its driving dependency is `violates_purdue_direct` from the IT side. Bound to
   `violates_purdue_direct`, so it can never inherit the conflated ambiguity.
3. **NIS2 × bypass composition** — elevate an existing NIS2 red-line when the
   high-risk SPOF vendor sits on the IT side and its blast reaches essential OT
   via a bypass: regulatory risk + segmentation violation + attack path in one
   node.
4. **Unknown-boundary hygiene** — count driver edges with `unknown` boundary
   fields and surface them ("Purdue levels missing"). Honesty over false
   assurance.

### 7.5 Stated limits (name the boundary)

- **Data-completeness, not architecture.** The IDMZ broker is a first-class node
  at level 3.5 — the schema fully supports it. The tool cannot *force* a modeller
  to include it; a real brokered path drawn as a single OT→IT edge (broker
  omitted) reads as `violates_purdue_direct`. That is the graph under-describing
  reality, **not** a modelling limit. Fix is data hygiene, not code.
- **Levels are modelling labels, not a scan** — asserted on public/illustrative
  topology; the tool never discovers them (consistent with the whole-repo "no
  live/scanned data" boundary).
- **No new timing** — overlay only; output byte-identical to Gate 5 without the
  new fields. **Unknown ≠ safe.**

### 7.6 Acceptance (golden pair)

A Purdue-annotated graph where an IT-side node (cloud historian / vendor SaaS at
L4) is injected and the cascade reaches an essential OT SCADA node (L1–L2) via a
**raw** edge → golden report shows `violates_purdue_direct (IT→OT)`, the
essential-OT red line, and (vendor NIS2 score present) the NIS2×bypass elevation.
A **brokered variant** inserts a DMZ historian (L3.5) on the path so the same
topology instead yields two `touches_dmz` edges and **zero** violations — the
pair that proves the split is real, not cosmetic.
