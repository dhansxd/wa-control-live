//! Call records and the conversation bridge: WhatsApp call audio <-> Gemini Live, with Hermes as
//! the voice agent's tool.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use serde::Serialize;
use serde_json::{Value, json};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;
use whatsapp_rust::{CallError, async_channel};
use whatsapp_rust::prelude::*;
use whatsapp_rust::voip::{CallHandle, CallTermination};
use whatsapp_rust::wacore::types::call::IncomingCall;
use whatsapp_rust::wacore::voip_control::media_stats::CallMediaStats;
use crate::media_diagnostics::{OutputDiagnostics, PcmDiagnostics};

use crate::audio::{Downsampler, OutputCapture, Playout, WavWriter};
use crate::config::Config;
use crate::core::{self, Core};
use crate::gemini::{Live, LiveEvent, ProviderFailure, Setup, local_adapter};
use crate::hermes::Hermes;
use crate::relay::log_inbound_offer_result;

const KEEP_RECORDS: usize = 50;
const FRAME_INTERVAL: Duration = Duration::from_millis(60);
const STILL_CONNECTED: &str = "(The call is still connected: the owner spoke after your goodbye. \
    Answer them, and call end_call again once they are done.)";

/// What a call is about, for the core session.
pub(crate) struct CallInfo {
    direction: &'static str,
    reason: String,
    details: String,
    /// First prompt to the model, and the call context for the separate-agent prompt.
    opening: String,
    context: String,
}

impl CallInfo {
    fn inbound() -> Self {
        Self {
            direction: "inbound",
            reason: String::new(),
            details: String::new(),
            opening:
                "(The owner is calling you and is now connected. Say hi briefly, the way you usually \
                talk with them.)"
                    .into(),
            context: "The owner called you.".into(),
        }
    }

    fn outbound(reason: String, details: String) -> Self {
        let opening = format!(
            "(The owner just answered the call you placed. Say hi the way you usually talk with them, \
             then get to the point: {reason})"
        );
        let mut context = format!("You placed this call. Reason: {reason}");
        if !details.is_empty() {
            context.push_str(&format!("\nDetails: {details}"));
        }
        Self {
            direction: "outbound",
            reason,
            details,
            opening,
            context,
        }
    }
}

/// The phone side of a call: the WhatsApp call, or a fake phone in tests.
pub(crate) trait Line {
    /// Resolves once the other side hangs up or the line drops.
    fn ended(&self) -> impl Future<Output = ()> + Send;
    fn hang_up(&self) -> impl Future<Output = ()> + Send;
    fn media_stats(&self) -> CallMediaStats;
}

fn start_playout(
    playout: Arc<Playout>,
    output_capture: Option<Arc<Mutex<OutputCapture>>>,
    mic_tx: async_channel::Sender<Vec<i16>>,
    connecting: Instant,
) -> (JoinHandle<()>, watch::Receiver<u64>) {
    let (accepted, progress) = watch::channel(0);
    let task = tokio::spawn(async move {
        let mut tick = tokio::time::interval(FRAME_INTERVAL);
        tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
        loop {
            let scheduled = tick.tick().await;
            let lateness = scheduled.elapsed();
            let (frame, drain) = playout.next_frame_with_trace();
            let captured_pcm = output_capture.as_ref().map(|_| frame.clone());
            let sending = Instant::now();
            let sent = mic_tx.send(frame).await;
            let accepted_at_ms = connecting.elapsed().as_millis();
            let send_wait = sending.elapsed();
            playout.observe_pacer(lateness, send_wait);
            if sent.is_err() {
                break;
            }
            if drain.source_samples > 0 {
                accepted.send_replace(drain.source_start_sample + drain.source_samples as u64);
            }
            if let (Some(capture), Some(pcm)) = (&output_capture, captured_pcm) {
                capture.lock().unwrap().sent(accepted_at_ms, &pcm, drain, lateness, send_wait);
            }
        }
    });
    (task, progress)
}

async fn play_failure_notice(
    line: &impl Line,
    playout: &Playout,
    progress: &mut watch::Receiver<u64>,
    mic_tx: &async_channel::Sender<Vec<i16>>,
    pcm: &[i16],
) -> NoticeAudio {
    if pcm.is_empty() {
        return NoticeAudio::Unavailable;
    }
    let (offset, dropped) = playout.clear();
    playout.push(pcm);
    let end = offset + dropped as u64 + pcm.len() as u64;
    let deadline = Duration::from_millis((pcm.len() as u64 * 1000).div_ceil(16_000) + 1000);
    let mut result = NoticeAudio::Unconfirmed;
    tokio::select! {
        _ = line.ended() => (),
        _ = tokio::time::timeout(deadline, async {
            if progress.wait_for(|&samples| samples >= end).await.is_ok() {
                result = NoticeAudio::MediaSubmitted;
                // Give the media worker time for frames already in its bounded input queue.
                tokio::time::sleep(FRAME_INTERVAL * (mic_tx.len() as u32 + 1)).await;
            }
        }) => (),
    }
    result
}

impl Line for CallHandle {
    fn media_stats(&self) -> CallMediaStats {
        CallHandle::media_stats(self)
    }

    fn ended(&self) -> impl Future<Output = ()> + Send {
        self.wait_ended()
    }

    fn hang_up(&self) -> impl Future<Output = ()> + Send {
        terminate(self)
    }
}

type ToolDone = (String, String, Value);

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Preparing,
    Dialing,
    Ringing,
    InProgress,
    Completed,
    NoAnswer,
    Rejected,
    Failed,
}

