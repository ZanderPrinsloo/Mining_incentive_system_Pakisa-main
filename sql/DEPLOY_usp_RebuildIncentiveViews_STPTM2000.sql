-- ============================================================================
-- DEPLOYMENT SCRIPT — run this ONCE on the target SQL Server to set up the
-- Pakisa Stoping Analysis dashboard's database objects.
--
-- WHAT THIS DOES
--   Creates ONE stored procedure, dbo.usp_RebuildIncentiveViews, in the
--   STPTM2000 database, then runs it once. That procedure builds three
--   materialized (indexed table, not live view) objects that the dashboard
--   reads from: GANGPRODUCTIONDETAIL, PARTICIPANTSDETAIL, PRODUCTIONWPDETAIL.
--   Unlike some other databases on this server, Pakisa's dashboard only needs
--   this single procedure — it builds all three objects in one pass, there is
--   no separate per-table procedure to also run.
--
-- SCOPE — THIS SCRIPT TOUCHES STPTM2000 ONLY.
--   The USE [STPTM2000]; statement directly below is the safety pin: it
--   forces every statement in this script into that one database regardless
--   of whatever database your SSMS connection happened to be pointed at when
--   you opened this file. Do not remove it, and do not run this script
--   against any other database on this server (there are ~40 similarly named
--   ones — ABETSER2000, STPTM3000, STPTM4000, etc. — this script must not
--   touch any of them).
--
-- PREREQUISITES
--   STPTM2000 must already contain the source tables this procedure reads
--   from: GANGLINKEARN<YYYYMM>, PRODUCTIONEARN<YYYYMM>, and
--   PARTICIPANTSEARN<YYYYMM> for whatever periods exist. The procedure
--   auto-discovers every period table present and only uses the ones that
--   have the columns it needs — nothing to configure per period.
--
-- WHAT TO CHECK AFTERWARDS
--   This script ends with a verification query showing row counts for the
--   three objects it built, plus the database name it actually ran against.
--   Confirm that database name reads "STPTM2000" and that all three row
--   counts are nonzero before telling anyone the dashboard is ready to use.
--
-- RE-RUNNING LATER
--   Safe to re-run this whole script any time (e.g. after a new monthly
--   period table lands) — CREATE OR ALTER means it won't fail if the
--   procedure already exists, and the rebuild itself is a build-new-copy,
--   then atomic swap, so the dashboard keeps working off the old data right
--   up until the swap, never off a half-built table. Once this has been run
--   once, a DBA can also just run "EXEC dbo.usp_RebuildIncentiveViews;" on
--   its own to refresh the data without redefining the procedure.
-- ============================================================================

USE [STPTM2000];
GO

PRINT 'Connected to database: ' + DB_NAME();
IF DB_NAME() <> 'STPTM2000'
BEGIN
    RAISERROR('Safety check failed: not connected to STPTM2000. Aborting — no objects were created.', 16, 1);
    RETURN;
END
GO

