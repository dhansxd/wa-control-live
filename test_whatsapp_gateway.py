"""Exercise the gateway transport with a single synthetic WhatsApp sidecar."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("gateway.config")
from aiohttp import web
from gateway.config import Platform, PlatformConfig
from gateway.platforms.event import MessageType

from whatsapp_gateway import WhatsAppLiveAdapter, configured_pair, register_gateway, voice_session_creator


OWNER = "15550100101"
BOT = "15550100102"
LID = "4021@lid"
TOKEN = "synthetic-private-token"


@pytest.fixture
def on_unload():
    callbacks = []
    yield callbacks.append
    for callback in reversed(callbacks):
        callback()


def test_registration_uses_only_the_configured_bot_and_one_profile_transport(tmp_path, on_unload):
    settings = {"unified_gateway": True, "bot_number": BOT, "owner_number": "+" + OWNER}
    calls = []
    ctx = SimpleNamespace(get_config=lambda name, default=None: settings.get(name, default),
                          state=SimpleNamespace(data_dir=tmp_path),
                          register_platform=lambda **kwargs: calls.append(kwargs) or object(),
                          register_rpc=lambda name, handler: None, on_unload=on_unload)
    register_gateway(ctx, *configured_pair(ctx))
    registration, = calls
    assert registration["name"] == "whatsapp"
    assert "trusted_inbound" not in registration
    adapter = registration["adapter_factory"](PlatformConfig())
    assert (adapter.platform, adapter.owner, adapter.bot) == (Platform.WHATSAPP, OWNER, BOT)
    assert adapter.token_path == tmp_path / "unified" / "adapter.token"
    settings["bot_number"] = OWNER
    with pytest.raises(ValueError, match="different numbers"):
        configured_pair(ctx)


def test_first_call_opens_the_native_whatsapp_route_before_any_dm(tmp_path, monkeypatch, on_unload):
    pytest.importorskip("pydantic", exc_type=ImportError)
    from gateway import config as gateway_config, run_api
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import plugin_rpc
    from whatsapp_session import save_voice_exchange

    async def exercise():
        registration, rpcs = {}, {}
        ctx = SimpleNamespace(plugin_id="wa-control-live", state=SimpleNamespace(data_dir=tmp_path),
                              register_platform=lambda **kwargs: registration.update(kwargs) or object(),
                              register_rpc=lambda name, handler: rpcs.update({f"plugin.{ctx.plugin_id}.{name}": handler}),
                              on_unload=on_unload)
        register_gateway(ctx, OWNER, BOT)
        monkeypatch.setattr(plugin_rpc, "plugin_rpc_handler", rpcs.get)
        adapter = registration["adapter_factory"](PlatformConfig())
        adapter.token_path.parent.mkdir()
        adapter.token_path.write_text(TOKEN, encoding="utf-8")
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions", multiplex_profiles=False))
        runner.adapters[Platform.WHATSAPP] = adapter
        adapter.gateway_runner = runner
        monkeypatch.setattr(run_api, "_ACTIVE_RUNNER", runner)
        authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id="fixture")
        connection = AuthorityConnection(authority, object(), {"user_id": "owner"})

        async def rpc(method, *, _timeout=None, **params):
            return await connection.dispatch({"id": 1, "method": method, "params": params})

        create_session = voice_session_creator(ctx)
        client = SimpleNamespace(rpc=AsyncMock(side_effect=rpc))
        try:
            source = adapter.owner_source()
            route_key = runner._session_key_for_source(source)
            assert await runner.async_session_store.lookup_by_session_key(route_key) is None

            method = f"plugin.{ctx.plugin_id}.voice.create_session"
            refused = await rpc(method, token=TOKEN)
            assert refused["error"]["data"]["reason"] == "plugin_rpc_failed"
            adapter.verified = True
            refused = await rpc(method, token="wrong-token")
            assert refused["error"]["data"]["reason"] == "plugin_rpc_failed"
            assert await runner.async_session_store.lookup_by_session_key(route_key) is None

            await create_session(client)
            client.rpc.assert_awaited_once_with(method, token=TOKEN, _timeout=4)
            opened = await runner.async_session_store.lookup_by_session_key(route_key)
            assert opened.origin.chat_id == opened.origin.user_id == OWNER
            assert opened.origin.platform is Platform.WHATSAPP
            assert await runner.async_session_store.load_transcript(opened.session_id) == []
            resumed = await rpc("session.resume", session_id=opened.session_id, omit_messages=True)
            assert resumed["result"]["session_id"] == opened.session_id

            monkeypatch.setattr(gateway_config, "load_gateway_config", lambda: runner.config)
            spoken = ("Judulnya Laut Biru.", "Aku catat judulnya.")
            for _ in range(2):
                assert await asyncio.to_thread(
                    save_voice_exchange, tmp_path, opened.session_id, "a" * 32, *spoken,
                ) == "saved"

            handler = AsyncMock()
            monkeypatch.setattr(adapter, "handle_message", handler)
            await adapter._receive_message({"id": "first-dm", "text": "Lanjutkan.", "chat_id": LID,
                                            "sender_id": LID, "sender_id_alt": f"{OWNER}@s.whatsapp.net"})
            dm = handler.await_args.args[0]
            existing = await runner.async_session_store.get_or_create_session(dm.source)
            assert existing.session_id == opened.session_id
            history = await runner.async_session_store.load_transcript(existing.session_id)
            assert [(row["role"], row["content"]) for row in history] == list(zip(("user", "assistant"), spoken))
            assert (await rpc(method, token=TOKEN))["result"] == {"session_id": opened.session_id}
        finally:
            await connection.close()
            runner.session_store.close_all_db_handles()
            runner.close_all_session_db_handles()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", OWNER)
    asyncio.run(exercise())


def test_secondary_call_uses_host_route_and_its_own_transcript(tmp_path, monkeypatch, on_unload):
    pytest.importorskip("pydantic", exc_type=ImportError)
    from contextlib import asynccontextmanager, closing
    from functools import partial
    import json

    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from gateway import run_api
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import gateway_client, gateway_runtime, plugin_rpc
    from hermes_state import SessionDB

    from live_agent import _gateway, _run
    from whatsapp_gateway import voice_routing_reader
    from whatsapp_session import resolve_owner_session

    launch_home = tmp_path / "hermes"
    name = "secondary"
    home = launch_home / "profiles" / name
    home.mkdir(parents=True)
    (home / ".env").write_text(f"WHATSAPP_ALLOWED_USERS={OWNER}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    scope = launch_home / "routing-not-default"
    config = GatewayConfig(sessions_dir=scope, multiplex_profiles=True)

    async def exercise():
        prior_multiplex = is_multiplex_active()
        runner = GatewayRunner(config)
        launch_db = runner.session_store._routing_db
        registry = SessionAuthorities(launch_home, multiplexed=True)
        runner.session_authorities = registry
        registration, rpcs = {}, {}
        ctx = SimpleNamespace(
            plugin_id="wa-control-live", state=SimpleNamespace(data_dir=home / "plugin-data"),
            register_platform=lambda **kwargs: registration.update(kwargs) or object(),
            register_rpc=lambda local, handler: rpcs.update({f"plugin.{ctx.plugin_id}.{local}": handler}),
            on_unload=on_unload,
        )
        monkeypatch.setattr(plugin_rpc, "plugin_rpc_handler", rpcs.get)
        connection = None
        with closing(SessionDB(home / "state.db")) as profile_db:
            try:
                root_auth = await initialize_session_authority(runner, profile_id=str(launch_home), instance_id="fixture")
                registry.add(launch_home, root_auth, name="default")
                with _profile_runtime_scope(home, hydrate_secrets=False):
                    authority = await initialize_session_authority(
                        runner, profile_id=str(home), instance_id="fixture", db=profile_db, register=False,
                    )
                    registry.add(home, authority, name=name)
                    register_gateway(ctx, OWNER, BOT)
                    adapter = registration["adapter_factory"](PlatformConfig())
                    adapter.token_path.parent.mkdir(parents=True)
                    adapter.token_path.write_text(TOKEN, encoding="utf-8")
                    adapter.gateway_runner = runner
                    runner._configure_profile_adapter(adapter, name, Platform.WHATSAPP)
                    runner._profile_adapters[name] = {Platform.WHATSAPP: adapter}
                    adapter.verified = True

                route = runner._session_key_for_source(adapter.owner_source())
                assert route == f"agent:{name}:whatsapp:dm:{OWNER}"
                store = runner.async_session_store
                assert await store.lookup_by_session_key(route) is None
                connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
                endpoint = gateway_runtime.GatewayEndpoint(
                    profile_id=str(home), instance_id="fixture", authority_epoch=authority.epoch,
                    runtime_protocol=1, api_origin="http://unused.local", supervisor="none",
                    capabilities=frozenset(), control_home=str(launch_home),
                )

                async def dispatch(method, **params):
                    # The worker has no in-process gateway outside RPC dispatch.
                    with monkeypatch.context() as gateway_process:
                        gateway_process.setattr(run_api, "_ACTIVE_RUNNER", runner)
                        return await connection.dispatch({"id": 1, "method": method, "params": params})

                async def rpc(method, *, _timeout=None, **params):
                    envelope = await dispatch(method, **params)
                    assert "error" not in envelope, envelope
                    return envelope["result"]

                @asynccontextmanager
                async def connect_gateway(*, endpoint):
                    assert endpoint.profile_id == str(home) and endpoint.control_home == str(launch_home)
                    yield SimpleNamespace(rpc=rpc)

                monkeypatch.setattr(run_api, "_ACTIVE_RUNNER", None)
                monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint",
                                    lambda _: gateway_runtime.GatewayDiscovery("ready", endpoint))
                monkeypatch.setattr(gateway_client, "connect_gateway", connect_gateway)

                routing_method = f"plugin.{ctx.plugin_id}.voice.routing"
                refused = await dispatch(routing_method, token="wrong-token")
                assert refused["error"]["data"]["reason"] == "plugin_rpc_failed"
                assert await rpc(routing_method, token=TOKEN) == {
                    "control_home": str(launch_home), "scope": str(scope),
                }

                read_routing = voice_routing_reader(ctx)

                async def call(request, *, routing=read_routing):
                    with _profile_runtime_scope(home, hydrate_secrets=False):
                        return await _run(
                            home, OWNER, request, resolve=resolve_owner_session,
                            connect=partial(_gateway, read_routing=routing),
                            ensure_session=voice_session_creator(ctx),
                        )

                async def mismatched_control_home(client):
                    return {**await read_routing(client), "control_home": str(home)}

                assert await call({"action": "context"}, routing=mismatched_control_home) == {"status": "unavailable"}
                assert await store.lookup_by_session_key(route) is None
                opened = await call({"action": "context"})
                assert opened["status"] == "ready" and not opened["history"]
                session_id = opened["session_id"]
                entries = launch_db.load_gateway_routing_entries(scope=str(scope))
                assert json.loads(entries[route])["session_id"] == session_id
                assert not profile_db.load_gateway_routing_entries(scope=str(scope))
                assert launch_db.get_session(session_id) is None

                exchange = {"action": "save_exchange", "session_id": session_id, "exchange_id": "a" * 32,
                            "user": "Judulnya Laut Biru.", "assistant": "Aku catat judulnya."}
                for _ in range(2):
                    assert await call(exchange) == {"status": "saved"}

                handler = AsyncMock()
                monkeypatch.setattr(adapter, "handle_message", handler)
                await adapter._receive_message({"id": "first-dm", "text": "Lanjutkan.", "chat_id": LID,
                                                "sender_id": LID, "sender_id_alt": f"{OWNER}@s.whatsapp.net"})
                dm = handler.await_args.args[0]
                assert (await store.get_or_create_session(dm.source)).session_id == session_id
                history = await store.load_transcript(session_id)
                assert [(row["role"], row["content"]) for row in history] == [
                    ("user", exchange["user"]), ("assistant", exchange["assistant"]),
                ]

                reset = await store.reset_session(route)
                assert (await call({"action": "context"}))["session_id"] == reset.session_id != session_id
                assert await call({**exchange, "exchange_id": "b" * 32}) == {"status": "session_changed"}
                assert await store.load_transcript(reset.session_id) == []
                await store.switch_session(route, session_id)
                resumed = await call({"action": "context"})
                assert resumed["session_id"] == session_id
                assert [row["text"] for row in resumed["history"]] == [exchange["user"], exchange["assistant"]]
            finally:
                if connection is not None:
                    await connection.close()
                runner.session_store.close_all_db_handles()
                runner.close_all_session_db_handles()
                set_multiplex_active(prior_multiplex)

    asyncio.run(exercise())


def test_rebuilt_adapter_rejects_previously_accepted_replay(tmp_path, monkeypatch):
    from gateway.platforms.helpers import carry_inbound_dedup

    async def exercise():
        old = WhatsAppLiveAdapter(PlatformConfig(), OWNER, BOT, tmp_path / "adapter.token")
        replacement = WhatsAppLiveAdapter(PlatformConfig(), OWNER, BOT, old.token_path)
        handled = AsyncMock()
        monkeypatch.setattr(old, "handle_message", handled)
        monkeypatch.setattr(replacement, "handle_message", handled)
        event = {"id": "received-1", "text": "Ini sudah diterima.", "chat_id": OWNER, "sender_id": OWNER}
        await old._receive_message(event)
        carry_inbound_dedup(old, replacement)
        await replacement._receive_message(event)
        handled.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("media,mime,kind,cache,ext", [
    ("image", "image/png", MessageType.PHOTO, "cache_image_from_bytes_async", ".png"),
    ("voice", "audio/ogg", MessageType.VOICE, "cache_audio_from_bytes_async", ".ogg"),
])
def test_only_the_verified_owner_can_attach_media_to_the_active_dm(
        tmp_path, monkeypatch, media, mime, kind, cache, ext):
    import whatsapp_gateway as gateway

    async def exercise():
        adapter = WhatsAppLiveAdapter(PlatformConfig(), OWNER, BOT, tmp_path / "adapter.token")
        handler = AsyncMock()
        cache_media = AsyncMock(return_value=str(tmp_path / f"attachment{ext}"))
        monkeypatch.setattr(adapter, "handle_message", handler)
        monkeypatch.setattr(gateway, cache, cache_media)
        requests = []

        async def chunks(_):
            yield b"verified media"

        @asynccontextmanager
        async def get(path, **kwargs):
            requests.append((path, kwargs["params"]))
            yield SimpleNamespace(raise_for_status=lambda: None, content_type=mime,
                                  content=SimpleNamespace(iter_chunked=chunks))

        adapter.http = SimpleNamespace(get=get)
        event = {"id": "media-id", "text": "", "media": media, "chat_id": LID, "sender_id": LID}
        await adapter._receive_message({**event, "sender_id_alt": "15551110003@s.whatsapp.net"})
        assert not requests
        event["sender_id_alt"] = f"{OWNER}@s.whatsapp.net"
        await adapter._receive_message(event)
        await adapter._receive_message(event)
        assert requests == [("/control/media", {"chat": LID, "id": "media-id", "bot_number": BOT})]
        cache_media.assert_awaited_once_with(b"verified media", ext)
        handler.assert_awaited_once()
        routed = handler.call_args.args[0]
        assert routed.source.chat_id == OWNER and routed.source.user_id == OWNER
        assert routed.message_type == kind and routed.media_types == [mime]
        assert routed.media_urls == [str(tmp_path / f"attachment{ext}")]

    asyncio.run(exercise())


@pytest.mark.parametrize("paired_bot", [BOT, "15550100103"])
def test_same_authenticated_sidecar_receives_owner_dm_and_sends_reply(tmp_path, monkeypatch, paired_bot):
    async def exercise():
        first_poll, wait_for_close = asyncio.Event(), asyncio.Event()
        health_read = asyncio.Event()
        requests = []
        owner_jid = f"{OWNER}@s.whatsapp.net"
        typed = {"id": "original-1", "text": "Lanjutkan yang tadi.", "chat_id": LID,
                 "sender_id": LID, "sender_id_alt": owner_jid}
        invalid = {**typed, "id": "wrong-origin", "chat_id": "12000000000@g.us"}

        async def health(request):
            assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
            health_read.set()
            return web.json_response({"logged_in": True, "bot": paired_bot, "owner": OWNER})

        async def inbound(request):
            assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
            if request.query.get("after"):
                assert request.query["after"] == "epoch:1"
                first_poll.set()
                await wait_for_close.wait()
            return web.json_response({"cursor": "epoch:1", "messages": [invalid, typed, typed]})

        async def outbound(request):
            assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
            requests.append(await request.json())
            return web.json_response({"message_id": "reply-from-same-sidecar"})

        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/owner/messages", inbound)
        app.router.add_post("/owner/messages", outbound)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        token_file = tmp_path / "adapter.token"
        token_file.write_text(TOKEN, encoding="utf-8")
        adapter = WhatsAppLiveAdapter(PlatformConfig(), OWNER, BOT, token_file, endpoint=f"http://127.0.0.1:{port}")
        mark = []
        monkeypatch.setattr(adapter, "_mark_connected", lambda: mark.append(adapter.send_path_degraded))
        monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)
        handler = AsyncMock()
        monkeypatch.setattr(adapter, "handle_message", handler)
        try:
            assert await adapter.connect()
            await asyncio.wait_for(health_read.wait(), 2)
            if paired_bot != BOT:
                reply = await adapter.send(OWNER, "Jangan dikirim.")
                assert adapter.send_path_degraded and not reply.success
                assert not first_poll.is_set() and not requests
                handler.assert_not_awaited()
                return
            await asyncio.wait_for(first_poll.wait(), 2)
            assert mark == [True, False]
            event = handler.await_args.args[0]
            handler.assert_awaited_once()
            assert (event.message_id, event.text, event.source.platform, event.source.chat_type) == (
                typed["id"], typed["text"], Platform.WHATSAPP, "dm",
            )
            assert (event.source.chat_id, event.source.user_id, event.source.user_id_alt) == (OWNER, OWNER, LID)
            reply = await adapter.send(event.source.chat_id, "Bisa, kita lanjut.")
            assert reply.success and reply.message_id == "reply-from-same-sidecar"
            assert (await adapter.send(LID, "Lanjut di percakapan yang sama.")).success
            assert not (await adapter.send("15550100104", "Jangan dikirim.")).success
            assert not (await adapter.send("4022@lid", "Jangan dikirim.")).success
            assert requests == [{"chat_id": LID, "text": "Bisa, kita lanjut."},
                                {"chat_id": LID, "text": "Lanjut di percakapan yang sama."}]
        finally:
            await adapter.disconnect()
            wait_for_close.set()
            await runner.cleanup()

    asyncio.run(exercise())
