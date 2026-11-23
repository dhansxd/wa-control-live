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

        async def append_chat(items, *, latest_work=False):
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
            progress, result = returned
            assert progress['delegation_item_id'] == 'fixture-delegation'
            assert progress['channel'] == 'commentary'
            assert result['delegation_item_id'] == 'fixture-delegation'
            assert result['channel'] == 'speakable'
            assert result['content'][0]['text'].endswith('Berhasil, daftar judul sudah ditambahkan.')
            await voice.on_data({'type':'delegation.context.appended',
                                 'delegation_item_id':'fixture-delegation'})
            await asyncio.gather(*voice.tasks)
            assert voice.report['acknowledged_delegations'] == ['fixture-delegation'] * 2
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


def test_delivery_updates_are_projected_even_when_codex_parks_context(tmp_path, monkeypatch):
    async def exercise():
        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace())
        voice.active_reply_turn = 'still-speaking'
        work = {'session_id': 'session-a', 'admission_id': 'a'*32, 'status': 'completed',
                'text': 'Kirim satu foto di DM.', 'kind': 'voice_call'}
        try:
            previous = {'has_attachments': True, 'text_delivery': 'sent', 'media_delivery': 'unconfirmed'}
            await voice.apply_chat_update({'work': [{**work, 'delivery': previous}]}, 0)
            assert 'unconfirmed' in voice.grounded_work_context()
            await voice.apply_chat_update({'work': [{**work, 'delivery': {**previous, 'media_delivery': 'sent'}}]}, 0)
            current = voice.grounded_work_context()
            assert '"media_delivery": "sent"' in current and 'unconfirmed' not in current
            assert voice.deferred_work_snapshot
            assert not voice.pending_work_speech
            voice.chat_reconnecting = True
            assert 'media_delivery' not in voice.grounded_work_context()
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize(('observed_media', 'watched_media'), [('unconfirmed', 'sent'), ('sent', 'partial')])
def test_attachment_spoken_after_a_pause_uses_its_latest_sender_receipt(tmp_path, monkeypatch, observed_media, watched_media):
    async def exercise():
        can_speak, waiting = asyncio.Event(), asyncio.Event()
        output = []
        aid = 'a' * 32
        receipt = {'session_id': 'session-a', 'admission_id': aid}
        observed_delivery = {'has_attachments': True, 'text_delivery': 'sent', 'media_delivery': observed_media}

        async def observe(session_id, admission_id):
            assert (session_id, admission_id) == ('session-a', aid)
            return {**receipt, **observed_delivery, 'status': 'completed', 'output': 'Hasil memuat lampiran.'}

        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace(observe_work=observe))
        monkeypatch.setattr(voice.connection.__class__, 'can_speak', property(lambda _: True))

        def ready():
            waiting.set()
            return can_speak.is_set()

        async def send(text):
            output.append(text)
            return True

        voice.work_speech_ready = ready
        voice.send_work_speech = send
        work = {**receipt, 'status': 'completed', 'text': 'Buat gambar.'}
        voice.update_dm_work([{**work, 'delivery': observed_delivery}])
        try:
            voice.watch_dm_work(aid, None, None)
            await asyncio.wait_for(waiting.wait(), 1)
            voice.update_dm_work([{**work, 'delivery': {**observed_delivery, 'media_delivery': watched_media}}])
            can_speak.set()
            await asyncio.wait_for(voice.work_followers[aid][0], 1)
            assert len(output) == 1
            assert 'menerima permintaan pengiriman semua lampiran' in output[0]
            assert 'belum terkonfirmasi' not in output[0]
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize(('action', 'status', 'evidence'), [
    ('delivery_status', 'completed', 'menerima permintaan pengiriman semua lampiran'),
    ('follow', 'started', 'failed'),
    ('follow', 'failed', 'gagal'),
    ('code_changes', 'started', 'print(42)'),
])
def test_work_question_without_provider_proposal_supplies_quiet_native_evidence(tmp_path, monkeypatch, action, status, evidence):
    async def exercise():
        aid = 'a' * 32
        scope = {'session_id': 'session-a', 'admission_id': aid}
        steps = [{'tool': 'editor', 'status': 'failed', 'detail': 'example.py',
                  'file_change': {'code_excerpt': 'print(42)'}}]
        receipt = {**scope, 'status': status,
                   'has_attachments': True, 'media_delivery': 'sent', 'tool_steps': steps}
        quiet = []
        received = asyncio.Event()

        async def observe(session_id, admission_id, **options):
            assert (session_id, admission_id) == ('session-a', aid)
            assert options == ({'include_code': True} if action == 'code_changes' else {})
            return receipt

        async def append(texts, **_):
            quiet.extend(texts)
            received.set()

        voice = voice_fixture(tmp_path, monkeypatch, SimpleNamespace(observe_work=observe))
        voice.append_chat_context = append
        voice.current_spoken_turn = 'owner'
        voice.dm_work = [{**scope, 'status': receipt['status'], 'text': 'Kerjakan.'}]
        voice.user_turns['owner'] = 'Bagaimana sekarang?'
        try:
            await voice.apply_voice_decision(None, 'owner', 'Bagaimana sekarang?', {
                'status': action, 'admission_id': aid,
            }, released_early=True)
            await asyncio.wait_for(received.wait(), .5)
            text = ' '.join(quiet)
            assert evidence in text
            assert not voice.pending_work_speech
        finally:
            await voice.close()
    asyncio.run(exercise())
