import asyncio
import json
import time

import av
from aiortc.mediastreams import MediaStreamError
import pytest

import realtime_connection as realtime
from call_adapter import ready_or_fail


class Call:
    def __init__(self):
        self.tasks = []
        self.events = asyncio.Queue()
        self.failed = asyncio.Event()
        self.closed = False
        self.audio = []
        self.report = {}

    async def ready_or_fail(self, awaitable, timeout):
        return await ready_or_fail(self, awaitable, timeout)

    def fail(self, message):
        self.report['error'] = message
        self.failed.set()

    def mark(self, name):
        self.report[name] = True

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def on_data(self, event):
        self.events.put_nowait(event)

    async def output_audio(self, pcm):
        self.audio.append(pcm)

    async def cleanup(self, connection):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await connection.close()


class EventTarget:
    def __init__(self):
        self.handlers = {}

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback
        return register

    def emit(self, event, *args):
        self.handlers[event](*args)


class Channel(EventTarget):
    def __init__(self):
        super().__init__()
        self.readyState = 'open'
        self.sent = asyncio.Queue()

    def send(self, data):
        self.sent.put_nowait(json.loads(data))

    def deliver(self, event):
        self.emit('message', json.dumps(event))

    def acknowledge(self, event):
        self.deliver({'type': 'conversation.item.added', 'item': {'id': event['item']['id']}})


class Peer(EventTarget):
    def __init__(self, configuration):
        super().__init__()
        assert configuration.iceServers == []
        self.channel = Channel()

    def addTrack(self, track):
        self.track = track

    def createDataChannel(self, label):
        assert label == 'oai-events'
        return self.channel

    async def createOffer(self):
        return realtime.RTCSessionDescription(type='offer', sdp='offer')

    async def setLocalDescription(self, offer):
        self.localDescription = offer

    async def setRemoteDescription(self, answer):
        assert answer.type == 'answer' and answer.sdp == 'answer'
        self.connectionState = 'connected'
        self.emit('connectionstatechange')
        self.channel.emit('open')
        self.channel.deliver({'type': 'session.created'})

    async def close(self):
        self.connectionState = 'closed'
        self.emit('connectionstatechange')


class HTTP:
    status = 201
    content_type = 'application/json'
    headers = {'Retry-After': '9'}

    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self

    async def text(self):
        return 'answer'

    async def json(self):
        return {'error': {'code': 'insufficient_quota', 'message': 'masked fake secret'}}


async def mint_secret():
    return {'status': 'ready', 'client_secret': 'ephemeral-fixture', 'expires_at': time.time() + 60}


async def take(queue):
    return await asyncio.wait_for(queue.get(), 2)


def test_realtime_wire_setup_audio_and_context_acknowledgements(monkeypatch):
    async def exercise():
        http = HTTP()
        monkeypatch.setattr(realtime, 'ClientSession', lambda: http)
        monkeypatch.setattr(realtime, 'RTCPeerConnection', Peer)
        call = Call()
        connection = realtime.HermesRealtimeConnection(call, mint_secret=mint_secret)
        try:
            await connection.prepare()
            assert http.requests == [('https://api.openai.com/v1/realtime/calls', {
                'data': 'offer', 'allow_redirects': False, 'headers': {
                    'Authorization': 'Bearer ephemeral-fixture', 'Content-Type': 'application/sdp',
                },
            })]
            assert not connection.can_speak
            channel = connection.channel
            startup = call.spawn(connection.start('behave', [{'role': 'developer', 'text': 'behave'},
                                                             {'role': 'user', 'text': 'prior DM'}]))
            history = await take(channel.sent)
            assert history['item']['content'] == [{'type': 'input_text', 'text': 'prior DM'}]
            assert not startup.done()
            channel.acknowledge(history)
            setup = await take(channel.sent)
            assert setup['type'] == 'session.update'
            assert setup['session']['instructions'] == 'behave'
            assert setup['session']['tools'][0]['name'] == 'ask_hermes'
            assert setup['session']['audio']['input']['turn_detection']['create_response']
            assert not startup.done()
            channel.deliver({'type': 'session.updated'})
            await startup
            assert connection.can_speak
            assert call.events.empty()

            frame = av.AudioFrame(format='s16', layout='mono', samples=24)
            frame.sample_rate = 24000
            pcm = b'\x02\x01' * 24
            frame.planes[0].update(pcm)

            class Remote:
                frames = iter([frame])

                async def recv(self):
                    for next_frame in self.frames:
                        return next_frame
                    raise MediaStreamError

            await connection._read_audio(Remote())
            assert call.audio == [pcm]
            assert call.report['provider_failure']['reason'] == 'unavailable'
        finally:
            await call.cleanup(connection)

    asyncio.run(exercise())


