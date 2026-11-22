"""Run /walive through the stock Hermes plugin dispatcher with private profile fixtures."""

import asyncio
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from threading import get_ident
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run_inbound_hooks import GatewayInboundHooksMixin
from gateway.session import SessionSource, build_session_key
from gateway.session_identity import canonical_identity, identity_of
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


OWNER = "628120001111"
BOT = "628120002222"
OTHER = "628120003333"


class GatewayFixture(GatewayInboundHooksMixin):
    config = GatewayConfig()
    session_store = None
    _session_env_scope = staticmethod(lambda context: nullcontext())

    def _session_key_for_source(self, source):
        identity = identity_of(source)
        return build_session_key(source, profile=identity.session_key_profile if identity else source.profile)


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "cobalt"
    home.mkdir(parents=True)
    token = set_hermes_home_override(home)
    settings = {
        "unified_gateway": True, "owner_number": OWNER, "bot_number": BOT,
        "live": {
            "selected": "codex", "providers": {
                "codex": {"kind": "codex"},
                "gemini": {"kind": "gemini", "accounts": {"default": {"secret_env": "GEMINI_PRIVATE_FIXTURE"}}},
            }, "pools": {"audio": ["codex", "gemini"]},
        },
    }
    config = home / "config.yaml"
    config.write_text(json.dumps({"plugins": {"entries": {"wa-control-live": {"settings": settings}}}}))

    path = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location("_fixture_walive_plugin", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module_commands = importlib.import_module(spec.name + ".live_commands")

    manager = PluginManager()
    context = PluginContext(PluginManifest(name="wa-control-live", source="user"), manager)
    runtime = context.state.data_dir / "unified"
    runtime.mkdir(parents=True)
    (runtime / "serving-identity.json").touch()
    calls = []
    result = {"status": "switched", "provider": "gemini", "account": "default"}
    notice = {}
    main_thread = get_ident()

    def select(state, selection, *, now):
        assert get_hermes_home() == home and get_ident() != main_thread
        calls.append((state, selection, now))
        return result.copy()

    def status(state):
        assert get_hermes_home() == home and get_ident() != main_thread
        assert state == {"private": str(runtime)}
        return {"services": {"adapter": {
            "active": True, "voice_ready": True, "active_provider": {"provider": "codex", "account": "default"},
            "routing": {"selected": "codex"}, "last_error": "/private/dont-send GEMINI_PRIVATE_FIXTURE",
            "failure_notice": notice.get("adapter"),
        }, "sidecar": {"last_provider_failure": notice.get("sidecar")}}}

    monkeypatch.setattr(module, "_launcher", lambda: SimpleNamespace(status=status, select_live_provider=select))
    context.register_command("walive", module_commands.walive_handler(context),
                             args_hint="[status | list | use <alias> [now]]")
    handler = manager._plugin_commands["walive"]["handler"]
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_command_handler",
                        lambda name: handler if name == "walive" else None)

    def dispatch(source, args):
        event = MessageEvent(text=f"/walive {args}", source=source)
        handled, reply, name = asyncio.run(GatewayFixture()._hm_dispatch_plugin_command(event, source, "walive"))
        assert handled and name == "walive"
        return reply

    try:
        yield SimpleNamespace(ctx=context, home=home, runtime=runtime, dispatch=dispatch,
                              handler=handler, calls=calls, result=result, notice=notice,
                              manager=manager, commands=module_commands)
    finally:
        reset_hermes_home_override(token)


def dm(platform=Platform.WHATSAPP, user=OWNER, **fields):
    return SessionSource(platform, fields.pop("chat_id", user), user_id=user, **fields)


def test_owner_can_inspect_and_switch_using_same_profile_operation(plugin):
    owner = dm()
    status = plugin.dispatch(owner, "")
    assert "Provider aktif: codex (default)" in status and "Audio panggilan siap." in status
    assert "/private/" not in status and "GEMINI_PRIVATE_FIXTURE" not in status
    assert plugin.dispatch(owner, "list") == "Provider Live: codex, gemini\nPool: audio"
    assert "sekarang memakai gemini (default)" in plugin.dispatch(owner, "use gemini now")
    assert plugin.calls == [(plugin.runtime, "gemini", True)]
    assert plugin.ctx.get_config("live.selected") == "gemini"


