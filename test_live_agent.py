import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import os
import sys
from types import SimpleNamespace

import pytest

import live_agent
from whatsapp_session import append_voice_exchange


FIRST = {'submission_id': '1' * 32, 'session_id': 'session-a', 'text': 'Riset tiga ide cerita pendek.'}
FOLLOW_UP = {'submission_id': '2' * 32, 'session_id': 'session-a', 'text': 'Aku mau kamu cek tugas terakhir.'}
PHOTO_WORK = {'admission_id': 'a' * 32, 'session_id': 'session-a', 'status': 'started',
              'text': 'Jelaskan foto ini.', 'kind': 'photo', 'seq': 1, 'attachment': True}


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.admissions = {}
        self.active_session = 'session-a'
        self.logical_session = None
        self.receipt = {'status': 'terminal', 'outcome': 'completed',
                        'result': {'final_response': 'Hasil asli Hermes.'}}
        self.prompts = []
        self.authority_epoch = 4

    @asynccontextmanager
    async def connect(self):
        yield self

    async def rpc(self, method, **params):
        params.pop('_timeout', None)
        self.calls.append((method, params))
        session_id = params['session_id']
        assert session_id == self.active_session
        if method == 'prompt.submit':
            assert params['surface'] == 'voice-live'
            key = params['submission_id']
            return self.admissions.setdefault(key, {
                'admission_id': f'admission-{len(self.admissions) + 1}',
                'ref': {'session_id': session_id},
            })
        if method == 'session.resume':
            assert params['omit_messages'] is True
            return {'session_id': self.logical_session or session_id, 'prompts': self.prompts,
                    'running': self.receipt['status'] == 'started',
                    'execution_generation': self.receipt.get('execution_generation'),
                    'authority_epoch': self.authority_epoch}

        assert method == 'prompt.receipt'
        assert params['include_result'] is True
        admission = next(row for row in self.admissions.values()
                         if row['admission_id'] == params['admission_id'])
        return {**admission, 'authority_epoch': self.authority_epoch, **self.receipt}

    def resolve(self, _home, _owner):
        return self.active_session


def run(data_dir, request, gateway, *, poll_seconds=0.02):
    return asyncio.run(live_agent._run(
        data_dir, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
        read_history=lambda _home, sid: ([{'role': 'user', 'text': f'Context from {sid}'}], 11),
        poll_seconds=poll_seconds,
    ))


@pytest.mark.parametrize('replacement, expected', [
    (None, {'status': 'ready', 'session_id': 'session-a', 'work': [PHOTO_WORK]}),
    ('session-b', {'status': 'session_changed'}),
])
def test_work_snapshot_reads_only_current_session_admissions(tmp_path, replacement, expected):
    gateway = FakeGateway()

    def read_work(_home, _owner, session_id):
        assert session_id == 'session-a'
        if replacement:
            gateway.active_session = replacement
        return [PHOTO_WORK]

    def unexpected_read(*_args):
        pytest.fail('Refreshing work must not reread the conversation')

    result = asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'work', 'session_id': 'session-a'},
        resolve=gateway.resolve, connect=gateway.connect, read_work=read_work,
        read_history=unexpected_read, read_context_updates=unexpected_read,
    ))
    assert result == expected
    assert all(method == 'session.resume' for method, _ in gateway.calls)


@pytest.mark.parametrize(('decision', 'expected'), [
    ({'action': 'reply'}, {'status': 'reply'}),
    ({'action': 'recall', 'terms': ['resi', 'kiriman']}, {'status': 'recall', 'terms': ['resi', 'kiriman']}),
    ({'action': 'task'}, {'status': 'reply'}),
    ({'action': 'task', 'evidence': 'Coba periksa paket.'}, {'status': 'reply'}),
    ({'action': 'steer', 'evidence': 'Kita bahas resi.'}, {'status': 'reply'}),
    ({'action': 'end_call'}, {'status': 'end_call'}),
    ({'action': 'attachment'}, {'status': 'attachment'}),
    ({'action': 'status'}, {'status': 'status'}),
    ({'action': 'task', 'text': 'replace the caller’s request'}, {'status': 'unavailable'}),
    ({'action': 'recall', 'terms': ['x']}, {'status': 'unavailable'}),
    (None, {'status': 'unavailable'}),
])
def test_voice_intent_uses_current_utterance_and_loaded_context_without_admitting(tmp_path, decision, expected):
    gateway = FakeGateway()
    data = {'current': 'Terus berapa tadi?', 'previous': 'Kita bahas resi.',
            'proposal': 'Coba periksa paket.', 'loaded_context': 'Di DM: nomor resinya 123456.'}

    async def classify(**kwargs):
        assert json.loads(kwargs['input'][0]['text']) == data
        assert kwargs['json_mode'] and 'external information' in kwargs['instructions']
        assert kwargs['task'] == live_agent.CALL_INTENT_TASK
        return SimpleNamespace(parsed=decision)

    result = asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'route', **data}, resolve=gateway.resolve,
        connect=gateway.connect, llm=SimpleNamespace(acomplete_structured=classify),
    ))
    assert result == expected
    assert gateway.calls == []


@pytest.mark.parametrize('reset_at', [None, 'before', 'after', 'compaction'])
def test_call_routes_once_with_fresh_dm_work_and_checks_session_again(tmp_path, reset_at):
    gateway = FakeGateway()
    request = {'action': 'route', 'session_id': 'session-a',
               'current': 'Apa isi dokumen yang baru kukirim?', 'proposal': '',
               'previous': '', 'loaded_context': '',
               'work': [{**PHOTO_WORK, 'admission_id': 'b' * 32}]}
    current = {**PHOTO_WORK, 'kind': 'document'}
    if reset_at == 'before':
        gateway.active_session = 'session-b'
    classifications = []

    def read_work(home, owner, session_id):
        assert (home, owner, session_id) == (tmp_path, '1234567890', 'session-a')
        return [current]

    async def classify(**kwargs):
        data = json.loads(kwargs['input'][0]['text'])
        classifications.append(data['work'])
        if reset_at in {'after', 'compaction'}:
            gateway.active_session = 'session-b'
            if reset_at == 'compaction':
                gateway.logical_session = 'session-a'
        return SimpleNamespace(parsed={'action': 'follow', 'admission_id': current['admission_id']})

    result = asyncio.run(live_agent._run(
        tmp_path, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
        read_work=read_work, llm=SimpleNamespace(acomplete_structured=classify),
    ))
    assert [method for method, _ in gateway.calls] == ['session.resume'] * (
        2 if reset_at in {'after', 'compaction'} else 1
    )
    assert classifications == ([] if reset_at == 'before' else [[current]])
    expected = {'status': 'follow', 'admission_id': current['admission_id'], 'work': [current]}
    assert result == ({'status': 'session_changed'} if reset_at in {'before', 'after'} else expected)


def test_call_rechecks_new_dm_work_before_submitting_a_possible_duplicate(tmp_path):
    gateway = FakeGateway()
    admissions, routed_work = [], []
    document = {**PHOTO_WORK, 'kind': 'document'}
    request = {'action': 'route', 'session_id': 'session-a', 'proposal': '',
               'current': 'Tolong lihat berkas yang baru kukirim.', 'previous': '', 'loaded_context': ''}

    async def classify(**kwargs):
        work = json.loads(kwargs['input'][0]['text']).get('work', [])
        routed_work.append(work)
        if not work:
            admissions.append(document)
            return SimpleNamespace(parsed={'action': 'task', 'evidence': 'Tolong lihat berkas'})
        return SimpleNamespace(parsed={'action': 'follow', 'admission_id': document['admission_id']})

    result = asyncio.run(live_agent._run(
        tmp_path, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
        read_work=lambda *_: list(admissions), llm=SimpleNamespace(acomplete_structured=classify),
    ))
    assert routed_work == [[], [document]]
    assert result == {'status': 'follow', 'admission_id': document['admission_id'], 'work': [document]}
    assert [method for method, _ in gateway.calls] == ['session.resume']


@pytest.mark.parametrize(('decision', 'status'), [
    ({'action': 'follow', 'admission_id': PHOTO_WORK['admission_id']}, 'follow'),
    ({'action': 'steer', 'admission_id': PHOTO_WORK['admission_id']}, 'steer'),
    ({'action': 'steer', 'admission_id': 'b' * 32}, 'reply'),
    ({'action': 'follow', 'admission_id': 'c' * 32}, 'reply'),
    ({'action': 'dm_only', 'admission_id': PHOTO_WORK['admission_id']}, 'dm_only'),
    ({'action': 'dm_only', 'admission_id': 'b' * 32}, 'dm_only'),
    ({'action': 'dm_only', 'admission_id': 'c' * 32}, 'status'),
    ({'action': 'delivery_status', 'admission_id': 'b' * 32}, 'delivery_status'),
    ({'action': 'code_changes', 'admission_id': 'b' * 32}, 'code_changes'),
    ({'action': 'code_changes', 'admission_id': 'c' * 32}, 'status'),
    ({'action': 'speak_result', 'admission_id': 'b' * 32}, 'speak_result'),
    ({'action': 'task', 'admission_id': 'b' * 32}, 'task'),
    ({'action': 'task', 'admission_id': PHOTO_WORK['admission_id']}, 'attachment'),
    ({'action': 'task', 'admission_id': 'c' * 32}, 'attachment'),
    ({'action': 'task', 'admission_id': 'd' * 32}, 'attachment'),
])
def test_voice_routes_only_to_the_supplied_work(tmp_path, decision, status):
    work = [PHOTO_WORK, {**PHOTO_WORK, 'admission_id': 'b' * 32, 'status': 'completed', 'seq': 2},
            {'admission_id': 'd' * 32, 'session_id': 'session-a', 'status': 'completed',
             'text': 'Jelaskan ini.', 'kind': 'text', 'seq': 3}]
    request = {'action': 'route', 'current': 'Tolong fokus ke tulisan di pojok.',
               'previous': 'Soal foto tadi.', 'proposal': '', 'loaded_context': '', 'work': work}

    async def classify(**kwargs):
        assert json.loads(kwargs['input'][0]['text'])['work'] == work
        if decision['action'] in ('task', 'steer'):
            return SimpleNamespace(parsed={**decision, 'evidence': 'Tolong fokus ke tulisan di pojok'})
        return SimpleNamespace(parsed=decision)

    gateway = FakeGateway()
    result = asyncio.run(live_agent._run(
        tmp_path, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
        llm=SimpleNamespace(acomplete_structured=classify),
    ))
    assert result == ({'status': status, 'admission_id': decision['admission_id']}
                      if status in {'follow', 'steer', 'task', 'dm_only', 'delivery_status', 'speak_result', 'code_changes'}
                      else {'status': status})
    assert not gateway.calls


@pytest.mark.parametrize(('utterance', 'decision', 'status'), [
    ('Kirimkan hasilnya dalam PDF dan ZIP, ya.',
     {'action': 'task', 'evidence': 'Kirimkan hasilnya dalam PDF dan ZIP'}, 'task'),
    ('Sudah dikirim PDF-nya?', {'action': 'delivery_status', 'admission_id': 'a' * 32}, 'delivery_status'),
    ('Laisse le résultat dans le chat.', {'action': 'dm_only', 'admission_id': 'a' * 32}, 'dm_only'),
])
def test_file_delivery_and_status_are_distinct_intents(tmp_path, utterance, decision, status):
    work = [{'admission_id': 'a' * 32, 'session_id': 'session-a', 'status': 'completed',
             'text': 'Buat laporan.', 'kind': 'text', 'seq': 1}]

    async def classify(**kwargs):
        assert "normal result, even if the job has already finished" in kwargs['instructions']
        assert 'explicit request to resend' in kwargs['instructions']
        assert 'send the file again just to answer its status' in kwargs['instructions']
        data = json.loads(kwargs['input'][0]['text'])
        assert data['current'] == utterance and data['work'] == work
        return SimpleNamespace(parsed=decision)

    gateway = FakeGateway()
    result = asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'route', 'current': utterance, 'work': work,
                                'proposal': '', 'previous': '',
                                'loaded_context': 'Laporan sudah tersedia.'},
        resolve=gateway.resolve, connect=gateway.connect, llm=SimpleNamespace(acomplete_structured=classify),
    ))
    assert result == {'status': status, **{key: value for key, value in decision.items()
                                         if key == 'admission_id'}}
    assert not gateway.calls


