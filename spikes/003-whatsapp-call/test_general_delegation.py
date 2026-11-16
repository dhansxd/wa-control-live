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


async def acknowledge(voice, item_id):
    await voice.on_data({'type':'delegation.context.appended', 'delegation_item_id':item_id})


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


def test_route_cannot_submit_to_another_dm(tmp_path, monkeypatch):
    async def exercise():
        class Jobs:
            async def route(self, *_args, session_id):
                assert session_id == ACTIVE_WHATSAPP_SESSION
                return {'status': 'session_changed'}

            async def submit(self, *_args, **_kwargs):
                pytest.fail('A call cannot submit into a different WhatsApp session')

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
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        routed, submitted = [], []
        receipt = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': 'native-admission'}

        class Jobs:
            async def route(self, proposal, current, previous, loaded_context, *, session_id):
                routed.append(current)
                assert proposal == current == ''.join(request_parts)
                return {'status': 'task'}

            async def submit(self, submission_id, text, *, session_id):
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
            assert not audio_messages(sent)
            await feed_output(voice, remote, 1500)
            assert audio_messages(sent) == [adapter.audio_envelope(struct.pack('<480h', *([1500] * 480)))]
            assert len(appended) == 1
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
            assert voice.report['acknowledged_delegations'] == ['request-1', 'request-1']
            await feed_output(voice, remote, 2000)
            await wait_until(lambda: voice.report['result_audio_forwarded_bytes'] == 960)
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('new_speaker', ['assistant', 'caller', 'same_turn_assistant'])
@pytest.mark.parametrize('speech_confirmed', [True, False])
def test_previous_task_speaks_once_after_the_current_exchange(tmp_path, monkeypatch, new_speaker, speech_confirmed):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        speech_accepted = asyncio.Event()
        submitted, speech_requests, saved = [], [], []

        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id):
                submitted.append((text, session_id))
                entered.set()
                await release.wait()
                return {'status': 'completed', 'output': 'Catatan sudah selesai.',
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

            question = 'Kamu masih dengar aku?'
            if new_speaker != 'same_turn_assistant':
                await turn('turn.created', 'user', 'caller-2')
            if new_speaker == 'assistant':
                await turn('turn.done', 'user', 'caller-2', question)
            if new_speaker in {'assistant', 'same_turn_assistant'}:
                await turn('turn.created', 'assistant', 'answer-2')
            release.set()
            await wait_until(lambda: len(appended) == 2)
            assert appended[-1]['delegation_item_id'] == 'request-1'
            assert appended[-1]['channel'] == 'commentary'
            await asyncio.sleep(0)
            assert speech_requests == []
            if new_speaker == 'caller':
                await turn('turn.done', 'user', 'caller-2', question)
                await turn('turn.created', 'assistant', 'answer-2')
            await feed_output(voice, remote, 2500)
            conversation_pcm = struct.pack('<480h', *([2500] * 480))
            assert audio_messages(sent) == [adapter.audio_envelope(conversation_pcm)]
            assert speech_requests == []
            await turn('turn.done', 'assistant', 'answer-2', 'Aku dengar.')
            assert sent[-1]['serverContent']['outputTranscription']['text'] == 'Aku dengar.'

            await wait_until(lambda: bool(speech_requests))
            assert 'Catatan sudah selesai.' in speech_requests[0]
            # Speech can start after the current exchange even while the
            # quiet update for the previous exchange awaits acknowledgment.
            assert 'request-1' in voice.pending_deliveries
            caller = voice.current_spoken_turn
            follow = voice.spawn(voice.present_work_result(
                'concurrent-follow', caller, DM_ADMISSION_ID, 'Catatan sudah selesai.',
            ))
            await asyncio.sleep(0)
            assert len(appended) == 2
            assert not follow.done()
            speech_accepted.set()
            await acknowledge(voice, 'request-1')
            if speech_confirmed:
                await asyncio.wait_for(follow, 1)
                assert len(appended) == 2 and DM_ADMISSION_ID in voice.offered_work_results
                await turn('turn.created', 'assistant', 'late-result')
                await feed_output(voice, remote, 3000)
                await turn('turn.done', 'assistant', 'late-result', 'Catatan sudah selesai.')
                result_pcm = struct.pack('<480h', *([3000] * 480))
                assert audio_messages(sent) == [
                    adapter.audio_envelope(frame) for frame in (conversation_pcm, result_pcm)
                ]
            else:
                await wait_until(lambda: len(appended) == 3)
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
        assert saved == ([] if new_speaker == 'same_turn_assistant' else [(ACTIVE_WHATSAPP_SESSION, 'Aku dengar.')])

    asyncio.run(exercise())


@pytest.mark.parametrize('final_received', [False, True])
def test_empty_proposal_requires_its_final_transcript(tmp_path, monkeypatch, final_received):
    monkeypatch.setattr(adapter, 'POST_ACK_TRANSCRIPT_WAIT', .005)

    async def exercise():
        routed = []

        class Jobs:
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

            async def submit(self, _submission_id, text, *, session_id):
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
            assert appended[3]['delegation_item_id'] == 'first'
            assert submitted == ['Kerjakan satu kali']
            await acknowledge(voice, 'first')
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

            async def submit(self, _submission_id, _text, *, session_id):
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

        class Jobs:
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

            async def submit(self, _submission_id, request, *, session_id):
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
        class Jobs:
            pass

        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=Jobs())
        voice.connection.thread_id = 'call-thread'
        voice.current_spoken_turn = 'caller-2'
        voice.spoken_turn_final = True
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
            await voice.send_work_speech('Progres pekerjaan lain.', preview=True)
            repeat = voice.spawn(voice.present_work_result(
                'repeat-1', 'caller-2', DM_ADMISSION_ID, 'Hasil yang ingin diulang.', repeat=True,
            ))
            await wait_until(lambda: len(appended) == 1)
            assert appended[0]['channel'] == 'commentary'
            assert speech == ['Progres pekerjaan lain.']
            await voice.on_data({'type': 'turn.created', 'turn': {'id': 'preview', 'role': 'assistant'}})
            await voice.on_data({'type': 'turn.done', 'turn': {'id': 'preview', 'role': 'assistant'}})
            await asyncio.wait_for(repeat, 1)
            assert speech == ['Progres pekerjaan lain.', 'Hasil yang ingin diulang.']
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('speak_first', [False, True])
def test_phone_follows_and_steers_one_dm_document_then_keeps_result_in_dm(tmp_path, monkeypatch, speak_first):
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
                        'text_delivery': 'sent', 'has_attachments': True}

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
            assert 'Pengiriman lampiran belum terkonfirmasi' in dict(responses)['p-4']
            assert final not in dict(responses)['p-4']
            assert not announcements and not voice.offered_work_results
            assert final in voice.loaded_context
            assert steered == [items[1]] and set(observed) == {DM_ADMISSION_ID}
            await utter(5, items[4])
            await wait_until(lambda: any(item_id == 'p-5' for item_id, _ in responses))
            assert final in dict(responses)['p-5']
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('submit_status, first_observation', [
    ('queued', 'started'),
    ('pending', 'unavailable'),
    ('unknown', 'unavailable'),
])
def test_pending_voice_task_is_observed_and_result_is_offered_once(
    tmp_path, monkeypatch, submit_status, first_observation,
):
    async def exercise():
        submitted, observed = [], []
        observing, complete = asyncio.Event(), asyncio.Event()
        request, output = 'Riset tiga ide cerita.', 'Tiga ide cerita sudah dirangkum.'
        receipt = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID}

        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, text, *, session_id):
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
        try:
            await voice.on_data({'type': 'turn.done', 'turn': {
                'role': 'user', 'id': 'caller-1', 'transcript': request,
            }})
            event = delegation([request])
            await voice.on_data(event)
            await wait_until(lambda: len(appended) == 1)
            assert appended[0]['channel'] == 'commentary'
            assert 'sedang diproses' not in appended[0]['content'][0]['text']
            await acknowledge(voice, 'request-1')
            await wait_until(lambda: len(appended) == 2)
            expected_status = {
                'queued': 'menunggu giliran',
                'pending': 'status terkininya belum terkonfirmasi',
                'unknown': 'belum bisa memastikan',
            }[submit_status]
            assert expected_status in appended[1]['content'][0]['text']
            await acknowledge(voice, 'request-1')

            await wait_until(lambda: len(observed) == 1)
            voice.update_dm_work([{**receipt, 'status': 'completed', 'text': request}])
            await asyncio.wait_for(observing.wait(), 1)
            assert len(appended) == 2
            await feed_output(voice, remote, 1700)
            assert audio_messages(sent) == [adapter.audio_envelope(struct.pack('<480h', *([1700] * 480)))]

            await voice.on_data(event)
            complete.set()
            await wait_until(lambda: len(appended) == 3)
            await acknowledge(voice, 'request-1')
            assert [(msg['delegation_item_id'], msg['channel']) for msg in appended
                    if output in msg['content'][0]['text']] == [('request-1', 'speakable')]
            assert submitted == [(request, ACTIVE_WHATSAPP_SESSION)]
            assert observed == [(ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID)] * 2
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

            async def submit(self, _submission_id, text, *, session_id):
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
                assert appended[-1]['channel'] == 'commentary'
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

            async def submit(self, _submission_id, text, *, session_id, attachment_admission_id):
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


