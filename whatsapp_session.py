"""Resolve the active WhatsApp DM and persist Live turns in Hermes storage."""

from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
import json
from pathlib import Path
import re
import sqlite3
from threading import Event


VOICE_CALL_KIND = 'voice_call'
_MULTIPLEX_ROUTING = ContextVar('whatsapp_multiplex_routing', default=None)


@contextmanager
def use_multiplex_routing(home: Path, control_home: Path, scope: str):
    """Bind the index location reported by this worker's authenticated gateway."""
    if not isinstance(scope, str) or not Path(scope).is_absolute():
        raise RuntimeError('Invalid Hermes gateway routing scope')
    profile_home, routing_home = Path(home).resolve(), Path(control_home).resolve()
    if profile_home == routing_home:
        raise RuntimeError('Invalid secondary Hermes profile routing home')
    token = _MULTIPLEX_ROUTING.set((profile_home, routing_home, scope))
    try:
        yield
    finally:
        _MULTIPLEX_ROUTING.reset(token)


def _routing_storage(home, profile, config=None):
    from gateway.config import load_gateway_config

    if profile != 'default':
        routing = _MULTIPLEX_ROUTING.get()
        if routing is not None and routing[0] == home:
            return profile, routing[1] / 'state.db', routing[2]

        from hermes_cli.gateway_runtime import discover_gateway_endpoint, gateway_profile_home

        endpoint = discover_gateway_endpoint(home).endpoint
        if endpoint is not None and endpoint.control_home:
            routing_home = Path(endpoint.control_home).resolve()
            if routing_home != gateway_profile_home(home):
                # Hooks and outbound tools run in the owning gateway process.
                from gateway.run_api import _ACTIVE_RUNNER

                runner = _ACTIVE_RUNNER
                store = runner.session_store if runner is not None else None
                if (store is None or store._routing_home != routing_home
                        or (runner.session_runtime_descriptor or {}).get('instance_id') != endpoint.instance_id):
                    raise RuntimeError('The secondary Hermes profile has no verified routing scope')
                return profile, routing_home / 'state.db', store._routing_scope()
        elif endpoint is None:
            config = config if config is not None else load_gateway_config()
            if config.multiplex_profiles:
                raise RuntimeError('The Hermes gateway serving this profile is unavailable')
    config = config if config is not None else load_gateway_config()
    return None, home / 'state.db', str(config.sessions_dir.resolve())


def owner_session_key(home: Path, owner_number: str, *, config=None) -> str:
    from gateway.config import Platform
    from gateway.session import SessionSource, build_session_key
    from hermes_constants import profile_name_for_home

    home = home.resolve()
    namespace, _, _ = _routing_storage(home, profile_name_for_home(home) or 'default', config)
    return build_session_key(SessionSource(Platform.WHATSAPP, owner_number), profile=namespace)


