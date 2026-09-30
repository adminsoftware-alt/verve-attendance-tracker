"""Observability + data-quality layer (review points 8 and 10).

- log_json(): structured JSON log lines. Cloud Logging parses a JSON line
  with a "severity" field into a filterable, alertable entry — unlike the
  bare print()s this codebase grew up on. New code paths use this; old
  prints migrate gradually.
- record_webhook()/webhook_health(): in-memory liveness for /health/webhook
  ("when did Zoom last talk to us?") with no BigQuery cost.
- bigquery_health(): can we reach BigQuery right now (cached 60s).
- data_quality_summary(): the numbers for the System Health view — events
  today, duplicate groups, unknown-room %, last build, disputed rooms,
  latest health_checks rows (docs/05*) if that watchdog is installed.
  Every metric is isolated: one failing query reports itself as an error
  string instead of taking the whole summary down. Read-only throughout.
- DASHBOARD_HTML: the /health/dashboard page (server-served, no React
  build step involved, so it cannot break the frontend deploy).
"""
import json
import threading
import time
from datetime import datetime, timedelta

__all__ = [
    'log_json',
    'record_webhook',
    'webhook_health',
    'bigquery_health',
    'data_quality_summary',
    'DASHBOARD_HTML',
]

IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_today():
    return (datetime.utcnow() + IST_OFFSET).strftime('%Y-%m-%d')


def log_json(severity, message, **fields):
    """One structured log line. severity: DEBUG/INFO/WARNING/ERROR."""
    entry = {'severity': severity, 'message': message}
    entry.update(fields)
    try:
        print(json.dumps(entry, default=str))
    except Exception:
        print(f"[{severity}] {message} {fields}")


# ── webhook liveness (memory only — survives nothing, costs nothing) ──────
_lock = threading.Lock()
_webhook = {'last_ts': None, 'last_event': None, 'count_date': None, 'count': 0}

# No webhook for 30 min during the workday = the pipe is broken.
WEBHOOK_STALE_S = 1800


def record_webhook(event):
    with _lock:
        _webhook['last_ts'] = time.time()
        _webhook['last_event'] = event
        today = _ist_today()
        if _webhook['count_date'] != today:
            _webhook['count_date'] = today
            _webhook['count'] = 0
        _webhook['count'] += 1


def webhook_health():
    with _lock:
        last_ts = _webhook['last_ts']
        snap = dict(_webhook)
    age = (time.time() - last_ts) if last_ts else None
    if age is None:
        status = 'NO_DATA'      # nothing since this instance started
    elif age > WEBHOOK_STALE_S:
        status = 'STALE'
    else:
        status = 'HEALTHY'
    return {
        'status': status,
        'seconds_since_last_event': round(age, 1) if age is not None else None,
        'last_event_type': snap['last_event'],
        'events_since_instance_start_today': snap['count'] if snap['count_date'] == _ist_today() else 0,
        'note': 'in-memory: resets on restart; BigQuery numbers are in /health/summary',
    }


# ── BigQuery reachability (cached: a health probe must not cost money) ────
_bq_probe = {'at': 0.0, 'result': None}
BQ_PROBE_TTL_S = 60


def bigquery_health(get_client):
    now = time.time()
    with _lock:
        if _bq_probe['result'] is not None and now - _bq_probe['at'] < BQ_PROBE_TTL_S:
            return dict(_bq_probe['result'], cached=True)
    try:
        t0 = time.time()
        list(get_client().query('SELECT 1').result())
        result = {'status': 'HEALTHY', 'query_ms': int((time.time() - t0) * 1000)}
    except Exception as e:
        result = {'status': 'DOWN', 'error': str(e)[:300]}
    with _lock:
        _bq_probe['at'] = now
        _bq_probe['result'] = result
    return dict(result, cached=False)


# ── data-quality summary ──────────────────────────────────────────────────
_summary_cache = {'at': 0.0, 'data': None}
SUMMARY_TTL_S = 60


def _one(client, sql):
    """First row of a query as a dict-like Row."""
    return list(client.query(sql).result())[0]


