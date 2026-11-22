"""Phone startup request contract and overlap; no subscription inference."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import call_adapter as adapter


async def start_phone(tmp_path, monkeypatch, *, require_overlap=False, agent_jobs=None, requests=None,
                      direction_reader=None, previous_voice_results=None):
    if requests is None:
        requests = []
    ice_started, provider_started = asyncio.Event(), asyncio.Event()

    class LocalServer:
        def __init__(self, _command):
            self.events, self.pending = asyncio.Queue(), {}
            self.process = type('LocalProcess', (), {'pid': None})()

        async def start(self):
            provider_started.set()
            if require_overlap:
                try:
                    await asyncio.wait_for(ice_started.wait(), .2)
                except TimeoutError:
                    pytest.fail('ICE gathering still waits for provider startup instead of overlapping it')

        async def send(self, message):
            pass

        async def call(self, method, params=None, timeout=35):
            requests.append((method, params))
            if method == 'account/read':
                return {'account': {'type': 'chatgpt'}}
            if method == 'thread/start':
                return {'thread': {'id': 'local-voice-fixture'}}
            if method == 'thread/realtime/start':
                if not voice.connection.answer.done():
                    voice.connection.answer.set_result('fixture-answer-sdp')
                voice.connection.connected.set()
                await voice.connection.on_data({'type': 'session.started'})
            return {}

        async def close(self):
            pass

    class LocalPeer:
        connectionState = 'closed'

        def __init__(self):
            self.localDescription = None

        async def createOffer(self):
            return 'fixture-offer-sdp'

        async def setLocalDescription(self, offer):
            ice_started.set()
            if require_overlap:
                await provider_started.wait()
            self.localDescription = type('Description', (), {'sdp': offer})()

        async def setRemoteDescription(self, answer):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(adapter, 'AppServer', LocalServer)
    voice = adapter.LiveCall(tmp_path, lambda _message: None, agent_jobs=agent_jobs,
                              direction_reader=direction_reader)
    voice.previous_voice_results = previous_voice_results
    await voice.connection.peer.close()
    voice.connection.peer = LocalPeer()
    try:
        await voice.start({
            'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': 'irrelevant-gemini-voice'}}},
            'systemInstruction': {'parts': [{'text': 'Untrusted facade persona must not replace local identity.'}]},
        })
        return requests
    finally:
        await voice.close()


def test_phone_start_requests_juniper_without_changing_subscription_route(tmp_path, monkeypatch):
    requests = asyncio.run(start_phone(tmp_path, monkeypatch))
    params = next(params for method, params in requests if method == 'thread/realtime/start')
    assert params['voice'] == 'juniper', 'The phone still requests the old masculine voice'
    assert params['transport'] == {'type': 'webrtc', 'sdp': 'fixture-offer-sdp'}
    assert params['clientManagedHandoffs'] is True
    thread = next(params for method, params in requests if method == 'thread/start')
    assert thread['modelProvider'] == 'openai'


@pytest.mark.parametrize('session_id', ['same-dm', 'new-dm'])
def test_replaced_peer_follows_only_admitted_work_from_the_same_dm(tmp_path, monkeypatch, session_id):
    recovered = []

    class Jobs:
        async def prepare_context(self):
            return {'status': 'ready', 'session_id': session_id, 'history': []}

    def watch(_self, admission_id, item_id, turn_id):
        assert item_id is None and turn_id is None
        recovered.append(admission_id)

    monkeypatch.setattr(adapter.LiveCall, 'watch_dm_work', watch)
    requests = asyncio.run(start_phone(
        tmp_path, monkeypatch, agent_jobs=Jobs(), previous_voice_results=(
            'same-dm', {'already-spoken'}, {'dm-only'}, {'kept', 'already-spoken', 'dm-only'},
        ),
    ))
    params = next(params for method, params in requests if method == 'thread/realtime/start')
    assert recovered == (['kept'] if session_id == 'same-dm' else [])
    assert ('dm-only' in str(params['initialItems'])) == (session_id == 'same-dm')


def test_phone_identity_and_language_reach_initial_developer_context(tmp_path, monkeypatch):
    requests = asyncio.run(start_phone(tmp_path, monkeypatch))
    params = next(params for method, params in requests if method == 'thread/realtime/start')
    items = params.get('initialItems', [])
    assert items and items[0]['role'] == 'developer', 'Phone identity has no initial developer context'
    identity = items[0]['text']
    assert 'pemilik akun' in identity
    assert 'Ikuti bahasa dan alur percakapan pemilik akun dengan wajar.' in identity
    assert 'hangat dan ekspresif sesuai suasana obrolan' in identity
    assert 'Mulai dengan jawaban atau tindakan yang diminta.' in identity
    assert 'Lewati pembuka yang hanya menyatakan bahwa kamu paham' in identity
    assert 'sebut singkat rencanamu sekali jika berguna' in identity
    assert 'Sebut pekerjaan selesai atau hasil terkirim hanya jika terbukti.' in identity
    assert 'apa yang sedang kamu kerjakan dengan sudut pandang orang pertama' in identity
    assert identity.startswith('Kamu adalah Hermes Agent.\n\n')
    assert 'Lanjutkan percakapan DM WhatsApp dengan pemilik akun lewat telepon ini.' in identity
    assert 'Minta persetujuan atau penjelasan hanya jika pekerjaan memang membutuhkannya.' in identity
    assert 'pesan suara yang dikirim ke DM' in identity
    assert identity == params['prompt']
    assert 'Untrusted facade persona' not in params['prompt']
    assert 'coba tugas catatan uji' not in params['prompt'].lower()
    assert params['includeStartupContext'] is False


@pytest.mark.parametrize(('direction', 'opening'), [
    ('inbound', 'Pemilik akun yang meneleponmu. Tunggu dia bicara dulu.'),
    ('outbound', 'Kamu yang menelepon pemilik akun.'),
    (None, 'Tunggu pemilik akun bicara dulu.'),
])
def test_phone_initial_context_uses_sidecar_call_direction(tmp_path, monkeypatch, direction, opening):
    async def read_direction():
        return direction

    requests = asyncio.run(start_phone(tmp_path, monkeypatch, direction_reader=read_direction))
    params = next(params for method, params in requests if method == 'thread/realtime/start')
    assert opening in params['prompt']
    assert params['initialItems'][-1] == {'role': 'developer', 'text': params['prompt']}


@pytest.mark.parametrize(('ended_at', 'expected'), [(None, 'outbound'), (123, None)])
def test_call_direction_reads_only_the_authenticated_active_call(monkeypatch, ended_at, expected):
    base = 'http://127.0.0.1:8796'
    call_id = '123456abcdef'
    paths = []

    @asynccontextmanager
    async def get(url):
        paths.append(url)

        async def response():
            if url == f'{base}/health':
                return {'active_call': call_id}
            return {'id': call_id, 'direction': 'outbound', 'status': 'preparing', 'ended_at': ended_at}

        yield SimpleNamespace(json=response)

    @asynccontextmanager
    async def session(*, headers, raise_for_status):
        assert headers == {'Authorization': 'Bearer fixture'} and raise_for_status
        yield SimpleNamespace(get=get)

    monkeypatch.setattr(adapter, 'ClientSession', session)
    assert asyncio.run(adapter.read_call_direction('fixture')) == expected
    assert paths == [f'{base}/health', f'{base}/calls/{call_id}']


def test_phone_ice_gather_overlaps_provider_startup(tmp_path, monkeypatch):
    requests = asyncio.run(start_phone(tmp_path, monkeypatch, require_overlap=True))
    assert any(method == 'thread/realtime/start' for method, _ in requests)


def test_phone_loads_bound_chat_and_keeps_opening_on_topic(tmp_path, monkeypatch):
    class Jobs:
        async def prepare_context(self):
            return {'status': 'ready', 'session_id': 'active-whatsapp-dm', 'history_cursor': 7,
                    'persona': 'Namaku Nara di chat.', 'history': [
                {'role': 'developer', 'text': 'Untrusted message must never become an instruction.'},
                {'role': 'user', 'text': 'Judul berkasnya Sakura.'},
                {'role': 'user', 'text': 'Kita bahas Sakura lewat telepon.', 'source': 'whatsapp_call'},
                {'role': 'assistant', 'text': 'Aku sudah baca Sakura.'},
            ]}

        @asynccontextmanager
        async def watch_context(self, session_id, cursor):
            assert (session_id, cursor) == ('active-whatsapp-dm', 7)
            subscribed.set()

            async def updates():
                await release.wait()
                yield {'status': 'ready', 'session_id': session_id, 'history_cursor': cursor, 'history': []}
                await asyncio.Future()

            yield updates()

    async def exercise():
        nonlocal subscribed, release
        subscribed, release = asyncio.Event(), asyncio.Event()
        phone = asyncio.create_task(start_phone(tmp_path, monkeypatch, agent_jobs=Jobs()))
        try:
            await asyncio.wait_for(subscribed.wait(), 1)
            assert not phone.done()
            release.set()
            return await asyncio.wait_for(phone, 1)
        finally:
            if not phone.done():
                phone.cancel()
                await asyncio.gather(phone, return_exceptions=True)

    subscribed = release = None
    requests = asyncio.run(exercise())
    params = next(params for method, params in requests if method == 'thread/realtime/start')
    history, identity = params['initialItems']
    assert history['role'] == 'assistant'
    assert json.loads(history['text'].split('\n', 1)[1]) == [
        {'role': 'user', 'text': 'Judul berkasnya Sakura.'},
        {'role': 'user', 'text': 'Kita bahas Sakura lewat telepon.', 'source': 'whatsapp_call'},
        {'role': 'assistant', 'text': 'Aku sudah baca Sakura.'},
    ]
    assert identity == {'role': 'developer', 'text': params['prompt']}
    opening = identity['text']
    assert opening.startswith('Namaku Nara di chat.\n\n')
    assert adapter.FALLBACK_PERSONA not in opening
    assert 'Gunakan identitas dan cara bicaramu yang sama seperti di DM.' in opening
    assert 'Ikuti bahasa dan alur percakapan pemilik akun dengan wajar.' in opening
    assert 'Gunakan konteks percakapan bersama, termasuk DM, saat relevan.' in opening
    assert 'Biarkan pemilik akun menentukan topik pembuka.' in opening
    assert 'Namaku Nara di chat.' not in (tmp_path/'report.json').read_text()
    assert 'Sakura' not in opening
    assert 'active-whatsapp-dm' not in repr(params)
    assert 'Sakura' not in (tmp_path/'report.json').read_text()
    assert 'active-whatsapp-dm' not in (tmp_path/'report.json').read_text()


def test_startup_history_keeps_summary_and_latest_text_within_budget():
    prompt = 'Persona dan petunjuk pembuka.'
    summary = {'role': 'assistant', 'text': 'Ringkasan yang masih aktif.', 'summary': True}
    older = {'role': 'user', 'text': 'Pesan lama.', '_message_refs': ['older']}
    recent = {'role': 'user', 'text': '雪\n' * 18000 + ' Kata uji: Sakura.', '_message_refs': ['recent']}
    latest = {'role': 'assistant', 'text': 'Pesan lengkap.', '_message_refs': ['latest']}
    seeded_refs = set()
    item = adapter.startup_chat_item([summary, older, recent, latest], prompt, seeded_refs=seeded_refs)
    rows = json.loads(item['text'].split('\n', 1)[1])
    assert rows[0] == summary
    assert rows[1]['text'].endswith('Kata uji: Sakura.')
    assert rows[-1] == {'role': 'assistant', 'text': 'Pesan lengkap.'}
    assert seeded_refs == {'latest'}
    assert len((item['text'] + prompt).encode('utf-8')) <= adapter.STARTUP_HISTORY_BYTES


def test_new_chat_context_is_sent_in_order_on_the_silent_channel(tmp_path, monkeypatch):
    async def exercise():
        sent, requests = [], []
        message = '雪' * 210 + ' Kenanga Biru.'

        async def send(message):
            pytest.fail(f'Chat update unexpectedly sent audio or speech: {message}')

        class Jobs:
            @asynccontextmanager
            async def watch_context(self, session_id, cursor):
                requests.append((session_id, cursor))

                async def updates():
                    yield {'status': 'ready', 'session_id': session_id, 'history_cursor': 12,
                           'history': [{'role': 'user', 'text': message},
                                       {'role': 'assistant', 'text': 'Aku ingat.'}]}
                    yield {'status': 'session_changed'}

                yield updates()

        def append(raw):
            sent.append(json.loads(raw))
            voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))

        voice = adapter.LiveCall(tmp_path, send, agent_jobs=Jobs())
        voice.hermes_session_id = 'active-whatsapp-dm'
        voice.connection.channel = SimpleNamespace(readyState='open', send=append)
        try:
            await asyncio.wait_for(voice.follow_chat(10), 1)
            assert requests == [('active-whatsapp-dm', 10)]
            assert [(item['type'], item['channel']) for item in sent] == [
                ('session.context.append', 'commentary'), ('session.context.append', 'commentary'),
                ('session.context.append', 'commentary'),
            ]
            texts = [item['content'][0]['text'] for item in sent]
            assert all(len(text.encode('utf-8')) <= adapter.CHAT_UPDATE_CHUNK_BYTES for text in texts)
            assert ''.join(text.split(': ', 1)[1] for text in texts[:2]) == message
            assert texts[2].endswith('Aku ingat.')
            assert voice.report['chat_updates_applied'] == 3
            assert voice.report['chat_updates_status'] == 'session_changed'
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_dm_attachments_update_call_context_without_a_text_history_change(tmp_path, monkeypatch):
    async def exercise():
        sent, polls = [], []

        async def send(_message):
            pytest.fail('A new DM attachment must not speak or send audio on its own')

        class Jobs:
            @asynccontextmanager
            async def watch_context(self, session_id, cursor):
                polls.append((session_id, cursor))

                async def updates():
                    for completed in (False, True, True):
                        work = [{'session_id': session_id, 'admission_id': 'a' * 32,
                                 'kind': 'document', 'text': 'Dokumen ' + '🟣' * 200, 'attachment': True,
                                 'files': ['anggaran.pdf'], 'status': 'completed' if completed else 'started'}]
                        if completed:
                            work.insert(0, {'session_id': session_id, 'admission_id': 'b' * 32,
                                            'kind': 'video', 'text': 'Video uji', 'attachment': True,
                                            'files': ['lokasi.mp4'], 'status': 'completed'})
                        yield {'status': 'ready', 'session_id': session_id, 'history_cursor': cursor,
                               'history': [], 'work': work}
                    yield {'status': 'session_changed'}

                yield updates()

        def append(raw):
            sent.append(json.loads(raw))
            voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))

        voice = adapter.LiveCall(tmp_path, send, agent_jobs=Jobs())
        voice.hermes_session_id = 'active-whatsapp-dm'
        voice.connection.channel = SimpleNamespace(readyState='open', send=append)
        try:
            await asyncio.wait_for(voice.follow_chat(10), 1)
            assert polls == [('active-whatsapp-dm', 10)]
            assert all(item['type'] == 'session.context.append' and item['channel'] == 'commentary'
                       for item in sent)
            texts = [item['content'][0]['text'] for item in sent]
            assert all(len(text.encode('utf-8')) <= adapter.CHAT_UPDATE_CHUNK_BYTES for text in texts)
            batches = []
            for text in texts:
                if text.startswith('Lanjutan status pekerjaan di DM: '):
                    batches[-1] += text.split(': ', 1)[1]
                else:
                    batches.append(text.split('\n', 1)[1])
            work_updates = [json.loads(line) for batch in batches for line in batch.splitlines()]
            assert [(entry['kind'], entry['status'], entry['attachment']) for entry in work_updates] == [
                ('document', 'started', True), ('video', 'completed', True), ('document', 'completed', True),
            ]
            assert [entry['text'] for entry in work_updates] == ['Dokumen ' + '🟣' * 200, 'Video uji',
                                                                'Dokumen ' + '🟣' * 200]
            assert [entry['files'] for entry in work_updates] == [['anggaran.pdf'], ['lokasi.mp4'], ['anggaran.pdf']]
            assert 'Video uji' in voice.loaded_context
            assert [(entry['admission_id'], entry['status']) for entry in voice.dm_work] == [
                ('b' * 32, 'completed'), ('a' * 32, 'completed'),
            ]
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_dm_completion_from_snapshot_never_requests_a_second_audio_reply(tmp_path, monkeypatch):
    async def exercise():
        spoken, receipts, sent = [], [], []
        old = {'session_id': 'active-whatsapp-dm', 'admission_id': 'a' * 32,
               'seq': 13, 'kind': 'document', 'status': 'completed', 'text': 'Dokumen lama.'}
        fresh = {**old, 'seq': 14, 'admission_id': 'b' * 32, 'text': 'Tolong olah berkas yang baru.'}
        voice_work = {**fresh, 'seq': 15, 'admission_id': 'c' * 32, 'kind': 'voice_call'}
        output = 'Sudah kuolah. Laporan dan gambar ada di DM.'
        repeated_poll = asyncio.Event()

        class Jobs:
            @asynccontextmanager
            async def watch_context(self, session_id, cursor):
                async def updates():
                    snapshot = {'status': 'ready', 'session_id': session_id, 'history_cursor': cursor,
                                'history': [], 'work': [voice_work, fresh, old]}
                    yield snapshot
                    yield snapshot
                    repeated_poll.set()
                    await asyncio.Future()

                yield updates()

            async def observe_work(self, session_id, admission_id):
                receipts.append((session_id, admission_id))
                return {'status': 'completed', 'output': output, 'admission_id': admission_id,
                        'session_id': session_id}

        async def send(_message):
            pass

        def append(raw):
            sent.append(json.loads(raw))
            voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))

        monkeypatch.setattr(adapter, 'RESULT_SPEECH_QUIET_SECONDS', 0)
        voice = adapter.LiveCall(tmp_path, send, agent_jobs=Jobs())
        voice.hermes_session_id = old['session_id']
        voice.connection.thread_id = 'live-thread'
        voice.update_dm_work([old, {**fresh, 'status': 'started'}])
        voice.connection.channel = SimpleNamespace(readyState='open', send=append)

        async def call(method, params, *, timeout):
            assert method == 'thread/realtime/appendSpeech'
            spoken.append(params['text'])
            return {}

        voice.connection.server.call = call
        try:
            voice.spawn(voice.follow_chat(10))
            await asyncio.wait_for(repeated_poll.wait(), 1)
            assert not receipts and not spoken
            assert not voice.work_followers and not voice.offered_work_results
            assert all(event['type'] == 'session.context.append' for event in sent)
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_chat_ack_timeout_replaces_voice_instead_of_continuing_with_stale_context(tmp_path, monkeypatch):
    async def exercise():
        sent, cursors = [], []

        class Jobs:
            @asynccontextmanager
            async def watch_context(self, session_id, cursor):
                cursors.append(cursor)

                async def updates():
                    yield {'status': 'ready', 'session_id': session_id, 'history_cursor': 12,
                           'history': [{'role': 'user', 'text': 'Pesan uji.'}]}
                    pytest.fail('The old provider should stop draining updates after an uncertain append')

                yield updates()

        monkeypatch.setattr(adapter, 'RESULT_ACK_TIMEOUT', .001)
        voice = adapter.LiveCall(tmp_path, lambda _message: None, agent_jobs=Jobs())
        voice.hermes_session_id = 'active-whatsapp-dm'
        voice.connection.channel = SimpleNamespace(readyState='open', send=sent.append)
        try:
            await asyncio.wait_for(voice.follow_chat(10), 1)
            assert cursors == [10]
            assert len(sent) == 1
            assert voice.failed.is_set()
            assert voice.replace_voice
            assert voice.report['chat_updates_status'] == 'ack_timeout'
            assert voice.report.get('chat_updates_applied', 0) == 0
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_phone_does_not_open_voice_without_the_active_whatsapp_chat(tmp_path, monkeypatch):
    class Jobs:
        async def prepare_context(self):
            return {'status': 'unavailable'}

    requests = []
    with pytest.raises(RuntimeError, match='Active WhatsApp chat context is unavailable'):
        asyncio.run(start_phone(tmp_path, monkeypatch, agent_jobs=Jobs(), requests=requests))
    assert not any(method == 'thread/realtime/start' for method, _ in requests)
