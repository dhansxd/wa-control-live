"""Lifecycle and account operations for the profile running the plugin."""

import asyncio
import contextlib
import fcntl
import hashlib
import json
import mimetypes
import os
import signal
import stat
import sys
from pathlib import Path

from .chatbot import Chatbot
from .operations import OperationLedger
from .runtime import Bridge, BridgeError, MAX_FRAME, encode_frame, socket_dir
from .settings import JID, account_id, phone_number, policy_patch, private_dir, read_json, write_json


SEND = {"send_text", "send_media", "forward_message"}
MUTATIONS = SEND | {"post_status", "group_create", "group_members", "group_settings"}
READS = {"group_metadata", "group_list", "contacts", "recent_messages", "pauses", "policy"}
SETTINGS = {"configure", "set_default", "pause", "extend_pause", "resume"}
CONTACT_SKIP_REASONS = {
    "contacts_unavailable": "Saved contacts could not be read.",
    "no_synced_contacts": "No synced contacts are available.",
}


def whatsapp_id(raw, *, groups=True):
    if number := phone_number(raw):
        return number + "@s.whatsapp.net"
    if not isinstance(raw, str) or not JID.fullmatch(raw) or (not groups and raw.endswith("@g.us")):
        raise ValueError("Use a WhatsApp chat ID or an international phone number.")
    return raw


def file_digest(path):
    # Recheck at the point of reading, after the gateway has validated the path.
    with open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Select a regular attachment file.")
        return hashlib.file_digest(handle, "sha256").hexdigest()


def target_selection(action, args):
    if action == "post_status":
        values = args.get("audience")
    elif action in SEND:
        values = args.get("chats", [args["chat"]] if args.get("chat") else None)
        if "chats" not in args and values == ["*"]:
            raise ValueError("Select all synced contacts explicitly with chats=['*'].")
    else:
        return None
    if not isinstance(values, list) or not values:
        raise ValueError("Select at least one destination.")
    if values == ["*"]:
        return values
    return sorted({whatsapp_id(value, groups=action != "post_status") for value in values})


def named_targets(targets, entries):
    by_name, selected = {}, {}
    for ids, label in entries:
        if not isinstance(label, str) or not label.strip():
            continue
        aliases = {identifier for identifier in ids if isinstance(identifier, str) and identifier}
        if not aliases:
            continue
        name = " ".join(label.casefold().split())
        matches = by_name.setdefault(name, [])
        separate = []
        for candidate in matches:
            if candidate & aliases:
                aliases.update(candidate)
            else:
                separate.append(candidate)
        by_name[name] = [*separate, aliases]
        for identifier in targets & aliases:
            selected.setdefault(identifier, (label, name))
    labels = {identifier: label for identifier, (label, _) in selected.items()}
    ambiguous = {identifier: sorted(min(other) for other in by_name[name] if identifier not in other)[:3]
                 for identifier, (_, name) in selected.items()}
    return labels, {identifier: matches for identifier, matches in ambiguous.items() if matches}


def recipient_aliases(records):
    candidates = {}
    for contact in records:
        if not isinstance(contact, dict):
            continue
        ids = {contact.get(field) for field in ("id", "phone", "lid")}
        phones = {item for item in ids if isinstance(item, str) and item.endswith("@s.whatsapp.net")
                  and JID.fullmatch(item)}
        lids = {item for item in ids if isinstance(item, str) and item.endswith("@lid") and JID.fullmatch(item)}
        for lid in lids:
            candidates.setdefault(lid, set()).update(phones)
    return {lid: next(iter(phones)) for lid, phones in candidates.items() if len(phones) == 1}


def unique_recipients(recipients, aliases):
    selected = {}
    for jid in recipients:
        selected.setdefault(aliases.get(jid, jid), jid)
    return selected


def job_target(action, payload):
    chat = payload.get("chat")
    if action == "group_members":
        return f"{chat}/{payload['participants'][0]}"
    if action == "group_settings":
        field = next(key for key in payload if key != "chat")
        return f"{chat}/{field}"
    return chat


def results_list(status, key):
    if isinstance(status, list):
        return status
    return status.get(key, []) if isinstance(status, dict) else []


