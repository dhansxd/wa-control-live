"""Guide first-time setup in the active Hermes profile."""

import json
import shutil
import subprocess

from .plugin_identity import StockBotNotPaired, resolve_setup_identity
from .setup_live import choose_live
from .whatsapp_control.cli import prepare as prepare_control
from .whatsapp_control.settings import account_id, hermes_command, phone_number, private_dir, read_json


def _phone(ask, label, current):
    prompt = f"{label} [{current}]: " if current else f"{label} (with country code): "
    value = ask(prompt).strip() or current
    number = phone_number(value)
    if not 7 <= len(number) <= 15:
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


def pair_bot(launcher, config, *, tell=print):
    tell("On the Hermes bot's phone, open WhatsApp > Linked Devices > Link a Device.")
    tell("Scan the Live QR below using that account. This link also serves the bot's DM and Control.")
    launcher.pair(config)


def run_setup(ctx, home, launcher, *, stock_profile=None, sidecar=None, sidecar_sha256=None, pair_now=False,
              ask=input, tell=print):
    from . import _bundled_runtime, _setup

    state = ctx.state.data_dir / "unified"
    current = read_json(state / "config.json")
    paired = (state / "paired.json").is_file()
    old_owner = ctx.get_config("owner_number", default="") or current.get("owner_number", "")
    old_bot = ctx.get_config("bot_number", default="") or current.get("bot_number", "")
    stock_profile = stock_profile or ctx.get_config("stock_profile", default="") or None
    tell(f"Set up WA Control & Live in Hermes profile: {home}")
    try:
        bot, suggested_owner = resolve_setup_identity(home, stock_profile)
        tell("Detected the bot from the existing Hermes WhatsApp pairing.")
    except StockBotNotPaired:
        if stock_profile is not None:
            raise
        bot, suggested_owner = None, None
    if not bot:
        bot = _phone(ask, "Hermes bot WhatsApp number", old_bot)
    if paired and bot != current.get("bot_number"):
        raise ValueError("This profile is already linked to another bot. Use a different Hermes profile or unlink it first.")
    owner = None
    if not old_owner and suggested_owner:
        answer = ask(
            f"Authorize the existing WhatsApp contact ending in {suggested_owner[-4:]} "
            "as Live caller and DM owner? [y/N]: "
        )
        if answer.strip().lower() in {"y", "yes"}:
            owner = suggested_owner
    if owner is None:
        owner = _phone(ask, "Authorized Live caller and DM owner", old_owner)
    if owner == bot:
        raise ValueError("The owner and Hermes bot must be different WhatsApp numbers.")

    settings = ctx.get_config("live")
    secrets = {}
    if pair_now and settings is None and shutil.which("codex"):
        settings = {"selected": "codex", "providers": {"codex": {"kind": "codex"}}}
        tell("Live audio will use the Codex CLI sign-in.")
    elif not pair_now or settings is None:
        settings, secrets = choose_live(ctx, ask=ask, tell=tell)
    additional = []
    if not pair_now:
        labels = ask("Additional Control account labels (e.g. personal, Enter to skip): ")
        additional = list(dict.fromkeys(account_id(label.strip()) for label in labels.split(",") if label.strip()))
    if pair_now:
        link_now = not paired
    elif not paired:
        answer = ask("Show the Hermes bot's Live pairing QR now? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes", "n", "no"}:
            raise ValueError("Enter yes or no to choose when to link the bot.")
        link_now = answer not in {"n", "no"}
    else:
        link_now = False

    values, save_profile_env = _profile_env(owner, secrets)
    prepare_control(__file__)
    private_dir(state)
    if sidecar is None:
        runtime = _bundled_runtime()
    else:
        binary = state / "hermes-wa-voice"
        python = state / "venv" / "bin" / "python"
        checksum = sidecar_sha256
        allowed = launcher.profile_sidecar_checksums(state, True, checksum)
        _setup(launcher, state, sidecar, binary, python, allowed, require_codex=False)
        runtime = {"binary": binary, "python": python, "sidecar_sha256": checksum}

    config = launcher.prepare_profile(owner=owner, bot=bot, private=state, hermes_home=home,
                                      unified_gateway=True, **runtime)
    if paired:
        launcher.verify_paired_identity(config)
        tell("Existing bot link verified.")
    elif link_now:
        try:
            pair_bot(launcher, config, tell=tell)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, KeyboardInterrupt):
            retry = "pair" if pair_now else "setup"
            tell(f"To retry bot pairing, run: {hermes_command(home, 'wa-control-live', retry)}")
            raise
        paired = True
        tell("Hermes bot link verified.")

    for name, value in values.items():
        save_profile_env(name, value)
    if settings is not None and settings != ctx.get_config("live"):
        ctx.set_config("live", settings)
    ctx.set_config("owner_number", owner)
    ctx.set_config("bot_number", bot)
    ctx.set_config("live_enabled", settings is not None)
    ctx.set_config("unified_gateway", True)

    if not paired:
        tell(f"Show the bot's Live pairing QR when ready: {hermes_command(home, 'wa-control-live', 'pair')}")
    if sidecar is None:
        tell("The plugin runs with this profile's Hermes gateway. Follow Hermes' gateway restart guidance if needed.")
    else:
        tell(f"Start the manual runtime with: {hermes_command(home, 'wa-control-live', 'serve')}")
    if stock_profile and stock_profile != home.name and home.parent.name == "profiles":
        tell("If the source profile still handles this bot, switch its WhatsApp routing before starting this profile's gateway.")
    if settings is None:
        tell(f"Live audio setup deferred. Configure it with: {hermes_command(home, 'wa-control-live', 'setup')}")
    tell("To show an additional Control QR once the gateway is running, choose an account label and run:")
    for label in additional or ["personal"]:
        tell(f"  {hermes_command(home, 'whatsapp-control', 'pair', label)}")
