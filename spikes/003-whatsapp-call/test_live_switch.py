import asyncio
import base64
from contextlib import asynccontextmanager
from functools import partial
import json
from math import sin, tau
from struct import pack
from time import time
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession, WSMsgType, web

from call_adapter import CodexLoginRequired, LiveCall, STATE, audio_envelope, create_app, provider_connector
from live_agent import _visible_history
from live_routing import LiveProviderPool


TOKEN = 'offline-sidecar-token'
SESSION = 'active-owner-whatsapp-dm'
ADMISSION = 'durable-hermes-admission'
RESULT = 'Hasil pekerjaan Hermes dari permintaan semula.'


class RateLimited(ConnectionError):
    reason = 'rate_limited'
    retry_after = 60


class Jobs:
    def __init__(self):
        self.submissions = []
        self.complete = asyncio.Event()

    async def prepare_context(self):
        work = [{'session_id': SESSION, 'admission_id': ADMISSION, 'status': 'started',
                 'text': 'Pekerjaan yang diterima sebelum perpindahan.'}] if self.submissions else []
        return {'status': 'ready', 'session_id': SESSION, 'history': [], 'work': work}

    async def submit(self, submission_id, text, *, session_id, spoken_at_ms=None):
        assert session_id == SESSION
        self.submissions.append((submission_id, text))
        return {'status': 'started',
                'receipt': {'session_id': session_id, 'admission_id': ADMISSION}}

    async def observe_work(self, session_id, admission_id):
        assert (session_id, admission_id) == (SESSION, ADMISSION)
        await self.complete.wait()
        return {'status': 'completed', 'session_id': session_id, 'admission_id': admission_id,
                'output': RESULT}

    async def save_exchange(self, session_id, exchange, **_):
        assert session_id == SESSION
        return {'status': 'saved'}


class Connection:
    can_speak = True

    def __init__(self, scope, candidate, *, pause=None, error=None):
        self.scope = scope
        self.route = candidate.kind
        self.pause = pause
        self.error = error
        self.prepared = asyncio.Event()
        self.input = asyncio.Queue()
        self.spoken = asyncio.Queue()
        self.closed = asyncio.Event()
        self.initial_items = []
        self.context_updates = []

    async def prepare(self):
        self.prepared.set()
        if self.pause is not None:
            await self.pause.wait()

    async def start(self, _prompt, items):
        if self.error is not None:
            raise self.error
        self.initial_items = items

    def stop_input(self):
        pass

    async def close(self):
        self.closed.set()

    def feed(self, pcm):
        self.input.put_nowait(pcm)

    async def append_context(self, text):
        self.context_updates.append(text)

    async def speak(self, text):
        self.spoken.put_nowait(text)


@asynccontextmanager
async def make_app(tmp_path, *, selected='codex', accounts=('a',), pauses=None, errors=None, jobs=None,
                   failure_notices=False, direction_reader=None, clock=time, gpt_live_key=None,
                   gpt_live_endpoint=None, hermes_realtime=False):
    pauses = {} if pauses is None else pauses
    errors = {} if errors is None else errors
    config = {
        'selected': selected,
        'providers': {
            'codex': {'kind': 'codex'},
            'gemini': {'kind': 'gemini', 'model': 'fixture-gemini-live',
                       'accounts': {name: {'secret_env': f'WA_FIXTURE_{name}'} for name in accounts}},
        },
        'pools': {'voice': ['codex', 'gemini']},
    }
    environment = {f'WA_FIXTURE_{name}': f'fixture-key-{name}' for name in accounts}
    if gpt_live_key is not None:
        config['providers']['gpt-live'] = {'kind': 'gpt-live', 'voice': 'marin'}
        config['pools']['voice'].append('gpt-live')
        environment['OPENAI_API_KEY'] = gpt_live_key if gpt_live_endpoint is None else 'unused-direct-openai-key'
    if gpt_live_endpoint is not None:
        config['providers']['gateway'] = {
            'kind': 'gpt-live', 'voice': 'marin', 'endpoint': gpt_live_endpoint,
            'accounts': {'project': {'secret_env': 'GATEWAY_LIVE_KEY'}},
        }
        config['pools']['voice'].append('gateway')
        environment['GATEWAY_LIVE_KEY'] = gpt_live_key
    if hermes_realtime:
        config['providers']['hermes-audio'] = {'kind': 'hermes-realtime'}
        config['pools']['voice'].append('hermes-audio')
    pool = LiveProviderPool(config, tmp_path / 'rotation.json', clock=clock, environ=environment)
    opened = asyncio.Queue()

    def builder(candidate, key):
        if candidate.kind == 'gpt-live':
            assert key == environment[candidate.secret_env]
            return provider_connector(candidate, key)
        if candidate.kind == 'hermes-realtime':
            assert key is None
            return provider_connector(candidate, key, jobs=jobs)
        assert key == (f'fixture-key-{candidate.account}' if candidate.kind == 'gemini' else None)

        def connect(scope):
            connection = Connection(scope, candidate, pause=pauses.get(candidate.key),
                                    error=errors.get(candidate.key))
            opened.put_nowait((candidate.key, connection))
            return connection

        return connect

    factory = partial(LiveCall, agent_jobs=jobs, routing=pool, connector_for=builder,
                      direction_reader=direction_reader)
    app = create_app(TOKEN, tmp_path / 'runs', voice_factory=factory, routing=pool,
                     failure_notices=failure_notices)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    try:
        async with ClientSession(headers={'Authorization': f'Bearer {TOKEN}'}) as client:
            yield app, client, url, opened
    finally:
        for pause in pauses.values():
            pause.set()
        await runner.cleanup()


async def provider_request(client, url, selection):
    async with client.post(url + '/provider', json={'selection': selection, 'now': True}) as response:
        return await response.json()


async def health(client, url):
    async with client.get(url + '/health') as response:
        return await response.json()


async def send_pcm(ws, pcm):
    await ws.send_json({'realtimeInput': {'audio': {'mimeType': 'audio/pcm;rate=16000',
                                                  'data': base64.b64encode(pcm).decode()}}})


