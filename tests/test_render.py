"""DOT render tests. Structural (not golden) so colour/format tweaks don't churn
the suite, but the load-bearing annotations are asserted exactly."""

import subprocess
import sys
from pathlib import Path

from cascade_map import engine as cm
from cascade_map.render import render_dot

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "region_x.yaml"
NODE_IDS = [
    "grid_substation_12",
    "mobile_core_region_x",
    "water_pump_station",
    "bank_datacentre",
    "card_payment_switch",
]


def _graph():
    return cm.load_graph(str(EXAMPLE))


def test_dot_is_wellformed_and_lists_nodes():
    dot = render_dot(_graph())
    assert dot.startswith("digraph cascade_map {")
    assert dot.rstrip().endswith("}")
    for nid in NODE_IDS:
        assert f'"{nid}"' in dot


def test_dot_without_injection_has_no_failure_annotations():
    dot = render_dot(_graph())
    assert "failed t=" not in dot
    assert "injected" not in dot


def test_dot_with_injection_annotates_failures_and_redline():
    dot = render_dot(_graph(), ["grid_substation_12"])
    assert "injected t=0" in dot                 # the injected substation
    assert "failed t=60min" in dot               # water fails at 60
    assert "SPOF → essential" in dot             # NIS2 red-line marker
    assert "doubleoctagon" in dot                # essential nodes shaped distinctly


def test_dot_edges_use_supplies_direction():
    dot = render_dot(_graph())
    # dst -> src, so failure flows along the arrows
    assert '"grid_substation_12" -> "mobile_core_region_x"' in dot
    assert '"mobile_core_region_x" -> "card_payment_switch"' in dot


def test_dot_cli_writes_file(tmp_path):
    import os

    out = tmp_path / "g.dot"
    r = subprocess.run(
        [sys.executable, "-m", "cascade_map.cli", "dot", str(EXAMPLE),
         "--inject", "grid_substation_12", "-o", str(out)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ["PATH"]},
    )
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8").startswith("digraph cascade_map {")
