"""Hermes Desktop's whole-utterance voice stop matcher with WhatsApp phrases."""

import unicodedata


def _normalize(text):
    composed = unicodedata.normalize('NFKC', text).lower()
    return ' '.join(''.join(' ' if unicodedata.category(ch).startswith('P') else ch
                            for ch in composed).split())


# Defaults and address handling ported from Hermes Desktop voice-stop-word.ts.
# The remaining scoped commands express the same action on a WhatsApp call.
DEFAULT_STOP_PHRASES = frozenset(map(_normalize, (
    'stop', 'stop listening', 'stop it', 'stop please', 'please stop', 'stop stop',
    'that is all', "that's all", 'never mind', 'nevermind', 'end conversation',
    'end the conversation', 'goodbye', 'good bye', 'bye', 'cancel',
    'hang up', 'please hang up', 'hang up the phone', 'end the call', 'end this call',
    'tutup telepon', 'tutup teleponnya', 'matikan telepon', 'matikan teleponnya',
    'akhiri panggilan', 'akhiri panggilan ini', 'tutup panggilan', 'tutup panggilannya',
)))
# These commands can refer to the work itself while a request is in flight.
_WORK_STOP_PHRASES = frozenset(map(_normalize, (
    'stop', 'stop it', 'stop please', 'please stop', 'stop stop', 'never mind', 'nevermind', 'cancel',
    'that is all', "that's all",
)))
_ADDRESSES = ('hey hermes', 'hermes', 'ok', 'okay', 'hey')


def stop_phrases(configured):
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        return DEFAULT_STOP_PHRASES
    phrases = [str(entry).strip() for entry in configured if type(entry) in (int, float, str)
               and str(entry).strip()]
    if [phrase.lower() for phrase in phrases] == ['stop']:
        return DEFAULT_STOP_PHRASES
    return frozenset(filter(None, map(_normalize, phrases)))


def is_stop_command(transcript, phrases, *, work_pending=False):
    text = _normalize(transcript)
    eligible = phrases - _WORK_STOP_PHRASES if phrases is DEFAULT_STOP_PHRASES and work_pending else phrases
    if text in eligible:
        return True
    # Strip one address, keeping arbitrary custom phrases exact as on Desktop.
    prefixes = _ADDRESSES
    if phrases is DEFAULT_STOP_PHRASES:
        prefixes += ('ya udah', 'yaudah', 'oke', 'tolong')
    for prefix in prefixes:
        if text.startswith(prefix + ' '):
            return text[len(prefix) + 1:] in eligible
    return False
