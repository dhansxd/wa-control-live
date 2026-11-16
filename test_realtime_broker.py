import io
import json
import sys
import time
from types import SimpleNamespace

import pytest

import live_agent


REQUEST = {'action': 'realtime_secret', 'provider': 'profile-login', 'account': 'default'}
PROVIDER_BROKER = 'hermes_cli.plugin_provider_requests'


class ProviderNotSignedIn(RuntimeError):
    pass


class HTTPError(Exception):
    pass


def install_broker(monkeypatch, request):
    monkeypatch.setitem(sys.modules, PROVIDER_BROKER, SimpleNamespace(
        ProviderNotSignedIn=ProviderNotSignedIn,
        credentialed_provider_request=request,
    ))
    monkeypatch.setitem(sys.modules, 'httpx', SimpleNamespace(HTTPError=HTTPError))


def config(**provider):
    return {'providers': {'profile-login': {'kind': 'hermes-realtime', **provider}}}


@pytest.mark.parametrize('provider, model, voice', [
    ({}, 'gpt-realtime-2.1', None),
    ({'model': 'gpt-realtime-2.1-mini', 'voice': 'marin'}, 'gpt-realtime-2.1-mini', 'marin'),
])
def test_profile_worker_mints_only_the_selected_realtime_secret(
    monkeypatch, tmp_path, capsys, provider, model, voice,
):
    expiry = int(time.time()) + 60

    def broker(sign_in, method, url, *, json, timeout):
        assert (sign_in, method, url) == (
            'openai-codex', 'POST', 'https://api.openai.com/v1/realtime/client_secrets'
        )
        assert timeout > 0
        audio = {
            'input': {'transcription': {'model': 'gpt-4o-mini-transcribe'},
                      'turn_detection': {'type': 'server_vad', 'create_response': False}},
            **({'output': {'voice': voice}} if voice else {}),
        }
        assert json == {'session': {
            'type': 'realtime', 'model': model, 'output_modalities': ['audio'], 'audio': audio,
        }}
        return SimpleNamespace(status=200, json=lambda: {
            'value': 'ek_fixture', 'expires_at': expiry, 'session': {'id': 'discard'},
        })

    install_broker(monkeypatch, broker)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(REQUEST)))

    def unexpected_session(*_):
        pytest.fail('Credential minting should never read the WhatsApp conversation')

    live_agent.run_cli(tmp_path, 'fixture-owner', resolve=unexpected_session,
                       get_live_config=lambda: config(**provider))
    output = capsys.readouterr()
    assert json.loads(output.out) == {'status': 'ready', 'client_secret': 'ek_fixture', 'expires_at': expiry}
    assert output.err == ''


@pytest.mark.parametrize('payload, settings', [
    ({**REQUEST, 'provider': 'unknown'}, config()),
    ({**REQUEST, 'account': 'unknown'}, config()),
    ({**REQUEST, 'account': []}, config()),
    ({**REQUEST, 'url': 'https://example.test'}, config()),
    (REQUEST, config(kind='codex')),
])
def test_only_a_configured_realtime_candidate_can_call_the_broker(monkeypatch, payload, settings):
    def unexpected_broker(*_, **__):
        pytest.fail('Invalid candidate must be rejected before requesting authentication')

    install_broker(monkeypatch, unexpected_broker)
    assert live_agent._realtime_secret(payload, lambda: settings) == {'status': 'unsupported'}


def test_no_broker_on_older_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, PROVIDER_BROKER, None)
    assert live_agent._realtime_secret(REQUEST, config) == {'status': 'unsupported'}


@pytest.mark.parametrize('error, status', [
    (ProviderNotSignedIn('private auth details'), 'auth_required'),
    (PermissionError('private gateway details'), 'unsupported'),
    (HTTPError('private endpoint details'), 'unavailable'),
])
def test_broker_exceptions_report_only_public_status(monkeypatch, error, status):
    def broker(*_, **__):
        raise error

    install_broker(monkeypatch, broker)
    assert live_agent._realtime_secret(REQUEST, config) == {'status': status}


@pytest.mark.parametrize('http_status, code, headers, expected', [
    (401, None, {}, {'status': 'auth_required'}),
    (403, None, {}, {'status': 'access_denied'}),
    (429, 'insufficient_quota', {}, {'status': 'quota_exhausted'}),
    (429, 'billing_not_active', {}, {'status': 'billing_required'}),
    (429, None, {'retry-after': '15'}, {'status': 'rate_limited', 'retry_after': 15}),
    (503, None, {'Retry-After': 'inf'}, {'status': 'unavailable'}),
    (302, None, {'Location': 'https://example.test'}, {'status': 'unavailable'}),
])
def test_provider_failure_does_not_expose_body_or_headers(monkeypatch, http_status, code, headers, expected):
    install_broker(monkeypatch, lambda *_, **__: SimpleNamespace(
        status=http_status, headers=headers,
        json=lambda: {'error': {'code': code, 'message': 'private provider detail'}},
    ))
    assert live_agent._realtime_secret(REQUEST, config) == expected


@pytest.mark.parametrize('value, expires_at', [
    ('sk-long-lived-token', int(time.time()) + 60),
    ('ek_expired', 1),
    ('ek_unknown_expiry', True),
    ('ek_malformed\n', int(time.time()) + 60),
])
def test_invalid_success_does_not_supply_a_credential(monkeypatch, value, expires_at):
    install_broker(monkeypatch, lambda *_, **__: SimpleNamespace(
        status=201, json=lambda: {'value': value, 'expires_at': expires_at},
    ))
    assert live_agent._realtime_secret(REQUEST, config) == {'status': 'unavailable'}
