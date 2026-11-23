"""Native final attachments must keep their own sidecar receipt and Hermes turn."""
import asyncio
from contextlib import closing
import json

import pytest

pytest.importorskip("gateway.config")
from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session_ingress import deliver_response
from hermes_state import SessionDB

from whatsapp_gateway import WhatsAppLiveAdapter


OWNER, BOT = "15550100101", "15550100102"
CHAT = OWNER + "@s.whatsapp.net"
SESSION = "owner-session"
ADMISSION = "a" * 32


@pytest.mark.parametrize("native", [False, True], ids=["call", "dm"])
def test_attachments_follow_exact_admission_and_sidecar_ack(tmp_path, monkeypatch, native):
    from gateway.platforms import base

    home = tmp_path / "home"
    home.mkdir()
    runtime = home / "plugin-data" / "wa-control-live" / "unified"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path,))
    adapter = WhatsAppLiveAdapter(PlatformConfig(), OWNER, BOT, runtime / "adapter.token")
    adapter.verified = True
    event_id = "whatsapp-message-a" if native else ADMISSION
    source = adapter.owner_source(message_id=event_id)
    event = MessageEvent(text="Buatkan dua file.", source=source, message_id=event_id)
    if not native:
        event._claimed_owner = (SESSION, 1)
    route_key = adapter._event_session_key(event)
    principal = "messaging:" + json.dumps([None, "whatsapp", OWNER, None, OWNER], separators=(",", ":"))
    payload = ({"native_text_v1": {"source": source.to_dict(), "event": {"message_id": event_id},
                                   "route": route_key}} if native else {"text": event.text})
    with closing(SessionDB(home / "state.db")) as db:
        db.create_session(SESSION, "whatsapp", session_key=route_key, chat_type="dm", chat_id=OWNER, user_id=OWNER)
        db._conn.execute(
            """INSERT INTO session_admissions (admission_id,request_id,principal_id,target_session_id,
                   lineage_json,payload_json,payload_digest,intent,status,outcome,generation)
                   VALUES(?,?,?,?,'[]',?,'unused','queue','terminal','completed',1)""",
            (ADMISSION, event_id, principal if native else "local_operator", SESSION, json.dumps(payload)),
        )
        db._conn.commit()

    supplied = [tmp_path / "first.png", tmp_path / "second.pdf"]
    for path in supplied:
        path.write_bytes(b"identical content")
    output = "\n".join(f"MEDIA:{path}" for path in supplied)
    receipts = []

    async def send_message(http, directory, bot, body, message_id, source_file):
        assert directory == runtime and bot == BOT and body["chat_id"] == CHAT
        receipts.append({"message_id": message_id, "status": "accepted", "chat_id": CHAT})
        return {"message_id": message_id, "status": "accepted"}

    monkeypatch.setattr("whatsapp_gateway.send_message", send_message)
    adapter.http = object()
    asyncio.run(deliver_response(adapter, event, route_key, output))
    assert len(receipts) == 2
    from whatsapp_media_delivery import read_media_delivery

    def status(**change):
        fields = {"runtime": runtime, "bot": BOT, "home": home, "owner_number": OWNER,
                  "route_id": SESSION, "session_id": SESSION, "admission_id": ADMISSION, "output": output}
        return read_media_delivery(**{**fields, **change})

    folder = runtime / "outbound-receipts" / BOT
    folder.mkdir(parents=True, exist_ok=True)

    def ack(i, **change):
        row = {**receipts[i], **change}
        (folder / receipts[i]["message_id"]).write_text(json.dumps(row))

    assert status() == "unconfirmed"
    ack(0)
    assert status() == "partial"
    ack(1, chat_id="9999999999@s.whatsapp.net")
    assert status() == "partial"
    ack(1, message_id=receipts[0]["message_id"])
    assert status() == "partial"
    path = folder / receipts[1]["message_id"]
    path.unlink()
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text(json.dumps(receipts[1]))
    path.symlink_to(unrelated)
    assert status() == "partial"
    path.unlink()
    ack(1, audience=[CHAT])
    assert status() == "partial"
    ack(1, status="rejected")
    assert status() == "partial"
    ack(0, status="rejected")
    assert status() == "rejected"
    ack(0)
    ack(1)
    assert status() == "sent"
    assert status(session_id="different-session") == "unconfirmed"
    assert status(admission_id="b" * 32) == "unconfirmed"
    assert status(owner_number=BOT) == "unconfirmed"
    assert status(bot=OWNER) == "unconfirmed"
    assert status(output="MEDIA:" + str(tmp_path / "different.png")) == "unconfirmed"
    # Hermes drops the invalid path while extracting. Its sibling's success
    # cannot confirm the whole response when the second directive disappeared.
    broken = output + "\nMEDIA:" + str(tmp_path / "broken\x00.png")
    assert status(output=broken) != "sent"

    # A second real turn uses the exact same files. Its missing/rejected ACKs
    # must never inherit the accepted receipts of the first turn.
    next_admission, next_message = "b" * 32, "whatsapp-message-b" if native else "b" * 32
    second_event = MessageEvent(text=event.text, source=adapter.owner_source(message_id=next_message),
                                message_id=next_message)
    if not native:
        second_event._claimed_owner = (SESSION, 2)
    second_payload = ({"native_text_v1": {"source": source.to_dict(), "event": {"message_id": next_message},
                                          "route": route_key}} if native else payload)
    with closing(SessionDB(home / "state.db")) as db, db._conn:
        db._conn.execute(
            """INSERT INTO session_admissions (admission_id,request_id,principal_id,target_session_id,
                   lineage_json,payload_json,payload_digest,intent,status,outcome,generation)
                   VALUES(?,?,?,?,'[]',?,'unused','queue','terminal','completed',2)""",
            (next_admission, next_message, principal if native else "local_operator", SESSION, json.dumps(second_payload)),
        )
    asyncio.run(deliver_response(adapter, second_event, route_key, output))
    assert len(receipts) == 4
    assert status(admission_id=next_admission) == "unconfirmed"
    ack(2, status="rejected")
    ack(3, status="rejected")
    assert status(admission_id=next_admission) == "rejected"
    assert status() == "sent"
    # An explicit MEDIA tag remains attributable when the input is later
    # deleted. A bare path that Hermes can no longer identify fails closed.
    bare = f"MEDIA:{supplied[0]}\n{supplied[1]}"
    assert status(output=bare) == "sent"
    supplied[1].unlink()
    assert status(output=bare) == "unconfirmed"
    assert status() == "sent"


