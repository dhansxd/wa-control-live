"""Exercise the installed plugin's serve command with offline child processes."""

import argparse
import asyncio
from contextlib import nullcontext
from contextvars import ContextVar
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


PLUGIN_ID = 'wa-control-live'


@pytest.fixture
def plugin(monkeypatch):
    entrypoint = Path(__file__).resolve().parents[2]/'__init__.py'
    spec = importlib.util.spec_from_file_location('_wa_control_live_lifecycle_test', entrypoint)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('result', ['switched', 'failed'])
def test_live_use_persists_confirmed_selection_in_current_profile(plugin, tmp_path, monkeypatch, result):
    settings = {'selected': 'codex', 'providers': {'codex': {'kind': 'codex'}, 'gemini': {'kind': 'gemini'}}}
    runtime = tmp_path/'plugin-data'
    runtime.mkdir()
    (runtime/'serving-identity.json').touch()
    calls, saved = [], []

    def select(path, alias, *, now):
        calls.append((path, alias, now))
        return {'status': result}

    monkeypatch.setattr(plugin, '_launcher', lambda: SimpleNamespace(select_live_provider=select))
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: tmp_path))
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.live_routing', __import__('live_routing'))
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.plugin_identity', SimpleNamespace(resolve_identity=None))
    ctx = SimpleNamespace(state=SimpleNamespace(data_dir=runtime),
                          get_config=lambda name, default=None: settings if name == 'live' else default,
                          set_config=lambda name, value: saved.append((name, value)))
    args = argparse.Namespace(action='use', timing='now', selection='gemini')
    if result == 'failed':
        with pytest.raises(SystemExit):
            plugin._run(ctx, args)
        assert saved == []
    else:
        plugin._run(ctx, args)
        assert saved == [('live.selected', 'gemini')]
    assert calls == [(runtime, 'gemini', True)]


