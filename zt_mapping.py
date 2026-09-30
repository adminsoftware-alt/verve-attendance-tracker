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
