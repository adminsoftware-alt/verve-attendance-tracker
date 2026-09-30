-- ============================================================================
-- BACKFILL — rebuild past days with the v12 room-name fix
--
-- Run 06_fix_room_name_v12.sql FIRST. This file only re-runs the procedure;
-- it contains no logic of its own. Every past day rebuilds from the same
-- webhook events with the same room UUIDs, so correcting the builder
-- corrects history.
--
-- RUN THE FOUR STEPS IN ORDER. Step 1 is what makes this reversible.
--
-- TWO RULES:
--   * Never backfill TODAY. The 5-minute scheduled query is already building
--     it, and two builds of the same day abort each other.
--   * Don't run this at 06:00 IST — the yesterday rebuild fires then.
-- ============================================================================


-- ════════════════════════════════════════════════════════════════════════
-- STEP 0 — how far back does the data go?  (read-only, run this first)
-- ════════════════════════════════════════════════════════════════════════
SELECT
  MIN(event_date) AS earliest_day,
  MAX(event_date) AS latest_day,
  COUNT(DISTINCT event_date) AS days_with_data,
  COUNT(*) AS total_intervals
FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`;


-- ════════════════════════════════════════════════════════════════════════
-- STEP 1 — SNAPSHOT before touching anything.  Run ONCE.
--
-- A full copy of the current numbers. Storage is trivial and it buys two
-- things: you can show exactly what moved, and you can put it all back
-- (STEP 4) if the rebuild looks wrong.
-- ════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE TABLE
`verve-attendance-tracker.breakout_room_calibrator.zz_presence_intervals_pre_v12`
PARTITION BY event_date
AS
SELECT * FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`;

