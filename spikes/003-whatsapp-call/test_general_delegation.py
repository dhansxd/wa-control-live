"""Synero delegation protocol with a local data channel, native-chat stub and PCM."""
import asyncio
from contextlib import asynccontextmanager
from fractions import Fraction
import json
import struct
import uuid

import av
import pytest

import call_adapter as adapter


ACTIVE_WHATSAPP_SESSION = 'active-whatsapp-dm'
DM_ADMISSION_ID = 'a' * 32


class TranscriptJobs:
    async def save_exchange(self, session_id, _exchange, *, wait_for_lease=False):
        assert session_id == ACTIVE_WHATSAPP_SESSION
        return {'status': 'saved'}


def delegation(text, *, item_id='request-1', turn_id='caller-1'):
    return {
        'type': 'delegation.created',
        'item': {'id': item_id, 'user_bidi_turn_id': turn_id, 'target': 'client',
                 'content': [{'type': 'input_text', 'text': part} for part in text]},
    }


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def local_voice(path, monkeypatch, *, agent_jobs=None):
    sent, appended = [], []
    async def send(message):
        sent.append(message)
    class Remote:
        def __init__(self):
            self.frames = asyncio.Queue()
        async def recv(self):
            return await self.frames.get()
    monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
    voice = adapter.LiveCall(path, send, agent_jobs=agent_jobs)
    if agent_jobs is not None:
        voice.hermes_session_id = ACTIVE_WHATSAPP_SESSION
    voice.connection.channel = type('CapturedChannel', (), {'send':lambda self, value:appended.append(json.loads(value))})()
    remote = Remote()
    voice.spawn(voice.connection.read_audio(remote))
    return voice, remote, sent, appended


async def feed_output(voice, remote, value):
    frame = av.AudioFrame(format='s16', layout='mono', samples=480)
    frame.sample_rate, frame.time_base = 24000, Fraction(1, 24000)
    frame.pts = voice.report['output_bytes']//2
    frame.planes[0].update(struct.pack('<480h', *([value]*480)))
    expected = voice.report['output_bytes'] + 960
    remote.frames.put_nowait(frame)
    await wait_until(lambda: voice.report['output_bytes'] == expected)


def audio_messages(sent):
    return [message for message in sent if 'modelTurn' in message.get('serverContent', {})]


@pytest.mark.parametrize('prior_turn', ['none', 'conversation', 'outbound_greeting'])
def test_idle_context_does_not_speak_before_next_caller_turn(tmp_path, monkeypatch, prior_turn):
    async def exercise():
        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        async def turn(role, turn_id, kind, text=''):
            await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}})
        async def append(text):
            return True
        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            if prior_turn != 'none':
                if prior_turn == 'conversation':
                    await turn('user', 'first', 'turn.done', 'Halo Hermes.')
                else:
                    voice.call_direction = 'outbound'
                    await voice.append_chat_context(['Panggilan balik dari Hermes.'])
                await turn('assistant', 'greeting', 'turn.created')
                await feed_output(voice, remote, 1700)
                assert audio_messages(sent)
                await turn('assistant', 'greeting', 'turn.done', 'Halo.')
                voice.playout_expected_until = asyncio.get_running_loop().time() - 1
            await voice.append_chat_context(['Pembaruan senyap: ekspor audio gagal.'])
            sent.clear()
            await feed_output(voice, remote, 1400)  # PCM can precede the provider turn ID.
            await turn('assistant', 'unrequested', 'turn.created')
            await turn('assistant', 'unrequested', 'turn.done', 'Ada progres baru.')
            assert not sent
            assert 'unrequested' not in voice.spoken_reply
            await turn('user', 'question', 'turn.done', 'Apa yang gagal?')
            await feed_output(voice, remote, 1500)
            await turn('assistant', 'answer', 'turn.created')
            await turn('assistant', 'answer', 'turn.done', 'Ekspor audionya gagal.')
            assert len(audio_messages(sent)) == 1
            assert sent[-1]['serverContent']['outputTranscription']['text'] == 'Ekspor audionya gagal.'
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('boundary', ['caller', 'idle', 'close'])
def test_silent_updates_wait_for_codex_to_finish_its_existing_turn(tmp_path, monkeypatch, boundary):
    async def exercise():
        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        delivered = []

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'id': turn_id, 'role': role, 'transcript': text}})

        async def append(text):
            delivered.append(text)
            if voice.active_reply_turn == 'first-answer':
                # Codex may continue the SAME turn on commentary following a pause.
                await feed_output(voice, remote, 2300)
                await voice.on_data({'type': 'turn.delta', 'turn_id': 'first-answer', 'delta': 'Jawaban ulang.'})

        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            await turn('turn.done', 'user', 'owner', 'Buatkan gambar.')
            await turn('turn.created', 'assistant', 'first-answer')
            await feed_output(voice, remote, 1500)
            sent.clear()
            for text in ['Mulai mengerjakan gambar.', 'Ekspor gagal.']:
                assert await voice.append_chat_context([text])
            await asyncio.sleep(0)
            assert not delivered and not audio_messages(sent)
            if boundary == 'close':
                await voice.close()
                assert not delivered
                return
            if boundary == 'idle':
                await turn('turn.done', 'assistant', 'first-answer', 'Sedang kubuat.')
                voice.playout_expected_until = asyncio.get_running_loop().time() - 1
                await asyncio.sleep(0)
                assert not delivered
            await turn('turn.created', 'user', 'owner-again', 'Sudah sampai mana?')
            await wait_until(lambda: len(delivered) == 2)
            assert delivered == ['Mulai mengerjakan gambar.', 'Ekspor gagal.']
            assert not audio_messages(sent)
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_codex_parks_updates_after_final_transcript_before_first_or_last_pcm(tmp_path, monkeypatch):
    async def exercise():
        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        delivered = []

        async def append(text):
            delivered.append(text)

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'id': turn_id, 'role': role, 'transcript': text}})

        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            voice.feed(struct.pack('<h', 260) * 320)
            await turn('turn.done', 'user', 'first', 'Buatkan gambar.')
            await turn('turn.created', 'assistant', 'first-answer')
            await turn('turn.done', 'assistant', 'first-answer', 'Aku buatkan.')
            await voice.append_chat_context(['Ekspor mulai.'])
            await turn('turn.created', 'user', 'fabricated', 'Kata yang tidak diucapkan.')
            assert 'fabricated' not in voice.user_turns and not delivered
            await feed_output(voice, remote, 1600)
            await voice.append_chat_context(['Ekspor selesai.'])
            await feed_output(voice, remote, 1300)
            assert len(audio_messages(sent)) == 2
            voice.playout_expected_until = asyncio.get_running_loop().time() - 1
            await asyncio.sleep(0)
            assert not delivered
            voice.feed(struct.pack('<h', 300) * 320)
            await turn('turn.created', 'user', 'actual-question', 'Sudah selesai?')
            await wait_until(lambda: len(delivered) == 2)
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('boundary', ['quiet', 'answer', 'replacement_turn', 'close'])
def test_codex_context_waits_for_actual_caller_audio_to_pause(tmp_path, monkeypatch, boundary):
    async def exercise():
        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        loop = asyncio.get_running_loop()
        clock = [loop.time()]
        monkeypatch.setattr(loop, 'time', lambda: clock[0])
        speech = struct.pack('<h', 320) * 320
        silence = bytes(len(speech))
        forwarded, delivered = [], []
        monkeypatch.setattr(voice.connection, 'feed', forwarded.append)

        async def append(text):
            delivered.append((voice.current_spoken_turn, text, clock[0]))

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'id': turn_id, 'role': role, 'transcript': text}})

        async def advance(seconds):
            clock[0] += seconds
            # Advance due call_later callbacks and their separate flush coroutine.
            for _ in range(4):
                await asyncio.sleep(0)

        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            voice.feed(speech)
            await turn('turn.done', 'user', 'first', 'Mulai ekspor.')
            await turn('turn.created', 'assistant', 'a1')
            await turn('turn.done', 'assistant', 'a1', 'Aku kerjakan.')
            await voice.append_chat_context(['Ekspor mencapai 37 persen.'])

            voice.feed(speech)
            await turn('turn.created', 'user', 'six-second-question')
            for frame in range(300):
                # 20 ms per frame; a real 280 ms pause halfway through.
                voice.feed(silence if 130 <= frame < 144 else speech)
                await advance(0.02)
                assert not delivered
            end_of_speech = clock[0] - 0.02
            await turn('turn.done', 'user', 'six-second-question', 'Sudah sampai mana?')
            assert len(forwarded) == 302
            assert voice.caller_context_timer is not None
            await advance(0.43)
            assert not delivered
            if boundary == 'close':
                await voice.close()
                await advance(0.2)
                assert voice.caller_context_timer is None and not delivered
                return
            if boundary in ('answer', 'replacement_turn'):
                await turn('turn.created', 'assistant', 'real-reply')
                if boundary == 'answer':
                    await advance(0.2)
                    assert not delivered  # Never write a passive update into this response.
                    return
                voice.feed(speech)
                end_of_speech = clock[0]
                await turn('turn.created', 'user', 'interruption', 'Tunggu.')
                await advance(0.15)
                assert not delivered
                await advance(0.36)
            else:
                await advance(0.06)
            expected = 'interruption' if boundary == 'replacement_turn' else 'six-second-question'
            assert [(turn_id, text) for turn_id, text, _ in delivered] == [(expected, 'Ekspor mencapai 37 persen.')]
            assert delivered[0][2] - end_of_speech >= adapter.CALLER_CONTEXT_QUIET_SECONDS
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_codex_defers_context_after_untagged_audio_and_drops_old_progress_and_commentary(tmp_path, monkeypatch):
    async def exercise():
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        delivered = []

        async def append(text):
            delivered.append(text)

        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'owner', 'transcript': 'Pakai lampiran tadi.'}})
            await feed_output(voice, remote, 1510)  # PCM arrived before the turn ID.
            work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                    'status': 'started', 'text': 'Coba ekspor.'}
            voice.update_dm_work([work])
            voice.work_progress[DM_ADMISSION_ID] = (1, {'tool-1': {'tool': 'render', 'percent': 15, 'status': 'started'}})
            for tick in range(100):
                assert await voice.append_chat_context([f'Pengukuran lama {tick}'], latest_work=True)
            assert await voice.append_chat_context(['Komentar lama.'], commentary=(DM_ADMISSION_ID, 1))
            assert not delivered and len(voice.pending_chat_context) == 1
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'reply'}})
            voice.work_progress[DM_ADMISSION_ID] = (2, {'tool-2': {'tool': 'export', 'percent': 57, 'status': 'started'}})
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'user', 'id': 'next'}})
            await wait_until(lambda: not voice.pending_chat_context and not voice.deferred_work_snapshot)
            assert '"percent": 57' in ' '.join(delivered)
            assert all('lama' not in text.lower().split(': ', 1)[-1] for text in delivered)
            assert not any('"percent": 15' in text for text in delivered)
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_codex_silent_context_with_invented_caller_cannot_reopen_audio(tmp_path, monkeypatch):
    async def exercise():
        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}})

        async def append(_text):
            return True

        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            voice.feed(struct.pack('<h', 250) * 320)
            await turn('turn.done', 'user', 'first', 'Buat satu poster.')
            await turn('turn.created', 'assistant', 'answer')
            await feed_output(voice, remote, 1000)
            await turn('turn.done', 'assistant', 'answer', 'Aku buat posternya.')
            voice.playout_expected_until = asyncio.get_running_loop().time() - 1
            await voice.append_chat_context(['Progres poster sudah 37 persen.'])
            assert voice.waiting_for_caller
            sent.clear()
            voice.feed(struct.pack('<h', 1) * 320)
            await turn('turn.created', 'user', 'invented', 'Terdengar kata.')
            await turn('turn.done', 'user', 'invented', 'Terdengar kata.')
            await voice.on_data(delegation(['Ulangi tugas'], turn_id='invented'))
            await turn('turn.created', 'assistant', 'unsolicited')
            await feed_output(voice, remote, 1900)
            await turn('turn.done', 'assistant', 'unsolicited', 'Progres poster 37 persen.')
            assert not sent and not appended
            assert 'invented' not in voice.user_turns
            assert voice.report['caller_turns_without_audio'] == 1

            # Actual quiet speech opens the next turn and its answer as usual.
            voice.feed(struct.pack('<h', 20) * 320)
            await turn('turn.created', 'user', 'real', 'Sampai mana?')
            await turn('turn.done', 'user', 'real', 'Sampai mana?')
            await turn('turn.created', 'assistant', 'asked')
            await feed_output(voice, remote, 1600)
            await turn('turn.done', 'assistant', 'asked', 'Sudah 37 persen.')
            assert len(audio_messages(sent)) == 1
            assert voice.user_turns['real'] == 'Sampai mana?'
        finally:
            await voice.close()
    asyncio.run(exercise())


