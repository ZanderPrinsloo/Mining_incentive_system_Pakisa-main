# Phakisa Mine Incentive Dashboard — Handoff Brief

**Status: this file is now a current-state brief, not a bootstrapping guide.** The original version
(2026-08-20) described how to set up Phakisa from scratch, copying Tshepong's codebase and treating
every fact as "verify this for Phakisa." That verification is done — the dashboard is built, deployed,
and has gone through many rounds of real-data correction. This version replaces every speculative
"Tshepong showed X, check if Phakisa matches" with what Phakisa's own database and policy actually are,
confirmed. Companion document: `HANDOFF_mponeng_port_deviations.md` covers the narrower story of
porting Mponeng's screen design/tab structure onto this database — read that one for UI-layout
history, this one for what the data and policy actually are.

**How to use this file**: paste its contents into the first message of a new session working on this
project, or point a fresh session at it directly.

---

## 1. What this is

A Flask dashboard reporting the Stope Team bonus actually paid at Phakisa against production (m²),
built from SQL Server database **`STPTM2000`**. Current bonus policy: **"Stoping Bonus Cat 4-8,"
JJ_202608_STPTEAM_REV04**. The dashboard's tab/sub-tab structure deliberately matches a sibling
Harmony Gold mine's dashboard (Mponeng), adapted to Phakisa's own database and policy rather than
reusing Mponeng's numbers or column names.

Stack: Python 3.12, Flask, SQLAlchemy + pyodbc, Bootstrap 5.3, Chart.js 4, DataTables 1.13. Deployed
as a Windows service (Waitress WSGI server) — see `DEPLOY.md` for the production setup, `run_web.py`
for local dev. Git remote: `https://github.com/ZanderPrinsloo/Mining_incentive_system_Pakisa-main.git`
(branch `main`, **public** — `.env` must never carry real credentials, see `deploy/` and `DEPLOY.md`).

---

## 2. Confirmed database structure (STPTM2000)

Three tables carry almost everything:

- **`GANGPRODUCTIONDETAIL`** — one row per (gang, workplace, period). Bonus columns
  (`GANGFINALBREAKBONUS`, `GANGDRILLERBONUS`, `GANGFINALSWEEPINGSBONUS`, `GANGFINALSAFETYBONUS`) are
  **per-person rates**, multiplied by `GANGLABOUR` (a rounded *average* headcount) to estimate a gang
  total. This estimate diverges from real payroll by double digits of a percent (one real check: 3,650
  labour-units vs 4,220 real employee-period records for the same gangs) — **never trust this table's
  dollar totals directly; anchor to `PARTICIPANTSDETAIL` instead** (see §4).
- **`PARTICIPANTSDETAIL`** — one row per (employee, period); the real, actual payments. Key columns:
  `EMPLOYEESTOPETEAMBONUS` ("STM" — the one real, correct total, verified against the source system's
  own per-employee `EMPLOYEETOTALBONUS`), `EMPLOYEESAFETYBONUS`, `EMPLOYEEDRILLERBONUS`,
  `EMPLOYEEAWOPPENALTY` (real, substantial — -R3.54m across all history — and not used anywhere in
  this app until 2026-11). **Safety, Driller and AWOP are all already netted into STM** — none of the
  three may ever be added on top anywhere in this app. This was the root cause of several real
  double-counting bugs (KPI tiles, Simulator, Gang Detail, R/m²) fixed earlier in this project.
- **`PRODUCTIONWPDETAIL`** — one row per (workplace, period). `WORKPLACERATETYPE` (Wide Raise
  exemption check), `WORKPLACENETTINGRATE`, `WORKPLACESTOPEWIDTHRATE`, `WORKPLACEAVGFACELENGTH`.

Real gang types (verified via `GROUP BY GANGTYPE` over the whole of `GANGPRODUCTIONDETAIL`):
**STOPE BREAKING, STOPE CLEANING, CENTRE GULLY, EQUIPPING, RIGGING** — five, not six, and not the
STOPING/SURFACE ASSISTANT/ANCILLARY the filter bar used to list (that was a real, pre-existing bug,
fixed 2026-11).

