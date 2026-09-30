-- ============================================================================
-- BUILD PRESENCE_INTERVALS v7
--
-- WHAT CHANGED vs v6 (both fixes verified against Harshita Rajput's raw events, 21 July)
--
-- 1. RECONNECT PAIRS ARE NOW ORDER-INDEPENDENT AND NOT TIED TO A 5s BREAKOUT WINDOW.
--    Zoom fires a participant_left + participant_joined pair on every breakout move, but the
--    delivery ORDER of that pair is not guaranteed and the gap to the breakout event is not
--    bounded by 5s. v6 required left-then-join AND a breakout_room_joined within 5s, so:
--      - 19:10:56 brk_joined / 19:10:57 joined / 19:10:57 left  (join arrived FIRST)
--        was read as a real exit, and 46 min in Virtual Vista was lost.
--      - 14:53:04 brk_joined(BREAK TIME) / 14:53:12 left / 14:53:12 joined  (8s gap, > 5s)
--        was read as a real reconnect, so the surviving joined re-opened '0.Main Room' and
--        78 min of BREAK TIME was billed as working time.
--    v7 rule: a participant_left and a participant_joined within 30s of each other, in EITHER
--    order, are the same reconnect artifact - drop both. The breakout stream carries the room.
--
-- 2. DETERMINISTIC EVENT ORDER. Several events share a timestamp to the second; LEAD() ordered
--    only by event_timestamp gave non-deterministic results. Ties now break by event class
--    (leave-a-room, then join-a-room, then leave-the-meeting) and finally event_id.
--
-- 3. Room-name evidence is scoped to the target IST day (+6h tail) because room names rotate
--    daily and the event scan spans three partitions.
--
-- Principle unchanged: trust the webhooks. No 600/240-minute caps. Phantom time is prevented by
-- pairing events correctly, not by clamping durations.
-- ============================================================================

DECLARE target_date DATE DEFAULT DATE '2026-07-21';
DECLARE day_start_utc TIMESTAMP DEFAULT TIMESTAMP_SUB(TIMESTAMP(target_date), INTERVAL 330 MINUTE);
DECLARE day_end_utc TIMESTAMP DEFAULT TIMESTAMP_ADD(day_start_utc, INTERVAL 24 HOUR);

-- ============================================================================
-- STEP 1: Raw events. Three partitions: the target day plus one either side, so a session that
-- starts late on the target day and ends after midnight is fully visible.
-- ============================================================================
WITH raw_events AS (
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
      'participant_joined', 'meeting.participant_joined',
      'participant_left',   'meeting.participant_left',
      'breakout_room_joined', 'breakout_room_left'
    )
),

-- ============================================================================
-- STEP 2: Resolve room_uuid -> room name. Two sources, newest wins.
-- Webhooks often carry a 'Room-xxxx' placeholder on join and the real name on leave, so the
-- event stream itself is the better source; room_mappings fills the rest.
-- ============================================================================
mappings_from_table AS (
  SELECT room_uuid, room_name, mapped_at AS last_seen
  FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
  WHERE mapping_date = target_date
    AND room_uuid IS NOT NULL AND room_uuid != ''
    AND room_name IS NOT NULL AND room_name != ''
    AND room_name NOT LIKE 'Room-%'
),

mappings_from_events AS (
  SELECT room_uuid, room_name, MAX(event_timestamp) AS last_seen
  FROM raw_events
  WHERE room_uuid IS NOT NULL AND room_uuid != ''
    AND room_name IS NOT NULL AND room_name != ''
    AND room_name NOT LIKE 'Room-%'
    AND event_type IN ('breakout_room_joined', 'breakout_room_left')
    -- room names rotate daily, so only trust names seen during this IST day
    AND event_timestamp >= day_start_utc
    AND event_timestamp <  TIMESTAMP_ADD(day_end_utc, INTERVAL 6 HOUR)
  GROUP BY room_uuid, room_name
),

latest_mappings AS (
  SELECT
    room_uuid,
    ARRAY_AGG(room_name ORDER BY last_seen DESC LIMIT 1)[OFFSET(0)] AS mapped_room_name
  FROM (
    SELECT * FROM mappings_from_table
    UNION ALL
    SELECT * FROM mappings_from_events
  )
  GROUP BY room_uuid
),

events_with_rooms AS (
  SELECT
    e.*,
    CASE
      WHEN e.room_name IS NOT NULL AND e.room_name != '' AND e.room_name NOT LIKE 'Room-%'
      THEN e.room_name
      ELSE COALESCE(m.mapped_room_name, e.room_name, 'Unknown Room')
    END AS resolved_room_name
  FROM raw_events e
  LEFT JOIN latest_mappings m ON e.room_uuid = m.room_uuid
),

-- ============================================================================
-- STEP 3: Identity. Use the email when a normalised name maps to exactly one email that day,
-- otherwise fall back to the name. Distinct names stay distinct: "Harsha Rajput" and
-- "Harshita Rajput" are two different people and must not merge.
-- ============================================================================
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

