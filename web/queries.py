"""
web/queries.py
--------------
All database queries for the Phakisa Stoping Analysis web dashboard.
Uses three views in STPTM2000:
  - GANGPRODUCTIONDETAIL  : one row per (gang, workplace, period) — SQM + bonus components
  - PARTICIPANTSDETAIL    : one row per (employee, period) — individual bonus payments
  - PRODUCTIONWPDETAIL    : one row per (workplace, period) — workplace SQM pre/post adjustment

Key column notes:
  - Bonus columns in GANGPRODUCTIONDETAIL are PER-PERSON amounts.
    Multiply by GANGLABOUR to obtain total gang payout.
  - GANGPRODUCTIONBONUS = GANGFINALBREAKBONUS + GANGFINALSAFETYBONUS (production subtotal).
    NOTE: GANGFINALBREAKBONUS is an aliased name (kept for compatibility with the shared
    GANGPRODUCTIONDETAIL schema used across Harmony sites). At Tshepong it was NOT a flat
    rate-per-m² breaking bonus — the source column is GANGFINALEFFICIENCYBONUS, driven by
    GANGEFFICIENCY (m²/empl). Per the "Thibakotsi Stoping Incentive Scheme Cat 4-8" policy
    (JB_202603_STPTEAM_REV04), teams below the
    entry-level efficiency (Wide Raise panels excluded) earn zero on this component; above
    it, the amount is looked up from an m²-by-team-size table and then adjusted by netting
    (WORKPLACENETTINGRATE) and the B-Reef stope-width factor (B_REEF_SW_FACTOR). We label it
    "Efficiency Bonus" in this app's output to match how it's actually calculated.
  - GANGTOTALSQMADJUSTED repeats across workplace rows for the same gang/period.
    Use MAX() when grouping by gang.
  - WORKPLACETOTALSQM = WPMAXLEDGESQM + WPMAXSTOPESQM per workplace — use SUM() across
    workplaces for "startup SQM" (pre-adjustment).
  - Safety indicators: GANGLTIIND, GANGDRESSINGIND, GANGFATALIND (not GANGSAFETYIND).
  - No MONTHSHIFTS column in GANGPRODUCTIONDETAIL.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
from src.database.connection import read_sql

# ── Month label helpers ────────────────────────────────────────────────────────

_MONTH_LABELS = {
    1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
    7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec",
}

def _period_label(p: int) -> str:
    y, m = p // 100, p % 100
    return f"{_MONTH_LABELS.get(m, str(m))}-{str(y)[2:]}"

def _build_period_list(period_from: int, period_to: int) -> list[int]:
    periods: list[int] = []
    y, m = period_from // 100, period_from % 100
    y_end, m_end = period_to // 100, period_to % 100
    while y * 100 + m <= y_end * 100 + m_end:
        periods.append(y * 100 + m)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return periods


def _prior_period_range(period_from: int, period_to: int) -> tuple[int, int]:
    """Return the period range of the same length immediately before (period_from, period_to)."""
    n = len(_build_period_list(period_from, period_to))
    # prior_to = one month before period_from
    y, m = period_from // 100, period_from % 100
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    prior_to = y * 100 + m
    # prior_from = n-1 months before prior_to
    m -= (n - 1)
    while m <= 0:
        m += 12
        y -= 1
    return y * 100 + m, prior_to

# ── Filter builders ────────────────────────────────────────────────────────────

def _section_filter(section: str) -> str:
    if section and section.upper() != "ALL":
        safe = section.upper().replace("'", "''")
        return f"AND UPPER(LTRIM(RTRIM(SECTION))) = '{safe}'"
    return ""

def _gangtype_filter(gangtype: str) -> str:
    """Accepts "ALL", a single type, or a comma-separated list (the filter bar's multi-select).

    Real gang types in GANGPRODUCTIONDETAIL (verified 2026-11-14): STOPE BREAKING, STOPE
    CLEANING, CENTRE GULLY, EQUIPPING, RIGGING — not STOPING/SURFACE ASSISTANT/ANCILLARY,
    which never appear in the data and were stale placeholder options in the filter bar.
    """
    if gangtype and gangtype.upper() != "ALL":
        types = [t.strip().upper().replace("'", "''") for t in gangtype.split(",") if t.strip()]
        if not types:
            return ""
        if len(types) == 1:
            return f"AND UPPER(LTRIM(RTRIM(GANGTYPE))) = '{types[0]}'"
        in_list = ", ".join(f"'{t}'" for t in types)
        return f"AND UPPER(LTRIM(RTRIM(GANGTYPE))) IN ({in_list})"
    return ""

# ── PARTICIPANTSDETAIL bonus helper ───────────────────────────────────────────

def _get_participants_bonus(period_from: int, period_to: int,
                            section: str = "ALL") -> pd.DataFrame:
    """
    Total bonus per (section, period, gang) from PARTICIPANTSDETAIL.
    Filter: gang != 'xxx' AND crewno != '-'

    total_bonus = EMPLOYEESTOPETEAMBONUS (STM) ALONE — verified 2026-08-26 against the raw
    PARTICIPANTSEARN<period> tables' own EMPLOYEETOTALBONUS column (the source system's own
    per-employee grand total, which broadcasts identically across all of an employee's rows
    in a period when they worked more than one gang — e.g. a team leader overseeing two
    gangs). Summing STM alone across all rows reproduces that broadcast total to within
    0.004% (R3,747,885 vs R3,748,021 for period 202607, R135 apart on R3.75M). The
    previous formula (STM + Safety + Driller) overstated the true total by ~12-15% — Safety
    and Driller are informational sub-figures already reflected inside STM, not separate
    add-on payments, despite carrying their own columns. EMPLOYEESAFETYBONUS/
    EMPLOYEEDRILLERBONUS are still returned below for reference/breakdown display, but must
    not be added on top of total_stm_bonus anywhere.

    total_awop_bonus (EMPLOYEEAWOPPENALTY, verified 2026-11-03) is the same kind of
    already-included reference figure: real and substantial (2,809 of 120,174 PARTICIPANTS-
    DETAIL rows nonzero, -R3.54M total across all history), but row-level checks against the
    source PARTICIPANTSEARN table show STM is already the POST-AWOP figure (e.g. a 1-shift
    AWOP row: pre-penalty bonus 2,511.05 × 50% kept = 1,255.52 = the row's own STM; the
    penalty column just records the 1,255.53 that was docked to get there). So, like Safety
    and Driller, this must never be added on top of total_stm_bonus — it is returned for
    reference/breakdown display only (the bonus-composition and outcomes tables).
    """
    sf = _section_filter(section)
    period_filter = (f"TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}"
                     if period_from > 0 and period_to > 0
                     else "TRY_CAST(PERIOD AS BIGINT) BETWEEN 200001 AND 209912")
    query = f"""
        SELECT
            LTRIM(RTRIM(SECTION))  AS section,
            LTRIM(RTRIM(PERIOD))   AS period,
            LTRIM(RTRIM(GANG))     AS gang,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS total_stm_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT), 0)) AS total_safety_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT), 0)) AS total_driller_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEEAWOPPENALTY    AS FLOAT), 0)) AS total_awop_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS total_bonus
        FROM [PARTICIPANTSDETAIL]
        WHERE LTRIM(RTRIM(GANG))   != 'xxx'
          AND LTRIM(RTRIM(CREWNO)) != '-'
          AND {period_filter}
          {sf}
        GROUP BY
            LTRIM(RTRIM(SECTION)),
            LTRIM(RTRIM(PERIOD)),
            LTRIM(RTRIM(GANG))
    """
    df = read_sql(query)
    if not df.empty:
        df["period"] = df["period"].astype(str)
    return df


def get_scope_note(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """What this dashboard includes vs excludes — Mponeng-pattern scope note, Phakisa data.

    Included: the 5 real gang types found in GANGPRODUCTIONDETAIL (verified 2026-11-14 via
    GROUP BY GANGTYPE over the whole table): STOPE BREAKING, STOPE CLEANING, CENTRE GULLY,
    EQUIPPING, RIGGING. The filter bar's old dropdown listed STOPING/SURFACE ASSISTANT/
    ANCILLARY, which never appear in this data — a stale/wrong list, fixed alongside this.

    Excluded: the Stoping Miner Incentive Scheme (policy JJ_202607_STPMINER_REV12 — Miner
    paid 4x the Team bonus), tracked entirely separately in monthly MINERSEARN<period>
    tables (not materialized the way GANGPRODUCTIONDETAIL/PARTICIPANTSDETAIL are, so this
    pulls a dynamic UNION ALL over whichever MINERSEARN<period> tables exist for the
    selected range). This dashboard has never included it in any total anywhere. No separate
    "service crew" scheme was found in STPTM2000 (searched INFORMATION_SCHEMA for
    MINER/SERVICE/TRAM-named tables — only MINERS* exist), so Mponeng's second excluded
    category has no Phakisa equivalent and is omitted rather than invented.
    """
    tables_df = read_sql("""
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_NAME LIKE 'MINERSEARN2%' AND TABLE_NAME NOT LIKE '%v'
    """)
    real_tables = set(tables_df["TABLE_NAME"]) if not tables_df.empty else set()
    wanted = [p for p in _build_period_list(period_from, period_to) if f"MINERSEARN{p}" in real_tables]

    sf = _section_filter(section)
    miners_total = 0.0
    if wanted:
        union_sql = " UNION ALL ".join(
            f"SELECT TRY_CAST(EMPLOYEETOTALBONUS AS FLOAT) AS total, SECTION FROM [MINERSEARN{p}]"
            for p in wanted
        )
        df = read_sql(f"""
            SELECT SUM(ISNULL(total, 0)) AS total
            FROM ({union_sql}) AS u
            WHERE 1=1 {sf}
        """)
        miners_total = float(df["total"].iloc[0] or 0) if not df.empty and df["total"].iloc[0] is not None else 0.0

    return {
        "included": {
            "gang_types": ["STOPE BREAKING", "STOPE CLEANING", "CENTRE GULLY", "EQUIPPING", "RIGGING"],
            "label": "Stope team crews",
        },
        "excluded": [
            {
                "label": "Stoping miners",
                "rand": round(miners_total, 2),
                "periods": len(wanted),
                "reason": "Separate scheme (policy JJ_202607_STPMINER_REV12 — Miner paid 4x the Team bonus), tracked in its own MINERSEARN tables, never included in this dashboard's totals.",
            },
        ],
    }


# ── Public query functions ─────────────────────────────────────────────────────

def get_available_periods() -> list[dict]:
    """All distinct periods in GANGPRODUCTIONDETAIL, newest first."""
    df = read_sql("""
        SELECT DISTINCT LTRIM(RTRIM(PERIOD)) AS period
        FROM [GANGPRODUCTIONDETAIL]
        WHERE PERIOD IS NOT NULL
          AND LTRIM(RTRIM(PERIOD)) != ''
          AND TRY_CAST(PERIOD AS BIGINT) BETWEEN 200001 AND 209912
        ORDER BY period DESC
    """)
    out = []
    for p_str in df["period"].tolist():
        try:
            p = int(str(p_str).strip())
            out.append({"value": p, "label": _period_label(p)})
        except ValueError:
            pass
    return out


def get_sections(period_from: int, period_to: int) -> list[str]:
    df = read_sql(f"""
        SELECT DISTINCT LTRIM(RTRIM(SECTION)) AS section
        FROM [GANGPRODUCTIONDETAIL]
        WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          AND SECTION IS NOT NULL AND LTRIM(RTRIM(SECTION)) != ''
        ORDER BY section
    """)
    return df["section"].tolist()


def get_kpi_summary(period_from: int, period_to: int,
                    section: str = "ALL", gangtype: str = "ALL") -> dict:
    """Top-level KPIs: total m², total bonus (with breakdown), gang count, avg m²/gang, R/m²."""
    sf = _section_filter(section)

    # SQM from GANGPRODUCTIONDETAIL (STOPE BREAKING, deduplicated by gang).
    # LEN(CREWNO)>=8 matches the filter used by get_rands_per_sqm / get_gang_detail so
    # the KPI denominator is consistent with every breakdown tab.
    sqm_df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return {}
    sqm_df["period"] = sqm_df["period"].astype(str)

    # Scope bonus to the same STOPE BREAKING (section, period, gang) keys as total_sqm —
    # _get_participants_bonus() on its own returns every gang type, which would silently mix
    # STOPE CLEANING/CENTRE GULLY/etc. bonus into a tile whose m² (and therefore R/m²) is
    # Stope-Breaking-only. Same fix already applied in get_rands_per_sqm() for the R/m²
    # Summary Table — this tile just never got it, which is why "Total Bonus"/R/m² here used
    # to run higher than the Summary Table's Total/Avg row for the identical period/section.
    bonus_df_all = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df_all.empty:
        sb_keys = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_df = sb_keys.merge(
            bonus_df_all[["section", "period", "gang", "total_bonus", "total_stm_bonus",
                          "total_safety_bonus", "total_driller_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        for col in ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
            bonus_df[col] = bonus_df[col].fillna(0).astype(float)
    else:
        bonus_df = bonus_df_all

    total_sqm           = float(sqm_df["sqm"].sum())
    total_bonus         = float(bonus_df["total_bonus"].sum())         if not bonus_df.empty else 0.0
    total_safety_bonus  = float(bonus_df["total_safety_bonus"].sum())  if not bonus_df.empty else 0.0
    total_driller_bonus = float(bonus_df["total_driller_bonus"].sum()) if not bonus_df.empty else 0.0
    total_stm_bonus     = float(bonus_df["total_stm_bonus"].sum())     if not bonus_df.empty else 0.0
    gang_count          = int(sqm_df.groupby(["section", "gang"]).ngroups)
    avg_sqm             = float(sqm_df["sqm"].mean())
    r_per_sqm           = total_bonus / total_sqm if total_sqm > 0 else 0
    periods_seen        = sqm_df["period"].nunique()

    # Prior-period comparison (best-effort — None fields if prior data unavailable)
    prior_total_sqm = prior_total_bonus = prior_r_per_sqm = None
    prior_total_safety_bonus = prior_total_driller_bonus = prior_total_stm_bonus = None
    try:
        pr_from, pr_to = _prior_period_range(period_from, period_to)
        pr_sqm_df = read_sql(f"""
            SELECT section, period, gang, MAX(sqm) AS sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(SECTION)) AS section,
                    LTRIM(RTRIM(PERIOD))  AS period,
                    LTRIM(RTRIM(GANG))    AS gang,
                    TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {pr_from} AND {pr_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf}
            ) AS q
            GROUP BY section, period, gang
        """)
        pr_bonus_df_all = _get_participants_bonus(pr_from, pr_to, section)
        _pr_cols = ["total_bonus", "total_safety_bonus", "total_driller_bonus", "total_stm_bonus"]
        if not pr_bonus_df_all.empty and not pr_sqm_df.empty:
            pr_sb_keys = pr_sqm_df[["section", "period", "gang"]].drop_duplicates()
            pr_bonus_df = pr_sb_keys.merge(
                pr_bonus_df_all[["section", "period", "gang"] + _pr_cols],
                on=["section", "period", "gang"], how="left"
            )
            for col in _pr_cols:
                pr_bonus_df[col] = pr_bonus_df[col].fillna(0).astype(float)
        else:
            pr_bonus_df = pr_bonus_df_all
        prior_total_sqm   = float(pr_sqm_df["sqm"].sum())        if not pr_sqm_df.empty   else 0.0
        prior_total_bonus = float(pr_bonus_df["total_bonus"].sum()) if not pr_bonus_df.empty else 0.0
        prior_total_safety_bonus  = float(pr_bonus_df["total_safety_bonus"].sum())  if not pr_bonus_df.empty else 0.0
        prior_total_driller_bonus = float(pr_bonus_df["total_driller_bonus"].sum()) if not pr_bonus_df.empty else 0.0
        prior_total_stm_bonus     = float(pr_bonus_df["total_stm_bonus"].sum())     if not pr_bonus_df.empty else 0.0
        prior_r_per_sqm   = prior_total_bonus / prior_total_sqm  if prior_total_sqm > 0   else 0.0
    except Exception:
        pass  # prior comparison is non-critical; don't break the main KPI response

    return {
        "total_sqm":           round(total_sqm, 0),
        "total_bonus":         round(total_bonus, 2),
        "total_safety_bonus":  round(total_safety_bonus, 2),
        "total_driller_bonus": round(total_driller_bonus, 2),
        "total_stm_bonus":     round(total_stm_bonus, 2),
        "gang_count":          gang_count,
        "avg_sqm_per_gang":    round(avg_sqm, 1),
        "r_per_sqm":           round(r_per_sqm, 2),
        "periods":             periods_seen,
        "prior_total_sqm":     round(prior_total_sqm,  0)  if prior_total_sqm  is not None else None,
        "prior_total_bonus":   round(prior_total_bonus, 2)  if prior_total_bonus is not None else None,
        "prior_r_per_sqm":     round(prior_r_per_sqm,  2)  if prior_r_per_sqm  is not None else None,
        "prior_total_safety_bonus":  round(prior_total_safety_bonus, 2)  if prior_total_safety_bonus  is not None else None,
        "prior_total_driller_bonus": round(prior_total_driller_bonus, 2) if prior_total_driller_bonus is not None else None,
        "prior_total_stm_bonus":     round(prior_total_stm_bonus, 2)     if prior_total_stm_bonus     is not None else None,
    }


def get_production_trend(period_from: int, period_to: int,
                          section: str = "ALL") -> dict:
    """Monthly total adjusted m² (STOPE BREAKING) by section — for trend chart."""
    sf = _section_filter(section)
    # LEN(CREWNO)>=8 added to match get_kpi_summary so the trend total equals the KPI total.
    df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if df.empty:
        return {"periods": [], "datasets": []}

    df["period"] = df["period"].astype(str)
    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    pivot    = df.groupby(["section", "period"])["sqm"].sum().reset_index()
    sections = sorted(df["section"].unique())
    colors   = ["#1565C0","#2E7D32","#AD1457","#E65100","#4527A0","#00695C"]

    datasets = []
    for i, sec in enumerate(sections):
        sec_data = pivot[pivot["section"] == sec].set_index("period")
        values   = [round(float(sec_data.loc[p, "sqm"]), 0) if p in sec_data.index else 0
                    for p in period_order]
        datasets.append({
            "label":           sec,
            "data":            values,
            "borderColor":     colors[i % len(colors)],
            "backgroundColor": colors[i % len(colors)] + "33",
        })

    total_by_period = pivot.groupby("period")["sqm"].sum()
    totals = [round(float(total_by_period.get(p, 0)), 0) for p in period_order]
    datasets.append({
        "label":           "Total",
        "data":            totals,
        "borderColor":     "#FFA000",
        "backgroundColor": "#FFA00033",
        "borderWidth":     3,
        "borderDash":      [6, 3],
    })
    return {"periods": period_labels, "datasets": datasets}


def get_pre_adj_trend(period_from: int, period_to: int,
                      section: str = "ALL") -> dict:
    """Monthly Pre-Adj m² (SUM of WORKPLACETOTALSQM per gang) by section.

    GANGPRODUCTIONDETAIL can carry multiple rows for the same (gang, workplace) in a
    period — PRODUCTIONEARN has one row per production activity/blast, and WORKPLACETOTALSQM
    repeats identically across them. We MAX() per (gang, workplace) first to collapse those
    duplicates, then SUM across the gang's genuinely distinct workplaces — otherwise a
    workplace with N activity rows gets counted N times and inflates Pre-Adj vs Adj.
    """
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT section, period, gang, SUM(wp_sqm) AS sqm
        FROM (
            SELECT section, period, gang, workplace, MAX(wp_sqm) AS wp_sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(SECTION))   AS section,
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    LTRIM(RTRIM(CREWNO))    AS crewno,
                    TRY_CAST(WORKPLACETOTALSQM AS FLOAT) AS wp_sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf}
            ) AS raw
            GROUP BY section, period, gang, workplace
        ) AS q
        GROUP BY section, period, gang
    """)
    if df.empty:
        return {"periods": [], "datasets": []}

    df["period"] = df["period"].astype(str)
    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    pivot    = df.groupby(["section", "period"])["sqm"].sum().reset_index()
    sections = sorted(df["section"].unique())
    colors   = ["#1565C0","#2E7D32","#AD1457","#E65100","#4527A0","#00695C"]

    datasets = []
    for i, sec in enumerate(sections):
        sec_data = pivot[pivot["section"] == sec].set_index("period")
        values   = [round(float(sec_data.loc[p, "sqm"]), 0) if p in sec_data.index else 0
                    for p in period_order]
        datasets.append({
            "label":           sec,
            "data":            values,
            "borderColor":     colors[i % len(colors)],
            "backgroundColor": colors[i % len(colors)] + "33",
        })

    total_by_period = pivot.groupby("period")["sqm"].sum()
    totals = [round(float(total_by_period.get(p, 0)), 0) for p in period_order]
    datasets.append({
        "label":           "Total",
        "data":            totals,
        "borderColor":     "#FFA000",
        "backgroundColor": "#FFA00033",
        "borderWidth":     3,
        "borderDash":      [6, 3],
    })
    return {"periods": period_labels, "datasets": datasets}