Labour/attendance tables (confirmed 2026-11): **`BONUSSHIFTS`** has real `AWOP_SHIFTS`,
`SHIFTS_WORKED`, `STRIKE_SHIFTS`, `EFF_SHIFTS` and covers 78 real periods (2020-01 to 2026-07) —
reliable across this app's whole normal range. **`ATTENDANCE`** and **`CLOCKEDSHIFTS`** (which carries
the per-shift `EVENTCODE` a sick-leave breakdown needs) each only have **one real period populated**
(2026-02 and 2026-07 respectively) — neither covers this app's normal Jul-25–Apr-26 range, so a
sick-leave column could not be built reliably; Labour Analysis shows Strike Shifts instead.
**`SAFETYREPS`** exists but has **0 rows**. No physical-conditions column exists anywhere in
`GANGPRODUCTIONDETAIL`. Neither gap is estimated or invented anywhere in this app — both are simply
left out, with the reason stated in the UI.

No "planned m²" / policy-target m² column exists anywhere in `GANGPRODUCTIONDETAIL` — confirmed absent
by search, not assumed. Anywhere this app would otherwise show "actual vs planned," it uses Adjusted
vs Pre-Adjustment m² (Overview tab) or ranks by m²/Man (Labour Analysis's Best vs Worst Crews) instead,
with the substitution explained in-app.

`MINERSEARN<period>` tables are real (confirming the separate Stoping Miner Incentive Scheme, policy
JJ_202607_STPMINER_REV12, has real backing data) but are **not materialized** the way
`GANGPRODUCTIONDETAIL`/`PARTICIPANTSDETAIL` are — the scope note's excluded-miners total is a dynamic
`UNION ALL` over whichever `MINERSEARN<period>` tables actually exist (there are real gaps, e.g. no
`202004` table), checked against `INFORMATION_SCHEMA` at request time, not assumed to exist for every
month.

---

## 3. Policy — JJ_202608_STPTEAM_REV04 ("Stoping Bonus Cat 4-8")

Phakisa's bonus is **multiplicative**, not an additive sum of parts:

```
raw_qualifying × Netting (§6.17.5) × B-Reef stope-width (§6.3) × Safety ladder (§8.1)
    = Efficiency Bonus = STM (the real paid total; Sweeping Penalty is always exactly R0
      on every real row checked across this whole project)
```

Confirmed facts, each independently verified against real data:
- **Entry level** (§6.8): 14.5 m²/empl qualifying gate, Undercut/Open mining; Wide Raise workplaces
  exempt. No reliable "did this gang clear the gate" signal exists in real data (a gang the estimate
  gates to zero can still show nonzero real STM for unrelated payroll reasons), so this lever has no
  "observed" default anywhere in the Simulator — only the policy value.
- **Netting** (§6.17.5): +6.6% of Qualifying Bonus for gangs with permanent netting installed. Real
  `WORKPLACENETTINGRATE` averages **exactly 1.066** on every netted gang — matches the written policy
  exactly, no discrepancy found.
- **B-Reef stope-width** (§6.3): two uplift tiers (1.25x/1.5x) described in policy. **Zero of 424 real
  Stope Breaking gang-periods checked ever show it applied** — the provision exists in policy but has
  not fired once in the data checked so far.
- **Safety ladder** (§8.1): clean +10%, dressing/TIA -25%, LTI -50%, fatal -100%. The real per-gang
  ratio for "clean" (safety_bonus ÷ efficiency_bonus) averages **~9.47%, not exactly 10%** — a real,
  small, quantified gap (≈R164k over Jul-25–Apr-26, 406 clean gangs). LTI/dressing/fatal percentages
  are read directly from the database's own `GANGLTIPENALTYPERCENTAGE` etc. columns (confirmed exactly
  -50.0% on every real LTI gang checked), not inferred from a hardcoded ladder value.
- **AWOP** (§10): 1 shift = -50%, 2+ shifts = -100% of the bonus. Real `EMPLOYEEAWOPPENALTY` is
  substantial (-R3.54m across all history) and, like Safety/Driller, already netted into STM — not an
  extra deduction on top.
- **Steep Stope** (§6.2, dip ≥35° → +10% m²) is real but already baked into
  `GANGTOTALSQMADJUSTED` upstream — not a separate Rand lever this app can model.
- **Not modelled anywhere in this app**: the §6.4 Face Length factor table, the §7 authorised-width
  deviation bonus/penalty table (both shown as real reference columns only, not recomputed), and the
  safety ladder's tiers beyond clean/dressing/LTI/fatal (the database only carries those 3 real
  incident indicators, so e.g. "2+ TIA" has no separate model).

---

## 4. The STM-anchoring pattern — load-bearing, used everywhere

