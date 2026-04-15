"""
CHATBOT — natural-language routing for the attendance tracker.
Uses Gemini 1.5 Flash to classify a user's prompt into an intent + parameters,
then dispatches to handler functions that reuse existing endpoints.

Intents fall into three families:
  - read    → fetch & summarise data (anyone)
  - export  → return a download URL for a CSV (anyone)
  - edit    → write to BigQuery overrides / leave (admin only, two-step confirm)

Edit intents return {confirm_required: True, summary, token} on first call.
The frontend re-POSTs with {confirm_token: token} to apply.
Tokens are HMAC-signed JSON with a 5-minute TTL — no in-memory state, so this
works behind multi-instance Cloud Run.
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
from datetime import datetime, timedelta
from calendar import monthrange

# Gemini SDK is optional — if the key is missing we fall back to a regex-only
# intent classifier so the chatbot still does something useful.
try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except Exception:
    genai = None
    _GEMINI_AVAILABLE = False

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash').strip()
CHAT_SECRET = os.environ.get('CHAT_SECRET', 'change-me-please').strip()

if _GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"[Chatbot] Gemini configure failed: {e}")
        _GEMINI_AVAILABLE = False


# ════════════════════════════════════════════════════════════
# HMAC confirm tokens (5-min TTL)
# ════════════════════════════════════════════════════════════

def make_confirm_token(payload):
    payload = dict(payload)
    payload['exp'] = int(time.time()) + 300  # 5 min
    body = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    sig = hmac.new(CHAT_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(body).decode().rstrip('=') + '.' + sig


def verify_confirm_token(token):
    try:
        body_b64, sig = token.rsplit('.', 1)
        # restore padding
        padded = body_b64 + '=' * (-len(body_b64) % 4)
        body = base64.urlsafe_b64decode(padded)
        expected = hmac.new(CHAT_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(body)
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# Intent classification (Gemini → JSON)
# ════════════════════════════════════════════════════════════

INTENT_SPEC = """
You are an attendance assistant. Classify the user's request into ONE intent
and extract its parameters. Reply ONLY with a JSON object — no markdown,
no commentary.

Available intents:
- lookup_employee:        Show one employee's monthly attendance.
                          params: {name, year, month}
- attendance_for_date:    Show full attendance for a date.
                          params: {date}
- team_summary:           Show team monthly summary.
                          params: {team_name, year, month}
- export_employee:        Download one employee's monthly attendance as CSV.
                          params: {name, year, month}
- export_team_monthly:    Download team monthly report as CSV.
                          params: {team_name, year, month}
- export_unrecognized:    Download list of unrecognised participants for a month.
                          params: {year, month}
- set_status:             (admin) Override an employee's status on a date.
                          params: {name, date, status}  status ∈ present|half_day|absent|leave
- set_active_mins:        (admin) Override active minutes for an employee on a date.
                          params: {name, date, minutes}
- add_leave:              (admin) Add a leave record for an employee.
                          params: {name, date, leave_type}  leave_type ∈ leave|sick|personal|wfh
- unknown:                Anything else / can't tell.

Rules:
- If a year/month is not given, OMIT them (handler defaults to current month).
- Dates must be YYYY-MM-DD. Convert "today", "yesterday", "april 12" using
  TODAY = {today}.
- Names are free-text; pass them as the user typed.
- confidence is a float 0.0–1.0; below 0.5 the chatbot will ask the user to
  rephrase.

Reply schema:
{"intent": "...", "params": {...}, "confidence": 0.0}

