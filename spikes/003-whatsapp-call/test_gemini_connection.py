import asyncio
import base64
import json

from aiohttp import WSMsgType, web
import pytest

import gemini_connection
from gemini_connection import GeminiConnection, GeminiProviderError


class FakeCall:
    def __init__(self):
        self.report = {}
        self.events = asyncio.Queue()
        self.audio = asyncio.Queue()
        self.failure = asyncio.Future()

    def mark(self, _name):
        pass

    def spawn(self, coroutine):
        return asyncio.create_task(coroutine)

    def fail(self, reason):
        if not self.failure.done():
            self.failure.set_result(reason)

    async def on_data(self, event):
        self.events.put_nowait(event)

    async def output_audio(self, pcm):
        self.audio.put_nowait(pcm)


async def serve(handler, monkeypatch):
    app = web.Application()
    app.router.add_get('/live', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = runner.addresses[0][1]
    monkeypatch.setattr(gemini_connection, 'ENDPOINT', f'ws://127.0.0.1:{port}/live')
    return runner


async def take(queue):
    return await asyncio.wait_for(queue.get(), 2)


def test_gemini_wire_setup_pcm_context_delegation_and_interruption(monkeypatch):
    async def exercise():
        calls, received = asyncio.Queue(), asyncio.Queue()

        async def handler(request):
            assert request.headers['x-goog-api-key'] == 'synthetic-key'
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            received.put_nowait(await socket.receive_json())
            await socket.send_json({'setupComplete': {}})
            calls.put_nowait(socket)
            async for frame in socket:
                if frame.type == WSMsgType.TEXT:
                    received.put_nowait(json.loads(frame.data))
            return socket

        runner = await serve(handler, monkeypatch)
        call = FakeCall()
        connection = GeminiConnection(call, model='models/gemini-3.8-live', voice='Kore', api_key='synthetic-key')
        try:
            await connection.prepare()
            assert not connection.can_speak
            await connection.start('instructions', [{'role': 'assistant', 'text': 'quoted history'}])
            socket = await take(calls)
            setup = (await take(received))['setup']
            assert setup['model'] == 'models/gemini-3.8-live'
            assert setup['generationConfig'] == {
                'responseModalities': ['AUDIO'],
                'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': 'Kore'}}},
            }
            assert setup['tools'][0]['functionDeclarations'][0]['name'] == 'ask_hermes'
            assert setup['historyConfig'] == {'initialHistoryInClientContent': True}
            assert (await take(received)) == {'clientContent': {
                'turns': [{'role': 'model', 'parts': [{'text': 'quoted history'}]}], 'turnComplete': True,
            }}
            assert call.report['initial_context_delivery'] == 'sent_unconfirmed'
            pcm = b'\x01\x02' * 8
            connection.feed(pcm)
            assert (await take(received)) == {'realtimeInput': {'audio': {
                'mimeType': 'audio/pcm;rate=16000', 'data': base64.b64encode(pcm).decode(),
            }}}
            assert await connection.append_context('new WhatsApp DM') is False
            assert (await take(received)) == {'clientContent': {
                'turns': [{'role': 'user', 'parts': [{'text': 'new WhatsApp DM'}]}], 'turnComplete': False,
            }}
            await socket.send_json({'toolCall': {'functionCalls': [{
                'id': 'gemini-tool-1', 'name': 'ask_hermes', 'args': {'request': 'find WhatsApp receipt'},
            }]}})
            await socket.send_json({'serverContent': {'inputTranscription': {'text': 'tolong cari bukti pesan'}}})
            created, completed, delegation = [await take(call.events) for _ in range(3)]
            assert created['type'] == 'turn.created' and created['turn']['role'] == 'user'
            assert completed['type'] == 'turn.done'
            assert completed['turn']['transcript'] == 'tolong cari bukti pesan'
            assert delegation['type'] == 'delegation.created'
            item = delegation['item']
            assert item['user_bidi_turn_id'] == created['turn']['id'] == completed['turn']['id']
            assert item['target'] == 'client' and item['content'] == [{'text': 'find WhatsApp receipt'}]
            connection.send_handoff(item['id'], 'working', 'commentary')
            assert (await take(received)) == {'toolResponse': {'functionResponses': [{
                'id': 'gemini-tool-1', 'name': 'ask_hermes', 'response': {'result': 'working'},
                'scheduling': 'SILENT',
            }]}}
            assert (await take(call.events)) == {'type': 'delegation.context.sent', 'delegation_item_id': item['id']}
            connection.send_handoff(item['id'], 'verified result', 'speakable')
            spoken = (await take(received))['realtimeInput']['text']
            assert spoken.endswith('"verified result"') and '[Pembaruan lisan dari aplikasi]' in spoken
            assert (await take(call.events))['type'] == 'delegation.context.sent'
            await socket.send_bytes(json.dumps({'serverContent': {
                'outputTranscription': {'text': 'ini hasilnya'},
                'modelTurn': {'parts': [{'inlineData': {
                    'mimeType': 'audio/pcm;rate=24000', 'data': base64.b64encode(pcm).decode(),
                }}]},
            }}).encode())
            assistant = await take(call.events)
            assert assistant['type'] == 'turn.created' and assistant['turn']['role'] == 'assistant'
            assert (await take(call.audio)) == pcm
            await socket.send_json({'serverContent': {'interrupted': True, 'turnComplete': True}})
            assert (await take(call.events)) == {'type': 'turn.interrupted', 'turn_id': assistant['turn']['id']}
            await socket.send_json({'serverContent': {'inputTranscription': {'text': 'tes lagi'}}})
            next_turn = await take(call.events)
            assert next_turn['turn']['id'] != created['turn']['id']
            await socket.send_json({'serverContent': {
                'outputTranscription': {'text': 'jawaban berikutnya'}, 'turnComplete': True,
            }})
            next_assistant, next_user_done, next_assistant_done = [await take(call.events) for _ in range(3)]
            assert next_user_done['turn']['transcript'] == 'tes lagi'
            assert next_assistant_done == {'type': 'turn.done', 'turn': {
                'id': next_assistant['turn']['id'], 'role': 'assistant', 'transcript': 'jawaban berikutnya',
            }}
            await socket.send_json({'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'details': [{
                '@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '3.25s',
            }]}})
            assert await asyncio.wait_for(call.failure, 2) == 'Gemini Live: rate_limited'
            assert call.report['provider_failure'] == {'reason': 'rate_limited', 'retry_after': 3.25}
            assert not connection.can_speak
        finally:
            await connection.close()
            await runner.cleanup()

    asyncio.run(exercise())