def resolve_owner_session(home: Path, owner_number: str) -> str:
    """Return the active WhatsApp DM session for the verified owner's phone."""
    from gateway.config import Platform
    from gateway.session import SessionEntry, SessionSource, build_session_key
    from gateway.whatsapp_identity import canonical_whatsapp_identifier
    from hermes_constants import get_hermes_home, profile_name_for_home
    from hermes_state import SessionDB

    unavailable = 'No verified active Hermes WhatsApp direct-message session'
    if (not isinstance(owner_number, str) or not re.fullmatch(r'[0-9]{7,15}', owner_number)
            or home.resolve() != get_hermes_home().resolve()):
        raise RuntimeError(unavailable)
    home = home.resolve()
    profile = profile_name_for_home(home) or 'default'
    owner = canonical_whatsapp_identifier(owner_number)
    try:
        namespace, routing_path, scope = _routing_storage(home, profile)
        key = build_session_key(SessionSource(Platform.WHATSAPP, owner_number), profile=namespace)
        profile_path = home / 'state.db'
        with SessionDB(profile_path, read_only=True) as db:
            routes = nullcontext(db) if routing_path == profile_path else SessionDB(routing_path, read_only=True)
            with routes as routing_db:
                entries = routing_db.load_gateway_routing_entries(scope=scope)
            entry = SessionEntry.from_dict(json.loads(entries[key]))
            origin = entry.origin
            if (entry.session_key != key or not isinstance(entry.session_id, str) or not entry.session_id
                    or entry.suspended or entry.expiry_finalized
                    or entry.transport_profile not in (None, profile)
                    or origin is None or origin.platform != Platform.WHATSAPP or origin.chat_type != 'dm'
                    or origin.thread_id or origin.profile not in (None, profile)
                    or canonical_whatsapp_identifier(origin.chat_id) != owner
                    or (origin.user_id and canonical_whatsapp_identifier(origin.user_id) != owner)):
                raise RuntimeError(unavailable)
            row = db.get_session(entry.session_id)
            if row and row['end_reason'] == 'compression':
                tip = db.get_compression_tip(entry.session_id)
                row = db.get_session(tip)
            if (not row or row['ended_at'] is not None or row['archived']
                    or row['source'] != Platform.WHATSAPP.value or row['chat_type'] != 'dm'
                    or row['session_key'] != key or row['thread_id']
                    or row['profile_name'] not in (None, profile)
                    or row['transport_profile'] not in (None, profile)
                    or canonical_whatsapp_identifier(row['chat_id']) != owner
                    or (row['user_id'] and canonical_whatsapp_identifier(row['user_id']) != owner)):
                raise RuntimeError(unavailable)
            return row['id']
    except (OSError, sqlite3.Error, RuntimeError, ValueError, KeyError, TypeError, AttributeError):
        # Keep private route keys and identifiers out of the worker's error output.
        raise RuntimeError(unavailable) from None


def save_voice_exchange(home, route_id, exchange_id, user, assistant, **options):
    """Save a completed exchange through the serving profile's transcript transaction."""
    try:
        from hermes_constants import profile_name_for_home
        from hermes_state import SessionDB

        home = home.resolve()
        _, routing_path, scope = _routing_storage(home, profile_name_for_home(home) or 'default')
        profile_path = home / 'state.db'
        with closing(SessionDB(profile_path)) as db:
            if routing_path == profile_path:
                return append_voice_exchange(db, scope, route_id, exchange_id, user, assistant, **options)
            if not routing_path.is_file():
                return 'unavailable'
            with closing(SessionDB(routing_path)) as routing_db:
                return append_voice_exchange(db, scope, route_id, exchange_id, user, assistant,
                                             routing_db=routing_db, **options)
    except (ImportError, AttributeError):
        # A changed Hermes API must leave this exchange pending for a retry.
        return 'unavailable'