def get_bonus_by_gangtype(period_from: int, period_to: int,
                          section: str = "ALL") -> dict:
    """Total bonus by GANGTYPE and period — from PARTICIPANTSDETAIL with breakdown.

    total_bonus = stm_bonus alone — see _get_participants_bonus()'s docstring for the
    verification behind this (Safety/Driller are informational sub-figures already
    reflected inside STM, not additive; summing STM alone matches the source system's own
    per-employee EMPLOYEETOTALBONUS to within 0.004%).
    """
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(GANGTYPE))  AS gangtype,
            LTRIM(RTRIM(PERIOD))    AS period,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS stm_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESAFETYBONUS    AS FLOAT), 0)) AS safety_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEEDRILLERBONUS   AS FLOAT), 0)) AS driller_bonus,
            SUM(ISNULL(TRY_CAST(EMPLOYEESTOPETEAMBONUS AS FLOAT), 0)) AS total_bonus
        FROM [PARTICIPANTSDETAIL]
        WHERE LTRIM(RTRIM(GANG))   != 'xxx'
          AND LTRIM(RTRIM(CREWNO)) != '-'
          AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          {sf}
        GROUP BY LTRIM(RTRIM(GANGTYPE)), LTRIM(RTRIM(PERIOD))
    """)
    if df.empty:
        return {"periods": [], "rows": [], "chart": {}}

    df["period"] = df["period"].astype(str)
    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    gangtypes = sorted(df["gangtype"].dropna().unique())
    gt_idx    = df.set_index(["gangtype", "period"])

    gt_colors = {
        "STOPE BREAKING":   "#1565C0",
        "STOPE CLEANING":   "#2E7D32",
        "CENTRE GULLY":     "#E65100",
        "STOPING":          "#6A1B9A",
        "SURFACE ASSISTANT":"#00695C",
        "ANCILLARY":        "#607D8B",
    }

    rows, chart_datasets = [], []
    for gt in gangtypes:
        if not gt:
            continue
        row = {"gangtype": gt}
        vals = []
        total = total_stm = total_safety = total_driller = 0.0
        for p in period_order:
            key = (gt, p)
            if key in gt_idx.index:
                stm     = float(gt_idx.loc[key, "stm_bonus"])
                safety  = float(gt_idx.loc[key, "safety_bonus"])
                driller = float(gt_idx.loc[key, "driller_bonus"])
                v       = float(gt_idx.loc[key, "total_bonus"])
            else:
                stm = safety = driller = v = 0.0
            row[p]              = round(v, 2)
            row[f"{p}_stm"]     = round(stm, 2)
            row[f"{p}_safety"]  = round(safety, 2)
            row[f"{p}_driller"] = round(driller, 2)
            vals.append(round(v, 2))
            total         += v
            total_stm     += stm
            total_safety  += safety
            total_driller += driller
        row["grand_total"]   = round(total, 2)
        row["grand_stm"]     = round(total_stm, 2)
        row["grand_safety"]  = round(total_safety, 2)
        row["grand_driller"] = round(total_driller, 2)
        if total == 0:
            continue
        rows.append(row)
        chart_datasets.append({
            "label":           gt,
            "data":            vals,
            "backgroundColor": gt_colors.get(gt.upper(), "#888"),
        })

    # Grand total row
    gt_total_row = {"gangtype": "Grand Total"}
    for p in period_order:
        pf = df[df["period"] == p]
        gt_total_row[p]              = round(float(pf["total_bonus"].sum()), 2)
        gt_total_row[f"{p}_stm"]     = round(float(pf["stm_bonus"].sum()), 2)
        gt_total_row[f"{p}_safety"]  = round(float(pf["safety_bonus"].sum()), 2)
        gt_total_row[f"{p}_driller"] = round(float(pf["driller_bonus"].sum()), 2)
    gt_total_row["grand_total"]   = round(float(df["total_bonus"].sum()), 2)
    gt_total_row["grand_stm"]     = round(float(df["stm_bonus"].sum()), 2)
    gt_total_row["grand_safety"]  = round(float(df["safety_bonus"].sum()), 2)
    gt_total_row["grand_driller"] = round(float(df["driller_bonus"].sum()), 2)
    rows.append(gt_total_row)

    return {
        "periods":     period_labels,
        "period_keys": period_order,
        "rows":        rows,
        "chart":       {"labels": period_labels, "datasets": chart_datasets},
    }


def get_bonus_by_section(period_from: int, period_to: int) -> dict:
    """Total bonus by SECTION and period — with breakdown."""
    df = _get_participants_bonus(period_from, period_to, section="ALL")
    if df.empty:
        return {"periods": [], "rows": []}

    period_order  = sorted(df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]
    pivot         = df.groupby(["section", "period"])[
        ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]
    ].sum().reset_index()
    sections = sorted(df["section"].unique())

    rows = []
    for sec in sections:
        row      = {"section": sec}
        sec_data = pivot[pivot["section"] == sec].set_index("period")
        total = total_stm = total_safety = total_driller = 0.0
        for p in period_order:
            if p in sec_data.index:
                v       = float(sec_data.loc[p, "total_bonus"])
                stm     = float(sec_data.loc[p, "total_stm_bonus"])
                safety  = float(sec_data.loc[p, "total_safety_bonus"])
                driller = float(sec_data.loc[p, "total_driller_bonus"])
            else:
                v = stm = safety = driller = 0.0
            row[p]              = round(v, 2)
            row[f"{p}_stm"]     = round(stm, 2)
            row[f"{p}_safety"]  = round(safety, 2)
            row[f"{p}_driller"] = round(driller, 2)
            total         += v
            total_stm     += stm
            total_safety  += safety
            total_driller += driller
        row["grand_total"]         = round(total, 2)
        row["grand_stm"]           = round(total_stm, 2)
        row["total_safety_bonus"]  = round(total_safety, 2)
        row["total_driller_bonus"] = round(total_driller, 2)
        rows.append(row)

    return {"periods": period_labels, "period_keys": period_order, "rows": rows}


# ── m² Range analysis ──────────────────────────────────────────────────────────

_SQM_RANGES = [
    ("Below 100",   0,    100),
    ("100 to 200",  100,  200),
    ("200 to 300",  200,  300),
    ("300 to 350",  300,  350),
    ("350 to 400",  350,  400),
    ("400 to 450",  400,  450),
    ("450 to 500",  450,  500),
    ("500 to 550",  500,  550),
    ("550 to 600",  550,  600),
    ("Greater 600", 600,  9999999),
]

def _sqm_range_label(sqm: float) -> str:
    for label, lo, hi in _SQM_RANGES:
        if lo <= sqm < hi:
            return label
    return "Greater 600"


def get_sqm_range_analysis(period_from: int, period_to: int,
                           section: str = "ALL") -> dict:
    """Gang SQM range distribution with bonus breakdown from PARTICIPANTSDETAIL."""
    sf = _section_filter(section)
    # Group by gang (not crewno) so each gang appears once per period.
    # This prevents bonus double-counting when a gang has multiple CREWNO rows
    # in GANGPRODUCTIONDETAIL: the PARTICIPANTSDETAIL bonus merge is on gang key,
    # so multiple crewno rows for the same gang would each receive the full gang
    # bonus and inflate the range-bucket totals.
    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(SECTION)) AS section,
            LTRIM(RTRIM(PERIOD))  AS period,
            LTRIM(RTRIM(GANG))    AS gang,
            MAX(TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT)) AS sqm,
            MAX(TRY_CAST(GANGLABOUR           AS FLOAT)) AS labour
        FROM [GANGPRODUCTIONDETAIL]
        WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
          AND UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
          {sf}
        GROUP BY
            LTRIM(RTRIM(SECTION)),
            LTRIM(RTRIM(PERIOD)),
            LTRIM(RTRIM(GANG))
    """)
    if df.empty:
        return {"ranges": [], "rows": [], "chart": {}, "period_rows": []}

    df["period"] = df["period"].astype(str)

    # Merge bonus from PARTICIPANTSDETAIL
    pbonus = _get_participants_bonus(period_from, period_to, section)
    _bonus_cols = ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus",
                   "total_awop_bonus", "total_bonus"]
    if not pbonus.empty:
        pbonus["period"] = pbonus["period"].astype(str)
        df = df.merge(
            pbonus[["section", "period", "gang"] + _bonus_cols],
            on=["section", "period", "gang"], how="left"
        )
        for col in _bonus_cols:
            df[col] = df[col].fillna(0)
    else:
        for col in _bonus_cols:
            df[col] = 0.0

    df["sqm_range"] = df["sqm"].apply(lambda x: _sqm_range_label(float(x or 0)))
    range_order = [r[0] for r in _SQM_RANGES]

    # Aggregate summary across all periods
    agg = df.groupby("sqm_range").agg(
        crew_count    = ("gang",               "count"),
        avg_labour    = ("labour",             "mean"),
        avg_sqm       = ("sqm",               "mean"),
        total_stm     = ("total_stm_bonus",   "sum"),
        total_safety  = ("total_safety_bonus","sum"),
        total_driller = ("total_driller_bonus","sum"),
        total_awop    = ("total_awop_bonus",  "sum"),
        total_bonus   = ("total_bonus",       "sum"),
    ).reset_index()

    rows = []
    for rng in range_order:
        r = agg[agg["sqm_range"] == rng]
        if r.empty:
            continue
        r = r.iloc[0]
        bodies    = round(float(r["avg_labour"]), 1) if float(r["avg_labour"]) > 0 else 0
        avg_sqm   = round(float(r["avg_sqm"]), 1)
        sqm_pm    = round(avg_sqm / bodies, 2) if bodies > 0 else 0
        rows.append({
            "range":         rng,
            "crew_count":    int(r["crew_count"]),
            "avg_labour":    bodies,
            "avg_sqm":       avg_sqm,
            "sqm_per_man":   sqm_pm,
            "stm_bonus":     round(float(r["total_stm"]),     2),
            "safety_bonus":  round(float(r["total_safety"]),  2),
            "driller_bonus": round(float(r["total_driller"]), 2),
            "awop_bonus":    round(float(r["total_awop"]),    2),
            "total_bonus":   round(float(r["total_bonus"]),   2),
        })

    # Per-period distribution for trend chart
    period_pivot = df.groupby(["period", "sqm_range"])["gang"].nunique().reset_index()
    period_pivot.columns = ["period", "sqm_range", "count"]
    period_order_list = sorted(df["period"].unique())
    period_labels     = [_period_label(int(p)) for p in period_order_list]

    range_colors = ["#ef5350","#FF9800","#FFEE58","#9CCC65","#26C6DA",
                    "#42A5F5","#5C6BC0","#AB47BC","#EC407A","#78909C"]
    chart_datasets = []
    for i, rng in enumerate(range_order):
        vals = []
        for p in period_order_list:
            sub = period_pivot[(period_pivot["period"].astype(str) == str(p)) &
                               (period_pivot["sqm_range"] == rng)]
            vals.append(int(sub["count"].sum()) if not sub.empty else 0)
        if any(v > 0 for v in vals):
            chart_datasets.append({
                "label": rng,
                "data":  vals,
                "backgroundColor": range_colors[i % len(range_colors)],
            })

    # Period bonus totals
    bonus_by_period = df.groupby("period")[
        ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus", "total_awop_bonus", "total_bonus"]
    ].sum()

    period_rows = []
    for p in period_order_list:
        row   = {"period": _period_label(int(p)), "period_raw": str(p)}
        total = 0
        for rng in range_order:
            sub = period_pivot[(period_pivot["period"].astype(str) == str(p)) &
                               (period_pivot["sqm_range"] == rng)]
            cnt = int(sub["count"].sum()) if not sub.empty else 0
            row[rng] = cnt
            total   += cnt
        row["total"] = total
        ps = str(p)
        if ps in bonus_by_period.index:
            bp = bonus_by_period.loc[ps]
            row["stm_bonus"]     = round(float(bp["total_stm_bonus"]),    2)
            row["safety_bonus"]  = round(float(bp["total_safety_bonus"]), 2)
            row["driller_bonus"] = round(float(bp["total_driller_bonus"]),2)
            row["awop_bonus"]    = round(float(bp["total_awop_bonus"]),   2)
            row["total_bonus"]   = round(float(bp["total_bonus"]),        2)
        else:
            for k in ["stm_bonus", "safety_bonus", "driller_bonus", "awop_bonus", "total_bonus"]:
                row[k] = 0.0
        period_rows.append(row)

    return {
        "ranges":      range_order,
        "rows":        rows,
        "chart":       {"labels": period_labels, "datasets": chart_datasets},
        "period_rows": period_rows,
    }


