"""Codex transcript correlation and failure handling with local PCM fixtures."""
import asyncio
import json

import pytest

import call_adapter as adapter
from test_general_delegation import (
    ACTIVE_WHATSAPP_SESSION, acknowledge, audio_messages, delegation, feed_output, local_voice, wait_until,
)


@pytest.mark.parametrize('event', [
    {'type':'turn.created', 'turn':['unexpected']},
    {'type':'turn.created', 'turn':{'id':'user', 'role':'user', 'transcript':123}},
    {'type':'turn.delta', 'turn_id':'user', 'delta':['unexpected']},
])
def test_malformed_transcript_event_fails_closed_without_forwarding_pcm(tmp_path, monkeypatch, event):
    async def exercise():
        native_requests = []
        class Jobs:
            async def submit(self, _submission_id, text):
                native_requests.append(text)
                raise AssertionError('Malformed transcripts must not launch Hermes')
        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            voice.connection._data(json.dumps({'type':'turn.created', 'turn':{
                'id':'user', 'role':'user', 'transcript':''}}))
            voice.connection._data(json.dumps(event))
            await asyncio.wait_for(voice.failed.wait(), 1)
            # Buffered provider events after the fatal event must not admit work.
            voice.connection._data(json.dumps({'type':'turn.done', 'turn':{
                'id':'late-user', 'role':'user', 'transcript':'coba tugas catatan uji'}}))
            voice.connection._data(json.dumps(delegation(['coba tugas catatan uji'], item_id='late-provider', turn_id='late-user')))
            await feed_output(voice, remote, 2000)
            assert audio_messages(sent) == []
            assert native_requests == appended == []
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize(('terminal', 'expected'), [
    ('kode uji?', 'Berapa kode uji?'),
    ('Berapa kode uji?', 'Berapa kode uji?'),
    ('Kapan pesan uji dikirim?', 'Kapan pesan uji dikirim?'),
])
def test_confirmed_transcript_uses_only_its_owned_turn_fragments(tmp_path, monkeypatch, terminal, expected):
    async def exercise():
        routed, saved = [], []

        class Jobs:
            async def route(self, proposal, current, _previous, _loaded, *, session_id):
                routed.append((proposal, current))
                return {'status': 'reply'}

            async def save_exchange(self, _session_id, exchange, **_):
                saved.append((exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, _, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}})

        async def delta(turn_id, text):
            await voice.on_data({'type': 'turn.delta', 'turn_id': turn_id, 'delta': text})

        try:
            await turn('turn.created', 'user', 'caller-1', 'Berapa')
            await voice.on_data(delegation(['Buat tugas yang tidak diminta.']))
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            await voice.on_data({'type': 'input_transcript.added', 'item': {'id': 'item-2', 'text': 'kode'}})
            await delta('caller-1', 'kode')
            await delta('unknown', 'kata yang tidak berkaitan')
            await turn('turn.done', 'assistant', 'caller-1', 'Jawaban tidak berkaitan.')
            await delta('caller-1', 'uji')
            await delta('caller-1', '?')
            await turn('turn.done', 'user', 'caller-1')
            assert not routed

            await turn('turn.done', 'user', 'caller-1', terminal)
            await wait_until(lambda: len(appended) == 2)
            await acknowledge(voice, 'request-1')
            await delta('caller-1', 'Ucapan terlambat.')
            await turn('turn.done', 'user', 'caller-1', 'Salinan penyelesaian.')
            await voice.on_data(delegation(['Duplikat'], item_id='duplicate'))
            await wait_until(lambda: len(appended) == 3)
            await acknowledge(voice, 'duplicate')
            await turn('turn.created', 'assistant', 'answer-1')
            await turn('turn.done', 'assistant', 'answer-1', 'Ada di chat.')
            await wait_until(lambda: bool(saved))
        finally:
            await voice.close()

        assert routed == [('Buat tugas yang tidak diminta.', expected)]
        assert saved == [(expected, 'Ada di chat.')]
        assert [entry['serverContent']['inputTranscription']['text'] for entry in sent
                if 'inputTranscription' in entry['serverContent']] == [expected]
        assert [entry['serverContent']['outputTranscription']['text'] for entry in sent
                if 'outputTranscription' in entry['serverContent']] == ['Ada di chat.']

    asyncio.run(exercise())


def test_completed_assistant_turn_does_not_reopen_or_emit_old_transcript(tmp_path, monkeypatch):
    async def exercise():
        voice, _, sent, _ = local_voice(tmp_path, monkeypatch)

        async def turn(kind, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'role': 'assistant', 'id': turn_id, 'transcript': text}})

        try:
            await turn('turn.created', 'first', 'Ada')
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'first', 'delta': 'di chat.'})
            await turn('turn.done', 'first', 'di chat.')
            await turn('turn.created', 'second')
            await turn('turn.created', 'first')
            await turn('turn.done', 'first', 'Salinan lama.')
            assert voice.active_reply_turn == 'second'
            await turn('turn.done', 'second', 'Jawaban baru.')
        finally:
            await voice.close()

        assert [entry['serverContent']['outputTranscription']['text'] for entry in sent] == [
            'Ada di chat.', 'Jawaban baru.',
        ]

    asyncio.run(exercise())


