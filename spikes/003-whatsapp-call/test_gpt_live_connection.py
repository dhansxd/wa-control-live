import asyncio
import base64
from contextlib import asynccontextmanager
import json

from aiohttp import WSMsgType, web
import pytest

import gpt_live_connection
from gpt_live_connection import GPTLiveConnection, GPTLiveProviderError


KEY = 'gpt-live-test-key'
STARTED = {
    'type': 'session.started', 'event_id': 'event-ready',
    'session': {'id': 'live-test', 'status': 'active', 'model': 'gpt-live-1', 'expires_at': 2_000_000_000},
}


class FakeCall:
    def __init__(self):
        self.report = {}
        self.events = asyncio.Queue()
        self.audio = asyncio.Queue()
        self.failure = asyncio.Future()
        self.marks = []

    def mark(self, name):
        self.marks.append(name)

    def spawn(self, coroutine):
        return asyncio.create_task(coroutine)

    def fail(self, reason):
        if not self.failure.done():
            self.failure.set_result(reason)

    async def on_data(self, event):
        self.events.put_nowait(event)

    async def output_audio(self, pcm):
        self.audio.put_nowait(pcm)


@asynccontextmanager
async def serve(handler, monkeypatch=None):
    app = web.Application()
    app.router.add_get('/v1/live/sessions', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    endpoint = f'ws://127.0.0.1:{runner.addresses[0][1]}/v1/live/sessions'
    if monkeypatch is not None:
        monkeypatch.setattr(gpt_live_connection, 'ENDPOINT', endpoint)
    try:
        yield endpoint
    finally:
        await runner.cleanup()


async def take(queue):
    return await asyncio.wait_for(queue.get(), 2)


def transcript(speaker, event_id, text, start, end):
    return {'type': f'session.{speaker}_transcript.delta', 'event_id': event_id,
            'delta': text, 'start_ms': start, 'end_ms': end}


def accepted(operation, client_event_id):
    return {'type': operation + '.appended', 'event_id': 'accepted-' + client_event_id,
            'client_event_id': client_event_id, 'start_ms': 500, 'end_ms': 500}


@asynccontextmanager
async def provider_socket(monkeypatch):
    opened, received = asyncio.Queue(), asyncio.Queue()
    closed = asyncio.Event()

    async def handler(request):
        assert request.headers['Authorization'] == f'Bearer {KEY}'
        assert not request.query
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        opened.put_nowait(socket)
        try:
            async for frame in socket:
                if frame.type == WSMsgType.TEXT:
                    event = json.loads(frame.data)
                    received.put_nowait(event)
                    if event.get('type') == 'session.close':
                        await socket.send_json({
                            'type': 'session.closed', 'event_id': 'event-closed', 'reason': 'close_requested',
                            'session': STARTED['session'], 'usage': {'seconds': 1},
                        })
        finally:
            closed.set()
        return socket

    async with serve(handler, monkeypatch):
        yield opened, received, closed


async def start_session(connection, opened, received, response=STARTED):
    await connection.prepare()
    socket = await take(opened)
    start = asyncio.create_task(connection.start('Instructions', []))
    try:
        assert (await take(received))['type'] == 'session.start'
        await socket.send_json(response)
        await asyncio.wait_for(start, 2)
    finally:
        start.cancel()
        await asyncio.gather(start, return_exceptions=True)
    return socket


def test_gpt_live_waits_for_session_started_then_streams_audio_and_closes(monkeypatch):
    async def exercise():
        async with provider_socket(monkeypatch) as (opened, received, closed):
            call = FakeCall()
            connection = GPTLiveConnection(call, model='gpt-live-1', voice='quartz', api_key=KEY)
            start = None
            try:
                await connection.prepare()
                socket = await take(opened)
                start = asyncio.create_task(connection.start('Lanjutkan percakapan aktif.', [
                    {'role': 'developer', 'text': 'Gunakan bahasa Indonesia.'},
                    {'role': 'user', 'text': 'Pesan sebelumnya.'},
                    {'role': 'assistant', 'text': 'Jawaban sebelumnya.'},
                ]))
                event = await take(received)
                assert event['type'] == 'session.start'
                assert event['session'] == {
                    'model': 'gpt-live-1', 'instructions': 'Lanjutkan percakapan aktif.',
                    'audio': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'output': {'voice': 'quartz'}},
                    'delegation': {'type': 'client'},
                    'input': [
                        {'type': 'message', 'role': 'developer', 'content': [
                            {'type': 'input_text', 'text': 'Gunakan bahasa Indonesia.'},
                        ]},
                        {'type': 'message', 'role': 'user', 'content': [
                            {'type': 'input_text', 'text': 'Pesan sebelumnya.'},
                        ]},
                        {'type': 'message', 'role': 'assistant', 'content': [
                            {'type': 'output_text', 'text': 'Jawaban sebelumnya.'},
                        ]},
                    ],
                }
                assert not connection.can_speak and not start.done()
                connection.feed(b'\x00\x00' * 320)
                await socket.send_json(STARTED)
                await asyncio.wait_for(start, 2)
                assert connection.can_speak
                assert 'voice_session_started' in call.marks

                connection.feed(b'\xe8\x03' * 320)
                audio = await take(received)
                assert audio['type'] == 'session.input_audio.append'
                converted = base64.b64decode(audio['audio'], validate=True)
                # Filter priming can delay a few samples in the first 20 ms frame.
                assert 900 <= len(converted) <= 960 and len(converted) % 2 == 0
                assert b'\xe8\x03' * 8 in converted

                pcm = b'\x01\x02' * 480
                await socket.send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(pcm).decode()})
                await socket.send_json(transcript('output', 'assistant-1', 'Ini hasil', 80, 220))
                await socket.send_json(transcript('output', 'assistant-2', ' lengkapnya.', 220, 340))
                assert await take(call.audio) == pcm
                started = await take(call.events)
                assert started['type'] == 'turn.created' and started['turn']['role'] == 'assistant'
                finished = await take(call.events)
                while finished['type'] != 'turn.done':
                    finished = await take(call.events)
                assert finished['turn'] == {
                    'role': 'assistant', 'id': started['turn']['id'], 'transcript': 'Ini hasil lengkapnya.',
                }
                assert KEY not in repr(call.report)
            finally:
                if start is not None:
                    start.cancel()
                    await asyncio.gather(start, return_exceptions=True)
                await connection.close()
            assert not connection.can_speak
            assert (await take(received))['type'] == 'session.close'
            await asyncio.wait_for(closed.wait(), 2)
            assert not call.failure.done()

    asyncio.run(exercise())


