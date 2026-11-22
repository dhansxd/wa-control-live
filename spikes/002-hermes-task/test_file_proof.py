"""A missing target before creation needs correlated successful file proof."""
import asyncio
import json
import sqlite3
import sys

import pytest

from test_handoff import completed_native_fixture, load_handoff


def tool_call(name, call_id, path='catatan-uji.txt'):
    arguments = {'path':path}
    if name == 'write_file':
        arguments['content'] = 'tes suara berhasil'
    return {'type':'tool_use', 'name':name, 'tool_call_id':call_id, 'input':arguments}


def tool_result(name, call_id, output, *, error=False):
    return {'type':'tool_result', 'name':name, 'tool_call_id':call_id,
            'is_error':error, 'output':json.dumps(output)}


MISSING = {'error':'File not found', 'not_found':True}
PREFLIGHT = [tool_call('read_file', 'preflight-read'),
             tool_result('read_file', 'preflight-read', MISSING, error=True)]
WRITE_CALL = tool_call('write_file', 'note-write')
WRITE_RESULT = tool_result('write_file', 'note-write', {'verified':True, 'bytes_written':18})
READ_CALL = tool_call('read_file', 'note-read')
READ_RESULT = tool_result('read_file', 'note-read', {'content':'1|tes suara berhasil', 'not_found':False})
VALID = [WRITE_CALL, WRITE_RESULT, READ_CALL, READ_RESULT]


def proof_cli(tmp_path, events):
    """An explicit local subprocess fixture creates only its temporary note."""
    script = tmp_path/'file_proof_cli.py'
    trace = [{'type':'system', 'subtype':'init', 'model':'offline-file-proof'}, *events,
             {'type':'result', 'session_id':'proof-session', 'exit_code':0,
              'text':'File dibuat. Isinya tes suara berhasil.'}]
    script.write_text(
        'import json, pathlib, sys\n'
        "workspace = pathlib.Path(sys.argv[sys.argv.index('--in')+1])\n"
        f'events = json.loads({json.dumps(trace)!r})\n'
        'for event in events:\n'
        "    if event.get('type') == 'tool_use' and event.get('name') == 'write_file':\n"
        "        (workspace/'catatan-uji.txt').write_text('tes suara berhasil')\n"
        '    print(json.dumps(event), flush=True)\n'
    )
    return [sys.executable, '-B', str(script)]


def test_missing_preflight_read_allows_one_verified_write_and_later_read(tmp_path):
    handoff = load_handoff()
    result = asyncio.run(handoff.run_hermes('Buat catatan', tmp_path/'run',
                                          command=proof_cli(tmp_path, PREFLIGHT + VALID)))
    assert result['exit_code'] == 0
    assert result['file']['content'] == 'tes suara berhasil'
    assert result['file']['write_count'] == 1 and result['file']['read_count'] >= 1
    assert result['session_id'] == 'proof-session'
    assert json.loads((tmp_path/'run/result.json').read_text()) == result


