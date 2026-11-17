# Hand-off: what it actually took to port Mponeng's design into Phakisa

**For:** whoever works on this next (human or AI assistant).
**Companion to:** `HANDOFF_full_program.md` and `HANDOFF_bonus_breakdown_tables.md` (the Mponeng-side documents this port
was built from).
**Purpose:** those two documents describe Mponeng's program and assume you can translate it onto your own mine's
data. This document records everywhere that translation needed real investigation, hit a mismatch, or required
something to be built from nothing — the parts no generic hand-off could have told you in advance.

> **Read this before trusting either Mponeng hand-off document as ground truth for Phakisa.** Several claims in
> `HANDOFF_full_program.md` turned out to be slightly stale or Mponeng-specific once checked against Mponeng's own
> live source (e.g. its "no vs-prior-window KPI comparisons" claim — the real markup still carries `kpi-delta`
> scaffolding, just normally empty). Where this document and the Mponeng hand-off disagree, trust this one for
> Phakisa, and trust Mponeng's actual running source/code over its own prose hand-off for Mponeng.

---

## 1. Phakisa's real schema doesn't match Mponeng's assumptions in several places

These were found by querying Phakisa's database directly — not assumed, not carried over from Mponeng.

- **Real gang types are STOPE BREAKING, STOPE CLEANING, CENTRE GULLY, EQUIPPING, RIGGING** (verified via
  `GROUP BY GANGTYPE` over the whole of `GANGPRODUCTIONDETAIL`). Phakisa's own filter-bar dropdown had been listing
  STOPING / SURFACE ASSISTANT / ANCILLARY — none of which ever appear in the real data — and was missing RIGGING,
  which does. This was a genuine pre-existing bug, unrelated to the Mponeng port, found only because the port work
  required touching that exact part of the UI. Fixed it alongside the multi-select rebuild.
- **No "service crew" exclusion category exists at Phakisa.** Mponeng's scope note excludes two things (stoping
  miners and service/ancillary crews like tramming and engineering). Searched `INFORMATION_SCHEMA.TABLES` for
  anything matching `%MINER%`, `%SERVICE%`, `%TRAM%` — only `MINERS*` tables exist. Phakisa's scope note has one
  excluded category, not two, because the second one has no real backing data here.
- **`MINERSEARN<period>` tables exist** (confirming the Stoping Miner Incentive Scheme, policy
  JJ_202607_STPMINER_REV12, has real data behind it) **but are not materialized** the way
  `GANGPRODUCTIONDETAIL`/`PARTICIPANTSDETAIL` are — there's no `MINERSDETAIL`-style combined table. The scope note's
  excluded-miners figure is a dynamic `UNION ALL` built in Python at request time over whichever
  `MINERSEARN<period>` tables actually exist for the selected range (there are real gaps, e.g. no `202004` table) —
  checked against `INFORMATION_SCHEMA` each time, not assumed to exist for every month.
- **Phakisa's bonus is multiplicative, not additive.** Mponeng's production bonus is
  `stoping + sweepings + quality − AWOP − penalty`. Phakisa's real identity (verified against
  `EMPLOYEESTOPETEAMBONUS`/`EMPLOYEETOTALBONUS`) is
  `raw_qualifying × Netting × B-Reef × (1 + safety%)` — a sequential chain of multipliers, not a sum of parts.
  `GANGFINALSWEEPINGSBONUS` is exactly `0` on every real row checked across this entire project — there is no
  sweepings component to add at all. "What the Bonus Is Made Of" had to be built as a waterfall (raw → ×netting →
  ×B-Reef → ×safety → = paid STM) rather than a row-per-component sum, because the sum doesn't apply here.
- **`EMPLOYEEAWOPPENALTY` is real, substantial (-R3.54m across all history, ~93 of 424 recent gang-periods), and
  had never been used anywhere in the app before this round of work**, despite being available all along. Verified
  row-level that it's already netted into the real paid STM total (same as Safety and Driller), not an extra
  deduction on top — confirmed via the "pre-penalty bonus × (1 + awop%) = real STM" identity on real rows.
