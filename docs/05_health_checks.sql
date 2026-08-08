-- ============================================================================
-- HEALTH CHECKS — watchdog for the presence_intervals pipeline
--
-- Writes ONE ROW PER CHECK PER RUN into `health_checks`. A human watches the
-- Google Sheet bound to `v_health_latest`. No email alerting.
--
-- SHARED OBJECTS:  health_checks (table), v_health_latest, v_health_trips_14d
-- TWO PROCEDURES,  because the checks split cleanly by cadence:
--
--   sp_health_live()   every 30 min, 24/7   — "is something broken RIGHT NOW"
--                                             checks 01, 03, 04, 05, 06, 08
--                                             all scoped to TODAY, cheap
--
--   sp_health_daily()  06:30 IST daily      — "was yesterday computed correctly"
--                                             checks 02, 07, 09-19
--                                             scoped to YESTERDAY / registry
--
-- WHY SPLIT: the yesterday-scoped checks are (a) meaningless before the 06:00
-- rebuild settles the day — under v11 an open segment on TODAY legitimately
-- runs to CURRENT_TIMESTAMP with a 14h ceiling at confidence 0.35, so ">14h"
-- cannot fire and "% guessed" is inflated by everyone currently online; and
-- (b) full-partition scans, so running them 48x a day is pure cost for a
-- number that changes once. The live set touches only today's partition.
--
-- BUSINESS DAY: everything is anchored to the 05:00 -> 05:00 IST day.
--   biz_date = DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata')
-- This MUST stay identical to sp_build_presence_intervals (is_current_day),
-- the two build scheduled queries, and get_business_date() in zt_helpers.py.
-- If they drift, checks silently examine the wrong partition.
--
-- NOT EXECUTED by the author — no BigQuery credentials. Run each procedure by
-- hand once and read v_health_latest before scheduling.
-- ============================================================================


-- ── results table ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS
`verve-attendance-tracker.breakout_room_calibrator.health_checks` (
  run_ts        TIMESTAMP NOT NULL,   -- when the check ran (UTC)
  run_ist       STRING,               -- same, formatted IST, for the sheet
  business_date DATE,                 -- the day the check examined
  check_id      STRING NOT NULL,      -- '01'..'19', stable
  check_name    STRING NOT NULL,
  scope         STRING,               -- today | yesterday | registry | identity
  severity      STRING NOT NULL,      -- OK | WARN | ALARM | SKIPPED
  metric        FLOAT64,              -- the measured number
  threshold     STRING,               -- what would have been acceptable
  detail        STRING,               -- names / samples, for triage
  action        STRING                -- what to do when it trips
)
PARTITION BY DATE(run_ts)
CLUSTER BY check_id, severity;


-- ── the view the Google Sheet reads ─────────────────────────────────────────
-- One row per check: its most recent run, ALARM first.
-- A check that stops running DISAPPEARS after 3 days rather than going stale-
-- green — that absence is the only way a self-monitoring system can report
-- that the watchdog itself died.
CREATE OR REPLACE VIEW
`verve-attendance-tracker.breakout_room_calibrator.v_health_latest` AS
SELECT
  severity, check_id, check_name, scope, metric, threshold, detail, action,
  business_date, run_ist
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY check_id ORDER BY run_ts DESC) AS rn
  FROM `verve-attendance-tracker.breakout_room_calibrator.health_checks`
  WHERE DATE(run_ts) >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 3 DAY)
)
WHERE rn = 1
ORDER BY
  CASE severity WHEN 'ALARM' THEN 1 WHEN 'WARN' THEN 2 WHEN 'OK' THEN 3 ELSE 4 END,
  check_id;


-- ── history of trips: "blip or pattern?" ────────────────────────────────────
CREATE OR REPLACE VIEW
`verve-attendance-tracker.breakout_room_calibrator.v_health_trips_14d` AS
SELECT run_ist, business_date, check_id, check_name, severity, metric, detail
FROM `verve-attendance-tracker.breakout_room_calibrator.health_checks`
WHERE DATE(run_ts) >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 14 DAY)
  AND severity IN ('WARN', 'ALARM')