def test_one_voice_task_uses_stock_whatsapp_delivery_for_mixed_files(tmp_path, monkeypatch):
    pytest.importorskip('hermes_state')
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms import base
    from gateway.session import SessionSource, build_session_key
    from gateway.session_ingress import deliver_response
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    monkeypatch.setattr(base, 'MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    names = ['Laporan.pdf', 'Arsip.zip', 'Data tabel.csv', 'Cuplikan.mp4']
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.touch()
    output = '\n'.join(f'MEDIA:"{path}"' for path in [*paths, paths[0]])
    gateway = FakeGateway()
    gateway.receipt['result']['final_response'] = output
    result = run(tmp_path, {**FIRST, 'text': 'Buat dan kirim empat lampirannya ke sini.'}, gateway)
    assert result['status'] == 'completed'
    assert result['output'] == 'Periksa DM untuk memastikan lampirannya diterima.'
    assert [method for method, _ in gateway.calls] == ['session.resume', 'prompt.submit', 'prompt.receipt']
    submitted = gateway.calls[1][1]
    assert submitted['text'] == 'Buat dan kirim empat lampirannya ke sini.'
    assert 'untuk setiap file yang diminta, apa pun jenisnya' in submitted['voice_context']
    assert 'jangan bacakan path' in submitted['voice_context']

    adapter = WhatsAppAdapter(PlatformConfig(enabled=True))
    sends = []

    async def bridge_unavailable():
        return None

    async def post_bridge(endpoint, payload, *, timeout):
        assert endpoint == 'send-media' and timeout == 120
        sends.append(payload)
        return base.SendResult(success=True)

    monkeypatch.setattr(adapter, '_bridge_unavailable', bridge_unavailable)
    monkeypatch.setattr(adapter, '_post_bridge_message', post_bridge)
    source = SessionSource(platform=Platform.WHATSAPP, chat_id='1234567890@s.whatsapp.net', chat_type='dm')
    event = base.MessageEvent(text='', message_type=base.MessageType.TEXT, source=source)
    assert asyncio.run(deliver_response(adapter, event, build_session_key(source), output))
    assert [(payload['filePath'], payload['mediaType'], payload.get('fileName')) for payload in sends] == [
        (str(path), kind, path.name if kind == 'document' else None)
        for path, kind in zip(paths, ['document', 'document', 'document', 'video'])
    ]
    assert {payload['chatId'] for payload in sends} == {source.chat_id}


def test_spoken_result_reuses_native_image_and_file_extraction(tmp_path):
    pytest.importorskip('hermes_state')
    file = tmp_path / 'Catatan.pdf'
    file.touch()
    result = live_agent._spoken_result(f'Ini hasilnya.\n![Diagram](https://example.org/diagram.png)\n{file}')
    assert result == 'Periksa DM untuk memastikan lampirannya diterima.\nIni hasilnya.'


def test_call_uses_the_dm_whose_context_it_opened(tmp_path, monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr(live_agent, '_read_persona', lambda _home: None)

    context = run(tmp_path, {'action': 'context'}, gateway)
    assert context == {'status': 'ready', 'session_id': 'session-a',
                       'history': [{'role': 'user', 'text': 'Context from session-a'}],
                       'history_cursor': 11}
    assert run(tmp_path, FIRST, gateway) == {
        'status': 'completed',
        'output': 'Hasil asli Hermes.',
        'receipt': {'session_id': 'session-a', 'admission_id': 'admission-1'},
    }
    follow_up = run(tmp_path, FOLLOW_UP, gateway)
    assert follow_up['receipt'] == {'session_id': 'session-a', 'admission_id': 'admission-2'}
    submitted = [params for method, params in gateway.calls if method == 'prompt.submit']
    assert [params['text'] for params in submitted] == [FIRST['text'], FOLLOW_UP['text']]
    assert not list(tmp_path.iterdir())


def test_voice_task_can_reference_a_spoken_exchange_while_the_dm_is_busy(tmp_path):
    gateway = FakeGateway()
    spoken = '[{"user":"Judulnya Laut Biru.","assistant":"Aku ingat."}]'
    assert run(tmp_path, {**FIRST, 'text': 'Buat catatan untuk cerita itu.',
                          'voice_context': spoken}, gateway)['status'] == 'completed'
    recorded = next(params for method, params in gateway.calls if method == 'prompt.submit')
    assert recorded['voice_context'].endswith(spoken)
    assert 'MEDIA:' in recorded['voice_context']
    assert spoken not in recorded['text']


@pytest.mark.parametrize(('direction', 'context'), [
    ('inbound', 'Pemilik akun yang meneleponmu. '),
    ('outbound', 'Kamu yang menelepon pemilik akun. '),
])
def test_delegation_preserves_call_direction_in_the_same_whatsapp_session(tmp_path, direction, context):
    gateway = FakeGateway()

    assert run(tmp_path, {**FIRST, 'direction': direction}, gateway)['status'] == 'completed'
    submitted = next(params for method, params in gateway.calls if method == 'prompt.submit')
    assert submitted['session_id'] == FIRST['session_id']
    assert submitted['text'] == FIRST['text']
    assert context.rstrip() in submitted['voice_context']
    assert 'MEDIA:' in submitted['voice_context']


def test_a_new_call_reads_the_new_dm_session_without_submitting_into_it_from_the_old_call(tmp_path):
    gateway = FakeGateway()
    original = run(tmp_path, {'action': 'context'}, gateway)
    gateway.active_session = 'session-b'

    assert run(tmp_path, {**FIRST, 'session_id': original['session_id']}, gateway) == {'status': 'session_changed'}
    assert not gateway.admissions
    updated = run(tmp_path, {'action': 'context'}, gateway)
    assert updated['history'] == [{'role': 'user', 'text': 'Context from session-b'}]
    assert run(tmp_path, {**FIRST, 'session_id': updated['session_id']}, gateway)['receipt']['session_id'] == 'session-b'


def test_context_does_not_publish_history_when_route_changes_during_the_read(tmp_path):
    gateway = FakeGateway()

    def read_history(_home, _session_id):
        gateway.active_session = 'session-b'
        return [{'role': 'user', 'text': 'Old chat'}], 11

    assert asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'context'}, resolve=gateway.resolve,
        connect=gateway.connect, read_history=read_history,
    )) == {'status': 'session_changed'}


@pytest.mark.parametrize(('soul', 'expected'), [
    (None, 'Native Hermes identity'),
    ('', 'Native Hermes identity'),
    ('[BLOCKED: SOUL.md contained potential prompt injection.]', 'Native Hermes identity'),
    ('Persona text. ' * 400, ('Persona text. ' * 400)[:4000].rstrip()),
])
def test_context_uses_only_the_verified_profiles_persona(tmp_path, monkeypatch, soul, expected):
    home, other_home = tmp_path / 'verified', tmp_path / 'other'
    home.mkdir()
    other_home.mkdir()
    (other_home / 'SOUL.md').write_text('Other profile persona', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(other_home))
    if soul is not None:
        (home / 'SOUL.md').write_text(soul, encoding='utf-8')

    loaded = []

    def load_soul_md(*, home_override):
        loaded.append(home_override)
        return (home_override / 'SOUL.md').read_text(encoding='utf-8')

    monkeypatch.setitem(sys.modules, 'agent.prompt_builder',
                        SimpleNamespace(load_soul_md=load_soul_md, DEFAULT_AGENT_IDENTITY='Native Hermes identity'))
    gateway = FakeGateway()

    context = run(home, {'action': 'context'}, gateway)

    assert context['status'] == 'ready'
    assert context.get('persona') == expected
    assert loaded == ([] if soul is None else [home])
    assert not gateway.admissions


def test_voice_context_includes_the_active_summary_and_conversation():
    history = live_agent._voice_history([
        {'role': 'user', 'content': 'Old message ' + 'x' * 1200},
        {'role': 'system', 'content': 'Private system instruction'},
        {'role': 'tool', 'content': 'Raw tool data'},
        {'role': 'user', 'content': [{'type': 'image', 'url': 'data:image/png;base64,private'}]},
        {'role': 'assistant', 'content': 'Internal summary', '_compressed_summary': True},
        {'role': 'assistant', 'content': 'Internal note', 'display_kind': 'internal_notification'},
        {'role': 'user', 'content': 'Chat message'},
        {'role': 'user', 'content': 'Earlier call', 'display_kind': live_agent.VOICE_CALL_KIND},
        {'role': 'assistant', 'content': 'Chat reply'},
    ])
    assert history[0] == {'role': 'user', 'text': 'Old message ' + 'x' * 1200}
    assert history[1:] == [
        {'role': 'user', 'text': 'Foto terlampir di chat WhatsApp.'},
        {'role': 'assistant', 'text': 'Internal summary', 'summary': True},
        {'role': 'user', 'text': 'Chat message'},
        {'role': 'user', 'text': 'Earlier call', 'source': 'whatsapp_call'},
        {'role': 'assistant', 'text': 'Chat reply'},
    ]


def test_context_updates_use_the_active_chat_without_reloading_persona_or_submitting(tmp_path, monkeypatch):
    gateway = FakeGateway()
    gateway.active_session = 'compression-tip'
    gateway.logical_session = 'session-a'
    updates = [{'role': 'user', 'text': 'Pesan baru dari chat.'},
               {'role': 'assistant', 'text': 'Balasan baru di chat.'}]
    reads = []

    def read_updates(home, session_id, cursor):
        reads.append((home, session_id, cursor))
        return updates, 14

    def unexpected_persona(*_args):
        pytest.fail('A history update must not load the persona')

    monkeypatch.setattr(live_agent, '_read_persona', unexpected_persona)
    request = {'action': 'context_updates', 'session_id': 'session-a', 'after_row_id': 11}

    def poll(read=read_updates):
        return asyncio.run(live_agent._run(
            tmp_path, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
            read_context_updates=read,
        ))

    assert poll() == {'status': 'ready', 'session_id': 'session-a',
                      'history': updates, 'history_cursor': 14}
    assert reads == [(tmp_path, 'compression-tip', 11)]
    assert [method for method, _ in gateway.calls] == ['session.resume']

    def reset_during_read(home, session_id, cursor):
        gateway.active_session = 'session-b'
        gateway.logical_session = None
        return read_updates(home, session_id, cursor)

    assert poll(reset_during_read) == {'status': 'session_changed'}
    assert poll() == {'status': 'session_changed'}
    assert len(reads) == 2
    assert not gateway.admissions


def test_context_watcher_follows_the_same_dm_and_catches_up_after_reconnect(tmp_path, monkeypatch):
    async def exercise():
        emitted, calls = [], []
        route = 'compression-tip'
        work, messages = [], []
        connections = 0
        events = asyncio.Queue()

        def resolve(_home, _owner):
            return route

        def read_updates(_home, sid, cursor):
            assert sid == 'compression-tip'
            observed = [(row_id, text) for row_id, text in messages if row_id > cursor][:1]
            return ([{'role': 'assistant', 'text': text} for _, text in observed],
                    observed[-1][0] if observed else cursor)

        def read_work(_home, _owner, sid):
            assert sid == 'session-a'
            return work.copy()

        def wake(event_type):
            events.put_nowait({'method': 'event', 'params': {'session_id': 'session-a', 'type': event_type}})

        async def rpc(method, **params):
            calls.append((method, params['session_id']))
            return {'session_id': 'session-a' if params['session_id'] == 'compression-tip' else 'session-b'}

        @asynccontextmanager
        async def connect():
            nonlocal connections
            connections += 1
            if connections == 2:
                messages.extend([(13, 'Pesan saat tersambung ulang.'), (14, 'Halaman berikutnya.')])
            yield SimpleNamespace(rpc=rpc, events=events)

        def emit(update):
            nonlocal route
            emitted.append(update)
            if len(emitted) == 1:
                work.append(PHOTO_WORK)
                wake('session.info')
            elif len(emitted) == 2:
                work[:] = [{**PHOTO_WORK, 'status': 'completed'}]
                messages.append((12, 'Hasil foto yang sama.'))
                wake('message.complete')
            elif len(emitted) == 3:
                events.put_nowait(ValueError('gateway disconnected'))
            elif len(emitted) == 6:
                route = 'session-b'

        monkeypatch.setattr(live_agent, 'CONTEXT_ROUTE_CHECK_SECONDS', 0.02)
        monkeypatch.setattr(live_agent, 'CONTEXT_RECONNECT_SECONDS', 0)
        await asyncio.wait_for(live_agent._watch_context(
            tmp_path, '1234567890', {'session_id': 'session-a', 'after_row_id': 11},
            resolve=resolve, emit=emit, connect=connect, read_context_updates=read_updates, read_work=read_work,
        ), 2)

        assert [(update['history_cursor'], update['history'])
                for update in emitted if update['status'] == 'ready'] == [
            (11, []),
            (11, []),
            (12, [{'role': 'assistant', 'text': 'Hasil foto yang sama.'}]),
            (13, [{'role': 'assistant', 'text': 'Pesan saat tersambung ulang.'}]),
            (14, [{'role': 'assistant', 'text': 'Halaman berikutnya.'}]),
        ]
        assert emitted[1]['work'] == [PHOTO_WORK]
        assert emitted[2]['work'] == [{**PHOTO_WORK, 'status': 'completed'}]
        assert emitted[3] == {'status': 'reconnecting'}
        assert emitted[-1] == {'status': 'session_changed'}
        assert connections == 3
        assert calls == [('session.resume', 'compression-tip'), ('session.resume', 'compression-tip'),
                         ('session.resume', 'session-b')]

    asyncio.run(exercise())


