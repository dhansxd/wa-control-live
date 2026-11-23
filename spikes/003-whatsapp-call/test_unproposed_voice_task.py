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

    async def route(self, proposal, current, previous, loaded_context, *, session_id, subsequent=()):
        self.routed.append((session_id, current))
        self.proposals.append(proposal)
        await self.route_gate.wait()
        if 'Batalkan, aku berubah pikiran.' in subsequent:
            return {'status': 'reply'}
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision or {'status': 'task' if current == PHOTO_REQUEST else 'reply'}

    async def submit(self, submission_id, text, *, session_id, spoken_at_ms=None, attachment_admission_id=None, voice_context=None):
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


def test_final_owner_request_survives_acknowledgment_before_any_assistant_audio(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary()
        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            jobs.route_gate.clear()
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await wait_until(lambda: jobs.routed)
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-2', 'He-eh. Iya aku tunggu.')
            jobs.route_gate.set()
            await wait_until(lambda: jobs.submitted)
            assert [(sid, text) for sid, text, _ in jobs.submitted] == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST)]
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('withdraw', [False, True], ids=['acknowledge', 'withdraw'])
def test_provider_proposal_before_final_uses_same_owner_and_correction_rules(tmp_path, monkeypatch, withdraw):
    async def exercise():
        jobs = NativeBoundary()
        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            jobs.route_gate.clear()
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.created', 'user', 'caller-1')
            await voice.on_data(delegation(['Potentially incorrect provider suggestion']))
            await wait_until(lambda: appended)
            await acknowledge(voice, 'request-1')
            await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await wait_until(lambda: jobs.routed)
            voice.feed(CALLER_AUDIO)
            followup = 'Batalkan, aku berubah pikiran.' if withdraw else 'He-eh. Iya aku tunggu.'
            await turn(voice, 'turn.done', 'user', 'caller-2', followup)
            jobs.route_gate.set()
            await wait_until(lambda: not voice.routing_turns)
            assert [text for _, text, _ in jobs.submitted] == ([] if withdraw else [PHOTO_REQUEST])
        finally:
            await voice.close()
    asyncio.run(exercise())


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
def test_admission_is_independent_of_assistant_audio(tmp_path, monkeypatch, timing):
    async def exercise():
        jobs = NativeBoundary()
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            await direct_reply(voice, remote, audio=False)
            if timing != 'no-pcm':
                await feed_output(voice, remote, 1000 if timing == 'late-pcm' else 1)
            await wait_until(lambda: jobs.submitted)
            assert len(jobs.submitted) == 1
            assert not jobs.saved
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['reply', 'steer', 'recall', 'follow', 'unavailable'])
def test_unmapped_control_decision_cannot_admit_native_work(tmp_path, monkeypatch, action):
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


def test_model_stop_decision_ends_phone_once_without_a_delegation_or_spoken_reply(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary(decision={'status': 'end_call'})
        voice, _, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-1', 'Aku harus pergi, akhiri sambungan suara ini ya.')
            await wait_until(lambda: {'ownerCallEnd': True} in sent)
            await voice.on_data(delegation(['Akhiri sambungan.'], item_id='late'))
            await asyncio.sleep(0)
            assert sent.count({'ownerCallEnd': True}) == 1
            assert not jobs.submitted
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
                                    'audio-failed'])
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
            # Caller finals begin routing even when the provider waits to propose an action.
            assert jobs.proposals == ['']
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
            assert 'caller-1' in voice.unheard_caller_turns
            assert not jobs.saved and not jobs.routed and not jobs.submitted
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


@pytest.mark.parametrize('action', ['steer', 'dm_only', 'speak_result'])
def test_late_provider_proposal_does_not_reapply_direct_work_change(tmp_path, monkeypatch, action):
    async def exercise():
        work = {'admission_id': DM_ADMISSION_ID, 'session_id': ACTIVE_WHATSAPP_SESSION, 'status': 'started'}

        class Requests(NativeBoundary):
            async def route(self, proposal, current, previous, context, *, session_id, **_):
                self.routed.append(current)
                return ({'status': action, 'admission_id': DM_ADMISSION_ID, 'work': [work]}
                        if current == 'Ubah pekerjaan ini.' else {'status': 'reply'})

        jobs = Requests()
        steered = []
        async def steer(session_id, admission_id, text):
            steered.append(text)
            return {'status': 'queued'}
        jobs.steer_work = steer
        voice, remote, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        if action == 'speak_result':
            voice.dm_only_results.add(DM_ADMISSION_ID)
        try:
            await direct_reply(voice, remote, caller_text='Ubah pekerjaan ini.')
            await wait_until(lambda: 'caller-1' not in voice.routing_turns)
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'caller-2', 'Oke.')
            await voice.on_data(delegation(['Ulangi!'], item_id='late'))
            await wait_until(lambda: appended)
            await acknowledge(voice, 'late')
            assert steered == (['Ubah pekerjaan ini.'] if action == 'steer' else [])
            assert jobs.routed.count('Ubah pekerjaan ini.') == 1
            assert not jobs.submitted
            assert voice.dm_only_results == ({DM_ADMISSION_ID} if action == 'dm_only' else set())
            assert [event['channel'] for event in appended] == ['commentary']
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('old_playout', [True, False])
def test_unrelated_playout_cannot_change_which_request_is_submitted(tmp_path, monkeypatch, old_playout):
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
            await wait_until(lambda: jobs.submitted)
            assert jobs.routed == [(ACTIVE_WHATSAPP_SESSION, PHOTO_REQUEST)]
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_rapid_caller_finals_are_classified_in_order_by_one_worker(tmp_path, monkeypatch):
    async def exercise():
        live, visits = set(), []
        class SlowRouter(NativeBoundary):
            async def route(self, proposal, current, previous, loaded_context, *, session_id, subsequent=()):
                live.add(current)
                try:
                    await self.route_gate.wait()
                    visits.append(current)
                    return {'status': 'reply'}
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
                await wait_until(lambda: bool(live))
                assert live == {'Ucapan 0'}
            jobs.route_gate.set()
            await wait_until(lambda: len(jobs.saved) == 12)
            assert visits == [f'Ucapan {i}' for i in range(12)]
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


