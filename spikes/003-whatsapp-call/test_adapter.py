"""Local protocol/media checks; no WhatsApp, Codex generation or auth access."""
import asyncio
import base64
from contextlib import asynccontextmanager, closing, suppress
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import av
import pytest
from aiohttp import ClientSession, WSMsgType, web


@pytest.mark.parametrize('use_codex', [True, False], ids=['codex', 'alternate'])
def test_native_dm_document_projects_one_job_to_voice(tmp_path, monkeypatch, use_codex):
    import call_adapter as adapter
    import live_agent
    from test_live_agent import add_dm_work

    SessionDB = pytest.importorskip('hermes_state').SessionDB
    monkeypatch.setattr('gateway.whatsapp_identity.expand_whatsapp_aliases', lambda _: {'1234567890'})
    session_id, admission_id = 'active-whatsapp', 'a' * 32
    filename = 'Catatan-produksi.pdf'
    prompt, final = 'Rangkum rencana produksi ini.', 'Hasil ringkas dari pekerjaan Hermes yang sama.'

    async def exercise(db):
        events, updates = asyncio.Queue(), asyncio.Queue()
        requests, spoken, contexts, openings = [], [], [], []
        receipt = {'status': 'terminal', 'outcome': 'completed', 'result': {'final_response': final},
                   'ref': {'session_id': session_id}, 'admission_id': admission_id}

        def resolve(_home, _owner):
            return session_id

        async def rpc(method, **params):
            assert params['session_id'] == session_id
            requests.append((method, params.get('admission_id')))
            if method == 'session.resume':
                return {'session_id': session_id, 'replay_epoch': 'epoch'}
            assert method == 'prompt.receipt' and params['admission_id'] == admission_id
            return receipt

        @asynccontextmanager
        async def connect():
            yield SimpleNamespace(rpc=rpc, events=events)

        def event(seq, kind, **payload):
            events.put_nowait({'method': 'event', 'params': {
                'type': kind, 'session_id': session_id, 'replay_epoch': 'epoch', 'seq': seq,
                'admission_id': admission_id, 'execution_generation': 1, 'payload': payload,
            }})

        class Jobs:
            async def prepare_context(self):
                return {'status': 'ready', 'session_id': session_id, 'history_cursor': 0, 'history': []}

            @asynccontextmanager
            async def watch_context(self, sid, cursor):
                watcher = asyncio.create_task(live_agent._watch_context(
                    tmp_path, '1234567890', {'session_id': sid, 'after_row_id': cursor},
                    resolve=resolve, connect=connect, emit=updates.put_nowait,
                ))

                async def stream():
                    while True:
                        yield await updates.get()

                try:
                    yield stream()
                finally:
                    watcher.cancel()
                    with suppress(asyncio.CancelledError):
                        await watcher

            async def observe_work(self, sid, work_id):
                return await live_agent._run(tmp_path, '1234567890', {
                    'action': 'observe_work', 'session_id': sid, 'admission_id': work_id,
                }, resolve=resolve, connect=connect)

        async def wait_for(predicate):
            async with asyncio.timeout(2):
                while not predicate():
                    await asyncio.sleep(0.002)

        async def send(_message):
            pass

        def receive_handoff(voice, item_id, text, channel):
            if channel == 'speakable':
                spoken.append(text)
            voice.spawn(voice.on_data({
                'type': 'delegation.context.appended', 'delegation_item_id': item_id,
            }))

        async def prepare_connection():
            pass

        async def open_audio(instructions, initial_items):
            openings.append((instructions, initial_items))

        class AlternateConnection:
            route = 'fixture'
            can_speak = True

            def __init__(self, voice):
                self.voice = voice

            async def prepare(self):
                await prepare_connection()

            async def start(self, instructions, initial_items):
                await open_audio(instructions, initial_items)

            async def append_context(self, text):
                contexts.append(text)

            def send_handoff(self, item_id, text, channel):
                receive_handoff(self.voice, item_id, text, channel)

            async def speak(self, text):
                spoken.append(text)

            def stop_input(self):
                pass

            async def close(self):
                pass

        connection_factory = adapter.CodexConnection if use_codex else AlternateConnection
        voice = adapter.LiveCall(tmp_path, send, agent_jobs=Jobs(), connection_factory=connection_factory)

        def append(raw):
            message = json.loads(raw)
            text = message['content'][0]['text']
            if item := message.get('delegation_item_id'):
                receive_handoff(voice, item, text, message['channel'])
            else:
                contexts.append(text)
                voice.spawn(voice.connection.on_data({'type': 'session.context.appended'}))

        async def announce(_method, params, **_kwargs):
            spoken.append(params['text'])

        if use_codex:
            monkeypatch.setattr(voice.connection, 'prepare', prepare_connection)
            monkeypatch.setattr(voice.connection, 'start', open_audio)
            voice.connection.thread_id = 'live-thread'
            voice.connection.channel = SimpleNamespace(readyState='open', send=append)
            voice.connection.server.call = announce
        try:
            await asyncio.wait_for(voice.start({}), 2)
            assert voice.hermes_session_id == session_id and voice.report['connected']
            assert len(openings) == 1 and openings[0][1][0]['text'] == openings[0][0]
            db.append_message(session_id, 'user', prompt)
            add_dm_work(db, session_id, admission_id, {'text': prompt, 'native_text_v1': {
                'source': {'platform': 'whatsapp', 'chat_type': 'dm',
                           'user_id': '1234567890', 'chat_id': '1234567890'},
                'message_type': 'document', 'media': [{'path': '/private/cache/report.pdf'}],
                'event': {'media_filenames': [filename], 'media_types': ['application/pdf']},
            }})
            event(10, 'session.info')
            await wait_for(lambda: filename in voice.loaded_context)
            event(11, 'tool.start', tool_id='read', name='read_file')
            await wait_for(lambda: 'read_file' in voice.progress_for_work(admission_id, 1))
            assert prompt in voice.loaded_context and '/private/cache/' not in voice.loaded_context
            assert not spoken

            db.append_message(session_id, 'assistant', final)
            db._execute_write(lambda conn: conn.execute(
                "UPDATE session_admissions SET status='terminal', outcome='completed' WHERE admission_id=?",
                (admission_id,),
            ))
            event(12, 'message.complete')
            event(12, 'message.complete')
            await wait_for(lambda: final in voice.loaded_context and 'completed' in voice.loaded_context)
            applied_context = '\n'.join(contexts)
            assert filename in applied_context and final in applied_context
            assert '/private/cache/' not in applied_context
            assert not spoken and not voice.work_followers

            voice.current_spoken_turn = 'spoken-request'
            await voice.follow_dm_work('request-result', 'spoken-request', admission_id, released_early=True)
            await wait_for(lambda: bool(spoken))
            assert len(spoken) == 1 and final in spoken[0]
            assert [(work['admission_id'], work['status']) for work in voice.dm_work] == [(admission_id, 'completed')]
            assert [request for request in requests if request[0] != 'session.resume'] == [
                ('prompt.receipt', admission_id),
            ]
            assert [row['content'] for row in db.get_messages_as_conversation(session_id)] == [prompt, final]
        finally:
            await voice.close()

    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session(session_id, 'whatsapp')
        asyncio.run(exercise(db))


