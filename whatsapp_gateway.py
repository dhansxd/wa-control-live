"""Gateway adapter for the unified WhatsApp bot transport."""

import asyncio
from hmac import compare_digest
import logging
import re
import subprocess
from uuid import uuid4

import aiohttp
from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter, SendResult, cache_audio_from_bytes_async, cache_image_from_bytes_async,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

if __package__:
    from .rust_whatsapp_messages import SIDECAR, prepare_media, send_message
else:
    from rust_whatsapp_messages import SIDECAR, prepare_media, send_message

_PHONE = re.compile(r"[1-9][0-9]{6,14}")
_PERSON = re.compile(r"([0-9]{1,20})(?::[0-9]+)?@(s\.whatsapp\.net|lid)")
_CURSOR_CONFLICT = 409
_CREATE_VOICE_SESSION = "voice.create_session"
_VOICE_ROUTING = "voice.routing"
_IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_TRANSPORT_RETRY_SECONDS = 5
_TRANSPORT_STOP_SECONDS = 4
logger = logging.getLogger(__name__)


def configured_pair(ctx):
    """Read the expected identities selected by the owner for this Hermes profile."""
    get_config = getattr(ctx, "get_config", None)
    if get_config is None or get_config("unified_gateway", default=False) is not True:
        return None
    numbers = []
    for name in ("owner_number", "bot_number"):
        value = get_config(name, default="")
        number = value.strip().removeprefix("+") if isinstance(value, str) else ""
        if not _PHONE.fullmatch(number):
            raise ValueError("Configure the owner and Hermes bot with full international WhatsApp numbers.")
        numbers.append(number)
    if numbers[0] == numbers[1]:
        raise ValueError("The owner and Hermes bot must be different numbers for WhatsApp calls.")
    return tuple(numbers)


def _phone(value):
    if not isinstance(value, str):
        return ""
    if _PHONE.fullmatch(value):
        return value
    match = _PERSON.fullmatch(value)
    return match[1] if match and match[2] == "s.whatsapp.net" else ""


