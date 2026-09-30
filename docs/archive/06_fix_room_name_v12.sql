-- ============================================================================
-- sp_build_presence_intervals — v12
-- Paste this WHOLE file into BigQuery and click Run. It replaces v11.
--
-- WHAT THIS FIXES
-- Zoom's breakout webhooks carry only a room UUID, never a room name. The
-- server fills the name in from memory at the moment the webhook arrives and
-- freezes that guess into participant_events_p.room_name. When the guess is
-- wrong, v11 believed it anyway:
--
--     WHEN e.room_name ... NOT LIKE 'Room-%' THEN e.room_name   -- v11
--
-- One non-placeholder name on the event won outright, and resolved_names —
-- which holds room_mappings, the deliberate record — was never consulted.
--
-- OBSERVED 2026-08-11: room p0a38bAFprbeXhSRe/Xigw== is "1.16:Creative Corner
-- - Team Dev" in room_mappings, and carried three different names across the
-- morning's events. One join event at 08:52:14 was stamped
-- "8.0:BREAK TIME - Tea/Lunch/ Dinner" (the name of the room the participant
-- had just LEFT). That single event named the whole 08:52->11:59 stay, so
-- 187 minutes of work were filed as break. Two people in the same room at the
-- same time came out with different room names.
--
-- TWO CHANGES
--
-- 1. events_with_rooms now uses the CONSENSUS name for the room UUID instead
--    of the name stamped on the individual event. This is the essential fix:
--    the room's identity is a property of the room, not of one webhook.
--    Low blast radius — resolved_room_name feeds exactly one expression,
--    the breakout_room_joined branch of current_room. Main Room handling
--    (which has no room_uuid) is untouched.
--
-- 2. name_evidence is re-ranked:
--      pri 1  room_mappings for the target date  (deliberate, same-day)
--      pri 2  today's event stream, MAJORITY first
--      pri 3  room_mappings from any date
--      pri 4  the event stream over the last 60 days
--    v11 put today's event stream first and broke ties by recency alone, so
--    the newest stamp won even when it was outvoted 6-to-1. Ties are now
--    broken by how many events support a name, then by recency.
--
--    TRADE-OFF, stated plainly: this trusts room_mappings over the live event
--    stream on the target date. room_mappings is written by the same SDK
--    lookup that produces the bad names, so it is not automatically clean —
--    health check 16 is what watches it for a UUID carrying two names. If 16
--    ever alarms, this precedence is the thing to revisit.
--
-- NOT A ROOT-CAUSE FIX. The lookup still resolves a room by asking "which
-- room is this participant in NOW", so it still mislabels anyone who has
-- moved on. This makes the builder resistant to that; it does not stop it.
--
-- NOT EXECUTED by the author — no BigQuery credentials. After running it,
-- rebuild the affected days and check the numbers (queries at the bottom).
-- ============================================================================

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
     participant_name, participant_email, room_name, room_category,
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

  -- ── 2. room_uuid -> room_name, four evidence sources, lowest pri wins ──
  --     CHANGED IN v12: room_mappings for the target date is now the top
  --     source, and each row carries `support` — how many rows back this
  --     name — so ties are settled by weight of evidence, not by whichever
  --     stamp happened to land last.
  name_evidence AS (
    -- (1) room_mappings saved for THIS date: the deliberate, verified record
    SELECT room_uuid, room_name, 1 AS pri, COUNT(*) AS support, MAX(mapped_at) AS seen
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE mapping_date = target_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
    GROUP BY room_uuid, room_name

    UNION ALL
    -- (2) names seen in TODAY's event stream — majority wins.
    --     This is where the poisoned name lives, so it must never be decided
    --     by a single event: on 2026-08-11 the correct name appeared on six
    --     events and the wrong one on a single event two seconds after the
    --     participant left the break room.
    SELECT room_uuid, room_name, 2, COUNT(*), MAX(event_timestamp)
    FROM raw_events
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%' AND room_name != 'Unknown Room'
      AND event_type IN ('breakout_room_joined','breakout_room_left')
      AND event_timestamp >= day_start_utc AND event_timestamp < tail_end_utc
    GROUP BY room_uuid, room_name

    UNION ALL
    -- (3) room_mappings from ANY date. Rescues a day the Room Mapper missed.
    --     Assumes room_uuid -> room_name is 1:1; health check 16 verifies it.
    SELECT room_uuid, room_name, 3, COUNT(*), MAX(mapped_at)
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
    GROUP BY room_uuid, room_name

    UNION ALL
    -- (4) names seen in the event stream on any recent day
    SELECT room_uuid, room_name, 4, COUNT(*), MAX(event_timestamp)
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

  -- ── 2b. THE v12 FIX ────────────────────────────────────────────────────
  -- A room's name is a property of the ROOM, so it is decided once per
  -- room_uuid and applied to every event for that room. v11 let each event's
  -- own stamped name override this, which is how one mislabelled webhook
  -- renamed a three-hour stay.
  --
  -- Fallback order when a UUID has no evidence at all: keep whatever the
  -- event carried (usually the 'Room-XXXXXXXX' placeholder, which health
  -- check 05 reports), then 'Unknown Room'. Events with no room_uuid — every
  -- Main Room event — do not join here and keep their own name, which is
  -- correct: current_room hardcodes '0.Main Room' for those anyway.
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

      CASE
        WHEN e.event_type IN ('participant_left','meeting.participant_left',
                              'participant_joined','meeting.participant_joined')
             -- (a) a breakout placement PRECEDES this event by 0..30s
             AND EXISTS (
               SELECT 1 FROM events_with_key b
               WHERE b.participant_key = e.participant_key
                 AND b.meeting_id      = e.meeting_id
                 AND b.event_type      = 'breakout_room_joined'
                 AND TIMESTAMP_DIFF(e.event_timestamp, b.event_timestamp, MILLISECOND)
                     BETWEEN 0 AND reconnect_window_ms
             )
             -- (b)+(c) a left/join pair exists within 30s of this event AND
             --         within 5s of each other, in EITHER delivery order
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

      -- Is this breakout_room_left part of the person LEAVING THE MEETING,
      -- rather than stepping back into the Main Room?
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

      -- Deterministic tie-break class. Many events share a timestamp to the
      -- second; without this, LEAD() results vary between runs.
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

END;


-- ── AFTER RUNNING THE ABOVE ─────────────────────────────────────────────────
--
-- 1. Rebuild the day the bug was found on:
--      CALL `verve-attendance-tracker.breakout_room_calibrator.sp_build_presence_intervals`(DATE '2026-08-11');
--
-- 2. Confirm the 187 minutes moved from break back to Creative Corner:
--      SELECT room_name, room_category,
--             ROUND(SUM(duration_seconds)/60) AS minutes
--      FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
--      WHERE event_date = DATE '2026-08-11'
--        AND participant_email = 'harsh.jain@verveadvisory.com'
--      GROUP BY 1, 2
--      ORDER BY minutes DESC;
--    BEFORE: 8.0:BREAK TIME ~ 244 min.  AFTER: 1.16:Creative Corner gains ~187.
--
-- 3. Confirm two people in the same room now agree:
--      SELECT participant_name, room_name, start_ts, end_ts
--      FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
--      WHERE event_date = DATE '2026-08-11'
--        AND participant_email IN ('harsh.jain@verveadvisory.com',
--                                  'rukaiya.rangoonwala@verveadvisory.com')
--      ORDER BY start_ts;
--
-- 4. Only then backfill further history, one day at a time or in a loop.
--    Every past day rebuilds from the same events, so corrections are
--    retroactive — the UUIDs never changed.
