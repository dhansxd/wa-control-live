"""Run Hermes's synchronous TTS tool in a cancellable, profile-bound child."""

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Thread

from .runtime import activation_ids, active_install


_MAX_FRAME = 64 * 1024
_RESPONSE = "hermes-tts-result.json"
_TOOL = "text_to_speech"


def _identity(path):
    info = path.stat()
    return [str(path), info.st_dev, info.st_ino]


def _install_paths(entry):
    import hermes_constants

    return Path(hermes_constants.__file__).resolve(), Path(entry).resolve()


def _command(host, home, action="_speech"):
    # Boot the documented Python CLI with the already-running interpreter and
    # the running installation on its import path, including managed installs.
    # An explicit default selects the supplied root despite sticky active_profile.
    profile = [] if home.parent.name == "profiles" else ["-p", "default"]
    bootstrap = ('import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); '
                 'runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)')
    return [sys.executable, "-I", "-B", "-c", bootstrap, str(host.parent), *profile, "whatsapp-control", action]


def _read_response(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing Hermes speech result")
    with path.open("rb") as stream:
        data = stream.read(_MAX_FRAME + 1)
    if len(data) > _MAX_FRAME:
        raise ValueError("Hermes speech result is too large")
    return json.loads(data)


async def _finish(task):
    # Stop/disable/purge can cancel the same job during spawn or process reaping.
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _run_child(argv, env, data):
    launching = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    ))
    process = None
    try:
        process = await asyncio.shield(launching)
        process.stdin.write(data + b"\n")
        await process.stdin.drain()
        if await process.wait() != 0:
            raise ValueError("Hermes speech synthesis failed")
    except BaseException:
        with suppress(OSError):
            process = await _finish(launching)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await _finish(asyncio.create_task(process.wait()))
        raise
    finally:
        if process is not None:
            process.stdin.close()


def _stop_on_parent_exit(pipe):
    # The worker keeps stdin open while it waits. EOF also covers abrupt worker death.
    try:
        os.read(pipe, 1)
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)


def speech_dispatch(entry, root):
    home = root.resolve().parents[1]
    paths = _install_paths(entry)
    identities = [_identity(path) for path in paths]
    argv = _command(paths[0], home)

    async def dispatch(name, payload):
        if name != _TOOL:
            raise ValueError("Unsupported speech tool")
        directory = Path(payload["output_path"]).resolve().parent
        with tempfile.TemporaryDirectory(prefix="tts-", dir=directory) as scratch:
            data = json.dumps({"home": str(home), "identity": identities, "scratch": scratch,
                               "payload": payload}).encode("utf-8")
            if len(data) > _MAX_FRAME:
                raise ValueError("Hermes speech request is too large")
            env = {**os.environ, "HERMES_HOME": str(home), "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch}
            await _run_child(argv, env, data)
            return _read_response(Path(scratch) / _RESPONSE)

    return dispatch


def run_speech(ctx, entry, root):
    raw = sys.stdin.buffer.readline(_MAX_FRAME + 2)
    if not raw.endswith(b"\n") or len(raw) > _MAX_FRAME + 1 or os.getpgrp() != os.getpid():
        raise ValueError("Invalid Hermes speech request")
    request = json.loads(raw)
    if (not isinstance(request, dict) or request.get("home") != str(root.resolve().parents[1])
            or request.get("identity") != [_identity(path) for path in _install_paths(entry)]
            or not active_install(entry, activation_ids(ctx))):
        raise ValueError("Hermes installation or profile changed during speech synthesis")
    payload = request.get("payload")
    if (not isinstance(payload, dict) or set(payload) != {"text", "output_path"}
            or not isinstance(payload["text"], str) or not payload["text"].strip()
            or not isinstance(payload["output_path"], str) or not isinstance(request.get("scratch"), str)):
        raise ValueError("Invalid Hermes speech request")
    output = Path(payload["output_path"])
    media = (root / "bridge" / "media").resolve()
    directory = output.parent.resolve(strict=True)
    scratch = Path(request["scratch"]).resolve(strict=True)
    if (not output.is_absolute() or output.name != "synthesized.mp3" or output.is_symlink()
            or directory.parent != media or not directory.name.startswith("speech-")
            or scratch.parent != directory or not scratch.name.startswith("tts-") or not scratch.is_dir()):
        raise ValueError("Invalid Hermes speech output directory")

    # Hermes loads the profile's .env at startup. Keep this invocation's temporary files together.
    os.environ.update(TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch))
    tempfile.tempdir = str(scratch)
    pipe = os.dup(sys.stdin.fileno())
    with open(os.devnull, "rb") as empty:
        os.dup2(empty.fileno(), sys.stdin.fileno())
    Thread(target=_stop_on_parent_exit, args=(pipe,), daemon=True).start()
    from tools.registry import discover_builtin_tools, registry

    if registry.get_entry(_TOOL) is None:
        discover_builtin_tools()
    result = ctx.dispatch_tool(_TOOL, {"text": payload["text"], "output_path": str(directory / output.name)})
    data = (result if isinstance(result, str) else json.dumps(result)).encode("utf-8")
    if len(data) > _MAX_FRAME:
        raise ValueError("Hermes speech result is too large")
    with (scratch / _RESPONSE).open("xb") as stream:
        stream.write(data)
