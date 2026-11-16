"""Offline fixtures never call a live model. Real voice evidence is separate."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


def test_duplicate_event_and_restart_execute_file_task_only_once(tmp_path):
    module_path = Path(__file__).with_name('handoff.py')
    assert module_path.exists(), 'The persistent handoff adapter is not implemented yet'
    spec = importlib.util.spec_from_file_location('handoff', module_path)
    handoff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handoff)
    calls = []
    async def execute(request):
        calls.append(request)
        await asyncio.sleep(.01)
        return {'text': 'File dibuat. Isinya: tes suara berhasil.', 'session_id': 'fixture-session'}
    async def run():
        store = tmp_path / 'handoff.sqlite'
        first = handoff.Handoff(store, execute)
        event = {'type': 'delegation.created', 'item': {'id': 'delegation-1', 'content': [{'type': 'input_text', 'text': 'Buat catatan uji'}]}}
        a, b = await asyncio.gather(first.handle(event), first.handle(event))
        assert a == b
        assert a['session_id'] == 'fixture-session'
        restarted = handoff.Handoff(store, execute)
        assert await restarted.handle(event) == a
        assert calls == ['Buat catatan uji']
    asyncio.run(run())


def load_handoff():
    spec = importlib.util.spec_from_file_location('handoff', Path(__file__).with_name('handoff.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def offline_cli(tmp_path, *, content='tes suara berhasil', final_code=0):
    """Explicit remote CLI fixture, not live model evidence."""
    import sys
    script = tmp_path / 'offline_cli.py'
    script.write_text('''import json, pathlib, sys
args = sys.argv
cwd = pathlib.Path(args[args.index('--in')+1])
target = cwd/'catatan-uji.txt'
target.write_text(%r)
events = [
 {'type':'system','subtype':'init','model':'offline-fixture'},
 {'type':'tool_use','name':'write_file','input':{'path':str(target),'content':%r}},
 {'type':'tool_result','name':'write_file','is_error':False,'output':'fixture-write'},
 {'type':'tool_use','name':'read_file','input':{'path':str(target)}},
 {'type':'tool_result','name':'read_file','is_error':False,'output':target.read_text()},
 {'type':'result','session_id':'fixture-session','exit_code':%r,'text':'File dibuat. Isinya: tes suara berhasil.'},
]
for e in events: print(json.dumps(e), flush=True)
''' % (content, content, final_code))
    return [sys.executable, str(script)]


@pytest.mark.parametrize('explicit_profile', [False, True])
def test_real_subprocess_result_requires_written_and_readback_file(tmp_path, monkeypatch, explicit_profile):
    h = load_handoff()
    assert hasattr(h, 'run_hermes'), 'Native Hermes subprocess runner is missing'
    command = offline_cli(tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'unrelated'))
    monkeypatch.delenv('HERMES_GATEWAY_PROFILE_PIN', raising=False)
    home = tmp_path/'selected' if explicit_profile else Path.home()/'.hermes'
    options = {'hermes_home': home} if explicit_profile else {}
    script = Path(command[1])
    script.write_text('import os\n'
                      f'assert os.environ.get("HERMES_HOME") == {str(home)!r}\n'
                      f'assert os.environ.get("HERMES_GATEWAY_PROFILE_PIN") == {("1" if explicit_profile else None)!r}\n'
                      + script.read_text())
    result = asyncio.run(h.run_hermes('Buat catatan uji', tmp_path/'run', command=command, **options))
    assert result['session_id'] == 'fixture-session'
    assert result['file']['content'] == 'tes suara berhasil'
    assert result['file']['write_count'] == 1
    assert result['file']['read_count'] == 1
    assert result['exit_code'] == 0
    assert result['model'] == 'offline-fixture'
    assert result['text'] == 'File dibuat. Isinya: tes suara berhasil.'


def test_failure_remains_unknown_and_is_not_replayed(tmp_path):
    import sqlite3
    import pytest
    h = load_handoff()
    calls = []
    async def execute(request):
        calls.append(request)
        raise OSError('fixture lost result')
    async def run():
        path = tmp_path/'store.sqlite'
        event = {'type': 'delegation.created', 'item': {'id':'a', 'content':[{'text':'buat catatan'}]}}
        with pytest.raises(OSError):
            await h.Handoff(path, execute).handle(event)
        with pytest.raises(RuntimeError, match='never auto-replay'):
            await h.Handoff(path, execute).handle(event)
        assert calls == ['buat catatan']
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT state FROM task').fetchone()[0] == 'unknown'
    asyncio.run(run())


def test_plausible_success_with_wrong_file_is_rejected(tmp_path):
    import pytest
    h = load_handoff()
    command = offline_cli(tmp_path, content='wrong actual file')
    with pytest.raises(RuntimeError, match='Independent file readback'):
        asyncio.run(h.run_hermes('Buat catatan', tmp_path/'run', command=command))


def completed_native_fixture(tmp_path, h, execution_request='Buat catatan'):
    native = tmp_path/'native-fixture.sqlite'
    run_dir = tmp_path/'hermes'
    asyncio.run(h.run_hermes(execution_request, run_dir, command=offline_cli(tmp_path)))
    workspace = run_dir/'workspace'
    submitted_query = (run_dir/'request.txt').read_text()
    with sqlite3.connect(native) as db:
        db.execute('CREATE TABLE sessions (id TEXT, model TEXT, billing_provider TEXT, cwd TEXT)')
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', ('session-1','fixture-model','fixture-provider',str(workspace)))
        db.execute('CREATE TABLE session_admissions (target_session_id TEXT, status TEXT, outcome TEXT, payload_json TEXT)')
        db.execute('INSERT INTO session_admissions VALUES (?,?,?,?)', ('session-1','terminal','completed',json.dumps({'text':submitted_query})))
        db.execute('CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, tool_call_id TEXT)')
        calls = lambda name: json.dumps([{'function':{'name':name,'arguments':json.dumps({'path':'catatan-uji.txt'})}}])
        rows = [(1,'session-1','assistant','',calls('write_file'),None,None),
                (2,'session-1','tool','{"verified":true}',None,'write_file','write-1'),
                (3,'session-1','assistant','',calls('read_file'),None,None),
                (4,'session-1','tool','{"content":"1|tes suara berhasil"}',None,'read_file','read-1'),
                (5,'session-1','assistant','File dibuat. Isinya: tes suara berhasil.',None,None,None)]
        db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',rows)
    return native, workspace


@pytest.mark.parametrize('saved_state,execution_request', [
    ('unknown', None), ('running', None),
    ('running', 'Buat file catatan-uji.txt berisi tepat tes suara berhasil, lalu baca file itu dan bacakan isinya.'),
])
def test_explicit_reconciliation_uses_completed_native_receipt_not_a_second_task(tmp_path, saved_state, execution_request):
    h = load_handoff()
    native, workspace = completed_native_fixture(tmp_path, h, execution_request or 'Buat catatan')
    native_before = native.read_bytes()
    store=tmp_path/'handoff.sqlite'
    h.Handoff(store, None)
    with sqlite3.connect(store) as db:
        db.execute("INSERT INTO task VALUES (1,'first-delegation','Buat catatan',?,NULL)", (saved_state,))
    options = {'execution_request':execution_request} if execution_request else {}
    recovered = h.reconcile_completed(store, native, 'session-1', workspace, **options)
    assert recovered['recovered'] is True
    assert recovered['model'] == 'fixture-model'
    assert recovered['file']['write_count'] == 1
    async def must_not_execute(request):
        raise AssertionError('Reconciliation must never re-execute work')
    event = {'type':'delegation.created','item':{'id':'new-voice-id','content':[{'text':'Buat catatan'}]}}
    assert asyncio.run(h.Handoff(store, must_not_execute).handle(event)) == recovered
    assert native.read_bytes() == native_before


def test_hard_crash_receipt_can_be_reconciled_without_resubmission(tmp_path):
    import subprocess
    import sys
    h = load_handoff()
    native, workspace = completed_native_fixture(tmp_path, h)
    store = tmp_path/'handoff.sqlite'
    script = '''import asyncio, importlib.util, os, sys
spec = importlib.util.spec_from_file_location('handoff', sys.argv[1])
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
async def crash(request):
    os._exit(7)
event = {'type':'delegation.created','item':{'id':'crashed','content':[{'text':'Buat catatan'}]}}
asyncio.run(h.Handoff(sys.argv[2], crash).handle(event))
'''
    child = subprocess.run([sys.executable, '-B', '-c', script, str(Path(__file__).with_name('handoff.py')), str(store)], timeout=5)
    assert child.returncode == 7
    with sqlite3.connect(store) as db:
        assert db.execute('SELECT state FROM task').fetchone()[0] == 'running'
    recovered = h.reconcile_completed(store, native, 'session-1', workspace)
    assert recovered['recovered']
    async def must_not_execute(request):
        pytest.fail('A reconciled task must never be resubmitted')
    event = {'type':'delegation.created','item':{'id':'after-crash','content':[{'text':'Buat catatan'}]}}
    assert asyncio.run(h.Handoff(store, must_not_execute).handle(event)) == recovered


@pytest.mark.parametrize('invalid', ['pending', 'failed', 'request', 'workspace', 'saved_query'])
def test_reconciliation_rejects_unproven_completion_without_changing_receipt(tmp_path, invalid):
    h = load_handoff()
    native, workspace = completed_native_fixture(tmp_path, h)
    store = tmp_path/'handoff.sqlite'
    h.Handoff(store, None)
    with sqlite3.connect(store) as db:
        db.execute("INSERT INTO task VALUES (1,'first','Buat catatan','unknown',NULL)")
        before = db.execute('SELECT * FROM task').fetchone()
    with sqlite3.connect(native) as db:
        if invalid == 'pending':
            db.execute("UPDATE session_admissions SET status='started',outcome=NULL")
        elif invalid == 'failed':
            db.execute("UPDATE session_admissions SET outcome='failed'")
        elif invalid == 'request':
            db.execute('UPDATE session_admissions SET payload_json=?', (json.dumps({'text':'Different task. Buat catatan'}),))
        elif invalid == 'workspace':
            db.execute('UPDATE sessions SET cwd=?', (str(tmp_path/'other'),))
    if invalid == 'saved_query':
        (workspace.parent/'request.txt').write_text('Changed local request')
    with pytest.raises(RuntimeError):
        h.reconcile_completed(store, native, 'session-1', workspace)
    with sqlite3.connect(store) as db:
        assert db.execute('SELECT * FROM task').fetchone() == before


def test_reconciliation_refuses_concurrent_receipt_change(tmp_path, monkeypatch):
    h = load_handoff()
    native, workspace = completed_native_fixture(tmp_path, h)
    store = tmp_path/'handoff.sqlite'
    h.Handoff(store, None)
    other_result = {'text':'Result saved by another reconciler'}
    with sqlite3.connect(store) as db:
        db.execute("INSERT INTO task VALUES (1,'first','Buat catatan','running',NULL)")
    read_text = Path.read_text
    def concurrent_read(path, *args, **kwargs):
        if path == workspace/'catatan-uji.txt':
            with sqlite3.connect(store) as db:
                db.execute("UPDATE task SET state='completed',result=?", (json.dumps(other_result),))
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', concurrent_read)
    with pytest.raises(RuntimeError, match='changed during reconciliation'):
        h.reconcile_completed(store, native, 'session-1', workspace)
    with sqlite3.connect(store) as db:
        state, result = db.execute('SELECT state,result FROM task').fetchone()
    assert state == 'completed'
    assert json.loads(result) == other_result


@pytest.mark.parametrize('outcome', ['cancel', 'error', 'complete'])
def test_live_handler_cannot_overwrite_reconciled_completion(tmp_path, outcome):
    h = load_handoff()
    native, workspace = completed_native_fixture(tmp_path, h)
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def execute(request):
            entered.set()
            await release.wait()
            if outcome == 'error':
                raise OSError('Late CLI error after canonical completion')
            return {'text':'Late CLI response'}
        store = tmp_path/'handoff.sqlite'
        event = {'type':'delegation.created','item':{'id':'live','content':[{'text':'Buat catatan'}]}}
        task = asyncio.create_task(h.Handoff(store, execute).handle(event))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            recovered = h.reconcile_completed(store, native, 'session-1', workspace)
            if outcome == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                if outcome == 'error':
                    with pytest.raises(OSError):
                        await task
                else:
                    assert await task == recovered
            with sqlite3.connect(store) as db:
                state, result = db.execute('SELECT state,result FROM task').fetchone()
            assert state == 'completed'
            assert json.loads(result) == recovered
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
