"""Point 5 — room-mapping module: witness rules + DISPUTED persistence.

The 2-witness rule exists because of a real incident (17-20 Aug: 63 hours
filed as break off ONE person's position). These tests pin the fixes:
  - one person = one witness, even with both email and name keys;
  - a display name shared by several no-email people identifies nobody;
  - DISPUTED freezes survive a restart via BigQuery.
"""
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import app as app_module
import zt_mapping
from helpers import FakeBQ

WU = 'WEBHOOK-UUID-AAAABBBBCCCCDDDD'


# ── pure logic ────────────────────────────────────────────────────────────

def test_person_with_email_and_name_is_one_entry():
    persons, ambiguous = zt_mapping.build_sdk_persons([
        {'room_name': 'Sales Team',
         'participants': [{'name': 'Ravi Kumar', 'email': 'Ravi@X.com'}]}])
    assert ambiguous == set()
    assert len(persons) == 1
    assert persons[0]['keys'] == ['e:ravi@x.com', 'n:ravi kumar']


def test_shared_no_email_name_is_excluded():
    persons, ambiguous = zt_mapping.build_sdk_persons([
        {'room_name': 'Sales Team', 'participants': [{'name': 'Priya'}]},
        {'room_name': 'Break Time', 'participants': [{'name': 'Priya'},
                                                     {'name': 'Arjun'}]}])
    assert ambiguous == {'priya'}
    assert [p['person_id'] for p in persons] == ['n:arjun']


def test_rejoin_suffix_normalized():
    persons, _ = zt_mapping.build_sdk_persons([
        {'room_name': 'Sales Team', 'participants': [{'name': 'Ravi Kumar-2'}]}])
    assert persons[0]['person_id'] == 'n:ravi kumar'


# ── /mapping/sync integration ─────────────────────────────────────────────

@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


@pytest.fixture
def sync_env(monkeypatch):
    """Synchronous persistence thread + recorded mapping writes + stable,
    2-minute-old webhook positions for the people we register."""
    class FakeThread:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(app_module.threading, 'Thread', FakeThread)
    saved = []
    monkeypatch.setattr(app_module, 'insert_room_mappings',
                        lambda rows: saved.extend(rows) or True)
    monkeypatch.setattr(zt_mapping, 'hydrate_disputes', lambda *a, **k: 0)

    ms = app_module.meeting_state
    ms.reset()
    ms.meeting_id = '123456789'
    ms.meeting_uuid = 'MTG-1'

    def put_person(name, email, room_uuid=WU):
        app_module._track_breakout_state(name, email, room_uuid, 'MTG-1')
        with ms._lock:
            for k in app_module._bo_state_keys(name, email):
                if k in ms.participant_current_breakout:
                    ms.participant_current_breakout[k]['ts'] = time.time() - 300
    return SimpleNamespace(saved=saved, put_person=put_person, ms=ms)


def _sync(client, participants, room='Sales Team'):
    return client.post('/mapping/sync', json={
        'meeting_id': '123456789', 'meeting_uuid': 'MTG-1',
        'rooms': [{'room_name': room, 'sdk_uuid': 'SDK-1',
                   'participants': participants}]})


def test_one_person_is_held_not_mapped(client, sync_env):
    """REGRESSION for the double-witness bug: one person with email+name
    used to count as 2 witnesses and name the room on their own."""
    sync_env.put_person('Ravi Kumar', 'ravi@x.com')
    r = _sync(client, [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'}])
    assert r.status_code == 200
    assert WU not in sync_env.ms.uuid_to_name           # HELD
    assert all(m['room_uuid'] != WU for m in sync_env.saved)


def test_two_people_map_the_room(client, sync_env):
    sync_env.put_person('Ravi Kumar', 'ravi@x.com')
    sync_env.put_person('Priya S', 'priya@x.com')
    _sync(client, [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'},
                   {'name': 'Priya S', 'email': 'priya@x.com'}])
    assert sync_env.ms.uuid_to_name.get(WU) == 'Sales Team'
    assert any(m['room_uuid'] == WU and m['room_name'] == 'Sales Team'
               for m in sync_env.saved)


def test_two_shared_name_people_do_not_map(client, sync_env):
    """Two no-email 'Priya's cancel out instead of pairing wrongly."""
    sync_env.put_person('Priya', '')
    r = client.post('/mapping/sync', json={
        'meeting_id': '123456789', 'meeting_uuid': 'MTG-1',
        'rooms': [
            {'room_name': 'Sales Team', 'sdk_uuid': 'S1',
             'participants': [{'name': 'Priya'}]},
            {'room_name': 'Break Time', 'sdk_uuid': 'S2',
             'participants': [{'name': 'Priya'}]},
        ]})
    assert r.status_code == 200
    assert WU not in sync_env.ms.uuid_to_name


# ── DISPUTED persistence ──────────────────────────────────────────────────

def _cfg():
    return {'project': 'test-project', 'dataset': 'test_ds'}


def test_persist_dispute_creates_table_and_row():
    fake = FakeBQ()
    assert zt_mapping.persist_dispute(lambda: fake, _cfg(), WU,
                                      meeting_uuid='MTG-1',
                                      kept_name='Sales Team',
                                      rejected_name='Team B')
    assert len(fake.rows) == 1
    assert fake.rows[0]['room_uuid'] == WU
    assert fake.rows[0]['kept_name'] == 'Sales Team'


def test_hydrate_restores_freezes_after_restart():
    """A frozen room must stay frozen across a deploy: BigQuery remembers."""
    fake = mock.MagicMock()
    fake.query.return_value.result.return_value = [
        SimpleNamespace(room_uuid=WU, frozen_epoch=1790000000)]
    ms = app_module.meeting_state
    ms.reset()
    merged = zt_mapping.hydrate_disputes(lambda: fake, _cfg(), ms)
    assert merged == 1
    assert WU in ms.mapping_disputes
    # memoized: a second call the same day does not query again
    assert zt_mapping.hydrate_disputes(lambda: fake, _cfg(), ms) == 0
    assert fake.query.call_count == 1


def test_hydrate_survives_missing_table():
    def boom():
        raise RuntimeError('Not found: Table test.mapping_disputes')
    ms = app_module.meeting_state
    ms.reset()
    assert zt_mapping.hydrate_disputes(boom, _cfg(), ms) == 0   # no raise


def test_disputed_freeze_is_persisted_from_sync(client, sync_env, monkeypatch):
    """When the anti-ping-pong freeze fires, the freeze reaches BigQuery."""
    persisted = []
    monkeypatch.setattr(zt_mapping, 'persist_dispute',
                        lambda *a, **k: persisted.append((a, k)) or True)
    ms = sync_env.ms
    # The room was already corrected Team B -> Sales Team; now stable people
    # claim 'Team B' again. That REVERSES the recent correction => freeze.
    ms.uuid_to_name[WU] = 'Sales Team'
    with ms._lock:
        ms.last_uuid_corrections[WU] = {'old': 'Team B', 'new': 'Sales Team',
                                        'ts': time.time() - 60}
    sync_env.put_person('Ravi Kumar', 'ravi@x.com')
    sync_env.put_person('Priya S', 'priya@x.com')
    _sync(client, [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'},
                   {'name': 'Priya S', 'email': 'priya@x.com'}], room='Team B')
    assert WU in ms.mapping_disputes
    assert len(persisted) == 1
    assert persisted[0][0][2] == WU     # room_uuid positional arg
