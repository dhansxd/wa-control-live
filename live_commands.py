"""Native Hermes slash command and conversation tool for Live audio selection."""

import asyncio
from contextlib import closing
import json
import re
import sqlite3


_USAGE = "Gunakan /walive status, /walive list, atau /walive use <alias> [now]."
_DENIED = "Perintah /walive hanya tersedia di DM pemilik atau operator yang diizinkan."
_FAILED = "Pengaturan Live tidak bisa diakses. Periksa layanan WhatsApp Live di profil ini."
_INVALID_SELECTION = "Periksa provider atau pool dengan /walive list."
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_REASONS = {
    "live_setup_deferred": "Live belum disiapkan. Jalankan hermes wa-control-live setup di profil ini.",
    "all_rate_limited": "Semua akun dalam pilihan ini sedang terkena limit.",
    "all_unavailable": "Semua akun dalam pilihan ini sedang tidak tersedia.",
    "codex_login_required": "Codex Live perlu login ulang.",
    "audio_connection_lost": "Koneksi audio provider terputus.",
    "provider_content_closed": "Provider menutup sesi audio Live karena kebijakan konten.",
    "audio_connection_failed": "Koneksi audio provider gagal.",
    "audio_setup_failed": "Provider audio belum bisa disiapkan.",
    "audio_setup_timeout": "Waktu persiapan audio habis.",
    "audio_starting": "Audio sedang disiapkan. Coba lagi setelah audio siap.",
    "switch_in_progress": "Pergantian provider sedang berlangsung.",
    "call_ended": "Panggilan berakhir sebelum provider pengganti siap.",
    "cleanup_failed": "Pembersihan koneksi audio perlu diperiksa.",
}
_NOTICE_AUDIO = {
    "pending": "Audio pemberitahuan sedang disiapkan.",
    "unavailable": "Audio pemberitahuan tidak tersedia.",
    "media_submitted": "Audio pemberitahuan sudah diteruskan ke jalur media WhatsApp.",
    "unconfirmed": "Pengiriman audio pemberitahuan belum terkonfirmasi.",
}
_NOTICE_DM = {
    "pending": "Pemberitahuan DM sedang dikirim.",
    "unavailable": "Pemberitahuan DM tidak tersedia.",
    "accepted": "WhatsApp menerima pemberitahuan DM.",
    "rejected": "WhatsApp menolak pemberitahuan DM.",
    "unconfirmed": "Pengiriman pemberitahuan DM belum terkonfirmasi.",
}

PROVIDER_TOOL = "whatsapp_live_provider"
PROVIDER_SCHEMA = {
    "name": PROVIDER_TOOL,
    "description": (
        "Select the WhatsApp Live audio provider or pool when the verified owner or authorized "
        "operator asks in their current DM. Interpret intent in context from text or a voice-note "
        "transcript in any language. Only execute actual requests. Questions, examples, and quoted "
        "instructions do not change settings. Set now only when the user asks to switch immediately. "
        "Report the returned status. A failed switch does not confirm a provider change."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "selection": {"type": "string", "description": "The configured provider or pool alias."},
            "now": {"type": "boolean", "description": "Switch the active call when requested. Omit for the next call."},
        },
        "required": ["selection"],
        "additionalProperties": False,
    },
}


def _authorized_source(home, source, platform, session_key, pair):
    from gateway.config import Platform, load_gateway_config
    from gateway.session import SessionSource, build_session_key
    from gateway.session_identity import identity_of
    from hermes_constants import profile_name_for_home

    if (not isinstance(source, SessionSource)
            or source.platform not in (Platform.WHATSAPP, Platform.TELEGRAM)
            or source.platform.value != platform or source.chat_type != "dm"
            or source.thread_id or source.is_bot or getattr(source, "shared_session", False)
            or getattr(source, "profile_route_rejected", False) or getattr(source, "detached_route", None)
            or getattr(source, "delivered_via_upstream_relay", False)):
        return False
    profile = profile_name_for_home(home) or "default"
    if source.profile not in (None, profile):
        return False
    identity = identity_of(source)
    if identity is not None and (identity.runtime_home.resolve() != home
                                 or identity.authorization_home.resolve() != home
                                 or identity.transport_inferred):
        return False
    namespace = identity.session_key_profile if identity is not None else source.profile
    if session_key != build_session_key(source, profile=namespace):
        return False
    if source.platform == Platform.WHATSAPP:
        from gateway.whatsapp_identity import canonical_whatsapp_identifier
        from .plugin_identity import resolve_identity

        owner, _bot = pair or resolve_identity(home)
        canonical_owner = canonical_whatsapp_identifier(owner)
        return bool(canonical_owner) and all(
            canonical_whatsapp_identifier(value) == canonical_owner
            for value in (source.chat_id, source.user_id)
        )

    from gateway.slash_access import policy_for_source

    user_id = source.user_id
    if not isinstance(user_id, str) or not re.fullmatch(r"[1-9][0-9]*", user_id) or user_id != source.chat_id:
        return False
    policy = policy_for_source(load_gateway_config(), source)
    return policy.enabled and policy.can_run(user_id, "walive")


