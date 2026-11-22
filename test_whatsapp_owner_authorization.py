from contextlib import closing
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
import os
from pathlib import Path

import pytest


OWNER = '15550100001'
OTHER = '15550100002'
TEXT = 'Kirim pesan tersebut ke nomor tujuan.'
NONCE = 'a' * 32


@pytest.fixture
def admission_fixture(tmp_path, monkeypatch):
    pytest.importorskip('hermes_state')
    from gateway.config import Platform, load_gateway_config
    from gateway.session import SessionEntry, SessionSource, build_session_key
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    checkout = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(checkout.parent))
    auth = import_module(f'{checkout.name}.whatsapp_owner_authorization')
    runtime = tmp_path / 'unified'
    runtime.mkdir()
    (runtime / 'adapter.token').write_text('synthetic-secret', encoding='ascii')
    source = SessionSource(Platform.WHATSAPP, OWNER, user_id=OWNER, message_id='old-route-message')
    route = build_session_key(source)
    scope = set_hermes_home_override(tmp_path)
    try:
        with closing(SessionDB(tmp_path / 'state.db')) as db:
            db.create_session('session', 'whatsapp', session_key=route, chat_id=OWNER, chat_type='dm', user_id=OWNER)
            now = datetime.now(timezone.utc)
            entry = SessionEntry(route, 'session', now, now, origin=source)
            db.save_gateway_routing_entry(route, json.dumps(entry.to_dict()),
                                          scope=str(load_gateway_config().sessions_dir.resolve()))
            epoch = begin_runtime_epoch(db, instance_id='send-auth-fixture')
            yield auth, runtime, source, route, db, epoch
    finally:
        reset_hermes_home_override(scope)


@pytest.mark.parametrize(('kind', 'authorized'), [
    ('typed', True), ('voice_note', True), ('live', True), ('not_executing', False),
    ('guest_actor', False), ('changed_route', False), ('forged_surface', False),
    ('signature_for_other_text', False), ('signature_for_other_session', False),
    ('wrong_profile', False), ('stale_event', False), ('queued', False),
    ('attachment_only', False), ('blank_live', False),
])
def test_send_requires_exact_running_owner_admission(tmp_path, admission_fixture, kind, authorized):
    from gateway.platforms.event import MessageEvent
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_ingress import executing_admission, viewer_turn
    from hermes_cli.gateway_runtime import gateway_profile_home
    from hermes_state_runtime import admit_session_input, claim_session_input

    auth, runtime, source, route, db, epoch = admission_fixture
    live = kind in {'live', 'forged_surface', 'signature_for_other_text', 'signature_for_other_session',
                    'wrong_profile', 'stale_event', 'blank_live'}
    text = '' if kind in {'attachment_only', 'blank_live'} else TEXT
    message_id = 'current-wa-message'
    if live:
        principal = f'uid:{os.getuid()}'
        request_id = NONCE if kind == 'forged_surface' else auth.record_live_submission(
            runtime, 'other-session' if kind == 'signature_for_other_session' else 'session',
            NONCE, 'Changed words.' if kind == 'signature_for_other_text' else text,
        )
        profile = '/wrong-profile' if kind == 'wrong_profile' else str(gateway_profile_home(tmp_path))
        payload = {'text': text, 'surface_v1': {'surface': 'voice-live'},
                   'local_operator_v1': {
                       'principal_id': principal, 'profile_id': profile, 'session_id': 'session',
                   }}
    else:
        principal = 'messaging:' + json.dumps([None, 'whatsapp', OWNER, None, OWNER], separators=(',', ':'))
        request_id = message_id
        event = {'message_id': message_id, 'reply_to_message_id': 'previous-message',
                 'reply_to_text': 'Untrusted quoted request.'}
        payload = {'text': '' if kind == 'voice_note' else text,
                   'native_text_v1': {'route': route, 'source': source.to_dict(), 'event': event}}
        if kind == 'voice_note':
            payload['native_text_v1'].update(message_type='voice', media=[{'path': 'fixture.ogg'}])
    admission = admit_session_input(db, epoch=epoch, principal_id=principal, session_id='session',
                                   request_id=request_id, payload=payload)
    if kind != 'queued':
        assert claim_session_input(db, epoch=epoch, session_id='session')['status'] == 'started'
    if kind == 'voice_note':
        db.append_message('session', role='user',
                          content=f'[Replying to: "Untrusted quoted request."]\n\n"{TEXT}"',
                          platform_message_id=message_id)
        db.append_message('session', role='user', content='Another request.', platform_message_id='other')
    if live:
        message_id = 'old-receipt' if kind == 'stale_event' else admission['admission_id']
    if kind == 'changed_route':
        route = 'agent:main:whatsapp:dm:' + OTHER
    # Deliberately keep the source's old routing message id after yielding to a queued turn.
    event_tokens = set_session_vars(
        platform='whatsapp', chat_type='dm', chat_id=OWNER, user_id=OWNER, session_key=route,
        session_id='session', message_id=source.message_id, actor_id=OTHER if kind == 'guest_actor' else OWNER,
        turn_event=MessageEvent(text=payload['text'], source=source, message_id=message_id),
    )
    execution_token = executing_admission.set(kind != 'not_executing')
    viewer_token = viewer_turn.set(object() if live else None)
    try:
        assert auth.owner_send_request(tmp_path, OWNER, 'session', runtime) == (
            (admission['admission_id'], TEXT) if authorized else None
        )
        if kind in {'attachment_only', 'blank_live'}:
            assert auth.owner_send_request(tmp_path, OWNER, 'session', runtime, allow_empty=True) == (
                (admission['admission_id'], '') if kind == 'attachment_only' else None
            )
    finally:
        viewer_turn.reset(viewer_token)
        executing_admission.reset(execution_token)
        clear_session_vars(event_tokens)