def test_voice_step_redacts_registered_secret_before_truncation(monkeypatch):
    from agent import redact_vault_values

    monkeypatch.setattr(redact_vault_values, '_VAULT_REDACTION_VALUES', {})
    secret = 'northwind-long-spelling-for-synthetic-registry-fixture-only'
    redact_vault_values.register_vault_redaction_value(secret)
    prefix = 'Lorem ipsum ' * 8
    assert len(prefix) < 120 < len(prefix + secret)
    safe = live_agent._voice_step_text(prefix + secret)
    assert safe.startswith(prefix)
    assert '«redacted-vault-secret»' in safe and secret[:14] not in safe


def test_voice_step_rejects_url_with_opaque_one_use_query_value():
    assert live_agent._voice_step_text('Use https://example.invalid/recover?k=syntheticoneusecode') == ''


def test_completed_patch_pushes_hunks_and_reveals_only_safe_code_on_follow():
    from tools.file_operations_common import PatchResult
    from gateway.run_turn_progress import _tool_complete_payload

    path = '/private/project/render.py'
    diff = (f'--- a/{path}\n+++ b/{path}\n@@ -20,2 +20,3 @@\n'
            ' def render(frame):\n-    return frame\n+    setup("private-value")\n+    return encode(frame)\n')
    result = PatchResult(success=True, files_modified=[path], diff=diff).to_dict()
    payload = _tool_complete_payload('patch-1', 'patch', {'path': path}, json.dumps(result), is_error=False)
    event = {'type': 'tool.complete', 'session_id': PHOTO_WORK['session_id'],
             'admission_id': PHOTO_WORK['admission_id'], 'execution_generation': 7, 'seq': 1, 'payload': payload}
    pushed = live_agent._voice_tool_progress(event, PHOTO_WORK['session_id'], [PHOTO_WORK])
    assert pushed['file_change'] == {
        'effect': 'patched', 'files_modified': ['render.py'],
        'hunks': [{'file': 'render.py', 'old_start': 20, 'old_count': 2, 'new_start': 20, 'new_count': 3,
                   'first_removed_line': 21, 'first_added_line': 21}],
    }
    followed = live_agent._voice_tool_progress(event, PHOTO_WORK['session_id'], [PHOTO_WORK], include_code=True)
    change = live_agent._public_tool_step(followed)['file_change']
    assert change == {**pushed['file_change'],
                      'code_excerpt': [{'file': 'render.py', 'line': 21, 'change': 'removed', 'text': 'return frame'},
                                       {'file': 'render.py', 'line': 22, 'change': 'added', 'text': 'return encode(frame)'}]}
    assert '/private/' not in str(change) and 'private-value' not in str(change)
    assert live_agent._voice_tool_progress(event, 'another-session', [PHOTO_WORK], include_code=True) is None


@pytest.mark.parametrize('tool, result, expected', [
    ('patch', {'success': True, 'no_change': True, 'files_modified': ['/private/render.py']},
     {'phase': 'completed', 'file_change': {'effect': 'no_change'}}),
    ('patch', {'truncated': True, 'output': 'partial-data'}, {'phase': 'completed'}),
    ('write_file', {'error': 'Write refused.', 'bytes_written': 0},
     {'phase': 'failed', 'finding': 'Write refused.'}),
    ('write_file', {'bytes_written': 0, 'resolved_path': '/private/empty.py', 'verified': True},
     {'phase': 'completed', 'file_change': {'effect': 'written', 'bytes_written': 0, 'verified': True,
                                             'files_modified': ['empty.py']}}),
    ('write_file', {'bytes_written': False}, {'phase': 'completed'}),
])
def test_file_progress_uses_only_the_tool_result(tool, result, expected):
    payload = {'result': json.dumps(result), 'args': {'content': 'private-input'}, 'is_error': False}
    assert live_agent._tool_result_fields(payload, tool) == expected


@pytest.mark.parametrize('verified, offset, included', [(True, 0, True), (False, 0, False), (True, 1, False)])
def test_whole_file_query_reports_only_lines_from_intact_verified_write(verified, offset, included):
    from tools.file_operations_common import WriteResult

    content = 'password = private_value\nretries = 3\nprint("secret-literal")\nreturn retries + 1\n'
    result = WriteResult(bytes_written=len(content.encode()) + offset, verified=verified).to_dict()
    result['resolved_path'] = '/private/render.py'
    payload = {'result': json.dumps(result), 'args': {'content': content}, 'is_error': False}
    summary = live_agent._tool_result_fields(payload, 'write_file')['file_change']
    assert 'code_excerpt' not in summary
    change = live_agent._tool_result_fields(payload, 'write_file', include_code=True)['file_change']
    assert 'hunks' not in change and 'private_value' not in json.dumps(change)
    examples = change.get('code_excerpt', [])
    assert [(e['line'], e['change'], e['text']) for e in examples] == (
        [(2, 'written', 'retries = 3'), (4, 'written', 'return retries + 1')] if included else [])


@pytest.mark.parametrize(('ask_code', 'epoch', 'has_steps'), [(False, 4, False), (True, 4, True), (True, 3, False)])
def test_finished_work_replays_edits_only_on_explicit_scoped_detail_request(tmp_path, ask_code, epoch, has_steps):
    job = PHOTO_WORK['admission_id']
    file = 'render.py'
    diff = ('--- a/render.py\n+++ b/render.py\n@@ -1 +1,3 @@\n-tries = 2\n+tries = 3\n'
            '+secret_token = private_value\n+return retries + 1\n')
    result = json.dumps({'success': True, 'files_modified': [file], 'diff': diff})
    def event(seq, *, session='session-a', admission=job, generation=8, replay_epoch='stream'):
        return {'type': 'tool.complete', 'session_id': session, 'admission_id': admission,
                'seq': seq, 'execution_generation': generation, 'replay_epoch': replay_epoch,
                'payload': {'name': 'patch', 'tool_id': f'patch-{seq}', 'args': {}, 'result': result}}

    class FinishedGateway(FakeGateway):
        async def rpc(self, method, **params):
            if method == 'session.events.since':
                self.calls.append((method, params))
                return {'events': [event(1), event(2, session='other'), event(3, admission='b' * 32),
                                   event(4, generation=9), event(5, replay_epoch='old')]}
            response = await super().rpc(method, **params)
            if method == 'session.resume':
                response.update(running=True, execution_generation=9, replay_epoch='stream', last_sequence=5)
            return response

    gateway = FinishedGateway()
    gateway.admissions['native'] = {'admission_id': job, 'ref': {'session_id': 'session-a'}}
    gateway.receipt.update(execution_generation=8, authority_epoch=epoch)
    response = asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'observe_work', 'session_id': 'session-a',
                                 'admission_id': job, 'include_progress': True, 'include_code': ask_code},
        resolve=gateway.resolve, connect=gateway.connect, read_work=lambda *_: [{**PHOTO_WORK, 'status': 'completed'}],
    ))
    assert response['status'] == 'completed'
    assert ('tool_steps' in response) is has_steps
    assert any(method == 'session.events.since' for method, _ in gateway.calls) is has_steps
    if has_steps:
        step, = response['tool_steps']
        assert [(line['line'], line['text']) for line in step['file_change']['code_excerpt']] == [
            (1, 'tries = 2'), (1, 'tries = 3'), (3, 'return retries + 1')]
        assert 'private_value' not in json.dumps(step)


@pytest.mark.parametrize('tool_result, expected', [
    ({'success': False, 'exit_code': 1, 'stderr': 'private-debug-log'},
     {'phase': 'failed', 'exit_code': 1}),
    ({'status': 'failed', 'request': {'state': 'rejected'}, 'success': False,
      'error': {'message': 'Pembuatan ditolak.'}, 'stdout': 'private-debug-log'},
     {'phase': 'failed', 'finding': 'Pembuatan ditolak.'}),
    ({'success': False, 'error': {'message': 'See /private/work/secret.log'}, 'output': 'private-debug-log'},
     {'phase': 'failed'}),
])
def test_browser_tool_failure_uses_bounded_structured_evidence(tool_result, expected):
    event = {
        'type': 'tool.complete', 'session_id': PHOTO_WORK['session_id'], 'admission_id': PHOTO_WORK['admission_id'],
        'execution_generation': 7, 'seq': 1, 'payload': {'tool_id': 'browser-call', 'name': 'browser_exec',
            'is_error': False, 'result': json.dumps(tool_result), 'context': 'private-debug-log',
            'args': {'code': 'print("private-debug-log")'}},
    }
    update = live_agent._voice_tool_progress(event, PHOTO_WORK['session_id'], [PHOTO_WORK])
    assert {key: update[key] for key in ('phase', 'exit_code', 'finding') if key in update} == expected
    assert not update['detail'] and 'private-debug-log' not in json.dumps(update)


@pytest.mark.parametrize('output, failed_medium', [
    ('[]\nwarning\nGagal\nPembuatan audio gagal. Coba perintah lain atau kirim masukan.', 'audio'),
    ('[]\nwarning\nGagal\nPembuatan video gagal. Coba lagi.', 'video'),
    ('[]\nPembuatan video gagal disebut dalam petunjuk.', None),
    ('[]\nGagal\nLanjutkan pemeriksaan.\nPembuatan audio gagal disebut dalam petunjuk.', None),
])
def test_browser_successful_page_read_keeps_separate_application_failure(output, failed_medium):
    tool_result = {'success': True, 'exit_code': 0, 'output': output + '\n' + 'private-browser-output'}
    event = {
        'type': 'tool.complete', 'session_id': PHOTO_WORK['session_id'], 'admission_id': PHOTO_WORK['admission_id'],
        'execution_generation': 7, 'seq': 1, 'payload': {'tool_id': 'page-read', 'name': 'browser_exec',
            'is_error': False, 'result': json.dumps(tool_result)},
    }
    update = live_agent._voice_tool_progress(event, PHOTO_WORK['session_id'], [PHOTO_WORK])
    assert update['phase'] == 'completed' and 'exit_code' not in update
    if failed_medium:
        assert update['application_outcome'] == 'failed'
        assert update['finding'] == f'Halaman melaporkan pembuatan {failed_medium} gagal.'
    else:
        assert 'application_outcome' not in update and 'finding' not in update
    assert 'private-browser-output' not in json.dumps(update)


@pytest.mark.parametrize('raw_percent, expected_percent', [(37, 37), ('37%', None), (True, None), (131, None)])
def test_native_tool_progress_preserves_only_valid_numeric_percent(raw_percent, expected_percent):
    event = {
        'type': 'tool.progress', 'session_id': PHOTO_WORK['session_id'], 'admission_id': PHOTO_WORK['admission_id'],
        'execution_generation': 7, 'seq': 1, 'payload': {'tool_id': 'video-call', 'name': 'video_generate',
            'progress': 37, 'total': 100, 'percent': raw_percent, 'message': 'See /private/work/secret.log',
            'stdout': 'private-debug-log'},
    }
    update = live_agent._voice_tool_progress(event, PHOTO_WORK['session_id'], [PHOTO_WORK])
    assert update['phase'] == 'started' and update.get('percent') == expected_percent
    assert 'finding' not in update
    assert 'secret.log' not in json.dumps(update) and 'private-debug-log' not in json.dumps(update)


