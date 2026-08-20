# Pakisa Mine Incentive Dashboard — Handoff Brief

Context for a fresh Claude Code session starting in the new Pakisa project folder.
Pakisa and Tshepong share the same bonus policy and bonus structure (per the mine
manager / user), so this dashboard should be built the same way — but **every
database fact below must be re-verified against Pakisa's own live data**, not
assumed. Tshepong and its sibling Doornkop looked similar on paper and still had
real structural differences once checked. Treat this doc as "what to check and
what pattern to replicate," not "facts already true of Pakisa."

**How to use this file**: copy it into the new Pakisa project folder root as
`CLAUDE.md` (Claude Code auto-loads that at session start), or just paste its
contents into the first message of the new chat. This file itself lives at
`c:\Mining_incentive_system_tshepong-main\PAKISA_HANDOFF.md` — move or copy it
out before it's easy to lose track of.

---

## 0. The single most valuable move: start from the Tshepong codebase, don't rebuild

Copy the entire Tshepong project (`C:\Mining_incentive_system_tshepong-main`) into
the new Pakisa folder as the starting point — `web/queries.py`, `web/templates/index.html`,
`web/app.py`, `run_web.py`, `config/`, `.vscode/launch.json`, `.gitignore`, `src/`.
Same policy + same bonus structure means the query logic, the Bonus Policy
Simulator's lever math, and the UI should transfer almost unchanged. What needs
to change is: `config/config.yaml` (server/database name), anything with
"Tshepong"/"STPTM4000"/"JB"-prefix hardcoded in labels or section-code
assumptions, and re-verification of every empirical fact below against Pakisa's
actual database (some things may match Tshepong exactly, some may not).

Do **not** copy `.venv/`, `data/dashboard_state.db` (that's Tshepong's local
target/scenario state), `.git/`, or `.claude/` — start those fresh in the new
folder.

---

## 1. Environment setup checklist (this machine may need all of this from scratch)

- Check Python is a **real install**, not the Windows Store stub alias:
  `Get-Command python -All` — if it only resolves to
  `...\WindowsApps\python.exe`, install via
  `winget install -e --id Python.Python.3.12 --scope user`, then refresh
  `$env:Path` in the *same* PowerShell call (session state doesn't persist
  between separate tool calls in this harness).
- Create `.venv`, install: `flask`, `pandas`, `pyodbc`, `sqlalchemy`, `pyyaml`,
  `python-dotenv`. **There is no `requirements.txt` in this codebase** — it was
  never created; dependencies were derived by reading source imports. Consider
  creating one this time around since you're setting up fresh.
- Check Git is installed (`git --version`); if not,
  `winget install -e --id Git.Git --scope user`.
- **Do not assume `localhost` is the right SQL Server instance.** On the
  Tshepong dev machine there were three local instances (default `MSSQLSERVER`,
  a named instance literally called `LOCALHOST`, and `MSSQLSERVER01`) — the
  real data was only on `MSSQLSERVER01`; plain `localhost` had empty system
  databases. Enumerate instances via `Get-Service MSSQL*` and
  `HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL`, then
  probe each with `sqlcmd -S <server> -E -Q "SELECT name FROM sys.databases"`
  to find which one actually has Pakisa's database before assuming anything.

---

## 2. Config pattern already built — reuse as-is

`config/settings.py` reads `config/config.yaml` and layers environment variable
overrides on top (12-factor pattern), and now also auto-loads a local `.env`
file via `python-dotenv` (added specifically so a colleague deploying to a
shared server, e.g. "Harmony," can override the DB target by editing one file
instead of setting real OS environment variables).

- `config/config.yaml` — `database.server`, `database.database` (the DB name,
  e.g. Tshepong's is `STPTM4000` — find Pakisa's equivalent), `use_windows_auth`.
- `.env` (gitignored, local-only — **never commit real credentials**) —
  `DB_SERVER` / `DB_USERNAME` / `DB_PASSWORD` / `DB_DATABASE`, all optional
  overrides. Setting `DB_USERNAME` is what switches the connection from
  Windows Trusted auth to SQL Server auth — see
  `DatabaseSettings.build_connection_string()`.
- Windows PowerShell 5.1 gotcha: `Out-File -Encoding utf8` / `Set-Content
  -Encoding utf8` still add a UTF-8 BOM in this environment, which silently
  breaks `python-dotenv` parsing of the first line. Write `.env` files with
  the Write tool instead, not PowerShell redirection.

---

## 3. Database structure to verify for Pakisa (this is the real work)

Tshepong's schema (STPTM4000) had three key views feeding everything:

