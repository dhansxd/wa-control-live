"""Standalone preparation must not touch Hermes or start network/model clients."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


def test_prepare_pins_binary_and_creates_private_isolated_state(tmp_path):
    from launch import RELEASE_SHA256, prepare, sidecar_env
    source = tmp_path/'identity.local.json'
    source.write_text(json.dumps({'bot_number':'628111111111','owner_number':'628222222222'}))
    binary = tmp_path/'binary'
    binary.write_bytes(b'not-executed-local-fixture')
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    config = prepare(tmp_path, source, binary=binary, expected_sha256=digest)
    private = tmp_path/'.private'
    assert private.stat().st_mode & 0o777 == 0o700
    assert (private/'adapter.token').stat().st_mode & 0o777 == 0o600
    token = (private/'adapter.token').read_text()
    assert len(token) >= 32
    env = sidecar_env(config, {'PATH':'/usr/bin','GEMINI_API_KEY':'never-inherit-key',
                              'OPENAI_API_KEY':'never-inherit-key','OPENAI_LIVE_BACKUP':'never-inherit-key',
                              'HERMES_API_KEY':'never-inherit-key','HWV_CONTEXT_URL':'http://upstream'},
                      live_settings={'providers': {
                          'speech': {'kind': 'gemini', 'accounts': {'a': {'secret_env': 'GEMINI_API_KEY'}}},
                          'live': {'kind': 'gpt-live', 'accounts': {'b': {'secret_env': 'OPENAI_LIVE_BACKUP'}}},
                      }})
    assert env['GEMINI_API_KEY'] == token
    assert 'OPENAI_API_KEY' not in env and 'OPENAI_LIVE_BACKUP' not in env
    assert env['HWV_GEMINI_URL'] == 'ws://127.0.0.1:8797/live'
    assert env['HWV_DATA_DIR'] == str(private/'whatsapp')
    assert env['HERMES_API_KEY'] == env['HWV_CONTEXT_URL'] == ''
    assert env['HWV_MAX_DURATION'] == '180s'
    assert env['HWV_SILENCE_TIMEOUT'] == '180s'
    current = sidecar_env(dict(config, binary_sha256=RELEASE_SHA256), {})
    assert current['HWV_MAX_DURATION'] == current['HWV_SILENCE_TIMEOUT'] == 'off'
    assert env['HWV_SEND_SUMMARY'] == 'false'
    assert env['HWV_OWNER'] == '628222222222'
    assert env['HWV_LISTEN'] == '127.0.0.1:8796'
    again = prepare(tmp_path, source, binary=binary, expected_sha256=digest)
    assert (private/'adapter.token').read_text() == token  # idempotent preparation, not relink
    assert again == config


def test_prepare_rejects_checksum_mismatch_before_writing_state(tmp_path):
    from launch import prepare
    identity = tmp_path/'identity.local.json'
    identity.write_text(json.dumps({'bot_number':'628111111111','owner_number':'628222222222'}))
    binary = tmp_path/'binary'
    binary.write_bytes(b'tampered')
    with pytest.raises(ValueError,match='checksum'):
        prepare(tmp_path, identity, binary=binary, expected_sha256='0'*64)
    assert not (tmp_path/'.private').exists()


@pytest.mark.parametrize('installed', [b'previous-sidecar', b'updated-sidecar', b'other-sidecar'])
def test_prepare_profile_accepts_only_pinned_upgrade_and_rollback(tmp_path, monkeypatch, installed):
    import launch
    releases = {hashlib.sha256(binary).hexdigest() for binary in (b'previous-sidecar', b'updated-sidecar')}
    monkeypatch.setattr(launch, 'PROFILE_RELEASE_SHA256', releases)
    binary = tmp_path/'sidecar'
    binary.write_bytes(installed)
    binary.chmod(0o700)
    private = tmp_path/'state'
    args = dict(owner='628111111111', bot='628222222222', private=private,
                binary=binary, python=binary)
    digest = hashlib.sha256(installed).hexdigest()
    if digest in releases:
        assert launch.prepare_profile(**args)['binary_sha256'] == digest
    else:
        with pytest.raises(ValueError, match='checksum'):
            launch.prepare_profile(**args)
        assert not private.exists()


def test_unified_profile_only_reopens_the_verified_local_build(tmp_path):
    import launch

    binary = tmp_path/'sidecar'
    binary.write_bytes(b'unified-candidate-fixture')
    binary.chmod(0o700)
    private = tmp_path/'state'
    args = dict(owner='628111111111', bot='628222222222', private=private,
                binary=binary, python=binary, unified_gateway=True)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='checksum'):
        launch.prepare_profile(**args)
    with pytest.raises(ValueError, match='unified sidecar build'):
        launch.prepare_profile(**args, sidecar_sha256=launch.RELEASE_SHA256)
    assert not private.exists()
    config = launch.prepare_profile(**args, sidecar_sha256=digest)
    assert launch.prepare_profile(**args) == config
    env = launch.sidecar_env(config, {})
    assert env['HWV_BOT_NUMBER'] == args['bot']
    assert env['HWV_MAX_DURATION'] == env['HWV_SILENCE_TIMEOUT'] == 'off'
    binary.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='checksum mismatch'):
        launch.prepare_profile(**args)


def test_managed_adapter_gets_hermes_dependencies_and_preserves_arguments(tmp_path):
    import launch

    hermes_runtime = tmp_path / 'hermes'
    hermes_runtime.mkdir()
    dependencies = tmp_path / 'dependencies'
    dependencies.mkdir()
    (dependencies / 'voice_dependency.py').write_text('value = "managed"')
    (hermes_runtime / 'hermes_bootstrap.py').write_text(
        'import sys\n' + f'sys.path.insert(0, {str(dependencies)!r})\n'
    )
    stage = tmp_path / 'plugin'
    stage.mkdir()
    (stage / 'adapter_sibling.py').write_text('value = "sibling"')
    script = stage / 'call_adapter.py'
    script.write_text(
        'import sys\n'
        'import voice_dependency, adapter_sibling\n'
        'print(voice_dependency.value, adapter_sibling.value, *sys.argv[1:])\n'
    )
    binary = tmp_path / 'sidecar'
    binary.write_bytes(b'unified-candidate-fixture')
    binary.chmod(0o700)
    config = launch.prepare_profile(
        owner='628111111111', bot='628222222222', private=tmp_path / 'state',
        binary=binary, python=sys.executable, hermes_runtime=hermes_runtime,
        unified_gateway=True, sidecar_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
    )
    assert config['hermes_runtime'] == str(hermes_runtime)
    command = launch._adapter_command(config, stage)
    result = subprocess.run([*command, '--runs', 'with spaces'], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'managed sibling --runs with spaces'
    assert launch._adapter_command({'python': '/legacy/python'}, stage) == ['/legacy/python', str(script)]


def test_serve_refuses_unpaired_without_starting_children(tmp_path, monkeypatch):
    import launch
    started = []
    monkeypatch.setattr(launch.subprocess,'Popen',lambda *a,**kw:started.append(a))
    with pytest.raises(RuntimeError,match='pair'):
        launch.serve({'private':str(tmp_path), 'stage':str(tmp_path)})
    assert not started


@pytest.mark.parametrize('has_token', [False, True])
def test_status_reads_existing_state_without_preparing_or_writing(tmp_path, monkeypatch, capsys, has_token):
    import launch
    if has_token:
        private = tmp_path/'.private'
        private.mkdir()
        (private/'adapter.token').write_text('local-test-token')
    before = {str(path.relative_to(tmp_path)):path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail('Status must read existing state without preparation')
    monkeypatch.setattr(launch, 'STAGE', tmp_path)
    monkeypatch.setattr(launch, 'prepare', forbidden)
    monkeypatch.setattr(launch, 'read_health', lambda port, token: {'reachable':True})
    monkeypatch.setattr('sys.argv', ['launch.py', 'status'])
    launch.main()
    result = json.loads(capsys.readouterr().out)
    assert result['paired_marker'] is False
    assert all(service['reachable'] == has_token for service in result['services'].values())
    after = {str(path.relative_to(tmp_path)):path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert after == before
    assert not (tmp_path/'.private/config.json').exists()


@pytest.mark.parametrize('phone', [None, '628222222222:7@s.whatsapp.net', '628111111111:7@s.whatsapp.net'])
def test_pair_requires_saved_expected_bot_identity(tmp_path, monkeypatch, phone):
    import sqlite3
    import launch
    private = tmp_path/'.private'
    store = private/'whatsapp/whatsapp.db'
    store.parent.mkdir(parents=True)
    marker = private/'paired.json'
    marker.write_text('stale prior pairing')
    with sqlite3.connect(store) as db:
        db.execute('CREATE TABLE device (pn TEXT)')
        db.execute('INSERT INTO device VALUES (?)', (phone,))
    config = {'private':str(private), 'bot_number':'628111111111', 'binary':'never-executed'}
    monkeypatch.setattr(launch, 'sidecar_env', lambda _config: {})
    monkeypatch.setattr(launch.subprocess, 'run', lambda *args, **kwargs:type('Result', (), {'returncode':0})())
    if phone == '628111111111:7@s.whatsapp.net':
        launch.pair(config)
        assert isinstance(json.loads(marker.read_text())['paired_at'], int)
    else:
        with pytest.raises(RuntimeError, match='bot identity'):
            launch.pair(config)
        assert not marker.exists()


def test_first_pair_keeps_only_a_verified_bot_session(tmp_path, monkeypatch):
    from contextlib import closing
    import sqlite3
    from types import SimpleNamespace
    import launch

    expected = '628111111111'
    scanned = iter(['628222222222', expected])
    config = {'private': str(tmp_path), 'bot_number': expected, 'binary': 'never-executed'}
    monkeypatch.setattr(launch, 'sidecar_env', lambda _: {})

    def scan(*_, env, **kwargs):
        whatsapp = Path(env['HWV_DATA_DIR'])
        whatsapp.mkdir()
        with closing(sqlite3.connect(whatsapp/'whatsapp.db')) as db, db:
            db.execute('CREATE TABLE device (pn TEXT)')
            db.execute('INSERT INTO device VALUES (?)', (next(scanned)+'@s.whatsapp.net',))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launch.subprocess, 'run', scan)
    with pytest.raises(RuntimeError, match='bot identity'):
        launch.pair(config)
    assert not (tmp_path/'whatsapp').exists()
    assert not (tmp_path/'paired.json').exists()
    launch.pair(config)
    launch.verify_paired_identity(config)
    assert (tmp_path/'paired.json').is_file()
    assert not list(tmp_path.glob('pairing-*'))


def test_serve_rejects_legacy_marker_with_wrong_bot_before_starting_children(tmp_path, monkeypatch):
    import sqlite3
    import launch
    private = tmp_path/'.private'
    store = private/'whatsapp/whatsapp.db'
    store.parent.mkdir(parents=True)
    (private/'paired.json').write_text('legacy marker')
    with sqlite3.connect(store) as db:
        db.execute('CREATE TABLE device (pn TEXT)')
        db.execute("INSERT INTO device VALUES ('628222222222:7@s.whatsapp.net')")
    def forbidden(*args, **kwargs):
        pytest.fail('A mismatched legacy voice identity started children')
    monkeypatch.setattr(launch.subprocess, 'Popen', forbidden)
    with pytest.raises(RuntimeError, match='bot identity'):
        launch.serve({'private':str(private), 'stage':str(tmp_path), 'bot_number':'628111111111'})


def test_bot_unlink_cleans_only_the_matched_pairing(tmp_path):
    import launch

    bot = '15550100102'
    other = '15550100103'
    config = {'private': str(tmp_path), 'bot_number': bot, 'unified_gateway': True}
    marker = tmp_path/'paired.json'
    marker.touch()
    store = tmp_path/'whatsapp'
    store.mkdir()
    (store/'whatsapp.db').touch()
    owned = tmp_path/'outbound-receipts'/bot
    unrelated = tmp_path/'outbound-receipts'/other
    owned.mkdir(parents=True)
    unrelated.mkdir()
    request = tmp_path/'unlink-request.json'
    request.write_text(json.dumps({'bot_number': other, 'remote_unlinked': True}))
    with pytest.raises(RuntimeError, match='does not match'):
        launch.finish_bot_unlink(config)
    assert marker.exists() and store.exists() and owned.exists()
    request.write_text(json.dumps({'bot_number': bot, 'remote_unlinked': False}))
    assert launch.finish_bot_unlink(config)
    assert not any(path.exists() for path in (marker, store, owned, request))
    assert unrelated.exists()


def test_purge_profile_runtime_does_not_follow_links_and_reports_each_saved_device(tmp_path):
    from contextlib import closing
    import sqlite3
    import launch

    bot = '628111111111'
    root = tmp_path/'plugin-data'
    runtime = root/'unified'
    old_device = tmp_path/'old-device.json'
    old_device.write_text('other installation fixture')
    for private in (root, runtime):
        (private/'whatsapp').mkdir(parents=True)
        with closing(sqlite3.connect(private/'whatsapp/whatsapp.db')) as db, db:
            db.execute('CREATE TABLE device (pn TEXT)')
            db.execute('INSERT INTO device VALUES (?)', (bot+'@s.whatsapp.net',))
        (private/'config.json').write_text(json.dumps({'bot_number': bot}))
        (private/'adapter.token').write_text('synthetic token')
    (runtime/'link').symlink_to(old_device)
    (runtime/'cleanup-unlink.json').write_text(json.dumps({'bot_number': bot, 'remote_unlinked': True}))
    (root/'cleanup-unlink.json').write_text(json.dumps({'bot_number': '628222222222', 'remote_unlinked': True}))

    report = launch.cleanup_profile_runtime(root, purge=True)
    assert report['unified_runtime_removed'] and report['legacy_runtime_removed']
    assert {(row['runtime'], row['bot_number'], row['remote_unlinked']) for row in report['runtime_accounts']} == {
        ('unified', bot, True), ('legacy', bot, False),
    }
    assert {path.name for path in runtime.iterdir()} == {'transport.lock', 'call-now.lock'}
    assert {path.name for path in root.iterdir()} == {'unified', 'transport.lock', 'call-now.lock', 'cleanup-purge.lock'}
    assert old_device.read_text() == 'other installation fixture'


def test_purge_refuses_another_installations_runtime_link(tmp_path):
    import launch

    root = tmp_path/'profile-plugin-data'
    root.mkdir()
    outside = tmp_path/'other-installation'
    outside.mkdir()
    (outside/'log').write_text('keep')
    (root/'unified').symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match='belong to this profile'):
        launch.cleanup_profile_runtime(root, purge=True)
    assert (outside/'log').read_text() == 'keep'


def test_purge_leaves_both_runtime_modes_if_owned_transport_does_not_stop(tmp_path, monkeypatch):
    import launch

    root = tmp_path/'profile-plugin-data'
    unified = root/'unified'
    unified.mkdir(parents=True)
    for private in (root, unified):
        (private/'adapter.log').touch()
    take_lock = launch._take_lock
    monkeypatch.setattr(launch, '_take_lock', lambda fd, _wait, message: take_lock(fd, 0, message))

    with launch.owned_transport({'private': str(unified)}):
        with pytest.raises(RuntimeError, match='already owned'):
            launch.cleanup_profile_runtime(root, purge=True)
    assert all((private/'adapter.log').exists() for private in (root, unified))


@pytest.mark.parametrize(('unified', 'purge'), [(False, False), (False, True), (True, True)])
def test_profile_cleanup_stops_owned_live_children_before_deleting(tmp_path, monkeypatch, unified, purge):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from types import SimpleNamespace
    import time
    import launch

    root = tmp_path/'plugin-data'
    private = root/'unified' if unified else root
    (private/'whatsapp').mkdir(parents=True)
    for name in ('paired.json', 'whatsapp/whatsapp.db', 'adapter.token'):
        (private/name).touch()
    bot = '628111111111'
    (private/'config.json').write_text(json.dumps({'bot_number': bot}))
    config = {'private': str(private), 'stage': str(tmp_path), 'python': 'fake-adapter',
              'binary': 'fake-sidecar', 'bot_number': bot, 'unified_gateway': unified}
    monkeypatch.setattr(launch, 'verify_paired_identity', lambda _: None)
    monkeypatch.setattr(launch, 'sidecar_env', lambda _: {})
    monkeypatch.setattr(launch, 'read_health', lambda *_: {})
    monkeypatch.setattr(launch.os, 'umask', lambda *_: None)
    probe = SimpleNamespace(setsockopt=lambda *_: None, bind=lambda *_: None)
    monkeypatch.setattr(launch.socket, 'socket', lambda: nullcontext(probe))
    children, terminated, requested = [], [], []

    def spawn(args, **_):
        child = SimpleNamespace(name=args[0], returncode=None)
        child.poll = lambda: child.returncode
        children.append(child)
        return child

    def terminate(child):
        if child is not None:
            terminated.append(child.name)
            child.returncode = 0

    def unlink(_):
        requested.append(bot)
        (private/'cleanup-unlink.json').write_text(json.dumps({'bot_number': bot, 'remote_unlinked': True}))

    def before_delete():
        assert children and all(child.poll() == 0 for child in children)
        assert (private/'whatsapp/whatsapp.db').exists()

    def cleanup():
        deadline = time.monotonic() + 3
        while not (private/'serving-identity.json').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (private/'serving-identity.json').exists()
        return launch.cleanup_profile_runtime(root, purge=purge, before_delete=before_delete)

    monkeypatch.setattr(launch.subprocess, 'Popen', spawn)
    monkeypatch.setattr(launch, 'terminate_owned', terminate)
    monkeypatch.setattr(launch, '_request_active_bot_unlink', unlink)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cleanup)
        launch.serve(config, is_active=lambda: not pending.done())
        report = pending.result(timeout=4)
    assert terminated == ['fake-sidecar', 'fake-adapter']
    assert report['runtime_data_retained'] is not purge
    assert (private/'whatsapp/whatsapp.db').exists() is not purge
    assert requested == ([bot] if unified and purge else [])
    if purge:
        assert report['runtime_accounts'][0]['remote_unlinked'] is unified


def test_launcher_waits_for_own_control_unlink_report_when_bot_logs_out(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import launch

    private = tmp_path/'unified'
    (private/'whatsapp').mkdir(parents=True)
    for name in ('paired.json', 'whatsapp/whatsapp.db', 'adapter.token'):
        (private/name).touch()
    config = {'private': str(private), 'stage': str(tmp_path), 'python': 'fake-adapter',
              'binary': 'fake-sidecar', 'bot_number': '628111111111',
              'hermes_home': str(tmp_path), 'unified_gateway': True}
    monkeypatch.setattr(launch, 'verify_paired_identity', lambda _: None)
    monkeypatch.setattr(launch, 'sidecar_env', lambda _: {})
    monkeypatch.setattr(launch, 'read_health', lambda *_: {})
    monkeypatch.setattr(launch.os, 'umask', lambda *_: None)
    probe = SimpleNamespace(setsockopt=lambda *_: None, bind=lambda *_: None)
    monkeypatch.setattr(launch.socket, 'socket', lambda: nullcontext(probe))
    children, waited, terminated = {}, [], []

    def spawn(args, **_):
        child = SimpleNamespace(name=args[0], pid=len(children)+100, returncode=None)
        child.poll = lambda: child.returncode

        def wait(*, timeout):
            waited.append((child.name, timeout))
            child.returncode = 0
            return 0

        child.wait = wait
        children[child.name] = child
        return child

    def logout(_):
        control = children['fake-control']
        (private/'purge-in-progress.json').write_text(json.dumps({'control_pid': control.pid}))
        children['fake-sidecar'].returncode = 0

    def terminate(child):
        if child is not None and child.poll() is None:
            terminated.append(child.name)
            child.returncode = 0

    monkeypatch.setattr(launch.subprocess, 'Popen', spawn)
    monkeypatch.setattr(launch.time, 'sleep', logout)
    monkeypatch.setattr(launch, 'terminate_owned', terminate)
    launch.serve(config, control_command=['fake-control'])

    assert waited == [('fake-control', 55)]
    assert terminated == ['fake-adapter']


@pytest.mark.parametrize('keep_listener', [False, True])
def test_serve_port_probe_distinguishes_time_wait_from_live_listener(tmp_path, monkeypatch, keep_listener):
    import os
    import socket
    import sqlite3
    import sys
    import launch
    real_socket = socket.socket
    listener = real_socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    address = listener.getsockname()
    if not keep_listener:
        client = socket.create_connection(address)
        connection, _ = listener.accept()
        connection.shutdown(socket.SHUT_WR)
        assert client.recv(1) == b''
        client.close()
        assert connection.recv(1) == b''
        connection.close()
        listener.close()

    class Probe:
        def __init__(self):
            self.socket = real_socket()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.socket.close()
        def setsockopt(self, *args):
            self.socket.setsockopt(*args)
        def bind(self, _address):
            self.socket.bind(address)

    class StartupReached(Exception):
        pass

    def stop_before_children(*args, **kwargs):
        raise StartupReached
    private = tmp_path/'.private'
    store = private/'whatsapp/whatsapp.db'
    store.parent.mkdir(parents=True)
    (private/'paired.json').write_text('paired fixture')
    with sqlite3.connect(store) as db:
        db.execute('CREATE TABLE device (pn TEXT)')
        db.execute("INSERT INTO device VALUES ('628111111111:7@s.whatsapp.net')")
    monkeypatch.setattr(launch.socket, 'socket', Probe)
    monkeypatch.setattr(launch.subprocess, 'Popen', stop_before_children)
    previous_umask = os.umask(0o077)
    try:
        with pytest.raises(OSError if keep_listener else StartupReached):
            launch.serve({'private':str(private), 'stage':str(tmp_path),
                          'bot_number':'628111111111', 'python':sys.executable})
    finally:
        listener.close()
        os.umask(previous_umask)
