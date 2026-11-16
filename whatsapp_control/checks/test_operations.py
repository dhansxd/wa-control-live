import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wa_operations_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
from wa_operations_test.operations import OperationLedger
from wa_operations_test.runtime import BridgeError
from wa_operations_test.service import Service
from wa_operations_test.settings import write_json


class OperationsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "operations.sqlite"

    def open(self):
        ledger = OperationLedger(self.db_path)
        self.addCleanup(ledger.close)
        return ledger

    async def test_retry_skips_accepted_and_retries_only_rejections(self):
        ledger = self.open()
        attempts = []
        first_dest, second_dest, third_dest = [f"1555010010{i}@s.whatsapp.net" for i in range(3)]

        async def send(action, account, payload):
            entry = (account, payload["chat"])
            attempts.append(entry)
            if payload["chat"] == second_dest and attempts.count(entry) == 1:
                raise BridgeError("Forbidden", "rejected")
            if payload["chat"] == third_dest:
                raise BridgeError("No acknowledgement", "unknown")
            return {"status": "accepted", "message_id": payload["message_id"]}

        async def submit(account, destination):
            return await ledger.run("send_text", account, {"chat": destination, "text": "Fixture"}, "batch", send)

        self.assertEqual((await submit("shop", first_dest))["status"], "accepted")
        self.assertEqual((await submit("shop", second_dest))["status"], "rejected")
        self.assertEqual((await submit("shop", third_dest))["status"], "unconfirmed")
        self.assertEqual((await submit("personal", first_dest))["status"], "accepted")
        self.assertEqual((await submit("shop", first_dest))["status"], "accepted")
        self.assertEqual((await submit("shop", second_dest))["status"], "accepted")
        self.assertEqual((await submit("shop", third_dest))["status"], "unconfirmed")
        self.assertEqual((await submit("personal", first_dest))["status"], "accepted")
        self.assertEqual(attempts, [("shop", first_dest), ("shop", second_dest), ("shop", third_dest),
                                    ("personal", first_dest), ("shop", second_dest)])

    async def test_unknown_send_survives_restart_and_reconciles_only_matching_ack(self):
        payload = {"chat": "15550100101@s.whatsapp.net", "text": "Fixture"}
        original = OperationLedger(self.db_path)
        messages = []

        async def connection_lost(action, account, request):
            messages.append(request["message_id"])
            raise ConnectionError("Lost response")

        try:
            self.assertEqual((await original.run("send_text", "shop", payload, "receipt",
                                                 connection_lost))["status"], "unconfirmed")
        finally:
            original.close()
        restarted = self.open()

        async def wrong_ack(action, account, request):
            self.assertEqual((action, account, request["message_id"]), ("send_result", "shop", messages[0]))
            return {"status": "accepted", "message_id": "different"}

        self.assertEqual((await restarted.reconcile("receipt", wrong_ack))[0]["state"], "unconfirmed")

        async def real_ack(action, account, request):
            return {"status": "accepted", "message_id": request["message_id"]}

        self.assertEqual((await restarted.reconcile("receipt", real_ack))[0]["state"], "done")
        self.assertEqual((await restarted.run("send_text", "shop", payload, "receipt",
                                              connection_lost))["message_id"], messages[0])
        self.assertEqual(len(messages), 1)

    async def test_requires_same_message_id_and_acceptance_for_completion(self):
        ledger = self.open()
        payload = {"chat": "15550100101@s.whatsapp.net", "text": "Fixture"}

        async def result_missing_status(action, account, request):
            return {"message_id": request["message_id"]}

        async def result_wrong_id(action, account, request):
            return {"status": "accepted", "message_id": "unrelated-receipt"}

        for key, send in (("missing", result_missing_status), ("wrong-id", result_wrong_id)):
            outcome = await ledger.run("send_text", "shop", {**payload, "text": key}, key, send)
            self.assertEqual(outcome["status"], "unconfirmed")

    async def test_offline_and_invalid_are_distinct(self):
        ledger = self.open()
        payload = {"chat": "15550100101@s.whatsapp.net", "text": "Fixture"}

        async def never_call(*args):
            self.fail("This operation must not be submitted to WhatsApp")

        offline = await ledger.run("send_text", "shop", payload, "initial", never_call, available=False)
        self.assertEqual((offline["status"], offline["retry_safe"]), ("unavailable", True))

        async def invalid(*args):
            raise BridgeError("Invalid payload", "invalid")

        outcome = await ledger.run("send_text", "shop", payload, "initial", invalid)
        self.assertEqual((outcome["status"], outcome["retry_safe"]), ("invalid", False))
        self.assertEqual(await ledger.run("send_text", "shop", payload, "initial", never_call), outcome)

    async def test_concurrent_retry_and_definitive_reconciliation(self):
        ledger = self.open()
        started, finished = asyncio.Event(), asyncio.Event()
        first_id = None
        payload = {"chat": "15550100101@s.whatsapp.net", "text": "Fixture"}

        async def transport(action, account, request):
            nonlocal first_id
            first_id = request["message_id"]
            started.set()
            await finished.wait()
            raise BridgeError("Timed out", "unknown")

        work = asyncio.create_task(ledger.run("send_text", "shop", payload, "concurrent", transport))
        await started.wait()
        try:
            self.assertEqual((await ledger.run("send_text", "shop", payload, "concurrent", transport))["status"],
                             "in_progress")
        finally:
            finished.set()
            await work

        async def rejected(action, account, request):
            return {"status": "rejected", "message_id": request["message_id"], "rejection_status": 403}

        refusal = (await ledger.reconcile("concurrent", rejected))[0]
        self.assertEqual((refusal["state"], refusal["result"]["rejection_status"]), ("rejected", 403))

        async def accepted(action, account, request):
            self.assertNotEqual(request["message_id"], first_id)
            return {"status": "accepted", "message_id": request["message_id"]}

        self.assertEqual((await ledger.run("send_text", "shop", payload, "concurrent", accepted))["status"],
                         "accepted")

    async def test_verified_aliases_block_pending_send_across_turns_until_exact_acceptance(self):
        ledger = self.open()
        started, release = asyncio.Event(), asyncio.Event()
        payload = {"chat": "15550100101@s.whatsapp.net", "text": "Fixture"}
        alias_payload = {**payload, "chat": "4021@lid"}
        sends = []

        async def send(action, account, request):
            sends.append(request)
            started.set()
            await release.wait()
            raise BridgeError("Lost response", "unknown")

        async def never_send(*args):
            self.fail("An unresolved send to a verified alias must not repeat")

        original = asyncio.create_task(ledger.run("send_text", "shop", alias_payload, "original", send,
                                                  turn="owner", effect_payload=payload))
        await started.wait()
        try:
            duplicate = await ledger.run("send_text", "shop", payload, "second", never_send, turn="owner")
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(duplicate["original_operation"], "original")
            self.assertEqual(duplicate["original_state"], "in_progress")
        finally:
            release.set()
            await original
        unresolved, = ledger.status("second")
        self.assertEqual(unresolved["result"]["original_state"], "unconfirmed")
        blocked = await ledger.run("send_text", "shop", payload, "third", never_send, turn="owner-2")
        self.assertEqual((blocked["status"], blocked["blocked_by_operation"]), ("blocked", "original"))

        async def wrong_ack(action, account, request):
            self.assertEqual((action, account), ("send_result", "shop"))
            self.assertEqual(request["message_id"], sends[0]["message_id"])
            return {"status": "accepted", "message_id": "different"}

        unresolved, = await ledger.reconcile("second", wrong_ack)
        self.assertEqual(unresolved["result"]["original_state"], "unconfirmed")

        async def exact_ack(action, account, request):
            self.assertEqual((action, account), ("send_result", "shop"))
            self.assertEqual(request["message_id"], sends[0]["message_id"])
            return {"status": "accepted", "message_id": request["message_id"]}

        result, = await ledger.reconcile("second", exact_ack)
        receipt, = ledger.status("original")
        self.assertEqual(result["state"], "duplicate")
        self.assertEqual(result["result"]["original_state"], "done")
        self.assertEqual(result["result"]["original_result"], receipt["result"])

        async def send_again(action, account, request):
            sends.append(request)
            return {"status": "accepted", "message_id": request["message_id"]}

        repeated = await ledger.run("send_text", "shop", payload, "fourth", send_again, turn="owner-3")
        self.assertEqual(repeated["status"], "accepted")
        self.assertEqual([request["chat"] for request in sends], [alias_payload["chat"], payload["chat"]])
        self.assertNotEqual(repeated["message_id"], receipt["result"]["message_id"])

    async def test_same_turn_default_and_explicit_recipients_share_send_receipt(self):
        root = self.db_path.parent
        write_json(root / "setup.json", {"default_account": "shop"})
        service = Service(root, None, lambda: True)
        self.addCleanup(service.ledger.close)
        self.addCleanup(service.bot.close)
        service.accounts = {"shop": {}, "personal": {}}
        service.ready = {account: {"status": "connected"} for account in service.accounts}
        phone, lid = "15550100101@s.whatsapp.net", "4021@lid"
        sends = []

        async def bridge(action, account, payload=None):
            if action == "contacts":
                return [{"id": lid, "phone": phone, "name": "Fixture"}] if account == "shop" else []
            sends.append((action, account, payload["chat"]))
            return {"status": "accepted", "message_id": payload["message_id"]}

        async def execute(turn, **selection):
            plan = await service.prepare_operation(turn, {"action": "send_text", "text": "Fixture", **selection})
            result = await service.execute_operation(turn, plan["operation_id"], retry=False)
            receipt, = result["results"]
            return plan, receipt

        service.bridge.call = bridge
        original, receipt = await execute("owner-1", chat=lid)
        self.assertTrue(original["uses_default"])
        self.assertEqual(receipt["state"], "done")
        explicit, duplicate = await execute("owner-1", accounts=["shop"], chat=phone)
        self.assertFalse(explicit["uses_default"])
        self.assertEqual(duplicate["state"], "duplicate")
        self.assertEqual(duplicate["result"]["original_operation"], original["operation_id"])
        self.assertEqual(duplicate["result"]["original_result"], receipt["result"])
        combined, duplicate = await execute("owner-1", accounts=["shop"], chats=[phone, lid])
        self.assertEqual(combined["counts"], {"shop": 1})
        self.assertEqual(duplicate["state"], "duplicate")
        _, other_account = await execute("owner-1", accounts=["personal"], chat=phone)
        self.assertEqual(other_account["state"], "done")
        _, later_turn = await execute("owner-2", chat=phone)
        self.assertEqual(later_turn["state"], "done")
        self.assertEqual(sends, [("send_text", "shop", lid), ("send_text", "personal", phone),
                                 ("send_text", "shop", phone)])

    async def test_forward_keeps_exact_source_identity_while_deduplicating_destination(self):
        service = Service(self.db_path.parent, None, lambda: True)
        self.addCleanup(service.ledger.close)
        self.addCleanup(service.bot.close)
        service.accounts = {"shop": {}}
        service.ready = {"shop": {"status": "connected"}}
        phone, lid = "15550100101@s.whatsapp.net", "4021@lid"
        calls = []

        async def bridge(action, account, payload=None):
            if action == "contacts":
                return [{"id": lid, "phone": phone}]
            calls.append((action, payload))
            if action == "recent_messages":
                return [{"id": "fixture-message", "type": "text"}] if payload["chat"] == lid else []
            return {"status": "accepted", "message_id": payload["message_id"]}

        service.bridge.call = bridge
        args = {"action": "forward_message", "source_chat": lid, "source_id": "fixture-message", "chat": phone}
        for plan_args in (args, {**args, "chat": lid}):
            plan = await service.prepare_operation("owner", plan_args)
            await service.execute_operation("owner", plan["operation_id"], retry=False)
        self.assertEqual([action for action, _ in calls], ["recent_messages", "forward_message", "recent_messages"])
        self.assertEqual(calls[0][1]["chat"], lid)
        self.assertEqual(calls[1][1]["source_chat"], lid)
        self.assertEqual(calls[1][1]["chat"], phone)
        with self.assertRaisesRegex(ValueError, "source message is unavailable"):
            await service.prepare_operation("owner", {**args, "source_chat": phone})

    async def test_status_audience_uses_verified_recipients_independent_of_alias_order(self):
        root = self.db_path.parent
        service = Service(root, None, lambda: True)
        self.addCleanup(service.ledger.close)
        self.addCleanup(service.bot.close)
        service.accounts = {"shop": {}}
        service.ready = {"shop": {"status": "connected"}}
        first, second, lid = "15550100101@s.whatsapp.net", "15550100102@s.whatsapp.net", "4021@lid"
        voice = root / "voice.ogg"
        voice.write_bytes(b"synthetic status audio fixture")
        stories = []

        async def bridge(action, account, payload=None):
            if action == "contacts":
                return [{"id": lid, "phone": first}]
            self.assertEqual(action, "post_status")
            stories.append(payload)
            return {"status": "accepted", "message_id": payload["message_id"]}

        service.bridge.call = bridge
        for kind, content in (("text", {"text": "Fixture"}), ("voice", {"path": str(voice)})):
            args = {"action": "post_status", "type": kind, **content}
            original = await service.prepare_operation("owner", {**args, "audience": [lid, second]})
            equivalent = await service.prepare_operation("owner", {**args, "audience": [second, first, lid]})
            self.assertEqual(original["counts"], {"shop": 2})
            self.assertEqual(equivalent["counts"], {"shop": 2})
            accepted = await service.execute_operation("owner", original["operation_id"], retry=False)
            duplicate = await service.execute_operation("owner", equivalent["operation_id"], retry=False)
            self.assertEqual(stories[-1]["audience"], [second, lid])
            self.assertEqual(duplicate["results"][0]["state"], "duplicate")
            self.assertEqual(duplicate["results"][0]["result"]["original_result"], accepted["results"][0]["result"])
        self.assertEqual([story["type"] for story in stories], ["text", "voice"])

    async def test_old_receipts_migrate_without_repeating_mutation(self):
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

        destination = "15550100101@s.whatsapp.net"
        payload = {"chat": destination, "text": "Fixture"}
        connection = sqlite3.connect(self.db_path)
        with connection:
            connection.execute("CREATE TABLE operations(key TEXT PRIMARY KEY, account TEXT, state TEXT, value TEXT)")
            for name, state in (("complete", "done"), ("not-sure", "pending")):
                connection.execute("INSERT INTO operations VALUES (?,?,?,?)", (
                    digest(["send_text", "shop", name, destination]), "shop", state,
                    json.dumps({"signature": digest(payload), "result": {"message_id": "legacy-receipt"}})))
        connection.close()

        async def never_call(*args):
            self.fail("Legacy receipt should block this bridge call")

        ledger = self.open()
        done = await ledger.run("send_text", "shop", payload, "complete", never_call)
        pending = await ledger.run("send_text", "shop", payload, "not-sure", never_call)
        self.assertEqual(done["message_id"], "legacy-receipt")
        self.assertEqual(pending["status"], "unconfirmed")
        self.assertEqual((await ledger.reconcile("not-sure", never_call))[0]["state"], "unconfirmed")
        with self.assertRaises(ValueError):
            await ledger.run("send_text", "shop", {**payload, "text": "Changed"}, "complete", never_call)
        with ledger.db:
            ledger.forget_account("shop")
        self.assertEqual(ledger.status("complete"), [])

    async def test_group_result_without_matching_participant_is_unconfirmed(self):
        ledger = self.open()
        group = "123456@g.us"
        participant = "15550100101@s.whatsapp.net"
        payload = {"chat": group, "action": "add", "participants": [participant]}

        async def mismatch(action, account, request):
            return [{"id": "12345@lid", "status": "200"}]

        async def forbidden(action, account, request):
            return [{"id": participant, "status": "403"}]

        unknown = await ledger.run("group_members", "shop", payload, "ambiguous", mismatch,
                                   target=f"{group}/{participant}")
        rejected = await ledger.run("group_members", "shop", {**payload, "action": "promote"}, "refused", forbidden,
                                    target=f"{group}/{participant}")
        self.assertEqual(unknown["status"], "unconfirmed")
        self.assertEqual(rejected["member_result"]["status"], "403")
        self.assertTrue(rejected["retry_safe"])

    async def test_group_state_only_reconciles_visible_matching_changes(self):
        ledger = self.open()
        group = "123456@g.us"
        phone = "15550100101@s.whatsapp.net"

        async def lost(*args):
            raise ConnectionError("Lost response")

        await ledger.run("group_members", "shop", {"chat": group, "action": "promote", "participants": [phone]},
                         "groups", lost, target=f"{group}/{phone}")
        await ledger.run("group_members", "shop", {"chat": group, "action": "remove", "participants": [phone]},
                         "groups", lost, target=f"{group}/{phone}/remove")
        await ledger.run("group_settings", "shop", {"chat": group, "subject": "Fixture"},
                         "groups", lost, target=f"{group}/subject")
        await ledger.run("group_create", "shop", {"subject": "Fixture"}, "groups", lost)

        async def wrong_group(action, account, request):
            return {"id": "654321@g.us", "subject": "Fixture"}

        self.assertTrue(all(row["state"] == "unconfirmed" for row in await ledger.reconcile("groups", wrong_group)))
        reads = []

        async def matching_group(action, account, request):
            reads.append((action, account, request))
            if action == "group_create_result":
                return {"request_id": request["request_id"], "status": "unknown"}
            return {"id": group, "subject": "Fixture", "participants": [
                {"id": "124356@lid", "phone": phone, "admin": "admin"}]}

        result = await ledger.reconcile("groups", matching_group)
        self.assertEqual([row["result"]["status"] for row in result],
                         ["unconfirmed", "satisfied", "unconfirmed", "satisfied"])
        self.assertEqual(len(reads), 2)
        self.assertIn(("group_metadata", "shop", {"chat": group}), reads)
        self.assertIn(("group_create_result", "shop"), [read[:2] for read in reads])

    async def test_group_creation_must_match_exact_receipt_across_restart(self):
        participants = [f"1555010010{i}@s.whatsapp.net" for i in range(2)]
        payload = {"subject": "Fixture", "participants": participants}
        original = OperationLedger(self.db_path)
        ids = []

        async def lost(action, account, request):
            self.assertEqual((action, account), ("group_create", "shop"))
            ids.append(request["request_id"])
            raise ConnectionError("No response")

        try:
            self.assertEqual((await original.run("group_create", "shop", payload, "first", lost))["status"], "unconfirmed")
        finally:
            original.close()
        restarted = self.open()

        async def wrong_group_receipt(action, account, request):
            self.assertEqual((action, account, request), ("group_create_result", "shop", {"request_id": ids[0]}))
            return {"request_id": "different", "status": "accepted", "group": {"id": "123456@g.us"}}

        self.assertEqual((await restarted.reconcile("first", wrong_group_receipt))[0]["state"], "unconfirmed")
        duplicate = await restarted.run("group_create", "shop", {**payload, "participants": participants[::-1]}, "second", lost)
        self.assertEqual((duplicate["status"], duplicate["blocked_by_operation"]), ("blocked", "first"))
        self.assertFalse(duplicate["retry_safe"])

        async def exact_receipt(action, account, request):
            return {"request_id": request["request_id"], "status": "accepted",
                    "group": {"id": "123456@g.us", "subject": "Fixture"}}

        result = (await restarted.reconcile("first", exact_receipt))[0]
        self.assertEqual((result["state"], result["result"]["id"]), ("done", "123456@g.us"))
        self.assertEqual((await restarted.run("group_create", "shop", payload, "first", lost))["id"], "123456@g.us")
        self.assertEqual(len(ids), 1)

        async def accepted(action, account, request):
            return {"request_id": request["request_id"], "status": "accepted", "group": {"id": "234567@g.us"}}

        self.assertEqual((await restarted.run("group_create", "shop", payload, "third", accepted))["id"], "234567@g.us")

    async def test_new_operation_matching_uncertain_send_references_original(self):
        ledger = self.open()
        payload = {"chat": "15550100101@s.whatsapp.net", "text": "Fixture"}

        async def lost(*args):
            raise ConnectionError("No response")

        async def should_not_send(*args):
            self.fail("A new operation must not repeat the unresolved write")

        await ledger.run("send_text", "shop", payload, "original", lost)
        result = await ledger.run("send_text", "shop", payload, "duplicate", should_not_send, turn="owner")
        self.assertEqual((result["status"], result["blocked_by_operation"]), ("blocked", "original"))
        self.assertEqual((await ledger.reconcile("duplicate", should_not_send))[0]["state"], "blocked")
        self.assertEqual(await ledger.run("send_text", "shop", payload, "duplicate", should_not_send), result)
