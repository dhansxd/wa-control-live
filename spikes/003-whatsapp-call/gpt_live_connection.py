"""OpenAI GPT-Live primary WebSocket for the shared WhatsApp LiveCall."""

import asyncio
import base64
from collections import deque
from dataclasses import dataclass, field
import json
import re
from uuid import uuid4

import av
from aiohttp import ClientError, ClientSession, TraceConfig, WSMsgType, WSServerHandshakeError

from call_adapter import ContextUpdateUnconfirmed, PCM_SILENCE_PEAK, RESULT_ACK_TIMEOUT, resample_pcm
from gemini_connection import retry_delay


ENDPOINT = 'wss://api.openai.com/v1/live/sessions'
DEFAULT_MODEL = 'gpt-live-1'
SETUP_TIMEOUT = 12
TURN_GAP_MS = 650
TRANSCRIPT_SETTLE = 0.55
DELEGATION_WAIT = 2
DELEGATION_LOOKBACK_MS = 2000
APPEND_BYTES = 480  # Leave room under the API's 500-token limit, including non-Latin text.


class GPTLiveProviderError(ConnectionError):
    def __init__(self, reason, retry_after=None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f'GPT-Live: {reason}')


async def _reject_redirect(_session, _context, params):
    # A gateway key is scoped to the exact configured endpoint.
    params.response.close()
    raise GPTLiveProviderError('unsupported')


def provider_error(status=None, *, detail=None, retry_after=None):
    detail = detail if isinstance(detail, dict) else {}
    code, category = detail.get('code'), detail.get('type')
    if code in ('billing_hard_limit_reached', 'billing_not_active'):
        reason = 'billing_required'
    elif code == 'insufficient_quota':
        reason = 'quota_exhausted'
    elif status == 401 or code in ('invalid_api_key', 'invalid_authorization'):
        reason = 'auth_required'
    elif status == 403 or code == 'permission_denied':
        reason = 'access_denied'
    elif status == 429 or code == 'rate_limit_exceeded' or category == 'rate_limit_error':
        reason = 'rate_limited'
    elif status in (400, 404) or category == 'invalid_request_error' or code == 'model_not_found':
        reason = 'unsupported'
    elif status in (408, 500, 502, 503, 504) or category == 'server_error':
        reason = 'unavailable'
    else:
        reason = 'unknown'
    return GPTLiveProviderError(reason, retry_after)


def _chunks(text):
    remaining = text.strip().encode('utf-8')
    while remaining:
        chunk = remaining[:APPEND_BYTES].decode('utf-8', 'ignore')
        if len(remaining) > APPEND_BYTES:
            # Desktop appends whole sentences, falling back to a split only when
            # a single sentence exceeds the provider's wire allowance.
            endings = list(re.finditer(r'[.!?](?=\s|$)', chunk))
            if endings:
                chunk = chunk[:endings[-1].end()]
            elif (space := chunk.rfind(' ')) > len(chunk) // 2:
                chunk = chunk[:space + 1]
        yield chunk
        remaining = remaining[len(chunk.encode('utf-8')):]


@dataclass
class _Turn:
    id: str
    start_ms: int
    end_ms: int
    deadline: float
    parts: list[str] = field(default_factory=list)