def test_outbound_audio_setup_uses_same_session_and_connector_for_first_media(tmp_path):
    async def exercise():
        class OutboundJobs(Jobs):
            context_reads = 0

            async def prepare_context(self):
                self.context_reads += 1
                context = await super().prepare_context()
                context['history'] = [{'role': 'user', 'text': 'Lanjutkan percakapan aktif ini.'}]
                return context

        async def read_direction():
            return 'outbound'

        jobs = OutboundJobs()
        async with make_app(tmp_path, jobs=jobs, direction_reader=read_direction) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                key, prepared = await asyncio.wait_for(opened.get(), 2)
                assert key == ('codex', 'default')
                voice = app[STATE]['voice']
                assert voice.hermes_session_id == SESSION
                assert any('Kamu yang menelepon' in item['text'] for item in prepared.initial_items)
                assert any('Lanjutkan percakapan aktif ini.' in item['text'] for item in prepared.initial_items)
                assert prepared.input.empty()

                pcm = b'\x34\x12' * 20
                await send_pcm(ws, pcm)
                assert await asyncio.wait_for(prepared.input.get(), 2) == pcm
                assert app[STATE]['voice'] is voice and voice.connection is prepared
                assert jobs.context_reads == 1 and opened.empty()

    asyncio.run(exercise())


def test_switch_keeps_one_whatsapp_socket_hermes_session_and_accepted_work(tmp_path):
    async def exercise():
        release = asyncio.Event()
        jobs = Jobs()
        async with make_app(tmp_path, pauses={('gemini', 'a'): release}, jobs=jobs) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                key, old = await asyncio.wait_for(opened.get(), 2)
                assert key == ('codex', 'default')
                voice = app[STATE]['voice']
                call_ws = app[STATE]['active']
                await old.scope.on_data({'type': 'turn.done', 'turn': {
                    'id': 'original-turn', 'role': 'user', 'transcript': 'Kerjakan ini',
                }})
                assert (await ws.receive_json(timeout=2))['serverContent']['inputTranscription']['text'] == 'Kerjakan ini'
                _, status, admission = await voice.submit_task('original-provider-item', 'original-turn', 'Kerjakan ini')
                assert (status, admission) == ('started', ADMISSION)
                voice.watch_dm_work(ADMISSION, None, None)
                observer = voice.work_followers[ADMISSION][0]
                voice.update_dm_work((await jobs.prepare_context())['work'])
                step = {'admission_id': ADMISSION, 'generation': 4, 'tool_name': 'patch',
                        'tool_id': 'edit', 'seq': 1, 'phase': 'completed', 'detail': 'render.py',
                        'file_change': {'effect': 'patched', 'files_modified': ['render.py'],
                                        'code_excerpt': ['only on explicit read']}}
                await voice.apply_tool_progress(step)

                request = asyncio.create_task(provider_request(client, url, 'gemini'))
                try:
                    key, target = await asyncio.wait_for(opened.get(), 2)
                    assert key == ('gemini', 'a')
                    await asyncio.wait_for(target.prepared.wait(), 2)
                    assert not request.done()
                    await voice.apply_tool_progress({**step, 'tool_id': 'tests', 'tool_name': 'terminal',
                                                     'seq': 2, 'phase': 'failed', 'file_change': None,
                                                     'finding': 'test_render gagal'})
                    pcm = b'\x20\x01' * 20
                    await target.scope.output_audio(b'\x11\x02' * 20)
                    await old.scope.output_audio(pcm)
                    assert await ws.receive_json(timeout=2) == audio_envelope(pcm)
                    await send_pcm(ws, b'\x40\x01' * 20)
                    assert await asyncio.wait_for(old.input.get(), 2) == b'\x40\x01' * 20
                finally:
                    release.set()
                    result = await asyncio.wait_for(request, 2)
                assert (result['status'], result['provider'], result['account']) == ('switched', 'gemini', 'a')
                assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                assert app[STATE]['voice'] is voice and app[STATE]['active'] is call_ws
                assert voice.hermes_session_id == SESSION
                assert voice.work_followers[ADMISSION][0] is observer and not observer.done()
                assert ADMISSION in voice.call_work
                assert any(ADMISSION in item['text'] for item in target.initial_items)
                assert any('render.py' in item['text'] for item in target.initial_items)
                work = [{'admission_id': ADMISSION, 'session_id': SESSION, 'status': 'started', 'text': 'video'}]
                brief = voice.replacement_work_context(work, {ADMISSION: [
                    {'detail': 'data ' * 1000}, {'tool': 'patch', 'detail': 'render.py'},
                ]})
                assert json.loads(brief.removeprefix('Status pekerjaan Hermes: '))[0]['tool_steps'] == [
                    {'tool': 'patch', 'detail': 'render.py'},
                ]
                assert all('only on explicit read' not in item['text'] for item in target.initial_items)
                assert sum('test_render gagal' in text for text in target.context_updates) == 1
                assert target.spoken.empty()
                await asyncio.wait_for(old.closed.wait(), 2)

                await old.scope.output_audio(b'\x44\x02' * 20)
                await old.scope.on_data({'type': 'turn.done', 'turn': {
                    'id': 'late-old-turn', 'role': 'user', 'transcript': 'stale transcript',
                }})
                await target.scope.on_data({'type': 'turn.created', 'turn': {
                    'id': 'replacement-reply', 'role': 'assistant',
                }})
                await target.scope.output_audio(b'\x20\x03' * 20)
                assert await ws.receive_json(timeout=2) == audio_envelope(b'\x20\x03' * 20)
                await target.scope.on_data({'type': 'turn.done', 'turn': {
                    'id': 'replacement-reply', 'role': 'assistant',
                }})
                assert 'late-old-turn' not in voice.user_turns
                await send_pcm(ws, b'\x40\x03' * 20)
                assert await asyncio.wait_for(target.input.get(), 2) == b'\x40\x03' * 20

                jobs.complete.set()
                assert RESULT in await asyncio.wait_for(target.spoken.get(), 3)
                await asyncio.wait_for(observer, 2)
                assert target.spoken.empty() and old.spoken.empty()
                assert len(jobs.submissions) == 1 and ADMISSION in voice.offered_work_results
                status = await health(client, url)
                assert status['active_provider'] == {'provider': 'gemini', 'account': 'a'}

    asyncio.run(exercise())


