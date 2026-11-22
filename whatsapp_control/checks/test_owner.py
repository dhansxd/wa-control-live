import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "whatsapp_control_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
from whatsapp_control_test.service import Service, file_digest
from whatsapp_control_test.owner_request import OwnerRequests, verify_request
from whatsapp_control_test.runtime import BridgeError
from whatsapp_control_test.settings import write_json


class RegisteredToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_unified_owner_tool_uses_only_the_current_authenticated_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            session = {"PLATFORM": "whatsapp", "CHAT_TYPE": "dm", "CHAT_ID": OwnerTest.sender,
                       "USER_ID": OwnerTest.sender, "ACTOR_ID": OwnerTest.sender, "PROFILE": "default"}
            registered = {}
            converted = "Jadikan grup ini hanya admin yang boleh mengirim."
            proposal = {"operation_id": "frozen-op", "action": "group_settings", "accounts": {"bot": []}}
            rpc = AsyncMock(side_effect=[proposal, {"operation_id": "frozen-op"}, proposal])
            activation = Path(directory) / "installed-plugin" / "__init__.py"
            llm = AsyncMock()
            llm.acomplete_structured.return_value = SimpleNamespace(parsed={"allow": True})
            ctx = SimpleNamespace(
                profile_name="default", plugin_id="wa-control-live", llm=llm,
                register_tool=lambda **tool: registered.update(tool),
                register_hook=lambda name, handler: registered.update(hook=handler),
                register_middleware=lambda name, handler: registered.update(middleware=handler),
                register_cli_command=lambda **kwargs: None,
            )
            proof = None

            def resolve(session_id):
                return proof if session_id == "current-session" else None

            modules = {
                "gateway.session_context": SimpleNamespace(get_session_env=lambda key: session.get(key.removeprefix("HERMES_SESSION_"), "")),
                "hermes_constants": SimpleNamespace(get_hermes_home=lambda: home),
            }
            with patch.dict(sys.modules, modules), patch("whatsapp_control_test.runtime.request_worker", rpc), \
                    patch("whatsapp_control_test.runtime.active_install", return_value=True) as active:
                PLUGIN.register(ctx, entry=activation, authenticated_request=resolve,
                                cli_options={"owner_number": OwnerTest.owner}, toolset="wa-control-live")
                self.assertEqual(set(registered["schema"]["parameters"]["properties"]["action"]["enum"]), PLUGIN.OWNER_ACTIONS)
                self.assertEqual(registered["name"], "whatsapp_control")
                self.assertEqual(registered["toolset"], "wa-control-live")

                def record(turn):
                    registered["hook"](user_message="Unverified staged text", task_id="task", turn_id=turn,
                                       session_id="current-session", conversation_history=[
                                           {"role": "user", "content": "Native-looking row", "platform_message_id": "claimed"},
                                       ])

                async def dispatch(turn):
                    def next_call(payload):
                        return asyncio.create_task(registered["handler"](payload, task_id="task", session_id="current-session"))

                    return json.loads(await registered["middleware"]("whatsapp_control", {"action": "group_settings"},
                                                                      next_call, turn_id=turn))

                record("untrusted-viewer")
                self.assertIn("error", await dispatch("untrusted-viewer"))
                rpc.assert_not_awaited()

                proof = ("native-admission", "")
                record("voice-note")
                proof = ("different-admission", converted)
                self.assertIn("error", await dispatch("voice-note"))
                rpc.assert_not_awaited()

                # The platform transcript may be persisted after the pre-LLM hook.
                proof = ("native-admission", converted)
                self.assertEqual(await dispatch("voice-note"), {"operation_id": "frozen-op"})
                self.assertEqual(rpc.await_args.args[2]["turn"], "native-admission")
                checked = json.loads(llm.acomplete_structured.await_args.kwargs["input"][0]["text"])
                self.assertEqual(checked["current_owner_request"], converted)
                self.assertEqual(checked["earlier_owner_requests"], [])
                active.assert_called_with(activation, ctx.plugin_id)

                async def change_admission(**kwargs):
                    nonlocal proof
                    proof = ("new-admission", converted)
                    return SimpleNamespace(parsed={"allow": True})

                proof = ("signed-live", converted)
                record("live")
                llm.acomplete_structured.side_effect = change_admission
                self.assertIn("changed before execution", (await dispatch("live"))["error"])
                self.assertEqual([call.args[2]["action"] for call in rpc.await_args_list], ["prepare", "execute", "prepare"])

    async def test_only_the_current_host_turn_can_execute_an_authorized_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / "plugin-data" / "hermes-whatsapp-control"
            root.mkdir(parents=True)
            write_json(root / "setup.json", {"owner": OwnerTest.owner})
            session = {"PLATFORM": "whatsapp", "CHAT_TYPE": "dm", "CHAT_ID": OwnerTest.sender,
                       "USER_ID": OwnerTest.sender, "ACTOR_ID": OwnerTest.sender,
                       "PROFILE": "default", "MESSAGE_ID": "message-1"}
            registered = {}
            llm = AsyncMock()
            ctx = SimpleNamespace(
                profile_name="default", plugin_id="hermes-whatsapp-control", llm=llm,
                register_tool=lambda name, toolset, schema, handler, **kw: registered.update(tool=handler),
                register_hook=lambda name, handler: registered.update(hook=handler),
                register_middleware=lambda name, handler: registered.update(middleware=handler),
                register_cli_command=lambda **kwargs: None,
            )
            async def worker(root, verb, request):
                if request["action"] == "prepare":
                    return {"operation_id": "frozen-op", "action": "send_text", "accounts": {"shop": []}}
                if request["action"] == "operation_status":
                    return {"operation": None, "results": []}
                return {"operation_id": "frozen-op"}

            fake_rpc = AsyncMock(side_effect=worker)
            modules = {
                "gateway.session_context": SimpleNamespace(get_session_env=lambda key: session.get(key.removeprefix("HERMES_SESSION_"), "")),
                "hermes_constants": SimpleNamespace(get_hermes_home=lambda: home),
            }
            with patch.dict(sys.modules, modules), patch("whatsapp_control_test.runtime.request_worker", fake_rpc), \
                    patch("whatsapp_control_test.runtime.active_install", return_value=True):
                PLUGIN.register(ctx)
                hook, tool, middleware = (registered[key] for key in ("hook", "tool", "middleware"))
                args = {"action": "send_text", "text": "Synthetic"}

                async def dispatch(turn_id, task_id="task-1", action="send_text", **kwargs):
                    def next_call(payload):
                        return asyncio.create_task(tool(payload, task_id=task_id, **kwargs))

                    return json.loads(await middleware("whatsapp_control", {**args, "action": action}, next_call,
                                                       turn_id=turn_id))

                def record(turn_id, text, message_id=None, **metadata):
                    row = {"role": "user", "content": text, **metadata}
                    if message_id is not None:
                        row["platform_message_id"] = message_id
                    hook(user_message=text, task_id="task-1", turn_id=turn_id, conversation_history=[
                        {"role": "user", "content": "Prior DM", "platform_message_id": "prior-message"}, row,
                    ])

                llm.acomplete_structured.return_value = SimpleNamespace(parsed={"allow": True, "reason": "Sesuai"})
                # The stock gateway can reuse the opener's DM context for internal queued events.
                for turn, message_id, metadata in (
                    ("wake", "message-1", {"display_kind": "internal_notification"}),
                    ("context-only", None, {}),
                    ("wrong-tail", "message-1", {"role": "assistant"}),
                ):
                    with self.subTest(turn=turn):
                        record(turn, "Kirim pesan ini ke Toko", message_id, **metadata)
                        self.assertIn("error", await dispatch(turn, action="contacts"))
                        self.assertIn("error", await dispatch(turn))
                        fake_rpc.assert_not_awaited()

                record("turn-1", "Kirim pesan ini ke Toko", "message-1")
                await dispatch("turn-1", "wrong-task")
                await dispatch("unrecorded-turn")
                await dispatch(None)
                await dispatch("turn-1", turn_origin="plugin_fork:example")
                await tool(args, task_id="task-1", turn_id="turn-1")
                fake_rpc.assert_not_awaited()
                session["ACTOR_ID"] = "15550100199@s.whatsapp.net"
                await dispatch("turn-1", action="contacts")
                fake_rpc.assert_not_awaited()
                session["ACTOR_ID"] = OwnerTest.sender
                llm.acomplete_structured.return_value = SimpleNamespace(parsed={"allow": False, "reason": "Tujuan berbeda"})
                self.assertEqual((await dispatch("turn-1"))["error"], "Tujuan berbeda")
                self.assertEqual(fake_rpc.await_args.args[2]["action"], "prepare")
                llm.acomplete_structured.return_value = SimpleNamespace(parsed={"allow": True, "reason": "Sesuai"})
                self.assertEqual(await dispatch("turn-1"), {"operation_id": "frozen-op"})
                self.assertEqual(fake_rpc.await_args.args[2], {"turn": "turn-1", "action": "execute",
                                                              "args": {"operation_id": "frozen-op", "retry": False}})

                async def record_queued_owner_request(**kwargs):
                    record("turn-2", '"Kirim ulang"', "message-2")
                    return SimpleNamespace(parsed={"allow": True})

                # A queued turn retains the previous transport message ID and task ID.
                fake_rpc.reset_mock()
                llm.acomplete_structured.side_effect = record_queued_owner_request
                self.assertIn("changed before execution", (await dispatch("turn-1"))["error"])
                self.assertEqual([call.args[2]["action"] for call in fake_rpc.await_args_list], ["prepare"])
                fake_rpc.reset_mock()
                await dispatch("turn-1")
                fake_rpc.assert_not_awaited()
                llm.acomplete_structured.side_effect = None
                self.assertEqual(await dispatch("turn-2"), {"operation_id": "frozen-op"})
                self.assertEqual(fake_rpc.await_args.args[2]["turn"], "turn-2")
                evidence = json.loads(llm.acomplete_structured.await_args.kwargs["input"][0]["text"])
                self.assertEqual(evidence["current_owner_request"], '"Kirim ulang"')
                self.assertEqual(await dispatch("turn-2", action="contacts"), {"operation_id": "frozen-op"})
                self.assertEqual(fake_rpc.await_args.args[2]["turn"], "turn-2")

                record("turn-3", "", "photo-3")
                self.assertEqual(await dispatch("turn-3", action="contacts"), {"operation_id": "frozen-op"})
                fake_rpc.reset_mock()
                self.assertIn("error", await dispatch("turn-3"))
                fake_rpc.assert_not_awaited()

                async def record_internal_turn(**kwargs):
                    record("later-internal", "Kirim ulang", display_kind="internal_notification")
                    return SimpleNamespace(parsed={"allow": True})

                record("turn-4", "Kirim ulang", "message-4")
                llm.acomplete_structured.side_effect = record_internal_turn
                self.assertIn("changed before execution", (await dispatch("turn-4"))["error"])
                self.assertEqual([call.args[2]["action"] for call in fake_rpc.await_args_list], ["prepare"])

                record("turn-5", "Coba ulang operasi tadi", "message-5")
                llm.reset_mock()
                fake_rpc.reset_mock()
                error = await dispatch("turn-5", action="retry_operation")
                self.assertIn("Only receipt status can be checked", error["error"])
                self.assertEqual([call.args[2]["action"] for call in fake_rpc.await_args_list], ["operation_status"])
                llm.acomplete_structured.assert_not_awaited()


