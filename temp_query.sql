SELECT
  participant_name,
  event_type,
  FORMAT_TIMESTAMP('%H:%M:%S', PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', event_timestamp), 'Asia/Kolkata') as time_ist,
  IFNULL(room_name, 'Main Room') as room
FROM `variant-finance-data-project.breakout_room_calibrator.participant_events`
WHERE DATE(event_date) = '2026-03-30'
  AND LOWER(participant_name) LIKE '%shashank channawar%'
ORDER BY event_timestamp DESC
LIMIT 10
