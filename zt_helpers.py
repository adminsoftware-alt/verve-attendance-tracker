"""Shared pure helpers: IST time, participant-name normalization,
participant merge/collapse, safe casts. No Flask/BQ dependencies."""
import re as _re
from datetime import datetime, timedelta

__all__ = [
    'IST_OFFSET',
    'get_ist_now',
    'get_ist_date',
    'utc_to_ist',
    'normalize_participant_name',
    'collapse_by_email',
    'merge_participants_by_name',
    'safe_int',
    'safe_str',
]

IST_OFFSET = timedelta(hours=5, minutes=30)


def get_ist_now():
    """Get current datetime in IST"""
    return datetime.utcnow() + IST_OFFSET


def get_ist_date():
    """Get current date in IST (YYYY-MM-DD)"""
    return get_ist_now().strftime('%Y-%m-%d')


def utc_to_ist(utc_dt):
    """Convert UTC datetime to IST datetime"""
    if utc_dt is None:
        return None
    return utc_dt + IST_OFFSET


def normalize_participant_name(name):
    """Strip Zoom rejoin suffixes to get the base participant name.
    'Aastha Chandwani-2' -> 'Aastha Chandwani'
    'Geo Prithvipal-1' -> 'Geo Prithvipal'
    'Yashasvi Dhakate_accurest' -> 'Yashasvi Dhakate'
    'CS Shweta Tulsani-KPRC' -> 'CS Shweta Tulsani'
    'Gayatri Dabi - KPRC' -> 'Gayatri Dabi'
    'Ronit 2' -> 'Ronit'
    Preserves legitimate hyphenated surnames:
    'Priya Sharma-Gupta' -> 'Priya Sharma-Gupta' (kept)
    """
    if not name:
        return name
    n = name.strip()
    # Remove trailing " - TEXT" (space dash space suffix, always organizational)
    n = _re.sub(r'\s+-\s+\w+$', '', n)
    # Remove trailing "-N" (number suffix like -1, -2, -5)
    n = _re.sub(r'-\d+$', '', n)
    # Remove trailing "_text" (underscore suffix like _accurest, _KPRC)
    n = _re.sub(r'_\w+$', '', n)
    # Remove trailing "-TEXT" ONLY if the suffix is ALL-CAPS (2+ chars, like -KPRC)
    # Preserves legitimate hyphenated surnames like Sharma-Gupta, Mary-Jane.
    # Mixed-case org tags (e.g. -Meeting, -Vridam) are handled by _strip_team_and_clean().
    n = _re.sub(r'-[A-Z]{2,}$', '', n)
    # Remove trailing " N" where N is a single digit (like "Ronit 2")
    n = _re.sub(r'\s+\d$', '', n)
    return n.strip()


def collapse_by_email(participants, mode='summary'):
    """Second-pass merge: if two records (already collapsed by normalized
    name) share the same non-empty email, merge them too. Handles the
    "Shashank Channawar" -> "Shashank C" rename where the names don't
    normalize to the same value but the email is identical.
    """
    groups = {}  # lower(email) -> primary record
    out = []
    for p in participants:
        email = (p.get('email') or p.get('participant_email') or '').strip().lower()
        if not email:
            out.append(p)
            continue
        primary = groups.get(email)
        if primary is None:
            groups[email] = p
            out.append(p)
            continue
        # Merge p into primary
        for email_key in ('email', 'participant_email'):
            if p.get(email_key) and not primary.get(email_key):
                primary[email_key] = p[email_key]
        if mode == 'summary':
            primary_visits = primary.get('room_visits', []) or []
            new_visits = p.get('room_visits', []) or []
            primary['room_visits'] = sorted(
                primary_visits + new_visits,
                key=lambda v: v.get('room_joined_ist', '') or ''
            )
            for tk in ('first_seen_ist',):
                if p.get(tk) and (not primary.get(tk) or p[tk] < primary[tk]):
                    primary[tk] = p[tk]
            for tk in ('last_seen_ist',):
                if p.get(tk) and (not primary.get(tk) or p[tk] > primary[tk]):
                    primary[tk] = p[tk]
            primary['total_duration_mins'] = (primary.get('total_duration_mins', 0) or 0) \
                                              + (p.get('total_duration_mins', 0) or 0)
        elif mode == 'team':
            for tk in ('first_seen_ist',):
                if p.get(tk) and (not primary.get(tk) or p[tk] < primary[tk]):
                    primary[tk] = p[tk]
            for tk in ('last_seen_ist',):
                if p.get(tk) and (not primary.get(tk) or p[tk] > primary[tk]):
                    primary[tk] = p[tk]
            for nk in ('total_duration_mins', 'breakout_mins', 'main_room_mins',
                       'break_minutes', 'isolation_minutes'):
                if nk in p:
                    primary[nk] = (primary.get(nk, 0) or 0) + (p.get(nk) or 0)
            status_rank = {'present': 3, 'half_day': 2, 'absent': 1}
            if status_rank.get(p.get('status'), 0) > status_rank.get(primary.get('status'), 0):
                primary['status'] = p['status']
    # Strip duplicates that were merged in place
    seen_ids = set()
    deduped = []
    for p in out:
        pid = id(p)
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        deduped.append(p)
    return deduped


