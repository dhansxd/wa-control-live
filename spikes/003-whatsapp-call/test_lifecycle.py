"""Regression checks against real loopback WebSockets; provider is a local fixture."""
import asyncio
import inspect
import json
from types import SimpleNamespace
from aiohttp import ClientSession, WSMsgType, web
import pytest

import call_adapter as adapter
from call_adapter import STATE, LiveCall, create_app


async def host(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])


def test_disconnect_during_startup_cancels_owned_start(tmp_path):
    async def exercise():
        entered, stopped, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class SlowVoice:
            def __init__(self, path, send):
                self.path, self.report, self.failed = path, {}, asyncio.Event()
            async def start(self, setup):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            async def close(self):
                closed.set()
        app = create_app('local-fixture', tmp_path, voice_factory=SlowVoice)
        runner, url = await host(app)
        try:
            async with ClientSession() as client:
                ws = await client.ws_connect(url+'/live',headers={'x-goog-api-key':'local-fixture'})
                await ws.send_json({'setup':{}})
                await entered.wait()
                ws._response.close()  # simulate an abruptly ended call, not graceful server shutdown
                try:
                    await asyncio.wait_for(stopped.wait(), .5)
                    await asyncio.wait_for(closed.wait(), .5)
                except TimeoutError:
                    pytest.fail('Caller left, but provider startup and owned resources remained active')
                assert app[STATE]['active'] is None
        finally:
            await runner.cleanup()
    asyncio.run(exercise())


def test_cleanup_exception_does_not_wedge_single_call_slot(tmp_path):
    async def exercise():
        class BadCleanup:
            def __init__(self, path, send):
                self.path, self.report, self.failed = path, {}, asyncio.Event()
            async def start(self, setup):
                pass
            async def close(self):
                raise RuntimeError('fixture cleanup failed')
        app = create_app('local-fixture', tmp_path, voice_factory=BadCleanup)
        runner, url = await host(app)
        try:
            async with ClientSession() as client:
                ws = await client.ws_connect(url+'/live',headers={'x-goog-api-key':'local-fixture'})
                await ws.send_json({'setup':{}})
                assert await ws.receive_json() == {'setupComplete':{}}
                await ws.close()
                for _ in range(30):
                    if app[STATE]['active'] is None:
                        break
                    await asyncio.sleep(.005)
                assert app[STATE]['active'] is None, 'Cleanup exception kept the bridge permanently busy'
                assert 'cleanup failed' in app[STATE]['last_error']
        finally:
            await runner.cleanup()
    asyncio.run(exercise())


def test_start_failure_persists_failed_not_clean_closed(tmp_path, monkeypatch):
    async def fail_start(self, setup):
        raise RuntimeError('fixture provider setup failed')
    monkeypatch.setattr(LiveCall, 'start', fail_start)
    async def exercise():
        runner, url = await host(create_app('local-fixture', tmp_path))
        try:
            async with ClientSession() as client:
                ws = await client.ws_connect(url+'/live',headers={'x-goog-api-key':'local-fixture'})
                await ws.send_json({'setup':{}})
                assert (await ws.receive()).type == WSMsgType.CLOSE
            import json
            report = json.loads(next(tmp_path.glob('*/report.json')).read_text())
            assert report['status'] == 'failed', report
            assert 'provider setup failed' in report['error']
        finally:
            await runner.cleanup()
    asyncio.run(exercise())


def test_live_call_finalizes_local_recordings_when_connection_close_fails(tmp_path):
    class Connection:
        route = 'fixture'

        def __init__(self, _call):
            pass

        def stop_input(self):
            pass

        async def close(self):
            raise RuntimeError('fixture teardown failed')

    async def exercise():
        voice = LiveCall(tmp_path, lambda _message: None, connection_factory=Connection)
        await voice.close()
        assert voice.recording._file is None and voice.input_recording._file is None
        assert voice.report['status'] == 'failed'
        assert voice.report['cleanup_errors'] == ['audio connection: fixture teardown failed']
        assert (tmp_path/'report.json').is_file()

    asyncio.run(exercise())


