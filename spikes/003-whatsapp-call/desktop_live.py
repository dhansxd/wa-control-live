"""Hermes Desktop Live conversation policy and its bounded transcript window.

Adapted from Nous Research's Hermes Agent. See THIRD_PARTY_NOTICES.md.
"""
from collections import deque


# Compatibility with Hermes installations predating tools.voice_live.
DESKTOP_PERSONA = (
    "You are Hermes, a calm and friendly voice assistant. Speak naturally at an unhurried pace. "
    "Be clear and direct, not overly cheerful. If the user is frustrated, acknowledge it briefly "
    "and focus on the next helpful step.\n\n"
    "Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with "
    "the main response.\n\n"
    "Interruption policy: Stop speaking when the user interrupts. Listen to what they say.\n\n"
    "Delegation policy:\n"
    "Backend tools:\n"
    "- Hermes agent: a full AI agent with tools — it can run commands, read and edit files, "
    "browse the web, search, remember things across sessions, schedule tasks, and reason "
    "carefully about anything. It is the one who actually does work and knows facts.\n\n"
    "Delegate to the backend when:\n"
    "- The user asks a question that needs facts, current information, or careful reasoning.\n"
    "- The user asks you to do, check, find, make, fix, run or remember anything.\n"
    "- A correction changes work already requested.\n\n"
    "Do not delegate to the backend when:\n"
    "- The user greets you, makes small talk, or asks you to repeat a result already provided.\n"
    "- You need a brief clarification to understand the request.\n\n"
    "Delegate before giving an answer that depends on backend work. Do not guess the result "
    "while waiting; say briefly that you are checking, then wait for the result."
)


def instructions():
    try:
        from tools.voice_live import live_instructions
    except ModuleNotFoundError as exc:
        if exc.name not in ('tools', 'tools.voice_live'):
            raise
        return DESKTOP_PERSONA
    return live_instructions()


class SpokenConversation:
    """WhatsApp supplies completed utterances in place of browser transcript fragments."""

    def __init__(self):
        self.fragments = deque(maxlen=80)

    def remember(self, turn_id, role, text, at_ms):
        words = ' '.join(text.split())
        if words:
            self.fragments.append((turn_id, role, words[-6000:], at_ms))

    def context_for(self, caller_turn):
        recent = []
        for turn_id, role, words, at_ms in self.fragments:
            recent.append((role, words, at_ms))
            if role == 'user' and turn_id == caller_turn:
                break
        else:
            return ''
        floor = recent[-1][2] - 300_000
        recent = [fragment for fragment in recent if fragment[2] >= floor]
        if len(recent) <= 1:
            return ''  # The original caller words are already the native prompt.
        turns = []
        for role, words, _ in recent:
            if turns and turns[-1][0] == role:
                turns[-1][1].append(words)
            else:
                turns.append((role, [words]))
        lines = []
        remaining = 6000  # Native Hermes prompt.submit voice_context budget.
        for role, parts in reversed(turns):
            label = 'User: ' if role == 'user' else 'Voice assistant: '
            if remaining <= len(label):
                break
            line = label + ' '.join(parts)[-(remaining - len(label)):]
            lines.append(line)
            remaining -= len(line) + 1
        return '\n'.join(reversed(lines))