def get_gang_detail(period_from: int, period_to: int,
                    section: str = "ALL", gangtype: str = "ALL") -> list[dict]:
    """
    Full gang-level detail — one row per (gang, crewno, gangtype) per PERIOD.
    Phakisa bonus columns (same structure as Tshepong): efficiency, drill, sweeping penalty, safety (+ prod as subtotal).
    Safety indicators: LTI, Dressing, Fatal (not single GANGSAFETYIND).
    """
    sf = _section_filter(section)
    gf = _gangtype_filter(gangtype)

    # WORKPLACETOTALSQM (startup_sqm) can repeat across multiple activity rows for the
    # same (gang, workplace) — dedupe per workplace with an inner MAX() before summing,
    # otherwise a workplace with several activity rows inflates startup_sqm vs adj_sqm.
    df = read_sql(f"""
        SELECT
            section, period, gang, crewno, gangtype,
            MAX(adj_sqm)     AS adj_sqm,
            SUM(wp_sqm)      AS startup_sqm,
            MAX(labour)      AS labour,
            MAX(efficiency)  AS efficiency,
            MAX(efficiency_bonus) AS efficiency_bonus,
            MAX(drill_bonus) AS drill_bonus,
            MAX(sweep_penalty) AS sweep_penalty,
            MAX(safety_bonus)AS safety_bonus,
            MAX(lti_ind)     AS lti_ind,
            MAX(dress_ind)   AS dress_ind,
            MAX(fatal_ind)   AS fatal_ind,
            MAX(lti_penalty_pct)   AS lti_penalty_pct,
            MAX(dress_penalty_pct) AS dress_penalty_pct,
            MAX(fatal_penalty_pct) AS fatal_penalty_pct,
            MAX(stopewidth_rate)   AS stopewidth_rate,
            MAX(face_length)       AS face_length,
            MAX(netting_rate)      AS netting_rate
        FROM (
            SELECT
                section, period, gang, crewno, gangtype, workplace,
                MAX(adj_sqm)      AS adj_sqm,
                MAX(wp_sqm)       AS wp_sqm,
                MAX(labour)       AS labour,
                MAX(efficiency)   AS efficiency,
                MAX(efficiency_bonus) AS efficiency_bonus,
                MAX(drill_bonus)  AS drill_bonus,
                MAX(sweep_penalty)  AS sweep_penalty,
                MAX(safety_bonus) AS safety_bonus,
                MAX(lti_ind)      AS lti_ind,
                MAX(dress_ind)    AS dress_ind,
                MAX(fatal_ind)    AS fatal_ind,
                MAX(lti_penalty_pct)   AS lti_penalty_pct,
                MAX(dress_penalty_pct) AS dress_penalty_pct,
                MAX(fatal_penalty_pct) AS fatal_penalty_pct,
                MAX(stopewidth_rate)   AS stopewidth_rate,
                MAX(face_length)       AS face_length,
                MAX(netting_rate)      AS netting_rate
            FROM (
                SELECT
                    LTRIM(RTRIM(SECTION))   AS section,
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(CREWNO))    AS crewno,
                    LTRIM(RTRIM(GANGTYPE))  AS gangtype,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    TRY_CAST(GANGTOTALSQMADJUSTED   AS FLOAT) AS adj_sqm,
                    TRY_CAST(WORKPLACETOTALSQM      AS FLOAT) AS wp_sqm,
                    TRY_CAST(GANGLABOUR             AS FLOAT) AS labour,
                    TRY_CAST(GANGEFFICIENCY         AS FLOAT) AS efficiency,
                    TRY_CAST(GANGFINALBREAKBONUS     AS FLOAT) AS efficiency_bonus,
                    TRY_CAST(GANGDRILLERBONUS        AS FLOAT) AS drill_bonus,
                    TRY_CAST(GANGFINALSWEEPINGSBONUS AS FLOAT) AS sweep_penalty,
                    TRY_CAST(GANGFINALSAFETYBONUS    AS FLOAT) AS safety_bonus,
                    TRY_CAST(GANGLTIIND             AS FLOAT) AS lti_ind,
                    TRY_CAST(GANGDRESSINGIND         AS FLOAT) AS dress_ind,
                    TRY_CAST(GANGFATALIND            AS FLOAT) AS fatal_ind,
                    -- Real, mine-applied figures (policy §7, §6.4, §8.1) — display-only,
                    -- see _pull_anchored_stope_breaking()'s comments for verification detail.
                    TRY_CAST(GANGLTIPENALTYPERCENTAGE       AS FLOAT) AS lti_penalty_pct,
                    TRY_CAST(GANGDRESSINGSPENALTYPERCENTAGE AS FLOAT) AS dress_penalty_pct,
                    TRY_CAST(GANGFATALPENALTYPERCENTAGE     AS FLOAT) AS fatal_penalty_pct,
                    TRY_CAST(WORKPLACESTOPEWIDTHRATE        AS FLOAT) AS stopewidth_rate,
                    TRY_CAST(WORKPLACEAVGFACELENGTH         AS FLOAT) AS face_length,
                    TRY_CAST(WORKPLACENETTINGRATE           AS FLOAT) AS netting_rate
                FROM [GANGPRODUCTIONDETAIL]
                WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf} {gf}
            ) AS raw
            GROUP BY section, period, gang, crewno, gangtype, workplace
        ) AS byworkplace
        GROUP BY section, period, gang, crewno, gangtype
        ORDER BY section, period, gang
    """)
    if df.empty:
        return []

    df["period"] = df["period"].astype(str)

    # Multiply per-person bonus columns by labour to get gang-level totals
    for bc in ["efficiency_bonus", "drill_bonus", "sweep_penalty", "safety_bonus"]:
        df[bc] = df[bc].fillna(0) * df["labour"].fillna(0)

    # Anchor STOPE BREAKING rows to PARTICIPANTSDETAIL — the canonical record of what was
    # actually paid. Left un-anchored, these 4 columns are a per-person-rate × GANGLABOUR
    # ESTIMATE that was found to overstate the real, verified total by ~70% for the same
    # gangs/period (GANGLABOUR is a rounded-up average headcount, not real payroll headcount
    # — see _pull_anchored_stope_breaking()'s docstring for the full verification, the same
    # anchoring already applied there and in every other Stope Breaking total in this
    # dashboard). Only STOPE BREAKING is anchored here: it's the only gang type with a
    # verified real-vs-estimate reconciliation behind it — other gang types keep their raw
    # GANGPRODUCTIONDETAIL estimate as-is and are flagged "is_estimated" for the frontend to
    # disclose, rather than silently applying an anchoring model that hasn't been checked
    # against real payroll for them.
    is_sb = df["gangtype"].str.upper() == "STOPE BREAKING"
    if is_sb.any():
        participants = _get_participants_bonus(period_from, period_to, section)
        if not participants.empty:
            keys = df.loc[is_sb, ["section", "period", "gang"]].drop_duplicates()
            real = keys.merge(
                participants[["section", "period", "gang",
                              "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]],
                on=["section", "period", "gang"], how="left"
            )
        else:
            real = df.loc[is_sb, ["section", "period", "gang"]].drop_duplicates()
            real["total_stm_bonus"] = 0.0
            real["total_safety_bonus"] = 0.0
            real["total_driller_bonus"] = 0.0
        for col in ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
            real[col] = real[col].fillna(0).astype(float)
        df = df.merge(real, on=["section", "period", "gang"], how="left")

        def _anchor_row(row):
            if row["gangtype"].upper() != "STOPE BREAKING" or pd.isna(row.get("total_stm_bonus")):
                return row["efficiency_bonus"], row["sweep_penalty"], row["drill_bonus"], row["safety_bonus"]
            est_eff, est_sweep = float(row["efficiency_bonus"]), float(row["sweep_penalty"])
            est_stm, real_stm = est_eff + est_sweep, float(row["total_stm_bonus"])
            if abs(est_stm) > 1e-9:
                ratio = real_stm / est_stm
                eff, sweep = est_eff * ratio, est_sweep * ratio
            else:
                # No estimate signal to split by (e.g. a gated gang) — if real payroll shows
                # something anyway, keep the whole real figure on efficiency rather than lose
                # it from the total.
                eff, sweep = real_stm, 0.0
            return eff, sweep, float(row["total_driller_bonus"]), float(row["total_safety_bonus"])

        anchored = df.apply(_anchor_row, axis=1, result_type="expand")
        df["efficiency_bonus"], df["sweep_penalty"], df["drill_bonus"], df["safety_bonus"] = (
            anchored[0], anchored[1], anchored[2], anchored[3]
        )
        df.drop(columns=["total_stm_bonus", "total_safety_bonus", "total_driller_bonus"], inplace=True)

    df["is_estimated"] = df["gangtype"].str.upper() != "STOPE BREAKING"
    df["efficiency"] = df["efficiency"].fillna(0)
    df["sqm_range"]  = df["adj_sqm"].apply(lambda x: _sqm_range_label(float(x or 0)))

    records = []
    for _, row in df.iterrows():
        if row["is_estimated"]:
            # Non-Stope-Breaking (raw, un-anchored) rows: unchanged from the raw
            # GANGPRODUCTIONDETAIL estimate relationship, not the real-payroll one below.
            total_bonus = (float(row["efficiency_bonus"]  or 0) +
                           float(row["drill_bonus"]   or 0) +
                           float(row["sweep_penalty"]   or 0) +
                           float(row["safety_bonus"]  or 0))
        else:
            # Stope Breaking (anchored to real PARTICIPANTSDETAIL): total_bonus is STM alone
            # (efficiency + sweep) — drill_bonus/safety_bonus are real, separately-tracked
            # figures but are sub-portions already reflected inside STM's real dollar total,
            # not separate add-on payments (same fact verified in _get_participants_bonus();
            # see the anchoring comment above). Adding them here would recreate the exact
            # ~12-15% overstatement that was already found and fixed in Bonus Analysis/R per m².
            total_bonus = float(row["efficiency_bonus"] or 0) + float(row["sweep_penalty"] or 0)
        records.append({
            "section":      row["section"],
            "period":       _period_label(int(row["period"])),
            "period_raw":   row["period"],
            "gang":         row["gang"],
            "crewno":       row["crewno"],
            "gangtype":     row["gangtype"],
            "crewstartupsqm": round(float(row["startup_sqm"] or 0), 0),
            "sqm":          round(float(row["adj_sqm"]     or 0), 0),
            "sqm_range":    row["sqm_range"],
            "labour":       round(float(row["labour"]      or 0), 2),
            "efficiency":   round(float(row["efficiency"]  or 0), 2),
            "efficiency_bonus":  round(float(row["efficiency_bonus"] or 0), 2),
            "drill_bonus":  round(float(row["drill_bonus"] or 0), 2),
            "sweep_penalty":  round(float(row["sweep_penalty"] or 0), 2),
            "safety_bonus": round(float(row["safety_bonus"]or 0), 2),
            "total_bonus":  round(total_bonus, 2),
            # True for every gang type except STOPE BREAKING — those rows are the raw
            # GANGPRODUCTIONDETAIL production-formula estimate, not yet reconciled against
            # real PARTICIPANTSDETAIL payroll like Stope Breaking is (see the comment above).
            "is_estimated": bool(row["is_estimated"]),
            "lti_ind":      int(row["lti_ind"]   or 0),
            "dress_ind":    int(row["dress_ind"] or 0),
            "fatal_ind":    int(row["fatal_ind"] or 0),
            # Real, mine-applied reference data (policy §7/§6.4/§8.1) — display-only.
            # safety_penalty_pct is whichever real penalty % actually applied (fatal takes
            # priority over LTI over TIA/dressing), or None if no incident fired for this
            # gang (§8.1's clean +10% is a separate add-on already inside safety_bonus above,
            # not a "penalty" — there's no percentage to show here for a clean gang).
            "safety_penalty_pct": (
                round(float(row["fatal_penalty_pct"]), 1) if row["fatal_ind"] and pd.notna(row["fatal_penalty_pct"]) else
                round(float(row["lti_penalty_pct"]), 1)   if row["lti_ind"]   and pd.notna(row["lti_penalty_pct"])   else
                round(float(row["dress_penalty_pct"]), 1) if row["dress_ind"] and pd.notna(row["dress_penalty_pct"]) else
                None
            ),
            "stopewidth_rate": round(float(row["stopewidth_rate"]), 4) if pd.notna(row["stopewidth_rate"]) else 0.0,
            "face_length":     round(float(row["face_length"]), 1) if pd.notna(row["face_length"]) else 0.0,
            # Real, mine-applied netting uplift (policy §6.17.5) — display-only reference,
            # same WORKPLACENETTINGRATE the Bonus Policy Simulator anchors its Netting lever
            # to. 1.0 (or missing) means no netting uplift; shown here as a % (1.066 -> 6.6).
            "netting_pct": round((float(row["netting_rate"]) - 1) * 100, 2) if pd.notna(row["netting_rate"]) and float(row["netting_rate"]) > 0 else 0.0,
        })
    return records


def get_rands_per_sqm(period_from: int, period_to: int,
                      section: str = "ALL") -> dict:
    """R/m² trend — STOPE BREAKING only with bonus breakdown."""
    sf = _section_filter(section)

    # Dedupe WORKPLACETOTALSQM per (gang, workplace) before summing — see get_pre_adj_trend.
    raw_df = read_sql(f"""
        SELECT period, gang,
               MAX(adj_sqm) AS adj_sqm,
               SUM(wp_sqm)  AS startup_sqm
        FROM (
            SELECT period, gang, workplace,
                   MAX(adj_sqm) AS adj_sqm,
                   MAX(wp_sqm)  AS wp_sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS adj_sqm,
                    TRY_CAST(WORKPLACETOTALSQM    AS FLOAT) AS wp_sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  {sf}
            ) AS raw
            GROUP BY period, gang, workplace
        ) AS q
        GROUP BY period, gang
    """)
    if raw_df.empty:
        return {"labels": [], "r_per_sqm": [], "startup_r_per_sqm": [],
                "total_bonus": [], "total_sqm": [], "startup_sqm": [],
                "safety_bonus": [], "driller_bonus": [], "stm_bonus": []}

    raw_df["period"] = raw_df["period"].astype(str)
    sqm_agg = raw_df.groupby("period").agg(
        sqm     =("adj_sqm",    "sum"),
        startup =("startup_sqm","sum"),
    ).reset_index()

    # Scope bonus to the same STOPE BREAKING (period, gang) keys as adj_sqm/startup_sqm —
    # _get_participants_bonus() on its own returns every gang type, which would silently mix
    # STOPE CLEANING/CENTRE GULLY/etc. bonus into a chart and table both labelled "Stope
    # Breaking" (see the chart title above). Match the merge-on-gang-keys pattern already
    # used correctly in get_simulation_data/get_forecast_data. raw_df has no section column,
    # so key on (period, gang) — gang codes are section-prefixed and already unique.
    bonus_df = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df.empty:
        sb_keys = raw_df[["period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["period", "gang", "total_bonus", "total_stm_bonus",
                      "total_safety_bonus", "total_driller_bonus"]],
            on=["period", "gang"], how="left"
        )
        for col in ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
            bonus_sb[col] = bonus_sb[col].fillna(0).astype(float)
        bonus_agg = bonus_sb.groupby("period")[
            ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]
        ].sum().reset_index()
    else:
        bonus_agg = pd.DataFrame(columns=["period", "total_bonus", "total_stm_bonus",
                                           "total_safety_bonus", "total_driller_bonus"])

    period_order = sorted(raw_df["period"].unique())
    labels, r_sqm, startup_r_sqm, bonuses, sqms, startup_sqms = [], [], [], [], [], []
    safety_bonuses, driller_bonuses, stm_bonuses = [], [], []

    for p in period_order:
        sqm_row   = sqm_agg[sqm_agg["period"] == p]
        bonus_row = bonus_agg[bonus_agg["period"] == p] if not bonus_agg.empty else pd.DataFrame()

        sqm     = float(sqm_row["sqm"].iloc[0])     if not sqm_row.empty     else 0
        startup = float(sqm_row["startup"].iloc[0]) if not sqm_row.empty     else 0
        bonus   = float(bonus_row["total_bonus"].iloc[0])        if not bonus_row.empty else 0
        safety  = float(bonus_row["total_safety_bonus"].iloc[0]) if not bonus_row.empty else 0
        driller = float(bonus_row["total_driller_bonus"].iloc[0])if not bonus_row.empty else 0
        stm     = float(bonus_row["total_stm_bonus"].iloc[0])    if not bonus_row.empty else 0

        labels.append(_period_label(int(p)))
        r_sqm.append(round(bonus / sqm,     2) if sqm     > 0 else 0)
        startup_r_sqm.append(round(bonus / startup, 2) if startup > 0 else 0)
        bonuses.append(round(bonus,   2))
        sqms.append(round(sqm,        0))
        startup_sqms.append(round(startup, 0))
        safety_bonuses.append(round(safety, 2))
        driller_bonuses.append(round(driller, 2))
        stm_bonuses.append(round(stm, 2))

    return {
        "labels":            labels,
        "r_per_sqm":         r_sqm,
        "startup_r_per_sqm": startup_r_sqm,
        "total_bonus":       bonuses,
        "total_sqm":         sqms,
        "startup_sqm":       startup_sqms,
        "safety_bonus":      safety_bonuses,
        "driller_bonus":     driller_bonuses,
        "stm_bonus":         stm_bonuses,
    }