#[derive(Clone, Debug, Serialize)]
pub struct Turn {
    pub role: &'static str,
    pub text: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct CallRecord {
    pub id: String,
    pub direction: &'static str,
    pub status: Status,
    pub reason: String,
    pub started_at: u64,
    pub answered_at: Option<u64>,
    pub ended_at: Option<u64>,
    pub end_reason: Option<String>,
    pub transcript: Vec<Turn>,
    pub summary: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_failure: Option<ProviderFailureStatus>,
    pub metrics: Metrics,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NoticeAudio {
    Pending,
    Unavailable,
    MediaSubmitted,
    Unconfirmed,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum NoticeDm {
    Pending,
    Unavailable,
    Accepted,
    Rejected,
    Unconfirmed,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct ProviderFailureStatus {
    pub call_id: String,
    pub reason: &'static str,
    pub audio: NoticeAudio,
    pub dm: NoticeDm,
}

/// How a call went, as the sidecar measured it: what the call tests check, and what to read after
/// a real call that went wrong.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Metrics {
    /// From answer to the model's first audio.
    pub first_audio_ms: Option<u64>,
    /// Each time the owner spoke: how long until the model's next audio.
    pub reply_ms: Vec<u64>,
    /// Longest stretch with nobody speaking (the owner, or model speech still playing).
    pub longest_silence_ms: u64,
    pub tool_calls: u32,
    /// Most tool calls the model made in a row without saying anything.
    pub max_silent_tools: u32,
    /// Who ended the call: phone (the owner hung up or the line dropped), assistant, silence,
    /// limit, voice_model or error.
    pub ended_by: &'static str,
    /// Tokens the voice model billed for the call, summed over its responses.
    pub prompt_tokens: u64,
    pub response_tokens: u64,
}

/// Fills `Metrics` from the call's events as they happen.
struct Timeline {
    m: Metrics,
    answered: Instant,
    last_sound: Instant,
    owner_spoke_at: Option<Instant>,
    silent_tools: u32,
}

impl Timeline {
    fn new(answered: Instant) -> Self {
        Self {
            m: Metrics::default(),
            answered,
            last_sound: answered,
            owner_spoke_at: None,
            silent_tools: 0,
        }
    }

    fn sound(&mut self, now: Instant) {
        let gap = now.duration_since(self.last_sound).as_millis() as u64;
        self.m.longest_silence_ms = self.m.longest_silence_ms.max(gap);
        self.last_sound = now;
    }

    fn owner_spoke(&mut self, now: Instant) {
        self.sound(now);
        self.owner_spoke_at = Some(now);
    }

    fn model_audio(&mut self, now: Instant) {
        self.sound(now);
        let ms = |since: Instant| now.duration_since(since).as_millis() as u64;
        self.m.first_audio_ms.get_or_insert(ms(self.answered));
        if let Some(asked) = self.owner_spoke_at.take() {
            self.m.reply_ms.push(ms(asked));
        }
    }

    fn model_spoke(&mut self) {
        self.silent_tools = 0;
    }

    fn tool(&mut self) {
        self.m.tool_calls += 1;
        self.silent_tools += 1;
        self.m.max_silent_tools = self.m.max_silent_tools.max(self.silent_tools);
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn new_id() -> String {
    format!("{:012x}", rand::random::<u64>() & 0xffff_ffff_ffff)
}

impl CallRecord {
    fn new(direction: &'static str, reason: &str) -> Self {
        Self {
            id: new_id(),
            direction,
            status: Status::Dialing,
            reason: reason.to_string(),
            started_at: now(),
            answered_at: None,
            ended_at: None,
            end_reason: None,
            transcript: Vec::new(),
            summary: None,
            provider_failure: None,
            metrics: Metrics::default(),
        }
    }

    /// Transcription arrives in fragments; consecutive fragments from one speaker form a turn.
    fn append(&mut self, role: &'static str, text: &str) {
        match self.transcript.last_mut() {
            Some(last) if last.role == role => last.text.push_str(text),
            _ => self.transcript.push(Turn {
                role,
                text: text.to_string(),
            }),
        }
    }

    /// Whether the assistant spoke last and ended on a question.
    fn ends_with_question(&self) -> bool {
        self.transcript
            .last()
            .is_some_and(|t| t.role == "assistant" && t.text.trim_end().ends_with('?'))
    }

    pub fn transcript_text(&self) -> String {
        self.transcript
            .iter()
            .map(|t| format!("{}: {}", t.role, t.text.trim()))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

/// Recent calls, newest last, and the single active call slot.
#[derive(Default)]
pub struct Calls {
    records: Mutex<VecDeque<CallRecord>>,
    active: Mutex<Option<String>>,
}

impl Calls {
    /// Claims the active slot for a new call; fails when one is already running.
    fn begin(&self, direction: &'static str, reason: &str) -> Result<CallRecord> {
        self.begin_with_status(direction, reason, Status::Dialing)
    }

    fn begin_with_status(
        &self,
        direction: &'static str,
        reason: &str,
        status: Status,
    ) -> Result<CallRecord> {
        let mut active = self.active.lock().unwrap();
        if let Some(id) = active.as_ref() {
            bail!("a call is already in progress ({id})");
        }
        let mut record = CallRecord::new(direction, reason);
        record.status = status;
        *active = Some(record.id.clone());
        let mut records = self.records.lock().unwrap();
        records.push_back(record.clone());
        if records.len() > KEEP_RECORDS {
            records.pop_front();
        }
        Ok(record)
    }

    fn finish(&self, id: &str, status: Status, end_reason: impl Into<String>) {
        self.update(id, |r| {
            r.status = status;
            r.ended_at = Some(now());
            r.end_reason = Some(end_reason.into());
        });
        let mut active = self.active.lock().unwrap();
        if active.as_deref() == Some(id) {
            *active = None;
        }
    }

    fn update(&self, id: &str, f: impl FnOnce(&mut CallRecord)) {
        if let Some(r) = self.records.lock().unwrap().iter_mut().find(|r| r.id == id) {
            f(r);
        }
    }

    pub fn get(&self, id: &str) -> Option<CallRecord> {
        self.records
            .lock()
            .unwrap()
            .iter()
            .find(|r| r.id == id)
            .cloned()
    }

    pub fn recent(&self, limit: usize) -> Vec<CallRecord> {
        self.records
            .lock()
            .unwrap()
            .iter()
            .rev()
            .take(limit)
            .cloned()
            .collect()
    }

    pub fn last_provider_failure(&self) -> Option<ProviderFailureStatus> {
        self.records
            .lock()
            .unwrap()
            .iter()
            .rev()
            .find_map(|record| record.provider_failure.clone())
    }

    pub fn active(&self) -> Option<String> {
        self.active.lock().unwrap().clone()
    }
}

/// What the callee's devices have said about an outbound call. whatsapp-rust leaves an outgoing
/// 1:1 call's phase at `Calling`, so answering is learned from the peer's call signaling.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Signal {
    Pending,
    Ringing,
    Accepted,
    Declined,
}

/// Peer signals for the outbound call that is ringing, keyed by WhatsApp call id. Collected only
/// while a call rings. A signal can arrive before `start()` returns the id, so whichever side
/// comes first creates the entry.
#[derive(Default)]
pub struct RingSignals(Mutex<Option<HashMap<String, watch::Sender<Signal>>>>);

impl RingSignals {
    fn start(&self) {
        *self.0.lock().unwrap() = Some(HashMap::new());
    }

    fn stop(&self) {
        *self.0.lock().unwrap() = None;
    }

    /// Called for the peer's preaccept/accept/reject stanzas.
    pub fn send(&self, call_id: &str, signal: Signal) {
        if let Some(map) = self.0.lock().unwrap().as_mut() {
            map.entry(call_id.to_string())
                .or_insert_with(|| watch::channel(Signal::Pending).0)
                .send_replace(signal);
        }
    }

    fn subscribe(&self, call_id: &str) -> Option<watch::Receiver<Signal>> {
        let mut guard = self.0.lock().unwrap();
        let map = guard.as_mut()?;
        Some(
            map.entry(call_id.to_string())
                .or_insert_with(|| watch::channel(Signal::Pending).0)
                .subscribe(),
        )
    }
}

/// Decides when a call is over, without reading transcripts (they lag, and come out in the wrong
/// language). The model ends the call itself, after the owner's goodbye; the sidecar hangs up once
/// that goodbye has finished playing and a short quiet window passes. Any owner speech in the window
/// calls the hangup off. A long silence ends a call nobody closed (or left on hold too long).
/// Keeps the model talking while it works: after a few tool calls in silence it must speak to the
/// owner before running more, and it must report what its tools did before hanging up.
struct Pacing {
    limit: u32,
    silent_tools: u32,
    owes_report: bool,
}

impl Pacing {
    fn new(limit: u32) -> Self {
        Self {
            limit,
            silent_tools: 0,
            owes_report: false,
        }
    }

    fn spoke(&mut self) {
        self.silent_tools = 0;
        self.owes_report = false;
    }

    /// False when the model must talk to the owner before another tool runs.
    fn allow_tool(&mut self) -> bool {
        if self.silent_tools >= self.limit {
            return false;
        }
        self.silent_tools += 1;
        true
    }

    fn result_sent(&mut self) {
        self.owes_report = true;
    }
}

struct Ending {
    silence: Option<Duration>,
    /// Quiet time after the goodbye before hanging up.
    grace: Duration,
    requested: bool,
    /// When playback went quiet after end_call.
    quiet_since: Option<Instant>,
    last_activity: Option<Instant>,
}

impl Ending {
    fn new(silence: Option<Duration>, grace: Duration) -> Self {
        Self {
            silence,
            grace,
            requested: false,
            quiet_since: None,
            last_activity: None,
        }
    }

    fn request(&mut self) {
        self.requested = true;
        self.quiet_since = None;
    }

    /// Returns true when this called off a pending hangup.
    fn owner_spoke(&mut self, now: Instant) -> bool {
        self.last_activity = Some(now);
        self.quiet_since = None;
        std::mem::take(&mut self.requested)
    }

    /// Why the call should end now, if it should. `playing`: model speech is still being played;
    /// `working`: a request to Hermes is pending, so silence is expected.
    fn check(&mut self, now: Instant, playing: bool, working: bool) -> Option<&'static str> {
        if playing {
            self.last_activity = Some(now);
            self.quiet_since = None;
            return None;
        }
        if self.requested && now.duration_since(*self.quiet_since.get_or_insert(now)) >= self.grace
        {
            return Some("assistant ended the call");
        }
        let silence = self.silence?;
        let last = *self.last_activity.get_or_insert(now);
        (!working && now.duration_since(last) >= silence).then_some("hung up after a long silence")
    }
}

pub struct Bridge {
    pub cfg: Config,
    /// None in tests, which drive calls without WhatsApp.
    pub client: Option<Arc<Client>>,
    pub calls: Calls,
    pub ring_signals: RingSignals,
    pub owner_inbox: Option<crate::owner_chat::OwnerInbox>,
    pub control_inbox: Option<crate::control_inbox::ControlInbox>,
    hermes: Option<Hermes>,
    /// Hermes's own briefing on who it is and who the owner is; empty until fetched.
    persona: Mutex<String>,
    /// Core session: where texts the owner sends during the call go (None when no call).
    texts: Mutex<Option<mpsc::UnboundedSender<String>>>,
}

impl Bridge {
    pub fn new(cfg: Config, client: Option<Arc<Client>>) -> Self {
        let hermes = (!cfg.hermes_api_key.is_empty())
            .then(|| Hermes::new(&cfg.hermes_api_url, &cfg.hermes_api_key));
        Self {
            control_inbox: cfg.expected_bot.as_ref().map(|_| crate::control_inbox::ControlInbox::new()),
            owner_inbox: cfg
                .expected_bot
                .as_ref()
                .map(|_| crate::owner_chat::OwnerInbox::new()),
            cfg,
            client,
            calls: Calls::default(),
            hermes,
            ring_signals: RingSignals::default(),
            persona: Mutex::default(),
            texts: Mutex::default(),
        }
    }

    /// Hands a WhatsApp text from the owner to the active core-session call; false if none.
    pub fn deliver_text(&self, text: &str) -> bool {
        self.texts
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|tx| tx.send(text.to_string()).is_ok())
    }

    /// Rings until the owner answers (true) or the call is declined, ends, or times out (false,
    /// with the call record finished).
    async fn ring(
        &self,
        id: &str,
        call_id: &str,
        line: &impl Line,
        prepared: &mut Option<Live>,
    ) -> bool {
        let Some(mut rx) = self.ring_signals.subscribe(call_id) else {
            self.ring_signals.stop();
            drop(prepared.take());
            line.hang_up().await;
            self.calls.finish(id, Status::Failed, "call signaling unavailable");
            return false;
        };
        let ended = line.ended();
        tokio::pin!(ended);
        let ringing = tokio::time::timeout(self.cfg.ring_timeout, async {
            loop {
                match *rx.borrow_and_update() {
                    Signal::Accepted => return Some(true),
                    Signal::Declined => return Some(false),
                    Signal::Ringing => self.calls.update(id, |r| r.status = Status::Ringing),
                    Signal::Pending => {}
                }
                tokio::select! {
                    _ = &mut ended => return None,
                    changed = rx.changed() => if changed.is_err() { return None },
                }
            }
        });
        // The offer already has a handle here, so we can hang up if the prepared voice ends.
        let outcome = if let Some(live) = prepared.as_mut() {
            tokio::select! {
                biased;
                outcome = ringing => Ok(outcome),
                failure = live.wait_terminated() => Err(failure),
            }
        } else {
            Ok(ringing.await)
        };
        self.ring_signals.stop();
        if !matches!(outcome, Ok(Ok(Some(true)))) {
            drop(prepared.take());
        }
        match outcome {
            Ok(Ok(Some(true))) => return true,
            Ok(Ok(Some(false))) => {
                line.hang_up().await;
                self.calls.finish(id, Status::Rejected, "declined");
            }
            Ok(Ok(None)) => {
                self.calls
                    .finish(id, Status::Rejected, "ended before answer");
            }
            Ok(Err(_)) => {
                line.hang_up().await;
                self.calls.finish(id, Status::NoAnswer, "no answer");
            }
            Err(failure) => {
                line.hang_up().await;
                self.fail_outbound_audio(id, failure).await;
            }
        }
        false
    }

    fn owner_jid(&self) -> Jid {
        Jid::pn(self.cfg.owner.as_str())
    }

    /// Whether an incoming offer comes from the owner's number.
    pub async fn is_owner(
        &self,
        call: &IncomingCall,
        creator: &Jid,
        caller_pn: Option<&Jid>,
    ) -> bool {
        for jid in caller_pn.into_iter().chain([creator, &call.from]) {
            if jid.is_pn() {
                return jid.user == self.cfg.owner;
            }
        }
        let Some(client) = &self.client else {
            return false;
        };
        for jid in [creator, &call.from] {
            if self.cfg.expected_bot.is_some() {
                if crate::owner_chat::owner_address(client, jid, &self.cfg.owner).await {
                    return true;
                }
                continue;
            }
            if jid.is_lid()
                && let Ok(Some(entry)) = client.get_lid_pn_entry(jid).await
            {
                return *entry.phone_number == *self.cfg.owner;
            }
        }
        log::warn!(
            "call from {creator}: no phone number known for this LID, so it cannot be matched to the owner"
        );
        false
    }

    /// Validates and registers an outbound call; the caller spawns `run_outbound`.
    pub fn begin_outbound(&self, reason: &str) -> Result<CallRecord> {
        self.calls.begin_with_status(
            "outbound",
            reason,
            if self.prepare_outbound_audio() { Status::Preparing } else { Status::Dialing },
        )
    }

    fn prepare_outbound_audio(&self) -> bool {
        self.cfg.context_url.is_empty()
            && local_adapter(&self.cfg.gemini_url, &self.cfg.gemini_api_key)
    }

    async fn prepare_and_dial<T>(
        &self,
        id: &str,
        info: &CallInfo,
        dial: impl AsyncFnOnce() -> Result<T>,
    ) -> Option<(T, Option<Live>)> {
        let prepared = if self.prepare_outbound_audio() {
            let connecting = Instant::now();
            match Live::connect(
                &self.cfg.gemini_url,
                &self.cfg.gemini_api_key,
                self.setup(&info.context),
            ).await {
                Ok(mut live) if live.is_terminated() => {
                    let failure = live.wait_terminated().await;
                    drop(live);
                    self.fail_outbound_audio(id, failure).await;
                    return None;
                }
                Ok(live) => {
                    log::info!(
                        "call {id}: voice model prepared before dialing in {:.2}s",
                        connecting.elapsed().as_secs_f32()
                    );
                    self.calls.update(id, |r| r.status = Status::Dialing);
                    Some(live)
                }
                Err(failure) => {
                    self.fail_outbound_audio(id, failure).await;
                    return None;
                }
            }
        } else {
            None
        };
        match dial().await {
            Ok(handle) => Some((handle, prepared)),
            Err(e) => {
                drop(prepared);
                self.calls.finish(id, Status::Failed, format!("{e:#}"));
                None
            }
        }
    }

    async fn fail_outbound_audio(&self, id: &str, failure: anyhow::Error) {
        let notice = match failure.downcast::<ProviderFailure>() {
            Ok(notice) => notice,
            Err(error) => {
                log::warn!("call {id}: voice unavailable before answer: {error:#}");
                ProviderFailure {
                    reason: "audio_connection_lost",
                    message: "Audio Live belum tersedia. Lihat /walive status untuk rinciannya.".into(),
                    pcm: Vec::new(),
                }
            }
        };
        if self.begin_provider_failure(id, &notice, false) {
            self.calls.update(id, |r| r.metrics.ended_by = "voice_model");
            self.calls.finish(id, Status::Failed, notice.reason);
            self.deliver_provider_dm(id, notice.message).await;
        }
    }

    pub async fn run_outbound(self: Arc<Self>, id: String, reason: String, details: String) {
        let Some(client) = self.client.clone() else {
            return self
                .calls
                .finish(&id, Status::Failed, "WhatsApp is not connected");
        };
        let info = CallInfo::outbound(reason, details);
        let (mic_tx, mic_rx) = async_channel::bounded::<Vec<i16>>(8);
        let (spk_tx, spk_rx) = async_channel::bounded::<Vec<i16>>(64);
        let Some((handle, mut prepared)) = self.prepare_and_dial(&id, &info, async || {
            if !crate::owner_chat::account_ready(&self) {
                bail!("WhatsApp is not connected");
            }
            self.ring_signals.start();
            let started = client
                .voip()
                .call(&self.owner_jid())
                .audio_opus(mic_rx, spk_tx)
                .start()
                .await;
            if started.is_err() {
                self.ring_signals.stop();
            }
            started.context("could not place call")
        }).await else {
            return;
        };
        log::info!("call {id}: dialing owner (whatsapp call {})", handle.call_id());
        if self.ring(&id, handle.call_id(), &handle, &mut prepared).await {
            self.converse(&id, &handle, mic_tx, spk_rx, info, prepared).await;
        }
    }

    pub async fn run_inbound(self: Arc<Self>, call: IncomingCall) {
        let Some(client) = self.client.clone() else {
            return;
        };
        let record = match self.calls.begin("inbound", "owner called") {
            Ok(r) => r,
            Err(e) => return log::info!("ignoring owner call: {e}"),
        };
        let (mic_tx, mic_rx) = async_channel::bounded::<Vec<i16>>(8);
        let (spk_tx, spk_rx) = async_channel::bounded::<Vec<i16>>(64);
        let info = CallInfo::inbound();
        let mut prepared = None;
        let mut provider_notice = None;
        let voip = client.voip();
        let answer = voip
            .accept(&call)
            .with_relay_failover()
            .audio_opus(mic_rx, spk_tx);
        // The plugin loads its chat context inside the live adapter. The upstream core-session
        // service opens a call record, so keep its existing after-answer setup.
        let started = if self.cfg.context_url.is_empty() {
            answer
                .start_after(async {
                    self.calls.update(&record.id, |r| r.status = Status::Ringing);
                    let connecting = Instant::now();
                    match Live::connect(
                        &self.cfg.gemini_url,
                        &self.cfg.gemini_api_key,
                        self.setup(&info.context),
                    ).await {
                        Ok(live) => prepared = Some(live),
                        Err(e) => {
                            provider_notice = e.downcast_ref::<ProviderFailure>().cloned();
                            return Err(CallError::Setup(format!("voice model unavailable: {e:#}")));
                        }
                    }
                    log::info!(
                        "call {}: voice model prepared while ringing in {:.2}s",
                        record.id,
                        connecting.elapsed().as_secs_f32()
                    );
                    Ok(())
                })
                .await
        } else {
            answer.start().await
        };
        if started.is_err() {
            drop(prepared.take());
        }
        log_inbound_offer_result(
            &record.id,
            call.media().and_then(|media| media.relay.as_ref()),
            started.as_ref().err(),
        );
        let handle = match started {
            Ok(h) => h,
            Err(e) => {
                // Close any prepared voice connection before releasing the incoming call slot.
                drop(prepared);
                if matches!(e, CallError::CallEndedDuringSetup) {
                    return self.calls.finish(&record.id, Status::NoAnswer, "ended during answer setup");
                }
                if let Some(notice) = provider_notice
                    && self.begin_provider_failure(&record.id, &notice, false)
                {
                    self.calls.update(&record.id, |r| r.metrics.ended_by = "voice_model");
                    self.calls.finish(&record.id, Status::Failed, notice.reason);
                    return self.deliver_provider_dm(&record.id, notice.message).await;
                }
                return self.calls.finish(&record.id, Status::Failed, format!("could not answer: {e}"));
            }
        };
        log::info!(
            "call {}: answered owner (whatsapp call {})",
            record.id,
            handle.call_id()
        );
        self.converse(&record.id, &handle, mic_tx, spk_rx, info, prepared)
            .await;
    }

    fn begin_provider_failure(
        &self,
        id: &str,
        notice: &ProviderFailure,
        audio_available: bool,
    ) -> bool {
        let mut started = false;
        self.calls.update(id, |record| {
            if record.provider_failure.is_none() {
                record.provider_failure = Some(ProviderFailureStatus {
                    call_id: id.into(),
                    reason: notice.reason,
                    audio: if audio_available && !notice.pcm.is_empty() {
                        NoticeAudio::Pending
                    } else {
                        NoticeAudio::Unavailable
                    },
                    dm: NoticeDm::Pending,
                });
                started = true;
            }
        });
        started
    }

    fn record_provider_audio(&self, id: &str, audio: NoticeAudio) {
        self.calls.update(id, |record| {
            if let Some(status) = &mut record.provider_failure {
                status.audio = audio;
            }
        });
    }

    async fn deliver_provider_dm(&self, id: &str, message: String) {
        let dm = crate::server::send_provider_notice(self, id, message).await;
        self.calls.update(id, |record| {
            if let Some(status) = &mut record.provider_failure {
                status.dm = dm;
            }
        });
    }

    /// Asks Hermes for a short briefing so the voice model shares its name, persona and memory.
    /// Fetched ahead of time so answering a call never waits on it.
    pub async fn refresh_persona(&self) {
        // In core-session mode the voice gets the session's own prompt instead.
        if !self.cfg.context_url.is_empty() {
            return;
        }
        let Some(h) = &self.hermes else { return };
        let prompt = "A voice version of you is about to talk with the owner on a WhatsApp call, and it \
            knows only what you write here. In under 150 words of plain sentences, tell it: your name and \
            personality as you present yourself, how the owner likes to be spoken to (including \
            language), and the few facts about the owner and their current priorities it should know. \
            Write it as instructions to that voice (\"You are ...\").";
        match h
            .run(
                prompt,
                "Reply in plain text only.",
                "whatsapp-voice-briefing",
                self.cfg.hermes_timeout,
            )
            .await
        {
            Ok(text) if !text.trim().is_empty() => {
                log::info!(
                    "persona briefing from Hermes loaded ({} chars)",
                    text.trim().len()
                );
                *self.persona.lock().unwrap() = text.trim().to_string()
            }
            Ok(_) => {}
            Err(e) => log::warn!("could not fetch persona briefing from Hermes: {e:#}"),
        }
    }

    fn setup(&self, context: &str) -> Setup {
        let persona = self.persona.lock().unwrap().clone();
        let tools_note = if self.hermes.is_some() {
            // Never name the voice "Hermes" here: the persona briefing supplies the name, and this
            // line used to override it ("this is a test call from Hermes").
            "The ask_hermes tool reaches the owner's full agent system, which has their memory, files, \
             calendar, reminders, messages and tools. For any action, or any fact you do not already \
             know, call ask_hermes with a clear, self-contained request. Say a few words first, like \"one moment, \
             let me check\"; the answer arrives a little later and you will be told when it does. Never \
             make up results."
        } else {
            "You cannot take actions or look things up during this call; say so if asked."
        };
        let mut owner_instructions = self.cfg.voice_instructions.clone();
        if let Some(first) = self.cfg.languages.first() {
            owner_instructions.push_str(&format!(
                "\n- Speak only these languages: {}. If the owner uses any other language, reply in {first}.",
                self.cfg.languages.join(", ")
            ));
        }
        let system_instruction = system_instruction(
            &self.cfg.assistant_name,
            &persona,
            &owner_instructions,
            tools_note,
            context,
        );
        let mut functions = vec![end_call_function()];
        if self.hermes.is_some() {
            functions.push(json!({
                "name": "ask_hermes",
                "description": "Hand a request to Hermes, the owner's full agent with memory and tools \
                    (calendar, reminders, messages, notes, web, home). Use it for any action or any \
                    information you do not have.",
                "parameters": {
                    "type": "object",
                    "properties": {"request": {
                        "type": "string",
                        "description": "A complete, self-contained instruction or question for Hermes.",
                    }},
                    "required": ["request"],
                },
                "behavior": "NON_BLOCKING",
            }));
        }
        Setup {
            model: self.cfg.model.clone(),
            voice: self.cfg.voice.clone(),
            system_instruction,
            functions,
            languages: self.cfg.languages.clone(),
            speech_language: self.cfg.speech_language.clone(),
            pause_ms: self.cfg.pause_ms,
            overrides: self.cfg.live_config.clone(),
            history: vec![],
            vocabulary: self.cfg.vocabulary.clone(),
        }
    }

    /// Setup when the voice acts as the owner's DM session: that session's prompt, history and tools.
    fn core_setup(&self, ctx: core::CallContext) -> Setup {
        let mut functions = ctx.functions;
        functions.push(end_call_function());
        functions.extend(core::bridge_functions());
        let mut vocabulary = self.cfg.vocabulary.clone();
        vocabulary.extend(ctx.vocabulary);
        vocabulary.sort();
        vocabulary.dedup();
        Setup {
            model: self.cfg.model.clone(),
            voice: self.cfg.voice.clone(),
            system_instruction: ctx.system_instruction,
            functions,
            languages: self.cfg.languages.clone(),
            speech_language: self.cfg.speech_language.clone(),
            pause_ms: self.cfg.pause_ms,
            overrides: self.cfg.live_config.clone(),
            history: ctx.history,
            vocabulary,
        }
    }

    /// Opens the core session for a call; None (separate-agent mode) if it is off or unavailable.
    async fn start_core(&self, id: &str, info: &CallInfo) -> Option<(Core, core::CallContext)> {
        if self.cfg.context_url.is_empty() {
            return None;
        }
        let core = Core::new(&self.cfg.context_url, &self.cfg.token, id);
        let started = Instant::now();
        match tokio::time::timeout(
            Duration::from_secs(30),
            core.start(info.direction, &info.reason, &info.details),
        )
        .await
        {
            Ok(Ok(ctx)) => {
                log::info!(
                    "call {id}: core session {} loaded in {:.2}s ({} history turns, {} tools)",
                    ctx.session_id,
                    started.elapsed().as_secs_f32(),
                    ctx.history.len(),
                    ctx.functions.len()
                );
                Some((core, ctx))
            }
            Ok(Err(e)) => {
                log::warn!(
                    "call {id}: core session unavailable ({e:#}); using the separate voice agent"
                );
                None
            }
            Err(_) => {
                log::warn!("call {id}: core session timed out; using the separate voice agent");
                None
            }
        }
    }

    fn spawn_ask_hermes(
        &self,
        id: &str,
        call_id: &str,
        args: &Value,
        done: mpsc::UnboundedSender<ToolDone>,
    ) -> JoinHandle<()> {
        let hermes = self.hermes.clone();
        let hermes_timeout = self.cfg.hermes_timeout;
        let request = args["request"].as_str().unwrap_or_default().to_string();
        let (tool_id, session) = (id.to_string(), format!("whatsapp-voice-{call_id}"));
        tokio::spawn(async move {
            let result = match hermes {
                None => json!({"error": "Hermes is not connected"}),
                Some(h) => {
                    let instructions = "This request comes from the owner, live on a WhatsApp voice call \
                        with your voice agent. Do what is asked. Reply in one to three short, plain \
                        sentences that can be read aloud: no markdown, lists, links or emoji.";
                    match h
                        .run(&request, instructions, &session, hermes_timeout)
                        .await
                    {
                        Ok(answer) => json!({"result": answer}),
                        Err(e) => json!({"error": format!("{e:#}")}),
                    }
                }
            };
            let _ = done.send((tool_id, "ask_hermes".to_string(), result));
        })
    }

    pub(crate) async fn converse(
        &self,
        id: &str,
        line: &impl Line,
        mic_tx: async_channel::Sender<Vec<i16>>,
        spk_rx: async_channel::Receiver<Vec<i16>>,
        info: CallInfo,
        prepared: Option<Live>,
    ) {
        self.calls.update(id, |r| {
            r.status = Status::InProgress;
            r.answered_at = Some(now());
        });
        let connecting = Instant::now();
        let mut startup_events = VecDeque::new();
        let (core, mut live) = match prepared {
            Some(mut live) => {
                // Model PCM buffered during ringing or relay setup must not delay the first reply.
                // Keep billing and disconnect events even if they arrived before the answer.
                for _ in 0..live.events.len() {
                    let Ok(event) = live.events.try_recv() else { break };
                    if matches!(event, LiveEvent::Usage { .. } | LiveEvent::Closed(_) | LiveEvent::ProviderFailure(_)) {
                        startup_events.push_back(event);
                    }
                }
                (None, live)
            }
            None => {
                let (core, setup) = match self.start_core(id, &info).await {
                    Some((core, ctx)) => (Some(core), self.core_setup(ctx)),
                    None => (None, self.setup(&info.context)),
                };
                let live =
                    match Live::connect(&self.cfg.gemini_url, &self.cfg.gemini_api_key, setup).await {
                        Ok(l) => l,
                        Err(e) => {
                            log::error!("call {id}: voice model unavailable: {e:#}");
                            if let Some(notice) = e.downcast_ref::<ProviderFailure>()
                                && self.begin_provider_failure(id, notice, true)
                            {
                                if !notice.pcm.is_empty() {
                                    let playout = Arc::new(Playout::default());
                                    let (pacer, mut progress) =
                                        start_playout(playout.clone(), None, mic_tx.clone(), connecting);
                                    let audio = play_failure_notice(
                                        line, &playout, &mut progress, &mic_tx, &notice.pcm,
                                    ).await;
                                    pacer.abort();
                                    let _ = pacer.await;
                                    self.record_provider_audio(id, audio);
                                }
                                line.hang_up().await;
                                self.calls.update(id, |r| r.metrics.ended_by = "voice_model");
                                self.calls.finish(id, Status::Failed, notice.reason);
                                return self.deliver_provider_dm(id, notice.message.clone()).await;
                            }
                            line.hang_up().await;
                            self.calls.update(id, |r| r.metrics.ended_by = "error");
                            return self.calls.finish(
                                id,
                                Status::Failed,
                                format!("voice model unavailable: {e:#}"),
                            );
                        }
                    };
                log::info!(
                    "call {id}: voice model ready {:.2}s after answer",
                    connecting.elapsed().as_secs_f32()
                );
                (core, live)
            }
        };
        live.send_text(&info.opening);

        // Core session: the call is written back to the owner's DM session as it happens, in order,
        // and text messages that arrive meanwhile come back as context for the model.
        let (events, mut news_rx, writer) = match &core {
            Some(core) => {
                let (tx, rx) = mpsc::unbounded_channel::<Value>();
                let (news_tx, news_rx) = mpsc::unbounded_channel::<String>();
                (
                    Some(tx),
                    news_rx,
                    Some(tokio::spawn(write_back(core.clone(), rx, news_tx))),
                )
            }
            None => (None, mpsc::unbounded_channel().1, None),
        };
        let emit = |event: Value| {
            if let Some(tx) = &events {
                let _ = tx.send(event);
            }
        };

        // Model speech goes out on a fixed 60 ms clock; the library does not pace for us.
        let playout = Arc::new(Playout::default());
        let output_capture = self.cfg.recordings.as_ref().and_then(|dir| {
            OutputCapture::create(dir, id)
                .map(|capture| Arc::new(Mutex::new(capture)))
                .map_err(|error| log::warn!("call {id}: cannot record output: {error}"))
                .ok()
        });
        let (pacer, mut playout_progress) =
            start_playout(playout.clone(), output_capture.clone(), mic_tx.clone(), connecting);

        let mut down = Downsampler::new();
        let (tool_tx, mut tool_rx) = mpsc::unbounded_channel::<ToolDone>();
        let mut tool_tasks: HashMap<String, JoinHandle<()>> = HashMap::new();
        let mut ending = Ending::new(self.cfg.silence_timeout, self.cfg.hangup_grace);
        let mut pacing = Pacing::new(self.cfg.max_silent_tools);
        let mut timeline = Timeline::new(connecting);
        let mut recorder = self.cfg.recordings.as_ref().and_then(|dir| {
            std::fs::create_dir_all(dir)
                .and_then(|_| WavWriter::create(&dir.join(format!("{id}.wav")), 16_000))
                .map_err(|e| log::warn!("call {id}: cannot record the owner's audio: {e}"))
                .ok()
        });
        // Texts the owner sends during a core-session call come here instead of the text agent.
        let (text_tx, mut text_rx) = mpsc::unbounded_channel::<String>();
        if core.is_some() {
            *self.texts.lock().unwrap() = Some(text_tx);
        }
        let mut pcm_diagnostics = PcmDiagnostics::default();
        let mut output_diagnostics = OutputDiagnostics::default();
        let mut media_check = tokio::time::interval(Duration::from_secs(1));
        media_check.set_missed_tick_behavior(MissedTickBehavior::Skip);
        let mut check = tokio::time::interval(Duration::from_millis(200));
        let max_duration = async {
            match self.cfg.max_duration {
                Some(duration) => tokio::time::sleep(duration).await,
                None => std::future::pending().await,
            }
        };
        let ended = line.ended();
        tokio::pin!(max_duration, ended);

        let mut provider_notice = None;
        let (mut end_reason, ended_by) = loop {
            tokio::select! {
                _ = &mut ended => break ("call ended".to_string(), "phone"),
                _ = &mut max_duration => break ("maximum call length reached".to_string(), "limit"),
                frame = spk_rx.recv() => match frame {
                    Ok(f) => {
                        pcm_diagnostics.observe(&f);
                        if let Some(w) = &mut recorder {
                            let _ = w.write(&f);
                        }
                        live.send_audio(&f);
                    }
                    Err(_) => break ("call audio closed".to_string(), "phone"),
                },
                Some((tool_id, name, result)) = tool_rx.recv() => {
                    tool_tasks.remove(&tool_id);
                    pacing.result_sent();
                    emit(json!({"type": "tool_result", "id": tool_id, "name": name, "result": result_text(&result)}));
                    live.send_tool_response(&tool_id, &name, result, "WHEN_IDLE");
                }
                Some(news) = news_rx.recv() => live.add_context(&news),
                Some(text) = text_rx.recv() => {
                    log::info!("call {id}: owner texted during the call");
                    if ending.owner_spoke(Instant::now()) {
                        live.send_text(STILL_CONNECTED);
                    }
                    emit(json!({"type": "owner_text", "text": text}));
                    self.calls.update(id, |r| r.append("owner", &format!(" (texted) {text} ")));
                    live.send_text(&format!("(The owner just texted you this during the call: {text})"));
                }
                _ = media_check.tick() => {
                    let mut snapshot = pcm_diagnostics.snapshot(connecting.elapsed().as_millis(), line.media_stats());
                    snapshot["output"] = output_diagnostics.snapshot(playout.snapshot());
                    log::info!("call {id}: media_snapshot {snapshot}");
                }
                _ = check.tick() => {
                    let playing = playout.buffered() > 0;
                    if playing {
                        timeline.sound(Instant::now());
                    }
                    if let Some(why) = ending.check(Instant::now(), playing, !tool_tasks.is_empty()) {
                        let by = if why.starts_with("assistant") { "assistant" } else { "silence" };
                        break (why.to_string(), by);
                    }
                }
                ev = async {
                    match startup_events.pop_front() {
                        Some(event) => Some(event),
                        None => live.events.recv().await,
                    }
                } => match ev {
                    // Unlike a model's proposed farewell, this is a committed owner action.
                    Some(LiveEvent::OwnerCallEnd) => break ("assistant ended the call".to_string(), "assistant"),
                    Some(LiveEvent::Audio(pcm)) => {
                        let at_ms = connecting.elapsed().as_millis();
                        output_diagnostics.observe(&pcm, at_ms);
                        timeline.model_audio(Instant::now());
                        let samples = down.process(&pcm);
                        playout.push(&samples);
                        if let Some(capture) = &output_capture {
                            capture.lock().unwrap().provider(at_ms, pcm.len(), samples.len());
                        }
                    }
                    Some(LiveEvent::RecoveryAudio(pcm)) => {
                        let (offset, samples) = playout.clear();
                        if let Some(capture) = &output_capture {
                            let at_ms = connecting.elapsed().as_millis();
                            let mut capture = capture.lock().unwrap();
                            capture.cleared(at_ms, offset, samples);
                            capture.recovery_audio(at_ms, pcm.len());
                        }
                        playout.push(&pcm);
                    }
                    Some(LiveEvent::Interrupted) => {
                        let (offset, samples) = playout.clear();
                        if let Some(capture) = &output_capture {
                            capture.lock().unwrap().cleared(connecting.elapsed().as_millis(), offset, samples);
                        }
                        if ending.owner_spoke(Instant::now()) {
                            live.send_text(STILL_CONNECTED);
                        }
                    }
                    Some(LiveEvent::InputTranscript(t)) => {
                        timeline.owner_spoke(Instant::now());
                        if ending.owner_spoke(Instant::now()) {
                            live.send_text(STILL_CONNECTED);
                        }
                        emit(json!({"type": "owner", "text": t}));
                        self.calls.update(id, |r| r.append("owner", &t));
                    }
                    Some(LiveEvent::OutputTranscript(t)) => {
                        pacing.spoke();
                        timeline.model_spoke();
                        emit(json!({"type": "assistant", "text": t}));
                        self.calls.update(id, |r| r.append("assistant", &t));
                    }
                    Some(LiveEvent::TurnComplete) => emit(json!({"type": "turn_complete"})),
                    Some(LiveEvent::Usage { prompt, response }) => {
                        timeline.m.prompt_tokens += prompt;
                        timeline.m.response_tokens += response;
                    }
                    Some(LiveEvent::ToolCall { id: tool_id, name, args }) => match name.as_str() {
                        "ask_hermes" if core.is_none() => {
                            timeline.tool();
                            log::info!("call {id}: ask_hermes {}", args["request"]);
                            let task = self.spawn_ask_hermes(&tool_id, id, &args, tool_tx.clone());
                            tool_tasks.insert(tool_id, task);
                        }
                        "end_call" => {
                            // The model sometimes asks a question and hangs up in the same turn.
                            if self.calls.get(id).is_some_and(|r| r.ends_with_question()) {
                                live.send_tool_response(&tool_id, &name, json!({"error":
                                    "You just asked the owner a question. Wait for their answer, and end the call only after they say bye."}), "SILENT");
                            } else if pacing.owes_report {
                                live.send_tool_response(&tool_id, &name, json!({"error":
                                    "Tell the owner what came of your last tools first (done, failed, or still open), then end the call only once they are done."}), "SILENT");
                            } else {
                                live.send_tool_response(&tool_id, &name, json!({"ok": true}), "SILENT");
                                ending.request();
                            }
                        }
                        _ => match &core {
                            Some(_) if !pacing.allow_tool() => {
                                log::info!("call {id}: tool {name} held back until the model speaks");
                                live.send_tool_response(&tool_id, &name, json!({"error":
                                    "You have run several tools without saying anything. Tell the owner briefly what you are doing or what you found, then continue."}), "WHEN_IDLE");
                            }
                            Some(core) => {
                                timeline.tool();
                                log::info!("call {id}: tool {name} {}", truncate(&args.to_string(), 200));
                                emit(json!({"type": "tool_call", "id": tool_id, "name": name, "args": args}));
                                let (core, done, tid) = (core.clone(), tool_tx.clone(), tool_id.clone());
                                let task = tokio::spawn(async move {
                                    let started = Instant::now();
                                    let result = core.call_tool(&tid, &name, &args).await;
                                    log::info!("tool {name} answered in {:.2}s", started.elapsed().as_secs_f32());
                                    let _ = done.send((tid, name, result));
                                });
                                tool_tasks.insert(tool_id, task);
                            }
                            None => live.send_tool_response(&tool_id, &name, json!({"error": "unknown function"}), "WHEN_IDLE"),
                        },
                    },
                    Some(LiveEvent::ToolCancelled(ids)) => {
                        for tool_id in ids {
                            if let Some(task) = tool_tasks.remove(&tool_id) {
                                task.abort();
                                emit(json!({"type": "tool_result", "id": tool_id, "result": "(cancelled)"}));
                            }
                        }
                    }
                    Some(LiveEvent::ProviderFailure(notice)) => {
                        let reason = notice.reason;
                        if self.begin_provider_failure(id, &notice, true) {
                            let audio = play_failure_notice(
                                line, &playout, &mut playout_progress, &mic_tx, &notice.pcm,
                            ).await;
                            self.record_provider_audio(id, audio);
                            provider_notice = Some(notice.message);
                        }
                        break (reason.into(), "voice_model");
                    }
                    Some(LiveEvent::Closed(why)) => break (format!("voice model disconnected: {why}"), "voice_model"),
                    None => break ("voice model stopped".to_string(), "voice_model"),
                },
            }
        };

        *self.texts.lock().unwrap() = None;
        pacer.abort();
        tool_tasks.values().for_each(JoinHandle::abort);
        let _ = pacer.await;
        if let Some(capture) = &output_capture {
            capture.lock().unwrap().finish();
        }
        drop(live);
        match ended_by {
            "assistant" => {
                // Dropping the native terminate future tears down this exact call
                // even when WhatsApp cannot send the final stanza to the peer.
                if tokio::time::timeout(Duration::from_secs(5), line.hang_up()).await.is_err() {
                    end_reason.push_str(" (peer notification timed out)");
                }
            }
            "phone" => {}
            _ => line.hang_up().await,
        }
        if let Some(w) = recorder
            && let Err(e) = w.finish()
        {
            log::warn!("call {id}: could not save the recording: {e}");
        }
        let mut snapshot = pcm_diagnostics.snapshot(connecting.elapsed().as_millis(), line.media_stats());
        snapshot["output"] = output_diagnostics.snapshot(playout.snapshot());
        log::info!("call {id}: media_snapshot_final {snapshot}");
        timeline.m.ended_by = ended_by;
        log::info!("call {id}: {end_reason} ({:?})", timeline.m);
        self.calls.update(id, |r| r.metrics = timeline.m);
        let status = if ended_by == "voice_model" {
            Status::Failed
        } else {
            Status::Completed
        };
        self.calls.finish(id, status, end_reason);
        let has_core = core.is_some();
        if let (Some(core), Some(writer)) = (core, writer) {
            drop(events);
            // Save the call independently of the owner notification and any session lock.
            let call = id.to_string();
            tokio::spawn(async move {
                let _ = writer.await;
                match core.end().await {
                    Ok(n) => log::info!("call {call}: {n} messages saved to the chat session"),
                    Err(e) => log::warn!(
                        "call {call}: could not save the call to the chat session: {e:#}"
                    ),
                }
            });
        }
        if let Some(message) = provider_notice {
            return self.deliver_provider_dm(id, message).await;
        }
        if has_core {
            return self.send_core_summary(id).await;
        }
        self.send_summary(id).await;
        // Memory may have changed during the call (e.g. a new preference); the next call should know.
        self.refresh_persona().await;
    }

    /// Core-session calls are already in the chat history; the owner just gets a short note.
    async fn send_core_summary(&self, id: &str) {
        let Some(record) = self.calls.get(id) else {
            return;
        };
        if !self.cfg.send_summary || record.transcript.is_empty() {
            return;
        }
        let minutes = record
            .answered_at
            .zip(record.ended_at)
            .map(|(a, e)| e.saturating_sub(a).div_ceil(60))
            .unwrap_or(0);
        let text = format!("📞 Call ended ({minutes} min). It's saved in this chat.");
        self.message_owner(id, text).await;
    }

    async fn message_owner(&self, id: &str, text: String) {
        let Some(client) = &self.client else { return };
        if let Err(e) = client
            .send_message(&self.owner_jid(), wa::Message::text(text))
            .await
        {
            log::warn!("call {id}: could not send summary: {e}");
        }
    }

    /// After a call: let Hermes digest the transcript, then message the owner a summary.
    async fn send_summary(&self, id: &str) {
        let Some(record) = self.calls.get(id) else {
            return;
        };
        if record.transcript.is_empty() {
            return;
        }
        let transcript = record.transcript_text();
        let summary = match &self.hermes {
            Some(h) => {
                let input = format!(
                    "A WhatsApp voice call between the owner and your voice agent just ended. \
                     Transcript:\n\n{transcript}\n\nNote anything worth remembering. Do not redo \
                     actions that were already done during the call. Reply with only a two or three \
                     sentence summary of the call for the owner."
                );
                match h
                    .run(
                        &input,
                        "Reply in plain text only.",
                        &format!("whatsapp-voice-{id}"),
                        self.cfg.hermes_timeout,
                    )
                    .await
                {
                    Ok(s) if !s.trim().is_empty() => s,
                    Ok(_) => transcript.clone(),
                    Err(e) => {
                        log::warn!("call {id}: summary run failed: {e:#}");
                        transcript.clone()
                    }
                }
            }
            None => transcript.clone(),
        };
        self.calls.update(id, |r| r.summary = Some(summary.clone()));
        if !self.cfg.send_summary {
            return;
        }
        let minutes = record
            .answered_at
            .zip(record.ended_at)
            .map(|(a, e)| e.saturating_sub(a).div_ceil(60))
            .unwrap_or(0);
        let text = format!(
            "📞 Call summary ({minutes} min):\n{}",
            truncate(&summary, 3500)
        );
        self.message_owner(id, text).await;
    }
}

fn system_instruction(
    name: &str,
    persona: &str,
    owner_instructions: &str,
    tools_note: &str,
    context: &str,
) -> String {
    let identity = if persona.is_empty() {
        format!("You are {name}, the owner's personal assistant.")
    } else {
        format!(
            "Hermes describes who you are and who the owner is; stay in that character:\n{persona}"
        )
    };
    format!(
        "{identity}\n\nYou are talking with the owner on a live WhatsApp voice call.\n\
         - This is a phone call: speak naturally and keep replies short. No lists, markdown, links or emoji.\n\
         - {tools_note}\n\
         - End the call when the owner says goodbye, or once the purpose is done and they have nothing \
         more: give a brief goodbye and call end_call in that same reply. Never call end_call while \
         asking a question.\n\
         {owner_instructions}\n\n\
         {context}"
    )
}

fn end_call_function() -> Value {
    json!({
        "name": "end_call",
        "description": "Hang up the call. Call it only after you have said goodbye.",
        "behavior": "NON_BLOCKING",
    })
}

/// A tool response as text for the chat history.
fn result_text(result: &Value) -> String {
    match result.get("result").and_then(Value::as_str) {
        Some(text) => text.to_string(),
        None => result.to_string(),
    }
}

/// Posts call events to the core session in order, batching whatever piled up meanwhile.
async fn write_back(
    core: Core,
    mut rx: mpsc::UnboundedReceiver<Value>,
    news: mpsc::UnboundedSender<String>,
) {
    while let Some(first) = rx.recv().await {
        let mut batch = vec![first];
        while let Ok(more) = rx.try_recv() {
            batch.push(more);
        }
        match core.events(batch).await {
            Ok(text) if !text.is_empty() => {
                let _ = news.send(text);
            }
            Ok(_) => {}
            Err(e) => log::warn!("could not save call events: {e:#}"),
        }
    }
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max).collect();
    out.push('…');
    out
}

async fn terminate(handle: &CallHandle) {
    match handle.terminate().await {
        CallTermination::LocalOnly(e) => log::warn!("hangup not confirmed by peer: {e}"),
        CallTermination::PartlyNotified { unconfirmed, .. } => {
            log::warn!("hangup unconfirmed on {unconfirmed} device(s)")
        }
        _ => {}
    }
}

#[cfg(test)]
mod sim;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transcript_fragments_merge_by_speaker() {
        let mut r = CallRecord::new("inbound", "");
        r.append("owner", "Hey, ");
        r.append("owner", "what's up?");
        r.append("assistant", "Not much.");
        r.append("owner", "Ok");
        assert_eq!(r.transcript.len(), 3);
        assert_eq!(
            r.transcript_text(),
            "owner: Hey, what's up?\nassistant: Not much.\nowner: Ok"
        );
    }

    #[test]
    fn detects_a_trailing_question() {
        let mut r = CallRecord::new("outbound", "");
        assert!(!r.ends_with_question());
        r.append("assistant", "Sure. Is that better? ");
        assert!(r.ends_with_question());
        r.append("owner", "Yes.");
        assert!(!r.ends_with_question(), "the owner answered");
        r.append("assistant", "Great, talk soon!");
        assert!(!r.ends_with_question());
    }

    #[test]
    fn one_call_at_a_time() {
        let calls = Calls::default();
        let first = calls.begin("outbound", "a").unwrap();
        assert!(calls.begin("inbound", "b").is_err());
        calls.finish(&first.id, Status::Completed, "done");
        assert_eq!(calls.get(&first.id).unwrap().status, Status::Completed);
        assert!(calls.active().is_none());
        assert!(calls.begin("inbound", "b").is_ok());
        assert_eq!(calls.recent(1)[0].direction, "inbound");
    }

    #[test]
    fn ring_signals_reach_a_late_subscriber_only_while_ringing() {
        let signals = RingSignals::default();
        signals.send("C1", Signal::Accepted);
        assert!(
            signals.subscribe("C1").is_none(),
            "nothing is collected before a call rings"
        );

        signals.start();
        signals.send("C1", Signal::Accepted); // the accept can beat start() returning the id
        assert_eq!(*signals.subscribe("C1").unwrap().borrow(), Signal::Accepted);
        assert_eq!(*signals.subscribe("C2").unwrap().borrow(), Signal::Pending);

        signals.stop();
        assert!(signals.subscribe("C1").is_none());
    }

    #[test]
    fn persona_replaces_the_default_identity() {
        let plain = system_instruction("Hermes", "", "", "tools", "ctx");
        assert!(plain.starts_with("You are Hermes, the owner's personal assistant."));
        let briefed =
            system_instruction("Hermes", "You are Nova.", "Speak Spanish.", "tools", "ctx");
        assert!(briefed.contains("You are Nova."));
        assert!(briefed.contains("Speak Spanish."));
        assert!(!briefed.contains("You are Hermes"));
        assert!(briefed.ends_with("ctx"));
    }

    #[test]
    fn ending_waits_for_the_goodbye_to_play_then_a_quiet_window() {
        let t0 = Instant::now();
        let at = |ms| t0 + Duration::from_millis(ms);
        let mut e = Ending::new(Some(Duration::from_secs(30)), Duration::from_secs(3));
        assert_eq!(e.check(t0, false, false), None);

        e.request();
        assert_eq!(
            e.check(at(1_000), true, false),
            None,
            "goodbye still playing"
        );
        assert_eq!(
            e.check(at(2_000), false, false),
            None,
            "quiet window starts"
        );
        assert_eq!(e.check(at(4_900), false, false), None);
        assert_eq!(
            e.check(at(5_000), false, false),
            Some("assistant ended the call")
        );
    }

    #[test]
    fn owner_speech_calls_the_hangup_off() {
        let t0 = Instant::now();
        let at = |ms| t0 + Duration::from_millis(ms);
        let mut e = Ending::new(Some(Duration::from_secs(30)), Duration::from_secs(3));
        assert!(!e.owner_spoke(t0), "nothing to call off before end_call");

        e.request();
        assert_eq!(e.check(at(1_000), false, false), None);
        assert!(
            e.owner_spoke(at(2_000)),
            "\"I have another question\", in any language"
        );
        assert_eq!(e.check(at(20_000), false, false), None, "call stays up");
    }

    #[test]
    fn pacing_makes_the_model_speak_between_tools_and_before_hanging_up() {
        let mut pacing = Pacing::new(5);
        for _ in 0..5 {
            assert!(pacing.allow_tool());
        }
        assert!(!pacing.allow_tool(), "too many tools in silence");
        pacing.result_sent();
        assert!(pacing.owes_report, "results not told to the owner yet");
        pacing.spoke();
        assert!(pacing.allow_tool() && !pacing.owes_report);
    }

    #[test]
    fn silence_ends_an_unclosed_call_unless_hermes_is_working() {
        let t0 = Instant::now();
        let at = |s| t0 + Duration::from_secs(s);
        let mut e = Ending::new(Some(Duration::from_secs(30)), Duration::from_secs(3));
        e.owner_spoke(t0);
        assert_eq!(e.check(at(29), false, false), None);
        assert_eq!(e.check(at(31), false, true), None, "waiting on ask_hermes");
        assert_eq!(
            e.check(at(31), false, false),
            Some("hung up after a long silence")
        );
        assert_eq!(
            e.check(at(32), true, false),
            None,
            "model speech is activity"
        );
        assert_eq!(e.check(at(61), false, false), None);
        assert_eq!(
            e.check(at(62), false, false),
            Some("hung up after a long silence")
        );
    }

    #[test]
    fn disabled_silence_timer_allows_long_silence_but_keeps_goodbye() {
        let t0 = Instant::now();
        let after_silence = t0 + Duration::from_secs(181);
        let grace = Duration::from_secs(3);
        let mut ending = Ending::new(None, grace);
        ending.owner_spoke(t0);
        assert_eq!(ending.check(after_silence, false, false), None);
        ending.request();
        assert_eq!(ending.check(after_silence, false, false), None);
        assert_eq!(
            ending.check(after_silence + grace, false, false),
            Some("assistant ended the call")
        );
    }

    #[test]
    fn truncates_on_char_boundary() {
        assert_eq!(truncate("héllo", 2), "hé…");
        assert_eq!(truncate("hi", 5), "hi");
    }
}