def test_realtime_tool_uses_correlated_spoken_turn_and_provider_acks(monkeypatch):
    async def exercise():
        monkeypatch.setattr(realtime, 'RTCPeerConnection', Peer)
        call = Call()
        connection = realtime.HermesRealtimeConnection(call, mint_secret=mint_secret)
        connection._open_peer()
        connection.ready = True
        channel = connection.channel

        async def speech(item, response):
            await connection._handle({'type': 'input_audio_buffer.speech_started', 'item_id': item})
            await connection._handle({'type': 'input_audio_buffer.committed', 'item_id': item})
            await connection._handle({'type': 'response.created', 'response': {'id': response}})
            await connection._handle({'type': 'conversation.item.input_audio_transcription.completed',
                                      'item_id': item, 'transcript': item})
            assert [(await take(call.events))['type'] for _ in range(2)] == ['turn.interrupted', 'turn.created']
            return (await take(call.events))['turn']

        def response(id_, status):
            return {'type': 'response.done', 'response': {'id': id_, 'status': status, 'output': [{
                'type': 'function_call', 'status': 'completed', 'name': 'ask_hermes',
                'call_id': f'{id_}-tool', 'arguments': json.dumps({'request': 'tolong periksa chat saya'}),
            }]}}

        try:
            await speech('older-audio', 'older-response')
            user = await speech('current-audio', 'current-response')
            await connection._handle(response('older-response', 'completed'))
            reject = await take(channel.sent)
            assert reject['item']['call_id'] == 'older-response-tool'
            assert call.events.empty()
            channel.acknowledge(reject)
            assert (await take(call.events))['type'] == 'delegation.context.appended'

            await connection._handle({'type': 'response.function_call_arguments.done', 'item_id': 'partial'})
            assert call.events.empty()
            await connection._handle(response('current-response', 'completed'))
            delegation = await take(call.events)
            assert delegation['type'] == 'delegation.created'
            item = delegation['item']
            assert item['user_bidi_turn_id'] == user['id']
            assert item['content'] == [{'text': 'tolong periksa chat saya'}]
            await connection._handle(response('current-response', 'completed'))
            assert call.events.empty()

            connection.send_handoff(item['id'], 'Pekerjaan diterima.', 'commentary')
            quiet = await take(channel.sent)
            assert quiet['item']['type'] == 'function_call_output'
            assert quiet['item']['call_id'] == 'current-response-tool'
            channel.deliver({'type': 'conversation.item.added', 'item': {'id': 'unrelated'}})
            assert call.events.empty()
            channel.acknowledge(quiet)
            assert (await take(call.events)) == {
                'type': 'delegation.context.appended', 'delegation_item_id': item['id'],
            }
            assert channel.sent.empty()

            connection.send_handoff(item['id'], 'Ini hasilnya.', 'speakable')
            message = await take(channel.sent)
            assert message['item']['role'] == 'system'
            assert call.events.empty()
            channel.acknowledge(message)
            assert (await take(call.events))['type'] == 'delegation.context.appended'
            speak = await take(channel.sent)
            assert speak['type'] == 'response.create'
            assert speak['response'] == {
                'metadata': {'source': 'hermes', 'request_id': speak['event_id']}, 'tool_choice': 'none',
            }
            channel.deliver({'type': 'response.created', 'response': {
                'id': 'manual-response', 'metadata': speak['response']['metadata'],
            }})
            channel.deliver({'type': 'response.done', 'response': {'id': 'manual-response', 'status': 'completed'}})

            await speech('cancelled-audio', 'cancelled-response')
            await connection._handle({'type': 'response.output_item.added', 'response_id': 'manual-response',
                                      'item': {'id': 'stale-assistant', 'role': 'assistant'}})
            await connection._handle(response('cancelled-response', 'cancelled'))
            assert call.events.empty()
            assert channel.sent.empty()
        finally:
            await call.cleanup(connection)

    asyncio.run(exercise())


def test_realtime_failure_is_typed_and_rejects_pending_context(monkeypatch):
    async def exercise():
        http = HTTP()
        monkeypatch.setattr(realtime, 'ClientSession', lambda: http)
        monkeypatch.setattr(realtime, 'RTCPeerConnection', Peer)
        call = Call()

        async def broker_limited():
            return {'status': 'rate_limited', 'retry_after': 23}

        denied = realtime.HermesRealtimeConnection(call, mint_secret=broker_limited)
        with pytest.raises(realtime.RealtimeProviderError) as error:
            await denied._answer('offer')
        assert (error.value.reason, error.value.retry_after) == ('rate_limited', 23)
        assert not http.requests
        await denied.close()

        connection = realtime.HermesRealtimeConnection(call, mint_secret=mint_secret)
        try:
            http.status = 307
            with pytest.raises(realtime.RealtimeProviderError, match='unsupported'):
                await connection._answer('offer')
            http.status = 429
            with pytest.raises(realtime.RealtimeProviderError, match='quota_exhausted') as error:
                await connection._answer('offer')
            assert error.value.retry_after == 9
            assert 'masked fake secret' not in str(error.value)

            connection._open_peer()
            update = call.spawn(connection.append_context('Update from DM'))
            message = await take(connection.channel.sent)
            assert message['item']['role'] == 'system'
            connection.channel.deliver({'type': 'error', 'error': {
                'code': 'rate_limit_exceeded', 'event_id': message['event_id'],
            }})
            with pytest.raises(realtime.RealtimeProviderError, match='rate_limited'):
                await asyncio.wait_for(update, 1)
            assert call.report['provider_failure']['reason'] == 'rate_limited'
        finally:
            await call.cleanup(connection)

    asyncio.run(exercise())
