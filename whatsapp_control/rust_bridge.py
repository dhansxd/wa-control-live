"""Adapt WhatsApp Control's bridge protocol to the existing bot client."""

import asyncio
import hashlib
import mimetypes
from pathlib import Path

import aiohttp

from ..rust_whatsapp_messages import SIDECAR, STATUS_CHAT, _open_media, get_send_receipt, send_message
from .runtime import BridgeError
from .settings import phone_number, write_json


def _attachment_digest(path):
    with _open_media(path) as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


class RustBridge:
    def __init__(self, runtime, owner, bot, media_root, on_event):
        self.runtime = Path(runtime)
        self.owner = phone_number(owner)
        self.bot = phone_number(bot)
        if not self.owner or not self.bot:
            raise ValueError("The unified WhatsApp bot and owner numbers are required.")
        self.media_root = media_root
        self.on_event = on_event
        self.account = None
        self.http = None
        self.tasks = []

    async def start(self):
        from .rust_inbound import poll_messages

        token = (self.runtime / "adapter.token").read_text(encoding="utf-8").strip()
        if not token or (self.runtime / "unlink-request.json").exists():
            raise RuntimeError("Finish WhatsApp bot runtime setup before serving the linked account.")
        self.http = aiohttp.ClientSession(
            base_url=SIDECAR, headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(connect=3, total=50),
        )
        self.tasks = [asyncio.create_task(self.watch_connection()),
                      asyncio.create_task(poll_messages(self.http, self.bot, self.event))]

    def event(self, event):
        self.on_event({**event, "account": self.account})

    async def connection(self):
        try:
            async with self.http.get("/health", timeout=3, allow_redirects=False) as response:
                health = await response.json() if response.status == 200 else {}
        except (aiohttp.ClientError, TimeoutError, ValueError):
            health = {}
        connected = (isinstance(health, dict) and health.get("logged_in") is True and health.get("owner") == self.owner
                     and health.get("bot") == self.bot)
        result = {"status": "connected" if connected else "offline"}
        if connected:
            result["identity"] = {"id": self.bot + "@s.whatsapp.net", "phone": self.bot + "@s.whatsapp.net"}
        self.event({"event": "connection", **result})
        return result

    async def watch_connection(self):
        while True:
            await self.connection()
            await asyncio.sleep(1)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.http is not None:
            await self.http.close()

    def request_unlink(self, result):
        write_json(self.runtime / "unlink-request.json", {
            "bot_number": self.bot, "remote_unlinked": result.get("remote_unlinked") is True,
        })

    async def send(self, op, args):
        kind = args.get("type", "text")
        payload = {"kind": kind, "chat_id": args.get("chat")}
        if op == "post_status":
            payload.update(chat_id=STATUS_CHAT, audience=args["audience"])
        source = None
        if kind == "text":
            payload["text"] = args["text"]
        else:
            source = Path(args["path"])
            try:
                digest = args.get("expected_sha256") or await asyncio.to_thread(_attachment_digest, source)
            except (OSError, ValueError) as error:
                raise BridgeError("The attachment is unavailable.", "invalid") from error
            mime = args.get("mime") or mimetypes.guess_type(source)[0]
            if kind == "voice":
                mime = "audio/ogg; codecs=opus"
            elif kind == "audio" and not (mime or "").startswith("audio/"):
                mime = "audio/mpeg"
            payload.update(filename=source.name, mimetype=mime or "application/octet-stream", expected_sha256=digest)
            if kind in {"image", "video", "document"} and args.get("caption") is not None:
                payload["text"] = args["caption"]
        receipt = await send_message(self.http, self.runtime, self.bot, payload, args["message_id"], source)
        if receipt["status"] != "accepted":
            raise BridgeError("WhatsApp did not confirm the send.", receipt["status"], receipt.get("rejection_status"))
        return receipt

    async def control(self, op, args):
        payload = {"bot_number": self.bot, "op": op, "args": args}
        try:
            async with self.http.post("/control", json=payload, allow_redirects=False) as response:
                data = await response.json()
                if response.status == 200:
                    return data
                code = {400: "invalid", 401: "unavailable", 422: "rejected", 503: "unavailable"}
                error = data if isinstance(data, dict) else {}
                raise BridgeError(str(error.get("error") or "WhatsApp bot operation failed.")[:300],
                                  code.get(response.status, "unconfirmed"), error.get("rejection_status"))
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise BridgeError("WhatsApp bot did not confirm the operation.", "unconfirmed") from error

    async def call(self, op, args):
        if op == "connect":
            return await self.connection()
        if op in {"pair", "repair"}:
            raise BridgeError("Manage the bot pairing with 'hermes wa-control-live pair'.", "invalid")
        if op in {"disconnect", "remove_account"}:
            # The launcher owns the Rust process and its pairing files.
            return {}
        if op in {"send_text", "send_media", "post_status"}:
            return await self.send(op, args)
        if op == "send_result":
            return await get_send_receipt(self.http, self.bot, args["message_id"])
        if op in {"download_media", "discard_media"}:
            from .rust_inbound import discard_media, download_media

            if op == "discard_media":
                return discard_media(self.media_root, args)
            try:
                return await download_media(self.http, self.bot, self.media_root, args)
            except aiohttp.ClientError as error:
                raise RuntimeError("The WhatsApp attachment download was interrupted.") from error
        return await self.control(op, args)