ORDER BY run_ts DESC, check_id;


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
  -- Liveness only means something when people are expected online.
  -- DAYOFWEEK: 1 = Sunday.
  DECLARE work_hours BOOL DEFAULT
    (EXTRACT(HOUR FROM CURRENT_TIMESTAMP() AT TIME ZONE 'Asia/Kolkata') BETWEEN 9 AND 23)
    AND (EXTRACT(DAYOFWEEK FROM DATE(CURRENT_TIMESTAMP(), 'Asia/Kolkata')) != 1);

  ---------------------------------------------------------------------------
  -- 01  BUILD FRESHNESS — is the 5-minute build scheduled query alive?
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, biz_date, '01', 'Build freshness', 'today',
    CASE WHEN NOT work_hours THEN 'SKIPPED'
         WHEN mins IS NULL   THEN 'ALARM'
         WHEN mins > 20      THEN 'ALARM'
         WHEN mins > 12      THEN 'WARN'
         ELSE 'OK' END,
    CAST(mins AS FLOAT64), 'last build <= 20 min ago (09:00-23:59 IST, Mon-Sat)',
    IFNULL(CONCAT('last built ', CAST(mins AS STRING), ' min ago'),
           'NO ROWS for today at all'),
    'Check the presence-intervals-today scheduled query. Manual fix: CALL sp_build_presence_intervals(<business date>).'
  FROM (
    SELECT TIMESTAMP_DIFF(run_ts, MAX(built_at), MINUTE) AS mins
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = biz_date
  );

  ---------------------------------------------------------------------------
  -- 03  WEBHOOK INGESTION ALIVE — no events means no attendance, full stop.
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


