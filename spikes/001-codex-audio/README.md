# Codex Realtime audio probe

This standalone experiment sends a prerecorded mono PCM16 WAV file to Codex
Realtime and writes received audio to a local report directory. It uses the
existing ChatGPT sign-in in Codex CLI and WebRTC. The probe records the
provider transcript when available, received audio, and cleanup outcomes.

The pinned implementation uses a client-managed v3 session through
`thread/realtime/start`. It checks account status through app-server,
negotiates SDP, and closes the owned realtime session, peer, and app-server
process. The account rate-limit reading, when available, is rounded.

## Run

Prepare an isolated Python 3.12 environment in this directory. Supply a local
WAV fixture and a fresh output directory for each run. The probe uses the live
provider and consumes subscription allowance.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
env PYTHONPATH= .venv/bin/python probe.py /path/to/input.wav \
  --output-dir runs/NEW --timeout 45
```

The offline tests exercise synthetic media, worker failures, and cleanup.

```sh
env PYTHONPATH= PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest -q test_probe.py
```

## References

- [Codex app-server](https://developers.openai.com/codex/app-server)
- [Pinned Codex Realtime source](https://raw.githubusercontent.com/openai/codex/rust-v0.147.0/codex-rs/core/src/realtime_conversation.rs)
- [Hermes Live Voice protocol example](https://github.com/Synero/hermes-live-voice/blob/0578e0cc35022a9dcb469cb9fbfc4d8afc93fc65/dashboard/plugin_api.py)
- [Hermes Talk audio helpers](https://github.com/TheSmokeDev/hermes-talk/blob/981711b8f44df78e2ad958174e931c1b584b8bdd/talk_live_audio.py)
