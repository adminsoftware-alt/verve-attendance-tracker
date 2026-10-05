-- ============================================================================
-- v17 = v16 + ONE addition: ignore a late "left old room" event (2026-10-05)
--
-- Zoom sometimes sends "left room A" up to ~45 s AFTER "joined room B"
-- (Aman Jalan 2026-10-05: joined 1.20 at 19:14:52, "left 1.15" at 19:15:35).
-- Read as-is, that late leave pulled the person out of room B and filed the
-- rest of the stay as 0.Main Room, so Live showed people in Main Room who
-- were sitting in a breakout room. v17 drops a breakout_room_left for a
-- DIFFERENT room than the one just joined when it arrives within 120 s of
-- that join. Marked "ADDITION (v17)" below; everything else is v16 verbatim.
--
-- Measured side by side with v16 before install (5 days): total hours
-- identical every day; 1-7 people a day affected; 0.2-3.7 h a day moved from
-- Main Room to the room the person was really in; break hours +1.4 h
-- (2026-09-29) and +2.1 h (2026-09-30), unchanged on the other days.
--
-- ROLLBACK: re-run docs/12_build_presence_intervals_v16.sql.
-- ============================================================================
-- ============================================================================
-- v16 = the TESTED v11 calculation, untouched, + additions only (2026-10-05)
--
-- Every line of v11's logic is kept exactly: reconnect-pair detection, event
-- ordering, open-segment caps, the 05:00 IST day, the login-date rule, and
-- v11's room-name rule (an event's own real name wins; otherwise v11's
-- four-source lookup).
--
-- ADDITIONS (each marked "ADDITION (v16)" in the text):
--   1. room_uuid is written to presence_intervals   (Live rename needs it)
--   2. one row per event_id                         (duplicate events)
--   3. HR's room name for the day wins              (pencil / dropdown)
--   4. same-meeting mapping tried before v11's lookup, only for events that
--      have no real name of their own               (room-id reuse)
--   5. HR-forced category (break / breakout / main)
--
-- ROLLBACK: re-run docs/11_build_presence_intervals_v15_1.sql.
-- ============================================================================
CREATE OR REPLACE PROCEDURE `verve-attendance-tracker`.breakout_room_calibrator.sp_build_presence_intervals(target_date DATE)
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
  -- ADDITION (v17): how long after joining room B a 'left room A' may still arrive
  DECLARE stale_leave_window_s   INT64 DEFAULT  120;

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

  BEGIN TRANSACTION;

  DELETE FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  WHERE event_date = target_date;

  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    (interval_id, event_date, meeting_id, meeting_uuid, participant_key,
     participant_name, participant_email, room_uuid, room_name, room_category,
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
    -- ADDITION (v16): same Zoom event stored twice -> keep one row.
    QUALIFY ROW_NUMBER() OVER (PARTITION BY pe.event_id ORDER BY pe.inserted_at) = 1
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

  -- ADDITION (v16): a human said what this room is, for this date.
  override_names AS (
    SELECT
      room_uuid,
      ARRAY_AGG(room_name ORDER BY set_at DESC LIMIT 1)[OFFSET(0)] AS room_name
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_overrides`
    WHERE COALESCE(active, TRUE)
      AND mapping_date = target_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
    GROUP BY room_uuid
  ),

  -- ADDITION (v16): a mapping saved for THIS meeting instance + this room.
  resolved_names_scoped AS (
    SELECT
      room_uuid,
      meeting_uuid,
      ARRAY_AGG(room_name ORDER BY support DESC, seen DESC LIMIT 1)[OFFSET(0)]
        AS mapped_room_name
    FROM (
      SELECT room_uuid, meeting_uuid, room_name,
             COUNT(*) AS support, MAX(mapped_at) AS seen
      FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
      WHERE meeting_uuid IS NOT NULL AND meeting_uuid != ''
        AND room_uuid IS NOT NULL AND room_uuid != ''
        AND room_name IS NOT NULL AND room_name != ''
        AND room_name NOT LIKE 'Room-%'
      GROUP BY room_uuid, meeting_uuid, room_name
    )
    GROUP BY room_uuid, meeting_uuid
  ),

  -- ADDITION (v16): a human forced the category (break / breakout / main).
  category_overrides AS (
    SELECT
      room_uuid,
      ARRAY_AGG(LOWER(TRIM(room_category)) ORDER BY set_at DESC LIMIT 1)[OFFSET(0)]
        AS forced_category
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_overrides`
    WHERE COALESCE(active, TRUE)
      AND mapping_date = target_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_category IS NOT NULL AND TRIM(room_category) != ''
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
      COALESCE(
        ov.room_name,          -- ADDITION (v16): HR's name for this room today wins
        CASE
        WHEN e.room_name IS NOT NULL AND e.room_name != ''
             AND e.room_name NOT LIKE 'Room-%' AND e.room_name != 'Unknown Room'
        THEN e.room_name
        -- ADDITION (v16): ms = same-meeting mapping, tried before v11's chain
        ELSE COALESCE(ms.mapped_room_name, m.mapped_room_name, e.room_name, 'Unknown Room')
        END
      ) AS resolved_room_name
    FROM raw_events e
    LEFT JOIN resolved_names m ON e.room_uuid = m.room_uuid
    LEFT JOIN override_names ov ON e.room_uuid = ov.room_uuid
    LEFT JOIN resolved_names_scoped ms
           ON e.room_uuid = ms.room_uuid AND e.meeting_uuid = ms.meeting_uuid
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

      -- ADDITION (v16): the room id of this stay (needed to rename a room).
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

  -- ADDITION (v17): LATE "LEFT OLD ROOM" EVENTS.
  -- Zoom sometimes sends "left room A" up to ~45 s AFTER "joined room B"
  -- (Aman Jalan 2026-10-05: joined 1.20 at 19:14:52, "left 1.15" at 19:15:35).
  -- Read as-is, that late leave pulls the person out of room B and files the
  -- rest of the stay as 0.Main Room. Joining room B already ended room A, so a
  -- breakout_room_left for a DIFFERENT room than the one just joined, arriving
  -- within stale_leave_window_s of that join, is dropped. Nothing else changes.
  events_live AS (
    SELECT f.* EXCEPT (last_join_room, last_join_ts)
    FROM (
      SELECT
        e.*,
        LAST_VALUE(IF(e.event_type = 'breakout_room_joined' AND NULLIF(e.room_uuid, '') IS NOT NULL,
                      e.room_uuid, NULL) IGNORE NULLS) OVER w AS last_join_room,
        LAST_VALUE(IF(e.event_type = 'breakout_room_joined' AND NULLIF(e.room_uuid, '') IS NOT NULL,
                      e.event_timestamp, NULL) IGNORE NULLS) OVER w AS last_join_ts
      FROM events_flagged e
      WHERE NOT e.is_reconnect_artifact
      WINDOW w AS (PARTITION BY e.participant_key, e.meeting_id
                   ORDER BY e.event_timestamp, e.ord_class, e.event_id
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
    ) f
    WHERE NOT (
      f.event_type = 'breakout_room_left'
      AND NULLIF(f.room_uuid, '') IS NOT NULL
      AND f.last_join_room IS NOT NULL
      AND f.room_uuid != f.last_join_room
      AND TIMESTAMP_DIFF(f.event_timestamp, f.last_join_ts, SECOND)
          BETWEEN 0 AND stale_leave_window_s
    )
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
    FROM events_live e
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

  intervals_on_date AS (
    SELECT
      ir.*,
      TIMESTAMP_DIFF(end_ts, start_ts, SECOND) AS duration_seconds,
      COALESCE(
        co.forced_category,    -- ADDITION (v16): HR-forced category
      CASE
        WHEN LOWER(room_name) LIKE '%break time%' THEN 'break'
        WHEN LOWER(room_name) LIKE '%main%' OR room_name = '0.Main Room' THEN 'main'
        ELSE 'breakout'
      END
      ) AS room_category
    FROM intervals_raw ir
    LEFT JOIN category_overrides co ON co.room_uuid = ir.room_uuid
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
