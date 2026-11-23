"""Caller requests and direct spoken replies on a Codex connection without a tool call."""
import asyncio
import struct

import pytest

import call_adapter as adapter
from test_general_delegation import (
    ACTIVE_WHATSAPP_SESSION, DM_ADMISSION_ID, acknowledge, delegation, feed_output, local_voice, wait_until,
)


PHOTO_REQUEST = 'Tolong buatkan foto kucing memakai jas hujan kuning lalu kirim ke WhatsApp ini.'
PHOTO_REPLY = 'Aku akan buat fotonya dan kirim ke chat ini.'
CALLER_AUDIO = struct.pack('<h', 250) * 320


class NativeBoundary:
    def __init__(self, *, decision=None, receipt=None):
        self.routed = []
        self.proposals = []
        self.submitted = []
        self.saved = []
        self.files = []
        self.decision = decision
        self.receipt = receipt
        self.route_gate = asyncio.Event()
        self.submit_gate = asyncio.Event()
        self.route_gate.set()
        self.submit_gate.set()

    async def route(self, proposal, current, previous, loaded_context, *, session_id):
        self.routed.append((session_id, current))
        self.proposals.append(proposal)
        await self.route_gate.wait()
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision or {'status': 'task' if current == PHOTO_REQUEST else 'reply'}

    async def submit(self, submission_id, text, *, session_id, spoken_at_ms=None, attachment_admission_id=None):
        self.submitted.append((session_id, text, submission_id))
        self.files.append(attachment_admission_id)
        await self.submit_gate.wait()
        # A gateway can lose the receipt. No outcome or delivery may be assumed.
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt or {'status': 'unknown'}

    async def observe_work(self, session_id, admission_id):
        return {'status': 'queued', 'session_id': session_id, 'admission_id': admission_id}

    async def save_exchange(self, session_id, exchange, *, wait_for_lease=False):
        self.saved.append((session_id, exchange['user'], exchange['assistant']))
        return {'status': 'saved'}


async def turn(voice, kind, role, turn_id, transcript=''):
    await voice.on_data({'type': kind, 'turn': {'role': role, 'id': turn_id, 'transcript': transcript}})


async def direct_reply(voice, remote, *, audio=True, caller_text=PHOTO_REQUEST):
    voice.feed(CALLER_AUDIO)
    await turn(voice, 'turn.done', 'user', 'caller-1', caller_text)
    await turn(voice, 'turn.created', 'assistant', 'reply-1')
    if audio:
        await feed_output(voice, remote, 1000)
    await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)


@pytest.mark.parametrize('caller_final_first', [True, False], ids=['caller-final-first', 'reply-final-first'])
def test_photo_request_without_provider_delegation_reaches_native_dm_once(
    tmp_path, monkeypatch, caller_final_first,
):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.created', 'user', 'caller-1')
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'caller-1', 'delta': 'Tolong buatkan foto'})
            if caller_final_first:
                await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            if not caller_final_first:
                assert not jobs.submitted
                await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)

            await wait_until(lambda: jobs.submitted or jobs.saved)
            assert jobs.routed == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST)]
            assert [(sid, text) for sid, text, _ in jobs.submitted] == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST)]
            assert not jobs.saved
            assert not [event for event in appended if event['type'] == 'delegation.context.append']

            # A retried final or late, multipart tool proposal belongs to the same caller turn.
            await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            late = delegation(['Buatkan foto', ' dan kirim ke chat.'], item_id='provider-late')
            await voice.on_data(late)
            await voice.on_data(late)
            await wait_until(lambda: any(event.get('delegation_item_id') == 'provider-late' for event in appended))
            await acknowledge(voice, 'provider-late')
        finally:
            await voice.close()
        assert len(jobs.routed) == len(jobs.submitted) == 1
        assert not jobs.saved

    asyncio.run(exercise())


@pytest.mark.parametrize(('caller_text', 'response'), [
    ('Apa kabar hari ini?', 'Baik, terima kasih.'),
    ('Kalau nanti aku minta foto, kamu bisa bikinkan?', 'Aku akan buat fotonya.'),
    ('Jangan buat foto. Aku cuma ingin membahas idenya.', 'Aku akan buat fotonya.'),
])
def test_direct_reply_to_conversation_does_not_admit_work(tmp_path, monkeypatch, caller_text, response):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-1', caller_text)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', response)
            await wait_until(lambda: jobs.saved or jobs.submitted)
        finally:
            await voice.close()
        assert jobs.saved == [(ACTIVE_WHATSAPP_SESSION, caller_text, response)]
        assert not jobs.submitted

    asyncio.run(exercise())


