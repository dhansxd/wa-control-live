"""Real orchestration with simulated remote WebRTC/Codex boundaries."""
import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'001-codex-audio'))
import probe
import test_probe


def test_voice_delegation_returns_result_before_success(monkeypatch, tmp_path):
    assert 'handoff' in inspect.signature(probe.run_probe).parameters, 'Voice probe has no client-managed task adapter'
    test_probe.install_offline_session(monkeypatch)
    import aiortc
    original = aiortc.RTCPeerConnection
    original_server = probe.AppServer
    errors = asyncio.Queue()
    class Server(original_server):
        def __init__(self, command):
            super().__init__(command)
            self.events = errors
    monkeypatch.setattr(probe, 'AppServer', Server)
    sent = []
    calls = []

    class Peer(original):
        def send(self, raw):
            import json
            sent.append(json.loads(raw))
        async def setRemoteDescription(self, desc):
            await super().setRemoteDescription(desc)
            import json
            event = {'type': 'delegation.created', 'item': {'id':'fixture-delegation', 'content':[{'text':'Buat catatan'}]}}
            self.handlers['message'](json.dumps(event))
            self.handlers['message'](json.dumps(event))
            await asyncio.sleep(0)
            await test_probe.probe.asyncio.sleep(0)
            await self.fake_server_error()

        async def fake_server_error(self):
            # App-server still emits unused backing-agent errors in client-managed mode.
            # The actual live event has threadId and turnId, separate from realtime errors.
            await errors.put({'method': 'error', 'params': {'threadId':'offline-thread', 'turnId':'unused-turn',
                              'error': {'message':'fixture incompatible backing model'}, 'willRetry':False}})

    monkeypatch.setattr(aiortc, 'RTCPeerConnection', Peer)
    async def handoff(event):
        calls.append(event['item']['id'])
        return {'text':'File dibuat. Isinya: tes suara berhasil.', 'session_id':'fixture-only'}
    result = asyncio.run(probe.run_probe(test_probe.make_wav(tmp_path/'input.wav'), tmp_path/'run', timeout=5,
                                         handoff=handoff, realtime_prompt='Fixture-only voice task prompt'))
    assert calls == ['fixture-delegation']
    assert len(sent) == 1
    assert sent[0]['type'] == 'delegation.context.append'
    assert sent[0]['delegation_item_id'] == 'fixture-delegation'
    assert 'tes suara berhasil' in sent[0]['content'][0]['text']
    assert result['handoff_completed'] is True
    assert result['handoff_result']['session_id'] == 'fixture-only'
    assert result['status'] == 'audio-received-content-unverified'
    assert result['peer_closed'] and result['app_server_exited']


def test_task_entrypoint_uses_persistent_handoff_and_task_prompt(monkeypatch, tmp_path):
    import importlib.util
    p = Path(__file__).with_name('task_probe.py')
    assert p.exists(), 'Live task entrypoint not implemented'
    spec = importlib.util.spec_from_file_location('task_probe', p)
    task_probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(task_probe)
    called = []
    async def remote_probe(input_path, output_dir, **kwargs):
        output_dir.mkdir()
        spoken = 'buat catatan uji berisi tes suara berhasil lalu bacakan isinya'
        if output_dir.name == 'recovery':
            spoken = 'Di Ravault file catatan uji berisi tes suara berhasil, lalu bacakan isinya'
        event = {'type':'delegation.created','item':{'id':'only-one','content':[{'text':spoken}]}}
        a = await kwargs['handoff'](event)
        assert await kwargs['handoff'](event) == a
        assert 'Hermes' in kwargs['realtime_prompt']
        return {'status':'fixture-orchestration-only', 'handoff_result':a}
    async def remote_hermes(request, run_dir):
        called.append(request)
        return {'text':'fixture result','session_id':'fixture'}
    monkeypatch.setattr(task_probe, 'run_probe', remote_probe)
    monkeypatch.setattr(task_probe, 'run_hermes', remote_hermes)
    result = asyncio.run(task_probe.run_task(tmp_path/'input.wav', tmp_path/'output'))
    assert result['handoff_result']['session_id'] == 'fixture'
    assert called == ['buat catatan uji berisi tes suara berhasil lalu bacakan isinya']
    assert (tmp_path/'output'/'handoff.sqlite').exists()
    second = asyncio.run(task_probe.run_task(tmp_path/'input.wav', tmp_path/'recovery',
                                            completed_store=tmp_path/'output'/'handoff.sqlite'))
    assert second['handoff_result'] == result['handoff_result']
    assert called == ['buat catatan uji berisi tes suara berhasil lalu bacakan isinya']
    assert (tmp_path/'recovery'/'handoff.sqlite').exists()
