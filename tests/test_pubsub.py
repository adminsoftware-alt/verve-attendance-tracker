"""Tests for the Pub/Sub webhook buffer (zt_pubsub + /webhook + /pubsub/*).

Run:  pytest tests/
No GCP access needed: BigQuery and the Pub/Sub publisher are faked.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from unittest import mock

import pytest

os.environ.setdefault('ZOOM_WEBHOOK_SECRET', 'test-secret')
os.environ.setdefault('GCP_PROJECT_ID', 'test-project')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import zt_pubsub  # noqa: E402

SECRET = os.environ['ZOOM_WEBHOOK_SECRET']


class FakeBQ:
    """insert_rows_json fails `fail_times` times, then succeeds."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.rows = []
        self.calls = 0

    def insert_rows_json(self, table_id, rows, **kw):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            return [{'index': 0, 'errors': [{'reason': 'backendError'}]}]
        self.rows.extend(rows)
        return []

    def query(self, *a, **kw):
        return mock.MagicMock(result=lambda *a, **kw: [])


def zoom_event(event='meeting.participant_joined', name='Ravi Kumar',
               email='ravi@example.com', ts_ms=None, room_uuid=None):
    obj = {
        'id': '123456789',
        'uuid': 'meeting-uuid-1',
        'participant': {'user_id': 'u1', 'user_name': name, 'email': email},
    }
    if room_uuid:
        obj['breakout_room_uuid'] = room_uuid
    return {'event': event, 'event_ts': ts_ms or int(time.time() * 1000),
            'payload': {'object': obj}}


def signed_post(client, body_dict):
    body = json.dumps(body_dict)
    ts = str(int(time.time()))
    sig = 'v0=' + hmac.new(SECRET.encode(), f'v0:{ts}:{body}'.encode(),
                           hashlib.sha256).hexdigest()
    return client.post('/webhook', data=body, content_type='application/json',
                       headers={'x-zm-signature': sig, 'x-zm-request-timestamp': ts})


def push_envelope(zoom_body_dict, message_id='m-1', attempt=1):
    inner = json.dumps({'zoom_body': json.dumps(zoom_body_dict),
                        'received_at': time.time(), 'schema_version': 1})
    return {'message': {'messageId': message_id,
                        'data': base64.b64encode(inner.encode()).decode()},
            'subscription': 'projects/p/subscriptions/s',
            'deliveryAttempt': attempt}


@pytest.fixture
def client(monkeypatch):
    app_module.app.config['TESTING'] = True
    # Fresh dedup / processed-message memory for every test
    app_module.meeting_state.event_dedup_cache.clear()
    zt_pubsub._processed.clear()
    zt_pubsub._verified_tokens.clear()
    return app_module.app.test_client()


@pytest.fixture
def bq(monkeypatch):
    fake = FakeBQ()
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    return fake


@pytest.fixture
def pubsub_on(monkeypatch):
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_ENABLED', True)
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_PUSH_AUDIENCE', 'https://svc/pubsub/zoom-events')
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_PUSH_SA_EMAIL', 'push@test.iam.gserviceaccount.com')


@pytest.fixture
def push_auth_ok(monkeypatch):
    monkeypatch.setattr(zt_pubsub, 'verify_push_request', lambda req: (True, None))


# ---------------------------------------------------------------- /webhook ---

def test_flag_off_processes_inline(client, bq, monkeypatch):
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_ENABLED', False)
    publish = mock.Mock()
    monkeypatch.setattr(zt_pubsub, 'publish_webhook', publish)
    r = signed_post(client, zoom_event())
    assert r.status_code == 200 and r.get_json()['status'] == 'success'
    publish.assert_not_called()
    assert len(bq.rows) == 1 and bq.rows[0]['event_type'] == 'participant_joined'


def test_flag_on_without_push_auth_stays_inline(client, bq, monkeypatch):
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_ENABLED', True)
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_PUSH_AUDIENCE', '')
    publish = mock.Mock()
    monkeypatch.setattr(zt_pubsub, 'publish_webhook', publish)
    r = signed_post(client, zoom_event())
    assert r.get_json()['status'] == 'success'
    publish.assert_not_called()
    assert len(bq.rows) == 1