@pytest.mark.parametrize('timing', ['no-pcm', 'late-pcm', 'silence'])
def test_only_audible_output_completes_direct_reply(tmp_path, monkeypatch, timing):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            await direct_reply(voice, remote, audio=False)
            await asyncio.sleep(0)
            assert not jobs.routed
            if timing != 'no-pcm':
                await feed_output(voice, remote, 1000 if timing == 'late-pcm' else 1)
            if timing != 'late-pcm':
                assert not jobs.routed
                voice.feed(CALLER_AUDIO)
                await turn(voice, 'turn.created', 'user', 'next-caller')
            await wait_until(lambda: jobs.submitted or jobs.saved)
            assert bool(jobs.submitted) == (timing == 'late-pcm')
            assert bool(jobs.saved) == (timing != 'late-pcm')
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['reply', 'end_call', 'steer', 'recall', 'follow', 'unavailable'])
def test_direct_reply_only_recovers_new_work(tmp_path, monkeypatch, action):
    async def exercise():
        jobs = NativeBoundary(decision={'status': action})
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.saved)
            assert jobs.proposals == ['']
            assert not jobs.submitted and not appended and not voice.pending_end_call
            assert not voice.failed.is_set()
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('boundary', ['route', 'submit'])
def test_close_preserves_only_unsubmitted_direct_exchange(tmp_path, monkeypatch, boundary):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        gate = jobs.route_gate if boundary == 'route' else jobs.submit_gate
        gate.clear()
        await direct_reply(voice, remote)
        await wait_until(lambda: jobs.routed if boundary == 'route' else jobs.submitted)
        await voice.close()
        assert not appended
        assert bool(jobs.saved) == (boundary == 'route')
        assert len(jobs.submitted) == (boundary == 'submit')
    asyncio.run(exercise())


@pytest.mark.parametrize('change', ['new-caller-turn', 'session-change', 'reconnecting', 'audio-replaced',
                                    'audio-failed', 'barge-in'])
