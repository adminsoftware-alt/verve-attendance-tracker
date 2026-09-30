"""Points 8 + 10 — health endpoints and the data-quality summary."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import app as app_module
import zt_observability
from helpers import FakeBQ, load_fixture


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


class FakeSummaryBQ:
    """Answers each summary query by recognising its SQL."""

    def __init__(self):
        self.sql_seen = []

    def query(self, sql, **kw):
        self.sql_seen.append(sql)
        if 'SELECT 1' in sql:
            rows = [SimpleNamespace(f0_=1)]
        elif 'v_health_latest' in sql:
            rows = [SimpleNamespace(check_id='03', check_name='Webhook ingestion alive',
                                    severity='OK', metric='512', detail='events flowing')]
        elif 'mapping_disputes' in sql:
            rows = [SimpleNamespace(n=1)]
        elif 'INFORMATION_SCHEMA.ROUTINES' in sql:
            rows = [SimpleNamespace(last_altered='2026-09-30 07:15:00')]
        elif "NOT LIKE 'Room-%'" in sql:   # today's known room names
            rows = [SimpleNamespace(room_name='BREAK TIME'), SimpleNamespace(room_name='Sales Team')]
        elif 'STRING_AGG' in sql:   # the unnamed-rooms listing
            rows = [SimpleNamespace(room_uuid='ROOM-UUID-1', people=8, minutes=1608,
                                    sample='Vishwa, Payal, Shivani', last_seen='14:03', live=True)]
        elif 'presence_intervals' in sql:
            rows = [SimpleNamespace(last_build='2026-09-29 10:00:00', unknown_room_pct=3.4)]
        elif 'HAVING COUNT(*) > 1' in sql:
            assert ' AS groups' not in sql, 'reserved BigQuery keyword as alias'
            rows = [SimpleNamespace(dup_groups=2, extra_rows=3)]
        else:  # events count
            rows = [SimpleNamespace(n=8421, last_inserted_at='2026-09-29 10:29:48',
                                    new_ids=8421)]
        return mock.MagicMock(result=lambda *a, **k: rows)


# ── /health/webhook ───────────────────────────────────────────────────────

def test_webhook_health_no_data_then_healthy(client, monkeypatch):
    r = client.get('/health/webhook')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'NO_DATA'

    fake = FakeBQ()
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    data = load_fixture('participant_joined')
    app_module._dispatch_webhook_event(data['event'], data)

    r = client.get('/health/webhook')
    body = r.get_json()
    assert body['status'] == 'HEALTHY'
    assert body['seconds_since_last_event'] < 5
    assert body['last_event_type'] == 'meeting.participant_joined'


def test_webhook_health_stale_returns_503(client, monkeypatch):
    monkeypatch.setattr(zt_observability, '_ist_hour', lambda: 14)   # 2 PM IST
    zt_observability.record_webhook('meeting.participant_joined')
    with zt_observability._lock:
        zt_observability._webhook['last_ts'] -= 3600   # an hour ago
    r = client.get('/health/webhook')
    assert r.status_code == 503
    assert r.get_json()['status'] == 'STALE'


def test_webhook_health_quiet_at_night_is_not_an_alarm(client, monkeypatch):
    """The uptime alert must not page anyone at 2 AM for a quiet feed."""
    monkeypatch.setattr(zt_observability, '_ist_hour', lambda: 2)
    zt_observability.record_webhook('meeting.participant_joined')
    with zt_observability._lock:
        zt_observability._webhook['last_ts'] -= 3600
    r = client.get('/health/webhook')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'QUIET_HOURS'


# ── /health/bigquery ──────────────────────────────────────────────────────

def test_bigquery_health_up_and_cached(client, monkeypatch):
    fake = FakeSummaryBQ()
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    assert client.get('/health/bigquery').status_code == 200
    assert client.get('/health/bigquery').get_json()['cached'] is True
    assert len([s for s in fake.sql_seen if 'SELECT 1' in s]) == 1


def test_bigquery_health_down_returns_503(client, monkeypatch):
    def boom():
        raise ConnectionError('no route to BigQuery')
    monkeypatch.setattr(app_module, 'get_bq_client', boom)
    r = client.get('/health/bigquery')
    assert r.status_code == 503
    assert r.get_json()['status'] == 'DOWN'


# ── /health/summary + dashboard ───────────────────────────────────────────

def test_summary_reports_all_sections(client, monkeypatch):
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: FakeSummaryBQ())
    body = client.get('/health/summary').get_json()
    assert body['webhook_events']['events_today'] == 8421
    assert body['webhook_events']['deterministic_id_pct'] == 100.0
    assert body['hours_builder']['last_updated'] == '2026-09-30 07:15:00'
    assert body['unnamed_rooms'] == [{'room_uuid': 'ROOM-UUID-1', 'people': 8,
                                      'minutes': 1608, 'who': 'Vishwa, Payal, Shivani',
                                      'last_seen': '14:03', 'live': True}]
    assert body['room_names_today'] == ['BREAK TIME', 'Sales Team']
    assert body['generated_at'].endswith('IST')
    assert body['duplicates'] == {'duplicate_groups_today': 2, 'extra_rows_today': 3}
    assert body['presence_intervals']['unknown_room_time_pct_today'] == 3.4
    assert body['disputed_rooms_today'] == 1
    assert body['health_checks'][0]['check_id'] == '03'
    assert 'pubsub' in body and 'webhook_liveness' in body
    assert body['cached'] is False


def test_summary_survives_bigquery_being_down(client, monkeypatch):
    """One broken metric must not take the page down: every section is
    guarded and reports its own error string."""
    def boom():
        raise ConnectionError('bq down')
    monkeypatch.setattr(app_module, 'get_bq_client', boom)
    r = client.get('/health/summary')
    assert r.status_code == 200
    body = r.get_json()
    assert 'error' in body['webhook_events']
    assert 'error' in body['presence_intervals']
    assert body['disputed_rooms_today'] is None
    assert body['webhook_liveness']['status'] in ('NO_DATA', 'HEALTHY')


def test_summary_is_cached(client, monkeypatch):
    fake = FakeSummaryBQ()
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    client.get('/health/summary')
    n = len(fake.sql_seen)
    assert client.get('/health/summary').get_json()['cached'] is True
    # only the (cheap, guarded) disputes extra re-runs; the summary body doesn't
    assert len([s for s in fake.sql_seen[n:] if 'participant_events' in s]) == 0


def test_dashboard_serves_html(client):
    r = client.get('/health/dashboard')
    assert r.status_code == 200
    assert r.mimetype == 'text/html'
    assert 'System Health' in r.get_data(as_text=True)


def test_dashboard_embeddable_by_frontend_only(client):
    """The attendance frontend may iframe /health/dashboard; other pages
    keep the Zoom-only frame-ancestors policy."""
    csp = client.get('/health/dashboard').headers['Content-Security-Policy']
    assert 'attendance-frontend-4e5na4tdha-uc.a.run.app' in csp
    other = client.get('/health/webhook').headers['Content-Security-Policy']
    assert 'attendance-frontend' not in other
    assert 'zoom.us' in other


def test_health_summary_allows_frontend_browser_calls(client, monkeypatch):
    """The System Health tab fetches /health/summary from the frontend
    origin; without the CORS header the browser reports 'Failed to fetch'."""
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: FakeSummaryBQ())
    origin = 'https://attendance-frontend-4e5na4tdha-uc.a.run.app'
    r = client.get('/health/summary', headers={'Origin': origin})
    assert r.headers.get('Access-Control-Allow-Origin') == origin


# ── /health/alert (watchdog ALARM digest email) ───────────────────────────

class FakeAlarmBQ:
    def __init__(self, alarms):
        self.alarms = alarms

    def query(self, sql, **kw):
        assert "severity = 'ALARM'" in sql
        return mock.MagicMock(result=lambda *a, **k: list(self.alarms))


def test_health_alert_emails_digest_when_alarms(client, monkeypatch):
    rows = [SimpleNamespace(check_id='05', check_name='Unresolved room names',
                            metric='44', detail='Room-abc, Room-def',
                            action='Run the Room Mapper panel')]
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: FakeAlarmBQ(rows))
    sent = []
    monkeypatch.setattr(app_module, 'send_email_alert',
                        lambda subject, html: sent.append((subject, html)) or {'ok': True})
    r = client.get('/health/alert')
    body = r.get_json()
    assert r.status_code == 200 and body['alarm_count'] == 1 and body['alert_sent'] is True
    assert '1 health alarm' in sent[0][0]
    assert 'Unresolved room names' in sent[0][1] and 'Run the Room Mapper panel' in sent[0][1]


def test_health_alert_check_only_and_quiet_when_clean(client, monkeypatch):
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: FakeAlarmBQ([]))
    sent = []
    monkeypatch.setattr(app_module, 'send_email_alert', lambda *a: sent.append(a))
    assert client.get('/health/alert').get_json()['alert_sent'] is False
    rows = [SimpleNamespace(check_id='03', check_name='Webhook ingestion alive',
                            metric='95', detail='', action='')]
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: FakeAlarmBQ(rows))
    body = client.get('/health/alert?alert=false').get_json()
    assert body['alarm_count'] == 1 and body['alert_sent'] is False
    assert sent == []


# ── /rooms/catalog: choices for the rename dropdowns ──────────────────────

def test_rooms_catalog_merges_panel_list_and_bigquery(client, monkeypatch):
    ms = app_module.meeting_state
    ms.last_sync_payload = {'rooms': [{'room_name': '6.0 BREAK TIME'},
                                      {'room_name': '1.1 Sales Wizard'},
                                      {'room_name': 'Room-abc12345'}]}   # placeholder: excluded
    ms.uuid_to_name['sdk:abc'] = '2.0 Vridam'
    fake = mock.MagicMock()
    fake.query.return_value.result.return_value = [
        SimpleNamespace(room_name='1.1 Sales Wizard'),        # duplicate: merged
        SimpleNamespace(room_name='3.3 Cloud Gunners')]
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    try:
        body = client.get('/rooms/catalog').get_json()
    finally:
        del ms.last_sync_payload
        ms.uuid_to_name.pop('sdk:abc', None)
    assert body['names'] == ['1.1 Sales Wizard', '2.0 Vridam', '3.3 Cloud Gunners', '6.0 BREAK TIME']
    assert body['from_panel'] == 4 and body['total'] == 4


# ── point 9: webhook must fail CLOSED without a secret ────────────────────

def test_webhook_rejected_when_secret_missing(client, monkeypatch):
    monkeypatch.setattr(app_module, 'ZOOM_WEBHOOK_SECRET', '')
    r = client.post('/webhook', data=json.dumps(load_fixture('participant_joined')),
                    content_type='application/json')
    assert r.status_code == 401


def test_log_json_is_parseable(capsys):
    zt_observability.log_json('WARNING', 'mapping conflict', room_uuid='X', count=2)
    line = capsys.readouterr().out.strip()
    parsed = json.loads(line)
    assert parsed == {'severity': 'WARNING', 'message': 'mapping conflict',
                      'room_uuid': 'X', 'count': 2}