def test_owner_selects_pool_for_next_call_and_failed_switch_does_not_persist(plugin):
    owner = dm()
    plugin.result.update(status="selected")
    assert "berikutnya akan memakai audio" in plugin.dispatch(owner, "use audio")
    plugin.result.update(status="failed", reason="all_rate_limited")
    assert "terkena limit" in plugin.dispatch(owner, "use gemini now")
    assert plugin.calls == [(plugin.runtime, "audio", False), (plugin.runtime, "gemini", True)]
    assert plugin.ctx.get_config("live.selected") == "audio"


@pytest.fixture
def provider_tool(plugin):
    from tools.registry import registry

    commands = plugin.commands
    registration = plugin.ctx.register_tool(
        name=commands.PROVIDER_TOOL, toolset="wa-control-live", schema=commands.PROVIDER_SCHEMA,
        handler=commands.provider_tool_handler(plugin.ctx), is_async=True,
    )
    assert registration is not None

    def invoke(args, session_id="provider-session"):
        return json.loads(registry.dispatch(
            commands.PROVIDER_TOOL, args, scope=plugin.manager.scope_key, session_id=session_id,
        ))

    try:
        yield invoke
    finally:
        registration.dispose()


@contextmanager
def provider_turn(plugin, source, text, *, voice_note=False, queued=False, forged=False, actor=None,
                  internal=False):
    from gateway.config import load_gateway_config
    from gateway.session import SessionEntry
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_ingress import executing_admission
    from hermes_state import SessionDB
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    sid, mid = "provider-session", "active-provider-message"
    platform = source.platform.value
    route = build_session_key(source, profile=source.profile)
    principal = "messaging:" + json.dumps(
        [source.profile, platform, source.chat_id, source.thread_id, source.user_id], separators=(",", ":"),
    )
    event = MessageEvent(text="" if voice_note else text, source=source, message_id=mid, internal=internal)
    native = {
        "route": route, "source": source.to_dict(),
        "event": {"message_id": "another-message" if forged else mid},
    }
    if voice_note:
        native.update(message_type="voice", media=[{"path": "voice-fixture.ogg"}])
    with closing(SessionDB(plugin.home / "state.db")) as db:
        db.create_session(sid, platform, session_key=route, chat_id=source.chat_id,
                          chat_type=source.chat_type, user_id=source.user_id, profile_name=source.profile)
        now = datetime.now(timezone.utc)
        entry = SessionEntry(route, sid, now, now, origin=source)
        db.save_gateway_routing_entry(route, json.dumps(entry.to_dict()),
                                      scope=str(load_gateway_config().sessions_dir.resolve()))
        epoch = begin_runtime_epoch(db, instance_id="provider-command-fixture")
        admission = admit_session_input(db, epoch=epoch, principal_id=principal, session_id=sid,
                                        request_id=mid, payload={"text": event.text, "native_text_v1": native})
        if not queued:
            admission = claim_session_input(db, epoch=epoch, session_id=sid)
            assert admission["status"] == "started"
        event._claimed_owner = (sid, admission["generation"])
        if voice_note:
            db.append_message(sid, role="user", content=f'"{text}"', platform_message_id=mid)

        session_tokens = set_session_vars(
            platform=platform, chat_id=source.chat_id, chat_type=source.chat_type,
            user_id=source.user_id, actor_id=actor, session_key=route, session_id=sid,
            profile=source.profile or "", message_id=mid, turn_event=event,
        )
        execution_token = executing_admission.set(True)
        try:
            yield
        finally:
            executing_admission.reset(execution_token)
            clear_session_vars(session_tokens)


