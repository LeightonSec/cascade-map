"""Gate 5 acceptance tests — failure severity & redundancy typing.

Covers all three failure kinds (physical / control_loss / capacity_degraded)
and all three failure_offset values (none / partial / full), the locked
derivation mapping (data -> control_loss, everything else -> physical,
partial overrides to capacity_degraded, unmapped types conservatively
physical), the fail-closed loader rules, and the region_y answer key:
water_treatment_plant's data dependency on fibre_backhaul_y resolves to
control_loss at the same t=60 the pre-Gate-5 engine produced.
"""

from pathlib import Path

import pytest

from cascade_map import analysis as an
from cascade_map import engine as cm
from cascade_map.engine import Edge, Graph, Node

ROOT = Path(__file__).resolve().parents[1]
REGION_X = ROOT / "examples" / "region_x.yaml"
REGION_Y = ROOT / "examples" / "region_y.yaml"


def _pair(edge_type: str, offset: str = "none") -> Graph:
    """upstream <- plant graph: plant depends on upstream via one typed edge."""
    return Graph(
        nodes=[Node(id="upstream"), Node(id="plant", autonomy_minutes=30)],
        edges=[
            Edge(src="plant", dst="upstream", type=edge_type, failure_offset=offset)
        ],
    )


def _kinds(failures):
    return {f.node: f.kind for f in failures}


# --------------------------------------------------------------------------- #
# The locked derivation mapping
# --------------------------------------------------------------------------- #
def test_data_edge_maps_to_control_loss():
    fs = cm.propagate(_pair("data"), ["upstream"])
    assert _kinds(fs)["plant"] == "control_loss"


@pytest.mark.parametrize("edge_type", ["power", "physical_route", "supply"])
def test_canonical_hard_types_map_to_physical(edge_type):
    fs = cm.propagate(_pair(edge_type), ["upstream"])
    assert _kinds(fs)["plant"] == "physical"


@pytest.mark.parametrize("edge_type", ["fuel", "staff", "generic"])
def test_unmapped_types_default_to_physical_conservative(edge_type):
    # DESIGN.md: unmapped dependency types default to physical (worst case)
    # pending explicit classification — never quietly downgrade an impact.
    fs = cm.propagate(_pair(edge_type), ["upstream"])
    assert _kinds(fs)["plant"] == "physical"


def test_injected_node_kind_is_physical():
    fs = cm.propagate(_pair("data"), ["upstream"])
    assert _kinds(fs)["upstream"] == "physical"


# --------------------------------------------------------------------------- #
# failure_offset semantics (none / partial / full)
# --------------------------------------------------------------------------- #
def test_none_offset_keeps_base_mapping():
    fs = cm.propagate(_pair("data", offset="none"), ["upstream"])
    assert _kinds(fs)["plant"] == "control_loss"


def test_partial_offset_overrides_base_mapping_to_capacity_degraded():
    # Gate 5 criterion 3: partial reports capacity_degraded, NOT the
    # base-mapped kind — proven on a power edge, whose base kind is physical.
    fs = cm.propagate(_pair("power", offset="partial"), ["upstream"])
    assert _kinds(fs)["plant"] == "capacity_degraded"


def test_partial_offset_does_not_change_timing():
    fs = cm.propagate(_pair("power", offset="partial"), ["upstream"])
    assert {f.node: f.time for f in fs}["plant"] == 30


def test_full_offset_on_only_failed_inbound_edge_survives():
    # Gate 5 criterion 2: a fully backed-up dependency never propagates.
    fs = cm.propagate(_pair("power", offset="full"), ["upstream"])
    assert {f.node for f in fs} == {"upstream"}


def test_full_offset_does_not_shield_other_edges():
    g = Graph(
        nodes=[Node(id="a"), Node(id="b"), Node(id="plant", autonomy_minutes=10)],
        edges=[
            Edge(src="plant", dst="a", type="power", failure_offset="full"),
            Edge(src="plant", dst="b", type="data"),
        ],
    )
    # The fully backed-up power edge alone cannot fail the plant...
    assert {f.node for f in cm.propagate(g, ["a"])} == {"a"}
    # ...but the unprotected data edge still can, with its own kind.
    fs = cm.propagate(g, ["b"])
    assert _kinds(fs)["plant"] == "control_loss"


