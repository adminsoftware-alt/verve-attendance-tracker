-- Part 1 of 3 — paste this WHOLE file into BigQuery and click Run.
-- Creates the results table and the two views.
-- Run parts in order: 1, then 2, then 3.

-- ── results table ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS
`verve-attendance-tracker.breakout_room_calibrator.health_checks` (
  run_ts        TIMESTAMP NOT NULL,   -- when the check ran (UTC)
  run_ist       STRING,               -- same, formatted IST, for the sheet
  business_date DATE,                 -- the day the check examined
  check_id      STRING NOT NULL,      -- stable id, NOT sequential
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
-- that the watchdog itself died. This 3-day window is also why every check
-- must run at least daily; see "WHY NO WEEKLY PROCEDURE" above.
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
