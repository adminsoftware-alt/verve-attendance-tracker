-- ============================================================================
--  HOW presence_intervals IS BUILT  (room-wise, from the 2 raw tables)
--  This is the exact logic of build_presence_intervals_sql() in zt_intervals.py,
--  unrolled into a single runnable script for ONE day (2026-07-22).
--
--  RAW INPUTS
--    1. participant_events_p   -- Zoom webhooks (joins/lefts/breakout moves)  [PRIMARY]
--    2. room_mappings          -- room_uuid -> room_name (rotates daily)
--    3. room_snapshots_v2      -- bot snapshots, used ONLY to help name<->email identity
--
--  OUTPUT: one row per person per room segment (same shape as presence_intervals),
--          which the UI then sums (CEIL(sum(non-break seconds)/60)) per person.
--
--  Run in BigQuery console. Change the date in the two DECLAREs to rebuild another day.
-- ============================================================================

DECLARE d          DATE      DEFAULT DATE '2026-07-22';
DECLARE day_start  TIMESTAMP DEFAULT TIMESTAMP_SUB(TIMESTAMP(DATE '2026-07-22'), INTERVAL 330 MINUTE);  -- 00:00 IST in UTC


-- ---------------------------------------------------------------------------
-- Helper 1: normalize a display name (strip Zoom rejoin suffixes: "-1", "_Team", etc.)
-- Mirrors _sql_normalize_name() so "Kajal Yadav-1" links to "Kajal Yadav".
-- ---------------------------------------------------------------------------
CREATE TEMP FUNCTION norm_name(s STRING) AS (
  LOWER(TRIM(
    REGEXP_REPLACE(
      REGEXP_REPLACE(
        REGEXP_REPLACE(
          REGEXP_REPLACE(
            REGEXP_REPLACE(TRIM(s), r'\s+-\s+\w+$', ''),
          r'-\d+$', ''),
        r'_\w+$', ''),
      r'-[A-Z]{2,}$', ''),
    r'\s+\d$', '')
  ))
);


-- ---------------------------------------------------------------------------
-- Helper 2: the STATE MACHINE. Takes one person's sorted events and returns
-- their room-by-room intervals. This is where all the "logic we consider" lives:
--   * 5s room-transition rule (+ 30s rejoin guard  <- the phantom-hours fix)
--   * 30s coalesce of micro-disconnects
--   * login-date attribution (session belongs to the IST day you logged in)
--   * window tiling into rooms via breakout_room_joined/left
--   * caps: 600min per segment, 240min if the leave webhook is missing
-- ---------------------------------------------------------------------------
CREATE TEMP FUNCTION build_intervals(
    events ARRAY<STRUCT<ts TIMESTAMP, et STRING, room STRING>>,
    monitoring_end TIMESTAMP,
    day_start TIMESTAMP,
    day_end TIMESTAMP,
    early_cutoff TIMESTAMP)
RETURNS ARRAY<STRUCT<room_name STRING, room_category STRING,
                     source STRING, confidence FLOAT64,
                     start_ts TIMESTAMP, end_ts TIMESTAMP>>
