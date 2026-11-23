"""Match a native Hermes final's attachments to the Rust transport's durable ACKs."""

import asyncio
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
from urllib.request import url2pathname

from gateway.platforms.base import BasePlatformAdapter
from hermes_state import SessionDB

if __package__:
    from .rust_whatsapp_messages import _open_media
else:
    from rust_whatsapp_messages import _open_media


logger = logging.getLogger(__name__)
_stream_dispatch = ContextVar("wa_live_stream_media_dispatch", default=None)


def _items(output):
    media, text = BasePlatformAdapter.extract_media(output)
    images, text = BasePlatformAdapter.extract_images(text)
    files, _ = BasePlatformAdapter.extract_local_files(BasePlatformAdapter.strip_media_directives_for_display(text))
    # Retain remote images and filtered paths in the expected total. A URL or a
    # platform fallback notice cannot stand in for a WhatsApp attachment ACK.
    paths = [path for path, _voice in media] + files
    urls = []
    for url, _caption in images:
        if url.startswith("file://"):
            paths.append(url2pathname(url[7:]))
        else:
            urls.append(url)
    items = [str(Path(path).resolve()) for path in paths] + urls
    # Hermes drops NUL-bearing MEDIA paths before returning the parsed list.
    # A sibling's ACK cannot establish that the entire malformed final landed.
    if "\x00" in output:
        items.append(None)
    return items


def _group(home, owner, session_id, admission_id, items):
    binding = [str(home.resolve()), owner, session_id, admission_id, items]
    return hashlib.sha256(json.dumps(binding, ensure_ascii=True).encode()).hexdigest()


def _event_admission(home, event):
    # Hermes uses an admission ID as the event's message_id for a viewer
    # submission. The native DM waiter uses the provider's message ID instead.
    source, message = event.source, event.message_id
    if (not message or event.internal or source.chat_type != "dm" or source.thread_id
            or source.platform.value != "whatsapp" or event.ledger_message_id not in (None, message)):
        return None
    claim = getattr(event, "_claimed_owner", None)
    with closing(SessionDB(home / "state.db", read_only=True)) as db, db._read_ctx() as conn:
        if claim:
            row = conn.execute(
                """SELECT target_session_id, admission_id FROM session_admissions
                   WHERE admission_id=? AND target_session_id=? AND generation=?
                     AND json_type(payload_json,'$.native_text_v1') IS NULL""",
                (message, *claim),
            ).fetchone()
            if row:
                return tuple(row)
        principal = "messaging:" + json.dumps(
            [source.profile, "whatsapp", source.chat_id, None, source.user_id], separators=(",", ":"))
        request = message + ":edit:" + event.edit_revision if event.edit_revision else message
        rows = conn.execute(
            """SELECT target_session_id, admission_id FROM session_admissions
               WHERE principal_id=? AND request_id=? AND (? IS NULL OR target_session_id=?)
                 AND json_extract(payload_json,'$.native_text_v1.event.message_id')=?
                 AND json_type(payload_json,'$.native_text_v1.automation') IS NULL LIMIT 2""",
            (principal, request, claim and claim[0], claim and claim[0], message),
        ).fetchall()
        return tuple(rows[0]) if len(rows) == 1 else None


@dataclass
class MediaDispatch:
    group: str
    remaining: dict


def prepare_dispatch(home, owner, event, output, expected_admission=None):
    try:
        binding = _event_admission(home, event)
        if binding and (expected_admission is None or binding[1] == expected_admission):
            items = _items(output)
            remaining = {}
            for slot, item in enumerate(items):
                remaining.setdefault(item, []).append(slot)
            return MediaDispatch(_group(home, owner, *binding, items), remaining)
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        logger.debug("Could not identify the Hermes turn for a WhatsApp media receipt.", exc_info=True)
    return None


