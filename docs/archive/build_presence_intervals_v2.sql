-- ============================================================================
-- BUILD PRESENCE_INTERVALS - Complete Query for July 22, 2026
--
-- RULE: Trust all webhook data completely. No caps, no skipping.
-- If no leave event, use the meeting's last event as the end time.
--
-- Run in BigQuery console. Change the date in DECLARE to rebuild another day.
-- ============================================================================

DECLARE target_date DATE DEFAULT DATE '2026-07-22';
DECLARE day_start_utc TIMESTAMP DEFAULT TIMESTAMP_SUB(TIMESTAMP(target_date), INTERVAL 330 MINUTE);
DECLARE day_end_utc TIMESTAMP DEFAULT TIMESTAMP_ADD(day_start_utc, INTERVAL 24 HOUR);

-- ============================================================================
-- STEP 1: Get all events for the target date (and next day for midnight crossing)
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
    -- Keep original name for display
    pe.participant_name AS name_original,
    -- Normalize name for grouping
    LOWER(TRIM(REGEXP_REPLACE(pe.participant_name, r'[-_]\d+$', ''))) AS name_normalized,
    LOWER(TRIM(pe.participant_email)) AS participant_email,
    pe.room_uuid,
    pe.room_name
  FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p` pe
  WHERE pe.event_date BETWEEN target_date AND DATE_ADD(target_date, INTERVAL 1 DAY)
    AND pe.participant_name IS NOT NULL
    AND TRIM(pe.participant_name) != ''
    -- Exclude bot
    AND LOWER(pe.participant_name) NOT LIKE '%scout%'
    -- Only relevant event types
    AND pe.event_type IN (
      'participant_joined', 'meeting.participant_joined',
      'participant_left', 'meeting.participant_left',
      'breakout_room_joined', 'breakout_room_left'
    )
),

-- ============================================================================
-- STEP 2: Get the LAST event time of the entire meeting (fallback for missing leave)
-- ============================================================================
meeting_last_event AS (
  SELECT
    meeting_id,
    MAX(event_timestamp) AS last_event_ts
  FROM raw_events
  GROUP BY meeting_id
),

-- ============================================================================
-- STEP 3: Get latest room mappings for resolving Room-xxxx placeholders
-- ============================================================================
latest_mappings AS (
  SELECT
    room_uuid,
    room_name AS mapped_room_name
  FROM (
    SELECT
      room_uuid,
      room_name,
      ROW_NUMBER() OVER (PARTITION BY room_uuid ORDER BY mapped_at DESC) AS rn
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE mapping_date = target_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
  )
  WHERE rn = 1
),

-- ============================================================================
-- STEP 4: Resolve room names
-- ============================================================================
events_with_rooms AS (
  SELECT
    e.*,
    CASE
      WHEN e.room_name LIKE 'Room-%' OR e.room_name IS NULL OR e.room_name = ''
      THEN COALESCE(m.mapped_room_name, e.room_name, 'Unknown Room')
      ELSE e.room_name
    END AS resolved_room_name
  FROM raw_events e
  LEFT JOIN latest_mappings m ON e.room_uuid = m.room_uuid
),

-- ============================================================================
-- STEP 5: Build participant_key (prefer email if unique, else use name)
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
    m.last_event_ts AS meeting_last_event_ts,
    LEAD(event_timestamp) OVER (
      PARTITION BY e.participant_key, e.meeting_id
      ORDER BY e.event_timestamp
    ) AS next_event_ts,
    LEAD(event_type) OVER (
      PARTITION BY e.participant_key, e.meeting_id
      ORDER BY e.event_timestamp
    ) AS next_event_type
  FROM events_with_key e
  LEFT JOIN meeting_last_event m ON e.meeting_id = m.meeting_id
),

-- ============================================================================
-- STEP 7: Check for room transitions (fake participant_left)
-- Rule: participant_left is FAKE if:
--   1. There's a breakout_room_joined within 5 seconds, AND
--   2. There's a participant_joined within 30 seconds after
-- ============================================================================
events_with_transition_check AS (
  SELECT
    e.*,
    -- Check if there's a breakout_room_joined within 5 seconds
    EXISTS (
      SELECT 1
      FROM events_with_key e2
      WHERE e2.participant_key = e.participant_key
        AND e2.meeting_id = e.meeting_id
        AND e2.event_type = 'breakout_room_joined'
        AND ABS(TIMESTAMP_DIFF(e2.event_timestamp, e.event_timestamp, SECOND)) <= 5
    ) AS has_breakout_join_nearby,
    -- Check if there's a participant_joined within 30 seconds AFTER
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
    -- What room is the person in after this event?
    CASE
      WHEN event_type IN ('participant_joined', 'meeting.participant_joined') THEN '0.Main Room'
      WHEN event_type = 'breakout_room_joined' THEN COALESCE(resolved_room_name, 'Unknown Room')
      WHEN event_type = 'breakout_room_left' THEN '0.Main Room'
      WHEN event_type IN ('participant_left', 'meeting.participant_left') THEN NULL
      ELSE NULL
    END AS current_room,
    -- Is this participant_left a fake (room transition)?
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
-- STEP 9: Build intervals - each event that starts a room stay
-- ============================================================================
intervals_raw AS (
  SELECT
    participant_key,
    name_original AS participant_name,
    participant_email,
    meeting_id,
    meeting_uuid,
    current_room AS room_name,
    event_timestamp AS start_ts,
    -- End time: next event, or if none, use meeting's last event
    COALESCE(next_event_ts, meeting_last_event_ts) AS end_ts,
    -- Flag if we used fallback
    CASE WHEN next_event_ts IS NULL THEN TRUE ELSE FALSE END AS used_fallback_end
  FROM events_with_state
  WHERE
    -- Must have a room (filters out participant_left)
    current_room IS NOT NULL
    -- Skip fake lefts
    AND NOT is_fake_left
),

-- ============================================================================
-- STEP 10: Apply login-date rule - only intervals STARTING on target date
-- ============================================================================
intervals_on_date AS (
  SELECT
    *,
    TIMESTAMP_DIFF(end_ts, start_ts, SECOND) AS duration_seconds,
    -- Room category
    CASE
      WHEN LOWER(room_name) LIKE '%break time%' THEN 'break'
      WHEN LOWER(room_name) LIKE '%main%' OR room_name = '0.Main Room' THEN 'main'
      ELSE 'breakout'
    END AS room_category
  FROM intervals_raw
  WHERE
    -- Login-date rule: interval must START on target date (IST)
    start_ts >= day_start_utc
    AND start_ts < day_end_utc
    -- End must be after start
    AND end_ts > start_ts
)

-- ============================================================================
-- FINAL OUTPUT
-- ============================================================================
SELECT
  GENERATE_UUID() AS interval_id,
  target_date AS event_date,
  meeting_id,
  meeting_uuid,
  participant_key,
  participant_name,
  participant_email,
  room_name,
  room_category,
  start_ts,
  end_ts,
  duration_seconds,
  0 AS alone_seconds,
  0 AS snapshot_count,
  'sql_builder_v2' AS source,
  CASE WHEN used_fallback_end THEN 0.5 ELSE 1.0 END AS confidence,
  CURRENT_TIMESTAMP() AS built_at
FROM intervals_on_date
ORDER BY participant_name, start_ts;