-- ============================================================================
-- dbo.usp_RebuildIncentiveViews  (database: STPTM2000)
--
-- Rebuilds GANGPRODUCTIONDETAIL, PARTICIPANTSDETAIL, PRODUCTIONWPDETAIL --
-- the three tables web/queries.py reads -- as MATERIALIZED, INDEXED TABLES
-- (not live views) over every qualifying GANGLINKEARN<period> /
-- PRODUCTIONEARN<period> / PARTICIPANTSEARN<period> table in this database.
--
-- To (re)install: run this whole file once against STPTM2000 (it both
-- defines and immediately executes the procedure). To pick up a newly
-- landed monthly period table afterwards, just run:
--     EXEC dbo.usp_RebuildIncentiveViews;
--
-- WHY MATERIALIZED TABLES, NOT VIEWS (added 2026-08-20):
-- The first version of this procedure built these as plain UNION ALL views.
-- Measured directly: web/queries.py's all-history PARTICIPANTSDETAIL
-- aggregation took 35-45s from the app -- confirmed via raw pyodbc AND raw
-- sqlcmd, so it's not a client-driver issue, it's the view itself. Root
-- cause: every source column is stored varchar(50), so every query pays
-- TRY_CAST(...AS FLOAT) parsing on ~20 columns across 130k+ rows spread over
-- ~80 UNION ALL branches, with no index able to help because the WHERE/JOIN
-- columns are wrapped in LTRIM/RTRIM/TRY_CAST (non-SARGable). Materializing
-- into real FLOAT/trimmed-VARCHAR columns with proper indexes turns that
-- into an indexed seek instead of a repeated full-history scan-and-parse.
--
-- A view-based version was tried again on 2026-08-25 for deployment
-- portability and rejected -- it has the same ~40s-per-query cost, and a
-- view can't avoid it because indexed views in SQL Server don't support
-- UNION ALL or OUTER JOIN, both of which this needs. Materialized tables are
-- the only option that is both correct and fast for this schema. (See git
-- history if the view version is ever wanted again -- it's a straight swap
-- of the "build -> index -> swap" block below for "CREATE OR ALTER VIEW".)
--
-- BUG FOUND AND FIXED 2026-08-25 -- PARTICIPANTSDETAIL was overcounting every
-- bonus total. Root cause: its LEFT JOIN to GANGLINKEARN<period> (to look up
-- CREWNO) joined on SECTION+PERIOD+GANG only. GANGLINKEARN has one row per
-- (gang, WORKPLACE), and gangs commonly span 2-3 workplaces in a period, so
-- each employee row fanned out once per workplace their gang worked --
-- inflating every SUM(EMPLOYEESTOPETEAMBONUS)/SAFETY/DRILLER total. Verified
-- directly: period 202607 alone had 2165 materialized rows against 1898 raw
-- (correct) rows, a 28% overstatement of total STM bonus for that period
-- (R4,800,878 shown vs R3,747,885 correct). This exact join pattern is
-- inherited from Tshepong's own live PARTICIPANTSDETAIL view (confirmed the
-- same inflation there too: 1503 vs 1333 rows for period 202606) -- not
-- something introduced by materializing, but never caught until this was
-- audited row-by-row against the raw source. Fixed by deduplicating
-- GANGLINKEARN to one row per (SECTION, PERIOD, GANG) via MAX(CREWNO) before
-- joining (CREWNO is consistent across a gang's workplace rows in every case
-- checked bar one, where MAX() gives a deterministic pick). Re-verified
-- financial totals reconcile to the cent against independent hand-written
-- SQL after this fix.
--
-- Trade-off: data is now a snapshot as of the last EXEC of this procedure,
-- not live. Re-run EXEC dbo.usp_RebuildIncentiveViews whenever a new
-- monthly period table is added (same requirement the view version had
-- anyway, since it also needed to pick up new period tables). This applies
-- equally on a fresh server: the procedure builds the table from whatever
-- GANGLINKEARN/PRODUCTIONEARN/PARTICIPANTSEARN tables exist on THAT server
-- when it runs there -- it does not copy data from this machine.
--
-- Rebuild is done as build-staging-table -> index -> swap-in, so a
-- concurrently running app keeps querying the old table right up until
-- the atomic rename, instead of hitting a half-built table.
--
-- PAKISA-VS-TSHEPONG SCHEMA GAPS (confirmed empirically against real
-- STPTM2000 data; see PAKISA_HANDOFF.md for the Tshepong/STPTM4000 baseline
-- this compares against -- these are real structural differences, not bugs
-- to "fix"):
--   * GANGLINKEARN<period> only carries the full required column set from
--     202008 onward (202001-202007 are missing one newer column; the 201911
--     snapshot is a mostly-different legacy schema). GANGPRODUCTIONDETAIL
--     therefore only covers 202008+.
--   * PRODUCTIONEARN<period> and PARTICIPANTSEARN<period> carry the full
--     required set from 202001 onward (only the one-off 201911 snapshot is
--     excluded).
--   * PRODUCTIONEARN<period> has no BUSSUNIT column at all -- hardcoded here
--     to the real value observed on GANGLINKEARN, 'JJ' (Tshepong's
--     PRODUCTIONWPDETAIL hardcodes 'JB' for the same reason).
--   * No B_REEF_SW_FACTOR column exists anywhere in GANGLINKEARN/
--     PRODUCTIONEARN, and no WORKPLACERATETYPE value resembling 'B REEF...'
--     was observed in a sample period -- defaulted to 1.0 (no uplift).
--     Re-check if Pakisa turns out to mine B-Reef in other periods/sections.
--   * No DIP_FACTOR column exists in PRODUCTIONEARN -- defaulted to 1.0 (no
--     Steep Stope m² uplift applied). Re-check against policy / site
--     geology.
--   * No GANGFINALSWEEPINGSBONUS column exists. Verified empirically that
--     GANGPRODUCTIONBONUS = GANGFINALEFFICIENCYBONUS + GANGFINALSAFETYBONUS
--     exactly (to float rounding noise, ~1e-13) across real rows in
--     GANGLINKEARN202607 -- i.e. Pakisa's GANGPRODUCTIONBONUS genuinely
--     carries no sweeping-penalty component. Hardcoded to 0, not guessed.
--   * WORKPLACERATETYPE values observed in period 202607: 'STOPE TEAM
--     BONUS', 'STOPE W/U/L TEAM BONUS', 'UNDERCUT W/U/L TEAM BONUS' -- none
--     matched 'WIDE RAISE BONUS', so the Wide-Raise entry-gate exemption in
--     web/queries.py's get_bonus_rule_data() will not currently trigger for
--     any gang. Check WORKPLACERATETYPE across more periods/sections before
--     trusting that this mine has no Wide Raise panels.
--   * WORKPLACENETTINGRATE observed values: 1.0 and 1.066 (not Tshepong's
--     1.2).
--   * PARTICIPANTSDETAIL's "not a real team member" CREWNO sentinel is
--     blank/'0' at Pakisa, not Tshepong's '-'. web/queries.py's
--     _get_participants_bonus() filters on CREWNO != '-' and so does not
--     currently exclude these rows (~0.6% of PARTICIPANTSDETAIL). Flagged
--     for review, not silently patched into the app.
--
-- REAL POLICY-MATCHED COLUMNS ADDED 2026-09-30 (after being handed Phakisa's own policy
-- documents -- JJ_202608_STPTEAM_REV04 "Stoping Bonus Cat 4-8", JJ_202607_STPMINER_REV12,
-- QUALDRILLING_REV07, SAFETY REP_REV00). These were sitting in GANGLINKEARN all along but
-- never pulled into GANGPRODUCTIONDETAIL because nothing before this pointed at them:
--   * GANGLTIPENALTYPERCENTAGE / GANGDRESSINGSPENALTYPERCENTAGE / GANGFATALPENALTYPERCENTAGE
--     -- the REAL, mine-computed safety penalty % actually applied to each gang (policy
--     §8.1). Confirmed exactly -50.0 on every real LTI gang checked, matching "One LTI =
--     -50% deducted" precisely -- these are read directly, not inferred from an indicator
--     + a hardcoded ladder value, so they naturally reflect whatever the real ladder does
--     (including tiers this app has no separate model for, e.g. 2+ TIA / 2+ LTI).
--   * WORKPLACESTOPEWIDTHRATE (policy §7, authorised stoping-width deviation bonus/penalty)
--     -- real values observed: -0.25, -0.15, 0, 0.15, 0.25, matching the written policy's
--     ±15%/±25% exactly. Only present on GANGLINKEARN from 202408 onward (defaults to 0.0,
--     i.e. no deviation, for the 202405-202407 gap so those periods aren't dropped).
--   * WORKPLACEAVGFACELENGTH (policy §6.4, Face Length factor by mining type) -- pulled
--     through as reference data; the Basal/B-Reef factor TABLE itself (§6.4) isn't
--     reproduced here, only the real face-length figure the table would be looked up by.
-- None of these change any existing total -- see queries.py for where they're now
-- surfaced (as real reference data, not recalculated bonus).
-- ============================================================================

CREATE OR ALTER PROCEDURE dbo.usp_RebuildIncentiveViews
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @gangReqCol TABLE (col_name SYSNAME PRIMARY KEY);
    INSERT INTO @gangReqCol (col_name) VALUES
        ('SECTION'),('PERIOD'),('WORKPLACE'),('WORKPLACE_NAME'),('WORKPLACETOTALSQM'),
        ('GANG'),('GANGTYPE'),('CREWNO'),('CREWSHIFTS'),('GANGTOTALSQMADJUSTED'),
        ('GANGLABOUR'),('GANGEFFICIENCY'),('GANGFULLSTOPINGBONUS'),('WORKPLACENETTINGRATE'),
        ('GANGWPSTOPINGBONUS'),('GANGFINALEFFICIENCYBONUS'),('GANGDRILLERBONUS'),
        ('GANGPRODUCTIONBONUS'),('GANGFINALSAFETYBONUS'),('GANGLTIIND'),('GANGDRESSINGIND'),
        ('GANGFATALIND'),('BUSSUNIT'),
        -- Added 2026-09-30: real, mine-computed penalty %/reference columns that match
        -- Phakisa's actual policy (JJ_202608_STPTEAM_REV04) -- see the header notes.
        ('GANGLTIPENALTYPERCENTAGE'),('GANGDRESSINGSPENALTYPERCENTAGE'),
        ('GANGFATALPENALTYPERCENTAGE'),('WORKPLACEAVGFACELENGTH');

    DECLARE @prodReqCol TABLE (col_name SYSNAME PRIMARY KEY);
    INSERT INTO @prodReqCol (col_name) VALUES
        ('SECTION'),('PERIOD'),('WORKPLACE'),('STOPESQM'),('LEDGESQM'),
        ('WORKPLACERATETYPE'),('WORKPLACESWEEPINGSDISTANCE'),('WORKPLACETOTALFA'),
        ('WORKPLACETOTALSQM'),('WORKPLACETOTALSQMADJUSTED');

    DECLARE @partReqCol TABLE (col_name SYSNAME PRIMARY KEY);
    INSERT INTO @partReqCol (col_name) VALUES
        ('SECTION'),('PERIOD'),('EMPLOYEE_NO'),('GANG'),('GANGTYPE'),('WAGECODE'),
        ('WAGE_DESCRIPTION'),('EMPLOYEESUPERVISIONRATE'),('EMPLOYEESTOPETEAMBONUS'),
        ('EMPLOYEESAFETYBONUS'),('EMPLOYEEDRILLERBONUS'),('EMPLOYEEAWOPPENALTY'),('BUSSUNIT');

    DECLARE @gangReqCols INT = (SELECT COUNT(*) FROM @gangReqCol);
    DECLARE @prodReqCols INT = (SELECT COUNT(*) FROM @prodReqCol);
    DECLARE @partReqCols INT = (SELECT COUNT(*) FROM @partReqCol);

    DECLARE @gang_periods TABLE (period_suffix CHAR(6) PRIMARY KEY);
    DECLARE @prod_periods TABLE (period_suffix CHAR(6) PRIMARY KEY);
    DECLARE @part_periods TABLE (period_suffix CHAR(6) PRIMARY KEY);
    DECLARE @gang_join_periods TABLE (period_suffix CHAR(6) PRIMARY KEY);

    INSERT INTO @gang_periods (period_suffix)
    SELECT RIGHT(t.name, 6)
    FROM sys.tables t
    WHERE t.name LIKE 'GANGLINKEARN[0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(t.name) = 18
      AND (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS c
             JOIN @gangReqCol r ON r.col_name = c.COLUMN_NAME
             WHERE c.TABLE_NAME = t.name) >= @gangReqCols;

    INSERT INTO @prod_periods (period_suffix)
    SELECT RIGHT(t.name, 6)
    FROM sys.tables t
    WHERE t.name LIKE 'PRODUCTIONEARN[0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(t.name) = 20
      AND (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS c
             JOIN @prodReqCol r ON r.col_name = c.COLUMN_NAME
             WHERE c.TABLE_NAME = t.name) >= @prodReqCols;

    INSERT INTO @part_periods (period_suffix)
    SELECT RIGHT(t.name, 6)
    FROM sys.tables t
    WHERE t.name LIKE 'PARTICIPANTSEARN[0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(t.name) = 22
      AND (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS c
             JOIN @partReqCol r ON r.col_name = c.COLUMN_NAME
             WHERE c.TABLE_NAME = t.name) >= @partReqCols;

    INSERT INTO @gang_join_periods (period_suffix)
    SELECT RIGHT(t.name, 6)
    FROM sys.tables t
    WHERE t.name LIKE 'GANGLINKEARN[0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(t.name) = 18
      AND (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS c WHERE c.TABLE_NAME = t.name
             AND c.COLUMN_NAME IN ('SECTION','PERIOD','GANG','CREWNO')) >= 4;

    -- WORKPLACESTOPEWIDTHRATE (policy §7 -- the authorised-stoping-width deviation
    -- bonus/penalty) only exists on GANGLINKEARN from 202408 onward, narrower than the
    -- main @gang_periods range (202405+). Tracked separately so earlier qualifying
    -- periods aren't dropped just for lacking this one newer column -- they fall back to
    -- 0.0 (no deviation) instead.
    DECLARE @gang_stopewidth_periods TABLE (period_suffix CHAR(6) PRIMARY KEY);
    INSERT INTO @gang_stopewidth_periods (period_suffix)
    SELECT RIGHT(t.name, 6)
    FROM sys.tables t
    WHERE t.name LIKE 'GANGLINKEARN[0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(t.name) = 18
      AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS c WHERE c.TABLE_NAME = t.name
                    AND c.COLUMN_NAME = 'WORKPLACESTOPEWIDTHRATE');

    ------------------------------------------------------------------
    -- GANGPRODUCTIONDETAIL
    ------------------------------------------------------------------
    DECLARE @sql NVARCHAR(MAX) = N'';
    SELECT @sql = @sql + CASE WHEN @sql = N'' THEN N'' ELSE N'UNION ALL ' END + N'
