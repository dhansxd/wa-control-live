"""Guide first-time setup in the active Hermes profile."""

import hashlib
import json
import os
import shlex

from .setup_live import choose_live
from .setup_sidecar import build_sidecar, source_checksum
from .whatsapp_control.cli import prepare as prepare_control
from .whatsapp_control.settings import account_id, phone_number, private_dir, read_json, write_json


def _phone(ask, label, current):
    prompt = f"{label} [{current}]: " if current else f"{label} (with country code): "
    value = ask(prompt).strip() or current
    number = phone_number(value)
    if len(number) < 7:
        raise ValueError("Enter each WhatsApp number in full international format.")
    return number


def _profile_env(owner, secrets):
    from hermes_cli.config import get_env_value_prefer_dotenv, require_env_writable, save_env_value

    key = "WHATSAPP_ALLOWED_USERS"
    raw = get_env_value_prefer_dotenv(key) or ""
    allowed = json.loads(raw) if raw.lstrip().startswith("[") else raw.split(",")
    if not isinstance(allowed, list) or any(not isinstance(value, str) for value in allowed):
        raise ValueError("Fix WHATSAPP_ALLOWED_USERS in this Hermes profile before setup.")
    allowed = [value.strip() for value in allowed if value.strip()]
    values = dict(secrets)
    if "*" not in allowed and owner not in {phone_number(value) for value in allowed}:
        values[key] = ",".join([*allowed, owner])
    for name in values:
        require_env_writable(name, "set")
    return values, save_env_value


def run_setup(ctx, home, launcher, *, ask=input, tell=print):
    from . import STAGE, _setup

    state = ctx.state.data_dir / "unified"
    current = read_json(state / "config.json")
    paired = (state / "paired.json").is_file()
    old_owner = ctx.get_config("owner_number", default="") or current.get("owner_number", "")
    old_bot = ctx.get_config("bot_number", default="") or current.get("bot_number", "")
    tell(f"Set up WA Control & Live in Hermes profile: {home}")
    owner = _phone(ask, "Owner WhatsApp number", old_owner)
    bot = _phone(ask, "Hermes bot WhatsApp number", old_bot)
    if owner == bot:
        raise ValueError("The owner and Hermes bot must be different WhatsApp numbers.")
    if paired and bot != current.get("bot_number"):
        raise ValueError("This profile is already linked to another bot. Use a different Hermes profile or unlink it first.")

    settings, secrets = choose_live(ctx, ask=ask, tell=tell)
    labels = ask("Additional Control account labels to link after setup (comma-separated, Enter to skip): ")
    additional = list(dict.fromkeys(account_id(label.strip()) for label in labels.split(",") if label.strip()))
    if not paired:
        answer = ask("Link the bot now in this terminal? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes", "n", "no"}:
            raise ValueError("Enter yes or no to choose when to link the bot.")
        link_now = answer not in {"n", "no"}
    else:
        link_now = False

    values, save_profile_env = _profile_env(owner, secrets)
    prepare_control(__file__)
    private_dir(state)
    binary = state / "hermes-wa-voice"
    python = state / "venv" / "bin" / "python"
    source = STAGE / "source" / "hermes-wa-voice-opus-media-1"
    source_sha = source_checksum(source)
    build_record = state / "sidecar-build.json"
    checksum = current.get("binary_sha256")
    reusable = (read_json(build_record) == {"source": source_sha, "binary": checksum}
                and checksum and binary.is_file() and os.access(binary, os.X_OK) and python.is_file()
                and os.access(python, os.X_OK) and hashlib.sha256(binary.read_bytes()).hexdigest() == checksum)
    if reusable:
        tell("Using the sidecar already prepared for this profile.")
    else:
        with build_sidecar(source, state=state) as sidecar:
            if source_checksum(source) != source_sha:
                raise RuntimeError("The installed plugin source changed during setup. Run setup again.")
            checksum = hashlib.sha256(sidecar.read_bytes()).hexdigest()
            allowed = launcher.profile_sidecar_checksums(state, True, checksum)
            _setup(launcher, state, sidecar, binary, python, allowed, require_codex=False)

    config = launcher.prepare_profile(owner=owner, bot=bot, private=state, binary=binary, python=python,
                                      hermes_home=home, unified_gateway=True, sidecar_sha256=checksum)
    if not reusable:
        write_json(build_record, {"source": source_sha, "binary": checksum})
    if paired:
        launcher.verify_paired_identity(config)
        tell("Existing bot link verified.")
    elif link_now:
        launcher.pair(config)
        paired = True

    for name, value in values.items():
        save_profile_env(name, value)
    if settings is not None and settings != ctx.get_config("live"):
        ctx.set_config("live", settings)
    ctx.set_config("owner_number", owner)
    ctx.set_config("bot_number", bot)
    ctx.set_config("live_enabled", settings is not None)
    ctx.set_config("unified_gateway", True)

    prefix = f"hermes -p {shlex.quote(home.name)}" if home.parent.name == "profiles" else "hermes -p default"
    if not paired:
        tell(f"Link the bot when ready: {prefix} wa-control-live pair")
    tell(f"Start the plugin service: {prefix} wa-control-live serve")
    tell("Start or reload the Hermes gateway for this profile to use the linked WhatsApp bot.")
    if settings is None:
        tell(f"Live setup deferred. Run {prefix} wa-control-live setup when ready.")
    if additional:
        tell("With the plugin service running, link each additional account in another terminal:")
        for label in additional:
            tell(f"  {prefix} whatsapp-control pair {label}")
