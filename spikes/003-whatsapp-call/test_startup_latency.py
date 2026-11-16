"""Startup latency contracts with local account, quota and WebRTC fixtures."""
import asyncio
import json

import pytest

import call_adapter as adapter


async def startup_voice(path, monkeypatch, *, account_type='chatgpt', announce_session=True, quota_error=None):
    requests = []
    quota_entered, quota_release, quota_stopped, realtime_started = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event())
    quota_result = {'rateLimits':{'primary':{'usedPercent':17}}}

    class LocalServer:
        def __init__(self, _command):
            self.events, self.pending = asyncio.Queue(), {}
            self.process = type('LocalProcess', (), {'pid':None})()

        async def start(self):
            pass

        async def send(self, message):
            pass

        async def call(self, method, params=None, timeout=35):
            requests.append((method, params))
            if method == 'account/read':
                return {'account':{'type':account_type}}
            if method == 'account/rateLimits/read':
                if not quota_entered.is_set():
                    quota_entered.set()
                    try:
                        await quota_release.wait()
                        if quota_error is not None:
                            raise RuntimeError(quota_error)
                    finally:
                        quota_stopped.set()
                return quota_result
            if method == 'thread/start':
                return {'thread':{'id':'offline-startup-thread'}}
            if method == 'thread/realtime/start':
                voice.connection.answer.set_result('offline-answer-sdp')
                voice.connection.connected.set()
                if announce_session:
                    await voice.connection.on_data({'type': 'session.started'})
                realtime_started.set()
            return {}

        async def close(self):
            pass

    class LocalPeer:
        connectionState = 'closed'
        localDescription = None

        async def createOffer(self):
            return 'offline-offer-sdp'

        async def setLocalDescription(self, offer):
            self.localDescription = type('Description', (), {'sdp':offer})()

        async def setRemoteDescription(self, answer):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(adapter, 'AppServer', LocalServer)
    monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
    voice = adapter.LiveCall(path, lambda _message: None)
    await voice.connection.peer.close()
    voice.connection.peer = LocalPeer()
    return voice, requests, quota_entered, quota_release, quota_stopped, realtime_started, quota_result


def test_call_peer_uses_host_only_ice_without_external_stun(tmp_path, monkeypatch):
    async def exercise():
        configurations = []
        original_peer = adapter.RTCPeerConnection
        def configured_peer(configuration=None):
            configurations.append(configuration)
            return original_peer(configuration)
        monkeypatch.setattr(adapter, 'RTCPeerConnection', configured_peer)
        monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
        voice = adapter.LiveCall(tmp_path, lambda _message: None)
        try:
            assert len(configurations) == 1
            assert configurations[0] is not None, 'Call startup still uses the default ICE configuration'
            assert isinstance(configurations[0], adapter.RTCConfiguration)
            assert configurations[0].iceServers == []
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('account_type', ['apiKey', None])
def test_non_subscription_account_starts_neither_quota_nor_provider_thread(tmp_path, monkeypatch, account_type):
    async def exercise():
        voice, requests, entered, release, stopped, realtime, quota = await startup_voice(
            tmp_path, monkeypatch, account_type=account_type)
        try:
            with pytest.raises(adapter.CodexLoginRequired, match='Subscription login required'):
                await voice.start({})
            assert not entered.is_set() and not realtime.is_set()
            assert [method for method, _ in requests] == ['initialize', 'account/read']
            assert not voice.connection.started
        finally:
            await voice.close()
        assert all(task.done() for task in voice.tasks)
    asyncio.run(exercise())


@pytest.mark.parametrize('quota_error', [None, 'fixture quota lookup unavailable'])
def test_slow_or_failed_quota_does_not_block_audio_readiness(tmp_path, monkeypatch, quota_error):
    async def exercise():
        voice, _, entered, release, stopped, realtime, quota = await startup_voice(
            tmp_path, monkeypatch, quota_error=quota_error,
        )
        startup = asyncio.create_task(voice.start({}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(realtime.wait(), 1)
            await asyncio.wait_for(startup, 1)
            assert voice.report['connected']
            assert not stopped.is_set()
            assert 'rate_limits_startup' not in voice.report
            timings = voice.report['timings_seconds']
            for name in ['app_server_started', 'app_server_initialized', 'subscription_account_validated',
                         'thread_created', 'ice_gathered', 'realtime_start_accepted', 'remote_sdp_received',
                         'peer_connected', 'ready']:
                assert isinstance(timings[name], (float, int)) and timings[name] >= 0
            assert timings['subscription_account_validated'] <= timings['thread_created']
            assert timings['peer_connected'] <= timings['ready']
            release.set()
            await asyncio.wait_for(stopped.wait(), 1)
            if quota_error:
                assert voice.report['quota_read_startup_error'] == quota_error
                assert 'startup_quota_confirmed' not in timings
            else:
                assert voice.report['rate_limits_startup'] == quota
                assert timings['ready'] <= timings['startup_quota_confirmed']
            assert not voice.failed.is_set()
        finally:
            release.set()
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            await voice.close()
    asyncio.run(exercise())


def test_preanswer_readiness_waits_for_the_live_session_event(tmp_path, monkeypatch):
    async def exercise():
        voice, _, _, release, _, realtime, _ = await startup_voice(
            tmp_path, monkeypatch, announce_session=False,
        )
        release.set()
        startup = asyncio.create_task(voice.start({}))
        try:
            await asyncio.wait_for(realtime.wait(), 1)
            async with asyncio.timeout(1):
                while 'peer_connected' not in voice.report['timings_seconds']:
                    await asyncio.sleep(0)
            assert not startup.done()
            assert 'ready' not in voice.report['timings_seconds']
            await voice.connection.on_data({'type': 'session.started'})
            await asyncio.wait_for(startup, 1)
            timings = voice.report['timings_seconds']
            assert timings['peer_connected'] <= timings['voice_session_started'] <= timings['ready']
            assert voice.report['input_bytes'] == 0
        finally:
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            await voice.close()
    asyncio.run(exercise())


def test_close_during_quota_diagnostic_drains_owned_task_without_late_report_mutation(tmp_path, monkeypatch):
    async def exercise():
        voice, _, entered, release, stopped, _, _ = await startup_voice(tmp_path, monkeypatch)
        startup = asyncio.create_task(voice.start({}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(startup, 1)
            assert voice.report['connected'] and not stopped.is_set()
            await voice.close()
            assert stopped.is_set()
            assert all(task.done() for task in voice.tasks)
            assert 'rate_limits_startup' not in voice.report
            persisted = (tmp_path/'report.json').read_bytes()
            report = json.loads(persisted)
            assert report == voice.report
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert report == voice.report
            assert (tmp_path/'report.json').read_bytes() == persisted
        finally:
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            await voice.close()
    asyncio.run(exercise())