class OwnerTest(unittest.TestCase):
    owner = "15550100100"
    sender = owner + "@s.whatsapp.net"

    def test_attachment_changed_after_path_validation_is_rejected_before_hashing(self):
        with tempfile.TemporaryDirectory() as directory:
            attachment = Path(directory) / "selected.pdf"
            outside = Path(directory) / "other.pdf"
            outside.write_bytes(b"fixture")
            attachment.symlink_to(outside)
            with self.assertRaises(OSError), patch("hashlib.file_digest") as digest:
                file_digest(attachment)
            digest.assert_not_called()
            attachment.unlink()
            os.mkfifo(attachment)
            with self.assertRaisesRegex(ValueError, "regular attachment"), patch("hashlib.file_digest") as digest:
                file_digest(attachment)
            digest.assert_not_called()

    def check(self, changes=None, *, fixed_chat=None):
        values = {
            "PLATFORM": "whatsapp", "CHAT_TYPE": "dm", "CHAT_ID": self.sender,
            "USER_ID": self.sender, "ACTOR_ID": self.sender,
            "PROFILE": "default", "MESSAGE_ID": "test-message",
        }
        values.update(changes or {})
        return PLUGIN.owner_turn({"owner": self.owner, "owner_chat": fixed_chat},
                                 lambda key: values.get(key.removeprefix("HERMES_SESSION_"), ""),
                                 "default")

    def test_live_owner_dm_and_lid_route(self):
        self.assertTrue(self.check())
        lid = "20404100@lid"
        self.assertTrue(self.check({"CHAT_ID": lid, "PROFILE": ""}, fixed_chat=lid))
        self.assertFalse(self.check({"CHAT_ID": lid}, fixed_chat=self.sender))
        lid_only = {"CHAT_ID": lid, "USER_ID": lid, "ACTOR_ID": lid}
        self.assertFalse(self.check(lid_only))
        self.assertTrue(self.check(lid_only, fixed_chat=lid))
        self.assertFalse(self.check({**lid_only, "USER_ID_ALT": "15550100199"}, fixed_chat=lid))

    def test_missing_untrusted_or_wrong_origin(self):
        for changes in (
            {"ACTOR_ID": ""}, {"ACTOR_ID": "15550100199@s.whatsapp.net"},
            {"PLATFORM": "api_server"}, {"CHAT_TYPE": "group"},
            {"PROFILE": "other"},
            {"CHAT_ID": "15550100199@s.whatsapp.net"}, {"CHAT_ID": ""},
            {"CHAT_ID": None}, {"USER_ID": "15550100199@s.whatsapp.net"},
            {"USER_ID_ALT": "15550100199@s.whatsapp.net", "ACTOR_ID": "15550100199@s.whatsapp.net"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.check(changes))
        with patch.dict(os.environ, {"HERMES_SESSION_ACTOR_ID": self.sender}):
            self.assertFalse(self.check())

    def test_attachment_uses_hermes_policy_and_fails_when_it_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            attachment = path / "file.pdf"
            attachment.write_bytes(b"synthetic")
            root = path / "plugin-data"
            root.mkdir()
            secret = root / "data.json"
            secret.write_text("{}")
            unified = path / "unified"
            unified.mkdir()
            token = unified / "adapter.token"
            token.write_text("synthetic")
            modules = {"agent.runtime_cwd": SimpleNamespace(resolve_agent_cwd=lambda: path)}
            checked = []

            def policy(selected):
                checked.append(selected)
                if selected == str(attachment.resolve()):
                    raise ValueError("Hermes blocked this attachment")

            with patch.dict(sys.modules, {**modules, "agent.file_safety": SimpleNamespace(raise_if_read_blocked=policy)}):
                with self.assertRaisesRegex(ValueError, "Hermes blocked"):
                    PLUGIN.attachment_path("file.pdf", root)
                self.assertEqual(checked, [str(attachment.resolve())])
                with self.assertRaisesRegex(ValueError, "private data"):
                    PLUGIN.attachment_path(str(secret), root)
                with self.assertRaisesRegex(ValueError, "private data"):
                    PLUGIN.attachment_path(str(token), root, unified)
            with patch.dict(sys.modules, {**modules, "agent.file_safety": None}):
                with self.assertRaisesRegex(RuntimeError, "cannot check attachment"):
                    PLUGIN.attachment_path(str(attachment), root)


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_extra_account_qr_uses_only_the_configured_bot_identity(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        service = Service(root, None, lambda: True)
        self.addCleanup(service.ledger.close)
        self.addCleanup(service.bot.close)
        service.bridge.call = AsyncMock()
        await service.transport("pair", "personal")
        service.bridge.call.assert_awaited_once_with("pair", "personal", None)

        service.rust = SimpleNamespace(account="bot", bot="15550100101", call=AsyncMock())
        service.bridge.call.reset_mock()
        await service.transport("pair", "personal", {"reserved_bot_number": "15550100102"})
        service.bridge.call.assert_awaited_once_with("pair", "personal", {"reserved_bot_number": service.rust.bot})
        identity = {"phone": "15550100102@s.whatsapp.net"}
        await service.transport("repair", "personal", {"identity": identity})
        service.bridge.call.assert_awaited_with("repair", "personal", {"identity": identity,
                                                                     "reserved_bot_number": service.rust.bot})
        await service.transport("pair", "bot")
        service.rust.call.assert_awaited_once_with("pair", {})

    async def test_status_reports_only_sanitized_chatbot_model_outage(self):
        with tempfile.TemporaryDirectory() as directory:
            llm = SimpleNamespace(acomplete_structured=AsyncMock())
            service = Service(Path(directory), llm, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {"dm_mode": "all"}, "personal": {"dm_mode": "all"}}
            service.transport = AsyncMock()
            error = RuntimeError("private provider detail")
            error.status_code = 401
            llm.acomplete_structured.side_effect = error

            await service.bot.handle_event({
                "event": "message", "account": "shop", "chat": "62811@s.whatsapp.net",
                "chat_type": "dm", "id": "fixture", "sender": "62811@s.whatsapp.net",
                "text": "Halo", "live": True, "from_me": False,
            })

            status = await service.status()
            self.assertEqual(status["accounts"]["shop"], {
                "status": "offline", "identity": None,
                "chatbot": {"status": "unavailable", "reason": "auth_required"},
            })
            self.assertEqual(status["accounts"]["personal"], {"status": "offline", "identity": None})
            self.assertNotIn("private provider detail", json.dumps(status))
            service.transport.assert_awaited_once()

    async def test_purge_reports_each_unlink_after_quiescing_and_retains_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Service(root, None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"offline": {}, "personal": {}, "work": {"_transport": "rust"}}
            runtime = root / "unified"
            runtime.mkdir()
            service.rust = SimpleNamespace(account="work", runtime=runtime)
            service.save_accounts()

            async def pending(started):
                started.set()
                await asyncio.Event().wait()

            def request(action, args=None):
                reader = asyncio.StreamReader()
                reader.feed_data(json.dumps({"action": action, "args": args or {}}).encode() + b"\n")
                frames = []
                writer = SimpleNamespace(write=lambda frame: frames.append(json.loads(frame)),
                                         drain=AsyncMock(), close=lambda: None)
                return asyncio.create_task(service.request(reader, writer)), frames

            owner_started = asyncio.Event()
            service.owner = lambda *_: pending(owner_started)
            owner, _ = request("owner", {"turn": "fixture", "action": "status", "args": {}})
            await owner_started.wait()
            job_started, connection_started = asyncio.Event(), asyncio.Event()
            job = asyncio.create_task(pending(job_started))
            connecting = asyncio.create_task(pending(connection_started))
            service.jobs.add(job)
            service.connecting.add(connecting)
            await job_started.wait()
            await connection_started.wait()

            release, unlink_started = asyncio.Event(), asyncio.Event()
            attempts = []

            async def transport(action, account):
                self.assertEqual(action, "logout")
                self.assertTrue(owner.cancelled())
                self.assertTrue(job.cancelled())
                self.assertTrue(connecting.cancelled())
                self.assertFalse(service.is_active())
                if account == "work":
                    self.assertCountEqual(attempts, ["offline", "personal"])
                    self.assertEqual(json.loads((runtime / "purge-in-progress.json").read_text()),
                                     {"control_pid": os.getpid()})
                attempts.append(account)
                unlink_started.set()
                await release.wait()
                if account == "offline":
                    raise BridgeError("WhatsApp connection is offline", "offline")
                return {"unlink_requested": True,
                        "remote_unlink_confirmed": True if account == "work" else "unverified"}

            service.transport = transport
            purge, report = request("purge")
            await unlink_started.wait()
            stopping, rejected = request("stop")
            await stopping
            self.assertFalse(rejected[0]["ok"])
            self.assertFalse(service.stop.is_set())
            release.set()
            await purge

            self.assertCountEqual(attempts, service.accounts)
            self.assertTrue(service.stop.is_set())
            self.assertEqual(json.loads((root / "accounts.json").read_text()), service.accounts)
            self.assertEqual(report, [{"ok": True, "result": [
                {"account": "offline", "local_removed": False, "unlink_requested": False, "remote_unlinked": False},
                {"account": "personal", "local_removed": False, "unlink_requested": True, "remote_unlinked": False},
                {"account": "work", "local_removed": False, "unlink_requested": True, "remote_unlinked": True,
                 "runtime": "unified"},
            ]}])

    async def test_equivalent_same_turn_requests_share_one_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}}
            service.ready = {"shop": {"status": "connected"}}
            sends = []

            async def call(action, account, payload=None):
                if action == "contacts":
                    return [{"id": "15550100111@s.whatsapp.net", "saved": True}]
                sends.append((action, account, payload["chat"]))
                return {"message_id": payload["message_id"], "status": "accepted"}

            service.bridge.call = call
            args = {"action": "send_text", "accounts": ["shop"], "text": "Synthetic"}
            variants = [
                {**args, "chat": "15550100111"},
                {**args, "chats": ["15550100111@s.whatsapp.net", "+15550100111"],
                 "accounts": ["shop", "shop"], "audience": ["*"]},
            ]
            first, equivalent = [await service.prepare_operation("same-turn", req) for req in variants]
            self.assertEqual(first, equivalent)
            for plan in (first, equivalent):
                result = await service.execute_operation("same-turn", plan["operation_id"], retry=False)
                self.assertEqual(result["results"][0]["state"], "done")
            self.assertEqual(sends, [("send_text", "shop", "15550100111@s.whatsapp.net")])
            broad = await service.prepare_operation("same-turn", {**args, "chats": ["*"]})
            self.assertNotEqual(broad["operation_id"], first["operation_id"])
            self.assertTrue(broad["all_contacts"])
            self.assertFalse(first["all_contacts"])

    async def test_same_name_resolution_includes_other_contacts_and_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}}
            selected = "15550100101@s.whatsapp.net"
            alternate = "15550100102@s.whatsapp.net"
            alias = "4021@lid"
            group = "12000000000@g.us"
            other_group = "12000000001@g.us"

            async def call(action, account, payload=None):
                if action == "contacts":
                    return [{"id": selected, "lid": alias, "name": "Sample"},
                            {"id": alias, "phone": selected, "name": "Sample"},
                            {"id": alternate, "name": " sample "},
                            {"id": "15550100103@s.whatsapp.net", "name": "Unrelated"}]
                return [{"id": group, "subject": "Forum"}, {"id": other_group, "subject": "forum"}]

            service.bridge.call = call
            result = await service.prepare_operation("send-turn", {
                "action": "send_text", "chats": [selected, group], "text": "Synthetic",
            })
            self.assertEqual(result["lookup"]["shop"], {
                "targets": {selected: "Sample", group: "Forum"},
                "ambiguous_names": {selected: [alternate], group: [other_group]},
            })

    async def test_unconfirmed_dispatch_blocks_equivalent_new_owner_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}}
            service.ready = {"shop": {"status": "connected"}}
            sends = []

            async def call(action, account, payload=None):
                if action == "contacts":
                    return []
                if action == "send_result":
                    return {"message_id": payload["message_id"], "status": "unknown"}
                sends.append((action, account, payload))
                raise BridgeError("Synthetic transport timeout", "unknown")

            service.bridge.call = call
            args = {"action": "send_text", "chat": "15550100111", "text": "Synthetic"}
            first = await service.prepare_operation("owner-1", args)
            result = await service.execute_operation("owner-1", first["operation_id"], retry=False)
            self.assertEqual(result["results"][0]["state"], "unconfirmed")
            duplicate = await service.prepare_operation("owner-2", args)
            result = await service.execute_operation("owner-2", duplicate["operation_id"], retry=False)
            self.assertEqual(result["results"][0]["state"], "blocked")
            self.assertEqual(result["results"][0]["result"]["blocked_by_operation"], first["operation_id"])
            await service.execute_operation("owner-3", first["operation_id"], retry=True)
            self.assertEqual(len(sends), 1)

    async def test_status_voice_and_send_use_only_their_actual_recipient_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Service(root, None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}}
            contact = "15550100101@s.whatsapp.net"
            historical = "15550100102@s.whatsapp.net"
            service.bridge.call = AsyncMock(return_value=[
                {"id": contact, "name": "Sample", "saved": True},
                {"id": historical, "name": "History only", "saved": False},
                {"id": "15550100103@s.whatsapp.net", "name": "Unconfirmed"},
                "15550100104@s.whatsapp.net",
            ])
            text = {"action": "send_text", "chat": contact, "audience": ["*"], "text": "Synthetic"}
            result = await service.owner("text-turn", "prepare", text)
            self.assertFalse(result["all_contacts"])
            self.assertEqual(result["accounts"]["shop"][0]["chat"], contact)
            self.assertEqual(result["lookup"]["shop"]["targets"], {contact: "Sample"})
            explicit = await service.owner("historical-turn", "prepare", {**text, "chat": historical})
            self.assertEqual(explicit["lookup"]["shop"]["targets"], {historical: "History only"})
            with self.assertRaisesRegex(ValueError, "chats="):
                await service.owner("text-turn", "prepare", {**text, "chat": "*"})
            voice = root / "example.ogg"
            voice.write_bytes(b"synthetic service fixture")
            status = {"action": "post_status", "type": "voice", "path": str(voice), "audience": ["*"]}
            result = await service.owner("status-turn", "prepare", status)
            self.assertTrue(result["all_contacts"])
            self.assertEqual(result["counts"], {"shop": 1})
            self.assertEqual(result["accounts"]["shop"], [{"type": "voice", "path": str(voice)}])
            plan = service.get_plan(result["operation_id"])[2]["accounts"]["shop"][0]
            self.assertEqual(plan["audience"], [contact])
            self.assertEqual(len(plan["expected_sha256"]), 64)
            service.bridge.call.assert_awaited_with("contacts", "shop", {"saved_only": True})

            saved_id = "5021@lid"
            service.bridge.call = AsyncMock(return_value=[{"id": saved_id, "phone": historical, "saved": True}])
            result = await service.owner("saved-lid-turn", "prepare", status)
            plan = service.get_plan(result["operation_id"])[2]["accounts"]["shop"][0]
            self.assertEqual(plan["audience"], [saved_id])

    async def test_legacy_all_contact_plan_keeps_receipts_without_retrying_its_old_audience(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}}
            service.ready = {"shop": {"status": "connected"}}
            service.bridge.call = AsyncMock(side_effect=[
                [{"id": "15550100101@s.whatsapp.net", "saved": True}],
                BridgeError("Synthetic rejection", "rejected"),
            ])
            prepared = await service.prepare_operation("original", {
                "action": "send_text", "chats": ["*"], "text": "Synthetic",
            })
            identifier = prepared["operation_id"]
            result = await service.execute_operation("original", identifier, retry=False)
            self.assertEqual(result["results"][0]["state"], "rejected")
            _, _, plan = service.get_plan(identifier)
            del plan["saved_contacts_only"]
            with service.db:
                service.db.execute("UPDATE plans SET plan=? WHERE id=?", (json.dumps(plan), identifier))

            status = await service.operation_status(identifier)
            self.assertEqual(status["results"], result["results"])
            with self.assertRaisesRegex(ValueError, "older contact list"):
                await service.execute_operation("retry", identifier, retry=True)
            self.assertEqual(service.bridge.call.await_count, 2)

    async def test_unlink_preserves_other_accounts_receipts_and_retry_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = Service(root, None, lambda: True)
            self.addCleanup(service.db.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}, "personal": {}}
            service.ready = {label: {"status": "connected"} for label in service.accounts}
            first, second = "15550100101@s.whatsapp.net", "15550100102@s.whatsapp.net"
            sends = []
            settled = False

            async def bridge(action, account, payload=None):
                if action == "contacts":
                    return [{"id": first, "name": f"{account} contact"}]
                if action == "send_result":
                    self.assertEqual(account, "personal")
                    self.assertEqual(payload["message_id"], sends[2][2])
                    return {"message_id": payload["message_id"], "status": "accepted"}
                if action == "send_text":
                    sends.append((account, payload["chat"], payload["message_id"]))
                    if account == "personal" and not settled:
                        if payload["chat"] == first:
                            raise BridgeError("Synthetic timeout", "unknown")
                        raise BridgeError("Synthetic rejection", "rejected")
                    return {"message_id": payload["message_id"], "status": "accepted"}
                return {}

            service.bridge.call = bridge
            # The wildcard uses the stored account order, with shop first.
            plan = await service.prepare_operation("turn-1", {
                "action": "send_text", "accounts": ["*"], "chats": [first, second], "text": "Synthetic",
            })
            operation_id = plan["operation_id"]
            await service.execute_operation("turn-1", operation_id, retry=False)
            await service.unlink("shop")
            stored = service.get_plan(operation_id)[2]
            self.assertEqual(list(stored["accounts"]), ["personal"])
            self.assertEqual(list(stored["lookup"]), ["personal"])
            repeated = await service.prepare_operation("repeat-turn", {
                "action": "send_text", "accounts": ["personal"], "chat": first, "text": "Synthetic",
            })
            blocked = await service.execute_operation("repeat-turn", repeated["operation_id"], retry=False)
            self.assertEqual(blocked["results"][0]["result"]["blocked_by_operation"], operation_id)
            status = await service.operation_status(operation_id)
            self.assertTrue(status["operation"]["scope_changed"])
            self.assertEqual([result["state"] for result in status["results"]], ["done", "rejected"])
            service.accounts["shop"] = {"_identity": {"id": "15550100115@s.whatsapp.net"}}
            service.ready["shop"] = {"status": "connected"}
            self.assertEqual((await service.operation_status(operation_id))["operation"]["counts"], {"personal": 2})
            settled = True
            retried = await service.execute_operation("retry-turn", operation_id, retry=True)
            self.assertEqual([result["state"] for result in retried["results"]], ["done", "done"])
            self.assertEqual([(account, chat) for account, chat, _ in sends], [
                ("shop", first), ("shop", second), ("personal", first), ("personal", second), ("personal", second),
            ])

    async def test_unlink_prevents_recreating_an_old_approval_with_a_reused_label(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.db.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}}
            service.bridge.call = AsyncMock(return_value={})
            args = {"action": "send_text", "accounts": ["shop"], "chat": "15550100101", "text": "Synthetic"}
            prepared = await service.prepare_operation("turn-1", args)
            await service.unlink("shop")
            service.accounts["shop"] = {"_identity": {"id": "15550100116@s.whatsapp.net"}}
            service.ready["shop"] = {"status": "connected"}
            with self.assertRaisesRegex(ValueError, "unlinked"):
                await service.prepare_operation("turn-1", args)
            with self.assertRaisesRegex(ValueError, "unlinked"):
                await service.execute_operation("turn-1", prepared["operation_id"], retry=False)
            self.assertFalse(any(call.args[0] == "send_text" for call in service.bridge.call.await_args_list))

    async def test_prepare_detects_unlink_during_recipient_lookup_even_when_label_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.db.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}}
            looking_up, resume_lookup = asyncio.Event(), asyncio.Event()

            async def bridge(action, account, payload=None):
                if action == "contacts":
                    looking_up.set()
                    await resume_lookup.wait()
                    return []
                return {}

            service.bridge.call = bridge
            args = {"action": "send_text", "accounts": ["shop"], "chat": "15550100101", "text": "Synthetic"}
            preparing = asyncio.create_task(service.prepare_operation("turn-1", args))
            await asyncio.wait_for(looking_up.wait(), 3)
            try:
                await service.unlink("shop")
                service.accounts["shop"] = {"_identity": {"id": "15550100116@s.whatsapp.net"}}
            finally:
                resume_lookup.set()
            with self.assertRaisesRegex(ValueError, "unlinked"):
                await preparing
            self.assertEqual(service.db.execute("SELECT COUNT(*) FROM plans").fetchone(), (0,))

    async def test_existing_orphan_receipt_can_be_checked_but_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.db.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"personal": {}}
            service.ready = {"personal": {"status": "connected"}}
            sent_id = None

            async def bridge(action, account, payload=None):
                nonlocal sent_id
                if action == "send_text":
                    self.assertIsNone(sent_id)
                    sent_id = payload["message_id"]
                    raise BridgeError("Synthetic timeout", "unknown")
                if action == "send_result":
                    self.assertEqual((account, payload["message_id"]), ("personal", sent_id))
                    return {"message_id": sent_id, "status": "accepted"}
                return []

            service.bridge.call = bridge
            args = {"action": "send_text", "chat": "15550100101", "text": "Synthetic"}
            plan = await service.prepare_operation("turn-1", args)
            operation_id = plan["operation_id"]
            await service.execute_operation("turn-1", operation_id, retry=False)
            with service.db:
                service.db.execute("DELETE FROM plans WHERE id=?", (operation_id,))
            status = await service.operation_status(operation_id)
            self.assertIsNone(status["operation"])
            self.assertEqual(status["results"][0]["state"], "done")
            with self.assertRaisesRegex(ValueError, "original plan is unavailable"):
                await service.prepare_operation("turn-1", args)
            with self.assertRaisesRegex(ValueError, "was not found"):
                await service.execute_operation("retry", operation_id, retry=True)

    async def test_unlink_drains_chatbot_work_only_for_the_removed_account(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.db.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}, "personal": {}}
            active = {label: asyncio.Event() for label in service.accounts}
            completed = []
            old_jobs = {}

            async def handle(event):
                account = event["account"]
                old_jobs[account] = asyncio.current_task()
                active[account].set()
                try:
                    await asyncio.Future()
                finally:
                    completed.append(account)

            async def bridge(action, account, payload=None):
                if action == "logout":
                    self.assertEqual(completed, ["shop"])
                    service.on_event({"event": "message", "account": "shop"})
                return {}

            service.bot.handle_event = handle
            service.bridge.call = bridge
            for account in active:
                service.on_event({"event": "message", "account": account})
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in active.values())), 3)
                await service.unlink("shop")
                await asyncio.sleep(0)
                self.assertEqual(service.jobs, {old_jobs["personal"]})
                self.assertTrue(old_jobs["shop"].cancelled())
            finally:
                tasks = list(service.jobs)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_multi_account_retry_keeps_scope_and_does_not_resend_success(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.db.close)
            self.addCleanup(service.bot.close)
            calls = []
            contacts = ["15550100101@s.whatsapp.net", "15550100102@s.whatsapp.net"]
            reject = True

            async def call(op, account, args=None):
                calls.append((op, account, args))
                if op == "contacts":
                    return [{"id": chat, "saved": True} for chat in contacts]
                if op == "logout":
                    return {"unlink_requested": True, "remote_unlink_confirmed": False}
                if op == "send_text":
                    if reject and account == "second" and args["chat"] == contacts[0]:
                        raise BridgeError("Denied", "rejected", 403)
                    return {"message_id": args["message_id"], "status": "accepted"}
                return {}

            service.bridge.call = call
            service.accounts = {"first": {}, "second": {}}
            service.ready = {name: {"status": "connected"} for name in service.accounts}
            args = {"action": "send_text", "accounts": ["*"], "chats": ["*"], "text": "Synthetic message"}
            plan = await service.owner("turn-1", "prepare", args)
            self.assertEqual(plan["counts"], {"first": 2, "second": 2})
            self.assertFalse(any(op == "send_text" for op, _, _ in calls))
            operation = {"operation_id": plan["operation_id"]}
            with self.assertRaises(ValueError):
                await service.owner("other-turn", "execute", operation)
            with self.assertRaises(ValueError):
                await service.owner("other-turn", "execute", {**operation, "retry": True})
            first = await service.owner("turn-1", "execute", operation)
            self.assertEqual([row["state"] for row in first["results"]], ["done", "done", "rejected", "done"])
            self.assertEqual(len([op for op, _, _ in calls if op == "send_text"]), 4)
            await service.owner("turn-1", "execute", operation)
            self.assertEqual(len([op for op, _, _ in calls if op == "send_text"]), 4)
            contacts.append("15550100103@s.whatsapp.net")
            repeat = await service.prepare_operation("turn-1", {**args, "limit": 10})
            self.assertEqual(repeat, plan)
            reject = False
            retry = await service.owner("turn-2", "execute", {**operation, "retry": True})
            self.assertEqual([row["state"] for row in retry["results"]], ["done"] * 4)
            self.assertEqual(len([op for op, _, _ in calls if op == "send_text"]), 5)
            self.assertEqual(calls[-1][1], "second")
            self.assertEqual(calls[-1][2]["chat"], contacts[0])
            result = await service.unlink("first")
            self.assertTrue(result["unlink_requested"])
            self.assertFalse(result["remote_unlinked"])
            self.assertEqual(list(service.accounts), ["second"])
            self.assertEqual(service.db.execute("SELECT DISTINCT account FROM operations").fetchall(), [("second",)])

    async def test_all_contacts_continue_for_eligible_accounts_and_keep_approved_scope(self):
        requests = [
            {"action": "send_text", "chats": ["*"], "text": "Synthetic message"},
            {"action": "post_status", "audience": ["*"], "type": "text", "text": "Synthetic status"},
        ]
        for args in requests:
            with self.subTest(action=args["action"]), tempfile.TemporaryDirectory() as directory:
                service = Service(Path(directory), None, lambda: True)
                self.addCleanup(service.ledger.close)
                self.addCleanup(service.bot.close)
                service.accounts = {"offline": {}, "ready": {}, "empty": {}}
                service.ready = {"ready": {"status": "connected"}, "empty": {"status": "connected"}}
                contact = "15550100101@s.whatsapp.net"
                recovered = False
                dispatched = []

                async def call(action, account, payload=None):
                    if action == "contacts":
                        self.assertEqual(payload, {"saved_only": True})
                        if account == "offline" and not recovered:
                            raise BridgeError("Private transport detail", "offline")
                        if account == "empty" and not recovered:
                            return [{"id": contact, "saved": False}]
                        return [{"id": contact, "saved": True}]
                    if action == args["action"]:
                        dispatched.append((account, payload.get("audience") or [payload["chat"]]))
                        return {"message_id": payload["message_id"], "status": "accepted"}
                    self.assertIn(action, {"logout", "disconnect", "remove_account"})
                    return {}

                service.bridge.call = AsyncMock(side_effect=call)
                request = {**args, "accounts": ["*"]}
                preview = await service.prepare_operation("owner-turn", request)
                identifier = preview["operation_id"]
                self.assertEqual(preview["counts"], {"ready": 1, "offline": 0, "empty": 0})
                self.assertEqual(preview["lookup"]["offline"]["skipped"], "contacts_unavailable")
                self.assertEqual(preview["lookup"]["empty"]["skipped"], "no_synced_contacts")
                self.assertNotIn(contact, json.dumps(preview))
                self.assertEqual((await service.operation_status(identifier))["results"], [])

                result = await service.execute_operation("owner-turn", identifier, retry=False)
                rows = {row["account"]: row for row in result["results"]}
                self.assertEqual(rows["ready"]["state"], "done")
                for account, reason in (("offline", "contacts_unavailable"), ("empty", "no_synced_contacts")):
                    self.assertEqual(rows[account]["state"], "skipped")
                    self.assertIsNone(rows[account]["target"])
                    self.assertEqual(rows[account]["result"]["reason"], reason)
                    self.assertFalse(rows[account]["result"]["retry_safe"])
                self.assertNotIn("Private transport detail", json.dumps(result))

                recovered = True
                service.ready["offline"] = {"status": "connected"}
                self.assertEqual(await service.prepare_operation("owner-turn", request), preview)
                for turn, retry in [("owner-turn", False), ("retry-turn", True)]:
                    repeated = await service.execute_operation(turn, identifier, retry=retry)
                    self.assertEqual(repeated["results"], result["results"])
                self.assertEqual(dispatched, [("ready", [contact])])
                self.assertEqual(service.bridge.call.await_count, 4)

                await service.unlink("offline")
                status = await service.operation_status(identifier)
                self.assertNotIn("offline", status["operation"]["counts"])
                self.assertEqual([row["account"] for row in status["results"]], ["empty", "ready"])
                self.assertEqual(dispatched, [("ready", [contact])])

    async def test_all_contacts_with_no_eligible_account_reports_each_lookup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"offline": {}, "empty": {}}
            service.bridge.call = AsyncMock(side_effect=[BridgeError("Private detail", "offline"), []])
            with self.assertRaisesRegex(ValueError, "offline:.*empty:") as failure:
                await service.prepare_operation("owner-turn", {
                    "action": "send_text", "accounts": ["*"], "chats": ["*"], "text": "Synthetic",
                })
            self.assertNotIn("Private detail", str(failure.exception))
            self.assertEqual(service.db.execute("SELECT count(*) FROM plans").fetchone()[0], 0)
            self.assertEqual([call.args[:2] for call in service.bridge.call.await_args_list],
                             [("contacts", "offline"), ("contacts", "empty")])

    async def test_default_and_pause_operations_do_not_repeat_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "setup.json", {"owner": "15550100100"})
            service = Service(root, None, lambda: True)
            self.addCleanup(service.ledger.close)
            self.addCleanup(service.bot.close)
            service.accounts = {"shop": {}, "personal": {}}

            async def apply(args, turn):
                proposal = await service.owner(turn, "prepare", args)
                ref = {"operation_id": proposal["operation_id"]}
                await service.owner(turn, "execute", ref)
                return ref

            await apply({"action": "set_default", "accounts": ["shop"]}, "default")
            self.assertEqual(service.targets({}), ["shop"])
            chat = "15550100111@s.whatsapp.net"
            await apply({"action": "pause", "chat": chat, "seconds": 60}, "pause")
            first = (await service.owner("lookup", "pauses", {"chat": chat}))["shop"]["paused_until"]
            ref = await apply({"action": "extend_pause", "chat": chat, "seconds": 60}, "extend")
            await service.owner("retry", "execute", {**ref, "retry": True})
            self.assertEqual((await service.owner("lookup", "pauses", {}))["shop"][chat], first + 60)

    async def test_verified_hook_text_and_exact_prepared_targets(self):
        origin = OwnerRequests()
        origin.record("profile/chat", "task-1", "turn-1", "Tolong kirim brosur ke tim A", authenticated=True)
        origin.record("profile/chat", "task-1", "turn-2", "Pakai nomor toko ya", authenticated=True)
        origin.record("profile/chat", "task-1", "turn-1", "Do not reinstate an older request", authenticated=True)
        self.assertEqual(origin.get("profile/chat", "fake-task", "turn-2"), (None, []))
        self.assertEqual(origin.get("other-profile/chat", "task-1", "turn-2"), (None, []))
        self.assertEqual(origin.get("profile/chat", "task-1", "turn-1"), (None, []))
        current, previous = origin.get("profile/chat", "task-1", "turn-2")
        seen = []

        async def complete(**kwargs):
            seen.append(kwargs)
            return SimpleNamespace(parsed={"allow": False, "reason": "Tujuan berbeda"})

        proposal = {"action": "send_media", "accounts": {"shop": [{"chat": "15550100199@s.whatsapp.net"}]}}
        with self.assertRaisesRegex(ValueError, "Tujuan berbeda"):
            await verify_request(SimpleNamespace(acomplete_structured=complete), current, previous, proposal)
        payload = json.loads(seen[0]["input"][0]["text"])
        self.assertEqual(payload["current_owner_request"], "Pakai nomor toko ya")
        self.assertEqual(payload["earlier_owner_requests"], ["Tolong kirim brosur ke tim A"])
        self.assertEqual(payload["operation"], proposal)
        self.assertNotIn("tools", seen[0])
