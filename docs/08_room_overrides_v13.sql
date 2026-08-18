-- ============================================================================
-- ROOM OVERRIDES + room_uuid on presence_intervals — v13
-- Paste this WHOLE file into BigQuery and click Run. Replaces v12.
--
-- WHAT THIS ADDS
--
-- 1. `room_overrides` — a table where a human states what a room actually is.
--    The builder reads it BEFORE every automatic source, so a correction
--    beats the Room Mapper, beats the webhook event names, beats everything.
--
-- 2. `presence_intervals.room_uuid` — until now an interval could not be
--    traced back to a room ID, which is why diagnosing the 2026-08-11
--    mislabelling needed the raw events table. The UI also needs it: you
--    cannot correct a room you cannot identify.
--
-- 3. `room_category` override — normally the name decides the category
--    ("...break time..." -> break). An explicit category lets you force
--    `break` even when the correct name does not contain those words.
--
-- WHY BREAK IS THE ONLY CATEGORY THAT MATTERS
-- Every hours figure in the tool is a binary split: `room_category = 'break'`
-- versus everything else. `main` and `breakout` are treated identically. So a
-- room labelled "Creative Corner" instead of "Innovation Station" costs
-- nothing, but the break room labelled as a work room silently inflates
-- everybody's working hours.
--
-- HOW AN OVERRIDE IS MATCHED
--   mapping_date = <the day>  -> applies to that day only   (priority 0)
--   mapping_date IS NULL      -> applies whenever that UUID appears (priority 1)
--
-- Both exist because room UUIDs are stable for a while and then rotate. In
-- room_mappings, the break room oUy7wbJxs4e0Xuveb+casg== is the same ID on
-- 6, 7 and 8 August across two different meeting sessions, then changes by
-- the 10th. A dated override fixes one day; an undated one holds for the
-- whole life of that room ID and stops applying by itself when Zoom rotates
-- it. Health check 16 is what verifies the assumption underneath the undated
-- form — that one UUID only ever means one room.
--
-- NOT EXECUTED by the author — no BigQuery credentials.
-- ============================================================================


-- ── the override table ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS
`verve-attendance-tracker.breakout_room_calibrator.room_overrides` (
  override_id   STRING NOT NULL,
  room_uuid     STRING NOT NULL,   -- the room being corrected
  mapping_date  DATE,              -- NULL = applies to every day this UUID appears
  room_name     STRING,            -- corrected name (optional)
  room_category STRING,            -- 'break' | 'breakout' | 'main' (optional)
  note          STRING,            -- why — free text from whoever fixed it
  set_by        STRING,            -- who
  set_at        TIMESTAMP,
  active        BOOL               -- FALSE retires an override without deleting it
);

