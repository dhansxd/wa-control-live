"""Regression coverage for the unified Control worker's account dispatch."""

import asyncio
import hashlib
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import ANY, AsyncMock, call, patch

from aiohttp import web


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("unified_control_test", ROOT / "__init__.py",
                                             submodule_search_locations=[str(ROOT)])
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)

from unified_control_test.whatsapp_control import rust_bridge, service as service_module
from unified_control_test.whatsapp_control.runtime import BridgeError, activation_ids, active_install
from unified_control_test.whatsapp_control.service import Service
from unified_control_test.whatsapp_control.settings import read_json, write_json


class OfflinePairTest(TestCase):
    bot = "15550100202"
    identity = {"id": "15550100203@s.whatsapp.net", "phone": "15550100203@s.whatsapp.net"}

    def setUp(self):
        self.workspace = Path(self.enterContext(TemporaryDirectory()))
        self.root = self.workspace / "control"
        self.runtime = self.workspace / "unified"
        self.runtime.mkdir()
        (self.runtime / "paired.json").touch()
        write_json(self.runtime / "config.json", {"bot_number": self.bot})
        self.enterContext(patch.object(service_module, "socket_dir", return_value=self.workspace / "socket"))

    def test_pair_without_gateway_uses_only_qr_bridge_and_holds_worker_lock(self):
        events = []
        bridge = SimpleNamespace(start=AsyncMock(), call=AsyncMock(), close=AsyncMock())

        def make_bridge(root, on_event):
            self.assertEqual(root, self.root)

            async def pair(op, account, args):
                self.assertEqual((op, account, args), ("pair", "personal", {"reserved_bot_number": self.bot}))
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with service_module._worker_lock(root):
                        self.fail("The normal worker can compete with offline pairing.")
                on_event({"event": "message", "account": "personal", "terminal": "ignored"})
                on_event({"event": "qr", "account": "other", "terminal": "ignored"})
                on_event({"event": "qr", "account": "personal", "terminal": "synthetic QR"})
                return {"status": "connected", "identity": self.identity}

            bridge.call.side_effect = pair
            return bridge

        with patch.object(service_module, "Bridge", side_effect=make_bridge):
            result = service_module.pair_offline(
                self.root, "personal", events.append, lambda: True, bot_runtime=self.runtime, bot_number=self.bot)

        self.assertEqual(result, {"account": "personal", "status": "paired", "identity": self.identity})
        self.assertEqual(events, [{"event": "qr", "account": "personal", "terminal": "synthetic QR"}])
        bridge.start.assert_awaited_once()
        bridge.close.assert_awaited_once()
        self.assertEqual(read_json(self.root / "accounts.json"), {"personal": {"_identity": self.identity}})
        self.assertEqual({path.name for path in self.root.iterdir()}, {"accounts.json"})
        with service_module._worker_lock(self.root):
            pass

    def test_duplicate_identity_rolls_back_new_link_and_keeps_existing_account(self):
        self.root.mkdir()
        accounts = {"existing": {"_identity": self.identity}}
        write_json(self.root / "accounts.json", accounts)
        bridge = SimpleNamespace(
            start=AsyncMock(), close=AsyncMock(), call=AsyncMock(side_effect=[
                {"status": "connected", "identity": self.identity}, {}, {}, {},
            ]),
        )
        with patch.object(service_module, "Bridge", return_value=bridge), self.assertRaisesRegex(
                ValueError, "already paired"):
            service_module.pair_offline(
                self.root, "personal", lambda _event: None, lambda: True,
                bot_runtime=self.runtime, bot_number=self.bot)
        bridge.call.assert_has_awaits([
            call("pair", "personal", {"reserved_bot_number": self.bot}),
            call("logout", "personal", None),
            call("disconnect", "personal", None),
            call("remove_account", "personal", None),
        ])
        bridge.close.assert_awaited_once()
        self.assertEqual(read_json(self.root / "accounts.json"), accounts)

    def test_rejects_unpaired_bot_and_nonlocal_inventory_before_starting_bridge(self):
        self.root.mkdir()
        inventory = self.workspace / "elsewhere.json"
        write_json(inventory, {})
        (self.root / "accounts.json").symlink_to(inventory)
        bridge = SimpleNamespace(start=AsyncMock())
        with patch.object(service_module, "Bridge", return_value=bridge), self.assertRaisesRegex(
                ValueError, "account inventory"):
            service_module.pair_offline(
                self.root, "personal", lambda _event: None, lambda: True,
                bot_runtime=self.runtime, bot_number=self.bot)
        (self.runtime / "paired.json").unlink()
        with patch.object(service_module, "Bridge", return_value=bridge), self.assertRaisesRegex(
                RuntimeError, "Link the Hermes bot"):
            service_module.pair_offline(
                self.root, "personal", lambda _event: None, lambda: True,
                bot_runtime=self.runtime, bot_number=self.bot)
        bridge.start.assert_not_awaited()


