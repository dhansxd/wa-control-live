"""Resolve the owner and bot for the voice plugin from Hermes WhatsApp."""

import json
import re
from pathlib import Path


_INDIVIDUAL_JID = re.compile(r'\+?([0-9]{1,20})(?:(?::[0-9]+)?@(s\.whatsapp\.net|lid))?')


def _whatsapp_settings(home: Path) -> tuple[Path, set[str], str | None]:
    from gateway.config import Platform, load_gateway_config
    from gateway.platforms._shared import decode_json_list_literal, extra_or_secret, get_scoped_secret
    from hermes_constants import get_hermes_dir, get_hermes_home

    if home != get_hermes_home().resolve():
        raise RuntimeError('WhatsApp identity requires the active Hermes profile')
    platform = load_gateway_config().platforms.get(Platform.WHATSAPP)
    if platform is None or not platform.enabled:
        raise RuntimeError('Hermes WhatsApp must be enabled for voice calls')
    extra = platform.extra
    dm_policy = extra_or_secret(extra, 'dm_policy', 'WHATSAPP_DM_POLICY', 'pairing')
    if str(dm_policy).strip().lower() == 'disabled':
        raise RuntimeError('Hermes WhatsApp direct messages are disabled')
    # The WhatsApp config keys win by presence, even when explicitly empty.
    if 'allow_from' in extra:
        raw = extra['allow_from']
    elif 'allowFrom' in extra:
        raw = extra['allowFrom']
    else:
        raw = get_scoped_secret('WHATSAPP_ALLOWED_USERS')
    values = decode_json_list_literal(raw) if raw is not None else []
    parts = values if isinstance(values, list) else str(values).split(',')
    allowed = {str(part).strip() for part in parts if str(part).strip()}
    if 'session_path' in extra:
        path = extra['session_path']
        if not isinstance(path, str) or not path.strip():
            raise RuntimeError('Hermes WhatsApp session path is invalid')
        session = Path(path).expanduser().absolute()
    else:
        session = get_hermes_dir('platforms/whatsapp/session', 'whatsapp/session', home=home)
    home_id = platform.home_channel.chat_id if platform.home_channel else None
    return session, allowed, home_id


def _read_json(path: Path, message: str):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise RuntimeError(message) from None


def _phone(value: str, session: Path, *, home_lid: str | None = None) -> str:
    text = value.strip() if isinstance(value, str) else ''
    match = _INDIVIDUAL_JID.fullmatch(text)
    if match is None:
        raise RuntimeError('Hermes WhatsApp requires an individual phone or mapped LID')
    number, server = match.groups()
    reverse = session / f'lid-mapping-{number}_reverse.json'
    # A bare allowlist entry can be a LID. A home address is only a type hint for
    # that same entry. It never grants authorization or chooses among owners.
    try:
        is_lid = server == 'lid' or (server is None and not text.startswith('+')
                                     and (number == home_lid or reverse.is_file()))
    except OSError:
        raise RuntimeError('Hermes WhatsApp local LID mapping is inaccessible') from None
    if is_lid:
        mapped = _read_json(reverse, 'Hermes WhatsApp LID has no readable local phone mapping')
        match = _INDIVIDUAL_JID.fullmatch(mapped.strip()) if isinstance(mapped, str) else None
        if match is None or match[2] == 'lid':
            raise RuntimeError('Hermes WhatsApp LID has no valid local phone mapping')
        number = match[1]
    if not 7 <= len(number) <= 15:
        raise RuntimeError('Hermes WhatsApp identity requires a full international phone number')
    return number


def resolve_identity(hermes_home: Path) -> tuple[str, str]:
    """Return one authorized owner and the paired chat account as phone digits."""
    session, allowed, home_id = _whatsapp_settings(hermes_home.resolve())
    home_match = _INDIVIDUAL_JID.fullmatch(home_id.strip()) if home_id else None
    home_lid = home_match[1] if home_match and home_match[2] == 'lid' else None
    owners = {_phone(entry, session, home_lid=home_lid) for entry in allowed}
    if len(owners) != 1:
        raise RuntimeError('Hermes WhatsApp must authorize exactly one voice owner')
    owner = owners.pop()

    creds = _read_json(session / 'creds.json', 'Hermes WhatsApp has no readable paired chat identity')
    me = creds.get('me') if isinstance(creds, dict) else None
    bot_id = me.get('id') if isinstance(me, dict) else None
    if not bot_id:
        raise RuntimeError('Hermes WhatsApp has no paired chat identity')
    bot = _phone(bot_id, session)
    if bot == owner:
        raise RuntimeError('Hermes WhatsApp voice owner and paired bot must be distinct accounts')
    return owner, bot
