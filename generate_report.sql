WITH events AS (
  SELECT
    participant_name,
    participant_email,
    event_type,
    PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', event_timestamp) as ts,
    IFNULL(room_name, 'Main Room') as room
  FROM `variant-finance-data-project.breakout_room_calibrator.participant_events`
  WHERE DATE(event_date) = '2026-03-30'
),
first_last AS (
  SELECT
    participant_name,
    participant_email,
    MIN(CASE WHEN event_type = 'participant_joined' THEN ts END) as first_join,
    MAX(CASE WHEN event_type = 'participant_left' THEN ts END) as last_leave
  FROM events
  GROUP BY participant_name, participant_email
),
room_visits AS (
  SELECT
    participant_name,
    room,
    MIN(ts) as enter_time,
    MAX(ts) as exit_time
  FROM events
  WHERE event_type IN ('breakout_room_joined', 'breakout_room_left')
  GROUP BY participant_name, room
)
SELECT
  f.participant_name as Name,
  f.participant_email as Email,
  FORMAT_TIMESTAMP('%H:%M', f.first_join, 'Asia/Kolkata') as Main_Joined_IST,
  FORMAT_TIMESTAMP('%H:%M', f.last_leave, 'Asia/Kolkata') as Main_Left_IST,
  CONCAT(
    CAST(TIMESTAMP_DIFF(f.last_leave, f.first_join, MINUTE) / 60 AS INT64), 'h ',
    MOD(TIMESTAMP_DIFF(f.last_leave, f.first_join, MINUTE), 60), 'm'
  ) as Total_Duration,
  STRING_AGG(
    CONCAT(r.room, ' [', FORMAT_TIMESTAMP('%H:%M', r.enter_time, 'Asia/Kolkata'), '-', FORMAT_TIMESTAMP('%H:%M', r.exit_time, 'Asia/Kolkata'), ']'),
    ' -> '
    ORDER BY r.enter_time
  ) as Room_History
FROM first_last f
LEFT JOIN room_visits r ON f.participant_name = r.participant_name
WHERE f.participant_name IS NOT NULL
GROUP BY f.participant_name, f.participant_email, f.first_join, f.last_leave
ORDER BY f.participant_name
