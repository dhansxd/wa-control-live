"""Local wire adapter between WhatsApp calls and Live audio providers.

The native sidecar owns WhatsApp auth/signaling. This process routes Live audio
and delegates tasks to the installed Hermes plugin.
"""
import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
import hmac
import json
import os
import re
import sys
import time
import uuid
import wave
from fractions import Fraction
from functools import partial
from itertools import chain
from pathlib import Path

import av
from aiohttp import ClientError, ClientSession, WSMsgType, web
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.exceptions import InvalidStateError
from aiortc.mediastreams import MediaStreamError

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / '001-codex-audio'))
sys.path.insert(0, str(BASE.parent))
from probe import AppServer, codex_command, event_summary, safe_text, start_params
from provider_failure import build_provider_failure, encode_notice_pcm


FALLBACK_PERSONA = 'Kamu adalah Hermes Agent.'
CALL_PROMPT = (
    'Lanjutkan percakapan DM WhatsApp dengan pemilik akun lewat telepon ini. '
    'Gunakan identitas dan cara bicaramu yang sama seperti di DM. '
    'Pesan dan transkrip dari DM tetap konteks chat, termasuk pesan suara yang dikirim ke DM. '
    'Entri riwayat bertanda whatsapp_call berasal dari panggilan sebelumnya. '
    'Ikuti bahasa dan alur percakapan pemilik akun dengan wajar. '
    'Saat berbicara, tanggapi dengan hangat dan ekspresif sesuai suasana obrolan. '
    'Mulai dengan jawaban atau tindakan yang diminta. '
    'Lewati pembuka yang hanya menyatakan bahwa kamu paham atau mengulang ucapan pemilik. '
    'Saat menerima permintaan kerja yang jelas, sebut rencanamu dalam satu kalimat singkat. '
    'Sebut tindakan dan tujuan yang diminta secara konkret, tanpa sekadar berjanji akan memberi kabar. '
    'Misalnya, saat diminta mencari sesuatu dan mengirimnya ke WhatsApp, '
    'kamu bisa berkata dengan gayamu sendiri, seperti "Aku cari dulu, lalu aku kirim ke WhatsApp." '
    'Ceritakan apa yang sedang kamu kerjakan dengan sudut pandang orang pertama, '
    'sesuai status yang sudah terkonfirmasi. Sebut pekerjaan selesai atau hasil terkirim hanya jika terbukti. '
    'Pakai cara bicaramu sendiri. Rangkai informasi progres dan hasil menjadi jawaban lisan yang alami, '
    'tanpa membacakan label teknis, data mentah, atau menjelaskan pembagian kerja internal. '
    'Gunakan konteks percakapan bersama, termasuk DM, saat relevan. Biarkan pemilik akun menentukan topik pembuka. '
    'Jawab langsung dari konteks dan hasil yang sudah tersedia. Jika detail lama belum tersedia, '
    'ambil dari riwayat percakapan ini. '
    'Pakai progres dan hasil dari pekerjaan DM yang sama, termasuk saat membahas lampiran. '
    'Tetap tanggapi obrolan dan pertanyaan baru secara langsung selama pekerjaan berjalan. '
    'Jika pemilik bertanya tentang pekerjaan itu, ceritakan langkah yang sungguh sedang kamu kerjakan. '
    'Ajukan delegasi hanya untuk tindakan atau informasi yang memerlukan akses ke sesi WhatsApp atau alat. '
    'Jika pemilik akun memilih agar hasil pekerjaan tetap di DM, teruskan pilihan itu ke client. '
    'Minta persetujuan atau penjelasan hanya jika pekerjaan memang membutuhkannya. '
    'Pekerjaan yang masih berjalan tidak membutuhkan konfirmasi dari pemilik. '
    'Kalau ia kemudian minta mendengar hasilnya, ambil hasil pekerjaan yang sama. '
    'Kalau pemilik akun bertanya apa pekerjaan masih berjalan atau sedang sampai mana sekarang, '
    'minta status terkini lewat client. Sebelum ada status itu, jangan menebak pekerjaan sedang '
    'berjalan dari permintaan, ucapan, atau data progres lama. '
    'Status akhir atau pembaruan bahwa data sudah kedaluwarsa mengalahkan progres sebelumnya. '
    'Jika ditanya apakah hasil sudah dikirim, minta status pengiriman terkonfirmasi lewat client. '
    'Pembaruan sementara bisa berubah. Gunakan hasil akhir jika sudah tersedia. '
    'Saat hasil terkonfirmasi menyusul pembaruan, lanjutkan dari bagian pekerjaan yang sudah kamu sampaikan. '
    'Sebutkan bagian baru dan luruskan perubahan. Jika pemilik akun minta ulang, sampaikan kembali. '
    'Teruskan perubahan arahan untuk pekerjaan yang sedang berjalan, dan mulai tugas hanya '
    'untuk permintaan baru yang membutuhkan alat atau pemeriksaan. '
    'Setelah selesai menjawab, beri ruang pemilik untuk bicara. Jangan ulangi jawaban atau '
    'laporkan perubahan status rutin tanpa diminta. Sampaikan hasil baru atau kendala yang perlu ditangani. '
    'Sampaikan hasil sesuai yang tersedia; status pekerjaan selesai tidak mengonfirmasi pesan terkirim. '
    'Pesan DM yang ditambahkan selama panggilan adalah konteks; jangan membacakannya tanpa diminta.'
)
WORK_STATUS_CONTEXT = {
    'pending': 'Permintaan diterima. Status terbaru belum tersedia.',
    'queued': 'Pekerjaan menunggu giliran.',
    'started': 'Pekerjaan masih berjalan.',
    'approval_required': 'Pekerjaan menunggu persetujuan pemilik akun di chat.',
    'clarification_required': 'Pekerjaan menunggu penjelasan tambahan dari pemilik akun di chat.',
    'cancelled': 'Pekerjaan dibatalkan.',
    'interrupted': 'Pekerjaan terhenti sebelum selesai.',
    'failed': 'Pekerjaan berakhir tanpa hasil yang bisa dibacakan.',
}
WORK_OPEN_STATUSES = {'pending', 'queued', 'started', 'approval_required', 'clarification_required'}
WORK_UNCONFIRMED_CONTEXT = (
    'Status terbaru pekerjaan ini belum bisa dipastikan. Data progres sebelumnya tidak mengonfirmasi status sekarang.'
)
VOICE_TASK_LAUNCH_CONTEXT = (
    'Tanggapi permintaan terakhir sekarang dengan satu kalimat pendek dalam bahasamu sendiri. '
    'Sebut apa yang akan kamu lakukan secara konkret, dengan sudut pandang orang pertama. '
    'Nyatakan itu sebagai rencana. Gunakan status terkonfirmasi untuk menyatakan pekerjaan sudah dimulai atau selesai. '
    'Setelah itu, beri ruang pemilik untuk meneruskan obrolan.'
)
STEER_ACCEPTED_CONTEXT = 'Arahan baru masuk antrean untuk pekerjaan yang sama.'
CALL_END_PROMPT = (
    'Memutus sambungan panggilan WhatsApp ini adalah tindakan yang perlu kamu delegasikan ke client. '
    'Jika pemilik akun jelas ingin mengakhiri panggilan yang sedang berlangsung, gunakan cara yang sama '
    'seperti saat mendelegasikan pekerjaan lain, dengan maksud lengkap dari ucapannya. '
    'Setelah mendapat konfirmasi, ucapkan salam penutup singkat dengan kata-katamu sendiri.'
)
AGENT_CONTEXT_TIMEOUT = 8  # Context must fit inside the existing call startup timeout.
AGENT_INTENT_TIMEOUT = 18  # Includes Hermes CLI startup; the quiet voice acknowledgment is already sent.
AGENT_ROUTE_TIMEOUT = 24  # Includes the active-session lookup and the normal single intent decision.
VOICE_HISTORY_RETRY_INTERVAL = 10
VOICE_HISTORY_RETRYABLE = {'busy', 'unavailable', 'unknown'}
AGENT_HISTORY_ITEM_BYTES = 800
STARTUP_HISTORY_BYTES = 24000  # Realtime V3 accepts 8192 estimated input tokens.
AGENT_OUTPUT_LIMIT = 3500
RESULT_ACK_TIMEOUT = 10
RESULT_SPEECH_QUIET_SECONDS = 1
PCM_SILENCE_PEAK = 16  # 16-bit PCM; covers the idle noise floor of the realtime track.
# The sidecar primes two 60 ms frames and hands off through an eight-frame channel.
# Use this estimate only to avoid muting a fresh response after a quiet gap.
PLAYOUT_MARGIN_SECONDS = 0.6
CHAT_UPDATE_CHUNK_BYTES = 500
RECENT_TOOL_STEPS = 12
VOICE_HISTORY_FLUSH_TIMEOUT = 1.5
DIAGNOSTIC_AUDIO_SECONDS = 180
POST_ACK_TRANSCRIPT_WAIT = 3
LIVE_SETUP_TIMEOUT = 14
RECOVERY_NOTICE_DELAY = 1.5


def append_transcript_fragment(text, fragment):
    fragment = fragment.strip()
    if not text or not fragment:
        return text or fragment
    # Codex emits words and punctuation as separate chunks without spaces.
    separator = '' if fragment[0] in '.,!?;:%)]}…。，？！、' or text[-1] in '([{' else ' '
    return text + separator + fragment


def completed_transcript(assembled, final):
    final = final.strip()
    # A short terminal fragment can confirm the tail of this same turn. If
    # it differs, the provider's terminal transcript can be a correction.
    return assembled if final and assembled.endswith(' ' + final) else final


def quote_chat(history, *, limit):
    quoted = []
    for item in history[-limit:]:
        if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant'):
            continue
        text = item.get('text')
        if isinstance(text, str):
            encoded = safe_text(text).encode('utf-8', 'replace')
            text = encoded[:AGENT_HISTORY_ITEM_BYTES].decode('utf-8', 'ignore')
            if text:
                turn = {'role': item['role'], 'text': text}
                if item.get('source') == 'whatsapp_call':
                    turn['source'] = 'whatsapp_call'
                quoted.append(turn)
    return quoted


def startup_chat_item(history, prompt, *, seeded_refs=None):
    label = 'Riwayat percakapan WhatsApp sebelum panggilan (kutipan JSON):\n'
    remaining = STARTUP_HISTORY_BYTES - len(prompt.encode('utf-8')) - len(label.encode('utf-8')) - 2
    recent, summary = [], None
    for item in history:
        if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant'):
            continue
        text = item.get('text')
        if not isinstance(text, str) or not text.strip():
            continue
        if item.get('summary'):
            summary = {'role': 'assistant', 'text': text.strip()[:2000], 'summary': True}
        else:
            recent.append(item)
    packed = []
    if summary:
        size = len(json.dumps(summary, ensure_ascii=False).encode('utf-8')) + 2
        if size <= remaining:
            remaining -= size
        else:
            summary = None
    for item in reversed(recent):
        quoted = {'role': item['role'], 'text': item['text'].strip()}
        if item.get('source') == 'whatsapp_call':
            quoted['source'] = 'whatsapp_call'
        size = len(json.dumps(quoted, ensure_ascii=False).encode('utf-8')) + 2
        if size > remaining:
            if remaining >= 256:
                # Each UTF-8 byte expands to at most six bytes in JSON.
                tail_bytes = (remaining - 80) // 6
                quoted['text'] = '…' + quoted['text'].encode('utf-8')[-tail_bytes:].decode('utf-8', 'ignore')
                packed.append(quoted)
            break
        packed.append(quoted)
        remaining -= size
        if seeded_refs is not None:
            seeded_refs.update(item.get('_message_refs', ()))
    if summary:
        packed.append(summary)
    if not packed:
        return None
    return {'role': 'assistant', 'text': label + json.dumps(packed[::-1], ensure_ascii=False)}


def chat_update_chunks(history):
    for item in history:
        if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant'):
            continue
        text = item.get('text')
        if not isinstance(text, str):
            continue
        speaker = 'Pemilik' if item['role'] == 'user' else 'Asisten'
        refs = item.get('_message_refs', ())
        for chunk in context_chunks(text, f'Pesan di DM saat telepon. {speaker}: ',
                                    f'Lanjutan pesan DM. {speaker}: '):
            yield refs, chunk


def dm_delivery_context(result):
    state = result.get('text_delivery')
    if state == 'sent':
        if result.get('has_attachments'):
            return 'Teks hasil sudah dikirim ke DM. Pengiriman lampiran belum terkonfirmasi.'
        return 'Teks hasil sudah dikirim ke DM.'
    if state == 'pending':
        return 'Pengiriman teks hasil ke DM masih dalam proses.'
    return 'Pengiriman hasil di DM belum terkonfirmasi.'


def context_chunks(text, prefix, continuation):
    remaining = text.strip().encode('utf-8')
    if len(remaining) > STARTUP_HISTORY_BYTES:
        tail = remaining[-(STARTUP_HISTORY_BYTES - 3):].decode('utf-8', 'ignore')
        remaining = ('…' + tail).encode('utf-8')
    while remaining:
        size = CHAT_UPDATE_CHUNK_BYTES - len(prefix.encode('utf-8'))
        chunk = remaining[:size].decode('utf-8', 'ignore')
        yield prefix + chunk
        remaining = remaining[len(chunk.encode('utf-8')):]
        prefix = continuation


async def read_call_direction(token):
    # The sidecar owns the call slot before opening the voice connection.
    headers = {'Authorization': f'Bearer {token}'}
    base = 'http://127.0.0.1:8796'
    try:
        async with asyncio.timeout(1), ClientSession(headers=headers, raise_for_status=True) as session:
            async with session.get(f'{base}/health') as response:
                health = await response.json()
            call_id = health.get('active_call') if isinstance(health, dict) else None
            if not isinstance(call_id, str) or not re.fullmatch(r'[0-9a-f]{12}', call_id):
                return None
            async with session.get(f'{base}/calls/{call_id}') as response:
                call = await response.json()
    except (ClientError, TimeoutError, ValueError):
        return None
    if isinstance(call, dict) and call.get('id') == call_id and call.get('ended_at') is None:
        direction = call.get('direction')
        if direction in ('inbound', 'outbound'):
            return direction
    return None


