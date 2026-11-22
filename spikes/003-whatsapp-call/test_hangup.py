"""Natural spoken hangup, local codec and sidecar protocol fixtures only."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from live_agent import _call_intent
from test_general_delegation import (
    acknowledge, delegation, feed_output, local_voice, wait_until,
)


def turn(role, turn_id, text='', *, phase='done'):
    return {'type': f'turn.{phase}', 'turn': {'id': turn_id, 'role': role, 'transcript': text}}


def end_call_events(messages):
    return [call for message in messages
            for call in message.get('toolCall', {}).get('functionCalls', [])]


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
        assert 'no other work' in kwargs['instructions']
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


@pytest.mark.parametrize('interruption', ['before_farewell', 'after_farewell', None])
def test_hangup_waits_for_spoken_farewell_and_can_be_cancelled(tmp_path, monkeypatch, interruption):
    async def exercise():
        current = 'Aku rasa cukup untuk hari ini.'
        routed = []

        class Jobs:
            async def route(self, proposal, spoken, previous, loaded_context, *, session_id):
                routed.append(spoken)
                assert proposal == spoken == current
                assert previous == loaded_context == ''
                return {'status': 'end_call'}

            async def save_exchange(self, _session_id, _exchange, *, wait_for_lease=False):
                return {'status': 'saved'}

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data(turn('user', 'caller-1', current, phase='created'))
            voice.connection._data(json.dumps(delegation([current])))
            await wait_until(lambda: bool(appended))
            assert appended[0]['delegation_item_id'] == 'request-1'
            assert appended[0]['channel'] == 'commentary'
            assert not routed
            assert not voice.pending_end_call
            await acknowledge(voice, 'request-1')
            await voice.on_data(turn('user', 'caller-1', current))
            await wait_until(lambda: len(appended) == 2)
            assert appended[1]['channel'] == 'speakable'
            assert routed == [current]
            await voice.on_data(turn('assistant', 'earlier-reply', phase='created'))
            await acknowledge(voice, 'request-1')
            await voice.on_data(turn('assistant', 'earlier-reply', 'Respon yang sudah berjalan.'))
            assert end_call_events(sent) == []

            await voice.on_data(turn('assistant', 'farewell', phase='created'))
            await voice.on_data(turn('assistant', 'farewell', 'Sampai nanti.'))
            assert end_call_events(sent) == []  # transcript can arrive before RTP

            if interruption == 'before_farewell':
                await voice.on_data(turn('user', 'caller-2', phase='created'))
                await feed_output(voice, remote, 1700)
                assert end_call_events(sent) == []
                return

            await feed_output(voice, remote, 1700)
            await wait_until(lambda: bool(end_call_events(sent)))
            assert 'modelTurn' in sent[-2]['serverContent']
            assert end_call_events(sent) == [{'id': 'request-1', 'name': 'end_call', 'args': {}}]
            assert not voice.native_task_turns
            voice.on_sidecar_response({'functionResponses': [
                {'id': 'request-1', 'name': 'end_call', 'response': {'ok': True}},
            ]})
            assert voice.report['assistant_hangup'] == 'accepted'
            await feed_output(voice, remote, 3)
            assert sent[-1].get('toolCall')  # idle frames do not keep the sidecar queue full

            if interruption == 'after_farewell':
                await voice.on_data(turn('user', 'caller-2', phase='created'))
                clear = {'serverContent': {'interrupted': True}}
                assert sent[-1] == clear and sent.count(clear) == 1
                assert voice.report['assistant_hangup'] == 'cancelled'
                await feed_output(voice, remote, 1200)
                assert sent[-1] == clear
                await voice.on_data(turn('user', 'caller-2', 'Bentar, aku mau lanjut.'))
                await voice.on_data(turn('assistant', 'new-reply', phase='created'))
                await feed_output(voice, remote, 1500)
                assert 'modelTurn' in sent[-1]['serverContent']
        finally:
            await voice.close()

    asyncio.run(exercise())