def test_streamed_final_receipt_requires_the_executing_task_and_its_final(tmp_path, monkeypatch):
    import os

    from gateway.config import GatewayConfig, Platform
    from gateway.platforms import base
    from gateway.run import GatewayRunner
    from gateway.session_context import set_session_vars, clear_session_vars
    from gateway.session_results import execution_result
    from whatsapp_media_delivery import read_media_delivery

    home = tmp_path / "profile"
    home.mkdir()
    runtime = home / "live"
    runtime.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (tmp_path,))
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", OWNER)
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / "sessions", multiplex_profiles=False))
    adapter = WhatsAppLiveAdapter(PlatformConfig(), OWNER, BOT, runtime / "adapter.token")
    adapter.gateway_runner = runner
    runner.adapters[Platform.WHATSAPP] = adapter
    adapter.verified, adapter.http = True, object()
    event = MessageEvent(text="Buat gambar.", source=adapter.owner_source(message_id=ADMISSION), message_id=ADMISSION)
    path = tmp_path / "result.png"
    path.write_bytes(b"image fixture")
    output = f"Hasil.\nMEDIA:{path}"
    result = {"final_response": output, "already_sent": True, "completed": True}
    receipts = runtime / "outbound-receipts" / BOT
    receipts.mkdir(parents=True)

    async def send_message(http, directory, bot, body, message_id, source_file):
        row = {"message_id": message_id, "chat_id": body["chat_id"], "status": "accepted"}
        (receipts / message_id).write_text(json.dumps(row))
        return row

    monkeypatch.setattr("whatsapp_gateway.send_message", send_message)

    async def exercise():
        from gateway.session_authority import initialize_session_authority
        from gateway.session_controls import AuthorityConnection

        entry = runner.session_store.get_or_create_session(event.source)
        authority = await initialize_session_authority(runner, profile_id=str(home), instance_id="stream-test")
        connection = AuthorityConnection(authority, object(), {
            "provider": "local", "user_id": f"uid:{os.getuid()}", "instance_id": authority.instance_id,
            "profile_id": str(home), "capabilities": ["session:read", "session:submit", "session:control"],
        }, operator=True)
        statuses = []

        async def run(real_event):
            # Hermes normally sets these per-turn values in _hmwa_prepare_turn
            # and TurnRunner.run_sync. Its outer authority is real in this test.
            sid, generation = real_event._claimed_owner
            job = real_event.message_id
            assert authority.sessions[sid].task is asyncio.current_task()

            def observed():
                return read_media_delivery(runtime, BOT, home, OWNER, sid, sid, job, output)

            tokens = set_session_vars(turn_event=real_event, session_id=sid)
            try:
                execution_result.get()["result"] = result
                async def deliver(actual=output):
                    return await runner._hmwa_deliver_turn_response(
                        real_event, real_event.source, entry, entry.session_key, generation,
                        result, [], actual, "", False)
                # This nested task has all the same ContextVars and sends the
                # same bytes. Only the original drain's send can count.
                await asyncio.create_task(deliver())
                statuses.append(observed())
                real_event.ledger_message_id = "later-queued-turn"
                await deliver()
                statuses.append(observed())
                real_event.ledger_message_id = None
                other = tmp_path / "other.png"
                other.write_bytes(path.read_bytes())
                await deliver(f"MEDIA:{other}")
                statuses.append(observed())
                with monkeypatch.context() as older_hermes:
                    older_hermes.delattr("gateway.session_context._SESSION_TURN_EVENT")
                    await deliver()
                statuses.append(observed())
                await deliver()
                statuses.append(observed())
            finally:
                clear_session_vars(tokens)
            return None

        monkeypatch.setattr(runner, "_handle_message", run)
        try:
            resumed = await connection.dispatch({"id": 1, "method": "session.resume",
                                                 "params": {"session_id": entry.session_id}})
            assert "error" not in resumed, resumed
            response = await connection.dispatch({"id": 2, "method": "prompt.submit", "params": {
                "session_id": entry.session_id, "text": "Buat gambar.", "submission_id": "2" * 32,
            }})
            assert "error" not in response, response
            drain = authority.sessions[entry.session_id].task
            if drain:
                await drain
            assert len(list(receipts.glob("3EB0*"))) == 5
            assert statuses == ["unconfirmed", "unconfirmed", "unconfirmed", "unconfirmed", "sent"]
        finally:
            await connection.close()
            runner.session_store.close_all_db_handles()
            runner.close_all_session_db_handles()

    asyncio.run(exercise())
