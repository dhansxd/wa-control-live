"""Actual local WebRTC/Opus + PCM WebSocket path, not WhatsApp or Codex inference."""
import asyncio
import base64
import json
import math
import struct
import wave
from pathlib import Path

import av
from aiohttp import ClientSession, WSMsgType, web
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription

import call_adapter as adapter
from test_lifecycle import host


class Tone(AudioStreamTrack):
    async def recv(self):
        frame = await super().recv()
        samples = [int(9000 * math.sin(2 * math.pi * 610 * (frame.pts+i)/frame.sample_rate)) for i in range(frame.samples)]
        frame.planes[0].update(struct.pack('<'+'h'*len(samples), *samples))
        return frame


def test_real_local_rtp_media_through_websocket_both_directions(tmp_path, monkeypatch):
    async def exercise():
        remotes, measured = [], []
        received, stop_ack = asyncio.Event(), asyncio.Event()
        class LocalServer:
            def __init__(self, command):
                self.events, self.pending = asyncio.Queue(), {}
                self.process = type('LocalProcess', (), {'pid':None})()
            async def start(self):
                pass
            async def send(self, msg):
                pass
            async def call(self, method, params=None, timeout=35):
                if method == 'account/read':
                    return {'account':{'type':'chatgpt'}}
                if method == 'thread/start':
                    return {'thread':{'id':'offline-local-peer'}}
                if method == 'thread/realtime/start':
                    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                    remotes.append(peer)
                    peer.addTrack(Tone())
                    @peer.on('datachannel')
                    def on_datachannel(channel):
                        channel.send(json.dumps({'type': 'session.started'}))
                    @peer.on('track')
                    def on_track(track):
                        async def consume():
                            try:
                                while True:
                                    frame = await track.recv()
                                    from array import array
                                    assert frame.format.name == 's16'
                                    samples = array('h', bytes(frame.planes[0]))
                                    peak = max((abs(x) for x in samples), default=0)
                                    measured.append(peak)
                                    if peak > 1000:
                                        received.set()
                            except Exception:
                                pass
                        measured_task = asyncio.create_task(consume())
                        consumers.append(measured_task)
                    await peer.setRemoteDescription(RTCSessionDescription(sdp=params['transport']['sdp'],type='offer'))
                    await peer.setLocalDescription(await peer.createAnswer())
                    await self.events.put({'method':'thread/realtime/sdp','params':{'sdp':peer.localDescription.sdp}})
                if method == 'thread/realtime/stop':
                    stop_ack.set()
                return {}
            async def close(self):
                pass
        consumers = []
        monkeypatch.setattr(adapter, 'AppServer', LocalServer)
        monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture-no-subprocess'])
        monkeypatch.setattr(adapter, 'DIAGNOSTIC_AUDIO_SECONDS', 1)
        app = adapter.create_app('local-media-fixture', tmp_path)
        runner, url = await host(app)
        try:
            async with ClientSession() as client:
                ws = await client.ws_connect(url+'/live',headers={'x-goog-api-key':'local-media-fixture'})
                await ws.send_json({'setup':{}})
                async with asyncio.timeout(8):
                    while not (await ws.receive_json()).get('setupComplete') == {}:
                        pass
                # Supply real PCM at the same 20ms cadence as the Rust call sidecar.
                async def send_caller():
                    for chunk in range(65):
                        pcm = struct.pack('<320h', *(int(7500*math.sin(2*math.pi*430*(chunk*320+i)/16000)) for i in range(320)))
                        await ws.send_json({'realtimeInput':{'audio':{'mimeType':'audio/pcm;rate=16000','data':base64.b64encode(pcm).decode()}}})
                        await asyncio.sleep(.02)
                feeder = asyncio.create_task(send_caller())
                output = bytearray()
                async with asyncio.timeout(8):
                    while len(output) < 12000:
                        message = await ws.receive_json()
                        for part in message.get('serverContent',{}).get('modelTurn',{}).get('parts',[]):
                            inline = part['inlineData']
                            assert inline['mimeType'] == 'audio/pcm;rate=24000'
                            output.extend(base64.b64decode(inline['data']))
                    await received.wait()
                await feeder
                assert max(struct.unpack('<'+'h'*(len(output)//2), output)) > 2000
                assert max(measured) > 1000, 'Live caller audio never crossed actual RTP decode'
                await ws.close()
                for _ in range(100):
                    if app[adapter.STATE]['active'] is None:
                        break
                    await asyncio.sleep(.01)
                assert app[adapter.STATE]['active'] is None
                assert stop_ack.is_set()
            path = next(tmp_path.glob('*/report.json'))
            report = json.loads(path.read_text())
            assert report['input_bytes'] == 65*640
            assert report['output_bytes'] >= 12000
            with wave.open(str(path.parent/'caller.wav'), 'rb') as recording:
                assert recording.getnframes() == 16000
            with wave.open(str(path.parent/'reply.wav'), 'rb') as recording:
                assert 0 < recording.getnframes() <= 24000
            assert report['realtime_stop_acknowledged']
            assert report['peer_closed'] and report['app-server_closed']
            assert not report['cleanup_errors']
            assert not report['backend_completed_delegations']
        finally:
            await runner.cleanup()
            await asyncio.gather(*(p.close() for p in remotes))
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers,return_exceptions=True)
    asyncio.run(exercise())