def test_gemini_late_transcription_keeps_turn_id_and_cancelled_tool_stays_unrouted():
    async def exercise():
        call = FakeCall()
        connection = GeminiConnection(call, api_key='synthetic-key')
        await connection._handle({'serverContent': {'interimInputTranscription': {'text': 'interim'}}})
        caller = (await take(call.events))['turn']
        assert caller['role'] == 'user' and not caller['transcript']
        await connection._handle({'toolCall': {'functionCalls': [{
            'id': 'cancelled', 'name': 'ask_hermes', 'args': {'request': 'model proposal'},
        }]}})
        assert call.events.empty()
        await connection._handle({'toolCallCancellation': {'ids': ['cancelled']}})
        await connection._handle({'serverContent': {'outputTranscription': {'text': 'response'}, 'turnComplete': True}})
        assistant, completed = [await take(call.events) for _ in range(2)]
        assert assistant['turn']['id'] == completed['turn']['id']
        await connection._handle({'serverContent': {'inputTranscription': {'text': 'late caller transcript'}}})
        assert await take(call.events) == {'type': 'turn.done', 'turn': {
            'role': 'user', 'id': caller['id'], 'transcript': 'late caller transcript',
        }}
        assert call.events.empty()

    asyncio.run(exercise())


@pytest.mark.parametrize(('code', 'reason', 'retry'), [
    (401, 'auth_required', None),
    (403, 'access_denied', None),
    (429, 'rate_limited', 17),
    (503, 'unavailable', None),
])
def test_gemini_handshake_reports_provider_status_without_credentials(monkeypatch, code, reason, retry):
    async def exercise():
        async def handler(_request):
            headers = {'Retry-After': str(retry)} if retry is not None else {}
            return web.Response(status=code, headers=headers, text='body containing synthetic-key')

        runner = await serve(handler, monkeypatch)
        connection = GeminiConnection(FakeCall(), api_key='synthetic-key')
        try:
            with pytest.raises(GeminiProviderError) as caught:
                await connection.prepare()
            assert caught.value.reason == reason and caught.value.retry_after == retry
            assert 'synthetic-key' not in str(caught.value)
        finally:
            await connection.close()
            await runner.cleanup()

    asyncio.run(exercise())
