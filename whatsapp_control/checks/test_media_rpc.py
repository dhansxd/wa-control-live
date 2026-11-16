import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "media_rpc_plugin", PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)]
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
from media_rpc_plugin.runtime import Bridge


class MediaRpcTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "plugin-data"
        self.media = self.root / "bridge" / "media"
        self.media.mkdir(parents=True)
        self.filename = "5fe02e5a-8dea-491f-a8b1-c7f81d947072.jpg"
        self.bridge = Bridge(self.root, lambda event: None)
        self.requests = asyncio.Queue()
        self.output = asyncio.StreamReader()
        self.sent = asyncio.Event()
        self.sent.set()
        self.handled = asyncio.Event()
        self.bridge.on_event = lambda event: self.handled.set()
        self.bridge.process = SimpleNamespace(
            returncode=None, stdout=self.output,
            stdin=SimpleNamespace(write=lambda data: self.requests.put_nowait(json.loads(data)), drain=self.sent.wait),
        )
        self.bridge.reader = asyncio.create_task(self.bridge._read())
        self.addAsyncCleanup(self.finish_reader)

    async def finish_reader(self):
        self.output.feed_eof()
        await self.bridge.reader

    async def request(self, operation="download_media"):
        task = asyncio.create_task(self.bridge.call(operation, "shop"))
        self.addAsyncCleanup(self.cancel, task)
        frame = await asyncio.wait_for(self.requests.get(), timeout=1)
        return task, frame["id"]

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def reply(self, identifier, path):
        self.handled.clear()
        frames = (
            {"id": identifier, "ok": True, "result": {"type": "image", "path": str(path)}},
            {"event": "fixture_barrier"},
        )
        self.output.feed_data("".join(json.dumps(frame) + "\n" for frame in frames).encode())
        await asyncio.wait_for(self.handled.wait(), timeout=1)

    async def test_late_abandoned_download_is_discarded(self):
        task, identifier = await self.request()
        await self.cancel(task)
        expiry = self.bridge.downloads[identifier]
        self.assertLessEqual(expiry.when() - asyncio.get_running_loop().time(), 50)
        path = self.media / self.filename
        path.write_bytes(b"synthetic image")
        await self.reply(identifier, path)
        self.assertFalse(path.exists())
        self.assertTrue(expiry.cancelled())
        self.assertNotIn(identifier, self.bridge.downloads)

    async def test_cancel_after_reply_arrives_keeps_no_download(self):
        self.sent.clear()
        task, identifier = await self.request()
        path = self.media / self.filename
        path.write_bytes(b"synthetic image")
        await self.reply(identifier, path)
        self.assertTrue(path.exists())
        await self.cancel(task)
        self.assertFalse(path.exists())

    async def test_successful_download_passes_ownership_to_caller(self):
        task, identifier = await self.request()
        path = self.media / self.filename
        path.write_bytes(b"synthetic image")
        await self.reply(identifier, path)
        self.assertEqual((await task)["path"], str(path))
        self.assertTrue(path.exists())
        self.assertNotIn(identifier, self.bridge.downloads)

    async def test_cleanup_rejects_other_replies_paths_and_symlinks(self):
        outside = Path(self.temp.name) / self.filename
        outside.write_bytes(b"unrelated file")
        symlink = self.media / self.filename
        symlink.symlink_to(outside)
        unexpected = self.media / "preferences.bin"
        unexpected.write_bytes(b"unrelated media directory file")
        for path in (outside, symlink, unexpected):
            task, identifier = await self.request()
            await self.cancel(task)
            await self.reply(identifier, path)
            self.assertTrue(path.exists())

        symlink.unlink()
        alias = self.media / self.filename
        alias.write_bytes(b"node-shaped name from another operation")
        task, identifier = await self.request("status")
        await self.cancel(task)
        await self.reply(identifier, alias)
        self.assertTrue(alias.exists())

    async def test_cleanup_does_not_follow_replaced_media_directory(self):
        outside = Path(self.temp.name) / "unrelated"
        outside.mkdir()
        outside_file = outside / self.filename
        outside_file.write_bytes(b"unrelated file")
        self.media.rmdir()
        self.media.symlink_to(outside, target_is_directory=True)
        task, identifier = await self.request()
        await self.cancel(task)
        await self.reply(identifier, self.media / self.filename)
        self.assertTrue(outside_file.exists())
