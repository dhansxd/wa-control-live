"""Offline tests for the plugin's reuse of Hermes WhatsApp identity."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


OWNER = '15550100001'
BOT = '15550100002'
LID = '123456789012345'


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import plugin_identity
    return plugin_identity


def paired_session(tmp_path):
    session = tmp_path / 'whatsapp' / 'session'
    session.mkdir(parents=True)
    (session / 'creds.json').write_text(json.dumps({
        'me': {'id': f'{BOT}:2@s.whatsapp.net'}, 'registered': False,
    }))
    return session


def install_hermes_config(monkeypatch, home, *, extra=None, env_owner=OWNER,
                          enabled=True, dm_policy='allowlist'):
    platform = SimpleNamespace(enabled=enabled, extra=extra or {}, home_channel=None)
    config = SimpleNamespace(platforms={'whatsapp': platform})

    def decode_list(value):
        return json.loads(value) if isinstance(value, str) and value.lstrip().startswith('[') else value

    modules = {
        'gateway.config': SimpleNamespace(Platform=SimpleNamespace(WHATSAPP='whatsapp'),
                                          load_gateway_config=lambda: config),
        'gateway.platforms._shared': SimpleNamespace(decode_json_list_literal=decode_list,
                                                     extra_or_secret=lambda *args: dm_policy,
                                                     get_scoped_secret=lambda name: env_owner),
        'hermes_constants': SimpleNamespace(get_hermes_dir=lambda *args, **kwargs: home / 'whatsapp/session',
                                            get_hermes_home=lambda: home),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.mark.parametrize('extra,env_owner', [
    ({'allow_from': [OWNER], 'allowFrom': [BOT]}, BOT),
    ({'allowFrom': json.dumps([OWNER])}, BOT),
    ({}, f' {OWNER}, '),
])
def test_resolves_effective_owner_and_paired_chat_bot(identity, monkeypatch, tmp_path, extra, env_owner):
    paired_session(tmp_path)
    install_hermes_config(monkeypatch, tmp_path, extra=extra, env_owner=env_owner)
    assert identity.resolve_identity(tmp_path) == (OWNER, BOT)


@pytest.mark.parametrize('configured', [[], None, ''])
def test_explicit_empty_allowlist_never_falls_back_to_environment(identity, monkeypatch, tmp_path, configured):
    paired_session(tmp_path)
    install_hermes_config(monkeypatch, tmp_path, extra={'allow_from': configured})
    with pytest.raises(RuntimeError, match='authorize exactly one voice owner'):
        identity.resolve_identity(tmp_path)


def test_identity_requires_active_profile(identity, monkeypatch, tmp_path):
    install_hermes_config(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='requires the active Hermes profile'):
        identity.resolve_identity(tmp_path / 'other')


def test_reuses_local_lid_mapping_for_the_same_authorized_owner(identity, monkeypatch, tmp_path):
    session = paired_session(tmp_path)
    (session / f'lid-mapping-{LID}_reverse.json').write_text(json.dumps(OWNER))
    monkeypatch.setattr(identity, '_whatsapp_settings', lambda home: (session, {OWNER, LID}, f'{LID}@lid'))
    assert identity.resolve_identity(tmp_path) == (OWNER, BOT)


@pytest.mark.parametrize('allowed,home_id', [
    (set(), f'{OWNER}@s.whatsapp.net'),
    ({OWNER, '*'}, None),
    ({f'{OWNER}@g.us'}, None),
    ({OWNER, BOT}, f'{OWNER}@s.whatsapp.net'),
    ({BOT}, None),
    ({LID}, f'{LID}@lid'),
])
def test_refuses_unsafe_or_ambiguous_identity(identity, monkeypatch, tmp_path, allowed, home_id):
    session = paired_session(tmp_path)
    monkeypatch.setattr(identity, '_whatsapp_settings', lambda home: (session, allowed, home_id))
    with pytest.raises(RuntimeError) as rejected:
        identity.resolve_identity(tmp_path)
    assert all(value not in str(rejected.value) for value in (OWNER, BOT, LID))


@pytest.mark.parametrize('enabled,dm_policy,error', [
    (False, 'allowlist', 'must be enabled'),
    (True, 'disabled', 'direct messages are disabled'),
])
def test_disabled_hermes_whatsapp_cannot_enable_voice(identity, monkeypatch, tmp_path,
                                                      enabled, dm_policy, error):
    install_hermes_config(monkeypatch, tmp_path, enabled=enabled, dm_policy=dm_policy)
    with pytest.raises(RuntimeError, match=error):
        identity.resolve_identity(tmp_path)