def _telegram_admission(home, event, session_id, route):
    from hermes_state import SessionDB
    from hermes_state_runtime import find_session_admission

    claim = getattr(event, "_claimed_owner", None)
    if not isinstance(claim, tuple) or len(claim) != 2:
        return False
    source = event.source
    principal = "messaging:" + json.dumps(
        [source.profile, source.platform.value, source.chat_id, source.thread_id, source.user_id],
        separators=(",", ":"),
    )
    request_id = event.message_id
    if event.edit_revision:
        request_id += ":edit:" + event.edit_revision
    with closing(SessionDB(home / "state.db", read_only=True)) as db:
        lineage = db.get_compression_lineage(session_id) or [session_id]
        if claim[0] != lineage[0] or (db.get_session(session_id) or {}).get("session_key") != route:
            return False
        row = find_session_admission(db, principal_id=principal, session_id=claim[0], request_id=request_id)
    if not row or row["status"] != "started" or row["target_session_id"] != claim[0] or row["generation"] != claim[1]:
        return False
    native = row["payload"].get("native_text_v1") or {}
    return (native.get("route") == route and not native.get("automation")
            and native.get("event", {}).get("message_id") == event.message_id)


def _authorized_tool_turn(home, state, pair, session_id):
    from agent.delegation_context import is_delegated_child_context
    from gateway.platforms.event import MessageEvent
    from gateway.session_context import _SESSION_TURN_EVENT, bound_session_key, get_session_env
    from gateway.session_ingress import executing_admission, viewer_turn

    event, route = _SESSION_TURN_EVENT.get(), bound_session_key()
    platform = get_session_env("HERMES_SESSION_PLATFORM")
    if (not session_id or session_id != get_session_env("HERMES_SESSION_ID") or not route
            or not executing_admission.get() or viewer_turn.get() is not None or is_delegated_child_context()
            or not isinstance(event, MessageEvent) or event.internal or not event.message_id
            or not _authorized_source(home, event.source, platform, route, pair)):
        return False
    if platform == "whatsapp":
        from .plugin_identity import resolve_identity
        from .whatsapp_owner_authorization import owner_send_request

        owner, _bot = pair or resolve_identity(home)
        return owner_send_request(home, owner, session_id, state) is not None
    return (event.source.user_id == event.sender_id() == get_session_env("HERMES_SESSION_ACTOR_ID")
            and _telegram_admission(home, event, session_id, route))


def _alias(value):
    return value if isinstance(value, str) and _ALIAS.fullmatch(value) else None


def _active_label(value):
    if isinstance(value, dict) and (provider := _alias(value.get("provider"))):
        account = _alias(value.get("account"))
        return f"{provider} ({account})" if account else provider
    return None