def test_live_pcm_track_resamples_and_bounds_waiting_audio():
    from call_adapter import CallerTrack

    async def exercise():
        track = CallerTrack()
        for _ in range(5):
            track.feed(struct.pack('<h', 1500) * 320)
        first = await track.recv()
        assert first.sample_rate == 24000 and first.samples == 480
        assert max(struct.unpack('<480h', bytes(first.planes[0])[:960])) > 1000
        for _ in range(100):
            track.feed(bytes(640))
        assert len(track.buffer) <= 24000 * 2
        assert track.dropped_bytes > 0
        track.stop()
        from aiortc.mediastreams import MediaStreamError
        with pytest.raises(MediaStreamError):
            await track.recv()

    asyncio.run(exercise())


def test_caller_startup_zero_burst_does_not_delay_first_voice():
    from call_adapter import CallerTrack

    async def exercise():
        track = CallerTrack()
        silence = bytes(640)
        try:
            empty = await track.recv()
            assert not any(bytes(empty.planes[0])[:960])
            for _ in range(64):
                track.feed(silence)
            track.feed(struct.pack('<h', 1500) * 320)
            track.feed(silence)
            track.feed(silence)
            track.feed(struct.pack('<h', -1500) * 320)

            frames = [await track.recv() for _ in range(4)]
            # Sample the middle of each frame, away from resampler transitions.
            first, quiet1, quiet2, second = [
                struct.unpack_from('<h', bytes(frame.planes[0]), 480)[0] for frame in frames
            ]
            assert first > 1000
            assert quiet1 == quiet2 == 0
            assert second < -1000
        finally:
            track.stop()

    asyncio.run(exercise())