@pytest.mark.parametrize(("voice_note", "text", "args", "status", "switch_now"), [
    (False, "For our next call, use the audio pool.", {"selection": "audio"}, "selected", False),
    (True, "Tolong pindah ke Gemini sekarang.", {"selection": "gemini", "now": True}, "switched", True),
])
def test_natural_provider_tool_uses_owner_turn_and_same_operation(
    plugin, provider_tool, voice_note, text, args, status, switch_now,
):
    plugin.result["status"] = status
    with provider_turn(plugin, dm(), text, voice_note=voice_note):
        assert provider_tool(args)["status"] == status
    assert plugin.calls == [(plugin.runtime, args["selection"], switch_now)]
    assert plugin.ctx.get_config("live.selected") == args["selection"]


def test_natural_provider_tool_failed_switch_leaves_saved_selection(plugin, provider_tool):
    plugin.result.clear()
    plugin.result.update(status="failed", reason="all_rate_limited")
    with provider_turn(plugin, dm(), "Ganti ke Gemini saat telepon ini berjalan."):
        assert provider_tool({"selection": "gemini", "now": True})["status"] == "failed"
    assert plugin.calls == [(plugin.runtime, "gemini", True)]
    assert plugin.ctx.get_config("live.selected") == "codex"


@pytest.mark.parametrize(("source_fields", "turn_fields"), [
    ({"user": OTHER}, {}), ({"is_bot": True}, {}), ({"chat_type": "group"}, {}),
    ({"profile": "other"}, {}), ({"delivered_via_upstream_relay": True}, {}),
    ({}, {"actor": OTHER}), ({}, {"forged": True}), ({}, {"queued": True}), ({}, {"internal": True}),
])
def test_natural_provider_tool_denies_untrusted_routes_and_turns(plugin, provider_tool, source_fields, turn_fields):
    with provider_turn(plugin, dm(**source_fields), "Switch audio to Gemini.", **turn_fields):
        assert provider_tool({"selection": "gemini", "now": True})["status"] == "denied"
    assert plugin.calls == []
    assert plugin.ctx.get_config("live.selected") == "codex"


def test_natural_provider_tool_denies_unbound_or_mismatched_session(plugin, provider_tool):
    args = {"selection": "gemini"}
    assert provider_tool(args)["status"] == "denied"
    with provider_turn(plugin, dm(), "Gemini for my next call."):
        assert provider_tool(args, session_id="copied-session")["status"] == "denied"
    assert plugin.calls == []


@pytest.mark.parametrize(("args", "status"), [
    ({"selection": "gemini", "now": "false"}, "invalid_request"),
    ({"selection": "gemini", "profile": "other"}, "invalid_request"),
    ({"selection": "https://secret.example"}, "invalid_selection"),
    ({"selection": "unknown-alias"}, "invalid_selection"),
])
def test_natural_provider_tool_rejects_invalid_payloads_without_changes(plugin, provider_tool, args, status):
    with provider_turn(plugin, dm(), "Change the provider."):
        result = provider_tool(args)
    if "profile" in args and "error" in result:
        # Hermes v0.23.2 rejects undeclared parameters before the plugin handler runs.
        assert result["error"] == (
            f"profile: not a parameter of {plugin.commands.PROVIDER_TOOL}. "
            "The tool was NOT invoked. Valid parameters: selection, now."
        )
    else:
        assert result["status"] == status
    assert "secret.example" not in json.dumps(result)
    assert plugin.calls == []
    assert plugin.ctx.get_config("live.selected") == "codex"


@pytest.mark.parametrize("allowed", [False, True])
def test_natural_provider_tool_applies_same_telegram_policy(plugin, provider_tool, monkeypatch, allowed):
    extra = {"allow_admin_from": [123 if allowed else 999]}
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, extra=extra)})
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    plugin.result["status"] = "selected"
    with provider_turn(plugin, dm(Platform.TELEGRAM, "123"), "Use Gemini for my next call."):
        result = provider_tool({"selection": "gemini"})
    assert result["status"] == ("selected" if allowed else "denied")
    assert plugin.calls == ([(plugin.runtime, "gemini", False)] if allowed else [])


@pytest.mark.parametrize("fields", [
    {"user": OTHER}, {"chat_id": OTHER}, {"chat_type": "group"}, {"thread_id": "1"},
    {"is_bot": True}, {"profile": "other"}, {"profile_route_rejected": True},
    {"delivered_via_upstream_relay": True},
])
def test_non_owner_and_unsafe_whatsapp_routes_are_denied(plugin, fields):
    assert "hanya tersedia" in plugin.dispatch(dm(**fields), "use gemini now")
    assert plugin.calls == [] and plugin.ctx.get_config("live.selected") == "codex"