def test_connected_codex_proposal_requires_a_verified_caller_turn(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary()
        voice, _, _, appended = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.connection.connected.set()
            voice.feed(bytes(len(CALLER_AUDIO)))
            await turn(voice, 'turn.done', 'user', 'caller-1', PHOTO_REQUEST)
            await voice.on_data(delegation([PHOTO_REPLY]))
            await wait_until(lambda: appended or jobs.submitted or 'caller-1' in voice.unheard_caller_turns)
            if appended:
                await acknowledge(voice, 'request-1')
            await wait_until(lambda: not voice.routing_turns)
            assert not jobs.routed and not jobs.submitted
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_queued_owner_turn_classifies_with_its_own_desktop_window(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary()
        jobs.route_gate.clear()
        seen = {}
        async def route(proposal, current, previous, context, *, session_id):
            seen[current] = context
            await jobs.route_gate.wait()
            return {'status': 'reply'}
        jobs.route = route
        voice, remote, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'one', 'Kita bahas foto.')
            await wait_until(lambda: bool(seen))
            await turn(voice, 'turn.created', 'assistant', 'answer-one')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'answer-one', 'Mau lihat foto kucing?')
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'two', 'Iya.')
            await turn(voice, 'turn.created', 'assistant', 'answer-two')
            await feed_output(voice, remote, 1000)
            await turn(voice, 'turn.done', 'assistant', 'answer-two', 'Sekarang mau telepon orang?')
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'three', 'Halo.')
            jobs.route_gate.set()
            await wait_until(lambda: not voice.routing_turns)
            assert 'Mau lihat foto kucing?' in seen['Iya.']
            assert 'Sekarang mau telepon orang?' not in seen['Iya.']
        finally:
            await voice.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('boundary', ['final', 'partials', 'unsettled', 'retired-peer'])
def test_silent_owner_text_never_enters_whatsapp_or_the_next_confirmations_context(tmp_path, monkeypatch, boundary):
    async def exercise():
        seen = []
        class Jobs(NativeBoundary):
            async def route(self, proposal, current, previous, context, **kwargs):
                seen.append((current, previous, context))
                return {'status': 'task' if PHOTO_REQUEST in previous or PHOTO_REQUEST in context else 'reply'}
        jobs = Jobs()
        voice, _, sent, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.call_direction = 'outbound'
            voice.connection.connected.set()
            if boundary == 'retired-peer':
                voice.feed(CALLER_AUDIO)
                old = voice.audio
                replacement = adapter.AudioConnection(voice, adapter.CodexConnection)
                voice.install_audio(replacement)
                voice.retire_audio(old)
                replacement.link.connected.set()
            else:
                voice.feed(bytes(len(CALLER_AUDIO)))
            if boundary in {'partials', 'unsettled'}:
                await turn(voice, 'turn.created', 'user', 'invented', PHOTO_REQUEST[:15])
                await voice.on_data({'type':'turn.delta','turn_id':'invented','delta':PHOTO_REQUEST[15:]})
            if boundary != 'unsettled':
                await turn(voice, 'turn.done', 'user', 'invented', '' if boundary == 'partials' else PHOTO_REQUEST)
            voice.feed(CALLER_AUDIO)
            await turn(voice, 'turn.done', 'user', 'real', 'Iya.')
            if boundary == 'unsettled':
                await turn(voice, 'turn.done', 'user', 'invented', PHOTO_REQUEST)
            await wait_until(lambda: not voice.routing_turns)
            await voice.close()
            assert 'invented' not in voice.user_turns
            assert seen == [('Iya.', '', '')]
            assert not jobs.submitted
            assert all(PHOTO_REQUEST not in str(row) for row in jobs.saved)
            assert all(PHOTO_REQUEST not in str(message) for message in sent)
        finally:
            await voice.close()
    asyncio.run(exercise())


def test_actual_speech_after_provisional_start_preserves_its_partial_transcript(tmp_path, monkeypatch):
    async def exercise():
        jobs = NativeBoundary()
        voice, _, _, _ = local_voice(tmp_path, monkeypatch, agent_jobs=jobs)
        try:
            voice.connection.connected.set()
            await turn(voice, 'turn.created', 'user', 'partial', 'Tolong buatkan')
            assert 'partial' not in voice.user_turns
            voice.feed(CALLER_AUDIO)
            await voice.on_data({'type': 'turn.delta', 'turn_id': 'partial', 'delta': PHOTO_REQUEST.removeprefix('Tolong buatkan ')})
            await turn(voice, 'turn.done', 'user', 'partial', 'ke WhatsApp ini.')
            await wait_until(lambda: not voice.routing_turns)
            assert [text for _, text, _ in jobs.submitted] == [PHOTO_REQUEST]
        finally:
            await voice.close()
    asyncio.run(exercise())