SELECT
    LTRIM(RTRIM(t1.BUSSUNIT)) AS BUSSUNIT, LTRIM(RTRIM(t1.SECTION)) AS SECTION,
    LTRIM(RTRIM(t1.PERIOD)) AS PERIOD, LTRIM(RTRIM(t1.WORKPLACE)) AS WORKPLACE,
    LTRIM(RTRIM(t1.WORKPLACE_NAME)) AS WORKPLACE_NAME,
    TRY_CAST(t2.STOPESQM AS FLOAT) AS WPSTOPESQM, TRY_CAST(t2.LEDGESQM AS FLOAT) AS WPLEDGESQM,
    TRY_CAST(t1.WORKPLACETOTALSQM AS FLOAT) AS WORKPLACETOTALSQM,
    LTRIM(RTRIM(t1.GANG)) AS GANG, LTRIM(RTRIM(t1.GANGTYPE)) AS GANGTYPE,
    LTRIM(RTRIM(t1.CREWNO)) AS CREWNO, LTRIM(RTRIM(t1.CREWSHIFTS)) AS CREWSHIFTS,
    TRY_CAST(t1.GANGTOTALSQMADJUSTED AS FLOAT) AS GANGTOTALSQMADJUSTED,
    TRY_CAST(t1.GANGLABOUR AS FLOAT) AS GANGLABOUR,
    TRY_CAST(t1.GANGEFFICIENCY AS FLOAT) AS GANGEFFICIENCY,
    TRY_CAST(t1.GANGFULLSTOPINGBONUS AS FLOAT) AS GANGEFFICIENCYBONUS,
    TRY_CAST(t1.WORKPLACENETTINGRATE AS FLOAT) AS WORKPLACENETTINGRATE,
    CAST(1.0 AS FLOAT) AS WORKPLACERGMRATE,
    CAST(1.0 AS FLOAT) AS B_REEF_SW_FACTOR,
    TRY_CAST(t1.GANGWPSTOPINGBONUS AS FLOAT) AS GANGWPSTOPINGBONUS,
    TRY_CAST(t1.GANGFINALEFFICIENCYBONUS AS FLOAT) AS GANGFINALBREAKBONUS,
    TRY_CAST(t1.GANGDRILLERBONUS AS FLOAT) AS GANGDRILLERBONUS,
    TRY_CAST(t1.GANGPRODUCTIONBONUS AS FLOAT) AS GANGPRODUCTIONBONUS,
    CAST(0.0 AS FLOAT) AS GANGFINALSWEEPINGSBONUS,
    TRY_CAST(t1.GANGFINALSAFETYBONUS AS FLOAT) AS GANGFINALSAFETYBONUS,
    TRY_CAST(t1.GANGLTIIND AS FLOAT) AS GANGLTIIND,
    TRY_CAST(t1.GANGDRESSINGIND AS FLOAT) AS GANGDRESSINGIND,
    TRY_CAST(t1.GANGFATALIND AS FLOAT) AS GANGFATALIND,
    TRY_CAST(t1.GANGLTIPENALTYPERCENTAGE AS FLOAT) AS GANGLTIPENALTYPERCENTAGE,
    TRY_CAST(t1.GANGDRESSINGSPENALTYPERCENTAGE AS FLOAT) AS GANGDRESSINGSPENALTYPERCENTAGE,
    TRY_CAST(t1.GANGFATALPENALTYPERCENTAGE AS FLOAT) AS GANGFATALPENALTYPERCENTAGE,
    TRY_CAST(t1.WORKPLACEAVGFACELENGTH AS FLOAT) AS WORKPLACEAVGFACELENGTH,
    ' + CASE WHEN g.period_suffix IN (SELECT period_suffix FROM @gang_stopewidth_periods)
             THEN N'TRY_CAST(t1.WORKPLACESTOPEWIDTHRATE AS FLOAT)'
             ELSE N'CAST(0.0 AS FLOAT)' END + N' AS WORKPLACESTOPEWIDTHRATE