class GPTLiveConnection:
    """Translate provider events without creating sessions or work in Hermes."""

    route = 'gpt-live'

    def __init__(self, call, *, model=None, voice=None, api_key, endpoint=None):
        self.call = call
        self.model, self.voice, self.api_key = model or DEFAULT_MODEL, voice, api_key
        self.endpoint = endpoint
        self.session = self.ws = None
        self.started = self.ready = self.closed = self.input_stopped = False
        self.input = asyncio.Queue(maxsize=25)
        self.resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
        self.tasks = set()
        self.acks = {}
        self.prefix = uuid4().hex
        self.turns = {'user': None, 'assistant': None}
        self.finished_users = deque(maxlen=8)
        self.transcript_events = deque(maxlen=256)
        self.last_transcript_end = {'user': -1, 'assistant': -1}
        self.audible_output_until = 0
        self.suppress_audio = False
        self.delegations, self.pending_delegations = {}, {}

    @property
    def can_speak(self):
        return self.ready and not self.closed

    async def prepare(self):
        if not self.api_key:
            raise GPTLiveProviderError('auth_required')
        traces = []
        if self.endpoint is not None:
            trace = TraceConfig()
            trace.on_request_redirect.append(_reject_redirect)
            traces.append(trace)
        self.session = ClientSession(trace_configs=traces)
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                self.ws = await self.session.ws_connect(
                    self.endpoint or ENDPOINT, headers={'Authorization': f'Bearer {self.api_key}'}, heartbeat=20,
                )
        except GPTLiveProviderError:
            raise
        except WSServerHandshakeError as exc:
            raise provider_error(exc.status, retry_after=retry_delay(exc.headers.get('Retry-After'))) from None
        except (ClientError, OSError):
            raise GPTLiveProviderError('unavailable') from None

    async def start(self, prompt, initial_items):
        audio = {'format': {'type': 'audio/pcm', 'rate': 24000}}
        if self.voice:
            audio['output'] = {'voice': self.voice}
        history = [
            {'type': 'message', 'role': item['role'], 'content': [{
                'type': 'output_text' if item['role'] == 'assistant' else 'input_text', 'text': item['text'],
            }]}
            for item in initial_items if item['role'] in ('user', 'assistant', 'developer') and item['text']
        ]
        config = {'model': self.model, 'instructions': prompt, 'audio': audio, 'delegation': {'type': 'client'}}
        if history:
            config['input'] = history
        event_id = uuid4().hex
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                await self._send({'type': 'session.start', 'event_id': event_id, 'session': config})
                while True:
                    event = await self._receive()
                    if event['type'] == 'error':
                        raise provider_error(detail=event.get('error'))
                    if event['type'] == 'session.closed':
                        raise GPTLiveProviderError('unavailable')
                    if event['type'] == 'session.started':
                        session = event.get('session')
                        if (not isinstance(session, dict) or not isinstance(session.get('id'), str)
                                or not session['id'] or event.get('client_event_id', event_id) != event_id):
                            raise GPTLiveProviderError('unknown')
                        break
        except TimeoutError:
            raise GPTLiveProviderError('unavailable') from None
        self.started = self.ready = True
        self.call.mark('voice_session_started')
        self._spawn(self._read())
        self._spawn(self._write_audio())

    async def _send(self, event):
        if self.closed or self.ws is None:
            raise GPTLiveProviderError('unavailable')
        try:
            await self.ws.send_json(event)
        except (ClientError, OSError, RuntimeError):
            raise GPTLiveProviderError('unavailable') from None

    async def _receive(self, timeout=None):
        try:
            frame = await self.ws.receive(timeout=timeout)
        except TimeoutError:
            raise
        except (ClientError, OSError):
            raise GPTLiveProviderError('unavailable') from None
        if frame.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
            raise GPTLiveProviderError('unavailable')
        try:
            event = json.loads(frame.data)
        except (ValueError, TypeError):
            raise GPTLiveProviderError('unknown') from None
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise GPTLiveProviderError('unknown')
        return event

    def _spawn(self, coroutine):
        task = self.call.spawn(coroutine)
        if task is not None:
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def _fail(self, error):
        if not self.closed and self.ready:
            self.ready = False
            self.stop_input()
            for _, future in self.acks.values():
                if not future.done():
                    future.set_exception(error)
            self.call.report['provider_failure'] = {'reason': error.reason, 'retry_after': error.retry_after}
            self.call.fail(str(error))

    async def _read(self):
        clock = asyncio.get_running_loop().time
        try:
            while not self.closed:
                deadline = await self._settle(clock())
                try:
                    event = await self._receive(max(0.001, deadline - clock()) if deadline is not None else None)
                except TimeoutError:
                    continue
                await self._handle(event, clock())
        except GPTLiveProviderError as exc:
            self._fail(exc)

    async def _write_audio(self):
        try:
            while not self.input_stopped:
                pcm = await self.input.get()
                frame = av.AudioFrame(format='s16', layout='mono', samples=len(pcm) // 2)
                frame.sample_rate = 16000  # WhatsApp sidecar input rate.
                frame.planes[0].update(pcm)
                frames = b''.join(resample_pcm(frame, self.resampler))
                if frames:
                    await self._send({'type': 'session.input_audio.append',
                                      'audio': base64.b64encode(frames).decode('ascii')})
        except GPTLiveProviderError as exc:
            self._fail(exc)

    def feed(self, pcm):
        if not self.ready or self.input_stopped or not pcm or len(pcm) % 2:
            return
        if self.input.full():
            self.call.report['dropped_input_bytes'] = (
                self.call.report.get('dropped_input_bytes', 0) + len(self.input.get_nowait()))
        self.input.put_nowait(pcm)

    async def _append(self, kind, text, delegation_id=None):
        if not self.can_speak:
            raise GPTLiveProviderError('unavailable')
        writes = {}
        loop = asyncio.get_running_loop()
        try:
            for chunk in _chunks(text):
                event_id = uuid4().hex
                future = loop.create_future()
                writes[event_id] = future
                self.acks[event_id] = (kind + '.appended', future)
                await self._send({'type': kind + '.append', 'event_id': event_id,
                                  'delegation_id': delegation_id, 'content': chunk})
            await asyncio.wait_for(asyncio.gather(*writes.values()), RESULT_ACK_TIMEOUT)
        finally:
            for event_id, future in writes.items():
                self.acks.pop(event_id, None)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    async def append_context(self, text):
        try:
            await self._append('session.thinking', text)
        except TimeoutError:
            raise ContextUpdateUnconfirmed('GPT-Live did not confirm the context append') from None
        return True

    async def speak(self, text):
        await self._append('session.commentary', text)

    def send_handoff(self, item_id, text, channel):
        if not self.can_speak:
            raise GPTLiveProviderError('unavailable')
        self._spawn(self._send_handoff(item_id, text, channel))

    async def _send_handoff(self, item_id, text, channel):
        kind = 'session.commentary' if channel == 'speakable' else 'session.thinking'
        try:
            await self._append(kind, text, self.delegations.get(item_id))
        except (GPTLiveProviderError, TimeoutError):
            # The shared call times out missing delivery receipts and can use its DM path.
            return
        await self.call.on_data({'type': 'delegation.context.appended', 'delegation_item_id': item_id})

    async def _handle(self, event, now):
        kind = event['type']
        if kind == 'error':
            detail = event.get('error') or {}
            error = provider_error(detail=detail)
            client_id = detail.get('client_event_id') if isinstance(detail, dict) else None
            ack = self.acks.get(client_id or event.get('client_event_id'))
            if ack is not None and error.reason in ('unsupported', 'unknown'):
                if not ack[1].done():
                    ack[1].set_exception(error)
                return
            raise error
        if kind == 'session.closed':
            self.started = False
            reason = event.get('reason')
            if reason in ('close_requested', 'expired', 'content', 'remote_hangup', 'connection_lost'):
                self.call.report['provider_close_reason'] = reason
            raise GPTLiveProviderError('unavailable')
        if kind in ('session.thinking.appended', 'session.commentary.appended'):
            ack = self.acks.get(event.get('client_event_id'))
            if ack is not None and kind == ack[0] and not ack[1].done():
                ack[1].set_result(None)
        elif kind == 'session.output_audio.delta':
            try:
                pcm = base64.b64decode(event.get('delta'), validate=True)
            except (TypeError, ValueError):
                raise GPTLiveProviderError('unknown') from None
            if not pcm or len(pcm) % 2:
                raise GPTLiveProviderError('unknown')
            if not self.suppress_audio:
                if max(map(abs, memoryview(pcm).cast('h')), default=0) > PCM_SILENCE_PEAK:
                    self.audible_output_until = now + TRANSCRIPT_SETTLE
                    if turn := self.turns['assistant']:
                        turn.deadline = self.audible_output_until
                await self.call.output_audio(pcm)
        elif kind == 'session.input_transcript.delta':
            await self._transcript('user', event, now)
        elif kind == 'session.output_transcript.delta':
            await self._transcript('assistant', event, now)
        elif kind == 'session.delegation.created':
            self._queue_delegation(event, now)

    async def _transcript(self, role, event, now):
        start, end, text = event.get('start_ms'), event.get('end_ms'), event.get('delta')
        if (type(start) is not int or type(end) is not int or start < 0 or end <= start
                or not isinstance(text, str) or not text):
            return
        # Caller audio is appended without a client event ID. An echoed app command
        # cannot establish an authenticated user utterance for a delegation.
        if role == 'user' and event.get('client_event_id'):
            return
        event_id = event.get('event_id')
        if isinstance(event_id, str):
            if event_id in self.transcript_events:
                return
            self.transcript_events.append(event_id)
        turn = self.turns[role]
        if end <= (turn.start_ms if turn else self.last_transcript_end[role]):
            return
        # The provider supplies timestamps, not completed turns. Group ordered fragments locally.
        if turn and start - turn.end_ms >= TURN_GAP_MS:
            await self._finish_turn(role)
            turn = None
        if role == 'assistant' and self.suppress_audio:
            if start < self.last_transcript_end['user']:
                return
            self.suppress_audio = False
        if turn is None:
            playing = self.turns['assistant']
            # Audio chunks have no turn ID and may lead their transcript.
            overlaps_playback = start >= playing.start_ms if playing else now < self.audible_output_until
            if role == 'user' and overlaps_playback:
                self.turns['assistant'] = None
                self.audible_output_until = 0
                self.suppress_audio = True
                await self.call.on_data({'type': 'turn.interrupted', 'turn_id': playing.id if playing else None})
            turn = self.turns[role] = _Turn(f'{self.prefix}-{role}-{start}', start, end, now)
            await self.call.on_data({'type': 'turn.created', 'turn': {'role': role, 'id': turn.id}})
        turn.parts.append(text)
        turn.end_ms, turn.deadline = max(turn.end_ms, end), now + TRANSCRIPT_SETTLE
        self.last_transcript_end[role] = turn.end_ms

    async def _finish_turn(self, role):
        turn, self.turns[role] = self.turns[role], None
        if role == 'user':
            self.finished_users.append(turn)
        await self.call.on_data({'type': 'turn.done', 'turn': {
            'role': role, 'id': turn.id, 'transcript': ''.join(turn.parts).strip(),
        }})

    def _queue_delegation(self, event, now):
        delegation = event.get('delegation')
        offset = event.get('offset_ms')
        if (not isinstance(delegation, dict) or delegation.get('target') != 'client'
                or type(offset) is not int or offset < 0):
            return
        raw_id = delegation.get('id')
        if not isinstance(raw_id, str) or not raw_id:
            return
        item_id = f'{self.prefix}-delegate-{raw_id}'
        if item_id not in self.delegations:
            self.delegations[item_id] = raw_id
            self.pending_delegations[item_id] = offset, now + TRANSCRIPT_SETTLE, now + DELEGATION_WAIT

    async def _settle(self, now):
        deadlines = []
        for role, turn in self.turns.items():
            if turn is not None:
                if turn.deadline <= now:
                    await self._finish_turn(role)
                else:
                    deadlines.append(turn.deadline)
        for item_id, (offset, due, expires) in tuple(self.pending_delegations.items()):
            if due > now:
                deadlines.append(due)
                continue
            active = self.turns['user']
            if active and active.start_ms <= offset and now < expires:
                deadlines.append(min(active.deadline, expires))
                continue
            turn = next((turn for turn in reversed(self.finished_users)
                         if turn.start_ms <= offset <= turn.end_ms + DELEGATION_LOOKBACK_MS), None)
            if turn is None or (active is not None and active.start_ms <= offset):
                if now < expires:
                    deadlines.append(expires)
                    continue
                self._spawn(self._send_handoff(item_id, 'Permintaan lisan belum terkonfirmasi.', 'commentary'))
            else:
                await self.call.on_data({'type': 'delegation.created', 'item': {
                    'id': item_id, 'target': 'client', 'user_bidi_turn_id': turn.id, 'content': [],
                }})
            self.pending_delegations.pop(item_id)
        return min(deadlines, default=None)

    def stop_input(self):
        self.input_stopped = True

    def buffered_transcripts(self):
        """Return transcript text already received before the call ended."""
        return tuple({'type': 'turn.done', 'turn': {
            'role': role, 'id': turn.id, 'transcript': ''.join(turn.parts).strip(),
        }} for role, turn in self.turns.items() if turn is not None and turn.parts)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.ready = False
        self.stop_input()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.ws is not None:
                if self.started and not self.ws.closed:
                    try:
                        async with asyncio.timeout(1):
                            await self.ws.send_json({'type': 'session.close', 'event_id': uuid4().hex})
                            while (await self._receive())['type'] != 'session.closed':
                                pass
                    except (ClientError, OSError, RuntimeError):
                        self.call.report['provider_close_unconfirmed'] = True
                await self.ws.close()
        finally:
            if self.session is not None:
                await self.session.close()