async def acknowledge(voice, item_id):
    await voice.on_data({'type':'delegation.context.appended', 'delegation_item_id':item_id})


@pytest.mark.parametrize('delayed', ['first_pcm', 'speakable_handoff'])
def test_quiet_progress_preserves_expected_late_speech(tmp_path, monkeypatch, delayed):
    async def exercise():
        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        async def append(_text):
            return True
        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            await voice.on_data({'type':'turn.done', 'turn':{'id':'u', 'role':'user', 'transcript':'Apa kabar?'}})
            await voice.on_data({'type':'turn.created', 'turn':{'id':'a', 'role':'assistant'}})
            if delayed == 'speakable_handoff':
                await feed_output(voice, remote, 1500)
            await voice.on_data({'type':'turn.done','turn':{'id':'a','role':'assistant','transcript':'Baik.'}})
            if delayed == 'speakable_handoff':
                voice.playout_expected_until = asyncio.get_running_loop().time() - 1
                response = asyncio.create_task(voice.append_handoff('checked', 'Sampaikan hasil terbaru.'))
                await wait_until(lambda: bool(appended))
                await acknowledge(voice, 'checked')
                assert await response
            sent.clear()
            await voice.append_chat_context(['Status tugas berubah.'])
            await feed_output(voice, remote, 1600)
            assert len(audio_messages(sent)) == 1
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_muted_audio_tail_stays_quiet_as_caller_starts_speaking(tmp_path, monkeypatch):
    async def exercise():
        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        async def append(_text):
            return True
        monkeypatch.setattr(voice.connection, 'append_context', append)
        try:
            await voice.append_chat_context(['Perkembangan tugas.'])
            await voice.on_data({'type':'turn.created','turn':{'id':'quiet','role':'assistant'}})
            await voice.on_data({'type':'turn.created','turn':{'id':'owner','role':'user'}})
            await feed_output(voice, remote, 1700)
            assert not audio_messages(sent)
            await voice.on_data({'type':'turn.done','turn':{'id':'owner','role':'user','transcript':'Apa kabar?'}})
            await voice.on_data({'type':'turn.created','turn':{'id':'new','role':'assistant'}})
            await feed_output(voice, remote, 1800)
            assert len(audio_messages(sent)) == 1
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_interrupting_voice_clears_playback_and_preserves_the_hermes_task(tmp_path, monkeypatch):
    async def exercise():
        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch)
        voice.hermes_session_id = ACTIVE_WHATSAPP_SESSION
        voice.call_work.add(DM_ADMISSION_ID)
        try:
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'old-reply'}})
            await feed_output(voice, remote, 1500)
            assert len(audio_messages(sent)) == 1

            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'user', 'id': 'new-user'}})
            assert sent[-1] == {'serverContent': {'interrupted': True}}
            await feed_output(voice, remote, 1600)
            assert len(sent) == 2
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'new-reply'}})
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'assistant', 'id': 'old-reply', 'transcript': 'Interrupted voice text.',
            }})
            assert len(sent) == 2
            assert voice.call_work == {DM_ADMISSION_ID}

            await feed_output(voice, remote, 1700)
            assert len(audio_messages(sent)) == 2
            assert voice.report['suppressed_output_bytes'] == 960
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('reply_transcript', ['Judulnya Catatan Uji.', ''])
def test_caller_speech_clears_queued_audio_after_provider_finishes_reply(tmp_path, monkeypatch, reply_transcript):
    async def exercise():
        submissions = []

        class Jobs(TranscriptJobs):
            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                submissions.append((session_id, text))
                return {'status': 'started', 'receipt': {
                    'session_id': session_id, 'admission_id': DM_ADMISSION_ID,
                }}

        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, transcript=''):
            await voice.on_data({'type': kind, 'turn': {
                'role': role, 'id': turn_id, 'transcript': transcript,
            }})

        try:
            task = 'Susun catatan uji.'
            _, status, admission_id = await voice.submit_task('task', 'task-request', task)
            assert (status, admission_id) == ('started', DM_ADMISSION_ID)

            await turn('turn.created', 'user', 'first-user')
            assert sent == []
            await turn('turn.done', 'user', 'first-user', 'Apa judul catatannya?')
            await turn('turn.created', 'assistant', 'old-reply')
            await feed_output(voice, remote, 1500)
            await turn('turn.done', 'assistant', 'old-reply', reply_transcript)
            if reply_transcript:
                assert sent[-1] == {'serverContent': {
                    'outputTranscription': {'text': reply_transcript}, 'turnComplete': True,
                }}

            # Generation is complete, but previously sent audio can still be in the sidecar queue.
            voice.feed(struct.pack('<320h', *([1800] * 320)))
            await turn('turn.created', 'user', 'new-user')
            await turn('turn.created', 'user', 'new-user')
            clear = {'serverContent': {'interrupted': True}}
            assert sent[-1] == clear
            assert sent.count(clear) == 1
            assert any(bytes((await voice.connection.track.recv()).planes[0]))

            late_interrupt = {'type': 'turn.interrupted', 'turn_id': 'old-reply'}
            await voice.on_data(late_interrupt)
            assert sent.count(clear) == 1
            await feed_output(voice, remote, 1600)
            assert sent[-1] == clear
            await turn('turn.done', 'user', 'new-user', 'Bentar, aku mau tanya yang lain.')
            assert sent[-1] == {'serverContent': {
                'inputTranscription': {'text': 'Bentar, aku mau tanya yang lain.'}, 'turnComplete': True,
            }}
            await turn('turn.created', 'assistant', 'new-reply')
            await voice.on_data(late_interrupt)
            await feed_output(voice, remote, 1700)
            assert sent.count(clear) == 1
            assert audio_messages(sent) == [
                adapter.audio_envelope(struct.pack('<480h', *([1500] * 480))),
                adapter.audio_envelope(struct.pack('<480h', *([1700] * 480))),
            ]
            assert voice.call_work == {DM_ADMISSION_ID}
            assert submissions == [(ACTIVE_WHATSAPP_SESSION, task)]
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('gap_before_caller', [True, False])
def test_gap_does_not_mute_fresh_audio_before_the_assistant_turn_event(tmp_path, monkeypatch, gap_before_caller):
    async def exercise():
        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch)
        loop = asyncio.get_running_loop()
        clock = loop.time
        try:
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'old-reply'}})
            await feed_output(voice, remote, 1500)
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'assistant', 'id': 'old-reply', 'transcript': 'Jawaban pertama.',
            }})

            with monkeypatch.context() as advance:
                if gap_before_caller:
                    advance.setattr(loop, 'time', lambda: clock() + 10)
                await voice.on_data({'type': 'turn.created', 'turn': {'role': 'user', 'id': 'new-user'}})
                if not gap_before_caller:
                    advance.setattr(loop, 'time', lambda: clock() + 10)
                    await feed_output(voice, remote, 1600)
                    assert len(audio_messages(sent)) == 1
                await voice.on_data({'type': 'turn.done', 'turn': {
                    'role': 'user', 'id': 'new-user', 'transcript': 'Ada kabar lain?',
                }})
                # The audio and turn events use different paths, so fresh PCM can lead the event.
                await feed_output(voice, remote, 1700)
                assert audio_messages(sent) == [
                    adapter.audio_envelope(struct.pack('<h', 1500) * 480),
                    adapter.audio_envelope(struct.pack('<h', 1700) * 480),
                ]
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_route_cannot_submit_to_another_dm(tmp_path, monkeypatch):
    async def exercise():
        class Jobs:
            async def route(self, *_args, session_id):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                return {'status': 'session_changed'}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('A call cannot submit into a different WhatsApp session')

            async def save_exchange(self, session_id, _exchange, *, wait_for_lease=False):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                return {'status': 'session_changed'}

        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'caller-1', 'role': 'user', 'transcript': 'Buat catatan baru.',
            }})
            await voice.on_data(delegation(['Buat catatan baru.']))
            await asyncio.wait_for(voice.failed.wait(), 1)
            assert voice.report['chat_updates_status'] == 'session_changed'
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('request_parts', [
    ['Buat catatan uji, lalu bacakan hasilnya.'],
    ['Riset tiga ide cerita pendek di web dan rangkum sumbernya.'],
    ['Could you look up three libraries open this Sunday and send me their hours?'],
    ['Cari nomor resi tadi lalu cek status kirimannya sekarang.'],
])
def test_confirmed_task_requires_final_transcript_and_returns_real_result(tmp_path, monkeypatch, request_parts):
    monkeypatch.setattr(adapter, 'PLAYOUT_MARGIN_SECONDS', 0)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        routed, submitted = [], []
        receipt = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': 'native-admission'}

        class Jobs:
            async def route(self, proposal, current, previous, loaded_context, *, session_id):
                routed.append(current)
                assert proposal == current == ''.join(request_parts)
                return {'status': 'task'}

            async def submit(self, submission_id, text, *, session_id, spoken_at_ms=None):
                submitted.append((submission_id, text, session_id))
                entered.set()
                await release.wait()
                return {'status': 'completed', 'receipt': receipt, 'output': 'Hasil asli Hermes.'}

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.created', 'turn': {'id': 'caller-1', 'role': 'user'}})
            event = delegation(request_parts)
            voice.connection._data(json.dumps(event))
            voice.connection._data(json.dumps(event))
            await wait_until(lambda: len(appended) == 1)
            assert not routed and not submitted
            assert appended[0]['delegation_item_id'] == 'request-1'
            assert appended[0]['channel'] == 'commentary'
            await acknowledge(voice, 'request-1')
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'caller-1', 'role': 'user', 'transcript': ''.join(request_parts),
            }})
            await asyncio.wait_for(entered.wait(), 1)
            submission_id = uuid.uuid5(uuid.NAMESPACE_URL, f'wa-control-live:{tmp_path.name}:caller-1').hex
            assert routed == [''.join(request_parts)]
            assert submitted == [(submission_id, ''.join(request_parts), ACTIVE_WHATSAPP_SESSION)]
            assert len(appended) == 1
            assert not audio_messages(sent)
            await feed_output(voice, remote, 1500)
            assert audio_messages(sent) == [adapter.audio_envelope(struct.pack('<480h', *([1500] * 480)))]
            await wait_until(lambda: voice.playout_expected_until <= asyncio.get_running_loop().time())
            release.set()
            await wait_until(lambda: len(appended) == 2)
            assert appended[1]['type'] == 'delegation.context.append'
            assert appended[1]['delegation_item_id'] == 'request-1'
            assert appended[1]['content'][0]['text'].endswith('Hasil asli Hermes.')
            assert voice.report['agent_receipts'] == {'request-1': {'admission_id': 'native-admission'}}
            await acknowledge(voice, 'different-request')
            assert voice.report['acknowledged_delegations'] == ['request-1']
            assert voice.pending_deliveries
            await acknowledge(voice, 'request-1')
            assert voice.report['acknowledged_delegations'] == ['request-1'] * 2
            await feed_output(voice, remote, 2000)
            await wait_until(lambda: voice.report['result_audio_forwarded_bytes'] == 960)
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('new_speaker', ['assistant', 'caller', 'same_turn_assistant', 'same_turn_audio_first', 'hesitation'])
@pytest.mark.parametrize('speech_confirmed', [True, False])
def test_previous_task_speaks_once_after_the_current_exchange(tmp_path, monkeypatch, new_speaker, speech_confirmed):
    monkeypatch.setattr(adapter, 'PLAYOUT_MARGIN_SECONDS', 0.1)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        speech_accepted = asyncio.Event()
        submitted, speech_requests, saved = [], [], []
        final = ('Mau catatannya dikirim ke mana? Dokumen sudah tersimpan.'
                 if new_speaker == 'hesitation' else 'Catatan sudah selesai.')

        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                submitted.append((text, session_id))
                entered.set()
                await release.wait()
                return {'status': 'completed', 'output': final,
                        'receipt': {'session_id': session_id, 'admission_id': DM_ADMISSION_ID}}

            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                saved.append((session_id, exchange['assistant']))
                return {'status': 'saved'}

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def app_server_call(method, params, *, timeout):
            assert method == 'thread/realtime/appendSpeech'
            assert params['threadId'] == 'call-thread'
            speech_requests.append(params['text'])
            await speech_accepted.wait()
            if not speech_confirmed:
                raise TimeoutError
            return {}

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'id': turn_id, 'role': role, 'transcript': text}})

        monkeypatch.setattr(adapter, 'RESULT_SPEECH_QUIET_SECONDS', 0, raising=False)
        voice.connection.thread_id = 'call-thread'
        voice.connection.server.call = app_server_call
        try:
            request = 'Buat catatan pertama.'
            await turn('turn.done', 'user', 'caller-1', request)
            await voice.on_data(delegation([request]))
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            await asyncio.wait_for(entered.wait(), 1)

            question = 'Em' if new_speaker == 'hesitation' else 'Kamu masih dengar aku?'
            if new_speaker == 'hesitation':
                await turn('turn.created', 'assistant', 'original-clarification')
                await feed_output(voice, remote, 2200)
                await turn('turn.done', 'assistant', 'original-clarification', 'Mau catatannya dikirim ke mana?')
                sent.clear()
            same_turn = new_speaker in {'same_turn_assistant', 'same_turn_audio_first'}
            audio_first = new_speaker == 'same_turn_audio_first'
            if not same_turn:
                await turn('turn.created', 'user', 'caller-2')
            if new_speaker == 'assistant':
                await turn('turn.done', 'user', 'caller-2', question)
            if new_speaker in {'assistant', 'same_turn_assistant'}:
                await turn('turn.created', 'assistant', 'answer-2')
            if audio_first:
                await feed_output(voice, remote, 2500)
            release.set()
            await wait_until(lambda: bool(voice.report['backend_completed_delegations']))
            assert len(appended) == 1
            assert speech_requests == []
            if new_speaker in {'caller', 'hesitation'}:
                await turn('turn.done', 'user', 'caller-2', question)
                await turn('turn.created', 'assistant', 'answer-2')
            if audio_first:
                await turn('turn.created', 'assistant', 'answer-2')
            else:
                await feed_output(voice, remote, 2500)
            conversation_pcm = struct.pack('<480h', *([2500] * 480))
            assert audio_messages(sent) == [adapter.audio_envelope(conversation_pcm)]
            assert speech_requests == []
            await turn('turn.done', 'assistant', 'answer-2', 'Aku dengar.')
            assert sent[-1]['serverContent']['outputTranscription']['text'] == 'Aku dengar.'
            assert not voice.work_speech_ready()

            await wait_until(lambda: bool(speech_requests))
            assert final in speech_requests[0]
            assert 'Jika kamu sudah mengajukan pertanyaan yang sama' in speech_requests[0]
            # Codex can speak even a commentary append inside a still-open reply.
            # Its single queued speech request is the only copy of the result.
            assert 'request-1' not in voice.pending_deliveries
            assert len(appended) == 1
            caller = voice.current_spoken_turn
            follow = voice.spawn(voice.present_work_result(
                'concurrent-follow', caller, DM_ADMISSION_ID, final,
            ))
            await asyncio.sleep(0)
            assert len(appended) == 1
            assert not follow.done()
            speech_accepted.set()
            if speech_confirmed:
                await asyncio.wait_for(follow, 1)
                assert len(appended) == 1 and DM_ADMISSION_ID in voice.offered_work_results
                await turn('turn.created', 'assistant', 'late-result')
                await feed_output(voice, remote, 3000)
                await turn('turn.done', 'assistant', 'late-result', 'Catatan sudah selesai.')
                result_pcm = struct.pack('<480h', *([3000] * 480))
                assert audio_messages(sent) == [
                    adapter.audio_envelope(frame) for frame in (conversation_pcm, result_pcm)
                ]
            else:
                await wait_until(lambda: len(appended) == 2)
                assert appended[-1]['delegation_item_id'] == 'concurrent-follow'
                assert 'Belum bisa dipastikan' in appended[-1]['content'][0]['text']
                await acknowledge(voice, 'concurrent-follow')
                await asyncio.wait_for(follow, 1)
                assert DM_ADMISSION_ID not in voice.offered_work_results
                assert audio_messages(sent) == [adapter.audio_envelope(conversation_pcm)]
            assert len(speech_requests) == 1
            assert submitted == [(request, ACTIVE_WHATSAPP_SESSION)]
        finally:
            await voice.close()
        assert saved == ([] if same_turn else [(ACTIVE_WHATSAPP_SESSION, 'Aku dengar.')])

    asyncio.run(exercise())


