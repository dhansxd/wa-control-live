import asyncio
import struct
import wave

import pytest

import probe


def make_wav(path, *, rate=24000, channels=1, width=2, frames=960):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(struct.pack('<h', 1200) * frames * channels if width == 2 else bytes(frames * channels * width))
    return path


def test_wav_to_paced_media_frames_and_silence(tmp_path):
    assert hasattr(probe, 'load_input'), 'Missing validated input reader'
    assert hasattr(probe, 'PCMFileTrack'), 'Missing headless PCM track'
    path = make_wav(tmp_path / 'short.wav')
    pcm = probe.load_input(path)
    assert len(pcm) == 1920

    async def exercise():
        track = probe.PCMFileTrack(pcm)
        try:
            # No fixture data may leave the peer before the connection is ready.
            before = await track.recv()
            assert bytes(before.planes[0]) == bytes(960)
            track.start_input()
            first = await track.recv()
            second = await track.recv()
            assert first.sample_rate == 24000
            assert first.samples == 480
            assert first.pts == 480
            assert second.pts == 960
            assert bytes(first.planes[0]) == struct.pack('<h', 1200) * 480
            assert track.input_finished.is_set()
            tail = await track.recv()
            assert bytes(tail.planes[0]) == bytes(960)
        finally:
            track.stop()
        assert track.readyState == 'ended'

    asyncio.run(exercise())


@pytest.mark.parametrize('settings', [
    {'rate': 48000}, {'channels': 2}, {'width': 1}, {'frames': 0}, {'frames': 24000 * 16},
])
def test_invalid_input_rejected_before_network(tmp_path, settings):
    with pytest.raises(ValueError):
        probe.load_input(make_wav(tmp_path / 'invalid.wav', **settings))


def test_app_server_rpc_and_process_cleanup():
    assert hasattr(probe, 'AppServer'), 'Missing owned app-server lifecycle'
    import sys
    program = '''import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if 'id' in msg:
        print(json.dumps({'method':'test/event','params':{'ready':True}}), flush=True)
        print(json.dumps({'id':msg['id'],'result':{'method':msg['method'], 'api_key_present':any(os.environ.get(k) for k in ['OPENAI_API_KEY','CODEX_API_KEY'])}}), flush=True)
'''

    async def exercise():
        server = probe.AppServer([sys.executable, '-c', program], env={
            'OPENAI_API_KEY': 'fixture-not-a-real-key', 'CODEX_API_KEY': 'fixture-not-a-real-key',
        })
        await server.start()
        pid = server.process.pid
        try:
            response = await server.call('initialize', {'test': True})
            assert response['method'] == 'initialize'
            assert response['api_key_present'] is False
            assert (await asyncio.wait_for(server.events.get(), 1))['method'] == 'test/event'
        finally:
            await server.close()
        assert server.process.returncode is not None
        assert server.reader_task.done()
        assert pid > 0
        await server.close()  # idempotent

    asyncio.run(exercise())


def test_start_contract_is_webrtc_subscription_only():
    assert hasattr(probe, 'start_params'), 'Missing subscription session contract'
    params = probe.start_params('thread-fixture', 'v=0\r\nfixture-offer', 'cove')
    assert params['transport'] == {'type': 'webrtc', 'sdp': 'v=0\r\nfixture-offer'}
    assert params['version'] == 'v3'
    assert params['clientManagedHandoffs'] is True
    assert params['includeStartupContext'] is False
    assert params['outputModality'] == 'audio'
    assert 'model' not in params  # Codex selects its shipped v3 model.
    assert 'realtimeStartInstructions' not in params
    assert 'realtimeEndInstructions' not in params


