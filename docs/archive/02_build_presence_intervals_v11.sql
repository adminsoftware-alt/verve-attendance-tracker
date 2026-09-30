-- ============================================================================
-- sp_build_presence_intervals — v11
--
-- ARCHIVE COPY of the procedure that was live in BigQuery on 2026-08-11,
-- captured from INFORMATION_SCHEMA.ROUTINES. v8 through v11 existed only
-- inside BigQuery; the repo's newest copy was v7. This file closes that gap
-- so the code that calculates everyone's hours is readable without querying
-- the warehouse.
--
-- DO NOT RUN THIS to "restore" anything — it is superseded by
-- 06_fix_room_name_v12.sql, which fixes the room-name precedence bug that
-- filed 187 minutes of Creative Corner time as BREAK TIME on 2026-08-11.
-- Kept verbatim, bug included, as the record of what v11 actually did.
-- ============================================================================

CREATE PROCEDURE `verve-attendance-tracker`.breakout_room_calibrator.sp_build_presence_intervals(target_date DATE)
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

  BEGIN TRANSACTION;

  DELETE FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  WHERE event_date = target_date;

  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    (interval_id, event_date, meeting_id, meeting_uuid, participant_key,
     participant_name, participant_email, room_name, room_category,
     start_ts, end_ts, duration_seconds, alone_seconds, snapshot_count,
     source, confidence, built_at)

  WITH
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

  name_evidence AS (
    SELECT room_uuid, room_name, 1 AS pri, MAX(event_timestamp) AS seen
    FROM raw_events
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%' AND room_name != 'Unknown Room'
      AND event_type IN ('breakout_room_joined','breakout_room_left')
      AND event_timestamp >= day_start_utc AND event_timestamp < tail_end_utc
    GROUP BY room_uuid, room_name

    UNION ALL
    SELECT room_uuid, room_name, 2, MAX(mapped_at)
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE mapping_date = target_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
    GROUP BY room_uuid, room_name

    UNION ALL
    SELECT room_uuid, room_name, 3, MAX(mapped_at)
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
    GROUP BY room_uuid, room_name

    UNION ALL
    SELECT room_uuid, room_name, 4, MAX(event_timestamp)
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
      ARRAY_AGG(room_name ORDER BY pri ASC, seen DESC LIMIT 1)[OFFSET(0)] AS mapped_room_name
    FROM name_evidence
    GROUP BY room_uuid
  ),

  -- *** THE v11 BUG ***
  -- If the event carries ANY non-placeholder name, that name wins outright and
  -- resolved_names is never consulted. A single webhook stamped with the wrong
  -- room name therefore beats every other piece of evidence, including the
  -- deliberate room_mappings record.
  events_with_rooms AS (
    SELECT
      e.*,
      CASE
        WHEN e.room_name IS NOT NULL AND e.room_name != ''
             AND e.room_name NOT LIKE 'Room-%' AND e.room_name != 'Unknown Room'
        THEN e.room_name
        ELSE COALESCE(m.mapped_room_name, e.room_name, 'Unknown Room')
      END AS resolved_room_name
    FROM raw_events e
    LEFT JOIN resolved_names m ON e.room_uuid = m.room_uuid
  ),

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

  events_flagged AS (
    SELECT
      e.*,
      CASE
        WHEN e.event_type IN ('participant_joined','meeting.participant_joined') THEN '0.Main Room'
        WHEN e.event_type = 'breakout_room_joined' THEN COALESCE(e.resolved_room_name, 'Unknown Room')
        WHEN e.event_type = 'breakout_room_left'   THEN '0.Main Room'
        ELSE NULL
      END AS current_room,

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

  intervals_raw AS (
    SELECT
      e.participant_key,
      e.name_original AS participant_name,
      e.participant_email,
      e.meeting_id,
      e.meeting_uuid,
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

  intervals_on_date AS (
    SELECT
      *,
      TIMESTAMP_DIFF(end_ts, start_ts, SECOND) AS duration_seconds,
      CASE
        WHEN LOWER(room_name) LIKE '%break time%' THEN 'break'
        WHEN LOWER(room_name) LIKE '%main%' OR room_name = '0.Main Room' THEN 'main'
        ELSE 'breakout'
      END AS room_category
    FROM intervals_raw
    WHERE session_start_ts IS NOT NULL
      AND session_start_ts >= day_start_utc
      AND session_start_ts <  day_end_utc
      AND end_ts > start_ts
  )

  SELECT
    GENERATE_UUID()   AS interval_id,
    target_date       AS event_date,
    meeting_id,
    meeting_uuid,
    participant_key,
    participant_name,
    NULLIF(participant_email, '') AS participant_email,
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

END
