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

        async def save_exchange(session_id, exchange):
            assert session_id == 'session-a' and not exchange['assistant']
            return {'status': 'saved'}

        monkeypatch.setattr(adapter, 'POST_ACK_TRANSCRIPT_WAIT', 0)
        routed_event = asyncio.Event()
        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace(route=route, save_exchange=save_exchange))
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


@pytest.mark.parametrize('steps, expected', [
    ([{'tool': 'read_file', 'status': 'started', 'detail': 'notes.txt'}], 'notes.txt'),
    ([{'tool': 'browser_exec', 'status': 'completed', 'application_outcome': 'failed',
       'finding': 'Halaman melaporkan pembuatan audio gagal.'}], 'pembuatan audio gagal'),
    ([], 'Langkah saat ini belum diketahui'),
])
def test_requested_progress_uses_fresh_observer_instead_of_lagging_voice_context(
    tmp_path, monkeypatch, steps, expected,
):
    async def exercise():
        answers = []
        answered = asyncio.Event()

        async def observe_work(session_id, admission_id):
            assert (session_id, admission_id) == ('session-a', 'admission-a')
            return {'status': 'started', 'session_id': session_id, 'admission_id': admission_id,
                    'execution_generation': 7, 'tool_steps': steps}

        async def append(item_id, text, *, channel='speakable'):
            assert (item_id, channel) == ('status', 'speakable')
            answers.append(text)
            answered.set()
            return True

        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace(observe_work=observe_work))
        voice.append_handoff = append
        voice.current_spoken_turn = 'owner'
        voice.work_progress['admission-a'] = (7, {
            'stale': {'tool': 'web_extract', 'status': 'started', 'detail': 'Langkah lama'},
        })
        try:
            voice.watch_dm_work('admission-a', 'status', 'owner')
            await asyncio.wait_for(answered.wait(), 1)
            assert len(answers) == 1 and expected in answers[0]
            assert 'Langkah lama' not in answers[0]
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_application_failure_reaches_live_before_final_task_receipt_and_is_only_offered_once(tmp_path, monkeypatch):
    async def exercise():
        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace())
        context, previews = [], []

        async def append_chat(items):
            context.extend(items)
            return True

        voice.append_chat_context = append_chat
        voice.queue_work_preview = lambda *args: previews.append(args)
        voice.dm_work = [{'admission_id': 'admission-a', 'status': 'started', 'text': 'Buat video.'}]
        voice.call_work.add('admission-a')
        voice.pending_work_preview = 'admission-a', 7, 'Perkiraan lama.'
        failure = {'admission_id': 'admission-a', 'generation': 7, 'seq': 219, 'tool_id': 'first',
                   'tool_name': 'browser_exec', 'phase': 'completed', 'application_outcome': 'failed',
                   'finding': 'Halaman melaporkan pembuatan audio gagal.'}
        try:
            await voice.apply_tool_progress(failure)
            await voice.apply_tool_progress({**failure, 'seq': 222, 'tool_id': 'second'})
            next_finding = 'Halaman melaporkan pembuatan video gagal.'
            await voice.apply_tool_progress({**failure, 'seq': 224, 'tool_id': 'third', 'finding': next_finding})
            assert previews == [('admission-a', 7, 219, failure['finding']), ('admission-a', 7, 224, next_finding)]
            assert any(failure['finding'] in text for text in context)
            assert voice.pending_work_preview is None
            assert voice.dm_work[0]['status'] == 'started'
            assert 'first_application_failure_observed' in voice.report['timings_seconds']
        finally:
            await voice.close()

    asyncio.run(exercise())


def test_progress_context_keeps_running_step_and_last_application_failure():
    active = {'tool': 'browser_exec', 'status': 'started', 'detail': 'Periksa percobaan terbaru'}
    failure = {'tool': 'browser_exec', 'status': 'completed', 'application_outcome': 'failed',
               'finding': 'Pembuatan audio gagal.'}
    background = [{'tool': 'read_file', 'status': 'completed', 'detail': f'Dokumen {index}'}
                  for index in range(6)]

    context = adapter.LiveCall.progress_context([active, failure, *background])

    assert active['detail'] in context
    assert failure['finding'] in context


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

        async def native_task(submission_id, request, *, session_id, spoken_at_ms):
            assert type(spoken_at_ms) is int
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
            progress, launch = returned
            assert progress['delegation_item_id'] == 'fixture-delegation'
            assert progress['channel'] == 'commentary'
            assert launch['delegation_item_id'] == 'fixture-delegation'
            assert launch['channel'] == 'speakable'
            result_sent.clear()
            await voice.on_data({'type':'delegation.context.appended',
                                 'delegation_item_id':'fixture-delegation'})
            await asyncio.wait_for(result_sent.wait(), 2)
            result = returned[2]
            assert result['delegation_item_id'] == 'fixture-delegation'
            assert result['content'][0]['text'].endswith('Berhasil, daftar judul sudah ditambahkan.')
            await voice.on_data({'type':'delegation.context.appended',
                                 'delegation_item_id':'fixture-delegation'})
            await asyncio.gather(*voice.tasks)
            assert voice.report['acknowledged_delegations'] == ['fixture-delegation'] * 3
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