def test_received_pcm_written_to_decodable_wav(tmp_path):
    assert hasattr(probe, 'OutputRecorder'), 'Missing RTP audio recorder'
    import av
    frame = av.AudioFrame(format='s16', layout='mono', samples=960)
    frame.sample_rate = 48000
    frame.planes[0].update(struct.pack('<h', 1400) * 960)
    path = tmp_path / 'reply.wav'
    recorder = probe.OutputRecorder(path)
    recorder.accept(frame)
    recorder.close()
    with wave.open(str(path), 'rb') as source:
        assert source.getframerate() == 24000
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getnframes() > 400
        pcm = source.readframes(source.getnframes())
        assert max(struct.unpack('<' + 'h' * (len(pcm)//2), pcm)) > 1000


def install_offline_session(monkeypatch, *, audio_failure=False, stop_failure=False, peer_failure=False):
    """Simulate only remote boundaries; exercise the real run_probe lifecycle."""
    from types import SimpleNamespace
    import aiortc
    import av

    class Server:
        def __init__(self, command):
            self.events = asyncio.Queue()
            self.process = SimpleNamespace(pid=12345, returncode=None)
            self.diagnostics = []
            self.methods = []

        async def start(self):
            pass

        async def send(self, msg):
            pass

        async def call(self, method, params=None, timeout=35):
            self.methods.append(method)
            if method == 'account/read':
                return {'account': {'type': 'chatgpt', 'planType': 'fixture'}}
            if method == 'thread/start':
                return {'thread': {'id': 'offline-thread'}}
            if method == 'thread/realtime/start':
                await self.events.put({'method': 'thread/realtime/sdp', 'params': {'sdp': 'fixture-sdp'}})
            if method == 'thread/realtime/stop' and stop_failure:
                raise OSError('fixture stop failure')
            return {}

        async def close(self):
            self.process.returncode = 0

    class Remote:
        kind = 'audio'

        def __init__(self):
            self.count = 0

        async def recv(self):
            await asyncio.sleep(.01)
            self.count += 1
            if self.count > 1:
                if audio_failure:
                    raise OSError('fixture audio reader failure')
                raise aiortc.mediastreams.MediaStreamError
            frame = av.AudioFrame(format='s16', layout='mono', samples=480)
            frame.sample_rate = 24000
            frame.planes[0].update(struct.pack('<h', 1700) * 480)
            return frame

    class Peer:
        def __init__(self):
            self.handlers = {}
            self.connectionState = 'new'
            self.localDescription = None

        def on(self, name):
            def register(handler):
                self.handlers[name] = handler
                return handler
            return register

        def addTrack(self, track):
            self.track = track

        def createDataChannel(self, name):
            return self

        async def createOffer(self):
            return SimpleNamespace(sdp='fixture-offer')

        async def setLocalDescription(self, desc):
            self.localDescription = desc

        async def setRemoteDescription(self, desc):
            self.connectionState = 'connected'
            self.handlers['track'](Remote())
            # Mark input complete, allowing the actual collector to run.
            self.track.input_finished.set()
            await self.handlers['connectionstatechange']()

        async def close(self):
            if peer_failure:
                raise OSError('fixture peer close failure')
            self.connectionState = 'closed'
            await self.handlers['connectionstatechange']()

    monkeypatch.setattr(probe, 'AppServer', Server)
    monkeypatch.setattr(probe, 'codex_command', lambda: ['offline-fixture-only'])
    monkeypatch.setattr(aiortc, 'RTCPeerConnection', Peer)


def test_audio_worker_failure_cannot_be_mistaken_for_silence(monkeypatch, tmp_path):
    install_offline_session(monkeypatch, audio_failure=True)
    report = asyncio.run(probe.run_probe(make_wav(tmp_path/'input.wav'), tmp_path/'run', timeout=5))
    assert report['received_audio_peak'] > 500
    assert report['status'] == 'failed', report
    assert 'fixture audio reader failure' in report['error']
    assert report['peer_closed'] and report['app_server_exited']


@pytest.mark.parametrize('mode', ['stop_failure', 'peer_failure'])
def test_required_cleanup_failure_sets_failed_verdict_and_exit(monkeypatch, tmp_path, mode):
    install_offline_session(monkeypatch, **{mode: True})
    report = asyncio.run(probe.run_probe(make_wav(tmp_path/'input.wav'), tmp_path/'run', timeout=5))
    assert report['received_audio_peak'] > 500
    assert report['cleanup_errors']
    assert report['status'] == 'failed', report
    assert report['audio_received'] is True
    assert report['app_server_exited']
    import sys
    async def saved_report(*args, **kwargs):
        return report
    monkeypatch.setattr(probe, 'run_probe', saved_report)
    monkeypatch.setattr(sys, 'argv', ['probe.py', str(tmp_path/'input.wav'), '--output-dir', str(tmp_path/'unused')])
    assert probe.main() == 1