class Service:
    def __init__(self, root, llm, is_active, *, tts_dispatch=None,
                 bot_runtime=None, owner_number=None, bot_number=None):
        self.root = root
        self.active = is_active
        self.stop = asyncio.Event()
        self.purging = False
        self.ready = {}
        self.qr_writer = {}
        self.jobs = set()
        self.chatbot_jobs = {}
        self.active_plans = {}
        self.clients = set()
        self.connecting = set()
        self.account_changes = asyncio.Lock()
        self.removing = set()
        self.bridge = Bridge(root, self.on_event)
        self.accounts = read_json(root / "accounts.json")
        self.rust = None
        if bot_runtime is not None:
            from .rust_bridge import RustBridge

            self.rust = RustBridge(bot_runtime, owner_number, bot_number, root / "bridge" / "media", self.on_event)
            if (self.rust.runtime / "unlink-request.json").exists():
                raise RuntimeError("Finish removing the previous WhatsApp bot connection before serving.")
            self.rust.account = self.register_rust_account()
        self.ledger = OperationLedger(root / "operations.sqlite")
        self.db = self.ledger.db
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, turn TEXT, plan TEXT, authorized INTEGER DEFAULT 0)")
        self.bot = Chatbot(root / "chatbot.sqlite", llm, self.bot_transport, self.bot_settings,
                           self.is_active, managed_identities=self.identities, media_root=root / "bridge" / "media",
                           tts_dispatch=tts_dispatch)

    def is_active(self):
        return not self.stop.is_set() and not self.purging and self.active()

    def check_active(self):
        if not self.is_active():
            raise RuntimeError("WhatsApp Control is disabled or shutting down.")

    def register_rust_account(self):
        identity = self.rust.bot + "@s.whatsapp.net"
        accounts = [label for label in self.accounts if any(phone_number(value) == self.rust.bot
                                                            for value in self.identities(label))]
        if len(accounts) > 1:
            raise ValueError("Multiple account labels match the configured WhatsApp bot.")
        account = accounts[0] if accounts else "bot"
        if not accounts:
            suffix = 2
            while account in self.accounts:
                account = f"bot-{suffix}"
                suffix += 1
        existing = self.accounts.get(account, {})
        settings = {**existing, "_transport": "rust",
                    "_identity": {**existing.get("_identity", {}), "id": identity, "phone": identity}}
        if existing != settings:
            if existing and "_transport" not in existing:
                settings["_former_baileys"] = True
            self.accounts[account] = settings
            self.save_accounts()
        return account

    async def transport(self, op, account, args=None):
        settings = self.accounts.get(account, {})
        if self.rust is not None and account == self.rust.account:
            if op in {"send_result", "group_create_result"} and settings.get("_former_baileys"):
                # Receipt reads for an account migrated from Baileys use only its local database.
                former = await self.bridge.call(op, account, args)
                if former and former.get("status") in {"accepted", "rejected"}:
                    return former
            return await self.rust.call(op, args or {})
        if settings.get("_transport") == "rust":
            raise BridgeError("This account's unified WhatsApp transport is unavailable.", "unavailable")
        if self.rust is not None and op in {"pair", "repair"}:
            args = {**(args or {}), "reserved_bot_number": self.rust.bot}
        return await self.bridge.call(op, account, args)

    def identities(self, account):
        saved = self.accounts.get(account, {}).get("_identity", {})
        identity = self.ready.get(account, {}).get("identity") or saved
        if self.accounts.get(account, {}).get("_transport") == "rust":
            identity = {**saved, **identity}
        return [identity[key] for key in ("id", "lid", "phone") if identity.get(key)]

    def bot_settings(self, account):
        if account in self.removing:
            return {}
        setup = read_json(self.root / "setup.json")
        settings = self.accounts.get(account, {})
        exclude = list(settings.get("exclude_chats", []))
        exclude += [number for number in (setup.get("owner"), setup.get("command_account")) if number]
        if self.rust is not None:
            exclude += [self.rust.owner, self.rust.bot]
        if setup.get("owner_chat"):
            exclude.append(setup["owner_chat"])
        return {**settings, "exclude_chats": exclude}

    async def bot_transport(self, op, account, args):
        self.check_active()
        if account not in self.accounts or account in self.removing:
            raise RuntimeError("This WhatsApp account is unavailable.")
        if op not in {"send_text", "send_media", "download_media", "discard_media"}:
            raise ValueError("Unsupported chatbot operation.")
        return await self.transport(op, account, args)

    def on_event(self, event):
        account = event.get("account")
        if event.get("event") == "connection":
            self.ready[account] = event
        elif event.get("event") == "qr" and account in self.qr_writer:
            writer = self.qr_writer[account]
            if not writer.is_closing() and event.get("terminal"):
                writer.write(encode_frame({"event": "qr", "terminal": event["terminal"]}))
        elif (event.get("event") == "message" and self.is_active()
              and account in self.accounts and account not in self.removing):
            task = asyncio.create_task(self.bot.handle_event(event))
            self.jobs.add(task)
            self.chatbot_jobs[task] = account
            task.add_done_callback(self.job_done)

    def job_done(self, task):
        self.jobs.discard(task)
        self.chatbot_jobs.pop(task, None)
        if not task.cancelled() and task.exception() is not None:
            print("WhatsApp Control: chatbot processing failed.", file=sys.stderr)

    def save_accounts(self):
        write_json(self.root / "accounts.json", self.accounts)

    async def status(self):
        accounts = {}
        for account, settings in self.accounts.items():
            current = self.ready.get(account, {})
            accounts[account] = {"status": current.get("status", "offline"),
                                 "identity": current.get("identity") or settings.get("_identity")}
            reason = self.bot.model_failure(account)
            if reason is not None:
                accounts[account]["chatbot"] = {"status": "unavailable", "reason": reason}
        return {"running": self.is_active(), "accounts": accounts,
                "default_account": self.default_account()}

    def default_account(self):
        selected = read_json(self.root / "setup.json").get("default_account")
        return selected if selected in self.accounts else None

    async def connect(self, account):
        try:
            result = await self.transport("connect", account)
            if (account in self.accounts and result.get("identity")
                    and (self.rust is None or account != self.rust.account)):
                self.accounts[account]["_identity"] = result["identity"]
                self.save_accounts()
        except (RuntimeError, OSError):
            self.ready.setdefault(account, {"status": "offline"})

    async def pair(self, account, writer):
        self.check_active()
        if account in self.accounts or account in self.qr_writer:
            raise ValueError("This account label is already in use.")
        self.qr_writer[account] = writer
        try:
            result = await self.transport("pair", account)
            self.check_active()
            identity = result.get("identity") or {}
            known = {value for key, value in identity.items() if key in {"id", "phone", "lid"} and value}
            if not known or any(known.intersection(self.identities(other)) for other in self.accounts):
                await self.unlink(account)
                raise ValueError("This WhatsApp identity is already paired or could not be identified.")
            self.accounts[account] = {"_identity": identity}
            self.ready[account] = result
            self.save_accounts()
            return {"account": account, **result}
        except (RuntimeError, OSError):
            if account not in self.accounts:
                with contextlib.suppress(RuntimeError, OSError):
                    await self.bridge.call("disconnect", account)
                    await self.bridge.call("remove_account", account)
                self.ready.pop(account, None)
            raise
        finally:
            self.qr_writer.pop(account, None)

    async def repair(self, account, writer):
        self.check_active()
        if account not in self.accounts:
            raise ValueError("Account label is not paired.")
        if self.ready.get(account, {}).get("status") == "connected":
            raise ValueError("This account already has a working connection.")
        if any(account in accounts for accounts in self.active_plans.values()):
            raise ValueError("Wait for this account's active WhatsApp operations before repairing it.")
        identity = self.accounts[account].get("_identity")
        self.qr_writer[account] = writer
        self.removing.add(account)
        try:
            result = await self.transport("repair", account, {"identity": identity})
            self.accounts[account]["_identity"] = result["identity"]
            self.ready[account] = result
            self.save_accounts()
            return {"account": account, **result}
        finally:
            self.qr_writer.pop(account, None)
            self.removing.discard(account)

    async def unlink(self, account):
        account_id(account)
        if any(account in accounts for accounts in self.active_plans.values()):
            raise ValueError("Wait for this account's active WhatsApp operations before unlinking it.")
        self.removing.add(account)
        try:
            return await self.remove_account(account)
        finally:
            self.removing.discard(account)

    async def unlink_transport(self, account):
        try:
            logout = await self.transport("logout", account)
        except (RuntimeError, OSError):
            logout = {}
        if not isinstance(logout, dict):
            logout = {}
        return {"account": account, "local_removed": False,
                "unlink_requested": logout.get("unlink_requested") is True,
                "remote_unlinked": logout.get("remote_unlink_confirmed") is True}

    async def remove_account(self, account):
        tasks = [task for task, label in self.chatbot_jobs.items() if label == account]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        rust_account = self.accounts.get(account, {}).get("_transport") == "rust"
        result = await self.unlink_transport(account)
        if not rust_account or self.accounts[account].get("_former_baileys"):
            await self.bridge.call("disconnect", account)
            await self.bridge.call("remove_account", account)
        self.accounts.pop(account, None)
        self.ready.pop(account, None)
        self.bot.forget_account(account)
        with self.db:
            self.ledger.forget_account(account)
            rows = self.db.execute("""SELECT id, authorized, plan FROM plans
                WHERE EXISTS (SELECT 1 FROM json_each(plans.plan, '$.accounts') WHERE key=?)""", (account,)).fetchall()
            for operation_id, authorized, serialized in rows:
                plan = json.loads(serialized)
                # Reserve the original ID so a later prepare cannot reuse an old approval.
                plan["accounts"] = {label: jobs for label, jobs in plan["accounts"].items()
                                    if authorized and label != account}
                plan["lookup"] = {label: detail for label, detail in plan.get("lookup", {}).items()
                                  if label in plan["accounts"]}
                plan["scope_changed"] = True
                self.db.execute("UPDATE plans SET plan=? WHERE id=?", (json.dumps(plan, ensure_ascii=False), operation_id))
        setup = read_json(self.root / "setup.json")
        if setup.get("default_account") == account:
            setup.pop("default_account")
            write_json(self.root / "setup.json", setup)
        self.save_accounts()
        result["local_removed"] = True
        if rust_account and self.rust is not None and account == self.rust.account:
            self.rust.request_unlink(result)
        return result

    async def purge(self):
        self.purging = True
        pending = (self.jobs | self.clients | self.connecting) - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        # Local deletion belongs to the CLI after this worker and its transports exit.
        accounts = list(self.accounts)
        bot = self.rust.account if self.rust is not None else None
        results = {row["account"]: row for row in await asyncio.gather(*(
            self.unlink_transport(account) for account in accounts if account != bot
        ))}
        if bot in self.accounts:
            # The sidecar can exit on logout. Let the parent wait for this worker's reply.
            with contextlib.suppress(OSError):
                write_json(self.rust.runtime / "purge-in-progress.json", {"control_pid": os.getpid()})
            results[bot] = await self.unlink_transport(bot)
        for account, result in results.items():
            if self.accounts[account].get("_transport") == "rust":
                result["runtime"] = "unified"
        return [results[account] for account in accounts]

    def targets(self, args):
        accounts = args.get("accounts")
        if accounts is None:
            if default := self.default_account():
                return [default]
            if len(self.accounts) == 1:
                return list(self.accounts)
        if not isinstance(accounts, list) or not accounts:
            raise ValueError("Select the account by label.")
        if accounts == ["*"]:
            if not self.accounts:
                raise ValueError("No WhatsApp accounts are paired.")
            return list(self.accounts)
        names = list(dict.fromkeys(account_id(account) for account in accounts))
        if unknown := [name for name in names if name not in self.accounts]:
            raise ValueError("Unknown account: " + ", ".join(unknown))
        return names

    def synced_recipients(self, contacts):
        normalized = []
        for contact in contacts:
            if not isinstance(contact, dict) or contact.get("saved") is not True:
                continue
            raw = contact.get("id")
            try:
                normalized.append(whatsapp_id(raw, groups=False))
            except ValueError:
                pass
        return list(dict.fromkeys(normalized))

    async def owner(self, turn, action, args):
        self.check_active()
        if not isinstance(turn, str) or not turn:
            raise ValueError("Verified owner request is required.")
        if action == "status":
            return await self.status()
        if action == "operation_status":
            return await self.operation_status(args.get("operation_id"))
        if action == "prepare":
            return await self.prepare_operation(turn, args)
        if action == "execute":
            return await self.execute_operation(turn, args["operation_id"], retry=args.get("retry") is True)
        if action in READS:
            output = {}
            for account in self.targets(args):
                try:
                    output[account] = await self.account_read(account, action, args)
                except (RuntimeError, ValueError, OSError) as error:
                    output[account] = {"error": str(error)}
            return output
        raise ValueError("Unknown WhatsApp operation.")

    def configure(self, accounts, patch, *, local):
        self.check_active()
        normalized = policy_patch(patch, local=local)
        for account in accounts:
            # A pairing keeps its object identity until unlink, even when its policy changes.
            self.accounts[account].update(normalized)
            if "history_limit" in normalized:
                self.bot.trim_history(account, normalized["history_limit"])
        self.save_accounts()
        return {"accounts": accounts, "updated": sorted(normalized)}

    async def account_read(self, account, action, args):
        if action == "pauses":
            if args.get("chat"):
                return {"paused_until": self.bot.paused_until(account, whatsapp_id(args["chat"])) or None}
            return self.bot.active_pauses(account)
        if action == "policy":
            return {key: value for key, value in self.accounts[account].items()
                    if not key.startswith("_") and key not in {"stt_command", "tts_command"}}
        payload = {"chat": whatsapp_id(args.get("chat"))} if action in {"group_metadata", "recent_messages"} else {}
        if action == "recent_messages" and args.get("limit") is not None:
            payload["limit"] = args["limit"]
        return await self.transport(action, account, payload)

    def operation_jobs(self, action, args, targets):
        if action == "configure":
            return [{"policy": policy_patch(args.get("policy"))}]
        if action == "set_default":
            return [{}]
        if action in {"pause", "extend_pause", "resume"}:
            chat = whatsapp_id(args.get("chat"))
            seconds = args.get("seconds")
            if action != "resume" and (type(seconds) is not int or seconds < 1):
                raise ValueError("Specify a positive pause duration in seconds.")
            return [{"chat": chat} if action == "resume" else {"chat": chat, "seconds": seconds}]
        payload = {}
        if action == "send_text":
            if not isinstance(args.get("text"), str) or not args["text"].strip():
                raise ValueError("Supply message text.")
            payload = {"text": args["text"]}
        if action == "send_media":
            if not args.get("path"):
                raise ValueError("Select a file to send.")
            payload = {key: args[key] for key in ("type", "path", "caption") if key in args}
            mime = mimetypes.guess_type(payload["path"])[0] or "application/octet-stream"
            payload.setdefault("type", mime.split("/")[0] if mime.startswith(("image/", "video/", "audio/")) else "document")
            if payload["type"] not in {"image", "video", "audio", "voice", "document"}:
                raise ValueError("Select a supported attachment type.")
            if payload["type"] != "audio" or mime.startswith("audio/"):
                payload["mime"] = mime
        if action == "forward_message":
            if not isinstance(args.get("source_id"), str) or not args["source_id"]:
                raise ValueError("Select the recent WhatsApp message to forward.")
            payload = {"source_chat": whatsapp_id(args.get("source_chat")), "source_id": args["source_id"]}
        if action in SEND:
            return [{**payload, "chat": chat} for chat in targets]
        if action == "post_status":
            content_type = args.get("type")
            if content_type not in {"text", "image", "video", "voice"}:
                raise ValueError("Select text, image, video, or voice for Status.")
            if content_type == "text":
                if not isinstance(args.get("text"), str) or not args["text"].strip():
                    raise ValueError("Supply Status text.")
                return [{"type": content_type, "text": args["text"], "audience": targets}]
            if not args.get("path"):
                raise ValueError("Select a file to publish.")
            payload = {key: args[key] for key in ("type", "path", "caption") if key in args}
            return [{**payload, "audience": targets}]
        if action in {"group_members", "group_settings"}:
            payload["chat"] = whatsapp_id(args.get("chat"))
            if not payload["chat"].endswith("@g.us"):
                raise ValueError("Select a group chat.")
        if action in {"group_members", "group_create"}:
            participants = args.get("participants", [])
            if not isinstance(participants, list) or (action == "group_members" and not participants):
                raise ValueError("Select group participants using their WhatsApp IDs.")
            payload["participants"] = sorted({whatsapp_id(item, groups=False) for item in participants})
        if action == "group_create":
            if not isinstance(args.get("subject"), str) or not args["subject"].strip():
                raise ValueError("Supply a group subject.")
            payload["subject"] = args["subject"]
        if action == "group_members":
            if args.get("member_action") not in {"add", "remove", "promote", "demote"}:
                raise ValueError("Select a supported group member action.")
            return [{"chat": payload["chat"], "participants": [participant], "action": args["member_action"]}
                    for participant in payload["participants"]]
        if action == "group_settings":
            fields = {key: args[key] for key in ("subject", "description", "announce", "restrict") if key in args}
            if not fields:
                raise ValueError("Select at least one group setting.")
            return [{"chat": payload["chat"], key: value} for key, value in fields.items()]
        return [payload]

    async def prepare_operation(self, turn, args):
        action = args.get("action")
        if action not in SETTINGS | MUTATIONS:
            raise ValueError("Unknown WhatsApp action.")
        scope = args.get("accounts")
        if scope is not None:
            if not isinstance(scope, list) or not scope:
                raise ValueError("Select the account by label.")
            if scope != ["*"]:
                scope = sorted({account_id(account) for account in scope})
        targets = target_selection(action, args)
        jobs = self.operation_jobs(action, args, targets)
        # Hash action-specific normalized inputs before resolving a wildcard or
        # default. Repeating a turn cannot rescope an already prepared send.
        operation_id = hashlib.sha256(json.dumps([turn, action, scope, jobs], sort_keys=True).encode()).hexdigest()[:24]
        _, _, saved = self.get_plan(operation_id, required=False)
        if saved:
            if saved.get("scope_changed"):
                raise ValueError("An account from this operation was unlinked. Use a new request to send again.")
            return self.preview(operation_id, saved)
        accounts = self.targets({"accounts": scope})
        pairings = {account: self.accounts[account] for account in accounts}
        if action == "set_default" and (len(accounts) != 1 or scope in (None, ["*"])):
            raise ValueError("Select exactly one account label to use by default.")
        if any(account in self.removing for account in accounts):
            raise ValueError("The account is being removed or repaired.")
        if path := jobs[0].get("path"):
            digest = await asyncio.to_thread(file_digest, path)
            jobs = [{**job, "expected_sha256": digest} for job in jobs]
        all_contacts = targets == ["*"]
        operations, contact_lists, skipped = {}, {}, {}
        for account in accounts:
            if not targets or not any(not target.endswith("@g.us") for target in targets):
                operations[account] = jobs
                continue
            records = []
            try:
                options = {"saved_only": True} if all_contacts else None
                records = results_list(await self.transport("contacts", account, options), "contacts")
            except (RuntimeError, OSError):
                if all_contacts:
                    operations[account] = []
                    skipped[account] = "contacts_unavailable"
                    continue
            contact_lists[account] = records
            recipients = self.synced_recipients(records) if all_contacts else targets
            if not recipients:
                operations[account] = []
                skipped[account] = "no_synced_contacts"
                continue
            selected = unique_recipients(recipients, recipient_aliases(records))
            if action == "post_status":
                operations[account] = [{**jobs[0], "audience": list(selected.values()), "_effect": sorted(selected)}]
            else:
                operations[account] = [{**jobs[0], "chat": jid, "_effect": canonical}
                                       for canonical, jid in selected.items()]
        if not any(operations.values()):
            details = " ".join(f"{account}: {CONTACT_SKIP_REASONS[reason]}" for account, reason in skipped.items())
            raise ValueError(f"No accounts have selectable synced contacts. {details}")
        names = await self.target_names(operations, action, all_contacts, contact_lists)
        names.update({account: {"skipped": reason} for account, reason in skipped.items()})
        plan = {"action": action, "accounts": operations,
                "uses_default": scope is None,
                "all_accounts": scope == ["*"],
                "all_contacts": all_contacts,
                "saved_contacts_only": all_contacts,
                "lookup": names}
        self.check_active()
        if any(self.accounts.get(account) is not pairing for account, pairing in pairings.items()):
            raise ValueError("An account was unlinked while preparing this operation.")
        if self.removing.intersection(accounts):
            raise ValueError("An account from this operation is being repaired or removed.")
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO plans(id,turn,plan) SELECT ?,?,?
                WHERE NOT EXISTS (SELECT 1 FROM operations WHERE operation_id=?)""",
                            (operation_id, turn, json.dumps(plan, ensure_ascii=False), operation_id))
        _, _, saved = self.get_plan(operation_id, required=False)
        if saved is None:
            raise ValueError("The original plan is unavailable. Only receipt status can be checked.")
        if saved.get("scope_changed"):
            raise ValueError("An account from this operation was unlinked. Use a new request to send again.")
        return self.preview(operation_id, saved)

    async def target_names(self, operations, action, all_contacts, contact_lists):
        lookup = {}
        for account, jobs in operations.items():
            if not jobs:
                continue
            entries = []
            targets = {job[field] for job in jobs for field in ("chat", "source_chat") if job.get(field)}
            targets.update(jid for job in jobs for field in ("participants", "audience") for jid in job.get(field, []))
            if all_contacts:
                targets = {job["source_chat"] for job in jobs if "source_chat" in job}
            if action == "configure":
                targets.update(whatsapp_id(jid) for field in ("dm_allowlist", "group_allowlist", "exclude_chats")
                               for jid in jobs[0]["policy"].get(field, []))
            if any(not target.endswith("@g.us") for target in targets):
                with contextlib.suppress(RuntimeError, OSError):
                    contacts = contact_lists.get(account)
                    if contacts is None:
                        contacts = results_list(await self.transport("contacts", account), "contacts")
                    for contact in contacts:
                        if isinstance(contact, dict):
                            entries.append(((contact.get("id"), contact.get("phone"), contact.get("lid")), contact.get("name")))
            if any(target.endswith("@g.us") for target in targets):
                with contextlib.suppress(RuntimeError, OSError):
                    groups = await self.transport("group_list", account)
                    entries.extend(((group.get("id"),), group.get("subject")) for group in groups)
            names, ambiguous = named_targets(targets, entries)
            details = {"targets": names}
            if ambiguous:
                details["ambiguous_names"] = ambiguous
            if action == "forward_message":
                source = jobs[0]
                cached = await self.transport("recent_messages", account, {"chat": source["source_chat"],
                                                                                "message_id": source["source_id"]})
                details["source"] = next((message for message in cached if message.get("id") == source["source_id"]), None)
                if details["source"] is None:
                    raise ValueError("The selected source message is unavailable in this account's recent messages.")
            lookup[account] = details
        return lookup

    def get_plan(self, operation_id, *, required=True):
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("Select the WhatsApp operation ID.")
        row = self.db.execute("SELECT turn,authorized,plan FROM plans WHERE id=?", (operation_id,)).fetchone()
        if not row:
            if required:
                raise ValueError("WhatsApp operation ID was not found.")
            return None, False, None
        return row[0], bool(row[1]), json.loads(row[2])

    def preview(self, operation_id, plan):
        action = plan["action"]
        counts = {account: len(jobs[0]["audience"]) if jobs and action == "post_status" else len(jobs)
                  for account, jobs in plan["accounts"].items()}
        accounts = {account: [{key: value for key, value in job.items() if key not in {"expected_sha256", "_effect"}}
                              for job in jobs]
                    for account, jobs in plan["accounts"].items() if jobs}
        if plan["all_contacts"] and action in SEND:
            accounts = {account: [{field: data for field, data in jobs[0].items() if field != "chat"}]
                        for account, jobs in accounts.items()}
        elif plan["all_contacts"] and action == "post_status":
            accounts = {account: [{field: data for field, data in jobs[0].items() if field != "audience"}]
                        for account, jobs in accounts.items()}
        preview = {"operation_id": operation_id, "action": action, "accounts": accounts, "counts": counts,
                   "all_contacts": plan["all_contacts"], "all_accounts": plan["all_accounts"],
                   "uses_default": plan["uses_default"], "lookup": plan.get("lookup", {})}
        if plan.get("scope_changed"):
            preview.update(all_accounts=False, scope_changed=True)
        return preview

    def operation_results(self, plan, results):
        skipped = []
        for account, details in plan.get("lookup", {}).items():
            if reason := details.get("skipped"):
                skipped.append({"account": account, "action": plan["action"], "target": None, "state": "skipped",
                                "result": {"status": "skipped", "reason": reason, "retry_safe": False,
                                           "error": CONTACT_SKIP_REASONS[reason]}})
        return sorted([*results, *skipped], key=lambda row: row["account"]) if skipped else results

    async def reconcile(self, operation_id):
        pairings = dict(self.accounts)

        async def call(action, account, payload):
            if account not in pairings or self.accounts.get(account) is not pairings[account] or account in self.removing:
                raise RuntimeError("An account from this operation is unavailable.")
            return await self.transport(action, account, payload)

        return await self.ledger.reconcile(operation_id, call)

    async def operation_status(self, operation_id):
        results = await self.reconcile(operation_id)
        _, authorized, plan = self.get_plan(operation_id, required=False)
        if plan is None and not results:
            raise ValueError("WhatsApp operation ID was not found.")
        if authorized:
            results = self.operation_results(plan, results)
        return {"operation": self.preview(operation_id, plan) if plan else None, "authorized": authorized,
                "running": operation_id in self.active_plans,
                "results": results}

    async def execute_operation(self, turn, operation_id, *, retry):
        self.check_active()
        prepared_turn, authorized, plan = self.get_plan(operation_id)
        if not any(plan["accounts"].values()):
            raise ValueError("An account from this operation was unlinked.")
        if (not retry and turn != prepared_turn) or (retry and not authorized):
            raise ValueError("This operation must be authorized in its owner turn.")
        if operation_id in self.active_plans or (authorized and not retry):
            return await self.operation_status(operation_id)
        if plan["all_contacts"] and plan.get("saved_contacts_only") is not True:
            raise ValueError("This operation used an older contact list. Check its receipt status and select recipients in a new request.")
        if not plan["accounts"].keys() <= self.accounts.keys():
            raise ValueError("An account from this operation was unlinked.")
        self.active_plans[operation_id] = set(plan["accounts"])
        try:
            if retry:
                await self.reconcile(operation_id)
            for account in plan["accounts"]:
                if account in self.removing:
                    raise ValueError("An account from this operation is being repaired or removed.")
            self.check_active()
            with self.db:
                self.db.execute("UPDATE plans SET authorized=1 WHERE id=?", (operation_id,))
            task = asyncio.create_task(self.run_plan(turn, operation_id, plan))
        except BaseException:
            self.active_plans.pop(operation_id, None)
            raise
        self.jobs.add(task)
        task.add_done_callback(self.job_done)
        try:
            return await asyncio.wait_for(asyncio.shield(task), 55)
        except TimeoutError:
            return await self.operation_status(operation_id)

    async def run_plan(self, turn, operation_id, plan):
        action = plan["action"]
        try:
            for account, jobs in plan["accounts"].items():
                for job in jobs:
                    self.check_active()
                    payload = {key: value for key, value in job.items() if key != "_effect"}
                    effect = job.get("_effect")
                    if effect is not None:
                        field = "audience" if action == "post_status" else "chat"
                        effect = {**payload, field: effect}
                    target = job_target(action, payload)
                    call = self.apply_setting if action in SETTINGS else self.transport
                    available = account not in self.removing and (action in SETTINGS or
                                 self.ready.get(account, {}).get("status") == "connected")
                    await self.ledger.run(action, account, payload, operation_id, call,
                                          available=available, target=target, turn=turn, effect_payload=effect)
            return {"operation_id": operation_id, "running": False,
                    "results": self.operation_results(plan, self.ledger.status(operation_id))}
        finally:
            self.active_plans.pop(operation_id, None)

    async def apply_setting(self, action, account, payload):
        self.check_active()
        if action == "configure":
            return self.configure([account], payload["policy"], local=False)
        if action == "set_default":
            setup = read_json(self.root / "setup.json")
            write_json(self.root / "setup.json", {**setup, "default_account": account})
            return {"default_account": account}
        chat, seconds = payload["chat"], payload.get("seconds")
        if action == "resume":
            self.bot.resume(account, chat)
            return {"paused_until": None}
        pause = self.bot.extend_pause if action == "extend_pause" else self.bot.pause
        return {"paused_until": pause(account, chat, seconds)}

    async def request(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        finished = False
        try:
            frame = json.loads(await asyncio.wait_for(reader.readline(), 5))
            action, args = frame["action"], frame["args"]
            if action == "status":
                result = await self.status()
            elif action == "stop":
                if self.purging:
                    raise RuntimeError("WhatsApp Control purge is already in progress.")
                result = {"stopping": True}
                finished = True
            else:
                self.check_active()
                if action in {"pair", "repair"}:
                    async with self.account_changes:
                        method = self.pair if action == "pair" else self.repair
                        result = await method(account_id(args["account"]), writer)
                elif action == "unlink":
                    async with self.account_changes:
                        account = account_id(args["account"])
                        if account not in self.accounts:
                            raise ValueError("Account label is not paired.")
                        result = await self.unlink(account)
                elif action == "purge":
                    async with self.account_changes:
                        self.check_active()
                        finished = True
                        result = await self.purge()
                elif action == "configure":
                    result = self.configure(self.targets(args), args["policy"], local=True)
                elif action == "owner":
                    result = await self.owner(args["turn"], args["action"], args["args"])
                else:
                    raise ValueError("Unknown service operation.")
            writer.write(encode_frame({"ok": True, "result": result}))
        except (KeyError, ValueError, RuntimeError, OSError, TimeoutError) as error:
            writer.write(encode_frame({"ok": False, "error": str(error)[:300]}))
        finally:
            try:
                with contextlib.suppress(OSError):
                    await writer.drain()
            finally:
                writer.close()
                self.clients.discard(task)
                if finished or (self.rust is not None and self.rust.account not in self.accounts):
                    self.stop.set()

    async def watch(self):
        while not self.stop.is_set() and self.active():
            await asyncio.sleep(0.5)
        self.stop.set()

    async def run(self):
        self.check_active()
        directory = private_dir(socket_dir(self.root))
        socket_path = directory / "rpc"
        # The profile lock is held before unlinking a socket left by a prior process.
        socket_path.unlink(missing_ok=True)
        server = None
        background = []
        transport_failed = False
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        try:
            await self.bridge.start()
            await self.bridge.call("ping", None)
            if self.rust is not None:
                await self.rust.start()
            server = await asyncio.start_unix_server(self.request, socket_path, limit=MAX_FRAME)
            socket_path.chmod(0o600)
            async def stop_on_transport_exit():
                nonlocal transport_failed
                await asyncio.shield(self.bridge.reader)
                if not self.purging:
                    transport_failed = not self.stop.is_set()
                    self.stop.set()

            background = [asyncio.create_task(self.watch()), asyncio.create_task(stop_on_transport_exit())]
            for account in self.accounts:
                task = asyncio.create_task(self.connect(account))
                self.connecting.add(task)
                task.add_done_callback(self.connecting.discard)
                background.append(task)
            await self.stop.wait()
            if transport_failed and self.active():
                raise RuntimeError("The Baileys transport stopped unexpectedly. Restart WhatsApp Control.")
        finally:
            self.stop.set()
            if server:
                server.close()
                await server.wait_closed()
            tasks = list(self.jobs | self.clients) + background
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.rust is not None:
                await self.rust.close()
            await self.bridge.close()
            socket_path.unlink(missing_ok=True)
            self.bot.close()
            self.ledger.close()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)


def run_service(root, llm, active, *, tts_dispatch=None, bot_runtime=None, owner_number=None, bot_number=None):
    # This lock also serializes state cleanup against startup.
    lock_dir = private_dir(socket_dir(root))
    with (lock_dir / "lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("The WhatsApp Control service is already running.") from error
        if not (lock_dir / "lock").exists() or not os.path.samestat(os.fstat(lock.fileno()), (lock_dir / "lock").stat()):
            raise RuntimeError("The worker lock changed during cleanup. Run setup or retry serve.")
        if not active():
            raise RuntimeError("WhatsApp Control is disabled or shutting down.")
        if bot_runtime is None and not read_json(root / "setup.json").get("owner"):
            raise RuntimeError("Run hermes whatsapp-control setup first.")
        asyncio.run(Service(root, llm, active, tts_dispatch=tts_dispatch, bot_runtime=bot_runtime,
                            owner_number=owner_number, bot_number=bot_number).run())
