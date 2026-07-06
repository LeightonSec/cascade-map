"""cascade-map NIS2 import — the vendor-risk composition as a real pipeline.

The nis2-vendor-risk-framework's deliverable is a filled markdown assessment
report (one per vendor) whose headline is an overall compliance percentage and
a risk rating. This module parses that artifact, converts the compliance
percentage onto the 0-10 ``nis2_vendor_score`` risk scale the graph uses, and
merges it onto a named node — so "composes with the NIS2 framework" is a
demonstrable import path, not a manual convention.

Fail-closed rules (the report is a compliance artifact; parse cleanly or reject):
  * exactly one filled "OVERALL SCORE: <n>%" line, value within 0-100
    (an unfilled template has none);
  * exactly one "RISK RATING:" line carrying exactly one rating token
    (an unfilled template still lists all four candidates);
  * the stated rating must match the band the framework itself defines
    (Low >= 85 | Medium 70-84 | High 50-69 | Critical < 50);
  * the target node must exist in an already-valid graph.

Conversion (recorded in the emitted provenance header):
    risk = (100 - overall_compliance_pct) / 10      # 0.0 best .. 10.0 worst
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import yaml

from cascade_map.engine import _validate_schema, load_graph

# A vendor assessment report is a short document; anything huge is not one.
_MAX_REPORT_BYTES = 1_000_000

_SCORE_RE = re.compile(r"OVERALL SCORE:\s*(\d+(?:\.\d+)?)\s*%")
_RATING_LINE_RE = re.compile(r"RISK RATING:(.*)")
_RATING_TOKEN_RE = re.compile(r"\b(LOW|MEDIUM|HIGH|CRITICAL)\b")
_VENDOR_RE = re.compile(r"\*\*Legal entity name\*\*\s*\|\s*([^|\n]+)")


@dataclass
class Assessment:
    """The parsed headline of one filled NIS2 assessment report."""

    overall_pct: float  # compliance percentage, 0-100, higher = better
    rating: str  # LOW | MEDIUM | HIGH | CRITICAL, as stated and band-checked
    vendor: str | None  # legal entity name, if the report carries one
    source: str  # path the report was read from


def rating_band(pct: float) -> str:
    """The framework's own thresholds: Low >=85, Medium 70-84, High 50-69,
    Critical <50."""
    if pct >= 85:
        return "LOW"
    if pct >= 70:
        return "MEDIUM"
    if pct >= 50:
        return "HIGH"
    return "CRITICAL"


def risk_score(pct: float) -> float:
    """Compliance % (higher = better) -> 0-10 risk score (higher = worse)."""
    return round((100.0 - pct) / 10.0, 1)


def parse_report(path: str) -> Assessment:
    if os.path.getsize(path) > _MAX_REPORT_BYTES:
        raise ValueError(f"report {path}: exceeds {_MAX_REPORT_BYTES} bytes; not an assessment report")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    scores = _SCORE_RE.findall(text)
    if len(scores) != 1:
        raise ValueError(
            f"report {path}: expected exactly one filled 'OVERALL SCORE: <n>%' line, "
            f"found {len(scores)} (an unfilled template has none)"
        )
    pct = float(scores[0])
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"report {path}: overall score {pct}% is outside 0-100")

    rating_lines = _RATING_LINE_RE.findall(text)
    if len(rating_lines) != 1:
        raise ValueError(
            f"report {path}: expected exactly one 'RISK RATING:' line, found {len(rating_lines)}"
        )
    tokens = _RATING_TOKEN_RE.findall(rating_lines[0])
    if len(tokens) != 1:
        raise ValueError(
            f"report {path}: risk rating not filled in — expected exactly one of "
            f"LOW/MEDIUM/HIGH/CRITICAL on the 'RISK RATING:' line, found {len(tokens)}"
        )
    stated = tokens[0]
    expected = rating_band(pct)
    if stated != expected:
        raise ValueError(
            f"report {path}: stated rating {stated} does not match the framework's band "
            f"for {pct}% (expected {expected}); refusing an internally inconsistent report"
        )

    m = _VENDOR_RE.search(text)
    vendor = m.group(1).strip() if m else None
    return Assessment(overall_pct=pct, rating=stated, vendor=vendor or None, source=path)


def merge_nis2(graph_path: str, pairs: list[tuple[str, str]]) -> str:
    """Merge (report, node_id) pairs into the graph at ``graph_path`` and return
    the merged YAML document with a provenance header. The input file is not
    modified; comments in the original are not preserved (the output is a
    normalized dump)."""
    load_graph(graph_path)  # full graph validation (duplicate ids, edge refs)
    with open(graph_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    _validate_schema(raw)  # validate untrusted input shape before consuming it
    nodes = {n["id"]: n for n in raw.get("nodes", [])}

    header = [
        "# nis2_vendor_score values imported by `cascade-map import-nis2` from",
        "# nis2-vendor-risk-framework assessment report(s):",
        "# conversion: risk = (100 - overall_compliance_pct) / 10   (0.0 best .. 10.0 worst)",
    ]
    seen: set[str] = set()
    for report_path, node_id in pairs:
        if node_id not in nodes:
            raise ValueError(f"cannot import onto unknown node: {node_id}")
        if node_id in seen:
            raise ValueError(f"node {node_id} targeted by more than one report")
        seen.add(node_id)
        a = parse_report(report_path)
        score = risk_score(a.overall_pct)
        old = nodes[node_id].get("nis2_vendor_score")
        provenance = "new" if old is None else f"replaces {old}"
        vendor = f'vendor "{a.vendor}", ' if a.vendor else ""
        header.append(
            f"#   {node_id}: {score} <- {report_path} "
            f"({vendor}overall {a.overall_pct}%, rating {a.rating}, {provenance})"
        )
        nodes[node_id]["nis2_vendor_score"] = score

    body = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    return "\n".join(header) + "\n" + body
