"""Engine acceptance tests.

Two kinds of check:
  * correctness — failure times derived BY HAND from DESIGN.md §4, so the test
    validates the algorithm independently of how output is formatted;
  * golden — the exact rendered bytes, locked against regression.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from cascade_map import engine as cm

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "region_x.yaml"
GOLDEN = Path(__file__).parent / "golden_region_x.txt"


def _times(failures):
    return {f.node: f.time for f in failures}


def test_engine_matches_design_numbers():
    g = cm.load_graph(str(EXAMPLE))
    t = _times(cm.propagate(g, ["grid_substation_12"]))
    assert t["grid_substation_12"] == 0       # injected
    assert t["water_pump_station"] == 60      # buffer 60 after power lost at t=0
    assert t["mobile_core_region_x"] == 240   # midpoint([120,360]) after t=0
    assert t["bank_datacentre"] == 240        # midpoint([180,300]) after t=0
    assert t["card_payment_switch"] == 240    # buffer 0 after mobile core @240
    assert len(t) == 5


def test_ordering_is_deterministic():
    g = cm.load_graph(str(EXAMPLE))
    order = [f.node for f in cm.propagate(g, ["grid_substation_12"])]
    assert order == [
        "grid_substation_12",
        "water_pump_station",
        "bank_datacentre",
        "card_payment_switch",
        "mobile_core_region_x",
    ]


def test_render_matches_golden_bytes():
    g = cm.load_graph(str(EXAMPLE))
    out = cm.render_timeline(cm.propagate(g, ["grid_substation_12"]), len(g.nodes))
    assert out == GOLDEN.read_text(encoding="utf-8")


def test_range_midpoint():
    assert cm._midpoint([120, 360]) == 240
    assert cm._midpoint(60) == 60
    with pytest.raises(ValueError):
        cm._midpoint([1, 2, 3])


def test_redundancy_holds_until_all_sources_fail(tmp_path):
    graph = tmp_path / "g.yaml"
    graph.write_text(
        "nodes:\n"
        "  - {id: a, autonomy_minutes: 0}\n"
        "  - {id: b, autonomy_minutes: 0}\n"
        "  - {id: load, autonomy_minutes: 10}\n"
        "edges:\n"
        "  - {from: load, to: a, type: power, redundancy: 1}\n"
        "  - {from: load, to: b, type: power, redundancy: 1}\n",
        encoding="utf-8",
    )
    g = cm.load_graph(str(graph))
    # One of two redundant sources down -> load survives.
    assert "load" not in {f.node for f in cm.propagate(g, ["a"])}
    # Both sources down -> load fails 10 min later.
    assert _times(cm.propagate(g, ["a", "b"]))["load"] == 10


def test_survivor_is_reported():
    g = cm.load_graph(str(EXAMPLE))
    fs = cm.propagate(g, ["card_payment_switch"])  # leaf; nothing depends on it
    assert {f.node for f in fs} == {"card_payment_switch"}


def test_unknown_injection_rejected():
    g = cm.load_graph(str(EXAMPLE))
    with pytest.raises(ValueError):
        cm.propagate(g, ["no_such_node"])


def test_validate_rejects_unknown_edge_target(tmp_path):
    graph = tmp_path / "g.yaml"
    graph.write_text(
        "nodes:\n  - {id: a}\nedges:\n  - {from: a, to: ghost, type: power}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        cm.load_graph(str(graph))


def test_validate_rejects_duplicate_ids(tmp_path):
    graph = tmp_path / "g.yaml"
    graph.write_text("nodes:\n  - {id: a}\n  - {id: a}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cm.load_graph(str(graph))


@pytest.mark.parametrize(
    "doc",
    [
        "- just\n- a\n- list\n",                                  # top-level not a mapping
        "nodes:\n  - {id: a, criticality: vital}\n",              # bad criticality
        "nodes:\n  - {id: a, autonomy_minutes: [90, 30]}\n",      # range low > high
        "nodes:\n  - {id: a, autonomy_minutes: soon}\n",          # non-numeric buffer
        "nodes:\n  - {id: a}\nedges:\n  - {from: a}\n",            # edge missing 'to'
        "nodes:\n  - {id: a, nis2_vendor_score: high}\n",         # non-numeric score
    ],
)
def test_schema_rejects_malformed(tmp_path, doc):
    graph = tmp_path / "g.yaml"
    graph.write_text(doc, encoding="utf-8")
    with pytest.raises(ValueError):
        cm.load_graph(str(graph))


def test_cli_runs():
    r = subprocess.run(
        [sys.executable, "-m", "cascade_map.cli", "timeline", str(EXAMPLE),
         "--inject", "grid_substation_12"],
        capture_output=True, text=True, cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT), "PATH": __import__("os").environ["PATH"]},
    )
    assert r.returncode == 0, r.stderr
    assert "5 of 5 nodes failed" in r.stdout
