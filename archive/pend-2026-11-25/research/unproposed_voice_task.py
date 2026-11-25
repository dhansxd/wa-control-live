"""Caller requests and direct spoken replies on a Codex connection without a tool call."""
import asyncio
import struct

import pytest

from test_general_delegation import (
    ACTIVE_WHATSAPP_SESSION, acknowledge, delegation, feed_output, local_voice, wait_until,
)


PHOTO_REQUEST = 'Tolong buatkan foto kucing memakai jas hujan kuning lalu kirim ke WhatsApp ini.'
PHOTO_REPLY = 'Aku akan buat fotonya dan kirim ke chat ini.'
CALLER_AUDIO = struct.pack('<h', 250) * 320


class NativeBoundary:
    def __init__(self):
        self.routed = []
        self.submitted = []
        self.saved = []

    async def route(self, proposal, current, previous, loaded_context, *, session_id):
        self.routed.append((session_id, current))
        return {'status': 'task' if current == PHOTO_REQUEST else 'reply'}

    async def submit(self, submission_id, text, *, session_id, spoken_at_ms=None):
        self.submitted.append((session_id, text, submission_id))
        # A gateway can lose the receipt. No outcome or delivery may be assumed.
        return {'status': 'unknown'}

    async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
        self.saved.append((session_id, exchange['user'], exchange['assistant']))
        return {'status': 'saved'}


async def turn(voice, kind, role, turn_id, transcript=''):
    await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': transcript}})


@pytest.mark.parametrize('caller_final_first', [True, False], ids=['caller-final-first', 'reply-final-first'])
def test_photo_request_without_provider_delegation_reaches_native_dm_once(
    tmp_path, monkeypatch, caller_final_first,
):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.created', 'user', 'caller-1')
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': 'Tolong buatkan foto'})
            if caller_final_first:
                await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            if not caller_final_first:
                assert not jobs.submitted
                await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)

            await wait_until(lambda: jobs.submitted or jobs.saved)
            assert jobs.routed == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST)]
            assert [(sid, text) for sid, text, _ in jobs.submitted] == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST)]
            assert not jobs.saved
            assert not [event for event in appended if event['type'] == 'delegation.context.append']

            # A retried final or late, multipart tool proposal belongs to the same caller turn.
            await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            late = delegation(['Buatkan foto', ' dan kirim ke chat.'], item_id='provider-late')
            await voice.on_data(late)
            await voice.on_data(late)
            await wait_until(lambda: any(event.get('delegation_item_id') == 'provider-late' for event in appended))
            await acknowledge(voice, 'provider-late')
        finally:
            await voice.close()
        assert len(jobs.routed) == len(jobs.submitted) == 1
        assert not jobs.saved

    asyncio.run(exercise())


@pytest.mark.parametrize(('caller_text', 'response'), [
    ('Apa kabar hari ini?', 'Baik, terima kasih.'),
    ('Kalau nanti aku minta foto, kamu bisa bikinkan?', 'Aku akan buat fotonya.'),
    ('Jangan buat foto. Aku cuma ingin membahas idenya.', 'Aku akan buat fotonya.'),
])
def test_direct_reply_to_conversation_does_not_admit_work(tmp_path, monkeypatch, caller_text, response):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-1', caller_text)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', response)
            await wait_until(lambda: jobs.saved or jobs.submitted)
        finally:
            await voice.close()
        assert jobs.saved == [(ACTIVE_WHATSAPP_SESSION, caller_text, response)]
        assert not jobs.submitted

    asyncio.run(exercise())


@pytest.mark.parametrize('missing', ['caller-audio', 'caller-final'])
def test_provider_promise_alone_cannot_admit_work(tmp_path, monkeypatch, missing):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            if missing == 'caller-final':
                voice.feed(CALLER_AUDIO)
            else:
                voice.feed(bytes(len(CALLER_AUDIO)))
            await turn(voice, 'turn.created', 'user', 'caller-1', PHOTO_REQUEST)
            if missing != 'caller-final':
                await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            await asyncio.sleep(0)
        finally:
            await voice.close()
        assert not jobs.submitted

    asyncio.run(exercise())
