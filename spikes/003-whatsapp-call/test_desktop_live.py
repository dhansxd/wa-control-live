"""Behavior shared with the Hermes Desktop Live conversation."""
import asyncio
import pytest

from test_general_delegation import ACTIVE_WHATSAPP_SESSION, TranscriptJobs, feed_output, local_voice
from test_voice import start_phone


def test_desktop_voice_policy_and_profile_instructions_reach_whatsapp(tmp_path, monkeypatch):
    voice_live = pytest.importorskip('tools.voice_live')
    from desktop_live import DESKTOP_PERSONA
    assert DESKTOP_PERSONA == voice_live.live_instructions({})
    monkeypatch.setattr(voice_live, '_live_section', lambda: {'instructions': 'Profil uji dwibahasa.'})
    requests = asyncio.run(start_phone(tmp_path, monkeypatch))
    params = next(params for method, params in requests if method == 'thread/realtime/start')
    assert voice_live.live_instructions() in params['initialItems'][0]['text']


@pytest.mark.parametrize('played', [True, False], ids=['heard-reply', 'transcript-without-audio'])
def test_admission_uses_the_conversation_at_the_original_caller_turn(tmp_path, monkeypatch, played):
    from test_unproposed_voice_task import CALLER_AUDIO, turn

    async def scenario():
        submitted = []
        class Jobs(TranscriptJobs):
            async def submit(self, _key, text, *, session_id, **fields):
                submitted.append((session_id, text, fields.get('voice_context')))
                return {'status': 'unavailable'}

        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        async def caller(turn_id, words):
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', turn_id, words)

        try:
            await caller('u1', 'Warna favoritku hijau.')
            await turn(voice, 'turn.created', 'assistant', 'a1')
            if played:
                await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'a1', 'Hijau sage?')
            request = 'Iya, buat gambar dengan warna itu.'
            await caller('u2', request)
            await caller('u3', 'Aku tunggu.')
            await voice.submit_task(None, 'u2', request)
            context = ('User: Warna favoritku hijau.\nVoice assistant: Hijau sage?\nUser: ' if played
                       else 'User: Warna favoritku hijau. ')
            assert submitted == [(ACTIVE_WHATSAPP_SESSION, request, context + request)]
        finally:
            await voice.close()

    asyncio.run(scenario())


def test_desktop_window_expires_old_speech_and_caps_fragment_count():
    from desktop_live import SpokenConversation

    history = SpokenConversation()
    history.remember('first', 'user', 'Lama sekali.', 1)
    history.remember('a', 'assistant', 'Earlier today.', 300_003)
    history.remember('current', 'user', 'Now please.', 300_004)
    assert history.context_for('current') == 'Voice assistant: Earlier today.\nUser: Now please.'
    for n in range(85):
        history.remember(f'next-{n}', 'user', f'part{n}.', 300_005 + n)
    tail = history.context_for('next-84')
    assert 'part4.' not in tail
    assert tail.startswith('User: part5. ')
    assert tail.endswith('part84.')
