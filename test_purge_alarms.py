"""Exercise call-alarm cleanup with Hermes cron storage in a temporary profile."""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def profile(tmp_path, monkeypatch):
    checkout = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(checkout.parent))
    outbound = import_module(f'{checkout.name}.whatsapp_outbound')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from hermes_cli.plugins_state import PluginState

    return tmp_path, outbound, SimpleNamespace(state=PluginState('wa-control-live-test'))


def test_purge_only_retires_owned_alarms_and_keeps_job_output(profile):
    from cron import jobs
    from cron.executions import create_execution, latest_execution
    from cron.occurrences import scheduled_instant

    home, outbound, ctx = profile

    def alarm(**overrides):
        return jobs.create_job(**{
            'name': outbound._ALARM_NAME, 'prompt': outbound._ALARM_PROMPT,
            'schedule': 'in 10m', 'deliver': 'origin', 'enabled_toolsets': ['wa-control-live'],
            'origin': {'platform': 'whatsapp', 'chat_id': '15550100001'}, **overrides,
        })['id']

    def proof(job_id, profile_home=home):
        return {'identity': {'hermes_home': str(profile_home), 'owner_number': '15550100001'},
                'instant': scheduled_instant(jobs.get_job(job_id)['schedule']['run_at'])}

    pending = alarm()
    with_output = alarm()
    redirected_output = alarm()
    with_execution = alarm()
    foreign = alarm()
    changed = alarm(prompt='Independent callback job')
    readdressed = alarm(origin={'platform': 'whatsapp', 'chat_id': '15550100002'})
    other = alarm()
    output = jobs.save_job_output(with_output, 'retained output')
    execution = create_execution(with_execution, source='builtin')
    foreign_output = jobs.save_job_output(foreign, 'other output')
    (jobs.get_cron_output_dir() / redirected_output).symlink_to(foreign_output.parent, target_is_directory=True)
    ctx.state.set('call_alarms', {
        pending: proof(pending), with_output: proof(with_output), with_execution: proof(with_execution),
        foreign: proof(foreign, home / 'profiles' / 'other'), changed: proof(changed),
        redirected_output: proof(redirected_output),
        readdressed: proof(readdressed),
    })

    assert outbound.remove_profile_call_alarms(ctx, home) == {
        'removed': 1, 'paused': 3, 'retained': 0, 'unverified': 3,
    }
    assert jobs.get_job(pending) is None
    assert not jobs.get_job(with_output)['enabled'] and not jobs.get_job(with_execution)['enabled']
    assert not jobs.get_job(redirected_output)['enabled']
    assert output.read_text() == 'retained output'
    assert latest_execution(with_execution)['id'] == execution['id']
    assert all(jobs.get_job(job_id)['enabled'] for job_id in (foreign, changed, readdressed, other))
    assert foreign_output.read_text() == 'other output'


def test_purge_refuses_a_redirected_cron_store(profile):
    from cron import jobs

    home, outbound, ctx = profile
    ctx.state.set('call_alarms', {'0123456789ab': {'identity': {'hermes_home': str(home)}}})
    target = home / 'other-output'
    target.mkdir()
    target.chmod(0o755)
    (home / 'cron').mkdir()
    jobs.get_cron_output_dir().symlink_to(target, target_is_directory=True)

    with pytest.raises(RuntimeError, match='Cannot verify the Hermes profile'):
        outbound.remove_profile_call_alarms(ctx, home)
    assert not (home / 'cron' / '.jobs.lock').exists()
    assert target.stat().st_mode & 0o777 == 0o755


def test_purge_leaves_no_late_alarm_after_runtime_is_removed(profile, monkeypatch):
    from cron import jobs

    home, outbound, ctx = profile
    runtime = ctx.state.data_dir
    identity = {'hermes_home': str(home)}
    assert outbound._schedule(ctx.state, identity, 'late', 'in 10m', runtime=runtime) == {
        'status': 'unavailable',
    }
    assert not runtime.exists()

    runtime.mkdir(parents=True)
    config = runtime / 'config.json'
    config.touch()
    with monkeypatch.context() as waiting:
        waiting.setattr(outbound.fcntl, 'flock', lambda *_args: config.unlink())
        assert outbound._schedule(ctx.state, identity, 'late', 'in 10m', runtime=runtime) == {
            'status': 'unavailable',
        }
    assert jobs.load_jobs() == []