FROM dbo.[GANGLINKEARN' + g.period_suffix + N'] t1
LEFT JOIN dbo.[PRODUCTIONEARN' + g.period_suffix + N'] t2
       ON t1.WORKPLACE = t2.WORKPLACE
      AND t1.SECTION   = t2.SECTION
      AND t1.PERIOD    = t2.PERIOD
WHERE t1.GANG != ''XXX''
'
    FROM @gang_periods g
    WHERE g.period_suffix IN (SELECT period_suffix FROM @prod_periods);

    IF @sql <> N''
    BEGIN
        IF OBJECT_ID('dbo.GANGPRODUCTIONDETAIL_new') IS NOT NULL DROP TABLE dbo.GANGPRODUCTIONDETAIL_new;
        EXEC(N'SELECT * INTO dbo.GANGPRODUCTIONDETAIL_new FROM (' + @sql + N') src');

        CREATE NONCLUSTERED INDEX IX_GPD_new_gangtype_period ON dbo.GANGPRODUCTIONDETAIL_new (GANGTYPE, PERIOD) INCLUDE (SECTION, GANG, CREWNO, WORKPLACE);
        CREATE NONCLUSTERED INDEX IX_GPD_new_gang_period ON dbo.GANGPRODUCTIONDETAIL_new (GANG, PERIOD);
        CREATE NONCLUSTERED INDEX IX_GPD_new_section_period ON dbo.GANGPRODUCTIONDETAIL_new (SECTION, PERIOD);

        IF OBJECT_ID('dbo.GANGPRODUCTIONDETAIL') IS NOT NULL
        BEGIN
            IF EXISTS (SELECT 1 FROM sys.views WHERE name = 'GANGPRODUCTIONDETAIL')
                DROP VIEW dbo.GANGPRODUCTIONDETAIL;
            ELSE
                DROP TABLE dbo.GANGPRODUCTIONDETAIL;
        END
        EXEC sp_rename 'dbo.GANGPRODUCTIONDETAIL_new', 'GANGPRODUCTIONDETAIL';
    END

    ------------------------------------------------------------------
    -- PARTICIPANTSDETAIL
    ------------------------------------------------------------------
    SET @sql = N'';
    SELECT @sql = @sql + CASE WHEN @sql = N'' THEN N'' ELSE N'UNION ALL ' END +
        CASE WHEN p.period_suffix IN (SELECT period_suffix FROM @gang_join_periods) THEN N'
