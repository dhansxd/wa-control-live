"""Local setup, pairing, and removal commands."""

import asyncio
from contextlib import closing
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .runtime import ServiceStopped, activation_ids, active_install, request_worker, runtime_path, socket_dir
from .settings import JID, account_id, phone_number, private_dir, read_json, write_json


def add_arguments(parser):
    commands = parser.add_subparsers(
        dest="action", required=True,
        metavar="{setup,serve,pair,repair,unlink,status,stop,configure,cleanup}",
    )
    setup = commands.add_parser("setup", help="Prepare the service and authorize a WhatsApp owner DM")
    setup.add_argument("--owner", required=True, help="Owner's WhatsApp number with country code")
    setup.add_argument("--owner-chat", help="Optional exact owner DM ID")
    setup.add_argument("--command-account", help="Optional number that receives the owner's commands")
    commands.add_parser("serve", help="Start the service in this terminal")
    pair = commands.add_parser("pair", help="Pair one WhatsApp account using a local QR")
    pair.add_argument("account")
    repair = commands.add_parser("repair", help="Refresh an unavailable pairing for the same account")
    repair.add_argument("account")
    unlink = commands.add_parser("unlink", help="Remove one linked account from this plugin")
    unlink.add_argument("account")
    commands.add_parser("status", help="Show the service and account connection status")
    commands.add_parser("stop", help="Stop the service and keep pairing")
    configure = commands.add_parser("configure", help="Update one account's chatbot policy")
    configure.add_argument("account")
    configure.add_argument("policy", help="JSON object or @path to a local JSON file")
    cleanup = commands.add_parser("cleanup", help="Stop the service before removing the plugin")
    cleanup.add_argument("--purge", action="store_true", help="Remove local plugin data and request remote unlink")
    commands.add_parser("_speech", add_help=False)


def prepare(entry):
    source = Path(entry).parent
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Install Node.js 22.13+ and npm before setup.")
    probe = subprocess.run(
        [node, "--input-type=module", "--eval", "import('node:sqlite'); await import('baileys'); await import('qrcode-terminal');"],
        cwd=source, capture_output=True, timeout=15, check=False,
    )
    if probe.returncode:
        raise RuntimeError("Node dependencies are unavailable. Run npm ci in the installed plugin directory.")


def show(result):
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def invoke(root, action, args=None, event=None):
    return asyncio.run(request_worker(root, action, args, event))


def _cleanup_storage(path):
    if path.is_symlink() or (path.exists() and (not path.is_dir() or path.stat().st_uid != os.getuid())):
        raise ValueError("WhatsApp cleanup storage must belong to this profile and user.")


def _cleanup_accounts(root):
    rows = {}
    inventory = root / "accounts.json"
    if inventory.is_symlink():
        raise ValueError("The WhatsApp Control account inventory must belong to this profile.")
    stored = read_json(inventory)
    if not isinstance(stored, dict):
        raise ValueError("The WhatsApp Control account inventory is invalid.")
    for account, settings in sorted(stored.items()):
        rows[account, "control"] = {"account": account, "local_removed": False, "remote_unlinked": False}
        if isinstance(settings, dict) and settings.get("_transport") == "rust":
            row = rows[account, "control"]
            row["runtime"] = "unified"
            identity = settings.get("_identity")
            if isinstance(identity, dict):
                row["_bot_number"] = phone_number(identity.get("phone") or identity.get("id"))

    bridge = root / "bridge"
    database = bridge / "transport.sqlite3"
    if bridge.is_symlink() or database.is_symlink():
        raise ValueError("WhatsApp Control transport storage must belong to this profile.")
    if database.is_file():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            has_auth = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'auth'").fetchone()
            credentials = db.execute(
                "SELECT DISTINCT account FROM auth WHERE kind = 'creds' AND id = 'current' ORDER BY account"
            ) if has_auth else ()
            for (account,) in credentials:
                row = rows.get((account, "control"))
                if row is None or row.get("runtime") == "unified":
                    rows[account, "baileys"] = {"account": account, "runtime": "baileys", "local_removed": False,
                                                "remote_unlinked": False}
    return rows


def _cleanup_report(rows, results, runtimes):
    for result in results:
        account = result["account"]
        row = rows.setdefault((account, "control"), {"account": account})
        if result.get("runtime") == "unified":
            row["runtime"] = "unified"
        row["unlink_requested"] = result.get("unlink_requested") is True
        row["remote_unlinked"] = result.get("remote_unlinked") is True
    for runtime in runtimes:
        kind = runtime["runtime"]
        bot = runtime.get("bot_number")
        matching = [row for row in rows.values() if row.get("runtime") == "unified"
                    and bot and row.get("_bot_number") == bot] if kind == "unified" else []
        row = matching[0] if len(matching) == 1 else None
        if row is None:
            row = rows[runtime["account"], kind] = {"account": runtime["account"], "runtime": kind}
        row["remote_unlinked"] = row.get("remote_unlinked") is True or runtime.get("remote_unlinked") is True
        if "unlink_requested" in runtime:
            row["unlink_requested"] = row.get("unlink_requested") is True or runtime["unlink_requested"] is True
    for row in rows.values():
        row.pop("_bot_number", None)
        row["local_removed"] = True
    return list(rows.values())