`GANGPRODUCTIONDETAIL`'s bonus columns are a per-person-rate × average-headcount *estimate*. The real
number is always `PARTICIPANTSDETAIL`'s `EMPLOYEESTOPETEAMBONUS` (STM), summed. Every function that
reports a dollar total anchors to STM and uses `GANGPRODUCTIONDETAIL` only for lever *mechanics*
(netting flag, B-Reef factor, safety indicators) — never as the absolute total. `_pull_anchored_stope_
breaking()` in `web/queries.py` is the shared pull+anchor step; `_decompose_bonus_row()` is the shared
decomposition (raw_qualifying from the anchored total) used by the Simulator, the bonus-composition/
outcomes tables, and the policy-discrepancies tab, so all four always agree with each other to the
cent for the same period/section scope.

**Never show Safety + Driller + AWOP added on top of STM anywhere** — all three are real,
separately-tracked `PARTICIPANTSDETAIL` columns but are already reflected inside STM's real dollar
total. Every toggle/picker in this app that lets a user include Safety/Driller/AWOP alongside STM
carries an explicit double-counting warning for this reason (KPI tiles, R/m² tab, m² Range Bonuses
picker, Gang Analysis's Bonuses picker).

---

## 5. Current dashboard structure

Six tabs, matching the sibling Mponeng dashboard's layout (see `HANDOFF_mponeng_port_deviations.md`
for the full story of what had to change to get there):

1. **Overview** — 1. Executive Summary (incl. "Monthly Bonus Cost per m² by Section") · 2. Production
   Performance · 3. Adj vs Pre-Adj m² (Phakisa's substitute for Mponeng's "Actual vs Planned," since no
   planned-m² column exists).
2. **Bonus Analysis** — 1. Bonus by Section · 2. Bonus by Gang Type · 3. Bonus by Component ("What the
   Bonus Is Made Of" — the real multiplicative waterfall) · 4. Bonus per m² · 5. Bonus by Parameter
   ("Who Earned What" — gang-periods classified by Netting/B-Reef/Safety-rung/AWOP outcome).
3. **Gang Analysis** — 1. Gang m² Range · 2. Gang Detail · 3. Workplace Detail · 4. Crew Performance.
4. **Labour Analysis** — 1. Crew Labour (Section checklist + Period filter, real BONUSSHIFTS data) ·
   2. Crew Drill Down (real wage-code breakdown) · 3. Best vs Worst Crews (ranked by m²/Man, with a
   selectable scatter X-axis and a m²/Man range filter).
5. **Forecast** — 1. Production vs Target · 2. Cost Efficiency · 3. Required to Hit Target · 4.
   Mid-Month Tracker · 5. Trend Forecast.
6. **Bonus Policy Simulator** — 1. Simulator (lever model, anchored to real payroll) · 2. Where the
   System Differs from the Policy (the confirmed-discrepancy findings from §3, re-surfaced as cards).

---

## 6. Working method that has worked well — keep doing this

- **Verify empirically before trusting written policy, code comments, or an earlier finding.** Nearly
  every real fact in §2–3 above was only confirmed by directly querying the database, often more than
  once with different samples — several early "findings" turned out to be fan-out/scoping artefacts on
  first pass.
- **Check for fan-out before summing anything new.** `PARTICIPANTSEARN<period>` (the raw, non-
  materialized table) repeats rows per employee per gang assignment; only the pre-aggregated
  `PARTICIPANTSDETAIL` is safe to sum directly. This exact mistake produced a ~10x overstatement during
  the Labour Analysis build before it was caught.
- **Never invent a column or figure that isn't there.** Safety Rep, Physical Cond, sick-leave detail,
  and planned-m² are all real, confirmed absences — shown as missing with a stated reason, never
  estimated.
- **Triple-check after every change**: fresh server restart, hit the new/changed API endpoints and
  confirm 200s, cross-check headline numbers against independent SQL, and re-run cross-tab
  reconciliation (Simulator vs Bonus Analysis vs Gang Detail, same scope) after anything that could
  affect more than one tab.
- The user wants direct action on routine work — verify, fix, build without asking permission at each
  step. **Do** pause before anything affecting shared/external state (pushing to git, committing real
  credentials) or a genuinely open design choice — and when told to make something "look identical" to
  a reference, check the reference's actual source/screenshots, not a prose description of it.

---

## 7. Git

Git identity: `ZanderPrinsloo <zanderprinsloo125@gmail.com>`. Repo is **public** — `.env` stays
placeholder-only (see `deploy/` and `DEPLOY.md`); never commit real SQL credentials. Create commits
only when asked; push only when asked.
