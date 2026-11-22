"""A one-task, persistent at-most-once boundary for this bounded voice spike."""
import asyncio
import json
import re
import sqlite3
from pathlib import Path


def task_prompt(request):
    return (
        'Kerjakan permintaan suara berikut dengan tools file Hermes, bukan simulasi:\n' + request +
        '\nIni sesi uji terpisah. Semua pekerjaan hanya di direktori kerja saat ini. '
        'Nama file catatan uji adalah catatan-uji.txt. Tulis isinya sesuai permintaan, '
        'lalu baca file tersebut memakai read_file untuk memeriksa hasil nyata. '
        'Jangan ubah file lain atau instruksi/skills/memory. '
        'Jawab singkat dalam bahasa Indonesia: status sebenarnya dan isi file yang terbaca.'
    )


def verify_tool_errors(events, workspace):
    """Waive only a proven missing-note preflight followed by a proven write/read."""
    target = Path(workspace).resolve()/'catatan-uji.txt'
    def same_target(event):
        path = Path(event.get('input', {}).get('path', ''))
        return (path if path.is_absolute() else target.parent/path).resolve() == target
    def payload(event):
        value = event.get('output', '')
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = {'content': value}
        return value if isinstance(value, dict) else {}
    def failed(event):
        return bool(event.get('is_error') or payload(event).get('error'))
    errors = [(i, e) for i, e in enumerate(events) if e.get('type') == 'tool_result' and failed(e)]
    if not errors:
        return
    uses, results = {}, {}
    for i, event in enumerate(events):
        mapping = uses if event.get('type') == 'tool_use' else results if event.get('type') == 'tool_result' else None
        if mapping is not None:
            mapping.setdefault(event.get('tool_call_id'), []).append((i, event))
    writes = [(i, e) for i, e in enumerate(events) if e.get('type') == 'tool_use' and e.get('name') == 'write_file']
    if len(writes) != 1 or not same_target(writes[0][1]):
        raise RuntimeError('Native error recovery requires one target write')
    write_index, write = writes[0]
    for i, error in errors:
        item_id = error.get('tool_call_id')
        matches = uses.get(item_id, [])
        if (not isinstance(item_id, str) or not item_id or len(matches) != 1
            or len(results.get(item_id, [])) != 1 or matches[0][0] >= i or i >= write_index
            or error.get('name') != 'read_file' or matches[0][1].get('name') != 'read_file'
            or not same_target(matches[0][1]) or payload(error).get('not_found') is not True):
            raise RuntimeError('Native file tool returned an unrecovered error')
    def successful_result(use_index, use):
        item_id = use.get('tool_call_id')
        matches = results.get(item_id, [])
        if (not isinstance(item_id, str) or not item_id or len(uses.get(item_id, [])) != 1
            or len(matches) != 1 or matches[0][0] <= use_index or failed(matches[0][1])
            or matches[0][1].get('name') != use.get('name')):
            return None
        return matches[0]
    written = successful_result(write_index, write)
    if written is not None:
        for i, read in enumerate(events):
            if i > written[0] and read.get('type') == 'tool_use' and read.get('name') == 'read_file' and same_target(read):
                result = successful_result(i, read)
                content = payload(result[1]).get('content') if result is not None else None
                if isinstance(content, str) and re.sub(r'(?m)^\d+\|', '', content).strip() == 'tes suara berhasil':
                    return
    raise RuntimeError('Native preflight error lacks correlated successful write and post-write readback')


