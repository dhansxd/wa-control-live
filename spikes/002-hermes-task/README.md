# Hermes voice task probe

This experiment runs one file task from a prerecorded voice prompt. Codex
Realtime issues `delegation.created`, Hermes performs the task with the
configured model and native file tools, and the adapter sends the result back
with `delegation.context.append`. The experiment records the gateway
receipt, tool results, return acknowledgment, and received audio.

## Run

Use the isolated media environment from the [audio probe](../001-codex-audio/README.md).
Supply an input WAV and a fresh output directory. This uses the live Codex
subscription and creates a Hermes task in the active environment.

```sh
env PYTHONPATH= ../001-codex-audio/.venv/bin/python task_probe.py /path/to/input.wav \
  --output-dir runs/NEW
```

Use `--completed-store PATH` for playback of an already verified result.
The stored receipt associates a delegation with its original native Hermes
session and admission. Reconciliation validates the exact prompt, task
result, tool calls, and artifact using a read-only database snapshot before
updating the probe's own receipt. An unresolved task can be reconciled after
native completion if its CLI process was interrupted.

The offline checks cover task handoff, duplicate deliveries, receipt
recovery, and matching the acknowledgment to the delegation.

```sh
env PYTHONPATH= PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  ../001-codex-audio/.venv/bin/python -m pytest -q \
  test_handoff.py test_task_probe.py ../001-codex-audio/test_probe.py
```

## References

- [Hermes CLI](https://hermes-agent.nousresearch.com/docs/user-guide/cli)
- [Hermes Live Voice](https://github.com/Synero/hermes-live-voice/tree/0578e0cc35022a9dcb469cb9fbfc4d8afc93fc65)
- [Codex app-server](https://developers.openai.com/codex/app-server)