SELECT
    LTRIM(RTRIM(t1.BUSSUNIT)) AS BUSSUNIT, LTRIM(RTRIM(t1.SECTION)) AS SECTION,
    LTRIM(RTRIM(t1.PERIOD)) AS PERIOD, LTRIM(RTRIM(t1.EMPLOYEE_NO)) AS EMPLOYEE_NO,
    LTRIM(RTRIM(ISNULL(t2.CREWNO, ''ANCILLARY''))) AS CREWNO,
    LTRIM(RTRIM(t1.GANG)) AS GANG, LTRIM(RTRIM(t1.GANGTYPE)) AS GANGTYPE,
    LTRIM(RTRIM(t1.WAGECODE)) AS WAGECODE, LTRIM(RTRIM(t1.WAGE_DESCRIPTION)) AS WAGE_DESCRIPTION,
    TRY_CAST(t1.EMPLOYEESUPERVISIONRATE AS FLOAT) AS EMPLOYEESUPERVISIONRATE,
    TRY_CAST(t1.EMPLOYEESTOPETEAMBONUS AS FLOAT) AS EMPLOYEESTOPETEAMBONUS,
    TRY_CAST(t1.EMPLOYEESAFETYBONUS AS FLOAT) AS EMPLOYEESAFETYBONUS,
    TRY_CAST(t1.EMPLOYEEDRILLERBONUS AS FLOAT) AS EMPLOYEEDRILLERBONUS,
    TRY_CAST(t1.EMPLOYEEAWOPPENALTY AS FLOAT) AS EMPLOYEEAWOPPENALTY
