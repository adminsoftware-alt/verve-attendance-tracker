"""Room-mapping domain module (review point 5).

First slice of pulling the mapping logic out of app.py:

  1. PURE decision logic — who counts as a mapping witness. Two fixes over
     the old inline code in /mapping/sync:
       - A person is ONE witness. The old loop iterated lookup KEYS, and a
         person with both an email and a name key counted twice, silently
         bypassing the 2-witness rule (the rule added after the 17-20 Aug
         incident: 63 hours filed as break off one person's position).
       - A display name shared by 2+ no-email participants in the SDK
         payload identifies nobody, so it is excluded as a witness instead
         of letting the last one overwrite the first.
  2. DISPUTED persistence — the anti-ping-pong freeze used to live only in
     meeting_state.mapping_disputes (process memory), so every deploy forgot
     it and a frozen room could resume flip-flopping. Frozen rooms are now
     also written to BigQuery (`mapping_disputes`) and re-loaded once per
     process per IST day.

app.py's /mapping/sync still orchestrates (snapshots, locks, BQ mapping
writes); it calls into here for the decisions above.
"""
import re
import threading
import traceback
from datetime import datetime, timedelta

__all__ = [
    'norm_pname',
    'build_sdk_persons',
    'select_witness_entry',
    'MAPPING_DISPUTES_TABLE',
    'persist_dispute',
    'hydrate_disputes',
    'hydrate_positions',
    'load_recent_positions',
    'count_disputes_today',
]

IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_today():
    return (datetime.utcnow() + IST_OFFSET).strftime('%Y-%m-%d')


def norm_pname(n):
    """Normalize a display name the way webhook tracking does: strip Zoom's
    rejoin suffix '-N', lowercase, trim."""
    return re.sub(r'-\d+$', '', (n or '').strip().lower()).strip()


def build_sdk_persons(rooms):
    """One entry per PERSON in the SDK sync payload.

    Returns (persons, ambiguous_names):
      persons          list of {'person_id', 'room_name', 'keys'} where keys
                       are participant_current_breakout lookup keys, email
                       first (same order _bo_state_keys writes them).
      ambiguous_names  set of normalized names shared by 2+ NO-EMAIL
                       participants — excluded from persons entirely, because
                       "which Priya" is unanswerable and a wrong guess names
                       a room after the wrong team.
    """
    # First pass: how many no-email participants carry each normalized name
    name_counts = {}
    for room in rooms or []:
        if not room.get('room_name'):
            continue
        for part in (room.get('participants') or []):
            em = (part.get('email') or '').strip().lower()
            nm = norm_pname(part.get('name'))
            if nm and not em:
                name_counts[nm] = name_counts.get(nm, 0) + 1
    ambiguous = {nm for nm, c in name_counts.items() if c > 1}

    persons = {}
    for room in rooms or []:
        rname = room.get('room_name')
        if not rname:
            continue
        for part in (room.get('participants') or []):
            em = (part.get('email') or '').strip().lower()
            nm = norm_pname(part.get('name'))
            if em:
                pid = 'e:' + em
                keys = ['e:' + em] + (['n:' + nm] if nm else [])
            elif nm:
                if nm in ambiguous:
                    continue
                pid = 'n:' + nm
                keys = ['n:' + nm]
            else:
                continue
            # dict keyed by person: a person listed twice stays one witness
            persons[pid] = {'person_id': pid, 'room_name': rname, 'keys': keys}
    return list(persons.values()), ambiguous


def select_witness_entry(person, bo_state):
    """The webhook-side position for this person, or None. Email key wins,
    matching the write order in _track_breakout_state."""
    for k in person['keys']:
        entry = bo_state.get(k)
        if entry:
            return entry
    return None


# ══════════════════════════════════════════════════════════════════════════
# DISPUTED persistence (BigQuery) — survives restarts and is queryable
# ══════════════════════════════════════════════════════════════════════════
MAPPING_DISPUTES_TABLE = 'mapping_disputes'

_state = {
    'ensured': False,
    'hydrated_date': None,   # IST date string for which memory was hydrated
    'lock': threading.Lock(),
}


def _table_id(cfg):
    return f"{cfg['project']}.{cfg['dataset']}.{MAPPING_DISPUTES_TABLE}"


