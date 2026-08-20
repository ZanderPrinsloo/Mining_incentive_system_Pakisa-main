"""
Regression tests asserting that any two endpoints reporting the same metric
for the same period/section actually agree with each other.

Run:  pytest tests/test_metric_consistency.py -v
Skip: all tests are skipped cleanly when STPTM2000 is not reachable.

Design notes
------------
* Bonus totals are canonical from PARTICIPANTSDETAIL (see queries.py header).
  get_gang_detail() uses a different source (GANGPRODUCTIONDETAIL per-person × labour)
  to provide the 4-component breakdown — the discrepancy test below documents
  the expected divergence without failing the suite.
* The CREWNO>=8 filter is now applied consistently in kpi_summary, production_trend,
  and sqm_range_analysis so the KPI total_sqm equals what appears in every breakdown tab.
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_DATABASE", "STPTM2000")


# ── DB availability probe ─────────────────────────────────────────────────────

def _probe_db() -> tuple[bool, tuple[int, int] | None]:
    """Try to connect and discover available periods. Returns (ok, (from, to))."""
    try:
        from web import queries as q
        periods = q.get_available_periods()
        if not periods:
            return False, None
        ordered = sorted(p["value"] for p in periods)
        p_from  = ordered[0]
        p_to    = ordered[min(5, len(ordered) - 1)]
        return True, (p_from, p_to)
    except Exception:
        return False, None


_DB_OK, _RANGE = _probe_db()
_skip = pytest.mark.skipif(not _DB_OK, reason="STPTM2000 not reachable — skipping consistency tests")

PERIOD_FROM: int = _RANGE[0] if _RANGE else 202501
PERIOD_TO:   int = _RANGE[1] if _RANGE else 202506


# ── Tolerance helper ──────────────────────────────────────────────────────────

def _within(a: float, b: float, tol: float = 1.0) -> bool:
    """True if |a - b| <= tol (handles floating-point rounding in SUM aggregations)."""
    return abs(a - b) <= tol


# ── SQM consistency ───────────────────────────────────────────────────────────

@_skip
def test_kpi_total_sqm_matches_trend_total():
    """
    KPI total_sqm must equal the sum of the 'Total' dataset in production_trend.
    Both now use LEN(CREWNO)>=8 and MAX(GANGTOTALSQMADJUSTED) per gang — they
    should produce identical aggregates.
    """
    from web import queries as q
    kpi   = q.get_kpi_summary(PERIOD_FROM, PERIOD_TO)
    trend = q.get_production_trend(PERIOD_FROM, PERIOD_TO)

    total_ds = next((d for d in trend["datasets"] if d["label"] == "Total"), None)
    assert total_ds is not None, "production_trend returned no 'Total' dataset"

    trend_sum = sum(total_ds["data"])
    assert _within(kpi["total_sqm"], trend_sum), (
        f"KPI total_sqm={kpi['total_sqm']} but trend Total sum={trend_sum} "
        f"(diff={abs(kpi['total_sqm'] - trend_sum):.0f})"
    )


@_skip
def test_kpi_total_sqm_matches_r_per_sqm_denominator():
    """
    KPI total_sqm must equal sum(r_per_sqm.total_sqm) — both use STOPE BREAKING
    and LEN(CREWNO)>=8.  A mismatch here would indicate the KPI is counting gangs
    that the R/m² tab excludes, producing an inflated R/m² ratio.
    """
    from web import queries as q
    kpi   = q.get_kpi_summary(PERIOD_FROM, PERIOD_TO)
    r_sqm = q.get_rands_per_sqm(PERIOD_FROM, PERIOD_TO)

    rps_sum = sum(r_sqm["total_sqm"])
    assert _within(kpi["total_sqm"], rps_sum), (
        f"KPI total_sqm={kpi['total_sqm']} but r_per_sqm sum={rps_sum} "
        f"(diff={abs(kpi['total_sqm'] - rps_sum):.0f}). "
        "Check LEN(CREWNO)>=8 filter in get_kpi_summary."
    )


@_skip
def test_kpi_total_sqm_matches_simulation_sqm():
    """KPI total_sqm must equal simulation total_actual_sqm (same filters)."""
    from web import queries as q
    kpi = q.get_kpi_summary(PERIOD_FROM, PERIOD_TO)
    sim = q.get_simulation_data(PERIOD_FROM, PERIOD_TO)

    assert _within(kpi["total_sqm"], sim["total_actual_sqm"]), (
        f"KPI total_sqm={kpi['total_sqm']} but simulation total_actual_sqm={sim['total_actual_sqm']} "
        f"(diff={abs(kpi['total_sqm'] - sim['total_actual_sqm']):.0f})"
    )


@_skip
def test_pre_adj_and_adj_are_in_same_order_of_magnitude():
    """
    Pre-adj SQM (WORKPLACETOTALSQM) and adj SQM (GANGTOTALSQMADJUSTED) measure the
    same workplaces.  They need not have a fixed direction: adjustments in stoping
    can be positive (extra m²) OR negative (deductions for over-mining, quality).
    This test guards against the two series drifting far apart (ratio outside 0.5–2.0)
    which would indicate a data or join error, not just a normal adjustment swing.
    """
    from web import queries as q
    pre = q.get_pre_adj_trend(PERIOD_FROM, PERIOD_TO)
    adj = q.get_production_trend(PERIOD_FROM, PERIOD_TO)

    pre_ds = next((d for d in pre["datasets"] if d["label"] == "Total"), None)
    adj_ds = next((d for d in adj["datasets"] if d["label"] == "Total"), None)
    if pre_ds is None or adj_ds is None:
        pytest.skip("No data returned for the probe period range")

    assert pre["periods"] == adj["periods"], (
        "pre_adj_trend and production_trend returned different period lists"
    )

    for lbl, pre_val, adj_val in zip(pre["periods"], pre_ds["data"], adj_ds["data"]):
        if adj_val == 0:
            continue  # skip empty months
        ratio = pre_val / adj_val
        assert 0.5 <= ratio <= 2.0, (
            f"{lbl}: pre-adj/adj ratio={ratio:.2f} (pre={pre_val}, adj={adj_val}). "
            "Values more than 2× apart suggest a data or aggregation error."
        )


# ── Bonus consistency ─────────────────────────────────────────────────────────

@_skip
def test_kpi_total_bonus_matches_gangtype_bonus_grand_total():
    """
    KPI total_bonus must equal the Grand Total row in get_bonus_by_gangtype.
    Both query PARTICIPANTSDETAIL with the same gang!='xxx' / crewno!='-' filters.
    """
    from web import queries as q
    kpi  = q.get_kpi_summary(PERIOD_FROM, PERIOD_TO)
    bsum = q.get_bonus_by_gangtype(PERIOD_FROM, PERIOD_TO)

    grand = next((r for r in bsum["rows"] if r.get("gangtype") == "Grand Total"), None)
    assert grand is not None, "get_bonus_by_gangtype returned no Grand Total row"

    assert _within(kpi["total_bonus"], grand["grand_total"]), (
        f"KPI total_bonus={kpi['total_bonus']} but gangtype grand_total={grand['grand_total']} "
        f"(diff={abs(kpi['total_bonus'] - grand['grand_total']):.2f})"
    )


@_skip
def test_kpi_total_bonus_matches_section_bonus_sum():
    """
    KPI total_bonus (section=ALL) must equal the sum of grand_totals across all
    sections returned by get_bonus_by_section.  Both use _get_participants_bonus
    with the same filters.
    """
    from web import queries as q
    kpi  = q.get_kpi_summary(PERIOD_FROM, PERIOD_TO)
    sbon = q.get_bonus_by_section(PERIOD_FROM, PERIOD_TO)

    section_sum = sum(r["grand_total"] for r in sbon["rows"])
    assert _within(kpi["total_bonus"], section_sum), (
        f"KPI total_bonus={kpi['total_bonus']} but section grand_total sum={section_sum} "
        f"(diff={abs(kpi['total_bonus'] - section_sum):.2f})"
    )


@_skip
def test_sqm_range_bonus_not_double_counted():
    """
    Total bonus summed across all SQM ranges should be between 0.5× and 1.5× of the
    KPI STOPE BREAKING section bonus from PARTICIPANTSDETAIL.
    A ratio outside this band would indicate double-counting in the bonus merge
    (the bug this test was written to catch: crewno-level grouping × gang-level bonus).

    Note: ranges only cover STOPE BREAKING + CREWNO>=8; KPI bonus covers all gang types.
    The ratio will legitimately be < 1.0 when non-STOPE-BREAKING bonus is significant.
    We check the upper bound (>1.5) as the double-counting signal.
    """
    from web import queries as q
    kpi   = q.get_kpi_summary(PERIOD_FROM, PERIOD_TO)
    rng   = q.get_sqm_range_analysis(PERIOD_FROM, PERIOD_TO)

    range_total = sum(r["total_bonus"] for r in rng["rows"])
    kpi_bonus   = kpi["total_bonus"]

    if kpi_bonus == 0:
        pytest.skip("No bonus data in probe period — cannot ratio-check")

    ratio = range_total / kpi_bonus
    assert ratio <= 1.5, (
        f"sqm_range total_bonus={range_total:.0f} is {ratio:.2f}× KPI bonus={kpi_bonus:.0f}. "
        "Ratio >1.5 suggests bonus double-counting in the crewno-to-gang merge."
    )


# ── Known discrepancy: gang_detail vs PARTICIPANTSDETAIL ─────────────────────

@_skip
def test_gang_detail_bonus_discrepancy_documented():
    """
    get_gang_detail() computes bonus as GANGPRODUCTIONDETAIL(per-person rate) × GANGLABOUR.
    get_kpi_summary() uses PARTICIPANTSDETAIL (canonical individual payments).

    These two WILL differ — this is by design: PARTICIPANTSDETAIL has 3 components
    (stm/safety/driller) while gang_detail needs 4 (break/drill/sweep/safety).

    This test WARNS (does not fail) if the discrepancy exceeds 30%, which would
    suggest a systematic rate mismatch worth investigating.
    """
    from web import queries as q
    gangs = q.get_gang_detail(PERIOD_FROM, PERIOD_TO)
    kpi   = q.get_kpi_summary(PERIOD_FROM, PERIOD_TO)

    gd_stm_total     = sum(r["total_bonus"] for r in gangs
                           if r.get("gangtype", "").upper() == "STOPE BREAKING")
    participants_total = kpi["total_bonus"]

    if participants_total == 0:
        pytest.skip("No bonus data in probe period")

    pct_diff = abs(gd_stm_total - participants_total) / participants_total * 100
    if pct_diff > 30:
        warnings.warn(
            f"STEP-0 discrepancy: gang_detail STOPE BREAKING bonus total "
            f"({gd_stm_total:,.0f}) differs from PARTICIPANTSDETAIL canonical "
            f"({participants_total:,.0f}) by {pct_diff:.1f}%. "
            "Possible cause: GANGPRODUCTIONDETAIL per-person rates diverge from "
            "actual individual payments in PARTICIPANTSDETAIL.",
            UserWarning,
            stacklevel=1,
        )
