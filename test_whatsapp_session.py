from contextlib import closing
from datetime import datetime, timezone
from enum import Enum
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from whatsapp_session import resolve_owner_session, save_voice_exchange, use_multiplex_routing


OWNER = '15550000001'
OWNER_LID = '123456789012345'
OTHER = '15550000002'
PROFILE = 'work'
KEY = f'agent:{PROFILE}:whatsapp:dm:{OWNER}'


@pytest.fixture
def route(monkeypatch, tmp_path):
    home = tmp_path / 'profiles' / PROFILE
    sessions_dir = home / 'custom-sessions'
    root_scope = str(tmp_path / 'launch-sessions')
    origin = dict(platform='whatsapp', chat_type='dm', chat_id=f'{OWNER_LID}@lid',
                  user_id=OWNER, thread_id=None, profile=PROFILE)
    entry = dict(session_id='current', session_key=KEY, origin=origin,
                 transport_profile=PROFILE, suspended=False, expiry_finalized=False)
    row = dict(id='current', source='whatsapp', chat_type='dm', session_key=KEY,
               chat_id=f'{OWNER}@s.whatsapp.net', user_id=OWNER, thread_id=None,
               profile_name=PROFILE, transport_profile=PROFILE, ended_at=None,
               end_reason=None, archived=False)
    entries = {KEY: entry}
    rows = {'current': row}
    endpoint = SimpleNamespace(control_home=str(tmp_path), instance_id='active-lease')
    store = SimpleNamespace(_routing_home=tmp_path, _routing_scope=lambda: root_scope)
    runner = SimpleNamespace(session_store=store, session_runtime_descriptor={'instance_id': endpoint.instance_id})

    class Platform(Enum):
        WHATSAPP = 'whatsapp'

    def key_for(source, *, profile):
        assert (source.platform, source.chat_id) == (Platform.WHATSAPP, OWNER)
        return f'agent:{profile or "main"}:whatsapp:dm:{OWNER}'

    def canonical(value):
        bare = value.split('@', 1)[0]
        return OWNER if bare == OWNER_LID else bare

    def parse_entry(data):
        stored_origin = data['origin']
        return SimpleNamespace(**{**data, 'origin': SimpleNamespace(
            **{**stored_origin, 'platform': Platform(stored_origin['platform'])})})

    class DB:
        def __init__(self, path, *, read_only):
            assert path in {home / 'state.db', tmp_path / 'state.db'}
            assert read_only is True
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def load_gateway_routing_entries(self, *, scope):
            if self.path == tmp_path / 'state.db':
                assert scope == root_scope
            else:
                assert scope == str(sessions_dir.resolve())
            return {key: json.dumps(value) for key, value in entries.items()}

        def get_session(self, session_id):
            assert self.path == home / 'state.db'
            return rows.get(session_id)

        def get_compression_tip(self, session_id):
            assert session_id == 'current'
            return 'tip'

    for name, module in {
        'gateway.config': SimpleNamespace(Platform=Platform, load_gateway_config=lambda: SimpleNamespace(
            sessions_dir=sessions_dir, multiplex_profiles=False)),
        'gateway.session': SimpleNamespace(SessionEntry=SimpleNamespace(from_dict=parse_entry),
                                          SessionSource=lambda platform, chat_id: SimpleNamespace(
                                              platform=platform, chat_id=chat_id), build_session_key=key_for),
        'gateway.whatsapp_identity': SimpleNamespace(canonical_whatsapp_identifier=canonical),
        'gateway.run_api': SimpleNamespace(_ACTIVE_RUNNER=runner),
        'hermes_constants': SimpleNamespace(get_hermes_home=lambda: home, profile_name_for_home=lambda _: PROFILE),
        'hermes_cli.gateway_runtime': SimpleNamespace(gateway_profile_home=lambda _home: home,
            discover_gateway_endpoint=lambda _home: SimpleNamespace(endpoint=endpoint)),
        'hermes_state': SimpleNamespace(SessionDB=DB),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(home=home, entries=entries, entry=entry, row=row, rows=rows, origin=origin,
                           endpoint=endpoint, runner=runner)


def test_resolver_uses_current_route_after_reset(route):
    assert resolve_owner_session(route.home, OWNER) == 'current'

    route.rows['new'] = {**route.row, 'id': 'new'}
    route.entry['session_id'] = 'new'
    route.row.update(ended_at=1, end_reason='session_reset')
    assert resolve_owner_session(route.home, OWNER) == 'new'


def test_resolver_does_not_fall_back_to_another_route_or_old_row(route):
    route.entries[f'agent:{PROFILE}:whatsapp:dm:{OTHER}'] = route.entries.pop(KEY)
    with pytest.raises(RuntimeError, match='No verified active Hermes WhatsApp'):
        resolve_owner_session(route.home, OWNER)


def test_named_standalone_uses_the_legacy_route_namespace(route):
    route.endpoint.control_home = str(route.home)
    standalone_key = f'agent:main:whatsapp:dm:{OWNER}'
    route.entries[standalone_key] = {**route.entry, 'session_key': standalone_key, 'session_id': 'standalone'}
    route.rows['standalone'] = {**route.row, 'id': 'standalone', 'session_key': standalone_key}
    assert resolve_owner_session(route.home, OWNER) == 'standalone'


def test_named_profile_rejects_a_stale_in_process_gateway(route):
    route.endpoint.instance_id = 'replacement-gateway'
    with pytest.raises(RuntimeError, match='No verified active Hermes WhatsApp'):
        resolve_owner_session(route.home, OWNER)


@pytest.mark.parametrize(('record', 'field', 'value'), [
    ('origin', 'chat_id', OTHER),
    ('origin', 'profile', 'other-profile'),
    ('row', 'transport_profile', 'other-profile'),
    ('row', 'ended_at', 1),
])
def test_resolver_rejects_inconsistent_or_inactive_route(route, record, field, value):
    getattr(route, record)[field] = value
    with pytest.raises(RuntimeError, match='No verified active Hermes WhatsApp'):
        resolve_owner_session(route.home, OWNER)


def test_resolver_follows_compression_only_within_the_same_route(route):
    route.rows['tip'] = {**route.row, 'id': 'tip'}
    route.row.update(ended_at=1, end_reason='compression')
    assert resolve_owner_session(route.home, OWNER) == 'tip'

    route.rows['tip']['chat_id'] = OTHER
    with pytest.raises(RuntimeError, match='No verified active Hermes WhatsApp'):
        resolve_owner_session(route.home, OWNER)


def test_named_multiplex_serializes_reset_with_voice_append(tmp_path, monkeypatch):
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    from gateway.config import Platform
    from gateway.session import SessionEntry, SessionSource

    home = tmp_path / 'profiles' / PROFILE
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    root_path, profile_path = tmp_path / 'state.db', home / 'state.db'
    scope = str(tmp_path / 'launch-sessions')
    source = SessionSource(Platform.WHATSAPP, OWNER, user_id=OWNER, profile=PROFILE)
    now = datetime.now(timezone.utc)

    def route_entry(session_id):
        entry = SessionEntry(session_key=KEY, session_id=session_id, created_at=now, updated_at=now,
                             origin=source, platform=Platform.WHATSAPP, chat_type='dm', transport_profile=PROFILE)
        return json.dumps(entry.to_dict())

    with closing(SessionDB(root_path)) as root, closing(SessionDB(profile_path)) as profile:
        for session_id in ('old', 'new'):
            profile.create_session(session_id, 'whatsapp', chat_id=OWNER, user_id=OWNER, chat_type='dm',
                                   session_key=KEY, profile_name=PROFILE, transport_profile=PROFILE)
        root.save_gateway_routing_entry(KEY, route_entry('old'), scope=scope)
        root.save_gateway_routing_entry(KEY, route_entry('new'), scope=str(tmp_path / 'stale-scope'))
        profile.save_gateway_routing_entry(KEY, route_entry('new'), scope=scope)

        append = SessionDB._append_messages_in_transaction
        observed = []

        def observe_reservation(db, conn, session_id, messages):
            with closing(sqlite3.connect(root_path, timeout=0)) as resetter:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    resetter.execute('BEGIN IMMEDIATE')
            observed.append(session_id)
            return append(db, conn, session_id, messages)

        monkeypatch.setattr(SessionDB, '_append_messages_in_transaction', observe_reservation)

        with use_multiplex_routing(home, tmp_path, scope):
            assert resolve_owner_session(home, OWNER) == 'old'
            args = (home, 'old', 'a' * 32, 'Ini giliran suara.', 'Sudah kucatat.')
            assert save_voice_exchange(*args) == 'saved'
            assert save_voice_exchange(*args) == 'saved'
            root.save_gateway_routing_entry(KEY, route_entry('new'), scope=scope)
            assert resolve_owner_session(home, OWNER) == 'new'
            assert save_voice_exchange(home, 'old', 'b' * 32, 'Terlambat.', 'Ini juga.') == 'session_changed'
            root.save_gateway_routing_entry(KEY, route_entry('old'), scope=scope)
            assert resolve_owner_session(home, OWNER) == 'old'
            assert save_voice_exchange(*args) == 'saved'

        assert observed == ['old']
        assert [row['content'] for row in profile.get_messages('old')] == list(args[-2:])
        assert profile.get_messages('new') == root.get_messages('old') == []
