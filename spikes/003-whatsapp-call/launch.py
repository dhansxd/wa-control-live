"""Standalone owner-only voice spike. No Hermes install, config edits or restart."""
import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.request

STAGE = Path(__file__).resolve().parent
RELEASE_SHA256 = 'dd41378d2cc15d3ea40b1c2c19fc27c4974fdde0ca47e643ade27bf7034c4c7a'
PROFILE_RELEASE_SHA256 = frozenset({
    RELEASE_SHA256,
    '79c5bf9c04478bf66ff97197d050c5b54d62793871c9c5301272a57774df3502',  # media11 rollback
    '152771cef08f350a6b14c5ec409d7bf375757e99828c5ca54573e20c7cdd1d88',  # media10 rollback
    'fc9111ce3d68e774d82437c37d65e9388baf5a13c58ba50fe4a4b735dd7eec5d',  # media9 rollback
    '8399cd785b191853aeb7d1513f7f006b7c3a9abc683d7f822d770601a85b07e6',  # media8 rollback
    '11f5485ef3ecd4ddc5916179a01f50bdba188891f6a158407b3f6654eaaddac1',  # media7 rollback
})


def prepare(stage, identity, *, binary=None, expected_sha256=RELEASE_SHA256):
    stage = Path(stage).resolve()
    binary = Path(binary) if binary is not None else stage/'bin/hermes-wa-voice_opus-media12_darwin_arm64'
    account = json.loads(Path(identity).read_text())
    return _prepare_state(stage, owner=account.get('owner_number', ''),
                          bot=account.get('bot_number', ''), private=stage/'.private', binary=binary,
                          python=stage.parent/'001-codex-audio/.venv/bin/python',
                          allowed_sha256=(expected_sha256,))


def profile_sidecar_checksums(private, unified_gateway, sidecar_sha256=None):
    if not unified_gateway:
        if sidecar_sha256 is not None:
            raise ValueError('Local sidecar checksums are only for the unified candidate')
        return PROFILE_RELEASE_SHA256
    if sidecar_sha256 is None:
        try:
            sidecar_sha256 = json.loads((private/'config.json').read_text())['binary_sha256']
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError('Run unified setup with the expected sidecar checksum') from error
    if (not isinstance(sidecar_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sidecar_sha256)
            or sidecar_sha256 in PROFILE_RELEASE_SHA256):
        raise ValueError('Use the SHA-256 of a unified sidecar build')
    return (sidecar_sha256,)


def prepare_profile(*, owner, bot, private, binary, python, hermes_home=None, unified_gateway=False,
                    sidecar_sha256=None):
    """Prepare explicit profile-owned state for the pinned sidecar and its adapter."""
    binary = Path(binary).expanduser().absolute()
    # Retain the virtualenv entry point when it is a symlink to a base interpreter.
    python = Path(python).expanduser().absolute()
    for name, path in (('Sidecar binary', binary), ('Adapter Python', python)):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f'{name} must be an executable file: {path}')
    if not (STAGE/'call_adapter.py').is_file():
        raise ValueError('Call adapter script is missing beside the launcher')
    if hermes_home is not None:
        hermes_home = Path(hermes_home).expanduser().resolve()
    private = Path(private).expanduser().resolve()
    allowed_sha256 = profile_sidecar_checksums(private, unified_gateway, sidecar_sha256)
    return _prepare_state(STAGE, owner=owner, bot=bot, private=private,
                          binary=binary, python=python, allowed_sha256=allowed_sha256,
                          hermes_home=hermes_home, unified_gateway=unified_gateway)


def _prepare_state(stage, *, owner, bot, private, binary, python, allowed_sha256, hermes_home=None,
                   unified_gateway=False):
    binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    if binary_sha256 not in allowed_sha256:
        raise ValueError('Sidecar release checksum mismatch; do not execute')
    if not all(isinstance(n, str) and n.isascii() and n.isdigit() and 7 <= len(n) <= 15
               for n in (owner, bot)) or owner == bot:
        raise ValueError('Need verified distinct bot and owner international phone identities')
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    private.chmod(0o700)
    config = {'stage':str(stage), 'private':str(private), 'binary':str(binary),
              'binary_sha256':binary_sha256, 'bot_number':bot, 'owner_number':owner,
              'python':str(python)}
    if hermes_home is not None:
        config['hermes_home'] = str(hermes_home)
    if unified_gateway:
        config['unified_gateway'] = True
    with owned_transport(config):
        token = private/'adapter.token'
        if not token.exists():
            with token.open('x') as out:
                out.write(secrets.token_urlsafe(32))
        token.chmod(0o600)
        config_file = private/'config.json'
        config_file.write_text(json.dumps(config, indent=2))
        config_file.chmod(0o600)
    return config