def _ensure_table(client, cfg):
    if _state['ensured']:
        return
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{_table_id(cfg)}` (
          dispute_date  DATE   NOT NULL,
          room_uuid     STRING NOT NULL,
          meeting_uuid  STRING,
          kept_name     STRING,
          rejected_name STRING,
          frozen_at     TIMESTAMP
        )
    """).result()
    _state['ensured'] = True


def persist_dispute(get_client, cfg, room_uuid, meeting_uuid='',
                    kept_name='', rejected_name=''):
    """Record a DISPUTED freeze durably. Never raises — a failed write only
    means the freeze is memory-only, exactly the pre-v15 behaviour."""
    try:
        client = get_client()
        _ensure_table(client, cfg)
        client.insert_rows_json(_table_id(cfg), [{
            'dispute_date': _ist_today(),
            'room_uuid': room_uuid,
            'meeting_uuid': meeting_uuid or '',
            'kept_name': kept_name or '',
            'rejected_name': rejected_name or '',
            'frozen_at': datetime.utcnow().isoformat(),
        }])
        print(f"[mapping] DISPUTED persisted to BigQuery: {room_uuid[:20]}...")
        return True
    except Exception as e:
        print(f"[mapping] WARN: could not persist DISPUTED for {room_uuid[:20]}...: {e}")
        return False


def load_disputes_for_today(get_client, cfg):
    """{room_uuid: frozen_at_epoch} for the current IST date."""
    client = get_client()
    rows = client.query(f"""
        SELECT room_uuid, UNIX_SECONDS(MAX(frozen_at)) AS frozen_epoch
        FROM `{_table_id(cfg)}`
        WHERE dispute_date = '{_ist_today()}'
        GROUP BY room_uuid
    """).result()
    return {r.room_uuid: float(r.frozen_epoch or 0) for r in rows}


def hydrate_disputes(get_client, cfg, meeting_state):
    """Merge today's persisted DISPUTED freezes into meeting_state — once per
    process per IST day, so a restarted server stays frozen. Never raises."""
    today = _ist_today()
    with _state['lock']:
        if _state['hydrated_date'] == today:
            return 0
        _state['hydrated_date'] = today
    try:
        loaded = load_disputes_for_today(get_client, cfg)
    except Exception as e:
        # Table missing = no dispute was ever persisted: nothing to merge,
        # stay memoized for the day. A TRANSIENT error un-memoizes so the
        # next sync (60s) retries instead of losing the whole day.
        if 'not found' not in str(e).lower():
            with _state['lock']:
                _state['hydrated_date'] = None
        print(f"[mapping] dispute hydration skipped: {e}")
        return 0
    merged = 0
    with meeting_state._lock:
        for wu, ts in loaded.items():
            if wu not in meeting_state.mapping_disputes:
                meeting_state.mapping_disputes[wu] = ts
                merged += 1
    if merged:
        print(f"[mapping] Restored {merged} DISPUTED freeze(s) from BigQuery for {today}")
    return merged


# ══════════════════════════════════════════════════════════════════════════
# Position rehydration after a restart (mapping fix B)
# ══════════════════════════════════════════════════════════════════════════
# "Who is in which room right now" lives in process memory and is filled only
# when someone MOVES. Every deploy/restart wiped it, so the Room Mapper panel
# had nothing to cross-match until people moved again — observed live on
# 2026-09-30 (Pub/Sub switch-on restart at ~13:20 IST left 40 rooms unnamed).
# On the first sync after a restart, positions are rebuilt from today's
# breakout events in BigQuery, stamped with their REAL event time, so the
# sync's own stability / freshness / consensus rules decide what counts.


def load_recent_positions(get_client, cfg, events_table):
    """Latest breakout event per person for the current IST business day.
    Returns [{keys, room_uuid, ts_epoch, meeting_uuid}] for people whose
    latest event is a JOIN (a leave means: not in any breakout room)."""
    client = get_client()
    rows = client.query(f"""
        SELECT participant_name, participant_email, event_type,
               room_uuid, meeting_uuid,
               UNIX_SECONDS(event_timestamp) AS ts_epoch
        FROM `{cfg['project']}.{cfg['dataset']}.{events_table}`
        WHERE event_date = '{_ist_today()}'
          AND event_type IN ('breakout_room_joined', 'breakout_room_left')
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY COALESCE(NULLIF(LOWER(TRIM(participant_email)), ''),
                                LOWER(TRIM(participant_name)))
          ORDER BY event_timestamp DESC) = 1
    """).result()
    out = []
    for r in rows:
        if r.event_type != 'breakout_room_joined' or not r.room_uuid:
            continue
        em = (r.participant_email or '').strip().lower()
        nm = norm_pname(r.participant_name)
        keys = ([f'e:{em}'] if em else []) + ([f'n:{nm}'] if nm else [])
        if keys:
            out.append({'keys': keys, 'room_uuid': r.room_uuid,
                        'ts_epoch': float(r.ts_epoch or 0),
                        'meeting_uuid': r.meeting_uuid or ''})
    return out


_pos_state = {'done': False}


def hydrate_positions(get_client, cfg, events_table, meeting_state):
    """Once per process: restore webhook positions lost to the restart.
    Memory always wins — only keys with no live entry are filled. Never
    raises; a transient error retries on the next sync."""
    with _state['lock']:
        if _pos_state['done']:
            return 0
        _pos_state['done'] = True
    try:
        positions = load_recent_positions(get_client, cfg, events_table)
    except Exception as e:
        with _state['lock']:
            _pos_state['done'] = False   # retry next sync
        print(f"[mapping] position rehydration skipped: {e}")
        return 0
    restored = 0
    with meeting_state._lock:
        for p in positions:
            if any(k in meeting_state.participant_current_breakout for k in p['keys']):
                continue   # live webhook data is fresher than our snapshot
            for k in p['keys']:
                meeting_state.participant_current_breakout[k] = {
                    'room_uuid': p['room_uuid'], 'ts': p['ts_epoch'],
                    'meeting_uuid': p['meeting_uuid'],
                }
            if p['meeting_uuid'] and not meeting_state.last_breakout_instance_uuid:
                meeting_state.last_breakout_instance_uuid = p['meeting_uuid']
            restored += 1
    if restored:
        print(f"[mapping] Restored {restored} participant position(s) from "
              f"BigQuery after restart")
    return restored


def count_disputes_today(get_client, cfg):
    """DISTINCT frozen rooms today, or None when unavailable (no table yet)."""
    try:
        client = get_client()
        rows = client.query(f"""
            SELECT COUNT(DISTINCT room_uuid) AS n
            FROM `{_table_id(cfg)}`
            WHERE dispute_date = '{_ist_today()}'
        """).result()
        return int(list(rows)[0].n)
    except Exception:
        return None


def reset_for_tests():
    _state['ensured'] = False
    _state['hydrated_date'] = None
    _pos_state['done'] = False