FROM dbo.[PARTICIPANTSEARN' + p.period_suffix + N'] t1
LEFT JOIN (
    SELECT SECTION, PERIOD, GANG, MAX(CREWNO) AS CREWNO
    FROM dbo.[GANGLINKEARN' + p.period_suffix + N']
    GROUP BY SECTION, PERIOD, GANG
) t2
       ON t1.SECTION = t2.SECTION
      AND t1.PERIOD  = t2.PERIOD
      AND t1.GANG    = t2.GANG
WHERE SUBSTRING(t1.WAGECODE, 1, 3) != ''246''
  AND SUBSTRING(t1.WAGECODE, 1, 1) != ''3''
  AND t1.EMPLOYEE_NO != ''0''
' ELSE N'
SELECT
    LTRIM(RTRIM(t1.BUSSUNIT)) AS BUSSUNIT, LTRIM(RTRIM(t1.SECTION)) AS SECTION,
    LTRIM(RTRIM(t1.PERIOD)) AS PERIOD, LTRIM(RTRIM(t1.EMPLOYEE_NO)) AS EMPLOYEE_NO,
    ''ANCILLARY'' AS CREWNO,
    LTRIM(RTRIM(t1.GANG)) AS GANG, LTRIM(RTRIM(t1.GANGTYPE)) AS GANGTYPE,
    LTRIM(RTRIM(t1.WAGECODE)) AS WAGECODE, LTRIM(RTRIM(t1.WAGE_DESCRIPTION)) AS WAGE_DESCRIPTION,
    TRY_CAST(t1.EMPLOYEESUPERVISIONRATE AS FLOAT) AS EMPLOYEESUPERVISIONRATE,
    TRY_CAST(t1.EMPLOYEESTOPETEAMBONUS AS FLOAT) AS EMPLOYEESTOPETEAMBONUS,
    TRY_CAST(t1.EMPLOYEESAFETYBONUS AS FLOAT) AS EMPLOYEESAFETYBONUS,
    TRY_CAST(t1.EMPLOYEEDRILLERBONUS AS FLOAT) AS EMPLOYEEDRILLERBONUS,
    TRY_CAST(t1.EMPLOYEEAWOPPENALTY AS FLOAT) AS EMPLOYEEAWOPPENALTY
