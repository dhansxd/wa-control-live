"""Concrete independent-review regressions; no external inference or phone clients."""
import asyncio
import json
from types import SimpleNamespace
import pytest

import call_adapter as adapter


def voice_fixture(path, monkeypatch, jobs):
    async def send(_message):
        pass

    monkeypatch.setattr(adapter, 'codex_command', lambda: ['offline-fixture'])
    voice = adapter.LiveCall(path, send, agent_jobs=jobs)
    voice.hermes_session_id = 'session-a'
    return voice


def test_delegation_waits_for_late_final_of_the_exact_user_turn(tmp_path, monkeypatch):
    async def exercise():
        routed = []
        prelim = asyncio.Event()

        async def route(proposal, current, *_, session_id):
            routed.append((proposal, current))
            routed_event.set()
            return {'status': 'reply'}

        async def append(_item_id, _text, **kwargs):
            assert kwargs['channel'] == 'commentary'
            prelim.set()
            return True

        monkeypatch.setattr(adapter, 'POST_ACK_TRANSCRIPT_WAIT', 0)
        routed_event = asyncio.Event()
        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace(route=route))
        voice.append_handoff = append
        item = {'type': 'delegation.created', 'item': {'id': 'proposal', 'target': 'client',
                'user_bidi_turn_id': 'owner', 'content': [{'text': 'Kalimat terpotong.'}]}}
        try:
            await voice.on_data(item)
            await voice.on_data(item)
            await voice.on_data({**item, 'item': {**item['item'], 'id': 'duplicate-proposal'}})
            await asyncio.wait_for(prelim.wait(), 1)
            await asyncio.sleep(0)
            await voice.on_data({'type': 'turn.created', 'turn': {'role': 'user', 'id': 'owner'}})
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'owner', 'delta': 'Hipotesis awal.'})
            await voice.on_data({'type': 'turn.done', 'turn': {'role': 'user', 'id': 'owner'}})
            await voice.on_data({'type': 'turn.done', 'turn': {'role': 'user', 'id': 'unrelated',
                                                             'transcript': 'Pesan lain.'}})
            assert not routed
            await voice.on_data({'type': 'turn.done', 'turn': {'role': 'user', 'id': 'owner',
                                                             'transcript': 'Kalimat lengkap.'}})
            await asyncio.wait_for(routed_event.wait(), 1)
            assert routed == [('Kalimat terpotong.', 'Kalimat lengkap.')]
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('status', ['pending', 'completed'])
@pytest.mark.parametrize('channel_error', [False, True])
def test_accepted_hermes_task_is_not_discarded_if_voice_preliminary_ack_fails(
    tmp_path, monkeypatch, status, channel_error,
):
    async def exercise():
        calls, followers, presented = [], [], []
        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace())

        async def submit(_item_id, _turn_id, request, **_):
            calls.append(request)
            return 'Pekerjaan diterima.', status, 'admission-a'

        if channel_error:
            def unavailable(_wire):
                raise adapter.InvalidStateError('voice data channel closed')

            voice.connection.channel = SimpleNamespace(send=unavailable)
        else:
            async def no_ack(_item_id, _text, **_):
                return False

            voice.append_handoff = no_ack

        async def present(item_id, turn_id, admission_id, text):
            presented.append((item_id, turn_id, admission_id, text))

        voice.submit_task = submit
        voice.present_work_result = present
        voice.watch_dm_work = lambda *args, **kwargs: followers.append((args, kwargs))
        try:
            await voice.run_voice_task('proposal', 'owner', 'Buat catatan.', released_early=False)
            assert calls == ['Buat catatan.']
            if status == 'completed':
                assert presented == [(None, 'owner', 'admission-a', 'Pekerjaan diterima.')]
                assert not followers
            else:
                assert followers == [(('admission-a', None, 'owner'), {'pending_reported': True})]
                assert not presented
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_transient_receipt_read_after_known_completion_recovers_without_new_work(tmp_path, monkeypatch):
    async def exercise():
        calls = []
        presented = asyncio.Event()
        receipt = {'session_id': 'session-a', 'admission_id': 'admission-a'}

        async def observe_work(session_id, admission_id):
            calls.append((session_id, admission_id))
            if len(calls) == 1:
                return {'status': 'unavailable'}
            return {'status': 'completed', 'output': 'Hasil dari Hermes.', **receipt}

        async def present(_item_id, _turn_id, admission_id, text, **_):
            assert admission_id == receipt['admission_id']
            assert 'Hasil dari Hermes.' in text
            presented.set()

        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace(observe_work=observe_work))
        voice.present_work_result = present
        voice.dm_work = [{'status': 'completed', **receipt}]
        try:
            voice.watch_dm_work('admission-a', None, None)
            await asyncio.wait_for(presented.wait(), 1)
            assert calls == [('session-a', 'admission-a')] * 2
        finally:
            await voice.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('spoken_request', [
    'Buat catatan uji berisi tes suara berhasil, lalu tambahkan daftar judul.',
    'Coba tugas catatan uji, lalu tambahkan daftar judul.',
    'Oke gimana coba tugas catatan uji itu, lalu tambahkan daftar judul.',
])
def test_delegated_request_keeps_all_instructions_without_replaying(tmp_path, spoken_request):
    async def exercise():
        submitted, returned, routed = [], [], []
        result_sent = asyncio.Event()
        fragment = spoken_request.split(',', 1)[0]

        async def route(proposal, current, _previous, _loaded_context, *, session_id):
            assert session_id == 'session-a'
            routed.append((proposal, current))
            return {'status': 'task'}

        async def native_task(submission_id, request, *, session_id):
            assert session_id == 'session-a'
            submitted.append(request)
            return {'status': 'completed', 'output': 'Berhasil, daftar judul sudah ditambahkan.',
                    'receipt': {'session_id': 'session-a', 'admission_id': 'admission-a'}}
        async def send(_message):
            pass
        voice = adapter.LiveCall(tmp_path, send, agent_jobs=SimpleNamespace(
            route=route, submit=native_task,
        ))
        voice.hermes_session_id = 'session-a'
        def append_result(_self, value):
            returned.append(json.loads(value))
            result_sent.set()
        voice.connection.channel = type('CapturedChannel', (), {'send':append_result})()
        event = {'type':'delegation.created','item':{'id':'fixture-delegation',
                 'target':'client', 'user_bidi_turn_id':'owner-turn','content':[{'type':'input_text','text':fragment}]}}
        try:
            await voice.on_data({'type':'turn.created', 'turn':{'role':'user', 'id':'owner-turn'}})
            await voice.on_data(event)
            await voice.on_data(event)
            await asyncio.wait_for(result_sent.wait(), 2)
            assert not routed and not submitted
            await voice.on_data({'type':'delegation.context.appended',
                                 'delegation_item_id':'fixture-delegation'})
            result_sent.clear()
            await voice.on_data({'type':'turn.done', 'turn':{'role':'user', 'id':'owner-turn',
                                                          'transcript':spoken_request}})
            await asyncio.wait_for(result_sent.wait(), 2)
            assert submitted == [spoken_request]
            assert routed == [(fragment, spoken_request)]
            progress, result = returned
            assert progress['delegation_item_id'] == 'fixture-delegation'
            assert progress['channel'] == 'commentary'
            assert result['delegation_item_id'] == 'fixture-delegation'
            assert result['content'][0]['text'].endswith('Berhasil, daftar judul sudah ditambahkan.')
            await voice.on_data({'type':'delegation.context.appended',
                                 'delegation_item_id':'fixture-delegation'})
            await asyncio.gather(*voice.tasks)
            assert voice.report['acknowledged_delegations'] == ['fixture-delegation', 'fixture-delegation']
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_app_server_eof_or_broken_json_ends_call_promptly(tmp_path):
    import sys

    async def exercise(script, path):
        voice = adapter.LiveCall(path, lambda _message: None)
        voice.connection.server = adapter.AppServer([sys.executable, '-c', script])
        try:
            await voice.connection.server.start()
            voice.spawn(voice.connection.read_events())
            try:
                await asyncio.wait_for(voice.failed.wait(), 1)
            except TimeoutError:
                raise AssertionError('App-server reader stopped, but live call did not fail') from None
            assert 'App-server stdout' in voice.report['error']
        finally:
            await voice.close()
        assert voice.connection.server.process.returncode is not None
        report = json.loads((path/'report.json').read_text())
        assert report['status'] == 'failed'
        assert report['peer_closed'] and report['app-server_closed']
        assert not any(not task.done() for task in voice.tasks)

    asyncio.run(exercise('pass', tmp_path/'eof'))
    asyncio.run(exercise("print('not-json')", tmp_path/'broken-json'))