- **`GANGPRODUCTIONDETAIL`** — one row per (gang, workplace, period). SQM +
  bonus component columns. **These bonus columns are per-person rates,
  multiplied by `GANGLABOUR`** (a rounded-up *average* headcount, not real
  headcount) to estimate a gang total — this estimate measurably diverges from
  real payroll (see §5).
- **`PARTICIPANTSDETAIL`** — one row per (employee, period). Real, actual
  individual bonus payments. **This is the canonical source of truth for
  "what was actually paid."** Columns: `EMPLOYEESTOPETEAMBONUS` ("STM" —
  confirmed to mean Efficiency + Sweeping Penalty combined, *excludes*
  Driller), `EMPLOYEESAFETYBONUS`, `EMPLOYEEDRILLERBONUS` — three
  independent, additive components.
- **`PRODUCTIONWPDETAIL`** — one row per (workplace, period). SQM detail,
  `WORKPLACERATETYPE` (category — see §4), `DIP_FACTOR` (Steep Stope
  geometry uplift — see §4).

**Verify, don't assume, for Pakisa:**
1. Do the same three views exist with the same names/columns? Run
   `INFORMATION_SCHEMA.COLUMNS` against each.
2. Does `GANGFINALBREAKBONUS` exist as an *alias* for a real column named
   something like `GANGFINALEFFICIENCYBONUS`? (At Tshepong, the view aliases
   `GANGFINALEFFICIENCYBONUS AS GANGFINALBREAKBONUS` for cross-site naming
   compatibility — the bonus is efficiency-based (m²/empl) despite the
   "break" name, confirmed against real gang rows:
   `GANGFINALEFFICIENCYBONUS = GANGFULLSTOPINGBONUS × WORKPLACENETTINGRATE ×
   B_REEF_SW_FACTOR`, exact match across 39 gangs spanning 100–600m².)
3. Is `GANGFINALSWEEPINGSBONUS` actually a **penalty** (signed, usually ≤0)
   despite the "bonus" name? Check real values.