@pytest.mark.parametrize('final_received', [False, True])
def test_empty_proposal_requires_its_final_transcript(tmp_path, monkeypatch, final_received):
    monkeypatch.setattr(adapter, 'POST_ACK_TRANSCRIPT_WAIT', .005)

    async def exercise():
        routed = []

        class Jobs(TranscriptJobs):
            async def route(self, proposal, current, previous, loaded_context, *, session_id):
                routed.append((proposal, current))
                return {'status': 'reply'}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'user', 'id': 'caller-1'}})
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': 'Apa hasilnya?'})
            assert routed == []
            voice.connection._data(json.dumps(delegation([])))
            await wait_until(lambda: bool(appended))
            assert not routed
            assert appended[0]['channel'] == 'commentary'
            await acknowledge(voice, 'request-1')
            if final_received:
                await voice.on_data({'type': 'turn.done', 'turn': {
                    'id': 'caller-1', 'role': 'user', 'transcript': 'Apa hasilnya?',
                }})
                await wait_until(lambda: len(appended) == 2)
                assert appended[1]['channel'] == 'speakable'
                await acknowledge(voice, 'request-1')
            handoff = voice.tasks[-1]
            await asyncio.wait_for(handoff, 1)
            assert routed == ([('', 'Apa hasilnya?')] if final_received else [])
            assert len(appended) == (2 if final_received else 1)
            assert not voice.report['backend_completed_delegations']
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_unlinked_or_backing_proposal_and_second_item_cannot_repeat_a_task(tmp_path, monkeypatch):
    async def exercise():
        submitted = []

        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                submitted.append(text)
                return {'status': 'pending'}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.created', 'turn': {'id': 'caller-1', 'role': 'user'}})
            backing = delegation(['Tugas backing'], item_id='backing')
            backing['item']['target'] = 'backing'
            await voice.on_data(backing)
            await voice.on_data(delegation(['Tugas tanpa giliran'], item_id='unlinked', turn_id='unlinked'))
            await voice.on_data(delegation(['Kerjakan satu kali'], item_id='first'))
            await voice.on_data(delegation(['Kerjakan satu kali'], item_id='second'))
            await wait_until(lambda: len(appended) == 3)
            assert not submitted
            assert {item['delegation_item_id']: item['channel'] for item in appended} == {
                'unlinked': 'commentary', 'first': 'commentary', 'second': 'commentary',
            }
            for item in appended:
                await acknowledge(voice, item['delegation_item_id'])
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'caller-1', 'role': 'user', 'transcript': 'Kerjakan satu kali',
            }})
            await wait_until(lambda: len(appended) == 4)
            assert (appended[3]['delegation_item_id'], appended[3]['channel']) == ('first', 'speakable')
            assert submitted == ['Kerjakan satu kali']
            await acknowledge(voice, 'first')
            assert 'belum bisa memastikan' in appended[3]['content'][0]['text']
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize(('result', 'spoken'), [
    ({'status': 'session_changed'}, None),
    ({'status': 'completed', 'output': 'Hasil sesi lain.',
      'receipt': {'session_id': 'other-whatsapp-dm', 'admission_id': 'other-task'}},
     'belum bisa memastikan hasil'),
])
def test_handoff_does_not_speak_results_from_a_different_chat(tmp_path, monkeypatch, result, spoken):
    async def exercise():
        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, _text, *, session_id, spoken_at_ms=None):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                return result

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'caller-1', 'role': 'user', 'transcript': 'Cek status tugas terakhir.',
            }})
            await voice.on_data(delegation(['Cek status tugas terakhir.']))
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            if spoken is None:
                await asyncio.wait_for(voice.failed.wait(), 1)
                assert voice.replace_voice
                assert len(appended) == 1
            else:
                await wait_until(lambda: len(appended) == 2)
                response = appended[1]['content'][0]['text']
                await acknowledge(voice, 'request-1')
                assert spoken in response
                assert 'Hasil sesi lain.' not in response
            assert not voice.report['backend_completed_delegations']
            assert 'agent_receipts' not in voice.report
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(('previous', 'spoken_request'), [
    ('', 'Bacakan lengkap nomor resi yang pernah aku ketik di chat ini.'),
    ('Aku mau bahas nomor resi yang pernah aku ketik di chat ini.', 'Sekarang bacain lengkap.'),
])
def test_explicit_history_read_uses_the_bound_dm_without_a_task(tmp_path, monkeypatch, previous, spoken_request):
    async def exercise():
        searches = []

        class Jobs(TranscriptJobs):
            async def route(self, proposal, current, earlier, loaded_context, *, session_id):
                assert proposal == current == spoken_request
                assert earlier == previous
                assert '824517' not in loaded_context
                return {'status': 'recall', 'terms': ['nomor', 'resi']}

            async def recall(self, session_id, terms):
                searches.append((session_id, terms))
                return {'status': 'found', 'session_id': session_id,
                        'history': [{'role': 'user', 'text': 'Nomor resi: 824517.'}]}

            async def submit(self, *_args, **_kwargs):
                raise AssertionError('A history read should not create an agent job')

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            if previous:
                await voice.on_data({'type': 'turn.done', 'turn': {
                    'role': 'user', 'id': 'previous', 'transcript': previous,
                }})
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'user', 'id': 'caller-1'}})
            await voice.on_data(delegation([spoken_request]))
            await wait_until(lambda: len(appended) == 1)
            assert not searches
            assert appended[0]['channel'] == 'commentary'
            await acknowledge(voice, 'request-1')
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': spoken_request,
            }})
            await wait_until(lambda: len(appended) == 2)
            assert searches == [(ACTIVE_WHATSAPP_SESSION, ['nomor', 'resi'])]
            assert '824517' in appended[1]['content'][0]['text']
            await acknowledge(voice, 'request-1')
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('kind', ['photo', 'document', 'audio', 'video', 'text'])
def test_phone_follows_the_existing_dm_turn_and_uses_its_result(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(adapter, 'PLAYOUT_MARGIN_SECONDS', 0)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        observed, saved = [], []
        question = 'Apa sudah bisa dilihat?' if kind == 'photo' else 'Hasilnya sudah ada?'
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'kind': kind, 'attachment': kind != 'text',
                'text': 'Tolong periksa kiriman tadi.'}
        output = 'Payung merah di sebelah pintu.'

        class Jobs:
            async def route(self, _proposal, current, _previous, _loaded_context, *, session_id):
                assert current == question
                assert session_id == ACTIVE_WHATSAPP_SESSION
                return {'status': 'follow', 'admission_id': DM_ADMISSION_ID, 'work': [work]}

            async def observe_work(self, session_id, admission_id):
                observed.append((session_id, admission_id))
                if len(observed) == 1:
                    return {'status': 'started', 'session_id': session_id, 'admission_id': admission_id}
                entered.set()
                await release.wait()
                return {'status': 'completed', 'session_id': session_id,
                        'admission_id': admission_id, 'output': output}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('Following existing DM work must never submit another native turn')

            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                saved.append((session_id, exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.dm_work = [work]
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': question,
            }})
            await voice.on_data(delegation([question]))
            await wait_until(lambda: bool(appended))
            assert appended[0]['delegation_item_id'] == 'request-1'
            assert output not in appended[0]['content'][0]['text']
            await acknowledge(voice, 'request-1')
            await wait_until(lambda: len(appended) == 2)
            assert appended[1]['channel'] == 'speakable'
            assert 'masih berjalan' in appended[1]['content'][0]['text']
            assert output not in appended[1]['content'][0]['text']
            await acknowledge(voice, 'request-1')
            assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)]
            voice.update_dm_work([{**work, 'status': 'completed'}])
            await asyncio.wait_for(entered.wait(), 1)
            await feed_output(voice, remote, 1500)
            assert audio_messages(sent) == [adapter.audio_envelope(struct.pack('<480h', *([1500] * 480)))]
            assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)] * 2

            await wait_until(lambda: voice.playout_expected_until <= asyncio.get_running_loop().time())
            release.set()
            await wait_until(lambda: len(appended) == 3)
            assert appended[2]['delegation_item_id'] == 'request-1'
            assert appended[2]['channel'] == 'speakable'
            assert output in appended[2]['content'][0]['text']
            await acknowledge(voice, 'request-1')
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'reply-1'}})
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'assistant', 'id': 'reply-1', 'transcript': output,
            }})
        finally:
            await voice.close()
        assert saved == [(ACTIVE_WHATSAPP_SESSION, question, '')]

    asyncio.run(exercise())