async def run_hermes(request, run_dir, *, command=None, timeout=180, hermes_home=None):
    """Run the real CLI by default. An explicit command is only for offline tests."""
    import hashlib
    import os
    import shutil
    import signal
    import time
    run_dir = Path(run_dir)
    workspace = run_dir / 'workspace'
    workspace.mkdir(parents=True, exist_ok=False)
    query = run_dir / 'request.txt'
    query.write_text(task_prompt(request))
    argv = list(command or [shutil.which('hermes') or 'hermes']) + [
        'chat', '--oneshot', '--query-file', str(query.resolve()), '--format', 'stream-json',
        '--source', 'tool', '--in', str(workspace.resolve()), '-t', 'file', '--max-turns', '8',
    ]
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'VIRTUAL_ENV', 'HERMES_SESSION_ID', 'HERMES_TUI_GATEWAY_URL'):
        env.pop(key, None)
    for key in list(env):
        if key.startswith('HERMES_KANBAN_'):
            env.pop(key)
    if hermes_home is None:
        # Keep standalone routing independent of the parent's profile environment.
        env['HERMES_HOME'] = str(Path.home() / '.hermes')
    else:
        env['HERMES_HOME'] = str(Path(hermes_home).expanduser().resolve())
        # The stock CLI otherwise lets a sticky profile override the default root.
        env['HERMES_GATEWAY_PROFILE_PIN'] = '1'
    begin = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, start_new_session=True, limit=2**21,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        (run_dir/'unresolved.json').write_text(json.dumps({
            'cli_pid': process.pid, 'cli_exited': process.returncode is not None,
            'remote_outcome': 'unknown; CLI detach does not cancel a gateway task',
            'retry': False,
        }, indent=2))
        raise
    (run_dir/'events.jsonl').write_bytes(stdout)
    (run_dir/'stderr.txt').write_bytes(stderr)
    events = [json.loads(line) for line in stdout.decode().splitlines() if line.strip()]
    finals = [e for e in events if e.get('type') == 'result']
    if len(finals) != 1 or process.returncode or finals[0].get('exit_code') != 0 or not finals[0].get('text'):
        raise RuntimeError('Hermes did not return one successful terminal result; inspect saved events, never replay')
    final = finals[0]
    uses = [e for e in events if e.get('type') == 'tool_use']
    writes = [e for e in uses if e.get('name') == 'write_file']
    reads = [e for e in uses if e.get('name') == 'read_file']
    target = workspace/'catatan-uji.txt'
    def same_target(event):
        path = Path(event.get('input', {}).get('path', ''))
        return (path if path.is_absolute() else workspace/path).resolve() == target.resolve()
    if len(writes) != 1 or not same_target(writes[0]) or not any(same_target(e) for e in reads):
        raise RuntimeError('Expected one native write_file and a read_file for the test note')
    verify_tool_errors(events, workspace)
    content = target.read_text()
    if content.strip() != 'tes suara berhasil':
        raise RuntimeError('Independent file readback does not match the spoken task')
    result = {
        'text': final['text'], 'session_id': final['session_id'],
        'exit_code': process.returncode, 'cli_pid': process.pid, 'cli_exited': True,
        'model': next((e.get('model') for e in events if e.get('subtype') == 'init'), None),
        'elapsed_seconds': round(time.monotonic()-begin, 3),
        'file': {'path': str(target.resolve()), 'content': content,
                 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                 'write_count': len(writes), 'read_count': sum(same_target(e) for e in reads)},
    }
    (run_dir/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def reconcile_completed(store, native_db, session_id, workspace, *, execution_request=None):
    """Explicit repair from a completed canonical receipt; never submit another prompt."""
    import hashlib
    workspace = Path(workspace).resolve()
    with sqlite3.connect(store) as db:
        row = db.execute('SELECT item, request, state, result FROM task WHERE id=1').fetchone()
    if row is None or row[2] not in {'running', 'unknown'}:
        raise RuntimeError('Only an unresolved saved task may be reconciled')
    submitted = task_prompt(row[1] if execution_request is None else execution_request)
    query = workspace.parent/'request.txt'
    if query.exists() and query.read_text() != submitted:
        raise RuntimeError('Saved submitted prompt does not match the recovery request')
    with sqlite3.connect(Path(native_db).resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        session = db.execute('SELECT model, billing_provider, cwd FROM sessions WHERE id=?', (session_id,)).fetchone()
        receipts = db.execute('SELECT status, outcome, payload_json FROM session_admissions WHERE target_session_id=?', (session_id,)).fetchall()
        messages = db.execute('SELECT role, content, tool_calls, tool_name, tool_call_id FROM messages WHERE session_id=? ORDER BY id', (session_id,)).fetchall()
    if not session or Path(session[2]).resolve() != workspace:
        raise RuntimeError('Native session workspace mismatch')
    if len(receipts) != 1 or receipts[0][:2] != ('terminal', 'completed') or json.loads(receipts[0][2]).get('text') != submitted:
        raise RuntimeError('Native receipt does not prove this task completed')
    calls, proof_events = [], []
    for role, content, tool_calls, tool_name, tool_id in messages:
        if tool_calls:
            for call in json.loads(tool_calls):
                function = call['function']
                args = function['arguments']
                args = json.loads(args) if isinstance(args, str) else args
                path = Path(args.get('path', ''))
                path = path if path.is_absolute() else workspace/path
                calls.append((function['name'], path.resolve()))
                proof_events.append({'type':'tool_use', 'name':function['name'], 'input':args,
                                     'tool_call_id':call.get('id')})
        if role == 'tool':
            proof_events.append({'type':'tool_result', 'name':tool_name, 'tool_call_id':tool_id,
                                 'output':content})
    target = workspace/'catatan-uji.txt'
    writes = [c for c in calls if c[0] == 'write_file']
    reads = [c for c in calls if c == ('read_file', target)]
    if writes != [('write_file', target)] or not reads:
        raise RuntimeError('Native history must show exactly one note write and its readback')
    outputs = [json.loads(content) for role, content, tc, tool_name, tid in messages if role == 'tool']
    verify_tool_errors(proof_events, workspace)
    if not any('tes suara berhasil' in out.get('content', '') for out in outputs):
        raise RuntimeError('Native readback evidence missing or tool error present')
    content = target.read_text()
    if content.strip() != 'tes suara berhasil':
        raise RuntimeError('Independent file readback mismatch')
    texts = [content for role, content, tc, tn, tid in messages if role == 'assistant' and content and not tc]
    if not texts or 'tes suara berhasil' not in texts[-1]:
        raise RuntimeError('No corresponding final Hermes response')
    result = {'text': texts[-1], 'session_id': session_id, 'model': session[0], 'provider': session[1],
              'recovered': True, 'native_outcome': 'completed',
              'file': {'path': str(target), 'content': content, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                       'write_count': len(writes), 'read_count': len(reads)}}
    with sqlite3.connect(store) as db:
        changed = db.execute(
            "UPDATE task SET state='completed', result=? WHERE id=1 AND item IS ? AND request=? AND state=? AND result IS ?",
            (json.dumps(result, ensure_ascii=False), *row),
        ).rowcount
        if changed != 1:
            raise RuntimeError('Saved task changed during reconciliation; recovery was not applied')
    return result


class Handoff:
    def __init__(self, path, execute):
        self.path = path
        self.execute = execute
        self.lock = asyncio.Lock()
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS task (id INTEGER PRIMARY KEY CHECK (id=1), item TEXT, request TEXT, state TEXT, result TEXT)')

    async def handle(self, event):
        item = event['item']
        item_id = item['id']
        request = ''.join(c.get('text', '') for c in item['content']).strip()
        if event.get('type') != 'delegation.created' or not item_id or not request:
            raise ValueError('Expected a nonempty delegation.created item')
        async with self.lock:
            with sqlite3.connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT item, request, state, result FROM task WHERE id=1').fetchone()
                if row:
                    if row[1] != request:
                        raise RuntimeError('One-task budget exhausted; no second request executed')
                    if row[2] != 'completed':
                        raise RuntimeError('Previous task outcome unresolved or failed; never auto-replay')
                    return json.loads(row[3])
                db.execute('INSERT INTO task VALUES (1, ?, ?, ?, NULL)', (item_id, request, 'running'))
            try:
                result = await self.execute(request)
            except BaseException:
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE task SET state='unknown' WHERE id=1 AND item=? AND request=? AND state='running'", (item_id, request))
                raise
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE task SET state='completed', result=? WHERE id=1 AND item=? AND request=? AND state='running'",
                           (json.dumps(result, ensure_ascii=False), item_id, request))
                saved = db.execute("SELECT result FROM task WHERE id=1 AND item=? AND request=? AND state='completed'", (item_id, request)).fetchone()
                if saved is None:
                    raise RuntimeError('Saved task changed before completion; never auto-replay')
            return json.loads(saved[0])
