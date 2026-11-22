"""Send attachments and control requests through the connected bot sidecar."""

import asyncio
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile

import aiohttp


SIDECAR = "http://127.0.0.1:8796"
STATUS_CHAT = "status@broadcast"
SEND_TIMEOUT = aiohttp.ClientTimeout(connect=3, total=120)
_PHONE = re.compile(r"[1-9][0-9]{6,14}")
_CHAT = re.compile(r"(?:[1-9][0-9]{6,14}@s\.whatsapp\.net|[0-9]{1,20}@lid|[0-9]+(?:-[0-9]+)?@g\.us)")


def whatsapp_chat(value):
    if isinstance(value, str):
        value = value.strip()
        if _PHONE.fullmatch(value.removeprefix("+")):
            return value.removeprefix("+") + "@s.whatsapp.net"
        if _CHAT.fullmatch(value):
            return value
    raise ValueError("Use a WhatsApp chat ID or an international phone number.")


def _open_media(path):
    handle = open(
        path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK),
    )
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise ValueError("Select a regular attachment file.")
    return handle


def _check_status_voice(handle):
    # The Ogg BOS page contains the Opus identification packet. A mono stream
    # uses channel mapping 0 and a 19-byte OpusHead, as in WhatsApp Control.
    header = handle.read(47)
    if (len(header) != 47 or header[:6] != b"OggS\x00\x02" or header[26:38] != b"\x01\x13OpusHead\x01\x01"
            or header[46] != 0):
        raise ValueError("Voice Status requires a mono Ogg Opus audio file.")
    handle.seek(0)


def prepare_media(raw_path, runtime, *, kind=None, caption=None, filename=None):
    from agent.file_safety import raise_if_read_blocked
    from agent.runtime_cwd import resolve_agent_cwd

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("Select an attachment path.")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(resolve_agent_cwd()) / path
    path = path.resolve(strict=True)
    raise_if_read_blocked(str(path))
    if path.is_relative_to(runtime.parent.resolve()):
        raise ValueError("Select an attachment outside the plugin's private data directory.")
    if caption is not None and not isinstance(caption, str):
        raise ValueError("Supply the attachment caption as text.")
    mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if kind is None:
        kind = "image" if mimetype.startswith("image/") else "document"
    if not isinstance(kind, str) or kind not in {"image", "video", "voice", "document"}:
        raise ValueError("Select a supported WhatsApp attachment type.")
    if kind in {"image", "video"} and not mimetype.startswith(f"{kind}/"):
        raise ValueError(f"Select a file matching the WhatsApp attachment type {kind}.")
    if kind == "voice" and caption:
        raise ValueError("Voice Status uses the audio without a caption.")
    filename = filename or path.name
    if not isinstance(filename, str) or not filename or any(char in filename for char in ("/", "\\", "\x00", "\r", "\n")):
        raise ValueError("Supply a valid attachment filename.")
    with _open_media(path) as handle:
        if kind == "voice":
            _check_status_voice(handle)
            mimetype = "audio/ogg; codecs=opus"
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    payload = {"kind": kind, "filename": filename, "mimetype": mimetype, "expected_sha256": digest}
    if caption:
        payload["text"] = caption
    return path, payload


def _stage_media(path, runtime, expected_sha256):
    spool = runtime / "outbound"
    spool.mkdir(mode=0o700, exist_ok=True)
    if spool.is_symlink() or stat.S_IMODE(spool.stat().st_mode) & 0o077:
        raise ValueError("The WhatsApp upload directory must be private.")

    with _open_media(path) as source, NamedTemporaryFile(dir=spool, prefix="media-", delete=False) as output:
        staged = Path(output.name)
        try:
            digest = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError("The attachment changed after it was prepared.")
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    return staged


async def _receipt(response, message_id):
    if response.status not in {200, 422}:
        return {"status": {400: "invalid", 503: "unavailable"}.get(response.status, "unconfirmed")}
    data = await response.json()
    expected = "accepted" if response.status == 200 else "rejected"
    if (not isinstance(data, dict) or data.get("message_id") != message_id or data.get("status") != expected):
        return {"status": "unconfirmed"}
    result = {"status": expected, "message_id": message_id}
    if expected == "rejected" and type(data.get("rejection_status")) is int:
        result["rejection_status"] = data["rejection_status"]
    return result


async def send_message(http, runtime, bot, payload, message_id, source=None):
    outgoing = {**payload, "bot_number": bot, "message_id": message_id}
    if source is not None:
        stage = asyncio.create_task(asyncio.to_thread(_stage_media, source, runtime, payload["expected_sha256"]))
        try:
            staged = await asyncio.shield(stage)
        except asyncio.CancelledError:
            def discard(completed):
                if not completed.cancelled() and completed.exception() is None:
                    completed.result().unlink(missing_ok=True)

            stage.add_done_callback(discard)
            raise
        except (OSError, ValueError):
            return {"status": "invalid"}
        outgoing["path"] = str(staged)
    else:
        staged = None
    try:
        async with http.post("/messages", json=outgoing, timeout=SEND_TIMEOUT, allow_redirects=False) as response:
            return await _receipt(response, message_id)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return {"status": "unconfirmed"}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


async def get_send_receipt(http, bot, message_id):
    try:
        async with http.get(f"/messages/{message_id}", params={"bot_number": bot},
                            allow_redirects=False) as response:
            return await _receipt(response, message_id)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