def test_repeated_status_never_resubmits_a_dm_turn_or_hides_a_new_task(tmp_path, monkeypatch):
    async def exercise():
        observed, submitted, saved = [], [], []
        observer_entered = asyncio.Event()
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'kind': 'text', 'text': 'Periksa dokumen ini.'}
        new_request = 'Buat file daftar belanja untuk hari Minggu.'

        class Jobs:
            async def route(self, _proposal, current, _previous, _loaded_context, *, session_id):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                if current == new_request:
                    return {'status': 'task'}
                return {'status': 'follow', 'admission_id': DM_ADMISSION_ID, 'work': [work]}

            async def observe_work(self, session_id, admission_id):
                observed.append((session_id, admission_id))
                observer_entered.set()
                status = 'started' if len(observed) == 1 else 'unavailable'
                return {'status': status, 'session_id': session_id, 'admission_id': admission_id}

            async def submit(self, _submission_id, request, *, session_id, spoken_at_ms=None):
                submitted.append((request, session_id))
                return {'status': 'completed', 'output': 'Daftar belanjanya sudah dibuat.',
                        'receipt': {'session_id': session_id, 'admission_id': 'unrelated-native-job'}}

            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                saved.append((session_id, exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.dm_work = [work]

        def append_and_ack(value):
            message = json.loads(value)
            appended.append(message)
            voice.spawn(acknowledge(voice, message['delegation_item_id']))

        voice.connection.channel.send = append_and_ack
        status_questions = ('Sudah sampai mana?', 'Itu tadi sudah selesai belum?')
        try:
            for index, question in enumerate((*status_questions, new_request), 1):
                turn_id, item_id = f'caller-{index}', f'request-{index}'
                await voice.on_data({'type': 'turn.done', 'turn': {
                    'role': 'user', 'id': turn_id, 'transcript': question,
                }})
                await voice.on_data(delegation([question], item_id=item_id, turn_id=turn_id))
                await wait_until(lambda: any(msg['delegation_item_id'] == item_id for msg in appended))
                if index == 1:
                    await asyncio.wait_for(observer_entered.wait(), 1)
                if index == 2:
                    await wait_until(lambda: any(
                        msg['delegation_item_id'] == item_id and msg['channel'] == 'speakable' for msg in appended
                    ))
                    reply = next(msg['content'][0]['text'] for msg in appended
                                 if msg['delegation_item_id'] == item_id and msg['channel'] == 'speakable')
                    assert 'belum bisa dipastikan' in reply
                    assert 'masih berjalan' not in reply
                if index < 3:
                    assert not submitted
            await wait_until(lambda: submitted == [(new_request, ACTIVE_WHATSAPP_SESSION)])
            assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)] * 2
            await wait_until(lambda: any(
                'Daftar belanjanya sudah dibuat.' in msg['content'][0]['text']
                for msg in appended
            ))
        finally:
            await voice.close()
        assert saved == [(ACTIVE_WHATSAPP_SESSION, question, '') for question in status_questions]

    asyncio.run(exercise())