def test_output_frame_has_real_24khz_pcm_not_provider_text():
    from call_adapter import audio_envelope, resample_pcm
    resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
    frame = av.AudioFrame(format='s16', layout='mono', samples=960)
    frame.sample_rate = 48000
    frame.planes[0].update(struct.pack('<h', 2100) * 960)
    results = [audio_envelope(pcm) for part in (frame, None) for pcm in resample_pcm(part, resampler)]
    data = b''.join(base64.b64decode(r['serverContent']['modelTurn']['parts'][0]['inlineData']['data']) for r in results)
    assert len(data) == 960
    assert max(struct.unpack('<480h', data)) > 1900
    assert all(r['serverContent']['modelTurn']['parts'][0]['inlineData']['mimeType'] == 'audio/pcm;rate=24000' for r in results)


def test_event_mapping_and_actual_realtime_errors_remain_fatal(tmp_path):
    from call_adapter import LiveCall

    async def exercise():
        sent = []
        async def send(message):
            sent.append(message)
        voice = LiveCall(tmp_path, send)
        voice.connection.thread_id = 'this-thread'
        try:
            await voice.on_data({'type':'turn.done', 'turn':{'role':'user', 'transcript':'buat catatan uji'}})
            await voice.on_data({'type':'turn.done', 'turn':{'role':'assistant', 'transcript':'selesai'}})
            assert sent[0]['serverContent']['inputTranscription']['text'] == 'buat catatan uji'
            assert sent[1]['serverContent']['outputTranscription']['text'] == 'selesai'
            caller_event, assistant_event = voice.report['events']
            assert 0 <= caller_event['handled_at_seconds'] <= assistant_event['handled_at_seconds']
            await voice.connection.on_server({'method':'error','params':{'threadId':'this-thread','turnId':'unused','error':{'message':'unused model'}}})
            assert voice.report['backing_agent_errors'] and not voice.failed.is_set()
            await voice.connection.on_server({'method':'thread/realtime/error','params':{'message':'transport broke'}})
            assert voice.failed.is_set() and voice.report['error'] == 'transport broke'
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('provider_disconnect', [False, True])
def test_websocket_auth_single_call_disconnect_and_no_resume(tmp_path, provider_disconnect):
    from call_adapter import create_app

    async def exercise():
        created = []
        class Voice:
            def __init__(self, path, send):
                self.send = send
                self.report = {}
                self.frames = []
                self.closed = False
                self.failed = asyncio.Event()
                created.append(self)
            async def start(self, setup):
                self.setup = setup
            def feed(self, data):
                self.frames.append(data)
            async def close(self):
                self.closed = True
        app = create_app('fixture-local-token', tmp_path, voice_factory=Voice)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        try:
            async with ClientSession() as client:
                with pytest.raises(Exception) as exc:
                    await client.ws_connect(url + '/live')
                assert getattr(exc.value, 'status', None) == 401
                ws = await client.ws_connect(url + '/live', headers={'x-goog-api-key':'fixture-local-token'})
                await ws.send_json({'setup':{'model':'models/local-codex-bridge'}})
                assert (await ws.receive_json()) == {'setupComplete':{}}
                with pytest.raises(Exception) as exc:
                    await client.ws_connect(url + '/live', headers={'x-goog-api-key':'fixture-local-token'})
                assert getattr(exc.value, 'status', None) == 409
                await ws.send_json({'realtimeInput':{'audio':{'mimeType':'audio/pcm;rate=16000','data':base64.b64encode(bytes(640)).decode()}}})
                for _ in range(50):
                    if created[0].frames: break
                    await asyncio.sleep(.005)
                assert created[0].frames == [bytes(640)]
                if provider_disconnect:
                    created[0].failed.set()
                    assert (await ws.receive(timeout=1)).type == WSMsgType.CLOSE
                else:
                    await ws.close()
                for _ in range(50):
                    if created[0].closed: break
                    await asyncio.sleep(.005)
                assert created[0].closed
                async with client.get(url + '/health', headers={'x-goog-api-key':'fixture-local-token'}) as response:
                    status = await response.json()
                    assert not status['voice_ready']
                    assert status['last_failure'] == ('audio_connection_lost' if provider_disconnect else None)
                ws2 = await client.ws_connect(url + '/live', headers={'x-goog-api-key':'fixture-local-token'})
                await ws2.send_json({'setup':{'sessionResumption':{'handle':'old-session'}}})
                msg = await ws2.receive()
                assert msg.type == WSMsgType.CLOSE
                assert len(created) == 1  # no task/session restarted for resumption
        finally:
            await runner.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize('reset_before_setup', [False, True])
