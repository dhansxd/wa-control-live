import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "wa_speech_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)],
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
from wa_speech_test import speech


class SpeechProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_after_spawn_reaps_child_before_returning(self):
        with tempfile.TemporaryDirectory() as temporary:
            launch_started = asyncio.Event()
            continue_launch = asyncio.Event()
            processes = []
            spawn = asyncio.create_subprocess_exec

            async def delayed_spawn(*args, **kwargs):
                process = await spawn(*args, **kwargs)
                processes.append(process)
                launch_started.set()
                await continue_launch.wait()
                return process

            env = {**os.environ, "TMPDIR": temporary}
            argv = [sys.executable, "-c", "import time; time.sleep(60)"]
            with patch.object(speech.asyncio, "create_subprocess_exec", delayed_spawn):
                task = asyncio.create_task(speech._run_child(argv, env, b"synthetic"))
                try:
                    await asyncio.wait_for(launch_started.wait(), 5)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                finally:
                    continue_launch.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            self.assertEqual(len(processes), 1)
            self.assertLess(processes[0].returncode, 0)

    async def test_child_receives_prompt_only_through_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / "result"
            fixture = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.readline())"
            argv = [sys.executable, "-c", fixture, str(result)]
            await speech._run_child(argv, {**os.environ, "TMPDIR": temporary}, b"synthetic speech")
            self.assertEqual(result.read_bytes(), b"synthetic speech\n")
