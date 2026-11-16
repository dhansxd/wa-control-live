"""Isolated Codex subscription audio feasibility probe."""

import asyncio
from fractions import Fraction
from pathlib import Path
import wave

from aiortc import AudioStreamTrack
from aiortc.mediastreams import MediaStreamError
import av

RATE = 24000
FRAME_SAMPLES = 480


def load_input(path: Path) -> bytes:
    with wave.open(str(path), 'rb') as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, RATE, 'NONE'):
            raise ValueError('Input must be uncompressed PCM16, 24000 Hz, mono WAV')
        count = source.getnframes()
        if not 0 < count <= RATE * 15:
            raise ValueError('Input must contain between 0 and 15 seconds of audio')
        pcm = source.readframes(count)
        if len(pcm) != count * 2:
            raise ValueError('Truncated PCM input')
        return pcm


class PCMFileTrack(AudioStreamTrack):
    def __init__(self, pcm: bytes):
        super().__init__()
        self.pcm = pcm
        self.offset = 0
        self.pts = 0
        self.deadline = None
        self.sending = False
        self.input_finished = asyncio.Event()

    def start_input(self):
        self.sending = True

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
        data = b''
        if self.sending:
            data = self.pcm[self.offset:self.offset + FRAME_SAMPLES * 2]
            self.offset += len(data)
            if self.offset == len(self.pcm):
                self.input_finished.set()
        frame = av.AudioFrame(format='s16', layout='mono', samples=FRAME_SAMPLES)
        frame.planes[0].update(data.ljust(FRAME_SAMPLES * 2, b'\x00'))
        frame.sample_rate = RATE
        frame.time_base = Fraction(1, RATE)
        frame.pts = self.pts
        self.pts += FRAME_SAMPLES
        return frame