def data_quality_summary(get_client, project, dataset, events_table,
                         extras=None, ttl_s=SUMMARY_TTL_S):
    """All System Health numbers, one guarded query per metric, cached.
    extras: dict of extra ready-made sections merged in (e.g. pubsub stats)."""
    now = time.time()
    with _lock:
        if _summary_cache['data'] is not None and now - _summary_cache['at'] < ttl_s:
            data = dict(_summary_cache['data'])
            data['cached'] = True
            return data

    today = _ist_today()
    ev = f"`{project}.{dataset}.{events_table}`"
    pi = f"`{project}.{dataset}.presence_intervals`"
    out = {'business_date_ist': today, 'generated_at': datetime.utcnow().isoformat() + 'Z'}

    def guarded(key, fn):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {'error': str(e)[:300]}

    def _events():
        r = _one(get_client(), f"""
            SELECT COUNT(*) AS n, CAST(MAX(inserted_at) AS STRING) AS last_inserted_at,
                   COUNTIF(STARTS_WITH(event_id, 'e1-')) AS new_ids
            FROM {ev} WHERE event_date = '{today}'""")
        n, new_ids = int(r.n), int(r.new_ids)
        return {'events_today': n, 'last_inserted_at': r.last_inserted_at,
                'deterministic_id_pct': round(100.0 * new_ids / n, 1) if n else 0.0}
    guarded('webhook_events', _events)

    def _builder():
        # Shows WHICH hours engine is live (v15.1 install date) — makes the
        # otherwise invisible SQL upgrade checkable from the dashboard.
        r = _one(get_client(), f"""
            SELECT CAST(last_altered AS STRING) AS last_altered
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.ROUTINES`
            WHERE routine_name = 'sp_build_presence_intervals'""")
        return {'procedure': 'sp_build_presence_intervals',
                'last_updated': r.last_altered}
    guarded('hours_builder', _builder)

    def _dupes():
        # Same person + type + timestamp arriving more than once = the
        # duplicates the 60s in-memory cache missed (restart / redelivery).
        # NB: the alias must not be "groups" — reserved keyword in BigQuery
        # (found in production on 2026-09-30; the fake BQ in tests cannot
        # catch parser errors).
        r = _one(get_client(), f"""
            SELECT COUNT(*) AS dup_groups, COALESCE(SUM(c - 1), 0) AS extra_rows FROM (
              SELECT COUNT(*) AS c FROM {ev}
              WHERE event_date = '{today}'
              GROUP BY participant_id, event_type, event_timestamp, room_uuid
              HAVING COUNT(*) > 1)""")
        return {'duplicate_groups_today': int(r.dup_groups), 'extra_rows_today': int(r.extra_rows)}
    guarded('duplicates', _dupes)

    def _presence():
        r = _one(get_client(), f"""
            SELECT CAST(MAX(built_at) AS STRING) AS last_build,
                   ROUND(100 * SAFE_DIVIDE(
                     SUM(IF(room_name = 'Unknown Room' OR room_name LIKE 'Room-%', duration_seconds, 0)),
                     SUM(duration_seconds)), 1) AS unknown_room_pct
            FROM {pi} WHERE event_date = '{today}'""")
        return {'last_build_at': r.last_build,
                'unknown_room_time_pct_today': float(r.unknown_room_pct) if r.unknown_room_pct is not None else 0.0}
    guarded('presence_intervals', _presence)

    def _checks():
        rows = get_client().query(f"""
            SELECT check_id, check_name, severity, metric, detail
            FROM `{project}.{dataset}.v_health_latest`
            ORDER BY severity, check_id LIMIT 25""").result()
        return [{'check_id': r.check_id, 'check_name': r.check_name,
                 'severity': r.severity, 'metric': str(r.metric), 'detail': r.detail}
                for r in rows]
    guarded('health_checks', _checks)

    out['webhook_liveness'] = webhook_health()
    for k, v in (extras or {}).items():
        out[k] = v

    with _lock:
        _summary_cache['at'] = now
        _summary_cache['data'] = dict(out)
    out['cached'] = False
    return out


def reset_for_tests():
    with _lock:
        _webhook.update({'last_ts': None, 'last_event': None, 'count_date': None, 'count': 0})
        _bq_probe.update({'at': 0.0, 'result': None})
        _summary_cache.update({'at': 0.0, 'data': None})