def test_flag_on_publishes_and_does_not_process(client, bq, pubsub_on, monkeypatch):
    publish = mock.Mock(return_value='msg-42')
    monkeypatch.setattr(zt_pubsub, 'publish_webhook', publish)
    event = zoom_event()
    r = signed_post(client, event)
    assert r.status_code == 200 and r.get_json()['status'] == 'queued'
    publish.assert_called_once()
    sent_data, sent_raw = publish.call_args[0]
    assert sent_data['event'] == 'meeting.participant_joined'
    assert json.loads(sent_raw) == event          # exact body forwarded
    assert bq.rows == []                           # nothing written inline


def test_publish_failure_falls_back_to_inline(client, bq, pubsub_on, monkeypatch):
    monkeypatch.setattr(zt_pubsub, 'publish_webhook',
                        mock.Mock(side_effect=RuntimeError('pubsub down')))
    r = signed_post(client, zoom_event())
    assert r.status_code == 200 and r.get_json()['status'] == 'success'
    assert len(bq.rows) == 1                       # not lost


def test_bad_signature_is_not_published(client, bq, pubsub_on, monkeypatch):
    publish = mock.Mock()
    monkeypatch.setattr(zt_pubsub, 'publish_webhook', publish)
    r = client.post('/webhook', data=json.dumps(zoom_event()), content_type='application/json',
                    headers={'x-zm-signature': 'v0=bad', 'x-zm-request-timestamp': str(int(time.time()))})
    assert r.status_code == 401
    publish.assert_not_called()


# ------------------------------------------------------- /pubsub/zoom-events ---

def test_push_rejected_when_auth_not_configured(client, bq, monkeypatch):
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_PUSH_AUDIENCE', '')
    r = client.post('/pubsub/zoom-events', json=push_envelope(zoom_event()))
    assert r.status_code == 403
    assert bq.rows == []


def test_push_rejected_for_wrong_service_account(client, bq, pubsub_on, monkeypatch):
    claims = {'email': 'attacker@evil.iam.gserviceaccount.com', 'email_verified': True}
    with mock.patch('google.oauth2.id_token.verify_oauth2_token', return_value=claims):
        r = client.post('/pubsub/zoom-events', json=push_envelope(zoom_event()),
                        headers={'Authorization': 'Bearer x'})
    assert r.status_code == 403


def test_push_accepted_for_expected_service_account(client, bq, pubsub_on):
    claims = {'email': 'push@test.iam.gserviceaccount.com', 'email_verified': True}
    with mock.patch('google.oauth2.id_token.verify_oauth2_token', return_value=claims):
        r = client.post('/pubsub/zoom-events', json=push_envelope(zoom_event()),
                        headers={'Authorization': 'Bearer x'})
    assert r.status_code == 204
    assert len(bq.rows) == 1


def test_verified_token_is_cached_until_expiry(client, bq, pubsub_on):
    claims = {'email': 'push@test.iam.gserviceaccount.com', 'email_verified': True,
              'exp': time.time() + 3600}
    with mock.patch('google.oauth2.id_token.verify_oauth2_token', return_value=claims) as v:
        for i in range(3):
            r = client.post('/pubsub/zoom-events', headers={'Authorization': 'Bearer tok'},
                            json=push_envelope(zoom_event(name=f'P{i}', email=f'p{i}@x.com'), f'c-{i}'))
            assert r.status_code == 204
    assert v.call_count == 1          # certs fetched once, not per push


def test_expired_cached_token_is_reverified(client, bq, pubsub_on):
    zt_pubsub._verified_tokens['old'] = {'email': 'push@test.iam.gserviceaccount.com',
                                         'email_verified': True, 'exp': time.time() - 1}
    with mock.patch('google.oauth2.id_token.verify_oauth2_token',
                    side_effect=ValueError('Token expired')):
        r = client.post('/pubsub/zoom-events', headers={'Authorization': 'Bearer old'},
                        json=push_envelope(zoom_event()))
    assert r.status_code == 403


def test_cached_token_still_checks_service_account(client, bq, pubsub_on):
    zt_pubsub._verified_tokens['evil'] = {'email': 'attacker@x.iam.gserviceaccount.com',
                                          'email_verified': True, 'exp': time.time() + 3600}
    r = client.post('/pubsub/zoom-events', headers={'Authorization': 'Bearer evil'},
                    json=push_envelope(zoom_event()))
    assert r.status_code == 403