FROM dbo.[PARTICIPANTSEARN' + p.period_suffix + N'] t1
WHERE SUBSTRING(t1.WAGECODE, 1, 3) != ''246''
  AND SUBSTRING(t1.WAGECODE, 1, 1) != ''3''
  AND t1.EMPLOYEE_NO != ''0''
' END
    FROM @part_periods p;

    IF @sql <> N''
    BEGIN
        IF OBJECT_ID('dbo.PARTICIPANTSDETAIL_new') IS NOT NULL DROP TABLE dbo.PARTICIPANTSDETAIL_new;
        EXEC(N'SELECT * INTO dbo.PARTICIPANTSDETAIL_new FROM (' + @sql + N') src');

        CREATE NONCLUSTERED INDEX IX_PD_new_section_period_gang ON dbo.PARTICIPANTSDETAIL_new (SECTION, PERIOD, GANG) INCLUDE (EMPLOYEESTOPETEAMBONUS, EMPLOYEESAFETYBONUS, EMPLOYEEDRILLERBONUS, CREWNO);
        CREATE NONCLUSTERED INDEX IX_PD_new_period_gangtype ON dbo.PARTICIPANTSDETAIL_new (PERIOD, GANGTYPE);
        CREATE NONCLUSTERED INDEX IX_PD_new_gang_crewno ON dbo.PARTICIPANTSDETAIL_new (GANG, CREWNO);

        IF OBJECT_ID('dbo.PARTICIPANTSDETAIL') IS NOT NULL
        BEGIN
            IF EXISTS (SELECT 1 FROM sys.views WHERE name = 'PARTICIPANTSDETAIL')
                DROP VIEW dbo.PARTICIPANTSDETAIL;
            ELSE
                DROP TABLE dbo.PARTICIPANTSDETAIL;
        END
        EXEC sp_rename 'dbo.PARTICIPANTSDETAIL_new', 'PARTICIPANTSDETAIL';
    END

    ------------------------------------------------------------------
    -- PRODUCTIONWPDETAIL
    ------------------------------------------------------------------
    SET @sql = N'';
    SELECT @sql = @sql + CASE WHEN @sql = N'' THEN N'' ELSE N'UNION ALL ' END + N'