def test_gpt_live_correlates_spoken_request_and_provider_acknowledgments(monkeypatch):
    async def exercise():
        async with provider_socket(monkeypatch) as (opened, received, _closed):
            call = FakeCall()
            connection = GPTLiveConnection(call, api_key=KEY)
            try:
                socket = await start_session(connection, opened, received)
                await socket.send_json(transcript('input', 'user-1', 'Tolong cari', 100, 250))
                await socket.send_json(transcript('input', 'user-2', ' bukti pesan.', 250, 400))
                await socket.send_json({'type': 'session.delegation.created', 'event_id': 'server-delegation',
                                        'offset_ms': 420, 'delegation': {
                                            'id': 'del-test', 'type': 'delegation', 'target': 'client',
                                        }})
                utterance = None
                delegation = None
                while utterance is None or delegation is None:
                    event = await take(call.events)
                    if event['type'] == 'turn.done':
                        utterance = event['turn']
                    elif event['type'] == 'delegation.created':
                        delegation = event['item']
                assert utterance['role'] == 'user' and utterance['transcript'] == 'Tolong cari bukti pesan.'
                assert (delegation['target'], delegation['user_bidi_turn_id']) == ('client', utterance['id'])
                item_id = delegation['id']

                connection.send_handoff(item_id, 'Pencarian sedang berjalan.', 'commentary')
                progress = await take(received)
                assert progress['type'] == 'session.thinking.append'
                assert progress['delegation_id'] == 'del-test' and progress['content'] == 'Pencarian sedang berjalan.'
                await socket.send_json(accepted('session.thinking', progress['event_id']))
                assert await take(call.events) == {'type': 'delegation.context.appended',
                                                   'delegation_item_id': item_id}

                connection.send_handoff(item_id, 'Buktinya ditemukan.', 'speakable')
                result = await take(received)
                assert result['type'] == 'session.commentary.append'
                assert result['delegation_id'] == 'del-test' and result['content'] == 'Buktinya ditemukan.'
                assert result['event_id'] != progress['event_id']
                await socket.send_json(accepted('session.commentary', 'unknown-command'))
                context = asyncio.create_task(connection.append_context('Pesan terbaru dari DM yang sama.'))
                next_command = await take(received)
                assert next_command['delegation_id'] is None
                assert next_command['content'] == 'Pesan terbaru dari DM yang sama.'
                await socket.send_json(accepted(next_command['type'].removesuffix('.append'), next_command['event_id']))
                assert await asyncio.wait_for(context, 2) is True
                assert call.events.empty(), 'an unrelated command must not acknowledge the delegated result'
                await socket.send_json(accepted('session.commentary', result['event_id']))
                assert await take(call.events) == {'type': 'delegation.context.appended',
                                                   'delegation_item_id': item_id}
            finally:
                await connection.close()

    asyncio.run(exercise())


