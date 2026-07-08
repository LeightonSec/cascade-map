# cascade-map

A **dependency-reachability model** for critical infrastructure. Describe a
sector as a graph of "function A needs function B"; inject an initial failure;
see what fails, in what order, and how fast, as buffers drain — then overlay
NIS2 vendor risk to find the suppliers that are also single points of failure.

> It traces how one failure *reaches* many services over time. It is **not** a
> load-redistribution cascade simulator and **not** a recovery model — both are
> out of scope by design. See `DESIGN.md` for the full scope declaration; the
> honesty about that boundary is deliberate.

## Why

Grid, telecoms, water and finance sit on top of each other, and the
interdependencies are largely unmanaged. cascade-map turns "a small failure
reaches essential services faster than they can be restored" from a rhetorical
worry into something you can model, inject, and measure — the OT/ICS + GRC lane.

## Install

```sh
pip install -e .
```

Runtime dependency: `pyyaml` only (a deliberately small audit surface).

## Usage

**Timeline** — the deterministic failure front:

```sh
cascade-map timeline examples/region_x.yaml --inject grid_substation_12
```

**Analyze** — the full report (time-to-critical, ride-through breaches, single
points of failure, NIS2 exposure, Monte-Carlo sensitivity):

```sh
cascade-map analyze examples/region_x.yaml --inject grid_substation_12 --runs 5000
```

```
## Time to critical
  first essential service lost: water_pump_station at t=60min

## Ride-through breaches
  water_pump_station     failed t=60min  (required 75min; 15min early)

## Single points of failure (solo blast radius)
  grid_substation_12     downstream failures: 4   <-- SPOF
  ...

## NIS2 exposure
  grid_substation_12     score 8.5  downstream 4   <== RED LINE (SPOF hitting an essential entity)  [worst impact: physical]
  ...

## Monte-Carlo sensitivity (5000 runs over [low,high] buffers)
  time-to-critical: p10 35.8min  p50 60.1min  p90 83.9min  (range 30.0min–90.0min)
  P(breach water_pump_station) = 75%
```

## Visualize

Emit a Graphviz diagram — nodes coloured by failure status, the injected node
and any NIS2 red-line SPOF highlighted, essential entities shaped distinctly.
The tool has **no Graphviz dependency**; it emits DOT, and the system `dot`
binary (`brew install graphviz`) rasterizes it:

```sh
cascade-map dot examples/region_x.yaml --inject grid_substation_12 | dot -Tpng -o cascade.png
```

Without `--inject`, nodes are coloured by sector (the topology view).

## Graph format

Edge direction is **`from` depends on `to`**. Time buffers accept a point value
or a `[low, high]` range (midpoint drives the deterministic run; the range feeds
the Monte-Carlo sampler).

```yaml
nodes:
  - id: mobile_core
    sector: telecom
    criticality: essential          # essential | important | supporting
    autonomy_minutes: [120, 360]    # buffer once dependencies are lost
    nis2_vendor_score: 7.5          # optional; imported from the NIS2 framework
    required_ride_through_min: 60   # optional; min minutes it must survive (breach if earlier)
edges:
  - from: mobile_core
    to: grid_substation
    type: power                     # power | data | fuel | water | staff | ...
    redundancy: 0                   # independent backups; 0 = single source
    failure_offset: none            # none | partial | full — per-edge backup category
```

## Model, in one paragraph

A dependency *type* of a node is **unmet** when `redundancy + 1` of its targets
have failed; the node then fails after its own `autonomy_minutes` buffer.
Different types are independently required (AND); redundant targets within a
type are the backup (OR). Times are computed by monotone relaxation, so cycles
converge and anything with an intact dependency simply never fails.

Every failure carries a **kind**, derived from the edge that caused it: a
`data` dependency unmet means `control_loss` (the node runs blind, it does not
physically stop); everything else means `physical`; unmapped types default to
`physical` (conservative worst-case) pending explicit classification. An edge
with `failure_offset: full` is fully backed up and never propagates;
`partial` downgrades the hit to `capacity_degraded`.

## Composes with the NIS2 vendor-risk framework

[nis2-vendor-risk-framework](https://github.com/LeightonSec/nis2-vendor-risk-framework)
scores a single supplier; cascade-map answers which of those suppliers is *also*
a structural single point of failure that drags an essential entity below its
required ride-through — the board-level question.

The composition is a real import path, not a naming convention. `import-nis2`
parses the framework's filled assessment report (its actual deliverable),
converts the overall compliance percentage onto the graph's 0–10 risk scale
(`risk = (100 − pct) / 10`, recorded in a provenance header along with the
source report, vendor, rating, and any value it replaced), and merges it onto
the named node:

```sh
cascade-map import-nis2 examples/region_x.yaml \
  --report examples/nis2_assessment_grid_substation_12.md \
  --node grid_substation_12 -o merged.yaml
cascade-map analyze merged.yaml --inject grid_substation_12
```

The import **fails closed**: an unfilled template, a missing or ambiguous risk
rating, a rating inconsistent with the framework's own score bands, an
out-of-range score, or an unknown node id are all hard errors. A compliance
artifact either parses cleanly or is rejected — never silently guessed at.

## Development

```sh
pip install -r requirements-dev.txt && pip install -e .
pytest -q
make pin && make pin-dev     # regenerate hashed lockfiles (needs uv)
```

## License

MIT — see `LICENSE`.