-- ============================================================================
-- STEP 4: Classify each event and flag reconnect artifacts.
--
-- current_room = the room the person is in AFTER this event. participant_left sets NULL: it
-- ends a stay rather than starting one, but it stays in the stream because the next step needs
-- it as an end marker.
--
-- is_reconnect_artifact = this left (or join) has a matching join (or left) within 30 seconds
-- in either direction. Zoom emits that pair on every breakout move and on brief drop-outs; the
-- person never actually left. A real exit has no counterpart for minutes or hours.
-- ============================================================================
events_flagged AS (
  SELECT
    e.*,
    CASE
      WHEN e.event_type IN ('participant_joined', 'meeting.participant_joined') THEN '0.Main Room'
      WHEN e.event_type = 'breakout_room_joined' THEN COALESCE(e.resolved_room_name, 'Unknown Room')
      WHEN e.event_type = 'breakout_room_left'   THEN '0.Main Room'
      ELSE NULL
    END AS current_room,

    CASE
      WHEN e.event_type IN ('participant_left', 'meeting.participant_left')
           AND EXISTS (
             SELECT 1 FROM events_with_key p
             WHERE p.participant_key = e.participant_key
               AND p.meeting_id      = e.meeting_id
               AND p.event_type IN ('participant_joined', 'meeting.participant_joined')
               AND ABS(TIMESTAMP_DIFF(p.event_timestamp, e.event_timestamp, SECOND)) <= 30
           )
      THEN TRUE
      WHEN e.event_type IN ('participant_joined', 'meeting.participant_joined')
           AND EXISTS (
             SELECT 1 FROM events_with_key p
             WHERE p.participant_key = e.participant_key
               AND p.meeting_id      = e.meeting_id
               AND p.event_type IN ('participant_left', 'meeting.participant_left')
               AND ABS(TIMESTAMP_DIFF(p.event_timestamp, e.event_timestamp, SECOND)) <= 30
           )
      THEN TRUE
      ELSE FALSE
    END AS is_reconnect_artifact
  FROM events_with_key e
),

-- ============================================================================
-- STEP 5: Order the surviving stream and look ahead.
-- Ties are common (Zoom stamps several events to the same second). Order within a timestamp:
-- leave a breakout, then join a breakout, then join the meeting, then leave the meeting.
-- event_id makes the result stable when even that ties.
-- ============================================================================
events_ordered AS (
  SELECT
    e.*,
    LEAD(e.event_timestamp) OVER (
      PARTITION BY e.participant_key, e.meeting_id
      ORDER BY e.event_timestamp,
               CASE e.event_type
                 WHEN 'breakout_room_left'   THEN 1
                 WHEN 'breakout_room_joined' THEN 2
                 WHEN 'participant_joined'   THEN 3
                 WHEN 'meeting.participant_joined' THEN 3
                 ELSE 4
               END,
               e.event_id
    ) AS next_event_ts,
    LEAD(e.event_type) OVER (
      PARTITION BY e.participant_key, e.meeting_id
      ORDER BY e.event_timestamp,
               CASE e.event_type
                 WHEN 'breakout_room_left'   THEN 1
                 WHEN 'breakout_room_joined' THEN 2
                 WHEN 'participant_joined'   THEN 3
                 WHEN 'meeting.participant_joined' THEN 3
                 ELSE 4
               END,
               e.event_id
    ) AS next_event_type
  FROM events_flagged e
  WHERE NOT e.is_reconnect_artifact
),

-- ============================================================================
-- STEP 6: One interval per room-setting event, ending at the next event.
-- A surviving participant_joined means a genuinely new session, so anything still open before it
-- had no leave webhook and we do not know when it ended: it collapses to zero and is dropped.
-- That is what keeps a 02:56 exit from being stitched to a 16:00 return.
-- ============================================================================
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
           AND e.next_event_type NOT IN ('participant_joined', 'meeting.participant_joined')
      THEN e.next_event_ts
      ELSE e.event_timestamp
    END AS end_ts,
    (e.next_event_ts IS NULL
     OR e.next_event_type IN ('participant_joined', 'meeting.participant_joined')) AS used_fallback_end
  FROM events_ordered e
  WHERE e.current_room IS NOT NULL
),

-- ============================================================================
-- STEP 7: Login-date rule - a session belongs to the IST day it started on.
-- ============================================================================
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
  WHERE start_ts >= day_start_utc
    AND start_ts <  day_end_utc
    AND end_ts   >  start_ts
)

SELECT
  target_date AS event_date,
  meeting_id,
  participant_key,
  participant_name,
  participant_email,
  room_name,
  room_category,
  FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(start_ts, INTERVAL 330 MINUTE)) AS start_ist,
  FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(end_ts,   INTERVAL 330 MINUTE)) AS end_ist,
  CAST(CEIL(duration_seconds / 60.0) AS INT64) AS duration_mins,
  duration_seconds,
  CASE WHEN used_fallback_end THEN 0.5 ELSE 1.0 END AS confidence
FROM intervals_on_date
WHERE duration_seconds >= 5
ORDER BY participant_name, start_ts;