def test_context_watcher_delivers_verified_tool_progress_after_initial_ready(tmp_path):
    async def exercise():
        events, emitted = asyncio.Queue(), []
        route = 'compressed-a'
        owner_admission = PHOTO_WORK['admission_id']
        secret = 'unrelated-secret-argument'

        def tool(seq, kind, name, tool_id, *, session_id='session-a', admission_id=owner_admission, **args):
            return {'seq': seq, 'type': kind, 'session_id': session_id, 'admission_id': admission_id,
                    'execution_generation': 9, 'replay_epoch': 'epoch-a',
                    'payload': {'tool_id': tool_id, 'name': name, 'args': args,
                                'context': secret, 'result': secret, 'is_error': name == 'terminal'}}

        replay_start = tool(180, 'tool.start', 'write_file', 'write', path='/secret/path/note.txt')
        replay_complete = tool(183, 'tool.complete', 'write_file', 'write', path='/secret/path/note.txt')
        skill_start = tool(184, 'tool.start', 'skill_view', 'skill', name_override=secret)
        skill_start['payload']['args']['name'] = 'story-research'
        live_complete = tool(185, 'tool.complete', 'skill_view', 'skill')
        foreign_tool = tool(186, 'tool.start', 'web_search', 'foreign', session_id='session-b')
        unowned_tool = tool(187, 'tool.start', 'web_search', 'unowned', admission_id='b' * 32)
        terminal_failed = tool(188, 'tool.complete', 'terminal', 'shell', command=secret)
        old_route_tool = tool(189, 'tool.start', 'web_search', 'stale')

        async def rpc(method, **params):
            if method == 'session.resume':
                if route != 'compressed-a':
                    return {'session_id': 'session-b'}
                assert params['session_id'] == route
                return {'session_id': 'session-a', 'running': True, 'execution_generation': 9,
                        'last_sequence': 184, 'replay_epoch': 'epoch-a', 'pending': [
                            {'admission_id': owner_admission, 'status': 'started', 'execution_generation': 9},
                        ]}
            assert method == 'session.events.since'
            assert emitted[0]['status'] == 'ready'
            assert params['session_id'] == 'session-a' and params['last_sequence'] == 88
            return {'events': [replay_start, replay_complete, skill_start]}

        @asynccontextmanager
        async def connect():
            yield SimpleNamespace(rpc=rpc, events=events)

        def emit(update):
            nonlocal route
            emitted.append(update)
            if update.get('seq') == 184:
                for frame in (skill_start, live_complete, foreign_tool, unowned_tool, terminal_failed, old_route_tool):
                    events.put_nowait({'method': 'event', 'params': frame})
            if update.get('seq') == 188:
                route = 'session-b'

        await asyncio.wait_for(live_agent._watch_context(
            tmp_path, '1234567890', {'session_id': 'session-a', 'after_row_id': 11},
            resolve=lambda *_: route, emit=emit, connect=connect,
            read_context_updates=lambda _home, _sid, cursor: ([], cursor),
            read_work=lambda *_: [PHOTO_WORK],
        ), 2)

        assert [update['status'] for update in emitted] == ['ready', *(['progress'] * 4), 'session_changed']
        progress = emitted[1:-1]
        assert [(item['seq'], item['tool_name'], item['phase'], item['detail']) for item in progress] == [
            (183, 'write_file', 'completed', 'note.txt'),
            (184, 'skill_view', 'started', 'story-research'),
            (185, 'skill_view', 'completed', ''),
            (188, 'terminal', 'failed', ''),
        ]
        assert all(item['admission_id'] == owner_admission and item['generation'] == 9 for item in progress)
        assert secret not in json.dumps(progress) and '/secret/path/' not in json.dumps(progress)

    asyncio.run(exercise())


def test_context_watcher_projects_only_complete_safe_commentary_from_the_active_admission(tmp_path):
    async def exercise():
        events, emitted = asyncio.Queue(), []
        admission_id = PHOTO_WORK['admission_id']
        physical = 'compressed-tip'

        def event(seq, kind, *, text='', offset=0, generation=9, owner=admission_id, sid='session-a'):
            return {'method': 'event', 'params': {'type': kind, 'seq': seq, 'session_id': sid,
                    'admission_id': owner, 'execution_generation': generation, 'replay_epoch': 'epoch-a',
                    'payload': {'text': text, 'offset': offset, 'segment': 0, 'turn_id': 'native-turn'}}}

        async def rpc(method, **params):
            assert method == 'session.resume'
            return {'session_id': 'session-a' if params['session_id'] == 'compressed-tip' else 'session-b',
                    'replay_epoch': 'epoch-a'}

        @asynccontextmanager
        async def connect():
            yield SimpleNamespace(rpc=rpc, events=events)

        first = '📷 Aku sudah '
        next_offset = len(first.encode('utf-16-le')) // 2
        second = 'menemukan sumber. Aku mengecek '
        third_offset = next_offset + len(second)
        third = 'detail. MEDIA: /Users/private/my file. Jangan diucapkan. '

        def emit(update):
            nonlocal physical
            emitted.append(update)
            if len(emitted) == 1:
                for frame in (
                    event(10, 'message.start'),
                    event(11, 'message.delta', text=first),
                    event(12, 'message.delta', text='Sesi lain. ', offset=next_offset, sid='session-other'),
                    event(13, 'message.delta', text='Pekerjaan lain. ', offset=next_offset, owner='b' * 32),
                    event(14, 'message.delta', text='Giliran lama. ', offset=next_offset, generation=8),
                    event(15, 'message.delta', text=second, offset=next_offset),
                    event(15, 'message.delta', text=second, offset=next_offset),
                    event(16, 'message.delta', text=third, offset=third_offset),
                    event(17, 'message.delta', text='Susulan rahasia. ', offset=third_offset + len(third)),
                    event(18, 'message.complete'),
                    event(19, 'message.start', generation=10),
                    event(20, 'message.delta', text='Sudah berganti sesi. ', generation=10),
                ):
                    events.put_nowait(frame)
            if update.get('text') == 'Aku mengecek detail.':
                physical = 'session-b'

        await asyncio.wait_for(live_agent._watch_context(
            tmp_path, '1234567890', {'session_id': 'session-a', 'after_row_id': 11},
            resolve=lambda *_: physical, emit=emit, connect=connect,
            read_context_updates=lambda _home, _sid, cursor: ([], cursor),
            read_work=lambda *_: [PHOTO_WORK],
        ), 2)

        assert emitted[1:-1] == [
            {'status': 'commentary', 'session_id': 'session-a', 'admission_id': admission_id,
             'generation': 9, 'seq': 15, 'text': '📷 Aku sudah menemukan sumber.'},
            {'status': 'commentary', 'session_id': 'session-a', 'admission_id': admission_id,
             'generation': 9, 'seq': 16, 'text': 'Aku mengecek detail.'},
        ]
        assert emitted[-1] == {'status': 'session_changed'}

    asyncio.run(exercise())


def test_context_updates_skip_voice_mirrors_and_compaction_copies(tmp_path):
    from contextlib import closing
    SessionDB = pytest.importorskip('hermes_state').SessionDB

    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('session-a', 'whatsapp')
        db.append_message('session-a', 'user', 'Pesan sebelum telepon.')
        _, cursor = live_agent._read_history(tmp_path, 'session-a')
        carried = db.get_messages_as_conversation('session-a')
        db.archive_and_compact('session-a', carried, tail_count=1)
        db.append_message('session-a', 'user', 'Sudah kukirim lewat chat.', message_uid='sent-in-dm')
        db.append_message('session-a', 'user',
                          'Keterangan foto.\n[Image attached at: /fixture/private.jpg]\n[screenshot]',
                          message_uid='photo-in-dm')
        db.append_message('session-a', 'assistant', 'Aku sudah melihatnya.', message_uid='dm-reply')
        last = db.append_message('session-a', 'assistant', 'Cermin panggilan.',
                                 display_kind=live_agent.VOICE_CALL_KIND)

    assert live_agent._read_context_updates(tmp_path, 'session-a', cursor) == ([
        {'role': 'user', 'text': 'Sudah kukirim lewat chat.', '_message_refs': ['sent-in-dm']},
        {'role': 'user', 'text': 'Foto terlampir di chat WhatsApp.\n\nKeterangan foto.',
         '_message_refs': ['photo-in-dm']},
        {'role': 'assistant', 'text': 'Aku sudah melihatnya.', '_message_refs': ['dm-reply']},
    ], last)
    assert live_agent._read_context_updates(tmp_path, 'session-a', last) == ([], last)
    history, _ = live_agent._read_history(tmp_path, 'session-a')
    assert any({'sent-in-dm', 'photo-in-dm'}.issubset(row.get('_message_refs', ())) for row in history)


def test_voice_history_keeps_attachment_content_and_removes_native_cache_paths():
    rows = [
        {'role': 'user', 'content': ("[The user sent an image~ Here's what I can see:\nPayung merah [A].]\n"
                                    "[If you need a closer look, use vision_analyze with "
                                    "image_url: /fixture/private.jpg ~]")},
        {'role': 'user', 'content': ("[The user sent a text document: 'Catatan.pdf'. Its content has been included "
                                    'below. The file is also saved at: /fixture/private.pdf]\n\n'
                                    '[Content of Catatan.pdf]:\nKonten yang dikirim.\n\nBaca bagian atas.')},
        {'role': 'user', 'content': ('[Image attached at: /fixture/native.jpg]\n\n'
                                    'Lihat ini.\n[screenshot detail=high]')},
        {'role': 'user', 'content': ('[User sent audio: /fixture/native.mp3]\n'
                                    '[User sent a video: /fixture/native.mp4]\n\nPutar ini.')},
        {'role': 'user', 'content': '[User sent a file: /fixture/unknown.bin]'},
        {'role': 'user', 'content': '[screenshot]'},
    ]
    assert [entry['text'] for entry in live_agent._visible_history(rows, str.strip)] == [
        'Foto dari chat WhatsApp. Deskripsi gambar:\nPayung merah [A].',
        "[The user sent a text document: 'Catatan.pdf'.]\n\n"
        '[Content of Catatan.pdf]:\nKonten yang dikirim.\n\nBaca bagian atas.',
        'Foto terlampir di chat WhatsApp.\n\nLihat ini.',
        'Audio terlampir di chat WhatsApp.\nVideo terlampir di chat WhatsApp.\n\nPutar ini.',
        'File terlampir di chat WhatsApp.',
        'Foto terlampir di chat WhatsApp.',
    ]


def test_voice_history_projects_only_text_and_attachment_kinds_from_native_parts():
    rows = [
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'Keterangan dari chat.'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,private-bytes'}},
            {'type': 'input_image', 'image_url': 'https://example.invalid/private-original'},
            {'type': 'input_audio', 'input_audio': {'data': 'private-audio'}},
            {'type': 'input_file', 'file_data': 'private-file', 'filename': 'private/name.bin'},
            {'type': 'video', 'path': '/fixture/private-video.mp4'},
            {'type': 'input_text', 'text': 'Tolong lihat lampirannya.'},
            {'type': 'unknown', 'text': 'private-unknown'},
        ]},
        {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Lampiran terlihat.'}]},
        {'role': 'user', 'content': [{'type': 'image', 'source': {'path': '/fixture/another.jpg'}}]},
    ]
    history = live_agent._voice_history(rows)
    assert history == [
        {'role': 'user', 'text': ('Foto terlampir di chat WhatsApp.\nAudio terlampir di chat WhatsApp.\n'
                                  'File terlampir di chat WhatsApp.\nVideo terlampir di chat WhatsApp.\n\n'
                                  'Keterangan dari chat.\n\nTolong lihat lampirannya.')},
        {'role': 'assistant', 'text': 'Lampiran terlihat.'},
        {'role': 'user', 'text': 'Foto terlampir di chat WhatsApp.'},
    ]


def add_dm_work(db, session, admission_id, payload, *, status='started', outcome=None, principal='fixture'):
    db._execute_write(lambda conn: conn.execute(
        """INSERT INTO session_admissions
           (admission_id,request_id,principal_id,target_session_id,lineage_json,
            payload_json,payload_digest,intent,status,outcome,owner_epoch,generation)
           VALUES (?,?,?,?,'[]',?,'fixture','queue',?,?,?,?)""",
        (admission_id, admission_id, principal, session, json.dumps(payload), status, outcome,
         1 if status == 'started' else None, 1 if status == 'started' else None),
    ))


def test_work_list_is_scoped_to_owner_dm_and_plugin_voice_rows(tmp_path, monkeypatch):
    from contextlib import closing
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    monkeypatch.setattr('gateway.whatsapp_identity.expand_whatsapp_aliases', lambda _: {'1234567890', '2468'})

    source = {'platform': 'whatsapp', 'chat_type': 'dm', 'chat_id': '2468@lid', 'user_id': '1234567890'}
    native_photo = {'text': PHOTO_WORK['text'], 'native_text_v1': {
        'source': source, 'message_type': 'photo', 'media': [{'path': '/fixture/unnamed.jpg'}],
    }}
    principal = f'uid:{os.getuid()}'
    operator = {'principal_id': principal, 'profile_id': str(tmp_path.resolve()), 'session_id': 'session-a'}
    voice = {'text': live_agent.VOICE_DIRECTION_CONTEXT['inbound'] + live_agent.VOICE_PREFIX + 'Buat catatan.',
             'surface_v1': {'surface': 'voice-live'}, 'local_operator_v1': operator}
    previous_voice = {**voice, 'text': live_agent.VOICE_DIRECTION_CONTEXT['outbound']
                      + live_agent.PREVIOUS_VOICE_PREFIX + 'Kirim hasil sebelumnya.'}
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('session-a', 'whatsapp', chat_type='dm', chat_id='2468@lid', user_id='1234567890')
        db.create_session('session-b', 'whatsapp')
        db.end_session('session-a', 'compression')
        db.create_session('compression-tip', 'whatsapp', parent_session_id='session-a')
        add_dm_work(db, 'session-a', 'a' * 32, native_photo)
        add_dm_work(db, 'session-b', 'b' * 32, native_photo)
        add_dm_work(db, 'session-a', 'c' * 32,
                    {'text': 'Wrong owner', 'native_text_v1': {'source': {**source, 'user_id': '42'}}})
        add_dm_work(db, 'session-a', 'd' * 32, {'text': 'Native text', 'native_text_v1': {'source': source}},
                    status='terminal', outcome='completed')
        add_dm_work(db, 'session-a', 'e' * 32, voice, status='queued', principal=principal)
        add_dm_work(db, 'session-a', 'f' * 32, {**voice, 'text': 'Unrelated voice'}, principal=principal)
        add_dm_work(db, 'session-a', '0' * 32, {'text': 'Internal notification',
                    'native_text_v1': {'source': source, 'automation': {'owner': 'session-a'}}})
        add_dm_work(db, 'session-a', '1' * 32, previous_voice, status='terminal', outcome='completed',
                    principal=principal)

    gateway = FakeGateway()
    gateway.active_session = 'compression-tip'
    gateway.logical_session = 'session-a'
    work = run(tmp_path, {'action': 'context'}, gateway)['work']
    assert work == [
        {'admission_id': 'e' * 32, 'session_id': 'session-a', 'status': 'queued',
         'text': 'Buat catatan.', 'kind': live_agent.VOICE_CALL_KIND, 'seq': 5},
        PHOTO_WORK,
        {'admission_id': '1' * 32, 'session_id': 'session-a', 'status': 'completed',
         'text': 'Kirim hasil sebelumnya.', 'kind': live_agent.VOICE_CALL_KIND, 'seq': 8},
        {'admission_id': 'd' * 32, 'session_id': 'session-a', 'status': 'completed',
         'text': 'Native text', 'kind': 'text', 'seq': 4},
    ]
    assert live_agent._read_dm_work(tmp_path, '1234567890', 'session-a', 'c' * 32) == []
    assert live_agent._read_dm_work(tmp_path, '1234567890', 'session-a', '0' * 32) == []
    assert [method for method, _ in gateway.calls] == ['session.resume']