@pytest.mark.parametrize('late_context_available', [True, False])
def test_switch_replays_final_voice_turns_once_before_replacement_receives_audio(tmp_path, late_context_available):
    async def exercise():
        release, snapshot_started, release_snapshot = (asyncio.Event() for _ in range(3))

        class TranscriptJobs(Jobs):
            def __init__(self):
                super().__init__()
                self.history = []
                self.context_reads = 0

            async def prepare_context(self):
                context = await super().prepare_context()
                self.context_reads += 1
                if self.context_reads > 1:
                    snapshot_started.set()
                    await release_snapshot.wait()
                return {**context, 'history': self.history.copy()}

            async def save_exchange(self, session_id, exchange, **_kwargs):
                assert session_id == SESSION
                for role in ('user', 'assistant'):
                    if transcript := exchange[role]:
                        self.history.append({
                            'role': role, 'text': transcript, 'source': 'whatsapp_call',
                            '_message_refs': [f"wa-control-live:{exchange['exchange_id']}:{role}"],
                        })
                return {'status': 'saved'}

        jobs = TranscriptJobs()
        async with make_app(tmp_path, pauses={('gemini', 'a'): release}, jobs=jobs) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                _, old = await asyncio.wait_for(opened.get(), 2)
                voice = app[STATE]['voice']
                socket = app[STATE]['active']
                _, status, admission = await voice.submit_task('earlier-item', 'earlier-task', 'Kerjakan ini')
                assert (status, admission) == ('started', ADMISSION)

                async def finish_turn(turn_id, role, transcript):
                    turn = {'id': turn_id, 'role': role}
                    await old.scope.on_data({'type': 'turn.created', 'turn': turn})
                    await old.scope.on_data({'type': 'turn.done', 'turn': {**turn, 'transcript': transcript}})
                    field = 'inputTranscription' if role == 'user' else 'outputTranscription'
                    assert await ws.receive_json(timeout=2) == {
                        'serverContent': {field: {'text': transcript}, 'turnComplete': True},
                    }

                earlier_user = 'Bahas nama proyek kita.'
                earlier_reply = 'Silakan sebutkan nama proyeknya.'
                await finish_turn('before', 'user', earlier_user)
                await finish_turn('before-reply', 'assistant', earlier_reply)

                late_user = 'Satu tambahan saat audio berpindah.'
                late_turn = None
                interrupted = {'serverContent': {'interrupted': True}}
                send = voice.send

                async def send_with_inflight_transcript(message):
                    nonlocal late_turn
                    if message == interrupted and late_turn is None:
                        late_turn = asyncio.create_task(old.scope.on_data({
                            'type': 'turn.done',
                            'turn': {'id': 'inflight', 'role': 'user', 'transcript': late_user},
                        }))
                        # The incoming transcript runs while the switch is sending to the sidecar.
                        await asyncio.sleep(0)
                    await send(message)

                voice.send = send_with_inflight_transcript
                request = asyncio.create_task(provider_request(client, url, 'gemini'))
                try:
                    await asyncio.wait_for(snapshot_started.wait(), 2)
                    native_turn = 'native-during-snapshot'
                    native_user = 'Buat catatan pengujian Cempaka.'
                    signature = 'a' * 64
                    await finish_turn(native_turn, 'user', native_user)
                    # Hermes has persisted the native prompt while its submission receipt is still pending.
                    jobs.history.extend(_visible_history([{
                        'role': 'user', 'content': native_user, 'message_uid': 'native-row-id',
                        'display_metadata': {'submission_id': f'wa-control-live:{voice.submission_id(native_turn)}:{signature}'},
                    }], str.strip, include_message_refs=True))
                    release_snapshot.set()
                    _, candidate = await asyncio.wait_for(opened.get(), 2)
                    await asyncio.wait_for(candidate.prepared.wait(), 2)
                    append_context = candidate.append_context

                    async def append_before_audio(text):
                        assert not candidate.scope.active
                        if late_user in text and not late_context_available:
                            raise ConnectionError('Replacement did not accept the final transcript')
                        return await append_context(text)

                    candidate.append_context = append_before_audio
                    during_user = 'Judul proyeknya Anggrek Sore.'
                    during_reply = 'Aku ingat judul tersebut.'
                    last_user = 'Untuk pengujian, pakai berkas contoh.'
                    await finish_turn('during', 'user', during_user)
                    await finish_turn('during-reply', 'assistant', during_reply)
                    await finish_turn('last', 'user', last_user)
                    assert candidate.input.empty() and not request.done()
                finally:
                    release_snapshot.set()
                    release.set()
                    result = await asyncio.wait_for(request, 2)

                assert await ws.receive_json(timeout=2) == interrupted
                assert late_turn is not None
                await late_turn
                assert app[STATE]['voice'] is voice and app[STATE]['active'] is socket
                assert voice.hermes_session_id == SESSION and ADMISSION in voice.call_work
                assert len(jobs.submissions) == 1

                pcm = b'\x22\x01' * 20
                if not late_context_available:
                    assert result['status'] == 'failed'
                    await asyncio.wait_for(candidate.closed.wait(), 2)
                    status = await health(client, url)
                    assert status['active_provider'] == {'provider': 'codex', 'account': 'default'}
                    assert status['routing']['selected'] == 'codex'
                    await send_pcm(ws, pcm)
                    assert await asyncio.wait_for(old.input.get(), 2) == pcm
                    return

                assert result['status'] == 'switched'
                startup = '\n'.join(item['text'] for item in candidate.initial_items)
                assert earlier_user in startup and earlier_reply in startup
                assert ADMISSION in startup
                context = '\n'.join([startup, *candidate.context_updates])
                assert signature not in context
                for transcript in (earlier_user, earlier_reply, native_user, during_user, during_reply, last_user, late_user):
                    assert context.count(transcript) == 1, transcript

                await send_pcm(ws, pcm)
                assert await asyncio.wait_for(candidate.input.get(), 2) == pcm

    asyncio.run(exercise())


