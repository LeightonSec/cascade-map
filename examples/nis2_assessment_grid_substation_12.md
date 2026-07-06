# NIS2 Third-Party Risk Assessment Report — Region X grid operator

> **Illustrative fixture.** The vendor, scores and findings below are invented
> to pair with the worked example in `region_x.yaml` (DESIGN.md §4). The report
> shape follows the `assessment-report-template.md` deliverable of
> [nis2-vendor-risk-framework](https://github.com/LeightonSec/nis2-vendor-risk-framework);
> `cascade-map import-nis2` parses exactly this artifact.

## 2. Vendor Details

| Field | Detail |
|---|---|
| **Legal entity name** | Region X Grid Operator AB |
| **Registered jurisdiction** | Sweden |
| **Services assessed** | HV substation operation (substation 12) |
| **Vendor tier (1/2/3)** | 1 |

## 5. Overall Risk Rating

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│   OVERALL SCORE:    15.0%                                   │
│                                                             │
│   RISK RATING:      CRITICAL                                │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Thresholds:** Low ≥85% | Medium 70–84% | High 50–69% | Critical <50%

---

Converted by `cascade-map import-nis2`: risk = (100 − 15.0) / 10 = **8.5** —
the same `nis2_vendor_score` hand-set on `grid_substation_12` in
`region_x.yaml`, so importing this report reproduces the placeholder exactly:

```sh
cascade-map import-nis2 examples/region_x.yaml \
  --report examples/nis2_assessment_grid_substation_12.md \
  --node grid_substation_12
```

The framework's action for a CRITICAL rating is "do not engage / suspend" —
which is exactly the tension cascade-map exists to surface: you cannot offboard
the grid operator, so the structural exposure has to be modelled instead.
