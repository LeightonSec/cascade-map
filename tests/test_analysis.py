"""Analysis tests. Deterministic analyses are checked against values derived BY
HAND from DESIGN.md §4; the Monte-Carlo pass is checked against its closed-form
expectation with a tolerance band (uniform buffer over [30, 90], required 75:
P(breach) = (75-30)/60 = 0.75; time-to-critical median = 60)."""

import subprocess
import sys
from pathlib import Path

from cascade_map import analysis as an
from cascade_map import engine as cm

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "region_x.yaml"
INJECT = ["grid_substation_12"]


def _graph():
    return cm.load_graph(str(EXAMPLE))


def test_time_to_critical():
    g = _graph()
    crit = an.time_to_critical(g, cm.propagate(g, INJECT), INJECT)
    assert crit.node == "water_pump_station"
    assert crit.time == 60


def test_breaches():
    g = _graph()
    br = an.breaches(g, cm.propagate(g, INJECT), INJECT)
    assert br == [("water_pump_station", 75.0, 60.0, 15.0)]


def test_spof_scan():
    g = _graph()
    assert an.spof_scan(g) == [
        ("grid_substation_12", 4),
        ("mobile_core_region_x", 1),
        ("bank_datacentre", 0),
        ("card_payment_switch", 0),
        ("water_pump_station", 0),
    ]


def test_nis2_exposure_red_line():
    g = _graph()
    ex = an.nis2_exposure(g)
    assert [r["node"] for r in ex] == [
        "grid_substation_12",
        "mobile_core_region_x",
        "bank_datacentre",
    ]
    red = {r["node"]: r["red_line"] for r in ex}
    assert red["grid_substation_12"] is True       # SPOF hitting essential water
    assert red["mobile_core_region_x"] is False     # SPOF but no essential downstream
    assert red["bank_datacentre"] is False          # not a SPOF


def test_monte_carlo_sensitivity():
    g = _graph()
    mc = an.monte_carlo(g, INJECT, runs=5000, seed=0)
    assert 55 <= mc["ttc"]["p50"] <= 65
    assert mc["ttc"]["min"] >= 30 and mc["ttc"]["max"] <= 90
    assert 0.72 <= mc["breach_prob"]["water_pump_station"] <= 0.78
    assert mc["breach_prob"].get("mobile_core_region_x", 0.0) == 0.0


def test_monte_carlo_is_seed_deterministic():
    g = _graph()
    assert an.monte_carlo(g, INJECT, runs=1000, seed=7) == an.monte_carlo(
        g, INJECT, runs=1000, seed=7
    )


def test_render_report_lines():
    g = _graph()
    out = an.render_report(g, INJECT, runs=200, seed=0)
    assert "first essential service lost: water_pump_station at t=60min" in out
    assert (
        "water_pump_station     failed t=60min  (required 75min; 15min early)" in out
    )
    assert "grid_substation_12     downstream failures: 4   <-- SPOF" in out
    assert (
        "grid_substation_12     score 8.5  downstream 4"
        "   <== RED LINE (SPOF hitting an essential entity)" in out
    )
    assert "Monte-Carlo sensitivity" in out


def test_render_report_without_monte():
    g = _graph()
    out = an.render_report(g, INJECT, monte=False)
    assert "Monte-Carlo" not in out
    assert "NIS2 exposure" in out


def test_analysis_cli_runs():
    import os

    r = subprocess.run(
        [sys.executable, "-m", "cascade_map.cli", "analyze", str(EXAMPLE),
         "--inject", "grid_substation_12", "--runs", "500"],
        capture_output=True, text=True, cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ["PATH"]},
    )
    assert r.returncode == 0, r.stderr
    assert "RED LINE" in r.stdout
    assert "Monte-Carlo sensitivity" in r.stdout
