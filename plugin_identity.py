"""Resolve the owner and bot for the voice plugin from Hermes WhatsApp."""

import json
import re
from pathlib import Path


_INDIVIDUAL_JID = re.compile(r'\+?([0-9]{1,20})(?:(?::[0-9]+)?@(s\.whatsapp\.net|lid))?')


class StockBotNotPaired(RuntimeError):
    """The selected Hermes profile has no stock WhatsApp bot identity."""


def _whatsapp_platform(home: Path):
    from gateway.config import Platform, load_gateway_config
    from hermes_constants import get_hermes_home

    if home != get_hermes_home().resolve():
        raise RuntimeError('WhatsApp identity requires the active Hermes profile')
    return load_gateway_config().platforms.get(Platform.WHATSAPP)


def _session_path(home: Path, extra: dict) -> Path:
    from hermes_constants import get_hermes_dir

    if 'session_path' in extra:
        path = extra['session_path']
        if not isinstance(path, str) or not path.strip():
            raise RuntimeError('Hermes WhatsApp session path is invalid')
        return Path(path).expanduser().absolute()
    return get_hermes_dir('platforms/whatsapp/session', 'whatsapp/session', home=home)


def _allowed_users(extra: dict) -> set[str]:
    from gateway.platforms._shared import decode_json_list_literal, get_scoped_secret

    if 'allow_from' in extra:
        raw = extra['allow_from']
    elif 'allowFrom' in extra:
        raw = extra['allowFrom']
    else:
        raw = get_scoped_secret('WHATSAPP_ALLOWED_USERS')
    values = decode_json_list_literal(raw) if raw is not None else []
    parts = values if isinstance(values, list) else str(values).split(',')
    return {str(part).strip() for part in parts if str(part).strip()}


def _whatsapp_settings(home: Path) -> tuple[Path, set[str], str | None]:
    from gateway.platforms._shared import extra_or_secret

    platform = _whatsapp_platform(home)
    if platform is None or not platform.enabled:
        raise RuntimeError('Hermes WhatsApp must be enabled for voice calls')
    extra = platform.extra
    dm_policy = extra_or_secret(extra, 'dm_policy', 'WHATSAPP_DM_POLICY', 'pairing')
    if str(dm_policy).strip().lower() == 'disabled':
        raise RuntimeError('Hermes WhatsApp direct messages are disabled')
    home_id = platform.home_channel.chat_id if platform.home_channel else None
    return _session_path(home, extra), _allowed_users(extra), home_id


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


def _owner_numbers(session, allowed, home_id):
    home_match = _INDIVIDUAL_JID.fullmatch(home_id.strip()) if home_id else None
    home_lid = home_match[1] if home_match and home_match[2] == 'lid' else None
    return {_phone(entry, session, home_lid=home_lid) for entry in allowed}


def _paired_bot(session: Path) -> str:
    try:
        creds = json.loads((session / 'creds.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise StockBotNotPaired('No paired Hermes WhatsApp bot in the selected profile') from None
    except (OSError, ValueError):
        raise RuntimeError('Hermes WhatsApp has no readable paired chat identity') from None
    me = creds.get('me') if isinstance(creds, dict) else None
    bot_id = me.get('id') if isinstance(me, dict) else None
    if not bot_id:
        raise StockBotNotPaired('No paired Hermes WhatsApp bot in the selected profile')
    return _phone(bot_id, session)


def resolve_identity(hermes_home: Path) -> tuple[str, str]:
    """Return one authorized owner and the paired chat account as phone digits."""
    session, allowed, home_id = _whatsapp_settings(hermes_home.resolve())
    owners = _owner_numbers(session, allowed, home_id)
    if len(owners) != 1:
        raise RuntimeError('Hermes WhatsApp must authorize exactly one voice owner')
    owner = owners.pop()

    bot = _paired_bot(session)
    if bot == owner:
        raise RuntimeError('Hermes WhatsApp voice owner and paired bot must be distinct accounts')
    return owner, bot


def _setup_identity(home: Path) -> tuple[str, str | None]:
    platform = _whatsapp_platform(home)
    extra = platform.extra if platform else {}
    session = _session_path(home, extra)
    bot = _paired_bot(session)
    home_id = platform.home_channel.chat_id if platform and platform.home_channel else None
    try:
        owners = _owner_numbers(session, _allowed_users(extra), home_id)
        owner = next(iter(owners)) if len(owners) == 1 else None
        if owner and owner != bot:
            return bot, owner
    except (RuntimeError, ValueError):
        pass
    try:
        owner = _phone(home_id, session)
    except RuntimeError:
        return bot, None
    return bot, owner if owner != bot else None


def resolve_setup_identity(home: Path, stock_profile: str | None = None) -> tuple[str, str | None]:
    """Find the stock bot and, separately, a DM identity for the operator to confirm."""
    if stock_profile is None:
        return _setup_identity(home.resolve())

    from hermes_cli.profiles import resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    source = Path(resolve_profile_env(stock_profile)).resolve()
    token = set_hermes_home_override(source)
    try:
        return _setup_identity(source)
    finally:
        reset_hermes_home_override(token)
