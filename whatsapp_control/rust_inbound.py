"""Read chatbot events and original attachments from the bot transport."""

import asyncio
import os
from pathlib import Path
import re
from uuid import uuid4

import aiohttp

from .settings import private_dir


_MEDIA = {
    "image/jpeg": ("jpg", 8 * 1024 * 1024),
    "image/png": ("png", 8 * 1024 * 1024),
    "image/webp": ("webp", 8 * 1024 * 1024),
    "audio/ogg": ("ogg", 16 * 1024 * 1024),
}
_TEMPORARY = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\.(jpg|png|webp|ogg)")


async def poll_messages(http, bot, on_event):
    params = {"bot_number": bot}
    while True:
        try:
            async with http.get("/control/messages", params=params, allow_redirects=False,
                                timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status == 409:
                    params.pop("cursor", None)
                    continue
                response.raise_for_status()
                page = await response.json()
                cursor = page["cursor"]
                if not isinstance(cursor, str) or not cursor:
                    raise ValueError("Invalid WhatsApp event cursor")
                for event in page["messages"]:
                    on_event(event)
                params["cursor"] = cursor
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError):
            await asyncio.sleep(1)


async def download_media(http, bot, media_root, args):
    params = {"bot_number": bot, "chat": args["chat"], "id": args["id"]}
    async with http.get("/control/media", params=params, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=45)) as response:
        if response.status != 200 or response.content_type not in _MEDIA:
            raise ValueError("WhatsApp attachment is unavailable or unsupported")
        extension, limit = _MEDIA[response.content_type]
        if response.content_length is not None and not 0 < response.content_length <= limit:
            raise ValueError("WhatsApp attachment size is unsupported")
        path = private_dir(media_root) / f"{uuid4()}.{extension}"
        size = 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as target:
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("WhatsApp attachment size is unsupported")
                    target.write(chunk)
            if not size:
                raise ValueError("WhatsApp attachment is empty")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"path": str(path), "mime": response.content_type}


def discard_media(media_root, args):
    path = Path(args["path"])
    if path.parent != media_root or not _TEMPORARY.fullmatch(path.name):
        raise ValueError("Invalid WhatsApp temporary attachment path")
    path.unlink(missing_ok=True)
    return {"removed": True}