class AgentJobs:
    """Run the local Hermes plugin worker in the serving profile."""

    def __init__(self, hermes_home):
        home = hermes_home.resolve()
        self.profile = home.name if home.parent.name == 'profiles' else 'default'
        self.env = dict(os.environ, HERMES_HOME=str(home),
                        HERMES_GATEWAY_PROFILE_PIN='1')
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', '__PYVENV_LAUNCHER__',
                    'HERMES_SESSION_ID', 'HERMES_TUI_GATEWAY_URL'):
            self.env.pop(key, None)

    async def prepare_context(self):
        return await self._request({'action': 'context'}, AGENT_CONTEXT_TIMEOUT)

    async def mint_realtime_secret(self, provider, account):
        return await self._request({'action': 'realtime_secret', 'provider': provider, 'account': account},
                                   AGENT_CONTEXT_TIMEOUT)

    async def recall(self, session_id, terms):
        request = {'action': 'recall', 'session_id': session_id, 'terms': terms}
        return await self._request(request, AGENT_CONTEXT_TIMEOUT)

    @asynccontextmanager
    async def watch_context(self, session_id, cursor):
        request = {'action': 'watch_context', 'session_id': session_id, 'after_row_id': cursor}
        async with self._worker(limit=1024 * 1024) as worker:
            worker.stdin.write(json.dumps(request, ensure_ascii=False).encode())
            await worker.stdin.drain()
            worker.stdin.close()

            async def updates():
                while line := await worker.stdout.readline():
                    yield json.loads(line)

            yield updates()

    async def observe_work(self, session_id, admission_id):
        return await self._request({'action': 'observe_work', 'session_id': session_id,
                                    'admission_id': admission_id, 'include_progress': True}, AGENT_CONTEXT_TIMEOUT)

    async def steer_work(self, session_id, admission_id, text):
        return await self._request({'action': 'steer_work', 'session_id': session_id,
                                    'admission_id': admission_id, 'text': text},
                                   AGENT_CONTEXT_TIMEOUT)

    async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
        request = {'action': 'save_exchange', 'session_id': session_id, **exchange}
        if wait_for_lease:
            request['wait_for_lease'] = True
        return await self._request(request, None if wait_for_lease else AGENT_CONTEXT_TIMEOUT)

    async def route(self, proposal, current, previous, loaded_context, *, session_id):
        request = {'action': 'route', 'proposal': proposal, 'current': current,
                   'previous': previous, 'loaded_context': loaded_context, 'session_id': session_id}
        return await self._request(request, AGENT_ROUTE_TIMEOUT)

    async def submit(self, submission_id, text, *, session_id, direction=None, voice_context=None,
                     attachment_admission_id=None):
        request = {'submission_id': submission_id, 'text': text, 'session_id': session_id, 'wait_for_result': False}
        if direction is not None:
            request['direction'] = direction
        if voice_context:
            request['voice_context'] = voice_context
        if attachment_admission_id is not None:
            request['attachment_admission_id'] = attachment_admission_id
        return await self._request(request, AGENT_INTENT_TIMEOUT)

    @asynccontextmanager
    async def _worker(self, *, limit=65536):
        worker = await asyncio.create_subprocess_exec(
            'hermes', '--profile', self.profile, 'wa-control-live', 'agent-job',
            '--expected-home', self.env['HERMES_HOME'], env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=limit,
        )
        try:
            yield worker
        finally:
            if worker.returncode is None:
                try:
                    worker.kill()
                except ProcessLookupError:
                    pass
            await worker.stdout.read()
            await worker.wait()

    async def _request(self, request, timeout):
        try:
            async with self._worker() as worker:
                reply, _ = await asyncio.wait_for(
                    worker.communicate(json.dumps(request, ensure_ascii=False).encode()),
                    timeout,
                )
                result = json.loads(reply)
                if not worker.returncode and isinstance(result, dict):
                    return result
        except (OSError, TimeoutError, ValueError):
            pass
        return {'status': 'unavailable' if request.get('action') == 'observe_work' else 'unknown'}


class CallerTrack(AudioStreamTrack):
    """Stateful 16→24-kHz conversion with 20-ms pacing and a bounded live buffer."""
    def __init__(self):
        super().__init__()
        self.resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
        self.buffer = bytearray()
        self.input_pts = self.pts = self.dropped_bytes = 0
        self.skipped_initial_silence_bytes = 0
        self.deadline = None
        self.first_nonzero_sent_at = None

    def feed(self, pcm):
        if not pcm or len(pcm) % 2 or len(pcm) > 32000:
            raise ValueError('Expected <=1s even-length PCM16 mono')
        # recv() already emits paced silence while empty. Do not queue an
        # extra burst of startup zeros in front of the first caller speech.
        if not self.input_pts and not any(pcm):
            self.skipped_initial_silence_bytes += len(pcm)
            return
        frame = av.AudioFrame(format='s16', layout='mono', samples=len(pcm)//2)
        frame.sample_rate, frame.time_base, frame.pts = 16000, Fraction(1, 16000), self.input_pts
        self.input_pts += frame.samples
        frame.planes[0].update(pcm)
        for part in self.resampler.resample(frame):
            self.buffer.extend(bytes(part.planes[0])[:part.samples * 2])
        excess = max(0, len(self.buffer) - 48000)
        self.dropped_bytes += excess
        del self.buffer[:excess]

    async def recv(self):
        if self.readyState != 'live':
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        if self.deadline is None:
            self.deadline = loop.time()
        await asyncio.sleep(max(0, self.deadline - loop.time()))
        if self.readyState != 'live':
            raise MediaStreamError
        self.deadline = max(self.deadline + .020, loop.time())
        pcm = bytes(self.buffer[:960])
        del self.buffer[:960]
        if self.first_nonzero_sent_at is None and any(pcm):
            self.first_nonzero_sent_at = loop.time()
        frame = av.AudioFrame(format='s16', layout='mono', samples=480)
        frame.sample_rate, frame.time_base, frame.pts = 24000, Fraction(1, 24000), self.pts
        self.pts += 480
        frame.planes[0].update(pcm.ljust(960, b'\x00'))
        return frame

    def stop(self):
        self.buffer.clear()
        super().stop()


def resample_pcm(frame, resampler):
    return (bytes(part.planes[0])[:part.samples * 2] for part in resampler.resample(frame))


def audio_envelope(pcm):
    return {'serverContent': {'modelTurn': {'parts': [{'inlineData': {
        'mimeType': 'audio/pcm;rate=24000',
        'data': base64.b64encode(pcm).decode(),
    }}]}}}


def record_diagnostic_pcm(recording, pcm, written):
    limit = DIAGNOSTIC_AUDIO_SECONDS * recording.getframerate() * recording.getsampwidth()
    if written < limit:
        recording.writeframesraw(pcm[:limit - written])


class CodexLoginRequired(RuntimeError):
    """The configured Codex route requires a ChatGPT login."""


class ContextUpdateUnconfirmed(TimeoutError):
    """The audio provider did not confirm a context update."""


class ProvidersUnavailable(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def spawn_owned(coro, tasks, fail):
    async def guarded():
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fail(f'{type(exc).__name__}: {exc}')
    task = asyncio.create_task(guarded())
    task.add_done_callback(lambda _task: coro.close())
    tasks.append(task)
    return task


async def ready_or_fail(owner, awaitable, timeout):
    task = asyncio.ensure_future(awaitable)
    failure = asyncio.create_task(owner.failed.wait())
    try:
        done, _ = await asyncio.wait((task, failure), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if owner.failed.is_set():
            raise RuntimeError(owner.report['error'])
        if task not in done:
            raise TimeoutError('Live audio startup timed out')
        return task.result()
    finally:
        for pending in (task, failure):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, failure, return_exceptions=True)


class AudioConnection:
    """Own provider callbacks and tasks for one connection within a call."""

    def __init__(self, call, factory, *, candidate=None):
        self.call = call
        self.path, self.began = call.path, call.began
        self.candidate = candidate
        self.report = {'cleanup_errors': [], 'timings_seconds': {}}
        self.failed = asyncio.Event()
        self.tasks = []
        self.retired = False
        self.superseded = False
        self.link = factory(self)

    @property
    def active(self):
        return self.call.audio is self and not self.closed

    @property
    def closed(self):
        return self.retired or self.call.closed

    @property
    def replace_voice(self):
        return self.superseded or self.call.replace_voice

    def mark(self, name):
        if not self.closed:
            self.report['timings_seconds'].setdefault(
                name, round(asyncio.get_running_loop().time() - self.began, 3),
            )

    def fail(self, message):
        if self.closed or self.failed.is_set():
            return
        self.report.setdefault('error', safe_text(message))
        self.failed.set()
        if self.active:
            self.call.connection_failed(self)

    def spawn(self, coro):
        if self.closed or self.failed.is_set():
            coro.close()
            return
        return spawn_owned(coro, self.tasks, self.fail)

    async def ready_or_fail(self, awaitable, timeout):
        return await ready_or_fail(self, awaitable, timeout)

    async def on_data(self, event):
        if self.active and not self.failed.is_set():
            await self.call.on_data(event)

    async def output_audio(self, pcm):
        if self.active:
            await self.call.output_audio(pcm)

    def retire(self, *, superseded=False):
        self.retired = True
        self.superseded = superseded
        try:
            self.link.stop_input()
        except Exception as exc:
            self.report['cleanup_errors'].append('audio input: ' + safe_text(str(exc)))
        for task in self.tasks:
            if not task.done():
                task.cancel()

    async def close(self):
        if not self.retired:
            self.retire()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await self.link.close()
        except Exception as exc:
            self.report['cleanup_errors'].append('audio connection: ' + safe_text(str(exc)))


class CodexConnection:
    """Codex app-server and WebRTC transport for a LiveCall."""

    route = 'codex-chatgpt-subscription'

    def __init__(self, call, *, voice=None):
        self.call = call
        self.voice = voice or 'juniper'
        self.server = AppServer(codex_command())
        # The public SFU accepted host/peer-reflexive connectivity in the
        # controlled probe. Avoid the default external STUN gather's 5s wait.
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.track = CallerTrack()
        self.peer.addTrack(self.track)
        self.channel = self.peer.createDataChannel('oai-events')
        self.connected = asyncio.Event()
        self.session_started = asyncio.Event()
        self.chat_update_ack = asyncio.Event()
        self.chat_channel_open = asyncio.Event()
        self.answer = asyncio.get_running_loop().create_future()
        self.thread_id = None
        self.started = False
        self.channel.on('message', self._data)
        self.channel.on('open', self.chat_channel_open.set)
        self.peer.on('connectionstatechange', self._state)
        self.peer.on('track', self._track)

    @property
    def can_speak(self):
        return self.thread_id is not None

    @property
    def report(self):
        return self.call.report

    def feed(self, pcm):
        self.track.feed(pcm)

    def stop_input(self):
        self.track.stop()

    def _data(self, raw):
        if self.call.closed or self.call.failed.is_set():
            return
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            self.call.fail('Malformed Codex datachannel event')
            return
        if not isinstance(event, dict):
            self.call.fail('Malformed Codex datachannel event')
            return
        self.call.spawn(self.on_data(event))

    async def on_data(self, event):
        if self.call.closed or self.call.failed.is_set():
            return
        kind = event.get('type')
        if kind == 'session.started':
            self.call.mark('voice_session_started')
            self.session_started.set()
        elif kind == 'session.context.appended':
            self.chat_update_ack.set()
        elif kind == 'error':
            self.call.fail(str(event.get('error', event)))
            return
        await self.call.on_data(event)

    async def _state(self):
        if self.call.closed:
            return
        state = self.peer.connectionState
        if state == 'connected':
            self.connected.set()
        elif state == 'failed':
            self.call.fail('Codex WebRTC failed')

    def _track(self, remote):
        if remote.kind == 'audio':
            self.call.spawn(self.read_audio(remote))

    async def read_audio(self, remote):
        resampler = av.AudioResampler(format='s16', layout='mono', rate=24000)
        try:
            while not self.call.closed:
                for pcm in resample_pcm(await remote.recv(), resampler):
                    await self.call.output_audio(pcm)
        except MediaStreamError:
            if not self.call.closed:
                self.call.fail('Codex remote audio ended before caller hang-up')

    async def append_context(self, text):
        if self.channel.readyState == 'connecting':
            try:
                await asyncio.wait_for(self.chat_channel_open.wait(), RESULT_ACK_TIMEOUT)
            except TimeoutError:
                raise ConnectionError('Sambungan konteks suara WhatsApp tidak terbuka') from None
        if self.channel.readyState != 'open':
            raise ConnectionError('Sambungan konteks suara WhatsApp tertutup')
        self.chat_update_ack.clear()
        self.channel.send(json.dumps({'type': 'session.context.append', 'channel': 'commentary',
                                      'content': [{'type': 'input_text', 'text': text}]}, ensure_ascii=False))
        try:
            await asyncio.wait_for(self.chat_update_ack.wait(), RESULT_ACK_TIMEOUT)
        except TimeoutError:
            raise ContextUpdateUnconfirmed('Pembaruan konteks suara WhatsApp tidak dikonfirmasi') from None

    def send_handoff(self, item_id, text, channel):
        try:
            self.channel.send(json.dumps({
                'type': 'delegation.context.append', 'delegation_item_id': item_id,
                'channel': channel, 'content': [{'type': 'input_text', 'text': text}],
            }, ensure_ascii=False))
        except InvalidStateError as exc:
            raise ConnectionError(str(exc)) from exc

    async def speak(self, text):
        await self.server.call('thread/realtime/appendSpeech', {
            'threadId': self.thread_id, 'text': text,
        }, timeout=RESULT_ACK_TIMEOUT)

    async def on_server(self, msg):
        method, params = msg.get('method'), msg.get('params') or {}
        if method == 'thread/realtime/sdp' and not self.answer.done():
            self.answer.set_result(params['sdp'])
        elif method in {'thread/realtime/error', 'thread/realtime/closed'}:
            self.call.fail(params.get('message') or params.get('reason') or method)
        elif method == 'error':
            if params.get('threadId') == self.thread_id and params.get('turnId'):
                self.report.setdefault('backing_agent_errors', []).append(event_summary(params))
            else:
                self.call.fail(str(params))
        elif method in {'item/started', 'item/completed'}:
            if (params.get('item') or {}).get('type') in {'commandExecution','fileChange','mcpToolCall','dynamicToolCall'}:
                self.call.fail('Unexpected backing Codex task tool; Hermes owns task execution')

    async def read_events(self):
        reader = getattr(self.server, 'reader_task', None)
        while True:
            event = asyncio.create_task(self.server.events.get())
            try:
                if reader is not None:
                    done, _ = await asyncio.wait((event, reader), return_when=asyncio.FIRST_COMPLETED)
                    if reader in done:
                        error = None if reader.cancelled() else reader.exception()
                        detail = safe_text(f'{type(error).__name__}: {error}') if error else 'closed'
                        raise RuntimeError('App-server stdout ' + detail)
                await self.on_server(await event)
            finally:
                if not event.done():
                    event.cancel()
                await asyncio.gather(event, return_exceptions=True)

    async def read_startup_quota(self):
        try:
            quota = await self.server.call('account/rateLimits/read')
        except (OSError, RuntimeError) as exc:
            self.report['quota_read_startup_error'] = safe_text(str(exc))
        else:
            self.report['rate_limits_startup'] = quota
            self.call.mark('startup_quota_confirmed')

    async def prepare(self):
        async def gather_offer():
            await self.peer.setLocalDescription(await self.peer.createOffer())
            self.call.mark('ice_gathered')
        offer = asyncio.create_task(gather_offer())
        self.call.tasks.append(offer)
        await self.server.start()
        self.call.mark('app_server_started')
        self.report['app_server_pid'] = self.server.process.pid
        await self.server.call('initialize', {'clientInfo':{'name':'wa-control-live','version':'0.1'},
                                              'capabilities':{'experimentalApi':True}})
        self.call.mark('app_server_initialized')
        await self.server.send({'method':'initialized'})
        account = (await self.server.call('account/read', {'refreshToken':False})).get('account') or {}
        if account.get('type') != 'chatgpt':
            raise CodexLoginRequired('Subscription login required for Codex Realtime')
        self.call.mark('subscription_account_validated')
        # The quota snapshot is diagnostic. Realtime setup confirms whether audio can start.
        self.call.spawn(self.read_startup_quota())
        thread = await self.server.call('thread/start', {'cwd':str(self.call.path.resolve()), 'ephemeral':True,
                    'modelProvider':'openai','approvalPolicy':'never','sandbox':'read-only',
                    'baseInstructions':'Do not run tools for client-managed requests.'})
        self.call.mark('thread_created')
        self.thread_id = thread['thread']['id']
        self.call.spawn(self.read_events())
        await offer

    async def start(self, prompt, initial_items):
        params = start_params(self.thread_id, self.peer.localDescription.sdp, self.voice)
        params['prompt'] = prompt
        params['initialItems'] = initial_items
        params['realtimeStartInstructions'] = 'Do not run tools. Reply only skip to backing delegations.'
        params['realtimeEndInstructions'] = ''
        self.started = True
        try:
            await self.server.call('thread/realtime/start', params)
        except RuntimeError:
            raise RuntimeError('Codex could not start the WhatsApp voice session') from None
        self.call.mark('realtime_start_accepted')
        sdp = await self.call.ready_or_fail(self.answer, 20)
        self.call.mark('remote_sdp_received')
        await self.peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type='answer'))
        await self.call.ready_or_fail(self.connected.wait(), 20)
        self.call.mark('peer_connected')
        await self.call.ready_or_fail(self.session_started.wait(), 20)

    async def close(self):
        try:
            stats = await asyncio.wait_for(self.peer.getStats(), 2)
            fields = ('type', 'kind', 'ssrc', 'packetsReceived', 'packetsLost', 'jitter',
                      'roundTripTime', 'fractionLost', 'packetsSent', 'bytesSent',
                      'bytesReceived', 'dtlsState')
            self.report['rtp_stats'] = [
                {key: getattr(item, key) for key in fields if hasattr(item, key)}
                for item in stats.values()
                if item.type in {'inbound-rtp', 'outbound-rtp', 'remote-inbound-rtp', 'transport'}
            ]
            self.report['rtp_stats_jitter_units'] = 'RTP timestamp units; remote fractionLost is scaled by 256'
        except Exception as exc:
            self.report['rtp_stats_error'] = safe_text(str(exc))
        if self.started and not self.call.replace_voice:
            try:
                self.report['rate_limits_after'] = await self.server.call('account/rateLimits/read', timeout=5)
            except Exception as exc:
                self.report['quota_read_error'] = safe_text(str(exc))
        if self.started:
            try:
                await self.server.call('thread/realtime/stop', {'threadId':self.thread_id},
                                       timeout=2 if self.call.replace_voice else 8)
                self.report['realtime_stop_acknowledged'] = True
            except Exception as exc:
                self.report['cleanup_errors'].append('realtime stop: '+safe_text(str(exc)))
        for name, cleanup in [('peer', self.peer.close), ('app-server', self.server.close)]:
            try:
                await asyncio.wait_for(cleanup(), 12)
                self.report[name+'_closed'] = True
            except Exception as exc:
                self.report['cleanup_errors'].append(name+': '+safe_text(str(exc)))
        self.report['dropped_input_bytes'] = self.track.dropped_bytes
        self.report['skipped_initial_silence_bytes'] = self.track.skipped_initial_silence_bytes
        if self.track.first_nonzero_sent_at is not None:
            self.report['timings_seconds']['first_caller_nonzero_pcm_sent'] = round(self.track.first_nonzero_sent_at-self.call.began, 3)