LANGUAGE js AS r"""
var COALESCE_GAP_MS = 30 * 1000;        // merge disconnects shorter than this
var SEG_MIN_MS      = 5 * 1000;         // drop segments shorter than this (Zoom double-fire noise)
var SEG_CAP_MS      = 600 * 60 * 1000;  // cap any single segment at 10h
var NO_LEAVE_CAP_MS = 240 * 60 * 1000;  // cap at 4h when there is no leave webhook

if (!events || events.length === 0) return [];
events = events.slice().sort(function(a, b) { return a.ts - b.ts; });

var JOINS = ['participant_joined', 'meeting.participant_joined'];
var LEFTS = ['participant_left', 'meeting.participant_left'];
var TRANSITION_GRACE_MS = 5000;  // participant_left within 5s of a breakout join = room move

var brk = events.filter(function(e) {
  return e.et === 'breakout_room_joined' || e.et === 'breakout_room_left';
});

// ---- presence windows (join -> leave pairs) ----
var brkJoinTimes = [];
brk.forEach(function(e) { if (e.et === 'breakout_room_joined') brkJoinTimes.push(e.ts.getTime()); });
function isRoomTransition(t) {
  // A real room move rejoins within ~0.3s; a real EXIT has no rejoin for hours.
  // Only suppress this left if the person rejoins within 30s (the fix for the
  // 10h phantom Main Room bug: the builder reads two days, so a next-morning
  // login must NOT count as a rejoin).
  var hasRejoinSoon = false;
  for (var j = 0; j < events.length; j++) {
    if (JOINS.indexOf(events[j].et) >= 0) {
      var dj = events[j].ts.getTime() - t;
      if (dj > 0 && dj < COALESCE_GAP_MS) { hasRejoinSoon = true; break; }
    }
  }
  if (!hasRejoinSoon) return false;  // no quick rejoin = real exit

  for (var i = 0; i < brkJoinTimes.length; i++) {
    var d = brkJoinTimes[i] - t;
    if (d > -TRANSITION_GRACE_MS && d < TRANSITION_GRACE_MS) return true;
    if (d >= TRANSITION_GRACE_MS) break;
  }
  return false;
}
var raw = [];
var cur = null;
events.forEach(function(e) {
  if (JOINS.indexOf(e.et) >= 0) {
    if (cur === null) cur = e.ts;
  } else if (LEFTS.indexOf(e.et) >= 0) {
    if (cur !== null && !isRoomTransition(e.ts.getTime())) {
      raw.push([cur, e.ts]); cur = null;
    }
  }
});
if (cur !== null) raw.push([cur, null]);

var windows = [];
raw.forEach(function(w) {
  if (windows.length > 0) {
    var prev = windows[windows.length - 1];
    if (prev[1] !== null && w[0] !== null && (w[0] - prev[1]) < COALESCE_GAP_MS) {
      prev[1] = w[1];   // micro-disconnect: merge into previous window
      return;
    }
  }
  windows.push([w[0], w[1]]);
});

// ---- fallbacks when there are no clean join/leave pairs ----
if (windows.length === 0) {
  var joinTs = null, leftTs = null;
  events.forEach(function(e) {
    if (JOINS.indexOf(e.et) >= 0 && (joinTs === null || e.ts < joinTs)) joinTs = e.ts;
    if (LEFTS.indexOf(e.et) >= 0 && (leftTs === null || e.ts > leftTs)) leftTs = e.ts;
  });
  if (joinTs !== null) {
    if (joinTs >= day_start && joinTs < day_end) {
      windows.push([joinTs, leftTs !== null && leftTs > joinTs ? leftTs : null]);
    } else { return []; }
  } else if (brk.length > 0) {
    var first = brk[0].ts;
    if (first >= day_start && first < day_end) {
      if (first < early_cutoff) return [];   // before 08:00 IST = yesterday's tail
      windows.push([first, null]);
    } else { return []; }
  } else { return []; }
}

function classify(room) {
  if (!room) return 'breakout';
  var n = room.trim().toLowerCase();
  if (n.indexOf('break time') >= 0) return 'break';
  if (n === 'main room' || n.indexOf('0.main') === 0) return 'main';
  return 'breakout';
}

var out = [];
function emit(room, s, e, noLeave) {
  var ms = e - s;
  if (ms < SEG_MIN_MS) return;
  if (ms > SEG_CAP_MS) ms = SEG_CAP_MS;
  var cat = classify(room);
  var s_ms = (s.getTime ? s.getTime() : s);
  out.push({
    room_name: cat === 'main' ? '0.Main Room' : room,
    room_category: cat,
    source: cat === 'main' ? 'webhook_fill' : 'webhook_room',
    confidence: noLeave ? 0.35 : 0.5,
    start_ts: new Date(s_ms),
    end_ts: new Date(s_ms + ms)
  });
}

windows.forEach(function(w) {
  var wStart = w[0], wLeave = w[1];
  if (wStart === null) return;

  // login-date attribution: keep only sessions that STARTED on day d
  if (wStart < day_start) return;
  if (wStart >= day_end) return;

  var noLeave = (wLeave === null);
  var wEnd = (wLeave !== null) ? wLeave : monitoring_end;
  if (wEnd === null || wEnd === undefined) return;
  if (monitoring_end !== null && wEnd > monitoring_end) wEnd = monitoring_end;
  if (noLeave && (wEnd - wStart) > NO_LEAVE_CAP_MS) {
    wEnd = new Date(wStart.getTime() + NO_LEAVE_CAP_MS);
  }
  if (wEnd <= wStart) return;

  // ---- tile the window into rooms using breakout events ----
  var curRoom = 'Main Room';
  var segStart = wStart;
  var prevEt = null, prevRoom = null, prevTs = null;
  var tiles = [];
  brk.forEach(function(e) {
    var ts = e.ts;
    if (ts < wStart || ts > wEnd) return;
    if (prevEt === e.et && (prevRoom || '') === (e.room || '') &&
        prevTs !== null && (ts - prevTs) < 5000) return;   // double-send
    prevEt = e.et; prevRoom = e.room; prevTs = ts;
    if (e.et === 'breakout_room_joined') {
      var newRoom = e.room || 'Unknown Room';
      if (newRoom === curRoom) return;
      tiles.push([curRoom, segStart, ts]);
      curRoom = newRoom; segStart = ts;
    } else {
      if (curRoom === 'Main Room') return;   // stray left: join was missed
      tiles.push([curRoom, segStart, ts]);
      curRoom = 'Main Room'; segStart = ts;
    }
  });
  tiles.push([curRoom, segStart, wEnd]);
  tiles.forEach(function(t) { if (t[2] > t[1]) emit(t[0], t[1], t[2], noLeave); });
});

return out;
""";


