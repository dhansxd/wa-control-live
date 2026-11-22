"""Durable per-destination receipts for owner operations."""

import hashlib
import json
import re
import sqlite3
import uuid

from .runtime import BridgeError


SEND_ACTIONS = {"send_text", "send_media", "post_status", "forward_message"}
RETRYABLE = {"rejected", "unavailable"}
KNOWN_FAILURES = {"unavailable", "invalid", "rejected"}
GROUP_ID = re.compile(r"[0-9]+(?:-[0-9]+)?@g\.us\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _check_id(operation_id):
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 120:
        raise ValueError("Select a valid WhatsApp operation ID.")


def _failure(status, error=None):
    result = {"status": status, "retry_safe": status in RETRYABLE}
    if error:
        result["error"] = error
    return result


def _group_receipt(result, request_id):
    if not isinstance(result, dict) or result.get("request_id") != request_id:
        return None
    status = result.get("status")
    group = result.get("group")
    if status == "accepted" and isinstance(group, dict) and GROUP_ID.fullmatch(str(group.get("id", ""))):
        return "done", group
    if status == "rejected":
        return "rejected", {**result, **_failure("rejected", "WhatsApp declined group creation.")}
    return None


def _member_result(participant, changes):
    # The bridge returns the requested identity where it can verify PN/LID equivalence.
    if not isinstance(changes, list):
        return "pending", _failure("unconfirmed")
    matches = [member for member in changes if isinstance(member, dict) and member.get("id") == participant]
    if len(matches) != 1:
        return "pending", _failure("unconfirmed")
    result = matches[0]
    status = str(result.get("status"))
    if len(status) == 3 and status.isascii() and status.isdigit():
        if 200 <= int(status) < 300:
            return "done", result
        if 400 <= int(status) < 500 and status != "408":
            return "rejected", {**_failure("rejected"), "member_result": result}
    return "pending", _failure("unconfirmed")


def _group_check(action, payload):
    if action == "group_create":
        participants = sorted(set(payload.get("participants", [])))
        return {"create_signature": _digest({"subject": payload.get("subject"), "participants": participants})}
    if action == "group_members" and len(payload.get("participants", [])) == 1:
        return {"chat": payload["chat"], "participant": payload["participants"][0], "action": payload["action"]}
    if action == "group_settings":
        settings = {name: payload[name] for name in ("subject", "description", "announce", "restrict") if name in payload}
        if settings:
            return {"chat": payload["chat"], "settings": settings}
    return None


def _group_matches(group, check):
    if not isinstance(group, dict) or group.get("id") != check["chat"]:
        return False
    if settings := check.get("settings"):
        return all(key in group and group[key] == value for key, value in settings.items())
    participants = group.get("participants")
    if not isinstance(participants, list):
        return False
    matches = [member for member in participants if isinstance(member, dict)
               and check["participant"] in (member.get("id"), member.get("phone"), member.get("lid"))]
    if len(matches) != 1:
        # Missing membership can be caused by an unresolved PN/LID or incomplete response.
        return False
    action = check["action"]
    if action == "add":
        return True
    if action == "promote":
        return matches[0].get("admin") in {"admin", "superadmin"}
    return action == "demote" and "admin" in matches[0] and matches[0]["admin"] is None


class OperationLedger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self._active = set()
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS operations(
                key TEXT PRIMARY KEY, account TEXT, state TEXT, value TEXT)""")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(operations)")}
            for name in ("operation_id", "action", "target", "message_id", "turn", "effect"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE operations ADD COLUMN {name} TEXT")
            self.db.execute("CREATE INDEX IF NOT EXISTS operation_lookup ON operations(operation_id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS turn_effects ON operations(turn,effect)")
            self.db.execute("CREATE INDEX IF NOT EXISTS pending_effects ON operations(effect) WHERE state='pending'")
            self.db.execute("""CREATE INDEX IF NOT EXISTS unresolved_targets ON operations(account,action,target)
                WHERE state IN ('pending','partial')""")

    def close(self):
        self.db.close()

    def forget_account(self, account):
        self.db.execute("DELETE FROM operations WHERE account=?", (account,))

    def _record(self, key, signature, state, result, group_check):
        value = {"signature": signature, "result": result}
        if group_check and state in {"pending", "partial"}:
            value["group_check"] = group_check
        with self.db:
            self.db.execute("UPDATE operations SET state=?,value=? WHERE key=?",
                            (state, json.dumps(value), key))

    def _known(self, key, state, recorded):
        if state == "pending":
            return _failure("in_progress" if key in self._active else "unconfirmed")
        if state == "duplicate":
            result = recorded["result"]
            receipt_key = recorded["receipt_key"]
            prior = self.db.execute("SELECT state,value FROM operations WHERE key=?", (receipt_key,)).fetchone()
            if prior:
                prior_state, prior_value = prior
                receipt = self._known(receipt_key, prior_state, json.loads(prior_value))
                return {**result, "original_state": receipt["status"] if prior_state == "pending" else prior_state,
                        "original_result": receipt}
            return result
        return recorded["result"]

    def _unresolved_duplicate(self, key, account, action, target, signature, group_check, effect):
        if effect:
            row = self.db.execute("""SELECT operation_id FROM operations
                WHERE effect=? AND key<>? AND state='pending' LIMIT 1""", (effect, key)).fetchone()
            if row:
                return row[0] or "unknown"
        rows = self.db.execute("""SELECT operation_id,value FROM operations
            WHERE account=? AND action=? AND target IS ? AND key<>? AND state IN ('pending','partial')""",
                              (account, action, target, key))
        for operation_id, serialized in rows:
            recorded = json.loads(serialized)
            if (recorded["signature"] == signature or
                    (action == "group_create" and group_check == recorded.get("group_check"))):
                return operation_id or "unknown"
        return None

    async def run(self, action, account, payload, operation_id, call, *, available=True, target=None,
                  turn=None, effect_payload=None):
        _check_id(operation_id)
        chat = payload.get("chat")
        # Keep existing hashes stable for records from the initial release.
        target = chat if target is None else target
        key = _digest([action, account, operation_id, target])
        signature = _digest(payload)
        effect = None
        if turn and action in SEND_ACTIONS:
            effect = _digest([action, account, effect_payload or payload])
        row = self.db.execute("SELECT state,value,operation_id FROM operations WHERE key=?", (key,)).fetchone()
        if row:
            state, serialized, recorded_id = row
            recorded = json.loads(serialized)
            if recorded["signature"] != signature:
                raise ValueError("This operation ID already has different arguments for this destination.")
            if recorded_id is None:
                with self.db:
                    self.db.execute("UPDATE operations SET operation_id=?,action=?,target=? WHERE key=?",
                                    (operation_id, action, target, key))
            if state not in RETRYABLE:
                return self._known(key, state, recorded)
        elif target != chat:
            legacy_key = _digest([action, account, operation_id, chat])
            if self.db.execute("SELECT 1 FROM operations WHERE key=?", (legacy_key,)).fetchone():
                # A legacy batch might already have changed this target. Its receipt is
                # not granular enough to retry individual members or settings safely.
                return _failure("unconfirmed", "A previous aggregate operation must be checked in WhatsApp.")

        message_id = "3EB0" + uuid.uuid4().hex[:18].upper() if action in SEND_ACTIONS | {"group_create"} else None
        state = "pending" if available else "unavailable"
        result = None if available else _failure(state, "WhatsApp account is offline or being removed.")
        group_check = _group_check(action, payload)
        prior_turn_effect = self.db.execute("""SELECT key,operation_id FROM operations
            WHERE turn=? AND effect=? AND key<>? AND state IN ('pending','done')
            ORDER BY rowid LIMIT 1""", (turn, effect, key)).fetchone() if effect else None
        if prior_turn_effect:
            state = "duplicate"
            result = {**_failure(state), "original_operation": prior_turn_effect[1]}
        elif blocked_by := self._unresolved_duplicate(key, account, action, target, signature, group_check, effect):
            state = "blocked"
            result = {**_failure(state, "Check the previous unresolved WhatsApp operation first."),
                      "blocked_by_operation": blocked_by}
        value = {"signature": signature, "result": result}
        if prior_turn_effect:
            value["receipt_key"] = prior_turn_effect[0]
        if group_check and state == "pending":
            value["group_check"] = group_check
        with self.db:
            self.db.execute("""INSERT INTO operations(key,account,state,value,operation_id,action,target,message_id,turn,effect)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                state=excluded.state,value=excluded.value,message_id=excluded.message_id,
                turn=excluded.turn,effect=excluded.effect""",
                (key, account, state, json.dumps(value),
                 operation_id, action, target, message_id if state == "pending" else None, turn, effect))
        if state != "pending":
            return self._known(key, state, value)

        self._active.add(key)
        try:
            id_key = "request_id" if action == "group_create" else "message_id"
            outgoing = {**payload, id_key: message_id} if message_id else payload
            result = await call(action, account, outgoing)
            state = "done"
            if action == "group_create":
                state, result = _group_receipt(result, message_id) or ("pending", _failure("unconfirmed"))
            elif message_id and (not isinstance(result, dict) or result.get("message_id") != message_id
                                 or result.get("status") != "accepted"):
                state, result = "pending", _failure("unconfirmed")
            elif action == "group_members" and len(payload.get("participants", [])) == 1:
                state, result = _member_result(payload["participants"][0], result or [])
            elif action == "group_settings" and isinstance(result, dict) and result.get("incomplete"):
                state = "partial"
                result = {**result, **_failure(state)}
        except BridgeError as error:
            if error.code in {"offline", "unpaired"}:
                state = "unavailable"
            else:
                state = error.code if error.code in KNOWN_FAILURES else "pending"
            result = _failure("unconfirmed" if state == "pending" else state,
                              str(error) if state != "pending" else None)
            if state == "rejected" and getattr(error, "status", None) is not None:
                result["rejection_status"] = error.status
        except (RuntimeError, OSError):
            state, result = "pending", _failure("unconfirmed")
        finally:
            self._active.discard(key)
        self._record(key, signature, state, result, group_check)
        return result

    def status(self, operation_id):
        _check_id(operation_id)
        rows = self.db.execute("""SELECT key,account,action,target,state,value,message_id
            FROM operations WHERE operation_id=? ORDER BY account,action,target""", (operation_id,))
        results = []
        for key, account, action, target, state, value, message_id in rows:
            result = self._known(key, state, json.loads(value))
            if action == "group_create" and result.get("status") == "unconfirmed" and not message_id:
                result["error"] = "This older group creation has no request receipt. Check WhatsApp before attempting it again."
            results.append({"account": account, "action": action, "target": target,
                            "state": result["status"] if state == "pending" else state, "result": result})
        return results

    async def reconcile(self, operation_id, call):
        _check_id(operation_id)
        rows = self.db.execute("""SELECT key,account,action,state,value,message_id FROM operations
            WHERE state IN ('pending','partial') AND (operation_id=? OR key IN (
                SELECT json_extract(value, '$.receipt_key') FROM operations WHERE operation_id=? AND state='duplicate'
            ))""", (operation_id, operation_id)).fetchall()
        groups = {}
        for key, account, action, prior_state, value, message_id in rows:
            if key in self._active:
                continue
            recorded = json.loads(value)
            if action == "group_create" and message_id:
                try:
                    received = await call("group_create_result", account, {"request_id": message_id})
                except (RuntimeError, OSError):
                    continue
                receipt = _group_receipt(received, message_id)
                if receipt is None:
                    continue
                state, result = receipt
            elif message_id:
                try:
                    result = await call("send_result", account, {"message_id": message_id})
                except (RuntimeError, OSError):
                    continue
                if not isinstance(result, dict) or result.get("message_id") != message_id:
                    continue
                state = {"accepted": "done", "rejected": "rejected"}.get(result.get("status"))
                if state is None:
                    continue
                if state == "rejected":
                    result = {**result, **_failure(state, "WhatsApp declined the send.")}
            elif check := recorded.get("group_check"):
                if action == "group_create":
                    continue
                group_key = (account, check["chat"])
                if group_key not in groups:
                    try:
                        groups[group_key] = await call("group_metadata", account, {"chat": check["chat"]})
                    except (RuntimeError, OSError):
                        groups[group_key] = None
                if not _group_matches(groups[group_key], check):
                    continue
                state, result = "done", {"status": "satisfied", "source": "current_group_state"}
            else:
                continue
            serialized = json.dumps({"signature": recorded["signature"], "result": result})
            with self.db:
                self.db.execute("""UPDATE operations SET state=?,value=?
                    WHERE key=? AND state=? AND value=? AND message_id IS ?""",
                    (state, serialized, key, prior_state, value, message_id))
        return self.status(operation_id)