def test_superseded_direct_decision_expires_before_submission(tmp_path, monkeypatch, change):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            jobs.route_gate.clear()
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.routed)
            if change == 'new-caller-turn':
                voice.feed(CALLER_AUDIO)
                await turn(voice, 'turn.done', 'user', 'caller-2', 'Batalkan, aku berubah pikiran.')
            elif change == 'session-change':
                voice.session_changed()
            elif change == 'reconnecting':
                voice.chat_reconnecting = True
            elif change == 'barge-in':
                await voice.on_data({'type': 'turn.interrupted', 'turn_id': 'reply-1'})
            elif change == 'audio-failed':
                voice.audio.failed.set()
            else:
                old = voice.audio
                voice.install_audio(adapter.AudioConnection(voice, adapter.CodexConnection))
                voice.retire_audio(old)
            jobs.route_gate.set()
            await wait_until(lambda: jobs.submitted or jobs.saved)
            assert not jobs.submitted
            assert jobs.saved == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST, PHOTO_REPLY)]
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('timing', ['before-classification', 'during-classification'])
def test_real_handoff_shares_one_decision_and_one_admission(tmp_path, monkeypatch, timing):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            jobs.route_gate.clear()
            await direct_reply(voice, remote)
            if timing == 'during-classification':
                await wait_until(lambda: jobs.routed)
            await voice.on_data(delegation(['Proposal native'], item_id='genuine'))
            await wait_until(lambda: jobs.routed)
            jobs.route_gate.set()
            await wait_until(lambda: jobs.submitted and appended)
            await acknowledge(voice, 'genuine')
            await wait_until(lambda: 'caller-1' not in voice.routing_turns or len(appended) > 1)
            assert jobs.proposals == (['Proposal native'] if timing == 'before-classification' else [''])
            assert len(jobs.submitted) == 1
            assert [(event['delegation_item_id'], event['channel']) for event in appended] == [('genuine', 'commentary')]
            assert not jobs.saved
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('attachment', ['verified', 'missing', 'wrong-session', 'not-completed'])
def test_recovery_attaches_only_verified_completed_native_media(tmp_path, monkeypatch, attachment):
    async def exercise():
        work = {'admission_id': DM_ADMISSION_ID, 'session_id': ACTIVE_WHATSAPP_SESSION,
                'attachment': True, 'status': 'completed'}
        if attachment == 'wrong-session':
            work['session_id'] = 'another-session'
        if attachment == 'not-completed':
            work['status'] = 'started'
        jobs = NativeBoundary(decision={'status': 'task', 'admission_id': DM_ADMISSION_ID,
                                        'work': [] if attachment == 'missing' else [work]})
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.saved or jobs.submitted)
            assert jobs.files == ([DM_ADMISSION_ID] if attachment == 'verified' else [])
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_definite_native_rejection_preserves_exchange_and_updates_live_context(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary(receipt={'status': 'attachment_unavailable'})
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.saved)
            assert jobs.saved == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST, PHOTO_REPLY)]
            assert any('file' in text and 'belum' in text for _, text, _ in voice.pending_chat_context)
            assert not voice.call_work and not appended
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('receipt,admitted', [
    (ConnectionResetError(), False),
    ({'status': 'completed', 'receipt': {'session_id': 'wrong-session', 'admission_id': DM_ADMISSION_ID}}, False),
    ({'invalid': 'reply'}, False),
    ({'status': 'queued', 'receipt': {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID}}, True),
])
def test_new_caller_turn_during_submit_cannot_repeat_or_invent_admission(tmp_path, monkeypatch, receipt, admitted):
    async def exercise():
        jobs = NativeBoundary(receipt=receipt)
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            jobs.submit_gate.clear()
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.submitted)
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-2', 'Apa kabar?')
            await voice.on_data(delegation(['Proposal terlambat'], item_id='late'))
            jobs.submit_gate.set()
            await wait_until(lambda: bool(appended) and 'caller-1' not in voice.routing_turns)
            await acknowledge(voice, 'late')
            assert voice.call_work == ({DM_ADMISSION_ID} if admitted else set())
            assert set(voice.work_followers) == voice.call_work
            assert voice.report['backend_completed_delegations'] == []
            assert len(jobs.submitted) == 1 and not jobs.saved
            assert not voice.failed.is_set() and not voice.pending_chat_context
            assert [(e['delegation_item_id'], e['channel']) for e in appended] == [('late', 'commentary')]
        finally:
            await voice.close()
        assert jobs.saved == [(ACTIVE_WHATSAPP_SESSION, 'Apa kabar?', '')]
    asyncio.run(exercise())