class LiveCall:
    """The shared WhatsApp conversation, independent of its audio connection."""

    def __init__(self, path, send, *, agent_jobs=None, direction_reader=None,
                 connection_factory=CodexConnection, routing=None, connector_for=None):
        self.began = asyncio.get_running_loop().time()
        self.path, self.send = Path(path), send
        self.agent_jobs = agent_jobs
        self.direction_reader = direction_reader
        self.call_direction = None
        self.hermes_session_id = None
        self.path.mkdir(parents=True, exist_ok=True)
        self.report = {'scope': 'WhatsApp-call adapter; Hermes profile chat',
                       'events': [], 'cleanup_errors': [],
                       'input_bytes': 0, 'output_bytes': 0,
                       'backend_completed_delegations': [], 'acknowledged_delegations': [],
                       'suppressed_output_bytes': 0,
                       'result_audio_forwarded_bytes': 0}
        self.report['created_at_unix'] = time.time()
        self.report['timings_seconds'] = {}
        self.failed = asyncio.Event()
        self.chat_context_ready = asyncio.Event()
        self.media_started = asyncio.Event()
        self.chat_reconnecting = False
        self.closed = False
        self.replace_voice = False
        self.previous_voice_results = None
        self.tasks, self.delegations = [], set()
        self.pending_ack = {}
        self.pending_deliveries = {}
        self.user_turns, self.delegated_turns = {}, set()
        self.user_turn_at_ms = None
        self.transcripts_done = {}
        self.reply_transcripts = {}
        self.finished_replies = set()
        self.deferred_handoffs = {}
        self.routing_turns = set()
        self.route_ready = asyncio.Event()
        self.loaded_context = ''
        self.dm_work = []
        self.work_progress = {}
        self.work_followers = {}
        self.shared_work_turns = set()
        self.offered_work_results = set()
        self.pending_work_speech = {}
        self.dm_only_results = set()
        self.result_preference_turns = {}
        self.call_work = set()
        self.result_speech_lock = asyncio.Lock()
        self.pending_work_preview = None
        self.work_preview_cursor = None
        self.work_preview_task = None
        self.last_speech_activity = self.began
        self.active_reply_turn = None
        self.latest_reply_turn = None
        self.playout_expected_until = 0.0
        self.result_speech_start_deadline = None
        self.work_preview_start = None
        self.ambiguous_preview_turn = None
        self.interrupted_replies = set()
        self.interrupted_output_until = 0.0
        self.last_spoken_exchange = ''
        self.current_spoken_turn = None
        self.spoken_turn_final = False
        self.assistant_reply_turn = None
        self.spoken_turn_audio_started = False
        self.spoken_reply = {}
        self.pending_spoken_replies = {}
        self.exchange_queued = False
        self.native_task_turns = set()
        self.completed_exchanges = deque()
        self.exchange_writer = None
        self.exchange_lock = asyncio.Lock()
        self.result_acknowledged = False
        self.output_lock = asyncio.Lock()
        self.context_lock = asyncio.Lock()
        self.switch_lock = asyncio.Lock()
        self.routing, self.connector_for = routing, connector_for
        self.routing_failure = None
        self.switch_updates = None
        self.seeded_message_refs = set()
        self.retiring_audio = []
        self.audio = None
        self.pending_end_call = None
        self.end_call_sent = None
        self.farewell_audio_sent = self.farewell_transcribed = False
        if routing is None:
            self.install_audio(AudioConnection(self, connection_factory))
        self.recording = wave.open(str(self.path/'reply.wav'), 'wb')
        self.recording.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        self.input_recording = wave.open(str(self.path/'caller.wav'), 'wb')
        self.input_recording.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))

    def mark(self, name):
        if not self.closed:
            self.report['timings_seconds'].setdefault(name, round(asyncio.get_running_loop().time()-self.began, 3))

    def spawn(self, coro):
        if self.closed or self.failed.is_set():
            coro.close()
            return
        return spawn_owned(coro, self.tasks, self.fail)

    @property
    def connection(self):
        return self.audio.link if self.audio is not None else None

    def install_audio(self, audio):
        self.audio = audio
        self.report['timings_seconds'].update(audio.report.pop('timings_seconds'))
        self.report['cleanup_errors'].extend(audio.report.pop('cleanup_errors'))
        self.report.update(audio.report)
        self.report['auth_route'] = audio.link.route
        audio.report = self.report

    def connection_failed(self, audio):
        if audio.report.get('provider_close_reason') == 'content':
            self.routing_failure = 'provider_content_closed'
            self.fail(audio.report['error'])
            return
        if self.routing is not None and self.report.get('connected'):
            self.spawn(self.recover_audio(audio))
        else:
            self.fail(audio.report['error'])

    async def record_audio_failure(self, audio, error=None, candidate=None):
        candidate = candidate or audio.candidate
        failure = audio.report.get('provider_failure', {}) if audio is not None else {}
        reason = getattr(error, 'reason', None) or failure.get('reason')
        if isinstance(error, CodexLoginRequired):
            reason = 'auth_required'
        retry_after = getattr(error, 'retry_after', None)
        await asyncio.to_thread(
            self.routing.record_failure, candidate, reason or 'unavailable',
            retry_after=retry_after if retry_after is not None else failure.get('retry_after'),
        )

    def retire_audio(self, audio):
        if audio.retired:
            return
        audio.retire(superseded=True)
        self.retiring_audio = [task for task in self.retiring_audio if not task.done()]
        task = asyncio.create_task(audio.close())
        self.retiring_audio.append(task)
        return task

    async def open_routed_audio(self, prompt, items, selection=None, *, exclude=(), only=None):
        tried = set(exclude)
        while candidate := await asyncio.to_thread(self.routing.next_candidate, selection, exclude=tried, only=only):
            tried.add(candidate.key)
            audio = None
            try:
                factory = self.connector_for(candidate, self.routing.secret(candidate))
                audio = AudioConnection(self, factory, candidate=candidate)
                await audio.link.prepare()
                await audio.link.start(prompt, items)
                if audio.failed.is_set():
                    raise RuntimeError('Audio connection failed during setup')
                await asyncio.to_thread(self.routing.record_ready, candidate)
                if audio.failed.is_set():
                    raise RuntimeError('Audio connection failed during setup')
                return audio
            except asyncio.CancelledError:
                if audio is not None:
                    self.retire_audio(audio)
                raise
            except (OSError, RuntimeError, ValueError) as error:
                if audio is not None:
                    self.retire_audio(audio)
                await self.record_audio_failure(audio, error, candidate)
        status = await asyncio.to_thread(self.routing.status, selection)
        raise ProvidersUnavailable(status['reason'] or 'all_unavailable')

    async def announce_audio_recovery(self, failed_audio):
        # The adapter can prepare or lose a provider while the phone is still ringing.
        await self.media_started.wait()
        await asyncio.sleep(RECOVERY_NOTICE_DELAY)
        try:
            async with asyncio.timeout(1), self.output_lock:
                if (self.closed or self.failed.is_set() or self.audio is not failed_audio
                        or self.pending_end_call or self.end_call_sent):
                    return
                if pcm := encode_notice_pcm('live-recovering.pcm', max_seconds=2):
                    await self.send({'recoveryAudio': pcm})
        except (OSError, RuntimeError):
            # The progress notice is best effort while recovery owns the call.
            return

    async def recover_audio(self, failed_audio):
        async with self.switch_lock:
            if self.audio is not failed_audio or self.closed or self.failed.is_set():
                return
            await self.record_audio_failure(failed_audio)
            candidate = failed_audio.candidate
            notice = asyncio.create_task(self.announce_audio_recovery(failed_audio))
            failure_reason = None
            try:
                async with asyncio.timeout(LIVE_SETUP_TIMEOUT) as budget:
                    try:
                        await self.replace_audio(exclude={candidate.key})
                    except ProvidersUnavailable:
                        if failed_audio.report.get('provider_close_reason') in {'close_requested', 'remote_hangup'}:
                            raise
                        delay = await asyncio.to_thread(self.routing.reconnect_delay, candidate)
                        loop = asyncio.get_running_loop()
                        if delay is None or delay >= budget.when() - loop.time():
                            raise
                        retry_at = loop.time() + delay
                        if retirement := self.retire_audio(failed_audio):
                            await asyncio.shield(retirement)
                        await asyncio.sleep(max(0, retry_at - loop.time()))
                        await self.replace_audio(only=candidate)
            except (OSError, RuntimeError, ValueError) as error:
                failure_reason = getattr(error, 'reason', None) or 'all_unavailable'
            finally:
                notice.cancel()
                await asyncio.gather(notice, return_exceptions=True)
            if failure_reason is not None:
                self.routing_failure = failure_reason
                self.fail(failure_reason)

    async def use_audio(self, selection):
        async with self.switch_lock:
            if self.closed or self.failed.is_set():
                raise RuntimeError('The active Live call ended')
            current = self.audio.candidate.key
            selected = await asyncio.to_thread(self.routing.status, selection)
            if not self.audio.failed.is_set() and len(selected['accounts']) == 1:
                only = selected['accounts'][0]
                if current == (only['provider'], only['account']):
                    return self.audio.candidate
            return await self.replace_audio(selection, exclude={current})

    async def replace_audio(self, selection=None, *, exclude=(), only=None):
        old = self.audio
        candidate = None
        self.switch_updates = deque()
        try:
            async with asyncio.timeout(LIVE_SETUP_TIMEOUT):
                turn_id = self.current_spoken_turn
                if self.spoken_turn_final:
                    self.remember_switch_transcript(
                        'user', self.user_turns[turn_id], self.exchange_id(turn_id), turn_id=turn_id,
                    )
                history = []
                work = self.dm_work
                if self.agent_jobs is not None:
                    await self.flush_history()
                    resolved = await self.agent_jobs.prepare_context()
                    if resolved.get('status') != 'ready' or resolved.get('session_id') != self.hermes_session_id:
                        raise RuntimeError('The active WhatsApp session changed during audio setup')
                    history = resolved.get('history', [])
                    work = resolved.get('work', [])
                seeded_refs = set()
                items = self.connection_items(history, self.voice_prompt, '', seeded_refs=seeded_refs)
                if work:
                    items.append({'role': 'assistant', 'text': 'Status pekerjaan Hermes: ' + json.dumps([
                        {key: entry[key] for key in ('admission_id', 'status', 'text') if key in entry}
                        for entry in work if isinstance(entry, dict) and entry.get('session_id') == self.hermes_session_id
                    ], ensure_ascii=False)[:AGENT_OUTPUT_LIMIT]})
                candidate = await self.open_routed_audio(self.voice_prompt, items, selection, exclude=exclude, only=only)
                turn_id = self.current_spoken_turn
                if turn_id and not self.spoken_turn_final and not old.failed.is_set():
                    try:
                        await old.ready_or_fail(self.transcripts_done[turn_id].wait(), LIVE_SETUP_TIMEOUT)
                    except RuntimeError:
                        if not old.failed.is_set():
                            raise
                async with self.context_lock:
                    await self.replay_switch_context(candidate, seeded_refs)
                    async with self.output_lock:
                        if self.closed or self.failed.is_set() or candidate.failed.is_set():
                            raise RuntimeError('Audio changed before the replacement was ready')
                        await self.clear_playout()
                        # The old provider can finish a transcript while the sidecar send yields.
                        await self.replay_switch_context(candidate, seeded_refs)
                        if self.closed or self.failed.is_set() or candidate.failed.is_set():
                            raise RuntimeError('Audio changed before the replacement was ready')
                        self.finish_exchange(user_only=True)
                        self.interrupt_reply()
                        for acknowledgement in self.pending_deliveries.values():
                            if not acknowledgement.done():
                                acknowledgement.set_exception(ConnectionError('Audio provider changed'))
                        old.report = {'cleanup_errors': [], 'timings_seconds': {}}
                        self.report.pop('provider_failure', None)
                        self.report.pop('provider_close_reason', None)
                        self.report.pop('error', None)
                        self.install_audio(candidate)
                        self.seeded_message_refs = seeded_refs
                        self.latest_reply_turn = None
                        self.interrupted_output_until = 0.0
                        self.retire_audio(old)
                return candidate.candidate
        finally:
            self.switch_updates = None
            if candidate is not None and self.audio is not candidate:
                self.retire_audio(candidate)

    async def replay_switch_context(self, candidate, seeded_refs):
        while self.switch_updates:
            refs, text, submission_ref = self.switch_updates.popleft()
            if (submission_ref and submission_ref in seeded_refs) or (refs and seeded_refs.issuperset(refs)):
                continue
            await candidate.link.append_context(text)

    def fail(self, message):
        self.report.setdefault('error', safe_text(message))
        self.failed.set()

    def session_changed(self):
        self.report['chat_updates_status'] = 'session_changed'
        self.replace_voice = True
        self.fail('Sesi DM WhatsApp berubah saat panggilan berlangsung')

    async def follow_chat(self, cursor):
        async with self.agent_jobs.watch_context(self.hermes_session_id, cursor) as updates:
            async for result in updates:
                if result.get('status') == 'reconnecting':
                    self.chat_reconnecting = True
                    self.work_progress.clear()
                    self.pending_work_preview = None
                    self.work_preview_cursor = None
                    if not await self.append_chat_context([
                        'Status dan progres pekerjaan sebelumnya kedaluwarsa sampai sesi tersambung kembali.',
                    ]):
                        return
                    continue
                if result.get('status') == 'session_changed':
                    self.session_changed()
                    return
                if result.get('status') == 'commentary':
                    if result.get('session_id') != self.hermes_session_id or self.chat_reconnecting:
                        continue
                    admission_id = result.get('admission_id')
                    generation = result.get('generation')
                    text = result.get('text')
                    latest_generation, _ = self.work_progress.get(admission_id, (generation, {}))
                    if (type(generation) is int and latest_generation <= generation
                            and isinstance(text, str) and text.strip()
                            and any(entry['admission_id'] == admission_id and entry['status'] == 'started'
                                    for entry in self.dm_work)):
                        self.queue_work_preview(admission_id, generation, result.get('seq'), text)
                        update = json.dumps({'id_pekerjaan': admission_id, 'generation': generation, 'text': text},
                                            ensure_ascii=False)
                        if not await self.append_chat_context(context_chunks(
                            update, 'Pembaruan sementara dari pekerjaan yang sedang dibahas (data): ',
                            'Lanjutan pembaruan sementara: ',
                        )):
                            return
                    continue
                if result.get('status') in {'progress', 'progress_reset'}:
                    if result.get('session_id') != self.hermes_session_id:
                        continue
                    if result.get('status') == 'progress':
                        await self.apply_tool_progress(result)
                        if self.closed or self.failed.is_set():
                            return
                    else:
                        self.work_progress.clear()
                        self.pending_work_preview = None
                        if not await self.append_chat_context([
                            'Progres pekerjaan (data): semua status tool sebelumnya kedaluwarsa. '
                            'Gunakan hanya pembaruan progres setelah pesan ini.',
                        ]):
                            return
                    continue
                if result.get('status') != 'ready' or result.get('session_id') != self.hermes_session_id:
                    self.fail('Pembaruan sesi DM WhatsApp tidak dapat dibaca')
                    return
                cursor = await self.apply_chat_update(result, cursor)
                if self.closed or self.failed.is_set():
                    return
                if self.chat_reconnecting:
                    self.chat_reconnecting = False
                    for _, _, _, _, wake in self.work_followers.values():
                        if wake is not None:
                            wake.set()
                self.chat_context_ready.set()
        if not self.closed:
            self.fail('Sambungan ke sesi DM WhatsApp berakhir saat panggilan berlangsung')

    async def apply_chat_update(self, result, cursor):
        work_updates = self.update_dm_work(result.get('work', []))
        next_cursor, history = result.get('history_cursor'), result.get('history')
        if type(next_cursor) is int and next_cursor > cursor and isinstance(history, list):
            cursor = next_cursor
        else:
            history = ()
        texts = context_chunks(work_updates, 'Status pekerjaan di sesi WhatsApp (data):\n',
                               'Lanjutan status pekerjaan di DM: ')
        await self.append_chat_context(texts, history=history)
        return cursor

    async def append_chat_context(self, texts, *, history=()):
        async with self.context_lock:
            updates = chain(chat_update_chunks(history), (((), text) for text in texts))
            for refs, text in updates:
                if self.switch_updates is not None:
                    self.switch_updates.append((refs, text, None))
                if refs and self.seeded_message_refs.issuperset(refs):
                    continue
                if self.routing is not None and self.audio.failed.is_set():
                    continue
                try:
                    confirmed = await self.connection.append_context(text)
                except ContextUpdateUnconfirmed as exc:
                    self.report['chat_updates_status'] = 'ack_timeout'
                    self.replace_voice = True
                    self.fail(str(exc))
                    return False
                except ConnectionError as exc:
                    if self.routing is not None and self.audio.failed.is_set():
                        continue
                    self.fail(str(exc))
                    return False
                self.loaded_context = (self.loaded_context + '\n' + text)[-STARTUP_HISTORY_BYTES:]
                field = 'chat_updates_sent_unconfirmed' if confirmed is False else 'chat_updates_applied'
                self.report[field] = self.report.get(field, 0) + 1
        return True

    async def apply_tool_progress(self, update):
        admission_id = update['admission_id']
        work = next((entry for entry in self.dm_work if entry['admission_id'] == admission_id), None)
        if work is None or work['status'] != 'started':
            return
        generation, steps = self.work_progress.get(admission_id, (update['generation'], {}))
        if update['generation'] < generation:
            return
        if update['generation'] != generation:
            generation, steps = update['generation'], {}
        tool_id = update['tool_id']
        step = {'tool': update['tool_name'], 'status': update['phase']}
        if update.get('detail'):
            step['detail'] = update['detail']
        previous = steps.get(tool_id)
        if previous == step or (
                previous and previous['status'] in {'completed', 'failed'} and step['status'] == 'started'):
            return
        steps.pop(tool_id, None)
        steps[tool_id] = step
        if len(steps) > RECENT_TOOL_STEPS:
            oldest_closed = next((key for key, value in steps.items() if value['status'] != 'started'), None)
            steps.pop(oldest_closed or next(iter(steps)))
        self.work_progress[admission_id] = generation, steps
        summary = {'id_pekerjaan': admission_id, 'id_tool': tool_id, 'giliran_eksekusi': generation,
                   'pekerjaan': safe_text(work['text'])[:120], **step}
        text = json.dumps(summary, ensure_ascii=False)
        await self.append_chat_context(list(context_chunks(
            text, 'Progres pekerjaan (data): ', 'Lanjutan progres pekerjaan: ',
        )))

    def progress_for_work(self, admission_id, execution_generation):
        if self.chat_reconnecting:
            return ''
        generation, steps = self.work_progress.get(admission_id, (None, {}))
        if generation != execution_generation or not steps:
            return ''
        return self.progress_context(steps.values())

    @staticmethod
    def progress_context(steps):
        recent = sorted(steps, key=lambda step: step['status'] == 'started')[-5:]
        progress = json.dumps(recent, ensure_ascii=False)
        return 'Jelaskan progres pekerjaanmu dengan kata-katamu sendiri berdasarkan data ini.\n' + progress

    def update_dm_work(self, work):
        if not isinstance(work, list):
            return ''
        previous = {entry['admission_id']: entry['status'] for entry in self.dm_work}
        self.dm_work = [entry for entry in work if isinstance(entry, dict)
                        and entry.get('session_id') == self.hermes_session_id
                        and isinstance(entry.get('admission_id'), str) and entry['admission_id']
                        and isinstance(entry.get('status'), str)
                        and isinstance(entry.get('text'), str)]
        current = {entry['admission_id']: entry['status'] for entry in self.dm_work}
        if self.pending_work_preview and current.get(self.pending_work_preview[0]) != 'started':
            self.pending_work_preview = None
        self.work_progress = {key: value for key, value in self.work_progress.items() if current.get(key) == 'started'}
        for admission_id, (_, _, _, _, wake) in self.work_followers.items():
            if wake is not None and previous.get(admission_id) != current.get(admission_id):
                wake.set()
        changes = []
        for entry in self.dm_work:
            prior = previous.get(entry['admission_id'])
            if prior == entry['status'] or (prior is None and not entry.get('attachment')
                                           and entry['status'] not in {'queued', 'started'}):
                continue
            summary = {'id_pekerjaan': entry['admission_id'], 'status': entry['status'], 'kind': entry.get('kind'),
                       'text': safe_text(entry['text'])[:240]}
            if entry.get('attachment'):
                summary['attachment'] = True
                if entry.get('files'):
                    summary['files'] = entry['files']
            changes.append(json.dumps(summary, ensure_ascii=False))
        for admission_id, status in previous.items():
            if status in {'queued', 'started'} and admission_id not in current:
                changes.append(json.dumps({'id_pekerjaan': admission_id, 'status': 'unknown'}))
        return '\n'.join(changes)

    def suppress_model_output(self):
        if self.failed.is_set() or (self.audio is not None and self.audio.failed.is_set()):
            return True
        interrupted = self.interrupted_output_until and (
            not self.spoken_turn_final or asyncio.get_running_loop().time() < self.interrupted_output_until
        )
        # An earlier Hermes job may still be running while the caller asks a simple question.
        return (interrupted or
                self.current_spoken_turn in self.pending_ack.values() and not self.result_acknowledged)

    def interrupt_reply(self):
        if self.active_reply_turn:
            self.interrupted_replies.add(self.active_reply_turn)
            self.reply_transcripts.pop(self.active_reply_turn, None)
        self.active_reply_turn = None
        self.assistant_reply_turn = None
        self.result_speech_start_deadline = None
        self.interrupted_output_until = float('inf')

    async def clear_playout(self):
        # Called under output_lock so a PCM frame cannot overtake the clear.
        self.playout_expected_until = 0.0
        await self.send({'serverContent': {'interrupted': True}})

    def exchange_id(self, key):
        return uuid.uuid5(uuid.NAMESPACE_URL, f'wa-control-live:{self.path.name}:exchange:{key}').hex

    def submission_id(self, turn_id):
        return uuid.uuid5(uuid.NAMESPACE_URL, f'wa-control-live:{self.path.name}:{turn_id}').hex

    def remember_switch_transcript(self, role, text, exchange_id, *, turn_id=None):
        if self.switch_updates is not None:
            refs = (f'wa-control-live:{exchange_id}:{role}',)
            submission_ref = f'wa-voice-submission:{self.submission_id(turn_id)}' if turn_id is not None else None
            speaker = 'Pemilik' if role == 'user' else 'Asisten'
            self.switch_updates.extend(
                (refs, chunk, submission_ref) for chunk in context_chunks(
                    text[:12000], f'Transkrip telepon. {speaker}: ', f'Lanjutan transkrip telepon. {speaker}: ',
                )
            )

    def voice_exchange(self, turn_id, assistant_turn, user, assistant, user_at_ms, assistant_at_ms):
        key = turn_id if user else f'{turn_id}:{assistant_turn}'
        exchange = {
            '_turn_id': turn_id,
            'exchange_id': self.exchange_id(key),
            'user': user[:12000],
            'assistant': assistant[:12000],
        }
        if user and user_at_ms is not None:
            exchange['user_at_ms'] = user_at_ms
        if assistant:
            exchange['assistant_at_ms'] = assistant_at_ms
            self.remember_switch_transcript('assistant', assistant, exchange['exchange_id'])
        return exchange

    def start_exchange_writer(self):
        if not self.closed and (self.exchange_writer is None or self.exchange_writer.done()):
            self.exchange_writer = self.spawn(self.retry_exchanges())

    def finish_exchange(self, assistant_turn=None, *, user_only=False):
        turn_id = self.current_spoken_turn
        shared = turn_id in self.shared_work_turns
        if (self.hermes_session_id and turn_id and self.spoken_turn_final and (assistant_turn or shared or user_only)
                and turn_id not in self.native_task_turns and self.user_turns[turn_id].strip()):
            user = '' if self.exchange_queued else self.user_turns[turn_id].strip()
            assistant, assistant_at_ms = ('', None) if shared or not assistant_turn else self.spoken_reply[assistant_turn]
            if not (user or assistant):
                return
            self.completed_exchanges.append(self.voice_exchange(
                turn_id, assistant_turn, user, assistant, self.user_turn_at_ms, assistant_at_ms,
            ))
            self.exchange_queued = True
            self.start_exchange_writer()

    def finish_pending_spoken_reply(self, turn_id):
        pending = self.pending_spoken_replies.pop(turn_id, None)
        if pending is None:
            return
        slot, user_at_ms, replies = pending
        try:
            position = self.completed_exchanges.index(slot)
        except ValueError:
            # Its ordering window elapsed, but the original pair can still be saved.
            position = len(self.completed_exchanges)
        else:
            del self.completed_exchanges[position]
        if turn_id not in self.native_task_turns:
            user = self.user_turns[turn_id]
            for assistant_turn, (assistant, assistant_at_ms) in replies.items():
                self.completed_exchanges.insert(position, self.voice_exchange(
                    turn_id, assistant_turn, user, assistant, user_at_ms, assistant_at_ms,
                ))
                position += 1
                user = ''
        self.route_ready.set()
        self.start_exchange_writer()

    async def save_exchanges(self, *, wait_for_lease=False):
        async with self.exchange_lock:
            while self.completed_exchanges:
                exchange = self.completed_exchanges[0]
                turn_id = exchange.get('_turn_id')
                if '_transcript_deadline' in exchange:
                    if not self.closed and self.transcript_wait(exchange) > 0:
                        return
                    self.completed_exchanges.popleft()
                    continue
                if turn_id in self.routing_turns:
                    # Wait to see whether the gateway will persist this turn itself.
                    return
                if turn_id in self.native_task_turns:
                    self.completed_exchanges.popleft()
                    continue
                payload = {key: value for key, value in exchange.items() if key != '_turn_id'}
                if turn_id in self.shared_work_turns:
                    payload['assistant'] = ''
                    payload.pop('assistant_at_ms', None)
                    if not payload['user']:
                        self.completed_exchanges.popleft()
                        continue
                try:
                    wait = {'wait_for_lease': True} if wait_for_lease else {}
                    result = await self.agent_jobs.save_exchange(self.hermes_session_id, payload, **wait)
                except (OSError, TimeoutError, ValueError):
                    self.report['voice_history_status'] = 'unknown'
                    return
                status = result.get('status', 'unknown')
                self.report['voice_history_status'] = status
                if status != 'saved':
                    return
                self.completed_exchanges.popleft()
                self.report['voice_exchanges_saved'] = self.report.get('voice_exchanges_saved', 0) + 1

    @staticmethod
    def transcript_wait(exchange):
        return max(0, exchange['_transcript_deadline'] - asyncio.get_running_loop().time())

    async def retry_exchanges(self):
        # The worker waits at the transcript boundary in one process. Back off
        # only if the worker or gateway is unavailable.
        try:
            while self.completed_exchanges:
                self.route_ready.clear()
                await self.save_exchanges(wait_for_lease=True)
                if not self.completed_exchanges:
                    break
                head = self.completed_exchanges[0]
                if '_transcript_deadline' in head:
                    try:
                        await asyncio.wait_for(self.route_ready.wait(), self.transcript_wait(head))
                    except TimeoutError:
                        pass
                elif head.get('_turn_id') in self.routing_turns:
                    await self.route_ready.wait()
                elif self.report.get('voice_history_status') in VOICE_HISTORY_RETRYABLE:
                    await asyncio.sleep(VOICE_HISTORY_RETRY_INTERVAL)
                else:
                    break
        finally:
            self.report['voice_exchanges_pending'] = len(self.completed_exchanges)
            if self.closed:
                self.write_report()

    async def flush_history(self):
        try:
            async with asyncio.timeout(VOICE_HISTORY_FLUSH_TIMEOUT):
                while self.completed_exchanges:
                    self.route_ready.clear()
                    await self.save_exchanges()
                    if not self.completed_exchanges:
                        break
                    head = self.completed_exchanges[0]
                    if '_transcript_deadline' in head or head.get('_turn_id') not in self.routing_turns:
                        return 'busy'
                    await self.route_ready.wait()
        except TimeoutError:
            return 'busy'
        return 'saved'

    def finish_routing(self, turn_id):
        self.routing_turns.discard(turn_id)
        self.route_ready.set()

    def write_report(self):
        report_path = self.path/'report.json'
        temporary = self.path/'report.json.tmp'
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2))
        temporary.replace(report_path)

    async def output_audio(self, data):
        if self.closed:
            return
        record_diagnostic_pcm(self.recording, data, self.report['output_bytes'])
        self.report['output_bytes'] += len(data)
        self.mark('first_model_pcm_received')
        if any(data):
            self.mark('first_model_nonzero_pcm_received')
        async with self.output_lock:
            if self.suppress_model_output():
                self.report['suppressed_output_bytes'] += len(data)
                return
            audible = max(map(abs, memoryview(data).cast('h')), default=0) > PCM_SILENCE_PEAK
            if audible:
                self.last_speech_activity = asyncio.get_running_loop().time()
            # RTP keeps sending near-zero PCM after the farewell. Let the sidecar's
            # own playback queue drain while still forwarding any late spoken audio.
            if self.end_call_sent and not audible:
                return
            if self.closed:
                return
            if audible:
                self.playout_expected_until = max(
                    self.playout_expected_until, self.last_speech_activity + PLAYOUT_MARGIN_SECONDS,
                ) + (len(data) / (24_000 * 2))
                if self.current_spoken_turn and not self.result_acknowledged:
                    # Untagged PCM can arrive before the assistant's turn.created.
                    self.spoken_turn_audio_started = True
            await self.send(audio_envelope(data))
            if audible and self.pending_end_call and self.assistant_reply_turn:
                self.farewell_audio_sent = True
                await self.end_after_farewell()
            self.mark('first_model_pcm_forwarded')
            if any(data):
                self.mark('first_model_nonzero_pcm_forwarded')
            if self.result_acknowledged:
                self.report['result_audio_forwarded_bytes'] += len(data)

    def feed(self, pcm):
        if self.closed or self.audio is None:
            return
        if pcm:
            self.media_started.set()
        if not self.audio.failed.is_set():
            self.connection.feed(pcm)
        record_diagnostic_pcm(self.input_recording, pcm, self.report['input_bytes'])
        self.report['input_bytes'] += len(pcm)
        self.mark('first_caller_pcm_received')
        if any(pcm):
            self.mark('first_caller_nonzero_pcm_received')

    def cancel_end_call(self):
        self.farewell_audio_sent = self.farewell_transcribed = False
        if self.pending_end_call:
            item_id, _ = self.pending_end_call
            self.pending_end_call = None
            self.report['assistant_hangup'] = 'cancelled'
            self.pending_ack.pop(item_id, None)
            acknowledgment = self.pending_deliveries.pop(item_id, None)
            if acknowledgment is not None and not acknowledgment.done():
                acknowledgment.set_result(None)
        if self.end_call_sent:
            self.end_call_sent = None
            self.report['assistant_hangup'] = 'cancelled'
            return True
        return False

    async def end_after_farewell(self):
        # Caller and assistant turn events race the PCM stream, so require both the spoken
        # reply's transcript and actual output on the sidecar socket, under output_lock.
        if (self.pending_end_call and self.spoken_turn_final
                and self.farewell_audio_sent and self.farewell_transcribed):
            item_id, spoken_turn = self.pending_end_call
            if spoken_turn == self.current_spoken_turn and item_id not in self.pending_ack:
                self.pending_end_call = None
                self.end_call_sent = item_id
                await self.send({'toolCall': {'functionCalls': [
                    {'id': item_id, 'name': 'end_call', 'args': {}},
                ]}})

    def on_sidecar_response(self, tool_response):
        for response in tool_response.get('functionResponses', []):
            if (self.end_call_sent and response.get('id') == self.end_call_sent
                    and response.get('name') == 'end_call'):
                result = response.get('response') or {}
                self.report['assistant_hangup'] = 'accepted' if result.get('ok') is True else 'declined'
                if result.get('ok') is not True:
                    self.end_call_sent = None

    async def on_data(self, event, *, history_only=False):
        kind = event.get('type')
        if history_only and kind != 'turn.done':
            return
        if not history_only and (self.closed or self.failed.is_set()):
            return
        if len(self.report['events']) < 200:
            summary = event_summary(event)
            summary['handled_at_seconds'] = round(asyncio.get_running_loop().time() - self.began, 3)
            self.report['events'].append(summary)
        if kind in {'input_transcript.added', 'output_transcript.added',
                    'session.context_window.approaching', 'session.context_window.rolled_over'}:
            self.mark(kind)
        if kind == 'turn.interrupted':
            async with self.output_lock:
                turn_id = event.get('turn_id')
                if isinstance(turn_id, str) and (
                    turn_id in self.interrupted_replies
                    or (turn_id in self.finished_replies and turn_id != self.latest_reply_turn)
                ):
                    return
                if self.interrupted_output_until and not self.playout_expected_until:
                    return
                self.interrupt_reply()
                await self.clear_playout()
        elif kind == 'error':
            self.fail(str(event.get('error', event)))
        elif kind in {'turn.created', 'turn.done'}:
            turn = event.get('turn') or {}
            if not isinstance(turn, dict):
                raise ValueError('Malformed Live turn object')
            text = turn.get('transcript') or ''
            if not isinstance(text, str):
                raise ValueError('Malformed Live transcript')
            role = turn.get('role')
            if role not in ('user', 'assistant'):
                return
            user = role == 'user'
            turn_id = turn.get('id')
            identified = isinstance(turn_id, str) and bool(turn_id)
            if identified and (turn_id in self.interrupted_replies or turn_id in self.finished_replies
                               or (user and turn_id in self.reply_transcripts)
                               or (not user and turn_id in self.user_turns)):
                return
            self.last_speech_activity = asyncio.get_running_loop().time()
            if user and identified:
                started = turn_id not in self.user_turns
                if started:
                    playing = self.active_reply_turn
                    self.finish_exchange()
                    previous = self.current_spoken_turn
                    shared = previous in self.shared_work_turns
                    if previous and previous not in self.native_task_turns and (self.spoken_reply or shared):
                        self.last_spoken_exchange = 'Ucapanku sebelumnya: ' + self.user_turns[previous][-1500:]
                        if not shared:
                            self.last_spoken_exchange += '\nJawabanmu: ' + ' '.join(
                                reply for reply, _ in self.spoken_reply.values()
                            )[-1500:]
                    if self.hermes_session_id and previous and not self.spoken_turn_final and self.spoken_reply:
                        slot = {'_turn_id': previous,
                                '_transcript_deadline': asyncio.get_running_loop().time() + POST_ACK_TRANSCRIPT_WAIT}
                        self.pending_spoken_replies[previous] = (slot, self.user_turn_at_ms, self.spoken_reply)
                        self.completed_exchanges.append(slot)
                    self.spoken_reply = {}
                    self.exchange_queued = False
                    self.user_turn_at_ms = time.time_ns() // 1_000_000
                    self.user_turns[turn_id] = text.strip() if kind == 'turn.created' else ''
                    self.transcripts_done.setdefault(turn_id, asyncio.Event())
                    self.current_spoken_turn = turn_id
                    self.spoken_turn_final = False
                    self.assistant_reply_turn = None
                    self.spoken_turn_audio_started = False
                    self.active_reply_turn = None
                    self.result_speech_start_deadline = None
                    self.result_acknowledged = False
                elif kind == 'turn.created' or self.transcripts_done[turn_id].is_set():
                    return
                if kind == 'turn.done':
                    text = completed_transcript(self.user_turns[turn_id], text)
                    if text:
                        self.user_turns[turn_id] = text
                        self.transcripts_done[turn_id].set()
                        self.remember_switch_transcript('user', text, self.exchange_id(turn_id), turn_id=turn_id)
                        if (deferred := self.deferred_handoffs.pop(turn_id, None)) and not history_only:
                            item, released_early = deferred
                            self.start_handoff(item, turn_id, released_early=released_early)
                        if turn_id == self.current_spoken_turn:
                            self.spoken_turn_final = True
                            # The caller transcript can finish after the spoken reply.
                            for reply_turn in self.spoken_reply:
                                self.finish_exchange(reply_turn)
                        else:
                            self.finish_pending_spoken_reply(turn_id)
                if started and not history_only:
                    notify_cancel = self.cancel_end_call()
                    clear_old_audio = kind == 'turn.created' and bool(playing or self.playout_expected_until)
                    if clear_old_audio:
                        if old_reply := playing or self.latest_reply_turn:
                            self.interrupted_replies.add(old_reply)
                            self.reply_transcripts.pop(old_reply, None)
                        if playing:
                            self.interrupted_output_until = float('inf')
                        elif self.playout_expected_until > self.last_speech_activity:
                            # After a final transcript, guard the untagged PCM tail. A fresh
                            # reply can start before its turn event once the caller has finished.
                            self.interrupted_output_until = min(
                                self.playout_expected_until, self.last_speech_activity + PLAYOUT_MARGIN_SECONDS,
                            )
                    if clear_old_audio or (notify_cancel and not self.interrupted_output_until):
                        async with self.output_lock:
                            await self.clear_playout()
            elif kind == 'turn.created' and identified:
                if turn_id in self.reply_transcripts:
                    return
                self.reply_transcripts[turn_id] = text.strip()
                preview = self.work_preview_start
                self.work_preview_start = None
                # V3 may start a preview after a new caller turn, and provides
                # no link to the append. Do not write that ambiguous transcript
                # as the reply to the caller's new message.
                preview_may_start = preview is not None and preview[1] > asyncio.get_running_loop().time()
                if preview_may_start and preview[0] != self.current_spoken_turn:
                    self.ambiguous_preview_turn = turn_id, self.current_spoken_turn
                self.interrupted_output_until = 0.0
                self.active_reply_turn = turn_id
                self.latest_reply_turn = turn_id
                if not (preview_may_start or self.result_acknowledged or self.result_speech_start_deadline is not None):
                    self.assistant_reply_turn = turn_id
                self.result_speech_start_deadline = None
            elif kind == 'turn.done' and identified:
                if turn_id == self.active_reply_turn:
                    self.active_reply_turn = None
                text = completed_transcript(self.reply_transcripts.get(turn_id, ''), text)
                if text:
                    self.reply_transcripts.pop(turn_id, None)
                    self.finished_replies.add(turn_id)
            if kind == 'turn.done' and text:
                if not user and self.ambiguous_preview_turn and turn_id == self.ambiguous_preview_turn[0]:
                    preview_caller = self.ambiguous_preview_turn[1]
                    self.ambiguous_preview_turn = None
                    if preview_caller == self.current_spoken_turn:
                        self.finish_exchange(user_only=True)
                # The next caller turn can arrive while forwarding this transcript.
                if (turn.get('role') == 'assistant' and turn_id and turn_id == self.assistant_reply_turn
                        and self.current_spoken_turn and text.strip() and not self.result_acknowledged
                        and not self.suppress_model_output()
                        and turn_id not in self.spoken_reply):
                    self.spoken_reply[turn_id] = (text, time.time_ns() // 1_000_000)
                    self.finish_exchange(turn_id)
                if history_only:
                    return
                field = 'inputTranscription' if user else 'outputTranscription'
                async with self.output_lock:
                    if not user and self.suppress_model_output():
                        return
                    await self.send({'serverContent': {field: {'text': text}, 'turnComplete': True}})
                    if not user and self.pending_end_call and turn_id and turn_id == self.assistant_reply_turn:
                        self.farewell_transcribed = True
                        await self.end_after_farewell()
        elif kind == 'turn.delta':
            turn_id = event.get('turn_id')
            if not isinstance(turn_id, str):
                return
            # The .added stream has item IDs and can duplicate these words.
            # Without a shared turn ID, fragments cannot safely be joined.
            if turn_id in self.user_turns and not self.transcripts_done[turn_id].is_set():
                transcripts = self.user_turns
            elif turn_id in self.reply_transcripts and turn_id == self.active_reply_turn:
                transcripts = self.reply_transcripts
            else:
                return
            delta = event.get('delta') or ''
            if not isinstance(delta, str):
                raise ValueError('Malformed Live transcript delta')
            transcripts[turn_id] = append_transcript_fragment(transcripts[turn_id], delta)
        elif kind == 'delegation.created':
            item = event.get('item') or {}
            if item.get('target') != 'client':
                return
            item_id = item.get('id')
            if not isinstance(item_id, str) or not item_id:
                self.fail('Delegation missing correlation ID')
            elif item_id not in self.delegations:
                self.delegations.add(item_id)
                turn_id = item.get('user_bidi_turn_id')
                if (isinstance(turn_id, str) and turn_id
                        and turn_id not in self.delegated_turns and self.agent_jobs is not None
                        and self.hermes_session_id is not None):
                    self.transcripts_done.setdefault(turn_id, asyncio.Event())
                    self.delegated_turns.add(turn_id)
                    self.start_handoff(item, turn_id)
                else:
                    self.spawn(self.append_handoff(item_id, 'Tidak ada tindakan tambahan.', channel='commentary'))
        elif kind in {'delegation.context.appended', 'delegation.context.sent'}:
            item_id = event.get('delegation_item_id')
            acknowledgment = self.pending_deliveries.get(item_id)
            if acknowledgment is not None:
                if acknowledgment.done():
                    return
                audio_turn = self.pending_ack.pop(item_id, None)
                field = 'acknowledged_delegations' if kind == 'delegation.context.appended' else 'sent_delegations'
                self.report.setdefault(field, []).append(item_id)
                has_result = (item_id in self.report['backend_completed_delegations']
                              or item_id in self.report.get('shared_dm_receipts', {}))
                result_for_current = audio_turn is not None and audio_turn == self.current_spoken_turn and has_result
                if result_for_current or (self.pending_end_call and self.pending_end_call[0] == item_id):
                    # Result/closing speech is separate from a reply already underway.
                    self.assistant_reply_turn = None
                if self.pending_end_call and self.pending_end_call[0] == item_id:
                    self.farewell_audio_sent = self.farewell_transcribed = False
                if result_for_current:
                    # WebRTC PCM has no delegation id and turn.done can be a fragment.
                    # Keep this reply audible until the caller starts a new turn.
                    self.result_acknowledged = True
                    self.last_speech_activity = asyncio.get_running_loop().time()
                self.pending_deliveries.pop(item_id)
                acknowledgment.set_result(None)

    def start_handoff(self, item, turn_id, *, released_early=False):
        async def route_turn():
            try:
                await self.handoff(item, turn_id, released_early=released_early)
            finally:
                self.finish_routing(turn_id)

        if task := self.spawn(route_turn()):
            self.routing_turns.add(turn_id)
            task.add_done_callback(lambda finished: finished.cancelled() and self.finish_routing(turn_id))

    async def handoff(self, item, turn_id, *, released_early=False):
        # Synero Hermes Live Voice uses this proposal/append transport. A proposal
        # can be the speech itself and is not evidence that Hermes work is needed.
        item_id = item['id']
        turn = self.transcripts_done[turn_id]
        quiet = 'Ikuti ucapan pemilik akun dan jawab langsung dari konteks yang tersedia.'
        if not turn.is_set():
            # V3 can wait for this acknowledgment before it closes the user's transcript.
            released_early = await self.append_handoff(
                item_id, 'Jawab inti ucapan pemilik secara langsung. '
                'Kalau ia jelas meminta tindakan, sebut langkah konkret yang akan kamu lakukan, '
                'tanpa menunggu hasil. Kalau ia bertanya tentang pekerjaan yang berjalan, gunakan status terkini.',
                channel='commentary',
            )
            try:
                await asyncio.wait_for(turn.wait(), POST_ACK_TRANSCRIPT_WAIT)
            except TimeoutError:
                self.report['handoff_missing_transcript'] = self.report.get('handoff_missing_transcript', 0) + 1
                if not turn.is_set():
                    # Resume on this turn's late final without leaving a waiting task behind.
                    self.deferred_handoffs[turn_id] = (item, released_early)
                    return
        request = self.user_turns[turn_id].strip()
        if not request:
            if not released_early:
                await self.append_handoff(item_id, quiet, channel='commentary')
            return
        if self.chat_reconnecting:
            await self.append_handoff(
                item_id, 'Status pekerjaan belum bisa dipastikan karena sambungan ke sesi sedang dipulihkan.',
                channel='speakable' if turn_id == self.current_spoken_turn else 'commentary',
            )
            return
        proposal = ''.join(
            part['text'] for part in item.get('content') or []
            if isinstance(part, dict) and isinstance(part.get('text'), str)
        ).strip()
        previous = ''
        for key, spoken in self.user_turns.items():
            if key == turn_id:
                break
            previous = spoken
        loaded = '\n'.join((self.loaded_context, self.last_spoken_exchange))
        decision = await self.agent_jobs.route(proposal[:12000], request[:12000],
                                              previous[-12000:], loaded.strip(),
                                              session_id=self.hermes_session_id)
        action = decision.get('status')
        if action == 'session_changed':
            self.session_changed()
            return
        work = [entry for entry in decision.get('work', self.dm_work)
                if entry.get('session_id') == self.hermes_session_id]
        admission_id = decision.get('admission_id')
        if action == 'task' and admission_id is not None and not any(
            entry['admission_id'] == admission_id and entry.get('attachment') and entry['status'] == 'completed'
            for entry in work
        ):
            action = 'attachment'
        if action == 'task':
            await self.run_voice_task(item_id, turn_id, request, released_early,
                                      attachment_admission_id=admission_id)
        elif action in {'follow', 'steer', 'dm_only', 'delivery_status', 'speak_result', 'attachment', 'status'}:
            if (action in {'follow', 'steer', 'dm_only', 'delivery_status', 'speak_result'}
                    and any(entry['admission_id'] == admission_id for entry in work)):
                if action in {'dm_only', 'speak_result'}:
                    earlier = self.result_preference_turns.get(admission_id)
                    if earlier is not None:
                        turns = list(self.user_turns)
                        if turns.index(turn_id) < turns.index(earlier):
                            if not released_early:
                                await self.append_handoff(
                                    item_id, 'Gunakan pilihan penyampaian hasil yang terbaru.', channel='commentary',
                                )
                            return
                    self.result_preference_turns[admission_id] = turn_id
                if action == 'dm_only':
                    self.dm_only_results.add(admission_id)
                    self.shared_work_turns.add(turn_id)
                    await self.append_handoff(
                        item_id, 'Pemilik akun memilih menerima hasil pekerjaan ini di DM. '
                        'Akui pilihannya dengan wajar. Biarkan isi hasil di DM.',
                        channel='speakable' if turn_id == self.current_spoken_turn else 'commentary',
                    )
                    return
                if action == 'delivery_status':
                    self.shared_work_turns.add(turn_id)
                    result = await self.agent_jobs.observe_work(self.hermes_session_id, admission_id)
                    if result.get('status') == 'session_changed':
                        self.session_changed()
                        return
                    status = (result.get('status') if result.get('session_id') == self.hermes_session_id
                              and result.get('admission_id') == admission_id else None)
                    text = (dm_delivery_context(result) if status == 'completed'
                            else WORK_STATUS_CONTEXT.get(status, WORK_UNCONFIRMED_CONTEXT))
                    await self.append_handoff(
                        item_id, 'Jawab status pengiriman dengan wajar sesuai informasi ini. ' + text,
                        channel='speakable' if turn_id == self.current_spoken_turn else 'commentary',
                    )
                    return
                if action == 'speak_result':
                    self.dm_only_results.discard(admission_id)
                await self.follow_dm_work(item_id, turn_id, admission_id, released_early,
                                          correction=request[:12000] if action == 'steer' else None)
            else:
                await self.append_handoff(item_id, 'Aku belum bisa memastikan pekerjaan yang kamu maksud.',
                                          channel='speakable' if turn_id == self.current_spoken_turn else 'commentary')
        elif action == 'recall':
            text = await self.recall_context(decision['terms'])
            await self.append_handoff(item_id, text, channel=(
                'speakable' if turn_id == self.current_spoken_turn else 'commentary'
            ))
        elif action == 'end_call' and turn_id == self.current_spoken_turn:
            self.pending_end_call = (item_id, turn_id)
            await self.append_handoff(item_id, 'Akhiri percakapan dengan salam penutup singkat dan alami.',
                                      audio_turn=turn_id)
        else:
            if action in ('unavailable', 'unknown'):
                self.report['handoff_route_unavailable'] = self.report.get('handoff_route_unavailable', 0) + 1
                quiet += ' Belum ada pekerjaan baru atau pengiriman yang terkonfirmasi untuk ucapan ini.'
            # If V3 already began answering after the first ACK, don't trigger a second reply.
            if (turn_id == self.current_spoken_turn
                    and not (self.assistant_reply_turn or self.spoken_reply or self.spoken_turn_audio_started)):
                await self.append_handoff(item_id, quiet)
            elif not released_early:
                await self.append_handoff(item_id, quiet, channel='commentary')

    async def append_handoff(self, item_id, text, *, channel='speakable', audio_turn=None):
        acknowledgment = asyncio.get_running_loop().create_future()
        self.pending_deliveries[item_id] = acknowledgment
        if audio_turn is not None:
            self.pending_ack[item_id] = audio_turn
        try:
            self.connection.send_handoff(item_id, text[:AGENT_OUTPUT_LIMIT], channel)
            await asyncio.wait_for(acknowledgment, RESULT_ACK_TIMEOUT)
            return True
        except OSError as exc:
            if self.pending_end_call and self.pending_end_call[0] == item_id:
                self.cancel_end_call()
            self.pending_ack.pop(item_id, None)
            self.pending_deliveries.pop(item_id, None)
            key = 'handoff_ack_timeouts' if isinstance(exc, TimeoutError) else 'handoff_send_failures'
            self.report.setdefault(key, []).append(item_id)
            return False

    async def follow_dm_work(self, item_id, turn_id, admission_id, released_early, *, correction=None):
        if correction is None:
            self.shared_work_turns.add(turn_id)
        self.call_work.add(admission_id)
        if not released_early:
            if not await self.append_handoff(
                item_id, 'Status terbaru sedang diperiksa. Jawab ketika statusnya tersedia.',
                channel='commentary',
            ):
                item_id = None
        if correction is not None:
            update = await self.agent_jobs.steer_work(self.hermes_session_id, admission_id, correction)
            if update.get('status') != 'not_running':
                # Accepted or uncertain delivery may already have saved the native instruction.
                self.native_task_turns.add(turn_id)
            if update.get('status') != 'queued':
                text = ('Pekerjaan itu sudah tidak bisa diarahkan pada giliran ini.'
                        if update.get('status') == 'not_running'
                        else 'Aku belum bisa memastikan arahan tadi diterima.')
                if item_id is not None:
                    await self.append_handoff(item_id, text, channel=(
                        'speakable' if turn_id == self.current_spoken_turn else 'commentary'))
                return
        if turn_id != self.current_spoken_turn:
            return
        follower = self.work_followers.get(admission_id)
        if follower is not None:
            task, _, _, _, wake = follower
            if wake is not None and not task.done():
                self.work_followers[admission_id] = (task, item_id, turn_id, correction is not None, wake)
                wake.set()
                return
        self.watch_dm_work(admission_id, item_id, turn_id, steered=correction is not None)

    def watch_dm_work(self, admission_id, item_id, turn_id, *, pending_reported=False, steered=False):
        wake = asyncio.Event()
        task = self.spawn(self.read_dm_work(admission_id, repeat=admission_id in self.offered_work_results,
                                          reported_item=item_id if pending_reported else None, wake=wake))
        if task is not None:
            self.work_followers[admission_id] = (task, item_id, turn_id, steered, wake)

    async def read_dm_work(self, admission_id, *, repeat, reported_item, wake):
        retried_terminal_read = False
        while not self.closed and not self.failed.is_set():
            wake.clear()
            task, item_id, turn_id, steered, _ = self.work_followers[admission_id]
            result = await self.agent_jobs.observe_work(self.hermes_session_id, admission_id)
            status = result.get('status')
            if status == 'session_changed':
                self.session_changed()
                return
            if self.work_followers[admission_id][1] != item_id:
                # A newer question requires its own observation.
                continue
            if (result.get('session_id') != self.hermes_session_id
                    or result.get('admission_id') != admission_id):
                status = 'unknown'
            if status in {'completed', 'failed', 'cancelled', 'interrupted', 'not_found'}:
                self.work_followers[admission_id] = (task, item_id, turn_id, steered, None)
                break
            if (not retried_terminal_read and status in {'unknown', 'unavailable'}
                    and any(entry['admission_id'] == admission_id
                            and entry['status'] in {'completed', 'failed', 'cancelled', 'interrupted'}
                            for entry in self.dm_work)):
                # The receipt reader can fail while the watcher still sees this settled admission.
                # Re-read the same receipt once, since its status may never change again.
                retried_terminal_read = True
                continue
            if item_id is not None and item_id != reported_item and turn_id == self.current_spoken_turn:
                reported_item = item_id
                text = WORK_STATUS_CONTEXT.get(status, WORK_UNCONFIRMED_CONTEXT)
                if status == 'started':
                    if 'tool_steps' in result:
                        progress = (self.progress_context(result['tool_steps']) if result['tool_steps'] else
                                    'Langkah saat ini belum diketahui. Gunakan status terbaru ini.')
                    else:
                        progress = self.progress_for_work(admission_id, result.get('execution_generation'))
                    if progress:
                        text += '\n' + progress
                if steered:
                    text = STEER_ACCEPTED_CONTEXT + '\n' + text
                await self.append_handoff(item_id, text)
            # The session stream wakes this observer on a status change or reconnect.
            # Silence on the stream does not justify polling or another task.
            await wake.wait()
        else:
            return
        if status != 'completed':
            self.call_work.discard(admission_id)
        output = result.get('output')
        current = turn_id == self.current_spoken_turn
        if status == 'completed' and isinstance(output, str) and output.strip():
            if item_id is not None:
                self.report.setdefault('shared_dm_receipts', {})[item_id] = admission_id
            if admission_id in self.dm_only_results:
                if item_id is not None and current:
                    delivery = ' ' + dm_delivery_context(result) if result.get('text_delivery') == 'sent' else ''
                    await self.append_handoff(
                        item_id, 'Pekerjaan selesai. Pemilik akun memilih menerima hasilnya di DM. '
                        'Sampaikan status pekerjaan saja dengan kata-katamu sendiri.' + delivery,
                    )
                return
            text = (
                'Sampaikan hasil terbaru langsung dengan kata-katamu sendiri, sesuai obrolan. '
                'Lewati status antre atau progres yang sudah berlalu.\n'
                + output
            )
            await self.present_work_result(item_id, turn_id, admission_id, text, repeat=repeat)
        elif item_id is not None:
            text = WORK_STATUS_CONTEXT.get(status, WORK_UNCONFIRMED_CONTEXT)
            if steered:
                text = STEER_ACCEPTED_CONTEXT + '\n' + text
            await self.append_handoff(item_id, text, channel='speakable' if current else 'commentary')

    def queue_work_preview(self, admission_id, generation, seq, text):
        if not self.connection.can_speak or admission_id not in self.call_work or admission_id in self.dm_only_results:
            return
        cursor = self.work_preview_cursor
        if type(seq) is not int or (cursor and cursor[0] == admission_id and (generation, seq) <= cursor[1:]):
            return
        self.work_preview_cursor = admission_id, generation, seq
        current = self.pending_work_preview
        combined = current[2] + '\n' + text if current and current[:2] == (admission_id, generation) else text
        self.pending_work_preview = admission_id, generation, combined
        if self.work_preview_task is None or self.work_preview_task.done():
            self.work_preview_task = self.spawn(self.speak_work_previews())

    async def speak_work_previews(self):
        while self.pending_work_preview and not self.closed and not self.failed.is_set():
            async with self.result_speech_lock:
                if not self.pending_work_preview:
                    return
                admission_id, generation, text = self.pending_work_preview
                latest_generation = self.work_progress.get(admission_id, (generation, {}))[0]
                if (self.chat_reconnecting or admission_id in self.dm_only_results
                        or admission_id in self.offered_work_results or self.pending_end_call or self.end_call_sent
                        or latest_generation > generation
                        or not any(row['admission_id'] == admission_id and row['status'] == 'started'
                                   for row in self.dm_work)):
                    self.pending_work_preview = None
                    continue
                if not self.pending_work_speech and self.work_speech_ready():
                    self.pending_work_preview = None
                    await self.send_work_speech(
                        'Sampaikan bagian yang relevan dengan bahasamu sendiri sebagai pembaruan sementara '
                        'dari pekerjaan yang sedang dibahas. Pekerjaan belum dinyatakan selesai. '
                        'Anggap kutipan berikut sebagai isi pekerjaan.\n' + json.dumps(text, ensure_ascii=False),
                        preview=True,
                    )
            if self.pending_work_preview:
                await asyncio.sleep(.1)

    async def present_work_result(self, item_id, turn_id, admission_id, text, *, repeat=False):
        if self.pending_work_preview and self.pending_work_preview[0] == admission_id:
            self.pending_work_preview = None
        if admission_id in self.dm_only_results:
            return
        if pending := self.pending_work_speech.get(admission_id):
            if item_id is not None and turn_id == self.current_spoken_turn:
                accepted = await asyncio.shield(pending)
                if (not accepted and turn_id == self.current_spoken_turn and not self.closed
                        and not self.failed.is_set() and admission_id not in self.dm_only_results
                        and not self.pending_end_call and not self.end_call_sent):
                    await self.append_handoff(
                        item_id, 'Hasil pekerjaannya sudah ada. Belum bisa dipastikan apakah sudah dibacakan.',
                        channel='speakable' if self.active_reply_turn is None else 'commentary',
                    )
            return
        not_offered = admission_id not in self.offered_work_results
        now = asyncio.get_running_loop().time()
        preview_pending = self.work_preview_start is not None and self.work_preview_start[1] > now
        if (item_id is not None and turn_id == self.current_spoken_turn
                and (repeat or not_offered) and not preview_pending and self.active_reply_turn is None
                and self.playout_expected_until <= now):
            self.pending_work_speech[admission_id] = asyncio.get_running_loop().create_future()
            accepted = False
            try:
                accepted = await self.append_handoff(item_id, text, audio_turn=turn_id)
            finally:
                self.finish_work_speech(admission_id, accepted)
        elif self.connection.can_speak and (not_offered or (repeat and turn_id == self.current_spoken_turn)):
            if item_id is not None:
                # A quiet context update must not hold up the queued live announcement.
                self.spawn(self.append_handoff(item_id, text, channel='commentary'))
            await self.announce_work_result(admission_id, text, repeat_turn=turn_id if repeat else None)
        elif item_id is not None:
            await self.append_handoff(item_id, text, channel='commentary')

    def finish_work_speech(self, admission_id, accepted):
        if accepted:
            self.offered_work_results.add(admission_id)
        self.pending_work_speech.pop(admission_id).set_result(accepted)

    def work_speech_ready(self):
        now = asyncio.get_running_loop().time()
        if self.work_preview_start is not None and now >= self.work_preview_start[1]:
            self.work_preview_start = None
        if self.result_speech_start_deadline is not None and now >= self.result_speech_start_deadline:
            self.result_speech_start_deadline = None
        return ((self.current_spoken_turn is None or self.spoken_turn_final) and self.active_reply_turn is None
                and self.current_spoken_turn not in self.routing_turns and self.result_speech_start_deadline is None
                and self.work_preview_start is None
                and self.playout_expected_until <= now
                and now - self.last_speech_activity >= RESULT_SPEECH_QUIET_SECONDS)

    async def send_work_speech(self, text, *, preview=False):
        now = asyncio.get_running_loop().time()
        self.result_speech_start_deadline = now + RESULT_ACK_TIMEOUT
        if preview:
            self.work_preview_start = self.current_spoken_turn, self.result_speech_start_deadline
        self.last_speech_activity = now
        audio = self.audio
        try:
            await audio.link.speak(text[:AGENT_OUTPUT_LIMIT])
        except (OSError, RuntimeError, TimeoutError):
            self.result_speech_start_deadline = None
            if preview:
                self.work_preview_start = None
            return False
        return self.audio is audio

    async def announce_work_result(self, admission_id, text, *, repeat_turn=None):
        async with self.result_speech_lock:
            loop = asyncio.get_running_loop()
            while not self.closed and not self.failed.is_set():
                if repeat_turn is None and admission_id in self.offered_work_results:
                    return
                if repeat_turn is not None and repeat_turn != self.current_spoken_turn:
                    return
                if self.pending_end_call or self.end_call_sent:
                    return
                if admission_id in self.dm_only_results or admission_id in self.pending_work_speech:
                    return
                if self.work_speech_ready():
                    self.pending_work_speech[admission_id] = loop.create_future()
                    accepted = False
                    try:
                        accepted = await self.send_work_speech(text)
                    finally:
                        self.finish_work_speech(admission_id, accepted)
                    return
                await asyncio.sleep(.1)

    async def run_voice_task(self, item_id, turn_id, request, released_early, *, attachment_admission_id=None):
        # A lost submit reply can hide a durable admission. Hold its potential
        # transcript mirror until a definite pre-admission rejection is known.
        self.native_task_turns.add(turn_id)
        submit = self.submit_task(item_id, turn_id, request, attachment_admission_id=attachment_admission_id)
        launch_requested = (turn_id == self.current_spoken_turn
                            and not (self.assistant_reply_turn or self.spoken_reply or self.spoken_turn_audio_started))
        if launch_requested or not released_early:
            result, acknowledged = await asyncio.gather(
                submit,
                self.append_handoff(
                    item_id,
                    VOICE_TASK_LAUNCH_CONTEXT if launch_requested else 'Permintaan sedang diperiksa.',
                    channel='speakable' if launch_requested else 'commentary',
                ),
            )
        else:
            result = await submit
            acknowledged = True
        if result is None:
            return
        text, status, admission_id = result
        self.finish_routing(turn_id)
        if status == 'completed':
            await self.present_work_result(item_id if acknowledged else None, turn_id, admission_id, text)
            return
        if acknowledged:
            current = turn_id == self.current_spoken_turn
            provisional = status in {'pending', 'queued', 'started', 'unknown'}
            already_replied = self.assistant_reply_turn or self.spoken_reply or self.spoken_turn_audio_started
            audible = current and (not provisional or not (launch_requested or already_replied))
            acknowledged = await self.append_handoff(
                item_id, text, channel='speakable' if audible else 'commentary',
                audio_turn=turn_id if audible else None,
            )
        if (status in WORK_OPEN_STATUSES | {'unknown'} and admission_id
                and admission_id not in self.work_followers):
            self.watch_dm_work(admission_id, item_id if acknowledged else None, turn_id, pending_reported=True)

    async def recall_context(self, terms):
        recall = await self.agent_jobs.recall(self.hermes_session_id, terms)
        history = recall.get('history')
        if (recall.get('status') == 'found' and recall.get('session_id') == self.hermes_session_id
                and isinstance(history, list) and history):
            evidence = json.dumps(quote_chat(history, limit=9), ensure_ascii=False)
            return ('Kutipan riwayat chat. Jawab pertanyaan saat ini berdasarkan kutipan yang cocok. '
                    'Jika sumbernya belum jelas, minta petunjuk singkat.\n' + evidence)
        if recall.get('status') == 'session_changed':
            self.session_changed()
            return ''
        return 'Bila detailnya belum tersedia, minta petunjuk singkat.'

    async def submit_task(self, item_id, turn_id, request, *, attachment_admission_id=None):
        submission_id = self.submission_id(turn_id)
        call_context = {'direction': self.call_direction} if self.call_direction is not None else {}
        if attachment_admission_id is not None:
            call_context['attachment_admission_id'] = attachment_admission_id
        if self.completed_exchanges:
            if not self.exchange_lock.locked():
                await self.save_exchanges()
            if self.completed_exchanges:
                unsaved = [
                    {'user': item['user'],
                     'assistant': '' if item.get('_turn_id') in self.shared_work_turns else item['assistant']}
                    for item in self.completed_exchanges
                    if '_transcript_deadline' not in item and item.get('_turn_id') not in self.routing_turns
                    and item.get('_turn_id') not in self.native_task_turns
                ]
                if unsaved:
                    call_context['voice_context'] = json.dumps(unsaved, ensure_ascii=False)[-6000:]
        result = await self.agent_jobs.submit(submission_id, request, session_id=self.hermes_session_id,
                                              **call_context)
        status = result.get('status')
        if status == 'session_changed':
            self.session_changed()
            return None
        receipt = result.get('receipt')
        admission_id = receipt.get('admission_id') if isinstance(receipt, dict) and (
            receipt.get('session_id') == self.hermes_session_id and isinstance(receipt.get('admission_id'), str)
        ) else None
        if admission_id and status in WORK_OPEN_STATUSES | {'unknown', 'completed'}:
            # The native receipt survives a lost or replaced Codex peer.
            self.call_work.add(admission_id)
        elif not admission_id and status not in WORK_OPEN_STATUSES | {'unknown', 'completed'}:
            self.native_task_turns.discard(turn_id)
        if status in WORK_OPEN_STATUSES | {'completed'} and not admission_id:
            status = 'unknown'
        if status == 'completed':
            # A caller may already be waiting for this exact voice-started work through a follow-up.
            follower = self.work_followers.get(admission_id)
            if admission_id in self.offered_work_results or (follower is not None and not follower[0].done()):
                return None
            self.report['backend_completed_delegations'].append(item_id)
            self.report.setdefault('agent_receipts', {})[item_id] = {'admission_id': admission_id}
            return ('Gunakan isi hasil yang tersedia untuk menjawab dengan wajar dan ringkas. '
                    'Lewati status antre atau progres yang sudah berlalu. '
                    'Kalau pemilik akun meminta hasil lengkap, bacakan bagian yang tersedia tanpa mengarang.\n'
                    + result['output']), status, admission_id
        text = {
            **WORK_STATUS_CONTEXT,
            'unavailable': 'Aku belum bisa mengerjakan permintaan ini sekarang.',
            'attachment_unavailable': 'Aku belum bisa membuka lagi file itu dari chat ini.',
            'not_found': 'Aku belum bisa memastikan file yang kamu maksud.',
            'unsupported_input': 'Permintaan belum bisa diteruskan. Bisa ucapkan lagi?',
        }.get(status, 'Aku belum bisa memastikan hasil permintaan ini.')
        return text, status, admission_id

    async def ready_or_fail(self, awaitable, timeout):
        return await ready_or_fail(self, awaitable, timeout)

    def connection_items(self, history, prompt, work_context, *, seeded_refs=None):
        chat_item = startup_chat_item(history, prompt + work_context, seeded_refs=seeded_refs)
        items = [chat_item] if chat_item else []
        items.append({'role': 'developer', 'text': prompt})
        if work_context:
            items.append({'role': 'assistant', 'text': work_context})
        return items

    async def start_default_connection(self, prepare_or_start, *args):
        try:
            await prepare_or_start(*args)
        except CodexLoginRequired:
            raise
        except (OSError, RuntimeError):
            self.routing_failure = 'all_unavailable'
            raise

    async def start(self, setup):
        # Keep the local identity; facade model, tools and persona do not select it.
        # Gather unchanged ICE candidates while the provider process initializes.
        direction = None
        if self.direction_reader is not None:
            direction = asyncio.create_task(self.direction_reader())
            self.tasks.append(direction)
        context = None
        if self.agent_jobs is not None:
            context = asyncio.create_task(self.agent_jobs.prepare_context())
            self.tasks.append(context)

        if self.routing is None:
            await self.start_default_connection(self.connection.prepare)
        persona = FALLBACK_PERSONA
        history = []
        history_cursor = None
        if context is not None:
            resolved = await context
            session_id = resolved.get('session_id') if isinstance(resolved, dict) else None
            if (not isinstance(resolved, dict) or resolved.get('status') != 'ready' or not isinstance(session_id, str)
                    or not session_id.strip()):
                raise RuntimeError('Active WhatsApp chat context is unavailable')
            if isinstance(resolved.get('persona'), str) and resolved['persona'].strip():
                persona = resolved['persona']
            history = resolved.get('history', [])
            if not isinstance(history, list):
                raise RuntimeError('Active WhatsApp chat history is unavailable')
            history_cursor = resolved.get('history_cursor')
            self.hermes_session_id = session_id
        if direction is not None:
            self.call_direction = await direction
        opening_context = {
            'inbound': 'Pemilik akun yang meneleponmu. Tunggu dia bicara dulu.',
            'outbound': 'Kamu yang menelepon pemilik akun.',
        }.get(self.call_direction, 'Tunggu pemilik akun bicara dulu.')
        prompt = '\n\n'.join((persona, CALL_PROMPT, opening_context, CALL_END_PROMPT))
        self.voice_prompt = prompt
        work_context = self.update_dm_work(resolved.get('work', [])) if context is not None else ''
        if self.previous_voice_results is not None:
            session_id, offered, dm_only, followed = self.previous_voice_results
            if session_id == self.hermes_session_id:
                self.offered_work_results.update(offered)
                self.dm_only_results.update(dm_only)
                self.call_work.update(followed)
            self.previous_voice_results = None
        if work_context:
            work_context = 'Status pekerjaan di sesi WhatsApp (data):\n' + '\n'.join(work_context.splitlines()[:2])
        if self.dm_only_results:
            work_context += '\nPemilik akun memilih hasil pekerjaan berikut di DM saja: ' + ', '.join(
                sorted(self.dm_only_results),
            )
        initial_items = self.connection_items(history, prompt, work_context)
        chat_item = initial_items[0]
        self.loaded_context = chat_item['text'] if chat_item['role'] != 'developer' else ''
        if work_context:
            self.loaded_context = (self.loaded_context + '\n' + work_context)[-STARTUP_HISTORY_BYTES:]
        if self.routing is None:
            await self.start_default_connection(self.connection.start, prompt, initial_items)
        else:
            self.install_audio(await self.open_routed_audio(prompt, initial_items))
        if type(history_cursor) is int and history_cursor >= 0:
            self.spawn(self.follow_chat(history_cursor))
            await self.ready_or_fail(self.chat_context_ready.wait(), AGENT_CONTEXT_TIMEOUT)
        for admission_id in sorted(self.call_work - self.dm_only_results - self.offered_work_results):
            if admission_id not in self.work_followers:
                self.watch_dm_work(admission_id, None, None)
        self.report['connected'] = True
        self.mark('ready')

    async def close(self):
        if self.closed:
            return
        self.closed = True
        closing_audio = self.audio if self.audio is not None and not self.audio.retired else None
        if closing_audio is not None:
            closing_audio.retire()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        buffered_transcripts = getattr(self.connection, 'buffered_transcripts', None) if self.audio else None
        if buffered_transcripts is not None:
            for event in buffered_transcripts():
                await self.on_data(event, history_only=True)
        # A final caller transcript can already be available without provider buffering.
        self.finish_exchange(user_only=True)
        if self.completed_exchanges:
            try:
                await asyncio.wait_for(self.save_exchanges(), AGENT_CONTEXT_TIMEOUT)
            except (OSError, TimeoutError, ValueError):
                self.report['voice_history_status'] = 'unknown'
            self.report['voice_exchanges_pending'] = len(self.completed_exchanges)
        if closing_audio is not None:
            await closing_audio.close()
        await asyncio.gather(*self.retiring_audio, return_exceptions=True)
        self.recording.close()
        self.input_recording.close()
        self.report['unacknowledged_delegations'] = sorted(self.pending_ack)
        self.report['pending_result_delegations'] = sorted(self.pending_deliveries)
        self.report['status'] = 'failed' if self.failed.is_set() or self.report.get('adapter_error') or self.report['cleanup_errors'] else 'transport-closed-content-unverified'
        self.write_report()
        return bool(self.completed_exchanges and self.report.get('voice_history_status') in VOICE_HISTORY_RETRYABLE)


STATE = web.AppKey('state', dict)


def create_app(token, runs, *, voice_factory=LiveCall, route=CodexConnection.route, routing=None,
               failure_notices=False):
    app = web.Application(client_max_size=128*1024)
    app[STATE] = {'active':None, 'voice':None, 'voice_ready':False, 'runs':Path(runs),
                  'switch':None, 'last':None, 'last_error':None, 'last_failure':None, 'failure_notice':None}
    history_writers = set()

    def authorized(request):
        got = request.headers.get('x-goog-api-key', '') or request.headers.get('Authorization', '').removeprefix('Bearer ')
        return bool(token) and hmac.compare_digest(got, token)

    async def health(request):
        if not authorized(request):
            raise web.HTTPUnauthorized()
        state = app[STATE]
        voice = state['voice']
        audio = getattr(voice, 'audio', None)
        voice_ready = (state['voice_ready'] and voice is not None and not voice.failed.is_set()
                       and (audio is None or not audio.failed.is_set()))
        result = {'route':audio.link.route if audio else route, 'active':state['active'] is not None,
                  'voice_ready':voice_ready, 'last_failure':state['last_failure'],
                  'last':state['last'], 'whatsapp_pairing':'owned by separate sidecar'}
        if routing is not None:
            candidate = audio.candidate if audio else None
            result['active_provider'] = ({'provider':candidate.provider, 'account':candidate.account}
                                         if candidate is not None else None)
            result['routing'] = await asyncio.to_thread(routing.status)
        if failure_notices:
            result['failure_notice'] = state['failure_notice']
        return web.json_response(result)

    async def send_failure_notice(ws, state, reason):
        try:
            async with asyncio.timeout(1):
                status = await asyncio.to_thread(routing.status) if routing is not None else {}
                notice = build_provider_failure(reason, status)
                await ws.send_json({'providerFailure':notice})
        except (OSError, RuntimeError, ValueError):
            state['failure_notice'] = 'unconfirmed'
        else:
            state['failure_notice'] = 'sent_to_sidecar'

    async def select_provider(request):
        if not authorized(request):
            raise web.HTTPUnauthorized()
        if routing is None:
            raise web.HTTPNotFound()
        try:
            payload = await request.json()
            if (not isinstance(payload, dict) or set(payload) != {'selection', 'now'}
                    or not isinstance(payload['selection'], str) or type(payload['now']) is not bool):
                raise ValueError
            selection = payload['selection']
            await asyncio.to_thread(routing.status, selection)
        except ValueError:
            raise web.HTTPBadRequest(text='Unknown Live provider or invalid request') from None
        state = app[STATE]
        if state['switch'] is not None:
            return web.json_response({'status':'failed', 'reason':'switch_in_progress'})
        if not payload['now'] or state['active'] is None:
            routing.set_selected(selection)
            return web.json_response({'status':'selected', 'selection':selection})
        if not state['voice_ready']:
            return web.json_response({'status':'failed', 'reason':'audio_starting'})
        voice = state['voice']
        task = asyncio.create_task(voice.use_audio(selection))
        state['switch'] = task
        try:
            candidate = await task
            if state['voice'] is not voice or voice.closed:
                return web.json_response({'status':'failed', 'reason':'call_ended'})
            routing.set_selected(selection)
            return web.json_response({'status':'switched', 'provider':candidate.provider,
                                      'account':candidate.account})
        except asyncio.CancelledError:
            if state['voice'] is not voice or task.cancelled():
                return web.json_response({'status':'failed', 'reason':'call_ended'})
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            return web.json_response({'status':'failed',
                                      'reason':getattr(exc, 'reason', 'audio_setup_failed')})
        finally:
            if state['switch'] is task:
                state['switch'] = None

    async def active_voice(request):
        if not authorized(request):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        if not isinstance(payload, dict) or set(payload) != {'session_id'} or not isinstance(payload['session_id'], str):
            raise web.HTTPBadRequest()
        voice = app[STATE]['voice']
        if voice is None or voice.closed or payload['session_id'] != voice.hermes_session_id:
            return None
        return voice

    async def flush_history(request):
        voice = await active_voice(request)
        status = await voice.flush_history() if voice is not None else 'idle'
        return web.json_response({'status': status})

    async def reset_session(request):
        voice = await active_voice(request)
        if voice is None:
            return web.json_response({'status': 'idle'})
        voice.session_changed()
        return web.json_response({'status': 'reset'})

    def receive_media(voice, msg):
        if msg.type not in {WSMsgType.TEXT, WSMsgType.BINARY}:
            return
        value = json.loads(msg.data)
        audio = (value.get('realtimeInput') or {}).get('audio')
        if audio:
            if audio.get('mimeType') != 'audio/pcm;rate=16000':
                raise ValueError('Sidecar input must be PCM16/16000Hz mono')
            voice.feed(base64.b64decode(audio['data'], validate=True))
        elif tool_response := value.get('toolResponse'):
            voice.on_sidecar_response(tool_response)

    async def close_voice(voice, state):
        state['voice'] = None
        state['voice_ready'] = False
        switch = state['switch']
        if switch is not None and not switch.done():
            switch.cancel()
            await asyncio.gather(switch, return_exceptions=True)
        if await voice.close():
            task = asyncio.create_task(voice.retry_exchanges())
            history_writers.add(task)
            task.add_done_callback(history_writers.discard)
        if voice.report.get('cleanup_errors') and state['last_failure'] is None:
            state['last_failure'] = 'cleanup_failed'
        state['last'] = getattr(voice, 'path', Path('fixture')).name

    async def live(request):
        if not authorized(request):
            raise web.HTTPUnauthorized()
        state = app[STATE]
        if state['active'] is not None:
            raise web.HTTPConflict(text='One call at a time')
        ws = web.WebSocketResponse(max_msg_size=128*1024, heartbeat=15)
        state['active'] = ws
        voice = None
        startup = failure = receiver = None
        state['last_error'] = None
        state['last_failure'] = None
        state['failure_notice'] = None
        setup_sent = False
        terminal_notice = None
        try:
            await ws.prepare(request)
            first = await asyncio.wait_for(ws.receive_json(), 5)
            if not isinstance(first, dict) or not isinstance(first.get('setup'), dict):
                raise ValueError('Expected sidecar setup envelope')
            if (first['setup'].get('sessionResumption') or {}).get('handle'):
                raise ValueError('Resume is not supported; no new session or task replay')
            receiver = asyncio.create_task(ws.receive())
            previous_voice_results = None
            while True:
                if receiver.done() and receiver.result().type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    return ws
                path = state['runs'] / uuid.uuid4().hex
                voice = voice_factory(path, ws.send_json)
                if previous_voice_results is not None:
                    voice.previous_voice_results = previous_voice_results
                state['voice'] = voice
                startup = asyncio.create_task(voice.start(first['setup']))
                # Watch caller departure during startup. During a provider replacement the
                # existing media track can accept audio while the new peer negotiates.
                async with asyncio.timeout(LIVE_SETUP_TIMEOUT):
                    while not startup.done():
                        done, _ = await asyncio.wait((startup, receiver), return_when=asyncio.FIRST_COMPLETED)
                        if receiver in done:
                            msg = receiver.result()
                            if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                                return ws
                            if not setup_sent:
                                raise ValueError('Unexpected sidecar data before setup completed')
                            receive_media(voice, msg)
                            receiver = asyncio.create_task(ws.receive())
                    try:
                        await startup
                    except RuntimeError:
                        if not getattr(voice, 'replace_voice', False):
                            raise
                if getattr(voice, 'replace_voice', False):
                    await close_voice(voice, state)
                    voice = None
                    continue
                if voice.failed.is_set():
                    raise RuntimeError(voice.report.get('error', 'Audio connection ended during setup'))
                if not setup_sent:
                    await ws.send_json({'setupComplete':{}})
                    setup_sent = True
                state['voice_ready'] = True
                failure = asyncio.create_task(voice.failed.wait())
                while True:
                    done, _ = await asyncio.wait((failure, receiver), return_when=asyncio.FIRST_COMPLETED)
                    if receiver in done:
                        msg = receiver.result()
                        if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            return ws
                    if failure in done:
                        break
                    receive_media(voice, msg)
                    receiver = asyncio.create_task(ws.receive())
                if not getattr(voice, 'replace_voice', False):
                    state['last_failure'] = getattr(voice, 'routing_failure', None) or 'audio_connection_lost'
                    if getattr(voice, 'routing_failure', None) or (
                        getattr(voice, 'audio', None) is not None and voice.audio.failed.is_set()
                    ):
                        terminal_notice = state['last_failure']
                    break
                await ws.send_json({'serverContent': {'interrupted': True}})
                previous_voice_results = (voice.hermes_session_id, voice.offered_work_results,
                                          voice.dm_only_results, voice.call_work)
                await close_voice(voice, state)
                voice = None
        except Exception as exc:
            state['last_error'] = safe_text(f'{type(exc).__name__}: {exc}')
            if isinstance(exc, ProvidersUnavailable):
                state['last_failure'] = exc.reason
                terminal_notice = exc.reason
            elif isinstance(exc, CodexLoginRequired):
                state['last_failure'] = 'codex_login_required'
                terminal_notice = state['last_failure']
            elif state['voice_ready']:
                state['last_failure'] = 'audio_connection_failed'
            elif isinstance(exc, TimeoutError):
                state['last_failure'] = 'audio_setup_timeout'
                if voice is not None:
                    terminal_notice = state['last_failure']
            else:
                state['last_failure'] = 'audio_setup_failed'
                terminal_notice = getattr(voice, 'routing_failure', None)
            if voice is not None:
                voice.report['adapter_error'] = state['last_error']
                voice.report.setdefault('error', state['last_error'])
        finally:
            try:
                if terminal_notice and failure_notices:
                    state['failure_notice'] = 'unconfirmed'
                    if not ws.closed:
                        await send_failure_notice(ws, state, terminal_notice)
            finally:
                tasks = tuple(t for t in (startup, receiver, failure) if t is not None)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    if voice is not None:
                        await close_voice(voice, state)
                except Exception as exc:
                    state['last_error'] = safe_text(f'cleanup failed: {type(exc).__name__}: {exc}')
                    if state['last_failure'] is None:
                        state['last_failure'] = 'cleanup_failed'
                finally:
                    try:
                        await ws.close()
                    finally:
                        state['active'] = None
        return ws

    async def shutdown(_app):
        if app[STATE]['active'] is not None:
            await app[STATE]['active'].close()

    async def stop_history_writers(_app):
        writers = tuple(history_writers)
        for task in writers:
            task.cancel()
        await asyncio.gather(*writers, return_exceptions=True)

    app.router.add_get('/live', live)
    app.router.add_get('/health', health)
    app.router.add_post('/flush-history', flush_history)
    app.router.add_post('/session-reset', reset_session)
    app.router.add_post('/provider', select_provider)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(stop_history_writers)
    return app


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--runs', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8797)
    parser.add_argument('--task-hermes-home', type=Path)
    parser.add_argument('--agent-jobs', action='store_true')
    parser.add_argument('--live-config', type=Path)
    args = parser.parse_args()
    if args.agent_jobs and args.task_hermes_home is None:
        parser.error('--agent-jobs requires --task-hermes-home')
    jobs = AgentJobs(args.task_hermes_home) if args.agent_jobs else None
    # Only the local Hermes worker needs the sidecar's authorized identity.
    for key in ('WA_CONTROL_LIVE_OWNER', 'WA_CONTROL_LIVE_BOT'):
        os.environ.pop(key, None)
    token = args.token_file.read_text().strip()
    routing = None
    if args.live_config is not None:
        from live_routing import LiveProviderPool

        routing = LiveProviderPool(json.loads(args.live_config.read_text()), args.live_config.with_name('live-rotation.json'))
    factory = partial(LiveCall, connection_factory=CodexConnection, agent_jobs=jobs, routing=routing,
                      connector_for=partial(provider_connector, jobs=jobs),
                      direction_reader=partial(read_call_direction, token))
    web.run_app(create_app(token, args.runs, voice_factory=factory, routing=routing,
                           failure_notices=args.agent_jobs),
                host='127.0.0.1',port=args.port)


def provider_connector(candidate, secret, *, jobs=None):
    if candidate.kind == 'gemini':
        from gemini_connection import GeminiConnection

        return partial(GeminiConnection, model=candidate.model, voice=candidate.voice, api_key=secret)
    if candidate.kind == 'gpt-live':
        from gpt_live_connection import GPTLiveConnection

        return partial(GPTLiveConnection, model=candidate.model, voice=candidate.voice, api_key=secret,
                       endpoint=candidate.endpoint)
    if candidate.kind == 'hermes-realtime':
        from realtime_connection import HermesRealtimeConnection

        if jobs is None:
            raise RuntimeError('Hermes Realtime requires the profile worker')
        return partial(HermesRealtimeConnection,
                       mint_secret=partial(jobs.mint_realtime_secret, candidate.provider, candidate.account))
    return partial(CodexConnection, voice=candidate.voice)


if __name__ == '__main__':
    # The launcher runs this file directly. Connectors must share the same exception types.
    sys.modules['call_adapter'] = sys.modules[__name__]
    main()