def merge_participants_by_name(participants, mode='summary'):
    """Merge duplicate participant entries by normalized name.

    For summary mode: merge room_visits, pick best email, earliest/latest times.
    For live mode: merge participant lists, pick best email.
    For team mode: merge durations, breaks, isolation.
    """
    merged = {}
    for p in participants:
        base_name = normalize_participant_name(p.get('name') or p.get('participant_name', ''))
        if not base_name:
            continue

        key = base_name.lower().strip()

        if key not in merged:
            merged[key] = {**p}
            # Store the cleanest name (the base name)
            if 'name' in merged[key]:
                merged[key]['name'] = base_name
            if 'participant_name' in merged[key]:
                merged[key]['participant_name'] = base_name
            continue

        existing = merged[key]

        # Pick best email (non-empty)
        for email_key in ['email', 'participant_email']:
            if email_key in p and p[email_key] and not existing.get(email_key):
                existing[email_key] = p[email_key]

        if mode == 'summary':
            # Merge room visits
            existing_visits = existing.get('room_visits', [])
            new_visits = p.get('room_visits', [])
            existing['room_visits'] = sorted(
                existing_visits + new_visits,
                key=lambda v: v.get('room_joined_ist', '') or ''
            )
            # Earliest first_seen, latest last_seen
            for time_key in ['first_seen_ist']:
                if p.get(time_key) and (not existing.get(time_key) or p[time_key] < existing[time_key]):
                    existing[time_key] = p[time_key]
            for time_key in ['last_seen_ist']:
                if p.get(time_key) and (not existing.get(time_key) or p[time_key] > existing[time_key]):
                    existing[time_key] = p[time_key]
            # Sum duration
            existing['total_duration_mins'] = existing.get('total_duration_mins', 0) + p.get('total_duration_mins', 0)

        elif mode == 'team':
            # Earliest first_seen, latest last_seen
            for time_key in ['first_seen_ist']:
                if p.get(time_key) and (not existing.get(time_key) or p[time_key] < existing[time_key]):
                    existing[time_key] = p[time_key]
            for time_key in ['last_seen_ist']:
                if p.get(time_key) and (not existing.get(time_key) or p[time_key] > existing[time_key]):
                    existing[time_key] = p[time_key]
            # Sum numeric fields
            for num_key in ['total_duration_mins', 'breakout_mins', 'main_room_mins',
                            'break_minutes', 'isolation_minutes']:
                if num_key in p:
                    existing[num_key] = existing.get(num_key, 0) + (p.get(num_key) or 0)
            # Best status: present > half_day > absent
            status_rank = {'present': 3, 'half_day': 2, 'absent': 1}
            if status_rank.get(p.get('status'), 0) > status_rank.get(existing.get('status'), 0):
                existing['status'] = p['status']

        elif mode == 'live':
            # Merge participant lists (for live room view)
            pass  # Live mode handled separately at room level

    return list(merged.values())


def safe_int(value, default=0):
    """Safely convert value to int, handling None and empty strings"""
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_str(value, default=''):
    """Safely convert value to string, handling None"""
    if value is None:
        return default
    return str(value).strip() if value else default


