"""Natural spoken hangup, local codec and sidecar protocol fixtures only."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import call_adapter as adapter
from live_agent import _call_intent
from test_general_delegation import (
    DM_ADMISSION_ID, TranscriptJobs, acknowledge, delegation, feed_output, local_voice, wait_until,
)


def turn(role, turn_id, text='', *, phase='done'):
    return {'type': f'turn.{phase}', 'turn': {'id': turn_id, 'role': role, 'transcript': text}}


def end_call_events(messages):
    return [message for message in messages if message.get('ownerCallEnd') is True]


def test_dm_reconnect_keeps_caller_and_reply_audio_running(tmp_path, monkeypatch):
    async def exercise():
        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch)
        try:
            voice.chat_reconnecting = True
            voice.feed(b'\x01\x00' * 320)
            assert voice.report['input_bytes'] == 640
            await feed_output(voice, remote, 2000)
            await wait_until(lambda: bool(sent))
            assert voice.report['suppressed_output_bytes'] == 0
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(('decision', 'expected'), [
    ({'action': 'end_call'}, 'end_call'),
    ({'action': 'reply'}, 'reply'),
    ({'action': True}, 'unavailable'),
    ({'action': 'end_call', 'extra': True}, 'unavailable'),
    (None, 'unavailable'),
])
def test_hangup_intent_uses_structured_hermes_result(decision, expected):
    utterance = 'Makasih, sampai sini aja ya.'
    previous = 'Tadi kamu sudah jawab pertanyaanku.'

    async def classify(**kwargs):
        assert kwargs['json_mode'] is True
        assert json.loads(kwargs['input'][0]['text']) == {
            'current': utterance, 'previous': previous, 'proposal': utterance, 'loaded_context': '',
        }
        assert 'already accepted' in kwargs['instructions']
        assert not {'provider', 'model'} & kwargs.keys()
        return SimpleNamespace(parsed=decision)

    result = asyncio.run(_call_intent(SimpleNamespace(acomplete_structured=classify), utterance,
                                     utterance, previous, ''))
    assert result == {'status': expected}


def test_hangup_intent_provider_failure_cannot_authorize_actions():
    async def classify(**_kwargs):
        raise RuntimeError('Fixture provider unavailable')

    result = asyncio.run(_call_intent(SimpleNamespace(acomplete_structured=classify),
                                     'Tolong urus ini.', 'Tolong urus ini.', '', ''))
    assert result == {'status': 'unavailable'}


@pytest.mark.parametrize(('utterance', 'config', 'expected'), [
    ("Hey Hermes, that's all!", None, True),
    ("Okay, stop listening.", ['stop'], True),
    ("That’s all.", None, True),
    ("Ya udah matikan teleponnya.", None, True),
    ("Hermes, tutup telepon.", None, True),
    ("Yaudah matikan, kenapa masih nyala? Matikan teleponnya", None, False),
    ("Tolong jangan tutup telepon.", None, False),
    ("Don't hang up", None, False),
    ("How do I stop a running process?", None, False),
    ("Tutup aplikasi telepon", None, False),
    ("Hermes", None, False),
    ("“Selesai, Hermes.”", ["selesai Hermes"], True),
    ("«ОТБОЙ!»", "отбой", True),
    ("bye", ["selesai"], False),
    ("stop", [], False),
    ("matikan teleponnya", [], False),
    ("end the conversation", {}, True),
])
def test_whole_utterance_stop_matches_desktop_and_profile(utterance, config, expected):
    from voice_stop import stop_phrases, is_stop_command

    assert is_stop_command(utterance, stop_phrases(config)) is expected


@pytest.mark.parametrize('current', ['Ya udah matikan teleponnya.', 'Hang up.'])
def test_spoken_stop_ends_once_without_a_provider_proposal_or_farewell(tmp_path, monkeypatch, current):
    async def exercise():
        voice, _, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        voice.call_work.add(DM_ADMISSION_ID)
        try:
            voice.feed(b'\x00\x01' * 320)
            await voice.on_data(turn('user', 'caller-1', current, phase='created'))
            assert not end_call_events(sent)
            await voice.on_data(turn('user', 'caller-1', current))
            assert end_call_events(sent) == [{'ownerCallEnd': True}]
            await voice.on_data(turn('user', 'caller-1', current))
            voice.feed(b'\x00\x01' * 320)
            await voice.on_data(turn('user', 'caller-2', 'Tutup teleponnya.'))
            await voice.on_data({'type': 'turn.interrupted'})
            await voice.output_audio(bytes(640))
            assert sent[-1] == {'ownerCallEnd': True}
            assert len(end_call_events(sent)) == 1
            assert voice.call_work == {DM_ADMISSION_ID}
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_closed_transport_after_owner_stop_ends_live_and_keeps_native_work(tmp_path, monkeypatch):
    async def exercise():
        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())

        async def disconnect(message):
            if message.get('ownerCallEnd'):
                raise ConnectionResetError('fixture native line disconnected')

        monkeypatch.setattr(voice, 'send', disconnect)
        voice.call_work.add(DM_ADMISSION_ID)
        try:
            voice.feed(bytes([0, 1]) * 320)
            await voice.on_data(turn('user', 'owner', 'End this call.'))
            assert voice.failed.is_set()
            assert voice.report['assistant_hangup'] == 'transport_disconnected'
            assert voice.call_work == {DM_ADMISSION_ID}
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('origin', ['silent', 'assistant', 'history', 'prior-call'])
def test_stop_requires_live_authenticated_caller_evidence(tmp_path, monkeypatch, origin):
    async def exercise():
        voice, _, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        try:
            if origin != 'silent':
                voice.feed(b'\x00\x01' * 320)
            if origin == 'prior-call':
                voice.session_changed()
            role = 'assistant' if origin == 'assistant' else 'user'
            await voice.on_data(turn(role, 'turn-1', 'Stop.'), history_only=origin == 'history')
            assert not end_call_events(sent)
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_verified_semantic_hangup_ends_once_during_new_speech(tmp_path, monkeypatch):
    async def exercise():
        class Jobs(TranscriptJobs):
            async def route(self, *_args, **_kwargs):
                return {'status': 'end_call'}

        voice, _, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        request = 'Aku rasa pembicaraan kita sampai di sini dulu.'
        try:
            voice.feed(b'\x00\x01' * 320)
            voice.feed(bytes([0, 1]) * 320)
            await voice.on_data(turn('user', 'caller-1', request))
            await voice.on_data(delegation([request]))
            await wait_until(lambda: bool(end_call_events(sent)))
            await voice.on_data(turn('user', 'caller-2', phase='created'))
            await voice.on_data(turn('user', 'caller-2', 'Iya, tutup.'))
            assert end_call_events(sent) == [{'ownerCallEnd': True}]
            assert voice.report['assistant_hangup'] == 'requested'
        finally:
            await voice.close()

    asyncio.run(exercise())

@pytest.mark.parametrize(('words', 'custom', 'close'), [
    ('Stop.', None, False),
    ('Hey Hermes, cancel.', None, False),
    ('Never mind.', None, False),
    ('Stop it.', None, False),
    ('Batalkan pekerjaan itu.', None, False),
    ('Tutup teleponnya.', None, True),
    ('Please hang up.', None, True),
    ('Cancel.', ['cancel'], True),
])
@pytest.mark.parametrize('working', ['admitted', 'pending'])
def test_bare_stop_with_work_keeps_native_intent_available(tmp_path, monkeypatch, words, custom, close, working):
    from voice_stop import stop_phrases

    async def exercise():
        routed = []

        class Jobs(TranscriptJobs):
            async def route(self, _proposal, request, *_args, **_kwargs):
                routed.append(request)
                return {'status': 'reply'}

        voice, _, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            if working == 'admitted':
                voice.dm_work = [{'admission_id': DM_ADMISSION_ID, 'status': 'started', 'generation': 0}]
            else:
                voice.routing_turns['prior'] = asyncio.current_task()
            voice.stop_phrases = stop_phrases(custom)
            voice.feed(bytes([0, 1]) * 320)
            await voice.on_data(turn('user', 'caller-1', words))
            if not close:
                await wait_until(lambda: routed == [words])
            assert bool(end_call_events(sent)) is close
            assert (not routed) is close
        finally:
            voice.routing_turns.pop('prior', None)
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('origin', ['unheard', 'withdrawn', 'custom', 'disabled'])
def test_semantic_close_needs_settled_current_owner_request(tmp_path, monkeypatch, origin):
    from voice_stop import stop_phrases

    async def exercise():
        voice, _, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        request = 'Okay, let’s finish up here.'
        try:
            if origin != 'unheard':
                voice.feed(bytes([0, 1]) * 320)
            await voice.on_data(turn('user', 'caller-1', request))
            if origin == 'withdrawn':
                voice.feed(bytes([0, 1]) * 320)
                await voice.on_data(turn('user', 'caller-2', 'Actually, keep talking.'))
            if origin in ('custom', 'disabled'):
                voice.stop_phrases = stop_phrases([] if origin == 'disabled' else ['beres'])
            await voice.request_hangup(None, 'caller-1', request, {'status': 'end_call'})
            assert not end_call_events(sent)
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('work,close,expected', [
    ('telepon aku besok jam sembilan', 'tutup sekarang', 'end_call'),
    ('hapus berkasnya', 'tutup sekarang', 'reply'),
    ('telepon aku besok jam sembilan', 'akhiri saja', 'reply'),
    ('tutup sekarang', 'tutup sekarang', 'reply'),
])
def test_combined_hangup_binds_both_parts_to_current_words(work, close, expected):
    current = 'Lanjutkan render yang sudah jalan, telepon aku besok jam sembilan, lalu tutup sekarang.'
    async def classify(**_kwargs):
        return SimpleNamespace(parsed={'action': 'end_call', 'new_work': work, 'end_evidence': close})

    result = asyncio.run(_call_intent(SimpleNamespace(acomplete_structured=classify),
                                     'hapus berkasnya', current, 'akhiri saja', ''))
    assert result == ({'status': expected, 'new_work': work} if expected == 'end_call' else {'status': expected})


@pytest.mark.parametrize('outcome', ['queued', 'unavailable', 'interrupted'])
def test_combined_callback_is_admitted_once_before_hangup(tmp_path, monkeypatch, outcome):
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        request = 'Lanjutkan render, telepon aku besok jam sembilan, lalu tutup sekarang.'
        callback = 'telepon aku besok jam sembilan'
        callback_id = 'b' * 32
        submissions = []

        class Jobs(TranscriptJobs):
            async def route(self, *_args, **_kwargs):
                return {'status': 'end_call', 'new_work': callback}

            async def submit(self, _submission_id, words, *, session_id, voice_context, spoken_at_ms=None):
                submissions.append((words, session_id, voice_context))
                started.set()
                await release.wait()
                if outcome == 'unavailable':
                    return {'status': 'unavailable'}
                return {'status': 'queued', 'receipt': {'admission_id': callback_id, 'session_id': session_id}}

        voice, _, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.call_work.add(DM_ADMISSION_ID)
        try:
            voice.feed(bytes([0, 1]) * 320)
            await voice.on_data(turn('user', 'caller-1', request))
            await voice.on_data(delegation([request]))
            await asyncio.wait_for(started.wait(), 1)
            assert end_call_events(sent) == []
            await voice.on_data(delegation([request]))
            if outcome == 'interrupted':
                await voice.on_data(turn('user', 'caller-2', phase='created'))
            elif outcome == 'unavailable':
                await voice.on_data(turn('assistant', 'early', phase='created'))
                await voice.on_data(turn('assistant', 'early', 'Aku urus dulu.'))
            release.set()
            await wait_until(lambda: bool(end_call_events(sent)))
            assert len(submissions) == 1
            assert submissions[0][:2] == (request, voice.hermes_session_id)
            assert callback in submissions[0][2]
            assert DM_ADMISSION_ID in voice.call_work
            assert (callback_id in voice.call_work) == (outcome != 'unavailable')
            assert end_call_events(sent) == [{'ownerCallEnd': True}]
        finally:
            release.set()
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('proposal', [False, True], ids=['without-provider-proposal', 'provider-acknowledged'])
def test_desktop_settled_stop_uses_caller_fragments_when_codex_omits_final(tmp_path, monkeypatch, proposal):
    async def exercise():
        monkeypatch.setattr(adapter, 'UTTERANCE_SETTLE_SECONDS', 0.03, raising=False)
        voice, _, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        try:
            voice.feed(bytes([0, 1]) * 320)
            await voice.on_data(turn('user', 'caller-1', 'Hermes,', phase='created'))
            for part in ('hang up the', 'phone'):
                await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': part})
            if proposal:
                await voice.on_data(delegation(['Make a picture instead']))
                await wait_until(lambda: appended)
                await acknowledge(voice, 'request-1')
            assert not end_call_events(sent)
            await wait_until(lambda: end_call_events(sent))
            assert end_call_events(sent) == [{'ownerCallEnd': True}]
            assert not voice.native_task_turns and not voice.transcripts_done['caller-1'].is_set()
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('boundary', ['extension', 'final-correction', 'new-utterance', 'new-peer', 'unheard', 'proposal-only'])
def test_unsettled_stop_cannot_close_another_utterance_or_peer(tmp_path, monkeypatch, boundary):
    async def exercise():
        monkeypatch.setattr(adapter, 'UTTERANCE_SETTLE_SECONDS', 0.03, raising=False)
        voice, _, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        try:
            voice.connection.connected.set()
            voice.feed(bytes([0, 1]) * 320 if boundary != 'unheard' else bytes(640))
            text = 'Halo.' if boundary == 'proposal-only' else 'Hang up.'
            await voice.on_data(turn('user', 'caller-1', text, phase='created'))
            if boundary == 'extension':
                await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': 'the picture frame.'})
            elif boundary == 'final-correction':
                await voice.on_data(turn('user', 'caller-1', 'Never hang up.'))
            elif boundary == 'new-utterance':
                await voice.on_data(turn('user', 'caller-2', 'Actually wait.', phase='created'))
            elif boundary == 'new-peer':
                old = voice.audio
                voice.install_audio(adapter.AudioConnection(voice, adapter.CodexConnection))
                voice.retire_audio(old)
            elif boundary == 'proposal-only':
                await voice.on_data(delegation(['Hang up the phone.']))
                await wait_until(lambda: appended)
                await acknowledge(voice, 'request-1')
            await asyncio.sleep(0.06)
            assert not end_call_events(sent) and not voice.native_task_turns
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('final', [False, True], ids=['steady-caller-fragments', 'corrected-provider-final'])
def test_provisional_first_word_then_owner_audio_and_native_ack(tmp_path, monkeypatch, final):
    async def exercise():
        monkeypatch.setattr(adapter, 'UTTERANCE_SETTLE_SECONDS', 0.03)
        voice, _, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        try:
            voice.connection.connected.set()
            await voice.on_data(turn('user', 'caller-1', 'Hermes,', phase='created'))
            voice.feed(bytes([0, 1]) * 320)
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': 'hang up the phone.'})
            await voice.on_data(delegation(['Schedule a call for tomorrow.']))
            await wait_until(lambda: appended)
            await acknowledge(voice, 'request-1')
            if final:
                await voice.on_data(turn('user', 'caller-1', 'Hermes, do not hang up.'))
                await asyncio.sleep(0.06)
                assert not end_call_events(sent)
            else:
                await wait_until(lambda: end_call_events(sent))
            assert not voice.native_task_turns
        finally:
            await voice.close()
    asyncio.run(exercise())
