"""presence_intervals subsystem: the ONE source of hours.
Materializes per-person room intervals from bot snapshots (primary)
and Zoom webhooks (fallback), plus freshness/auto-build machinery."""
import os
import json
import time
import threading
import uuid as uuid_lib
from datetime import datetime, timedelta
from collections import defaultdict
from google.cloud import bigquery
from zt_config import *  # noqa: F401,F403
from zt_helpers import *  # noqa: F401,F403

__all__ = [
    'PRESENCE_INTERVALS_TABLE',
    'GAP_THRESHOLD_SECONDS',
    'BUCKET_SECONDS',
    'MAIN_ROOM_SYNTH_CAP_MINUTES',
    'MAIN_ROOM_SYNTH_MIN_SECONDS',
    'WEBHOOK_FILL_NO_LEAVE_CAP_MINUTES',
    'WEBHOOK_SEGMENT_MIN_SECONDS',
    'PAGELOAD_AUTO_BUILD',
    'SETTLING_STALE_MINUTES',
    'INHERITED_MIN_PARTICIPANTS',
    'INHERITED_LEAVE_FRACTION',
    'INHERITED_MAX_BOUNDARY_IST_MIN',
    'INHERITED_EXIT_SUSTAIN_BUCKETS',
    'IST_OFFSET_MINUTES',
    '_TODAY_BUILD_GUARD',
    '_TODAY_BUILD_LOCK',
    'TODAY_REBUILD_MIN_INTERVAL_S',
    'TODAY_REBUILD_FAILURE_BACKOFF_S',
    '_rebuild_today_guarded',
    '_sql_whole_minutes',
    '_classify_room',
    '_sql_normalize_name',
    '_ensure_presence_intervals_table',
    'build_presence_intervals',
    '_auto_build_dates_in_range',
    'PRESENCE_INTERVALS_SQL_TABLE',
    'build_presence_intervals_sql',
    'compare_sql_vs_python',
]

PRESENCE_INTERVALS_TABLE = 'presence_intervals'


GAP_THRESHOLD_SECONDS = 300          # >5min gap between snapshots = new interval


BUCKET_SECONDS = 30                  # 30s dedup bucket (multi-source polling)


MAIN_ROOM_SYNTH_CAP_MINUTES = 600    # Cap any single synthesized Main Room interval


MAIN_ROOM_SYNTH_MIN_SECONDS = 120    # Don't synthesize gaps smaller than 2min


WEBHOOK_FILL_NO_LEAVE_CAP_MINUTES = int(os.environ.get('WEBHOOK_FILL_NO_LEAVE_CAP_MINUTES', '240'))


WEBHOOK_SEGMENT_MIN_SECONDS = 60     # Webhook-timeline segments shorter than this are noise


PAGELOAD_AUTO_BUILD = os.environ.get('PAGELOAD_AUTO_BUILD', 'true').lower() != 'false'


SETTLING_STALE_MINUTES = int(os.environ.get('SETTLING_STALE_MINUTES', '90'))


INHERITED_MIN_PARTICIPANTS     = int(os.environ.get('INHERITED_MIN_PARTICIPANTS', '10'))      # >=N frozen at 00:00 => inherited meeting


INHERITED_LEAVE_FRACTION       = float(os.environ.get('INHERITED_LEAVE_FRACTION', '0.5'))     # occupancy < frac*start_level => mass-exit


INHERITED_MAX_BOUNDARY_IST_MIN = int(os.environ.get('INHERITED_MAX_BOUNDARY_IST_MIN', '840')) # don't place boundary after 14:00 IST


INHERITED_EXIT_SUSTAIN_BUCKETS = int(os.environ.get('INHERITED_EXIT_SUSTAIN_BUCKETS', '10'))  # exit must stay low this many 30s buckets (5min)


IST_OFFSET_MINUTES = 330


_TODAY_BUILD_GUARD = {'date': None, 'ts': 0.0}


_TODAY_BUILD_LOCK = threading.Lock()


TODAY_REBUILD_MIN_INTERVAL_S = int(os.environ.get('TODAY_REBUILD_MIN_INTERVAL_S', '180'))


TODAY_REBUILD_FAILURE_BACKOFF_S = int(os.environ.get('TODAY_REBUILD_FAILURE_BACKOFF_S', '30'))


def _rebuild_today_guarded(date_str):
    """Rebuild today's partition unless this process already did (or started
    to) within TODAY_REBUILD_MIN_INTERVAL_S. Returns True if a rebuild ran.

    The guard is CLAIMED before the build starts (under a lock): a dashboard
    fires one attendance_v2 call per team in parallel, and a build takes many
    seconds — claiming after completion let every parallel request start its
    own full rebuild. Parallel callers now return immediately and serve the
    existing (at most ~3 min old) data. On failure the claim is shortened to
    a backoff so the next request retries soon without a failure storm."""
    now = time.time()
    with _TODAY_BUILD_LOCK:
        if (_TODAY_BUILD_GUARD['date'] == date_str
                and now - _TODAY_BUILD_GUARD['ts'] < TODAY_REBUILD_MIN_INTERVAL_S):
            return False
        # Claim BEFORE building so concurrent requests don't duplicate it.
        _TODAY_BUILD_GUARD['date'] = date_str
        _TODAY_BUILD_GUARD['ts'] = now
    try:
        build_presence_intervals(date_str)
    except Exception:
        with _TODAY_BUILD_LOCK:
            if _TODAY_BUILD_GUARD['date'] == date_str:
                # Shorten the claim: retry after the backoff, not the full interval
                _TODAY_BUILD_GUARD['ts'] = (
                    time.time() - TODAY_REBUILD_MIN_INTERVAL_S + TODAY_REBUILD_FAILURE_BACKOFF_S
                )
        raise
    with _TODAY_BUILD_LOCK:
        _TODAY_BUILD_GUARD['date'] = date_str
        _TODAY_BUILD_GUARD['ts'] = time.time()
    return True


def _sql_whole_minutes(seconds_expr):
    # Round half-up — must stay identical to _whole_minutes_from_seconds.
    return f"CAST(FLOOR((COALESCE({seconds_expr}, 0) + 30) / 60.0) AS INT64)"


def _classify_room(room_name):
    """Return 'main' | 'breakout' | 'break' for a room name.
    Matches v1 conventions at app.py:7753, 7760, 7775."""
    if not room_name:
        return 'breakout'
    n = room_name.strip().lower()
    if 'break time' in n:
        return 'break'
    if n == 'main room' or n.startswith('0.main'):
        return 'main'
    return 'breakout'


def _sql_normalize_name(col_expr):
    """SQL expression that strips Zoom rejoin suffixes the same way
    normalize_participant_name() does in Python. Used by the team_v2
    identity bridge so 'Kajal Yadav-1' in snapshots links to the
    'Kajal Yadav' row in team_members."""
    s = f"TRIM({col_expr})"
    # In order: " - TEXT" suffix, "-N", "_TEXT", "-CAPS", trailing " N"
    s = f"REGEXP_REPLACE({s}, r'\\s+-\\s+\\w+$', '')"
    s = f"REGEXP_REPLACE({s}, r'-\\d+$', '')"
    s = f"REGEXP_REPLACE({s}, r'_\\w+$', '')"
    s = f"REGEXP_REPLACE({s}, r'-[A-Z]{{2,}}$', '')"
    s = f"REGEXP_REPLACE({s}, r'\\s+\\d$', '')"
    return f"LOWER(TRIM({s}))"