def test_gpt_live_maps_new_user_speech_to_shared_interruption(monkeypatch):
    async def exercise():
        async with provider_socket(monkeypatch) as (opened, received, _closed):
            call = FakeCall()
            connection = GPTLiveConnection(call, api_key=KEY)
            try:
                socket = await start_session(connection, opened, received)
                pcm = b'\x01\x02' * 480
                await socket.send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(pcm).decode()})
                await socket.send_json(transcript('output', 'assistant-1', 'Ini hasil pertama', 120, 250))
                assert await take(call.audio) == pcm
                assistant = (await take(call.events))['turn']
                assert assistant['role'] == 'assistant'

                # Primary GPT-Live has no dedicated VAD or interruption event.
                await socket.send_json(transcript('input', 'user-1', 'Tunggu sebentar.', 300, 400))
                interrupted = None
                final = None
                while interrupted is None or final is None:
                    event = await take(call.events)
                    if event['type'] == 'turn.interrupted':
                        interrupted = event
                    elif event['type'] == 'turn.done' and event['turn']['role'] == 'user':
                        final = event['turn']
                assert interrupted['turn_id'] == assistant['id']
                assert final['transcript'] == 'Tunggu sebentar.'
                assert final['id'] != assistant['id']
            finally:
                await connection.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(('sample', 'interrupted'), [(b'\xe8\x03', True), (b'\x00\x00', False)])
def test_gpt_live_can_interrupt_playing_audio_before_its_transcript(monkeypatch, sample, interrupted):
    async def exercise():
        async with provider_socket(monkeypatch) as (opened, received, _closed):
            call = FakeCall()
            connection = GPTLiveConnection(call, api_key=KEY)
            try:
                socket = await start_session(connection, opened, received)
                pcm = sample * 480
                await socket.send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(pcm).decode()})
                assert await take(call.audio) == pcm
                await socket.send_json(transcript('input', 'user-1', 'Tunggu sebentar.', 300, 400))
                events = [await take(call.events)]
                while events[-1]['type'] != 'turn.done':
                    events.append(await take(call.events))
                cutoffs = [event for event in events if event['type'] == 'turn.interrupted']
                assert cutoffs == ([{'type': 'turn.interrupted', 'turn_id': None}] if interrupted else [])
                assert events[-1]['turn']['transcript'] == 'Tunggu sebentar.'
            finally:
                await connection.close()

    asyncio.run(exercise())


