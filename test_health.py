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
        elif 'presence_intervals' in sql:
            rows = [SimpleNamespace(last_build='2026-09-29 10:00:00', unknown_room_pct=3.4)]
        elif 'HAVING COUNT(*) > 1' in sql:
            assert ' AS groups' not in sql, 'reserved BigQuery keyword as alias'
            rows = [SimpleNamespace(dup_groups=2, extra_rows=3)]
        else:  # events count
            rows = [SimpleNamespace(n=8421, last_inserted_at='2026-09-29 10:29:48')]
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
    zt_observability.record_webhook('meeting.participant_joined')
    with zt_observability._lock:
        zt_observability._webhook['last_ts'] -= 3600   # an hour ago
    r = client.get('/health/webhook')
    assert r.status_code == 503
    assert r.get_json()['status'] == 'STALE'


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


def test_log_json_is_parseable(capsys):
    zt_observability.log_json('WARNING', 'mapping conflict', room_uuid='X', count=2)
    line = capsys.readouterr().out.strip()
    parsed = json.loads(line)
    assert parsed == {'severity': 'WARNING', 'message': 'mapping conflict',
                      'room_uuid': 'X', 'count': 2}