-- ── room_uuid on presence_intervals ─────────────────────────────────────────
-- ADD COLUMN IF NOT EXISTS is required because the table already exists;
-- the CREATE TABLE IF NOT EXISTS inside the procedure will not alter it.
ALTER TABLE `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  ADD COLUMN IF NOT EXISTS room_uuid STRING;


CREATE OR REPLACE PROCEDURE
`verve-attendance-tracker`.breakout_room_calibrator.sp_build_presence_intervals(target_date DATE)
BEGIN

  -- The attendance day starts at 05:00 IST, not midnight, because shifts here
  -- routinely run past midnight. 330 = IST offset in minutes.
  DECLARE day_boundary_hour INT64 DEFAULT 5;   -- documentation; literals below are authoritative

  DECLARE day_start_utc  TIMESTAMP DEFAULT TIMESTAMP_ADD(
                           TIMESTAMP_SUB(TIMESTAMP(target_date), INTERVAL 330 MINUTE),
                           INTERVAL 5 HOUR);                                   -- 05:00 IST on D
  DECLARE day_end_utc    TIMESTAMP DEFAULT TIMESTAMP_ADD(
                           TIMESTAMP_SUB(TIMESTAMP(target_date), INTERVAL 330 MINUTE),
                           INTERVAL 29 HOUR);                                  -- 05:00 IST on D+1
  DECLARE tail_end_utc   TIMESTAMP DEFAULT TIMESTAMP_ADD(
                           TIMESTAMP_SUB(TIMESTAMP(target_date), INTERVAL 330 MINUTE),
                           INTERVAL 35 HOUR);                                  -- 11:00 IST on D+1

  -- tunables
  DECLARE reconnect_window_ms  INT64 DEFAULT 30000;  -- breakout->pair and join<->left window
  DECLARE pair_tightness_ms    INT64 DEFAULT  5000;  -- max gap WITHIN the left/join pair
  DECLARE min_segment_seconds  INT64 DEFAULT     5;  -- drop webhook slivers
  DECLARE no_leave_cap_minutes   INT64 DEFAULT   10;  -- PAST days: leave webhook lost
  DECLARE live_open_cap_minutes  INT64 DEFAULT  840;  -- TODAY: 14h ceiling only

  DECLARE is_current_day        BOOL;
  DECLARE horizon               TIMESTAMP;
  DECLARE effective_cap_minutes INT64;

  SET is_current_day = (target_date =
        DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata'));

  SET horizon = IF(is_current_day, CURRENT_TIMESTAMP(), tail_end_utc);

  SET effective_cap_minutes = IF(is_current_day,
                                 live_open_cap_minutes,
                                 no_leave_cap_minutes);

  CREATE TABLE IF NOT EXISTS
  `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` (
    interval_id       STRING NOT NULL,
    event_date        DATE   NOT NULL,
    meeting_id        STRING,
    meeting_uuid      STRING,
    participant_key   STRING NOT NULL,
    participant_name  STRING,
    participant_email STRING,
    room_uuid         STRING,
    room_name         STRING,
    room_category     STRING,
    start_ts          TIMESTAMP NOT NULL,
    end_ts            TIMESTAMP NOT NULL,
    duration_seconds  INT64,
    alone_seconds     INT64,
    snapshot_count    INT64,
    source            STRING,
    confidence        FLOAT64,
    built_at          TIMESTAMP
  )
  PARTITION BY event_date
  CLUSTER BY meeting_id, participant_key;

  -- Atomic swap. Two overlapping builds otherwise interleave their
  -- DELETE/INSERT and duplicate every interval.
  BEGIN TRANSACTION;

  DELETE FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  WHERE event_date = target_date;

  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    (interval_id, event_date, meeting_id, meeting_uuid, participant_key,
     participant_name, participant_email, room_uuid, room_name, room_category,
     start_ts, end_ts, duration_seconds, alone_seconds, snapshot_count,
     source, confidence, built_at)

  WITH
  -- ── 1. raw events: target day +/- 1 partition so midnight crossings survive ──
  raw_events AS (
    SELECT
      pe.event_id,
      pe.event_type,
      pe.event_timestamp,
      CAST(pe.meeting_id AS STRING) AS meeting_id,
      pe.meeting_uuid,
      pe.participant_name AS name_original,
      LOWER(TRIM(REGEXP_REPLACE(pe.participant_name, r'[-_]\d+$', ''))) AS name_normalized,
      LOWER(TRIM(pe.participant_email)) AS participant_email,
      pe.room_uuid,
      pe.room_name
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p` pe
    WHERE pe.event_date BETWEEN DATE_SUB(target_date, INTERVAL 1 DAY)
                            AND DATE_ADD(target_date, INTERVAL 1 DAY)
      AND pe.participant_name IS NOT NULL
      AND TRIM(pe.participant_name) != ''
      AND LOWER(pe.participant_name) NOT LIKE '%scout%'
      AND pe.event_type IN (
        'participant_joined','meeting.participant_joined',
        'participant_left','meeting.participant_left',
        'breakout_room_joined','breakout_room_left')
  ),

  -- ── 2. room_uuid -> room_name. Six evidence sources, lowest pri wins. ──
  --     v13 adds the two human-override tiers at the top.
  name_evidence AS (
    -- (0) OVERRIDE pinned to THIS date — the most specific statement there is
    SELECT room_uuid, room_name, 0 AS pri, 1000000 AS support, set_at AS seen
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_overrides`
    WHERE COALESCE(active, TRUE)
      AND mapping_date = target_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''

    UNION ALL
    -- (1) OVERRIDE with no date — holds for the whole life of this room UUID
    SELECT room_uuid, room_name, 1, 1000000, set_at
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_overrides`
    WHERE COALESCE(active, TRUE)
      AND mapping_date IS NULL
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''

    UNION ALL
    -- (2) room_mappings saved for THIS date: the deliberate machine record
    SELECT room_uuid, room_name, 2, COUNT(*), MAX(mapped_at)
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE mapping_date = target_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
    GROUP BY room_uuid, room_name

    UNION ALL
    -- (3) names seen in TODAY's event stream — MAJORITY wins.
    --     Zoom never sends a room name; the server guesses it when the
    --     webhook arrives and freezes the guess. Never let one event decide.
    SELECT room_uuid, room_name, 3, COUNT(*), MAX(event_timestamp)
    FROM raw_events
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%' AND room_name != 'Unknown Room'
      AND event_type IN ('breakout_room_joined','breakout_room_left')
      AND event_timestamp >= day_start_utc AND event_timestamp < tail_end_utc
    GROUP BY room_uuid, room_name

    UNION ALL
    -- (4) room_mappings from ANY date. Rescues a day the Room Mapper missed.
    SELECT room_uuid, room_name, 4, COUNT(*), MAX(mapped_at)
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
    GROUP BY room_uuid, room_name

    UNION ALL
    -- (5) names seen in the event stream on any recent day
    SELECT room_uuid, room_name, 5, COUNT(*), MAX(event_timestamp)
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p`
    WHERE event_date BETWEEN DATE_SUB(target_date, INTERVAL 60 DAY)
                         AND DATE_ADD(target_date, INTERVAL 1 DAY)
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%' AND room_name != 'Unknown Room'
      AND event_type IN ('breakout_room_joined','breakout_room_left')
    GROUP BY room_uuid, room_name
  ),

  resolved_names AS (
    SELECT
      room_uuid,
      ARRAY_AGG(room_name ORDER BY pri ASC, support DESC, seen DESC LIMIT 1)[OFFSET(0)]
        AS mapped_room_name
    FROM name_evidence
    GROUP BY room_uuid
  ),

  -- ── 2b. explicit CATEGORY overrides, same date-then-undated precedence ──
  category_overrides AS (
    SELECT
      room_uuid,
      ARRAY_AGG(room_category ORDER BY pri ASC, set_at DESC LIMIT 1)[OFFSET(0)]
        AS forced_category
    FROM (
      SELECT room_uuid, LOWER(TRIM(room_category)) AS room_category, 0 AS pri, set_at
      FROM `verve-attendance-tracker.breakout_room_calibrator.room_overrides`
      WHERE COALESCE(active, TRUE)
        AND mapping_date = target_date
        AND room_uuid IS NOT NULL AND room_uuid != ''
        AND room_category IS NOT NULL AND TRIM(room_category) != ''
      UNION ALL
      SELECT room_uuid, LOWER(TRIM(room_category)), 1, set_at
      FROM `verve-attendance-tracker.breakout_room_calibrator.room_overrides`
      WHERE COALESCE(active, TRUE)
        AND mapping_date IS NULL
        AND room_uuid IS NOT NULL AND room_uuid != ''
        AND room_category IS NOT NULL AND TRIM(room_category) != ''
    )
    GROUP BY room_uuid
  ),

  -- ── 2c. a room's name belongs to the ROOM, not to one webhook ──────────
  -- v11 let each event's own stamped name win, which is how a single
  -- mislabelled webhook renamed a three-hour stay. The name is decided once
  -- per room_uuid and applied to every event for that room.
  events_with_rooms AS (
    SELECT
      e.*,
      COALESCE(m.mapped_room_name, NULLIF(e.room_name, ''), 'Unknown Room')
        AS resolved_room_name
    FROM raw_events e
    LEFT JOIN resolved_names m ON e.room_uuid = m.room_uuid
  ),

  -- ── 3. identity: email when a cleaned name maps to exactly one email ──
  unique_email_per_name AS (
    SELECT name_normalized, ANY_VALUE(participant_email) AS mapped_email
    FROM events_with_rooms
    WHERE participant_email IS NOT NULL AND participant_email != ''
    GROUP BY name_normalized
    HAVING COUNT(DISTINCT participant_email) = 1
  ),

  events_with_key AS (
    SELECT e.*, COALESCE(u.mapped_email, e.name_normalized) AS participant_key
    FROM events_with_rooms e
    LEFT JOIN unique_email_per_name u ON e.name_normalized = u.name_normalized
  ),

  -- ── 4. classify + flag reconnect artifacts ──
  events_flagged AS (
    SELECT
      e.*,
      CASE
        WHEN e.event_type IN ('participant_joined','meeting.participant_joined') THEN '0.Main Room'
        WHEN e.event_type = 'breakout_room_joined' THEN COALESCE(e.resolved_room_name, 'Unknown Room')
        WHEN e.event_type = 'breakout_room_left'   THEN '0.Main Room'
        ELSE NULL
      END AS current_room,

      -- The room UUID this interval is actually IN. Only a breakout JOIN puts
      -- someone in a room: a breakout LEAVE carries the UUID of the room being
      -- left while starting a Main Room stay, so copying it here would tag
      -- main-room rows with a breakout room's ID.
      CASE
        WHEN e.event_type = 'breakout_room_joined' THEN NULLIF(e.room_uuid, '')
        ELSE NULL
      END AS current_room_uuid,

      CASE
        WHEN e.event_type IN ('participant_left','meeting.participant_left',
                              'participant_joined','meeting.participant_joined')
             AND EXISTS (
               SELECT 1 FROM events_with_key b
               WHERE b.participant_key = e.participant_key
                 AND b.meeting_id      = e.meeting_id
                 AND b.event_type      = 'breakout_room_joined'
                 AND TIMESTAMP_DIFF(e.event_timestamp, b.event_timestamp, MILLISECOND)
                     BETWEEN 0 AND reconnect_window_ms
             )
             AND EXISTS (
               SELECT 1
               FROM events_with_key l
               JOIN events_with_key j
                 ON  j.participant_key = l.participant_key
                 AND j.meeting_id      = l.meeting_id
               WHERE l.participant_key = e.participant_key
                 AND l.meeting_id      = e.meeting_id
                 AND l.event_type IN ('participant_left','meeting.participant_left')
                 AND j.event_type IN ('participant_joined','meeting.participant_joined')
                 AND ABS(TIMESTAMP_DIFF(j.event_timestamp, l.event_timestamp, MILLISECOND))
                     <= pair_tightness_ms
                 AND ABS(TIMESTAMP_DIFF(l.event_timestamp, e.event_timestamp, MILLISECOND))
                     <= reconnect_window_ms
                 AND ABS(TIMESTAMP_DIFF(j.event_timestamp, e.event_timestamp, MILLISECOND))
                     <= reconnect_window_ms
             )
        THEN TRUE
        ELSE FALSE
      END AS is_reconnect_artifact,

      CASE
        WHEN e.event_type = 'breakout_room_left'
             AND EXISTS (
               SELECT 1 FROM events_with_key l
               WHERE l.participant_key = e.participant_key
                 AND l.meeting_id      = e.meeting_id
                 AND l.event_type IN ('participant_left','meeting.participant_left')
                 AND ABS(TIMESTAMP_DIFF(l.event_timestamp, e.event_timestamp, MILLISECOND))
                     <= reconnect_window_ms
             )
        THEN TRUE
        ELSE FALSE
      END AS is_exit_teardown,

      CASE e.event_type
        WHEN 'breakout_room_left'         THEN 1
        WHEN 'breakout_room_joined'       THEN 2
        WHEN 'participant_joined'         THEN 3
        WHEN 'meeting.participant_joined' THEN 3
        ELSE 4
      END AS ord_class
    FROM events_with_key e
  ),

  -- ── 5. deterministic ordering + look-ahead ──
  events_ordered AS (
    SELECT
      e.*,
      LEAD(e.event_timestamp) OVER (
        PARTITION BY e.participant_key, e.meeting_id
        ORDER BY e.event_timestamp, e.ord_class, e.event_id
      ) AS next_event_ts,
      LEAD(e.event_type) OVER (
        PARTITION BY e.participant_key, e.meeting_id
        ORDER BY e.event_timestamp, e.ord_class, e.event_id
      ) AS next_event_type,
      MAX(IF(e.event_type IN ('participant_joined','meeting.participant_joined'),
             e.event_timestamp, NULL)) OVER (
        PARTITION BY e.participant_key, e.meeting_id
        ORDER BY e.event_timestamp, e.ord_class, e.event_id
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) AS session_start_ts
    FROM events_flagged e
    WHERE NOT e.is_reconnect_artifact
      AND e.event_timestamp >= day_start_utc
      AND e.event_timestamp <  tail_end_utc
  ),

  -- ── 6. one interval per room-setting event ──
  intervals_raw AS (
    SELECT
      e.participant_key,
      e.name_original AS participant_name,
      e.participant_email,
      e.meeting_id,
      e.meeting_uuid,
      e.current_room_uuid AS room_uuid,
      e.current_room AS room_name,
      e.event_timestamp AS start_ts,
      CASE
        WHEN e.next_event_ts IS NOT NULL
             AND e.next_event_type NOT IN ('participant_joined','meeting.participant_joined')
        THEN e.next_event_ts
        WHEN e.next_event_ts IS NOT NULL
        THEN e.event_timestamp
        ELSE LEAST(
               GREATEST(horizon, e.event_timestamp),
               TIMESTAMP_ADD(e.event_timestamp, INTERVAL effective_cap_minutes MINUTE))
      END AS end_ts,
      (e.next_event_ts IS NULL) AS used_open_end,
      e.session_start_ts
    FROM events_ordered e
    WHERE e.current_room IS NOT NULL
      AND NOT (e.next_event_ts IS NULL AND e.is_exit_teardown)
  ),

  -- ── 7. login-date rule: a session belongs to the IST day it started ──
  intervals_on_date AS (
    SELECT
      ir.*,
      TIMESTAMP_DIFF(ir.end_ts, ir.start_ts, SECOND) AS duration_seconds,
      -- An explicit category override wins; otherwise the name decides.
      COALESCE(
        co.forced_category,
        CASE
          WHEN LOWER(ir.room_name) LIKE '%break time%' THEN 'break'
          WHEN LOWER(ir.room_name) LIKE '%main%' OR ir.room_name = '0.Main Room' THEN 'main'
          ELSE 'breakout'
        END
      ) AS room_category
    FROM intervals_raw ir
    LEFT JOIN category_overrides co ON co.room_uuid = ir.room_uuid
    WHERE ir.session_start_ts IS NOT NULL
      AND ir.session_start_ts >= day_start_utc
      AND ir.session_start_ts <  day_end_utc
      AND ir.end_ts > ir.start_ts
  )

  SELECT
    GENERATE_UUID()   AS interval_id,
    target_date       AS event_date,
    meeting_id,
    meeting_uuid,
    participant_key,
    participant_name,
    NULLIF(participant_email, '') AS participant_email,
    room_uuid,
    room_name,
    room_category,
    start_ts,
    end_ts,
    duration_seconds,
    0 AS alone_seconds,
    0 AS snapshot_count,
    IF(room_category = 'main', 'webhook_fill', 'webhook_room') AS source,
    IF(used_open_end, 0.35, 0.5) AS confidence,
    CURRENT_TIMESTAMP() AS built_at
  FROM intervals_on_date
  WHERE duration_seconds >= min_segment_seconds;

  COMMIT TRANSACTION;

END;


-- ── AFTER RUNNING ───────────────────────────────────────────────────────────
--
-- 1. Rebuild a day so room_uuid gets populated:
--      CALL `verve-attendance-tracker.breakout_room_calibrator.sp_build_presence_intervals`(DATE '2026-08-11');
--
-- 2. Confirm the new column is filled for breakout rows and NULL for main:
--      SELECT room_category, COUNT(*) AS rows,
--             COUNTIF(room_uuid IS NOT NULL) AS with_uuid
--      FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
--      WHERE event_date = DATE '2026-08-11'
--      GROUP BY 1;
--    Expect: main -> with_uuid = 0, breakout/break -> with_uuid = every row.
--
-- 3. Correcting a room by hand (the My Day tab does exactly this):
--      INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.room_overrides`
--        (override_id, room_uuid, mapping_date, room_name, room_category,
--         note, set_by, set_at, active)
--      VALUES (GENERATE_UUID(), 'p0a38bAFprbeXhSRe/Xigw==', DATE '2026-08-11',
--              '8.0:BREAK TIME - Tea/Lunch/ Dinner', 'break',
--              'was labelled Creative Corner', 'you@verveadvisory.com',
--              CURRENT_TIMESTAMP(), TRUE);
--    then rebuild that date. Use mapping_date = NULL to make it permanent
--    for that room UUID.
--
-- 4. To retire an override, set active = FALSE and rebuild — never DELETE, so
--    the record of who changed what survives.