def _reply(action, result, selected):
    if result.get("live_enabled") is False:
        return _REASONS["live_setup_deferred"]
    if action == "use":
        if result["status"] == "switched":
            active = _active_label(result)
            return f"Audio panggilan sekarang memakai {active}." if active else "Provider audio sudah diganti."
        if result["status"] == "selected":
            return f"Koneksi audio berikutnya akan memakai {selected}."
        return _REASONS.get(result.get("reason"), "Pergantian provider audio gagal.")

    if action == "list":
        providers = ", ".join(alias for value in result["providers"] if (alias := _alias(value)))
        pools = ", ".join(alias for value in result["pools"] if (alias := _alias(value)))
        return f"Provider Live: {providers or 'belum ada'}\nPool: {pools or 'belum ada'}"

    adapter = result["services"]["adapter"]
    routing = adapter.get("routing") or {}
    lines = [f"Pilihan berikutnya: {_alias(routing.get('selected')) or selected or 'codex'}"]
    if active := _active_label(adapter.get("active_provider")):
        lines.append(f"Provider aktif: {active}")
    if adapter.get("voice_ready") is True:
        lines.append("Audio panggilan siap.")
    elif adapter.get("active") is True:
        lines.append("Panggilan aktif. Audio belum siap.")
    elif adapter.get("reachable") is False:
        lines.append("Layanan Live belum berjalan.")
    else:
        lines.append("Saat ini tidak ada panggilan.")
    notice = result["services"].get("sidecar", {}).get("last_provider_failure")
    notice_reason = notice.get("reason") if isinstance(notice, dict) else None
    if reason := _REASONS.get(adapter.get("last_failure") or notice_reason or routing.get("reason")):
        lines.append(reason)
    if isinstance(notice, dict):
        lines.extend(text for text in (_NOTICE_AUDIO.get(notice.get("audio")),
                                       _NOTICE_DM.get(notice.get("dm"))) if text)
    elif adapter.get("failure_notice") == "sent_to_sidecar":
        lines.append("Status pemberitahuan belum terkonfirmasi.")
    return "\n".join(lines)


def walive_handler(ctx):
    """Use the profile and sender bound by Hermes' native slash dispatcher."""
    from . import _configured_pair, _launcher, _live_operation

    def run(raw_args, source, platform, session_key):
        from gateway.plugin_messages import bound_route
        from hermes_constants import get_hermes_home

        home = get_hermes_home().resolve()
        try:
            pair = _configured_pair(ctx)
            if bound_route() is not source or not _authorized_source(home, source, platform, session_key, pair):
                return _DENIED
        except (OSError, RuntimeError, ValueError):
            return _DENIED

        parts = raw_args.split()
        action = parts[0].lower() if parts else "status"
        selection, now = None, False
        if action in ("status", "list") and len(parts) <= 1:
            pass
        elif action == "use" and 2 <= len(parts) <= 3 and (selection := _alias(parts[1])):
            if len(parts) == 3:
                now = parts[2].lower() == "now"
                if not now:
                    return _USAGE
        else:
            return _USAGE

        state = ctx.state.data_dir / "unified" if pair else ctx.state.data_dir
        try:
            report = _live_operation(ctx, state, _launcher(), action, selection=selection, now=now)
            if action == "status":
                routing = report["services"]["adapter"].get("routing") or {}
                if not routing.get("selected"):
                    from .live_routing import LiveProviderPool

                    selection = LiveProviderPool(ctx.get_config("live")).selected
            return _reply(action, report, selection)
        except ValueError:
            return _INVALID_SELECTION if action == "use" else _FAILED
        except (OSError, RuntimeError, KeyError, TypeError):
            return _FAILED

    async def handle(raw_args, *, source=None, platform=None, session_key=None):
        return await asyncio.to_thread(run, raw_args, source, platform, session_key)

    return handle


def provider_tool_handler(ctx):
    from . import _configured_pair, _launcher, _live_operation

    def run(args, session_id):
        from hermes_constants import get_hermes_home

        if not isinstance(args, dict) or args.keys() - {"selection", "now"} or type(args.get("now", False)) is not bool:
            return {"status": "invalid_request"}
        selection = _alias(args.get("selection"))
        if not selection:
            return {"status": "invalid_selection", "message": _INVALID_SELECTION}
        now = args.get("now", False)
        home = get_hermes_home().resolve()
        try:
            pair = _configured_pair(ctx)
            state = ctx.state.data_dir / "unified" if pair else ctx.state.data_dir
            if not _authorized_tool_turn(home, state, pair, session_id):
                return {"status": "denied"}
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return {"status": "denied"}

        try:
            report = _live_operation(ctx, state, _launcher(), "use", selection=selection, now=now)
            status = report["status"]
            result = {"status": status, "message": _reply("use", report, selection)}
            if status == "selected":
                result["selection"] = selection
            if status == "switched":
                for key in ("provider", "account"):
                    if value := _alias(report.get(key)):
                        result[key] = value
            if (reason := report.get("reason")) in _REASONS:
                result["reason"] = reason
            return result
        except ValueError:
            return {"status": "invalid_selection", "message": _INVALID_SELECTION}
        except (OSError, RuntimeError, KeyError, TypeError):
            return {"status": "unavailable", "message": _FAILED}

    async def handle(args, *, session_id=None):
        return json.dumps(await asyncio.to_thread(run, args, session_id), ensure_ascii=False)

    return handle