User prompt:
"""


def classify_intent_with_gemini(prompt, today_iso):
    """Call Gemini and return {intent, params, confidence}. Falls back to a
    regex-only classifier if Gemini is unavailable or the response can't be
    parsed."""
    if _GEMINI_AVAILABLE and GEMINI_API_KEY:
        try:
            full_prompt = INTENT_SPEC.replace('{today}', today_iso) + prompt
            model = genai.GenerativeModel(GEMINI_MODEL)
            resp = model.generate_content(
                full_prompt,
                generation_config={
                    'temperature': 0.0,
                    'max_output_tokens': 256,
                    'response_mime_type': 'application/json',
                },
            )
            text = (resp.text or '').strip()
            data = json.loads(text)
            if isinstance(data, dict) and 'intent' in data:
                data.setdefault('params', {})
                data.setdefault('confidence', 0.7)
                return data
        except Exception as e:
            print(f"[Chatbot] Gemini classification failed: {e} — falling back to regex")
    return _regex_fallback(prompt, today_iso)


def _regex_fallback(prompt, today_iso):
    """Tiny regex-based intent guesser for when Gemini is offline."""
    p = prompt.lower().strip()
    today = datetime.strptime(today_iso, '%Y-%m-%d')

    def _resolve_month(text):
        names = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec']
        for i, n in enumerate(names, start=1):
            if n in text:
                return today.year, i
        return today.year, today.month

    if 'unrecogni' in p or 'unrecognized' in p or 'unknown people' in p:
        y, m = _resolve_month(p)
        return {'intent': 'export_unrecognized', 'params': {'year': y, 'month': m}, 'confidence': 0.6}

    if 'download' in p or 'export' in p or 'csv' in p:
        # heuristic: if "team" is mentioned → team monthly; else employee
        if 'team' in p:
            y, m = _resolve_month(p)
            # crude team-name extraction: text after "team"
            mo = re.search(r'team\s+([a-z][a-z\s]{1,30})', p)
            team = mo.group(1).strip() if mo else ''
            return {'intent': 'export_team_monthly', 'params': {'team_name': team, 'year': y, 'month': m}, 'confidence': 0.5}
        y, m = _resolve_month(p)
        # crude name extraction: text after "for" or first capitalised word
        mo = re.search(r"(?:for|of)\s+([a-z][a-z\s]{1,30})", p)
        name = mo.group(1).strip() if mo else ''
        return {'intent': 'export_employee', 'params': {'name': name, 'year': y, 'month': m}, 'confidence': 0.4}

    if 'check' in p or 'show' in p or 'attendance' in p:
        y, m = _resolve_month(p)
        mo = re.search(r"(?:check|show|of)\s+([a-z][a-z\s]{1,30})", p)
        name = mo.group(1).strip() if mo else ''
        if name:
            return {'intent': 'lookup_employee', 'params': {'name': name, 'year': y, 'month': m}, 'confidence': 0.5}

    return {'intent': 'unknown', 'params': {}, 'confidence': 0.0}


# ════════════════════════════════════════════════════════════
# Helpers (employee / team lookup, month parsing)
# ════════════════════════════════════════════════════════════

def _today_ist():
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d')


def _now_ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _yymm_from_params(params):
    now = _now_ist()
    year = int(params.get('year') or now.year)
    month = int(params.get('month') or now.month)
    return year, month


def _normalize(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _find_employee(client, dataset_ref, name_query):
    """Fuzzy lookup: best-match employee_id by name. Returns dict or None."""
    if not name_query:
        return None
    q = f"""
    SELECT employee_id, participant_name, display_name, participant_email,
           team_id, category
    FROM `{dataset_ref}.employee_registry`
    WHERE status IS NULL OR status = '' OR status = 'active'
    """
    rows = list(client.query(q).result())
    if not rows:
        return None
    target = _normalize(name_query)
    target_words = set([w for w in re.split(r'\s+', name_query.lower()) if len(w) >= 3])

    scored = []
    for r in rows:
        cands = [r.participant_name or '', r.display_name or '']
        best_score = 0
        for c in cands:
            n = _normalize(c)
            if not n:
                continue
            if target == n:
                best_score = max(best_score, 100)
            elif target in n or n in target:
                best_score = max(best_score, 80)
            else:
                # word overlap
                cwords = set([w for w in re.split(r'\s+', c.lower()) if len(w) >= 3])
                overlap = len(target_words & cwords)
                if overlap:
                    best_score = max(best_score, 50 + 10 * overlap)
        if best_score > 0:
            scored.append((best_score, r))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    top = scored[0][1]
    return {
        'employee_id': top.employee_id,
        'participant_name': top.participant_name,
        'display_name': top.display_name or top.participant_name,
        'participant_email': top.participant_email or '',
        'team_id': top.team_id or '',
        'category': top.category or 'employee',
    }


def _find_team(client, dataset_ref, team_query):
    if not team_query:
        return None
    q = f"SELECT team_id, team_name, manager_name FROM `{dataset_ref}.teams`"
    rows = list(client.query(q).result())
    target = _normalize(team_query)
    for r in rows:
        if _normalize(r.team_name) == target:
            return {'team_id': r.team_id, 'team_name': r.team_name}
    for r in rows:
        if target in _normalize(r.team_name):
            return {'team_id': r.team_id, 'team_name': r.team_name}
    return None


def _fmt_mins(m):
    if not m:
        return '0m'
    m = int(m)
    if m >= 60:
        return f"{m // 60}h {m % 60}m"
    return f"{m}m"


# ════════════════════════════════════════════════════════════
# Intent handlers
# Each handler signature: (params, ctx) -> dict (response)
# ctx = {client, dataset_ref, project_id, base_url, user, role, confirm_payload}
# Response shape:
#   {message: str, data?: any, download_url?: str, filename?: str,
#    confirm_required?: bool, confirm_token?: str, confirm_summary?: str}
# ════════════════════════════════════════════════════════════

def _http_get_json(url, timeout=20):
    """Internal helper: fetch JSON from one of our own endpoints."""
    import requests as _r
    try:
        r = _r.get(url, timeout=timeout)
        if r.status_code >= 300:
            return None
        return r.json()
    except Exception as e:
        print(f"[Chatbot] _http_get_json {url} failed: {e}")
        return None


def h_lookup_employee(params, ctx):
    name_q = params.get('name', '')
    year, month = _yymm_from_params(params)
    emp = _find_employee(ctx['client'], ctx['dataset_ref'], name_q)
    if not emp:
        return {'message': f"I couldn't find an employee matching “{name_q}”."}

    yymm = f"{year}-{month:02d}"
    detail_url = f"{ctx['base_url']}/employees/{emp['employee_id']}/attendance/{yymm}"
    payload = _http_get_json(detail_url) or {}
    daily = payload.get('daily') or []

    # Aggregate
    days_present = sum(1 for d in daily if (d.get('active_minutes') or 0) > 0)
    total_active = sum(d.get('active_minutes') or 0 for d in daily)
    total_break = sum(d.get('break_minutes') or 0 for d in daily)
    total_iso = sum(d.get('isolation_minutes') or 0 for d in daily)
    avg = (total_active // days_present) if days_present else 0

    msg = (
        f"**{emp['display_name']}** — {yymm}\n"
        f"• Days present: **{days_present}**\n"
        f"• Total active: **{_fmt_mins(total_active)}**\n"
        f"• Avg / day: **{_fmt_mins(avg)}**\n"
        f"• Total break: **{_fmt_mins(total_break)}**\n"
        f"• Total isolation: **{_fmt_mins(total_iso)}**"
    )
    if emp.get('participant_email'):
        msg += f"\n• Email: {emp['participant_email']}"

    table_rows = [
        [
            d.get('date') or '',
            d.get('status') or '',
            d.get('first_seen_ist') or '-',
            d.get('last_seen_ist') or '-',
            _fmt_mins(d.get('active_minutes') or 0),
            _fmt_mins(d.get('break_minutes') or 0),
        ]
        for d in daily[:31]
    ]
    table = {
        'title': 'Daily breakdown',
        'columns': ['Date', 'Status', 'First seen', 'Last seen', 'Active', 'Break'],
        'rows': table_rows,
    } if table_rows else None

    return {
        'message': msg,
        'data': {'employee_id': emp['employee_id'], 'name': emp['display_name'], 'email': emp['participant_email']},
        'table': table,
        'download_url': f"{detail_url}?format=csv",
        'filename': f"{emp['display_name'].replace(' ','_')}_{yymm}.csv",
    }


def h_attendance_for_date(params, ctx):
    date = params.get('date') or _today_ist()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return {'message': f"Date format should be YYYY-MM-DD (got “{date}”)."}

    detail_url = f"{ctx['base_url']}/attendance/summary/{date}"
    payload = _http_get_json(detail_url) or {}
    parts = payload.get('participants') or []
    if not parts:
        return {'message': f"No attendance records for **{date}**."}

    # Sort by total time desc, take top 25 for display
    parts_sorted = sorted(parts, key=lambda p: -(p.get('total_duration_mins') or 0))
    top = parts_sorted[:25]

    total_people = len(parts)
    total_mins = sum(p.get('total_duration_mins') or 0 for p in parts)
    msg = (
        f"Attendance on **{date}** — {total_people} people · "
        f"{_fmt_mins(total_mins)} total active time. "
        f"{'Top ' + str(len(top)) + ' shown:' if len(parts) > len(top) else ''}"
    )
    table = {
        'title': f"Participants on {date}",
        'columns': ['Name', 'First seen', 'Last seen', 'Total time'],
        'rows': [
            [p.get('name') or '', p.get('first_seen_ist') or '-', p.get('last_seen_ist') or '-',
             _fmt_mins(p.get('total_duration_mins') or 0)]
            for p in top
        ],
    }
    return {'message': msg, 'data': {'date': date}, 'table': table}


def h_team_summary(params, ctx):
    team_q = params.get('team_name', '')
    year, month = _yymm_from_params(params)
    team = _find_team(ctx['client'], ctx['dataset_ref'], team_q)
    if not team:
        return {'message': f"I couldn't find a team matching “{team_q}”."}

    detail_url = f"{ctx['base_url']}/teams/{team['team_id']}/report/monthly?year={year}&month={month}"
    csv_url = detail_url + '&format=csv'
    payload = _http_get_json(detail_url) or {}
    summary = payload.get('member_summary') or []

    if not summary:
        return {
            'message': f"No data for **{team['team_name']}** in {year}-{month:02d}.",
            'download_url': csv_url,
        }

    summary_sorted = sorted(summary, key=lambda m: -(m.get('total_active_mins') or 0))
    total_active = sum(m.get('total_active_mins') or 0 for m in summary)
    total_break = sum(m.get('total_break_mins') or 0 for m in summary)
    msg = (
        f"**{team['team_name']}** — {year}-{month:02d}\n"
        f"• Members: **{len(summary)}**\n"
        f"• Combined active: **{_fmt_mins(total_active)}**\n"
        f"• Combined break: **{_fmt_mins(total_break)}**"
    )
    table = {
        'title': 'Member summary',
        'columns': ['Name', 'Days', 'Total active', 'Avg / day', 'Break', 'Isolation'],
        'rows': [
            [
                m.get('name') or '',
                m.get('days_present') or 0,
                _fmt_mins(m.get('total_active_mins') or 0),
                _fmt_mins((m.get('total_active_mins') or 0) // (m.get('days_present') or 1)),
                _fmt_mins(m.get('total_break_mins') or 0),
                _fmt_mins(m.get('total_isolation_mins') or 0),
            ]
            for m in summary_sorted
        ],
    }
    return {
        'message': msg,
        'data': {'team_id': team['team_id'], 'team_name': team['team_name']},
        'table': table,
        'download_url': csv_url,
        'filename': f"team_{team['team_name'].replace(' ','_')}_{year}-{month:02d}.csv",
    }


def h_export_employee(params, ctx):
    emp = _find_employee(ctx['client'], ctx['dataset_ref'], params.get('name', ''))
    if not emp:
        return {'message': f"I couldn't find an employee matching “{params.get('name','')}”."}
    year, month = _yymm_from_params(params)
    yymm = f"{year}-{month:02d}"
    csv_url = f"{ctx['base_url']}/employees/{emp['employee_id']}/attendance/{yymm}?format=csv"
    return {
        'message': f"CSV ready for **{emp['display_name']}** ({yymm}).",
        'download_url': csv_url,
        'filename': f"{emp['display_name'].replace(' ','_')}_{yymm}.csv",
    }


def h_export_team_monthly(params, ctx):
    team = _find_team(ctx['client'], ctx['dataset_ref'], params.get('team_name', ''))
    if not team:
        return {'message': f"I couldn't find a team matching “{params.get('team_name','')}”."}
    year, month = _yymm_from_params(params)
    csv_url = f"{ctx['base_url']}/teams/{team['team_id']}/report/monthly?year={year}&month={month}&format=csv"
    return {
        'message': f"CSV ready for **{team['team_name']}** ({year}-{month:02d}).",
        'download_url': csv_url,
        'filename': f"team_{team['team_name'].replace(' ','_')}_{year}-{month:02d}.csv",
    }


def h_export_unrecognized(params, ctx):
    year, month = _yymm_from_params(params)
    json_url = f"{ctx['base_url']}/employees/unrecognized-monthly?year={year}&month={month}"
    return {
        'message': f"Unrecognised participants for {year}-{month:02d}. (JSON — open the Employees → Unrecognized tab to export to CSV.)",
        'download_url': json_url,
        'filename': f"unrecognized_{year}-{month:02d}.json",
    }


# ── EDIT intents (admin only, two-step confirm) ─────────────

_VALID_STATUS = {'present', 'half_day', 'absent', 'leave'}
_VALID_LEAVE_TYPE = {'leave', 'sick', 'personal', 'wfh'}


def _require_admin(ctx):
    role = (ctx.get('role') or '').lower()
    if role not in ('admin', 'superadmin'):
        return {'message': "Only an admin can change attendance — I can show you the data but not edit it."}
    return None


def h_set_status(params, ctx):
    deny = _require_admin(ctx)
    if deny: return deny
    name_q = params.get('name', '')
    date = params.get('date', '')
    status = (params.get('status') or '').lower().replace(' ', '_')
    if status == 'half day':
        status = 'half_day'
    if status not in _VALID_STATUS:
        return {'message': f"Status must be one of: present, half_day, absent, leave (got “{params.get('status')}”)."}
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date or ''):
        return {'message': f"Date must be YYYY-MM-DD (got “{date}”)."}
    emp = _find_employee(ctx['client'], ctx['dataset_ref'], name_q)
    if not emp:
        return {'message': f"I couldn't find an employee matching “{name_q}”."}

    # If this is the confirmed pass, apply
    if ctx.get('confirm_payload'):
        cp = ctx['confirm_payload']
        if cp.get('intent') != 'set_status':
            return {'message': 'Confirm token does not match this intent.'}
        return _apply_override(ctx, cp['employee_name'], cp['date'], {'status': cp['status']})

    # Otherwise, propose
    summary = f"Set **{emp['display_name']}** on **{date}** → status **{status}**."
    token = make_confirm_token({
        'intent': 'set_status',
        'employee_name': emp['participant_name'],
        'date': date,
        'status': status,
        'user': ctx.get('user'),
    })
    return {
        'message': summary + " Confirm to apply.",
        'confirm_required': True,
        'confirm_summary': summary,
        'confirm_token': token,
    }


def h_set_active_mins(params, ctx):
    deny = _require_admin(ctx)
    if deny: return deny
    name_q = params.get('name', '')
    date = params.get('date', '')
    try:
        mins = int(params.get('minutes'))
    except (TypeError, ValueError):
        return {'message': f"Minutes must be an integer (got “{params.get('minutes')}”)."}
    if mins < 0 or mins > 24 * 60:
        return {'message': f"Minutes out of range (0–1440): {mins}."}
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date or ''):
        return {'message': f"Date must be YYYY-MM-DD (got “{date}”)."}
    emp = _find_employee(ctx['client'], ctx['dataset_ref'], name_q)
    if not emp:
        return {'message': f"I couldn't find an employee matching “{name_q}”."}

    if ctx.get('confirm_payload'):
        cp = ctx['confirm_payload']
        if cp.get('intent') != 'set_active_mins':
            return {'message': 'Confirm token does not match this intent.'}
        return _apply_override(ctx, cp['employee_name'], cp['date'], {'active_mins': cp['minutes']})

    summary = f"Set **{emp['display_name']}** on **{date}** → active **{_fmt_mins(mins)}** ({mins} min)."
    token = make_confirm_token({
        'intent': 'set_active_mins',
        'employee_name': emp['participant_name'],
        'date': date,
        'minutes': mins,
        'user': ctx.get('user'),
    })
    return {
        'message': summary + " Confirm to apply.",
        'confirm_required': True,
        'confirm_summary': summary,
        'confirm_token': token,
    }


def h_add_leave(params, ctx):
    deny = _require_admin(ctx)
    if deny: return deny
    name_q = params.get('name', '')
    date = params.get('date', '')
    leave_type = (params.get('leave_type') or 'leave').lower()
    if leave_type not in _VALID_LEAVE_TYPE:
        return {'message': f"Leave type must be one of: leave, sick, personal, wfh (got “{leave_type}”)."}
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date or ''):
        return {'message': f"Date must be YYYY-MM-DD (got “{date}”)."}
    emp = _find_employee(ctx['client'], ctx['dataset_ref'], name_q)
    if not emp:
        return {'message': f"I couldn't find an employee matching “{name_q}”."}

    if ctx.get('confirm_payload'):
        cp = ctx['confirm_payload']
        if cp.get('intent') != 'add_leave':
            return {'message': 'Confirm token does not match this intent.'}
        return _apply_leave(ctx, cp['employee_id'], cp['date'], cp['leave_type'])

    summary = f"Add **{leave_type}** for **{emp['display_name']}** on **{date}**."
    token = make_confirm_token({
        'intent': 'add_leave',
        'employee_id': emp['employee_id'],
        'employee_name': emp['participant_name'],
        'date': date,
        'leave_type': leave_type,
        'user': ctx.get('user'),
    })
    return {
        'message': summary + " Confirm to apply.",
        'confirm_required': True,
        'confirm_summary': summary,
        'confirm_token': token,
    }


# ── Apply helpers (called only after a verified confirm token) ──

def _apply_override(ctx, employee_name, date, fields):
    """Wrap the existing /attendance/override write logic by directly POSTing
    to the same Flask app."""
    import requests as _r
    url = f"{ctx['base_url']}/attendance/override"
    body = {
        'employee_name': employee_name,
        'event_date': date,
        'created_by': f"chatbot:{ctx.get('user') or 'unknown'}",
    }
    body.update(fields)
    try:
        r = _r.post(url, json=body, timeout=20)
        if r.status_code >= 300:
            return {'message': f"Override failed: {r.status_code} {r.text[:200]}"}
        return {'message': f"✅ Saved override for **{employee_name}** on **{date}**."}
    except Exception as e:
        return {'message': f"Override request error: {e}"}


def _apply_leave(ctx, employee_id, date, leave_type):
    import requests as _r
    url = f"{ctx['base_url']}/employees/{employee_id}/leave"
    try:
        r = _r.post(url, json={'date': date, 'leave_type': leave_type}, timeout=20)
        if r.status_code >= 300:
            return {'message': f"Leave creation failed: {r.status_code} {r.text[:200]}"}
        return {'message': f"✅ Added **{leave_type}** leave on **{date}**."}
    except Exception as e:
        return {'message': f"Leave request error: {e}"}


# ════════════════════════════════════════════════════════════
# Registry + dispatcher
# ════════════════════════════════════════════════════════════

def h_general_chat(params, ctx):
    """Conversational fallback. Sends the prompt to Gemini with a system
    primer about what the app does + capabilities, and returns the reply
    inline. Used when no structured intent matches."""
    prompt = (params.get('prompt') or '').strip()
    history = params.get('history') or []
    if not prompt:
        return {'message': "Ask me anything about attendance, teams, exports, or overrides."}

    # If Gemini is unavailable, return a fixed help message.
    if not (_GEMINI_AVAILABLE and GEMINI_API_KEY):
        return {
            'message': (
                "I can answer attendance questions when Gemini is configured. "
                "Right now I only handle direct commands — try:\n"
                "• show Shashank attendance\n"
                "• team Accurest April summary\n"
                "• download Shashank April CSV\n"
                "• mark Shashank present on 2026-04-12 (admin)"
            )
        }

    primer = (
        "You are the AI assistant inside Verve Advisory's attendance tracker. "
        "You help HR / managers / admins explore Zoom-based attendance data: "
        "rooms occupied, time tracked, breaks, isolation, leaves, holidays. "
        "Be concise (max 4 sentences). If the user asks for actual employee "
        "or team numbers, tell them to phrase it as one of: "
        "'show <name> attendance', 'team <team> summary', 'attendance for <date>', "
        "'download <name> April', or 'mark <name> present on <date>'. "
        "Answer general operational, conceptual, or how-to questions directly. "
        f"Today is {_today_ist()} (IST)."
    )

    try:
        model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=primer)
        # Build short history (last 6) as Gemini chat turns
        history_turns = []
        for h in (history or [])[-6:]:
            role = 'user' if h.get('role') == 'user' else 'model'
            txt = (h.get('message') or '').strip()
            if txt:
                history_turns.append({'role': role, 'parts': [txt]})
        chat = model.start_chat(history=history_turns)
        resp = chat.send_message(prompt, generation_config={
            'temperature': 0.4,
            'max_output_tokens': 512,
        })
        text = (resp.text or '').strip() or "I'm not sure how to answer that."
        return {'message': text}
    except Exception as e:
        print(f"[Chatbot] general_chat failed: {e}")
        return {'message': f"Sorry, I couldn't answer that just now ({e})."}


INTENTS = {
    # Read (data-rich)
    'lookup_employee':     {'role': None,    'requires_confirm': False, 'handler': h_lookup_employee},
    'attendance_for_date': {'role': None,    'requires_confirm': False, 'handler': h_attendance_for_date},
    'team_summary':        {'role': None,    'requires_confirm': False, 'handler': h_team_summary},
    # Export
    'export_employee':       {'role': None,  'requires_confirm': False, 'handler': h_export_employee},
    'export_team_monthly':   {'role': None,  'requires_confirm': False, 'handler': h_export_team_monthly},
    'export_unrecognized':   {'role': None,  'requires_confirm': False, 'handler': h_export_unrecognized},
    # Edit
    'set_status':       {'role': 'admin', 'requires_confirm': True, 'handler': h_set_status},
    'set_active_mins':  {'role': 'admin', 'requires_confirm': True, 'handler': h_set_active_mins},
    'add_leave':        {'role': 'admin', 'requires_confirm': True, 'handler': h_add_leave},
    # Conversational fallback
    'general_chat':     {'role': None,    'requires_confirm': False, 'handler': h_general_chat},
}


def dispatch(prompt, ctx, confirm_token=None, history=None):
    """Main entry. Returns dict response; never raises.
    `history` is the recent conversation (list of {role,message}) used as
    context for the general-chat fallback."""
    try:
        # Confirm path: token tells us the intent + params, skip LLM
        if confirm_token:
            payload = verify_confirm_token(confirm_token)
            if not payload:
                return {'success': False, 'message': 'Confirm token is invalid or expired. Please retry your request.'}
            intent = payload.get('intent')
            spec = INTENTS.get(intent)
            if not spec:
                return {'success': False, 'message': f"Unknown intent in token: {intent}"}
            ctx['confirm_payload'] = payload
            result = spec['handler'](payload, ctx)
            return {'success': True, 'intent': intent, **result}

        # Fresh prompt path
        clf = classify_intent_with_gemini(prompt, _today_ist())
        intent = clf.get('intent', 'unknown')
        confidence = float(clf.get('confidence', 0.0))
        params = clf.get('params', {}) or {}

        # Low confidence OR explicitly unknown → conversational fallback so
        # the user always gets a helpful answer instead of a canned hint.
        if intent == 'unknown' or confidence < 0.4 or intent not in INTENTS:
            params = {'prompt': prompt, 'history': history or []}
            result = INTENTS['general_chat']['handler'](params, ctx)
            return {'success': True, 'intent': 'general_chat', 'confidence': confidence, **result}

        spec = INTENTS[intent]
        result = spec['handler'](params, ctx)
        return {'success': True, 'intent': intent, 'confidence': confidence, **result}

    except Exception as e:
        import traceback; traceback.print_exc()
        return {'success': False, 'message': f"Chatbot error: {e}"}