def test_live_silently_receives_latest_tool_progress_while_caller_keeps_talking(tmp_path, monkeypatch):
    async def exercise():
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'text': 'Render video yang diminta di DM.'}
        event = {'status': 'progress', 'session_id': ACTIVE_WHATSAPP_SESSION,
                 'admission_id': DM_ADMISSION_ID, 'generation': 2, 'tool_id': 'render',
                 'tool_name': 'render_video', 'phase': 'started'}
        updates = asyncio.Queue()

        class Jobs(TranscriptJobs):
            @asynccontextmanager
            async def watch_context(self, session_id, cursor):
                async def events():
                    yield {'status': 'ready', 'session_id': session_id, 'history_cursor': cursor,
                           'history': [], 'work': [work]}
                    while True:
                        yield await updates.get()
                yield events()

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        pending_ack = asyncio.Event()

        def send(value):
            item = json.loads(value)
            assert item['type'] == 'session.context.append' and item['channel'] == 'commentary'
            appended.append(item['content'][0]['text'])
            if '"percent": 24' in appended[-1]:
                pending_ack.set()
            else:
                voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))

        voice.connection.channel.readyState = 'open'
        voice.connection.channel.send = send
        try:
            voice.spawn(voice.follow_chat(0))
            await asyncio.wait_for(voice.chat_context_ready.wait(), 1)
            for percent in range(10, 25):
                updates.put_nowait({**event, 'seq': percent, 'percent': percent})
            await asyncio.wait_for(pending_ack.wait(), 1)
            assert sum('"percent"' in text for text in appended) == 1

            # A pending provider context receipt must not hold up the call's audio.
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'small-talk', 'transcript': 'Sekarang kita ngobrol santai dulu.'
            }})
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'reply'}})
            await feed_output(voice, remote, 1500)
            assert len(audio_messages(sent)) == 1
            await voice.connection.on_data({'type': 'session.context.appended'})
            await wait_until(lambda: '"percent": 24' in voice.loaded_context)

            failure = {**event, 'seq': 27, 'tool_id': 'check-page', 'tool_name': 'browser_exec',
                       'phase': 'completed', 'application_outcome': 'failed',
                       'finding': 'Pembuatan audio gagal.'}
            for update in ({**event, 'seq': 26, 'percent': 26}, failure, failure):
                updates.put_nowait(update)
            # The watcher keeps receiving real evidence while Codex holds its
            # assistant turn open. Injecting it now would trigger a second answer.
            await wait_until(lambda: 'check-page' in voice.work_progress[DM_ADMISSION_ID][1])
            assert not any('Pembuatan audio gagal.' in text for text in appended)
            await voice.on_data({'type': 'turn.created', 'turn': {
                'role': 'user', 'id': 'status-question', 'transcript': 'Sudah sampai mana?'
            }})
            await wait_until(lambda: 'Pembuatan audio gagal.' in voice.loaded_context)
            fresh = ' '.join(appended[2:])
            assert fresh.count('Pembuatan audio gagal.') == 1
            assert fresh.index('"percent": 26') < fresh.index('Pembuatan audio gagal.')
            assert voice.dm_work == [work] and not voice.delegations
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('status, bound', [('started', True), ('completed', True), ('completed', False)])
def test_live_explicit_code_question_reads_only_same_job_without_repeating_its_result(tmp_path, monkeypatch, status, bound):
    async def exercise():
        question = 'Baris kode mana yang tadi kamu ubah?'
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': status, 'text': 'Perbaiki render.'}
        calls = []

        class Jobs(TranscriptJobs):
            async def route(self, _proposal, current, _previous, _context, *, session_id):
                assert current == question and session_id == ACTIVE_WHATSAPP_SESSION
                return {'status': 'code_changes', 'admission_id': DM_ADMISSION_ID, 'work': [work]}

            async def observe_work(self, session_id, admission_id, *, include_code):
                calls.append((session_id, admission_id, include_code))
                return {'status': status, 'session_id': session_id if bound else 'other-session',
                        'admission_id': admission_id, 'output': 'Already delivered job result',
                        'tool_steps': [{'file_change': {'effect': 'patched', 'code_excerpt': [
                            {'file': 'render.py', 'line': 11, 'text': 'return audio.frames'},
                        ]}}]}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            voice.offered_work_results.add(DM_ADMISSION_ID)
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': question,
            }})
            await voice.on_data(delegation([question]))
            await wait_until(lambda: appended)
            if appended[-1]['channel'] == 'commentary':
                await acknowledge(voice, 'request-1')
            await wait_until(lambda: any(item['channel'] == 'speakable' for item in appended))
            await acknowledge(voice, 'request-1')
            answers = [item['content'][0]['text'] for item in appended if item['channel'] == 'speakable']
            assert len(answers) == 1 and ('return audio.frames' in answers[0]) is bound
            assert 'Already delivered job result' not in answers[0]
            assert calls == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID, True)]
            assert not voice.work_followers
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('route, status, generation, expected', [
    ('reply', 'unused', 2, ''),
    ('follow', 'started', 2, 'Baca sumber museum'),
    ('follow', 'started', 3, 'masih berjalan'),
    ('follow', 'queued', 2, 'menunggu giliran'),
    ('follow', 'approval_required', 2, 'menunggu persetujuan'),
    ('follow', 'clarification_required', 2, 'menunggu penjelasan tambahan'),
    ('follow', 'failed', 2, 'berakhir tanpa hasil'),
    ('follow', 'cancelled', 2, 'dibatalkan'),
    ('follow', 'interrupted', 2, 'terhenti sebelum selesai'),
    ('follow', 'unavailable', 2, 'belum bisa dipastikan'),
])
def test_live_tool_progress_answers_from_the_same_work_without_submitting(
    tmp_path, monkeypatch, route, status, generation, expected,
):
    async def exercise():
        question = 'Risetnya tadi sampai tahap mana?'
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'kind': 'text', 'text': 'Riset museum.'}
        progress = {'status': 'progress', 'session_id': ACTIVE_WHATSAPP_SESSION,
                    'admission_id': DM_ADMISSION_ID, 'generation': 2,
                    'tool_id': 'read-1', 'tool_name': 'web_extract',
                    'phase': 'started', 'detail': 'Baca sumber museum'}
        routed = asyncio.Event()
        observed = []

        class Jobs:
            @asynccontextmanager
            async def watch_context(self, session_id, cursor):
                async def updates():
                    yield {'status': 'ready', 'session_id': session_id, 'history_cursor': cursor,
                           'history': [], 'work': [work]}
                    yield progress
                    yield progress  # A replayed copy cannot send a second context item.
                    yield {**progress, 'generation': 1, 'phase': 'failed'}
                    await asyncio.Future()
                yield updates()

            async def route(self, _proposal, current, _previous, context, *, session_id):
                assert current == question
                assert session_id == ACTIVE_WHATSAPP_SESSION
                assert 'web_extract' in context and 'Baca sumber museum' in context
                assert '"status": "failed"' not in context
                routed.set()
                return {'status': route, 'admission_id': DM_ADMISSION_ID}

            async def observe_work(self, session_id, admission_id):
                observed.append((session_id, admission_id))
                return {'status': status, 'session_id': session_id, 'admission_id': admission_id,
                        'execution_generation': generation}

            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                assert session_id == ACTIVE_WHATSAPP_SESSION and exchange['user'] == question
                return {'status': 'saved'}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('Checking progress cannot start a second job')

        voice, _, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        def append(value):
            event = json.loads(value)
            appended.append(event)
            if event['type'] == 'session.context.append':
                voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))
            else:
                voice.spawn(acknowledge(voice, event['delegation_item_id']))

        voice.connection.channel.readyState = 'open'
        voice.connection.channel.send = append
        try:
            voice.spawn(voice.follow_chat(0))
            await wait_until(lambda: 'Baca sumber museum' in voice.loaded_context)
            assert not sent
            assert len([item for item in appended if 'Progres pekerjaan' in item['content'][0]['text']]) == 1
            assert all(item['channel'] == 'commentary' for item in appended)
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': question,
            }})
            await voice.on_data(delegation([question]))
            await asyncio.wait_for(routed.wait(), 1)
            if route == 'follow':
                await wait_until(lambda: any(
                    item['channel'] == 'speakable' and expected in item['content'][0]['text'] for item in appended
                ))
                response = next(item['content'][0]['text'] for item in appended if item['channel'] == 'speakable')
                if status != 'started' or generation != 2:
                    assert 'Baca sumber museum' not in response
                if status != 'started':
                    assert 'masih berjalan' not in response
            assert observed == ([(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)] if route == 'follow' else [])
            assert not voice.native_task_turns
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('tracked, completed_while_busy', [(False, False), (True, False), (True, True)])
def test_live_dm_work_is_offered_to_voice_before_the_same_result_completes(
    tmp_path, monkeypatch, tracked, completed_while_busy,
):
    async def exercise():
        draft = 'Aku menemukan katalog resmi untuk museum pertama.'
        output = 'Riset selesai. Ini tiga museum beserta sumbernya.'
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'kind': 'text', 'text': 'Riset museum.', 'seq': 2}
        draft_event = {'status': 'commentary', 'session_id': ACTIVE_WHATSAPP_SESSION,
                       'admission_id': DM_ADMISSION_ID, 'generation': 3, 'seq': 20, 'text': draft}
        finish = asyncio.Event()
        status = 'started'
        speech, observations = [], []

        class Jobs:
            @asynccontextmanager
            async def watch_context(self, session_id, cursor):
                async def updates():
                    yield {'status': 'ready', 'session_id': session_id, 'history_cursor': cursor,
                           'history': [], 'work': [work]}
                    yield {**draft_event, 'session_id': 'other-session', 'text': 'other session'}
                    yield {**draft_event, 'admission_id': 'b' * 32, 'text': 'unrelated work'}
                    yield draft_event
                    yield draft_event
                    await finish.wait()
                    yield {'status': 'ready', 'session_id': session_id, 'history_cursor': cursor,
                           'history': [], 'work': [{**work, 'status': 'completed'}]}
                    yield {**draft_event, 'text': 'obsolete draft'}
                    await asyncio.Future()
                yield updates()

            async def observe_work(self, session_id, admission_id):
                observations.append((session_id, admission_id))
                return {'status': status, 'session_id': session_id,
                        'admission_id': admission_id, 'output': output}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('The live stream must not submit a second Hermes turn')

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.connection.thread_id = 'call-thread'
        monkeypatch.setattr(adapter, 'RESULT_SPEECH_QUIET_SECONDS', 0)

        def append(raw):
            appended.append(json.loads(raw))
            voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))

        async def announce(method, params, *, timeout):
            assert method == 'thread/realtime/appendSpeech'
            speech.append(params['text'])

        voice.connection.channel.readyState = 'open'
        voice.connection.channel.send = append
        voice.connection.server.call = announce
        try:
            if tracked:
                voice.update_dm_work([work])
                voice.call_work.add(DM_ADMISSION_ID)
                voice.watch_dm_work(DM_ADMISSION_ID, None, None)
            if completed_while_busy:
                voice.active_reply_turn = 'other-reply'
            voice.spawn(voice.follow_chat(0))
            if completed_while_busy:
                await wait_until(lambda: voice.work_preview_cursor is not None)
                assert draft not in voice.loaded_context
            else:
                await wait_until(lambda: draft in voice.loaded_context)
            if tracked and not completed_while_busy:
                await wait_until(lambda: len(speech) == 1)
                assert draft in speech[0] and 'pembaruan sementara' in speech[0]
                assert DM_ADMISSION_ID not in voice.offered_work_results
                await voice.on_data({'type': 'turn.created', 'turn': {'id': 'live-preview', 'role': 'assistant'}})
                await feed_output(voice, remote, 2300)
                await voice.on_data({'type': 'turn.done', 'turn': {'id': 'live-preview', 'role': 'assistant'}})
                assert audio_messages(sent)
            else:
                assert not speech and not sent
            assert 'other session' not in voice.loaded_context
            assert 'unrelated work' not in voice.loaded_context
            status = 'completed'
            finish.set()
            await wait_until(lambda: voice.dm_work[0]['status'] == 'completed')
            if tracked:
                if completed_while_busy:
                    assert not speech
                    await voice.on_data({'type': 'turn.done', 'turn': {'id': 'other-reply', 'role': 'assistant'}})
                await wait_until(lambda: DM_ADMISSION_ID in voice.offered_work_results)
                assert len(speech) == (1 if completed_while_busy else 2)
                assert speech[-1].endswith(output)
                assert observations == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)] * 2
            else:
                assert not observations and not speech
            assert 'obsolete draft' not in voice.loaded_context
            assert all(item['channel'] == 'commentary' for item in appended)
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_new_caller_turn_during_preview_keeps_speech_and_the_callers_input(tmp_path, monkeypatch):
    async def exercise():
        saved = []

        class Jobs:
            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                saved.append((session_id, exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, remote, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def speak(method, params, *, timeout):
            assert method == 'thread/realtime/appendSpeech'

        voice.connection.server.call = speak
        try:
            await voice.send_work_speech('Pembaruan pekerjaan lama.', preview=True)
            for event, role, turn, text in (
                ('turn.created', 'user', 'caller-2', ''),
                ('turn.done', 'user', 'caller-2', 'Apa kabar?'),
                ('turn.created', 'assistant', 'next-reply', ''),
            ):
                await voice.on_data({'type': event, 'turn': {'id': turn, 'role': role, 'transcript': text}})
            await feed_output(voice, remote, 2100)
            await voice.on_data({'type': 'turn.done', 'turn': {'id': 'next-reply', 'role': 'assistant',
                                                             'transcript': 'Teks yang belum bisa dikaitkan.'}})
            await wait_until(lambda: bool(saved))
            assert audio_messages(sent)
            assert saved == [(ACTIVE_WHATSAPP_SESSION, 'Apa kabar?', '')]
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_explicit_result_repeat_waits_for_preview_from_another_job(tmp_path, monkeypatch):
    async def exercise():
        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=TranscriptJobs())
        voice.connection.thread_id = 'call-thread'
        voice.offered_work_results.add(DM_ADMISSION_ID)
        speech = []

        async def speak(method, params, *, timeout):
            assert method == 'thread/realtime/appendSpeech'
            speech.append(params['text'])

        def append(raw):
            item = json.loads(raw)
            appended.append(item)
            voice.spawn(acknowledge(voice, item['delegation_item_id']))

        voice.connection.channel.send = append
        voice.connection.server.call = speak
        monkeypatch.setattr(adapter, 'RESULT_SPEECH_QUIET_SECONDS', 0)
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-2', 'transcript': 'Bacakan lagi hasilnya.',
            }})
            await voice.send_work_speech('Progres pekerjaan lain.', preview=True)
            repeat = voice.spawn(voice.present_work_result(
                'repeat-1', 'caller-2', DM_ADMISSION_ID, 'Hasil yang ingin diulang.', repeat=True,
            ))
            await asyncio.sleep(0)
            assert not appended
            assert speech == ['Progres pekerjaan lain.']
            await voice.on_data({'type': 'turn.created', 'turn': {'id': 'preview', 'role': 'assistant'}})
            await voice.on_data({'type': 'turn.done', 'turn': {'id': 'preview', 'role': 'assistant'}})
            await asyncio.wait_for(repeat, 1)
            assert speech == ['Progres pekerjaan lain.', 'Hasil yang ingin diulang.']
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(('speak_first', 'delivery_state'), [(False, 'sent'), (False, 'pending'), (True, 'sent')])
def test_phone_follows_and_steers_one_dm_document_then_keeps_result_in_dm(
    tmp_path, monkeypatch, speak_first, delivery_state,
):
    async def exercise():
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'kind': 'document', 'text': 'Rangkum dokumen ini.',
                'seq': 2, 'attachment': True, 'files': [{'name': 'alur.pdf', 'mime': 'application/pdf'}]}
        observed, steered, announcements, responses = [], [], [], []
        final = 'Ringkasan alur produksi yang asli.'
        instructions = {
            'Sudah sampai mana?': 'follow',
            'Fokus ke alur produksinya.': 'steer',
            'Hasil akhirnya di chat ini saja.': 'dm_only',
            'Teks dan file sudah masuk DM?': 'delivery_status',
            'Sekarang bacakan juga.': 'speak_result',
        }
        routed = asyncio.Event()
        release = asyncio.Event()

        class Jobs:
            async def route(self, _proposal, current, _previous, _context, *, session_id):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                action = instructions[current]
                if action == 'dm_only':
                    routed.set()
                    await release.wait()
                return {'status': action, 'admission_id': DM_ADMISSION_ID}

            async def observe_work(self, session_id, admission_id):
                assert (session_id, admission_id) == (ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)
                observed.append(admission_id)
                return {'status': work['status'], 'session_id': session_id, 'admission_id': admission_id,
                        'execution_generation': 3, 'output': final,
                        'text_delivery': delivery_state, 'has_attachments': True}

            async def steer_work(self, session_id, admission_id, text):
                assert (session_id, admission_id) == (ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)
                steered.append(text)
                return {'status': 'queued'}

            async def save_exchange(self, *_args, **_kwargs):
                return {'status': 'saved'}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('An existing DM task must never be submitted again')

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.connection.thread_id = 'call-thread'
        voice.update_dm_work([work.copy()])
        monkeypatch.setattr(adapter, 'RESULT_SPEECH_QUIET_SECONDS', 0)

        def append(raw):
            event = json.loads(raw)
            appended.append(event)
            if item_id := event.get('delegation_item_id'):
                if event['channel'] == 'speakable':
                    responses.append((item_id, event['content'][0]['text']))
                voice.spawn(acknowledge(voice, item_id))
            else:
                voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))

        async def announce(method, params, *, timeout):
            assert method == 'thread/realtime/appendSpeech'
            announcements.append(params['text'])

        voice.connection.channel.readyState = 'open'
        voice.connection.channel.send = append
        voice.connection.server.call = announce

        async def utter(turn, question):
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': str(turn), 'transcript': question,
            }})
            await voice.on_data(delegation([question], item_id=f'p-{turn}', turn_id=str(turn)))

        try:
            items = list(instructions)
            await utter(1, items[0])
            await wait_until(lambda: any(item_id == 'p-1' for item_id, _ in responses))
            await utter(2, items[1])
            await wait_until(lambda: any(item_id == 'p-2' for item_id, _ in responses))
            await utter(3, items[2])
            await asyncio.wait_for(routed.wait(), 1)
            work['status'] = 'completed'
            await voice.apply_chat_update({'work': [work.copy()], 'history_cursor': 1,
                                           'history': [{'role': 'assistant', 'text': final}]}, 0)
            await wait_until(lambda: voice.work_followers[DM_ADMISSION_ID][4] is None)
            assert not announcements and all(final not in text for _, text in responses)
            if speak_first:
                await utter(4, items[4])
                await wait_until(lambda: voice.result_preference_turns.get(DM_ADMISSION_ID) == '4')
                release.set()
                await wait_until(lambda: '3' not in voice.routing_turns)
                assert DM_ADMISSION_ID not in voice.dm_only_results
                assert not any(item_id == 'p-3' for item_id, _ in responses)
                return
            release.set()
            await wait_until(lambda: any(item_id == 'p-3' for item_id, _ in responses))
            await utter(4, items[3])
            await wait_until(lambda: any(item_id == 'p-4' for item_id, _ in responses))
            delivery = dict(responses)['p-4']
            if delivery_state == 'sent':
                assert 'Pengiriman lampiran belum terkonfirmasi' in delivery
            else:
                assert 'Pengiriman teks hasil ke DM masih dalam proses.' in delivery
            assert final not in dict(responses)['p-4']
            assert not announcements and not voice.offered_work_results
            assert final in voice.loaded_context
            assert steered == [items[1]] and set(observed) == {DM_ADMISSION_ID}
            await utter(5, items[4])
            await wait_until(lambda: any(item_id == 'p-5' for item_id, _ in responses))
            assert final in dict(responses)['p-5']
            assert 'pengiriman' not in dict(responses)['p-5'].lower()
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('submit_status, first_observation', [
    ('queued', 'started'),
    ('pending', 'unavailable'),
    ('unknown', 'unavailable'),
    ('approval_required', 'approval_required'),
])
@pytest.mark.parametrize('prior_reply', [None, 'turn', 'pcm'])
def test_pending_voice_task_is_observed_and_result_is_offered_once(
    tmp_path, monkeypatch, submit_status, first_observation, prior_reply,
):
    async def exercise():
        submitted, observed = [], []
        observing, complete = asyncio.Event(), asyncio.Event()
        request, output = 'Riset tiga ide cerita.', 'Tiga ide cerita sudah dirangkum.'
        receipt = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID}

        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                submitted.append((text, session_id))
                return {'status': submit_status, 'receipt': receipt}

            async def observe_work(self, session_id, admission_id):
                observed.append((session_id, admission_id))
                if len(observed) == 1:
                    return {'status': first_observation, **receipt}
                observing.set()
                await complete.wait()
                return {'status': 'completed', 'output': output, **receipt}

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        monkeypatch.setattr(adapter, 'PLAYOUT_MARGIN_SECONDS', 0)
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': request,
            }})
            if prior_reply == 'turn':
                await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'reply-1'}})
                await voice.on_data({'type': 'turn.done', 'turn': {
                    'role': 'assistant', 'id': 'reply-1', 'transcript': 'Baik, kita lanjut ngobrol.',
                }})
            elif prior_reply == 'pcm':
                await feed_output(voice, remote, 1500)
            event = delegation([request])
            await voice.on_data(event)
            await wait_until(lambda: len(appended) == 1)
            assert appended[0]['channel'] == ('commentary' if prior_reply else 'speakable')
            assert 'sedang diproses' not in appended[0]['content'][0]['text']
            await acknowledge(voice, 'request-1')
            expected_messages = 1
            if submit_status == 'approval_required':
                await wait_until(lambda: len(appended) == 2)
                assert 'menunggu persetujuan' in appended[1]['content'][0]['text']
                assert appended[1]['channel'] == 'speakable'
                await acknowledge(voice, 'request-1')
                expected_messages += 1

            await wait_until(lambda: len(observed) == 1)
            voice.update_dm_work([{**receipt, 'status': 'completed', 'text': request}])
            await asyncio.wait_for(observing.wait(), 1)
            assert len(appended) == expected_messages
            await feed_output(voice, remote, 1700)
            spoken_samples = (1500, 1700) if prior_reply == 'pcm' else (1700,)
            assert audio_messages(sent) == [
                adapter.audio_envelope(struct.pack('<h', value) * 480) for value in spoken_samples
            ]
            await wait_until(lambda: voice.playout_expected_until <= asyncio.get_running_loop().time())

            await voice.on_data(event)
            complete.set()
            await wait_until(lambda: len(appended) == expected_messages + 1)
            await acknowledge(voice, 'request-1')
            assert [(msg['delegation_item_id'], msg['channel']) for msg in appended
                    if output in msg['content'][0]['text']] == [('request-1', 'speakable')]
            assert submitted == [(request, ACTIVE_WHATSAPP_SESSION)]
            assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)] * 2
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('reply_started', [True, False])
def test_codex_early_release_does_not_reprompt_for_native_admission(tmp_path, monkeypatch, reply_started):
    async def exercise():
        route_started, route_ready = asyncio.Event(), asyncio.Event()
        request = 'Buat satu ilustrasi.'

        class Jobs(TranscriptJobs):
            async def route(self, *_args, session_id):
                route_started.set()
                await route_ready.wait()
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id, **_):
                assert text == request
                return {'status': 'started', 'receipt': {'session_id': session_id, 'admission_id': DM_ADMISSION_ID}}

            async def observe_work(self, session_id, admission_id):
                return {'status': 'started', 'session_id': session_id, 'admission_id': admission_id}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'user', 'id': 'caller-1'}})
            await voice.on_data(delegation([request]))
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            await voice.on_data({'type': 'turn.done', 'turn': {'role': 'user', 'id': 'caller-1',
                                                               'transcript': request}})
            await asyncio.wait_for(route_started.wait(), 1)
            if reply_started:
                await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'reply'}})
            route_ready.set()
            await wait_until(lambda: DM_ADMISSION_ID in voice.work_followers or len(appended) > 1)
            assert len(appended) == 1
            assert appended[0]['channel'] == 'commentary'
            assert DM_ADMISSION_ID in voice.work_followers
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('submit_returns_first', [False, True])
def test_follow_up_receives_the_voice_started_task_result_once(tmp_path, monkeypatch, submit_returns_first):
    async def exercise():
        submission_started, observer_started = asyncio.Event(), asyncio.Event()
        release_submission, release_observer = asyncio.Event(), asyncio.Event()
        submitted, observed = [], []
        request, question = 'Buat ringkasan rapat.', 'Ringkasannya sudah selesai?'
        output = 'Hasil ringkasan rapat: jadwal sudah disepakati.'
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'kind': 'text', 'text': request}

        class Jobs:
            async def route(self, _proposal, current, _previous, _context, *, session_id):
                if current == request:
                    return {'status': 'task'}
                assert session_id == ACTIVE_WHATSAPP_SESSION
                return {'status': 'follow', 'admission_id': DM_ADMISSION_ID, 'work': [work]}

            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                submitted.append((text, session_id))
                submission_started.set()
                await release_submission.wait()
                return {'status': 'completed', 'output': output,
                        'receipt': {'session_id': session_id, 'admission_id': DM_ADMISSION_ID}}

            async def observe_work(self, session_id, admission_id):
                observed.append((session_id, admission_id))
                observer_started.set()
                await release_observer.wait()
                return {'status': 'completed', 'session_id': session_id,
                        'admission_id': admission_id, 'output': output}

            async def save_exchange(self, _session_id, _exchange, *, wait_for_lease=False):
                return {'status': 'saved'}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            task = None
            for index, text in enumerate((request, question), 1):
                turn_id, item_id = f'caller-{index}', f'request-{index}'
                await voice.on_data({'type': 'turn.done', 'turn': {
                    'role': 'user', 'id': turn_id, 'transcript': text,
                }})
                await voice.on_data(delegation([text], item_id=item_id, turn_id=turn_id))
                if index == 1:
                    task = voice.tasks[-1]
                await wait_until(lambda: len(appended) == index)
                assert appended[-1]['channel'] == ('speakable' if index == 1 else 'commentary')
                await acknowledge(voice, item_id)
                started = submission_started if index == 1 else observer_started
                await asyncio.wait_for(started.wait(), 1)

            if submit_returns_first:
                release_submission.set()
                await asyncio.wait_for(task, 1)
            release_observer.set()
            await wait_until(lambda: len(appended) == 3)
            await acknowledge(voice, 'request-2')
            release_submission.set()
            await asyncio.wait_for(task, 1)

            assert submitted == [(request, ACTIVE_WHATSAPP_SESSION)]
            assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)]
            assert [(msg['delegation_item_id'], msg['channel']) for msg in appended
                    if output in msg['content'][0]['text']] == [('request-2', 'speakable')]
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_voice_acknowledges_work_and_keeps_talking_before_the_native_receipt(tmp_path, monkeypatch):
    async def exercise():
        submission_started, release_submission = asyncio.Event(), asyncio.Event()
        routes, submissions = [], []
        request = 'Cari jam buka perpustakaan besok.'
        question = 'Sambil itu, apa kabar?'

        class Jobs(TranscriptJobs):
            async def route(self, _proposal, current, _previous, _context, *, session_id):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                routes.append(current)
                return {'status': 'task' if current == request else 'reply'}

            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                submissions.append((text, session_id))
                submission_started.set()
                await release_submission.wait()
                return {'status': 'queued', 'receipt': {
                    'session_id': session_id, 'admission_id': DM_ADMISSION_ID,
                }}

            async def observe_work(self, session_id, admission_id):
                assert (session_id, admission_id) == (ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)
                return {'status': 'queued', 'session_id': session_id, 'admission_id': admission_id}

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, transcript=''):
            await voice.on_data({'type': kind, 'turn': {
                'role': role, 'id': turn_id, 'transcript': transcript,
            }})

        try:
            await turn('turn.done', 'user', 'caller-1', request)
            launch = delegation([request])
            await voice.on_data(launch)
            await wait_until(lambda: len(appended) == 1)
            assert (appended[0]['delegation_item_id'], appended[0]['channel']) == ('request-1', 'speakable')
            assert 'sudut pandang orang pertama' in appended[0]['content'][0]['text']
            # Submission can start before the provider acknowledges the speech request.
            await asyncio.wait_for(submission_started.wait(), 1)
            await acknowledge(voice, 'request-1')
            await turn('turn.created', 'assistant', 'launch')
            await feed_output(voice, remote, 1500)
            await turn('turn.done', 'assistant', 'launch', 'Aku cek jam bukanya untuk besok.')

            await turn('turn.created', 'user', 'caller-2')
            await turn('turn.done', 'user', 'caller-2', question)
            await voice.on_data(delegation([question], item_id='request-2', turn_id='caller-2'))
            await wait_until(lambda: len(appended) == 2)
            assert (appended[1]['delegation_item_id'], appended[1]['channel']) == ('request-2', 'speakable')
            await acknowledge(voice, 'request-2')
            await turn('turn.created', 'assistant', 'small-talk')
            await feed_output(voice, remote, 2500)
            await turn('turn.done', 'assistant', 'small-talk', 'Baik, aku di sini.')
            assert audio_messages(sent) == [
                adapter.audio_envelope(struct.pack('<h', value) * 480) for value in (1500, 2500)
            ]
            assert DM_ADMISSION_ID not in voice.call_work

            await voice.on_data(launch)
            release_submission.set()
            await wait_until(lambda: DM_ADMISSION_ID in voice.work_followers)
            assert len(appended) == 2
            assert voice.call_work == {DM_ADMISSION_ID}
            assert routes == [request, question]
            assert submissions == [(request, ACTIVE_WHATSAPP_SESSION)]
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('kind, question, route', [
    ('photo', 'Apa warna payung di foto tadi?', 'follow'),
    ('document', 'Apa judul dokumen yang baru kukirim?', 'follow'),
    ('voice', 'Apa yang terdengar di rekaman itu?', 'follow'),
    ('video', 'Apa yang terlihat di video tadi?', 'follow'),
    ('photo', 'Apa warna payung di foto tadi?', 'task'),
])
def test_late_dm_attachment_is_bound_before_the_only_route_decision(
    tmp_path, monkeypatch, kind, question, route,
):
    async def exercise():
        output = 'Ini hasil dari kirimanmu.'
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'started', 'kind': kind, 'attachment': True, 'text': 'Tolong lihat isinya.'}
        routes, observed, submitted = [], [], []

        class Jobs:
            async def route(self, _proposal, current, _previous, _context, *, session_id):
                assert current == question and session_id == ACTIVE_WHATSAPP_SESSION
                routes.append(current)
                return {'status': route, 'admission_id': DM_ADMISSION_ID, 'work': [work]}

            async def observe_work(self, session_id, admission_id):
                observed.append((session_id, admission_id))
                return {'status': 'completed', 'session_id': session_id,
                        'admission_id': admission_id, 'output': output}

            async def submit(self, _submission_id, text, **_kwargs):
                submitted.append(text)
                return {'status': 'unavailable'}

            async def save_exchange(self, _session_id, _exchange, *, wait_for_lease=False):
                return {'status': 'saved'}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': question,
            }})
            await voice.on_data(delegation([question]))
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            assert routes == [question]
            if route == 'follow':
                assert appended[0]['channel'] == 'commentary'
                await wait_until(lambda: len(appended) == 2)
                await acknowledge(voice, 'request-1')
                assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)]
                assert output in appended[-1]['content'][0]['text']
            else:
                assert observed == []
                assert output not in appended[-1]['content'][0]['text']
            assert appended[-1]['channel'] == 'speakable'
        finally:
            await voice.close()
        assert submitted == []

    asyncio.run(exercise())