def cleanup(root, purge, *, cleanup_runtime=None):
    _cleanup_storage(root.parent)
    _cleanup_storage(root)
    directory = socket_dir(root)
    _cleanup_storage(directory)
    path = directory / "rpc"
    if path.is_symlink():
        raise ValueError("WhatsApp Control's worker address must be local to this profile.")
    lock_path = directory / "lock"
    if lock_path.is_symlink():
        raise ValueError("WhatsApp Control's worker lock must belong to this profile.")
    results = []
    if path.exists():
        try:
            results = invoke(root, "purge" if purge else "stop")
        except ServiceStopped:
            pass
    private_dir(directory)
    if lock_path.is_symlink():
        raise ValueError("WhatsApp Control's worker lock must belong to this profile.")
    with lock_path.open("a", encoding="utf-8") as lock:
        deadline = time.monotonic() + 12
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError("The worker has not exited. Stop it before cleanup.") from error
                time.sleep(0.1)
        if not lock_path.exists() or not os.path.samestat(os.fstat(lock.fileno()), lock_path.stat()):
            raise RuntimeError("The worker lock changed during cleanup. Retry purging.")
        _cleanup_storage(root)
        rows = _cleanup_accounts(root) if purge and root.exists() else {}
        runtime = cleanup_runtime(purge=purge) if cleanup_runtime is not None else {}
        if purge and root.exists():
            shutil.rmtree(root)
        path.unlink(missing_ok=True)
        lock_path.unlink()
        try:
            directory.rmdir()
        except OSError:
            pass
    accounts = _cleanup_report(rows, results, runtime.pop("runtime_accounts", [])) if purge else []
    report = {"stopped": True, **runtime}
    if purge:
        report.update(local_data_removed=True, accounts=accounts)
        if any(not row["remote_unlinked"] for row in accounts):
            report["note"] = "For remote_unlinked=false, check WhatsApp > Linked Devices and remove the device if it remains listed."
    else:
        report["data_retained"] = root.exists() or report.get("runtime_data_retained", False)
    show(report)


def run(ctx, entry, args, *, bot_runtime=None, owner_number=None, bot_number=None,
        cleanup_runtime=None, cleanup_guard=None):
    from hermes_constants import get_hermes_home

    root = runtime_path(get_hermes_home())
    try:
        if args.action == "setup":
            prepare(entry)
            owner = phone_number(args.owner)
            command_account = phone_number(args.command_account)
            if not owner or (args.command_account and not command_account):
                raise ValueError("Enter each WhatsApp number in international format.")
            if bot_runtime is not None:
                if owner != phone_number(owner_number) or (command_account and command_account != phone_number(bot_number)):
                    raise ValueError("Use the owner and bot configured for WA Control & Live.")
                command_account = phone_number(bot_number)
            if args.owner_chat and (not JID.fullmatch(args.owner_chat) or args.owner_chat.endswith("@g.us")):
                raise ValueError("Owner chat must be a WhatsApp DM ID.")
            private_dir(root)
            setup_file = root / "setup.json"
            write_json(setup_file, {
                **read_json(setup_file), "owner": owner, "owner_chat": args.owner_chat, "command_account": command_account,
            })
            show({"ready": True, "next": "hermes whatsapp-control serve"})
        elif args.action == "serve":
            from .service import run_service
            from .speech import speech_dispatch

            prepare(entry)
            if bot_runtime is None and not read_json(root / "setup.json").get("owner"):
                raise RuntimeError("Run hermes whatsapp-control setup first.")
            private_dir(root)
            st = Path(entry).stat()
            generation = (st.st_dev, st.st_ino)

            run_service(root, ctx.llm, lambda: active_install(entry, activation_ids(ctx), generation),
                        tts_dispatch=speech_dispatch(entry, root), bot_runtime=bot_runtime,
                        owner_number=owner_number, bot_number=bot_number)
        elif args.action == "_speech":
            from .speech import run_speech

            return run_speech(ctx, entry, root)
        elif args.action in {"pair", "repair"}:
            if not sys.stdout.isatty():
                raise RuntimeError("Pairing requires a local interactive terminal for the QR.")
            show(invoke(root, args.action, {"account": account_id(args.account)},
                        lambda event: print(event["terminal"], flush=True)))
        elif args.action == "unlink":
            show(invoke(root, "unlink", {"account": account_id(args.account)}))
        elif args.action == "configure":
            raw = args.policy
            if raw.startswith("@"):
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            show(invoke(root, "configure", {"accounts": [account_id(args.account)], "policy": json.loads(raw)}))
        elif args.action == "cleanup":
            if cleanup_guard is not None:
                cleanup_guard()
            cleanup(root, args.purge, cleanup_runtime=cleanup_runtime)
        elif args.action == "status":
            try:
                status = invoke(root, "status")
            except ServiceStopped:
                accounts = read_json(root / "accounts.json")
                default = read_json(root / "setup.json").get("default_account")
                status = {"running": False, "default_account": default if default in accounts else None,
                          "accounts": {account: {"status": "offline"} for account in accounts}}
            show(status)
        else:
            show(invoke(root, args.action))
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"WhatsApp Control: {error}", file=sys.stderr)
        raise SystemExit(1) from error