def test_reply_is_saved_when_the_matching_caller_transcript_finishes_later(tmp_path, monkeypatch):
    async def exercise():
        saved = []

        class Jobs:
            async def save_exchange(self, session_id, exchange, **_):
                saved.append((session_id, exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}})

        try:
            await turn('turn.created', 'user', 'caller-1', 'Judulnya')
            await turn('turn.created', 'assistant', 'answer-1')
            await turn('turn.done', 'assistant', 'answer-1', 'Aku ingat judulnya.')
            assert not voice.completed_exchanges and not saved
            await turn('turn.done', 'user', 'caller-1', 'Judulnya Laut Biru.')
            await turn('turn.done', 'user', 'caller-1', 'Judulnya Laut Biru.')
            await turn('turn.done', 'assistant', 'answer-1', 'Aku ingat judulnya.')
            await wait_until(lambda: bool(saved))
        finally:
            await voice.close()

        assert saved == [(ACTIVE_WHATSAPP_SESSION, 'Judulnya Laut Biru.', 'Aku ingat judulnya.')]

    asyncio.run(exercise())


@pytest.mark.parametrize(('after_wait', 'native_task'), [(False, False), (True, False), (False, True)])
def test_late_caller_final_preserves_its_completed_reply(tmp_path, monkeypatch, after_wait, native_task):
    if after_wait:
        monkeypatch.setattr(adapter, 'POST_ACK_TRANSCRIPT_WAIT', .01)

    async def exercise():
        saved, submitted = [], []

        class Jobs:
            async def save_exchange(self, session_id, exchange, **_):
                saved.append((session_id, exchange))
                return {'status': 'saved'}

            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id, **_):
                submitted.append((session_id, text))
                return {'status': 'unknown'}

        voice, _, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}})

        try:
            await turn('turn.created', 'user', 'caller-1', 'Judulnya')
            if native_task:
                await voice.on_data(delegation(['Catat judulnya.'], item_id='older-job', turn_id='caller-1'))
                await wait_until(lambda: len(appended) == 1)
                await acknowledge(voice, 'older-job')
            await turn('turn.created', 'assistant', 'answer-1')
            await turn('turn.done', 'assistant', 'answer-1', 'Aku ingat judulnya.')
            await turn('turn.created', 'assistant', 'answer-1-continued')
            await turn('turn.done', 'assistant', 'answer-1-continued', 'Aku juga mengingat lanjutannya.')
            await turn('turn.done', 'user', 'caller-2', 'Tokohnya Nara.')
            await turn('turn.created', 'assistant', 'answer-2')
            await turn('turn.done', 'assistant', 'answer-2', 'Aku juga ingat tokohnya.')
            await asyncio.sleep(0)
            if after_wait:
                await wait_until(lambda: len(saved) == 1)
                assert saved[0][1]['user'] == 'Tokohnya Nara.'
            else:
                assert not saved
                assert await voice.flush_history() == 'busy'
            assert any(message['serverContent'].get('outputTranscription', {}).get('text')
                       == 'Aku juga ingat tokohnya.' for message in sent)

            await turn('turn.done', 'user', 'caller-1', 'Judulnya Laut Biru.')
            await turn('turn.done', 'user', 'caller-1', 'Salinan transkrip lama.')
            await turn('turn.done', 'assistant', 'answer-1', 'Salinan jawaban lama.')
            await wait_until(lambda: len(saved) == (1 if native_task else 3))
        finally:
            await voice.close()

        assert [session_id for session_id, _ in saved] == [ACTIVE_WHATSAPP_SESSION] * len(saved)
        if native_task:
            assert submitted == [(ACTIVE_WHATSAPP_SESSION, 'Judulnya Laut Biru.')]
            assert saved[0][1]['user'] == 'Tokohnya Nara.'
            return
        first, continued, second = [exchange for _, exchange in (saved[1:] + saved[:1] if after_wait else saved)]
        assert (first['user'], first['assistant']) == ('Judulnya Laut Biru.', 'Aku ingat judulnya.')
        assert (continued['user'], continued['assistant']) == ('', 'Aku juga mengingat lanjutannya.')
        assert (second['user'], second['assistant']) == ('Tokohnya Nara.', 'Aku juga ingat tokohnya.')
        assert (first['user_at_ms'] <= first['assistant_at_ms'] <= continued['assistant_at_ms']
                <= second['user_at_ms'] <= second['assistant_at_ms'])
        assert len({item['exchange_id'] for item in (first, continued, second)}) == 3

    asyncio.run(exercise())