def test_new_question_about_a_completed_attachment_submits_the_selected_native_document(tmp_path, monkeypatch):
    async def exercise():
        question = 'Kalau dari dokumen itu, berapa rata-rata biaya per lokasi?'
        output = 'Rata-ratanya dua juta per lokasi.'
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                'status': 'completed', 'kind': 'document', 'attachment': True,
                'text': 'Rangkum dokumen anggaran.'}
        later_work = dict(work, admission_id='c' * 32, kind='photo', text='Periksa foto lokasi.')
        submitted = []

        class Jobs:
            async def route(self, _proposal, current, _previous, _context, *, session_id):
                assert current == question
                return {'status': 'task', 'admission_id': DM_ADMISSION_ID,
                        'work': [later_work, work]}

            async def submit(self, _submission_id, text, *, session_id, attachment_admission_id, spoken_at_ms=None):
                submitted.append((text, session_id, attachment_admission_id))
                return {'status': 'completed', 'output': output,
                        'receipt': {'session_id': session_id, 'admission_id': 'b' * 32}}

            async def observe_work(self, *_args):
                pytest.fail('A new question should not replay the existing summary')

            async def save_exchange(self, *_args, **_kwargs):
                pytest.fail('The native task already persists the new question')

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.dm_work = [later_work, work]
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': question,
            }})
            await voice.on_data(delegation([question]))
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            await wait_until(lambda: len(appended) == 2)
            await acknowledge(voice, 'request-1')
            assert submitted == [(question, ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)]
            assert appended[-1]['channel'] == 'speakable'
            assert output in appended[-1]['content'][0]['text']
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_follow_does_not_read_work_outside_the_bound_dm(tmp_path, monkeypatch):
    async def exercise():
        foreign_output = 'Hasil dari percakapan lain.'

        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'follow', 'admission_id': DM_ADMISSION_ID}

            async def observe_work(self, session_id, admission_id):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                return {'status': 'completed', 'session_id': 'other-whatsapp-dm',
                        'admission_id': admission_id, 'output': foreign_output}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('A rejected receipt must never start a new Hermes turn')

            async def save_exchange(self, _session_id, _exchange, *, wait_for_lease=False):
                return {'status': 'saved'}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.dm_work = [{'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                          'status': 'started', 'kind': 'text', 'text': 'Periksa dokumen ini.'}]
        try:
            question = 'Hasilnya sudah ada?'
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': question,
            }})
            await voice.on_data(delegation([question]))
            await wait_until(lambda: bool(appended))
            await acknowledge(voice, 'request-1')
            await wait_until(lambda: len(appended) == 2)
            assert foreign_output not in json.dumps(appended)
            await acknowledge(voice, 'request-1')
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('steer_status', ['queued', 'unknown'])
def test_correction_steers_the_same_native_dm_turn_once(tmp_path, monkeypatch, steer_status):
    async def exercise():
        correction = 'Yang tadi fokus ke hari Minggu saja.'
        output = 'Sudah diperbarui untuk hari Minggu.'
        steered, observed = [], []

        class Jobs:
            async def route(self, _proposal, current, _previous, _loaded_context, *, session_id):
                assert current == correction
                return {'status': 'steer', 'admission_id': DM_ADMISSION_ID}

            async def steer_work(self, session_id, admission_id, text):
                steered.append((session_id, admission_id, text))
                return {'status': steer_status}

            async def observe_work(self, session_id, admission_id):
                observed.append((session_id, admission_id))
                return {'status': 'completed', 'session_id': session_id,
                        'admission_id': admission_id, 'output': output}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('A correction to DM work must never submit a fallback turn')

            async def save_exchange(self, *_args, **_kwargs):
                pytest.fail('Hermes can already persist the correction as native input')

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.dm_work = [{'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID,
                          'status': 'started', 'kind': 'text', 'text': 'Buat jadwal akhir pekan.'}]
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': correction,
            }})
            event = delegation([correction])
            await voice.on_data(event)
            await wait_until(lambda: len(appended) == 1)
            assert appended[0]['channel'] == 'commentary'
            await voice.on_data(event)
            await voice.on_data(delegation([correction], item_id='duplicate-request'))
            await wait_until(lambda: any(msg['delegation_item_id'] == 'duplicate-request' for msg in appended))
            await acknowledge(voice, 'duplicate-request')
            await acknowledge(voice, 'request-1')
            await wait_until(lambda: len(appended) == 3)
            assert steered == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID, correction)]
            result = appended[-1]
            assert result['delegation_item_id'] == 'request-1'
            assert result['channel'] == 'speakable'
            if steer_status == 'queued':
                assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)]
                assert output in result['content'][0]['text']
            else:
                assert observed == []
                assert 'belum bisa memastikan arahan tadi diterima' in result['content'][0]['text']
            await acknowledge(voice, 'request-1')
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'assistant', 'id': 'reply-1'}})
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'assistant', 'id': 'reply-1', 'transcript': result['content'][0]['text'],
            }})
        finally:
            await voice.close()
        assert not voice.report.get('voice_exchanges_saved')

    asyncio.run(exercise())