class WhatsAppLiveAdapter(BasePlatformAdapter):
    def __init__(self, config, owner, bot, token_path, *, endpoint=SIDECAR, transport_command=None):
        super().__init__(config, Platform.WHATSAPP)
        self.owner = owner
        self.bot = bot
        self.token_path = token_path
        self.endpoint = endpoint
        self.http = None
        self.listener = None
        self.verified = False
        self.cursor = None
        # The Rust sidecar retains at most 256 messages for the life of its process.
        # Hermes transfers native dedup caches when replacing an adapter.
        self.seen = MessageDeduplicator(max_size=512, ttl_seconds=float("inf"))
        self.owner_lid = None
        self.transport_command = transport_command
        self._transport = None
        self._transport_paused = False
        self._next_transport_start = 0.0

    @property
    def send_path_degraded(self):
        return not self.verified

    async def connect(self, *, is_reconnect=False):
        if self.listener is not None and not self.listener.done():
            return True
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
            if not token:
                return False
        except OSError:
            return False
        self.http = aiohttp.ClientSession(
            base_url=self.endpoint, headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=22, connect=3),
        )
        # The sidecar can reconnect independently. Keep a single gateway consumer
        # attached even when the WhatsApp network connection is temporarily down.
        self._mark_connected()
        self.listener = asyncio.create_task(self._receive())
        return True

    async def disconnect(self):
        if self.listener is not None:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
            self.listener = None
        if self.http is not None:
            await self.http.close()
            self.http = None
        await self._stop_transport()
        self.verified = False
        self._mark_disconnected()

    def _ensure_transport(self):
        if (self.transport_command is None or self._transport_paused
                or not self.token_path.with_name("paired.json").is_file()):
            return
        if self._transport is not None:
            code = self._transport.poll()
            if code is None:
                return
            self._transport = None
            if code == 0:
                # An explicit cleanup or disable exited the service normally.
                self._transport_paused = True
                return
            logger.warning("The gateway-owned WhatsApp transport exited with code %s.", code)
        now = asyncio.get_running_loop().time()
        if now < self._next_transport_start:
            return
        self._next_transport_start = now + _TRANSPORT_RETRY_SECONDS
        try:
            argv, env = self.transport_command()
            # Record the child before yielding, so disconnect cannot race a spawn.
            self._transport = subprocess.Popen(
                argv, env=env, stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except (OSError, RuntimeError, ValueError):
            logger.warning("Could not start the WhatsApp transport for this gateway.", exc_info=True)

    async def _stop_transport(self):
        process, self._transport = self._transport, None
        self._transport_paused = False
        self._next_transport_start = 0.0
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=_TRANSPORT_STOP_SECONDS)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            # The native serve command is responsible for stopping its own children.
            logger.warning("The gateway-owned WhatsApp transport is still shutting down.")

    async def _receive(self):
        while True:
            try:
                if not self.verified:
                    async with self.http.get("/health", allow_redirects=False) as response:
                        response.raise_for_status()
                        health = await response.json()
                    if self._transport is None or self._transport.poll() is not None:
                        # An already-running foreground transport owns this profile.
                        self._transport_paused = True
                    if (health.get("logged_in") is not True or health.get("bot") != self.bot
                            or health.get("owner") != self.owner):
                        await asyncio.sleep(1)
                        continue
                    self.verified = True
                    self._mark_connected()
                params = {"after": self.cursor} if self.cursor is not None else None
                async with self.http.get("/owner/messages", params=params, allow_redirects=False) as response:
                    if response.status == _CURSOR_CONFLICT:
                        logger.warning("The WhatsApp sidecar backlog changed. Reading its available current messages.")
                        self.cursor = None
                        self.verified = False
                        self._mark_degraded()
                        continue
                    response.raise_for_status()
                    batch = await response.json()
                cursor, messages = batch.get("cursor"), batch.get("messages")
                if not isinstance(cursor, str) or not isinstance(messages, list):
                    raise ValueError("Invalid WhatsApp sidecar event batch")
                for message in messages:
                    await self._receive_message(message)
                self.cursor = cursor
            except (aiohttp.ClientError, OSError, ValueError, AttributeError, RuntimeError) as error:
                if self.verified:
                    self.verified = False
                    self._mark_degraded()
                if isinstance(error, aiohttp.ClientConnectorError):
                    self._ensure_transport()
                await asyncio.sleep(1)

    async def _receive_message(self, message):
        if not isinstance(message, dict):
            return
        identifier, text = message.get("id"), message.get("text")
        chat, sender, alternate = (message.get(key) for key in ("chat_id", "sender_id", "sender_id_alt"))
        media = message.get("media")
        if (not isinstance(identifier, str) or not identifier or self.seen.contains(identifier)
                or not isinstance(text, str) or (not text.strip() and media not in ("image", "voice"))):
            return
        from_owner_phone = _phone(chat) == self.owner and _phone(sender) == self.owner
        from_owner_lid = (isinstance(sender, str) and chat == sender and sender.endswith("@lid")
                          and _PERSON.fullmatch(sender) and _phone(alternate) == self.owner)
        if not (from_owner_phone or from_owner_lid):
            return
        # Use the verified peer's phone as the route anchor. Keep the transport LID
        # as an alias so Hermes can authorize and deliver without a second chat.
        alias = sender if from_owner_lid else None
        if alias is not None:
            self.owner_lid = alias
        source = self.owner_source(alias=alias, message_id=identifier)
        event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id=identifier)
        if media in ("image", "voice"):
            try:
                await self._attach_owner_media(event, chat, media)
            except (aiohttp.ClientError, OSError, ValueError, TimeoutError):
                logger.warning("Could not read the WhatsApp owner's attachment")
                if not text.strip():
                    return
        await self.handle_message(event)
        self.seen.is_duplicate(identifier)

    async def _attach_owner_media(self, event, chat, media):
        params = {"bot_number": self.bot, "chat": chat, "id": event.message_id}
        maximum = (8 if media == "image" else 16) * 1024 * 1024
        async with self.http.get("/control/media", params=params, allow_redirects=False,
                                 timeout=aiohttp.ClientTimeout(total=45, connect=3)) as response:
            response.raise_for_status()
            mime = response.content_type
            if media == "image" and mime in _IMAGE_EXTENSIONS:
                cache, extension, kind = cache_image_from_bytes_async, _IMAGE_EXTENSIONS[mime], MessageType.PHOTO
            elif media == "voice" and mime == "audio/ogg":
                cache, extension, kind = cache_audio_from_bytes_async, ".ogg", MessageType.VOICE
            else:
                raise ValueError("Unexpected WhatsApp attachment type")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                if len(data) + len(chunk) > maximum:
                    raise ValueError("WhatsApp attachment is too large")
                data.extend(chunk)
        event.media_urls = [await cache(bytes(data), extension)]
        event.media_types = [mime]
        event.message_type = kind

    def owner_source(self, *, alias=None, message_id=None):
        return self.build_source(chat_id=self.owner, user_id=self.owner, chat_type="dm",
                                 chat_id_alt=alias, user_id_alt=alias, message_id=message_id)

    def _owner_recipient(self, chat_id):
        if not self.verified or self.http is None:
            return None
        if _phone(chat_id) == self.owner or (
            self.owner_lid is not None and chat_id == self.owner_lid
        ):
            return self.owner_lid or f"{self.owner}@s.whatsapp.net"
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        recipient = self._owner_recipient(chat_id)
        if recipient is None:
            return SendResult(success=False, error="WhatsApp bot route is unavailable for this destination")
        try:
            async with self.http.post("/owner/messages", json={"chat_id": recipient, "text": content},
                                      allow_redirects=False) as response:
                response.raise_for_status()
                result = await response.json()
            if isinstance(result.get("message_id"), str) and result["message_id"]:
                return SendResult(success=True, message_id=result["message_id"])
        except (aiohttp.ClientError, OSError, ValueError, AttributeError):
            pass
        return SendResult(success=False, error="The WhatsApp sidecar did not confirm the reply")

    async def _send_attachment(self, chat_id, path, kind, caption, filename=None):
        recipient = self._owner_recipient(chat_id)
        if recipient is None:
            return SendResult(success=False, error="WhatsApp bot route is unavailable for this destination")
        runtime = self.token_path.parent
        try:
            source, payload = await asyncio.to_thread(
                prepare_media, path, runtime, kind=kind, caption=caption, filename=filename,
            )
        except (OSError, ValueError, RuntimeError):
            return SendResult(success=False, error="This WhatsApp attachment could not be prepared")
        if self._owner_recipient(chat_id) != recipient:
            return SendResult(success=False, error="WhatsApp bot route is unavailable for this destination")
        message_id = "3EB0" + uuid4().hex[:18].upper()
        receipt = await send_message(self.http, runtime, self.bot, {**payload, "chat_id": recipient}, message_id, source)
        if receipt["status"] == "accepted":
            return SendResult(success=True, message_id=message_id)
        return SendResult(success=False, error="The WhatsApp sidecar did not confirm the attachment")

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_attachment(chat_id, image_path, "image", caption)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None,
                            metadata=None, **kwargs):
        return await self._send_attachment(chat_id, file_path, "document", caption, file_name)

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}