def test_active_phone_restarts_only_voice_when_native_dm_resets(tmp_path, reset_before_setup):
    from call_adapter import create_app

    async def exercise():
        voices, saved = [], []
        old_starting, new_session = asyncio.Event(), asyncio.Event()

        class Voice:
            def __init__(self, path, send):
                self.path, self.send = path, send
                self.hermes_session_id = 'old-dm' if not voices else 'new-dm'
                self.offered_work_results = {'old-work'}
                self.dm_only_results = {'old-dm-only'}
                self.call_work = {'accepted-voice-work'}
                self.closed = False
                self.failed = asyncio.Event()
                self.report = {}
                voices.append(self)

            async def start(self, _setup):
                if self.hermes_session_id == 'old-dm' and reset_before_setup:
                    old_starting.set()
                    await self.failed.wait()
                    raise RuntimeError('DM reset stopped old voice startup')
                if self.hermes_session_id == 'new-dm':
                    assert voices[0].closed
                    if reset_before_setup:
                        assert getattr(self, 'previous_voice_results', None) is None
                    else:
                        assert self.previous_voice_results == (
                            'old-dm', {'old-work'}, {'old-dm-only'}, {'accepted-voice-work'},
                        )
                    new_session.set()

            async def flush_history(self):
                saved.append(self.hermes_session_id)
                return 'saved'

            def session_changed(self):
                self.replace_voice = True
                self.failed.set()

            def feed(self, _pcm):
                asyncio.create_task(self.send({'serverContent': {'outputTranscription': {
                    'text': self.hermes_session_id,
                }}}))

            async def close(self):
                self.closed = True

        app = create_app('fixture-local-token', tmp_path, voice_factory=Voice)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        try:
            async with ClientSession() as client:
                async with client.post(url + '/session-reset', json={'session_id': 'old-dm'}) as response:
                    assert response.status == 401
            headers = {'Authorization': 'Bearer fixture-local-token'}
            async with ClientSession(headers=headers) as client, client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                if reset_before_setup:
                    await asyncio.wait_for(old_starting.wait(), 1)
                else:
                    assert await ws.receive_json(timeout=1) == {'setupComplete': {}}
                async with client.get(url + '/health') as response:
                    status = await response.json()
                    assert status['voice_ready'] == (not reset_before_setup)
                    assert status['last_failure'] is None
                for path, session, status in [
                    ('/flush-history', 'different-dm', 'idle'),
                    ('/flush-history', 'old-dm', 'saved'),
                    ('/session-reset', 'different-dm', 'idle'),
                    ('/session-reset', 'old-dm', 'reset'),
                ]:
                    async with client.post(url + path, json={'session_id': session}) as response:
                        assert await response.json() == {'status': status}
                if reset_before_setup:
                    assert await ws.receive_json(timeout=1) == {'setupComplete': {}}
                else:
                    assert await ws.receive_json(timeout=1) == {'serverContent': {'interrupted': True}}
                await asyncio.wait_for(new_session.wait(), 1)
                await ws.send_json({'realtimeInput': {'audio': {'mimeType': 'audio/pcm;rate=16000',
                                                               'data': 'AAA='}}})
                assert await ws.receive_json(timeout=1) == {'serverContent': {'outputTranscription': {
                    'text': 'new-dm',
                }}}
                async with client.get(url + '/health') as response:
                    status = await response.json()
                    assert status['voice_ready'] and status['last_failure'] is None
                async with client.post(url + '/flush-history', json={'session_id': 'old-dm'}) as response:
                    assert await response.json() == {'status': 'idle'}
                async with client.post(url + '/flush-history', json={'session_id': 'new-dm'}) as response:
                    assert await response.json() == {'status': 'saved'}
            assert len(voices) == 2
            assert saved == ['old-dm', 'new-dm']
        finally:
            await runner.cleanup()

    asyncio.run(exercise())


