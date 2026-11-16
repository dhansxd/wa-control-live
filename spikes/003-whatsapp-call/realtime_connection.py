"""OpenAI Realtime WebRTC using a client secret minted through Hermes."""

import asyncio
from collections import deque
import json
import math
import time
from uuid import uuid4

import av
from aiohttp import ClientError, ClientSession
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from call_adapter import CallerTrack, ContextUpdateUnconfirmed, RESULT_ACK_TIMEOUT, resample_pcm
from gemini_connection import retry_delay
from gpt_live_connection import provider_error


ENDPOINT = 'https://api.openai.com/v1/realtime/calls'
SETUP_TIMEOUT = 12
FAILURES = {'auth_required', 'access_denied', 'billing_required', 'quota_exhausted',
            'rate_limited', 'unsupported', 'unavailable', 'unknown'}
HERMES_TOOL = {
    'type': 'function', 'name': 'ask_hermes',
    'description': 'Ask the Hermes agent to use the active WhatsApp conversation or its tools.',
    'parameters': {'type': 'object', 'properties': {'request': {'type': 'string'}},
                   'required': ['request'], 'additionalProperties': False},
}


class RealtimeProviderError(ConnectionError):
    def __init__(self, reason, retry_after=None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f'OpenAI Realtime: {reason}')


