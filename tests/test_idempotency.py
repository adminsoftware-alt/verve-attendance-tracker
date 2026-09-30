"""Point 3 — idempotent event processing.

The same Zoom event must always produce the same event_id, so BigQuery
(streaming insertId + the v15 QUALIFY) can drop repeats that get past the
60-second in-memory cache (server restart, >60s retry, Pub/Sub redelivery).
"""
import datetime

import pytest

import app as app_module
from helpers import FakeBQ, load_fixture


@pytest.fixture
def bq(monkeypatch):
    fake = FakeBQ()
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    return fake


def _p(ts=None):
    return {
        'meeting_uuid': 'MTGuuid-instance-1==', 'meeting_id': '123456789',
        'participant_id': '16778240', 'participant_email': 'ravi.kumar@example.com',
        'participant_name': 'Ravi Kumar', 'room_uuid': '',
        'event_dt': ts or datetime.datetime(2026, 9, 29, 10, 0, 0),
    }


def test_same_event_same_id():
    assert (app_module.deterministic_event_id('participant_joined', _p())
            == app_module.deterministic_event_id('participant_joined', _p()))


def test_different_event_different_id():
    base = app_module.deterministic_event_id('participant_joined', _p())
    later = app_module.deterministic_event_id(
        'participant_joined', _p(ts=datetime.datetime(2026, 9, 29, 10, 0, 1)))
    other_type = app_module.deterministic_event_id('participant_left', _p())
    other_room = app_module.deterministic_event_id('participant_joined', _p(),
                                                   room_uuid='BR-1')
    assert len({base, later, other_type, other_room}) == 4


@pytest.mark.parametrize('fixture,expected_type', [
    ('participant_joined', 'participant_joined'),
    ('participant_left', 'participant_left'),
    ('breakout_joined', 'breakout_room_joined'),
    ('breakout_left', 'breakout_room_left'),
    ('reconnect', 'participant_joined'),
])
def test_fixture_stores_deterministic_id(bq, fixture, expected_type):
    data = load_fixture(fixture)
    app_module._dispatch_webhook_event(data['event'], data)
    assert len(bq.rows) == 1
    row = bq.rows[0]
    assert row['event_type'] == expected_type
    assert row['event_id'].startswith('e1-')
    # and the id is passed to BigQuery as the streaming-dedup insertId
    assert bq.row_ids == [row['event_id']]


def test_redelivered_event_gets_identical_id(bq):
    """Simulates the case the old code could not handle: the same webhook
    processed twice with the in-memory cache gone (restart). Both rows get
    the SAME id, so BigQuery / the v15 SQL can collapse them."""
    data = load_fixture('participant_joined')
    app_module._dispatch_webhook_event(data['event'], data)
    app_module.meeting_state.event_dedup_cache.clear()   # "restart"
    app_module._dispatch_webhook_event(data['event'], data)
    assert len(bq.rows) == 2
    assert bq.rows[0]['event_id'] == bq.rows[1]['event_id']


def test_within_window_duplicate_still_dropped_in_memory(bq):
    """The 60s cache still works as the first line of defence."""
    data = load_fixture('participant_joined')
    app_module._dispatch_webhook_event(data['event'], data)
    app_module._dispatch_webhook_event(data['event'], data)
    assert len(bq.rows) == 1