def _ensure_presence_intervals_table():
    """Create presence_intervals table if it does not yet exist.
    Partitioned by event_date, clustered by meeting_id, participant_key."""
    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` (
        interval_id        STRING NOT NULL,
        event_date         DATE   NOT NULL,
        meeting_id         STRING,
        meeting_uuid       STRING,
        participant_key    STRING NOT NULL,
        participant_name   STRING,
        participant_email  STRING,
        room_name          STRING,
        room_category      STRING,
        start_ts           TIMESTAMP NOT NULL,
        end_ts             TIMESTAMP NOT NULL,
        duration_seconds   INT64,
        alone_seconds      INT64,
        snapshot_count     INT64,
        source             STRING,
        confidence         FLOAT64,
        built_at           TIMESTAMP
    )
    PARTITION BY event_date
    CLUSTER BY meeting_id, participant_key
    """
    client.query(ddl).result()


def build_presence_intervals(date_str):
    """Materialize presence_intervals for one IST date.

    Steps:
      1. Pull bucketed snapshot data (per participant+room+30s bucket).
      2. Pull webhook timestamps (meeting_joined, meeting_left, breakout flag).
      3. Compute real intervals from snapshots, with alone_seconds.
      4. Fill snapshot gaps from the webhook timeline: uncovered stretches
         become Main Room (synthesized) OR the breakout room the webhooks
         reported (webhook_room) — a mid-day bot outage no longer mislabels
         breakout time as Main Room.
      5. Webhook timeline fallback: participants with webhook presence but
         NO snapshots (bot off / bot missed them) get their full room
         timeline rebuilt from webhook events — Main Room between meeting
         join and breakout joins, the breakout room between its join/left
         events. Bot data (snapshots) stays primary when it exists.
      6. DELETE existing rows for the date, INSERT new rows.

    Returns dict with counts and timing.
    """
    started = time.time()
    _ensure_presence_intervals_table()
    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

    # ----- Step 1: bucketed snapshot data ----------------------------------
    # CANONICAL participant_key = normalized name (Zoom rejoin suffixes
    # stripped). Same person across rejoin variants gets one key, all
    # their intervals merge before synthesis. Edge case: two real people
    # with identical normalized names would collide — acceptable trade-off
    # for the simplicity (Zoom snapshots often lack email anyway).
    norm_sn = _sql_normalize_name('s.participant_name')
    bucket_q = f"""
    WITH normalized AS (
      SELECT
        s.snapshot_time,
        s.meeting_id,
        s.participant_name,
        s.participant_email,
        s.participant_uuid,
        s.room_name,
        {norm_sn} AS participant_key,
        DIV(UNIX_SECONDS(s.snapshot_time), {BUCKET_SECONDS}) AS bucket30
      FROM `{dataset_ref}.room_snapshots_v2` s
      WHERE s.event_date = @date
        AND s.room_name IS NOT NULL AND s.room_name != ''
        AND s.participant_name IS NOT NULL AND s.participant_name != ''
        AND LOWER(s.participant_name) NOT LIKE '%scout%'
    ),
    dedup AS (
      -- Within (canonical_key, bucket30): keep ONE row. Prefer non-Main-Room.
      -- This kills both SDK-transition artifacts (same uuid, same instant,
      -- two rooms) AND multi-source duplication (Source A says Main Room,
      -- Source B says Breakout for same person in same 30s window).
      SELECT *
      FROM normalized
      QUALIFY ROW_NUMBER() OVER (
        PARTITION BY participant_key, bucket30
        ORDER BY
          CASE WHEN LOWER(room_name) = 'main room' OR LOWER(room_name) LIKE '0.main%' THEN 1 ELSE 0 END,
          room_name
      ) = 1
    ),
    occupancy AS (
      SELECT room_name, bucket30, COUNT(DISTINCT participant_key) AS occupant_count
      FROM dedup
      GROUP BY room_name, bucket30
    )
    SELECT
      d.participant_key,
      ANY_VALUE(d.participant_name) AS participant_name,
      MAX(d.participant_email) AS participant_email,
      ANY_VALUE(d.meeting_id) AS meeting_id,
      d.room_name,
      d.bucket30,
      MIN(d.snapshot_time) AS bucket_start,
      MAX(d.snapshot_time) AS bucket_end,
      MAX(o.occupant_count) AS occupant_count
    FROM dedup d
    JOIN occupancy o USING (room_name, bucket30)
    GROUP BY d.participant_key, d.room_name, d.bucket30
    ORDER BY d.participant_key, d.bucket30
    """
    job = client.query(bucket_q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date_str)]
    ))
    buckets = list(job.result())

    # ----- Step 1b: identity map (email-first participant keys) ------------
    # Names drift (renames, rejoin suffixes, "Shashank C" vs "Shashank
    # Channawar"); emails don't. Wherever a normalized name maps to exactly
    # ONE email that day (seen in snapshots or webhooks), the email becomes
    # the participant_key — so all of a person's name variants merge into a
    # single identity. Names with no email (or ambiguously two emails) keep
    # the normalized-name key, exactly as before. Consumers already match
    # rows by BOTH the name and email columns, so either key form resolves.
    norm_pn_i = _sql_normalize_name('pe.participant_name')
    ident_q = f"""
    WITH pairs AS (
      SELECT DISTINCT {norm_sn} AS name_key, LOWER(TRIM(s.participant_email)) AS email
      FROM `{dataset_ref}.room_snapshots_v2` s
      WHERE s.event_date = @date
        AND s.participant_name IS NOT NULL AND s.participant_name != ''
        AND s.participant_email IS NOT NULL AND TRIM(s.participant_email) != ''
        AND LOWER(s.participant_name) NOT LIKE '%scout%'
      UNION DISTINCT
      SELECT DISTINCT {norm_pn_i} AS name_key, LOWER(TRIM(pe.participant_email)) AS email
      FROM `{dataset_ref}.{BQ_EVENTS_TABLE}` pe
      WHERE pe.event_date = @date
        AND pe.participant_name IS NOT NULL AND pe.participant_name != ''
        AND pe.participant_email IS NOT NULL AND TRIM(pe.participant_email) != ''
        AND LOWER(pe.participant_name) NOT LIKE '%scout%'
    )
    SELECT name_key, ANY_VALUE(pairs.email) AS email
    FROM pairs
    GROUP BY name_key
    -- Qualified pairs.email: bare "email" would resolve to the SELECT alias
    -- (ANY_VALUE) and BigQuery rejects aggregating an aggregate.
    HAVING COUNT(DISTINCT pairs.email) = 1
    """
    ident_rows = list(client.query(ident_q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date_str)]
    )).result())
    _ident_map = {r.name_key: r.email for r in ident_rows}

    def _remap(name_key):
        return _ident_map.get(name_key, name_key)

    # Last snapshot anywhere = initial monitoring window end.
    # This will be extended in Step 2b if webhooks continued after snapshots stopped.
    last_snapshot_time = None
    if buckets:
        last_snapshot_time = max(b.bucket_end for b in buckets)
    monitoring_end = last_snapshot_time  # Will be updated after Step 2

    # ----- Step 2: webhook timestamps per participant ----------------------
    # Use the SAME canonical key formula as snapshots (normalized name)
    # so webhook data merges correctly with snapshot data in Python.
    norm_pn = _sql_normalize_name('pe.participant_name')
    webhook_q = f"""
    SELECT
      {norm_pn} AS participant_key,
      ANY_VALUE(pe.participant_name) AS participant_name,
      ANY_VALUE(pe.participant_email) AS participant_email,
      ANY_VALUE(pe.meeting_id) AS meeting_id,
      MIN(CASE WHEN pe.event_type IN ('participant_joined','meeting.participant_joined')
          THEN CAST(pe.event_timestamp AS TIMESTAMP) END) AS meeting_joined,
      MAX(CASE WHEN pe.event_type IN ('participant_left','meeting.participant_left')
          THEN CAST(pe.event_timestamp AS TIMESTAMP) END) AS meeting_left,
      COUNTIF(pe.event_type = 'breakout_room_joined') AS breakout_webhook_count
    FROM `{dataset_ref}.{BQ_EVENTS_TABLE}` pe
    WHERE pe.event_date = @date
      AND pe.participant_name IS NOT NULL AND pe.participant_name != ''
      AND LOWER(pe.participant_name) NOT LIKE '%scout%'
    GROUP BY participant_key
    """
    webhook_rows = list(client.query(webhook_q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date_str)]
    )).result())
    # Merge webhook rows under remapped keys: when two name variants of the
    # same person (same email) each produced a row, combine them into one.
    from types import SimpleNamespace as _NS
    webhook_by_key = {}
    for r in webhook_rows:
        k = _remap(r.participant_key)
        w = webhook_by_key.get(k)
        if w is None:
            webhook_by_key[k] = _NS(
                participant_key=k, participant_name=r.participant_name,
                participant_email=r.participant_email, meeting_id=r.meeting_id,
                meeting_joined=r.meeting_joined, meeting_left=r.meeting_left,
                breakout_webhook_count=r.breakout_webhook_count or 0)
        else:
            w.participant_email = w.participant_email or r.participant_email
            w.meeting_id = w.meeting_id or r.meeting_id
            if r.meeting_joined and (not w.meeting_joined or r.meeting_joined < w.meeting_joined):
                w.meeting_joined = r.meeting_joined
            if r.meeting_left and (not w.meeting_left or r.meeting_left > w.meeting_left):
                w.meeting_left = r.meeting_left
            w.breakout_webhook_count += (r.breakout_webhook_count or 0)

    # ----- Step 2b: presence windows from join/leave events ----------------
    # For each participant, build (joined_ts, left_ts) windows so synthesis
    # only credits Main Room time when they were ACTUALLY in the meeting.
    # Without this, a participant who left for an hour between breakouts
    # gets credited that whole hour as phantom Main Room time.
    # Room names are resolved at BUILD time via room_mappings: webhook events
    # that arrived before a room's uuid->name mapping existed are stored with
    # a 'Room-xxxxxxxx' placeholder, and UPDATEing them in place fails while
    # rows sit in the streaming buffer. COALESCE picks the mapped name when
    # one exists for that day (webhook-primary SDK lookups land here too),
    # falling back to whatever the event stored.
    events_q = f"""
    SELECT
      {norm_pn} AS participant_key,
      ARRAY_AGG(
        STRUCT(
          CAST(pe.event_timestamp AS TIMESTAMP) AS ts,
          pe.event_type AS et,
          COALESCE(rm.mapped_name, pe.room_name) AS room
        )
        ORDER BY pe.event_timestamp
      ) AS events
    FROM `{dataset_ref}.{BQ_EVENTS_TABLE}` pe
    LEFT JOIN (
      SELECT room_uuid,
             ARRAY_AGG(room_name ORDER BY mapped_at DESC LIMIT 1)[OFFSET(0)] AS mapped_name
      FROM `{dataset_ref}.{BQ_MAPPINGS_TABLE}`
      WHERE CAST(mapping_date AS STRING) = CAST(@date AS STRING)
        AND room_uuid IS NOT NULL AND room_uuid != ''
        AND room_name IS NOT NULL AND room_name != ''
        AND room_name NOT LIKE 'Room-%'
      GROUP BY room_uuid
    ) rm ON pe.room_uuid = rm.room_uuid
    WHERE pe.event_date = @date
      AND pe.participant_name IS NOT NULL AND pe.participant_name != ''
      AND LOWER(pe.participant_name) NOT LIKE '%scout%'
      AND pe.event_type IN ('participant_joined','participant_left',
                            'meeting.participant_joined','meeting.participant_left',
                            'breakout_room_joined','breakout_room_left')
    GROUP BY participant_key
    """
    events_rows = list(client.query(events_q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date_str)]
    )).result())

    # Coalesce threshold: leave/rejoin gaps shorter than this are treated
    # as continuous presence (Zoom often sends micro-disconnects).
    COALESCE_GAP_S = 30

    def _build_presence_windows(evt_list):
        """Return list of (join_dt, leave_dt) UTC tuples. leave_dt may be
        None if the participant was still present at end of capture."""
        raw = []
        current_join = None
        for e in evt_list:
            ts = e['ts'] if isinstance(e, dict) else e.ts
            et = e['et'] if isinstance(e, dict) else e.et
            if et in ('participant_joined', 'meeting.participant_joined'):
                if current_join is None:
                    current_join = ts
            elif et in ('participant_left', 'meeting.participant_left'):
                if current_join is not None:
                    raw.append((current_join, ts))
                    current_join = None
        if current_join is not None:
            raw.append((current_join, None))
        if not raw:
            return []
        merged = [raw[0]]
        for w in raw[1:]:
            prev_s, prev_e = merged[-1]
            if prev_e is not None and w[0] is not None:
                gap = (w[0] - prev_e).total_seconds()
                if gap < COALESCE_GAP_S:
                    merged[-1] = (prev_s, w[1])
                    continue
            merged.append(w)
        return merged

    # Merge event timelines under remapped keys (rename variants combine),
    # keeping chronological order so window-building stays correct.
    full_events_by_key = {}
    for r in events_rows:
        full_events_by_key.setdefault(_remap(r.participant_key), []).extend(list(r.events))
    for _k, _evts in full_events_by_key.items():
        _evts.sort(key=lambda e: e['ts'] if isinstance(e, dict) else e.ts)
    presence_windows_by_key = {
        k: _build_presence_windows(evts) for k, evts in full_events_by_key.items()
    }

    # ----- Step 2c: extend monitoring_end if webhooks continued after snapshots -----
    # When SDK monitoring stops but webhooks keep flowing, use the latest webhook
    # event time as the cap. This prevents discarding valid webhook data.
    last_webhook_time = None
    for wh in webhook_by_key.values():
        if wh.meeting_left and (last_webhook_time is None or wh.meeting_left > last_webhook_time):
            last_webhook_time = wh.meeting_left

    if last_webhook_time:
        if monitoring_end is None:
            monitoring_end = last_webhook_time
        elif last_webhook_time > monitoring_end:
            # Webhooks continued after snapshots stopped — extend the window
            print(f"[PresenceIntervals] Extending monitoring_end from {monitoring_end} to {last_webhook_time} (webhooks continued after snapshot outage)")
            monitoring_end = last_webhook_time

    # ----- Step 3: build real intervals from buckets -----------------------
    # Group buckets by (participant_key, room_name), sorted by bucket30.
    # Consecutive buckets (diff == 1, i.e. 30s apart) belong to the same
    # interval. A gap > GAP_THRESHOLD_SECONDS / BUCKET_SECONDS = 10 buckets
    # starts a new interval.
    from collections import defaultdict
    by_key_room = defaultdict(list)
    name_by_key = {}
    email_by_key = {}
    meeting_by_key = {}
    for b in buckets:
        bkey = _remap(b.participant_key)
        by_key_room[(bkey, b.room_name)].append(b)
        # Track latest seen identity per key (snapshot is authoritative for name)
        name_by_key[bkey] = b.participant_name
        if b.participant_email:
            email_by_key[bkey] = b.participant_email
        if b.meeting_id:
            meeting_by_key[bkey] = b.meeting_id

    gap_buckets = GAP_THRESHOLD_SECONDS // BUCKET_SECONDS  # 10

    # --- Cross-room gap credit --------------------------------------------
    # A missed 30s poll must not discard presence: the person did not
    # teleport out of the meeting because the SDK skipped a beat. For each
    # observed bucket, credit the time up to the participant's NEXT observed
    # bucket in ANY room (they stayed in the current room until seen
    # elsewhere), capped at the gap threshold. Holes larger than the
    # threshold still split intervals and are not credited. Crediting to the
    # PRECEDING room and looking across rooms keeps the per-person timeline
    # tiled with no double-counting when someone bounces A->B->A quickly.
    buckets_by_key = defaultdict(set)
    for b in buckets:
        buckets_by_key[_remap(b.participant_key)].add(b.bucket30)
    credit_by_key_bucket = {}
    for _pkey, bset in buckets_by_key.items():
        blist = sorted(bset)
        for i, bk in enumerate(blist):
            if i + 1 < len(blist):
                gap = blist[i + 1] - bk
                credit = gap if gap <= gap_buckets else 1
            else:
                credit = 1  # last observation: credit its own bucket only
            credit_by_key_bucket[(_pkey, bk)] = credit * BUCKET_SECONDS

    intervals = []  # list of dicts ready for BQ insert
    intervals_by_participant = defaultdict(list)  # key -> [interval dict, ...]

    now_ts = datetime.utcnow()
    built_at_iso = now_ts.replace(microsecond=0).isoformat() + 'Z'

    for (pkey, room), brows in by_key_room.items():
        category = _classify_room(room)
        # Sort by bucket30
        brows.sort(key=lambda x: x.bucket30)
        # Gap-group
        groups = []
        current = [brows[0]]
        for prev, curr in zip(brows, brows[1:]):
            if (curr.bucket30 - prev.bucket30) > gap_buckets:
                groups.append(current)
                current = [curr]
            else:
                current.append(curr)
        groups.append(current)

        for grp in groups:
            start_ts = grp[0].bucket_start
            # Duration = sum of per-bucket credits (each bucket counts until
            # the participant's next observation in any room, capped at the
            # gap threshold). This credits sub-threshold polling holes that
            # the old `distinct_buckets * 30` formula silently discarded —
            # the source of every view showing less time than the wall clock.
            seen_buckets = {}
            for g in grp:
                seen_buckets.setdefault(g.bucket30, g)
            duration = sum(
                credit_by_key_bucket.get((pkey, bk), BUCKET_SECONDS)
                for bk in seen_buckets
            )
            # End = start of last bucket + its credit, so end-start stays
            # consistent with the credited duration in Day View room rows.
            last_credit = credit_by_key_bucket.get((pkey, grp[-1].bucket30), BUCKET_SECONDS)
            end_ts = grp[-1].bucket_start + timedelta(seconds=last_credit)
            # Alone seconds: 30 per bucket where occupant_count==1, excluding Main
            alone = 0
            if category != 'main':
                alone = sum(
                    BUCKET_SECONDS for g in seen_buckets.values()
                    if (g.occupant_count or 0) == 1
                )
            iv = {
                'interval_id': str(uuid_lib.uuid4()),
                'event_date': date_str,
                'meeting_id': meeting_by_key.get(pkey),
                'meeting_uuid': None,
                'participant_key': pkey,
                'participant_name': name_by_key.get(pkey),
                'participant_email': email_by_key.get(pkey),
                'room_name': room,
                'room_category': category,
                'start_ts': start_ts.isoformat(),
                'end_ts': end_ts.isoformat(),
                'duration_seconds': duration,
                'alone_seconds': alone,
                'snapshot_count': len(grp),
                'source': 'snapshot',
                'confidence': 1.0,
                'built_at': built_at_iso,
            }
            intervals.append(iv)
            intervals_by_participant[pkey].append(iv)

    # ----- Step 4: synthesize Main Room from webhook presence --------------
    # Snapshots are primary truth. Wherever a participant has webhook presence
    # (join->leave windows) NOT already covered by a snapshot interval, credit
    # that time as Main Room. This fills holes when the snapshot monitor had an
    # outage but webhooks kept flowing (Zoom sends them automatically), so a day
    # is no longer under-counted just because polling stopped. Open windows
    # (missing leave) are capped at the monitoring window end — never phantom
    # presence past it.
    def _parse_ts(s):
        # ISO string -> naive UTC datetime
        if isinstance(s, str):
            return datetime.fromisoformat(s.replace('Z', ''))
        return s

    def _emit_main(bucket_list, pkey, s_dt, e_dt):
        secs = (e_dt - s_dt).total_seconds()
        if secs < MAIN_ROOM_SYNTH_MIN_SECONDS:
            return
        secs = min(secs, MAIN_ROOM_SYNTH_CAP_MINUTES * 60)
        bucket_list.append({
            'interval_id': str(uuid_lib.uuid4()),
            'event_date': date_str,
            'meeting_id': meeting_by_key.get(pkey),
            'meeting_uuid': None,
            'participant_key': pkey,
            'participant_name': name_by_key.get(pkey),
            'participant_email': email_by_key.get(pkey),
            'room_name': '0.Main Room',
            'room_category': 'main',
            'start_ts': s_dt.isoformat(),
            'end_ts': (s_dt + timedelta(seconds=secs)).isoformat(),
            'duration_seconds': int(secs),
            'alone_seconds': 0,
            'snapshot_count': 0,
            'source': 'synthesized_main',
            'confidence': 0.6,
            'built_at': built_at_iso,
        })

    # --- Shared webhook-timeline machinery (used by Steps 4 and 5) ---------
    def _evt(e, name):
        return e[name] if isinstance(e, dict) else getattr(e, name)

    def _breakout_events_for(pkey):
        return [e for e in full_events_by_key.get(pkey, [])
                if _evt(e, 'et') in ('breakout_room_joined', 'breakout_room_left')]

    def _room_state_at(breakout_evts, at_ts, not_before):
        """Which room was the participant in at `at_ts`, judged from their
        last breakout event in [not_before, at_ts]. Events before
        `not_before` (a previous meeting session) are stale — ignored."""
        room = 'Main Room'
        for e in breakout_evts:
            ets = _evt(e, 'ts')
            if ets < not_before or ets > at_ts:
                continue
            if _evt(e, 'et') == 'breakout_room_joined':
                room = _evt(e, 'room') or 'Unknown Room'
            else:
                room = 'Main Room'
        return room

    def _tile_window_with_rooms(breakout_evts, w_start, w_end, initial_room='Main Room'):
        """Tile [w_start, w_end] into (room, start, end) segments from
        breakout join/left webhooks — a state machine starting in
        `initial_room`. Drops Zoom double-sends (same event repeated <5s;
        the in-memory dedup misses those across Cloud Run instances)."""
        tiles = []
        cur_room = initial_room
        seg_start = w_start
        prev_et, prev_room, prev_ts = None, None, None
        for e in breakout_evts:
            ts, et, room = _evt(e, 'ts'), _evt(e, 'et'), _evt(e, 'room')
            if ts < w_start or ts > w_end:
                continue
            if (prev_et == et and (prev_room or '') == (room or '')
                    and prev_ts and (ts - prev_ts).total_seconds() < 5):
                continue
            prev_et, prev_room, prev_ts = et, room, ts
            if et == 'breakout_room_joined':
                new_room = room or 'Unknown Room'
                if new_room == cur_room:
                    continue
                tiles.append((cur_room, seg_start, ts))
                cur_room, seg_start = new_room, ts
            else:  # breakout_room_left -> back to Main Room
                if cur_room == 'Main Room':
                    continue  # stray left (its join was missed) — stay in Main
                tiles.append((cur_room, seg_start, ts))
                cur_room, seg_start = 'Main Room', ts
        tiles.append((cur_room, seg_start, w_end))
        return [(r, s, e) for (r, s, e) in tiles if e > s]

    def _emit_uncovered_stretch(bucket_list, pkey, brk_evts, window_start, s_dt, e_dt):
        """Fill an uncovered stretch for a SNAPSHOT participant. Previously
        this was always synthesized Main Room — wrong when the bot died
        mid-day while webhooks show the person in a breakout. Now the
        stretch is tiled by the webhook timeline: Main portions keep the
        legacy synthesized_main path, breakout portions become webhook_room
        intervals with the room the webhooks reported."""
        init_room = _room_state_at(brk_evts, s_dt, window_start)
        for room, t_s, t_e in _tile_window_with_rooms(brk_evts, s_dt, e_dt,
                                                      initial_room=init_room):
            category = _classify_room(room)
            if category == 'main':
                _emit_main(bucket_list, pkey, t_s, t_e)
                continue
            secs = (t_e - t_s).total_seconds()
            if secs < WEBHOOK_SEGMENT_MIN_SECONDS:
                continue
            secs = min(secs, MAIN_ROOM_SYNTH_CAP_MINUTES * 60)
            bucket_list.append({
                'interval_id': str(uuid_lib.uuid4()),
                'event_date': date_str,
                'meeting_id': meeting_by_key.get(pkey),
                'meeting_uuid': None,
                'participant_key': pkey,
                'participant_name': name_by_key.get(pkey),
                'participant_email': email_by_key.get(pkey),
                'room_name': room,
                'room_category': category,
                'start_ts': t_s.isoformat(),
                'end_ts': (t_s + timedelta(seconds=secs)).isoformat(),
                'duration_seconds': int(secs),
                'alone_seconds': 0,
                'snapshot_count': 0,
                'source': 'webhook_room',
                'confidence': 0.5,
                'built_at': built_at_iso,
            })

    for pkey, plist in list(intervals_by_participant.items()):
        windows = presence_windows_by_key.get(pkey, [])
        if not windows:
            continue  # no webhook presence -> nothing to synthesize
        wh = webhook_by_key.get(pkey)
        meeting_left = wh.meeting_left if wh else None
        brk_evts = _breakout_events_for(pkey)
        # Time already covered by ANY snapshot interval (breakout or main).
        covered = sorted(
            (_parse_ts(iv['start_ts']), _parse_ts(iv['end_ts']))
            for iv in plist if iv['source'] == 'snapshot'
        )
        synthesized = []
        for w_s, w_e in windows:
            if w_e is None:
                w_e = meeting_left or monitoring_end
            if w_e is None:
                continue
            # No phantom presence after monitoring/meeting ended.
            if monitoring_end and w_e > monitoring_end:
                w_e = monitoring_end
            if w_e <= w_s:
                continue
            # Walk the window, filling every stretch not already covered by
            # a snapshot interval from the webhook timeline (Main Room when
            # webhooks say Main, the actual breakout room when they don't).
            cursor = w_s
            for c_s, c_e in covered:
                if c_e <= cursor or c_s >= w_e:
                    continue
                if c_s > cursor:
                    _emit_uncovered_stretch(synthesized, pkey, brk_evts,
                                            w_s, cursor, min(c_s, w_e))
                cursor = max(cursor, c_e)
                if cursor >= w_e:
                    break
            if cursor < w_e:
                _emit_uncovered_stretch(synthesized, pkey, brk_evts, w_s, cursor, w_e)
        intervals.extend(synthesized)
        intervals_by_participant[pkey].extend(synthesized)

    # ----- Step 5: webhook timeline for participants with NO snapshots -----
    # Bot-primary, webhook-fallback. When the bot (snapshots) saw a
    # participant, Steps 3-4 already built their day and this step skips
    # them. When it did not — bot off all day, bot crashed mid-meeting,
    # person never captured by polling — reconstruct their room timeline
    # from the webhook event sequence itself: Main Room from meeting join
    # until the first breakout join, the breakout room until its left event
    # (or the next join), Main Room again after leaving, until meeting
    # leave. Room names come from the events (real names when calibration
    # mappings existed that day, 'Room-xxxxxxxx' otherwise — still counted
    # as breakout time either way).
    #
    # This replaces the old rule that skipped anyone with breakout webhooks
    # and no snapshots, which blanked entire bot-off days (e.g. 2026-07-15).
    #
    # Each PRESENCE WINDOW (join->leave pair) is credited separately:
    # someone who attended 09:00-09:10 and 17:00-17:30 gets 40 min, not the
    # whole day. A window with a missing leave (laptop closed, Zoom dropped
    # the event) is credited until monitoring end but capped tighter
    # (WEBHOOK_FILL_NO_LEAVE_CAP_MINUTES) because monitoring_end is the
    # MEETING's last activity, not this person's.
    snapshot_keys = set(intervals_by_participant.keys())

    def _emit_webhook_segment(pkey, wh, room, s_dt, e_dt, no_leave):
        secs = (e_dt - s_dt).total_seconds()
        if secs < WEBHOOK_SEGMENT_MIN_SECONDS:
            return
        secs = min(secs, MAIN_ROOM_SYNTH_CAP_MINUTES * 60)
        category = _classify_room(room)
        intervals.append({
            'interval_id': str(uuid_lib.uuid4()),
            'event_date': date_str,
            'meeting_id': wh.meeting_id,
            'meeting_uuid': None,
            'participant_key': pkey,
            'participant_name': wh.participant_name,
            'participant_email': wh.participant_email,
            'room_name': '0.Main Room' if category == 'main' else room,
            'room_category': category,
            'start_ts': s_dt.isoformat(),
            'end_ts': (s_dt + timedelta(seconds=secs)).isoformat(),
            'duration_seconds': int(secs),
            'alone_seconds': 0,
            'snapshot_count': 0,
            # webhook_fill = Main Room credit from webhooks (legacy name,
            # kept for audit continuity); webhook_room = placed in a
            # specific breakout room by breakout join/left webhooks.
            'source': 'webhook_fill' if category == 'main' else 'webhook_room',
            # 0.35 flags "leave webhook never arrived" fills for auditing
            'confidence': 0.35 if no_leave else 0.5,
            'built_at': built_at_iso,
        })

    # Compute IST day boundaries (00:00:00 IST to 23:59:59 IST) in UTC for
    # filtering out events from midnight-crossing meetings that belong to
    # the previous day.
    from datetime import timezone as _tz
    _day_start_utc = datetime.fromisoformat(date_str).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=_tz.utc
    ) - timedelta(minutes=IST_OFFSET_MINUTES)  # 00:00 IST = 18:30 UTC prev day
    _day_end_utc = _day_start_utc + timedelta(days=1)

    # MIDNIGHT-CROSSING FIX: The meeting runs ~9AM to ~9AM+1 day (24h continuous).
    # Events between midnight and early morning (00:00-08:00 IST) are likely from
    # the previous day's meeting continuing past midnight, NOT a fresh session.
    # For webhook-only participants (no snapshots), skip events before the
    # "safe" work start time to avoid double-counting attendance.
    # This is in addition to the boundary checks below.
    EARLY_MORNING_CUTOFF_IST_HOURS = 8  # Skip webhook-only events before 08:00 IST
    _early_cutoff_utc = _day_start_utc + timedelta(hours=EARLY_MORNING_CUTOFF_IST_HOURS)

    for pkey, wh in webhook_by_key.items():
        if pkey in snapshot_keys:
            continue  # bot data exists — snapshot path already covered them

        breakout_evts = _breakout_events_for(pkey)

        # Presence windows from join/leave events; if Zoom dropped every
        # join/leave but breakout events exist, fall back to an open window
        # starting at the first breakout event (no-leave cap applies).
        #
        # MIDNIGHT-CROSSING FIX: Only use breakout fallback if the first
        # breakout event is actually within this IST day. If the meeting
        # crossed midnight and the join was yesterday, breakout/leave events
        # on the new day should NOT create phantom intervals — that time is
        # already counted on the previous day.
        windows = presence_windows_by_key.get(pkey)
        if not windows:
            if wh.meeting_joined:
                # Verify the join is within this IST day (not a stale event
                # with wrong event_date due to timezone edge cases)
                if wh.meeting_joined >= _day_start_utc and wh.meeting_joined < _day_end_utc:
                    # EARLY-MORNING FILTER: For webhook-only participants, skip
                    # if their join is before 08:00 IST — likely a continuation
                    # from yesterday's meeting that crossed midnight.
                    if wh.meeting_joined < _early_cutoff_utc:
                        continue
                    windows = [(wh.meeting_joined, wh.meeting_left)]
                else:
                    # Join timestamp is outside this IST day — skip
                    continue
            elif breakout_evts:
                # Only use breakout fallback if the first event is within
                # this IST day. Breakout events after midnight from a
                # meeting that started yesterday should not create new
                # intervals — the person's time is counted on the start day.
                first_evt_ts = _evt(breakout_evts[0], 'ts')
                if first_evt_ts >= _day_start_utc and first_evt_ts < _day_end_utc:
                    # EARLY-MORNING FILTER: Same check for breakout fallback
                    if first_evt_ts < _early_cutoff_utc:
                        continue
                    windows = [(first_evt_ts, None)]
                else:
                    continue
            else:
                continue

        for w_start, w_leave in windows:
            if w_start is None:
                continue

            # MIDNIGHT-CROSSING FIX: Skip windows that start before this IST
            # day begins. These are tail events from a previous-day meeting
            # that got event_date set to today because their UTC timestamp
            # crossed midnight IST. The actual presence was already counted
            # on the day the meeting started.
            if w_start < _day_start_utc:
                # If the window ends within today, clamp to day start
                if w_leave and w_leave > _day_start_utc:
                    w_start = _day_start_utc
                else:
                    continue

            # EARLY-MORNING FILTER: For webhook-only participants, skip windows
            # that start before 08:00 IST — likely a continuation from yesterday's
            # meeting. This catches cases where presence_windows_by_key has early
            # morning windows from events that got event_date = today.
            if w_start < _early_cutoff_utc:
                # Clamp to early cutoff if the window extends past it
                if w_leave and w_leave > _early_cutoff_utc:
                    w_start = _early_cutoff_utc
                else:
                    continue

            no_leave = w_leave is None
            w_end = w_leave or monitoring_end
            if not w_end:
                continue
            if monitoring_end and w_end > monitoring_end:
                w_end = monitoring_end
            if no_leave:
                cap = timedelta(minutes=WEBHOOK_FILL_NO_LEAVE_CAP_MINUTES)
                if w_end - w_start > cap:
                    w_end = w_start + cap
            if w_end <= w_start:
                continue

            # Tile [w_start, w_end] with Main/breakout segments from the
            # breakout events inside this window.
            for room, t_s, t_e in _tile_window_with_rooms(breakout_evts, w_start, w_end):
                _emit_webhook_segment(pkey, wh, room, t_s, t_e, no_leave)

    # ----- Step 5b: drop the inherited (overnight) meeting block -----------
    # See the INHERITED_* constants above for the full rationale. We rebuild
    # global per-30s-bucket occupancy from the snapshot buckets, check whether
    # a large cohort was already present at the very start of the IST day, and
    # if so find the mass-exit boundary (occupancy falls below a fraction of
    # that start level and STAYS low). Every interval starting before that
    # boundary is the tail of yesterday's meeting and is dropped.
    inherited_cutoff_utc = None
    if buckets:
        presence_by_bucket = defaultdict(set)
        for b in buckets:
            presence_by_bucket[b.bucket30].add(_remap(b.participant_key))
        # First 30s bucket of this IST day: 00:00 IST == UTC midnight - 5:30.
        from datetime import timezone as _tz
        day0_utc_unix = int(
            datetime.fromisoformat(date_str).replace(tzinfo=_tz.utc).timestamp()
        ) - IST_OFFSET_MINUTES * 60
        day_start_bucket = day0_utc_unix // BUCKET_SECONDS
        # Occupancy at the very start of the IST day (peak over first 5 min).
        start_level = max(
            (len(presence_by_bucket.get(day_start_bucket + i, ())) for i in range(10)),
            default=0,
        )
        if start_level >= INHERITED_MIN_PARTICIPANTS:
            threshold = max(1, int(start_level * INHERITED_LEAVE_FRACTION))
            max_scan_bucket = day_start_bucket + (INHERITED_MAX_BOUNDARY_IST_MIN * 60) // BUCKET_SECONDS
            sustain = INHERITED_EXIT_SUSTAIN_BUCKETS
            scan = day_start_bucket
            while scan <= max_scan_bucket:
                # A real mass-exit stays low; a one-bucket polling blip does not.
                if all(len(presence_by_bucket.get(scan + i, ())) < threshold
                       for i in range(sustain)):
                    # Keep tz-aware (UTC): the interval start_ts values parsed
                    # below come from BigQuery timestamps and are tz-aware, so
                    # the cutoff must be too or the comparison raises.
                    inherited_cutoff_utc = datetime.fromtimestamp(
                        scan * BUCKET_SECONDS, _tz.utc)
                    break
                scan += 1
            if inherited_cutoff_utc is None:
                print(f"[BuildIntervals {date_str}] WARNING: {start_level} present at "
                      f"00:00 IST but no sustained mass-exit found before "
                      f"{INHERITED_MAX_BOUNDARY_IST_MIN // 60:02d}:00 IST — no overnight drop applied")

    if inherited_cutoff_utc is not None:
        kept_intervals = []
        dropped_n = 0
        dropped_secs = 0
        clamped_n = 0
        for iv in intervals:
            iv_start = _parse_ts(iv['start_ts'])
            iv_end = _parse_ts(iv['end_ts'])
            if iv_end <= inherited_cutoff_utc:
                # Entirely inside yesterday's tail — drop.
                dropped_n += 1
                dropped_secs += iv['duration_seconds']
            elif iv_start < inherited_cutoff_utc:
                # Spans the boundary: keep the post-cutoff remainder instead
                # of throwing the whole interval away (which also destroyed
                # its valid same-day time).
                keep_secs = int((iv_end - inherited_cutoff_utc).total_seconds())
                if keep_secs <= 0:
                    # Ends <1s past the cutoff — nothing meaningful to keep
                    dropped_n += 1
                    dropped_secs += iv['duration_seconds']
                    continue
                dropped_secs += max(0, iv['duration_seconds'] - keep_secs)
                iv['start_ts'] = inherited_cutoff_utc.isoformat()
                iv['duration_seconds'] = min(iv['duration_seconds'], keep_secs)
                iv['alone_seconds'] = min(iv['alone_seconds'], iv['duration_seconds'])
                clamped_n += 1
                kept_intervals.append(iv)
            else:
                kept_intervals.append(iv)
        if dropped_n or clamped_n:
            ist_cut = inherited_cutoff_utc + timedelta(minutes=IST_OFFSET_MINUTES)
            print(f"[BuildIntervals {date_str}] inherited overnight meeting detected "
                  f"(start_level={start_level}); dropped {dropped_n}, clamped {clamped_n} "
                  f"intervals ({dropped_secs // 60} min removed) before {ist_cut.strftime('%H:%M')} IST")
        intervals = kept_intervals

    # ----- Step 6: Atomic partition replace via load job -------------------
    # Use a BigQuery LOAD job with WRITE_TRUNCATE and a partition decorator
    # instead of DELETE+streaming-INSERT. This avoids the streaming buffer
    # entirely: load jobs go straight to the partition's permanent storage,
    # and WRITE_TRUNCATE replaces the partition atomically (no DELETE needed,
    # no buffer-blocks-DELETE race). Also free (no streaming insert cost).
    import io as _io
    table_id = f"{dataset_ref}.{PRESENCE_INTERVALS_TABLE}"
    partition_id = f"{table_id}${date_str.replace('-', '')}"

    inserted = 0
    if intervals:
        ndjson = "\n".join(json.dumps(iv) for iv in intervals)
        load_cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=[
                bigquery.SchemaField("interval_id",       "STRING",    mode="REQUIRED"),
                bigquery.SchemaField("event_date",        "DATE",      mode="REQUIRED"),
                bigquery.SchemaField("meeting_id",        "STRING"),
                bigquery.SchemaField("meeting_uuid",      "STRING"),
                bigquery.SchemaField("participant_key",   "STRING",    mode="REQUIRED"),
                bigquery.SchemaField("participant_name",  "STRING"),
                bigquery.SchemaField("participant_email", "STRING"),
                bigquery.SchemaField("room_name",         "STRING"),
                bigquery.SchemaField("room_category",     "STRING"),
                bigquery.SchemaField("start_ts",          "TIMESTAMP", mode="REQUIRED"),
                bigquery.SchemaField("end_ts",            "TIMESTAMP", mode="REQUIRED"),
                bigquery.SchemaField("duration_seconds",  "INT64"),
                bigquery.SchemaField("alone_seconds",     "INT64"),
                bigquery.SchemaField("snapshot_count",    "INT64"),
                bigquery.SchemaField("source",            "STRING"),
                bigquery.SchemaField("confidence",        "FLOAT64"),
                bigquery.SchemaField("built_at",          "TIMESTAMP"),
            ],
        )
        load_job = client.load_table_from_file(
            _io.BytesIO(ndjson.encode('utf-8')),
            partition_id,
            job_config=load_cfg,
        )
        load_job.result()  # wait for completion; raises on error
        inserted = len(intervals)
    else:
        # No intervals for this date — clear the partition so it stays
        # consistent with the source data. Empty load with WRITE_TRUNCATE
        # is also fine (creates a 0-row partition).
        load_cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        load_job = client.load_table_from_file(
            _io.BytesIO(b""),
            partition_id,
            job_config=load_cfg,
        )
        try:
            load_job.result()
        except Exception:
            # Empty load can sometimes 400; safe to ignore — no data to write
            pass

    # ----- Summary stats ---------------------------------------------------
    by_source = defaultdict(int)
    by_category = defaultdict(int)
    duration_total = 0
    participants = set()
    for iv in intervals:
        by_source[iv['source']] += 1
        by_category[iv['room_category']] += 1
        duration_total += iv['duration_seconds']
        participants.add(iv['participant_key'])

    return {
        'date': date_str,
        'intervals_built': inserted,
        'by_source': dict(by_source),
        'by_category': dict(by_category),
        'participants': len(participants),
        'duration_seconds_total': duration_total,
        'monitoring_window_end_utc': monitoring_end.isoformat() if monitoring_end else None,
        'elapsed_seconds': round(time.time() - started, 2),
    }


def _auto_build_dates_in_range(start_date, end_date, max_builds=15, force=False):
    """Build presence_intervals for dates in [start, end] that need it, and
    return (built_count, still_missing_count).

    When PAGELOAD_AUTO_BUILD is disabled, page-triggered calls become no-ops
    (pages are pure readers; Cloud Scheduler owns freshness via
    /intervals/rebuild + /intervals/auto-build). force=True bypasses the
    gate — used by the scheduler endpoint itself.

    Freshness for recent days is owned by Cloud Scheduler:
      - an hourly job rebuilds TODAY  (so the pivot is never >1h stale), and
      - a nightly job rebuilds YESTERDAY at 00:30 IST (after it completes).
    This function is the lazy/self-healing backstop on top of that:
      1. Dates with NO rows yet: built once, capped at max_builds, so a wide
         range the user navigates to still shows something even if backfill
         wasn't run. Already-built older days are stable — left alone.
      2. The still-settling window (TODAY/YESTERDAY IST) is rebuilt ONLY if its
         materialization is STALE (older than SETTLING_STALE_MINUTES). That way
         if a scheduler ever dies (e.g. pointed at a dead URL), opening the view
         self-heals the frozen day — but a freshly-built day adds no latency.
         Builds are idempotent (load-job WRITE_TRUNCATE) so re-running is safe.
    """
    if not PAGELOAD_AUTO_BUILD and not force:
        return 0, 0

    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
    _ensure_presence_intervals_table()

    # Still-settling window: today + yesterday IST, intersected with the range.
    today_ist = get_ist_date()
    yesterday_ist = (get_ist_now() - timedelta(days=1)).strftime('%Y-%m-%d')
    settling = {d for d in (today_ist, yesterday_ist) if start_date <= d <= end_date}

    # Per-date row count + freshness + latest SOURCE activity in one pass.
    # last_source lets past days self-heal: if a day's materialization is
    # OLDER than the last snapshot/webhook observed for that day (e.g. the
    # hourly job built it at 15:10 but the meeting ran to 18:30 and the
    # nightly rebuild died), it is rebuilt instead of staying frozen short
    # forever.
    q = f"""
    WITH wanted AS (
      SELECT day FROM UNNEST(GENERATE_DATE_ARRAY(@start, @end)) AS day
    ),
    built AS (
      SELECT event_date, MAX(built_at) AS last_built
      FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
      WHERE event_date BETWEEN @start AND @end
      GROUP BY event_date
    ),
    snap_src AS (
      SELECT event_date, MAX(snapshot_time) AS last_src
      FROM `{dataset_ref}.room_snapshots_v2`
      WHERE event_date BETWEEN @start AND @end
      GROUP BY event_date
    ),
    evt_src AS (
      -- Use DELIVERY time (inserted_at) when available: Zoom retries failed
      -- webhooks for hours, so late-DELIVERED events with old event
      -- timestamps must still mark the build as outdated.
      SELECT event_date,
             MAX(COALESCE(CAST(inserted_at AS TIMESTAMP),
                          CAST(event_timestamp AS TIMESTAMP))) AS last_src
      FROM `{dataset_ref}.{BQ_EVENTS_TABLE}`
      WHERE event_date BETWEEN @start AND @end
      GROUP BY event_date
    )
    SELECT
      w.day,
      b.event_date IS NOT NULL AS has_rows,
      (s.last_src IS NOT NULL OR e.last_src IS NOT NULL) AS has_source,
      TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), b.last_built, MINUTE) AS age_min,
      (
        b.last_built IS NOT NULL AND
        GREATEST(
          COALESCE(s.last_src, TIMESTAMP('1970-01-01')),
          COALESCE(e.last_src, TIMESTAMP('1970-01-01'))
        ) > b.last_built
      ) AS build_outdated
    FROM wanted w
    LEFT JOIN built b ON w.day = b.event_date
    LEFT JOIN snap_src s ON w.day = s.event_date
    LEFT JOIN evt_src e ON w.day = e.event_date
    ORDER BY w.day DESC
    """
    rows = list(client.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start", "DATE", start_date),
            bigquery.ScalarQueryParameter("end", "DATE", end_date),
        ]
    )).result())

    missing = []         # older dates never built
    stale_settling = []  # today/yesterday needing a rebuild
    outdated = []        # past dates whose build predates their latest source data
    for r in rows:
        d = r.day.isoformat()
        # NEVER build future dates: they have no source data yet, and an empty
        # partition stays at 0 rows, so they'd look "missing" and get rebuilt on
        # EVERY load — e.g. viewing the current month rebuilds all remaining
        # days of the month each time, adding ~20s+ of pointless latency.
        if d > today_ist:
            continue
        if not r.has_rows:
            # No source data at all (weekend/holiday/pre-deployment day):
            # building it would produce 0 rows AGAIN, so it would look
            # "missing" and be rebuilt on EVERY load — a permanent
            # rebuild loop costing ~20s + several BQ queries per empty day
            # per page view. Skip outright.
            if not r.has_source:
                continue
            if d not in settling:
                missing.append(d)
            else:
                stale_settling.append(d)  # in window but never built -> build
        elif d == today_ist:
            # Rebuild today, matching Day View / attendance_v2. This was the
            # Team-vs-Day discrepancy: Day View force-rebuilt today while the
            # monthly/range pivot served up-to-90-min-stale data. The
            # in-process guard skips it if a rebuild ran in the last ~3 min.
            if (_TODAY_BUILD_GUARD['date'] != d
                    or time.time() - _TODAY_BUILD_GUARD['ts'] >= TODAY_REBUILD_MIN_INTERVAL_S):
                stale_settling.append(d)
        elif d in settling and (r.age_min is None or r.age_min >= SETTLING_STALE_MINUTES):
            stale_settling.append(d)
        elif r.build_outdated:
            outdated.append(d)

    built = 0
    # 1. Refresh today/yesterday first (self-healing backstop).
    for d in sorted(stale_settling, reverse=True):
        try:
            if d == today_ist:
                if _rebuild_today_guarded(d):
                    built += 1
            else:
                build_presence_intervals(d)
                built += 1
        except Exception as e:
            print(f"[AutoBuildRange] settling-day {d} failed: {e}")
    # 2. Build never-built dates, then self-heal outdated ones, sharing the cap.
    to_build = missing[:max_builds]
    remaining_slots = max_builds - len(to_build)
    to_build += sorted(outdated, reverse=True)[:remaining_slots]
    for d in to_build:
        try:
            build_presence_intervals(d)
            built += 1
        except Exception as e:
            print(f"[AutoBuildRange] {d} failed: {e}")
    # Report BOTH kinds of leftover work so the UI's "unbuilt dates" count
    # is honest: never-built dates and stale-but-built dates beyond the cap.
    still_missing = max(0, len(missing) + len(outdated) - max_builds)
    return built, still_missing


# ============================================================================
# PURE-BQ-SQL INTERVAL BUILDER (webhook-primary, 2026-07-20)
# ============================================================================
# Replicates the webhook-timeline logic of build_presence_intervals (Step 5 +
# identity map + midnight/early-morning filters) entirely INSIDE BigQuery:
# the state machine runs as a JavaScript UDF in the query engine, so no event
# data flows through Python. Output goes to a SEPARATE table
# (presence_intervals_sql) for side-by-side comparison - production reports
# keep reading presence_intervals until the numbers are verified equal.
#
# Deliberate scope: webhook events ONLY (the webhook-primary future). On days
# where the bot also produced snapshots, snapshot participants' hours WILL
# differ from the Python table (which prefers snapshots) - the compare
# endpoint flags those rows with had_bot_data=TRUE.

PRESENCE_INTERVALS_SQL_TABLE = 'presence_intervals_sql'


def _validate_date(date_str):
    import re as _re_mod
    if not _re_mod.match(r'^\d{4}-\d{2}-\d{2}$', str(date_str or '')):
        raise ValueError(f"invalid date: {date_str!r} (expected YYYY-MM-DD)")
    return str(date_str)


# The JS body is a plain (non-f) string: it is full of braces and must reach
# BigQuery verbatim. Constants mirror the Python builder's defaults.
_BUILD_INTERVALS_JS = r"""
var COALESCE_GAP_MS = 30 * 1000;        // COALESCE_GAP_S
var SEG_MIN_MS      = 60 * 1000;        // WEBHOOK_SEGMENT_MIN_SECONDS
var SEG_CAP_MS      = 600 * 60 * 1000;  // MAIN_ROOM_SYNTH_CAP_MINUTES
var NO_LEAVE_CAP_MS = 240 * 60 * 1000;  // WEBHOOK_FILL_NO_LEAVE_CAP_MINUTES

if (!events || events.length === 0) return [];
events = events.slice().sort(function(a, b) { return a.ts - b.ts; });

var JOINS = ['participant_joined', 'meeting.participant_joined'];
var LEFTS = ['participant_left', 'meeting.participant_left'];

var brk = events.filter(function(e) {
  return e.et === 'breakout_room_joined' || e.et === 'breakout_room_left';
});

// ---- presence windows (= _build_presence_windows) ----
var raw = [];
var cur = null;
events.forEach(function(e) {
  if (JOINS.indexOf(e.et) >= 0) {
    if (cur === null) cur = e.ts;
  } else if (LEFTS.indexOf(e.et) >= 0) {
    if (cur !== null) { raw.push([cur, e.ts]); cur = null; }
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

// ---- fallbacks when no join/leave pairs exist (= Step 5 head) ----
if (windows.length === 0) {
  var joinTs = null, leftTs = null;
  events.forEach(function(e) {
    if (JOINS.indexOf(e.et) >= 0 && (joinTs === null || e.ts < joinTs)) joinTs = e.ts;
    if (LEFTS.indexOf(e.et) >= 0 && (leftTs === null || e.ts > leftTs)) leftTs = e.ts;
  });
  if (joinTs !== null) {
    if (joinTs >= day_start && joinTs < day_end) {
      if (joinTs < early_cutoff) return [];   // midnight-crossing continuation
      windows.push([joinTs, leftTs]);
    } else { return []; }
  } else if (brk.length > 0) {
    var first = brk[0].ts;
    if (first >= day_start && first < day_end) {
      if (first < early_cutoff) return [];
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

  // midnight-crossing: window started before this IST day
  if (wStart < day_start) {
    if (wLeave !== null && wLeave > day_start) { wStart = day_start; } else { return; }
  }
  // early-morning filter: before 08:00 IST = yesterday's meeting continuing
  if (wStart < early_cutoff) {
    if (wLeave !== null && wLeave > early_cutoff) { wStart = early_cutoff; } else { return; }
  }

  var noLeave = (wLeave === null);
  var wEnd = (wLeave !== null) ? wLeave : monitoring_end;
  if (wEnd === null || wEnd === undefined) return;
  if (monitoring_end !== null && wEnd > monitoring_end) wEnd = monitoring_end;
  if (noLeave && (wEnd - wStart) > NO_LEAVE_CAP_MS) {
    wEnd = new Date(wStart.getTime() + NO_LEAVE_CAP_MS);
  }
  if (wEnd <= wStart) return;

  // ---- tile the window (= _tile_window_with_rooms) ----
  var curRoom = 'Main Room';
  var segStart = wStart;
  var prevEt = null, prevRoom = null, prevTs = null;
  var tiles = [];
  brk.forEach(function(e) {
    var ts = e.ts;
    if (ts < wStart || ts > wEnd) return;
    // Zoom double-send: identical event repeated < 5s apart
    if (prevEt === e.et && (prevRoom || '') === (e.room || '') &&
        prevTs !== null && (ts - prevTs) < 5000) return;
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
"""


def build_presence_intervals_sql(date_str):
    """Build presence intervals for one IST date ENTIRELY in BigQuery,
    writing to presence_intervals_sql. Returns dict with counts + timing."""
    started = time.time()
    date_str = _validate_date(date_str)
    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
    norm_pn = _sql_normalize_name('pe.participant_name')
    norm_sn = _sql_normalize_name('s.participant_name')

    # date_str is regex-validated above, safe to inline as a DATE literal.
    # (Inline literal because multi-statement scripts + query params are
    # brittle; the regex makes injection impossible.)
    d = f"DATE '{date_str}'"
    day_start = f"TIMESTAMP_SUB(TIMESTAMP({d}), INTERVAL {IST_OFFSET_MINUTES} MINUTE)"

    js_quote = '"""'
    script = f"""
    CREATE TEMP FUNCTION build_intervals(
        events ARRAY<STRUCT<ts TIMESTAMP, et STRING, room STRING>>,
        monitoring_end TIMESTAMP,
        day_start TIMESTAMP,
        day_end TIMESTAMP,
        early_cutoff TIMESTAMP)
    RETURNS ARRAY<STRUCT<room_name STRING, room_category STRING,
                         source STRING, confidence FLOAT64,
                         start_ts TIMESTAMP, end_ts TIMESTAMP>>
    LANGUAGE js AS r{js_quote}{_BUILD_INTERVALS_JS}{js_quote};

    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{PRESENCE_INTERVALS_SQL_TABLE}` (
        interval_id        STRING NOT NULL,
        event_date         DATE   NOT NULL,
        meeting_id         STRING,
        meeting_uuid       STRING,
        participant_key    STRING NOT NULL,
        participant_name   STRING,
        participant_email  STRING,
        room_name          STRING,
        room_category      STRING,
        start_ts           TIMESTAMP NOT NULL,
        end_ts             TIMESTAMP NOT NULL,
        duration_seconds   INT64,
        alone_seconds      INT64,
        snapshot_count     INT64,
        source             STRING,
        confidence         FLOAT64,
        built_at           TIMESTAMP
    )
    PARTITION BY event_date
    CLUSTER BY meeting_id, participant_key;

    DELETE FROM `{dataset_ref}.{PRESENCE_INTERVALS_SQL_TABLE}`
    WHERE event_date = {d};

    INSERT INTO `{dataset_ref}.{PRESENCE_INTERVALS_SQL_TABLE}`
        (interval_id, event_date, meeting_id, meeting_uuid, participant_key,
         participant_name, participant_email, room_name, room_category,
         start_ts, end_ts, duration_seconds, alone_seconds, snapshot_count,
         source, confidence, built_at)
    WITH base AS (
      SELECT
        {norm_pn} AS name_key,
        pe.participant_name,
        pe.participant_email,
        -- meeting_id is INT64 in participant_events_p (same quirk that bit
        -- load_mappings_from_bigquery on 2026-07-17) — target column is STRING
        CAST(pe.meeting_id AS STRING) AS meeting_id,
        CAST(pe.event_timestamp AS TIMESTAMP) AS ts,
        pe.event_type AS et,
        COALESCE(rm.mapped_name, pe.room_name) AS room
      FROM `{dataset_ref}.{BQ_EVENTS_TABLE}` pe
      LEFT JOIN (
        SELECT room_uuid,
               ARRAY_AGG(room_name ORDER BY mapped_at DESC LIMIT 1)[OFFSET(0)] AS mapped_name
        FROM `{dataset_ref}.{BQ_MAPPINGS_TABLE}`
        WHERE CAST(mapping_date AS STRING) = CAST({d} AS STRING)
          AND room_uuid IS NOT NULL AND room_uuid != ''
          AND room_name IS NOT NULL AND room_name != ''
          AND room_name NOT LIKE 'Room-%'
        GROUP BY room_uuid
      ) rm ON pe.room_uuid = rm.room_uuid
      WHERE pe.event_date = {d}
        AND pe.participant_name IS NOT NULL AND pe.participant_name != ''
        AND LOWER(pe.participant_name) NOT LIKE '%scout%'
        AND pe.event_type IN ('participant_joined','participant_left',
                              'meeting.participant_joined','meeting.participant_left',
                              'breakout_room_joined','breakout_room_left')
    ),
    -- day-scoped name->email identity map (same rule as the Python builder:
    -- a name maps to an email only when it maps to EXACTLY ONE email that
    -- day, pairs drawn from both webhooks and bot snapshots)
    ident_pairs AS (
      SELECT DISTINCT name_key, LOWER(TRIM(participant_email)) AS email
      FROM base
      WHERE participant_email IS NOT NULL AND TRIM(participant_email) != ''
      UNION DISTINCT
      SELECT DISTINCT {norm_sn} AS name_key, LOWER(TRIM(s.participant_email)) AS email
      FROM `{dataset_ref}.room_snapshots_v2` s
      WHERE s.event_date = {d}
        AND s.participant_name IS NOT NULL AND s.participant_name != ''
        AND s.participant_email IS NOT NULL AND TRIM(s.participant_email) != ''
        AND LOWER(s.participant_name) NOT LIKE '%scout%'
    ),
    ident AS (
      -- alias is mapped_email, NOT email: HAVING COUNT(DISTINCT email) with
      -- an aggregate alias named email trips BigQuery (2026-07-16 incident)
      SELECT name_key, ANY_VALUE(email) AS mapped_email
      FROM ident_pairs
      GROUP BY name_key
      HAVING COUNT(DISTINCT email) = 1
    ),
    participants AS (
      SELECT
        COALESCE(i.mapped_email, b.name_key) AS participant_key,
        ANY_VALUE(b.participant_name) AS participant_name,
        ANY_VALUE(NULLIF(TRIM(COALESCE(b.participant_email, '')), '')) AS participant_email,
        ANY_VALUE(NULLIF(COALESCE(b.meeting_id, ''), '')) AS meeting_id,
        ARRAY_AGG(STRUCT(b.ts AS ts, b.et AS et, b.room AS room) ORDER BY b.ts) AS events
      FROM base b
      LEFT JOIN ident i ON b.name_key = i.name_key
      GROUP BY participant_key
    ),
    -- capture end = last leave webhook of the day (Step 2c, no-snapshot day)
    mon AS (
      SELECT MAX(ts) AS monitoring_end
      FROM base
      WHERE et IN ('participant_left', 'meeting.participant_left')
    )
    SELECT
      GENERATE_UUID(),
      {d},
      p.meeting_id,
      CAST(NULL AS STRING),
      p.participant_key,
      p.participant_name,
      p.participant_email,
      t.room_name,
      t.room_category,
      t.start_ts,
      t.end_ts,
      TIMESTAMP_DIFF(t.end_ts, t.start_ts, SECOND),
      0,
      0,
      t.source,
      t.confidence,
      CURRENT_TIMESTAMP()
    FROM participants p
    CROSS JOIN mon
    CROSS JOIN UNNEST(build_intervals(
        p.events,
        mon.monitoring_end,
        {day_start},
        TIMESTAMP_ADD({day_start}, INTERVAL 24 HOUR),
        TIMESTAMP_ADD({day_start}, INTERVAL 8 HOUR)
    )) AS t;
    """
    client.query(script).result()

    stats = list(client.query(f"""
        SELECT COUNT(*) AS n_rows,
               COUNT(DISTINCT participant_key) AS n_participants,
               ROUND(SUM(duration_seconds) / 3600, 1) AS total_hours
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_SQL_TABLE}`
        WHERE event_date = {d}
    """).result())[0]

    return {
        'date': date_str,
        'table': PRESENCE_INTERVALS_SQL_TABLE,
        'rows': stats.n_rows,
        'participants': stats.n_participants,
        'total_hours': float(stats.total_hours or 0),
        'seconds': round(time.time() - started, 1),
    }


def compare_sql_vs_python(date_str, top_n=20):
    """Per-participant daily totals: presence_intervals_sql vs
    presence_intervals. had_bot_data=TRUE rows are EXPECTED to differ while
    the snapshot pipeline still runs (Python prefers bot data there)."""
    date_str = _validate_date(date_str)
    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
    q = f"""
    WITH s AS (
      SELECT participant_key, ANY_VALUE(participant_name) AS name,
             SUM(duration_seconds) / 3600 AS hrs
      FROM `{dataset_ref}.{PRESENCE_INTERVALS_SQL_TABLE}`
      WHERE event_date = @d
      GROUP BY participant_key
    ),
    p AS (
      SELECT participant_key, ANY_VALUE(participant_name) AS name,
             SUM(duration_seconds) / 3600 AS hrs,
             LOGICAL_OR(source = 'snapshot') AS had_bot
      FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
      WHERE event_date = @d
      GROUP BY participant_key
    )
    SELECT participant_key,
           COALESCE(s.name, p.name) AS participant_name,
           ROUND(IFNULL(s.hrs, 0), 2) AS sql_hours,
           ROUND(IFNULL(p.hrs, 0), 2) AS python_hours,
           ROUND(IFNULL(s.hrs, 0) - IFNULL(p.hrs, 0), 2) AS diff_hours,
           IFNULL(p.had_bot, FALSE) AS had_bot_data
    FROM s
    FULL OUTER JOIN p USING (participant_key)
    ORDER BY ABS(IFNULL(s.hrs, 0) - IFNULL(p.hrs, 0)) DESC
    """
    rows = list(client.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", date_str)]
    )).result())

    all_rows = [{
        'participant_key': r.participant_key,
        'participant_name': r.participant_name,
        'sql_hours': float(r.sql_hours),
        'python_hours': float(r.python_hours),
        'diff_hours': float(r.diff_hours),
        'had_bot_data': bool(r.had_bot_data),
    } for r in rows]

    webhook_only = [r for r in all_rows if not r['had_bot_data']]
    matched = [r for r in webhook_only if abs(r['diff_hours']) <= 0.1]
    return {
        'date': date_str,
        'participants_total': len(all_rows),
        'webhook_only_participants': len(webhook_only),
        'webhook_only_matched_within_6min': len(matched),
        'webhook_only_mismatched': len(webhook_only) - len(matched),
        'note': ('had_bot_data=TRUE rows are EXPECTED to differ: the Python '
                 'builder uses bot snapshots for them, the SQL builder is '
                 'webhook-only by design. Judge parity on webhook-only rows.'),
        'top_differences': all_rows[:top_n],
    }