def test_multiplex_transport_home_cannot_use_another_profiles_live_service(plugin):
    owner = dm(profile="cobalt")
    runner = SimpleNamespace(config=GatewayConfig(multiplex_profiles=True), _primary_profile_name="other",
                             _transport_owner=lambda _: None, _resolve_profile_home_for_source=lambda _: plugin.home)
    assert canonical_identity(owner, runner=runner, transport_profile="other",
                              primary_home=plugin.home.parent / "other") is not None
    assert "hanya tersedia" in plugin.dispatch(owner, "status")


def test_direct_unbound_call_and_invalid_alias_are_denied_without_leaking_text(plugin):
    owner = dm()
    reply = asyncio.run(plugin.handler("use gemini now", source=owner, platform="whatsapp",
                                       session_key=build_session_key(owner)))
    assert "hanya tersedia" in reply
    assert "Gunakan /walive" in plugin.dispatch(owner, "use https://secret.example")
    assert "Periksa provider atau pool" in plugin.dispatch(owner, "use unknown-alias now")
    assert plugin.calls == []


def test_status_reports_safe_failure_notice_evidence(plugin):
    owner = dm()
    plugin.notice["adapter"] = "sent_to_sidecar"
    reply = plugin.dispatch(owner, "status")
    assert "belum terkonfirmasi" in reply and "menerima pemberitahuan DM" not in reply
    plugin.notice["sidecar"] = {"reason": "all_rate_limited", "audio": "media_submitted", "dm": "accepted",
                                "call_id": "fixture-private-call", "receipt": "fixture-private-receipt"}
    reply = plugin.dispatch(owner, "status")
    assert "terkena limit" in reply and "diteruskan ke jalur media" in reply and "menerima pemberitahuan DM" in reply
    assert "fixture-private" not in reply


@pytest.mark.parametrize("access,is_allowed", [
    ({}, False),
    ({"allow_admin_from": [999]}, False),
    ({"allow_admin_from": [123]}, True),
    ({"allow_admin_from": [999], "user_allowed_commands": ["walive"]}, True),
])
def test_telegram_requires_explicit_profile_slash_authorization(plugin, monkeypatch, access, is_allowed):
    config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, extra=access)})
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    reply = plugin.dispatch(dm(Platform.TELEGRAM, "123"), "use gemini now")
    assert ("sekarang memakai" in reply) is is_allowed
    assert bool(plugin.calls) is is_allowed
    assert "hanya tersedia" in plugin.dispatch(dm(Platform.TELEGRAM, "123", chat_type="group"), "list")


@pytest.mark.parametrize("mode", ["queue", "steer", "interrupt"])
def test_registered_plugin_slash_follows_stock_hermes_busy_input_mode(plugin, mode):
    pytest.importorskip("gateway.run")
    from gateway.run_inbound import GatewayInboundMixin

    routes = []

    class BusyGateway(GatewayInboundMixin):
        _draining = False
        _agent_has_active_subagents = staticmethod(lambda agent: False)
        _effective_busy_input_mode = staticmethod(lambda source: mode)
        _peek_session_state = staticmethod(lambda key: SimpleNamespace(
            turn=SimpleNamespace(agent=object(), started_ts=0),
        ))

        async def _busy_skill_turn(self, _event, _source, _key):
            return False, None

        async def _session_has_compression_in_flight(self, _key):
            return False

        def _queue_or_replace_pending_event(self, _key, _event):
            routes.append("queue")

        def _hm_busy_steer(self, _event, _agent, _key):
            routes.append("steer")

        async def _hm_busy_interrupt(self, _event, _source, _agent, _key):
            routes.append("interrupt")

    owner = dm()
    event = MessageEvent(text="/walive use gemini now", source=owner)
    reply = asyncio.run(BusyGateway()._hm_handle_running_session_message(event, owner, build_session_key(owner)))
    assert reply is None and routes == [mode] and plugin.calls == []