def test_prefix_free_voice_work_requires_stock_provenance_and_owner_dm(tmp_path, monkeypatch):
    from contextlib import closing
    from gateway.session_surface import admit_surface
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    monkeypatch.setattr('gateway.whatsapp_identity.expand_whatsapp_aliases',
                        lambda number: {'1234567890', '2468'} if number == '1234567890' else {number})
    gateway = FakeGateway()
    voice_text = 'Tolong kerjakan catatan yang tadi.'
    assert run(tmp_path, {**FIRST, 'text': voice_text}, gateway)['status'] == 'completed'
    submit = next(params for method, params in gateway.calls if method == 'prompt.submit')
    principal = f'uid:{os.getuid()}'
    operator = {'principal_id': principal, 'profile_id': str(tmp_path.resolve()), 'session_id': 'session-a'}
    payload = {'text': submit['text'], **admit_surface(submit), 'local_operator_v1': operator}
    rejected = [
        (principal, {'text': voice_text, 'surface_v1': {'surface': 'voice-live'}, 'local_operator_v1': operator}),
        (principal, {key: value for key, value in payload.items() if key != 'local_operator_v1'}),
        ('uid:10002', {**payload, 'local_operator_v1': {**operator, 'principal_id': 'uid:10002'}}),
        (principal, {**payload, 'local_operator_v1': {**operator, 'profile_id': str(tmp_path / 'another')}}),
        (principal, {**payload, 'local_operator_v1': {**operator, 'session_id': 'session-b'}}),
        (principal, {**payload, 'local_operator_v1': {**operator, 'principal_id': 'uid:10002'}}),
    ]
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('session-a', 'whatsapp', chat_type='dm', chat_id='2468@lid', user_id='1234567890')
        add_dm_work(db, 'session-a', 'a' * 32, payload, principal=principal)
        for index, (row_principal, row_payload) in enumerate(rejected):
            add_dm_work(db, 'session-a', str(index) * 32, row_payload, principal=row_principal)
    assert live_agent._read_dm_work(tmp_path, '1234567890', 'session-a') == [
        {'admission_id': 'a' * 32, 'seq': 1, 'session_id': 'session-a', 'status': 'started',
         'text': voice_text, 'kind': live_agent.VOICE_CALL_KIND},
    ]
    assert live_agent._read_dm_work(tmp_path, '42', 'session-a', 'a' * 32) == []


def test_attachment_metadata_and_new_question_use_only_the_exact_retained_file(tmp_path, monkeypatch):
    from contextlib import closing
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    from gateway import session_ingress_media

    media_root = tmp_path / 'private-cache'
    monkeypatch.setattr(session_ingress_media, '_media_root', lambda: media_root)
    monkeypatch.setattr('tools.credential_files.to_agent_visible_cache_path', lambda path: path)
    source = {'platform': 'whatsapp', 'chat_type': 'dm', 'chat_id': '1234567890', 'user_id': '1234567890'}
    files = [
        ('a', 'document', 'doc_0123456789ab_report.pdf', 'draft/Anggaran.pdf', 'APPLICATION/PDF'),
        ('b', 'document', 'doc_0123456789ab_assets.zip', '', 'application/zip'),
        ('c', 'audio', 'aud_0123456789ab.mp3', '', 'audio/mpeg; version=1'),
        ('d', 'video', 'vid_0123456789ab.mp4', '', 'video/mp4'),
        ('e', 'text', 'relay_media_0123456789ab.bin', '', ''),
        ('f', 'document', 'expired.pdf', '', 'application/pdf'),
    ]
    retained = {}
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('session-a', 'whatsapp')
        db.create_session('session-b', 'whatsapp')
        for key, kind, basename, name, mime in files:
            data = key.encode()
            digest = sha256(data).hexdigest()
            path = media_root / digest / basename
            if key != 'f':
                path.parent.mkdir(parents=True)
                path.write_bytes(data)
            retained[key] = str(path)
            media = [{'path': str(path), 'sha256': digest, 'size': len(data)}]
            payload = {'text': 'File chat.', 'native_text_v1': {
                'source': source, 'message_type': kind, 'media': media,
                'event': {'media_filenames': [name], 'media_types': [mime]},
            }}
            add_dm_work(db, 'session-a', key * 32, payload, status='terminal', outcome='completed')
        add_dm_work(db, 'session-b', '0' * 32, payload, status='terminal', outcome='completed')
        add_dm_work(db, 'session-a', '1' * 32, payload, status='queued')

    gateway = FakeGateway()
    context = run(tmp_path, {'action': 'context'}, gateway)
    assert live_agent._valid_work(context['work'])
    assert str(media_root) not in json.dumps(context)
    assert {item['admission_id'][0]: item.get('files') for item in context['work']} == {
        '1': [{'mime': 'application/pdf'}], 'a': [{'name': 'Anggaran.pdf', 'mime': 'application/pdf'}],
        'b': [{'name': 'assets.zip', 'mime': 'application/zip'}], 'c': [{'mime': 'audio/mpeg'}],
        'd': [{'mime': 'video/mp4'}], 'e': None, 'f': [{'mime': 'application/pdf'}],
    }
    assert all(item['attachment'] for item in context['work'])
    assert not live_agent._valid_work([{**context['work'][0], 'files': [{'name': '/tmp/private.pdf'}]}])
    assert not live_agent._valid_work([{**context['work'][0], 'files': [{'mime': 'text/plain\nextra'}]}])

    question = {**FIRST, 'text': 'Berapa jumlah anggaran di file itu?', 'voice_context': 'x' * 6000}
    assert run(tmp_path, {**question, 'attachment_admission_id': 'a' * 32}, gateway)['status'] == 'completed'
    submitted, = [params for method, params in gateway.calls if method == 'prompt.submit']
    assert submitted['text'] == question['text']
    assert 'MEDIA:' in submitted['voice_context']
    assert retained['a'] in submitted['voice_context'] and 'Anggaran.pdf' in submitted['voice_context']
    assert len(submitted['voice_context']) <= 6000 and 'attachments' not in submitted
    assert all(path not in submitted['voice_context'] for key, path in retained.items() if key != 'a')

    gateway.calls.clear()
    assert run(tmp_path, {**question, 'attachment_admission_id': 'f' * 32}, gateway) == {
        'status': 'attachment_unavailable',
    }
    for unavailable_id in ('0', '1'):
        assert run(tmp_path, {**question, 'attachment_admission_id': unavailable_id * 32}, gateway) == {
            'status': 'not_found',
        }
    assert {method for method, _ in gateway.calls} == {'session.resume'}


@pytest.mark.parametrize(('state', 'status'), [('started', 'started'), ('queued', 'queued'),
                                               ('approval', 'approval_required'), ('clarify', 'clarification_required'),
                                               ('stale', 'unknown'), ('terminal', 'completed'),
                                                   ('partial', 'failed'), ('unknown', 'unknown'),
                                                   ('reset', 'session_changed'), ('foreign', 'not_found')])
def test_observing_existing_whatsapp_photo_does_not_submit(tmp_path, state, status):
    class ObserverGateway(FakeGateway):
        async def rpc(self, method, **params):
            result = await super().rpc(method, **params)
            if method == 'prompt.receipt' and state == 'reset':
                self.active_session = 'session-b'
            return result

    gateway = ObserverGateway()
    if state in {'started', 'approval', 'clarify', 'stale'}:
        gateway.receipt = {'status': 'started', 'execution_generation': 14}
    if state in {'approval', 'clarify', 'stale'}:
        kind = 'approval' if state == 'stale' else state
        gateway.prompts = [{'kind': kind, 'execution_generation': 13 if state == 'stale' else 14}]
    if state == 'stale':
        gateway.receipt['authority_epoch'] = gateway.authority_epoch - 1
    if state == 'queued':
        gateway.receipt = {'status': 'queued'}
    if state == 'partial':
        gateway.receipt['result']['partial'] = True
    if state == 'unknown':
        gateway.receipt = {'status': 'unknown'}
    gateway.admissions['native'] = {'admission_id': PHOTO_WORK['admission_id'], 'ref': {'session_id': 'session-a'}}

    def read_work(home, owner, sid, admission_id):
        assert (home, owner, sid, admission_id) == (tmp_path, '1234567890', 'session-a', PHOTO_WORK['admission_id'])
        return [] if state == 'foreign' else [PHOTO_WORK]

    response = asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'observe_work', 'session_id': 'session-a',
                                 'admission_id': PHOTO_WORK['admission_id']},
        resolve=gateway.resolve, connect=gateway.connect, read_work=read_work,
    ))
    assert response['status'] == status
    if state in {'started', 'approval', 'clarify'}:
        assert response['execution_generation'] == 14
    assert response.get('output') == ('Hasil asli Hermes.' if state == 'terminal' else None)
    assert [method for method, _ in gateway.calls] == {
        'foreign': ['session.resume'], 'reset': ['session.resume', 'prompt.receipt', 'session.resume'],
        'started': ['session.resume', 'prompt.receipt', 'session.resume'],
        'approval': ['session.resume', 'prompt.receipt', 'session.resume'],
        'clarify': ['session.resume', 'prompt.receipt', 'session.resume'],
        'stale': ['session.resume', 'prompt.receipt', 'session.resume'],
    }.get(state, ['session.resume', 'prompt.receipt'])


def test_observing_running_work_replays_only_current_scoped_tool_steps(tmp_path):
    admission_id = PHOTO_WORK['admission_id']
    replay_epoch = 'live-epoch'

    def tool(seq, kind, name, tool_id, *, admission=admission_id, generation=7, **payload):
        return {'type': kind, 'session_id': 'session-a', 'admission_id': admission,
                'seq': seq, 'execution_generation': generation, 'replay_epoch': replay_epoch,
                'payload': {'tool_id': tool_id, 'name': name, **payload}}

    class ProgressGateway(FakeGateway):
        async def rpc(self, method, **params):
            if method == 'session.events.since':
                self.calls.append((method, params))
                assert params['session_id'] == 'session-a' and params['replay_epoch'] == replay_epoch
                return {'events': [
                    tool(185, 'tool.start', 'web_extract', 'extract'),
                    tool(186, 'tool.start', 'terminal', 'unrelated', admission='b' * 32),
                    tool(187, 'tool.complete', 'web_extract', 'extract'),
                    tool(188, 'tool.start', 'read_file', 'previous-run', generation=6),
                    tool(189, 'tool.start', 'read_file', 'read', args={'path': '/private/work/notes.txt'}),
                    tool(190, 'tool.complete', 'browser_exec', 'browser', is_error=False,
                         result=json.dumps({'success': False, 'exit_code': 1, 'stderr': 'private-debug-log'})),
                    tool(191, 'tool.progress', 'read_file', 'read', percent=37),
                    tool(192, 'tool.complete', 'browser_exec', 'page-read', is_error=False,
                         result=json.dumps({'success': True, 'exit_code': 0,
                                            'output': 'Gagal\nPembuatan audio gagal.'})),
                ]}
            result = await super().rpc(method, **params)
            if method == 'session.resume':
                result.update({'replay_epoch': replay_epoch, 'last_sequence': 187, 'pending': [{
                    'admission_id': admission_id, 'status': 'started', 'execution_generation': 7,
                }]})
            return result

    gateway = ProgressGateway()
    gateway.admissions['native'] = {'admission_id': admission_id, 'ref': {'session_id': 'session-a'}}
    gateway.receipt = {'status': 'started', 'execution_generation': 7}
    response = asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'observe_work', 'session_id': 'session-a',
                                 'admission_id': admission_id, 'include_progress': True},
        resolve=gateway.resolve, connect=gateway.connect,
        read_work=lambda *_: [{**PHOTO_WORK, 'status': 'queued'}],
    ))
    assert response == {
        'status': 'started', 'session_id': 'session-a', 'admission_id': admission_id,
        'execution_generation': 7, 'tool_steps': [
            {'tool': 'web_extract', 'status': 'completed', 'detail': ''},
            {'tool': 'browser_exec', 'status': 'failed', 'detail': '', 'exit_code': 1},
            {'tool': 'read_file', 'status': 'started', 'detail': 'notes.txt', 'percent': 37},
            {'tool': 'browser_exec', 'status': 'completed', 'detail': '', 'application_outcome': 'failed',
             'finding': 'Halaman melaporkan pembuatan audio gagal.'},
        ],
    }
    assert [method for method, _ in gateway.calls] == [
        'session.resume', 'prompt.receipt', 'session.resume', 'session.events.since',
    ]