@pytest.mark.parametrize('first_reply_signal', [None, 'turn-created', 'pcm'])
def test_context_question_with_early_false_proposals_is_answered_and_saved_once(
    tmp_path, monkeypatch, first_reply_signal,
):
    async def exercise():
        decisions, saved = [], []
        question = 'Apa kode yang kukirim lewat chat?'

        class Jobs:
            async def route(self, proposal, current, previous, loaded_context, *, session_id):
                decisions.append((proposal, current, previous, loaded_context))
                return {'status': 'reply'}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('The loaded-context answer must not submit a Hermes task')

            async def recall(self, *_args):
                pytest.fail('The loaded-context answer must not search history')

            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                saved.append((session_id, exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, remote, sent, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            voice.loaded_context = 'DM terbaru: Kode uji adalah Anggrek Nila.'
            await voice.on_data({'type': 'turn.created', 'turn': {'id': 'caller-1', 'role': 'user'}})
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': 'Apa kode'})
            await voice.on_data(delegation(['Cari kode baru'], item_id='early'))
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': 'yang kukirim lewat chat?'})
            await voice.on_data(delegation(['yang kukirim'], item_id='second'))
            await wait_until(lambda: len(appended) == 2)
            assert not decisions
            assert {item['delegation_item_id']: item['channel'] for item in appended} == {
                'early': 'commentary', 'second': 'commentary',
            }
            for item in appended:
                await acknowledge(voice, item['delegation_item_id'])
            assistant_started = {'type': 'turn.created', 'turn': {'id': 'answer', 'role': 'assistant'}}
            if first_reply_signal == 'turn-created':
                await voice.on_data(assistant_started)
            elif first_reply_signal == 'pcm':
                await feed_output(voice, remote, 2000)
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'caller-1', 'role': 'user', 'transcript': question,
            }})
            await wait_until(lambda: bool(decisions))
            assert decisions == [('Cari kode baru', question, '', voice.loaded_context)]
            if first_reply_signal:
                assert len(appended) == 2
            else:
                await wait_until(lambda: len(appended) == 3)
                assert (appended[2]['delegation_item_id'], appended[2]['channel']) == ('early', 'speakable')
                await acknowledge(voice, 'early')
            if first_reply_signal != 'turn-created':
                await voice.on_data(assistant_started)
            if first_reply_signal != 'pcm':
                await feed_output(voice, remote, 2000)
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'answer', 'role': 'assistant', 'transcript': 'Anggrek Nila.',
            }})
        finally:
            await voice.close()
        assert len(audio_messages(sent)) == 1
        assert saved == [(ACTIVE_WHATSAPP_SESSION, question, 'Anggrek Nila.')]
        assert not voice.report['backend_completed_delegations']

    asyncio.run(exercise())


