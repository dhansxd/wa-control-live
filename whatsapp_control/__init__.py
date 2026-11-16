"""Hermes plugin registration. All network activity belongs to the profile worker."""

import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path

from .settings import PLUGIN, JID, phone_number, read_json
from .owner_request import OwnerRequests, verify_request


OWNER_ACTIONS = {
    "status", "configure", "set_default", "policy", "pause", "extend_pause", "resume", "pauses",
    "send_text", "send_media", "forward_message", "post_status", "recent_messages",
    "group_create", "group_members", "group_metadata", "group_list", "group_settings", "contacts",
    "operation_status", "retry_operation",
}
READ_ACTIONS = {"status", "policy", "pauses", "recent_messages", "group_metadata", "group_list", "contacts", "operation_status"}

SCHEMA = {
    "name": "whatsapp_control",
    "description": (
        "Execute an explicit WhatsApp request from the verified owner. Look up paired accounts "
        "with status and their local WhatsApp IDs with contacts or group_list. Omit the label to use "
        "the owner's default or only paired account. Select '*' only when the owner requests all accounts. "
        "Use recent_messages to locate a WhatsApp message for forwarding. For an existing operation use "
        "operation_status with its returned operation_id, or retry_operation if the owner wants to retry. "
        "If authorization reports unclear details, ask the owner to clarify."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(OWNER_ACTIONS)},
            "accounts": {"type": "array", "items": {"type": "string"}},
            "chat": {"type": "string", "description": "WhatsApp chat ID, or phone number for a DM."},
            "chats": {"type": "array", "items": {"type": "string"}, "description": "Explicit destinations, or ['*'] when the owner asks for all synced contacts."},
            "text": {"type": "string"},
            "type": {"type": "string", "enum": ["text", "image", "video", "audio", "voice", "document"],
                     "description": "For Status, use text, image, video, or voice. Voice uses a mono Ogg/Opus file."},
            "path": {"type": "string", "description": "Path of the owner's explicitly selected file."},
            "source_chat": {"type": "string", "description": "WhatsApp source chat on the same sending account."},
            "source_id": {"type": "string", "description": "Message ID returned by recent_messages."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "How many recent messages to search."},
            "caption": {"type": "string"},
            "audience": {"type": "array", "items": {"type": "string"}, "description": "Explicit Status recipients. Use ['*'] only if the owner explicitly selects all synced contacts."},
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "announce": {"type": "boolean"},
            "restrict": {"type": "boolean"},
            "participants": {"type": "array", "items": {"type": "string"}},
            "member_action": {"type": "string", "enum": ["add", "remove", "promote", "demote"]},
            "seconds": {"type": "integer", "minimum": 1},
            "policy": {
                "type": "object",
                "description": "Chatbot settings, e.g. dm_mode all, group_mode off, instructions, history_limit, handoff_seconds, image_enabled, voice_enabled, or reply_mode text/voice/match.",
            },
            "operation_id": {"type": "string", "description": "ID from a previous operation result."},
        },
        "required": ["action"],
    },
}


def session_value(session_env, name):
    key = "HERMES_SESSION_" + name
    # Hermes falls back to the environment when a ContextVar is unset.
    if key in os.environ:
        return ""
    value = session_env(key)
    return value if isinstance(value, str) else ""


def owner_turn(config, session_env, expected_profile):
    def value(name):
        return session_value(session_env, name)

    if value("PLATFORM") != "whatsapp" or value("CHAT_TYPE") not in {"dm", "direct"}:
        return False
    # The stock single-profile gateway can leave the route profile empty.
    if value("PROFILE") not in {"", expected_profile}:
        return False
    actor = value("ACTOR_ID")
    sender = value("USER_ID")
    alternative = value("USER_ID_ALT")
    chat = value("CHAT_ID")
    if not actor or actor not in {sender, alternative}:
        return False
    chat_number = phone_number(chat)
    if not chat_number and not (JID.fullmatch(chat) and chat.endswith("@lid")):
        return False
    sender_numbers = {number for value in (sender, alternative) if (number := phone_number(value))}
    if chat_number and chat_number not in sender_numbers:
        return False
    owner = config.get("owner")
    expected_chat = config.get("owner_chat")
    if expected_chat and expected_chat != chat:
        return False
    if not owner or sender_numbers - {owner}:
        return False
    if not sender_numbers:
        # A locally configured exact LID is trusted only when the current DM and
        # actor both use that LID, and Hermes didn't offer a conflicting PN.
        if not (expected_chat == chat == actor and chat.endswith("@lid")):
            return False
    return True