@pytest.mark.parametrize('transition, during_startup', [
    ('disable', False),
    ('uninstall', False),
    ('uninstall_symlink', False),
    ('replace_installation', False),
    ('disable', True),
])
def test_stock_plugin_lifecycle_stops_only_owned_voice_children(
    plugin, tmp_path, monkeypatch, transition, during_startup,
):
    import launch

    home = tmp_path/'profile'
    installed = home/'plugins'/PLUGIN_ID
    installed.parent.mkdir(parents=True)
    source = installed
    if transition == 'uninstall_symlink':
        source = tmp_path/'checkout'
        installed.symlink_to(source, target_is_directory=True)
    source.mkdir()
    (source/'__init__.py').touch()
    (source/'plugin.yaml').write_text(f'name: {PLUGIN_ID}\n')
    config_file = home/'config.yaml'
    config_file.write_text(json.dumps({'plugins': {'enabled': [PLUGIN_ID]}}))

    monkeypatch.setattr(plugin, '__file__', str(installed/'__init__.py'))
    monkeypatch.setattr(plugin, '_launcher', lambda: launch)
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.plugin_identity', SimpleNamespace(
        resolve_identity=lambda profile: ('15550100001', '15550100002'),
    ))
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: home))
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', SimpleNamespace(
        load_config_readonly=lambda: json.loads(config_file.read_text()),
    ))

    private = home/'plugin-data'/PLUGIN_ID
    (private/'whatsapp').mkdir(parents=True)
    for path in ('whatsapp/whatsapp.db', 'paired.json', 'config.json', 'adapter.token'):
        (private/path).write_text('local fixture')
    config = {'private': str(private), 'stage': str(source), 'python': sys.executable,
              'binary': 'fake-sidecar', 'hermes_home': str(home),
              'owner_number': '15550100001', 'bot_number': '15550100002'}
    monkeypatch.setattr(launch, 'prepare_profile', lambda **kwargs: config)
    monkeypatch.setattr(launch, 'verify_paired_identity', lambda config: None)
    monkeypatch.setattr(launch, 'sidecar_env', lambda config: {})

    probe = SimpleNamespace(setsockopt=lambda *_: None, bind=lambda *_: None)
    monkeypatch.setattr(launch.socket, 'socket', lambda: nullcontext(probe))
    monkeypatch.setattr(launch.os, 'umask', lambda _: None)

    children, terminated, reaped = [], [], []

    def spawn(command, *, start_new_session, **kwargs):
        assert start_new_session
        pid = 2000 + len(children)
        if not children:
            assert kwargs['env']['WA_CONTROL_LIVE_OWNER'] == config['owner_number']
            assert kwargs['env']['WA_CONTROL_LIVE_BOT'] == config['bot_number']
        children.append((pid, command))

        def wait(timeout):
            assert (pid, launch.signal.SIGTERM) in terminated
            reaped.append(pid)

        return SimpleNamespace(pid=pid, poll=lambda: None, wait=wait)

    monkeypatch.setattr(launch.subprocess, 'Popen', spawn)
    monkeypatch.setattr(launch.os, 'killpg', lambda pid, sig: terminated.append((pid, sig)))

    def deactivate():
        if transition == 'disable':
            config_file.write_text(json.dumps({'plugins': {'enabled': [], 'disabled': [PLUGIN_ID]}}))
        elif transition == 'uninstall_symlink':
            installed.unlink()
        else:
            installed.rename(home/'removed-installation')
            if transition == 'replace_installation':
                installed.mkdir()
                (installed/'__init__.py').touch()
                (installed/'plugin.yaml').write_text(f'name: {PLUGIN_ID}\n')

    ticks = 0

    def tick(_delay):
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            config_file.write_text(json.dumps({'plugins': {'enabled': [PLUGIN_ID], 'disabled': ['other']}}))
        elif ticks == 2:
            deactivate()
        else:
            pytest.fail('serve kept the voice children after deactivation')

    def ready(_port, _token):
        if during_startup:
            deactivate()
        return {'reachable': True}

    monkeypatch.setattr(launch.time, 'sleep', tick)
    monkeypatch.setattr(launch, 'read_health', ready)
    plugin._run(SimpleNamespace(plugin_id=PLUGIN_ID, manifest=SimpleNamespace(name=PLUGIN_ID),
                                state=SimpleNamespace(data_dir=private)),
                SimpleNamespace(action='serve'))

    expected = [2000] if during_startup else [2001, 2000]
    assert len(children) == len(expected)
    assert children[0][1][-3:] == ['--task-hermes-home', str(home), '--agent-jobs']
    assert ticks == (0 if during_startup else 2)
    assert terminated == [(pid, launch.signal.SIGTERM) for pid in expected]
    assert reaped == expected
    assert (private/'whatsapp/whatsapp.db').read_text() == 'local fixture'
    if transition == 'uninstall_symlink':
        assert source.is_dir()