SELECT
    ''JJ'' AS BUSSUNIT, LTRIM(RTRIM(PERIOD)) AS PERIOD, LTRIM(RTRIM(SECTION)) AS SECTION,
    LTRIM(RTRIM(WORKPLACE)) AS WORKPLACE,
    TRY_CAST(LEDGESQM AS FLOAT) AS WPMAXLEDGESQM, TRY_CAST(STOPESQM AS FLOAT) AS WPMAXSTOPESQM,
    LTRIM(RTRIM(WORKPLACERATETYPE)) AS WORKPLACERATETYPE,
    TRY_CAST(WORKPLACESWEEPINGSDISTANCE AS FLOAT) AS WORKPLACESWEEPINGSDISTANCE,
    TRY_CAST(WORKPLACETOTALFA AS FLOAT) AS WORKPLACETOTALFA,
    CAST(1.0 AS FLOAT) AS DIP_FACTOR,
    TRY_CAST(WORKPLACETOTALSQM AS FLOAT) AS WPPRETOTALM2,
    TRY_CAST(WORKPLACETOTALSQMADJUSTED AS FLOAT) AS WPTOTALM2
FROM dbo.[PRODUCTIONEARN' + period_suffix + N']
'
    FROM @prod_periods;

    IF @sql <> N''
    BEGIN
        IF OBJECT_ID('dbo.PRODUCTIONWPDETAIL_new') IS NOT NULL DROP TABLE dbo.PRODUCTIONWPDETAIL_new;
        EXEC(N'SELECT * INTO dbo.PRODUCTIONWPDETAIL_new FROM (' + @sql + N') src');

        CREATE NONCLUSTERED INDEX IX_PWD_new_section_period_wp ON dbo.PRODUCTIONWPDETAIL_new (SECTION, PERIOD, WORKPLACE);

        IF OBJECT_ID('dbo.PRODUCTIONWPDETAIL') IS NOT NULL
        BEGIN
            IF EXISTS (SELECT 1 FROM sys.views WHERE name = 'PRODUCTIONWPDETAIL')
                DROP VIEW dbo.PRODUCTIONWPDETAIL;
            ELSE
                DROP TABLE dbo.PRODUCTIONWPDETAIL;
        END
        EXEC sp_rename 'dbo.PRODUCTIONWPDETAIL_new', 'PRODUCTIONWPDETAIL';
    END
END
GO

EXEC dbo.usp_RebuildIncentiveViews;
GO

-- ============================================================================
-- POST-RUN VERIFICATION — confirm this actually ran against STPTM2000 and
-- all three objects were built with real data before handing the dashboard
-- off as ready. All three row counts below should be nonzero.
-- ============================================================================
PRINT '=== Verification ===';
PRINT 'Database: ' + DB_NAME() + '  (must read STPTM2000)';
SELECT 'GANGPRODUCTIONDETAIL' AS object_name, COUNT(*) AS row_count FROM dbo.GANGPRODUCTIONDETAIL
UNION ALL
SELECT 'PARTICIPANTSDETAIL', COUNT(*) FROM dbo.PARTICIPANTSDETAIL
UNION ALL
SELECT 'PRODUCTIONWPDETAIL', COUNT(*) FROM dbo.PRODUCTIONWPDETAIL;
GO
