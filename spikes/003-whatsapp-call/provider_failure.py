"""Build a provider-independent WhatsApp call failure notice."""

import base64
from datetime import datetime
from pathlib import Path


ASSETS = Path(__file__).with_name('assets')
HEADINGS = {
    'all_rate_limited': 'Semua pilihan audio Live mencapai batas pemakaian.',
    'all_unavailable': 'Pilihan audio Live sedang tidak tersedia.',
    'codex_login_required': 'Codex Live perlu login ulang lewat Codex CLI.',
    'audio_setup_timeout': 'Audio Live belum siap saat waktu sambungan habis.',
    'audio_connection_lost': 'Koneksi audio Live terputus.',
    'provider_content_closed': 'Provider menutup sesi audio Live karena kebijakan konten.',
}
ACCOUNT_ACTIONS = {
    'rate_limited': 'sedang dibatasi pemakaiannya',
    'quota_exhausted': 'kuotanya habis',
    'billing_required': 'perlu memeriksa saldo atau pengaturan billing',
    'access_denied': 'perlu memeriksa izin proyek dan model',
    'unsupported': 'perlu memeriksa model atau konfigurasi Live',
    'unavailable': 'gagal tersambung',
    'unknown': 'belum diketahui penyebab putusnya',
}
LOGIN_ACTIONS = {
    'codex': 'perlu login ulang lewat Codex CLI',
    'hermes-realtime': 'perlu login ulang di profil Hermes lewat hermes auth add openai-codex',
}


def encode_notice_pcm(name, *, max_seconds):
    max_bytes = 16_000 * 2 * max_seconds
    try:
        with (ASSETS / name).open('rb') as clip:
            pcm = clip.read(max_bytes + 1)
    except OSError:
        return None
    if 0 < len(pcm) <= max_bytes and len(pcm) % 2 == 0:
        return base64.b64encode(pcm).decode('ascii')
    return None


def build_provider_failure(reason, routing_status):
    if reason not in HEADINGS:
        reason = 'audio_connection_lost'
    lines = [HEADINGS[reason]]
    accounts = routing_status.get('accounts', ())
    for account in accounts[:8]:
        problem = account.get('reason')
        if problem == 'auth_required':
            action = LOGIN_ACTIONS.get(account.get('kind'), 'API key perlu diperiksa di konfigurasi profil')
        else:
            action = ACCOUNT_ACTIONS.get(problem)
        if action:
            lines.append(f"{account['provider']}/{account['account']} {action}.")
    retry_at = routing_status.get('retry_at')
    if retry_at is not None:
        try:
            when = datetime.fromtimestamp(retry_at).astimezone().strftime('%d %b %H:%M %Z')
        except (OSError, OverflowError, ValueError):
            pass
        else:
            prefix = 'Perkiraan dapat dicoba lagi' if routing_status.get('retry_estimated') else 'Coba lagi paling awal'
            lines.append(f'{prefix} {when}.')
    if accounts:
        lines.append('Buka /walive status untuk rincian provider.')
    notice = {'reason': reason, 'message': '\n'.join(lines)}
    name = 'live-rate-limited.pcm' if reason == 'all_rate_limited' else 'live-unavailable.pcm'
    if pcm := encode_notice_pcm(name, max_seconds=8):
        notice['audioPcm'] = pcm
    return notice