def _authorized_session_store(home, owner, bot, token_path, params, session_id):
    from gateway.run_api import _ACTIVE_RUNNER
    from hermes_constants import get_hermes_home, profile_name_for_home

    token = params.get("token")
    if (session_id is not None or params.keys() != {"token"} or not isinstance(token, str)
            or not token.isascii() or get_hermes_home().resolve() != home):
        raise ValueError("Invalid request for the WhatsApp owner session")
    expected = token_path.read_text(encoding="utf-8").strip()
    if not expected or not compare_digest(token, expected):
        raise ValueError("WhatsApp voice service authorization failed")

    runner = _ACTIVE_RUNNER
    profile = profile_name_for_home(home) or "default"
    adapter = runner._adapters_for_profile(profile).get(Platform.WHATSAPP) if runner is not None else None
    if (not isinstance(adapter, WhatsAppLiveAdapter) or not adapter.verified
            or (adapter.owner, adapter.bot, adapter.token_path) != (owner, bot, token_path)):
        raise RuntimeError("The verified WhatsApp bot is not ready in this Hermes profile")

    source = adapter.owner_source(alias=adapter.owner_lid)
    identity = adapter._canonicalize(source)
    if (identity is None or identity.runtime_home.resolve() != home
            or identity.authorization_home.resolve() != home
            or not runner._is_user_authorized_for_source(source)):
        raise RuntimeError("The WhatsApp owner cannot use this Hermes profile")
    return runner.session_store, source


def _voice_gateway_request(ctx, name):
    token_path = ctx.state.data_dir / "unified" / "adapter.token"
    method = f"plugin.{ctx.plugin_id}.{name}"

    async def request(client):
        token = await asyncio.to_thread(token_path.read_text, encoding="utf-8")
        return await client.rpc(method, token=token.strip(), _timeout=4)

    return request


def voice_session_creator(ctx):
    return _voice_gateway_request(ctx, _CREATE_VOICE_SESSION)


def voice_routing_reader(ctx):
    return _voice_gateway_request(ctx, _VOICE_ROUTING)


def register_gateway(ctx, owner, bot, *, transport_command=None):
    from hermes_constants import get_hermes_home
    if __package__:
        from .whatsapp_reconnect import register_whatsapp_platform
    else:
        from whatsapp_reconnect import register_whatsapp_platform

    home = get_hermes_home().resolve()
    token_path = ctx.state.data_dir / "unified" / "adapter.token"

    def create(params, *, session_id, **_):
        store, source = _authorized_session_store(home, owner, bot, token_path, params, session_id)
        # The native routing lock coordinates the first call with an arriving DM.
        return {"session_id": store.get_or_create_session(source).session_id}

    def routing(params, *, session_id, **_):
        store, _source = _authorized_session_store(home, owner, bot, token_path, params, session_id)
        if store._routing_home is None:
            raise RuntimeError("The serving gateway has no routing store")
        return {"control_home": str(store._routing_home.resolve()), "scope": store._routing_scope()}

    def adapter_factory(config):
        return WhatsAppLiveAdapter(config, owner, bot, token_path, transport_command=transport_command)

    register_whatsapp_platform(ctx, adapter_factory, token_path)
    ctx.register_rpc(_CREATE_VOICE_SESSION, create)
    ctx.register_rpc(_VOICE_ROUTING, routing)
