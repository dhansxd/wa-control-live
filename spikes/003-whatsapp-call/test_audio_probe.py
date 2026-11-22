"""Diagnostic capture and comparison checks without calls or provider sessions."""
import asyncio
import hashlib
import json
import struct
import wave

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import make_mocked_request

import audio_probe
import call_adapter
from test_lifecycle import host


def write_wav(path, pcm, rate=16000):
    with wave.open(str(path), 'wb') as recording:
        recording.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        recording.writeframes(pcm)


def test_capture_preserves_samples_and_records_timing_without_provider(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Diagnostic capture opened a provider or Hermes task')
    monkeypatch.setattr(call_adapter, 'AppServer', forbidden)
    monkeypatch.setattr(call_adapter, 'codex_command', forbidden)
    monkeypatch.setattr(call_adapter, 'AgentJobs', forbidden)

    async def exercise():
        ticks = iter((1_000_000_000, 1_020_000_000, 1_040_000_000, 1_060_000_000))
        monkeypatch.setattr(audio_probe.time, 'monotonic_ns', lambda: next(ticks))
        voice = audio_probe.DiagnosticCall(tmp_path/'capture', forbidden)
        await voice.start({'tools':[{'never':'execute'}]})
        chunks = [struct.pack('<4h', -32768, 0, 32767, 0), bytes(8)]
        for chunk in chunks:
            voice.feed(chunk)
        await voice.close()
        await voice.close()
        assert audio_probe.read_pcm(voice.path/'caller.wav') == b''.join(chunks)
        rows = [json.loads(line) for line in (voice.path/'envelopes.ndjson').read_text().splitlines()]
        assert [row['sample_offset'] for row in rows] == [0, 4]
        assert [row['elapsed_ms'] for row in rows] == [20, 40]
        assert rows[0]['peak'] == 32768 and rows[0]['zero_samples'] == 2
        assert rows[1]['peak'] == rows[1]['rms'] == 0
        report = json.loads((voice.path/'report.json').read_text())
        assert report['mode'] == 'audio-diagnostic' and report['inference'] == 'disabled'
        assert report['input_bytes'] == 16 and report['samples'] == 8
        assert report['sha256'] == hashlib.sha256(b''.join(chunks)).hexdigest()
        assert report['state'] == 'captured' and not report['cleanup_errors']
        assert (voice.path/'caller.wav').stat().st_mode & 0o777 == 0o600
    asyncio.run(exercise())


def test_capture_limits_and_invalid_pcm(tmp_path):
    async def exercise():
        voice = audio_probe.DiagnosticCall(tmp_path/'limited', None, max_seconds=.001)
        await voice.start({})
        with pytest.raises(ValueError, match='PCM16'):
            voice.feed(b'x')
        voice.feed(bytes(32))
        voice.feed(bytes(2))
        assert voice.failed.is_set()
        assert voice.report['limit_reached'] == 'sample_count'
        await voice.close()
        assert audio_probe.read_pcm(voice.path/'caller.wav') == bytes(32)

        voice = audio_probe.DiagnosticCall(tmp_path/'deadline', None, max_seconds=.001)
        await voice.start({})
        await asyncio.wait_for(voice.failed.wait(), 1)
        assert voice.report['limit_reached'] == 'wall_time'
        await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('sidecar,facade,comparison,prefix,sidecar_tail,facade_tail', [
    ([1, 2, 3], [1, 2, 3], 'exact', 3, 0, 0),
    ([1, 2, 3], [1, 2], 'facade_is_prefix', 2, 1, 0),
    ([1, 2], [1, 2, 3], 'sidecar_is_prefix', 2, 0, 1),
    ([1, 2, 3], [1, 9, 3], 'mismatch', 1, 0, 0),
    ([], [], 'empty_capture', 0, 0, 0),
    ([1, 2], [], 'empty_capture', 0, 0, 0),
])
def test_analyzer_distinguishes_exact_prefix_and_mismatch(tmp_path, sidecar, facade, comparison, prefix, sidecar_tail, facade_tail):
    paths = [tmp_path/'sidecar.wav', tmp_path/'facade.wav']
    for path, samples in zip(paths, (sidecar, facade)):
        write_wav(path, struct.pack('<'+'h'*len(samples), *samples))
    result = audio_probe.analyze(*paths)
    assert result['comparison'] == comparison
    assert result['matching_prefix_samples'] == prefix
    assert result['sidecar_extra_tail_samples'] == sidecar_tail
    assert result['facade_extra_tail_samples'] == facade_tail
    assert result['bridge_pcm_equal'] == (comparison == 'exact')


def test_analyzer_rejects_wrong_format_and_truncated_data(tmp_path):
    path = tmp_path/'input.wav'
    write_wav(path, bytes(8), rate=24000)
    with pytest.raises(ValueError, match='16000'):
        audio_probe.read_pcm(path)
    write_wav(path, bytes(8))
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError, match='truncated'):
        audio_probe.read_pcm(path)


def test_app_health_identifies_diagnostic_and_reuses_authorization(tmp_path):
    async def exercise():
        app = audio_probe.create_probe_app('local-test-token', tmp_path)
        health = next(route.handler for route in app.router.routes()
                      if route.method == 'GET' and route.resource.canonical == '/health')
        with pytest.raises(Exception) as exc:
            await health(make_mocked_request('GET', '/health'))
        assert getattr(exc.value, 'status', None) == 401
        response = await health(make_mocked_request('GET', '/health', headers={'x-goog-api-key':'local-test-token'}))
        assert json.loads(response.body)['route'] == 'audio-diagnostic'
    asyncio.run(exercise())


def test_websocket_capture_uses_diagnostic_without_provider_or_native_task(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Diagnostic WebSocket opened inference or native work')
    monkeypatch.setattr(call_adapter, 'AppServer', forbidden)
    monkeypatch.setattr(call_adapter, 'codex_command', forbidden)
    monkeypatch.setattr(call_adapter, 'AgentJobs', forbidden)
    async def exercise():
        import base64
        app = audio_probe.create_probe_app('local-test-token', tmp_path)
        runner, url = await host(app)
        pcm = struct.pack('<320h', *range(320))
        try:
            async with ClientSession() as client:
                ws = await client.ws_connect(url+'/live', headers={'x-goog-api-key':'local-test-token'})
                await ws.send_json({'setup':{'model':'ignored', 'tools':[{'ignored':True}]}})
                assert await ws.receive_json() == {'setupComplete':{}}
                await ws.send_json({'realtimeInput':{'audio':{'mimeType':'audio/pcm;rate=16000',
                                   'data':base64.b64encode(pcm).decode()}}})
                await ws.close()
                for _ in range(100):
                    if app[call_adapter.STATE]['active'] is None:
                        break
                    await asyncio.sleep(.01)
            assert app[call_adapter.STATE]['active'] is None
            run = next(tmp_path.iterdir())
            assert audio_probe.read_pcm(run/'caller.wav') == pcm
            report = json.loads((run/'report.json').read_text())
            assert report['state'] == 'captured'
            assert report['inference'] == 'disabled'
        finally:
            await runner.cleanup()
    asyncio.run(exercise())
