"""Hermes plugin entry point for WA Control & Live."""

import asyncio
from contextlib import closing
from functools import partial
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


STAGE = Path(__file__).resolve().parent / "spikes" / "003-whatsapp-call"


def _launcher():
    spec = importlib.util.spec_from_file_location("wa_control_live_launcher", STAGE / "launch.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher


def _parser(parser):
    commands = parser.add_subparsers(dest="action", required=True)
    setup = commands.add_parser("setup", help="Guide setup for the current Hermes profile")
    setup.add_argument("--sidecar", type=Path, help="Use a prepared macOS arm64 sidecar instead of the wizard")
    setup.add_argument("--sidecar-sha256", help="Expected SHA-256 for a local unified sidecar build")
    setup.add_argument("--reuse-pairing-from", type=Path, help="Private state of a stopped voice launcher")
    commands.add_parser("status", help="Check voice pairing and service status")
    commands.add_parser("list", help="List configured Live providers and pools")
    use = commands.add_parser("use", help="Choose a Live provider or pool")
    use.add_argument("selection")
    use.add_argument("timing", nargs="?", choices=("now",))
    commands.add_parser("pair", help="Link the voice device to this profile's WhatsApp bot")
    commands.add_parser("serve", help="Run the profile's WhatsApp service")
    cleanup = commands.add_parser("cleanup", help="Stop this profile's Control and Live services")
    cleanup.add_argument("--purge", action="store_true", help="Also remove this plugin's data and request device unlink")
    agent_job = commands.add_parser("agent-job", help="Read context or delegate to the owner's WhatsApp chat")
    agent_job.add_argument("--expected-home", type=Path, required=True, help="Active profile of the voice adapter")


def _setup(launcher, state, sidecar, binary, python, allowed_sha256, *, require_codex=True):
    sidecar_sha256 = hashlib.sha256(sidecar.read_bytes()).hexdigest() if sidecar.is_file() else None
    if sidecar_sha256 not in allowed_sha256:
        raise ValueError("The sidecar must match the expected checksum")
    if require_codex and shutil.which("codex") is None:
        raise RuntimeError("Install Codex CLI and sign in with ChatGPT before running voice setup")
    python312 = shutil.which("python3.12")
    if not python.is_file() and python312 is None:
        raise RuntimeError("Install Python 3.12 before running voice setup")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    with launcher.owned_transport({"private": str(state)}):
        if not python.is_file():
            subprocess.run([python312, "-I", "-m", "venv", str(python.parent.parent)], env=env, check=True)
        subprocess.run(
            [str(python), "-I", "-m", "pip", "install", "-r", str(STAGE / "requirements-lock.txt")],
            env=env,
            check=True,
        )
        with tempfile.TemporaryDirectory(dir=state) as directory:
            candidate = Path(directory) / binary.name
            shutil.copyfile(sidecar, candidate)
            candidate.chmod(0o700)
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != sidecar_sha256:
                raise ValueError("The source sidecar changed during setup")
            candidate.replace(binary)


def _reuse_pairing(launcher, config, previous):
    previous = previous.resolve()
    state = Path(config["private"])
    if previous == state or not (previous / "paired.json").is_file():
        raise RuntimeError("Choose the paired state of a separate voice launcher")
    previous_config = dict(config, private=str(previous))
    with launcher.owned_transport(previous_config), launcher.owned_transport(config):
        if (state / "paired.json").exists() or (state / "whatsapp").exists():
            raise RuntimeError("This plugin already has voice pairing state")
        launcher.verify_paired_identity(previous_config)
        source = previous / "whatsapp" / "whatsapp.db"
        with tempfile.TemporaryDirectory(dir=state) as directory:
            temporary = Path(directory)
            store = temporary / "whatsapp"
            store.mkdir(mode=0o700)
            destination = store / "whatsapp.db"
            with (
                closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old,
                closing(sqlite3.connect(destination)) as new,
            ):
                old.backup(new)
            destination.chmod(0o600)
            launcher.verify_paired_identity(dict(config, private=str(temporary)))
            marker = temporary / "paired.json"
            marker.write_text(json.dumps({"source": "verified voice device migration"}))
            marker.chmod(0o600)
            store.rename(state / "whatsapp")
            try:
                marker.rename(state / "paired.json")
            except OSError:
                (state / "whatsapp").rename(store)
                raise


def _serve_activation(home, ctx):
    """Track this installed copy and profile while its separate CLI process serves."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import get_hermes_home

    home = home.resolve()
    # Preserve the installed path so uninstalling a symlink also revokes serve.
    entrypoint = Path(__file__).absolute()
    identity = entrypoint.stat()
    names = {ctx.plugin_id, ctx.manifest.name}

    def is_active():
        try:
            current = entrypoint.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                return False
            if get_hermes_home().resolve() != home:
                return False
        except OSError:
            return False
        plugins = load_config_readonly().get("plugins")
        if not isinstance(plugins, dict):
            return False
        enabled, disabled = plugins.get("enabled"), plugins.get("disabled")
        if isinstance(disabled, list) and names.intersection(disabled):
            return False
        return isinstance(enabled, list) and bool(names.intersection(enabled))

    return is_active


def _configured_pair(ctx):
    get_config = getattr(ctx, "get_config", None)
    if get_config is None or get_config("unified_gateway", default=False) is not True:
        return None
    from .whatsapp_gateway import configured_pair

    return configured_pair(ctx)


def _live_enabled(ctx):
    get_config = getattr(ctx, "get_config", None)
    return get_config is None or get_config("live_enabled", default=True) is not False


def _serving_owner(ctx, home):
    from .plugin_identity import resolve_identity

    pair = _configured_pair(ctx)
    runtime = ctx.state.data_dir / "unified" if pair else ctx.state.data_dir
    serving = json.loads((runtime / "serving-identity.json").read_text())
    if not isinstance(serving, dict) or serving.get("hermes_home") != str(home):
        return
    owner, bot = pair or resolve_identity(home)
    if (owner, bot) != (serving.get("owner_number"), serving.get("bot_number")):
        return
    token = (runtime / "adapter.token").read_text().strip()
    if token:
        return owner, token


def _notify_voice_adapter(token, endpoint, session_id):
    with closing(http.client.HTTPConnection("127.0.0.1", 8797, timeout=2)) as conn:
        conn.request("POST", endpoint, json.dumps({"session_id": session_id}),
                     {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        conn.getresponse()


def _flush_owner_voice_history(ctx, session_key, source):
    from gateway.whatsapp_identity import canonical_whatsapp_identifier
    from hermes_constants import get_hermes_home, profile_name_for_home
    from hermes_state import SessionDB
    from .whatsapp_session import owner_session_key, resolve_owner_session

    home = get_hermes_home().resolve()
    profile = profile_name_for_home(home) or "default"
    if source.get("profile") not in (None, profile):
        return
    serving = _serving_owner(ctx, home)
    if serving is None:
        return
    owner, token = serving
    owner_id = canonical_whatsapp_identifier(owner)
    if not owner_id or any(canonical_whatsapp_identifier(source.get(key)) != owner_id
                           for key in ("chat_id", "user_id")):
        return
    if session_key != owner_session_key(home, owner):
        return
    target = resolve_owner_session(home, owner)
    with SessionDB(home / "state.db", read_only=True) as db:
        lineage = db.get_compression_lineage(target)
    if lineage:
        _notify_voice_adapter(token, "/flush-history", lineage[0])


def _notify_owner_voice_reset(ctx, old_session_id, new_session_id):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from .whatsapp_session import owner_session_key, resolve_owner_session

    home = get_hermes_home().resolve()
    serving = _serving_owner(ctx, home)
    if serving is None:
        return
    owner, token = serving
    target = resolve_owner_session(home, owner)
    route = owner_session_key(home, owner)
    with SessionDB(home / "state.db", read_only=True) as db:
        current_lineage = db.get_compression_lineage(target)
        if new_session_id not in current_lineage or old_session_id in current_lineage:
            return
        old = db.get_session(old_session_id)
        if not old or old.get("session_key") != route:
            return
        old_lineage = db.get_compression_lineage(old_session_id)
    if old_lineage:
        _notify_voice_adapter(token, "/session-reset", old_lineage[0])


async def _run_voice_hook(operation, *args):
    try:
        await asyncio.to_thread(operation, *args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, http.client.HTTPException):
        return


def _live_operation(ctx, state, launcher, action, *, selection=None, now=False):
    enabled = _live_enabled(ctx)
    if action == "status":
        report = launcher.status({"private": str(state)})
        report["data_dir"] = str(state)
        report["runtime_prepared"] = (state / "config.json").is_file()
        report["live_enabled"] = enabled
        return report
    if not enabled:
        if action == "list":
            return {"providers": [], "pools": [], "live_enabled": False}
        return {"status": "failed", "reason": "live_setup_deferred"}

    from .live_routing import LiveProviderPool

    settings = ctx.get_config("live")
    routing = LiveProviderPool(settings)
    if action == "list":
        active = launcher.status({"private": str(state)})["services"]["adapter"]
        return {"providers": list(routing.providers), "pools": list(routing.pools),
                "routing": active.get("routing") or routing.status(),
                "active_provider": active.get("active_provider")}
    routing.set_selected(selection)
    result = {"status": "selected", "selection": selection}
    if settings and (state / "serving-identity.json").is_file():
        result = launcher.select_live_provider(state, selection, now=now)
        if result["status"] not in ("selected", "switched"):
            return result
    if settings:
        ctx.set_config("live.selected", selection)
    return result


def _require_control_ownership():
    from hermes_cli.plugins_cmd import _find_plugin_entry
    from .whatsapp_control.settings import PLUGIN

    entry = _find_plugin_entry(PLUGIN)
    if entry and (not entry[4] or Path(str(entry[4])).resolve() != Path(__file__).resolve().parent):
        raise RuntimeError("The standalone WhatsApp Control installation shares this profile's Control data. "
                           "Resolve the shared ownership before cleanup.")


def _cleanup_live_runtime(ctx, *, purge):
    from hermes_constants import get_hermes_home
    from .whatsapp_outbound import remove_profile_call_alarms

    home = get_hermes_home()
    details = {}

    def remove_alarms():
        details["call_alarms"] = remove_profile_call_alarms(ctx, home)

    result = _launcher().cleanup_profile_runtime(ctx.state.data_dir, purge=purge, before_delete=remove_alarms)
    return {**result, **details}


def _run(ctx, args):
    from hermes_constants import get_hermes_home
    from .plugin_identity import resolve_identity

    home = get_hermes_home()
    if args.action == "cleanup":
        from .whatsapp_control.cli import cleanup
        from .whatsapp_control.runtime import runtime_path

        _require_control_ownership()
        cleanup(runtime_path(home), args.purge, cleanup_runtime=partial(_cleanup_live_runtime, ctx))
        return
    if args.action == "setup" and args.sidecar is None:
        if args.sidecar_sha256 or args.reuse_pairing_from:
            raise ValueError("--sidecar-sha256 and --reuse-pairing-from require --sidecar.")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Run setup in an interactive terminal, or pass --sidecar for manual setup.")
        from .setup_wizard import run_setup

        try:
            run_setup(ctx, home, _launcher())
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("Setup cancelled.") from None
        return
    pair = _configured_pair(ctx)
    if args.action == "agent-job":
        if args.expected_home.resolve() != home.resolve():
            raise RuntimeError("Voice job requires the same Hermes profile as the call")
        if not _serve_activation(home, ctx)():
            raise RuntimeError("WA Control & Live plugin is inactive in this Hermes profile")
        from .live_agent import run_cli
        from .whatsapp_session import resolve_owner_session

        owner, bot = pair or resolve_identity(home)
        if (owner, bot) != (os.environ.get("WA_CONTROL_LIVE_OWNER"), os.environ.get("WA_CONTROL_LIVE_BOT")):
            raise RuntimeError("WhatsApp identity changed. Restart the WhatsApp voice service.")
        options = {}
        get_config = getattr(ctx, "get_config", None)
        if get_config is not None:
            options['get_live_config'] = partial(get_config, "live")
        if pair:
            from .whatsapp_owner_authorization import record_live_submission
            from .whatsapp_gateway import voice_routing_reader, voice_session_creator

            options['ensure_session'] = voice_session_creator(ctx)
            options['read_routing'] = voice_routing_reader(ctx)
            options['sign_submission'] = partial(record_live_submission, ctx.state.data_dir / 'unified')
        run_cli(home, owner, resolve=resolve_owner_session, llm=ctx.llm, **options)
        return

    launcher = _launcher()
    state = ctx.state.data_dir / "unified" if pair else ctx.state.data_dir
    if args.action in ("list", "status", "use"):
        result = _live_operation(ctx, state, launcher, args.action,
                                 selection=getattr(args, "selection", None),
                                 now=getattr(args, "timing", None) == "now")
        print(json.dumps(result, indent=2))
        if args.action == "use" and result["status"] not in ("selected", "switched"):
            raise SystemExit(1)
        return

    owner, bot = pair or resolve_identity(home)
    binary = state / "hermes-wa-voice"
    python = state / "venv" / "bin" / "python"
    candidate_sha256 = None
    if args.action == "setup":
        if pair and args.reuse_pairing_from is not None:
            raise ValueError("Pair the bot with the unified plugin's own WhatsApp device.")
        candidate_sha256 = args.sidecar_sha256
        if pair and not candidate_sha256:
            raise ValueError("Pass --sidecar-sha256 for the candidate unified build.")
        if pair:
            from .whatsapp_control.cli import prepare

            prepare(__file__)
        allowed = launcher.profile_sidecar_checksums(state, bool(pair), candidate_sha256)
        settings = ctx.get_config("live") if hasattr(ctx, "get_config") else None
        if settings:
            from .live_routing import LiveProviderPool

            LiveProviderPool(settings)
            _setup(launcher, state, args.sidecar, binary, python, allowed, require_codex=False)
        else:
            _setup(launcher, state, args.sidecar, binary, python, allowed)
    elif not (state / "config.json").is_file():
        raise RuntimeError("Run hermes wa-control-live setup first")
    config = launcher.prepare_profile(
        owner=owner, bot=bot, private=state, binary=binary, python=python, hermes_home=home,
        **({"unified_gateway": True, "sidecar_sha256": candidate_sha256} if pair else {}),
    )
    if args.action == "pair":
        launcher.pair(config)
    elif args.action == "serve":
        options = {}
        if not _live_enabled(ctx):
            options["live_enabled"] = False
        settings = ctx.get_config("live") if hasattr(ctx, "get_config") else None
        if settings:
            from .live_routing import LiveProviderPool

            LiveProviderPool(settings)
            options["live_settings"] = settings
        if pair:
            import hermes_constants
            from .whatsapp_control.speech import _command

            options["control_command"] = _command(Path(hermes_constants.__file__).resolve(), home, "serve")
        launcher.serve(config, is_active=_serve_activation(home, ctx), agent_jobs=True, **options)
    else:
        if args.reuse_pairing_from is not None:
            _reuse_pairing(launcher, config, args.reuse_pairing_from)
        print("Voice runtime ready. Use hermes wa-control-live status to check pairing.")


def register(ctx):
    from .live_agent import CALL_INTENT_TASK
    from .live_commands import PROVIDER_SCHEMA, PROVIDER_TOOL, provider_tool_handler, walive_handler
    from .whatsapp_control import register as register_control

    cleanup_options = {"cleanup_runtime": partial(_cleanup_live_runtime, ctx),
                       "cleanup_guard": _require_control_ownership}
    if pair := _configured_pair(ctx):
        from hermes_constants import get_hermes_home
        from .whatsapp_owner_authorization import owner_send_request
        from .whatsapp_gateway import register_gateway

        register_gateway(ctx, *pair)
        owner, bot = pair
        runtime = ctx.state.data_dir / "unified"

        def authenticated_request(session_id):
            return owner_send_request(get_hermes_home().resolve(), owner, session_id, runtime,
                                      allow_empty=True)

        register_control(ctx, entry=__file__, authenticated_request=authenticated_request,
                         cli_options={"bot_runtime": runtime, "owner_number": owner, "bot_number": bot,
                                      **cleanup_options},
                         toolset="wa-control-live")
    else:
        register_control(ctx, entry=__file__, cli_options=cleanup_options,
                         toolset="wa-control-live")

    ctx.register_auxiliary_task(
        CALL_INTENT_TASK, display_name='WA Control & Live call intent',
        description='Distinguish a spoken reply from work requiring Hermes.',
        defaults={'reasoning_effort': 'none'},
    )

    def handle(args):
        try:
            _run(ctx, args)
        except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
            raise SystemExit(str(exc)) from exc

    ctx.register_cli_command(
        name="wa-control-live",
        help="Set up, pair and run WA Control & Live",
        setup_fn=_parser,
        handler_fn=handle,
    )
    if register_command := getattr(ctx, "register_command", None):
        register_command("walive", walive_handler(ctx), description="Atur provider audio WhatsApp Live",
                         args_hint="[status | list | use <alias> [now]]")
    ctx.register_tool(name=PROVIDER_TOOL, toolset="wa-control-live", schema=PROVIDER_SCHEMA,
                      handler=provider_tool_handler(ctx), description=PROVIDER_SCHEMA["description"], is_async=True)
    from .whatsapp_outbound import DESCRIPTION, SCHEMA, TOOL_NAME, call_now, call_now_hint

    def dial(args, *, session_id=None):
        return json.dumps(call_now(ctx, args, session_id=session_id), ensure_ascii=False)

    async def flush_voice_history(*, platform, session_key, source, **_):
        if (platform != "whatsapp" or not isinstance(source, dict)
                or source.get("chat_type") != "dm" or source.get("thread_id")):
            return
        # This hook runs before Hermes admits the next DM, so its native turn can read
        # a voice exchange that already finished. The next DM continues if voice is unavailable.
        await _run_voice_hook(_flush_owner_voice_history, ctx, session_key, source)

    def reset_voice_session(*, platform=None, old_session_id=None, new_session_id=None, **_):
        if (platform != "whatsapp" or not isinstance(old_session_id, str) or not old_session_id
                or not isinstance(new_session_id, str) or not new_session_id):
            return
        # /new invokes this hook on the gateway loop. The adapter must be able to use that
        # loop to resume the new route while the notification is in flight.
        ctx.spawn_task(
            _run_voice_hook(_notify_owner_voice_reset, ctx, old_session_id, new_session_id)
        )

    ctx.register_tool(name=TOOL_NAME, toolset="wa-control-live", schema=SCHEMA,
                      handler=dial, description=DESCRIPTION)
    ctx.register_hook("pre_llm_call", call_now_hint)
    ctx.register_hook("post_gateway_admission", flush_voice_history)
    ctx.register_hook("on_session_reset", reset_voice_session)
    ctx.register_skill(
        name="call-now", path=Path(__file__).with_name("skills") / "call-now" / "SKILL.md",
        description="Use when the WhatsApp owner asks the assistant in a DM to call now or at a later time.",
        frontmatter={"metadata": {"hermes": {
            "session_platforms": ["whatsapp"], "requires_toolsets": ["wa-control-live"],
        }}},
    )