- **Safety, Driller and AWOP are all already inside the real paid STM total.** None of the three may ever be added
  on top of STM anywhere in this app — this was independently re-verified multiple times this project (it's the
  root cause of several earlier KPI/Simulator double-counting bugs, long before this Mponeng-port work started) and
  it shaped every new table built during the port: composition waterfalls, outcome tables, and the m² Range
  "Bonuses" picker all treat Safety/Driller/AWOP as reference-only figures with an explicit double-counting warning,
  never summed into a displayed total by default.

## 2. Places where Mponeng's structure didn't exist in Phakisa and had to be built from nothing

- **Gang Analysis → Workplace Detail.** Phakisa already had this feature, but as an inline hide/show card
  (`card-gang-prod-detail`, toggled with a `d-none` class), not a sub-tab. Converting it into a real Bootstrap pill
  sub-tab required writing a `showSubTab(navId, targetHref)` helper from scratch (Phakisa had no equivalent —
  `bootstrap.Tab.getOrCreateInstance(el).show()` wrapped for reuse) and rewiring the double-click handler and the
  "Close" button to switch sub-tabs instead of toggling visibility.
- **Gang Analysis → Crew Performance.** Did not exist in any form. Built entirely new: a backend
  `get_crew_performance()` function (per-gang aggregate across the selected periods — total m², months active,
  m²/Man, total bonus, R/m² — reusing the same STOPE BREAKING scoping and real-payroll bonus anchoring as every
  other table in this app), a new `/api/crew_performance` route, and a new Top-10-crews bar chart + detail table
  with period/sort/section filters.
- **Bonus Analysis → "Bonus by Section" had no chart**, only a table — Mponeng's version has a chart above its
  table, matching the pattern every other Bonus Analysis sub-tab already used. Added a matching stacked bar chart
  reusing `d.rows`/`d.period_keys` that the table already had, rather than a new endpoint.
- **Forecast tab.** This was the single largest piece of extra work. Phakisa's pre-existing structure was three
  separate top-level tabs (Simulation / Projection / Forecast) that didn't map 1:1 onto Mponeng's five named
  sub-tabs (Production vs Target / Cost Efficiency / Required to Hit Target / Mid-Month Tracker / Trend Forecast).
  What actually happened:
  - Investigating the existing content found that Phakisa's old "Forecast" tab *already* contained, in order, almost
    exactly Mponeng's first four sub-tabs — including a **working Mid-Month Production Tracker that already existed**
    and matched Mponeng's "scratch-pad, one row per period, required-rate-over-remaining-shifts" concept closely
    enough to reuse as-is. This was not previously known to be a Mponeng-equivalent feature — it was just Phakisa's
    own earlier work that happened to already fit.
  - The old "Projection" tab's content (historical+forecast m²/bonus charts, R/Man budget track, scenario analysis,
    section outlook, gang trajectory, forecast narrative, a second workplace drill-down) was folded wholesale into
    the new "Trend Forecast" sub-tab — broader than Mponeng's own minimal spec for that sub-tab, but kept rather
    than thrown away since it's real, working, Phakisa-specific functionality with nowhere else to go.
  - The old "Simulation" tab's content (Target Settings, KPI cards, Achievement chart, Section Summary, a full
    What-If Scenario Simulator with Interactive/Sensitivity/Goal-Seek panels, Gang-Level Simulation Detail, Budget
    Analysis) had **no Mponeng Forecast equivalent at all** and was deleted outright — "Target Settings" was kept
    only insofar as its inputs became the new shared Controls card (Period, Target m²/Gang, Target R/Man, Target
    R/m²) sitting above the five sub-tabs. The What-If Simulator specifically has no home anywhere in the app right
    now; it was working functionality and is simply gone unless it gets rebuilt somewhere (Bonus Policy Simulator
    might be the more natural home for it, since that tab already has its own what-if machinery, but nobody has
    asked for that move).
  - Mechanically, this merge involved deleting roughly 400 lines and relocating roughly 230 more within a single
    HTML file. The blocks were too large to safely delete/move with simple text-match edits, so it was done with a
    small Python script that read the file, sliced it by exact line number (found via `grep -n` on the tab-pane
    boundary comments), and wrote it back — not the usual find-and-replace approach used everywhere else in this
    project.
