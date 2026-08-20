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
-- sqlcmd, so it wasn't a client-driver issue, it was the view itself. Root
-- cause: every source column is stored varchar(50), so every query paid
-- TRY_CAST(...AS FLOAT) parsing on ~20 columns across 130k+ rows spread over
-- ~80 UNION ALL branches, with no index able to help because the WHERE/JOIN
-- columns were wrapped in LTRIM/RTRIM/TRY_CAST (non-SARGable). Materializing
-- into real FLOAT/trimmed-VARCHAR columns with proper indexes turns that
-- into an indexed seek instead of a repeated full-history scan-and-parse.
-- Result: the app's slowest endpoint (Forecast tab, all-history) went from
-- ~41s to ~1.5s; everything else dropped too. Re-verified financial totals
-- reconcile to the cent against independent hand-written SQL after the
-- change.
--
-- Trade-off: data is a snapshot as of the last EXEC of this procedure, not
-- live. Re-run it whenever a new monthly period table is added (the
-- original view-based version had the same requirement anyway, since it
-- also needed to pick up new period tables).
--
-- Rebuild is done as build-staging-table -> index -> atomic-swap-in (via
-- sp_rename), so a concurrently running app keeps querying the old table
-- right up until the swap, instead of ever hitting a half-built table.
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
        ('GANGFATALIND'),('BUSSUNIT');

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

    -- Lightweight check: GANGLINKEARN<period> tables usable as a join target for
    -- PARTICIPANTSDETAIL's CREWNO lookup (needs far fewer columns than @gang_periods).
    INSERT INTO @gang_join_periods (period_suffix)
    SELECT RIGHT(t.name, 6)
    FROM sys.tables t
    WHERE t.name LIKE 'GANGLINKEARN[0-9][0-9][0-9][0-9][0-9][0-9]' AND LEN(t.name) = 18
      AND (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS c WHERE c.TABLE_NAME = t.name
             AND c.COLUMN_NAME IN ('SECTION','PERIOD','GANG','CREWNO')) >= 4;

    ------------------------------------------------------------------
    -- GANGPRODUCTIONDETAIL: one row per (gang, workplace, period).
    -- Only periods present in BOTH @gang_periods and @prod_periods (the LEFT JOIN
    -- source) qualify.
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
    TRY_CAST(t1.GANGFATALIND AS FLOAT) AS GANGFATALIND
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
    -- PARTICIPANTSDETAIL: one row per (employee, period).
    -- Joined to GANGLINKEARN only where a light-schema join target exists;
    -- otherwise CREWNO falls back to 'ANCILLARY' exactly like Tshepong's ISNULL.
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
LEFT JOIN dbo.[GANGLINKEARN' + p.period_suffix + N'] t2
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
    -- PRODUCTIONWPDETAIL: one row per (workplace, period).
    -- BUSSUNIT hardcoded -- PRODUCTIONEARN carries no such column at Pakisa.
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
