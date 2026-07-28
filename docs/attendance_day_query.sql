-- ============================================================================
-- Attendance for one day, exactly as the UI (Day View / Team View v2) shows it.
-- Source: presence_intervals (the ONE source of hours). No extra logic.
--
-- Rules baked in (same as app.py team query):
--   * working time  = everything EXCEPT 'break' rooms  (main + breakout)
--   * one row per person   -> GROUP BY participant_key
--   * minutes       = CEIL( SUM(seconds) / 60 )   (round the day up to a minute)
--   * IST times     = UTC + 330 minutes
-- ============================================================================

CREATE OR REPLACE TABLE `breakout_room_calibrator.attendance_2026_07_22` AS
SELECT
  ANY_VALUE(participant_name)                       AS name,
  COALESCE(MAX(participant_email), '')              AS email,

  -- first join / last leave, shown in IST
  FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MIN(start_ts), INTERVAL 330 MINUTE)) AS first_in_ist,
  FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MAX(end_ts),   INTERVAL 330 MINUTE)) AS last_out_ist,

  -- WORKING minutes = main + breakout (break excluded) -- this is the UI "total"
  CAST(CEILING(SUM(IF(room_category != 'break', duration_seconds, 0)) / 60.0) AS INT64) AS working_mins,

  -- same number as hours (2 decimals) for readability
  ROUND(SUM(IF(room_category != 'break', duration_seconds, 0)) / 3600.0, 2)              AS working_hours,

  -- breakdown
  CAST(CEILING(SUM(IF(room_category = 'main',     duration_seconds, 0)) / 60.0) AS INT64) AS main_mins,
  CAST(CEILING(SUM(IF(room_category = 'breakout', duration_seconds, 0)) / 60.0) AS INT64) AS breakout_mins,
  CAST(CEILING(SUM(IF(room_category = 'break',    duration_seconds, 0)) / 60.0) AS INT64) AS break_mins

FROM `breakout_room_calibrator.presence_intervals`
WHERE event_date = '2026-07-22'
GROUP BY participant_key
ORDER BY name;