@pytest.mark.parametrize('already_answering', [False, True])
def test_context_question_with_early_false_proposals_is_answered_and_saved_once(
    tmp_path, monkeypatch, already_answering,
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
            if already_answering:
                await voice.on_data(assistant_started)
            await voice.on_data({'type': 'turn.done', 'turn': {
                'id': 'caller-1', 'role': 'user', 'transcript': question,
            }})
            await wait_until(lambda: bool(decisions))
            assert decisions == [('Cari kode baru', question, '', voice.loaded_context)]
            if already_answering:
                assert len(appended) == 2
            else:
                await wait_until(lambda: len(appended) == 3)
                assert (appended[2]['delegation_item_id'], appended[2]['channel']) == ('early', 'speakable')
                await acknowledge(voice, 'early')
                await voice.on_data(assistant_started)
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

        class Jobs:
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


def test_call_close_saves_completed_exchange_but_not_unanswered_speech(tmp_path, monkeypatch):
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
            await turn('turn.done', 'user', 'user-2', 'Ucapan ini belum dijawab.')
        finally:
            await voice.close()
        assert len(saved) == 1
        session_id, exchange = saved[0]
        assert session_id == ACTIVE_WHATSAPP_SESSION
        assert 0 < exchange.pop('user_at_ms') <= exchange.pop('assistant_at_ms')
        assert exchange == {
            'exchange_id': uuid.uuid5(uuid.NAMESPACE_URL, f'wa-control-live:{tmp_path.name}:exchange:user-1').hex,
            'user': 'Judul cerita kita Laut Biru.', 'assistant': 'Baik, aku ingat judulnya.',
        }
        assert voice.report['voice_exchanges_saved'] == 1

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


def test_native_task_reply_is_not_written_as_a_second_chat_exchange(tmp_path, monkeypatch):
    async def exercise():
        class Jobs:
            async def route(self, *_args, session_id):
                return {'status': 'task'}

            async def submit(self, _submission_id, _text, *, session_id):
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
            for kind in ('turn.created', 'turn.done'):
                await voice.on_data({'type': kind, 'turn': {
                    'role': 'assistant', 'id': 'task-reply', 'transcript': 'Catatan sudah selesai.',
                }})
        finally:
            await voice.close()
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

            async def submit(self, _submission_id, text, *, session_id):
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
            if route_status == 'task':
                await wait_until(lambda: len(appended) == 2)
                await acknowledge(voice, 'request-1')
            assert await asyncio.wait_for(flush, 1) == 'saved'
        finally:
            await voice.close()
        assert submitted == ([(ACTIVE_WHATSAPP_SESSION, request)] if route_status == 'task' else [])
        assert saved == ([] if submit_status in {'queued', 'unknown'} else [(request, 'Baik.')])

    asyncio.run(exercise())