@pytest.mark.parametrize('first_status', ['busy', 'unknown', 'unavailable'])
def test_voice_history_retries_after_hangup_without_blocking_next_call(tmp_path, monkeypatch, first_status):
    async def exercise():
        lease_released = asyncio.Event()
        writer_waiting = asyncio.Event()
        wait_attempts = []
        saved = []

        class Jobs:
            async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
                if wait_for_lease:
                    wait_attempts.append(exchange['exchange_id'])
                    writer_waiting.set()
                    await lease_released.wait()
                if not lease_released.is_set():
                    return {'status': first_status}
                saved.append((session_id, exchange['user'], exchange['assistant']))
                return {'status': 'saved'}

        class Voice(LiveCall):
            async def start(self, setup):
                self.hermes_session_id = 'active-whatsapp-dm'
                if not setup.get('second_call'):
                    self.completed_exchanges.append({
                        'exchange_id': 'first-pair', 'user': 'Kucing Biru.', 'assistant': 'Aku ingat.',
                    })

        monkeypatch.setattr(adapter, 'VOICE_HISTORY_RETRY_INTERVAL', .01)
        monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
        app = create_app('local-fixture', tmp_path, voice_factory=lambda path, send: Voice(path, send, agent_jobs=Jobs()))
        runner, url = await host(app)
        try:
            async with ClientSession(headers={'x-goog-api-key': 'local-fixture'}) as client:
                first = await client.ws_connect(url + '/live')
                await first.send_json({'setup': {}})
                assert await first.receive_json() == {'setupComplete': {}}
                await first.close()
                async with asyncio.timeout(2):
                    while app[STATE]['active'] is not None:
                        await asyncio.sleep(.01)
                report_path = tmp_path / app[STATE]['last'] / 'report.json'
                report = json.loads(report_path.read_text())
                assert report['voice_history_status'] == first_status
                assert report['voice_exchanges_pending'] == 1
                await asyncio.wait_for(writer_waiting.wait(), 1)
                second = await client.ws_connect(url + '/live')
                await second.send_json({'setup': {'second_call': True}})
                assert await second.receive_json() == {'setupComplete': {}}
                lease_released.set()
                async with asyncio.timeout(2):
                    while json.loads(report_path.read_text())['voice_exchanges_pending']:
                        await asyncio.sleep(.01)
                assert saved == [('active-whatsapp-dm', 'Kucing Biru.', 'Aku ingat.')]
                assert wait_attempts == ['first-pair']
                await second.close()
        finally:
            lease_released.set()
            await runner.cleanup()

    asyncio.run(exercise())