# ── System Health page (served by Flask; independent of the React build) ──
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>System Health</title>
<style>
 :root{--bg:#f5f7fa;--card:#fff;--ink:#1d2330;--mut:#5a6475;--line:#dde3ec;
       --ok:#1f8a4c;--warn:#b8661d;--bad:#b23a3a;}
 @media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
   --bg:#12151c;--card:#1b2029;--ink:#e8ebf1;--mut:#9aa3b2;--line:#2a3140;}}
 body{margin:0;background:var(--bg);color:var(--ink);
      font:14px/1.5 "Segoe UI",system-ui,Arial,sans-serif;padding:16px;}
 h1{font-size:20px;margin:0 0 2px} .sub{color:var(--mut);margin:0 0 16px;font-size:12px}
 .grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;}
 .k{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
 .v{font-size:24px;font-weight:600;margin-top:2px}
 .s{font-size:12px;color:var(--mut);margin-top:2px;word-break:break-word}
 .ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
 th,td{text-align:left;border-top:1px solid var(--line);padding:6px 8px;vertical-align:top}
 th{color:var(--mut);font-weight:600;border-top:none}
 .card.wide{grid-column:1/-1}
</style></head><body>
<h1>System Health</h1>
<p class="sub" id="meta">loading…</p>
<div class="grid" id="tiles"></div>
<div class="card wide"><div class="k">Pipeline health checks (v_health_latest)</div>
  <div id="checks" class="s">loading…</div></div>
<script>
function tile(k,v,cls,s){return '<div class="card"><div class="k">'+k+'</div>'+
  '<div class="v '+(cls||'')+'">'+v+'</div><div class="s">'+(s||'')+'</div></div>';}
function esc(x){return String(x==null?'—':x).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));}
function num(x){return (x==null||x.error)?'—':x;}
async function load(){
 try{
  const r = await fetch('/health/summary'); const d = await r.json();
  document.getElementById('meta').textContent =
    'Business day (IST): '+d.business_date_ist+' · generated '+d.generated_at+(d.cached?' · cached':'');
  const t=[]; const wl=d.webhook_liveness||{};
  t.push(tile('Webhook feed', esc(wl.status),
    wl.status==='HEALTHY'?'ok':(wl.status==='STALE'?'bad':'warn'),
    wl.seconds_since_last_event!=null?('last event '+Math.round(wl.seconds_since_last_event)+'s ago'):'no events since restart'));
  const we=d.webhook_events||{};
  t.push(tile('Events today', esc(num(we.events_today)),'', 'last insert: '+esc(we.last_inserted_at)));
  const du=d.duplicates||{};
  t.push(tile('Duplicate groups', esc(num(du.duplicate_groups_today)),
    (du.duplicate_groups_today>0?'warn':'ok'), esc(num(du.extra_rows_today))+' extra rows'));
  const pi=d.presence_intervals||{};
  t.push(tile('Unknown-room time', esc(num(pi.unknown_room_time_pct_today))+'%',
    (pi.unknown_room_time_pct_today>10?'bad':(pi.unknown_room_time_pct_today>2?'warn':'ok')),
    'last build: '+esc(pi.last_build_at)));
  t.push(tile('Disputed rooms', esc(d.disputed_rooms_today==null?'—':d.disputed_rooms_today),
    (d.disputed_rooms_today>0?'warn':'ok'),'frozen by anti-ping-pong today'));
  const ps=d.pubsub||{};
  t.push(tile('Pub/Sub', ps.active?'ACTIVE':'inline', ps.active?'ok':'',
    ps.active?('published '+esc(ps.published)+' · processed '+esc(ps.push_processed)):'events processed in webhook request'));
  const hb=d.hours_builder||{};
  t.push(tile('Pipeline', esc(num(we.deterministic_id_pct))+'% new IDs',
    (we.deterministic_id_pct>=99?'ok':''),
    'dedup-safe event ids today · hours builder updated: '+esc((hb.last_updated||'—').slice(0,16))));
  document.getElementById('tiles').innerHTML=t.join('');
  const hc=d.health_checks;
  const el=document.getElementById('checks');
  if(Array.isArray(hc)&&hc.length){
    el.innerHTML='<table><tr><th>ID</th><th>Check</th><th>Severity</th><th>Metric</th><th>Detail</th></tr>'+
     hc.map(c=>'<tr><td>'+esc(c.check_id)+'</td><td>'+esc(c.check_name)+'</td><td class="'+
       (String(c.severity).toUpperCase().includes('ALARM')?'bad':(String(c.severity).toUpperCase().includes('WARN')?'warn':'ok'))+
       '">'+esc(c.severity)+'</td><td>'+esc(c.metric)+'</td><td>'+esc(c.detail)+'</td></tr>').join('')+'</table>';
  } else if(Array.isArray(hc)){ el.textContent='No check rows yet — run sp_health_live / sp_health_daily (docs/05a-c).';
  } else { el.textContent='health_checks watchdog not installed (docs/05a_setup.sql): '+esc(hc&&hc.error); }
 }catch(e){ document.getElementById('meta').textContent='Failed to load /health/summary: '+e; }
}
load(); setInterval(load, 60000);
</script></body></html>
"""