def append_voice_exchange(db, scope, route_id, exchange_id, user, assistant, *,
                          user_at_ms=None, assistant_at_ms=None, wait_for_lease=False, cancelled=None,
                          routing_db=None):
    from agent.message_sanitization import tool_call_id_variants, tool_result_id_variants
    from hermes_state import _compression_lock_holder_process_is_dead

    # A question that reads an existing result adds only the caller's words.
    exchange = [('user', user)] if user else []
    if assistant:
        exchange.append(('assistant', assistant))
    messages = [
        {'role': role, 'content': content, 'message_uid': f'wa-control-live:{exchange_id}:{role}',
         'display_kind': VOICE_CALL_KIND,
         'display_metadata': {'source': 'whatsapp_voice', 'exchange_id': exchange_id}}
        for role, content in exchange
    ]
    spoken_at_ms = {'user': user_at_ms, 'assistant': assistant_at_ms}
    for message in messages:
        if (at_ms := spoken_at_ms[message['role']]) is not None:
            message['timestamp'] = at_ms / 1000

    def current_target(conn, route_conn):
        row = conn.execute('SELECT session_key FROM sessions WHERE id=?', (route_id,)).fetchone()
        if not row:
            return None
        route = route_conn.execute('SELECT entry_json FROM gateway_routing WHERE scope=? AND session_key=?',
                                   (scope, row['session_key'])).fetchone()
        entry = json.loads(route['entry_json']) if route else {}
        routed = entry.get('session_id')
        if not isinstance(routed, str) or not routed or entry.get('suspended') or entry.get('expiry_finalized'):
            return None
        # A compaction moves the physical transcript while keeping this conversation's lease.
        target = db._compression_tip_on(conn, route_id)
        if db._compression_tip_on(conn, routed) != target:
            return None
        tip = conn.execute('SELECT ended_at, archived FROM sessions WHERE id=?', (target,)).fetchone()
        return target if tip and tip['ended_at'] is None and not tip['archived'] else None

    def open_tool_batch(conn, target, lineage):
        lease = conn.execute('SELECT holder FROM session_turn_leases WHERE conversation_id=?',
                             (lineage,)).fetchone()
        # Expiry makes a lease reclaimable, but Hermes may still renew it before its next write.
        if lease is None or _compression_lock_holder_process_is_dead(lease['holder']):
            return False
        tail = conn.execute('SELECT id, role, tool_calls FROM messages WHERE session_id=? AND active=1 '
                            "AND role <> 'tool' ORDER BY id DESC LIMIT 1",
                            (target,)).fetchone()
        if not tail or tail['role'] != 'assistant' or not tail['tool_calls']:
            return False
        calls = json.loads(tail['tool_calls'])
        if not isinstance(calls, list):
            return True
        pending = [tool_call_id_variants(call) for call in calls]
        if any(not ids for ids in pending):
            return True
        results = conn.execute('SELECT tool_call_id FROM messages WHERE session_id=? AND active=1 AND id>? '
                               "AND role='tool' ORDER BY id", (target, tail['id']))
        for row in results:
            result_ids = tool_result_id_variants(row[0])
            for index, call_ids in enumerate(pending):
                if call_ids & result_ids:
                    pending.pop(index)
                    break
        return bool(pending)

    def append(conn, route_conn=None):
        if cancelled is not None and cancelled.is_set():
            return 'busy'
        target = current_target(conn, route_conn if route_conn is not None else conn)
        if not target:
            return 'session_changed'
        lineage = db._session_turn_lease_key_on_conn(conn, route_id)
        existing = conn.execute(
            'SELECT session_id, role, content FROM messages WHERE message_uid IN (?, ?) '
            'AND session_id IN (SELECT id FROM sessions WHERE session_key = '
            '(SELECT session_key FROM sessions WHERE id = ?))',
            (f'wa-control-live:{exchange_id}:user', f'wa-control-live:{exchange_id}:assistant', route_id),
        ).fetchall()
        saved = [(row['role'], db._decode_content(row['content'])) for row in existing
                 if db._session_turn_lease_key_on_conn(conn, row['session_id']) == lineage]
        if saved:
            return 'saved' if set(saved) == set(exchange) else 'conflict'
        # A standalone voice exchange uses the native append path. Don't split a
        # running assistant tool call from the tool results it has yet to persist.
        if open_tool_batch(conn, target, lineage):
            return 'busy'
        db._append_messages_in_transaction(conn, target, messages)
        return 'saved'

    def write():
        if routing_db is None:
            return db._execute_transcript_write(append, messages)

        def under_route_lock(route_conn):
            # Reserve the gateway index against /new and /resume until the
            # profile transcript commits. Only the profile DB is modified.
            return db._execute_transcript_write(lambda conn: append(conn, route_conn), messages, patience_s=0)

        from hermes_state_errors import is_sqlite_lock_error
        try:
            return routing_db._execute_write(under_route_lock, patience_s=0)
        except sqlite3.OperationalError as exc:
            if is_sqlite_lock_error(exc):
                return 'busy'
            raise

    status = write()
    if status != 'busy' or not wait_for_lease:
        return status
    stopped = cancelled if cancelled is not None else Event()
    while not stopped.wait(1):
        if routing_db is None:
            with db._read_ctx() as conn:
                target = current_target(conn, conn)
                if not target:
                    return 'session_changed'
                lineage = db._session_turn_lease_key_on_conn(conn, route_id)
                if open_tool_batch(conn, target, lineage):
                    continue
        status = write()
        if status != 'busy':
            return status
    return 'busy'