4. Does `GANGFINALSAFETYBONUS = (safety_pct/100) × efficiency_bonus` hold
   exactly (verified on Tshepong's clean/LTI/gated rows)? If so, Safety isn't
   an independent figure, it's a slice of Efficiency.
5. Does `GANGLABOUR` (average headcount) diverge from real headcount in
   `PARTICIPANTSDETAIL` the same way? Tshepong showed a confirmed 15.6% gap
   (3,650 labour-units vs 4,220 real employee-period records for the same
   gangs/period) — this is *why* the Bonus Policy Simulator must anchor its
   dollar totals to `PARTICIPANTSDETAIL`, using `GANGPRODUCTIONDETAIL` only
   for the lever mechanics (relative ratios), never as the absolute total.
6. Do gangs span multiple workplaces within a period, causing
   `WORKPLACETOTALSQM`/`GANGFULLSTOPINGBONUS` to repeat per workplace row?
   If so, **dedupe with `MAX()` per (gang, workplace) before summing across
   workplaces** — this exact bug caused a major "Adj vs Pre-Adj m² variance"
   miscalculation at Tshepong (some sections showed -600m² phantom swings
   from double-counted workplace rows).
7. Does the `LEN(CREWNO)>=8` filter (used on `GANGPRODUCTIONDETAIL` queries)
   actually match the same rows as `CREWNO != '-'` (used on
   `PARTICIPANTSDETAIL` queries)? At Tshepong all real crewno values were
   exactly 10 characters, making the two filters equivalent — verify this
   holds for Pakisa's data rather than assuming.
8. What SQL Server instance and database name does Pakisa actually live on?
   (See §1 — don't assume `localhost`.)

---

## 4. Policy — same document applies (per user)

The current, active policy is **"Thibakotsi Stoping Incentive Scheme Cat 4-8,"
JB_202603_STPTEAM_REV04**, effective March 2026 — this superseded an earlier
"Stoping Bonus Cat 4-8" REV02 (June 2024). The user said Pakisa uses the same
policy and bonus structure, so this REV04 document should apply directly —
but get the actual Pakisa-specific PDF if one exists, in case there are
site-specific annexure rate tables or amendments.

Key structure (section numbers refer to REV04):
- §5.1 efficiency-based (m²/empl), Annexures 1–6.
- §5.2 Wide Raise → Annexure 5. Wide Raise workplaces are **exempt from the
  entry-level gate** (§5.4) — confirmed against real low-efficiency Wide
  Raise gangs at Tshepong that still earned nonzero bonus. Wide Raise status
  comes from `WORKPLACERATETYPE`, **not** `GANGTYPE`, and the two are
  orthogonal — a Wide Raise workplace can carry any `GANGTYPE`.
- §5.3 / §5.13 Netting installed to standard → +20% (`WORKPLACENETTINGRATE`
  observed as 1.2 at Tshepong).
- §5.4 entry level 14.0 m²/empl qualifying gate.
- §5.10 / §5.11 **Steep Stope**: dip 35–44° → +10%, dip ≥45° → +20% —
  **added to the square-metre figure itself** (`DIP_FACTOR`), not a Rand
  multiplier. Confirmed: `WPTOTALM2 = WPPRETOTALM2 × DIP_FACTOR` exactly.
  This means it's already baked into every m² and bonus figure derived from
  `GANGTOTALSQMADJUSTED` — it is **not** a separate lever the simulator can
  offer, because modelling a change would require re-running the changed m²
  through an external bonus-table lookup this database doesn't expose.
- §5.12 B-Reef stoping width ≥1.60m → +10% to base bonus
  (`B_REEF_SW_FACTOR` observed as 1.1 at Tshepong;
  `FACTORS.MINEBREEF_SW_ENTRY = 160` i.e. 1.60m,
  `FACTORS.MINEBREEF_SW_RATE = 1.1`).
- §5.14 Sweepings — penalty for failing standard.
- §6.1.1 / §6.1.2 **Physical Conditions Rating** (new in REV04): working
  place inspection <80% forfeits the 25% safety add-on; consecutive <80%
  triggers an additional -25%. **No database column implementing this was
  found anywhere at Tshepong** (searched every production/rates table for
  CONDITION/RATING/INSPECT — nothing). Every "clean" gang across a full year
  showed exactly +25%, zero exceptions — check the same for Pakisa before
  assuming it can/can't be modelled.
- §6.1.3–6.1.6 injury-based safety ladder: clean +25%, dressing 0%, LTI -25%,
  fatal -100%.
- §7.5 **4 categories** (not 3, as of REV04): Basal Undercut & Ledging
  (Annexures 1–2), B-Reef Open & Ledging (Annexures 3–4), Wide Raises
  (Annexure 5), **Haulage Pillar and IBG** (Annexure 6, new in REV04).
  `WORKPLACERATETYPE` values at Tshepong: `BASAL UNDERCUT BONUS`,
  `BASAL LEDGING BONUS`, `B REEF BONUS`, `B REEF LEDGING BONUS`,
  `WIDE RAISE BONUS`, `HAULAGE PILLAR IBG BONUS`,
  `HAULAGE PILLAR IBG LEDGING BONUS`.
- **The old m² tier escalator (REV02 §5.3: extra 5/10/20/30/50% for breaking
  300/400/500/600/700m²) has been removed from REV04.** Confirmed both by
  reading the policy document and empirically (payout ratios showed zero
  tier effect across every m² band in real data). Don't rebuild this lever.
- §9 has a worked example — useful for sanity-checking any formula
  reconstruction against a known input/output pair.

---

## 5. Bonus Policy Simulator — design to replicate

The simulator lets a manager change policy parameters (entry level, netting %,
B-Reef stope-width %, safety ladder) and see the Rand impact, without touching
Driller or Sweeping (kept as simple flat-% sliders — no verified rate
breakdown exists for those).

Core mechanics (see Tshepong's `get_bonus_rule_data` in `web/queries.py` for
the full implementation to copy):
1. Pull `GANGPRODUCTIONDETAIL` per-gang rows (efficiency, netting flag,
   B-Reef SW flag, safety indicators, `WORKPLACERATETYPE`-derived Wide Raise
   flag) — this supplies the *lever mechanics*.
2. Pull `PARTICIPANTSDETAIL` real totals (`total_stm_bonus`,
   `total_safety_bonus`, `total_driller_bonus`) per (section, period, gang) —
   this supplies the *real dollar anchor*.
3. **Anchor**: Driller and Safety take the real `PARTICIPANTSDETAIL` values
   directly. Efficiency and Sweep (bundled as one real "STM" figure) are
   split back apart using the `GANGPRODUCTIONDETAIL` estimate's own
   efficiency:sweep *ratio* as weights — this preserves the formula's
   relative split while anchoring the absolute total to real payroll. This
   is what makes the simulator's grand total match the "Bonus by Gang Type"
   / Bonus Analysis tab exactly for the same scope — verify this reconciles
   to the cent before considering it done.
4. Decompose the anchored Efficiency figure into a lever-independent
   `raw_qualifying` base: `raw_qualifying = anchored_efficiency /
   (netting_mult × sw_mult × safety_mult)`, then recompute forward under
   whatever lever values the user sets.
5. **Identity shortcut**: when every lever is still at its policy-default
   value, skip the formula path entirely and pass the real anchored numbers
   straight through — this guarantees "Proposed = Current" exactly (to the
   cent) at rest, and the formula only runs once the user actually changes
   something. Without this, a small structural residual (~0.3% at Tshepong,
   from genuine ambiguity in real entry-level boundary data — e.g. two gangs
   at the identical qualifying efficiency, one paid, one not) would show as
   a confusing phantom gap even when nothing had been changed.
6. Gangs already earning R0 (gated below entry level, or a fatal incident)
   can't be reconstructed upward — no data signal exists for what they
   "would have" earned. Flag and hold at R0 regardless of lever changes.

---

## 6. Naming/labeling fixes to carry over (these were real bugs, not style)

- "Break Bonus" → **"Efficiency Bonus"** everywhere (backend field names,
  frontend labels, tooltips) — the underlying mechanism is efficiency-based
  (m²/empl table lookup), not a flat rate per m² broken.
- "Sweep Bonus" → **"Sweeping Penalty"** everywhere — it's a signed penalty
  deduction (policy §5.14), not a bonus, and can be negative.
- "STM Bonus" tooltip must say **Efficiency + Sweeping Penalty**, not
  "Efficiency + Drill + Sweep" — Driller is a separate, independent
  component (verified via the raw `GANGPRODUCTIONBONUS` column = efficiency
  + sweep + safety, driller excluded).
- **R/m² scope bugs**: any tab/table that labels itself "Stope Breaking
  only" must scope the bonus numerator to the *same* gang keys as the m²
  denominator — `_get_participants_bonus()` on its own returns every gang
  type. Two real bugs were found and fixed this way (Section Ranking, R per
  m² tab) where the label promised "Stope Breaking only" but the bonus side
  silently included every gang type. Grep for "only" scope claims in the UI
  and check the backend actually delivers that scope.
- DataTables sorting: Rand-formatted cells (`"R -1 082,80"`, en-ZA locale —
  space thousands separator, comma decimal) sort *alphabetically* by
  default in DataTables, not numerically, unless you register a custom
  numeric type and apply it via `columnDefs`. This silently breaks
  "highest to lowest" sorting on every formatted numeric column, not just
  one — check any DataTables-based table in the new project for the same
  issue.
- Period dropdowns should list **newest first**, matching how `/api/periods`
  already returns them — don't let the frontend re-reverse one of two
  paired dropdowns.

---

## 7. Working method that worked well this whole project — keep doing this

- **Verify empirically before trusting written policy, code comments, or
  even your own earlier findings.** Multiple real bugs in this project were
  only caught by re-running checks with fresh, larger, or differently-
  filtered samples rather than trusting an earlier narrow spot-check.
- **Triple-check after every change**: fresh server restart, hit every API
  endpoint and confirm 200s with no server-side errors, independently
  cross-check headline numbers against hand-written SQL (not the app's own
  query logic — genuinely independent), and re-run cross-tab reconciliation
  checks (e.g. Simulator total vs Bonus Analysis total for the same scope)
  after any change that could affect them.
- The user wants direct action, not hand-holding, for routine work — install
  things, fix things, verify things without asking permission at each step.
  **Do** pause to confirm before things that affect shared/external state:
  pushing to git, or any decision that's genuinely the user's to make (e.g.
  "should we exclude this data anomaly or investigate further").
- The user is meticulous and will notice unverified claims — always
  distinguish "verified against real data" from "matches the written policy
  but unconfirmed against the database" in any explanation, and say so
  explicitly rather than implying more confidence than the evidence
  supports.

---

## 8. Git / GitHub

The user has a GitHub account (`ZanderPrinsloo`) and a separate repo per mine
(Tshepong's is `Mining_incentive_system_tshepong`). For Pakisa, expect a new
repo `Mining_incentive_system_pakisa` or similar. Git identity to use for
commits (matches existing repo history):
`ZanderPrinsloo <zanderprinsloo125@gmail.com>`. Git may not be installed on a
given machine — check `git --version` first; if missing,
`winget install -e --id Git.Git --scope user`.

Setting up a new local folder against an *existing* GitHub repo with history,
without overwriting local files: `git init`, `git remote add origin <url>`,
`git fetch origin`, `git branch -m main` (or whatever the remote's default
branch is), `git reset origin/main` (mixed reset — updates HEAD/index to
match the remote without touching working-directory files), then
`git status` shows exactly what differs locally vs the remote for review
before committing. Never `git checkout`/`reset --hard` onto files you haven't
reviewed first.