class UnifiedControlTest(IsolatedAsyncioTestCase):
    owner = "15550100201"
    bot = "15550100202"
    secondary = "15550100203"

    def setUp(self):
        from tempfile import TemporaryDirectory

        workspace = Path(self.enterContext(TemporaryDirectory()))
        self.root = workspace / "control"
        self.root.mkdir()
        self.runtime = workspace / "unified"
        self.runtime.mkdir()
        self.identity = {"id": "9202@lid", "phone": self.bot + "@s.whatsapp.net", "lid": "9202@lid"}
        self.accounts = {"main": {"_identity": self.identity, "dm_mode": "all"},
                         "shop": {"_identity": {"id": self.secondary + "@s.whatsapp.net"}}}
        write_json(self.root / "accounts.json", self.accounts)
        write_json(self.root / "setup.json", {"default_account": "main"})

    def service(self, *, unified=True):
        options = {"bot_runtime": self.runtime, "owner_number": self.owner, "bot_number": self.bot} if unified else {}
        service = Service(self.root, None, lambda: True, **options)
        self.addCleanup(service.ledger.close)
        self.addCleanup(service.bot.close)
        service.bridge.call = AsyncMock()
        if unified:
            service.rust.call = AsyncMock()
        return service

    async def test_full_control_uses_rust_only_for_the_exact_configured_bot(self):
        service = self.service()
        self.assertEqual(service.default_account(), "main")
        self.assertEqual(service.accounts["main"]["dm_mode"], "all")
        self.assertIn("9202@lid", service.identities("main"))
        self.assertIn(self.owner, service.bot_settings("main")["exclude_chats"])
        self.assertIn(self.bot, service.bot_settings("shop")["exclude_chats"])

        service.rust.call.return_value = [{"id": "10010@g.us"}]
        service.bridge.call.return_value = [{"id": "10011@g.us"}]
        groups = await service.owner("active-turn", "group_list", {"accounts": ["*"]})
        self.assertEqual(groups, {"main": [{"id": "10010@g.us"}], "shop": [{"id": "10011@g.us"}]})
        service.rust.call.assert_awaited_once_with("group_list", {})
        service.bridge.call.assert_awaited_once_with("group_list", "shop", {})

    async def test_existing_receipt_recovery_reads_old_ack_without_a_second_bot_connection(self):
        service = self.service()
        old = {"status": "accepted", "message_id": "old-ack"}
        service.bridge.call.return_value = old
        self.assertEqual(await service.transport("send_result", "main", {"message_id": "old-ack"}), old)
        service.rust.call.assert_not_called()
        service.bridge.call.assert_awaited_once_with("send_result", "main", {"message_id": "old-ack"})

        service = self.service(unified=False)
        with self.assertRaises(BridgeError) as error:
            await service.transport("connect", "main")
        self.assertEqual(error.exception.code, "unavailable")
        service.bridge.call.assert_not_called()

    async def test_unlink_targets_the_same_runtime_and_keeps_other_accounts(self):
        service = self.service()
        service.rust.call.return_value = {"unlink_requested": True, "remote_unlink_confirmed": False}
        result = await service.unlink("main")
        self.assertTrue(result["unlink_requested"])
        self.assertFalse(result["remote_unlinked"])
        self.assertEqual(read_json(self.runtime / "unlink-request.json"),
                         {"bot_number": self.bot, "remote_unlinked": False})
        self.assertEqual(read_json(self.root / "accounts.json"), {"shop": self.accounts["shop"]})
        service.rust.call.assert_awaited_once_with("logout", {})
        self.assertEqual([call.args for call in service.bridge.call.await_args_list],
                         [("disconnect", "main"), ("remove_account", "main")])
        with self.assertRaisesRegex(RuntimeError, "previous WhatsApp bot connection"):
            self.service()

    async def test_rust_send_preserves_attachment_digest_and_receipt_and_explicit_status_audience(self):
        service = self.service()
        content = b"synthetic movie"
        media = self.root / "video.mp4"
        media.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        identifier = "3EB0000000000000000011"
        with patch.object(rust_bridge, "send_message", new_callable=AsyncMock) as send:
            send.return_value = {"status": "accepted", "message_id": identifier}
            args = {"type": "video", "path": str(media), "caption": "test", "expected_sha256": digest,
                    "audience": [self.secondary + "@s.whatsapp.net"], "message_id": identifier}
            result = await service.rust.send("post_status", args)
            self.assertEqual(result, {"status": "accepted", "message_id": identifier})
            send.assert_awaited_once_with(ANY, self.runtime, self.bot,
                                         {"kind": "video", "chat_id": "status@broadcast", "text": "test",
                                          "filename": "video.mp4", "mimetype": "video/mp4", "expected_sha256": digest,
                                          "audience": args["audience"]}, identifier, media)

    def test_old_enabled_plugin_name_cannot_activate_this_installation(self):
        config = {"plugins": {"enabled": ["hermes-whatsapp-control"]}}
        with patch.dict(sys.modules, {"hermes_cli.config": SimpleNamespace(load_config_readonly=lambda: config)}):
            ctx = SimpleNamespace(plugin_id="checkout-wa-control-live", manifest=SimpleNamespace(name="wa-control-live"))
            self.assertFalse(active_install(ROOT / "__init__.py", activation_ids(ctx)))
            config["plugins"]["enabled"].append(ctx.plugin_id)
            self.assertTrue(active_install(ROOT / "__init__.py", activation_ids(ctx)))
            config["plugins"]["disabled"] = [ctx.manifest.name]
            self.assertFalse(active_install(ROOT / "__init__.py", activation_ids(ctx)))

    async def test_existing_chatbot_and_owner_controls_share_the_live_http_client(self):
        guest_chat = self.secondary + "@s.whatsapp.net"
        token = "synthetic-sidecar-token"
        (self.runtime / "adapter.token").write_text(token, encoding="utf-8")
        authorized = lambda request: request.headers.get("Authorization") == "Bearer " + token
        ready = asyncio.Event()
        sent = asyncio.Queue()
        requests = []

        async def health(request):
            if not authorized(request):
                raise web.HTTPUnauthorized()
            return web.json_response({"logged_in": True, "bot": self.bot, "owner": self.owner})

        async def inbox(request):
            if not authorized(request) or request.query.get("bot_number") != self.bot:
                raise web.HTTPUnauthorized()
            await ready.wait()
            messages = []
            if request.query.get("cursor") is None:
                messages = [{"event": "message", "chat": guest_chat, "id": "incoming-one", "live": True,
                             "sender": guest_chat, "text": "halo", "chat_type": "dm", "from_me": False}]
            else:
                await asyncio.sleep(0.01)
            return web.json_response({"messages": messages, "cursor": "fixture-cursor"})

        async def send(request):
            if not authorized(request):
                raise web.HTTPUnauthorized()
            payload = await request.json()
            if path := payload.get("path"):
                payload["staged_bytes"] = Path(path).read_bytes()
            sent.put_nowait(payload)
            if payload["kind"] == "voice":
                return web.json_response({"status": "rejected", "message_id": payload["message_id"],
                                          "rejection_status": 479}, status=422)
            return web.json_response({"status": "accepted", "message_id": payload["message_id"]})

        async def receipt(request):
            if not authorized(request) or request.query.get("bot_number") != self.bot:
                raise web.HTTPUnauthorized()
            return web.json_response({"status": "accepted", "message_id": request.match_info["identifier"]})

        async def control(request):
            if not authorized(request):
                raise web.HTTPUnauthorized()
            requests.append(await request.json())
            return web.json_response([{"id": "10010@g.us"}])

        app = web.Application()
        app.add_routes([web.get("/health", health), web.get("/control/messages", inbox),
                        web.post("/messages", send), web.get("/messages/{identifier}", receipt),
                        web.post("/control", control)])
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0]
        # This contact is not a managed account in this fixture.
        write_json(self.root / "accounts.json", {"main": self.accounts["main"]})
        llm = SimpleNamespace(acomplete_structured=AsyncMock(return_value=SimpleNamespace(
            parsed={"action": "reply", "text": "Ada yang bisa kubantu?"})))
        service = Service(self.root, llm, lambda: True, bot_runtime=self.runtime,
                          owner_number=self.owner, bot_number=self.bot)
        self.addCleanup(service.ledger.close)
        self.addCleanup(service.bot.close)
        self.addAsyncCleanup(service.rust.close)
        self.addCleanup(ready.set)
        with patch.object(rust_bridge, "SIDECAR", f"http://{host}:{port}"):
            await service.rust.start()
        await service.connect("main")
        self.assertEqual((await service.status())["accounts"]["main"]["status"], "connected")
        self.assertEqual(await service.account_read("main", "group_list", {}), [{"id": "10010@g.us"}])
        self.assertEqual(requests, [{"bot_number": self.bot, "op": "group_list", "args": {}}])
        ready.set()
        output = await asyncio.wait_for(sent.get(), 2)
        self.assertEqual(output, {"bot_number": self.bot, "chat_id": guest_chat, "kind": "text",
                                  "message_id": output["message_id"], "text": "Ada yang bisa kubantu?"})
        self.assertRegex(output["message_id"], r"^3EB0[A-F0-9]{18}$")
        self.assertEqual(await service.rust.call("send_result", {"message_id": output["message_id"]}),
                         {"status": "accepted", "message_id": output["message_id"]})

        voice = self.root / "synthetic.ogg"
        voice.write_bytes(b"test voice bytes")
        with self.assertRaises(BridgeError) as rejected:
            await service.rust.call("send_media", {"type": "voice", "message_id": "3EB0TESTVOICE000000001",
                                                    "chat": guest_chat, "path": str(voice)})
        self.assertTrue(rejected.exception.definitive_rejection)
        self.assertEqual(rejected.exception.status, 479)
        upload = await asyncio.wait_for(sent.get(), 2)
        self.assertEqual(upload["mimetype"], "audio/ogg; codecs=opus")
        self.assertEqual(upload["staged_bytes"], b"test voice bytes")
        self.assertFalse(Path(upload["path"]).exists())