def attachment_path(raw, root, *private_roots):
    # Keep Hermes's documented file policy in force. If an older or changed
    # installation cannot provide it, fail before the worker opens the file.
    try:
        from agent.file_safety import raise_if_read_blocked
        from agent.runtime_cwd import resolve_agent_cwd
    except ImportError as error:
        raise RuntimeError("This Hermes installation cannot check attachment paths.") from error

    if not isinstance(raw, str) or not raw:
        raise ValueError("Select an attachment path.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(resolve_agent_cwd()) / path
    path = path.resolve(strict=True)
    raise_if_read_blocked(str(path))
    if not path.is_file() or any(path.is_relative_to(directory.resolve()) for directory in (root, *private_roots)):
        raise ValueError("Select a regular attachment outside the plugin's private data directory.")
    return str(path)


def register(ctx, *, entry=__file__, authenticated_request=None, cli_options=None, toolset="whatsapp_control"):
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home

    from .runtime import activation_ids, active_install, request_worker, runtime_path
    from .cli import add_arguments, run

    requests = OwnerRequests()
    tool_turn = ContextVar("whatsapp_control_tool_turn", default="")
    cli_options = cli_options or {}
    configured_owner = phone_number(cli_options.get("owner_number"))
    plugin_data = getattr(getattr(ctx, "state", None), "data_dir", None)
    private_roots = (Path(plugin_data),) if plugin_data is not None else ()

    def origin():
        root = runtime_path(get_hermes_home())
        setup = read_json(root / "setup.json")
        if configured_owner and setup.get("owner") != configured_owner:
            setup = {"owner": configured_owner}
        if owner_turn(setup, get_session_env, ctx.profile_name):
            return root, session_value(get_session_env, "CHAT_ID")
        return root, ""

    def record_request(user_message=None, conversation_history=None, task_id=None, turn_id=None,
                       parent_session_id=None, session_id=None, **_):
        if parent_session_id:
            return
        root, chat = origin()
        if not chat or not isinstance(turn_id, str):
            return
        if authenticated_request is not None:
            proof = authenticated_request(session_id) if session_id else None
            requests.record((str(root), chat), task_id, turn_id, proof[1] if proof else "",
                            authenticated=proof is not None, admission_id=proof[0] if proof else None)
            return
        # The staged user row identifies this inbound event even when Hermes
        # retains the opening DM's routing context for a queued follow-up.
        row = conversation_history[-1] if isinstance(conversation_history, list) and conversation_history else None
        message_id = row.get("platform_message_id") if isinstance(row, dict) else None
        authenticated = (isinstance(message_id, str) and bool(message_id.strip())
                         and row.get("role") == "user" and isinstance(row.get("content"), (str, list))
                         and row.get("display_kind") is None)
        requests.record((str(root), chat), task_id, turn_id, user_message, authenticated=authenticated)

    def bind_tool_turn(tool_name, args, next_call, turn_id=None, **_):
        if tool_name != "whatsapp_control":
            return next_call(args)
        token = tool_turn.set(turn_id if isinstance(turn_id, str) else "")
        try:
            return next_call(args)
        finally:
            tool_turn.reset(token)

    async def handle(args, task_id=None, turn_origin=None, session_id=None, **_):
        try:
            root, chat = origin()
            if not chat or turn_origin:
                return json.dumps({"error": "This operation requires a verified WhatsApp owner DM with Hermes actor context."})
            if not active_install(entry, activation_ids(ctx)):
                return json.dumps({"error": "WhatsApp Control is disabled."})
            action = args.get("action")
            if action not in OWNER_ACTIONS:
                return json.dumps({"error": "Unknown WhatsApp operation."})
            host_turn = tool_turn.get()
            request_key = ((str(root), chat), task_id, host_turn)
            proof = None
            if authenticated_request is not None:
                proof = await asyncio.to_thread(authenticated_request, session_id) if session_id else None
                if proof is None:
                    raise ValueError("No authenticated owner request is available for this Hermes turn.")
            admission_id = proof[0] if proof else None
            current, previous = requests.get(*request_key, admission_id=admission_id)
            if current is None:
                raise ValueError("No authenticated owner request is available for this Hermes turn.")
            if proof is not None:
                # Hermes can persist the transcribed voice note after pre_llm_call.
                current = proof[1]
            operation_turn = admission_id or host_turn

            async def worker(verb, arguments):
                return await request_worker(root, "owner", {"turn": operation_turn, "action": verb, "args": arguments})

            if action in READ_ACTIONS:
                result = await worker(action, args)
            else:
                if not current:
                    raise ValueError("No authenticated owner request is available for this Hermes turn.")
                if action in {"send_media", "post_status"} and args.get("path"):
                    args = {**args, "path": attachment_path(args["path"], root, *private_roots)}

                retry = action == "retry_operation"
                if retry:
                    prepared = (await worker("operation_status", args))["operation"]
                    if prepared is None:
                        raise ValueError("The original plan is unavailable. Only receipt status can be checked.")
                    if not prepared["accounts"]:
                        raise ValueError("All accounts from this operation were unlinked.")
                else:
                    prepared = await worker("prepare", args)
                await verify_request(ctx.llm, current, previous, prepared, retry=retry)
                if prepared.get("all_contacts") and not retry:
                    summary = ", ".join(f"{account}: {count}" for account, count in prepared["counts"].items())
                    await asyncio.to_thread(ctx.post_message, f"WhatsApp · {prepared['action']} · synced contacts ({summary})")
                if proof is not None and await asyncio.to_thread(authenticated_request, session_id) != proof:
                    raise ValueError("The owner turn or plugin activation changed before execution.")
                if (origin() != (root, chat) or requests.get(*request_key, admission_id=admission_id)[0] is None
                        or not active_install(entry, activation_ids(ctx))):
                    raise ValueError("The owner turn or plugin activation changed before execution.")
                result = await worker("execute", {"operation_id": prepared["operation_id"], "retry": retry})
                if prepared.get("all_contacts"):
                    result["synced_contacts_per_account"] = prepared["counts"]
            return json.dumps(result, ensure_ascii=False)
        except (OSError, RuntimeError, ValueError) as error:
            return json.dumps({"error": str(error)})

    ctx.register_hook("pre_llm_call", record_request)
    ctx.register_middleware("tool_execution", bind_tool_turn)
    ctx.register_tool(name="whatsapp_control", toolset=toolset, schema=SCHEMA, handler=handle, is_async=True)
    ctx.register_cli_command(
        name="whatsapp-control", help="Manage linked WhatsApp accounts",
        setup_fn=add_arguments, handler_fn=lambda args: run(ctx, entry, args, **cli_options),
    )
