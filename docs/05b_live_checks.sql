-- Part 2 of 3 — paste this WHOLE file into BigQuery and click Run.
-- Creates sp_health_live() - the every-30-minutes checks.
-- Run parts in order: 1, then 2, then 3.

-- ############################################################################
-- ##  LIVE CHECKS — schedule every 30 min, 24/7                             ##
-- ##  CALL `...breakout_room_calibrator.sp_health_live`();                   ##
-- ############################################################################
CREATE OR REPLACE PROCEDURE
`verve-attendance-tracker.breakout_room_calibrator.sp_health_live`()
BEGIN

  DECLARE run_ts   TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE run_ist  STRING    DEFAULT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', CURRENT_TIMESTAMP(), 'Asia/Kolkata');
  DECLARE biz_date DATE      DEFAULT DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata');
  DECLARE ist_hour INT64     DEFAULT EXTRACT(HOUR FROM CURRENT_TIMESTAMP() AT TIME ZONE 'Asia/Kolkata');

  -- Checks that depend on PEOPLE being online skip when nobody is expected.
  -- Check 33 deliberately does NOT use this: the scheduled query runs every
  -- 5 minutes whether or not anyone is in the meeting, so a job that breaks
  -- on Saturday night should be visible on Saturday night.
  DECLARE work_hours BOOL DEFAULT
    (EXTRACT(HOUR FROM CURRENT_TIMESTAMP() AT TIME ZONE 'Asia/Kolkata') BETWEEN 9 AND 23)
    AND (EXTRACT(DAYOFWEEK FROM DATE(CURRENT_TIMESTAMP(), 'Asia/Kolkata')) != 1);

  ---------------------------------------------------------------------------
  -- 33  BUILD JOB HEALTH — replaces the old freshness check (01), which
  --     inferred pipeline health from the DATA looking recent. This reads
  --     BigQuery's own job history instead, which is better in three ways:
  --     it hands you the actual error text, it goes red on the first failed
  --     run instead of after 20 minutes of staleness, and it has no
  --     work-hours blind spot.
  --
  --     Written as "when was the last SUCCESSFUL run", NOT "did the last run
  --     error". A paused or deleted scheduled query produces no job rows at
  --     all — the WHERE-did-it-error phrasing would report OK forever.
  --
  --     KNOWN GAP (accepted): a build that succeeds but writes zero rows
  --     looks green here. Row counts for a procedure CALL are not reliably
  --     exposed in job metadata, so this is not covered by anything.
  --
  --     Both build scheduled queries (5-min today, 06:00 yesterday) CALL the
  --     same procedure and are indistinguishable in job metadata, so for up
  --     to ~20 min after 06:00 the daily job can mask a dead 5-min job.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH last_run AS (
    -- ONLY the most recent run, per the 2026-08-10 decision: if the latest
    -- scheduled build succeeded and is recent, everything upstream is fine.
    -- Counting historical failures added noise without adding information.
    --
    -- statement_type = 'CALL' is what makes this specific. Filtering on the
    -- query text alone also matched hand-run console queries — including
    -- this health-check file itself, whose action strings mention
    -- sp_build_presence_intervals — so a failed manual paste was reported
    -- as a failed build. The scheduled query is a CALL; nothing else is.
    SELECT end_time, error_result.message AS err
    FROM `verve-attendance-tracker`.`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE creation_time >= TIMESTAMP_SUB(run_ts, INTERVAL 6 HOUR)
      AND parent_job_id IS NULL
      AND state = 'DONE'
      AND statement_type = 'CALL'
      AND query LIKE '%sp_build_presence_intervals%'
    ORDER BY end_time DESC
    LIMIT 1
  ),
  v AS (
    SELECT (SELECT end_time FROM last_run) AS t,
           (SELECT err      FROM last_run) AS err
  )
  SELECT run_ts, run_ist, biz_date, '33', 'Build job health', 'today',
    CASE WHEN t IS NULL                              THEN 'ALARM'
         WHEN err IS NOT NULL                        THEN 'ALARM'
         WHEN TIMESTAMP_DIFF(run_ts, t, MINUTE) > 20 THEN 'ALARM'
         WHEN TIMESTAMP_DIFF(run_ts, t, MINUTE) > 12 THEN 'WARN'
         ELSE 'OK' END,
    CAST(TIMESTAMP_DIFF(run_ts, t, MINUTE) AS FLOAT64),
    'the latest build ran within the last 20 min and succeeded (runs every 5 min, 24/7)',
    CASE
      WHEN t IS NULL THEN
        'NO build job ran at all in the last 6 hours — the scheduled query is paused or deleted'
      WHEN err IS NOT NULL THEN
        CONCAT('the latest build FAILED ', CAST(TIMESTAMP_DIFF(run_ts, t, MINUTE) AS STRING),
               ' min ago. Error: ', err)
      ELSE CONCAT('latest build succeeded ', CAST(TIMESTAMP_DIFF(run_ts, t, MINUTE) AS STRING), ' min ago')
    END,
    'Open BigQuery > Scheduled queries > presence-intervals-today. If it is paused, resume it; if it is failing, the error above is the fix. Manual catch-up: CALL sp_build_presence_intervals(<business date>).'
  FROM v;

  ---------------------------------------------------------------------------
  -- 03  WEBHOOK INGESTION ALIVE — no events means no attendance, full stop.
  --     Note this is the check that survives when 33 is green but the data
  --     is empty: the build job can run perfectly over an empty input.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, biz_date, '03', 'Webhook ingestion alive', 'today',
    CASE WHEN NOT work_hours THEN 'SKIPPED'
         WHEN mins IS NULL   THEN 'ALARM'
         WHEN mins > 30      THEN 'ALARM'
         WHEN mins > 15      THEN 'WARN'
         ELSE 'OK' END,
    CAST(mins AS FLOAT64), 'last Zoom event <= 30 min ago',
    IFNULL(CONCAT('last event ', CAST(mins AS STRING), ' min ago'), 'NO events today'),
    'Check the Zoom Marketplace webhook subscription and Cloud Run /webhook logs. Signature failures 401 and write nothing to BQ.'
  FROM (
    SELECT TIMESTAMP_DIFF(run_ts, MAX(event_timestamp), MINUTE) AS mins
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p`
    WHERE event_date BETWEEN DATE_SUB(biz_date, INTERVAL 1 DAY)
                         AND DATE_ADD(biz_date, INTERVAL 1 DAY)
  );

  ---------------------------------------------------------------------------
  -- 04  ROOM MAPPINGS CREATED TODAY — the leading indicator. This is the
  --     check that would have caught the 5-day Room Mapper gap BEFORE
  --     anyone saw Room-XXXX names in a report.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, biz_date, '04', 'Room mappings created today', 'today',
    CASE WHEN ist_hour < 14 OR NOT work_hours THEN 'SKIPPED'
         WHEN n = 0 THEN 'ALARM'
         ELSE 'OK' END,
    CAST(n AS FLOAT64), '> 0 mappings by 14:00 IST',
    CONCAT(CAST(n AS STRING), ' mappings saved today'),
    'Run the Room Mapper against the live meeting, then rebuild today.'
  FROM (
    SELECT COUNT(*) AS n
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE mapping_date = CURRENT_DATE('Asia/Kolkata')
  );

  ---------------------------------------------------------------------------
  -- 05  UNRESOLVED ROOM NAMES — the user-visible symptom of 04, and an
  --     HOURS-ACCURACY issue: an unresolved "Break Time" room classifies as
  --     'breakout', so break time is silently counted as working time.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, biz_date, '05', 'Unresolved room names', 'today',
    CASE WHEN n = 0 THEN 'OK' WHEN n <= 2 THEN 'WARN' ELSE 'ALARM' END,
    CAST(n AS FLOAT64), '0 rooms named Room-XXXX / Unknown Room',
    IFNULL((SELECT STRING_AGG(rn, ', ') FROM (
              SELECT DISTINCT room_name AS rn
              FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
              WHERE event_date = biz_date
                AND (room_name LIKE 'Room-%' OR room_name = 'Unknown Room')
              ORDER BY rn LIMIT 5)), 'none'),
    'Run the Room Mapper so these UUIDs get names, then rebuild today. Until then any unresolved Break Time room is being counted as WORKING time.'
  FROM (
    SELECT COUNT(DISTINCT room_name) AS n
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = biz_date
      AND (room_name LIKE 'Room-%' OR room_name = 'Unknown Room')
  );

  ---------------------------------------------------------------------------
  -- 06  BREAK ROOMS STILL CLASSIFYING — misclassification is invisible to
  --     every other check, because total hours stay perfectly plausible.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, biz_date, '06', 'Break rooms classified', 'today',
    CASE WHEN people < 20     THEN 'SKIPPED'   -- too few people to conclude
         WHEN break_rows = 0  THEN 'ALARM'
         ELSE 'OK' END,
    CAST(break_rows AS FLOAT64), '> 0 break intervals on a day with 20+ people',
    CONCAT(CAST(people AS STRING), ' people, ', CAST(break_rows AS STRING), ' break intervals'),
    'A full office with zero breaks means the Break Time room name stopped resolving (see check 05) — break time is being counted as working time.'
  FROM (
    SELECT COUNTIF(room_category = 'break') AS break_rows,
           COUNT(DISTINCT participant_key)  AS people
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = biz_date
  );

  ---------------------------------------------------------------------------
  -- 08  LONG DAY WATCH — early notice while the day is still live. WARN only:
  --     a genuine long shift looks identical to a lost leave webhook until
  --     the 06:00 rebuild settles it (check 07 gives the verdict).
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, biz_date, '08', 'Long day watch', 'today',
    CASE WHEN n = 0 THEN 'OK' ELSE 'WARN' END,
    CAST(n AS FLOAT64), 'informational: people above 12h so far today',
    IFNULL((SELECT STRING_AGG(txt, '; ') FROM (
              SELECT CONCAT(ANY_VALUE(participant_name), ' ',
                     CAST(ROUND(SUM(duration_seconds)/3600, 1) AS STRING), 'h') AS txt
              FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
              WHERE event_date = biz_date
              GROUP BY participant_key
              HAVING SUM(duration_seconds)/3600 > 12
              LIMIT 5)), 'none'),
    'Either a genuine long shift or a leave webhook that never arrived. Check 07 tomorrow morning tells you which.'
  FROM (
    SELECT COUNT(*) AS n FROM (
      SELECT participant_key
      FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
      WHERE event_date = biz_date
      GROUP BY participant_key
      HAVING SUM(duration_seconds)/3600 > 12
    )
  );

END;