@pytest.mark.parametrize('active, same_profile, same_identity, error', [
    (True, True, True, None),
    (True, False, True, 'same Hermes profile'),
    (False, True, True, 'plugin is inactive'),
    (True, True, False, 'WhatsApp identity changed'),
])
def test_agent_job_dispatch_is_profile_bound(plugin, monkeypatch, tmp_path, active, same_profile,
                                            same_identity, error):
    home = tmp_path/'profile'
    state = home/'plugin-data'/PLUGIN_ID
    registered, dispatched, tools, skills, hooks = [], [], [], [], []
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: home))
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', SimpleNamespace(load_config_readonly=lambda: {
        'plugins': {'enabled': [PLUGIN_ID], 'disabled': [] if active else [PLUGIN_ID]},
    }))
    owner, bot = '15550100001', '15550100002'
    monkeypatch.setenv('WA_CONTROL_LIVE_OWNER', owner if same_identity else '15550100003')
    monkeypatch.setenv('WA_CONTROL_LIVE_BOT', bot)
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.plugin_identity', SimpleNamespace(
        resolve_identity=lambda _: (owner, bot),
    ))
    def resolve(*_):
        pytest.fail('the CLI dispatch fixture should not resolve a real route')
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.whatsapp_session',
                        SimpleNamespace(resolve_owner_session=resolve))
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.live_agent', SimpleNamespace(
        CALL_INTENT_TASK='wa_control_live_call_intent',
        run_cli=lambda profile, number, **kwargs: dispatched.append((profile, number, kwargs)),
    ))
    llm = object()
    plugin.register(SimpleNamespace(
        plugin_id=PLUGIN_ID, manifest=SimpleNamespace(name=PLUGIN_ID), state=SimpleNamespace(data_dir=state),
        llm=llm,
        register_auxiliary_task=lambda key, **options: (
            key == 'wa_control_live_call_intent' and options['defaults']['reasoning_effort'] == 'none'
        ) or pytest.fail('Expected the plugin-owned lightweight call intent route'),
        register_cli_command=lambda **command: registered.append(command),
        register_tool=lambda **tool: tools.append(tool),
        register_skill=lambda **skill: skills.append(skill),
        register_hook=lambda name, callback: hooks.append((name, callback)),
        register_middleware=lambda *_: None,
    ))
    control, provider, outbound = tools
    assert control['name'] == 'whatsapp_control'
    assert control['toolset'] == provider['toolset'] == outbound['toolset']
    assert {'configure', 'post_status', 'group_create'}.issubset(
        control['schema']['parameters']['properties']['action']['enum']
    )
    assert provider['name'] == 'whatsapp_live_provider' and provider['is_async']
    assert provider['schema']['parameters']['required'] == ['selection']
    assert outbound['name'] == 'whatsapp_call_now'
    parameters = outbound['schema']['parameters']
    assert set(parameters['properties']) == {'schedule'}
    assert parameters['properties']['schedule']['type'] == 'string'
    assert not parameters.get('required')
    skill, = skills
    assert skill['name'] == 'call-now'
    assert skill['frontmatter']['metadata']['hermes'] == {
        'session_platforms': ['whatsapp'], 'requires_toolsets': [outbound['toolset']],
    }
    assert outbound['name'] in skill['path'].read_text()
    assert set(dict(hooks)) == {'pre_llm_call', 'post_gateway_admission', 'on_session_reset'}
    assert dict(hooks)['pre_llm_call'](platform='telegram') is None
    command = next(command for command in registered if command['name'] == 'wa-control-live')
    assert command['name'] == 'wa-control-live'
    parser = argparse.ArgumentParser()
    command['setup_fn'](parser)
    expected_home = home if same_profile else tmp_path/'other-profile'
    args = parser.parse_args(['agent-job', '--expected-home', str(expected_home)])

    if error:
        with pytest.raises(SystemExit, match=error):
            command['handler_fn'](args)
        assert not dispatched
    else:
        command['handler_fn'](args)
        assert dispatched == [(home, owner, {'resolve': resolve, 'llm': llm})]


