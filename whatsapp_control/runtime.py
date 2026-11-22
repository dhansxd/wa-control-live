"""Private local protocol and plugin activation checks."""

import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from .settings import PLUGIN, private_dir


MAX_FRAME = 2 * 1024 * 1024
_BRIDGE_TIMEOUT = 50
_DOWNLOAD_NAME = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.(?:jpg|png|webp|ogg|bin)\Z")


class ServiceStopped(RuntimeError):
    pass


class BridgeError(RuntimeError):
    def __init__(self, message, code, status=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.definitive_rejection = code == "rejected"


def runtime_path(home):
    return Path(home) / "plugin-data" / PLUGIN


def socket_dir(root):
    identity = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:24]
    return Path("/tmp") / f"hermes-wa-{os.getuid()}-{identity}"


def activation_ids(ctx):
    name = getattr(getattr(ctx, "manifest", None), "name", None)
    key = ctx.plugin_id
    return (key, name) if name and name != key else key


def active_install(entry, key, generation=None):
    try:
        from hermes_cli.config import load_config_readonly

        st = Path(entry).stat()
        if generation is not None and generation != (st.st_dev, st.st_ino):
            return False
        plugins = load_config_readonly().get("plugins", {})
        names = {key} if isinstance(key, str) else set(key)
        return bool(names.intersection(plugins.get("enabled") or [])) and not names.intersection(
            plugins.get("disabled") or []
        )
    except (ImportError, OSError, ValueError, TypeError, AttributeError):
        return False


def encode_frame(payload):
    frame = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    if len(frame) > MAX_FRAME:
        raise ValueError("Request exceeds the plugin message size limit.")
    return frame


def node_executable():
    try:
        from pm import env_for
    except ImportError:
        node = shutil.which("node")
    else:
        node = shutil.which("node", path=env_for("node").get("PATH", ""))
    if node is None:
        raise RuntimeError("Node.js is unavailable. Accept the Node dependency prompt when installing this plugin in Hermes.")
    return node


async def request_worker(root, action, args=None, on_event=None):
    directory = socket_dir(root)
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid():
        raise ServiceStopped("WhatsApp Control service is stopped. Start it with 'hermes whatsapp-control serve'.")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(directory / "rpc", limit=MAX_FRAME), timeout=3
        )
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as error:
        raise ServiceStopped("WhatsApp Control service is stopped. Start it with 'hermes whatsapp-control serve'.") from error
    try:
        writer.write(encode_frame({"action": action, "args": args or {}}))
        await writer.drain()
        async with asyncio.timeout(190 if action in {"pair", "repair"} else 240):
            while line := await reader.readline():
                response = json.loads(line)
                if response.get("event"):
                    if on_event:
                        on_event(response)
                    continue
                if not response.get("ok"):
                    raise RuntimeError(response.get("error", "Operation failed."))
                return response.get("result")
        raise RuntimeError("The service disconnected before returning a result.")
    except TimeoutError as error:
        raise RuntimeError("The service did not return a result in time.") from error
    finally:
        writer.close()
        await writer.wait_closed()


class Bridge:
    def __init__(self, root, on_event):
        self.root = root
        self.on_event = on_event
        self.process = None
        self.reader = None
        self.pending = {}
        self.downloads = {}
        self.sequence = 0

    async def start(self):
        source = Path(__file__).parent.resolve()
        private_dir(self.root / "bridge")
        env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
        self.process = await asyncio.create_subprocess_exec(
            node_executable(), str(source / "bridge.mjs"), str(self.root / "bridge"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=MAX_FRAME,
        )
        self.reader = asyncio.create_task(self._read())

    def _discard_download(self, result):
        raw = result.get("path") if isinstance(result, dict) else None
        if not isinstance(raw, str):
            return
        path = Path(raw)
        if not _DOWNLOAD_NAME.fullmatch(path.name):
            return
        try:
            root = self.root.resolve(strict=True)
            if self.root.is_symlink() or path.parent not in {self.root / "bridge" / "media", root / "bridge" / "media"}:
                return
            # Use directory descriptors so swapping a scratch directory cannot redirect unlink.
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            with ExitStack() as opened:
                descriptor = None
                for name in (root, "bridge", "media"):
                    descriptor = os.open(name, flags, dir_fd=descriptor)
                    opened.callback(os.close, descriptor)
                    if os.fstat(descriptor).st_uid != os.getuid():
                        return
                info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                    os.unlink(path.name, dir_fd=descriptor)
        except (OSError, RuntimeError, ValueError):
            pass

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if message.get("event"):
                    self.on_event(message)
                    continue
                identifier = message.get("id")
                pending = self.pending.get(identifier)
                if identifier in self.downloads:
                    expiry = self.downloads.pop(identifier)
                    if expiry:
                        expiry.cancel()
                    if message.get("ok") and (pending is None or pending.done()):
                        self._discard_download(message.get("result"))
                        continue
                if pending is not None and not pending.done():
                    if message.get("ok"):
                        pending.set_result(message.get("result"))
                    else:
                        pending.set_exception(BridgeError(str(message.get("error", "WhatsApp operation failed."))[:300],
                                                          message.get("code"), message.get("status")))
        except (OSError, ValueError):
            pass
        finally:
            for expiry in self.downloads.values():
                if expiry:
                    expiry.cancel()
            self.downloads.clear()
            for pending in self.pending.values():
                if not pending.done():
                    pending.set_exception(RuntimeError("WhatsApp transport stopped before returning a result."))

    async def call(self, op, account, args=None):
        if self.process is None or self.process.returncode is not None or self.reader.done():
            raise BridgeError("WhatsApp transport is stopped. Check the Node dependencies and restart the service.",
                              "unavailable")
        self.sequence += 1
        identifier = str(self.sequence)
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        self.pending[identifier] = pending
        if op == "download_media":
            self.downloads[identifier] = None
        sent, received = False, False
        try:
            self.process.stdin.write(encode_frame({"id": identifier, "op": op, "account": account, "args": args or {}}))
            sent = True
            await self.process.stdin.drain()
            result = await asyncio.wait_for(pending, 180 if op in {"pair", "repair"} else _BRIDGE_TIMEOUT)
            received = True
            return result
        except TimeoutError as error:
            raise RuntimeError("WhatsApp did not confirm the result in time.") from error
        finally:
            self.pending.pop(identifier, None)
            if op == "download_media" and not received:
                if pending.done() and not pending.cancelled() and pending.exception() is None:
                    self._discard_download(pending.result())
                if identifier in self.downloads:
                    if sent:
                        # A Node download normally settles within 30 seconds. Keep only the request ID
                        # for a bounded window to reclaim a file that was already in flight.
                        self.downloads[identifier] = loop.call_later(
                            _BRIDGE_TIMEOUT, self.downloads.pop, identifier, None)
                    else:
                        self.downloads.pop(identifier)

    async def close(self):
        if self.process is None:
            return
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 4)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        await self.reader