-- Confirm the copy matches:
SELECT
  (SELECT COUNT(*) FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`)              AS live_rows,
  (SELECT COUNT(*) FROM `verve-attendance-tracker.breakout_room_calibrator.zz_presence_intervals_pre_v12`)   AS snapshot_rows;


-- ════════════════════════════════════════════════════════════════════════
-- STEP 2 — THE BACKFILL.  Edit the two dates, then Run.
--
-- Do ONE MONTH AT A TIME. Each day rebuilt scans a 60-day window of the
-- events table (the builder looks that far back for room names), so the cost
-- grows with the number of days. Run one month, look at the bytes billed
-- BigQuery reports, then decide how much more to do.
--
-- Only days that already have intervals are rebuilt — empty days and
-- weekends are skipped automatically.
-- ════════════════════════════════════════════════════════════════════════
BEGIN
  DECLARE backfill_from DATE DEFAULT DATE '2026-08-01';   -- <<< EDIT
  DECLARE backfill_to   DATE DEFAULT DATE '2026-08-10';   -- <<< EDIT
  DECLARE today_biz     DATE DEFAULT DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata');
  DECLARE done_count    INT64 DEFAULT 0;

  -- Guard: never rebuild the live day.
  IF backfill_to >= today_biz THEN
    SET backfill_to = DATE_SUB(today_biz, INTERVAL 1 DAY);
  END IF;

  FOR rec IN (
    SELECT DISTINCT event_date AS d
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date BETWEEN backfill_from AND backfill_to
    ORDER BY event_date
  ) DO
    CALL `verve-attendance-tracker.breakout_room_calibrator.sp_build_presence_intervals`(rec.d);
    SET done_count = done_count + 1;
  END FOR;

  SELECT done_count AS days_rebuilt, backfill_from AS from_date, backfill_to AS to_date;
END;


-- ════════════════════════════════════════════════════════════════════════
-- STEP 3 — WHAT MOVED.  Read-only. Run after each month.
-- ════════════════════════════════════════════════════════════════════════

-- 3a. Day-level summary: how much time changed category?
WITH before AS (
  SELECT event_date, room_category, SUM(duration_seconds)/60 AS mins
  FROM `verve-attendance-tracker.breakout_room_calibrator.zz_presence_intervals_pre_v12`
  GROUP BY 1, 2
),
after AS (
  SELECT event_date, room_category, SUM(duration_seconds)/60 AS mins
  FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  GROUP BY 1, 2
)
SELECT
  COALESCE(b.event_date, a.event_date)       AS event_date,
  COALESCE(b.room_category, a.room_category) AS room_category,
  ROUND(COALESCE(b.mins, 0))                 AS minutes_before,
  ROUND(COALESCE(a.mins, 0))                 AS minutes_after,
  ROUND(COALESCE(a.mins, 0) - COALESCE(b.mins, 0)) AS change
FROM before b
FULL OUTER JOIN after a
  ON b.event_date = a.event_date AND b.room_category = a.room_category
WHERE ABS(COALESCE(a.mins, 0) - COALESCE(b.mins, 0)) >= 1
ORDER BY event_date DESC, ABS(change) DESC;
-- Expect: 'break' minutes DOWN, 'breakout' minutes UP by a similar amount.
-- That is mislabelled break time returning to working hours.


-- 3b. Per person: whose working hours changed, and by how much?
--     Working minutes = everything except the Break Time room.
WITH before AS (
  SELECT event_date, participant_key, ANY_VALUE(participant_name) AS nm,
         SUM(IF(room_category != 'break', duration_seconds, 0))/60 AS work_mins
  FROM `verve-attendance-tracker.breakout_room_calibrator.zz_presence_intervals_pre_v12`
  GROUP BY 1, 2
),
after AS (
  SELECT event_date, participant_key,
         SUM(IF(room_category != 'break', duration_seconds, 0))/60 AS work_mins
  FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  GROUP BY 1, 2
)
SELECT
  b.event_date,
  b.nm                                  AS name,
  ROUND(b.work_mins)                    AS working_mins_before,
  ROUND(a.work_mins)                    AS working_mins_after,
  ROUND(a.work_mins - b.work_mins)      AS change
FROM before b
JOIN after a USING (event_date, participant_key)
WHERE ABS(a.work_mins - b.work_mins) >= 5
ORDER BY ABS(a.work_mins - b.work_mins) DESC
LIMIT 200;


-- 3c. Sanity: nobody should have LOST a large amount of working time.
--     A few minutes of movement is normal; hours are not. If this returns
--     rows, stop and investigate before backfilling further.
WITH before AS (
  SELECT event_date, participant_key, ANY_VALUE(participant_name) AS nm,
         SUM(IF(room_category != 'break', duration_seconds, 0))/60 AS work_mins
  FROM `verve-attendance-tracker.breakout_room_calibrator.zz_presence_intervals_pre_v12`
  GROUP BY 1, 2
),
after AS (
  SELECT event_date, participant_key,
         SUM(IF(room_category != 'break', duration_seconds, 0))/60 AS work_mins
  FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  GROUP BY 1, 2
)
SELECT b.event_date, b.nm AS name,
       ROUND(b.work_mins) AS before_mins,
       ROUND(a.work_mins) AS after_mins,
       ROUND(a.work_mins - b.work_mins) AS lost_minutes
FROM before b
JOIN after a USING (event_date, participant_key)
WHERE a.work_mins - b.work_mins < -30
ORDER BY lost_minutes ASC;


-- ════════════════════════════════════════════════════════════════════════
-- STEP 4 — ROLLBACK, if the numbers look wrong.
--
-- Puts the pre-v12 figures back for a date range. Edit the dates.
-- Only useful while the snapshot from STEP 1 still exists.
-- ════════════════════════════════════════════════════════════════════════
-- BEGIN TRANSACTION;
--
-- DELETE FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
-- WHERE event_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-10';
--
-- INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
-- SELECT * FROM `verve-attendance-tracker.breakout_room_calibrator.zz_presence_intervals_pre_v12`
-- WHERE event_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-10';
--
-- COMMIT TRANSACTION;


-- ════════════════════════════════════════════════════════════════════════
-- STEP 5 — when you are satisfied, drop the snapshot (optional).
-- Keep it until at least one monthly report has been issued off the new
-- numbers and nobody has queried the change.
-- ════════════════════════════════════════════════════════════════════════
-- DROP TABLE `verve-attendance-tracker.breakout_room_calibrator.zz_presence_intervals_pre_v12`;