def test_task_proposal_corrected_to_a_question_does_not_submit(tmp_path, monkeypatch):
    async def exercise():
        routed = []
        fragment = 'Buat laporan'
        corrected = 'Buat laporan yang kemarin perlu judul apa, sudah ada di chat?'

        class Jobs(TranscriptJobs):
            async def route(self, _proposal, current, _previous, _loaded_context, *, session_id):
                routed.append(current)
                return {'status': 'task' if current == fragment else 'reply'}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('The final question must not admit a task')

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.created', 'turn': {'id': 'caller-1', 'role': 'user'}})
            await voice.on_data(delegation([fragment]))
            await wait_until(lambda: len(appended) == 1)
            assert not routed
            await acknowledge(voice, 'request-1')
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'caller-1', 'role': 'user', 'transcript': corrected,
            }})
            await wait_until(lambda: len(appended) == 2)
            assert routed == [corrected]
            assert not voice.native_task_turns
            assert appended[1]['channel'] == 'speakable'
            await acknowledge(voice, 'request-1')
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('final_received', [True, False])
def test_call_close_preserves_only_final_owner_speech(tmp_path, monkeypatch, final_received):
    async def exercise():
        saved = []

        class Jobs:
            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                saved.append((session_id, exchange))
                return {'status': 'saved'}

        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, text):
            await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}})

        try:
            await turn('turn.done', 'user', 'user-1', 'Judul cerita kita Laut Biru.')
            await turn('turn.created', 'assistant', 'reply-1', '')
            await turn('turn.done', 'assistant', 'reply-1', 'Baik, aku ingat judulnya.')
            await turn('turn.created', 'user', 'user-2', 'Nama proyeknya')
            if final_received:
                await turn('turn.done', 'user', 'user-2', 'Nama proyeknya Anggrek Sore.')
                await turn('turn.done', 'user', 'user-2', 'Nama proyeknya Anggrek Sore.')
        finally:
            await voice.close()
        await voice.close()

        expected_count = 2 if final_received else 1
        assert len(saved) == expected_count
        session_id, exchange = saved[0]
        assert session_id == ACTIVE_WHATSAPP_SESSION
        assert 0 < exchange.pop('user_at_ms') <= exchange.pop('assistant_at_ms')
        assert exchange == {
            'exchange_id': uuid.uuid5(uuid.NAMESPACE_URL, f'wa-control-live:{tmp_path.name}:exchange:user-1').hex,
            'user': 'Judul cerita kita Laut Biru.', 'assistant': 'Baik, aku ingat judulnya.',
        }
        if final_received:
            session_id, exchange = saved[1]
            assert session_id == ACTIVE_WHATSAPP_SESSION
            assert exchange.pop('user_at_ms') > 0
            assert exchange == {
                'exchange_id': uuid.uuid5(uuid.NAMESPACE_URL, f'wa-control-live:{tmp_path.name}:exchange:user-2').hex,
                'user': 'Nama proyeknya Anggrek Sore.', 'assistant': '',
            }
        assert voice.report['voice_exchanges_saved'] == expected_count

    asyncio.run(exercise())


def test_spoken_reply_is_available_to_dm_before_the_next_spoken_turn(tmp_path, monkeypatch):
    async def exercise():
        saved = []

        class Jobs:
            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                saved.append((exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, text=''):
            await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}})

        try:
            await turn('turn.done', 'user', 'caller-1', 'Nama tokohnya Nara.')
            await turn('turn.created', 'assistant', 'answer-1')
            await turn('turn.done', 'assistant', 'answer-1', 'Aku ingat Nara.')
            await wait_until(lambda: len(saved) == 1)
            await turn('turn.created', 'assistant', 'answer-2')
            await turn('turn.done', 'assistant', 'answer-2', 'Dia tokoh utama.')
            await turn('turn.done', 'assistant', 'answer-2', 'Dia tokoh utama.')
            await wait_until(lambda: len(saved) == 2)
            assert not voice.closed
        finally:
            await voice.close()
        assert saved == [('Nama tokohnya Nara.', 'Aku ingat Nara.'), ('', 'Dia tokoh utama.')]

    asyncio.run(exercise())


def test_voice_reply_is_saved_with_its_user_turn_when_transcript_output_waits(tmp_path, monkeypatch):
    async def exercise():
        saved = []
        output_started, release_output = asyncio.Event(), asyncio.Event()

        class Jobs:
            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                saved.append((session_id, exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def send(message):
            transcript = message['serverContent'].get('outputTranscription', {})
            if transcript.get('text') == 'Aku ingat judul Laut Biru.':
                output_started.set()
                await release_output.wait()

        voice.send = send

        def turn(kind, role, turn_id, text=''):
            return {'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': text}}

        try:
            await voice.on_data(turn('turn.done', 'user', 'user-1', 'Judulnya Laut Biru.'))
            await voice.on_data(turn('turn.created', 'assistant', 'reply-1'))
            voice.connection._data(json.dumps(turn('turn.done', 'assistant', 'reply-1', 'Aku ingat judul Laut Biru.')))
            await asyncio.wait_for(output_started.wait(), 1)
            await voice.on_data(turn('turn.created', 'user', 'user-2', 'Tokohnya'))
            release_output.set()
            await voice.on_data(turn('turn.done', 'user', 'user-2', 'Tokohnya Nara.'))
            await voice.on_data(turn('turn.done', 'assistant', 'reply-1', 'Balasan lama.'))
            await voice.on_data(turn('turn.created', 'assistant', 'reply-2'))
            await voice.on_data(turn('turn.done', 'assistant', 'reply-2', 'Aku ingat tokohnya Nara.'))
        finally:
            await voice.close()
        assert saved == [
            (ACTIVE_WHATSAPP_SESSION, 'Judulnya Laut Biru.', 'Aku ingat judul Laut Biru.'),
            (ACTIVE_WHATSAPP_SESSION, 'Tokohnya Nara.', 'Aku ingat tokohnya Nara.'),
        ]

    asyncio.run(exercise())


@pytest.mark.parametrize('spoken_reply', [True, False])
def test_call_close_does_not_write_a_second_exchange_for_a_native_task(tmp_path, monkeypatch, spoken_reply):
    async def exercise():
        submitted = []

        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                submitted.append((session_id, text))
                return {'status': 'completed', 'output': 'Catatan sudah selesai.',
                        'receipt': {'session_id': session_id, 'admission_id': 'native-task'}}

            async def save_exchange(self, *_args, **_kwargs):
                pytest.fail('The gateway already persists native tasks')

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': 'Buat catatan.',
            }})
            await voice.on_data(delegation(['Buat catatan.']))
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            await wait_until(lambda: len(appended) == 2)
            await acknowledge(voice, 'request-1')
            if spoken_reply:
                for kind in ('turn.created', 'turn.done'):
                    await voice.on_data({'type': kind, 'turn': {
                        'role': 'assistant', 'id': 'task-reply', 'transcript': 'Catatan sudah selesai.',
                    }})
        finally:
            await voice.close()
        assert submitted == [(ACTIVE_WHATSAPP_SESSION, 'Buat catatan.')]
        assert not voice.report.get('voice_exchanges_saved')

    asyncio.run(exercise())


@pytest.mark.parametrize(('route_status', 'submit_status'), [
    ('task', 'queued'), ('task', 'unknown'), ('task', 'failed'), ('reply', None),
])
def test_next_turn_does_not_save_exchange_before_pending_route_finishes(
    tmp_path, monkeypatch, route_status, submit_status,
):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        saved, submitted = [], []
        request = 'Buat catatan.'

        class Jobs:
            async def route(self, *_args, session_id):
                entered.set()
                await release.wait()
                return {'status': route_status}

            async def submit(self, _submission_id, text, *, session_id, spoken_at_ms=None):
                submitted.append((session_id, text))
                if submit_status == 'queued':
                    return {'status': 'queued', 'receipt': {'session_id': session_id, 'admission_id': DM_ADMISSION_ID}}
                return {'status': submit_status}

            async def observe_work(self, session_id, admission_id):
                return {'status': 'queued', 'session_id': session_id, 'admission_id': admission_id}

            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                saved.append((exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())

        async def turn(kind, role, turn_id, transcript=''):
            await voice.on_data({'type': kind, 'turn': {
                'role': role, 'id': turn_id, 'transcript': transcript,
            }})

        try:
            await turn('turn.done', 'user', 'caller-1', request)
            await voice.on_data(delegation([request]))
            await asyncio.wait_for(entered.wait(), 1)
            await turn('turn.created', 'assistant', 'answer-1')
            await turn('turn.done', 'assistant', 'answer-1', 'Baik.')
            await turn('turn.created', 'user', 'caller-2')
            await asyncio.sleep(0)
            assert not saved
            flush = asyncio.create_task(voice.flush_history())
            await asyncio.sleep(0)
            assert not flush.done()
            release.set()
            await wait_until(lambda: len(appended) == 1)
            await acknowledge(voice, 'request-1')
            if route_status == 'task' and submit_status != 'queued':
                await wait_until(lambda: len(appended) == 2)
                await acknowledge(voice, 'request-1')
            assert await asyncio.wait_for(flush, 1) == 'saved'
        finally:
            await voice.close()
        assert submitted == ([(ACTIVE_WHATSAPP_SESSION, request)] if route_status == 'task' else [])
        assert saved == ([] if submit_status in {'queued', 'unknown'} else [(request, 'Baik.')])

    asyncio.run(exercise())