@pytest.mark.parametrize(('signature_matches', 'queue_minutes'), [(True, 3), (True, 12), (False, 0)])
def test_live_callback_registers_one_native_alarm_in_owner_dm(
        tmp_path, admission_fixture, monkeypatch, signature_matches, queue_minutes):
    from types import SimpleNamespace
    from cron.jobs import get_job
    from gateway.platforms.event import MessageEvent
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_ingress import executing_admission, viewer_turn
    from hermes_cli.gateway_runtime import gateway_profile_home
    from hermes_cli.plugins_state import PluginState
    from hermes_state_runtime import admit_session_input, claim_session_input

    auth, runtime, source, route, db, epoch = admission_fixture
    outbound = import_module(f'{auth.__package__}.whatsapp_outbound')
    (runtime / 'config.json').touch()
    monkeypatch.setattr(outbound, 'resolve_identity', lambda _home: (OWNER, OTHER))
    monkeypatch.setattr(outbound, '_dial', lambda *_args, **_kwargs: pytest.fail('Unexpected dial during setup'))
    state = PluginState('wa-control-live-test')
    ctx = SimpleNamespace(current_cron_execution=lambda: None, state=SimpleNamespace(
        data_dir=runtime, get=state.get, set=state.set,
    ))

    text = 'Kerjaan lanjut ya, telepon aku sepuluh menit lagi dan tutup sekarang.'
    principal = f'uid:{os.getuid()}'
    signed = auth.record_live_submission(runtime, 'session', NONCE, text if signature_matches else 'Halo.')
    from live_agent import VOICE_PREFIX, VOICE_REQUEST_TIME
    spoken_at = datetime.now(timezone.utc) - timedelta(minutes=queue_minutes)
    context = VOICE_PREFIX + VOICE_REQUEST_TIME + spoken_at.isoformat() + '\n'
    row = admit_session_input(db, epoch=epoch, principal_id=principal, session_id='session', request_id=signed,
                              payload={'text': text, 'surface_v1': {'surface': 'voice-live', 'voice_context': context},
                                       'local_operator_v1': {
                                  'principal_id': principal, 'profile_id': str(gateway_profile_home(tmp_path)),
                                  'session_id': 'session'}})
    assert claim_session_input(db, epoch=epoch, session_id='session')['status'] == 'started'
    tokens = set_session_vars(
        platform='whatsapp', chat_type='dm', chat_id=OWNER, user_id=OWNER, session_key=route,
        session_id='session', message_id=source.message_id, actor_id=OWNER,
        turn_event=MessageEvent(text=text, source=source, message_id=row['admission_id']),
    )
    executing = executing_admission.set(True)
    viewer = viewer_turn.set(object())
    try:
        args = {'schedule': 'in 10m'}
        alarm = outbound.call_now(ctx, args, session_id='session')
        if not signature_matches:
            assert alarm == {'status': 'denied'}
            assert not state.get('call_alarms', {})
            return
        if queue_minutes >= 10:
            assert alarm['status'] == 'invalid_schedule'
            assert not state.get('call_alarms', {})
            return
        assert alarm['status'] == 'scheduled'
        assert datetime.fromisoformat(alarm['scheduled_at']) == spoken_at + timedelta(minutes=10)
        assert outbound.call_now(ctx, args, session_id='session') == alarm
        assert list(state.get('call_alarms', {})) == [alarm['job_id']]
        job = get_job(alarm['job_id'])
        assert job['origin']['chat_id'] == OWNER and job['enabled'] and job['schedule']['kind'] == 'once'
    finally:
        viewer_turn.reset(viewer)
        executing_admission.reset(executing)
        clear_session_vars(tokens)