@pytest.mark.parametrize(('native', 'ledger_chat', 'ledger_state', 'mixed', 'expected'), [
    (True, '2468@lid', 'delivered', False, 'sent'),
    (False, '2468@lid', 'attempting', False, 'pending'),
    (True, '2468@lid', 'delivered', True, 'sent'),
    (True, '99@lid', 'delivered', False, 'unconfirmed'),
    (True, '2468@lid', 'failed', False, 'unconfirmed'),
])
def test_observe_matches_text_delivery_without_assuming_file_delivery(
    tmp_path, monkeypatch, native, ledger_chat, ledger_state, mixed, expected,
):
    from contextlib import closing
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    from gateway.delivery_ledger import _initialize_schema, compute_obligation_id

    monkeypatch.setattr('gateway.whatsapp_identity.expand_whatsapp_aliases', lambda _: {'1234567890', '2468'})
    admission_id = 'a' * 32
    session_key = 'fixture-owner-dm'
    work_text = 'Rangkum dokumen ini.'
    result = 'Ini ringkasannya.'
    payload = {'text': work_text}
    if native:
        source = {'platform': 'whatsapp', 'chat_type': 'dm', 'chat_id': '2468@lid', 'user_id': '1234567890'}
        payload['native_text_v1'] = {'source': source, 'event': {'message_id': 'inbound-a'}}
    else:
        payload = {'text': work_text, 'surface_v1': {'surface': 'voice-live',
                                                   'voice_context': live_agent.VOICE_PREFIX}}
    principal = f'uid:{os.getuid()}'
    if not native:
        payload['local_operator_v1'] = {'principal_id': principal, 'profile_id': str(tmp_path.resolve()),
                                        'session_id': 'session-a'}
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('session-a', 'whatsapp', session_key=session_key, chat_type='dm',
                          chat_id='2468@lid', user_id='1234567890')
        add_dm_work(db, 'session-a', admission_id, payload, status='terminal', outcome='completed',
                    principal=principal)
        with db._conn:
            _initialize_schema(db._conn)
            for message_ref, state in (('unrelated', 'delivered'),
                                       ('inbound-a' if native else admission_id, ledger_state)):
                obligation_id = compute_obligation_id(session_key, message_ref, result)
                db._conn.execute(
                    """INSERT INTO delivery_obligations
                       (obligation_id, session_key, platform, chat_id, content, state, created_at, updated_at)
                       VALUES (?, ?, 'whatsapp', ?, ?, ?, 0, 0)""",
                    (obligation_id, session_key, ledger_chat, result, state),
                )
    gateway = FakeGateway()
    gateway.admissions['existing'] = {'admission_id': admission_id, 'ref': {'session_id': 'session-a'}}
    if mixed:
        artifact = tmp_path / 'hasil.txt'
        artifact.write_text('Isi lampiran.')
        gateway.receipt['result']['final_response'] = result + '\nMEDIA:' + str(artifact)
    else:
        gateway.receipt['result']['final_response'] = result

    actual = run(tmp_path, {'action': 'observe_work', 'session_id': 'session-a',
                            'admission_id': admission_id}, gateway)
    assert actual['status'] == 'completed' and actual['text_delivery'] == expected
    assert actual['has_attachments'] is mixed
    assert result in actual['output'] and 'MEDIA:' not in actual['output']
    assert [method for method, _ in gateway.calls] == ['session.resume', 'prompt.receipt']


@pytest.mark.parametrize(('state', 'status'), [('started', 'queued'), ('terminal', 'not_running'),
                                                   ('reset', 'session_changed'), ('stale', 'not_running'),
                                                   ('timeout', 'unknown')])
def test_spoken_instruction_steers_exact_running_turn_once(tmp_path, state, status):
    GatewayRPCError = pytest.importorskip('hermes_cli.gateway_client').GatewayRPCError
    instruction = 'Fokus ke tulisan di pojok.'

    class SteerGateway(FakeGateway):
        async def rpc(self, method, **params):
            if method == 'session.steer':
                params.pop('_timeout')
                self.calls.append((method, params))
                assert params == {'session_id': 'session-a', 'execution_generation': 14, 'text': instruction}
                if state == 'stale':
                    raise GatewayRPCError('stale_generation')
                if state == 'timeout':
                    raise TimeoutError
                return {'status': 'queued', 'execution_generation': 14}
            result = await super().rpc(method, **params)
            if method == 'prompt.receipt' and state == 'reset':
                self.active_session = 'session-b'
            return result

    gateway = SteerGateway()
    gateway.admissions['native'] = {'admission_id': PHOTO_WORK['admission_id'], 'ref': {'session_id': 'session-a'}}
    if state != 'terminal':
        gateway.receipt = {'status': 'started', 'execution_generation': 14}
    response = asyncio.run(live_agent._run(
        tmp_path, '1234567890', {'action': 'steer_work', 'session_id': 'session-a',
                                 'admission_id': PHOTO_WORK['admission_id'], 'text': instruction},
        resolve=gateway.resolve, connect=gateway.connect, read_work=lambda *_: [PHOTO_WORK],
    ))
    assert response == ({'status': status} if state == 'reset' else {
        'status': status, 'session_id': 'session-a', 'admission_id': PHOTO_WORK['admission_id'],
    })
    expected = ['session.resume', 'prompt.receipt'] + {
        'reset': ['session.resume'], 'terminal': [],
    }.get(state, ['session.steer'])
    assert [method for method, _ in gateway.calls] == expected


def test_recall_reads_the_current_dm_without_submitting_a_turn(tmp_path):
    gateway = FakeGateway()
    reads = []

    def read_recall(home, session_id, terms):
        reads.append((home, session_id, terms))
        return [{'role': 'user', 'text': 'Kode paketnya UJI-BUNGA-427.'}]

    request = {'action': 'recall', 'session_id': 'session-a', 'terms': ['Kode', 'paket']}
    result = asyncio.run(live_agent._run(tmp_path, '1234567890', request, resolve=gateway.resolve,
                                       connect=gateway.connect, read_recall=read_recall))
    assert result['status'] == 'found' and result['session_id'] == 'session-a'
    assert 'UJI-BUNGA-427' in result['history'][0]['text']
    assert reads == [(tmp_path, 'session-a', ['kode', 'paket'])]
    assert [method for method, _ in gateway.calls] == ['session.resume']

    gateway.active_session = 'session-b'
    result = asyncio.run(live_agent._run(tmp_path, '1234567890', request, resolve=gateway.resolve,
                                       connect=gateway.connect, read_recall=read_recall))
    assert result == {'status': 'session_changed'}
    assert len(reads) == 1


@pytest.mark.parametrize('existing,action,calls', [
    (False, 'context', 1), (True, 'context', 0), (False, 'work', 0),
])
def test_initial_call_creates_a_missing_gateway_route_only_for_context(tmp_path, monkeypatch, existing, action, calls):
    gateway = FakeGateway()
    monkeypatch.setattr(live_agent, '_read_persona', lambda _home: None)
    gateway.active_session = 'session-a' if existing else None
    attempts = []

    def resolve(_home, _owner):
        if gateway.active_session is None:
            raise RuntimeError('No verified active WhatsApp session')
        return gateway.active_session

    async def ensure_session(client):
        assert client is gateway
        attempts.append(client)
        gateway.active_session = 'session-a'

    request = {'action': action}
    if action == 'work':
        request['session_id'] = 'session-a'
    result = asyncio.run(live_agent._run(
        tmp_path, '1234567890', request, resolve=resolve, connect=gateway.connect,
        read_history=lambda _home, _route: ([], 0), read_work=lambda *_args: [],
        ensure_session=ensure_session,
    ))
    assert len(attempts) == calls
    if action == 'context':
        assert result == {'status': 'ready', 'session_id': 'session-a', 'history': [], 'history_cursor': 0}
        assert gateway.calls[0][0] == 'session.resume'
    else:
        assert result == {'status': 'unavailable'} and gateway.calls == []


def test_recall_keeps_the_matching_old_message_and_its_reply():
    rows = [
        {'role': 'user', 'content': 'Nomor resi: 824517.'},
        {'role': 'assistant', 'content': 'Aku catat nomornya.'},
        *[{'role': 'user', 'content': 'Topik lain.'} for _ in range(30)],
        {'role': 'tool', 'content': 'Nomor resi lain dari internal tool.'},
    ]
    assert live_agent._find_history(rows, ['nomor', 'resi']) == [
        {'role': 'user', 'text': 'Nomor resi: 824517.'},
        {'role': 'assistant', 'text': 'Aku catat nomornya.'},
    ]


def test_recall_preserves_evidence_late_in_a_long_message():
    text = 'Pembukaan panjang. ' * 30 + 'Nomor resi: 824517.'
    assert 'Nomor resi: 824517.' in live_agent._find_history(
        [{'role': 'user', 'content': text}], ['resi'],
    )[0]['text']


def test_voice_exchange_is_only_saved_to_the_call_dm(tmp_path):
    gateway = FakeGateway()
    writes = []

    def save(*args):
        writes.append(args)
        return 'saved'

    request = {'action': 'save_exchange', 'session_id': 'session-a', 'exchange_id': 'a' * 32,
               'user': 'Judulnya Laut Biru.', 'assistant': 'Aku ingat judulnya.'}
    assert asyncio.run(live_agent._run(
        tmp_path, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
        save_exchange=save,
    )) == {'status': 'saved'}
    assert writes == [(tmp_path, 'session-a', 'a' * 32, request['user'], request['assistant'])]
    assert [method for method, _ in gateway.calls] == ['session.resume']
    gateway.active_session = 'session-b'
    assert asyncio.run(live_agent._run(
        tmp_path, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
        save_exchange=save,
    )) == {'status': 'session_changed'}
    assert len(writes) == 1


def test_native_voice_exchange_replays_once_and_respects_reset(tmp_path):
    from contextlib import closing
    SessionDB = pytest.importorskip('hermes_state').SessionDB

    with closing(SessionDB(tmp_path / 'state.db')) as db:
        key, scope = 'agent:main:whatsapp:dm:1234567890', str(tmp_path)
        db.create_session('old', 'whatsapp', session_key=key)
        db.create_session('new', 'whatsapp', session_key=key)
        db.save_gateway_routing_entry(key, json.dumps({'session_id': 'old'}), scope=scope)
        args = (db, scope, 'old', 'a' * 32, 'Judulnya Laut Biru.', 'Aku ingat judulnya.')
        assert append_voice_exchange(*args) == 'saved'
        assert append_voice_exchange(*args) == 'saved'
        rows = db.get_messages_as_conversation('old', repair_alternation=True)
        assert [(row['role'], row['content']) for row in rows] == [
            ('user', 'Judulnya Laut Biru.'), ('assistant', 'Aku ingat judulnya.'),
        ]
        assert live_agent._voice_history(db.get_messages('old'))[-1]['text'] == 'Aku ingat judulnya.'
        # Reset publishes its route before retiring the old row.
        db.save_gateway_routing_entry(key, json.dumps({'session_id': 'new'}), scope=scope)
        assert append_voice_exchange(db, scope, 'old', 'b' * 32, 'Ucapanku.', 'Jawabanmu.') == 'session_changed'
        assert db.get_messages('new') == []
        assert len(db.get_messages('old')) == 2


def test_native_voice_exchange_saves_before_the_running_dm_finishes(tmp_path):
    from contextlib import closing

    SessionDB = pytest.importorskip('hermes_state').SessionDB
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        key, scope = 'agent:main:whatsapp:dm:1234567890', str(tmp_path)
        sid, lease = 'session-a', 'native-dm'
        db.create_session(sid, 'whatsapp', session_key=key)
        db.save_gateway_routing_entry(key, json.dumps({'session_id': sid}), scope=scope)
        assert db.try_acquire_session_turn_lease(sid, lease)
        db.append_messages_batch(sid, [{'role': 'user', 'content': 'Riset.'}], turn_lease_holder=lease)
        args = (db, scope, sid, 'a' * 32, 'Halo.', 'Halo juga.')
        at = {'user_at_ms': 1_780_000_001_234, 'assistant_at_ms': 1_780_000_003_565}
        assert append_voice_exchange(*args, **at) == 'saved'
        db.append_messages_batch(sid, [{'role': 'assistant', 'content': 'Hasil riset.'}], turn_lease_holder=lease)
        assert append_voice_exchange(*args) == 'saved'
        rows = db.get_messages(sid)
        assert [row['content'] for row in rows] == ['Riset.', 'Halo.', 'Halo juga.', 'Hasil riset.']
        assert [row['timestamp'] for row in rows[1:3]] == [1_780_000_001.234, 1_780_000_003.565]
        replay = db.get_messages_as_conversation(sid, repair_alternation=True)
        replay_text = '\n'.join(row['content'] for row in replay)
        assert replay_text.index('Riset.') < replay_text.index('Halo.') < replay_text.index('Hasil riset.')