def test_connected_websocket_keeps_forwarding_after_the_old_duration_limit(tmp_path, monkeypatch):
    import call_adapter

    async def exercise():
        received = asyncio.Event()
        frames = []

        class Voice:
            def __init__(self, _path, _send):
                self.report = {}
                self.failed = asyncio.Event()
            async def start(self, _setup):
                pass
            def feed(self, data):
                frames.append(data)
                received.set()
            async def close(self):
                pass

        # Shorten the previous timer if it is reintroduced.
        monkeypatch.setattr(call_adapter, 'ACTIVE_CALL_SECONDS', 0.05, raising=False)
        runner = web.AppRunner(call_adapter.create_app('fixture-token', tmp_path, voice_factory=Voice))
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as client:
                ws = await client.ws_connect(f'http://127.0.0.1:{port}/live',
                                             headers={'x-goog-api-key': 'fixture-token'})
                await ws.send_json({'setup': {}})
                assert (await ws.receive_json()) == {'setupComplete': {}}
                await ws.send_json({'realtimeInput': {'text': 'WhatsApp connected'}})
                with pytest.raises(TimeoutError):
                    await ws.receive(timeout=0.15)
                pcm = bytes(640)
                await ws.send_json({'realtimeInput': {'audio': {
                    'mimeType': 'audio/pcm;rate=16000', 'data': base64.b64encode(pcm).decode(),
                }}})
                await asyncio.wait_for(received.wait(), 1)
                assert frames == [pcm]
                await ws.close()
        finally:
            await runner.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize(('mode', 'reason'), [
    ('error', 'audio_setup_failed'),
    ('event', 'audio_setup_failed'),
    ('login', 'codex_login_required'),
    ('timeout', 'audio_setup_timeout'),
])
def test_failed_start_is_closed_without_false_setup_success(tmp_path, mode, reason):
    from call_adapter import CodexLoginRequired, create_app

    async def exercise():
        instances = []
        class Failure:
            def __init__(self, path, send):
                self.closed = False
                self.report = {}
                self.failed = asyncio.Event()
                instances.append(self)
            async def start(self, setup):
                if mode == 'event':
                    self.failed.set()
                elif mode == 'login':
                    raise CodexLoginRequired('fixture login detail')
                elif mode == 'timeout':
                    raise TimeoutError('fixture timeout detail')
                else:
                    raise RuntimeError('fixture provider detail')
            async def close(self):
                self.closed = True
        runner = web.AppRunner(create_app('fixture-token', tmp_path, voice_factory=Failure))
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        try:
            async with ClientSession(headers={'x-goog-api-key':'fixture-token'}) as client:
                ws = await client.ws_connect(url + '/live')
                await ws.send_json({'setup':{}})
                assert (await ws.receive()).type == WSMsgType.CLOSE
                assert instances[0].closed
                async with client.get(url + '/health') as response:
                    status = await response.json()
                    assert not status['voice_ready'] and status['last_failure'] == reason
                    assert 'fixture' not in status['last_failure'] and 'last_error' not in status
        finally:
            await runner.cleanup()

    asyncio.run(exercise())