@pytest.mark.parametrize('phase', ['account/rateLimits/read', 'thread/realtime/stop', 'peer'])
def test_close_rejects_late_callbacks_and_preserves_final_report(tmp_path, monkeypatch, phase):
    async def exercise():
        submitted, sent, audio_reads = [], [], []
        event = {'type':'delegation.created', 'item':{'id':'late-delegation',
                 'content':[{'text':'Buat catatan uji berisi tes suara berhasil'}]}}

        async def native_task(submission_id, request, *, session_id):
            submitted.append(request)

        async def send(message):
            sent.append(message)

        class RemoteAudio:
            kind = 'audio'

            async def recv(self):
                audio_reads.append(True)
                await asyncio.Event().wait()

        async def late_callbacks():
            voice.connection._data(json.dumps(event))
            voice.connection._data('malformed late event')
            voice.connection._track(RemoteAudio())
            await voice.connection._state()
            await voice.output_audio(bytes(960))
            await voice.on_data({'type':'turn.done', 'turn':{'role':'assistant', 'transcript':'late reply'}})
            voice.feed(bytes(640))
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        class LocalServer:
            async def call(self, method, params=None, timeout=35):
                if method == phase:
                    await late_callbacks()
                return {}

            async def close(self):
                pass

        class LocalPeer:
            connectionState = 'failed'

            async def close(self):
                if phase == 'peer':
                    await late_callbacks()
                await original_peer.close()

        monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
        voice = LiveCall(tmp_path, send, agent_jobs=SimpleNamespace(submit=native_task))
        voice.hermes_session_id = 'active-whatsapp-dm'
        original_peer = voice.connection.peer
        voice.connection.peer, voice.connection.server = LocalPeer(), LocalServer()
        voice.connection.channel = type('CapturedChannel', (), {'send':lambda self, message:sent.append(message)})()
        voice.connection.started, voice.connection.thread_id = True, 'offline-fixture'
        try:
            await voice.close()
            assert submitted == [], 'A delegation started native work during call cleanup'
            assert audio_reads == [], 'A late audio callback started a worker during cleanup'
            assert sent == []
            assert all(task.done() for task in voice.tasks)
            report_bytes = (tmp_path/'report.json').read_bytes()
            report = json.loads(report_bytes)
            assert report == voice.report
            assert report['status'] == 'transport-closed-content-unverified'
            assert report['input_bytes'] == report['output_bytes'] == 0
            assert report['cleanup_errors'] == []
            recordings = {name:(tmp_path/name).read_bytes() for name in ('caller.wav', 'reply.wav')}
            task_count = len(voice.tasks)

            await late_callbacks()
            assert submitted == audio_reads == sent == []
            assert len(voice.tasks) == task_count
            assert voice.report == report
            assert (tmp_path/'report.json').read_bytes() == report_bytes
            assert all((tmp_path/name).read_bytes() == data for name, data in recordings.items())
            await voice.close()
        finally:
            for task in voice.tasks:
                task.cancel()
            await asyncio.gather(*voice.tasks, return_exceptions=True)
            await original_peer.close()
            voice.recording.close()
            voice.input_recording.close()
    asyncio.run(exercise())


def test_close_disposes_worker_cancelled_before_its_first_step(tmp_path, monkeypatch):
    async def exercise():
        monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
        voice = LiveCall(tmp_path, lambda _message: None)
        worker = voice.on_data({'type':'turn.done'})
        task = voice.spawn(worker)
        try:
            await voice.close()
            assert task.cancelled()
            assert inspect.getcoroutinestate(worker) == inspect.CORO_CLOSED
        finally:
            worker.close()
    asyncio.run(exercise())


def test_close_cancels_pending_handoff_observer_without_replaying(tmp_path, monkeypatch):
    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        submitted, sent = [], []
        event = {'type':'delegation.created', 'item':{'id':'active-delegation',
                 'target':'client', 'user_bidi_turn_id':'owner-turn',
                 'content':[{'text':'Buat catatan uji berisi tes suara berhasil'}]}}

        async def native_task(submission_id, request, *, session_id):
            assert session_id == 'active-whatsapp-dm'
            submitted.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
        async def send(_message):
            pass
        async def route(*_args, session_id):
            assert session_id == 'active-whatsapp-dm'
            return {'status': 'task'}
        voice = LiveCall(tmp_path, send, agent_jobs=SimpleNamespace(
            submit=native_task, route=route,
        ))
        voice.hermes_session_id = 'active-whatsapp-dm'
        def append(_channel, message):
            sent.append(json.loads(message))
            voice.connection._data(json.dumps({'type': 'delegation.context.appended',
                                    'delegation_item_id': 'active-delegation'}))
        voice.connection.channel = type('CapturedChannel', (), {'send': append})()
        await voice.on_data({'type':'turn.done', 'turn':{'role':'user', 'id':'owner-turn',
                            'transcript': 'Buat catatan uji berisi tes suara berhasil'}})
        voice.connection._data(json.dumps(event))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await voice.close()
            assert cancelled.is_set()
            assert all(task.done() for task in voice.tasks)
            report = json.loads((tmp_path/'report.json').read_text())
            assert report == voice.report
            assert report['backend_completed_delegations'] == []
            assert len(sent) == 1  # Only the preliminary acknowledgment.
            voice.connection._data(json.dumps(event))
            await asyncio.sleep(0)
            assert len(submitted) == 1
        finally:
            await voice.close()
    asyncio.run(exercise())
