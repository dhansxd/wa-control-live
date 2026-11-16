"""Bind the linked bot's pairing proof to the Hermes WhatsApp platform."""

from pathlib import Path
from tempfile import TemporaryDirectory


def register_whatsapp_platform(ctx, adapter_factory, token_path):
    # Stock Hermes probes session_path/creds.json before creating or retrying
    # the WhatsApp adapter. Give that check this registration's pairing proof.
    reference = TemporaryDirectory(prefix="hermes-wa-control-live-")
    try:
        (Path(reference.name) / "creds.json").symlink_to(token_path.with_name("paired.json"))
        registration = ctx.register_platform(
            name="whatsapp", label="WhatsApp", allowed_users_env="WHATSAPP_ALLOWED_USERS",
            adapter_factory=adapter_factory, check_fn=token_path.is_file,
            env_enablement_fn=lambda: {"session_path": reference.name},
        )
        if registration is None:
            raise RuntimeError("Hermes could not register the unified WhatsApp platform.")
        # Unload disposes this callback before removing the platform registration.
        ctx.on_unload(reference.cleanup)
    except BaseException:
        reference.cleanup()
        raise