def test_switch_replays_only_dm_rows_missing_from_candidate_snapshot(tmp_path):
    async def exercise():
        snapshot_started, release_snapshot, release_candidate = (asyncio.Event() for _ in range(3))
        first_dm = {'role': 'user', 'text': 'Aku sudah kirim.', '_message_refs': ['original-dm']}
        repeated_dm = {**first_dm, '_message_refs': ['second-dm']}
        later_dm = {'role': 'assistant', 'text': 'Sudah kuterima.', '_message_refs': ['third-dm']}

        class SwitchingJobs(Jobs):
            context_reads = 0

            async def prepare_context(self):
                self.context_reads += 1
                if self.context_reads == 1:
                    return await super().prepare_context()
                snapshot_started.set()
                await release_snapshot.wait()
                return {'status': 'ready', 'session_id': SESSION, 'history_cursor': 11, 'history': [first_dm]}

        async with make_app(tmp_path, pauses={('gemini', 'a'): release_candidate}, jobs=SwitchingJobs()) as (
            app, client, url, opened,
        ):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                _, old = await asyncio.wait_for(opened.get(), 2)
                voice = app[STATE]['voice']
                request = asyncio.create_task(provider_request(client, url, 'gemini'))
                try:
                    await asyncio.wait_for(snapshot_started.wait(), 2)
                    # One watcher batch starts before the snapshot cursor and ends after it.
                    cursor = await voice.apply_chat_update(
                        {'history_cursor': 12, 'history': [first_dm, repeated_dm]}, 10,
                    )
                    release_snapshot.set()
                    _, candidate = await asyncio.wait_for(opened.get(), 2)
                    work = {'session_id': SESSION, 'admission_id': ADMISSION, 'status': 'started',
                            'text': 'Pekerjaan masuk saat ganti audio.'}
                    await voice.apply_chat_update(
                        {'history_cursor': 13, 'history': [later_dm], 'work': [work]}, cursor,
                    )
                finally:
                    release_snapshot.set()
                    release_candidate.set()
                    result = await asyncio.wait_for(request, 2)

                assert result['status'] == 'switched'
                assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                assert app[STATE]['voice'] is voice and voice.hermes_session_id == SESSION
                assert old.context_updates[:2] == ['Pesan di DM saat telepon. Pemilik: Aku sudah kirim.'] * 2
                seeded = json.loads(candidate.initial_items[0]['text'].split('\n', 1)[1])
                assert seeded == [{'role': 'user', 'text': 'Aku sudah kirim.'}]
                assert candidate.context_updates[:2] == [
                    'Pesan di DM saat telepon. Pemilik: Aku sudah kirim.',
                    'Pesan di DM saat telepon. Asisten: Sudah kuterima.',
                ]
                assert len(candidate.context_updates) == 3
                assert ADMISSION in candidate.context_updates[2]
                assert 'Pekerjaan masuk saat ganti audio.' in candidate.context_updates[2]
                await voice.apply_chat_update(
                    {'history_cursor': 15, 'history': [first_dm, {
                        'role': 'user', 'text': 'Pesan setelah pindah.', '_message_refs': ['fourth-dm'],
                    }], 'work': [work]}, 13,
                )
                assert candidate.context_updates[3:] == ['Pesan di DM saat telepon. Pemilik: Pesan setelah pindah.']

    asyncio.run(exercise())


def test_failed_candidate_keeps_active_provider_and_reports_limit(tmp_path):
    async def exercise():
        async with make_app(tmp_path, errors={('gemini', 'a'): RateLimited()}) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                _, old = await asyncio.wait_for(opened.get(), 2)
                voice = app[STATE]['voice']
                async with client.post(url + '/provider', headers={'Authorization': 'Bearer wrong'},
                                       json={'selection': 'gemini', 'now': True}) as response:
                    assert response.status == 401
                result = await provider_request(client, url, 'gemini')
                assert (result['status'], result['reason']) == ('failed', 'all_rate_limited')
                key, failed = await asyncio.wait_for(opened.get(), 2)
                assert key == ('gemini', 'a')
                await asyncio.wait_for(failed.closed.wait(), 2)
                assert app[STATE]['voice'] is voice and not old.closed.is_set()
                await old.scope.output_audio(b'\x40\x01' * 20)
                assert await ws.receive_json(timeout=2) == audio_envelope(b'\x40\x01' * 20)
                status = await health(client, url)
                assert status['active_provider'] == {'provider': 'codex', 'account': 'default'}
                assert status['routing']['selected'] == 'codex'

    asyncio.run(exercise())


def test_hermes_realtime_rejoins_the_active_call_after_profile_sign_in(tmp_path, monkeypatch):
    import realtime_connection

    class BrokerJobs(Jobs):
        def __init__(self):
            super().__init__()
            self.attempts = []
            self.signed_in = False

        async def mint_realtime_secret(self, provider, account):
            self.attempts.append((provider, account))
            if not self.signed_in:
                return {'status': 'auth_required'}
            return {'status': 'ready', 'client_secret': 'ephemeral-test-secret', 'expires_at': 40}

    class ProfileAudio(Connection):
        def __init__(self, scope, *, mint_secret):
            super().__init__(scope, scope.candidate)
            self.mint_secret = mint_secret

        async def prepare(self):
            result = await self.mint_secret()
            if result['status'] != 'ready':
                failure = ConnectionError('Hermes profile needs Codex sign-in')
                failure.reason = result['status']
                raise failure
            assert result['client_secret'] == 'ephemeral-test-secret'

    async def exercise():
        jobs = BrokerJobs()
        now = [10.0]
        monkeypatch.setattr(realtime_connection, 'HermesRealtimeConnection', ProfileAudio)
        async with make_app(tmp_path, selected='voice', jobs=jobs, clock=lambda: now[0],
                            hermes_realtime=True) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                _, old = await asyncio.wait_for(opened.get(), 2)
                voice, call_socket = app[STATE]['voice'], app[STATE]['active']

                result = await provider_request(client, url, 'hermes-audio')
                assert (result['status'], result['reason']) == ('failed', 'all_unavailable')
                status = await health(client, url)
                account = next(row for row in status['routing']['accounts'] if row['provider'] == 'hermes-audio')
                assert account['reason'] == 'auth_required'
                pcm = b'\x44\x02' * 20
                await old.scope.output_audio(pcm)
                assert await ws.receive_json(timeout=2) == audio_envelope(pcm)

                jobs.signed_in = True
                now[0] = account['retry_at']
                result = await provider_request(client, url, 'hermes-audio')
                assert (result['status'], result['provider']) == ('switched', 'hermes-audio')
                assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                assert app[STATE]['voice'] is voice and app[STATE]['active'] is call_socket
                assert voice.hermes_session_id == SESSION
                await send_pcm(ws, pcm)
                assert await asyncio.wait_for(voice.connection.input.get(), 2) == pcm
                assert jobs.attempts == [('hermes-audio', 'default')] * 2
                assert 'ephemeral-test-secret' not in repr(await health(client, url))

    asyncio.run(exercise())


