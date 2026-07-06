"""NIS2 import tests. The parser is checked against the shape of the
nis2-vendor-risk-framework's assessment-report-template.md (its real
deliverable): a filled report parses to (overall %, band-checked rating), an
unfilled or internally inconsistent one is rejected — the import fails closed.
The end-to-end merge is checked by re-loading the merged YAML through
``load_graph`` and confirming the analysis layer sees the imported score."""

from pathlib import Path

import pytest

from cascade_map import analysis as an
from cascade_map import engine as cm
from cascade_map import nis2_import as ni
from cascade_map.cli import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "region_x.yaml"
REPORT = ROOT / "examples" / "nis2_assessment_grid_substation_12.md"


def _report(tmp_path, score_line, rating_line, name="report.md"):
    p = tmp_path / name
    p.write_text(
        "# NIS2 Third-Party Risk Assessment Report\n\n"
        "| **Legal entity name** | Test Vendor AB |\n\n"
        f"   {score_line}\n\n"
        f"   {rating_line}\n",
        encoding="utf-8",
    )
    return str(p)


# --------------------------------------------------------------------------- #
# Parsing: filled reports
# --------------------------------------------------------------------------- #
def test_parse_example_fixture():
    a = ni.parse_report(str(REPORT))
    assert a.overall_pct == 15.0
    assert a.rating == "CRITICAL"
    assert a.vendor == "Region X Grid Operator AB"


def test_parse_worked_example_shape(tmp_path):
    # The framework's published worked example: 78.9% -> MEDIUM.
    p = _report(tmp_path, "OVERALL SCORE:    78.9%", "RISK RATING:      MEDIUM")
    a = ni.parse_report(p)
    assert a.overall_pct == 78.9
    assert a.rating == "MEDIUM"
    assert a.vendor == "Test Vendor AB"


def test_band_and_conversion():
    for pct, band, risk in [
        (100.0, "LOW", 0.0),
        (85.0, "LOW", 1.5),
        (84.9, "MEDIUM", 1.5),
        (70.0, "MEDIUM", 3.0),
        (50.0, "HIGH", 5.0),
        (49.9, "CRITICAL", 5.0),
        (15.0, "CRITICAL", 8.5),
        (0.0, "CRITICAL", 10.0),
    ]:
        assert ni.rating_band(pct) == band
        assert ni.risk_score(pct) == risk


# --------------------------------------------------------------------------- #
# Parsing: fail-closed rejections
# --------------------------------------------------------------------------- #
def test_unfilled_template_rejected(tmp_path):
    p = _report(tmp_path, "OVERALL SCORE:    ____%", "RISK RATING:      MEDIUM")
    with pytest.raises(ValueError, match="expected exactly one filled 'OVERALL SCORE"):
        ni.parse_report(p)


def test_ambiguous_rating_rejected(tmp_path):
    # An unfilled template still lists all four candidates on the rating line.
    p = _report(
        tmp_path,
        "OVERALL SCORE:    78.9%",
        "RISK RATING:      [ LOW ] [ MEDIUM ] [ HIGH ] [ CRITICAL ]",
    )
    with pytest.raises(ValueError, match="risk rating not filled in"):
        ni.parse_report(p)


def test_inconsistent_rating_rejected(tmp_path):
    p = _report(tmp_path, "OVERALL SCORE:    90.0%", "RISK RATING:      HIGH")
    with pytest.raises(ValueError, match="does not match the framework's band"):
        ni.parse_report(p)


def test_out_of_range_score_rejected(tmp_path):
    p = _report(tmp_path, "OVERALL SCORE:    250%", "RISK RATING:      LOW")
    with pytest.raises(ValueError, match="outside 0-100"):
        ni.parse_report(p)


def test_duplicate_score_lines_rejected(tmp_path):
    p = tmp_path / "dup.md"
    p.write_text(
        "OVERALL SCORE: 78.9%\nOVERALL SCORE: 40.0%\nRISK RATING: MEDIUM\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="found 2"):
        ni.parse_report(str(p))


def test_oversized_report_rejected(tmp_path):
    p = tmp_path / "huge.md"
    p.write_text("x" * (ni._MAX_REPORT_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        ni.parse_report(str(p))


# --------------------------------------------------------------------------- #
# Merge: end-to-end
# --------------------------------------------------------------------------- #
def test_merge_roundtrip(tmp_path):
    merged = ni.merge_nis2(str(EXAMPLE), [(str(REPORT), "grid_substation_12")])
    assert "conversion: risk = (100 - overall_compliance_pct) / 10" in merged
    assert 'vendor "Region X Grid Operator AB"' in merged
    assert "replaces 8.5" in merged
    out = tmp_path / "merged.yaml"
    out.write_text(merged, encoding="utf-8")
    g = cm.load_graph(str(out))  # merged output must still be a valid graph
    node = next(n for n in g.nodes if n.id == "grid_substation_12")
    assert node.nis2_vendor_score == 8.5
    # the analysis layer sees the imported score: still the red-line supplier
    top = an.nis2_exposure(g)[0]
    assert top["node"] == "grid_substation_12" and top["red_line"]


def test_merge_new_score(tmp_path):
    p = _report(tmp_path, "OVERALL SCORE: 60.0%", "RISK RATING: HIGH")
    merged = ni.merge_nis2(str(EXAMPLE), [(str(p), "water_pump_station")])
    assert "water_pump_station: 4.0" in merged and "new)" in merged


def test_merge_unknown_node_rejected():
    with pytest.raises(ValueError, match="unknown node"):
        ni.merge_nis2(str(EXAMPLE), [(str(REPORT), "no_such_node")])


def test_merge_duplicate_node_rejected():
    with pytest.raises(ValueError, match="more than one report"):
        ni.merge_nis2(
            str(EXAMPLE),
            [(str(REPORT), "grid_substation_12"), (str(REPORT), "grid_substation_12")],
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_import_writes_merged_graph(tmp_path):
    out = tmp_path / "merged.yaml"
    rc = main(
        [
            "import-nis2", str(EXAMPLE),
            "--report", str(REPORT), "--node", "grid_substation_12",
            "-o", str(out),
        ]
    )
    assert rc == 0
    g = cm.load_graph(str(out))
    node = next(n for n in g.nodes if n.id == "grid_substation_12")
    assert node.nis2_vendor_score == 8.5


def test_cli_unpaired_report_and_node_rejected(tmp_path):
    with pytest.raises(SystemExit):
        main(
            [
                "import-nis2", str(EXAMPLE),
                "--report", str(REPORT),
                "--node", "grid_substation_12", "--node", "water_pump_station",
            ]
        )
