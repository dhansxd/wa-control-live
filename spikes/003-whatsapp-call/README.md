# WhatsApp call adapter

Use the [plugin setup guide](../../README.md) for build, pairing, and lifecycle
commands. The native sidecar handles the linked WhatsApp device, owner
admission, and Opus media. Its local PCM WebSocket connects to the Python
adapter, which uses Codex Realtime over WebRTC. Client-managed delegations
reach the active profile and DM through the stock Hermes gateway.

`hermes wa-control-live serve` starts the unified runtime. The launcher
verifies the selected sidecar's SHA-256 and the saved bot identity. It
manages its child processes and stops them on interruption or plugin
deactivation. Use `hermes wa-control-live status` to inspect the runtime.

## Audio diagnostics

`audio_probe.py` can capture input PCM through the authenticated local
WebSocket route in an isolated diagnostic run. Its reports include sample
offsets, monotonic arrival times, hashes, and amplitude statistics. When
comparing to a native sidecar recording, provide both local WAV files.

```sh
../001-codex-audio/.venv/bin/python audio_probe.py serve \
  --token-file /path/to/adapter.token --runs /path/to/private-runs
../001-codex-audio/.venv/bin/python audio_probe.py analyze \
  /path/to/sidecar-caller.wav /path/to/adapter-caller.wav
```

The adapter receives 24 kHz provider samples and uses 16 kHz PCM for
WhatsApp playout. Its diagnostics record arrivals, queue depth, prefill,
underruns, pacer lateness, and send duration. The native sidecar has an
optional `HWV_RECORD_CALLS=true` setting for private WAV and event traces.
Recordings are bounded to 180 seconds. Trace timestamps and an observed
call are useful for locating a breakup across the recording boundaries.

Task reports distinguish native Hermes completion, the voice context
acknowledgment, and forwarded PCM. Provider WebRTC timing and WhatsApp
relay timing describe separate transport connections.

## Upstream

- [Hermes WhatsApp Voice v0.2.1](https://github.com/ashwanthbalakrishnan5/hermes-whatsapp-voice/tree/ef3b8be4a79651b6c11a720f20a95b9e2939c92e)
- [Hermes Live Voice delegation](https://github.com/Synero/hermes-live-voice/blob/0578e0cc35022a9dcb469cb9fbfc4d8afc93fc65/desktop/plugin.js#L259-L349)