@pytest.fixture
def voice_hooks(plugin, tmp_path, monkeypatch):
    owner, bot, hooks = '15550100001', '15550100002', {}
    state = SimpleNamespace(data_dir=tmp_path)
    profile_home = ContextVar('hook_profile_home')
    route_key = f'agent:profile:whatsapp:dm:{owner}'
    (tmp_path / 'serving-identity.json').write_text(json.dumps({
        'hermes_home': str(tmp_path), 'owner_number': owner, 'bot_number': bot,
    }))
    (tmp_path / 'adapter.token').write_text('fixture-token')
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(
        get_hermes_home=profile_home.get, profile_name_for_home=lambda _: 'profile',
    ))
    monkeypatch.setitem(sys.modules, 'gateway.config', SimpleNamespace(
        Platform=SimpleNamespace(WHATSAPP='whatsapp'),
    ))
    monkeypatch.setitem(sys.modules, 'gateway.session', SimpleNamespace(
        SessionSource=lambda platform, chat: (platform, chat),
        build_session_key=lambda source, *, profile: route_key,
    ))
    monkeypatch.setitem(sys.modules, 'gateway.whatsapp_identity', SimpleNamespace(
        canonical_whatsapp_identifier=lambda value: (value or '').split('@', 1)[0],
    ))
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.plugin_identity', SimpleNamespace(
        resolve_identity=lambda _: (owner, bot),
    ))
    monkeypatch.setitem(sys.modules, f'{plugin.__name__}.whatsapp_session', SimpleNamespace(
        resolve_owner_session=lambda *_: 'compressed-tip',
        owner_session_key=lambda *_: route_key,
        VOICE_CALL_KIND='voice_call',
        save_voice_exchange=lambda *_: pytest.fail('The hook must flush through the voice adapter'),
    ))
    def get_lineage(session):
        if session == 'old-tip':
            return ['old-logical-dm', 'old-tip']
        return ['logical-dm', session]

    def get_session(session):
        return {'session_key': route_key if session == 'old-tip' else 'unrelated-route'}

    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(
        SessionDB=lambda *_, **__: nullcontext(SimpleNamespace(
            get_compression_lineage=get_lineage, get_session=get_session,
        )),
    ))
    plugin.register(SimpleNamespace(
        state=state, register_hook=hooks.__setitem__,
        register_auxiliary_task=lambda *_, **__: None, register_cli_command=lambda *_, **__: None,
        register_skill=lambda **_: None, register_tool=lambda *_, **__: None, spawn_task=asyncio.create_task,
        register_middleware=lambda *_: None,
    ))
    profile_token = profile_home.set(tmp_path)
    try:
        yield hooks, route_key, owner
    finally:
        profile_home.reset(profile_token)


@pytest.mark.parametrize('scenario', ['saved', 'busy', 'idle', 'other_chat', 'other_route', 'group', 'offline'])
def test_whatsapp_admission_awaits_same_dm_voice_history(plugin, monkeypatch, voice_hooks, scenario):
    hooks, route_key, owner = voice_hooks
    steps = []

    def request(method, path, body, headers):
        assert (method, path, json.loads(body)) == ('POST', '/flush-history', {'session_id': 'logical-dm'})
        assert headers['Authorization'] == 'Bearer fixture-token'
        steps.append('request')
        if scenario == 'offline':
            raise ConnectionRefusedError

    def getresponse():
        steps.append(scenario)
        return SimpleNamespace(status=200, read=lambda: json.dumps({'status': scenario}).encode())

    def connect(host, port, *, timeout):
        assert (host, port) == ('127.0.0.1', 8797) and timeout > 0
        return SimpleNamespace(request=request, getresponse=getresponse, close=lambda: None)

    monkeypatch.setattr(plugin.http.client, 'HTTPConnection', connect)
    source = {'chat_type': 'group' if scenario == 'group' else 'dm',
              'chat_id': '15550100003' if scenario == 'other_chat' else owner,
              'user_id': f'{owner}@s.whatsapp.net', 'profile': 'profile'}
    key = 'unrelated-route' if scenario == 'other_route' else route_key

    async def admit():
        assert await hooks['post_gateway_admission'](platform='whatsapp', source=source, session_key=key) is None
        steps.append('continue DM')

    asyncio.run(admit())
    if scenario in ('saved', 'busy', 'idle'):
        assert steps == ['request', scenario, 'continue DM']
    elif scenario == 'offline':
        assert steps == ['request', 'continue DM']
    else:
        assert steps == ['continue DM']