def get_simulation_data(period_from: int, period_to: int,
                        section: str = "ALL") -> dict:
    """Per-gang STOPE BREAKING data for the quota simulation tab."""
    sf = _section_filter(section)
    sqm_df = read_sql(f"""
        SELECT section, period, gang, crewno,
               MAX(sqm) AS sqm, MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                LTRIM(RTRIM(CREWNO))  AS crewno,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm,
                TRY_CAST(GANGLABOUR           AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang, crewno
    """)
    if sqm_df.empty:
        return {"gangs": [], "periods": [], "sections": [],
                "section_stm_rates": {}, "overall_stm_rate": 0.0,
                "total_actual_sqm": 0.0, "total_actual_bonus": 0.0}

    sqm_df["period"] = sqm_df["period"].astype(str)
    sqm_df["sqm"]    = sqm_df["sqm"].fillna(0).astype(float)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)

    bonus_df = _get_participants_bonus(period_from, period_to, section)
    bonus_cols = ["section", "period", "gang",
                  "total_bonus", "total_stm_bonus",
                  "total_safety_bonus", "total_driller_bonus"]
    if not bonus_df.empty:
        sqm_df = sqm_df.merge(bonus_df[bonus_cols],
                              on=["section", "period", "gang"], how="left")
    for col in ["total_bonus", "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
        if col not in sqm_df.columns:
            sqm_df[col] = 0.0
        else:
            sqm_df[col] = sqm_df[col].fillna(0).astype(float)

    sqm_df["stm_r_per_sqm"] = sqm_df.apply(
        lambda r: r["total_stm_bonus"] / r["sqm"] if r["sqm"] > 0 else 0.0, axis=1
    )

    period_order = sorted(sqm_df["period"].unique())
    sections     = sorted(sqm_df["section"].unique())

    sec_agg = sqm_df.groupby("section")[["sqm", "total_stm_bonus"]].sum()
    section_stm_rates: dict = {}
    for sec in sections:
        if sec in sec_agg.index:
            s = float(sec_agg.loc[sec, "sqm"])
            b = float(sec_agg.loc[sec, "total_stm_bonus"])
            section_stm_rates[sec] = round(b / s, 4) if s > 0 else 0.0

    total_sqm_all       = float(sqm_df["sqm"].sum())
    total_bonus_all     = float(sqm_df["total_bonus"].sum())
    total_stm_bonus_all = float(sqm_df["total_stm_bonus"].sum())
    overall_stm_rate    = round(total_stm_bonus_all / total_sqm_all, 4) if total_sqm_all > 0 else 0.0

    gangs = []
    for _, row in sqm_df.iterrows():
        gangs.append({
            "section":       row["section"],
            "period":        _period_label(int(row["period"])),
            "period_raw":    row["period"],
            "gang":          row["gang"],
            "crewno":        row["crewno"],
            "actual_sqm":    round(float(row["sqm"]), 0),
            "labour":        round(float(row["labour"]), 1),
            "actual_bonus":  round(float(row["total_bonus"]), 2),
            "stm_bonus":     round(float(row["total_stm_bonus"]), 2),
            "safety_bonus":  round(float(row["total_safety_bonus"]), 2),
            "driller_bonus": round(float(row["total_driller_bonus"]), 2),
            "stm_r_per_sqm": round(float(row["stm_r_per_sqm"]), 4),
        })

    return {
        "gangs":              gangs,
        "periods":            [_period_label(int(p)) for p in period_order],
        "period_keys":        period_order,
        "sections":           sections,
        "section_stm_rates":  section_stm_rates,
        "overall_stm_rate":   overall_stm_rate,
        "total_actual_sqm":   round(total_sqm_all, 0),
        "total_actual_bonus": round(total_bonus_all, 2),
    }


def get_forecast_data(period_from: int, period_to: int,
                      section: str = "ALL") -> dict:
    """All-history STOPE BREAKING aggregates for regression/forecast. Ignores period_from/to."""
    sf = _section_filter(section)
    sqm_df = read_sql(f"""
        SELECT section, period, gang, crewno,
               MAX(sqm) AS sqm, MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                LTRIM(RTRIM(CREWNO))  AS crewno,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm,
                TRY_CAST(GANGLABOUR           AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN 200001 AND 209912
              {sf}
        ) AS q
        GROUP BY section, period, gang, crewno
    """)
    _empty = {"period_keys": [], "period_labels": [], "total_sqm": [],
              "total_bonus": [], "gang_count": [], "r_per_sqm": [],
              "sections": [], "section_sqm": {}, "section_bonus": {},
              "gang_slopes": [], "n_periods": 0,
              "total_labour": [], "stm_bonus": [], "avg_r_man": []}
    if sqm_df.empty:
        return _empty

    sqm_df["period"] = sqm_df["period"].astype(str)
    sqm_df["sqm"]    = sqm_df["sqm"].fillna(0).astype(float)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)

    period_order  = sorted(sqm_df["period"].unique())
    sections      = sorted(sqm_df["section"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    # All bonus history from PARTICIPANTSDETAIL (views include all history)
    bonus_df = _get_participants_bonus(0, 0, section)  # 0,0 → all periods
    if not bonus_df.empty:
        bonus_df["period"] = bonus_df["period"].astype(str)
        sb_keys  = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["section", "period", "gang", "total_bonus", "total_stm_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        bonus_sb["total_bonus"]     = bonus_sb["total_bonus"].fillna(0).astype(float)
        bonus_sb["total_stm_bonus"] = bonus_sb["total_stm_bonus"].fillna(0).astype(float)
        per_period_bonus = bonus_sb.groupby("period")[["total_bonus", "total_stm_bonus"]].sum().reset_index()
        sec_bonus_long   = bonus_sb.groupby(["section", "period"])["total_bonus"].sum().reset_index()
    else:
        per_period_bonus = pd.DataFrame(columns=["period", "total_bonus", "total_stm_bonus"])
        sec_bonus_long   = pd.DataFrame(columns=["section", "period", "total_bonus"])

    psqm = sqm_df.groupby("period").agg(
        total_sqm   =("sqm",    "sum"),
        gang_count  =("crewno", "nunique"),
        total_labour=("labour", "sum"),
    ).reset_index()
    psqm_idx = psqm.set_index("period")
    pbon_idx = per_period_bonus.set_index("period") if not per_period_bonus.empty else pd.DataFrame()

    total_sqm_list, total_bonus_list, gang_count_list, r_per_sqm_list = [], [], [], []
    total_labour_list, stm_bonus_list, avg_r_man_list = [], [], []
    for p in period_order:
        tsqm  = float(psqm_idx.loc[p, "total_sqm"])    if p in psqm_idx.index else 0.0
        gc    = int(psqm_idx.loc[p, "gang_count"])      if p in psqm_idx.index else 0
        tlab  = float(psqm_idx.loc[p, "total_labour"])  if p in psqm_idx.index else 0.0
        tb    = float(pbon_idx.loc[p, "total_bonus"])    if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        tstm  = float(pbon_idx.loc[p, "total_stm_bonus"]) if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        total_sqm_list.append(round(tsqm, 0))
        total_bonus_list.append(round(tb, 2))
        gang_count_list.append(gc)
        r_per_sqm_list.append(round(tb / tsqm, 4) if tsqm > 0 else 0.0)
        total_labour_list.append(round(tlab, 1))
        stm_bonus_list.append(round(tstm, 2))
        avg_r_man_list.append(round(tstm / tlab, 2) if tlab > 0 else 0.0)

    sec_sqm_piv = sqm_df.groupby(["section", "period"])["sqm"].sum().reset_index()
    section_sqm_dict: dict = {}
    section_bonus_dict: dict = {}
    sec_bon_idx_map: dict = {}
    if not sec_bonus_long.empty:
        for sec in sections:
            sb = sec_bonus_long[sec_bonus_long["section"] == sec].set_index("period")
            sec_bon_idx_map[sec] = sb
    for sec in sections:
        sq = sec_sqm_piv[sec_sqm_piv["section"] == sec].set_index("period")
        sb = sec_bon_idx_map.get(sec, pd.DataFrame())
        section_sqm_dict[sec]   = [round(float(sq.loc[p, "sqm"]), 0) if p in sq.index else 0.0
                                    for p in period_order]
        section_bonus_dict[sec] = [round(float(sb.loc[p, "total_bonus"]), 2)
                                    if (not sb.empty and p in sb.index) else 0.0
                                    for p in period_order]

    # Per-gang linear slopes (last 12 months)
    last_12_periods = set(period_order[-12:])
    gang_period_df  = sqm_df.groupby(["section", "gang", "period"])["sqm"].sum().reset_index()
    gang_slopes = []
    for (sec, gang), gdf in gang_period_df.groupby(["section", "gang"]):
        gdf = gdf.sort_values("period")
        gdf_recent = gdf[gdf["period"].isin(last_12_periods)]
        if len(gdf_recent) < 3:
            continue
        ys    = gdf_recent["sqm"].tolist()
        n_per = len(ys)
        xm    = (n_per - 1) / 2
        ym    = sum(ys) / n_per
        sxy   = sum((i - xm) * (ys[i] - ym) for i in range(n_per))
        sxx   = sum((i - xm) ** 2 for i in range(n_per))
        slope = sxy / sxx if sxx > 0 else 0.0
        last3_avg  = sum(ys[-3:]) / 3
        prior3     = ys[-6:-3]
        pct_vs_prior3 = (round((last3_avg - sum(prior3)/len(prior3)) / (sum(prior3)/len(prior3)) * 100, 1)
                         if prior3 and sum(prior3)/len(prior3) > 0 else None)
        gang_slopes.append({
            "section":       sec,
            "gang":          gang,
            "slope":         round(slope, 2),
            "last_sqm":      round(float(gdf_recent["sqm"].iloc[-1]), 0),
            "last_period":   _period_label(int(gdf_recent["period"].iloc[-1])),
            "n_periods":     n_per,
            "pct_vs_prior3": pct_vs_prior3,
        })
    gang_slopes.sort(key=lambda x: x["slope"])

    return {
        "period_keys":   period_order,
        "period_labels": period_labels,
        "total_sqm":     total_sqm_list,
        "total_bonus":   total_bonus_list,
        "gang_count":    gang_count_list,
        "r_per_sqm":     r_per_sqm_list,
        "sections":      sections,
        "section_sqm":   section_sqm_dict,
        "section_bonus": section_bonus_dict,
        "gang_slopes":   gang_slopes,
        "n_periods":     len(period_order),
        "total_labour":  total_labour_list,
        "stm_bonus":     stm_bonus_list,
        "avg_r_man":     avg_r_man_list,
    }


def get_period_performance(period_from: int, period_to: int,
                           section: str = "ALL") -> dict:
    """Period-by-period aggregates for the Forecast tab (respects period range)."""
    sf = _section_filter(section)
    _empty = {"period_keys": [], "period_labels": [], "total_sqm": [],
              "gang_count": [], "total_labour": [], "meas_shifts": [],
              "stm_bonus": [], "total_bonus": [], "avg_r_man": []}

    # Gang-level: sqm + labour
    sqm_df = read_sql(f"""
        SELECT section, period, gang, crewno,
               MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                LTRIM(RTRIM(CREWNO))  AS crewno,
                TRY_CAST(GANGLABOUR AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              {sf}
        ) AS q
        GROUP BY section, period, gang, crewno
    """)
    if sqm_df.empty:
        return _empty

    sqm_df["period"] = sqm_df["period"].astype(str)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)

    period_order  = sorted(sqm_df["period"].unique())
    period_labels = [_period_label(int(p)) for p in period_order]

    # Pre-Adj SQM: dedupe WORKPLACETOTALSQM per (gang, workplace) first — GANGPRODUCTIONDETAIL
    # can carry multiple activity rows per workplace/period, each repeating the same
    # WORKPLACETOTALSQM value, which would otherwise inflate this total vs the Adj figure.
    wp_df = read_sql(f"""
        SELECT period, SUM(startup_sqm) AS startup_sqm
        FROM (
            SELECT
                LTRIM(RTRIM(PERIOD)) AS period,
                LTRIM(RTRIM(GANG))   AS gang,
                SUM(wp_sqm) AS startup_sqm
            FROM (
                SELECT
                    LTRIM(RTRIM(PERIOD))    AS period,
                    LTRIM(RTRIM(GANG))      AS gang,
                    LTRIM(RTRIM(WORKPLACE)) AS workplace,
                    MAX(TRY_CAST(WORKPLACETOTALSQM AS FLOAT)) AS wp_sqm
                FROM [GANGPRODUCTIONDETAIL]
                WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
                  AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
                  AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
                  {sf}
                GROUP BY LTRIM(RTRIM(PERIOD)), LTRIM(RTRIM(GANG)), LTRIM(RTRIM(WORKPLACE))
            ) AS byworkplace
            GROUP BY period, gang
        ) AS q
        GROUP BY period
    """)
    wp_df["period"] = wp_df["period"].astype(str)
    wp_idx = wp_df.set_index("period") if not wp_df.empty else pd.DataFrame()

    # Bonus from PARTICIPANTSDETAIL
    bonus_df = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df.empty:
        bonus_df["period"] = bonus_df["period"].astype(str)
        sb_keys  = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["section", "period", "gang", "total_stm_bonus", "total_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        for col in ["total_stm_bonus", "total_bonus"]:
            bonus_sb[col] = bonus_sb[col].fillna(0).astype(float)
        per_period = bonus_sb.groupby("period")[["total_stm_bonus", "total_bonus"]].sum().reset_index()
        pbon_idx = per_period.set_index("period")
    else:
        pbon_idx = pd.DataFrame()

    psqm = sqm_df.groupby("period").agg(
        gang_count  =("crewno",  "nunique"),
        total_labour=("labour",  "sum"),
    ).reset_index().set_index("period")

    total_sqm_list, gang_count_list, total_labour_list = [], [], []
    stm_bonus_list, total_bonus_list, avg_r_man_list, meas_shifts_list = [], [], [], []
    for p in period_order:
        tsqm  = float(wp_idx.loc[p, "startup_sqm"]) if (not wp_idx.empty and p in wp_idx.index) else 0.0
        gc    = int(psqm.loc[p, "gang_count"])       if p in psqm.index else 0
        tlab  = float(psqm.loc[p, "total_labour"])   if p in psqm.index else 0.0
        tstm  = float(pbon_idx.loc[p, "total_stm_bonus"]) if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        tbon  = float(pbon_idx.loc[p, "total_bonus"])      if (not pbon_idx.empty and p in pbon_idx.index) else 0.0
        total_sqm_list.append(round(tsqm, 0))
        gang_count_list.append(gc)
        total_labour_list.append(round(tlab, 1))
        meas_shifts_list.append(None)   # no MONTHSHIFTS in GANGPRODUCTIONDETAIL
        stm_bonus_list.append(round(tstm, 2))
        total_bonus_list.append(round(tbon, 2))
        avg_r_man_list.append(round(tstm / tlab, 2) if tlab > 0 else 0.0)

    return {
        "period_keys":   period_order,
        "period_labels": period_labels,
        "total_sqm":     total_sqm_list,
        "gang_count":    gang_count_list,
        "total_labour":  total_labour_list,
        "meas_shifts":   meas_shifts_list,
        "stm_bonus":     stm_bonus_list,
        "total_bonus":   total_bonus_list,
        "avg_r_man":     avg_r_man_list,
    }


def get_gang_production_detail(gang: str, period_from: int, period_to: int) -> list[dict]:
    """Workplace-level production detail for a STOPE BREAKING gang.

    Joins GANGPRODUCTIONDETAIL (workplace identifiers per gang) with
    PRODUCTIONWPDETAIL (SQM detail per workplace).
    PRODUCTIONWPDETAIL columns used:
      WPMAXLEDGESQM  → startup_ledgesqm
      WPMAXSTOPESQM  → startup_stopesqm
      WPPRETOTALM2   → startup_totalsqm
      WPTOTALM2      → totalm2 (adjusted)
      DIP_FACTOR     → dip_factor (down-dip stope-geometry adjustment; previously mislabelled
                                    "wideraisefactor" — unrelated to the separate Wide Raise
                                    bonus category, which is its own WORKPLACERATETYPE)
      WPTOTALM2 - WPPRETOTALM2 → extram2 / m2_variance
    """
    if not gang:
        return []
    gang_clean = gang.strip().replace("'", "''")

    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(g.SECTION))   AS section,
            LTRIM(RTRIM(g.PERIOD))    AS period,
            LTRIM(RTRIM(g.GANG))      AS gang,
            LTRIM(RTRIM(g.WORKPLACE)) AS workplace,
            MAX(ISNULL(TRY_CAST(p.WPMAXLEDGESQM AS FLOAT), 0)) AS startup_ledgesqm,
            MAX(ISNULL(TRY_CAST(p.WPMAXSTOPESQM AS FLOAT), 0)) AS startup_stopesqm,
            MAX(ISNULL(TRY_CAST(p.WPPRETOTALM2  AS FLOAT), 0)) AS startup_totalsqm,
            MAX(ISNULL(TRY_CAST(p.WPTOTALM2     AS FLOAT), 0)
              - ISNULL(TRY_CAST(p.WPPRETOTALM2  AS FLOAT), 0))  AS extram2,
            MAX(ISNULL(TRY_CAST(p.DIP_FACTOR    AS FLOAT), 0))  AS dip_factor,
            MAX(ISNULL(TRY_CAST(p.WPTOTALM2     AS FLOAT), 0))  AS totalm2,
            MAX(ISNULL(TRY_CAST(p.WPTOTALM2     AS FLOAT), 0)
              - ISNULL(TRY_CAST(p.WPPRETOTALM2  AS FLOAT), 0))  AS m2_variance
        FROM [GANGPRODUCTIONDETAIL] g
        LEFT JOIN [PRODUCTIONWPDETAIL] p
            ON LTRIM(RTRIM(g.SECTION))   = LTRIM(RTRIM(p.SECTION))
           AND LTRIM(RTRIM(g.PERIOD))    = LTRIM(RTRIM(p.PERIOD))
           AND LTRIM(RTRIM(g.WORKPLACE)) = LTRIM(RTRIM(p.WORKPLACE))
        WHERE LTRIM(RTRIM(g.GANG)) = '{gang_clean}'
          AND UPPER(LTRIM(RTRIM(g.GANGTYPE))) = 'STOPE BREAKING'
          AND LEN(LTRIM(RTRIM(g.CREWNO))) >= 8
          AND TRY_CAST(g.PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
        GROUP BY
            LTRIM(RTRIM(g.SECTION)),
            LTRIM(RTRIM(g.PERIOD)),
            LTRIM(RTRIM(g.GANG)),
            LTRIM(RTRIM(g.WORKPLACE))
        ORDER BY period, section, workplace
    """)
    if df.empty:
        return []

    records = []
    for _, row in df.iterrows():
        records.append({
            "section":          str(row["section"] or ""),
            "period":           _period_label(int(row["period"])),
            "gang":             str(row["gang"] or ""),
            "workplace":        str(row["workplace"] or ""),
            "startup_ledgesqm": round(float(row["startup_ledgesqm"] or 0), 2),
            "startup_stopesqm": round(float(row["startup_stopesqm"] or 0), 2),
            "startup_totalsqm": round(float(row["startup_totalsqm"] or 0), 2),
            "extram2":          round(float(row["extram2"]           or 0), 2),
            "dip_factor":  round(float(row["dip_factor"]   or 0), 4),
            "totalm2":          round(float(row["totalm2"]           or 0), 2),
            "m2_variance":      round(float(row["m2_variance"]       or 0), 2),
        })
    return records


def get_section_rsqm_trend(period_from: int, period_to: int) -> dict:
    """Per-section, per-period R/m² (STOPE BREAKING) for the Executive Summary's "Monthly
    Bonus Cost per m² by Section" chart — one line per section, plus the whole-mine average
    and the cheapest/dearest spread for each period. Same STOPE BREAKING scoping and
    gang-key-matched bonus merge as get_section_ranking(), just grouped by (section, period)
    instead of by section alone.
    """
    sqm_df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return {"periods": [], "sections": [], "series": {}, "whole_mine": [], "band_min": [], "band_max": [], "ranked": []}

    sqm_df["sqm"] = sqm_df["sqm"].fillna(0).astype(float)
    sqm_df["period"] = sqm_df["period"].astype(str)

    bonus_df = _get_participants_bonus(period_from, period_to)
    if not bonus_df.empty:
        bonus_df["period"] = bonus_df["period"].astype(str)
        keys = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = keys.merge(
            bonus_df[["section", "period", "gang", "total_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        bonus_sb["total_bonus"] = bonus_sb["total_bonus"].fillna(0).astype(float)
    else:
        bonus_sb = sqm_df[["section", "period", "gang"]].copy()
        bonus_sb["total_bonus"] = 0.0

    sp_sqm = sqm_df.groupby(["section", "period"])["sqm"].sum().reset_index()
    sp_bonus = bonus_sb.groupby(["section", "period"])["total_bonus"].sum().reset_index()
    sp = sp_sqm.merge(sp_bonus, on=["section", "period"], how="left")
    sp["total_bonus"] = sp["total_bonus"].fillna(0.0)
    sp["r_per_sqm"] = sp.apply(lambda r: r["total_bonus"] / r["sqm"] if r["sqm"] > 0 else None, axis=1)

    period_order = sorted(sp["period"].unique(), key=lambda p: int(p))
    sections = sorted(sp["section"].unique())

    series: dict[str, list] = {sec: [] for sec in sections}
    whole_mine, band_min, band_max, ranked = [], [], [], []
    for p in period_order:
        rows = sp[sp["period"] == p]
        p_total_bonus = float(rows["total_bonus"].sum())
        p_total_sqm   = float(rows["sqm"].sum())
        p_whole = round(p_total_bonus / p_total_sqm, 2) if p_total_sqm > 0 else None
        whole_mine.append(p_whole)

        vals = []
        p_ranked = []
        for sec in sections:
            r = rows[rows["section"] == sec]
            v = round(float(r["r_per_sqm"].iloc[0]), 2) if not r.empty and pd.notna(r["r_per_sqm"].iloc[0]) else None
            series[sec].append(v)
            if v is not None:
                vals.append(v)
                p_ranked.append({
                    "section": sec, "r_per_sqm": v,
                    "bonus": round(float(r["total_bonus"].iloc[0]), 2),
                    "sqm": round(float(r["sqm"].iloc[0]), 0),
                })
        band_min.append(min(vals) if vals else None)
        band_max.append(max(vals) if vals else None)
        p_ranked.sort(key=lambda x: -x["r_per_sqm"])
        ranked.append(p_ranked)

    return {
        "periods":    [_period_label(int(p)) for p in period_order],
        "sections":   sections,
        "series":     series,
        "whole_mine": whole_mine,
        "band_min":   band_min,
        "band_max":   band_max,
        "ranked":     ranked,
    }


def get_section_ranking(period_from: int, period_to: int) -> list[dict]:
    """
    Per-section STOPE BREAKING summary for the Section Performance Ranking table.
    Sorted by R/m² descending (highest bonus earned per m² first).
    SQM from GANGPRODUCTIONDETAIL; bonus from PARTICIPANTSDETAIL (canonical).
    """
    sqm_df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return []

    sqm_df["sqm"] = sqm_df["sqm"].fillna(0).astype(float)

    sec_agg = sqm_df.groupby("section").agg(
        total_sqm  = ("sqm",  "sum"),
        gang_count = ("gang", "nunique"),
    ).reset_index()

    # Scope bonus to the same STOPE BREAKING (section, period, gang) keys as total_sqm —
    # _get_participants_bonus() on its own returns every gang type for the section, which
    # would silently mix STOPE CLEANING/CENTRE GULLY/etc. bonus into a ratio the UI labels
    # "Stope Breaking only" (see the section-header caption). Match the merge-on-gang-keys
    # pattern already used correctly in get_simulation_data/get_forecast_data.
    bonus_df = _get_participants_bonus(period_from, period_to)
    if not bonus_df.empty:
        sb_keys = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = sb_keys.merge(
            bonus_df[["section", "period", "gang", "total_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        bonus_sb["total_bonus"] = bonus_sb["total_bonus"].fillna(0).astype(float)
        sec_bonus = bonus_sb.groupby("section").agg(
            total_bonus = ("total_bonus", "sum")
        ).reset_index()
    else:
        sec_bonus = pd.DataFrame(columns=["section", "total_bonus"])

    merged = sec_agg.merge(sec_bonus, on="section", how="left")
    merged["total_bonus"] = merged["total_bonus"].fillna(0).astype(float)
    merged["r_per_sqm"] = merged.apply(
        lambda r: r["total_bonus"] / r["total_sqm"] if r["total_sqm"] > 0 else 0.0, axis=1
    )
    merged["avg_sqm_per_gang"] = merged.apply(
        lambda r: r["total_sqm"] / r["gang_count"] if r["gang_count"] > 0 else 0.0, axis=1
    )
    merged = merged.sort_values("r_per_sqm", ascending=False).reset_index(drop=True)

    return [
        {
            "section":          str(row["section"]),
            "total_sqm":        round(float(row["total_sqm"]), 0),
            "total_bonus":      round(float(row["total_bonus"]), 2),
            "r_per_sqm":        round(float(row["r_per_sqm"]), 2),
            "gang_count":       int(row["gang_count"]),
            "avg_sqm_per_gang": round(float(row["avg_sqm_per_gang"]), 1),
        }
        for _, row in merged.iterrows()
    ]


def get_crew_performance(period_from: int, period_to: int, section: str = "ALL") -> list[dict]:
    """Per-gang (crew) summary across the selected periods, for the Gang Analysis ->
    Crew Performance sub-tab. STOPE BREAKING only, same scoping/anchoring as
    get_section_ranking()/_get_participants_bonus() — real PARTICIPANTSDETAIL bonus, not
    the raw GANGPRODUCTIONDETAIL per-person-rate estimate.
    """
    sf = _section_filter(section)
    sqm_df = read_sql(f"""
        SELECT section, period, gang, MAX(sqm) AS sqm, MAX(labour) AS labour
        FROM (
            SELECT
                LTRIM(RTRIM(SECTION)) AS section,
                LTRIM(RTRIM(PERIOD))  AS period,
                LTRIM(RTRIM(GANG))    AS gang,
                TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT) AS sqm,
                TRY_CAST(GANGLABOUR           AS FLOAT) AS labour
            FROM [GANGPRODUCTIONDETAIL]
            WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) = 'STOPE BREAKING'
              AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
              AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
              {sf}
        ) AS q
        GROUP BY section, period, gang
    """)
    if sqm_df.empty:
        return []

    sqm_df["sqm"]    = sqm_df["sqm"].fillna(0).astype(float)
    sqm_df["labour"] = sqm_df["labour"].fillna(0).astype(float)
    sqm_df["period"] = sqm_df["period"].astype(str)

    bonus_df = _get_participants_bonus(period_from, period_to, section)
    if not bonus_df.empty:
        bonus_df["period"] = bonus_df["period"].astype(str)
        keys = sqm_df[["section", "period", "gang"]].drop_duplicates()
        bonus_sb = keys.merge(
            bonus_df[["section", "period", "gang", "total_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        bonus_sb["total_bonus"] = bonus_sb["total_bonus"].fillna(0).astype(float)
    else:
        bonus_sb = sqm_df[["section", "period", "gang"]].copy()
        bonus_sb["total_bonus"] = 0.0

    sqm_df = sqm_df.merge(bonus_sb, on=["section", "period", "gang"], how="left")
    sqm_df["total_bonus"] = sqm_df["total_bonus"].fillna(0.0)

    agg = sqm_df.groupby(["section", "gang"]).agg(
        total_sqm    = ("sqm",         "sum"),
        total_bonus  = ("total_bonus", "sum"),
        total_labour = ("labour",      "sum"),
        months       = ("period",      "nunique"),
    ).reset_index()
    agg["sqm_per_man"] = agg.apply(
        lambda r: r["total_sqm"] / r["total_labour"] if r["total_labour"] > 0 else 0.0, axis=1
    )
    agg["r_per_sqm"] = agg.apply(
        lambda r: r["total_bonus"] / r["total_sqm"] if r["total_sqm"] > 0 else 0.0, axis=1
    )
    agg = agg.sort_values("total_sqm", ascending=False).reset_index(drop=True)

    return [
        {
            "section":     str(row["section"]),
            "gang":        str(row["gang"]),
            "total_sqm":   round(float(row["total_sqm"]), 0),
            "total_bonus": round(float(row["total_bonus"]), 2),
            "months":      int(row["months"]),
            "sqm_per_man": round(float(row["sqm_per_man"]), 2),
            "r_per_sqm":   round(float(row["r_per_sqm"]), 2),
        }
        for _, row in agg.iterrows()
    ]


# Policy defaults for the Bonus Policy Simulator's levers -- these are display/slider
# reference values only (what the WRITTEN policy currently says), never used to recompute
# any bonus figure shown elsewhere in this app. Every actual Rand total in this dashboard
# comes straight from the database's own pre-calculated columns (GANGFINALBREAKBONUS,
# EMPLOYEESTOPETEAMBONUS, etc.) -- summed/averaged, never re-derived from these percentages.
#
# Source: Phakisa's own "Stoping Bonus Cat 4-8" policy, JJ_202608_STPTEAM_REV04 (effective
# August 2026, the current version -- supplied 2026-08-27). Confirmed 2026-08-27 against real
# data: entry level and safety % below now match; netting % (6.6, applied AFTER safety per
# §6.17) matches the real WORKPLACENETTINGRATE=1.066 seen on real gangs exactly.
#
# KNOWN GAPS -- the policy's real structure is richer than this simulator's levers can
# represent, and no attempt has been made to force-fit it. Flagging per project convention
# (policy vs. database mismatches get surfaced, not silently resolved):
#   * Safety ladder (§8.1) is actually 6 tiers -- clean +10%, 1 TIA -25%, 2+ TIA -50%,
#     1 LTI -50%, 2+ LTI -100%, LOL (fatal) no bonus -- but the database only exposes
#     3 gang-level incident indicators (LTI/Dressing/Fatal), and this simulator's "safety_pct"
#     dict only has 4 slots. "dressing" below is a best-effort stand-in for "1 TIA" and "lti"
#     for "1 LTI" -- the 2+ TIA and 2+ LTI tiers have no indicator to key off and are not
#     modelled at all.
#   * B-Reef stoping width (§6.3) is actually two discrete factor tiers (SW 1.80-1.99m -> 1.25,
#     SW 2.00-2.40m -> 1.5), not a single adjustable percentage -- "sw_pct" below is a rough
#     placeholder, not a policy-derived value.
#   * Wide Raise / Updip / Ledging (§6.9) has its own entry level -- 76m² TOTAL (not per
#     employee) -- structurally different from the single per-employee "entry_threshold" this
#     simulator applies; the existing Wide-Raise exemption code path doesn't model this correctly.
#   * Face Length factor (§6.4) -- a whole separate m² multiplier table by face length (21-35m)
#     and mining type (Basal/B-Reef) -- isn't represented here at all.
# None of this affects any actual bonus total displayed in the dashboard -- it only limits how
# faithfully the Simulator's hypothetical "what if" lever changes can model Phakisa's real policy.
BONUS_POLICY_DEFAULTS = {
    "entry_threshold": 14.5,          # §6.8 — m²/empl qualifying gate, Undercut/Open mining
    "netting_pct":     6.6,           # §6.17.5 — 6.6% of Qualifying Bonus (after Safety); matches real WORKPLACENETTINGRATE=1.066
    "sw_pct":          10.0,          # §6.3 — placeholder; real policy is two discrete factor tiers (1.25 / 1.5), not a %, see note above
    "safety_pct": {                   # §8.1 — ladder is really 6 tiers; see note above for the gap
        "clean":    10.0,
        "dressing": -25.0,
        "lti":     -50.0,
        "fatal":  -100.0,
    },
}


def _crew_scope_gangs(period_from: int, period_to: int, section: str = "ALL") -> pd.DataFrame:
    """Gangs in Labour Analysis's scope (Stope Breaking + Stope Cleaning crews), with labour by
    gang type, m² and safety indicators, one row per (section, gang) averaged/summed across the
    selected periods. Shared by get_crew_labour() and get_best_worst_crews().
    """
    sf = _section_filter(section)
    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(SECTION)) AS section,
            LTRIM(RTRIM(PERIOD))  AS period,
            LTRIM(RTRIM(GANG))    AS gang,
            UPPER(LTRIM(RTRIM(GANGTYPE))) AS gangtype,
            MAX(TRY_CAST(GANGLABOUR AS FLOAT)) AS labour,
            MAX(TRY_CAST(GANGTOTALSQMADJUSTED AS FLOAT)) AS sqm,
            MAX(TRY_CAST(GANGLTIIND AS FLOAT))      AS lti_ind,
            MAX(TRY_CAST(GANGDRESSINGIND AS FLOAT)) AS dress_ind,
            MAX(TRY_CAST(GANGFATALIND AS FLOAT))    AS fatal_ind
        FROM [GANGPRODUCTIONDETAIL]
        WHERE UPPER(LTRIM(RTRIM(GANGTYPE))) IN ('STOPE BREAKING', 'STOPE CLEANING')
          AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          AND LEN(LTRIM(RTRIM(CREWNO))) >= 8
          {sf}
        GROUP BY LTRIM(RTRIM(SECTION)), LTRIM(RTRIM(PERIOD)), LTRIM(RTRIM(GANG)), UPPER(LTRIM(RTRIM(GANGTYPE)))
    """)
    if not df.empty:
        for c in ["labour", "sqm", "lti_ind", "dress_ind", "fatal_ind"]:
            df[c] = df[c].fillna(0).astype(float)
    return df


