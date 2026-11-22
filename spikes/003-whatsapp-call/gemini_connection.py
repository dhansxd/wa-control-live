"""Gemini Live audio transport for the shared WhatsApp LiveCall."""

import asyncio
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
from uuid import uuid4

from aiohttp import ClientError, ClientSession, WSMsgType, WSServerHandshakeError


ENDPOINT = (
    'wss://generativelanguage.googleapis.com/ws/'
    'google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent'
)
DEFAULT_MODEL = 'gemini-3.8-live'
TOOL = 'ask_hermes'
PCM_IN = 'audio/pcm;rate=16000'
PCM_OUT = 'audio/pcm;rate=24000'
SETUP_TIMEOUT = 12


class GeminiProviderError(ConnectionError):
    def __init__(self, reason, retry_after=None):
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f'Gemini Live: {reason}')


def retry_delay(value):
    if not isinstance(value, str):
        return None
    if value.isascii() and value.isdecimal():
        return int(value)
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return max(0, (date - datetime.now(timezone.utc)).total_seconds())


def provider_error(status, *, detail=None, retry_after=None):
    detail = detail if isinstance(detail, dict) else {}
    status_name = detail.get('status')
    entries = detail.get('details')
    details = [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []
    reasons = {item['reason'] for item in details if isinstance(item.get('reason'), str)}
    for item in details:
        if item.get('@type') == 'type.googleapis.com/google.rpc.RetryInfo':
            delay = item.get('retryDelay')
            if isinstance(delay, str) and delay.endswith('s'):
                try:
                    seconds = float(delay[:-1])
                except ValueError:
                    continue
                if math.isfinite(seconds) and seconds >= 0:
                    retry_after = max(retry_after or 0, seconds)
    if reasons & {'BILLING_DISABLED', 'BILLING_NOT_ACTIVE', 'BILLING_NOT_ENABLED'}:
        reason = 'billing_required'
    elif reasons & {'API_KEY_INVALID', 'API_KEY_EXPIRED'} or status == 401 or status_name == 'UNAUTHENTICATED':
        reason = 'auth_required'
    elif status == 403 or status_name == 'PERMISSION_DENIED':
        reason = 'access_denied'
    elif status == 429 or status_name == 'RESOURCE_EXHAUSTED':
        reason = ('quota_exhausted' if any(
            item.get('@type') == 'type.googleapis.com/google.rpc.QuotaFailure' for item in details
        ) and retry_after is None else 'rate_limited')
    elif status in (400, 404) or status_name in ('INVALID_ARGUMENT', 'NOT_FOUND', 'UNIMPLEMENTED'):
        reason = 'unsupported'
    elif status in (408, 500, 502, 503, 504) or status_name in ('UNAVAILABLE', 'DEADLINE_EXCEEDED'):
        reason = 'unavailable'
    else:
        reason = 'unknown'
    return GeminiProviderError(reason, retry_after)


def content(text, role='user'):
    return {'role': role, 'parts': [{'text': text}]}


class GeminiConnection:
    """Translate Gemini's wire protocol to LiveCall events without running tasks."""

    route = 'gemini-live'

    def __init__(self, call, *, model=None, voice=None, api_key):
        self.call = call
        self.model = (model or DEFAULT_MODEL).removeprefix('models/')
        self.scheduled_functions = self.model == DEFAULT_MODEL
        self.voice = voice
        self.api_key = api_key
        self.session = self.ws = None
        self.ready = self.closed = self.input_stopped = False
        self.input = asyncio.Queue(maxsize=25)
        self.writers = set()
        self.prefix = uuid4().hex
        self.sequence = 0
        self.user_turn = self.assistant_turn = None
        self.user_text, self.assistant_text = [], []
        self.user_finished = self.user_waiting_final = False
        self.assistant_interrupted = False
        self.tools, self.pending_tools = {}, {}
        self.responded_tools, self.cancelled_tools = set(), set()

    @property
    def can_speak(self):
        return self.ready and not self.closed

    async def prepare(self):
        if not self.api_key:
            raise GeminiProviderError('auth_required')
        self.session = ClientSession()
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                self.ws = await self.session.ws_connect(
                    ENDPOINT, headers={'x-goog-api-key': self.api_key}, heartbeat=20,
                )
        except WSServerHandshakeError as exc:
            raise provider_error(exc.status, retry_after=retry_delay(exc.headers.get('Retry-After'))) from None
        except (ClientError, OSError):
            raise GeminiProviderError('unavailable') from None

    async def start(self, prompt, initial_items):
        history = [content(item['text'], 'model' if item['role'] == 'assistant' else 'user')
                   for item in initial_items if item['role'] != 'developer' and item['text']]
        generation = {'responseModalities': ['AUDIO']}
        if self.voice:
            generation['speechConfig'] = {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': self.voice}}}
        tool = {
            'name': TOOL,
            'description': 'Minta Hermes menangani permintaan lisan pemilik yang memerlukan tindakan, '
                           'pekerjaan aktif, atau riwayat yang belum tersedia.',
            'parameters': {'type': 'OBJECT', 'properties': {'request': {'type': 'STRING'}},
                           'required': ['request']},
        }
        if self.scheduled_functions:
            tool['behavior'] = 'NON_BLOCKING'
        setup = {
            'model': f'models/{self.model}', 'generationConfig': generation,
            'systemInstruction': {'parts': [{'text': prompt}]},
            'tools': [{'functionDeclarations': [tool]}],
            'inputAudioTranscription': {}, 'outputAudioTranscription': {},
            'realtimeInputConfig': {'activityHandling': 'START_OF_ACTIVITY_INTERRUPTS'},
            'contextWindowCompression': {'slidingWindow': {}},
        }
        if history:
            setup['historyConfig'] = {'initialHistoryInClientContent': True}
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                await self._send({'setup': setup})
                while 'setupComplete' not in await self._receive():
                    pass
                if history:
                    await self._send({'clientContent': {'turns': history, 'turnComplete': True}})
                    self.call.report['initial_context_delivery'] = 'sent_unconfirmed'
        except TimeoutError:
            raise GeminiProviderError('unavailable') from None
        self.ready = True
        self.call.mark('voice_session_started')
        self._spawn(self._read())
        self._spawn(self._write_audio())

    async def _send(self, value):
        if self.closed or self.ws is None:
            raise GeminiProviderError('unavailable')
        try:
            await self.ws.send_json(value)
        except (ClientError, OSError, RuntimeError):
            raise GeminiProviderError('unavailable') from None

    async def _receive(self):
        try:
            frame = await self.ws.receive()
        except (ClientError, OSError):
            raise GeminiProviderError('unavailable') from None
        if frame.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
            # Close text and HTTP exception reprs may contain credentials.
            raise GeminiProviderError('unavailable' if frame.type == WSMsgType.ERROR else 'unknown')
        try:
            event = json.loads(frame.data)
        except (ValueError, TypeError):
            raise GeminiProviderError('unknown') from None
        if not isinstance(event, dict):
            raise GeminiProviderError('unknown')
        if isinstance(error := event.get('error'), dict):
            raise provider_error(error.get('code'), detail=error)
        return event

    def _spawn(self, coroutine):
        task = self.call.spawn(coroutine)
        if task is not None:
            self.writers.add(task)
            task.add_done_callback(self.writers.discard)

    def _fail(self, error):
        if not self.closed:
            self.ready = False
            self.call.report['provider_failure'] = {'reason': error.reason, 'retry_after': error.retry_after}
            self.call.fail(str(error))

    async def _read(self):
        try:
            while not self.closed:
                await self._handle(await self._receive())
        except GeminiProviderError as exc:
            self._fail(exc)

    async def _write_audio(self):
        try:
            while not self.input_stopped:
                pcm = await self.input.get()
                await self._send({'realtimeInput': {'audio': {
                    'mimeType': PCM_IN, 'data': base64.b64encode(pcm).decode('ascii'),
                }}})
        except GeminiProviderError as exc:
            self._fail(exc)

    def feed(self, pcm):
        if not self.ready or self.input_stopped:
            return
        if self.input.full():
            self.call.report['dropped_input_bytes'] = (
                self.call.report.get('dropped_input_bytes', 0) + len(self.input.get_nowait()))
        self.input.put_nowait(pcm)

    async def append_context(self, text):
        await self._send({'clientContent': {'turns': [content(text)], 'turnComplete': False}})
        return False

    async def speak(self, text):
        instruction = ('[Pembaruan lisan dari aplikasi] Sampaikan dengan wajar dalam percakapan. '
                       'Gunakan data berikut sebagai bahan jawaban.\n')
        await self._send({'realtimeInput': {'text': instruction + json.dumps(text, ensure_ascii=False)}})

    def send_handoff(self, item_id, text, channel):
        if item_id in self.cancelled_tools or not self.can_speak:
            raise GeminiProviderError('unavailable')
        self._spawn(self._send_handoff(item_id, text, channel))

    async def _send_handoff(self, item_id, text, channel):
        try:
            if item_id in self.cancelled_tools:
                return
            if item_id in self.tools and item_id not in self.responded_tools:
                await self._tool_response(item_id, text, channel)
            elif channel == 'speakable':
                await self.speak(text)
            else:
                await self.append_context(text)
        except GeminiProviderError as exc:
            self._fail(exc)
            return
        await self.call.on_data({'type': 'delegation.context.sent', 'delegation_item_id': item_id})

    async def _tool_response(self, item_id, text, channel):
        self.responded_tools.add(item_id)
        response = {'id': self.tools[item_id], 'name': TOOL, 'response': {'result': text}}
        if self.scheduled_functions:
            response['scheduling'] = 'WHEN_IDLE' if channel == 'speakable' else 'SILENT'
        await self._send({'toolResponse': {'functionResponses': [response]}})

    def _new_turn(self, role):
        self.sequence += 1
        return f'gemini-{self.prefix}-{role}-{self.sequence}'

    async def _turn(self, kind, role, turn_id, transcript=''):
        await self.call.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': transcript}})

    async def _start_user_turn(self):
        if self.user_turn is None:
            self.user_turn = self._new_turn('user')
            await self._turn('turn.created', 'user', self.user_turn)

    async def _finish_user_turn(self):
        text = ''.join(self.user_text).strip()
        if self.user_turn and text and not self.user_finished:
            self.user_finished = True
            await self._turn('turn.done', 'user', self.user_turn, text)

    async def _dispatch_tools(self):
        # Model-supplied arguments are a routing hint. An observed caller
        # transcript is required before handing off to the shared workflow.
        if not self.pending_tools or not self.user_text:
            return
        await self._finish_user_turn()
        for item_id, proposal in tuple(self.pending_tools.items()):
            self.pending_tools.pop(item_id)
            await self.call.on_data({'type': 'delegation.created', 'item': {
                'id': item_id, 'target': 'client', 'user_bidi_turn_id': self.user_turn,
                'content': [{'text': proposal}],
            }})

    async def _reject_pending_tools(self):
        for item_id in tuple(self.pending_tools):
            self.pending_tools.pop(item_id)
            await self._tool_response(
                item_id, 'Permintaan lisan pemilik belum terkonfirmasi.', 'commentary',
            )

    async def _handle(self, event):
        if 'goAway' in event:
            raise GeminiProviderError('unavailable')
        for raw_id in event.get('toolCallCancellation', {}).get('ids', []):
            item_id = f'gemini-{self.prefix}-tool-{raw_id}'
            self.pending_tools.pop(item_id, None)
            self.cancelled_tools.add(item_id)
        for tool in event.get('toolCall', {}).get('functionCalls', []):
            raw_id, name = tool.get('id'), tool.get('name')
            if not isinstance(raw_id, str) or not raw_id or name != TOOL:
                continue
            item_id = f'gemini-{self.prefix}-tool-{raw_id}'
            if item_id in self.tools or item_id in self.cancelled_tools:
                continue
            self.tools[item_id] = raw_id
            args = tool.get('args')
            proposal = args.get('request', '') if isinstance(args, dict) else ''
            self.pending_tools[item_id] = proposal if isinstance(proposal, str) else ''
        if server := event.get('serverContent'):
            await self._production(server)
        await self._dispatch_tools()

    async def _production(self, server):
        if server.get('interrupted'):
            self.assistant_interrupted = True
            self.assistant_text.clear()
            await self.call.on_data({'type': 'turn.interrupted', 'turn_id': self.assistant_turn})
            if self.assistant_turn is not None:
                await self._finish_user_turn()
                self.cancelled_tools.update(self.pending_tools)
                self.pending_tools.clear()
                self._clear_user()
        if server.get('interimInputTranscription'):
            if self.user_waiting_final:
                await self._reject_pending_tools()
                self._clear_user()
            await self._start_user_turn()
        if transcript := server.get('inputTranscription', {}).get('text'):
            await self._start_user_turn()
            self.user_text.append(transcript)
            if self.user_waiting_final:
                await self._finish_user_turn()
                await self._dispatch_tools()
                self._clear_user()
        if not self.assistant_interrupted:
            parts = server.get('modelTurn', {}).get('parts', [])
            transcript = server.get('outputTranscription', {}).get('text')
            if transcript or parts:
                if self.assistant_turn is None:
                    self.assistant_turn = self._new_turn('assistant')
                    await self._turn('turn.created', 'assistant', self.assistant_turn)
                if transcript:
                    self.assistant_text.append(transcript)
                for part in parts:
                    blob = part.get('inlineData') or {}
                    if blob.get('mimeType') in (PCM_OUT, 'audio/pcm'):
                        pcm = base64.b64decode(blob.get('data') or '', validate=True)
                        if pcm and len(pcm) % 2 == 0:
                            await self.call.output_audio(pcm)
        if server.get('turnComplete'):
            interrupted = self.assistant_interrupted
            if not interrupted:
                await self._finish_user_turn()
                await self._dispatch_tools()
                if self.assistant_turn:
                    await self._turn('turn.done', 'assistant', self.assistant_turn,
                                     ''.join(self.assistant_text).strip())
            self.assistant_turn = None
            self.assistant_text.clear()
            self.assistant_interrupted = False
            if not interrupted:
                if self.user_turn and not self.user_finished:
                    self.user_waiting_final = True
                else:
                    await self._reject_pending_tools()
                    self._clear_user()

    def _clear_user(self):
        self.user_turn = None
        self.user_text.clear()
        self.user_finished = self.user_waiting_final = False

    def stop_input(self):
        self.input_stopped = True

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.ready = False
        tasks = tuple(self.writers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.ws is not None:
                await self.ws.close()
        finally:
            if self.session is not None:
                await self.session.close()
