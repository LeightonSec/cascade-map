"""Gate 6 — Purdue / IT–OT boundary overlay tests.

Three kinds of check:
  * the derivation truth table (locked in DESIGN.md §7.3), asserted zone-pair by
    zone-pair including every ``unknown`` row — a missing level must never
    masquerade as ``false``;
  * the golden pair — ``region_ot`` (raw IT→OT edge) vs ``region_ot_brokered``
    (same cascade, same timing, but routed via a Level-3.5 DMZ node), proving
    ``violates_purdue_direct`` and ``touches_dmz`` genuinely diverge;
  * backward-compat — a graph with no ``purdue_level`` classifies every edge as
    ``unknown`` and leaves the Gate-5 timeline byte-identical.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from cascade_map import analysis as an
from cascade_map import engine as cm

ROOT = Path(__file__).resolve().parents[1]
OT = ROOT / "examples" / "region_ot.yaml"
OT_BROKERED = ROOT / "examples" / "region_ot_brokered.yaml"
REGION_X = ROOT / "examples" / "region_x.yaml"


def _write(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


# --------------------------------------------------------------------------- #
# zone() + domain validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "level,expected",
    [
        (0, cm.ZONE_OT),
        (1, cm.ZONE_OT),
        (2, cm.ZONE_OT),
        (3, cm.ZONE_OT),
        (3.5, cm.ZONE_DMZ),  # the float-boundary trap: must be DMZ, not OT
        (4, cm.ZONE_IT),
        (5, cm.ZONE_IT),
        (None, cm.ZONE_UNKNOWN),
    ],
)
def test_zone_mapping(level, expected):
    assert cm.zone(level) == expected


@pytest.mark.parametrize("bad", [9, 2.7, -1, True, "3.5"])
def test_purdue_level_domain_rejected(bad):
    doc = f"nodes:\n  - {{id: a, purdue_level: {bad!r}}}\nedges: []\n"
    with pytest.raises(ValueError, match="purdue_level"):
        cm.load_graph(_write(doc))


@pytest.mark.parametrize("ok", [0, 2, 3.5, 5])
def test_purdue_level_domain_accepted(ok):
    doc = f"nodes:\n  - {{id: a, purdue_level: {ok}}}\nedges: []\n"
    g = cm.load_graph(_write(doc))
    assert g.nodes[0].purdue_level == ok


def test_purdue_level_optional():
    g = cm.load_graph(_write("nodes:\n  - {id: a}\nedges: []\n"))
    assert g.nodes[0].purdue_level is None


@pytest.mark.parametrize(
    "field", ["touches_dmz", "violates_purdue_direct", "crosses_sector"]
)
def test_derived_edge_fields_rejected(field):
    doc = (
        "nodes:\n  - {id: a}\n  - {id: b}\n"
        f"edges:\n  - {{from: a, to: b, {field}: true}}\n"
    )
    with pytest.raises(ValueError, match=field):
        cm.load_graph(_write(doc))


# --------------------------------------------------------------------------- #
# Derivation truth table (DESIGN.md §7.3).
# --------------------------------------------------------------------------- #
# (zone_a, zone_b): (touches_dmz, violates_purdue_direct)
_TRUTH = {
    (cm.ZONE_OT, cm.ZONE_OT): (cm.TRI_FALSE, cm.TRI_FALSE),
    (cm.ZONE_IT, cm.ZONE_IT): (cm.TRI_FALSE, cm.TRI_FALSE),
    (cm.ZONE_OT, cm.ZONE_IT): (cm.TRI_FALSE, cm.TRI_TRUE),
    (cm.ZONE_OT, cm.ZONE_DMZ): (cm.TRI_TRUE, cm.TRI_FALSE),
    (cm.ZONE_IT, cm.ZONE_DMZ): (cm.TRI_TRUE, cm.TRI_FALSE),
    (cm.ZONE_DMZ, cm.ZONE_DMZ): (cm.TRI_TRUE, cm.TRI_FALSE),
    (cm.ZONE_DMZ, cm.ZONE_UNKNOWN): (cm.TRI_TRUE, cm.TRI_FALSE),
    (cm.ZONE_OT, cm.ZONE_UNKNOWN): (cm.TRI_UNKNOWN, cm.TRI_UNKNOWN),
    (cm.ZONE_IT, cm.ZONE_UNKNOWN): (cm.TRI_UNKNOWN, cm.TRI_UNKNOWN),
    (cm.ZONE_UNKNOWN, cm.ZONE_UNKNOWN): (cm.TRI_UNKNOWN, cm.TRI_UNKNOWN),
}


@pytest.mark.parametrize("pair,expected", list(_TRUTH.items()))
def test_derivation_truth_table(pair, expected):
    za, zb = pair
    touches, violates = expected
    # assert against zones directly...
    assert cm.derive_touches_dmz(za, zb) == touches
    assert cm.derive_violates_purdue_direct(za, zb) == violates
    # ...and symmetrically (order of endpoints must not change classification)
    assert cm.derive_touches_dmz(zb, za) == touches
    assert cm.derive_violates_purdue_direct(zb, za) == violates


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("water", "digital", cm.TRI_TRUE),
        ("water", "water", cm.TRI_FALSE),
        ("water", "", cm.TRI_UNKNOWN),
        ("", "", cm.TRI_UNKNOWN),
    ],
)
def test_crosses_sector(a, b, expected):
    assert cm.derive_crosses_sector(a, b) == expected


# --------------------------------------------------------------------------- #
# The golden pair: bypass vs brokered
# --------------------------------------------------------------------------- #
def test_region_ot_bypass_red_line():
    g = cm.load_graph(str(OT))
    fs = cm.propagate(g, ["vendor_cloud_setpoints"])
    brl = an.bypass_red_lines(g, fs)
    assert [c["node"] for c in brl] == ["plant_scada"]
    (c,) = brl
    assert c["driver"] == "vendor_cloud_setpoints"
    assert c["flow"] == "IT->OT"
    assert c["violates_purdue_direct"] == cm.TRI_TRUE


def test_region_ot_brokered_has_no_violation():
    g = cm.load_graph(str(OT_BROKERED))
    fs = cm.propagate(g, ["vendor_cloud_setpoints"])
    assert an.bypass_red_lines(g, fs) == []
    bc = an.boundary_crossings(g, fs)
    assert all(c["violates_purdue_direct"] == cm.TRI_FALSE for c in bc)
    touches = [c for c in bc if c["touches_dmz"] == cm.TRI_TRUE]
    # both legs of the brokered path touch the DMZ
    assert {(c["driver"], c["node"]) for c in touches} == {
        ("vendor_cloud_setpoints", "dmz_historian"),
        ("dmz_historian", "plant_scada"),
    }


def test_golden_pair_timing_parity():
    """The pair is only a clean test if the cascade timing is identical — only
    the boundary classification may differ between them."""
    t_raw = {
        f.node: f.time
        for f in cm.propagate(cm.load_graph(str(OT)), ["vendor_cloud_setpoints"])
    }
    t_brk = {
        f.node: f.time
        for f in cm.propagate(
            cm.load_graph(str(OT_BROKERED)), ["vendor_cloud_setpoints"]
        )
    }
    assert t_raw["plant_scada"] == t_brk["plant_scada"] == 30
    assert t_raw["plc_dosing"] == t_brk["plc_dosing"] == 40


def test_nis2_bypass_composition():
    ex_raw = {r["node"]: r for r in an.nis2_exposure(cm.load_graph(str(OT)))}
    ex_brk = {r["node"]: r for r in an.nis2_exposure(cm.load_graph(str(OT_BROKERED)))}
    assert ex_raw["vendor_cloud_setpoints"]["bypass_to_essential_ot"] is True
    assert ex_brk["vendor_cloud_setpoints"]["bypass_to_essential_ot"] is False


@pytest.mark.parametrize(
    "path,golden",
    [
        (OT, "golden_region_ot.txt"),
        (OT_BROKERED, "golden_region_ot_brokered.txt"),
    ],
)
def test_golden_timeline_bytes(path, golden):
    g = cm.load_graph(str(path))
    out = cm.render_timeline(cm.propagate(g, ["vendor_cloud_setpoints"]), len(g.nodes))
    assert out == (Path(__file__).parent / golden).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Report wiring + injected-root safety + backward-compat
# --------------------------------------------------------------------------- #
def test_report_shows_bypass_and_nis2_elevation():
    out = an.render_report(cm.load_graph(str(OT)), ["vendor_cloud_setpoints"], monte=False)
    lines = out.splitlines()
    # Structural (per-line substring) checks, not byte-exact — column padding is
    # a formatting choice that must be free to change without breaking the test.
    assert any("vendor_cloud_setpoints" in ln and "IT→OT BYPASS" in ln for ln in lines)
    assert any(
        "plant_scada" in ln
        and "IT→OT BYPASS red line" in ln
        and "vendor_cloud_setpoints" in ln
        for ln in lines
    )
    assert any(
        "vendor_cloud_setpoints -> plant_scada" in ln and "IT->OT" in ln
        for ln in lines
    )


def test_report_brokered_shows_designed_dmz_not_bypass():
    out = an.render_report(
        cm.load_graph(str(OT_BROKERED)), ["vendor_cloud_setpoints"], monte=False
    )
    assert "no IT→OT bypass reaches an essential OT node" in out
    assert "IDMZ-routed dependencies on failure path (designed):" in out
    assert "IT→OT BYPASS" not in out


def test_injected_root_has_no_driver_and_does_not_crash():
    g = cm.load_graph(str(OT))
    fs = cm.propagate(g, ["vendor_cloud_setpoints"])
    root = next(f for f in fs if f.node == "vendor_cloud_setpoints")
    assert root.driver is None
    # boundary_crossings must not choke on the None-driver root, and must not
    # emit a row for it (a root cause is not a crossing).
    bc = an.boundary_crossings(g, fs)
    assert all(c["node"] != "vendor_cloud_setpoints" for c in bc)


def test_backward_compat_region_x_boundary_all_unknown():
    """region_x carries no purdue_level: every driving edge is unclassified, and
    nothing is silently reported as a safe 'false'."""
    g = cm.load_graph(str(REGION_X))
    fs = cm.propagate(g, ["grid_substation_12"])
    bc = an.boundary_crossings(g, fs)
    assert bc  # there are driven failures
    assert all(c["touches_dmz"] == cm.TRI_UNKNOWN for c in bc)
    assert all(c["violates_purdue_direct"] == cm.TRI_UNKNOWN for c in bc)
    assert an.bypass_red_lines(g, fs) == []


def test_backward_compat_region_x_timeline_unchanged():
    """The Gate-5 golden timeline must be byte-identical after Gate 6 lands."""
    g = cm.load_graph(str(REGION_X))
    out = cm.render_timeline(cm.propagate(g, ["grid_substation_12"]), len(g.nodes))
    golden = (Path(__file__).parent / "golden_region_x.txt").read_text(encoding="utf-8")
    assert out == golden


def test_cli_analyze_region_ot_reports_bypass():
    import os

    r = subprocess.run(
        [sys.executable, "-m", "cascade_map.cli", "analyze", str(OT),
         "--inject", "vendor_cloud_setpoints", "--runs", "100"],
        capture_output=True, text=True, cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ["PATH"]},
    )
    assert r.returncode == 0, r.stderr
    assert "IT→OT BYPASS" in r.stdout