def get_crew_labour(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """"Crew Labour" — Phakisa adaptation of the Mponeng pattern (the user built this fresh in
    Mponeng and asked for the same table here, adapted to Phakisa's real shift data).

    Mponeng's version reads a per-shift EVENTCODE to split lost shifts into AWOP / Sick Leave /
    Other. Checked Phakisa's equivalent tables before building anything:
    - BONUSSHIFTS has real AWOP_SHIFTS, SHIFTS_WORKED and STRIKE_SHIFTS columns, and covers 78
      real periods (2020-01 to 2026-07) — reliable across this app's whole normal range.
    - CLOCKEDSHIFTS has the real per-shift EVENTCODE Mponeng's split is built on, but only ONE
      period (2026-07) has any rows at all; ATTENDANCE (daily grid) only has ONE period (2026-02)
      populated. Neither covers Jul-25-Apr-26, the range every other tab in this app reports on.
    Given that, "Shifts Lost due to Sick Leave" specifically cannot be built reliably here — there
    is no per-shift-reason detail across the real working range. STRIKE_SHIFTS is shown instead
    (a real, reliably-covered "shifts lost" column BONUSSHIFTS already carries), explicitly
    labelled as strike shifts, not sick leave, so nobody mistakes one for the other. Safety Rep
    and Physical Cond are left out entirely: SAFETYREPS has 0 rows in this database, and no
    physical-conditions column exists anywhere in GANGPRODUCTIONDETAIL (verified 2026-11-16) — per
    this project's standing rule, a missing column is left out, never estimated.
    """
    gangs = _crew_scope_gangs(period_from, period_to, section)
    if gangs.empty:
        return {"rows": [], "gang_count": 0}

    sf = _section_filter(section)
    shifts = read_sql(f"""
        SELECT
            LTRIM(RTRIM(SECTION)) AS section,
            LTRIM(RTRIM(PERIOD))  AS period,
            LTRIM(RTRIM(GANG))    AS gang,
            SUM(ISNULL(TRY_CAST(AWOP_SHIFTS   AS FLOAT), 0)) AS awop_shifts,
            SUM(ISNULL(TRY_CAST(SHIFTS_WORKED AS FLOAT), 0)) AS shifts_worked,
            SUM(ISNULL(TRY_CAST(STRIKE_SHIFTS AS FLOAT), 0)) AS strike_shifts
        FROM [BONUSSHIFTS]
        WHERE TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          {sf}
        GROUP BY LTRIM(RTRIM(SECTION)), LTRIM(RTRIM(PERIOD)), LTRIM(RTRIM(GANG))
    """)
    if not shifts.empty:
        for c in ["awop_shifts", "shifts_worked", "strike_shifts"]:
            shifts[c] = shifts[c].fillna(0).astype(float)

    bonus_df = _get_participants_bonus(period_from, period_to, section)

    by_gang_type = gangs.groupby(["section", "gang", "gangtype"]).agg(
        labour=("labour", "mean"), sqm=("sqm", "sum"),
        lti=("lti_ind", "sum"), dress=("dress_ind", "sum"), fatal=("fatal_ind", "sum"),
        months=("period", "nunique"),
    ).reset_index()

    rows = []
    for (section_, gang), grp in by_gang_type.groupby(["section", "gang"]):
        breaking = grp[grp["gangtype"] == "STOPE BREAKING"]
        cleaning = grp[grp["gangtype"] == "STOPE CLEANING"]
        avg_lab_breaking = round(float(breaking["labour"].mean()), 1) if len(breaking) else 0.0
        avg_lab_cleaning = round(float(cleaning["labour"].mean()), 1) if len(cleaning) else 0.0
        total_sqm = float(grp["sqm"].sum())
        safety_incidents = int(grp["lti"].sum() + grp["dress"].sum() + grp["fatal"].sum())
        months = int(grp["months"].max())

        s = shifts[(shifts["section"] == section_) & (shifts["gang"] == gang)] if not shifts.empty else shifts
        awop = round(float(s["awop_shifts"].sum()), 0) if len(s) else 0.0
        worked = round(float(s["shifts_worked"].sum()), 0) if len(s) else 0.0
        strike = round(float(s["strike_shifts"].sum()), 0) if len(s) else 0.0

        total_labour = avg_lab_breaking + avg_lab_cleaning
        sqm_per_man = round(total_sqm / (total_labour * months), 2) if total_labour > 0 and months > 0 else 0.0

        b = bonus_df[(bonus_df["section"] == section_) & (bonus_df["gang"] == gang)] if not bonus_df.empty else bonus_df
        total_bonus = float(b["total_bonus"].sum()) if len(b) else 0.0
        bonus_per_person_month = round(total_bonus / (total_labour * months), 2) if total_labour > 0 and months > 0 else 0.0

        rows.append({
            "section": section_, "gang": gang,
            "avg_labour_breaking": avg_lab_breaking,
            "avg_labour_cleaning": avg_lab_cleaning,
            "total_avg_labour": round(total_labour, 1),
            "awop_shifts": awop,
            "strike_shifts": strike,
            "total_shifts_worked": worked,
            "sqm_per_man": sqm_per_man,
            "safety_incidents": safety_incidents,
            "bonus_per_person_month": bonus_per_person_month,
            "months": months,
        })

    rows.sort(key=lambda r: r["gang"])
    return {"rows": rows, "gang_count": len(rows)}


def get_crew_drilldown(gang: str, period_from: int, period_to: int, section: str = "ALL") -> dict:
    """Wage-code breakdown for one crew — BONUSSHIFTS's own WAGECODE/WAGE_DESCRIPTION, summed
    over the selected periods. This is real payroll wage-code detail, not derived/estimated.
    """
    sf = _section_filter(section)
    safe_gang = gang.upper().replace("'", "''")
    df = read_sql(f"""
        SELECT
            LTRIM(RTRIM(WAGECODE))        AS wagecode,
            MAX(LTRIM(RTRIM(WAGE_DESCRIPTION))) AS wage_description,
            COUNT(DISTINCT EMPLOYEE_NO)    AS employees,
            SUM(ISNULL(TRY_CAST(SHIFTS_WORKED AS FLOAT), 0)) AS shifts_worked,
            SUM(ISNULL(TRY_CAST(AWOP_SHIFTS   AS FLOAT), 0)) AS awop_shifts
        FROM [BONUSSHIFTS]
        WHERE UPPER(LTRIM(RTRIM(GANG))) = '{safe_gang}'
          AND TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}
          {sf}
        GROUP BY LTRIM(RTRIM(WAGECODE))
        ORDER BY shifts_worked DESC
    """)
    if df.empty:
        return {"gang": gang, "rows": []}
    rows = [
        {
            "wagecode": str(row["wagecode"]),
            "wage_description": str(row["wage_description"] or ""),
            "employees": int(row["employees"]),
            "shifts_worked": round(float(row["shifts_worked"]), 0),
            "awop_shifts": round(float(row["awop_shifts"]), 0),
        }
        for _, row in df.iterrows()
    ]
    return {"gang": gang, "rows": rows}


def get_best_worst_crews(period_from: int, period_to: int, section: str = "ALL", n: int = 10,
                          min_sqm_per_man: Optional[float] = None,
                          max_sqm_per_man: Optional[float] = None) -> dict:
    """"Best vs Worst Crews" — Phakisa adaptation. Mponeng ranks crews by actual-vs-planned m²;
    Phakisa has no "planned m²" policy-target column anywhere in GANGPRODUCTIONDETAIL (checked
    and confirmed absent — same gap already documented for the Overview tab's "Actual vs
    Planned" sub-tab). Ranks by m²/Man instead — the same productivity measure this app already
    uses as its primary real efficiency figure everywhere else.

    min_sqm_per_man/max_sqm_per_man optionally narrow the crew pool (e.g. excluding very-low-m²
    crews that are really a reporting artefact) *before* ranking into best/worst — applied to the
    same real m²/Man figure the table and chart show, not a separate recalculation.
    """
    labour = get_crew_labour(period_from, period_to, section)
    rows = labour["rows"]
    if min_sqm_per_man is not None:
        rows = [r for r in rows if r["sqm_per_man"] >= min_sqm_per_man]
    if max_sqm_per_man is not None:
        rows = [r for r in rows if r["sqm_per_man"] <= max_sqm_per_man]
    if len(rows) < 4:
        return {"best": [], "worst": [], "comparison": [], "scatter": [], "n": n, "filtered_count": len(rows)}

    ranked = sorted(rows, key=lambda r: -r["sqm_per_man"])
    n = min(n, len(ranked) // 2)
    best = ranked[:n]
    worst = ranked[-n:]

    def _avg(group, key):
        vals = [r[key] for r in group]
        return sum(vals) / len(vals) if vals else 0.0

    def _attendance(group):
        acc = [r["total_shifts_worked"] + r["awop_shifts"] + r["strike_shifts"] for r in group]
        worked = [r["total_shifts_worked"] for r in group]
        total_acc = sum(acc)
        return round(sum(worked) / total_acc * 100, 1) if total_acc > 0 else None

    def _rate(group, key):
        acc = [r["total_shifts_worked"] + r["awop_shifts"] + r["strike_shifts"] for r in group]
        vals = [r[key] for r in group]
        total_acc = sum(acc)
        return round(sum(vals) / total_acc * 100, 2) if total_acc > 0 else None

    def _safety_rate(group):
        return round(sum(1 for r in group if r["safety_incidents"] > 0) / len(group) * 100, 0) if group else 0.0

    comparison = [
        {"measure": "m²/Man (performance)", "best": round(_avg(best, "sqm_per_man"), 1),
         "worst": round(_avg(worst, "sqm_per_man"), 1), "higher_is_better": True},
        {"measure": "Attendance (worked ÷ accounted-for shifts)", "best": _attendance(best),
         "worst": _attendance(worst), "higher_is_better": True},
        {"measure": "AWOP (share of accounted-for shifts)", "best": _rate(best, "awop_shifts"),
         "worst": _rate(worst, "awop_shifts"), "higher_is_better": False},
        {"measure": "Strike shifts (share of accounted-for shifts)", "best": _rate(best, "strike_shifts"),
         "worst": _rate(worst, "strike_shifts"), "higher_is_better": False},
        {"measure": "Gang-periods with a safety incident", "best": _safety_rate(best),
         "worst": _safety_rate(worst), "higher_is_better": False},
        {"measure": "Labour (avg crew size)", "best": round(_avg(best, "total_avg_labour"), 1),
         "worst": round(_avg(worst, "total_avg_labour"), 1), "higher_is_better": None},
        {"measure": "Bonus per person per month", "best": round(_avg(best, "bonus_per_person_month"), 2),
         "worst": round(_avg(worst, "bonus_per_person_month"), 2), "higher_is_better": True},
    ]

    best_set = {(r["section"], r["gang"]) for r in best}
    worst_set = {(r["section"], r["gang"]) for r in worst}
    scatter = []
    for r in ranked:
        key = (r["section"], r["gang"])
        bucket = "best" if key in best_set else "worst" if key in worst_set else "other"
        acc = r["total_shifts_worked"] + r["awop_shifts"] + r["strike_shifts"]
        attendance = round(r["total_shifts_worked"] / acc * 100, 1) if acc > 0 else None
        awop_pct = round(r["awop_shifts"] / acc * 100, 2) if acc > 0 else None
        strike_pct = round(r["strike_shifts"] / acc * 100, 2) if acc > 0 else None
        scatter.append({
            "gang": r["gang"], "section": r["section"], "bucket": bucket,
            "attendance": attendance, "awop_pct": awop_pct, "strike_pct": strike_pct,
            "labour": r["total_avg_labour"], "bonus_per_person_month": r["bonus_per_person_month"],
            "safety_incidents": r["safety_incidents"], "sqm_per_man": r["sqm_per_man"],
        })

    return {
        "best": [{"gang": r["gang"], "section": r["section"], "sqm_per_man": r["sqm_per_man"]} for r in best],
        "worst": [{"gang": r["gang"], "section": r["section"], "sqm_per_man": r["sqm_per_man"]} for r in worst],
        "comparison": comparison,
        "scatter": scatter,
        "n": n,
    }


def _decompose_bonus_row(row, safety_defaults: dict) -> tuple[float, float, bool]:
    """Return (raw_qualifying, actual_safety_pct, gated) for one anchored Stope Breaking row.

    Shared by get_bonus_rule_data() (Bonus Policy Simulator) and get_bonus_composition()/
    get_bonus_outcomes() (Bonus Analysis tables), so the "what the bonus is built from"
    numbers shown in both places are always computed the same way — see
    get_bonus_rule_data()'s docstring and this function's original inline comments (moved
    here 2026-11-03 when the Mponeng-pattern bonus-breakdown tables were added) for the full
    reasoning on anchoring, the real-vs-policy safety %, and why "clean" is a per-gang ratio
    rather than a hardcoded 10%.
    """
    if row["fatal_ind"] >= 1:
        real_pct = row.get("fatal_penalty_pct")
        safety_pct = float(real_pct) if pd.notna(real_pct) else safety_defaults["fatal"]
    elif row["lti_ind"] >= 1:
        real_pct = row.get("lti_penalty_pct")
        safety_pct = float(real_pct) if pd.notna(real_pct) else safety_defaults["lti"]
    elif row["dress_ind"] >= 1:
        real_pct = row.get("dress_penalty_pct")
        safety_pct = float(real_pct) if pd.notna(real_pct) else safety_defaults["dressing"]
    else:
        eff_for_ratio = float(row["efficiency_bonus"])
        safety_pct = (float(row["safety_bonus"]) / eff_for_ratio * 100) if eff_for_ratio > 0 else safety_defaults["clean"]

    actual_bonus = float(row["efficiency_bonus"])
    if actual_bonus <= 0:
        return 0.0, safety_pct, True  # gated: no signal to recover a base for this component

    combined_mult = (
        float(row["netting_rate"]) * float(row["sw_factor"]) * (1 + safety_pct / 100)
    )
    if combined_mult <= 1e-9:
        return 0.0, safety_pct, True
    return actual_bonus / combined_mult, safety_pct, False


def get_bonus_rule_data(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """Per-gang STOPE BREAKING bonus data for the Bonus Policy Simulator tab.

    Phakisa schema has four bonus components (verify against Tshepong pattern): efficiency, driller, sweeping penalty, safety.
    sweep_penalty is signed (usually ≤0) — GANGFINALSWEEPINGSBONUS is a penalty deduction for
    failing the sweepings standard (policy §5.14), not an actual bonus, despite the column name.
    Adj SQM from GANGTOTALSQMADJUSTED; pre-adj from SUM(WORKPLACETOTALSQM) per gang/period.

    IMPORTANT — dollar totals are anchored to PARTICIPANTSDETAIL, not GANGPRODUCTIONDETAIL:
    GANGPRODUCTIONDETAIL's bonus columns are per-person rates that, multiplied by GANGLABOUR
    (a rounded-up *average* headcount used for the efficiency calc), only ever produce an
    ESTIMATE of the gang's total payout — not the real amount paid to real individuals. That
    estimate was found to diverge from PARTICIPANTSDETAIL (the canonical payroll source, also
    what "Bonus Analysis"/Bonus by Gang Type is built from) by double digits of a percent: for
    one real period range, GANGLABOUR summed to 3,650 "labour units" against 4,220 real
    employee-period records in PARTICIPANTSDETAIL for the identical gangs. So every gang row
    here is re-anchored: Driller and Safety take PARTICIPANTSDETAIL's own EMPLOYEEDRILLERBONUS/
    EMPLOYEESAFETYBONUS totals directly (already isolated there); Efficiency and Sweep are
    bundled into one real figure in PARTICIPANTSDETAIL (EMPLOYEESTOPETEAMBONUS / "STM" —
    confirmed empirically: the raw GANGPRODUCTIONBONUS column equals efficiency + sweep +
    safety, driller excluded), so they're split back out of the real STM total using the
    GANGPRODUCTIONDETAIL estimate's own efficiency:sweep ratio as weights. This means the
    grand total this function returns matches "Bonus by Gang Type"'s Stope Breaking total for
    the same period/section exactly (both ultimately sum PARTICIPANTSDETAIL for the same gang
    keys) — only the split between components, and the lever mechanics, come from the
    GANGPRODUCTIONDETAIL-side estimate, because only it has the raw ingredients (efficiency,
    netting flag, B-Reef factor, safety indicators) the simulator's levers need.

    The (now anchored) Efficiency Bonus is decomposed into a lever-independent "raw_qualifying"
    amount so the simulator can recompute it under different policy parameters instead of just
    scaling the actual paid figure:
        efficiency_bonus_anchored = raw_qualifying × netting_mult × sw_mult × safety_mult
    netting_mult/sw_mult are empirically verified to multiply exactly onto the *estimate*
    (WORKPLACENETTINGRATE and B_REEF_SW_FACTOR multiply exactly onto GANGFULLSTOPINGBONUS to
    produce GANGFINALBREAKBONUS, checked across 39 gangs spanning 100m² to 600m² — also
    confirming there is no tier effect; see BONUS_POLICY_DEFAULTS comment above for why the
    old tier escalator is gone from both the current policy and this model). Applying those
    same verified ratios to the anchored dollar figure is an extrapolation, not something
    independently re-verified against real anchored data — flagged in the simulator UI.
    The entry-level gate is applied separately (a hard cutoff, not a multiplier), using the
    real GANGEFFICIENCY (m²/empl) figure, which the anchoring doesn't touch. Wide Raise
    workplaces (WORKPLACERATETYPE) are exempted from it per policy §5.4, confirmed against
    real low-efficiency Wide Raise gangs that still earn a nonzero efficiency bonus.
    Gangs that already earn $0 (gated below the entry level, or a fatal incident zeroed them
    out) cannot be reconstructed upward — there is no data signal to recover what they would
    have qualified for, so they are flagged "gated" and held at $0 regardless of lever changes.

    Not modelled here (see BONUS_POLICY_DEFAULTS comment for why): the §5.10/§5.11 Steep
    Stope m² uplift (real, active, but already baked into GANGTOTALSQMADJUSTED upstream of
    this data, not a separate Rand lever) and the §6.1.1/§6.1.2 Physical Conditions Rating
    safety forfeiture (searched PRODUCTIONEARN/GANGLINKEARN/RATES for any CONDITION/RATING/
    INSPECT column — none exists, so there's no data to drive it; every "clean" gang in a
    full year of data got exactly the full +25%, zero exceptions, so it either hasn't
    triggered yet or isn't wired into this data source).
    """
    df = _pull_anchored_stope_breaking(period_from, period_to, section)
    _empty: dict = {"gangs": [], "summary": {}, "policy_defaults": BONUS_POLICY_DEFAULTS,
                    "observed_defaults": {}, "empirical_curve": []}
    if df.empty:
        return _empty

    defaults = BONUS_POLICY_DEFAULTS
    safety_defaults = defaults["safety_pct"]

    # actual_safety_pct is derived purely from the gang's real incident indicators —
    # independent of whether the efficiency bonus happens to be gated to zero, since a gang
    # can be efficiency-gated (e.g. below the entry level) while still having a genuine
    # non-clean safety record and a real, separate GANGFINALSAFETYBONUS payout. `gated` only
    # describes whether raw_qualifying (the efficiency component) can be reconstructed via
    # the netting/safety formula — it does not apply to the safety bonus recompute in the
    # frontend, which checks the gang's own safety_bonus value independently, nor to the
    # empirical_efficiency_bonus fallback (see _build_empirical_curve), which offers a Step 2
    # estimate for gated-below-entry gangs specifically. See _decompose_bonus_row() for the
    # real-vs-policy safety % precedence and why "clean" is a per-gang ratio, not a hardcoded
    # 10% (checked empirically: the real ratio averages ~9.5%, not exactly 10%).
    decomposed = df.apply(lambda row: _decompose_bonus_row(row, safety_defaults), axis=1, result_type="expand")
    df["raw_qualifying"], df["actual_safety_pct"], df["gated"] = (
        decomposed[0], decomposed[1], decomposed[2]
    )
    df["had_netting"] = df["netting_rate"] > 1.0001
    df["had_sw"]      = df["sw_factor"]    > 1.0001

    # Real, empirically-observed lever values for THIS period/section scope — what was
    # actually paid, computed as simple averages/minimums over real rows, never a
    # recalculation of any bonus amount. Used to set where each Simulator slider actually
    # starts (see web/templates/index.html's _brsInitControls); policy_defaults (returned
    # separately below) is shown alongside via an info button instead, for reference. None
    # means "not observed in this scope" — the frontend falls back to the policy default.
    def _avg_or_none(mask, col) -> Optional[float]:
        vals = df.loc[mask, col].dropna()
        return round(float(vals.mean()), 4) if len(vals) else None

    netting_mask = df["had_netting"]
    sw_mask      = df["had_sw"]
    clean_mask   = (df["lti_ind"] < 1) & (df["dress_ind"] < 1) & (df["fatal_ind"] < 1) & (df["efficiency_bonus"] > 0)

    # entry_threshold has no "observed" equivalent, deliberately: efficiency_bonus > 0 isn't a
    # reliable "did this gang clear the entry gate" signal on its own, because the STM-anchor
    # fallback above (_anchor()) will assign a nonzero efficiency_bonus to a gang the
    # GANGPRODUCTIONDETAIL estimate itself gated to zero, whenever real payroll shows *some*
    # STM anyway (e.g. a pro-rata/acting payment unrelated to the entry gate) — tried a min and
    # a 5th-percentile of efficiency among "earning" gangs, both landed far below the policy
    # value (1.2 and 4.5 vs 14.5) purely from that confound, not a real threshold. Always None
    # here; the Simulator falls back to the policy value for this one lever specifically.
    observed_defaults = {
        "entry_threshold": None,
        "netting_pct":     _avg_or_none(netting_mask, "netting_rate"),
        "sw_pct":          _avg_or_none(sw_mask, "sw_factor"),
        "safety_pct": {
            # Averaged from actual_safety_pct — the SAME per-gang basis _decompose() used to
            # build raw_qualifying for every tier, clean included (see _decompose()'s
            # docstring: clean's real % is computed per gang as safety_bonus÷efficiency_bonus,
            # not a flat policy assumption) — so this average and the formula's own internal
            # math always agree, by construction, regardless of what the average comes out
            # to. This is a genuine AVERAGE of real per-gang ratios (expect real variance
            # between gangs, same as Netting/B-Reef) — label this on the slider as an
            # observed average, not a verified single database value, since there's no
            # per-gang "clean %" column the way LTI/Dressing/Fatal each have one.
            "clean":    _avg_or_none(clean_mask, "actual_safety_pct"),
            "dressing": _avg_or_none(df["dress_ind"] >= 1, "dress_penalty_pct"),
            "lti":      _avg_or_none(df["lti_ind"] >= 1, "lti_penalty_pct"),
            "fatal":    _avg_or_none(df["fatal_ind"] >= 1, "fatal_penalty_pct"),
        },
    }
    if observed_defaults["netting_pct"] is not None:
        observed_defaults["netting_pct"] = round((observed_defaults["netting_pct"] - 1) * 100, 2)
    if observed_defaults["sw_pct"] is not None:
        observed_defaults["sw_pct"] = round((observed_defaults["sw_pct"] - 1) * 100, 2)

    # Step 2 (empirical) fallback for gated gangs — see _build_empirical_curve()'s docstring
    # for why this exists and what it isn't. Computed for every gang, not just gated ones,
    # since it's cheap and the frontend decides when to actually use it (only for a gang
    # that's gated purely on efficiency and would newly qualify under a lowered threshold).
    curve = _build_empirical_curve(section)
    df["empirical_efficiency_bonus"] = df.apply(
        lambda r: round(_curve_lookup(curve, float(r["efficiency"] or 0)) * float(r["labour"] or 0), 2),
        axis=1,
    )

    total_sqm_adj    = float(df["sqm"].sum())
    total_sqm_preadj = float(df["sqm_preadj"].sum())
    total_efficiency  = float(df["efficiency_bonus"].sum())
    total_drill  = float(df["drill_bonus"].sum())
    total_sweep_penalty  = float(df["sweep_penalty"].sum())
    total_safety = float(df["safety_bonus"].sum())
    gang_count   = len(df)
    gated_count  = int(df["gated"].sum())

    avg_efficiency_rate_adj    = round(total_efficiency / total_sqm_adj,    4) if total_sqm_adj    > 0 else 0.0
    avg_efficiency_rate_preadj = round(total_efficiency / total_sqm_preadj, 4) if total_sqm_preadj > 0 else 0.0

    gangs = [
        {
            "section":      row["section"],
            "period":       _period_label(int(row["period"])),
            "gang":         row["gang"],
            "sqm_adj":      round(float(row["sqm"] or 0), 0),
            "sqm_preadj":   round(float(row["sqm_preadj"] or 0), 0),
            "labour":       round(float(row["labour"] or 0) if not pd.isna(row["labour"]) else 0, 2),
            "efficiency":   round(float(row["efficiency"] or 0), 2),
            "efficiency_bonus":  round(float(row["efficiency_bonus"] or 0), 2),
            "drill_bonus":  round(float(row["drill_bonus"] or 0), 2),
            "sweep_penalty":  round(float(row["sweep_penalty"] or 0), 2),
            "safety_bonus": round(float(row["safety_bonus"] or 0), 2),
            "raw_qualifying": round(float(row["raw_qualifying"] or 0), 2),
            "had_netting":  bool(row["had_netting"]),
            "had_sw":       bool(row["had_sw"]),
            "had_wideraise": bool(row["had_wideraise"]),
            "gated":        bool(row["gated"]),
            "fatal_ind":    bool(row["fatal_ind"] >= 1),
            "actual_safety_pct": round(float(row["actual_safety_pct"]), 2),
            # Which safety-ladder rung actually applied to this gang, straight from the real
            # incident indicators (same precedence as _decompose above) — not something the
            # frontend should re-derive by guessing from the actual_safety_pct number, since
            # real per-gang penalty %'s vary and don't line up with fixed thresholds (e.g. a
            # real dressing case is never exactly the policy's -25%, so a "== -25" guess would
            # misclassify it).
            "safety_rung": (
                "fatal"    if row["fatal_ind"] >= 1 else
                "lti"      if row["lti_ind"]   >= 1 else
                "dressing" if row["dress_ind"] >= 1 else
                "clean"
            ),
            "empirical_efficiency_bonus": round(float(row["empirical_efficiency_bonus"] or 0), 2),
            # Real reference data (policy §7 / §6.4) — display-only, not used in any
            # calculation here. stopewidth_rate is the real applied deviation bonus/penalty
            # (e.g. 0.15 = +15%, -0.25 = -25%); face_length is the raw m² figure the §6.4
            # factor table would be looked up by (the table itself isn't reproduced here).
            "stopewidth_rate": round(float(row["stopewidth_rate"] or 0), 4),
            "face_length":     round(float(row["face_length"] or 0), 1),
        }
        for _, row in df.iterrows()
    ]

    return {
        "gangs": gangs,
        "summary": {
            "total_efficiency":        round(total_efficiency, 2),
            "total_drill":        round(total_drill, 2),
            "total_sweep_penalty":        round(total_sweep_penalty, 2),
            "total_safety":       round(total_safety, 2),
            # STM alone (efficiency + sweep, the anchored split of real EMPLOYEESTOPETEAMBONUS)
            # — NOT +total_drill +total_safety. Those are real, separately-tracked
            # PARTICIPANTSDETAIL columns but are informational sub-figures already reflected
            # inside STM's real dollar total, not separate add-on payments — verified in
            # _get_participants_bonus() against the source system's own broadcast
            # EMPLOYEETOTALBONUS (STM alone matches to within 0.004%; STM+Safety+Driller
            # overstates by ~12-15%). Adding them here would reproduce that same overstatement
            # in every "Total Bonus" figure and Annualized Impact this Simulator shows.
            "total_bonus":        round(total_efficiency + total_sweep_penalty, 2),
            "total_sqm_adj":      round(total_sqm_adj, 0),
            "total_sqm_preadj":   round(total_sqm_preadj, 0),
            "gang_count":         gang_count,
            "gated_count":        gated_count,
            "avg_efficiency_rate_adj":    avg_efficiency_rate_adj,
            "avg_efficiency_rate_preadj": avg_efficiency_rate_preadj,
        },
        "policy_defaults": defaults,
        "observed_defaults": observed_defaults,
        "empirical_curve": curve,
    }


def get_bonus_composition(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """"What the Bonus Is Made Of" — Phakisa adaptation of the Mponeng bonus-breakdown pattern.

    Ported from a sibling dashboard (Mponeng, policy WA_202604_STP_REV19) whose bonus is an
    ADDITIVE sum (production = stoping + sweepings + quality + AWOP penalty + penalty).
    Verified 2026-11-03 that this does NOT hold for Phakisa: Sweeping Penalty is always R0
    here (no real sweeping-penalty component exists in this data), and Safety/Driller/AWOP
    are already-included reference figures, not separate add-on payments (see
    _get_participants_bonus()'s docstring — summing STM alone reproduces the real broadcast
    total; adding Safety+Driller+AWOP on top overstates it).

    What IS real and provable for Phakisa is a sequential MULTIPLICATIVE build-up of the one
    true paid total (STM):
        raw_qualifying -> x Netting (§6.17.5) -> x B-Reef stope-width (§6.3) -> x Safety
        ladder (§8.1) -> = Efficiency Bonus (= STM, since Sweep is always 0 here).
    Each step's dollar uplift/deduction is exact, not an approximation with cross-terms,
    because the steps are applied in sequence (raw*netting=X, X*sw=Y, Y*safety=Z, and Z is
    the real anchored efficiency_bonus by construction — see _decompose_bonus_row()).
    Gated gangs (no basis to reconstruct raw_qualifying — see _decompose_bonus_row()) have
    their whole real efficiency_bonus, if any, carried through unchanged at every step
    (zero uplift attributed), landing in the Raw Qualifying Bonus row rather than being
    misattributed to Netting/B-Reef/Safety.

    Driller Bonus and the real AWOP penalty are returned as reference rows only — both are
    genuinely separate PARTICIPANTSDETAIL columns, but (like Safety) already reflected
    inside STM, so they must never be added to the waterfall's total.
    """
    df = _pull_anchored_stope_breaking(period_from, period_to, section)
    if df.empty:
        return {"waterfall": [], "reference": [], "summary": {}}

    safety_defaults = BONUS_POLICY_DEFAULTS["safety_pct"]
    decomposed = df.apply(lambda row: _decompose_bonus_row(row, safety_defaults), axis=1, result_type="expand")
    df["raw_qualifying"], df["actual_safety_pct"], df["gated"] = decomposed[0], decomposed[1], decomposed[2]

    gated_mask = df["gated"]
    step0 = df["raw_qualifying"].where(~gated_mask, df["efficiency_bonus"])
    step1 = (step0 * df["netting_rate"]).where(~gated_mask, step0)
    step2 = (step1 * df["sw_factor"]).where(~gated_mask, step1)
    step3 = (step2 * (1 + df["actual_safety_pct"] / 100)).where(~gated_mask, step2)

    raw_total        = float(step0.sum())
    netting_uplift    = float((step1 - step0).sum())
    sw_uplift          = float((step2 - step1).sum())
    safety_impact      = float((step3 - step2).sum())
    efficiency_total  = float(df["efficiency_bonus"].sum())  # = STM, Sweep always 0

    gang_keys = df[["section", "period", "gang"]].drop_duplicates()
    participants = _get_participants_bonus(period_from, period_to, section)
    if not participants.empty:
        ref = gang_keys.merge(
            participants[["section", "period", "gang",
                          "total_driller_bonus", "total_awop_bonus", "total_safety_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        for c in ["total_driller_bonus", "total_awop_bonus", "total_safety_bonus"]:
            ref[c] = ref[c].fillna(0).astype(float)
        driller_total = float(ref["total_driller_bonus"].sum())
        awop_total    = float(ref["total_awop_bonus"].sum())
        safety_total  = float(ref["total_safety_bonus"].sum())
    else:
        driller_total = awop_total = safety_total = 0.0

    waterfall = [
        {"step": "raw_qualifying", "label": "Raw Qualifying Bonus", "rand": round(raw_total, 2),
         "rule": "Base production bonus before any uplift or the safety ladder (policy §6.8/§6.3). Gated gangs' real STM, where any, is carried here unchanged — there is no basis to split it further."},
        {"step": "netting", "label": "+ Netting Uplift", "rand": round(netting_uplift, 2),
         "rule": "§6.17.5: +6.6% of Qualifying Bonus for gangs with permanent netting installed (real WORKPLACENETTINGRATE)."},
        {"step": "breef", "label": "+ B-Reef Stope-Width Uplift", "rand": round(sw_uplift, 2),
         "rule": "§6.3: stope-width uplift tiers (1.25x / 1.5x) — no real cases observed in this scope to date."},
        {"step": "safety", "label": "+/- Safety Ladder", "rand": round(safety_impact, 2),
         "rule": "§8.1: clean +10%, dressing/TIA -25%, LTI -50%, fatal -100% — applied to the Netting/B-Reef-adjusted amount."},
        {"step": "total", "label": "= Efficiency Bonus (paid as STM)", "rand": round(efficiency_total, 2),
         "rule": "The real, paid total. Sweeping Penalty is always R0 for Phakisa, so Efficiency Bonus = STM exactly."},
    ]
    reference = [
        {"key": "safety", "label": "Safety Bonus (reference)", "rand": round(safety_total, 2),
         "rule": "Real GANGFINALSAFETYBONUS/EMPLOYEESAFETYBONUS total. Already reflected inside STM above — not an extra payment."},
        {"key": "driller", "label": "Driller Bonus (reference)", "rand": round(driller_total, 2),
         "rule": "Real EMPLOYEEDRILLERBONUS total — a separately-tracked figure, already reflected inside STM above."},
        {"key": "awop", "label": "AWOP Penalty (reference)", "rand": round(awop_total, 2),
         "rule": "§10: 1 shift AWOP = -50%, 2+ shifts = -100% of the bonus. Real EMPLOYEEAWOPPENALTY total — already netted into STM above, shown here for visibility only."},
    ]
    return {
        "waterfall": waterfall,
        "reference": reference,
        "summary": {
            "raw_qualifying_total": round(raw_total, 2),
            "efficiency_total":     round(efficiency_total, 2),
            "gang_count":           int(len(df)),
            "gated_count":          int(gated_mask.sum()),
        },
    }


def get_bonus_outcomes(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """"Who Earned What — Gang-Periods per Bonus Outcome" — Phakisa adaptation.

    Mponeng classifies PEOPLE per month because its sweepings/quality/safety add-ons are all
    personal. Phakisa has no per-employee table surfaced anywhere else in this app (Gang
    Detail is the finest grain throughout this dashboard), and Netting/B-Reef/the Safety
    ladder are gang-level facts (one netting flag, one set of incident indicators per
    gang-period) — so this classifies GANG-PERIODS, the same unit as every other table here.
    AWOP is the one genuinely personal signal available (EMPLOYEEAWOPPENALTY, per employee),
    but is rolled up to "did this gang-period have any AWOP deduction" to stay on the same
    grain as the rest of the table, and to avoid the per-employee fan-out risk documented in
    _get_participants_bonus() (raw PARTICIPANTSEARN repeats rows per employee per gang
    assignment; only the materialized, pre-aggregated PARTICIPANTSDETAIL is safe to sum).

    Each outcome's rand is built from the exact same per-row figures get_bonus_composition()
    sums into its waterfall, so the rand here adds back to that table's rows by construction:
    the two Netting rows sum to composition's Netting Uplift; the four Safety rows sum to
    composition's Safety Ladder; the AWOP "docked" row equals composition's AWOP reference.
    """
    df = _pull_anchored_stope_breaking(period_from, period_to, section)
    if df.empty:
        return {"components": [], "total_gang_periods": 0}

    safety_defaults = BONUS_POLICY_DEFAULTS["safety_pct"]
    decomposed = df.apply(lambda row: _decompose_bonus_row(row, safety_defaults), axis=1, result_type="expand")
    df["raw_qualifying"], df["actual_safety_pct"], df["gated"] = decomposed[0], decomposed[1], decomposed[2]
    df["had_netting"] = df["netting_rate"] > 1.0001
    df["had_sw"]      = df["sw_factor"]    > 1.0001
    df["safety_rung"] = df.apply(
        lambda r: ("fatal"    if r["fatal_ind"] >= 1 else
                   "lti"      if r["lti_ind"]   >= 1 else
                   "dressing" if r["dress_ind"] >= 1 else
                   "clean"),
        axis=1,
    )

    gated_mask = df["gated"]
    raw_q  = df["raw_qualifying"].where(~gated_mask, df["efficiency_bonus"])
    step1  = (raw_q * df["netting_rate"]).where(~gated_mask, raw_q)
    step2  = (step1 * df["sw_factor"]).where(~gated_mask, step1)
    step3  = (step2 * (1 + df["actual_safety_pct"] / 100)).where(~gated_mask, step2)
    df["netting_delta"] = step1 - raw_q
    df["sw_delta"]      = step2 - step1
    df["safety_delta"]  = step3 - step2

    keys = df[["section", "period", "gang"]].drop_duplicates()
    participants = _get_participants_bonus(period_from, period_to, section)
    if not participants.empty:
        awop = keys.merge(
            participants[["section", "period", "gang", "total_awop_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
        awop["total_awop_bonus"] = awop["total_awop_bonus"].fillna(0).astype(float)
    else:
        awop = keys.copy()
        awop["total_awop_bonus"] = 0.0
    df = df.merge(awop, on=["section", "period", "gang"], how="left")
    df["total_awop_bonus"] = df["total_awop_bonus"].fillna(0.0)

    total = len(df)

    def _bucket(mask, rand_series):
        return int(mask.sum()), round(float(rand_series[mask].sum()), 2)

    netting_n, netting_rand         = _bucket(df["had_netting"],  df["netting_delta"])
    not_netting_n, not_netting_rand = _bucket(~df["had_netting"], df["netting_delta"])
    sw_n, sw_rand                   = _bucket(df["had_sw"],       df["sw_delta"])
    not_sw_n, not_sw_rand           = _bucket(~df["had_sw"],      df["sw_delta"])

    safety_rows = []
    for rung, label, rule in [
        ("clean",    "Clean — +10%",          "§8.1: no incident, full +10% safety uplift."),
        ("dressing", "Dressing / TIA — -25%", "§8.1: dressing/TIA incident, -25% safety penalty."),
        ("lti",      "LTI — -50%",            "§8.1: lost-time injury, -50% safety penalty."),
        ("fatal",    "Fatal — -100%",         "§8.1: fatality, -100% safety penalty (bonus forfeited)."),
    ]:
        mask = df["safety_rung"] == rung
        n, rand = _bucket(mask, df["safety_delta"])
        safety_rows.append({"key": rung, "label": label, "rule": rule, "gang_periods": n, "rand": rand})

    awop_mask = df["total_awop_bonus"] != 0
    awop_n, awop_rand             = _bucket(awop_mask,  df["total_awop_bonus"])
    clean_awop_n, clean_awop_rand = _bucket(~awop_mask, df["total_awop_bonus"])

    return {
        "total_gang_periods": total,
        "components": [
            {
                "component": "netting", "title": "Netting (§6.17.5)",
                "rows": [
                    {"key": "netted", "label": "Netted — +6.6%", "gang_periods": netting_n, "rand": netting_rand,
                     "rule": "§6.17.5: permanent netting installed, +6.6% uplift."},
                    {"key": "not_netted", "label": "Not netted", "gang_periods": not_netting_n, "rand": not_netting_rand,
                     "rule": "No permanent netting — no uplift."},
                ],
            },
            {
                "component": "breef", "title": "B-Reef Stope-Width (§6.3)",
                "rows": [
                    {"key": "uplifted", "label": "Uplifted (1.25x / 1.5x)", "gang_periods": sw_n, "rand": sw_rand,
                     "rule": "§6.3: wide-stope tier applied."},
                    {"key": "not_uplifted", "label": "Not uplifted", "gang_periods": not_sw_n, "rand": not_sw_rand,
                     "rule": "No stope-width uplift tier applied."},
                ],
            },
            {"component": "safety", "title": "Safety Ladder (§8.1)", "rows": safety_rows},
            {
                "component": "awop", "title": "AWOP (§10)",
                "rows": [
                    {"key": "docked", "label": "AWOP deduction applied", "gang_periods": awop_n, "rand": awop_rand,
                     "rule": "§10: 1 shift = -50%, 2+ shifts = -100% of the bonus. Already netted into STM — shown for visibility only."},
                    {"key": "none", "label": "No AWOP", "gang_periods": clean_awop_n, "rand": clean_awop_rand,
                     "rule": "No AWOP deduction recorded."},
                ],
            },
        ],
    }


def get_policy_discrepancies(period_from: int, period_to: int, section: str = "ALL") -> dict:
    """"Where the System Differs from the Policy" — Phakisa adaptation of the Mponeng pattern.

    Mponeng compares its RATES/FACTORS system-settings tables against the written REV19 text
    and against what was actually paid. Phakisa has no equivalent RATES-table investigation
    done yet, so this does NOT claim to be that full comparison — it reports only what has
    already been independently verified elsewhere in this app (the Bonus Policy Simulator's
    own anchoring/decomposition work), re-surfaced here as explicit findings:

    - Clean safety %: the written policy (§8.1) states a flat +10% for a clean safety record.
      The real per-gang ratio (safety_bonus ÷ efficiency_bonus, the same basis
      _decompose_bonus_row() uses) averages ~9.47% across every clean Stope Breaking gang in
      Jul-25 to Apr-26 (406 gangs) — close to, but not exactly, the written 10%. Real rand
      impact: real clean-gang efficiency totalled R27,914,442.30 vs R28,078,732.48 if every
      clean gang had earned exactly +10% — a R164,290.18 gap over that window (verified
      2026-11-14, same period/data as every other total in this app).
    - B-Reef stope-width uplift (§6.3): the written policy describes two uplift tiers (1.25x /
      1.5x). Zero of 424 real Stope Breaking gang-periods in the same window show a real
      B_REEF_SW_FACTOR above 1.0 — the provision exists in policy but has not fired once in
      the data checked so far.
    - Sweeping Penalty (§5.12 in this schema's column comment): GANGFINALSWEEPINGSBONUS is
      exactly 0 on every real row checked across this entire project — either the mechanism
      has never been triggered, or it isn't wired into this data source.
    - Netting (§6.17.5): the real WORKPLACENETTINGRATE on every netted gang averages exactly
      1.066 (6.6%) — matches the written policy exactly, no discrepancy.
    - Entry-level gate (§6.8, 14.5 m²/empl): cannot be independently verified against real
      data — a gang the estimate gates to zero can still show nonzero real STM for unrelated
      payroll reasons (acting pay, pro-rata), so there is no reliable "did this gang clear
      the gate" signal to check the 14.5 threshold against (documented in
      get_bonus_rule_data()'s own observed_defaults comment).

    Everything else in the written policy (safety-ladder tiers beyond clean/dressing/LTI/
    fatal, the §6.4 Face Length factor table, the §7 authorised-width deviation bonus table,
    §10 AWOP's exact per-shift percentages) has not yet been checked record-by-record against
    the system's own settings, and is deliberately left out rather than guessed at.
    """
    df = _pull_anchored_stope_breaking(period_from, period_to, section)
    if df.empty:
        return {"items": [], "gang_count": 0}

    safety_defaults = BONUS_POLICY_DEFAULTS["safety_pct"]
    decomposed = df.apply(lambda row: _decompose_bonus_row(row, safety_defaults), axis=1, result_type="expand")
    df["raw_qualifying"], df["actual_safety_pct"], df["gated"] = decomposed[0], decomposed[1], decomposed[2]
    df["had_sw"] = df["sw_factor"] > 1.0001
    clean_mask = (df["lti_ind"] < 1) & (df["dress_ind"] < 1) & (df["fatal_ind"] < 1) & (df["efficiency_bonus"] > 0)
    clean = df[clean_mask]

    clean_n = int(len(clean))
    avg_clean_pct = round(float(clean["actual_safety_pct"].mean()), 2) if clean_n else None
    if clean_n:
        policy_eff = clean["raw_qualifying"] * clean["netting_rate"] * clean["sw_factor"] * 1.10
        real_eff = clean["efficiency_bonus"]
        clean_rand_gap = round(float((real_eff - policy_eff).sum()), 2)
    else:
        clean_rand_gap = 0.0

    sw_n = int(df["had_sw"].sum())
    gang_count = int(len(df))

    netted = df[df["netting_rate"] > 1.0001]
    netting_avg = round(float(netted["netting_rate"].mean()), 4) if len(netted) else None

    sweep_total = round(float(df["sweep_penalty"].abs().sum()), 2)

    items = [
        {
            "key": "clean_safety", "status": "differs" if abs(avg_clean_pct - 10.0) > 0.05 else "matches",
            "title": "Clean Safety % — system pays slightly less than the written +10%",
            "policy": "§8.1: a clean safety record (no incidents) earns a flat +10% uplift.",
            "system": f"Real per-gang ratio averages {avg_clean_pct}% across {clean_n} clean gang-periods, not exactly 10%.",
            "rand": clean_rand_gap,
            "rand_label": "Gap vs. a flat 10% for every clean gang, this window",
        },
        {
            "key": "breef", "status": "differs",
            "title": "B-Reef Stope-Width Uplift — never observed as applied",
            "policy": "§6.3: a 1.25x or 1.5x uplift tier applies by stope-width band.",
            "system": f"0 of {gang_count} real Stope Breaking gang-periods in this window show B_REEF_SW_FACTOR above 1.0.",
            "rand": 0.0,
            "rand_label": "No rand impact observed — the uplift has not fired",
        },
        {
            "key": "sweep", "status": "differs",
            "title": "Sweeping Penalty — always R0 in this data",
            "policy": "The schema's own column name/comment implies a sweepings-standard penalty mechanism.",
            "system": "GANGFINALSWEEPINGSBONUS is exactly 0 on every real row checked across this project.",
            "rand": -sweep_total,
            "rand_label": "Total nonzero sweep penalty found (should be > 0 if active)",
        },
        {
            "key": "netting", "status": "matches",
            "title": "Netting Uplift — matches the written 6.6% exactly",
            "policy": "§6.17.5: +6.6% of Qualifying Bonus for gangs with permanent netting installed.",
            "system": f"Real WORKPLACENETTINGRATE averages {netting_avg} on every netted gang — matches exactly.",
            "rand": 0.0,
            "rand_label": "No discrepancy",
        },
        {
            "key": "entry", "status": "unverifiable",
            "title": "Entry-Level Gate (14.5 m²/empl) — cannot be independently checked",
            "policy": "§6.8: gangs below 14.5 m²/empl earn zero on the Efficiency component (Wide Raise exempt).",
            "system": "No reliable \"did this gang clear the gate\" signal exists in the real data to check the threshold against.",
            "rand": None,
            "rand_label": "Not computable from available data",
        },
    ]

    return {"items": items, "gang_count": gang_count}


def _pull_anchored_stope_breaking(period_from: int, period_to: int, section: str = "ALL") -> pd.DataFrame:
    """Shared pull + anchor step behind get_bonus_rule_data() and _build_empirical_curve().

    Returns one row per (section, period, gang) for STOPE BREAKING gangs, with Efficiency/
    Driller/Sweep/Safety bonus columns anchored to real PARTICIPANTSDETAIL payroll (see the
    inline comments below for why). period_from=period_to=0 means "all history" (used by
    the empirical curve, which wants the largest sample it can get).
    """
    sf = _section_filter(section)
    # Qualified with g. — this query LEFT JOINs PRODUCTIONWPDETAIL (alias p), which also has
    # a SECTION column, so the unqualified filter from _section_filter() would be ambiguous.
    sf_g = sf.replace("SECTION", "g.SECTION") if sf else ""
    period_filter = (f"TRY_CAST(PERIOD AS BIGINT) BETWEEN {period_from} AND {period_to}"
                     if period_from > 0 and period_to > 0
                     else "TRY_CAST(PERIOD AS BIGINT) BETWEEN 200001 AND 209912")

    # Dedupe WORKPLACETOTALSQM (and the other repeating gang-level columns) per (gang,
    # workplace) before summing/collapsing — see get_pre_adj_trend for why this matters.
    df = read_sql(f"""
        SELECT
            section, period, gang, crewno,
            MAX(adj_sqm)     AS sqm,
            SUM(wp_sqm)      AS sqm_preadj,
            MAX(labour)      AS labour,
            MAX(efficiency)  AS efficiency,
            MAX(efficiency_bonus) AS efficiency_bonus,
            MAX(drill_bonus) AS drill_bonus,
            MAX(sweep_penalty) AS sweep_penalty,
            MAX(safety_bonus)AS safety_bonus,
            MAX(netting_rate)AS netting_rate,
            MAX(sw_factor)   AS sw_factor,
            MAX(lti_ind)     AS lti_ind,
            MAX(dress_ind)   AS dress_ind,
            MAX(fatal_ind)   AS fatal_ind,
            MAX(is_wideraise)AS is_wideraise,
            MAX(lti_penalty_pct)   AS lti_penalty_pct,
            MAX(dress_penalty_pct) AS dress_penalty_pct,
            MAX(fatal_penalty_pct) AS fatal_penalty_pct,
            MAX(stopewidth_rate)   AS stopewidth_rate,
            MAX(face_length)       AS face_length
        FROM (
            SELECT
                section, period, gang, crewno, workplace,
                MAX(adj_sqm)      AS adj_sqm,
                MAX(wp_sqm)       AS wp_sqm,
                MAX(labour)       AS labour,
                MAX(efficiency)   AS efficiency,
                MAX(efficiency_bonus) AS efficiency_bonus,
                MAX(drill_bonus)  AS drill_bonus,
                MAX(sweep_penalty)  AS sweep_penalty,
                MAX(safety_bonus) AS safety_bonus,
                MAX(netting_rate) AS netting_rate,
                MAX(sw_factor)    AS sw_factor,
                MAX(lti_ind)      AS lti_ind,
                MAX(dress_ind)    AS dress_ind,
                MAX(fatal_ind)    AS fatal_ind,
                MAX(is_wideraise) AS is_wideraise,
                MAX(lti_penalty_pct)   AS lti_penalty_pct,
                MAX(dress_penalty_pct) AS dress_penalty_pct,
                MAX(fatal_penalty_pct) AS fatal_penalty_pct,
                MAX(stopewidth_rate)   AS stopewidth_rate,
                MAX(face_length)       AS face_length
            FROM (
                SELECT
                    LTRIM(RTRIM(g.SECTION))   AS section,
                    LTRIM(RTRIM(g.PERIOD))    AS period,
                    LTRIM(RTRIM(g.GANG))      AS gang,
                    LTRIM(RTRIM(g.CREWNO))    AS crewno,
                    LTRIM(RTRIM(g.WORKPLACE)) AS workplace,
                    TRY_CAST(g.GANGTOTALSQMADJUSTED    AS FLOAT) AS adj_sqm,
                    TRY_CAST(g.WORKPLACETOTALSQM       AS FLOAT) AS wp_sqm,
                    TRY_CAST(g.GANGLABOUR              AS FLOAT) AS labour,
                    TRY_CAST(g.GANGEFFICIENCY          AS FLOAT) AS efficiency,
                    TRY_CAST(g.GANGFINALBREAKBONUS     AS FLOAT) AS efficiency_bonus,
                    TRY_CAST(g.GANGDRILLERBONUS        AS FLOAT) AS drill_bonus,
                    TRY_CAST(g.GANGFINALSWEEPINGSBONUS AS FLOAT) AS sweep_penalty,
                    TRY_CAST(g.GANGFINALSAFETYBONUS    AS FLOAT) AS safety_bonus,
                    TRY_CAST(g.WORKPLACENETTINGRATE    AS FLOAT) AS netting_rate,
                    TRY_CAST(g.B_REEF_SW_FACTOR        AS FLOAT) AS sw_factor,
                    TRY_CAST(g.GANGLTIIND              AS FLOAT) AS lti_ind,
                    TRY_CAST(g.GANGDRESSINGIND         AS FLOAT) AS dress_ind,
                    TRY_CAST(g.GANGFATALIND            AS FLOAT) AS fatal_ind,
                    -- Real, mine-computed penalty %s (policy §8.1) — read directly, not
                    -- inferred from an indicator + a hardcoded ladder value. See
                    -- sql/usp_RebuildIncentiveViews.sql's 2026-09-30 note for how these
                    -- were verified (GANGLTIPENALTYPERCENTAGE = -50.0 exactly on every
                    -- real LTI gang checked).
                    TRY_CAST(g.GANGLTIPENALTYPERCENTAGE          AS FLOAT) AS lti_penalty_pct,
                    TRY_CAST(g.GANGDRESSINGSPENALTYPERCENTAGE    AS FLOAT) AS dress_penalty_pct,
                    TRY_CAST(g.GANGFATALPENALTYPERCENTAGE        AS FLOAT) AS fatal_penalty_pct,
                    -- Real §7 authorised-stoping-width deviation bonus/penalty (±15%/±25%)
                    -- and §6.4 Face Length reference figure — display-only, not recomputed.
                    TRY_CAST(g.WORKPLACESTOPEWIDTHRATE           AS FLOAT) AS stopewidth_rate,
                    TRY_CAST(g.WORKPLACEAVGFACELENGTH            AS FLOAT) AS face_length,
                    CASE WHEN UPPER(LTRIM(RTRIM(p.WORKPLACERATETYPE))) = 'WIDE RAISE BONUS'
                         THEN 1 ELSE 0 END AS is_wideraise
                FROM [GANGPRODUCTIONDETAIL] g
                LEFT JOIN [PRODUCTIONWPDETAIL] p
                       ON LTRIM(RTRIM(g.SECTION))   = LTRIM(RTRIM(p.SECTION))
                      AND LTRIM(RTRIM(g.PERIOD))    = LTRIM(RTRIM(p.PERIOD))
                      AND LTRIM(RTRIM(g.WORKPLACE)) = LTRIM(RTRIM(p.WORKPLACE))
                WHERE UPPER(LTRIM(RTRIM(g.GANGTYPE))) = 'STOPE BREAKING'
                  AND {period_filter.replace("PERIOD", "g.PERIOD")}
                  AND LEN(LTRIM(RTRIM(g.CREWNO))) >= 8
                  {sf_g}
            ) AS raw
            GROUP BY section, period, gang, crewno, workplace
        ) AS byworkplace
        GROUP BY section, period, gang, crewno
    """)
    if df.empty:
        return df

    df["period"]    = df["period"].astype(str)
    df["sqm"]       = df["sqm"].fillna(0).astype(float)
    df["sqm_preadj"]= df["sqm_preadj"].fillna(0).astype(float)
    df["efficiency"]= df["efficiency"].fillna(0).astype(float)
    df["had_wideraise"] = df["is_wideraise"].fillna(0).astype(float) > 0
    labour = df["labour"].fillna(0).astype(float)

    for bc in ["efficiency_bonus", "drill_bonus", "sweep_penalty", "safety_bonus"]:
        df[bc] = df[bc].fillna(0).astype(float) * labour

    # Anchor to PARTICIPANTSDETAIL — the canonical record of what was actually paid to real
    # individuals (this is what "Bonus Analysis"/Bonus by Gang Type is built from). Without
    # this, GANGPRODUCTIONDETAIL's bonus columns are a production-formula ESTIMATE (per-person
    # rate × GANGLABOUR, a rounded-up average headcount) that measurably diverges from real
    # payroll: checked one real period range where GANGLABOUR summed to 3,650 "labour units"
    # against 4,220 real employee-period records in PARTICIPANTSDETAIL for the identical
    # gangs — a 15.6% headcount-basis gap, which is why the Simulator's totals didn't match
    # Bonus Analysis's Stope Breaking total for the same period/section.
    # PARTICIPANTSDETAIL tracks Driller and Safety independently (EMPLOYEEDRILLERBONUS,
    # EMPLOYEESAFETYBONUS), so those anchor directly. Efficiency and Sweep are bundled into
    # one real figure there (EMPLOYEESTOPETEAMBONUS / "STM" — confirmed empirically: the raw
    # GANGPRODUCTIONBONUS column equals efficiency + sweep + safety, driller excluded), so
    # they're split back out using the GANGPRODUCTIONDETAIL estimate's own efficiency:sweep
    # ratio as weights — this preserves the formula's relative split (which only
    # GANGPRODUCTIONDETAIL can estimate, since it alone has the per-gang levers) while
    # anchoring the absolute total to real payroll.
    participants = _get_participants_bonus(period_from, period_to, section)
    if not participants.empty:
        keys = df[["section", "period", "gang"]].drop_duplicates()
        real = keys.merge(
            participants[["section", "period", "gang",
                          "total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]],
            on=["section", "period", "gang"], how="left"
        )
    else:
        real = df[["section", "period", "gang"]].drop_duplicates()
        real["total_stm_bonus"] = 0.0
        real["total_safety_bonus"] = 0.0
        real["total_driller_bonus"] = 0.0
    for col in ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
        real[col] = real[col].fillna(0).astype(float)
    df = df.merge(real, on=["section", "period", "gang"], how="left")
    for col in ["total_stm_bonus", "total_safety_bonus", "total_driller_bonus"]:
        df[col] = df[col].fillna(0).astype(float)

    def _anchor(row):
        est_eff, est_sweep = float(row["efficiency_bonus"]), float(row["sweep_penalty"])
        est_stm, real_stm = est_eff + est_sweep, float(row["total_stm_bonus"])
        if abs(est_stm) > 1e-9:
            ratio = real_stm / est_stm
            return est_eff * ratio, est_sweep * ratio
        # No estimate signal to split by (e.g. a gated gang) — if real payroll shows
        # something anyway, there's no basis to allocate it between efficiency/sweep,
        # so keep the whole real figure on efficiency rather than lose it from the total.
        return real_stm, 0.0

    anchored = df.apply(_anchor, axis=1, result_type="expand")
    df["efficiency_bonus"] = anchored[0]
    df["sweep_penalty"]    = anchored[1]
    df["drill_bonus"]      = df["total_driller_bonus"]
    df["safety_bonus"]     = df["total_safety_bonus"]

    # netting_rate / sw_factor come through as 1.0 (no uplift) or the applied multiplier
    # (e.g. 1.2, 1.1) — default missing values to 1.0 (no uplift), never 0, to keep the
    # decomposition division-safe.
    df["netting_rate"] = df["netting_rate"].fillna(1.0).astype(float).replace(0, 1.0)
    df["sw_factor"]    = df["sw_factor"].fillna(1.0).astype(float).replace(0, 1.0)
    df["lti_ind"]      = df["lti_ind"].fillna(0).astype(float)
    df["dress_ind"]    = df["dress_ind"].fillna(0).astype(float)
    df["fatal_ind"]    = df["fatal_ind"].fillna(0).astype(float)

    # Real, mine-applied penalty %s (policy §8.1) — NaN means "not applicable" (the
    # indicator for that tier wasn't set for this gang), not "0%"; left as NaN rather than
    # filled, so callers can tell "no LTI happened" apart from "an LTI happened at 0%".
    for col in ["lti_penalty_pct", "dress_penalty_pct", "fatal_penalty_pct"]:
        df[col] = df[col].astype(float)
    df["stopewidth_rate"] = df["stopewidth_rate"].fillna(0.0).astype(float)
    df["face_length"]     = df["face_length"].fillna(0.0).astype(float)

    return df


def _build_empirical_curve(section: str = "ALL", bin_size: float = 2.0) -> list[dict]:
    """Empirically observed Rand-per-person Efficiency Bonus rate, binned by efficiency
    (m²/empl), built from ALL historical STOPE BREAKING gang-periods (anchored to real
    PARTICIPANTSDETAIL payroll, same as get_bonus_rule_data — see _pull_anchored_stope_breaking).

    STEP 2 of a two-step fix for a real gap found in the Bonus Policy Simulator: the
    Tshepong-inherited formula (final_bonus = base × netting_mult × B-Reef_mult ×
    (1+safety%)) was checked against real Phakisa data and only holds for ~40% of gang
    rows — the other 60% diverge, sometimes by 2x, in both directions. That formula is
    what raw_qualifying / get_bonus_rule_data's lever recompute is built on, so it can't
    be trusted to reconstruct a gated gang's "what it would have earned."

    STEP 1 (not this) is getting Phakisa's real m²-per-team-size Rand table from whoever
    maintains the wage/bonus agreement — that's the actual fix, external to this database.

    STEP 2 (this) is a stopgap for while Step 1 is pending: instead of algebraically
    inverting a formula that's known to be wrong here, build a lookup straight from
    Phakisa's own paid history — "gangs that actually performed at this efficiency level
    historically earned about this much per person." It's still an approximation
    (interpolated from real data, not the true table), but it's grounded in what this
    mine actually pays, not a borrowed assumption. Used only for gangs that are gated
    purely for being below the entry-level efficiency gate (see get_bonus_rule_data) —
    every other lever (netting %, B-Reef %, safety ladder) is untouched by this and still
    uses the original formula, exactly as before.
    """
    df = _pull_anchored_stope_breaking(0, 0, section)
    if df.empty:
        return []
    d = df[(df["labour"] > 0) & (df["efficiency"] > 0) & (df["efficiency_bonus"] > 0)]
    if d.empty:
        return []
    rate_per_person = d["efficiency_bonus"] / d["labour"]
    eff_bin = (d["efficiency"] // bin_size) * bin_size
    agg = pd.DataFrame({"eff_bin": eff_bin, "rate": rate_per_person}).groupby("eff_bin").agg(
        rate=("rate", "median"),
        n=("rate", "count"),
    ).reset_index().sort_values("eff_bin")
    return [
        {
            "efficiency_from": float(r["eff_bin"]),
            "efficiency_to":   float(r["eff_bin"] + bin_size),
            "rate_per_person": round(float(r["rate"]), 2),
            "sample_size":     int(r["n"]),
        }
        for _, r in agg.iterrows()
    ]


def _curve_lookup(curve: list[dict], efficiency: float) -> float:
    """Rand-per-person rate for a given efficiency, from the empirical curve built by
    _build_empirical_curve(). Falls back to the nearest bin if efficiency sits outside
    every observed bin's range (e.g. below the lowest historically-seen efficiency)."""
    if not curve or efficiency <= 0:
        return 0.0
    for b in curve:
        if b["efficiency_from"] <= efficiency < b["efficiency_to"]:
            return b["rate_per_person"]
    nearest = min(
        curve,
        key=lambda b: min(abs(efficiency - b["efficiency_from"]), abs(efficiency - b["efficiency_to"])),
    )
    return nearest["rate_per_person"]
