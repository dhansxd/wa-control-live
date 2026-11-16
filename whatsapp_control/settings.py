"""Profile-owned configuration and bounded input validation."""

import json
import os
import re
from pathlib import Path


PLUGIN = "hermes-whatsapp-control"
ACCOUNT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
PHONE = re.compile(r"[1-9][0-9]{5,14}\Z")
JID = re.compile(r"[0-9]+(?:-[0-9]+)?@(?:s\.whatsapp\.net|lid|g\.us)\Z")

POLICY_TYPES = {
    "dm_mode": str, "dm_allowlist": list, "group_mode": str, "group_allowlist": list,
    "require_mention": (bool, dict), "exclude_chats": list, "instructions": str,
    "history_limit": int, "handoff_seconds": int, "image_enabled": bool,
    "voice_enabled": bool, "stt_command": list, "tts_command": list, "reply_mode": str,
}


def phone_number(value):
    if not isinstance(value, str):
        return ""
    number = value.strip().removeprefix("+").removesuffix("@s.whatsapp.net")
    return number if PHONE.fullmatch(number) else ""


def account_id(value):
    if not isinstance(value, str) or not ACCOUNT.fullmatch(value):
        raise ValueError("Use a lowercase account label containing letters, digits, hyphens, or underscores.")
    return value


def private_dir(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise ValueError("Plugin storage must be a directory owned by this user.")
    root.chmod(0o700)
    return root


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write_json(path, data):
    import tempfile

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def policy_patch(patch, *, local=False):
    if not isinstance(patch, dict):
        raise ValueError("Chatbot settings must be a JSON object.")
    for key, value in patch.items():
        if key not in POLICY_TYPES or not isinstance(value, POLICY_TYPES[key]):
            raise ValueError(f"Invalid setting: {key}")
        if key in {"dm_mode", "group_mode"} and value not in {"off", "allowlist", "all"}:
            raise ValueError(f"{key} must be off, allowlist, or all.")
        if key == "reply_mode" and value not in {"text", "voice", "match"}:
            raise ValueError("reply_mode must be text, voice, or match.")
        if key in {"history_limit", "handoff_seconds"} and (type(value) is not int or value < 0):
            raise ValueError(f"{key} must be a nonnegative integer.")
        if key in {"dm_allowlist", "group_allowlist", "exclude_chats"}:
            if not all(isinstance(entry, str) and (phone_number(entry) or JID.fullmatch(entry)) for entry in value):
                raise ValueError(f"{key} must contain WhatsApp phone numbers or chat IDs.")
        if key == "require_mention" and isinstance(value, dict):
            if not all(JID.fullmatch(chat) and chat.endswith("@g.us") and type(flag) is bool for chat, flag in value.items()):
                raise ValueError("require_mention mapping must be group IDs to booleans.")
        if key in {"stt_command", "tts_command"}:
            if not local:
                raise ValueError("Audio commands are configured locally through the plugin CLI.")
            if not all(isinstance(arg, str) and arg for arg in value):
                raise ValueError(f"{key} must be a list of command arguments.")
    return patch