@pytest.mark.parametrize('decision', [OSError('offline'), ['task'], {'status': 'task', 'work': 'wrong'}])
def test_bad_classification_preserves_call_and_transcript(tmp_path, monkeypatch, decision):
    async def exercise():
        jobs = NativeBoundary(decision=decision)
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.saved or jobs.submitted or voice.failed.is_set())
            assert jobs.saved and not jobs.submitted and not voice.failed.is_set()
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_unmatched_old_caller_pcm_does_not_authorize_future_transcript(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            loop = asyncio.get_running_loop()
            now = loop.time
            voice.feed(CALLER_AUDIO)
            monkeypatch.setattr(loop, 'time', lambda: now() + 600)
            await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            await wait_until(lambda: jobs.submitted or jobs.saved)
            assert jobs.saved and not jobs.routed
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_late_rejection_cannot_describe_another_callers_turn(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary(receipt={'status': 'attachment_unavailable'})
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            jobs.submit_gate.clear()
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.submitted)
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-2', 'Halo?')
            jobs.submit_gate.set()
            await wait_until(lambda: jobs.saved)
            assert not voice.pending_chat_context
            assert not voice.failed.is_set()
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize(('action', 'timing'), [
    ('steer', 'before-proposal'), ('steer', 'during-ack'), ('dm_only', 'before-proposal'),
    ('speak_result', 'before-proposal'),
])
def test_late_handoff_cannot_apply_stale_decision_to_existing_work(tmp_path, monkeypatch, action, timing):
    async def exercise():
        work = {'session_id': ACTIVE_WHATSAPP_SESSION, 'admission_id': DM_ADMISSION_ID, 'status': 'started'}
        jobs = NativeBoundary(decision={'status': action, 'admission_id': DM_ADMISSION_ID, 'work': [work]})
        steered = []
        async def steer(session_id, admission_id, text):
            steered.append((session_id, admission_id, text))
            return {'status': 'queued'}
        jobs.steer_work = steer
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        if action == 'speak_result':
            voice.dm_only_results.add(DM_ADMISSION_ID)
        try:
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.saved)
            late = delegation(['Proposal terlambat'], item_id='late')
            if timing == 'during-ack':
                await voice.on_data(late)
                await wait_until(lambda: appended)
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-2', 'Batalkan perubahan itu.')
            if timing == 'before-proposal':
                await voice.on_data(late)
                await wait_until(lambda: appended)
            await acknowledge(voice, 'late')
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not steered and not jobs.submitted and not voice.result_preference_turns
            assert voice.dm_only_results == ({DM_ADMISSION_ID} if action == 'speak_result' else set())
            assert [event['channel'] for event in appended] == ['commentary']
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('old_playout', [True, False])
def test_pcm_before_reply_id_requires_unambiguous_playout(tmp_path, monkeypatch, old_playout):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            if old_playout:
                await turn(voice, 'turn.done', 'user', 'prior', 'Halo.')
                await turn(voice, 'turn.created', 'assistant', 'prior-answer')
                await feed_output(voice, remote, 1200)
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            if old_playout:
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not jobs.routed
                # After the actual reply ID, the PCM can belong to this request.
                await feed_output(voice, remote, 1500)
            await wait_until(lambda: jobs.submitted)
            assert jobs.routed == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST)]
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_only_current_direct_classifier_survives_rapid_conversation(tmp_path, monkeypatch):
    async def exercise():
        live, cancelled = set(), []
        class SlowRouter(NativeBoundary):
            async def route(self, proposal, current, previous, loaded_context, *, session_id):
                live.add(current)
                try:
                    await self.route_gate.wait()
                    return {'status': 'reply'}
                except asyncio.CancelledError:
                    cancelled.append(current)
                    raise
                finally:
                    live.discard(current)

        jobs = SlowRouter()
        jobs.route_gate.clear()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            for i in range(12):
                text = f'Ucapan {i}'
                voice.feed(CALLER_AUDIO)
                await turn(voice, 'turn.done', 'user', f'caller-{i}', text)
                await turn(voice, 'turn.created', 'assistant', f'reply-{i}')
                await feed_output(voice, remote, 1000)
                await turn(voice, 'turn.done', 'assistant', f'reply-{i}', 'Baik.')
                await wait_until(lambda: text in live)
                assert live == {text}
            jobs.route_gate.set()
            await wait_until(lambda: len(jobs.saved) == 12)
            assert len(cancelled) == 11
            assert [row[1] for row in jobs.saved] == [f'Ucapan {i}' for i in range(12)]
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_retired_provider_takeover_never_acks_old_item_on_replacement(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary()
        jobs.route_gate.clear()
        voice, remote, _, old_appends = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        replacement_appends = []
        try:
            await direct_reply(voice, remote)
            await wait_until(lambda: jobs.routed)
            await voice.on_data(delegation(['Permintaan lama'], item_id='old-peer-item'))
            # Start the native takeover, which shares the same outstanding classifier.
            await asyncio.sleep(0)
            old = voice.audio
            replacement = adapter.AudioConnection(voice, adapter.CodexConnection)
            replacement.link.channel = type('Channel', (), {'send': lambda _, value: replacement_appends.append(value)})()
            voice.install_audio(replacement)
            voice.retire_audio(old)
            jobs.route_gate.set()
            await wait_until(lambda: replacement_appends or 'caller-1' not in voice.routing_turns)
            assert not replacement_appends and not old_appends
            assert not voice.pending_deliveries and not jobs.submitted
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('missing', ['caller-audio', 'caller-final'])
def test_provider_promise_alone_cannot_admit_work(tmp_path, monkeypatch, missing):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            if missing == 'caller-final':
                voice.feed(CALLER_AUDIO)
            else:
                voice.feed(bytes(len(CALLER_AUDIO)))
            await turn(voice, 'turn.created', 'user', 'caller-1', PHOTO_REQUEST)
            if missing != 'caller-final':
                await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await turn(voice, 'turn.created', 'assistant', 'reply-1')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'reply-1', PHOTO_REPLY)
            await asyncio.sleep(0)
        finally:
            await voice.close()
        assert not jobs.submitted

    asyncio.run(exercise())
