"""Collect Live settings before the setup wizard writes to the active profile."""

import getpass
import re
import shutil

from .live_routing import LiveProviderPool, _ALIAS, _ENV_NAME, _websocket_endpoint


_LABELS = {
    "codex": "Codex CLI sign-in",
    "hermes-realtime": "OpenAI Realtime through the Hermes Codex sign-in",
    "gemini": "Gemini Live",
    "gpt-live": "OpenAI GPT-Live",
    "custom-live": "Custom WebSocket compatible with GPT-Live",
}
_PROFILE_KEYS = {"gemini": "GEMINI_API_KEY", "gpt-live": "OPENAI_API_KEY"}


def _provider(reuse, ask, tell):
    choices = ("hermes-realtime", "gemini", "gpt-live") if reuse else _LABELS
    menu = {str(index): kind for index, kind in enumerate(choices, start=1)}
    for number, kind in menu.items():
        label = _LABELS[kind]
        if reuse and kind in _PROFILE_KEYS:
            label += f" ({_PROFILE_KEYS[kind]} in this profile)"
        tell(f"  {number}. {label}")
    while (selection := ask("Live provider: ").strip()) not in menu:
        tell("Choose one of the listed provider numbers.")
    return menu[selection]


def _key(alias, reuse, reserved, ask, tell, read_secret):
    from hermes_cli.config import get_env_value

    reference = reuse if reuse else ask("Existing profile secret name [Enter to add a key]: ").strip()
    if reference:
        if not _ENV_NAME.fullmatch(reference):
            raise ValueError("Enter a valid profile secret variable name.")
        if not get_env_value(reference):
            tell(f"Set {reference} in this Hermes profile before using Live audio.")
        return reference, {}

    stem = "WA_CONTROL_LIVE_" + re.sub(r"[^A-Z0-9_]", "_", alias.upper())
    reference = stem + "_KEY"
    suffix = 2
    while reference in reserved or get_env_value(reference) is not None:
        reference = f"{stem}_{suffix}_KEY"
        suffix += 1
    secret = read_secret(f"New API key for {reference} [Enter to add it later]: ").strip()
    if not secret:
        tell(f"Set {reference} in this Hermes profile before using Live audio.")
        return reference, {}
    if not all("!" <= character <= "~" for character in secret):
        raise ValueError("Live API keys must contain printable ASCII without spaces.")
    return reference, {reference: secret}


def choose_live(ctx, *, ask=input, tell=print, read_secret=getpass.getpass):
    """Return Live settings and pending secret writes for the active Hermes profile."""
    current = ctx.get_config("live")
    existing = LiveProviderPool(current) if current is not None else None
    default = "keep existing settings" if current else "set up later"
    while (mode := ask(f"Live setup [1 reuse Hermes credentials, 2 manual, Enter {default}]: ").strip()) not in (
        "", "1", "2"
    ):
        tell("Choose 1, 2, or press Enter.")
    if not mode:
        return current or None, {}

    reuse = mode == "1"
    choice = _provider(reuse, ask, tell)
    if choice == "codex" and shutil.which("codex") is None:
        raise ValueError("Install Codex CLI and sign in with ChatGPT to use this Live provider.")
    if choice == "hermes-realtime":
        tell("This route uses the profile's OpenAI Codex sign-in. Sign in with hermes auth add openai-codex.")

    settings = dict(current or {})
    providers = dict(settings.get("providers", {}))
    aliases = providers.keys() | settings.get("pools", {}).keys()
    alias = choice
    suffix = 2
    while alias in aliases:
        alias = f"{choice}-{suffix}"
        suffix += 1
    alias = ask(f"New Live provider alias [{alias}]: ").strip() or alias
    if not _ALIAS.fullmatch(alias) or alias in aliases:
        raise ValueError("Enter an unused Live provider alias with letters, digits, dots, underscores or hyphens.")

    provider = {"kind": "gpt-live" if choice == "custom-live" else choice}
    if choice == "custom-live":
        provider["endpoint"] = _websocket_endpoint(ask("Gateway WebSocket URL without credentials: ").strip())
    if choice != "codex" and (model := ask("Live model ID [provider default]: ").strip()):
        provider["model"] = model
    if voice := ask("Voice [provider default]: ").strip():
        provider["voice"] = voice

    secrets = {}
    if provider["kind"] in _PROFILE_KEYS:
        reserved = {
            account.secret_env for accounts in existing.providers.values() for account in accounts.values()
        } if existing is not None else set()
        reference, secrets = _key(alias, _PROFILE_KEYS.get(choice) if reuse else None,
                                  reserved, ask, tell, read_secret)
        provider["accounts"] = {"default": {"secret_env": reference}}

    providers[alias] = provider
    settings.update(selected=alias, providers=providers)
    LiveProviderPool(settings)
    return settings, secrets