class HermesRealtimeConnection:
    """Use the Realtime audio and translate its events for the existing LiveCall."""

    route = 'hermes-realtime'

    def __init__(self, call, *, mint_secret):
        self.call, self.mint_secret = call, mint_secret
        self.peer = self.channel = None
        self.track = CallerTrack()
        self.ready = self.closed = False
        self.connected = asyncio.Event()
        self.channel_open = asyncio.Event()
        self.session_created = asyncio.Event()
        self.session_updated = asyncio.Event()
        self.events = asyncio.Queue(maxsize=512)
        self.pending = {}
        self.delegations = {}
        self.completed_responses = deque(maxlen=64)
        self.prefix = uuid4().hex
        self.caller = self.committed = None
        self.assistant = self.assistant_response = None
        self.audio_suppressed = False
        self.response_inputs = {}
        self.response_idle = asyncio.Event()
        self.response_idle.set()
        self.speech_lock = asyncio.Lock()
        self.speech_request = None

    @property
    def can_speak(self):
        return self.ready and not (self.closed or self.call.failed.is_set())

    async def prepare(self):
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                self._open_peer()
                offer = await self.peer.createOffer()
                await self.peer.setLocalDescription(offer)
                answer = await self._answer(self.peer.localDescription.sdp)
                await self.peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
        except (ClientError, TimeoutError):
            raise RealtimeProviderError('unavailable') from None
        await self.call.ready_or_fail(self._wait_connected(), SETUP_TIMEOUT)
        self.call.mark('peer_connected')

    def _open_peer(self):
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peer.addTrack(self.track)
        self.channel = self.peer.createDataChannel('oai-events')
        self.call.spawn(self._read_events())

        @self.peer.on('connectionstatechange')
        def state_changed():
            if self.peer.connectionState == 'connected':
                self.connected.set()
            elif self.peer.connectionState in {'failed', 'closed'}:
                self._fail(RealtimeProviderError('unavailable'))

        @self.peer.on('track')
        def track_added(remote):
            if remote.kind == 'audio':
                self.call.spawn(self._read_audio(remote))

        @self.channel.on('open')
        def opened():
            self.channel_open.set()

        @self.channel.on('close')
        def closed():
            self._fail(RealtimeProviderError('unavailable'))

        @self.channel.on('message')
        def message_received(message):
            try:
                self.events.put_nowait(message)
            except asyncio.QueueFull:
                self._fail(RealtimeProviderError('unavailable'))

    async def _wait_connected(self):
        await self.connected.wait()
        await self.channel_open.wait()
        await self.session_created.wait()

    async def _answer(self, offer):
        minted = await self.mint_secret()
        status = minted.get('status') if isinstance(minted, dict) else None
        if status != 'ready':
            delay = minted.get('retry_after') if isinstance(minted, dict) else None
            if type(delay) not in (int, float) or not math.isfinite(delay) or delay <= 0:
                delay = None
            raise RealtimeProviderError(status if isinstance(status, str) and status in FAILURES else 'unavailable',
                                        delay)
        secret, expiry = minted.get('client_secret'), minted.get('expires_at')
        if (not isinstance(secret, str) or not secret or not secret.isascii() or any(ch.isspace() for ch in secret)
                or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time()):
            raise RealtimeProviderError('unavailable')
        headers = {'Authorization': f'Bearer {secret}', 'Content-Type': 'application/sdp'}
        async with ClientSession() as http:
            async with http.post(ENDPOINT, data=offer, headers=headers, allow_redirects=False) as response:
                if 200 <= response.status < 300:
                    return await response.text()
                if 300 <= response.status < 400:
                    raise RealtimeProviderError('unsupported')
                detail = None
                if response.content_type == 'application/json':
                    try:
                        body = await response.json()
                    except (ValueError, ClientError):
                        body = None
                    if isinstance(body, dict):
                        detail = body.get('error')
                error = provider_error(response.status, detail=detail,
                                       retry_after=retry_delay(response.headers.get('Retry-After')))
                raise RealtimeProviderError(error.reason, error.retry_after)

    async def start(self, prompt, initial_items):
        # Confirm the existing WhatsApp context before enabling automatic speaking.
        for item in initial_items:
            if item['text'] and not (item['role'] == 'developer' and item['text'] == prompt):
                await self._add_item(self._message(item['role'], item['text']))
        self._send({
            'type': 'session.update', 'session': {
                'type': 'realtime', 'instructions': prompt, 'tools': [HERMES_TOOL], 'tool_choice': 'auto',
                'audio': {'input': {
                    'transcription': {'model': 'gpt-4o-mini-transcribe'},
                    'turn_detection': {'type': 'server_vad', 'create_response': True, 'interrupt_response': True},
                }},
            },
        })
        await self.call.ready_or_fail(self.session_updated.wait(), SETUP_TIMEOUT)
        self.ready = True
        self.call.mark('voice_session_started')

    def feed(self, pcm):
        if self.can_speak:
            self.track.feed(pcm)

    def stop_input(self):
        self.track.stop()

    async def _read_audio(self, track):
        resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
        try:
            while not self.closed:
                frame = await track.recv()
                if self.can_speak and not self.audio_suppressed:
                    for pcm in resample_pcm(frame, resampler):
                        await self.call.output_audio(pcm)
        except MediaStreamError:
            self._fail(RealtimeProviderError('unavailable'))

    def _fail(self, error):
        if not (self.closed or self.call.closed or self.call.failed.is_set()):
            self.call.report['provider_failure'] = {'reason': error.reason, 'retry_after': error.retry_after}
            self.call.fail(str(error))
            for pending in set(self.pending.values()):
                if not pending.done():
                    pending.set_exception(error)

    def _send(self, event):
        if self.closed or self.channel is None or self.channel.readyState != 'open':
            raise RealtimeProviderError('unavailable')
        try:
            self.channel.send(json.dumps(event, ensure_ascii=False))
        except Exception:
            raise RealtimeProviderError('unavailable') from None

    @staticmethod
    def _message(role, text):
        if role == 'developer':
            role = 'system'
        content_type = 'output_text' if role == 'assistant' else 'input_text'
        return {'type': 'message', 'role': role, 'content': [{'type': content_type, 'text': text}]}

    async def _add_item(self, item):
        item_id = uuid4().hex
        event_id = uuid4().hex
        pending = asyncio.get_running_loop().create_future()
        self.pending[item_id] = self.pending[event_id] = pending
        try:
            self._send({'type': 'conversation.item.create', 'event_id': event_id,
                        'item': {'id': item_id, **item}})
            await asyncio.wait_for(pending, RESULT_ACK_TIMEOUT)
        finally:
            self.pending.pop(item_id, None)
            self.pending.pop(event_id, None)
            if not pending.done():
                pending.cancel()

    async def append_context(self, text):
        try:
            await self._add_item(self._message('developer', text))
        except TimeoutError:
            raise ContextUpdateUnconfirmed('OpenAI Realtime did not confirm the context append') from None
        return True

    async def _speak_appended(self, caller):
        async with self.speech_lock, asyncio.timeout(RESULT_ACK_TIMEOUT):
            await self.call.ready_or_fail(self.response_idle.wait(), RESULT_ACK_TIMEOUT)
            if self.caller != caller:
                return
            event_id = uuid4().hex
            pending = asyncio.get_running_loop().create_future()
            self.pending[event_id] = pending
            self.speech_request = (event_id, caller)
            try:
                self._send({'type': 'response.create', 'event_id': event_id, 'response': {
                    'tool_choice': 'none', 'metadata': {'source': 'hermes', 'request_id': event_id},
                }})
                await pending
            finally:
                self.pending.pop(event_id, None)
                self.speech_request = None
                if not pending.done():
                    pending.cancel()

    async def speak(self, text):
        caller = self.caller
        await self.append_context(text)
        await self._speak_appended(caller)

    def send_handoff(self, item_id, text, channel):
        if not self.can_speak:
            raise RealtimeProviderError('unavailable')
        self.call.spawn(self._send_handoff(item_id, text, channel))

    async def _send_handoff(self, item_id, text, channel):
        caller = self.caller
        call_id = self.delegations.pop(item_id, None)
        item = ({'type': 'function_call_output', 'call_id': call_id, 'output': text}
                if call_id else self._message('developer', text))
        try:
            await self._add_item(item)
        except (RealtimeProviderError, TimeoutError):
            if call_id is not None and not self.closed:
                self.delegations.setdefault(item_id, call_id)
            return
        await self.call.on_data({'type': 'delegation.context.appended', 'delegation_item_id': item_id})
        if channel == 'speakable':
            try:
                await self._speak_appended(caller)
            except (OSError, RuntimeError):
                # The item was stored. The existing call decides if it still needs to announce it.
                return

    async def _read_events(self):
        while not self.closed:
            message = await self.events.get()
            try:
                event = json.loads(message)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                await self._handle(event)

    def _id(self, item_id):
        return f'{self.prefix}:{item_id}'

    async def _turn(self, kind, item_id, role, transcript=''):
        await self.call.on_data({'type': kind, 'turn': {
            'id': self._id(item_id), 'role': role, 'transcript': transcript,
        }})

    async def _handle(self, data):
        kind = data.get('type')
        item = data.get('item') or {}
        item_id = data.get('item_id') or item.get('id')
        if kind == 'session.created':
            self.session_created.set()
        elif kind == 'session.updated':
            self.session_updated.set()
        elif kind in {'conversation.item.added', 'conversation.item.done'}:
            if pending := self.pending.get(item_id):
                if not pending.done():
                    pending.set_result(None)
        elif kind == 'error':
            detail = data.get('error') or {}
            mapped = provider_error(detail=detail)
            error = RealtimeProviderError(mapped.reason)
            busy = detail.get('code') == 'conversation_already_has_active_response'
            pending = self.pending.get(detail.get('event_id'))
            if pending is not None and (busy or error.reason in {'unsupported', 'unknown'}):
                if not pending.done():
                    pending.set_exception(error)
            elif not busy:
                self._fail(error)
        elif kind == 'input_audio_buffer.speech_started':
            if not item_id:
                return
            self.caller = item_id
            self.audio_suppressed = True
            await self.call.on_data({'type': 'turn.interrupted'})
            self.assistant = self.assistant_response = None
            await self._turn('turn.created', item_id, 'user')
        elif kind == 'input_audio_buffer.committed':
            self.committed = item_id
        elif kind == 'conversation.item.input_audio_transcription.completed':
            await self._turn('turn.done', item_id, 'user', data.get('transcript', ''))
        elif kind == 'response.created':
            response = data['response']
            metadata = response.get('metadata') or {}
            is_manual = metadata.get('source') == 'hermes'
            caller = self.committed
            if is_manual:
                request = self.speech_request
                caller = None
                if request is not None and request[0] == metadata.get('request_id'):
                    event_id, caller = request
                    if (pending := self.pending.get(event_id)) and not pending.done():
                        pending.set_result(None)
            else:
                self.committed = None
            self.response_inputs[response['id']] = (caller, is_manual)
            self.response_idle.clear()
        elif kind == 'response.output_item.added' and item.get('role') == 'assistant':
            source = self.response_inputs.get(data.get('response_id'))
            if source is None or source[0] != self.caller:
                return
            self.assistant, self.assistant_response = item_id, data.get('response_id')
            self.audio_suppressed = False
            await self._turn('turn.created', item_id, 'assistant')
        elif kind == 'response.output_audio_transcript.delta' and item_id == self.assistant:
            await self.call.on_data({'type': 'turn.delta', 'turn_id': self._id(item_id), 'delta': data.get('delta', '')})
        elif kind == 'response.output_audio_transcript.done' and item_id == self.assistant:
            await self._turn('turn.done', item_id, 'assistant', data.get('transcript', ''))
        elif kind == 'output_audio_buffer.stopped' and data.get('response_id') == self.assistant_response:
            self.assistant = self.assistant_response = None
        elif kind == 'response.done':
            await self._response_done(data['response'])

    async def _response_done(self, response):
        response_id = response['id']
        if response_id in self.completed_responses:
            return
        self.completed_responses.append(response_id)
        caller, is_manual = self.response_inputs.pop(response_id, (None, False))
        if not self.response_inputs:
            self.response_idle.set()
        if response.get('status') == 'failed':
            details = response.get('status_details') or {}
            self._fail(RealtimeProviderError(provider_error(detail=details.get('error')).reason))
        if response.get('status') != 'completed':
            return
        for item in response.get('output', []):
            call_id = item.get('call_id')
            if (item.get('type') != 'function_call' or item.get('name') != HERMES_TOOL['name']
                    or not isinstance(call_id, str) or not call_id
                    or item.get('status') not in (None, 'completed')):
                continue
            try:
                arguments = json.loads(item.get('arguments', ''))
            except (TypeError, ValueError):
                arguments = {}
            proposal = arguments.get('request') if isinstance(arguments, dict) else None
            item_id = self._id(call_id)
            self.delegations[item_id] = call_id
            if is_manual or not caller or caller != self.caller or not isinstance(proposal, str) or not proposal.strip():
                self.call.spawn(self._send_handoff(item_id, 'Tidak ada tindakan tambahan.', 'commentary'))
                continue
            await self.call.on_data({'type': 'delegation.created', 'item': {
                'id': item_id, 'target': 'client', 'user_bidi_turn_id': self._id(caller),
                'content': [{'text': proposal}],
            }})

    async def close(self):
        self.closed = True
        self.stop_input()
        for pending in set(self.pending.values()):
            pending.cancel()
        if self.peer is not None:
            await self.peer.close()