-- ---------------------------------------------------------------------------
-- MAIN QUERY: raw events -> room-resolved -> per-person -> intervals
-- ---------------------------------------------------------------------------
WITH
-- (1) Pull the day's webhook events. Reads TWO partitions (d and d+1) so a
--     session that starts on d and ends after midnight is fully captured;
--     the state machine keeps only windows that START on d.
base AS (
  SELECT
    norm_name(pe.participant_name)       AS name_key,
    pe.participant_name,
    pe.participant_email,
    CAST(pe.meeting_id AS STRING)        AS meeting_id,
    CAST(pe.event_timestamp AS TIMESTAMP) AS ts,
    pe.event_type                        AS et,
    -- ROOM NAME RESOLUTION: neither the event's in-flight name nor the saved
    -- mapping is always right. Per uuid, NEWEST KNOWLEDGE WINS: compare the
    -- latest event name vs the mapping's mapped_at and take the newer one.
    CASE
      WHEN pe.room_uuid IS NULL OR pe.room_uuid = '' THEN pe.room_name
      WHEN un.ev_name IS NOT NULL AND rm.mapped_name IS NOT NULL THEN
           IF(rm.mapped_at > un.ev_ts, rm.mapped_name, un.ev_name)
      WHEN un.ev_name IS NOT NULL THEN un.ev_name
      WHEN rm.mapped_name IS NOT NULL THEN rm.mapped_name
      ELSE pe.room_name
    END AS room
  FROM `breakout_room_calibrator.participant_events_p` pe
  -- latest SAVED name per room from room_mappings
  LEFT JOIN (
    SELECT room_uuid,
           ARRAY_AGG(room_name ORDER BY mapped_at DESC LIMIT 1)[OFFSET(0)] AS mapped_name,
           MAX(mapped_at) AS mapped_at
    FROM `breakout_room_calibrator.room_mappings`
    WHERE CAST(mapping_date AS STRING) = CAST(d AS STRING)
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
    GROUP BY room_uuid
  ) rm ON pe.room_uuid = rm.room_uuid
  -- latest name SEEN in the event stream itself per room
  LEFT JOIN (
    SELECT room_uuid,
           ARRAY_AGG(room_name ORDER BY event_timestamp DESC LIMIT 1)[OFFSET(0)] AS ev_name,
           MAX(event_timestamp) AS ev_ts
    FROM `breakout_room_calibrator.participant_events_p`
    WHERE event_date BETWEEN d AND DATE_ADD(d, INTERVAL 1 DAY)
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
      AND room_name != 'Unknown Room'
      AND event_type IN ('breakout_room_joined','breakout_room_left')
    GROUP BY room_uuid
  ) un ON pe.room_uuid = un.room_uuid
  WHERE pe.event_date BETWEEN d AND DATE_ADD(d, INTERVAL 1 DAY)
    AND pe.participant_name IS NOT NULL AND pe.participant_name != ''
    AND LOWER(pe.participant_name) NOT LIKE '%scout%'          -- drop the bot
    AND pe.event_type IN ('participant_joined','participant_left',
                          'meeting.participant_joined','meeting.participant_left',
                          'breakout_room_joined','breakout_room_left')
),