def test_push_processes_event(client, bq, push_auth_ok):
    r = client.post('/pubsub/zoom-events', json=push_envelope(zoom_event()))
    assert r.status_code == 204
    assert len(bq.rows) == 1
    assert bq.rows[0]['participant_email'] == 'ravi@example.com'


def test_push_redelivery_same_message_id_is_skipped(client, bq, push_auth_ok):
    env = push_envelope(zoom_event(), message_id='same-id')
    assert client.post('/pubsub/zoom-events', json=env).status_code == 204
    assert client.post('/pubsub/zoom-events', json=env).status_code == 204
    assert len(bq.rows) == 1


def test_bigquery_failure_returns_500_then_retry_succeeds(client, monkeypatch, push_auth_ok):
    """The core point of Pub/Sub: a failed insert is retried, not lost.
    The retry must NOT be swallowed by the 60s in-memory dedup cache."""
    fake = FakeBQ(fail_times=1)
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    event = zoom_event()  # identical body on both deliveries, as Pub/Sub does

    first = client.post('/pubsub/zoom-events', json=push_envelope(event, 'retry-me', 1))
    assert first.status_code == 500 and fake.rows == []

    second = client.post('/pubsub/zoom-events', json=push_envelope(event, 'retry-me', 2))
    assert second.status_code == 204
    assert len(fake.rows) == 1


def test_bigquery_exception_also_retried(client, monkeypatch, push_auth_ok):
    class Boom(FakeBQ):
        def insert_rows_json(self, *a, **kw):
            raise ConnectionError('network down')
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: Boom())
    r = client.post('/pubsub/zoom-events', json=push_envelope(zoom_event()))
    assert r.status_code == 500


def test_handler_bug_is_acked_not_retried(client, bq, push_auth_ok, monkeypatch):
    monkeypatch.setattr(app_module, 'handle_participant_joined',
                        mock.Mock(side_effect=KeyError('bug')))
    r = client.post('/pubsub/zoom-events', json=push_envelope(zoom_event()))
    assert r.status_code == 204


def test_malformed_message_is_acked(client, bq, push_auth_ok):
    r = client.post('/pubsub/zoom-events', json={'message': {'messageId': 'x', 'data': 'bm90IGpzb24='}})
    assert r.status_code == 204
    assert bq.rows == []


def test_inline_path_unchanged_on_bigquery_failure(client, monkeypatch):
    """Strict mode must not leak into the inline path: Zoom still gets 200."""
    monkeypatch.setattr(zt_pubsub, 'PUBSUB_ENABLED', False)
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: FakeBQ(fail_times=5))
    r = signed_post(client, zoom_event())
    assert r.status_code == 200


def test_breakout_join_via_push_tracks_position(client, bq, push_auth_ok):
    ev = zoom_event(event='meeting.participant_joined_breakout_room', room_uuid='ROOM-UUID-ABCDEFGH')
    r = client.post('/pubsub/zoom-events', json=push_envelope(ev, message_id='bo-1'))
    assert r.status_code == 204
    assert bq.rows[0]['event_type'] == 'breakout_room_joined'
    pos = app_module.meeting_state.participant_current_breakout.get('e:ravi@example.com')
    assert pos and pos['room_uuid'] == 'ROOM-UUID-ABCDEFGH'


# ------------------------------------------------------------ zt_pubsub unit ---

def test_ordering_key_prefers_email_then_normalised_name():
    assert zt_pubsub._ordering_key(zoom_event(email='A@B.com')) == 'e:a@b.com'
    assert zt_pubsub._ordering_key(zoom_event(email='', name='Ravi Kumar-2')) == 'n:ravi kumar'
    assert zt_pubsub._ordering_key({'payload': {'object': {'uuid': 'M'}}}) == 'm:M'


def test_publish_resumes_ordering_key_on_failure(monkeypatch):
    future = mock.Mock()
    future.result.side_effect = TimeoutError()
    publisher = mock.Mock()
    publisher.publish.return_value = future
    monkeypatch.setattr(zt_pubsub, '_get_publisher', lambda: (publisher, 'projects/p/topics/t'))
    with pytest.raises(TimeoutError):
        zt_pubsub.publish_webhook(zoom_event(email='x@y.com'), '{}')
    publisher.resume_publish.assert_called_once_with('projects/p/topics/t', 'e:x@y.com')