- **Bonus Policy Simulator → "Where the System Differs from the Policy".** Mponeng's version is built on a deep,
  dedicated investigation of its own RATES/FACTORS system-settings tables against the written policy text — work
  that has never been done for Phakisa and wasn't attempted here either (it would be its own substantial project).
  Rather than fabricate comparisons to fill out a second sub-tab, this was built **only from findings already
  independently verified elsewhere in this project**, re-surfaced as explicit policy-vs-system items:
  - Clean safety %: real average **9.47%** vs. the written flat **+10%** (§8.1), a real **R164,290.18** gap across
    406 clean Stope Breaking gangs, Jul-25–Apr-26 — computed fresh for this feature, not previously surfaced
    anywhere in the UI.
  - B-Reef stope-width uplift (§6.3): **0 of 424** real gang-periods ever show it applied, despite the written
    policy describing two active tiers.
  - Sweeping Penalty: confirmed **exactly R0** on every real row (same fact as in §1 above, re-surfaced here).
  - Netting (§6.17.5): real rate averages **exactly 1.066** — matches the written 6.6% exactly, no gap.
  - Entry-level gate (§6.8, 14.5 m²/empl): flagged as **not independently checkable** from available data, with the
    reason stated plainly, rather than guessed at.
  The sub-tab's own intro text says explicitly what hasn't been checked yet (safety-ladder tiers beyond the four
  modelled, the §6.4 Face Length factor table, the §7 width-deviation bonus table, AWOP's exact per-shift
  percentages) so nobody mistakes "5 items checked" for "the whole policy verified."

## 3. New shared backend infrastructure that didn't exist before this work

- **`_decompose_bonus_row()`** was pulled out of the Bonus Policy Simulator (where it lived as a private closure)
  into a module-level function in `web/queries.py`, specifically so the Simulator, the bonus-composition table, the
  bonus-outcomes table, and the policy-discrepancies table all run the *exact same* verified math for
  raw-qualifying/netting/B-Reef/safety decomposition. Before this, each new feature would have needed to either
  re-implement that logic (risking drift) or reach into a private closure it couldn't see.
- **`get_section_rsqm_trend()`** — per-section, per-period R/m² with a cheapest/dearest band and a ranked-by-period
  tooltip, for the Executive Summary's "Monthly Bonus Cost per m² by Section" chart. Same STOPE BREAKING scoping
  and real-payroll anchoring as everything else; Phakisa's own four sections (JJAA–JJAD), not Mponeng's.
- **`get_scope_note()`** — the dynamic multi-month `MINERSEARN` union described in §1.
- **Multi-value gang-type filtering.** `_gangtype_filter()` previously only emitted `= 'X'`. Extended (in one place)
  to accept a comma-separated list and emit `IN (...)`, which is what let the filter-bar dropdown become a genuine
  multi-select without touching every function that already called it.
- **Sticky Forecast controls.** The Forecast tab's shared Controls card pins itself just below the main pinned
  header — but that header's height isn't fixed (the scope note's excluded-miners text wraps differently depending
  on the current period range and section filter). Its `top` offset is measured against the header's actual
  rendered height via JS (`document.getElementById('sticky-top').offsetHeight`), recalculated on load, on window
  resize, and after every "Apply" refresh — not a hardcoded pixel value.

## 4. Process notes — things that went wrong before they went right

- The first attempt at the Mponeng-pattern bonus-breakdown tables was built from the *prose* hand-off document
  alone. It was rejected — not wrong in spirit, but not faithful enough to Mponeng's actual design. The fix was to
  go read Mponeng's real `web/templates/index.html` and `web/queries.py` directly and match structure from the
  source, not from a description of the source.
- Even after that, several rounds of correction were needed before the structure actually matched: Bonus Analysis's
  sub-tab order and names were wrong on the first pass (built as "Gang Type, Section, What the Bonus Is Made Of, Who
  Earned What, Bonus per m²" instead of Mponeng's actual "Section, Gang Type, Bonus by Component, Bonus per m²,
  Bonus by Parameter"); Gang Analysis was missing two of its four real sub-tabs; charts were placed beside their
  tables instead of above them in two places. All of these were caught by the person comparing live screenshots of
  both programs side by side, not by independent verification — worth doing that comparison proactively next time
  rather than waiting to be told.
- Takeaway for whoever extends this next: **when the instruction is "make it match," check the actual running
  program or its actual source, not a description of it** — prose hand-off documents (including this one and the
  two it's a companion to) are a starting point, not a verified spec.