def test_int_redundancy_semantics_unchanged_alongside_offsets():
    # The pre-Gate-5 integer redundancy field keeps its group-count
    # semantics: with redundancy 1, one of two sources down is survivable.
    g = Graph(
        nodes=[Node(id="a"), Node(id="b"), Node(id="load", autonomy_minutes=5)],
        edges=[
            Edge(src="load", dst="a", type="power", redundancy=1),
            Edge(src="load", dst="b", type="power", redundancy=1),
        ],
    )
    assert "load" not in {f.node for f in cm.propagate(g, ["a"])}
    fs = cm.propagate(g, ["a", "b"])
    assert {f.node: f.time for f in fs}["load"] == 5
    assert _kinds(fs)["load"] == "physical"


def test_derive_failure_kind_rejects_full():
    # A fully backed-up edge can never drive a failure; asking for its kind
    # is a caller bug and must fail loudly.
    with pytest.raises(ValueError):
        cm.derive_failure_kind("power", "full")


# --------------------------------------------------------------------------- #
# Fail-closed loader rules
# --------------------------------------------------------------------------- #
def test_loader_rejects_failure_kind_in_yaml(tmp_path):
    graph = tmp_path / "g.yaml"
    graph.write_text(
        "nodes:\n"
        "  - {id: a}\n"
        "  - {id: b}\n"
        "edges:\n"
        "  - {from: b, to: a, type: data, failure_kind: control_loss}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="failure_kind is derived"):
        cm.load_graph(str(graph))


def test_loader_rejects_invalid_failure_offset(tmp_path):
    graph = tmp_path / "g.yaml"
    graph.write_text(
        "nodes:\n"
        "  - {id: a}\n"
        "  - {id: b}\n"
        "edges:\n"
        "  - {from: b, to: a, type: data, failure_offset: total}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="failure_offset"):
        cm.load_graph(str(graph))


def test_loader_accepts_valid_failure_offset(tmp_path):
    graph = tmp_path / "g.yaml"
    graph.write_text(
        "nodes:\n"
        "  - {id: a}\n"
        "  - {id: b}\n"
        "edges:\n"
        "  - {from: b, to: a, type: data, failure_offset: partial}\n",
        encoding="utf-8",
    )
    g = cm.load_graph(str(graph))
    assert g.edges[0].failure_offset == "partial"


# --------------------------------------------------------------------------- #
# The region_y answer key (Gate 5 criterion 4) and rendering (criterion 5)
# --------------------------------------------------------------------------- #
def test_region_y_timing_unchanged_and_kinds_attached():
    g = cm.load_graph(str(REGION_Y))
    fs = cm.propagate(g, ["fibre_backhaul_y"])
    times = {f.node: f.time for f in fs}
    kinds = _kinds(fs)
    # Timing identical to the pre-Gate-5 verified run.
    assert times == {
        "fibre_backhaul_y": 0,
        "water_treatment_plant": 60,
        "mobile_core_region_y": 240,
        "card_payment_switch": 240,
    }
    # The answer key: water's data dependency resolves to control_loss.
    assert kinds["water_treatment_plant"] == "control_loss"
    assert kinds["mobile_core_region_y"] == "control_loss"
    assert kinds["card_payment_switch"] == "control_loss"
    assert kinds["fibre_backhaul_y"] == "physical"  # injected, down by fiat


def test_timeline_renders_kind_for_every_failed_node():
    g = cm.load_graph(str(REGION_Y))
    fs = cm.propagate(g, ["fibre_backhaul_y"])
    out = cm.render_timeline(fs, len(g.nodes))
    for f in fs:
        line = next(ln for ln in out.splitlines() if f.node in ln)
        assert f.kind in line
    assert "water_treatment_plant  control_loss" in out


def test_nis2_exposure_impact_annotation_region_y():
    # The thesis of the demo case: fibre_backhaul_y is a red-line SPOF whose
    # downstream impact is control_loss — serious, but NOT a physical stop.
    g = cm.load_graph(str(REGION_Y))
    rows = {r["node"]: r for r in an.nis2_exposure(g)}
    fibre = rows["fibre_backhaul_y"]
    assert fibre["red_line"] is True
    assert fibre["impact"] == "control_loss"
    # Zero-downstream vendor gets no impact annotation.
    assert rows["erp_billing_saas"]["impact"] is None


def test_nis2_exposure_impact_annotation_region_x():
    # Contrast case: the substation's blast is dominated by physical failures.
    g = cm.load_graph(str(REGION_X))
    rows = {r["node"]: r for r in an.nis2_exposure(g)}
    assert rows["grid_substation_12"]["impact"] == "physical"


def test_report_renders_impact_annotation():
    g = cm.load_graph(str(REGION_Y))
    out = an.render_report(g, ["fibre_backhaul_y"], monte=False)
    assert (
        "<== RED LINE (SPOF hitting an essential entity)"
        "  [worst impact: control_loss]" in out
    )
