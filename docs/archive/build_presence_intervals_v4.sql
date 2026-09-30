-- ============================================================================
-- BUILD PRESENCE_INTERVALS v4 - Final working version
--
-- FIXES:
-- 1. monitoring_end = last participant_left (supports midnight crossing)
-- 2. Room mappings use LATEST from both raw events AND room_mappings table
-- 3. IST times, minutes, filters <5s noise
--
-- Run in BigQuery console. Change target_date to rebuild another day.
-- ============================================================================

DECLARE target_date DATE DEFAULT DATE '2026-07-22';
DECLARE day_start_utc TIMESTAMP DEFAULT TIMESTAMP_SUB(TIMESTAMP(target_date), INTERVAL 330 MINUTE);
DECLARE day_end_utc TIMESTAMP DEFAULT TIMESTAMP_ADD(day_start_utc, INTERVAL 24 HOUR);

-- ============================================================================
-- STEP 1: Get all events for target date (and next day for midnight crossing)
-- ============================================================================
WITH raw_events AS (
  SELECT
    pe.event_id,
    pe.event_type,
    pe.event_timestamp,
    pe.event_date,
    CAST(pe.meeting_id AS STRING) AS meeting_id,
    pe.meeting_uuid,
    pe.participant_id,
    pe.participant_name AS name_original,
    LOWER(TRIM(REGEXP_REPLACE(pe.participant_name, r'[-_]\d+$', ''))) AS name_normalized,
    LOWER(TRIM(pe.participant_email)) AS participant_email,
    pe.room_uuid,
    pe.room_name
  FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p` pe
  WHERE pe.event_date BETWEEN target_date AND DATE_ADD(target_date, INTERVAL 1 DAY)
    AND pe.participant_name IS NOT NULL
    AND TRIM(pe.participant_name) != ''
    AND LOWER(pe.participant_name) NOT LIKE '%scout%'
    AND pe.event_type IN (
      'participant_joined', 'meeting.participant_joined',
      'participant_left', 'meeting.participant_left',
      'breakout_room_joined', 'breakout_room_left'
    )
),

-- ============================================================================
-- STEP 2: Get room mappings from BOTH sources, take the LATEST
-- Source 1: room_mappings table (from Zoom App panel)
-- Source 2: raw events themselves (webhook room names)
-- ============================================================================
mappings_from_table AS (
  SELECT
    room_uuid,
    room_name,
    mapped_at AS last_seen
  FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
  WHERE mapping_date = target_date
    AND room_uuid IS NOT NULL AND room_uuid != ''
    AND room_name IS NOT NULL AND room_name != ''
    AND room_name NOT LIKE 'Room-%'
),

mappings_from_events AS (
  SELECT
    room_uuid,
    room_name,
    MAX(event_timestamp) AS last_seen
  FROM raw_events
  WHERE room_uuid IS NOT NULL AND room_uuid != ''
    AND room_name IS NOT NULL AND room_name != ''
    AND room_name NOT LIKE 'Room-%'
    AND event_type IN ('breakout_room_joined', 'breakout_room_left')
  GROUP BY room_uuid, room_name
),

all_mappings AS (
  SELECT * FROM mappings_from_table
  UNION ALL
  SELECT * FROM mappings_from_events
),

latest_mappings AS (
  SELECT
    room_uuid,
    ARRAY_AGG(room_name ORDER BY last_seen DESC LIMIT 1)[OFFSET(0)] AS mapped_room_name
  FROM all_mappings
  GROUP BY room_uuid
),

-- ============================================================================
-- STEP 3: Resolve room names (prefer webhook name, fallback to mapping)
-- ============================================================================
events_with_rooms AS (
  SELECT
    e.*,
    CASE
      -- If webhook has real name, use it
      WHEN e.room_name IS NOT NULL
           AND e.room_name != ''
           AND e.room_name NOT LIKE 'Room-%'
      THEN e.room_name
      -- Otherwise use mapping
      ELSE COALESCE(m.mapped_room_name, e.room_name, 'Unknown Room')
    END AS resolved_room_name
  FROM raw_events e
  LEFT JOIN latest_mappings m ON e.room_uuid = m.room_uuid
),

-- ============================================================================
-- STEP 4: Build participant_key (prefer email if unique, else use name)
-- ============================================================================
unique_email_per_name AS (
  SELECT
    name_normalized,
    ANY_VALUE(participant_email) AS mapped_email
  FROM events_with_rooms
  WHERE participant_email IS NOT NULL AND participant_email != ''
  GROUP BY name_normalized
  HAVING COUNT(DISTINCT participant_email) = 1
),

events_with_key AS (
  SELECT
    e.*,
    COALESCE(u.mapped_email, e.name_normalized) AS participant_key
  FROM events_with_rooms e
  LEFT JOIN unique_email_per_name u ON e.name_normalized = u.name_normalized
),

-- ============================================================================
-- STEP 5: Get monitoring_end = LAST participant_left (meeting end time)
-- Allow up to 6 AM next day for midnight-crossing meetings
-- ============================================================================
monitoring_end AS (
  SELECT MAX(event_timestamp) AS end_ts
  FROM events_with_key
  WHERE event_type IN ('participant_left', 'meeting.participant_left')
    AND event_timestamp >= day_start_utc
    AND event_timestamp < TIMESTAMP_ADD(day_end_utc, INTERVAL 6 HOUR)  -- Up to 6 AM next day
),

-- ============================================================================
-- STEP 6: Order events per person and find next event timestamp
-- ============================================================================
events_ordered AS (
  SELECT
    e.event_id,
    e.event_type,
    e.event_timestamp,
    e.event_date,
    e.meeting_id,
    e.meeting_uuid,
    e.participant_id,
    e.name_original,
    e.name_normalized,
    e.participant_email,
    e.room_uuid,
    e.room_name,
    e.resolved_room_name,
    e.participant_key,
    LEAD(event_timestamp) OVER (
      PARTITION BY e.participant_key, e.meeting_id
      ORDER BY e.event_timestamp
    ) AS next_event_ts,
    LEAD(event_type) OVER (
      PARTITION BY e.participant_key, e.meeting_id
      ORDER BY e.event_timestamp
    ) AS next_event_type
  FROM events_with_key e
),

-- ============================================================================
-- STEP 7: Check for room transitions (fake participant_left)
-- ============================================================================
events_with_transition_check AS (
  SELECT
    e.*,
    EXISTS (
      SELECT 1
      FROM events_with_key e2
      WHERE e2.participant_key = e.participant_key
        AND e2.meeting_id = e.meeting_id
        AND e2.event_type = 'breakout_room_joined'
        AND ABS(TIMESTAMP_DIFF(e2.event_timestamp, e.event_timestamp, SECOND)) <= 5
    ) AS has_breakout_join_nearby,
    EXISTS (
      SELECT 1
      FROM events_with_key e3
      WHERE e3.participant_key = e.participant_key
        AND e3.meeting_id = e.meeting_id
        AND e3.event_type IN ('participant_joined', 'meeting.participant_joined')
        AND e3.event_timestamp > e.event_timestamp
        AND TIMESTAMP_DIFF(e3.event_timestamp, e.event_timestamp, SECOND) <= 30
    ) AS has_rejoin_within_30s
  FROM events_ordered e
),

-- ============================================================================
-- STEP 8: Determine current room and mark fake lefts
-- ============================================================================
events_with_state AS (
  SELECT
    *,
    CASE
      WHEN event_type IN ('participant_joined', 'meeting.participant_joined') THEN '0.Main Room'
      WHEN event_type = 'breakout_room_joined' THEN COALESCE(resolved_room_name, 'Unknown Room')
      WHEN event_type = 'breakout_room_left' THEN '0.Main Room'
      WHEN event_type IN ('participant_left', 'meeting.participant_left') THEN NULL
      ELSE NULL
    END AS current_room,
    CASE
      WHEN event_type IN ('participant_left', 'meeting.participant_left')
           AND has_breakout_join_nearby
           AND has_rejoin_within_30s
      THEN TRUE
      ELSE FALSE
    END AS is_fake_left
  FROM events_with_transition_check
),

-- ============================================================================
-- STEP 9: Build intervals
-- STRICT MODE v2: participant_joined = NEW SESSION (ends previous interval)
--   1. If next event is breakout/left → use it (continuation)
--   2. If next event is participant_joined → new session, end at start (0 duration)
--   3. If no next event → end at start (0 duration)
-- This fixes phantom hours when someone leaves and rejoins hours later.
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
    -- STRICT v2: participant_joined means new session, NOT continuation
    CASE
      -- Valid continuation: next event is NOT a rejoin (breakout events, participant_left)
      WHEN e.next_event_ts IS NOT NULL
           AND e.next_event_type NOT IN ('participant_joined', 'meeting.participant_joined')
      THEN e.next_event_ts
      -- Rejoin or no next event → interval ends at start (0 duration, filtered out)
      ELSE e.event_timestamp
    END AS end_ts,
    CASE
      WHEN e.next_event_ts IS NULL THEN TRUE
      WHEN e.next_event_type IN ('participant_joined', 'meeting.participant_joined') THEN TRUE
      ELSE FALSE
    END AS used_fallback_end
  FROM events_with_state e
  CROSS JOIN monitoring_end m
  WHERE
    e.current_room IS NOT NULL
    AND NOT e.is_fake_left
),

-- ============================================================================
-- STEP 10: Apply login-date rule - only intervals STARTING on target date
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
  WHERE
    start_ts >= day_start_utc
    AND start_ts < day_end_utc
    AND end_ts > start_ts
    AND end_ts IS NOT NULL
)

-- ============================================================================
-- FINAL OUTPUT - IST times, minutes
-- ============================================================================
SELECT
  target_date AS event_date,
  meeting_id,
  participant_key,
  participant_name,
  participant_email,
  room_name,
  room_category,
  FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(start_ts, INTERVAL 330 MINUTE)) AS start_ist,
  FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(end_ts, INTERVAL 330 MINUTE)) AS end_ist,
  CAST(CEIL(duration_seconds / 60.0) AS INT64) AS duration_mins,
  duration_seconds,
  CASE WHEN used_fallback_end THEN 0.5 ELSE 1.0 END AS confidence
FROM intervals_on_date
WHERE duration_seconds >= 5
ORDER BY participant_name, start_ts;