@pytest.mark.parametrize('platform, old_session_id, new_session_id, expected', [
    ('whatsapp', 'old-tip', 'logical-dm', True),
    ('whatsapp', 'old-tip', 'elsewhere', False),
    ('whatsapp', 'elsewhere', 'logical-dm', False),
    ('whatsapp', 'logical-dm', 'compressed-tip', False),
    ('telegram', 'old-tip', 'logical-dm', False),
    ('whatsapp', 'old-tip', None, False),
])
def test_session_reset_notifies_only_the_preceding_owner_dm(
    plugin, monkeypatch, voice_hooks, platform, old_session_id, new_session_id, expected,
):
    hooks, _, _ = voice_hooks
    requests = []

    def request(method, path, body, headers):
        requests.append((method, path, json.loads(body), headers['Authorization']))

    monkeypatch.setattr(plugin.http.client, 'HTTPConnection', lambda *_args, **_kwargs: SimpleNamespace(
        request=request, getresponse=lambda: None, close=lambda: None,
    ))

    async def reset():
        assert hooks['on_session_reset'](
            platform=platform, old_session_id=old_session_id, new_session_id=new_session_id,
        ) is None
        assert not requests
        await asyncio.gather(*(task for task in asyncio.all_tasks() if task is not asyncio.current_task()))

    asyncio.run(reset())
    assert requests == ([('POST', '/session-reset', {'session_id': 'old-logical-dm'}, 'Bearer fixture-token')]
                        if expected else [])


@pytest.mark.parametrize(('sidecar_status', 'tool_status'), [
    ('preparing', 'preparing_audio'), ('dialing', 'dial_requested'),
])
def test_call_now_posts_only_once_to_loopback(plugin, tmp_path, monkeypatch, sidecar_status, tool_status):
    from importlib import import_module
    from threading import Thread
    from http.server import BaseHTTPRequestHandler, HTTPServer

    outbound = import_module(f'{plugin.__name__}.whatsapp_outbound')
    outbound_path = tmp_path / 'voice'
    outbound_path.mkdir()
    owner, bot = '15550100001', '15550100002'
    (outbound_path / 'serving-identity.json').write_text(json.dumps({
        'hermes_home': str(tmp_path), 'owner_number': owner, 'bot_number': bot,
    }))
    (outbound_path / 'adapter.token').write_text('fixture-token')
    calls, values = [], {}
    real_connect = outbound.http.client.HTTPConnection
    monkeypatch.setitem(sys.modules, 'hermes_constants', SimpleNamespace(get_hermes_home=lambda: tmp_path))
    monkeypatch.setattr(outbound, 'resolve_identity', lambda home: (owner, bot))
    monkeypatch.setattr(outbound, '_owner_request', lambda home, user, session: 'message-1' if session == 'dm' else None)
    ctx = SimpleNamespace(
        state=SimpleNamespace(data_dir=outbound_path, get=values.get, set=values.__setitem__),
        current_cron_execution=lambda: None,
    )

    class Sidecar(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.headers['Authorization'],
                          json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(202)
            self.end_headers()
            self.wfile.write(json.dumps({
                'id': 'fixture-call', 'direction': 'outbound', 'status': sidecar_status,
            }).encode())

        def log_message(self, *_args):
            pass

    with HTTPServer(('127.0.0.1', 0), Sidecar) as server:
        def connect(host, port, *, timeout):
            assert (host, port) == ('127.0.0.1', 8796)
            assert timeout == 4
            return real_connect(host, server.server_port, timeout=timeout)

        monkeypatch.setattr(outbound.http.client, 'HTTPConnection', connect)
        runner = Thread(target=server.serve_forever, daemon=True)
        runner.start()
        try:
            assert outbound.call_now(ctx, {'number': bot}, session_id='dm') == {'status': 'denied'}
            assert outbound.call_now(ctx, {}, session_id='other') == {'status': 'denied'}
            assert outbound.call_now(ctx, {}, session_id='dm') == {
                'status': tool_status, 'call_id': 'fixture-call',
            }
            assert outbound.call_now(ctx, {}, session_id='dm') == {'status': 'already_attempted'}
            monkeypatch.setattr(outbound, 'resolve_identity', lambda home: (bot, owner))
            assert outbound.call_now(ctx, {}, session_id='dm') == {'status': 'voice_restart_required'}
        finally:
            server.shutdown()
            runner.join()
    assert calls == [('/calls', 'Bearer fixture-token', {'reason': 'Panggilan atas permintaanmu di WhatsApp'})]
