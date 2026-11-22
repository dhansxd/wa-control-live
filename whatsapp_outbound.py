"""Start or schedule an owner-requested callback through the voice sidecar."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import http.client
import json
import re
import sqlite3

from .plugin_identity import resolve_identity
from .whatsapp_session import resolve_owner_session

TOOL_NAME = 'whatsapp_call_now'
_ALARM_NAME = 'Alarm telepon WhatsApp'
_ALARM_PROMPT = (f'Call the WhatsApp owner now using {TOOL_NAME} with empty arguments. '
                 'Report the status returned by the tool.')
DESCRIPTION = (
    'Call the registered owner on WhatsApp at their request in their DM or during a live call. Interpret their intent '
    'in context, from typed text or a voice message transcript in any language. '
    'Use empty arguments to call now, or pass schedule for a requested one-time alarm. '
    'Resolve calendar dates against the time of the spoken request. For a spoken relative delay, use in 10m or similar. '
    'When executing the alarm in Hermes cron, use empty arguments to place the call. '
    'scheduled means a future call was scheduled. '
    'preparing_audio means the request was accepted and audio is being prepared before dialing. '
    'dial_requested means the call request was accepted. already_attempted means no new call was sent. '
    'denied means no call was started by the tool; busy means the line already has a call. '
    'Report only the confirmed status. Ringing and answer require their own confirmation.'
)
SCHEMA = {
    'name': TOOL_NAME,
    'description': DESCRIPTION,
    'parameters': {
        'type': 'object',
        'properties': {
            'schedule': {
                'type': 'string',
                'description': 'For a one-time alarm, an ISO 8601 date and time (include the user’s timezone) '
                               'or a Hermes relative delay such as "in 10m". Omit to call now.',
            },
        },
        'additionalProperties': False,
    },
}


def call_now_hint(*, platform, **_):
    if platform != 'whatsapp':
        return None
    from gateway.session_context import get_session_env
    from gateway.session_ingress import executing_admission, viewer_turn

    if not executing_admission.get() or viewer_turn.get() is not None:
        return None
    if get_session_env('HERMES_SESSION_CHAT_TYPE') != 'dm':
        return None
    arguments = json.dumps({'calls': [{'name': TOOL_NAME, 'arguments': {}}]})
    return {'context': (
        'Giliran ini adalah balasan di chat WhatsApp. Pesan suara di DM juga bagian dari chat. '
        'Pahami maksud pesan terbaru dengan konteks percakapan, dalam bahasa apa pun. '
        f'Jika pemilik meminta ditelepon sekarang, gunakan {TOOL_NAME} dengan argumen kosong. '
        f'Jika pemilik meminta dijadwalkan satu panggilan, gunakan {TOOL_NAME} dengan schedule. '
        f'Jika tool dimuat sesuai kebutuhan, gunakan tool_call seperti {arguments} '
        'dengan argumen yang sesuai untuk permintaan itu. '
        'Konfirmasi waktu dari scheduled_at hanya setelah status scheduled. '
        'Pertanyaan tentang fitur telepon dan kutipan cukup dibalas di chat. '
        'Jika statusnya preparing_audio, sampaikan bahwa audio sedang disiapkan. '
        'Jika statusnya dial_requested, sampaikan bahwa permintaan telepon diterima. '
        'Sebut panggilan berdering atau sudah dijawab hanya jika status panggilan mengonfirmasi.'
    )}


def _owner_request(home, owner, session_id):
    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import bound_session_key, get_session_env
    from gateway.whatsapp_identity import canonical_whatsapp_identifier
    from hermes_state import SessionDB
    from hermes_state_runtime import find_session_admission

    route = bound_session_key()
    chat, sender, message, profile = (
        get_session_env('HERMES_SESSION_' + key) for key in ('CHAT_ID', 'USER_ID', 'MESSAGE_ID', 'PROFILE')
    )
    if (not route or not message or is_delegated_child_context()
            or get_session_env('HERMES_SESSION_PLATFORM') != 'whatsapp'
            or get_session_env('HERMES_SESSION_CHAT_TYPE') != 'dm'
            or get_session_env('HERMES_SESSION_THREAD_ID')):
        return None
    owner_id = canonical_whatsapp_identifier(owner)
    if not owner_id or any(canonical_whatsapp_identifier(value) != owner_id for value in (chat, sender)):
        return None
    target = resolve_owner_session(home, owner)
    principal = 'messaging:' + json.dumps([profile or None, 'whatsapp', chat, None, sender],
                                          separators=(',', ':'))
    with closing(SessionDB(home / 'state.db', read_only=True)) as db:
        lineage = db.get_compression_lineage(target) or [target]
        if session_id not in lineage or get_session_env('HERMES_SESSION_ID') not in lineage:
            return None
        row = find_session_admission(db, principal_id=principal, session_id=lineage[0], request_id=message)
        if not row or row['status'] != 'started':
            return None
        native = row['payload'].get('native_text_v1', {})
        if (native.get('route') != route or native.get('event', {}).get('message_id') != message
                or native.get('automation')):
            return None
        return row['admission_id']


def _live_request_time(home, admission_id):
    from hermes_state import SessionDB
    from hermes_state_runtime import get_session_admission
    from .live_agent import VOICE_PREFIX, VOICE_REQUEST_TIME

    with closing(SessionDB(home / 'state.db', read_only=True)) as db:
        row = get_session_admission(db, admission_id=admission_id)
    note = row['payload'].get('surface_v1', {}).get('voice_context', '') if row else ''
    prefix = VOICE_PREFIX + VOICE_REQUEST_TIME
    if note.startswith(prefix):
        when = datetime.fromisoformat(note[len(prefix):].split('\n', 1)[0])
        if when.tzinfo is None:
            raise ValueError('Missing Live request timezone')
        return when
    return None


def _schedule(state, identity, admission_id, schedule, *, runtime, spoken_at=None):
    from cron.jobs import get_job, parse_duration, parse_schedule
    from hermes_time import now as hermes_now
    from cron.occurrences import scheduled_instant
    from tools.cronjob_tools import cronjob

    try:
        parsed = parse_schedule(schedule)
    except ValueError as exc:
        return {'status': 'invalid_schedule', 'error': str(exc)}
    if parsed['kind'] != 'once':
        return {'status': 'invalid_schedule', 'error': 'An alarm needs a single date and time.'}
    when = parsed['run_at']
    if spoken_at is not None and schedule.lower().startswith('in '):
        minutes = parse_duration(schedule[3:].strip())
        when = (spoken_at.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(spoken_at.tzinfo).isoformat()
    if not (runtime / 'config.json').is_file():
        return {'status': 'unavailable'}
    with (runtime / 'call-now.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (runtime / 'config.json').is_file():
            return {'status': 'unavailable'}
        alarms = state.get('call_alarms', {})
        for job_id, alarm in alarms.items():
            if (alarm['admission_id'], alarm['schedule']) == (admission_id, schedule):
                job = get_job(job_id)
                if alarm.get('attempted') or (job and job['enabled']):
                    return {'status': 'scheduled', 'job_id': job_id, 'scheduled_at': alarm['when']}
                return {'status': 'schedule_failed', 'job_id': job_id}
        if spoken_at is not None and datetime.fromisoformat(when) <= hermes_now():
            return {'status': 'invalid_schedule', 'error': 'The requested callback time has already passed.'}
        # Persist the owner's permission before the scheduler can fire the job.
        result = json.loads(cronjob(
            'create', schedule=when, name=_ALARM_NAME, deliver='origin', prompt=_ALARM_PROMPT,
            enabled_toolsets=['wa-control-live'], paused=True,
        ))
        if not result.get('success'):
            return {'status': 'schedule_failed', 'error': result.get('error')}
        job_id = result['job_id']
        alarms[job_id] = {'identity': identity, 'instant': scheduled_instant(when), 'when': when,
                          'admission_id': admission_id, 'schedule': schedule}
        state.set('call_alarms', alarms)
        result = json.loads(cronjob('resume', job_id=job_id))
        if not result.get('success'):
            return {'status': 'schedule_failed', 'job_id': job_id, 'error': result.get('error')}
        return {'status': 'scheduled', 'job_id': job_id, 'scheduled_at': when}


def remove_profile_call_alarms(ctx, home):
    """Retire recorded call alarms while preserving any execution or output history."""
    from hermes_constants import get_hermes_home

    home = home.resolve()
    if get_hermes_home().resolve() != home:
        raise RuntimeError('Cannot clean up call alarms from another Hermes profile.')
    results = {'removed': 0, 'paused': 0, 'retained': 0, 'unverified': 0}
    state_file = ctx.state.data_dir / 'state.json'
    if state_file.is_symlink() or state_file.with_name('.state.json.lock').is_symlink():
        raise ValueError('Cannot verify the local state that owns the call alarms.')
    if not state_file.is_file():
        return results
    alarms = ctx.state.get('call_alarms', {})
    if not isinstance(alarms, dict):
        raise ValueError('Cannot verify the recorded call alarms for this profile.')
    if not alarms:
        return results

    from cron.executions import latest_execution
    from cron.jobs import _jobs_lock, get_cron_output_dir, get_job, pause_job, remove_job, use_cron_store
    from cron.notepad import list_notes
    from cron.occurrences import scheduled_instant
    from gateway.whatsapp_identity import canonical_whatsapp_identifier

    output_root = get_cron_output_dir()
    cron_root = home / 'cron'
    if (output_root != cron_root / 'output' or cron_root.is_symlink() or output_root.is_symlink()
            or any((cron_root / name).is_symlink() for name in ('jobs.json', '.jobs.lock'))):
        raise RuntimeError('Cannot verify the Hermes profile that owns the call alarm jobs.')
    history_unverified = any((cron_root / name).is_symlink() for name in ('executions.db', 'notepad.db'))

    # Native remove/pause accept either ID or name. Hold the cron write lock
    # across the exact-ID lookup and mutation so a concurrently removed job
    # cannot fall back to an unrelated job whose name happens to equal its ID.
    with use_cron_store(home), _jobs_lock():
        for job_id, alarm in alarms.items():
            identity = alarm.get('identity') if isinstance(alarm, dict) else None
            if (not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{12}', job_id)
                    or not isinstance(identity, dict) or identity.get('hermes_home') != str(home)):
                results['unverified'] += 1
                continue
            job = get_job(job_id)
            if job is None:
                continue
            schedule = job.get('schedule')
            origin = job.get('origin')
            owner = canonical_whatsapp_identifier(identity.get('owner_number'))
            instant = scheduled_instant(schedule.get('run_at')) if isinstance(schedule, dict) else None
            if (job.get('id') != job_id or job.get('name') != _ALARM_NAME
                    or job.get('prompt') != _ALARM_PROMPT or job.get('deliver') != 'origin'
                    or job.get('enabled_toolsets') != ['wa-control-live'] or not instant
                    or not isinstance(origin, dict) or origin.get('platform') != 'whatsapp'
                    or origin.get('thread_id') or not owner
                    or canonical_whatsapp_identifier(origin.get('chat_id')) != owner
                    or schedule.get('kind') != 'once' or instant != alarm.get('instant')):
                results['unverified'] += 1
                continue
            output = output_root / job_id
            repeat = job.get('repeat')
            completed = repeat.get('completed') if isinstance(repeat, dict) else None
            if (alarm.get('attempted') or job.get('last_run_at') or job.get('last_status')
                    or job.get('run_claim') or job.get('fire_claim') or completed != 0
                    or history_unverified or output.is_symlink() or output.exists()
                    or latest_execution(job_id) is not None or list_notes(job_id)):
                if not job.get('enabled'):
                    results['retained'] += 1
                elif pause_job(job_id, reason='WA Control & Live data cleanup') is not None:
                    results['paused'] += 1
                else:
                    results['unverified'] += 1
            elif remove_job(job_id):
                results['removed'] += 1
            else:
                results['unverified'] += 1
    return results


def _dial(state, identity, admission_id, *, runtime, execution=None):
    # Record the attempt before the sidecar request so an ambiguous response cannot redial.
    with (runtime / 'call-now.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if execution is None:
            recent = state.get('call_now_recent_admissions', [])
        else:
            alarms = state.get('call_alarms', {})
            alarm = alarms.get(execution.job_id)
            if (not alarm or alarm['identity'] != identity
                    or alarm['instant'] != execution.scheduled_instant):
                return {'status': 'denied'}
            if alarm.get('attempted'):
                return {'status': 'already_attempted'}
        try:
            serving = json.loads((runtime / 'serving-identity.json').read_text())
            token = (runtime / 'adapter.token').read_text().strip()
        except (OSError, ValueError):
            return {'status': 'unavailable'}
        if serving != identity:
            return {'status': 'voice_restart_required'}
        if not token:
            return {'status': 'unavailable'}
        if execution is None:
            if admission_id in recent:
                return {'status': 'already_attempted'}
            state.set('call_now_recent_admissions', [*recent[-31:], admission_id])
        else:
            alarm['attempted'] = True
            state.set('call_alarms', alarms)
        with closing(http.client.HTTPConnection('127.0.0.1', 8796, timeout=4)) as conn:
            conn.request('POST', '/calls', json.dumps({'reason': 'Panggilan atas permintaanmu di WhatsApp'}),
                         {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
            response = conn.getresponse()
            if response.status != 202:
                return {'status': {409: 'busy', 503: 'offline'}.get(response.status, 'unavailable')}
            record = json.loads(response.read(65536))
            if (isinstance(record, dict) and record.get('direction') == 'outbound'
                    and isinstance(record.get('id'), str) and record['id']):
                status = 'preparing_audio' if record.get('status') == 'preparing' else 'dial_requested'
                return {'status': status, 'call_id': record['id']}
            return {'status': 'unknown'}


def call_now(ctx, args, *, session_id):
    from gateway.session_ingress import viewer_turn
    from hermes_constants import get_hermes_home
    from . import _configured_pair, _live_enabled
    from .whatsapp_owner_authorization import owner_send_request

    if not session_id or not isinstance(args, dict) or args.keys() - {'schedule'}:
        return {'status': 'denied'}
    execution = ctx.current_cron_execution()
    if execution is not None and (args or not execution.scheduled_instant):
        return {'status': 'denied'}
    schedule = args.get('schedule')
    if 'schedule' in args and (not isinstance(schedule, str) or not schedule.strip()):
        return {'status': 'invalid_schedule'}
    home = get_hermes_home().resolve()
    try:
        pair = _configured_pair(ctx)
        owner, bot = pair or resolve_identity(home)
        runtime = ctx.state.data_dir / 'unified' if pair else ctx.state.data_dir
        admission_id = None
        spoken_at = None
        if execution is None:
            if viewer_turn.get() is None:
                admission_id = _owner_request(home, owner, session_id)
            elif verified := owner_send_request(home, owner, session_id, runtime):
                admission_id, _ = verified
                if schedule:
                    spoken_at = _live_request_time(home, admission_id)
            if not admission_id:
                return {'status': 'denied'}
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return {'status': 'unavailable'}

    if not _live_enabled(ctx):
        return {'status': 'live_setup_deferred'}
    identity = {'owner_number': owner, 'bot_number': bot, 'hermes_home': str(home)}
    try:
        if schedule is not None:
            return _schedule(ctx.state, identity, admission_id, schedule.strip(), runtime=runtime, spoken_at=spoken_at)
        return _dial(ctx.state, identity, admission_id, runtime=runtime, execution=execution)
    except ConnectionRefusedError:
        return {'status': 'offline'}
    except (OSError, RuntimeError, ValueError, http.client.HTTPException):
        return {'status': 'unknown'}