def test_hermes_realtime_moves_audio_both_ways_over_a_local_webrtc_peer(tmp_path, monkeypatch):
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamError
    from call_adapter import CallerTrack
    import realtime_connection

    spoken = 'Cek jadwal hari ini.'

    class BrokerJobs(Jobs):
        async def mint_realtime_secret(self, provider, account):
            assert (provider, account) == ('hermes-audio', 'default')
            return {'status': 'ready', 'client_secret': 'ek_local_media_fixture', 'expires_at': time() + 30}

        async def route(self, _proposal, current, _previous, _context, *, session_id):
            assert (current, session_id) == (spoken, SESSION)
            return {'status': 'task', 'work': []}

        async def submit(self, submission_id, text, *, session_id, spoken_at_ms=None):
            result = await super().submit(submission_id, text, session_id=session_id)
            return {**result, 'status': 'completed', 'output': RESULT}

    async def exercise():
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        speaker = CallerTrack()
        peer.addTrack(speaker)
        heard_caller = asyncio.Event()
        speech_requested = asyncio.Event()
        channel_ready = asyncio.Queue()
        provider_items = asyncio.Queue()
        jobs = BrokerJobs()
        audio_reader = None

        @peer.on('track')
        def track_received(track):
            nonlocal audio_reader

            async def read_audio():
                try:
                    while True:
                        frame = await track.recv()
                        if max(map(abs, memoryview(frame.planes[0]).cast('h')), default=0) > 1000:
                            heard_caller.set()
                except MediaStreamError:
                    return

            audio_reader = asyncio.create_task(read_audio())

        @peer.on('datachannel')
        def data_channel(channel):
            channel_ready.put_nowait(channel)
            channel.send(json.dumps({'type': 'session.created'}))

            @channel.on('message')
            def receive(message):
                event = json.loads(message)
                if event['type'] == 'session.update':
                    channel.send(json.dumps({'type': 'session.updated', 'session': event['session']}))
                elif event['type'] == 'conversation.item.create':
                    provider_items.put_nowait(event['item'])
                    channel.send(json.dumps({'type': 'conversation.item.added', 'item': event['item']}))
                elif event['type'] == 'response.create':
                    response = {'id': 'result-speech', 'metadata': event['response']['metadata']}
                    channel.send(json.dumps({'type': 'response.created', 'response': response}))
                    speech_requested.set()

        async def answer(request):
            assert request.headers['Authorization'] == 'Bearer ek_local_media_fixture'
            assert request.content_type == 'application/sdp'
            await peer.setRemoteDescription(RTCSessionDescription(sdp=await request.text(), type='offer'))
            await peer.setLocalDescription(await peer.createAnswer())
            return web.Response(text=peer.localDescription.sdp, content_type='application/sdp')

        provider = web.Application()
        provider.router.add_post('/v1/realtime/calls', answer)
        runner = web.AppRunner(provider)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        monkeypatch.setattr(realtime_connection, 'ENDPOINT',
                            f'http://127.0.0.1:{runner.addresses[0][1]}/v1/realtime/calls')

        try:
            async with make_app(tmp_path, selected='hermes-audio', jobs=jobs,
                                hermes_realtime=True) as (app, client, url, _opened):
                async with client.ws_connect(url + '/live') as ws:
                    await ws.send_json({'setup': {}})
                    assert await ws.receive_json(timeout=6) == {'setupComplete': {}}
                    assert app[STATE]['voice'].hermes_session_id == SESSION
                    pcm = pack('<320h', *(round(12000 * sin(i * tau / 40)) for i in range(320)))
                    for _ in range(6):
                        await send_pcm(ws, pcm)
                        speaker.feed(pcm)
                    await asyncio.wait_for(heard_caller.wait(), 3)
                    async with asyncio.timeout(3):
                        while True:
                            parts = (await ws.receive_json())['serverContent']['modelTurn']['parts']
                            audio = base64.b64decode(parts[0]['inlineData']['data'])
                            if max(map(abs, memoryview(audio).cast('h')), default=0) > 1000:
                                break
                    channel = await channel_ready.get()
                    response = {
                        'id': 'generated-response', 'status': 'completed', 'output': [{
                            'type': 'function_call', 'call_id': 'fixture-ask-hermes', 'status': 'completed',
                            'name': 'ask_hermes', 'arguments': json.dumps({'request': spoken}),
                        }],
                    }
                    for event in (
                        {'type': 'input_audio_buffer.speech_started', 'item_id': 'real-caller-turn'},
                        {'type': 'input_audio_buffer.committed', 'item_id': 'real-caller-turn'},
                        {'type': 'conversation.item.input_audio_transcription.completed',
                         'item_id': 'real-caller-turn', 'transcript': spoken},
                        {'type': 'response.created', 'response': {'id': response['id']}},
                        {'type': 'response.done', 'response': response},
                        {'type': 'response.done', 'response': response},
                    ):
                        channel.send(json.dumps(event))
                    async with asyncio.timeout(3):
                        while RESULT not in json.dumps(item := await provider_items.get()):
                            pass
                    assert item['type'] in {'function_call_output', 'message'}
                    await asyncio.wait_for(speech_requested.wait(), 2)
                    assert [requested for _, requested in jobs.submissions] == [spoken]
        finally:
            speaker.stop()
            await peer.close()
            if audio_reader is not None:
                audio_reader.cancel()
                await asyncio.gather(audio_reader, return_exceptions=True)
            await runner.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize(('outcome', 'custom_gateway'), [
    ('hangup', False), ('limited', False), ('content', False),
    ('expired', False),
    pytest.param('hangup', True, id='gateway-hangup'),
    pytest.param('limited', True, id='gateway-limited'),
])
def test_switch_to_gpt_live_uses_the_same_whatsapp_and_hermes_session(tmp_path, monkeypatch, outcome, custom_gateway):
    import gpt_live_connection

    provider_alias = 'gateway' if custom_gateway else 'gpt-live'
    account_alias = 'project' if custom_gateway else 'default'
    path = '/gateway/live' if custom_gateway else '/v1/live/sessions'
    ws_connect = ClientSession.ws_connect

    def loopback_ws(self, url, *args, **kwargs):
        assert urlsplit(str(url)).hostname == '127.0.0.1'
        return ws_connect(self, url, *args, **kwargs)

    monkeypatch.setattr(ClientSession, 'ws_connect', loopback_ws)
    spoken = 'Tolong periksa jadwal hari ini.'
    limited = outcome == 'limited'

    class CompletedJobs(Jobs):
        async def route(self, _proposal, current, _previous, _context, *, session_id):
            assert (current, session_id) == (spoken, SESSION)
            return {'status': 'task', 'work': []}

        async def submit(self, submission_id, text, *, session_id, spoken_at_ms=None):
            result = await super().submit(submission_id, text, session_id=session_id)
            return {**result, 'status': 'completed', 'output': RESULT}

        async def save_exchange(self, session_id, exchange):
            await self.saved.put((session_id, exchange['user'], exchange['assistant']))
            return {'status': 'saved'}

    async def exercise():
        opened_provider = asyncio.Queue()
        caller_audio = asyncio.Queue()
        updates = asyncio.Queue()
        release = asyncio.Event()
        api_key = 'loopback-gateway-key' if custom_gateway else 'loopback-openai-key'
        jobs = CompletedJobs()
        jobs.saved = asyncio.Queue()

        async def serve_provider(request):
            assert request.headers.get('Authorization') == f'Bearer {api_key}'
            assert request.query.get('project') == ('demo' if custom_gateway else None)
            if limited:
                raise web.HTTPTooManyRequests(headers={'Retry-After': '20'})
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await opened_provider.put((socket, await socket.receive_json(timeout=2)))
            await release.wait()
            await socket.send_json({'type': 'session.started', 'session': {'id': 'fixture-gpt-live'}})
            async for frame in socket:
                if frame.type is WSMsgType.TEXT:
                    event = frame.json()
                    if event['type'] == 'session.input_audio.append':
                        await caller_audio.put(base64.b64decode(event['audio'], validate=True))
                    elif event['type'] in {'session.thinking.append', 'session.commentary.append'}:
                        await socket.send_json({'type': event['type'] + 'ed', 'event_id': 'ack-' + event['event_id'],
                                                'client_event_id': event['event_id'], 'start_ms': 220, 'end_ms': 220})
                        await updates.put(event)
                    elif event['type'] == 'session.close':
                        await socket.send_json({'type': 'session.closed', 'event_id': 'fixture-closed',
                                                'reason': 'close_requested'})
                        break
            return socket

        provider = web.Application()
        provider.router.add_get(path, serve_provider)
        runner = web.AppRunner(provider)
        await runner.setup()
        server = web.TCPSite(runner, '127.0.0.1', 0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        endpoint = f'ws://127.0.0.1:{port}{path}'
        if not custom_gateway:
            monkeypatch.setattr(gpt_live_connection, 'ENDPOINT', endpoint)
        try:
            async with make_app(tmp_path, jobs=jobs, gpt_live_key=api_key,
                                gpt_live_endpoint=f'{endpoint}?project=demo' if custom_gateway else None,
                                failure_notices=True) as (app, client, url, opened):
                async with client.ws_connect(url + '/live') as ws:
                    await ws.send_json({'setup': {}})
                    assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                    _, old = await asyncio.wait_for(opened.get(), 2)
                    voice, call_socket = app[STATE]['voice'], app[STATE]['active']
                    old_output = b'\x20\x01' * 20

                    if limited:
                        result = await provider_request(client, url, provider_alias)
                        assert (result['status'], result['reason']) == ('failed', 'all_rate_limited')
                        assert not old.closed.is_set()
                        await old.scope.output_audio(old_output)
                        assert await ws.receive_json(timeout=2) == audio_envelope(old_output)
                    else:
                        request = asyncio.create_task(provider_request(client, url, provider_alias))
                        try:
                            gpt, setup = await asyncio.wait_for(opened_provider.get(), 2)
                            assert setup['type'] == 'session.start'
                            assert setup['session']['delegation'] == {'type': 'client'}
                            assert setup['session']['audio']['output']['voice'] == 'marin'
                            assert not request.done()
                            await old.scope.output_audio(old_output)
                            assert await ws.receive_json(timeout=2) == audio_envelope(old_output)
                        finally:
                            release.set()
                            result = await asyncio.wait_for(request, 3)
                        assert (result['status'], result['provider'], result['account']) == (
                            'switched', provider_alias, account_alias,
                        )
                        assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                        await asyncio.wait_for(old.closed.wait(), 2)
                        await send_pcm(ws, b'\x40\x03' * 320)
                        assert await asyncio.wait_for(caller_audio.get(), 2)
                        new_output = b'\x44\x02' * 240
                        await gpt.send_json({'type': 'session.output_audio.delta',
                                             'delta': base64.b64encode(new_output).decode()})
                        assert await ws.receive_json(timeout=2) == audio_envelope(new_output)
                        await gpt.send_json({'type': 'session.input_transcript.delta', 'event_id': 'fixture-input',
                                             'delta': spoken, 'start_ms': 80, 'end_ms': 190})
                        delegation = {'type': 'session.delegation.created', 'event_id': 'fixture-delegation',
                                      'offset_ms': 220, 'delegation': {'target': 'client', 'id': 'fixture-task',
                                                                     'type': 'delegation'}}
                        await gpt.send_json(delegation)
                        await gpt.send_json(delegation)
                        async with asyncio.timeout(3):
                            while RESULT not in (update := await updates.get())['content']:
                                assert update['delegation_id'] == 'fixture-task'
                        assert update['type'] == 'session.commentary.append'
                        assert update['delegation_id'] == 'fixture-task'
                        assert [text for _, text in jobs.submissions] == [spoken]

                    assert app[STATE]['voice'] is voice and app[STATE]['active'] is call_socket
                    assert voice.hermes_session_id == SESSION
                    status = await health(client, url)
                    active = {'provider': 'codex', 'account': 'default'} if limited else {
                        'provider': provider_alias, 'account': account_alias,
                    }
                    assert status['active_provider'] == active
                    assert status['routing']['selected'] == active['provider']
                    if outcome == 'hangup':
                        await gpt.send_json({'type': 'session.input_transcript.delta', 'event_id': 'last-user',
                                             'delta': 'Terima kasih.', 'start_ms': 1000, 'end_ms': 1200})
                        await gpt.send_json({'type': 'session.output_transcript.delta', 'event_id': 'last-assistant',
                                             'delta': 'Sama-sama.', 'start_ms': 1300, 'end_ms': 1500})
                        async with asyncio.timeout(2):
                            while len(voice.connection.buffered_transcripts()) < 2:
                                await asyncio.sleep(0)
                        await ws.close()
                        assert await asyncio.wait_for(jobs.saved.get(), 2) == (SESSION, 'Terima kasih.', 'Sama-sama.')
                    elif outcome == 'expired':
                        expired_audio = voice.audio
                        await gpt.send_json({'type': 'session.closed', 'event_id': 'fixture-expired',
                                             'reason': 'expired'})
                        reopened, setup = await asyncio.wait_for(opened_provider.get(), 5)
                        assert reopened is not gpt and setup['type'] == 'session.start'
                        assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                        async with asyncio.timeout(2):
                            while voice.audio is expired_audio:
                                await asyncio.sleep(0)
                        assert app[STATE]['voice'] is voice and app[STATE]['active'] is call_socket
                        assert voice.hermes_session_id == SESSION and len(jobs.submissions) == 1
                        assert 'provider_close_reason' not in voice.report
                    elif outcome == 'content':
                        async with client.post(url + '/provider', json={'selection': 'voice', 'now': False}) as response:
                            assert (await response.json())['status'] == 'selected'
                        await gpt.send_json({'type': 'session.closed', 'event_id': 'fixture-content-close',
                                             'reason': 'content'})
                        async with asyncio.timeout(2):
                            while 'providerFailure' not in (notice := await ws.receive_json()):
                                pass
                        assert notice['providerFailure']['reason'] == 'provider_content_closed'
                        assert (await ws.receive(timeout=2)).type == WSMsgType.CLOSE
                        assert opened.empty(), 'a content closure must not send past input to another provider'
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(exercise())


def test_codex_login_recovery_can_rejoin_the_active_call(tmp_path):
    async def exercise():
        codex = ('codex', 'default')
        errors = {codex: CodexLoginRequired()}
        now = [10.0]
        async with make_app(tmp_path, selected='voice', errors=errors, jobs=Jobs(),
                            clock=lambda: now[0]) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                key, rejected = await asyncio.wait_for(opened.get(), 2)
                assert key == codex
                await asyncio.wait_for(rejected.closed.wait(), 2)
                key, fallback = await asyncio.wait_for(opened.get(), 2)
                assert key == ('gemini', 'a')
                voice = app[STATE]['voice']
                assert voice.hermes_session_id == SESSION

                status = await health(client, url)
                blocked = status['routing']['accounts'][0]
                assert blocked['reason'] == 'auth_required'
                assert blocked['retry_at'] is not None and blocked['retry_estimated']
                errors.pop(codex)  # The next Codex connection can authenticate again.
                result = await provider_request(client, url, 'codex')
                assert result['status'] == 'failed' and opened.empty()
                assert not fallback.closed.is_set()

                now[0] = blocked['retry_at']
                result = await provider_request(client, url, 'codex')
                assert (result['status'], result['provider'], result['account']) == ('switched', *codex)
                assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                key, recovered = await asyncio.wait_for(opened.get(), 2)
                assert key == codex
                assert app[STATE]['voice'] is voice and voice.hermes_session_id == SESSION
                pcm = b'\x24\x03' * 20
                await send_pcm(ws, pcm)
                assert await asyncio.wait_for(recovered.input.get(), 2) == pcm
                status = await health(client, url)
                assert status['voice_ready'] and status['routing']['accounts'][0]['reason'] is None

    asyncio.run(exercise())


def test_every_configured_account_limited_closes_without_setup_ack(tmp_path):
    async def exercise():
        accounts = ('a', 'b', 'c')
        errors = {('gemini', name): RateLimited() for name in accounts}
        async with make_app(tmp_path, selected='gemini', accounts=accounts, errors=errors,
                            failure_notices=True) as (_, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                notice = (await ws.receive_json(timeout=2))['providerFailure']
                assert notice['reason'] == 'all_rate_limited'
                assert 'gemini/a' in notice['message'] and 'Coba lagi paling awal' in notice['message']
                assert 0 < len(base64.b64decode(notice['audioPcm'], validate=True)) <= 16_000 * 2 * 8
                assert (await ws.receive(timeout=2)).type == WSMsgType.CLOSE
            attempted = []
            while not opened.empty():
                key, connection = opened.get_nowait()
                attempted.append(key)
                assert connection.closed.is_set()
            assert attempted == [('gemini', name) for name in accounts]
            status = await health(client, url)
            assert status['voice_ready'] is False
            assert status['last_failure'] == 'all_rate_limited'
            assert status['routing']['reason'] == 'all_rate_limited'
            assert status['failure_notice'] == 'sent_to_sidecar'

    asyncio.run(exercise())


@pytest.mark.parametrize('exhausted', [False, True])
@pytest.mark.parametrize(('delayed', 'media_started'), [
    (False, True), (True, True), (True, False),
], ids=['immediate', 'delayed-in-call', 'delayed-before-media'])
def test_active_provider_rate_limit_falls_back_or_reports_pool_exhaustion(
    tmp_path, monkeypatch, exhausted, delayed, media_started,
):
    notice_delay = .01 if delayed else .5
    monkeypatch.setattr('call_adapter.RECOVERY_NOTICE_DELAY', notice_delay, raising=False)

    async def exercise():
        release = asyncio.Event()
        if not delayed:
            release.set()
        errors = {('gemini', 'a'): RateLimited()} if exhausted else {}
        async with make_app(tmp_path, selected='voice', errors=errors, jobs=Jobs(),
                            pauses={('gemini', 'a'): release},
                            failure_notices=True) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                key, active = await asyncio.wait_for(opened.get(), 2)
                assert key == ('codex', 'default')
                voice, socket = app[STATE]['voice'], app[STATE]['active']
                assert voice.hermes_session_id == SESSION
                pcm = b'\x24\x03' * 20
                if media_started:
                    await send_pcm(ws, pcm)
                    assert await asyncio.wait_for(active.input.get(), 2) == pcm
                active.scope.report['provider_failure'] = {'reason': 'rate_limited', 'retry_after': 60}
                active.scope.fail('fixture active limit')
                key, candidate = await asyncio.wait_for(opened.get(), 2)
                assert key == ('gemini', 'a')
                if delayed and media_started:
                    notice = await ws.receive_json(timeout=1)
                    assert set(notice) == {'recoveryAudio'}
                    clip = base64.b64decode(notice['recoveryAudio'], validate=True)
                    assert 0 < len(clip) <= 64_000 and len(clip) % 2 == 0 and any(clip)
                if delayed:
                    with pytest.raises(TimeoutError):
                        await ws.receive(timeout=.06)
                    release.set()
                if exhausted:
                    notice = (await ws.receive_json(timeout=2))['providerFailure']
                    assert notice['reason'] == 'all_rate_limited' and notice['audioPcm']
                    assert (await ws.receive(timeout=2)).type == WSMsgType.CLOSE
                    status = await health(client, url)
                    assert not status['voice_ready'] and status['last_failure'] == 'all_rate_limited'
                    assert status['failure_notice'] == 'sent_to_sidecar'
                else:
                    assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                    assert app[STATE]['voice'] is voice and app[STATE]['active'] is socket
                    assert voice.hermes_session_id == SESSION
                    await send_pcm(ws, pcm)
                    assert await asyncio.wait_for(candidate.input.get(), 2) == pcm
                    await candidate.scope.output_audio(pcm)
                    assert await ws.receive_json(timeout=2) == audio_envelope(pcm)
                    if not delayed:
                        with pytest.raises(TimeoutError):
                            await ws.receive(timeout=notice_delay + .1)
                    status = await health(client, url)
                    assert status['active_provider'] == {'provider': 'gemini', 'account': 'a'}
                    assert status['failure_notice'] is None
                assert status['routing']['selected'] == 'voice'

    asyncio.run(exercise())


def test_failure_dm_distinguishes_login_and_key_repair_from_limit():
    from provider_failure import build_provider_failure

    notice = build_provider_failure('all_unavailable', {
        'accounts': [
            {'provider': 'codex', 'account': 'default', 'kind': 'codex', 'reason': 'auth_required'},
            {'provider': 'gemini', 'account': 'a', 'kind': 'gemini', 'reason': 'auth_required',
             'secret_env': 'PRIVATE_REFERENCE'},
            {'provider': 'gemini', 'account': 'b', 'kind': 'gemini', 'reason': 'quota_exhausted'},
        ],
    })
    assert 'codex/default perlu login ulang lewat Codex CLI' in notice['message']
    assert 'gemini/a API key perlu diperiksa' in notice['message']
    assert 'gemini/b kuotanya habis' in notice['message']
    assert 'PRIVATE_REFERENCE' not in notice['message']


@pytest.mark.parametrize('reconnect_fails', [False, True])
def test_active_transient_failure_retries_the_same_account_once(tmp_path, reconnect_fails):
    async def exercise():
        key = ('gemini', 'a')
        errors = {}
        async with make_app(tmp_path, selected='gemini', errors=errors, jobs=Jobs(),
                            failure_notices=True) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                assert (initial := await asyncio.wait_for(opened.get(), 2))[0] == key
                old = initial[1]
                voice, media = app[STATE]['voice'], app[STATE]['active']
                if reconnect_fails:
                    errors[key] = ConnectionError('fixture retry failed')
                old.scope.report['provider_failure'] = {'reason': 'unavailable', 'retry_after': .04}
                old.scope.fail('fixture active network disconnect')

                retry_key, recovered = await asyncio.wait_for(opened.get(), 2)
                assert retry_key == key
                assert old.closed.is_set()
                if reconnect_fails:
                    notice = (await ws.receive_json(timeout=2))['providerFailure']
                    assert notice['reason'] == 'all_unavailable'
                    assert (await ws.receive(timeout=2)).type == WSMsgType.CLOSE
                    assert opened.empty()
                else:
                    assert await ws.receive_json(timeout=2) == {'serverContent': {'interrupted': True}}
                    assert app[STATE]['voice'] is voice and app[STATE]['active'] is media
                    assert voice.hermes_session_id == SESSION
                    pcm = b'\x24\x03' * 20
                    await send_pcm(ws, pcm)
                    assert await asyncio.wait_for(recovered.input.get(), 2) == pcm
                    assert (await health(client, url))['voice_ready']

    asyncio.run(exercise())


@pytest.mark.parametrize(('reason', 'retry_after'), [('unavailable', 60), ('rate_limited', .04)])
def test_active_reconnection_does_not_retry_outside_the_recovery_policy(tmp_path, reason, retry_after):
    async def exercise():
        async with make_app(tmp_path, selected='gemini', failure_notices=True) as (_, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                _, old = await asyncio.wait_for(opened.get(), 2)
                old.scope.report['provider_failure'] = {'reason': reason, 'retry_after': retry_after}
                old.scope.fail('fixture active disconnect')
                notice = (await ws.receive_json(timeout=2))['providerFailure']
                assert notice['reason'] == ('all_rate_limited' if reason == 'rate_limited' else 'all_unavailable')
                assert (await ws.receive(timeout=2)).type == WSMsgType.CLOSE
                assert opened.empty()

    asyncio.run(exercise())


def test_unrepresentable_provider_retry_time_preserves_failure_notice():
    from provider_failure import build_provider_failure

    notice = build_provider_failure('all_rate_limited', {'retry_at': 1e100})
    assert notice['reason'] == 'all_rate_limited'
    assert 'Coba lagi paling awal' not in notice['message']
    assert notice['audioPcm']


def test_switch_keeps_confirmed_receipt_and_drops_old_pcm_queued_behind_playout_clear(tmp_path):
    async def exercise():
        row = {'session_id': SESSION, 'admission_id': ADMISSION, 'status': 'completed', 'text': 'Buat gambar.'}
        class FinishedJobs(Jobs):
            async def prepare_context(self):
                # Initial Hermes read has the admission. The sender receipt is a later watcher update.
                return {'status': 'ready', 'session_id': SESSION, 'history': [], 'work': [row]}

        async with make_app(tmp_path, jobs=FinishedJobs()) as (app, client, url, opened):
            async with client.ws_connect(url + '/live') as ws:
                await ws.send_json({'setup': {}})
                assert await ws.receive_json(timeout=2) == {'setupComplete': {}}
                _, old = await asyncio.wait_for(opened.get(), 2)
                voice = app[STATE]['voice']
                voice.update_dm_work([{**row, 'delivery': {'has_attachments': True, 'media_delivery': 'sent'}}])
                clearing, release = asyncio.Event(), asyncio.Event()
                clear = voice.clear_playout
                async def pause_clear():
                    await clear()
                    clearing.set()
                    await release.wait()
                voice.clear_playout = pause_clear
                switch = asyncio.create_task(provider_request(client, url, 'gemini'))
                try:
                    await asyncio.wait_for(clearing.wait(), 2)
                    old_pcm = asyncio.create_task(old.scope.output_audio(b'\x12\x01' * 20))
                    await asyncio.sleep(0)
                    assert not old_pcm.done()
                finally:
                    release.set()
                assert (await asyncio.wait_for(switch, 2))['status'] == 'switched'
                await asyncio.wait_for(old_pcm, 1)
                _, new = await asyncio.wait_for(opened.get(), 1)
                fresh = b'\x50\x02' * 20
                await new.scope.output_audio(fresh)
                assert await ws.receive_json(timeout=1) == {'serverContent': {'interrupted': True}}
                assert await ws.receive_json(timeout=1) == audio_envelope(fresh)
                assert any('"media_delivery": "sent"' in item['text'] for item in new.initial_items)
    asyncio.run(exercise())
