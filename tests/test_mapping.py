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
    real_hydrate_positions = zt_mapping.hydrate_positions
    monkeypatch.setattr(zt_mapping, 'hydrate_positions', lambda *a, **k: 0)

    ms = app_module.meeting_state
    ms.reset()
    ms.meeting_id = '123456789'
    ms.meeting_uuid = 'MTG-1'

    def put_person(name, email, room_uuid=WU, age_s=300):
        app_module._track_breakout_state(name, email, room_uuid, 'MTG-1')
        with ms._lock:
            for k in app_module._bo_state_keys(name, email):
                if k in ms.participant_current_breakout:
                    ms.participant_current_breakout[k]['ts'] = time.time() - age_s
    return SimpleNamespace(saved=saved, put_person=put_person, ms=ms,
                           hydrate_positions=real_hydrate_positions)


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


# ── incident 2026-10-06: no single-person naming, one name = one room ────

def test_pending_request_is_not_resolved_by_one_person(client, sync_env):
    """The shortcut that named five rooms BREAK TIME: one person's current
    SDK room must not name the webhook room they joined earlier."""
    ms = sync_env.ms
    with ms._lock:
        ms.pending_mapping_requests.append({
            'request_id': 'r1', 'webhook_uuid': WU, 'participant_name': 'Ravi Kumar',
            'participant_email': 'ravi@x.com', 'timestamp': time.time()})
    r = _sync(client, [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'}], room='8.0:BREAK TIME')
    assert r.status_code == 200
    assert WU not in ms.uuid_to_name                      # not named from one person
    assert ms.pending_mapping_requests == []              # request not re-served either
    assert all(m['room_uuid'] != WU for m in sync_env.saved)


def test_resolve_endpoint_does_not_name_from_one_person(client, sync_env):
    r = client.post('/mapping/resolve', json={
        'request_id': 'r1', 'webhook_uuid': WU, 'room_name': '8.0:BREAK TIME',
        'meeting_id': '123456789'})
    assert r.status_code == 200
    assert WU not in sync_env.ms.uuid_to_name
    assert sync_env.saved == []


def test_two_rooms_claiming_one_name_keeps_the_better_supported(client, sync_env):
    """Five rooms were named BREAK TIME in one sync. Now only the room with
    more witnesses gets the name; the other stays unnamed."""
    WU2 = 'WEBHOOK-UUID-EEEEFFFFGGGGHHHH'
    for p in _three_people():
        sync_env.put_person(p['name'], p['email'], room_uuid=WU)        # 3 in WU
    sync_env.put_person('Dev K', 'dev@x.com', room_uuid=WU2)             # 2 in WU2
    sync_env.put_person('Esha P', 'esha@x.com', room_uuid=WU2)
    _sync(client, _three_people() + [{'name': 'Dev K', 'email': 'dev@x.com'},
                                     {'name': 'Esha P', 'email': 'esha@x.com'}])
    assert sync_env.ms.uuid_to_name.get(WU) == 'Sales Team'
    assert WU2 not in sync_env.ms.uuid_to_name


OTHER = 'OTHER-ROOM-UUID-AAAAAAAAAAA'


def test_name_already_used_by_an_occupied_room_is_refused(client, sync_env):
    sync_env.ms.uuid_to_name[OTHER] = 'Sales Team'
    sync_env.put_person('Zed Q', 'zed@x.com', room_uuid=OTHER)   # OTHER is occupied
    for p in _three_people():
        sync_env.put_person(p['name'], p['email'])
    _sync(client, _three_people())
    assert WU not in sync_env.ms.uuid_to_name
    assert sync_env.ms.uuid_to_name[OTHER] == 'Sales Team'


def test_empty_rooms_old_name_does_not_block_a_new_room(client, sync_env):
    """Host recreates the rooms: the old id keeps its name in memory but is
    empty, so the new id must still get the name."""
    sync_env.ms.uuid_to_name[OTHER] = 'Sales Team'                  # nobody in it
    for p in _three_people():
        sync_env.put_person(p['name'], p['email'])
    _sync(client, _three_people())
    assert sync_env.ms.uuid_to_name.get(WU) == 'Sales Team'


def test_fresh_witnesses_correct_a_wrong_name(client, sync_env):
    sync_env.ms.uuid_to_name[WU] = 'Team B'
    sync_env.put_person('Ravi Kumar', 'ravi@x.com')
    sync_env.put_person('Priya S', 'priya@x.com')
    _sync(client, [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'},
                   {'name': 'Priya S', 'email': 'priya@x.com'}], room='Sales Team')
    assert sync_env.ms.uuid_to_name.get(WU) == 'Sales Team'


def test_correction_into_an_occupied_rooms_name_is_refused(client, sync_env):
    """The Aug-2026 / 2026-10-06 shape: a team room must not be renamed to
    BREAK TIME while the real break room is occupied."""
    ms = sync_env.ms
    ms.uuid_to_name[WU] = 'Sales Team'
    ms.uuid_to_name[OTHER] = 'Break Time'
    sync_env.put_person('Zed Q', 'zed@x.com', room_uuid=OTHER)   # break room occupied
    sync_env.put_person('Ravi Kumar', 'ravi@x.com')
    sync_env.put_person('Priya S', 'priya@x.com')
    _sync(client, [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'},
                   {'name': 'Priya S', 'email': 'priya@x.com'}], room='Break Time')
    assert ms.uuid_to_name.get(WU) == 'Sales Team'
    assert not [m for m in sync_env.saved if m['room_uuid'] == WU and m['room_name'] == 'Break Time']


def test_live_shows_one_card_per_occupied_room_id():
    rooms = [
        {'room_name': 'BREAK TIME', 'room_uuid': 'A', 'participant_count': 17, 'participants': []},
        {'room_name': 'BREAK TIME', 'room_uuid': 'B', 'participant_count': 4, 'participants': []},
        {'room_name': 'BREAK TIME', 'room_uuid': 'C', 'participant_count': 0, 'participants': []},
        {'room_name': 'Sales Team', 'room_uuid': 'D', 'participant_count': 0, 'participants': []},
        {'room_name': 'Sales Team', 'room_uuid': 'E', 'participant_count': 0, 'participants': []},
        {'room_name': 'Main Room', 'room_uuid': '', 'participant_count': 3, 'participants': []},
    ]
    out = app_module.split_same_name_rooms(rooms)
    assert [(r['room_name'], r['room_uuid']) for r in out] == [
        ('BREAK TIME', 'A'), ('BREAK TIME [room 2]', 'B'),   # both occupied ids shown
        ('Sales Team', 'D'),                                  # empty duplicates collapse
        ('Main Room', ''),
    ]


def test_correction_falls_back_to_insert_when_update_is_blocked(monkeypatch):
    """BigQuery refuses UPDATE on freshly streamed rows for ~90 min; a
    correction must still land (as a newer row) instead of failing forever."""
    class FakeClient:
        def __init__(self):
            self.inserted = []

        def query(self, sql, job_config=None):
            if 'UPDATE' in sql:
                raise RuntimeError('UPDATE or DELETE statement over table would affect '
                                   'rows in the streaming buffer, which is not supported')
            row = SimpleNamespace(mapping_id='m1', room_name='8.0:BREAK TIME',
                                  source='webhook_primary_sdk_lookup')
            return mock.MagicMock(result=lambda *a, **k: [row])

        def insert_rows_json(self, table_id, rows, **kw):
            self.inserted.extend(rows)
            return []

    fake = FakeClient()
    monkeypatch.setattr(app_module, 'get_bq_client', lambda: fake)
    ok = app_module.insert_room_mappings([{
        'mapping_id': 'm2', 'meeting_id': '123', 'meeting_uuid': 'MTG-1',
        'room_uuid': WU, 'room_name': '1.2:Between The Spreadsheet', 'room_index': -1,
        'mapping_date': '2026-10-06', 'mapped_at': '2026-10-06T09:00:00',
        'source': 'webhook_primary_sdk_lookup'}])
    assert ok is True
    assert len(fake.inserted) == 1 and fake.inserted[0]['room_name'] == '1.2:Between The Spreadsheet'


# ── consensus: teams that sat all morning (positions older than 30 min) ──

STALE = 2 * 3600   # 2 hours: past the 30-min freshness cap


def _three_people():
    return [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'},
            {'name': 'Priya S', 'email': 'priya@x.com'},
            {'name': 'Arjun M', 'email': 'arjun@x.com'}]


def test_three_stale_people_map_by_consensus(client, sync_env):
    """The 2026-09-30 case: 8-person team in a room since morning, nobody
    moved for hours -> the freshness cap kept the room unnamed all day."""
    for p in _three_people():
        sync_env.put_person(p['name'], p['email'], age_s=STALE)
    _sync(client, _three_people())
    assert sync_env.ms.uuid_to_name.get(WU) == 'Sales Team'


def test_two_stale_people_are_still_held(client, sync_env):
    """Below consensus AND not fresh: a lost move-webhook could strand two
    people on an old room id, so two stale witnesses are not enough."""
    for p in _three_people()[:2]:
        sync_env.put_person(p['name'], p['email'], age_s=STALE)
    _sync(client, _three_people()[:2])
    assert WU not in sync_env.ms.uuid_to_name


def test_stale_people_cannot_overwrite_an_existing_name(client, sync_env):
    """Corrections stay fresh-only: three stale witnesses may NAME an unknown
    room but may not RENAME a room that already has a name."""
    sync_env.ms.uuid_to_name[WU] = 'Team B'
    for p in _three_people():
        sync_env.put_person(p['name'], p['email'], age_s=STALE)
    _sync(client, _three_people(), room='Sales Team')
    assert sync_env.ms.uuid_to_name.get(WU) == 'Team B'


# ── position rehydration after restart (mapping fix B) ───────────────────

def _bq_rows(*rows):
    fake = mock.MagicMock()
    fake.query.return_value.result.return_value = list(rows)
    return fake


def _row(name, email, etype, room=WU, ts_ago=300, mtg='MTG-1'):
    return SimpleNamespace(participant_name=name, participant_email=email,
                           event_type=etype, room_uuid=room, meeting_uuid=mtg,
                           ts_epoch=time.time() - ts_ago)


def test_positions_restored_with_real_timestamps():
    ms = app_module.meeting_state
    ms.reset()
    fake = _bq_rows(_row('Ravi Kumar', 'ravi@x.com', 'breakout_room_joined', ts_ago=300),
                    _row('Priya S', '', 'breakout_room_left'))          # left => no entry
    n = zt_mapping.hydrate_positions(lambda: fake, _cfg(), 'events', ms)
    assert n == 1
    entry = ms.participant_current_breakout['e:ravi@x.com']
    assert entry['room_uuid'] == WU and entry['meeting_uuid'] == 'MTG-1'
    assert 290 <= time.time() - entry['ts'] <= 310        # real event time kept
    assert 'n:ravi kumar' in ms.participant_current_breakout
    assert 'n:priya s' not in ms.participant_current_breakout
    assert ms.last_breakout_instance_uuid == 'MTG-1'


def test_live_memory_wins_over_snapshot():
    ms = app_module.meeting_state
    ms.reset()
    app_module._track_breakout_state('Ravi Kumar', 'ravi@x.com', 'LIVE-ROOM', 'MTG-1')
    fake = _bq_rows(_row('Ravi Kumar', 'ravi@x.com', 'breakout_room_joined', room='OLD-ROOM'))
    assert zt_mapping.hydrate_positions(lambda: fake, _cfg(), 'events', ms) == 0
    assert ms.participant_current_breakout['e:ravi@x.com']['room_uuid'] == 'LIVE-ROOM'


def test_position_hydration_runs_once_and_retries_on_error():
    ms = app_module.meeting_state
    ms.reset()

    def boom():
        raise ConnectionError('bq down')
    assert zt_mapping.hydrate_positions(boom, _cfg(), 'events', ms) == 0
    fake = _bq_rows(_row('Ravi Kumar', 'ravi@x.com', 'breakout_room_joined'))
    assert zt_mapping.hydrate_positions(lambda: fake, _cfg(), 'events', ms) == 1  # retried
    assert zt_mapping.hydrate_positions(lambda: fake, _cfg(), 'events', ms) == 0  # memoized
    assert fake.query.call_count == 1


def test_restart_then_panel_sync_maps_room(client, sync_env):
    """The 2026-09-30 incident: server restarted, memory empty, panel opens.
    With rehydration the panel can still map the room from BigQuery-restored
    positions (2 real people, stable for 5 minutes)."""
    ms = sync_env.ms                                  # fresh, empty memory
    fake = _bq_rows(_row('Ravi Kumar', 'ravi@x.com', 'breakout_room_joined'),
                    _row('Priya S', 'priya@x.com', 'breakout_room_joined'))
    assert sync_env.hydrate_positions(lambda: fake, _cfg(), 'events', ms) == 2
    _sync(client, [{'name': 'Ravi Kumar', 'email': 'ravi@x.com'},
                   {'name': 'Priya S', 'email': 'priya@x.com'}])
    assert ms.uuid_to_name.get(WU) == 'Sales Team'


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