def sidecar_env(config, environ=None, *, live_settings=None):
    env = dict(os.environ if environ is None else environ)
    private_keys = {'GEMINI_API_KEY', 'OPENAI_API_KEY', 'HERMES_API_KEY', 'HERMES_API_URL'}
    if live_settings:
        private_keys.update(account['secret_env']
                            for provider in live_settings['providers'].values()
                            for account in provider.get('accounts', {}).values()
                            if 'secret_env' in account)
    for key in list(env):
        if key.startswith('HWV_') or key in private_keys:
            env.pop(key)
    private = Path(config['private'])
    # Older pinned binaries still require a duration value.
    supports_optional_timeout = config.get('unified_gateway') or config.get('binary_sha256') == RELEASE_SHA256
    call_timeout = 'off' if supports_optional_timeout else '180s'
    env.update({
        'HWV_OWNER':config['owner_number'], 'HWV_DATA_DIR':str(private/'whatsapp'),
        'HWV_LISTEN':'127.0.0.1:8796', 'HWV_TOKEN':(private/'adapter.token').read_text().strip(),
        # Local façade authorization ONLY, never a Google API credential.
        'GEMINI_API_KEY':(private/'adapter.token').read_text().strip(),
        'HWV_GEMINI_URL':'ws://127.0.0.1:8797/live',
        'HWV_MODEL':'local-codex-subscription-bridge', 'HWV_ASSISTANT_NAME':'WhatsApp Assistant',
        'HWV_CONTEXT_URL':'', 'HERMES_API_KEY':'', 'HERMES_API_URL':'http://127.0.0.1:1',
        'HWV_MAX_DURATION':call_timeout, 'HWV_SILENCE_TIMEOUT':call_timeout,
        'HWV_ANSWER_INBOUND':'true', 'HWV_SEND_SUMMARY':'false', 'HWV_RECORD_CALLS':'false',
        'RUST_LOG':('info,whatsapp_rust=warn,wacore=warn,'
                    'whatsapp_rust::voip::transport::native=debug,'
                    'whatsapp_rust::voip_control::wacore_backend=info'),
    })
    if config.get('unified_gateway'):
        env['HWV_BOT_NUMBER'] = config['bot_number']
    return env


_CLEANUP_LOCKS = {'purge': 'cleanup-purge.lock', 'stop': 'cleanup-stop.lock'}
_RUNTIME_LOCKS = {'transport.lock', 'call-now.lock', *_CLEANUP_LOCKS.values()}


@contextlib.contextmanager
def _runtime_lock(private, name, *, create=False):
    path = private/name
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('WhatsApp runtime lock must belong to this profile')
        yield fd
    finally:
        os.close(fd)


def _take_lock(fd, wait, message):
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError(message) from None
            time.sleep(.1)


def _cleanup_request(private):
    root = private.parent if private.name == 'unified' else private
    for mode, name in _CLEANUP_LOCKS.items():
        try:
            with _runtime_lock(root, name) as fd:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return mode
        except FileNotFoundError:
            continue
    return None


@contextlib.contextmanager
def owned_transport(config, *, wait=0, cleaning=False):
    private = Path(config['private'])
    with _runtime_lock(private, 'transport.lock', create=True) as fd:
        _take_lock(fd, wait, 'WhatsApp transport already owned by another launcher')
        if not cleaning and _cleanup_request(private):
            raise RuntimeError('WhatsApp cleanup is in progress')
        yield


def read_health(port, token):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/health',headers={'Authorization':'Bearer '+token})
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def select_live_provider(private, selection, *, now):
    token = (private/'adapter.token').read_text().strip()
    request = urllib.request.Request(
        'http://127.0.0.1:8797/provider',
        data=json.dumps({'selection':selection, 'now':now}).encode(),
        headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'},
    )
    with urllib.request.urlopen(request, timeout=17) as response:
        return json.load(response)


def write_live_settings(private, settings):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=private, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(settings, file)
            file.flush()
            os.replace(temporary, private/'live-config.json')
        finally:
            temporary.unlink(missing_ok=True)


def verify_paired_identity(config):
    store = (Path(config['private'])/'whatsapp/whatsapp.db').resolve()
    with sqlite3.connect(store.as_uri()+'?mode=ro', uri=True) as db:
        rows = db.execute('SELECT pn FROM device').fetchall()
    phones = [pn.split('@', 1)[0].split(':', 1)[0] for (pn,) in rows
              if isinstance(pn, str) and pn.endswith('@s.whatsapp.net')]
    if phones != [config['bot_number']]:
        raise RuntimeError('Saved voice bot identity is missing or mismatched; pair the expected bot account')


