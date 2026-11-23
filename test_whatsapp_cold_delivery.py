"""Compare historical WA Control & Live revisions with the same cold-call scenario.

Only the model's work and the WhatsApp sidecar are simulated. The plugin's
Live commands, Hermes routing, admission, delivery and attachment staging run
in a temporary Hermes home. The sidecar is HTTP loopback only.
"""

import asyncio
import base64
from contextlib import asynccontextmanager
from functools import partial
from importlib.util import find_spec
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


OWNER = "15550100101"
BOT = "15550100102"
LID = "40123456789001@lid"
PHONE = f"{OWNER}@s.whatsapp.net"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


@pytest.mark.parametrize("origin,inbound_after_restart,image_status", [
    pytest.param(LID, False, 200, id="persisted-lid-first-call"),
    pytest.param(LID, True, 200, id="persisted-lid-dm-before-call"),
    pytest.param(PHONE, False, 200, id="persisted-phone-first-call"),
    pytest.param(PHONE, False, 422, id="photo-rejected-after-text-sent"),
])
def test_first_voice_task_delivers_image(tmp_path, monkeypatch, origin, inbound_after_restart, image_status):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", OWNER)

    pytest.importorskip("gateway.config")
    from aiohttp import web
    import live_agent
    from gateway import config as gateway_config
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms import base
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from hermes_constants import get_hermes_dir
    from whatsapp_gateway import WhatsAppLiveAdapter
    from whatsapp_session import resolve_owner_session

    config = GatewayConfig(sessions_dir=home / "sessions", multiplex_profiles=False)
    monkeypatch.setattr(gateway_config, "load_gateway_config", lambda: config)
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path,))
    identity = get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")
    identity.mkdir(parents=True, exist_ok=True)
    (identity / f"lid-mapping-{OWNER}.json").write_text(json.dumps(LID.split("@")[0]))
    (identity / f"lid-mapping-{LID.split('@')[0]}_reverse.json").write_text(json.dumps(OWNER))

    # Stock WhatsApp can leave this origin in the existing owner's DM.
    before = GatewayRunner(config)
    entry = before.session_store.get_or_create_session(
        SessionSource(platform=Platform.WHATSAPP, chat_id=origin, user_id=PHONE, chat_type="dm")
    )
    before.session_store.close_all_db_handles()
    before.close_all_session_db_handles()

    async def exercise():
        connected = asyncio.Event()
        stop = asyncio.Event()
        outbound = []
        release_image = asyncio.Event()

        async def health(_):
            return web.json_response({"logged_in": True, "owner": OWNER, "bot": BOT})

        async def incoming(_):
            connected.set()
            await stop.wait()
            return web.json_response({"cursor": "test:0", "messages": []})

        async def outgoing(request):
            body = await request.json()
            if request.path == "/owner/messages":
                outbound.append(("text", body["chat_id"]))
                return web.json_response({"message_id": "fixture-text"})
            assert body["kind"] == "image" and Path(body["path"]).read_bytes() == PNG
            outbound.append(("image", body["chat_id"]))
            await release_image.wait()
            result = "accepted" if image_status == 200 else "rejected"
            record = {"message_id": body["message_id"], "status": result, "chat_id": body["chat_id"]}
            directory = runtime / "outbound-receipts" / BOT
            directory.mkdir(parents=True, exist_ok=True)
            (directory / body["message_id"]).write_text(json.dumps(record))
            return web.json_response(record, status=image_status)

        sidecar = web.Application()
        sidecar.router.add_get("/health", health)
        sidecar.router.add_get("/owner/messages", incoming)
        sidecar.router.add_post("/owner/messages", outgoing)
        sidecar.router.add_post("/messages", outgoing)
        server = web.AppRunner(sidecar)
        await server.setup()
        await web.TCPSite(server, "127.0.0.1", 0).start()
        endpoint = f"http://127.0.0.1:{server.addresses[0][1]}"
        runtime = home / "plugin-data" / "unified"
        runtime.mkdir(parents=True)
        token_file = runtime / "adapter.token"
        token_file.write_text("test-only")
        runner = GatewayRunner(config)
        adapter = WhatsAppLiveAdapter(PlatformConfig(), OWNER, BOT, token_file, endpoint=endpoint)
        adapter.gateway_runner = runner
        runner.adapters[Platform.WHATSAPP] = adapter
        authority = await initialize_session_authority(runner, profile_id=str(home), instance_id="after-restart")
        connection = AuthorityConnection(authority, object(), {
            "provider": "local", "user_id": f"uid:{os.getuid()}",
            "instance_id": authority.instance_id, "profile_id": str(home),
            "capabilities": ["session:read", "session:submit", "session:control"],
        }, operator=True)

        async def rpc(method, *, _timeout=None, **params):
            reply = await connection.dispatch({"id": 1, "method": method, "params": params})
            assert "error" not in reply, (method, reply.get("error"))
            return reply["result"]

        @asynccontextmanager
        async def connect():
            yield SimpleNamespace(rpc=rpc)

        jobs = []
        async def generate(event):
            assert event.text == "Buatkan satu gambar."
            jobs.append(event._claimed_owner[0])
            image = tmp_path / "hasil.png"
            image.write_bytes(PNG)
            return f"Sudah kukirim gambarnya.\nMEDIA:{image}"

        monkeypatch.setattr(runner, "_handle_message", generate)
        async def receive_owner_dm(event):
            resumed = await runner.async_session_store.get_or_create_session(event.source)
            assert resumed.session_id == entry.session_id

        monkeypatch.setattr(adapter, "handle_message", receive_owner_dm)
        # Keep the same fixture executable against revisions predating media receipts.
        options = {}
        if find_spec("whatsapp_media_delivery") is not None:
            from whatsapp_media_delivery import read_media_delivery
            options["read_media_delivery"] = partial(read_media_delivery, runtime, BOT)

        async def voice(request):
            return await live_agent._run(home, OWNER, request, resolve=resolve_owner_session,
                                         connect=connect, poll_seconds=5, **options)

        try:
            assert await adapter.connect()
            await asyncio.wait_for(connected.wait(), 3)
            assert adapter.verified
            if inbound_after_restart:
                await adapter._receive_message({"id": "dm-before-call", "text": "Halo", "chat_id": LID,
                                                "sender_id": LID, "sender_id_alt": PHONE})
            opened = await voice({"action": "context"})
            assert opened["session_id"] == entry.session_id
            receipt = await voice({"action": "submit", "session_id": opened["session_id"],
                                   "submission_id": "2" * 32, "text": "Buatkan satu gambar.",
                                   "direction": "inbound"})
            assert receipt["status"] == "completed", receipt
            observe = {"action": "observe_work", **receipt["receipt"]}
            during_upload = await voice(observe)
            assert during_upload["status"] == "completed" and during_upload["has_attachments"]
            assert during_upload.get("media_delivery") != "sent"
            release_image.set()
            drain = authority.sessions[entry.session_id].task
            if drain is not None:
                await asyncio.wait_for(asyncio.shield(drain), 10)
            assert jobs == [entry.session_id]
            assert [kind for kind, _ in outbound] == ["text", "image"]
            assert all(dest in (PHONE, LID) for _, dest in outbound)
            after_upload = await voice(observe)
            assert after_upload["text_delivery"] == "sent"
            assert after_upload.get("media_delivery") == ("sent" if image_status == 200 else "rejected")
            assert jobs == [entry.session_id]
        finally:
            release_image.set()
            await connection.close()
            await adapter.disconnect()
            stop.set()
            await server.cleanup()
            runner.session_store.close_all_db_handles()
            runner.close_all_session_db_handles()

    asyncio.run(exercise())