async def streamed_dispatch(runner, home, owner, chat_id):
    """Verify the original FIFO task for Hermes' post-stream final (v0.23.2)."""
    from gateway.session_authorities import authority_for_home
    from gateway.session_context import _SESSION_TURN_EVENT
    from gateway.session_results import execution_result

    event = _SESSION_TURN_EVENT.get()
    claim = getattr(event, "_claimed_owner", None)
    result = (execution_result.get() or {}).get("result") or {}
    output = result.get("final_response")
    if (not claim or event.source.chat_id != chat_id or event.ledger_message_id not in (None, event.message_id)
            or result.get("already_sent") is not True or result.get("failed")
            or result.get("media_already_delivered") or not isinstance(output, str) or not output):
        return None

    authority = authority_for_home(runner, home)
    session_id, generation = claim
    live = authority and authority.sessions.get(session_id)
    if (live is None or live.task is not asyncio.current_task()
            or authority.executing.get(session_id) != generation):
        return None
    stamp = live.event_stream.execution
    admission_id = stamp.get("admission_id")
    if (not admission_id or stamp.get("authority_epoch") != authority.epoch
            or stamp.get("execution_generation") != generation):
        return None

    # The runner sends a post-stream final one attachment at a time. A child
    # task inheriting this ContextVar is excluded by the drain check above.
    identity = (home, owner, session_id, admission_id, output)
    cached = _stream_dispatch.get()
    if cached and cached[0] == identity:
        return cached[1]
    batch = await asyncio.to_thread(prepare_dispatch, home, owner, event, output, admission_id)
    _stream_dispatch.set((identity, batch))
    return batch


def record_attempt(runtime, bot, dispatch, path, recipient, message_id):
    """Persist the random outgoing ID before transport, including its verified recipient."""
    slots = dispatch.remaining.get(str(path))
    if not slots:
        return
    slot = slots.pop(0)
    directory = runtime / "outbound-receipts" / bot
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(directory / "live-media.sqlite3")) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS attempts
                (batch TEXT, slot INTEGER, message_id TEXT, recipient TEXT, PRIMARY KEY(batch,slot,message_id))""")
            db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (dispatch.group, slot, message_id, recipient))
    except (OSError, sqlite3.Error):
        # Receipt tracking is best effort. The native send still owns delivery.
        logger.warning("Could not record the WhatsApp attachment's Hermes turn.", exc_info=True)


def _ack(directory, message_id, recipient):
    if not re.fullmatch(r"3EB0[0-9A-F]{18}", message_id):
        return "unconfirmed"
    try:
        with _open_media(directory / message_id) as handle:
            row = json.load(handle)
        if (isinstance(row, dict) and row.get("message_id") == message_id
                and row.get("chat_id") == recipient and row.get("audience", []) == []
                and row.get("status") in ("accepted", "rejected")):
            return row["status"]
    except (OSError, ValueError):
        pass
    return "unconfirmed"


def read_media_delivery(runtime, bot, home, owner_number, route_id, session_id, admission_id, output):
    """Read sidecar receipts without retrying delivery or opening a second session."""
    directory = runtime / "outbound-receipts" / bot
    index = directory / "live-media.sqlite3"
    if not index.is_file():
        return "unconfirmed"
    try:
        if route_id != session_id:
            with closing(SessionDB(home / "state.db", read_only=True)) as db:
                if session_id not in db.get_compression_lineage(route_id):
                    return "unconfirmed"
        items = _items(output)
        group = _group(home, owner_number, session_id, admission_id, items)
        states = [set() for _ in items]
        with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
            for slot, message_id, recipient in db.execute(
                    "SELECT slot,message_id,recipient FROM attempts WHERE batch=?", (group,)):
                if type(slot) is int and 0 <= slot < len(states):
                    states[slot].add(_ack(directory, message_id, recipient))
    except (OSError, ValueError, TypeError, IndexError, RuntimeError, sqlite3.Error):
        return "unconfirmed"
    accepted = sum("accepted" in state for state in states)
    if accepted:
        return "sent" if accepted == len(states) else "partial"
    if states and all(state == {"rejected"} for state in states):
        return "rejected"
    return "unconfirmed"