def test_voice_exchange_saved_during_compaction_stays_live_once(tmp_path):
    from contextlib import closing

    SessionDB = pytest.importorskip('hermes_state').SessionDB
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        key, scope = 'agent:main:whatsapp:dm:1234567890', str(tmp_path)
        sid, lease = 'session-a', 'native-dm'
        db.create_session(sid, 'whatsapp', session_key=key)
        db.save_gateway_routing_entry(key, json.dumps({'session_id': sid}), scope=scope)
        assert db.try_acquire_session_turn_lease(sid, lease)
        db.append_messages_batch(sid, [{'role': 'user', 'content': 'Riwayat lama.'}], turn_lease_holder=lease)
        watermark = db.get_active_message_watermark(sid)
        args = (db, scope, sid, 'a' * 32, 'Halo.', 'Halo juga.')
        assert append_voice_exchange(*args, user_at_ms=1_780_000_001_234) == 'saved'
        db.archive_and_compact(sid, [{'role': 'user', 'content': 'Ringkasan sebelumnya.'}], watermark=watermark)
        assert append_voice_exchange(*args) == 'saved'
        rows = db.get_messages(sid)
        assert [row['content'] for row in rows] == ['Ringkasan sebelumnya.', 'Halo.', 'Halo juga.']
        assert rows[1]['timestamp'] == 1_780_000_001.234


@pytest.mark.parametrize('reset', [False, True])
def test_voice_exchange_waits_for_the_native_tool_batch_without_restarting(tmp_path, reset):
    from contextlib import closing

    SessionDB = pytest.importorskip('hermes_state').SessionDB
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        key, scope = 'agent:main:whatsapp:dm:1234567890', str(tmp_path)
        sid, lease = 'session-a', 'native-dm'
        db.create_session(sid, 'whatsapp', session_key=key)
        db.save_gateway_routing_entry(key, json.dumps({'session_id': sid}), scope=scope)
        assert db.try_acquire_session_turn_lease(sid, lease)
        db.append_messages_batch(sid, [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call-a', 'type': 'function', 'function': {'name': 'one', 'arguments': '{}'}},
            {'id': 'call-b|item-b', 'type': 'function', 'function': {'name': 'two', 'arguments': '{}'}},
        ]}, {'role': 'tool', 'tool_call_id': 'call-a', 'content': 'Satu.'}], turn_lease_holder=lease)
        args = (db, scope, sid, 'a' * 32, 'Halo.', 'Halo juga.')
        with db._read_ctx() as conn:
            assert conn.execute('SELECT holder FROM session_turn_leases WHERE conversation_id=?', (sid,)).fetchone()[0] == lease
        assert append_voice_exchange(*args) == 'busy'

        class PendingWrite:
            def is_set(self):
                return False

            def wait(self, interval):
                assert interval == 1
                if reset:
                    db.create_session('session-b', 'whatsapp', session_key=key)
                    db.save_gateway_routing_entry(key, json.dumps({'session_id': 'session-b'}), scope=scope)
                else:
                    db.append_messages_batch(sid, [{'role': 'tool', 'tool_call_id': 'call-b',
                                                    'content': 'Dua.'}], turn_lease_holder=lease)
                return False

        assert append_voice_exchange(*args, wait_for_lease=True, cancelled=PendingWrite()) == (
            'session_changed' if reset else 'saved'
        )
        rows = db.get_messages(sid)
        assert [row['role'] for row in rows] == (
            ['assistant', 'tool'] if reset else ['assistant', 'tool', 'tool', 'user', 'assistant']
        )
        if not reset:
            replay = db.get_messages_as_conversation(sid, repair_alternation=True)
            assert [len(row['tool_calls']) for row in replay if row.get('tool_calls')] == [2]
            assert [row['content'] for row in replay if row['role'] == 'tool'] == ['Satu.', 'Dua.']


def test_shared_result_can_save_only_the_callers_question(tmp_path):
    from contextlib import closing
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    gateway = FakeGateway()
    request = {'action': 'save_exchange', 'session_id': 'session-a', 'exchange_id': 'a' * 32,
               'user': 'Apa hasil foto tadi?', 'assistant': ''}
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        key, scope = 'agent:main:whatsapp:dm:1234567890', str(tmp_path)
        db.create_session('session-a', 'whatsapp', session_key=key)
        db.save_gateway_routing_entry(key, json.dumps({'session_id': 'session-a'}), scope=scope)

        def save(_home, sid, exchange_id, user, assistant):
            return append_voice_exchange(db, scope, sid, exchange_id, user, assistant)

        for attempt in (request, request, {**request, 'assistant': 'A different answer.'}):
            response = asyncio.run(live_agent._run(
                tmp_path, '1234567890', attempt, resolve=gateway.resolve,
                connect=gateway.connect, save_exchange=save,
            ))
            expected = 'saved' if attempt is request else 'conflict'
            assert response == {'status': expected}
        assert [(row['role'], row['content']) for row in db.get_messages('session-a')] == [
            ('user', request['user']),
        ]


def test_continued_voice_answer_keeps_one_user_message_in_the_dm(tmp_path):
    from contextlib import closing
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    gateway = FakeGateway()
    first = {'action': 'save_exchange', 'session_id': 'session-a', 'exchange_id': 'a' * 32,
             'user': 'Nama tokohnya Nara.', 'assistant': 'Aku ingat Nara.'}
    continuation = {**first, 'exchange_id': 'b' * 32, 'user': '', 'assistant': 'Dia tokoh utama.'}
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        key, scope = 'agent:main:whatsapp:dm:1234567890', str(tmp_path)
        db.create_session('session-a', 'whatsapp', session_key=key)
        db.save_gateway_routing_entry(key, json.dumps({'session_id': 'session-a'}), scope=scope)

        def save(_home, sid, exchange_id, user, assistant):
            return append_voice_exchange(db, scope, sid, exchange_id, user, assistant)

        for request in (first, continuation, continuation):
            assert asyncio.run(live_agent._run(
                tmp_path, '1234567890', request, resolve=gateway.resolve, connect=gateway.connect,
                save_exchange=save,
            )) == {'status': 'saved'}
        assert [(row['role'], row['content']) for row in db.get_messages('session-a')] == [
            ('user', first['user']), ('assistant', first['assistant']), ('assistant', continuation['assistant']),
        ]
        visible = db.get_messages_as_conversation('session-a', repair_alternation=True)
        assert 'Aku ingat Nara.' in ' '.join(row['content'] for row in visible)
        assert 'Dia tokoh utama.' in ' '.join(row['content'] for row in visible)


def test_native_voice_exchange_retry_follows_compression_within_the_same_dm(tmp_path):
    from contextlib import closing

    SessionDB = pytest.importorskip('hermes_state').SessionDB
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        key, scope = 'agent:main:whatsapp:dm:1234567890', str(tmp_path)
        db.create_session('root', 'whatsapp', session_key=key)
        db.save_gateway_routing_entry(key, json.dumps({'session_id': 'root'}), scope=scope)
        before = ('a' * 32, 'Nama awal.', 'Sudah kucatat.')
        assert append_voice_exchange(db, scope, 'root', *before) == 'saved'
        db.end_session('root', 'compression')
        db.create_session('tip', 'whatsapp', session_key=key, parent_session_id='root')
        db.save_gateway_routing_entry(key, json.dumps({'session_id': 'tip'}), scope=scope)

        # First worker reply may be lost just before compaction changed the target.
        assert append_voice_exchange(db, scope, 'root', *before) == 'saved'
        after = ('b' * 32, 'Judulnya Laut Biru.', 'Aku ingat judulnya.')
        assert append_voice_exchange(db, scope, 'root', *after) == 'saved'
        assert append_voice_exchange(db, scope, 'tip', *after) == 'saved'
        assert [row['content'] for row in db.get_messages('tip')] == list(after[1:])

        db.create_session('new', 'whatsapp', session_key=key, parent_session_id='tip')
        db.save_gateway_routing_entry(key, json.dumps({'session_id': 'new'}), scope=scope)
        assert append_voice_exchange(db, scope, 'root', *after) == 'session_changed'
        assert not db.get_messages('new')


@pytest.mark.parametrize(('text', 'kind', 'authorized'), [
    ('Asisten, telepon aku sekarang', 'native', True),
    ('Asisten, telepon aku sekarang', 'compacted', True),
    ('Asisten, telepon aku sekarang', 'viewer', False),
    ('Asisten, telepon aku sekarang', 'queued', False),
    ('¿Podemos hablar por WhatsApp ahora? Me vendría bien hablarlo contigo.', 'native', True),
    ('', 'voice', True),
    ('', 'wrong-message', False),
    ('', 'wrong-sender', False),
    ('', 'automation', False),
])
def test_native_outbound_requires_current_whatsapp_owner_admission(tmp_path, monkeypatch, text, kind, authorized):
    SessionDB = pytest.importorskip('hermes_state').SessionDB
    from contextlib import closing
    from datetime import datetime, timezone
    from importlib import import_module
    from pathlib import Path

    from gateway.config import Platform, load_gateway_config
    from gateway.session import SessionEntry, SessionSource, build_session_key
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    checkout = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(checkout.parent))
    outbound = import_module(f'{checkout.name}.whatsapp_outbound')
    owner = '15550100001'
    message_id = 'test-owner-message'
    source = SessionSource(Platform.WHATSAPP, owner, user_id=owner, message_id=message_id)
    principal = 'messaging:' + json.dumps([None, 'whatsapp', owner, None, owner], separators=(',', ':'))
    home_token = set_hermes_home_override(tmp_path)
    try:
        route = build_session_key(source)
        active_id = 'tip' if kind == 'compacted' else 'session'
        with closing(SessionDB(tmp_path / 'state.db')) as db:
            db.create_session('session', 'whatsapp', session_key=route, chat_id=owner, chat_type='dm', user_id=owner)
            now = datetime.now(timezone.utc)
            entry = SessionEntry(route, 'session', now, now, origin=source)
            db.save_gateway_routing_entry(route, json.dumps(entry.to_dict()),
                                          scope=str(load_gateway_config().sessions_dir.resolve()))
            voice_note = kind == 'voice'
            payload = {'text': '' if voice_note else text}
            if kind == 'viewer':
                payload['surface_v1'] = {'surface': 'voice-live'}
                principal = 'uid:123'
            else:
                payload['native_text_v1'] = {
                    'route': route, 'source': source.to_dict(), 'event': {'message_id': message_id},
                }
                if kind == 'automation':
                    payload['native_text_v1']['automation'] = {'kind': 'scheduled'}
                if voice_note:
                    native = payload['native_text_v1']
                    native['message_type'] = 'voice'
                    native['media'] = [{'path': 'voice-note.ogg', 'sha256': 'a' * 64, 'size': 42}]
                    native['event'].update(
                        media_types=['audio/ogg; codecs=opus'],
                        metadata={'whatsapp_native_type': 'audioMessage',
                                  'whatsapp_native': {'audio': {'ptt': True}}},
                    )
            epoch = begin_runtime_epoch(db, instance_id='outbound-fixture')
            row = admit_session_input(db, epoch=epoch, principal_id=principal, session_id='session',
                                      request_id=message_id, payload=payload)
            if kind != 'queued':
                assert claim_session_input(db, epoch=epoch, session_id='session')['status'] == 'started'
            if kind == 'compacted':
                db.end_session('session', 'compression')
                db.create_session('tip', 'whatsapp', parent_session_id='session')
        sender = '15550100002' if kind == 'wrong-sender' else owner
        current_message = 'another-message' if kind == 'wrong-message' else message_id
        tokens = set_session_vars(platform='whatsapp', chat_type='dm', chat_id=owner, user_id=sender,
                                  message_id=current_message, session_key=route, session_id=active_id)
        try:
            assert outbound._owner_request(tmp_path, owner, active_id) == (row['admission_id'] if authorized else None)
        finally:
            clear_session_vars(tokens)
    finally:
        reset_hermes_home_override(home_token)