def finish_bot_unlink(config):
    private = Path(config['private'])
    request = private/'unlink-request.json'
    if not config.get('unified_gateway') or not request.is_file():
        return False
    data = json.loads(request.read_text())
    bot = config['bot_number']
    if (not isinstance(data, dict) or data.get('bot_number') != bot
            or not isinstance(data.get('remote_unlinked'), bool)
            or not re.fullmatch(r'[1-9][0-9]{6,14}', bot)):
        raise RuntimeError('The bot unlink request does not match this profile')
    (private/'paired.json').unlink(missing_ok=True)
    for directory in ('whatsapp', 'outbound-receipts', 'control-contacts', 'control-group-requests'):
        path = private/directory
        if directory != 'whatsapp':
            path = path/bot
        if path.exists():
            shutil.rmtree(path)
    request.unlink()
    return True


def _runtime_dir(path):
    if path.is_symlink() or (path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid())):
        raise ValueError('WhatsApp runtime must belong to this profile')
    return path.is_dir()


def _local_json(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _runtime_account(private, mode):
    config = _local_json(private/'config.json')
    bot = config.get('bot_number')
    has_bot = isinstance(bot, str) and re.fullmatch(r'[1-9][0-9]{6,14}', bot)
    store = private/'whatsapp'/'whatsapp.db'
    receipt = _local_json(private/'cleanup-unlink.json') or _local_json(private/'unlink-request.json')
    matched_receipt = mode == 'unified' and bool(has_bot and receipt.get('bot_number') == bot)
    if not (store.exists() or (private/'paired.json').is_file() or matched_receipt):
        return None
    row = {'account': 'bot', 'runtime': mode, 'local_removed': True,
           'remote_unlinked': matched_receipt and receipt.get('remote_unlinked') is True}
    if has_bot and not (store.is_symlink() or store.parent.is_symlink()):
        try:
            verify_paired_identity({'private': str(private), 'bot_number': bot})
            row['bot_number'] = bot
        except (OSError, RuntimeError, sqlite3.Error):
            if matched_receipt and not store.exists():
                row['bot_number'] = bot
            else:
                row['remote_unlinked'] = False
    else:
        row['remote_unlinked'] = False
    return row


def cleanup_profile_runtime(root, *, purge, before_delete=None):
    """Stop this installation's Live launchers, optionally purging their private data."""
    root = Path(root)
    report = {'unified_runtime_removed': False, 'legacy_runtime_removed': False,
              'runtime_accounts': [], 'runtime_data_retained': False}
    if root.parent.is_symlink():
        raise ValueError('WhatsApp runtime parent must belong to this profile')
    if not _runtime_dir(root):
        return report
    unified = root/'unified'
    name = _CLEANUP_LOCKS['purge' if purge else 'stop']
    deadline = time.monotonic() + 45

    def contents(private):
        return (path for path in private.iterdir() if path != unified and path.name not in _RUNTIME_LOCKS)

    with contextlib.ExitStack() as locks:
        request = locks.enter_context(_runtime_lock(root, name, create=True))
        _take_lock(request, 0, 'WhatsApp cleanup is already in progress')
        scopes = [(root, 'legacy')]
        if _runtime_dir(unified):
            scopes.append((unified, 'unified'))
        for private, _ in scopes:
            locks.enter_context(owned_transport({'private': str(private)},
                                                wait=max(0, deadline - time.monotonic()), cleaning=True))
            if (private/'shutdown-incomplete').exists():
                raise RuntimeError('An owned WhatsApp process has not confirmed shutdown')
        if not purge:
            report['runtime_data_retained'] = any(next(contents(private), None) for private, _ in scopes)
            return report
        for private, _ in scopes:
            fd = locks.enter_context(_runtime_lock(private, 'call-now.lock', create=True))
            _take_lock(fd, max(0, deadline - time.monotonic()), 'A WhatsApp call action is still in progress')
        if before_delete is not None:
            before_delete()
        for private, mode in reversed(scopes):
            if account := _runtime_account(private, mode):
                report['runtime_accounts'].append(account)
            for path in contents(private):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                report[f'{mode}_runtime_removed'] = True
    return report


def _request_active_bot_unlink(config):
    """Use the authenticated sidecar op when Control could not request unlink."""
    private = Path(config['private'])
    if not config.get('unified_gateway') or (private/'unlink-request.json').exists():
        return
    bot = config['bot_number']
    body = {'bot_number': bot, 'op': 'logout', 'args': {}}
    confirmed = False
    try:
        token = (private/'adapter.token').read_text().strip()
        if not token:
            return
        with contextlib.closing(http.client.HTTPConnection('127.0.0.1', 8796, timeout=4)) as conn:
            conn.request('POST', '/control', json.dumps(body), {
                'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
            })
            response = conn.getresponse()
            if response.status == 200:
                result = json.loads(response.read(8192))
                confirmed = (isinstance(result, dict) and result.get('unlink_requested') is True
                             and result.get('remote_unlink_confirmed') is True)
    except (OSError, ValueError, http.client.HTTPException):
        pass
    with tempfile.NamedTemporaryFile(mode='w', dir=private, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump({'bot_number': bot, 'remote_unlinked': confirmed}, file)
            file.flush()
            os.replace(temporary, private/'cleanup-unlink.json')
        finally:
            temporary.unlink(missing_ok=True)


def pair(config):
    """Owner scans the QR on the bot phone. Never share the chat bridge's store."""
    private = Path(config['private'])
    with owned_transport(config):
        finish_bot_unlink(config)
        marker = private/'paired.json'
        marker.unlink(missing_ok=True)
        fresh = not (private/'whatsapp').exists()
        destination = (tempfile.TemporaryDirectory(prefix='pairing-', dir=private) if fresh
                       else contextlib.nullcontext(private))
        with destination as directory:
            pending = Path(directory)
            env = sidecar_env(config)
            if fresh:
                env['HWV_DATA_DIR'] = str(pending/'whatsapp')
            result = subprocess.run([config['binary'], 'pair'], env=env, timeout=180)
            if result.returncode:
                raise RuntimeError(f'Pairing ended with exit {result.returncode}; not marked linked')
            if not (pending/'whatsapp/whatsapp.db').exists():
                raise RuntimeError('Pairing command returned but separate session store is missing')
            verify_paired_identity(dict(config, private=str(pending)))
            if fresh:
                (pending/'whatsapp').rename(private/'whatsapp')
                contacts = Path('control-contacts')/config['bot_number']
                if (pending/contacts).is_dir() and not (private/contacts).exists():
                    (private/contacts).parent.mkdir(mode=0o700, exist_ok=True)
                    (pending/contacts).rename(private/contacts)
        marker.write_text(json.dumps({'paired_at':int(time.time()), 'source':'successful native pair command and verified bot identity'}))
        marker.chmod(0o600)
        (private/'cleanup-unlink.json').unlink(missing_ok=True)


def terminate_owned(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=35)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise RuntimeError('Owned voice child required SIGKILL; cleanup is unverified')


def serve(config, *, is_active=None, agent_jobs=False, control_command=None, live_settings=None, live_enabled=True):
    private, stage = Path(config['private']), Path(config['stage'])
    adapter = sidecar = control = None
    logs = []
    serving_identity = private/'serving-identity.json'
    cleanup_mode = None

    class PluginInactive(Exception):
        pass

    class CleanupRequested(Exception):
        pass

    def require_active():
        if requested := _cleanup_request(private):
            raise CleanupRequested(requested)
        if is_active is not None and not is_active():
            raise PluginInactive

    def interrupted(*_):
        raise KeyboardInterrupt
    previous = {}
    with owned_transport(config):
        if finish_bot_unlink(config):
            return
        if not (private/'paired.json').exists() or not (private/'whatsapp/whatsapp.db').exists():
            raise RuntimeError('Voice device is not paired; run launch.py pair first')
        verify_paired_identity(config)
        control_purge = private/'purge-in-progress.json'
        control_purge.unlink(missing_ok=True)
        for port in (8796, 8797):
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('127.0.0.1',port))
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupted)
        try:
            require_active()
            os.umask(0o077)
            for name in ('adapter', 'sidecar'):
                logs.append((private/f'{name}.log').open('ab', buffering=0))
            env = dict(os.environ)
            for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', '__PYVENV_LAUNCHER__'):
                env.pop(key, None)
            command = [config['python'],str(stage/'call_adapter.py'),
                       '--token-file',str(private/'adapter.token'),'--runs',str(private/'runs')]
            if 'hermes_home' in config:
                command.extend(['--task-hermes-home', config['hermes_home']])
            if live_settings is not None:
                write_live_settings(private, live_settings)
                command.extend(['--live-config', str(private/'live-config.json')])
            if agent_jobs:
                command.append('--agent-jobs')
                env.update(WA_CONTROL_LIVE_OWNER=config['owner_number'], WA_CONTROL_LIVE_BOT=config['bot_number'])
            adapter = subprocess.Popen(command,
                       env=env,stdout=logs[0],stderr=subprocess.STDOUT,start_new_session=True)
            token = (private/'adapter.token').read_text().strip()
            deadline = time.monotonic()+10
            while True:
                require_active()
                if adapter.poll() is not None:
                    raise RuntimeError('Adapter exited during startup; inspect private adapter.log')
                try:
                    read_health(8797, token)
                    break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Adapter readiness timed out')
                    time.sleep(.1)
            require_active()
            wa_env = sidecar_env(config, live_settings=live_settings) if live_settings else sidecar_env(config)
            if not live_enabled:
                wa_env['HWV_ANSWER_INBOUND'] = 'false'
            sidecar = subprocess.Popen([config['binary'],'serve'],env=wa_env,
                        stdout=logs[1],stderr=subprocess.STDOUT,start_new_session=True)
            if control_command:
                require_active()
                control_log = (private/'control.log').open('ab', buffering=0)
                logs.append(control_log)
                control = subprocess.Popen(control_command,
                            env={**env, 'HERMES_HOME':config['hermes_home']}, stdout=control_log,
                            stderr=subprocess.STDOUT, start_new_session=True)
            with (private/'call-now.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                serving_identity.write_text(json.dumps({key: config.get(key) for key in
                                                        ('owner_number', 'bot_number', 'hermes_home')}))
            children = [child for child in (control, sidecar, adapter) if child is not None]
            print('WhatsApp service started. Ctrl-C stops its processes.', flush=True)
            while all(child.poll() is None for child in children):
                require_active()
                time.sleep(.25)
            if (control is not None and control.poll() is None and sidecar.poll() is not None
                    and _local_json(control_purge).get('control_pid') == control.pid):
                try:
                    control.wait(timeout=55)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError('Control did not finish reporting WhatsApp unlink results') from exc
            if control is not None and control.returncode == 0:
                return
            raise RuntimeError('A WhatsApp service process exited; inspect private logs. Transport not left running.')
        except CleanupRequested as exc:
            cleanup_mode = exc.args[0]
            print('Stopping the WhatsApp processes owned by this profile.', flush=True)
        except PluginInactive:
            print('WA Control & Live is no longer active; stopping owned voice transport.',flush=True)
        except KeyboardInterrupt:
            print('Stopping owned voice transport; existing Hermes chat remains untouched.',flush=True)
        finally:
            errors = []
            control_already_purged = (control is not None
                                     and _local_json(control_purge).get('control_pid') == control.pid)
            try:
                with (private/'call-now.lock').open('a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    serving_identity.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(str(exc))
            for child in (control, sidecar, adapter):
                if (child is sidecar and sidecar is not None and sidecar.poll() is None
                        and config.get('unified_gateway') and cleanup_mode == 'purge'
                        and not control_already_purged and not errors):
                    try:
                        _request_active_bot_unlink(config)
                    except (OSError, ValueError) as exc:
                        errors.append(str(exc))
                try:
                    terminate_owned(child)
                except Exception as exc:
                    errors.append(str(exc))
            incomplete = private/'shutdown-incomplete'
            if any(child is not None and child.poll() is None for child in (control, sidecar, adapter)):
                with _runtime_lock(private, incomplete.name, create=True):
                    pass
            else:
                incomplete.unlink(missing_ok=True)
            if not errors:
                try:
                    finish_bot_unlink(config)
                except (OSError, ValueError, RuntimeError) as exc:
                    errors.append(str(exc))
            for log in logs:
                log.close()
            for sig, handler in previous.items():
                signal.signal(sig,handler)
            if errors:
                raise RuntimeError('; '.join(errors))


def status(config):
    private = Path(config['private'])
    result = {'paired_marker':(private/'paired.json').exists(), 'autostart':False,
              'live_call_verified':False, 'services':{'adapter':{'reachable':False}, 'sidecar':{'reachable':False}}}
    try:
        token = (private/'adapter.token').read_text().strip()
    except FileNotFoundError:
        return result
    for name, port in (('adapter',8797),('sidecar',8796)):
        try:
            result['services'][name] = read_health(port,token)
        except (OSError, ValueError):
            result['services'][name] = {'reachable':False}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','status','pair','serve'])
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'status':
        config = {'private':str(STAGE/'.private')}
    else:
        config = prepare(STAGE, STAGE/'identity.local.json')
    if args.action in {'prepare','status'}:
        print(json.dumps(status(config),indent=2))
    elif args.action == 'pair':
        pair(config)
    else:
        serve(config)


if __name__ == '__main__':
    main()