class AppServer:
    """JSONL RPC with one owned subprocess; never reads credential files."""

    def __init__(self, command, env=None):
        import os
        self.command = command
        self.env = dict(os.environ if env is None else env)
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'PYTHONPATH'):
            self.env.pop(key, None)
        self.process = None
        self.reader_task = None
        self.stderr_task = None
        self.events = asyncio.Queue(maxsize=512)
        self.pending = {}
        self.sequence = 0
        self.diagnostics = []

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=2**20,
            env=self.env,
        )
        self.reader_task = asyncio.create_task(self._read())
        self.stderr_task = asyncio.create_task(self._stderr())

    async def _stderr(self):
        while line := await self.process.stderr.readline():
            self.diagnostics.append(safe_text(line.decode(errors='replace')))
            self.diagnostics = self.diagnostics[-20:]

    async def _read(self):
        import json
        try:
            while line := await self.process.stdout.readline():
                msg = json.loads(line)
                if 'id' in msg and ('result' in msg or 'error' in msg):
                    future = self.pending.pop(msg['id'], None)
                    if future and not future.done():
                        if 'error' in msg:
                            future.set_exception(RuntimeError(safe_text(str(msg['error']))))
                        else:
                            future.set_result(msg.get('result'))
                elif 'id' in msg:
                    # This voice-only proof executes no backing agent tools.
                    await self.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'No task tools in audio proof'}})
                else:
                    self.events.put_nowait(msg)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('App-server pipe closed'))
            self.pending.clear()

    async def send(self, message):
        import json
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def call(self, method, params=None, timeout=35):
        self.sequence += 1
        rid = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.send({'id': rid, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(rid, None)

    async def close(self):
        import os
        import signal
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 4)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(self.reader_task, self.stderr_task, return_exceptions=True)


def safe_text(value):
    import re
    text = re.sub(r'(?i)(bearer\s+)[^\s\"\x27]+', r'\1[REDACTED]', str(value))
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_.-]{30,})', '[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    return text[:1200].strip()


def start_params(thread_id, offer, voice):
    return {
        'threadId': thread_id,
        'transport': {'type': 'webrtc', 'sdp': offer},
        'version': 'v3', 'outputModality': 'audio', 'voice': voice,
        'clientManagedHandoffs': True, 'includeStartupContext': False,
        'flushTranscriptTailOnSessionEnd': False,
        'delegationAckFiller': False,
        'prompt': (
            'Kamu adalah asisten dalam panggilan WhatsApp ini. Bicara bahasa Indonesia santai, pakai aku/kamu. '
            'Ini hanya uji suara singkat. Tunggu ucapan pengguna, lalu jawab pertanyaannya '
            'langsung dalam satu kalimat pendek. Tidak perlu menyapa dulu atau menjalankan task.'
        ),
    }


class OutputRecorder:
    def __init__(self, path):
        self.writer = wave.open(str(path), 'wb')
        self.writer.setnchannels(1)
        self.writer.setsampwidth(2)
        self.writer.setframerate(RATE)
        self.resampler = av.AudioResampler(format='s16', layout='mono', rate=RATE)
        self.samples = 0
        self.peak = 0
        self.last_active = None
        self.first_active = None
        self.closed = False

    def accept(self, frame):
        import array
        import time
        for part in self.resampler.resample(frame):
            data = bytes(part.planes[0])[:part.samples * 2]
            self.writer.writeframesraw(data)
            self.samples += part.samples
            peak = max((abs(x) for x in array.array('h', data)), default=0)
            self.peak = max(self.peak, peak)
            if peak > 500:
                self.last_active = time.monotonic()
                if self.first_active is None:
                    self.first_active = self.last_active

    def close(self):
        if not self.closed:
            self.accept(None)
            self.writer.close()
            self.closed = True


def codex_command():
    import shutil
    import tomllib
    command = [
        shutil.which('codex') or 'codex', 'app-server', '--listen', 'stdio://',
        '--enable', 'realtime_conversation',
    ]
    overrides = [
        'model_provider="openai"', 'sandbox_mode="read-only"', 'approval_policy="never"',
        'notify=[]', 'features.memories=false', 'features.chronicle=false',
        'analytics.enabled=false',
    ]
    import os
    config = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'
    if config.exists():
        for name in tomllib.loads(config.read_text()).get('mcp_servers', {}):
            overrides.append(f'mcp_servers.{name}.enabled=false')
    for value in overrides:
        command.extend(['-c', value])
    return command


def event_summary(value):
    """Retain protocol evidence, not SDP, auth headers or base64 audio."""
    if isinstance(value, dict):
        return {key: event_summary(val) for key, val in value.items()
                if key.lower() not in {'data', 'audio', 'sdp', 'authorization', 'token', 'email'}}
    if isinstance(value, list):
        return [event_summary(item) for item in value[:10]]
    if isinstance(value, str):
        return safe_text(value)
    return value


async def run_probe(input_path, output_dir, *, timeout=55, voice='cove', handoff=None, realtime_prompt=None):
    import contextlib
    import json
    import time
    from aiortc import RTCPeerConnection, RTCSessionDescription

    pcm = load_input(input_path)  # Must reject invalid input before starting anything.
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        'status': 'running', 'auth_route': 'codex-chatgpt-subscription',
        'transport': 'webrtc', 'realtime_version': 'v3',
        'input_seconds': len(pcm) / (RATE * 2),
        'input_sha256': __import__('hashlib').sha256(input_path.read_bytes()).hexdigest(),
        'timeout_seconds': timeout, 'events': [], 'cleanup_errors': [],
    }
    server = AppServer(codex_command())
    peer = RTCPeerConnection()
    track = PCMFileTrack(pcm)
    peer.addTrack(track)
    recorder = OutputRecorder(output_dir / 'reply.wav')
    channel = peer.createDataChannel('oai-events')
    loop = asyncio.get_running_loop()
    answer = loop.create_future()
    connected = asyncio.Event()
    fatal = loop.create_future()
    tasks = []
    thread_id = None
    start_attempted = False
    begin = time.monotonic()
    handoff_ids = set()
    if handoff is not None:
        report['handoff_completed'] = False
        report['audio_after_handoff'] = False

    def save_event(source, msg):
        if len(report['events']) < 200:
            report['events'].append({'source': source, 'at': round(time.monotonic()-begin, 3), **event_summary(msg)})

    async def run_handoff(event):
        try:
            result = await handoff(event)
            channel.send(json.dumps({
                'type': 'delegation.context.append', 'delegation_item_id': event['item']['id'],
                'content': [{'type': 'input_text', 'text': result['text']}],
            }, ensure_ascii=False))
            report['handoff_result'] = result
            report['handoff_completed'] = True
            report['handoff_returned_at_seconds'] = round(time.monotonic()-begin, 3)
            report['handoff_returned_sample'] = recorder.samples
        except Exception as exc:
            if not fatal.done():
                fatal.set_result(safe_text(f'Hermes handoff failed: {type(exc).__name__}: {exc}'))

    @channel.on('message')
    def on_message(raw):
        try:
            event = json.loads(raw)
        except (ValueError, TypeError):
            return
        save_event('datachannel', event)
        if event.get('type') == 'error' and not fatal.done():
            fatal.set_result(safe_text(str(event.get('error', event))))
        if event.get('type') == 'delegation.created' and handoff is not None:
            item_id = (event.get('item') or {}).get('id')
            if not item_id:
                if not fatal.done():
                    fatal.set_result('Delegation missing correlation ID')
            elif item_id not in handoff_ids:
                handoff_ids.add(item_id)
                tasks.append(asyncio.create_task(run_handoff(event)))

    @peer.on('connectionstatechange')
    async def on_state():
        save_event('media', {'type': 'connectionstatechange', 'state': peer.connectionState})
        if peer.connectionState == 'connected':
            connected.set()
        elif peer.connectionState == 'failed' and not fatal.done():
            fatal.set_result('WebRTC connection failed')

    async def read_audio(remote):
        try:
            while True:
                previous_active = recorder.last_active
                recorder.accept(await remote.recv())
                if report.get('handoff_completed') and recorder.last_active != previous_active:
                    report['audio_after_handoff'] = True
        except MediaStreamError:
            return
        except Exception as exc:
            if not fatal.done():
                fatal.set_result(safe_text(f'Audio worker failed: {type(exc).__name__}: {exc}'))

    @peer.on('track')
    def on_track(remote):
        if remote.kind == 'audio':
            tasks.append(asyncio.create_task(read_audio(remote)))

    async def read_events():
        while True:
            msg = await server.events.get()
            method, params = msg.get('method', ''), msg.get('params') or {}
            if method.startswith('thread/realtime/'):
                save_event('app-server', msg)
                if method == 'thread/realtime/sdp' and not answer.done():
                    answer.set_result(params['sdp'])
                elif method in {'thread/realtime/error', 'thread/realtime/closed'} and not fatal.done():
                    fatal.set_result(safe_text(params.get('message') or params.get('reason') or method))
            elif handoff is not None and method in {'item/started', 'item/completed', 'turn/started', 'turn/completed'}:
                save_event('app-server', msg)
                item = params.get('item') or {}
                if item.get('type') in {'commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall'} and not fatal.done():
                    fatal.set_result('Unexpected backing Codex task tool; Hermes must own execution')
            elif method == 'error' and not fatal.done():
                save_event('app-server', msg)
                if handoff is not None and params.get('threadId') == thread_id and params.get('turnId'):
                    # Codex still starts an unused backing turn for client handoffs.
                    # Its failure is distinct from thread/realtime/error and Hermes.
                    report.setdefault('backing_agent_errors', []).append(event_summary(params))
                else:
                    fatal.set_result(safe_text(str(params)))

    async def wait_for(awaitable, seconds):
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait((task, fatal), timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
            if fatal in done:
                raise RuntimeError(fatal.result())
            if task not in done:
                raise TimeoutError('Timed out waiting for realtime media')
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    try:
        async with asyncio.timeout(timeout + 45):
            await server.start()
            report['app_server_pid'] = server.process.pid
            await server.call('initialize', {'clientInfo': {'name': 'whatsapp-audio-probe', 'version': '0.1'}, 'capabilities': {'experimentalApi': True}})
            await server.send({'method': 'initialized'})
            account = (await server.call('account/read', {'refreshToken': False})).get('account') or {}
            report['account'] = {key: account.get(key) for key in ('type', 'planType')}
            if account.get('type') != 'chatgpt':
                raise RuntimeError('ChatGPT subscription login required; API fallback disabled')
            report['rate_limits_before'] = await server.call('account/rateLimits/read', timeout=20)
            result = await server.call('thread/start', {
                'cwd': str(output_dir.resolve()), 'ephemeral': True,
                'modelProvider': 'openai', 'approvalPolicy': 'never', 'sandbox': 'read-only',
                'baseInstructions': 'Audio transport experiment only. No task execution.',
            })
            thread_id = result['thread']['id']
            report['thread_id'] = thread_id
            tasks.append(asyncio.create_task(read_events()))
            await peer.setLocalDescription(await peer.createOffer())
            start_attempted = True
            params = start_params(thread_id, peer.localDescription.sdp, voice)
            if realtime_prompt is not None:
                params['prompt'] = realtime_prompt
            if handoff is not None:
                params['realtimeStartInstructions'] = (
                    'The client executes every task through Hermes. On a realtime_delegation, '
                    'do not use tools or read files; reply only: skip'
                )
            await server.call('thread/realtime/start', params, timeout=35)
            sdp = await wait_for(answer, 35)
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type='answer'))
            await wait_for(connected.wait(), 25)
            report['connected_at_seconds'] = round(time.monotonic()-begin, 3)
            # Start the fixture only after SRTP is connected; pre-connect frames are silence.
            track.start_input()
            await wait_for(track.input_finished.wait(), report['input_seconds'] + 5)
            report['input_finished_at_seconds'] = round(time.monotonic()-begin, 3)
            deadline = time.monotonic() + (timeout if handoff is not None else min(timeout, 35))
            while time.monotonic() < deadline:
                if fatal.done():
                    raise RuntimeError(fatal.result())
                task_ready = handoff is None or (report.get('handoff_completed') and report.get('audio_after_handoff'))
                if task_ready and recorder.last_active and time.monotonic() - recorder.last_active > 3:
                    break
                await asyncio.sleep(.1)
            if recorder.first_active is None:
                raise RuntimeError('No non-silent remote audio received')
            if handoff is not None and not (report.get('handoff_completed') and report.get('audio_after_handoff')):
                raise RuntimeError('No completed Hermes handoff followed by non-silent audio')
            report['status'] = 'audio-received-content-unverified'
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = safe_text(f'{type(exc).__name__}: {exc}')
    finally:
        report['audio_received'] = recorder.first_active is not None
        if start_attempted and thread_id:
            try:
                await server.call('thread/realtime/stop', {'threadId': thread_id}, timeout=8)
                report['realtime_stop_acknowledged'] = True
            except Exception as exc:
                report['cleanup_errors'].append(safe_text(f'realtime-stop: {exc}'))
        if server.process and server.process.returncode is None:
            try:
                report['rate_limits_after'] = await server.call('account/rateLimits/read', timeout=10)
            except Exception as exc:
                report['rate_limits_after_error'] = safe_text(exc)
        track.stop()
        try:
            await peer.close()
            report['peer_closed'] = peer.connectionState == 'closed'
        except Exception as exc:
            report['cleanup_errors'].append(safe_text(f'peer-close: {exc}'))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.close()
        report['app_server_exit_code'] = server.process.returncode if server.process else None
        report['app_server_exited'] = bool(server.process and server.process.returncode is not None)
        report['diagnostics'] = server.diagnostics[-8:]
        recorder.close()
        report['received_audio_seconds'] = recorder.samples / RATE
        report['received_audio_peak'] = recorder.peak
        report['elapsed_seconds'] = round(time.monotonic()-begin, 3)
        if recorder.first_active:
            report['first_remote_audio_at_seconds'] = round(recorder.first_active-begin, 3)
        if report['cleanup_errors']:
            report['status'] = 'failed'
            report.setdefault('error', 'Required cleanup failed; audio observation retained separately')
        (output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description='One WAV -> Codex subscription WebRTC -> real response WAV. No Desktop or API fallback.')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=int, choices=range(15, 91), default=55, metavar='15..90')
    parser.add_argument('--voice', default='cove')
    args = parser.parse_args()
    report = asyncio.run(run_probe(args.input, args.output_dir, timeout=args.timeout, voice=args.voice))
    print(json.dumps({key: val for key, val in report.items() if key not in {'events', 'diagnostics'}}, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'audio-received-content-unverified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