@pytest.mark.parametrize('events', [
    pytest.param(PREFLIGHT + [WRITE_CALL, WRITE_RESULT,
                 tool_call('read_file', 'missing-after-write'),
                 tool_result('read_file', 'missing-after-write', MISSING, error=True),
                 READ_CALL, READ_RESULT], id='missing-after-write'),
    pytest.param([tool_call('read_file', 'wrong-target', 'other.txt'),
                  tool_result('read_file', 'wrong-target', MISSING, error=True)] + VALID,
                 id='preflight-error-for-another-target'),
    pytest.param([PREFLIGHT[0], tool_result('read_file', 'preflight-read',
                  {'error':'Permission denied', 'not_found':False}, error=True)] + VALID,
                 id='preflight-permission-error'),
    pytest.param(PREFLIGHT + [WRITE_CALL,
                  tool_result('write_file', 'note-write', {'error':'Write failed'}, error=True),
                  READ_CALL, READ_RESULT], id='write-error'),
    pytest.param([PREFLIGHT[0], tool_result('read_file', 'unrelated-preflight', MISSING, error=True)] + VALID,
                 id='preflight-result-id-mismatch'),
    pytest.param(PREFLIGHT + [WRITE_CALL,
                  tool_result('write_file', 'unrelated-write', {'verified':True}), READ_CALL, READ_RESULT],
                 id='write-result-id-mismatch'),
    pytest.param(PREFLIGHT + [WRITE_CALL, WRITE_RESULT, READ_CALL,
                  tool_result('read_file', 'unrelated-read', {'content':'1|tes suara berhasil'})],
                 id='read-result-id-mismatch'),
    pytest.param(PREFLIGHT + [READ_CALL, READ_RESULT, WRITE_CALL, WRITE_RESULT],
                 id='successful-read-before-write'),
    pytest.param(PREFLIGHT + [WRITE_CALL, READ_CALL, WRITE_RESULT, READ_RESULT],
                 id='read-called-before-write-confirmed'),
    pytest.param(PREFLIGHT + [WRITE_CALL, WRITE_RESULT, READ_RESULT, READ_CALL],
                 id='read-result-before-its-call'),
    pytest.param(PREFLIGHT + [WRITE_CALL, WRITE_RESULT, READ_CALL,
                  tool_result('read_file', 'note-read', {'content':'1|different content'})],
                 id='read-result-content-mismatch'),
    pytest.param(PREFLIGHT + [tool_call('read_file', 'another-error'),
                  tool_result('read_file', 'another-error', {'error':'Input/output error'}, error=True)] + VALID,
                 id='another-tool-error'),
    pytest.param(PREFLIGHT + [WRITE_CALL, READ_CALL, READ_RESULT], id='missing-write-result'),
    pytest.param(PREFLIGHT + [WRITE_CALL, WRITE_RESULT, READ_CALL], id='missing-read-result'),
])
def test_preflight_exception_rejects_unproven_or_failed_file_operations(tmp_path, events):
    handoff = load_handoff()
    with pytest.raises(RuntimeError):
        asyncio.run(handoff.run_hermes('Buat catatan', tmp_path/'run',
                                      command=proof_cli(tmp_path, events)))
    assert not (tmp_path/'run/result.json').exists()


@pytest.mark.parametrize('case', ['verified', 'read-id-mismatch', 'read-before-write', 'post-write-missing'])
def test_recovery_applies_the_same_correlated_preflight_exception(tmp_path, case):
    handoff = load_handoff()
    native, workspace = completed_native_fixture(tmp_path, handoff)
    if case == 'read-id-mismatch':
        events = PREFLIGHT + [WRITE_CALL, WRITE_RESULT, READ_CALL,
                  tool_result('read_file', 'wrong-id', {'content':'1|tes suara berhasil'})]
    elif case == 'read-before-write':
        events = PREFLIGHT + [READ_CALL, READ_RESULT, WRITE_CALL, WRITE_RESULT]
    elif case == 'post-write-missing':
        events = PREFLIGHT + [WRITE_CALL, WRITE_RESULT,
                  tool_call('read_file', 'late-missing'),
                  tool_result('read_file', 'late-missing', MISSING, error=True), READ_CALL, READ_RESULT]
    else:
        events = PREFLIGHT + VALID
    rows = []
    for number, event in enumerate(events, 1):
        if event['type'] == 'tool_use':
            calls = [{'id':event['tool_call_id'], 'type':'function', 'function':{
                      'name':event['name'], 'arguments':json.dumps(event['input'])}}]
            rows.append((number, 'session-1', 'assistant', '', json.dumps(calls), None, None))
        else:
            rows.append((number, 'session-1', 'tool', event['output'], None,
                         event['name'], event['tool_call_id']))
    rows.append((len(rows)+1, 'session-1', 'assistant', 'File dibuat. Isinya tes suara berhasil.', None, None, None))
    with sqlite3.connect(native) as db:
        db.execute('DELETE FROM messages')
        db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', rows)
    store = tmp_path/'handoff.sqlite'
    handoff.Handoff(store, None)
    with sqlite3.connect(store) as db:
        db.execute("INSERT INTO task VALUES (1,'saved-task','Buat catatan','unknown',NULL)")
    if case == 'verified':
        result = handoff.reconcile_completed(store, native, 'session-1', workspace)
        assert result['recovered'] and result['file']['content'] == 'tes suara berhasil'
        expected_state = 'completed'
    else:
        with pytest.raises(RuntimeError):
            handoff.reconcile_completed(store, native, 'session-1', workspace)
        expected_state = 'unknown'
    with sqlite3.connect(store) as db:
        state, result = db.execute('SELECT state, result FROM task').fetchone()
        assert state == expected_state
        if case != 'verified':
            assert result is None