-- (2) Day-scoped identity: a name maps to an email only if it maps to EXACTLY
--     ONE email that day. Pairs come from BOTH raw sources (webhooks + bot).
ident_pairs AS (
  SELECT DISTINCT name_key, LOWER(TRIM(participant_email)) AS email
  FROM base
  WHERE participant_email IS NOT NULL AND TRIM(participant_email) != ''
  UNION DISTINCT
  SELECT DISTINCT norm_name(s.participant_name) AS name_key, LOWER(TRIM(s.participant_email)) AS email
  FROM `breakout_room_calibrator.room_snapshots_v2` s
  WHERE s.event_date = d
    AND s.participant_name IS NOT NULL AND s.participant_name != ''
    AND s.participant_email IS NOT NULL AND TRIM(s.participant_email) != ''
    AND LOWER(s.participant_name) NOT LIKE '%scout%'
),
ident AS (
  SELECT name_key, ANY_VALUE(email) AS mapped_email
  FROM ident_pairs
  GROUP BY name_key
  HAVING COUNT(DISTINCT email) = 1
),

-- (3) One row per person, with their events bundled and sorted.
--     participant_key = email when unambiguous, else the normalized name.
participants AS (
  SELECT
    COALESCE(i.mapped_email, b.name_key) AS participant_key,
    ANY_VALUE(b.participant_name)        AS participant_name,
    ANY_VALUE(NULLIF(TRIM(COALESCE(b.participant_email, '')), '')) AS participant_email,
    ANY_VALUE(NULLIF(COALESCE(b.meeting_id, ''), ''))              AS meeting_id,
    ARRAY_AGG(STRUCT(b.ts AS ts, b.et AS et, b.room AS room) ORDER BY b.ts) AS events
  FROM base b
  LEFT JOIN ident i ON b.name_key = i.name_key
  GROUP BY participant_key
),

-- (4) monitoring_end = last leave webhook of the day (used to close windows
--     that have no explicit leave).
mon AS (
  SELECT MAX(ts) AS monitoring_end
  FROM base
  WHERE et IN ('participant_left', 'meeting.participant_left')
)

-- (5) Run the state machine per person and explode into room-wise intervals.
SELECT
  p.participant_key,
  p.participant_name,
  p.participant_email,
  p.meeting_id,
  t.room_name,
  t.room_category,                                          -- main | breakout | break
  FORMAT_TIMESTAMP('%H:%M:%S', TIMESTAMP_ADD(t.start_ts, INTERVAL 330 MINUTE)) AS start_ist,
  FORMAT_TIMESTAMP('%H:%M:%S', TIMESTAMP_ADD(t.end_ts,   INTERVAL 330 MINUTE)) AS end_ist,
  TIMESTAMP_DIFF(t.end_ts, t.start_ts, SECOND)             AS duration_seconds,
  ROUND(TIMESTAMP_DIFF(t.end_ts, t.start_ts, SECOND)/60.0, 1) AS minutes,
  t.source,
  t.confidence
FROM participants p
CROSS JOIN mon
CROSS JOIN UNNEST(build_intervals(
    p.events,
    mon.monitoring_end,
    day_start,
    TIMESTAMP_ADD(day_start, INTERVAL 24 HOUR),   -- day_end
    TIMESTAMP_ADD(day_start, INTERVAL  8 HOUR)    -- 08:00 IST early cutoff
)) AS t
ORDER BY p.participant_name, t.start_ts;


-- ============================================================================
-- To roll these room-wise rows up to the per-person UI number, wrap the SELECT
-- above in a subquery `pi` and aggregate (break excluded):
--
--   SELECT participant_name,
--          CAST(CEILING(SUM(IF(room_category!='break', duration_seconds,0))/60.0) AS INT64) AS working_mins
--   FROM ( <the SELECT above> ) pi
--   GROUP BY participant_name, participant_key
--   ORDER BY working_mins DESC;
-- ============================================================================