-- ############################################################################
-- ##  DAILY CHECKS — schedule 06:30 IST (AFTER the 06:00 yesterday rebuild) ##
-- ##  CALL `...breakout_room_calibrator.sp_health_daily`();                  ##
-- ############################################################################
CREATE OR REPLACE PROCEDURE
`verve-attendance-tracker.breakout_room_calibrator.sp_health_daily`()
BEGIN

  DECLARE run_ts    TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE run_ist   STRING    DEFAULT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', CURRENT_TIMESTAMP(), 'Asia/Kolkata');
  DECLARE biz_date  DATE      DEFAULT DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata');
  DECLARE prev_date DATE      DEFAULT DATE_SUB(DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata'), INTERVAL 1 DAY);
  DECLARE ist_hour  INT64     DEFAULT EXTRACT(HOUR FROM CURRENT_TIMESTAMP() AT TIME ZONE 'Asia/Kolkata');

  ---------------------------------------------------------------------------
  -- 02  YESTERDAY REBUILD RAN — the 06:00 job applies the past-day 10-minute
  --     cap. Without it, yesterday keeps today's inflated open segments and
  --     every hours check below is measuring the wrong thing.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '02', 'Yesterday rebuild ran', 'yesterday',
    CASE WHEN ist_hour < 7      THEN 'SKIPPED'   -- 06:00 job may not have run
         WHEN built IS NULL     THEN 'ALARM'
         WHEN built < expected  THEN 'ALARM'
         ELSE 'OK' END,
    CAST(TIMESTAMP_DIFF(run_ts, built, MINUTE) AS FLOAT64),
    'yesterday rebuilt after 06:00 IST today',
    IFNULL(CONCAT('yesterday last built ',
                  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', built, 'Asia/Kolkata'), ' IST'),
           'NO ROWS for yesterday'),
    'Run the presence-intervals-yesterday scheduled query, or CALL sp_build_presence_intervals(<yesterday>).'
  FROM (
    SELECT MAX(built_at) AS built,
           TIMESTAMP_ADD(TIMESTAMP_SUB(TIMESTAMP(biz_date), INTERVAL 330 MINUTE),
                         INTERVAL 6 HOUR) AS expected
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 07  IMPOSSIBLE HOURS — would have caught the v9 phantom 240-minute tail.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '07', 'Impossible hours', 'yesterday',
    CASE WHEN n = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST(n AS FLOAT64), '0 people above 14h after the 06:00 rebuild',
    IFNULL((SELECT STRING_AGG(txt, '; ') FROM (
              SELECT CONCAT(ANY_VALUE(participant_name), ' ',
                     CAST(ROUND(SUM(duration_seconds)/3600, 1) AS STRING), 'h') AS txt
              FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
              WHERE event_date = prev_date
              GROUP BY participant_key
              HAVING SUM(duration_seconds)/3600 > 14
              LIMIT 5)), 'none'),
    'A phantom open segment or a missed reconnect pair. Inspect that person''s raw rows in participant_events_p for the day.'
  FROM (
    SELECT COUNT(*) AS n FROM (
      SELECT participant_key
      FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
      WHERE event_date = prev_date
      GROUP BY participant_key
      HAVING SUM(duration_seconds)/3600 > 14
    )
  );

  ---------------------------------------------------------------------------
  -- 09  GUESSED ENDINGS % — confidence 0.35 means "no leave webhook arrived,
  --     the ending was invented". A rising share = webhook delivery degrading,
  --     which corrupts hours silently.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '09', 'Guessed endings %', 'yesterday',
    CASE WHEN total = 0 THEN 'SKIPPED'
         WHEN pct > 20  THEN 'ALARM'
         WHEN pct > 10  THEN 'WARN'
         ELSE 'OK' END,
    pct, '<= 10% of intervals at confidence 0.35',
    CONCAT(CAST(guessed AS STRING), ' of ', CAST(total AS STRING),
           ' intervals had no closing webhook'),
    'Zoom is dropping participant_left events. Check the Marketplace subscription and Cloud Run error logs.'
  FROM (
    SELECT COUNT(*) AS total,
           COUNTIF(confidence <= 0.35) AS guessed,
           ROUND(100 * SAFE_DIVIDE(COUNTIF(confidence <= 0.35), COUNT(*)), 1) AS pct
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 10  SAME PERSON IN TWO ROOMS AT ONCE — the invariant the v8 reconnect bug
  --     violated. Catches any future regression in the pairing logic.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '10', 'Overlapping rooms', 'yesterday',
    CASE WHEN n = 0 THEN 'OK' WHEN n <= 3 THEN 'WARN' ELSE 'ALARM' END,
    CAST(n AS FLOAT64), '0 overlaps longer than 60s',
    IFNULL((SELECT STRING_AGG(who, ', ') FROM (
              SELECT DISTINCT a.participant_name AS who
              FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` a
              JOIN `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` b
                ON  a.event_date      = b.event_date
                AND a.participant_key = b.participant_key
                AND a.interval_id    <  b.interval_id
                AND a.start_ts < b.end_ts AND b.start_ts < a.end_ts
              WHERE a.event_date = prev_date
                AND a.room_name != b.room_name
                AND TIMESTAMP_DIFF(LEAST(a.end_ts, b.end_ts),
                                   GREATEST(a.start_ts, b.start_ts), SECOND) > 60
              LIMIT 5)), 'none'),
    'One person cannot be in two rooms at once — the event pairing in sp_build_presence_intervals has regressed and their hours are double-counted.'
  FROM (
    SELECT COUNT(*) AS n
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` a
    JOIN `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` b
      ON  a.event_date      = b.event_date
      AND a.participant_key = b.participant_key
      AND a.interval_id    <  b.interval_id      -- count each pair once
      AND a.start_ts < b.end_ts AND b.start_ts < a.end_ts
    WHERE a.event_date = prev_date
      AND a.room_name != b.room_name
      AND TIMESTAMP_DIFF(LEAST(a.end_ts, b.end_ts),
                         GREATEST(a.start_ts, b.start_ts), SECOND) > 60
  );

  ---------------------------------------------------------------------------
  -- 11  STRUCTURAL SANITY — cheap, catches a half-failed build. Duplicates
  --     matter most: if the DELETE+INSERT ever half-commits and re-runs,
  --     every hour doubles with no other visible symptom.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '11', 'Structural sanity', 'yesterday',
    CASE WHEN bad_range + bad_dur + dupes = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST(bad_range + bad_dur + dupes AS FLOAT64), '0 malformed or duplicate rows',
    CONCAT('end<=start: ', CAST(bad_range AS STRING),
           ' | duration<=0: ', CAST(bad_dur AS STRING),
           ' | duplicate (person,room,start): ', CAST(dupes AS STRING)),
    'Rebuild the day: CALL sp_build_presence_intervals(<date>). Duplicates mean an interrupted build — verify row counts afterwards.'
  FROM (
    SELECT
      COUNTIF(end_ts <= start_ts)                 AS bad_range,
      COUNTIF(COALESCE(duration_seconds, 0) <= 0) AS bad_dur,
      (SELECT COUNT(*) FROM (
         SELECT participant_key, room_name, start_ts
         FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
         WHERE event_date = prev_date
         GROUP BY 1, 2, 3
         HAVING COUNT(*) > 1
       ))                                         AS dupes
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 12  ISOLATION DATA PRESENT — REGRESSION FLAG. v11 writes
  --     `0 AS alone_seconds` unconditionally, so every isolation figure in
  --     the tool (Employee Year Summary, Team View, the Isolation sheet in
  --     the Excel export, the Isolation pivot tab — six endpoints read
  --     SUM(alone_seconds)) is 0 for every day it built.
  --     Expect ALARM until alone_seconds is computed in the procedure.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '12', 'Isolation data present', 'yesterday',
    CASE WHEN breakout_hours < 1 THEN 'SKIPPED'
         WHEN nonzero_rows = 0   THEN 'ALARM'
         ELSE 'OK' END,
    CAST(nonzero_rows AS FLOAT64), '> 0 intervals carrying alone_seconds',
    CONCAT(CAST(ROUND(breakout_hours, 1) AS STRING), 'h of breakout time, ',
           CAST(nonzero_rows AS STRING), ' intervals carry isolation'),
    'sp_build_presence_intervals v11 hardcodes alone_seconds = 0, so all isolation reporting is 0. Compute it from interval overlap (see the isolation note in this file''s header comment).'
  FROM (
    SELECT COUNTIF(alone_seconds > 0) AS nonzero_rows,
           SUM(IF(room_category = 'breakout', duration_seconds, 0)) / 3600 AS breakout_hours
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 13  CONFIDENCE SPREAD — the reports flag a day "⚠ estimated" when >50%
  --     of its seconds come from rows below the LOWCONF cutoff in app.py
  --     (now 0.4). v11 emits 0.35 (guessed ending) and 0.5 (webhook-closed),
  --     so the flag is meaningful only while BOTH values appear. If every row
  --     is 0.35, everyone is flagged and the marker stops informing.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '13', 'Confidence spread', 'yesterday',
    CASE WHEN n = 0            THEN 'SKIPPED'
         WHEN max_conf < 0.4   THEN 'WARN'
         ELSE 'OK' END,
    max_conf, 'some intervals at confidence >= 0.4 (the app.py LOWCONF cutoff)',
    CONCAT('highest confidence yesterday: ', CAST(max_conf AS STRING)),
    'Every row is below the cutoff, so the UI marks ALL attendance as estimated. Check the confidence values sp_build_presence_intervals assigns.'
  FROM (
    SELECT COUNT(*) AS n, MAX(confidence) AS max_conf
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 14  UNRECOGNIZED PEOPLE WITH REAL HOURS — someone worked a full day and
  --     appears in nobody's team report. Approximate name/email match only;
  --     the Unrecognized tab does the careful multi-pass matching.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH per AS (
    SELECT participant_key,
           ANY_VALUE(participant_name) AS nm,
           LOWER(TRIM(COALESCE(ANY_VALUE(participant_email), ''))) AS em,
           SUM(duration_seconds) / 3600 AS h
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
    GROUP BY participant_key
  ),
  reg AS (
    SELECT DISTINCT LOWER(TRIM(participant_name)) AS k,
           LOWER(TRIM(COALESCE(participant_email, ''))) AS em
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
  ),
  unknown AS (
    SELECT nm, h FROM per
    WHERE h >= 2
      AND NOT EXISTS (SELECT 1 FROM reg WHERE reg.k = per.participant_key)
      AND NOT EXISTS (SELECT 1 FROM reg WHERE per.em != '' AND reg.em = per.em)
  )
  SELECT run_ts, run_ist, prev_date, '14', 'Unrecognized people with hours', 'yesterday',
    CASE WHEN (SELECT COUNT(*) FROM unknown) = 0 THEN 'OK'
         WHEN (SELECT COUNT(*) FROM unknown) <= 3 THEN 'WARN'
         ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM unknown) AS FLOAT64), '0 unregistered people with 2h+',
    IFNULL((SELECT STRING_AGG(nm, ', ') FROM (SELECT nm FROM unknown ORDER BY h DESC LIMIT 5)), 'none'),
    'Open Employees > Unrecognized Participants and either register them or map them to an existing employee. Until then their hours are in no team report.';

  ---------------------------------------------------------------------------
  -- 15  REGISTRY EMAILS MISSING — with no email, identity rests entirely on
  --     name matching, so one Zoom rename drops a person from their team's
  --     report. (As of Aug 2026 essentially every registry row is blank.)
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, biz_date, '15', 'Registry emails missing', 'registry',
    CASE WHEN n = 0 THEN 'OK' WHEN n <= 5 THEN 'WARN' ELSE 'ALARM' END,
    CAST(n AS FLOAT64), '0 active employees without an email',
    CONCAT(CAST(n AS STRING), ' active employees have no email on file'),
    'Fill participant_email in the Registry tab. Without it, name matching is the ONLY identity link — a Zoom display-name change silently removes someone from reports.'
  FROM (
    SELECT COUNT(*) AS n
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
      AND LOWER(COALESCE(category, 'employee')) = 'employee'
      AND (participant_email IS NULL OR TRIM(participant_email) = '')
  );

  ---------------------------------------------------------------------------
  -- 16  ROOM UUID -> MULTIPLE NAMES.  *** THE ASSUMPTION v11 RESTS ON ***
  --     v11 tier-3 name resolution takes a room name from room_mappings on
  --     ANY date, justified by "room_uuid -> room_name is provably 1:1 (176
  --     uuids, ZERO mapping to more than one name)". That was measured once.
  --     If a UUID is ever reused for a renamed/different room, cross-day
  --     resolution starts stamping the WRONG name on historical rooms —
  --     silently, with no error anywhere. This check re-verifies the premise
  --     continuously, over the same 60-day window the builder searches.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH ev AS (
    SELECT DISTINCT room_uuid, room_name
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p`
    WHERE event_date BETWEEN DATE_SUB(prev_date, INTERVAL 60 DAY) AND biz_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%' AND room_name != 'Unknown Room'
    UNION DISTINCT
    SELECT DISTINCT room_uuid, room_name
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
  ),
  conflicts AS (
    SELECT room_uuid, COUNT(DISTINCT room_name) AS names,
           STRING_AGG(room_name, ' | ' ORDER BY room_name) AS name_list
    FROM ev GROUP BY room_uuid HAVING COUNT(DISTINCT room_name) > 1
  )
  SELECT run_ts, run_ist, biz_date, '16', 'Room UUID maps to one name', 'identity',
    CASE WHEN (SELECT COUNT(*) FROM conflicts) = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM conflicts) AS FLOAT64),
    '0 room UUIDs with more than one name (v11 cross-day resolution assumes 1:1)',
    IFNULL((SELECT STRING_AGG(CONCAT(SUBSTR(room_uuid, 1, 12), '=', name_list), '; ')
            FROM (SELECT room_uuid, name_list FROM conflicts ORDER BY names DESC LIMIT 3)), 'none'),
    'A UUID with two names breaks the premise of v11 tier-3 (any-day) name resolution — rooms may be getting the WRONG name on days the mapper did not run. Restrict resolution to same-day mappings for these UUIDs, or re-map.';

  ---------------------------------------------------------------------------
  -- 17  ONE DISPLAY NAME, TWO EMAILS -> TWO PEOPLE SILENTLY MERGED.
  --     v11 assigns participant_key = email only when a normalized name maps
  --     to EXACTLY ONE email (unique_email_per_name ... HAVING COUNT(DISTINCT
  --     participant_email) = 1). When two real people share a display name
  --     ("Harsh"), the CTE finds two emails, the rule declines, BOTH fall back
  --     to the same name-key — and their hours are summed into one person.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH pairs AS (
    SELECT LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS name_key,
           LOWER(TRIM(participant_email)) AS email
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p`
    WHERE event_date = prev_date
      AND participant_name IS NOT NULL AND TRIM(participant_name) != ''
      AND participant_email IS NOT NULL AND TRIM(participant_email) != ''
      AND LOWER(participant_name) NOT LIKE '%scout%'
    GROUP BY 1, 2
  ),
  collisions AS (
    SELECT name_key, COUNT(DISTINCT email) AS emails,
           STRING_AGG(email, ' | ' ORDER BY email) AS email_list
    FROM pairs GROUP BY name_key HAVING COUNT(DISTINCT email) > 1
  )
  SELECT run_ts, run_ist, prev_date, '17', 'Shared display name (people merged)', 'identity',
    CASE WHEN (SELECT COUNT(*) FROM collisions) = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM collisions) AS FLOAT64),
    '0 display names used by two different email addresses',
    IFNULL((SELECT STRING_AGG(CONCAT(name_key, ' = ', email_list), '; ')
            FROM (SELECT name_key, email_list FROM collisions ORDER BY emails DESC LIMIT 3)), 'none'),
    'Two people share one Zoom display name, so v11 merges them into ONE participant_key and their hours are combined. Ask one of them to change their Zoom display name.';

  ---------------------------------------------------------------------------
  -- 18  ONE PERSON SPLIT ACROSS TWO KEYS — the mirror of 17. A name variant
  --     carrying an email keys on the email; a variant with no email keys on
  --     the name. Same human, two rows, hours halved in each.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH split AS (
    SELECT LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS name_key,
           COUNT(DISTINCT participant_key) AS keys,
           STRING_AGG(DISTINCT participant_key, ' | ') AS key_list
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
      AND participant_name IS NOT NULL AND TRIM(participant_name) != ''
    GROUP BY 1
    HAVING COUNT(DISTINCT participant_key) > 1
  )
  SELECT run_ts, run_ist, prev_date, '18', 'Person split across keys', 'identity',
    CASE WHEN (SELECT COUNT(*) FROM split) = 0 THEN 'OK'
         WHEN (SELECT COUNT(*) FROM split) <= 2 THEN 'WARN'
         ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM split) AS FLOAT64),
    '0 display names resolving to more than one participant_key',
    IFNULL((SELECT STRING_AGG(CONCAT(name_key, ' -> ', key_list), '; ')
            FROM (SELECT name_key, key_list FROM split ORDER BY keys DESC LIMIT 3)), 'none'),
    'One human is being counted as two people, so their day is split across two rows and each looks like a half day. Usually a missing email on one name variant — fix in the Registry, or add a participant_alias.';

  ---------------------------------------------------------------------------
  -- 19  PEOPLE IN WEBHOOKS BUT MISSING FROM INTERVALS — the whole-person
  --     equivalent of a phantom. v11 DROPS any interval whose session_start_ts
  --     is NULL or outside the day window (v11 step 7), so a shift that began
  --     before 05:00 IST, or a login the pairing logic mis-read, removes that
  --     person from the day entirely — with no error and nothing in the UI.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH joined_people AS (
    -- everyone who genuinely logged in inside yesterday's 05:00->05:00 window
    SELECT DISTINCT
      LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS name_key,
      ANY_VALUE(participant_name) OVER (
        PARTITION BY LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', '')))
      ) AS nm
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p`
    WHERE event_date BETWEEN prev_date AND DATE_ADD(prev_date, INTERVAL 1 DAY)
      AND event_type IN ('participant_joined', 'meeting.participant_joined')
      AND participant_name IS NOT NULL AND TRIM(participant_name) != ''
      AND LOWER(participant_name) NOT LIKE '%scout%'
      AND event_timestamp >= TIMESTAMP_ADD(TIMESTAMP_SUB(TIMESTAMP(prev_date), INTERVAL 330 MINUTE), INTERVAL 5 HOUR)
      AND event_timestamp <  TIMESTAMP_ADD(TIMESTAMP_SUB(TIMESTAMP(prev_date), INTERVAL 330 MINUTE), INTERVAL 29 HOUR)
  ),
  built_people AS (
    SELECT DISTINCT LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS name_key
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  ),
  missing AS (
    SELECT j.nm FROM joined_people j
    LEFT JOIN built_people b USING (name_key)
    WHERE b.name_key IS NULL
  )
  SELECT run_ts, run_ist, prev_date, '19', 'People missing from intervals', 'yesterday',
    CASE WHEN (SELECT COUNT(*) FROM missing) = 0 THEN 'OK'
         WHEN (SELECT COUNT(*) FROM missing) <= 2 THEN 'WARN'
         ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM missing) AS FLOAT64),
    '0 people who logged in but produced no intervals',
    IFNULL((SELECT STRING_AGG(nm, ', ') FROM (SELECT nm FROM missing ORDER BY nm LIMIT 5)), 'none'),
    'These people logged in yesterday but have ZERO hours. Usually a session starting before 05:00 IST (filed to the previous day) or a login the pairing logic mis-read. Check their raw events in participant_events_p.';

  ---------------------------------------------------------------------------
  -- retention
  ---------------------------------------------------------------------------
  DELETE FROM `verve-attendance-tracker.breakout_room_calibrator.health_checks`
  WHERE DATE(run_ts) < DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 90 DAY);

END;


-- ── first run (do this by hand before scheduling) ───────────────────────────
-- CALL `verve-attendance-tracker.breakout_room_calibrator.sp_health_live`();
-- CALL `verve-attendance-tracker.breakout_room_calibrator.sp_health_daily`();
-- SELECT * FROM `verve-attendance-tracker.breakout_room_calibrator.v_health_latest`;
