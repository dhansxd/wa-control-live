"""Bind WhatsApp sends to the current owner turn in the serving Hermes gateway."""

from contextlib import closing
import hmac
import json
import os
import re
import sqlite3

from .whatsapp_session import resolve_owner_session


_LIVE_TAG = 'wa-control-live'
_LIVE_REQUEST = re.compile(r'wa-control-live:([0-9a-f]{32}):[0-9a-f]{64}')


def record_live_submission(runtime, session_id, submission_id, text):
    """Sign the submission ID without putting a credential in the model context."""
    token = (runtime / 'adapter.token').read_bytes().strip()
    if not token:
        raise ValueError('The WhatsApp transport identity is unavailable.')
    payload = json.dumps([_LIVE_TAG, session_id, submission_id, text], ensure_ascii=False,
                         separators=(',', ':')).encode()
    signature = hmac.digest(token, payload, 'sha256').hex()
    return f'{_LIVE_TAG}:{submission_id}:{signature}'


def _native_words(db, lineage, event, native, text):
    if native.get('message_type') != 'voice' or not native.get('media'):
        return text
    transcripts = getattr(event, '_gateway_pending_stt_transcripts', None)
    if transcripts:
        return '\n\n'.join([*transcripts, text]).strip()

    # Hermes persists the current voice note's transcribed prompt before inference.
    # Read it by its authentic platform ID, never by transcript recency.
    for session_id in reversed(lineage):
        row = db._read_one(
            "SELECT content FROM messages WHERE session_id=? AND platform_message_id=? "
            "AND role='user' AND active=1 ORDER BY id DESC LIMIT 1",
            (session_id, event.message_id),
        )
        if row is None:
            continue
        prepared = db._decode_content(row[0])
        if not isinstance(prepared, str):
            break
        quoted = native['event'].get('reply_to_text')
        if quoted and native['event'].get('reply_to_message_id'):
            author = ' your previous message' if native['event'].get('reply_to_is_own_message') else ''
            prefix = f'[Replying to{author}: "{quoted}"]\n\n'
            if not prepared.startswith(prefix):
                break
            prepared = prepared[len(prefix):]
        if text.strip():
            if not prepared.endswith('\n\n' + text):
                break
            prepared = prepared[:-len(text) - 2]
        if prepared.startswith('"') and prepared.endswith('"'):
            return '\n\n'.join([prepared[1:-1], text]).strip()
        break
    return text


def owner_send_request(home, owner, session_id, runtime, *, allow_empty=False):
    """Return the durable admission and authentic owner's text for this tool invocation."""
    from agent.delegation_context import is_delegated_child_context
    from gateway.session_context import _SESSION_TURN_EVENT, bound_session_key, get_session_env
    from gateway.session_ingress import executing_admission, viewer_turn
    from gateway.whatsapp_identity import canonical_whatsapp_identifier
    from hermes_cli.gateway_runtime import gateway_profile_home
    from hermes_state import SessionDB
    from hermes_state_runtime import find_session_admission, get_session_admission

    event = _SESSION_TURN_EVENT.get()
    source = getattr(event, 'source', None)
    owner_id = canonical_whatsapp_identifier(owner)
    route = bound_session_key()
    if (not executing_admission.get() or is_delegated_child_context() or not route or not owner_id
            or not event or event.internal or not event.message_id or source is None
            or source.platform.value != 'whatsapp' or source.chat_type != 'dm' or source.thread_id
            or any(canonical_whatsapp_identifier(value) != owner_id for value in (
                source.chat_id, source.user_id, event.sender_id(), get_session_env('HERMES_SESSION_ACTOR_ID'),
            ))):
        return None

    try:
        target = resolve_owner_session(home, owner)
        with closing(SessionDB(home / 'state.db', read_only=True)) as db:
            lineage = db.get_compression_lineage(target) or [target]
            if (session_id not in lineage or get_session_env('HERMES_SESSION_ID') not in lineage
                    or (db.get_session(target) or {}).get('session_key') != route):
                return None
            is_viewer = viewer_turn.get() is not None
            if not is_viewer:
                principal = 'messaging:' + json.dumps(
                    [source.profile, 'whatsapp', source.chat_id, None, source.user_id], separators=(',', ':'),
                )
                request_id = event.message_id
                if event.edit_revision:
                    request_id += ':edit:' + event.edit_revision
                row = find_session_admission(db, principal_id=principal, session_id=lineage[0],
                                             request_id=request_id)
            else:
                row = get_session_admission(db, admission_id=event.message_id)
            if not row or row['status'] != 'started' or row['target_session_id'] != lineage[0]:
                return None
            payload = row['payload']
            text = payload.get('text')
            if not isinstance(text, str):
                return None
            if not is_viewer:
                native = payload.get('native_text_v1') or {}
                if (native.get('route') != route or native.get('event', {}).get('message_id') != event.message_id
                        or native.get('automation')):
                    return None
                text = _native_words(db, lineage, event, native, text)
            else:
                principal = f'uid:{os.getuid()}'
                signed = _LIVE_REQUEST.fullmatch(row['request_id'])
                if (not signed or row['principal_id'] != principal or 'native_text_v1' in payload
                        or (payload.get('surface_v1') or {}).get('surface') != 'voice-live'
                        or payload.get('local_operator_v1') != {
                            'principal_id': principal, 'profile_id': str(gateway_profile_home(home)),
                            'session_id': lineage[0],
                        }
                        or not hmac.compare_digest(row['request_id'], record_live_submission(
                            runtime, lineage[0], signed.group(1), text,
                        ))):
                    return None
            if text.strip():
                return row['admission_id'], text
            if allow_empty and not is_viewer:
                return row['admission_id'], ''
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return None