def test_gpt_live_can_keep_unfinalized_transcript_on_hangup(monkeypatch):
    async def exercise():
        monkeypatch.setattr(gpt_live_connection, 'TRANSCRIPT_SETTLE', 5)
        async with provider_socket(monkeypatch) as (opened, received, _closed):
            call = FakeCall()
            connection = GPTLiveConnection(call, api_key=KEY)
            try:
                socket = await start_session(connection, opened, received)
                await socket.send_json(transcript('input', 'user-1', 'Ada satu', 100, 200))
                await socket.send_json(transcript('input', 'user-2', ' pertanyaan.', 200, 300))
                await socket.send_json(transcript('output', 'assistant-1', 'Ini jawaban', 350, 450))
                await socket.send_json(transcript('output', 'assistant-2', ' terakhir.', 450, 550))
                pcm = b'\x01\x02' * 480
                await socket.send_json({'type': 'session.output_audio.delta', 'delta': base64.b64encode(pcm).decode()})
                assert await take(call.audio) == pcm
                started = [await take(call.events), await take(call.events)]
                assert all(event['type'] == 'turn.created' for event in started)
                turns = {event['turn']['role']: event['turn']['id'] for event in started}

                buffered = connection.buffered_transcripts()
                assert {event['turn']['role']: (event['type'], event['turn']['id'], event['turn']['transcript'])
                        for event in buffered} == {
                    'user': ('turn.done', turns['user'], 'Ada satu pertanyaan.'),
                    'assistant': ('turn.done', turns['assistant'], 'Ini jawaban terakhir.'),
                }
                assert call.events.empty(), 'history capture must not dispatch a turn or fabricate an acknowledgment'
            finally:
                await connection.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(('status', 'reason', 'retry'), [
    (401, 'auth_required', None),
    (403, 'access_denied', None),
    (429, 'rate_limited', 17),
    (503, 'unavailable', None),
])
def test_gpt_live_handshake_failure_reports_safe_reason(monkeypatch, status, reason, retry):
    async def exercise():
        async def handler(_request):
            headers = {'Retry-After': str(retry)} if retry is not None else {}
            return web.Response(status=status, headers=headers, text=f'private provider payload {KEY}')

        async with serve(handler, monkeypatch):
            call = FakeCall()
            connection = GPTLiveConnection(call, api_key=KEY)
            try:
                with pytest.raises(GPTLiveProviderError) as caught:
                    await connection.prepare()
                assert caught.value.reason == reason
                assert caught.value.retry_after == retry
                assert KEY not in str(caught.value) and KEY not in repr(call.report)
                assert not connection.can_speak
            finally:
                await connection.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('same_origin', [True, False])
def test_gpt_live_gateway_does_not_follow_redirects(same_origin):
    async def exercise():
        requests = []

        async def redirected(request):
            requests.append(('redirected', request.headers.get('Authorization')))
            return web.Response(status=400)

        async with serve(redirected) as redirect_target:
            async def gateway(request):
                if request.query:
                    return await redirected(request)
                requests.append(('gateway', request.headers.get('Authorization')))
                destination = '/v1/live/sessions?redirected=1' if same_origin else redirect_target.replace('ws:', 'http:', 1)
                raise web.HTTPTemporaryRedirect(destination)

            async with serve(gateway) as endpoint:
                call = FakeCall()
                connection = GPTLiveConnection(call, api_key=KEY, endpoint=endpoint)
                try:
                    with pytest.raises(GPTLiveProviderError) as caught:
                        await connection.prepare()
                    assert caught.value.reason == 'unsupported'
                    assert KEY not in str(caught.value) and endpoint not in str(caught.value)
                    assert requests == [('gateway', f'Bearer {KEY}')]
                finally:
                    await connection.close()

    asyncio.run(exercise())


def test_gpt_live_start_rejection_and_expired_session_report_safe_reasons(monkeypatch):
    async def exercise():
        async with provider_socket(monkeypatch) as (opened, received, _closed):
            call = FakeCall()
            connection = GPTLiveConnection(call, api_key=KEY)
            try:
                rejection = {'type': 'error', 'event_id': 'event-rejected', 'error': {
                    'type': 'authentication_error', 'code': 'invalid_api_key', 'message': f'Rejected key {KEY}',
                }}
                with pytest.raises(GPTLiveProviderError) as caught:
                    await start_session(connection, opened, received, rejection)
                assert caught.value.reason == 'auth_required' and KEY not in str(caught.value)
                assert not connection.can_speak
            finally:
                await connection.close()

        async with provider_socket(monkeypatch) as (opened, received, _closed):
            call = FakeCall()
            connection = GPTLiveConnection(call, api_key=KEY)
            try:
                socket = await start_session(connection, opened, received)
                await socket.send_json({
                    'type': 'session.closed', 'event_id': 'event-expired', 'reason': 'expired',
                    'error': None, 'session': STARTED['session'], 'usage': {'seconds': 30},
                })
                failure = await asyncio.wait_for(call.failure, 2)
                assert KEY not in failure and not connection.can_speak
                assert call.report['provider_failure']['reason'] == 'unavailable'
            finally:
                await connection.close()

    asyncio.run(exercise())