def test_outbound_prompt_marks_whatsapp_dm_and_ignores_call_turns(monkeypatch):
    pytest.importorskip('hermes_state')
    from importlib import import_module
    from pathlib import Path

    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_ingress import executing_admission, viewer_turn

    checkout = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(checkout.parent))
    outbound = import_module(f'{checkout.name}.whatsapp_outbound')
    tokens = set_session_vars(platform='whatsapp', chat_type='dm')
    executing = executing_admission.set(True)
    try:
        hint = outbound.call_now_hint(platform='whatsapp')['context']
        assert 'balasan di chat WhatsApp' in hint
        assert outbound.TOOL_NAME in hint
        assert 'Jika pemilik meminta ditelepon sekarang' in hint
        viewer = viewer_turn.set(object())
        try:
            assert outbound.call_now_hint(platform='whatsapp') is None
        finally:
            viewer_turn.reset(viewer)
    finally:
        executing_admission.reset(executing)
        clear_session_vars(tokens)


def test_native_tool_registry_receives_outbound_result(monkeypatch):
    ToolRegistry = pytest.importorskip('tools.registry').ToolRegistry
    from importlib import import_module
    from pathlib import Path

    checkout = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(checkout.parent))
    plugin = import_module(checkout.name)
    outbound = import_module(f'{checkout.name}.whatsapp_outbound')
    registry = ToolRegistry()
    ctx = SimpleNamespace(
        register_auxiliary_task=lambda *_args, **_kwargs: None,
        register_cli_command=lambda **_: None,
        register_tool=registry.register,
        register_hook=lambda *_: None,
        register_middleware=lambda *_: None,
        register_skill=lambda **_: None,
    )
    accepted = {'status': 'dial_requested', 'call_id': 'fixture-call'}

    def fake_call_now(context, args, *, session_id):
        assert context is ctx
        assert args == {}
        assert session_id == 'owner-dm'
        return accepted

    monkeypatch.setattr(outbound, 'call_now', fake_call_now)
    plugin.register(ctx)

    result = registry.dispatch(outbound.TOOL_NAME, {}, session_id='owner-dm')
    assert json.loads(result) == accepted


def test_whatsapp_alarm_uses_native_cron_and_dials_once(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime
    from importlib import import_module
    from pathlib import Path

    from cron.execution_identity import current_cron_execution, enter_cron_execution, exit_cron_execution
    from cron import jobs
    from cron.occurrences import scheduled_instant
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.plugins_state import PluginState

    checkout = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(checkout.parent))
    outbound = import_module(f'{checkout.name}.whatsapp_outbound')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    owner, bot = '15550100001', '15550100002'
    monkeypatch.setattr(outbound, 'resolve_identity', lambda home: (owner, bot))
    ctx = SimpleNamespace(state=PluginState('wa-control-live-test'), current_cron_execution=current_cron_execution)
    ctx.state.data_dir.mkdir(parents=True)
    (ctx.state.data_dir / 'config.json').touch()
    (ctx.state.data_dir / 'adapter.token').write_text('test')
    (ctx.state.data_dir / 'serving-identity.json').write_text(json.dumps({
        'owner_number': owner, 'bot_number': bot, 'hermes_home': str(tmp_path.resolve()),
    }))
    calls = []
    response = SimpleNamespace(status=202, read=lambda _: b'{"id":"test-call","direction":"outbound"}')
    conn = SimpleNamespace(request=lambda *args: calls.append(args[:2]), getresponse=lambda: response,
                           close=lambda: None)
    monkeypatch.setattr(outbound.http.client, 'HTTPConnection', lambda *args, **kwargs: conn)
    monkeypatch.setattr(outbound, '_owner_request',
                        lambda home, number, session_id: 'owner-admission' if session_id == 'owner-dm' else None)

    args = {'schedule': 'in 10m'}
    tokens = set_session_vars(platform='whatsapp', chat_id=owner, chat_type='dm', user_id=owner)
    try:
        assert outbound.call_now(ctx, args, session_id='other-dm') == {'status': 'denied'}
        assert outbound.call_now(ctx, {'schedule': 'every 10m'}, session_id='owner-dm')['status'] == 'invalid_schedule'
        alarm = outbound.call_now(ctx, args, session_id='owner-dm')
        assert alarm['status'] == 'scheduled'
        assert outbound.call_now(ctx, args, session_id='owner-dm') == alarm
        assert len(jobs.load_jobs()) == 1
        assert calls == []
        assert outbound.call_now(ctx, {}, session_id='owner-dm')['status'] == 'dial_requested'
    finally:
        clear_session_vars(tokens)

    job = jobs.get_job(alarm['job_id'])
    assert job['enabled'] and job['schedule']['kind'] == 'once'
    assert job['enabled_toolsets'] == ['wa-control-live']
    assert job['origin']['chat_id'] == owner and job['deliver'] == 'origin'
    instant = scheduled_instant(job['next_run_at'])
    assert jobs.get_due_jobs() == []
    monkeypatch.setattr(jobs, '_hermes_now', lambda: datetime.fromisoformat(instant))
    due, = jobs.get_due_jobs()
    assert due['id'] == job['id'] and due['_scheduled_instant'] == instant

    @contextmanager
    def fire(job, instant, execution_id):
        token = enter_cron_execution(job, execution_id, {'source': 'builtin', 'scheduled_instant': instant})
        try:
            yield
        finally:
            exit_cron_execution(token)

    for rejected_job, rejected_instant in [
        ({**job, 'id': 'unapproved'}, instant), (job, None), (job, '2030-01-01T00:00:00+00:00'),
    ]:
        with fire(rejected_job, rejected_instant, 'rejected'):
            assert outbound.call_now(ctx, {}, session_id='cron-session') == {'status': 'denied'}
    with fire(due, due['_scheduled_instant'], 'first'):
        assert outbound.call_now(ctx, args, session_id='cron-session') == {'status': 'denied'}
        assert outbound.call_now(ctx, {}, session_id='cron-session')['status'] == 'dial_requested'
    with fire(job, instant, 'replayed'):
        assert outbound.call_now(ctx, {}, session_id='cron-session') == {'status': 'already_attempted'}
    assert calls == [('POST', '/calls'), ('POST', '/calls')]


def test_repeat_delegation_uses_gateway_idempotency(tmp_path):
    gateway = FakeGateway()

    assert run(tmp_path, FIRST, gateway) == run(tmp_path, FIRST, gateway)
    submissions = [params for method, params in gateway.calls if method == 'prompt.submit']
    assert [params['submission_id'] for params in submissions] == [FIRST['submission_id']] * 2
    assert len(gateway.admissions) == 1


def test_queued_voice_request_keeps_when_owner_spoke(tmp_path):
    from datetime import datetime
    gateway = FakeGateway()
    spoken_at_ms = 1_700_000_000_123
    run(tmp_path, {**FIRST, 'spoken_at_ms': spoken_at_ms}, gateway)
    note = next(p['voice_context'] for method, p in gateway.calls if method == 'prompt.submit')
    stamp = note.removeprefix(live_agent.VOICE_PREFIX + live_agent.VOICE_REQUEST_TIME).splitlines()[0]
    assert round(datetime.fromisoformat(stamp).timestamp() * 1000) == spoken_at_ms


def test_owner_live_submission_is_signed_before_gateway_admission(tmp_path):
    gateway = FakeGateway()
    request = {**FIRST, 'text': 'Kirim dokumennya ke aku.'}
    signed_id = 'wa-control-live:' + FIRST['submission_id'] + ':' + 'a' * 64

    def sign(session_id, nonce, actual_text):
        assert (session_id, nonce, actual_text) == (FIRST['session_id'], FIRST['submission_id'], request['text'])
        return signed_id

    result = asyncio.run(live_agent._run(tmp_path, '1234567890', request, resolve=gateway.resolve,
                                         connect=gateway.connect, sign_submission=sign))
    assert result['receipt']['session_id'] == FIRST['session_id']
    submitted = next(params for method, params in gateway.calls if method == 'prompt.submit')
    assert submitted['submission_id'] == signed_id
    assert submitted['text'] == request['text'] and signed_id not in submitted['voice_context']


@pytest.mark.parametrize(('text', 'protect_from_commands'), [
    ('  /approve once', True), ('/yolo', True), ('@FILE:test\n/approve', True),
    ('/tmp/folder/file', False), ('Ceritakan kapan /new berguna.', False),
])
def test_delegated_text_is_submitted_as_literal_task_prose(tmp_path, text, protect_from_commands):
    gateway = FakeGateway()

    assert run(tmp_path, {**FIRST, 'text': text}, gateway)['status'] == 'completed'
    submitted = next(params for method, params in gateway.calls if method == 'prompt.submit')
    assert submitted['text'] == (live_agent.VOICE_PREFIX + text if protect_from_commands else text)
    assert not live_agent._gateway_command(submitted['text'])


@pytest.mark.parametrize(('kind', 'status'), [('approval', 'approval_required'),
                                               ('clarify', 'clarification_required')])
def test_observer_reports_current_native_control_without_answering(tmp_path, kind, status):
    gateway = FakeGateway()
    gateway.receipt = {'status': 'started', 'execution_generation': 2}
    gateway.prompts = [{'kind': 'approval', 'execution_generation': 1},
                       {'kind': kind, 'execution_generation': 2}]

    assert run(tmp_path, FIRST, gateway)['status'] == status
    assert set(method for method, _ in gateway.calls) == {
        'prompt.submit', 'prompt.receipt', 'session.resume',
    }


def test_observer_deadline_leaves_the_gateway_task_running(tmp_path):
    gateway = FakeGateway()
    gateway.receipt = {'status': 'started', 'execution_generation': 2}

    assert run(tmp_path, FIRST, gateway)['status'] == 'started'

    gateway.receipt = {'status': 'terminal', 'outcome': 'completed',
                       'result': {'final_response': 'Selesai setelah panggilan.'}}
    assert run(tmp_path, FIRST, gateway)['output'] == 'Selesai setelah panggilan.'
    assert len(gateway.admissions) == 1


def test_live_submit_returns_bound_receipt_without_waiting_for_result(tmp_path):
    class QueueGateway(FakeGateway):
        async def rpc(self, method, **params):
            assert method in {'session.resume', 'prompt.submit'}
            result = await super().rpc(method, **params)
            return {**result, 'status': 'queued'} if method == 'prompt.submit' else result

    gateway = QueueGateway()
    report = run(tmp_path, {**FIRST, 'wait_for_result': False}, gateway)
    assert report['status'] == 'queued'
    assert report['receipt'] == {'session_id': 'session-a', 'admission_id': 'admission-1'}
    assert len(gateway.admissions) == 1


def test_slow_receipt_is_returned_without_resubmitting(tmp_path, monkeypatch):
    async def observe():
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        elapsed = 0

        async def advance(delay):
            nonlocal elapsed
            elapsed += delay

        class SlowGateway(FakeGateway):
            async def rpc(self, method, **params):
                if method == 'prompt.receipt' and elapsed < 105:
                    self.receipt = {'status': 'started', 'execution_generation': 2}
                elif method == 'prompt.receipt':
                    self.receipt = {'status': 'terminal', 'outcome': 'completed',
                                    'result': {'final_response': 'Hasil yang sudah tersimpan.'}}
                return await super().rpc(method, **params)

        gateway = SlowGateway()
        with monkeypatch.context() as patch:
            patch.setattr(loop, 'time', lambda: started_at + elapsed)
            patch.setattr(asyncio, 'sleep', advance)
            report = await live_agent._run(
                tmp_path, '1234567890', FOLLOW_UP, resolve=gateway.resolve, connect=gateway.connect,
            )
        assert report['status'] == 'completed'
        assert report['output'] == 'Hasil yang sudah tersimpan.'
        assert sum(method == 'prompt.submit' for method, _ in gateway.calls) == 1

    asyncio.run(observe())


def test_hangup_cancels_the_observer_without_cancelling_the_gateway_task(tmp_path):
    class PausedGateway(FakeGateway):
        async def rpc(self, method, **params):
            result = await super().rpc(method, **params)
            if method == 'prompt.receipt' and not self.observing.is_set():
                self.observing.set()
                await asyncio.Future()
            return result

    async def hang_up():
        gateway = PausedGateway()
        gateway.observing = asyncio.Event()
        worker = asyncio.create_task(live_agent._run(
            tmp_path, '1234567890', FIRST, resolve=gateway.resolve, connect=gateway.connect,
        ))
        await asyncio.wait_for(gateway.observing.wait(), timeout=1)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        result = await live_agent._run(
            tmp_path, '1234567890', FIRST, resolve=gateway.resolve, connect=gateway.connect,
        )
        assert result['status'] == 'completed'
        assert len(gateway.admissions) == 1

    asyncio.run(hang_up())


def test_terminal_status_with_partial_result_is_not_success(tmp_path):
    gateway = FakeGateway()
    gateway.receipt['result']['partial'] = True
    report = run(tmp_path, FIRST, gateway)
    assert report['status'] != 'completed'
    assert 'output' not in report


@pytest.mark.parametrize('request_data', [
    {**FIRST, 'submission_id': 'bad'},
    {**FIRST, 'text': ' '},
    {**FIRST, 'session_id': None},
    {'action': 'latest'},
])
def test_invalid_requests_do_not_contact_hermes(tmp_path, request_data):
    gateway = FakeGateway()
    with pytest.raises(ValueError):
        run(tmp_path, request_data, gateway)
    assert gateway.calls == []
