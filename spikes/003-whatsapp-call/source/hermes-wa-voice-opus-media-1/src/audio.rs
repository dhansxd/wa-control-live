//! Audio plumbing between a WhatsApp call and Gemini Live.
//!
//! Call audio (whatsapp-rust) is 16 kHz mono i16: we push 960-sample (60 ms) frames and receive
//! 320-sample (20 ms) frames. Gemini Live takes 16 kHz s16le and returns 24 kHz s16le.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use std::path::Path;
use std::os::unix::fs::PermissionsExt;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

pub const FRAME_SAMPLES: usize = 960;
const PREFILL_SAMPLES: usize = FRAME_SAMPLES * 2;
const QUIET_FRAME_RMS: i64 = 100;
// Leave one 60 ms pacing interval for the eligibility check to run before the 180 ms bound.
const FULL_TAIL_WAIT: Duration = Duration::from_millis(60);
const TAIL_WAIT: Duration = Duration::from_millis(120);
const MAX_TAIL_LATENCY: Duration = Duration::from_millis(180);

pub fn i16_to_le_bytes(samples: &[i16]) -> Vec<u8> {
    samples.iter().flat_map(|s| s.to_le_bytes()).collect()
}

pub fn le_bytes_to_i16(bytes: &[u8]) -> Vec<i16> {
    bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|b| i16::from_le_bytes(*b))
        .collect()
}

const HALF_TAPS: usize = 16;

/// Streaming 24 kHz -> 16 kHz converter with a windowed-sinc low-pass.
///
/// Output sample n sits at input position 1.5n, so only two filter phases exist (fraction 0 and
/// 0.5) and both are precomputed.
pub struct Downsampler {
    buf: Vec<f32>,
    pos: f64, // input index of the next output sample, relative to buf
    phases: [[f32; 2 * HALF_TAPS]; 2],
}

impl Downsampler {
    pub fn new() -> Self {
        let cutoff = 7400.0 / 24000.0; // just under the 8 kHz Nyquist of the output
        let mut phases = [[0f32; 2 * HALF_TAPS]; 2];
        for (phase, frac) in [0.0f64, 0.5].into_iter().enumerate() {
            let mut w = [0f64; 2 * HALF_TAPS];
            for (k, wk) in w.iter_mut().enumerate() {
                let x = (k as f64 - HALF_TAPS as f64 + 1.0) - frac; // offset from the output position
                let sinc = if x == 0.0 {
                    2.0 * cutoff
                } else {
                    (2.0 * std::f64::consts::PI * cutoff * x).sin() / (std::f64::consts::PI * x)
                };
                let hann = 0.5 + 0.5 * (std::f64::consts::PI * x / HALF_TAPS as f64).cos();
                *wk = sinc * hann;
            }
            let sum: f64 = w.iter().sum();
            for (tap, wk) in phases[phase].iter_mut().zip(w) {
                *tap = (wk / sum) as f32; // unity DC gain
            }
        }
        Self {
            buf: vec![0.0; HALF_TAPS],
            pos: HALF_TAPS as f64,
            phases,
        }
    }

    pub fn process(&mut self, input: &[i16]) -> Vec<i16> {
        self.buf.extend(input.iter().map(|&s| s as f32));
        let mut out = Vec::with_capacity(input.len() * 2 / 3 + 1);
        loop {
            let base = self.pos as usize;
            if base + HALF_TAPS >= self.buf.len() {
                break;
            }
            let taps = &self.phases[usize::from(self.pos.fract() > 0.0)];
            let start = base + 1 - HALF_TAPS;
            let acc: f32 = self.buf[start..start + 2 * HALF_TAPS]
                .iter()
                .zip(taps)
                .map(|(x, h)| x * h)
                .sum();
            out.push(acc.round().clamp(i16::MIN as f32, i16::MAX as f32) as i16);
            self.pos += 1.5;
        }
        let drop = (self.pos as usize).saturating_sub(HALF_TAPS);
        if drop > 0 {
            self.buf.drain(..drop);
            self.pos -= drop as f64;
        }
        out
    }
}

/// Model speech waiting to be played into the call, drained one 60 ms frame per tick.
#[derive(Default)]
pub struct Playout {
    state: Mutex<PlayoutState>,
}

#[derive(Default)]
struct PlayoutState {
    queue: VecDeque<i16>,
    playing: bool,
    has_played: bool,
    last_input_at: Option<Instant>,
    max_queued_samples: usize,
    frames: u64,
    empty_frames: u64,
    partial_frames: u64,
    padded_samples: u64,
    partial_padded_samples: u64,
    prefill_padded_samples: u64,
    quiet_refill_padded_samples: u64,
    rebuffer_padded_samples: u64,
    idle_padded_samples: u64,
    final_tail_padded_samples: u64,
    cleared_samples: u64,
    pacer_ticks: u64,
    pacer_max_lateness_us: u128,
    mic_send_wait_us: u128,
    mic_send_max_wait_us: u128,
}

#[derive(Clone, Copy)]
pub struct DrainTrace {
    pub source_start_sample: u64,
    pub source_samples: usize,
    pub padded_samples: usize,
    pub padding_reason: &'static str,
    pub queue_before: usize,
    pub queue_after: usize,
}

impl Playout {
    pub fn push(&self, samples: &[i16]) {
        self.push_at(samples, Instant::now());
    }

    fn push_at(&self, samples: &[i16], now: Instant) {
        let mut state = self.state.lock().unwrap();
        state.queue.extend(samples);
        if !samples.is_empty() {
            state.last_input_at = Some(now);
        }
        state.max_queued_samples = state.max_queued_samples.max(state.queue.len());
    }

    /// Drop queued speech, used when the caller barges in.
    pub fn clear(&self) -> (u64, usize) {
        let mut state = self.state.lock().unwrap();
        let offset = state.frames * FRAME_SAMPLES as u64 - state.padded_samples + state.cleared_samples;
        let samples = state.queue.len();
        state.cleared_samples += samples as u64;
        state.queue.clear();
        state.playing = false;
        state.has_played = false;
        state.last_input_at = None;
        (offset, samples)
    }

    pub fn buffered(&self) -> usize {
        self.state.lock().unwrap().queue.len()
    }

    /// Keep partial PCM queued while priming, and bound the wait for a final short tail.
    #[cfg(test)]
    fn next_frame_at(&self, now: Instant) -> Vec<i16> {
        self.drain_at(now).0
    }

    pub fn next_frame_with_trace(&self) -> (Vec<i16>, DrainTrace) {
        self.drain_at(Instant::now())
    }

    fn drain_at(&self, now: Instant) -> (Vec<i16>, DrainTrace) {
        let mut state = self.state.lock().unwrap();
        let buffered = state.queue.len();
        let offset = state.frames * FRAME_SAMPLES as u64 - state.padded_samples + state.cleared_samples;
        let quiet_for = state.last_input_at.map(|last| now.saturating_duration_since(last))
            .unwrap_or_default();
        if buffered < FRAME_SAMPLES {
            state.playing = false;
        }
        // A full tail gets one tick's head start so its partial remainder meets the same bound.
        if buffered >= PREFILL_SAMPLES
            || (state.has_played && buffered >= FRAME_SAMPLES)
            || (buffered >= FRAME_SAMPLES && quiet_for >= FULL_TAIL_WAIT)
            || (buffered > 0 && quiet_for >= TAIL_WAIT)
        {
            state.playing = true;
        }
        // Refill the reserve while the next frame is quiet and PCM is still arriving.
        let quiet_refill = state.playing
            && buffered >= FRAME_SAMPLES
            && buffered < PREFILL_SAMPLES + FRAME_SAMPLES
            && quiet_for < FULL_TAIL_WAIT
            && state.queue.iter().take(FRAME_SAMPLES)
                .map(|&sample| i64::from(sample).pow(2)).sum::<i64>()
                < FRAME_SAMPLES as i64 * QUIET_FRAME_RMS.pow(2);
        let n = if state.playing && !quiet_refill { buffered.min(FRAME_SAMPLES) } else { 0 };
        let padding_reason = if n == FRAME_SAMPLES { "none" }
            else if n > 0 { "final_tail" }
            else if quiet_refill { "quiet_refill" }
            else if buffered == 0 { "idle" }
            else if state.has_played { "rebuffer" }
            else { "prefill" };
        state.frames += 1;
        state.empty_frames += u64::from(n == 0);
        state.partial_frames += u64::from(n > 0 && n < FRAME_SAMPLES);
        state.padded_samples += (FRAME_SAMPLES-n) as u64;
        if n == 0 {
            if quiet_refill {
                state.quiet_refill_padded_samples += FRAME_SAMPLES as u64;
            } else if buffered == 0 {
                state.idle_padded_samples += FRAME_SAMPLES as u64;
            } else if state.has_played {
                state.rebuffer_padded_samples += FRAME_SAMPLES as u64;
            } else {
                state.prefill_padded_samples += FRAME_SAMPLES as u64;
            }
        }
        if n > 0 {
            state.has_played = true;
            state.partial_padded_samples += (FRAME_SAMPLES-n) as u64;
            state.final_tail_padded_samples += (FRAME_SAMPLES-n) as u64;
        }
        let mut frame: Vec<i16> = state.queue.drain(..n).collect();
        frame.resize(FRAME_SAMPLES, 0);
        (frame, DrainTrace {
            source_start_sample: offset, source_samples: n, padded_samples: FRAME_SAMPLES-n,
            padding_reason, queue_before: buffered, queue_after: buffered-n,
        })
    }

    pub fn observe_pacer(&self, lateness: Duration, send_wait: Duration) {
        let mut state = self.state.lock().unwrap();
        state.pacer_ticks += 1;
        state.pacer_max_lateness_us = state.pacer_max_lateness_us.max(lateness.as_micros());
        state.mic_send_wait_us += send_wait.as_micros();
        state.mic_send_max_wait_us = state.mic_send_max_wait_us.max(send_wait.as_micros());
    }

    pub fn snapshot(&self) -> Value {
        let state = self.state.lock().unwrap();
        json!({
            "queued_samples": state.queue.len(),
            "max_queued_samples": state.max_queued_samples,
            "frames": state.frames,
            "empty_frames": state.empty_frames,
            "partial_frames": state.partial_frames,
            "padded_samples": state.padded_samples,
            "partial_padded_samples": state.partial_padded_samples,
            "prefill_padded_samples": state.prefill_padded_samples,
            "quiet_refill_padded_samples": state.quiet_refill_padded_samples,
            "rebuffer_padded_samples": state.rebuffer_padded_samples,
            "idle_padded_samples": state.idle_padded_samples,
            "final_tail_padded_samples": state.final_tail_padded_samples,
            "prefill_target_samples": PREFILL_SAMPLES,
            "tail_wait_ms": TAIL_WAIT.as_millis(),
            "max_tail_latency_ms": MAX_TAIL_LATENCY.as_millis(),
            "cleared_samples": state.cleared_samples,
            "pacer_ticks": state.pacer_ticks,
            "pacer_max_lateness_us": state.pacer_max_lateness_us,
            "mic_send_wait_us": state.mic_send_wait_us,
            "mic_send_max_wait_us": state.mic_send_max_wait_us,
        })
    }
}

/// Optional bounded recording of source arrivals and PCM accepted by the microphone channel.
pub struct OutputCapture {
    wav: Option<WavWriter>,
    events: BufWriter<File>,
    enabled: bool,
    records: u64,
    provider_samples: u64,
    queued_samples: u64,
    sent_frames: u64,
}

impl OutputCapture {
    pub fn create(dir: &Path, id: &str) -> io::Result<Self> {
        std::fs::create_dir_all(dir)?;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
        let path = dir.join(format!("{id}-output.wav"));
        let wav = WavWriter::create(&path, 16_000)?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        let path = dir.join(format!("{id}-output.jsonl"));
        let events = File::create(&path)?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        Ok(Self {
            wav: Some(wav), events: BufWriter::new(events), enabled: true, records: 0,
            provider_samples: 0, queued_samples: 0, sent_frames: 0,
        })
    }

    fn disable(&mut self, reason: &str) {
        self.enabled = false;
        log::warn!("output capture disabled: {reason}");
    }

    fn within_limit(&mut self, at_ms: u128) -> bool {
        if self.enabled && (at_ms > 180_000 || self.records >= 18_000) {
            self.disable("180 second or record limit reached");
        }
        self.enabled
    }

    fn write_event(&mut self, event: Value) {
        self.records += 1;
        if let Err(error) = serde_json::to_writer(&mut self.events, &event)
            .map_err(io::Error::other).and_then(|_| self.events.write_all(b"\n"))
        {
            self.disable(&error.to_string());
        }
    }

    pub fn provider(&mut self, at_ms: u128, raw_samples: usize, queued_samples: usize) {
        if !self.within_limit(at_ms) { return; }
        if self.provider_samples + raw_samples as u64 > 180 * 24_000 {
            return self.disable("provider sample limit reached");
        }
        self.write_event(json!({
            "kind": "provider", "at_ms": at_ms,
            "raw_sample_rate_hz": 24000, "raw_sample_offset": self.provider_samples,
            "raw_samples": raw_samples, "queued_sample_rate_hz": 16000,
            "queued_sample_offset": self.queued_samples, "queued_samples": queued_samples,
        }));
        self.provider_samples += raw_samples as u64;
        self.queued_samples += queued_samples as u64;
    }

    pub fn recovery_audio(&mut self, at_ms: u128, samples: usize) {
        if !self.within_limit(at_ms) { return; }
        self.write_event(json!({
            "kind": "recovery_audio", "at_ms": at_ms,
            "queued_sample_rate_hz": 16000,
            "queued_sample_offset": self.queued_samples, "queued_samples": samples,
        }));
        self.queued_samples += samples as u64;
    }

    pub fn sent(&mut self, at_ms: u128, pcm: &[i16], drain: DrainTrace,
                lateness: Duration, send_wait: Duration) {
        if !self.within_limit(at_ms) { return; }
        if self.sent_frames >= 3000 { return self.disable("output sample limit reached"); }
        if let Err(error) = self.wav.as_mut().unwrap().write(pcm) {
            return self.disable(&error.to_string());
        }
        self.write_event(json!({
            "kind": "sent", "at_ms": at_ms, "sample_rate_hz": 16000,
            "frame_index": self.sent_frames, "output_sample_offset": self.sent_frames * 960,
            "source_start_sample": drain.source_start_sample, "source_samples": drain.source_samples,
            "padded_samples": drain.padded_samples, "padding_reason": drain.padding_reason,
            "queue_before": drain.queue_before, "queue_after": drain.queue_after,
            "lateness_us": lateness.as_micros(), "send_wait_us": send_wait.as_micros(),
        }));
        self.sent_frames += 1;
    }

    pub fn cleared(&mut self, at_ms: u128, offset: u64, samples: usize) {
        if self.within_limit(at_ms) {
            self.write_event(json!({"kind":"clear", "at_ms":at_ms,
                "source_start_sample":offset, "source_samples":samples}));
        }
    }

    pub fn finish(&mut self) {
        if let Some(wav) = self.wav.take() {
            if let Err(error) = wav.finish() { self.disable(&error.to_string()); }
            self.write_event(json!({"kind":"finished", "complete":self.enabled,
                "provider_samples":self.provider_samples, "queued_samples":self.queued_samples,
                "sent_frames":self.sent_frames}));
            if let Err(error) = self.events.flush() { self.disable(&error.to_string()); }
            self.enabled = false;
        }
    }
}

/// Writes mono 16-bit PCM to a WAV file as it arrives; `finish` fills in the sizes.
pub struct WavWriter {
    file: BufWriter<File>,
    bytes: u32,
}

impl WavWriter {
    pub fn create(path: &Path, rate: u32) -> io::Result<Self> {
        let mut file = BufWriter::new(File::create(path)?);
        file.write_all(b"RIFF\0\0\0\0WAVEfmt ")?;
        for (v, n) in [
            (16, 4),
            (1, 2),
            (1, 2),
            (rate, 4),
            (rate * 2, 4),
            (2, 2),
            (16, 2),
        ] {
            file.write_all(&u32::to_le_bytes(v)[..n])?;
        }
        file.write_all(b"data\0\0\0\0")?;
        Ok(Self { file, bytes: 0 })
    }

    pub fn write(&mut self, pcm: &[i16]) -> io::Result<()> {
        self.bytes += (pcm.len() * 2) as u32;
        self.file.write_all(&i16_to_le_bytes(pcm))
    }

    pub fn finish(mut self) -> io::Result<()> {
        self.file.seek(SeekFrom::Start(4))?;
        self.file.write_all(&(36 + self.bytes).to_le_bytes())?;
        self.file.seek(SeekFrom::Start(40))?;
        self.file.write_all(&self.bytes.to_le_bytes())?;
        self.file.flush()
    }
}

/// Reads a mono 16-bit PCM WAV written by `WavWriter` (or any plain 44-byte-header WAV).
#[cfg(test)]
pub fn read_wav(path: &Path) -> io::Result<(u32, Vec<i16>)> {
    let bytes = std::fs::read(path)?;
    let rate = u32::from_le_bytes(bytes[24..28].try_into().unwrap());
    Ok((rate, le_bytes_to_i16(&bytes[44..])))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(freq: f64, rate: f64, n: usize) -> Vec<i16> {
        (0..n)
            .map(|i| (16384.0 * (2.0 * std::f64::consts::PI * freq * i as f64 / rate).sin()) as i16)
            .collect()
    }

    fn rms(s: &[i16]) -> f64 {
        (s.iter().map(|&v| (v as f64).powi(2)).sum::<f64>() / s.len() as f64).sqrt()
    }

    #[test]
    fn wav_round_trip() {
        let path = std::env::temp_dir().join(format!("hwv-{}.wav", std::process::id()));
        let mut w = WavWriter::create(&path, 16_000).unwrap();
        w.write(&[1, -2, 3]).unwrap();
        w.write(&[4]).unwrap();
        w.finish().unwrap();
        assert_eq!(read_wav(&path).unwrap(), (16_000, vec![1, -2, 3, 4]));
        assert_eq!(std::fs::metadata(&path).unwrap().len(), 44 + 8);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn output_capture_preserves_accepted_pcm_and_atomic_source_offsets_across_clear() {
        let dir = std::env::temp_dir().join(format!("hwv-output-{}", rand::random::<u32>()));
        let mut capture = OutputCapture::create(&dir, "test").unwrap();
        let playout = Playout::default();
        let now = Instant::now();
        capture.provider(0, 2880, 1920);
        playout.push_at(&[700; 1920], now);
        let (first, drain) = playout.drain_at(now);
        capture.sent(1, &first, drain, Duration::from_micros(8), Duration::from_micros(3));
        let (offset, samples) = playout.clear();
        capture.cleared(20, offset, samples);
        capture.recovery_audio(20, 160);
        playout.push_at(&[6; 160], now + Duration::from_millis(20));
        capture.provider(20, 480, 320);
        playout.push_at(&[8; 320], now + Duration::from_millis(20));
        let (tail, drain) = playout.drain_at(now + Duration::from_millis(200));
        capture.sent(201, &tail, drain, Duration::ZERO, Duration::ZERO);
        capture.finish();
        capture.finish(); // Ending twice must not rewrite headers or duplicate the final row.
        let wav_path = dir.join("test-output.wav");
        let (rate, pcm) = read_wav(&wav_path).unwrap();
        let mut expected = vec![700; 960];
        expected.extend([6; 160]);
        expected.extend([8; 320]);
        expected.extend([0; 480]);
        assert_eq!((rate, pcm), (16_000, expected));
        let wav = std::fs::read(&wav_path).unwrap();
        assert_eq!(u32::from_le_bytes(wav[4..8].try_into().unwrap()) as usize, wav.len() - 8);
        assert_eq!(u32::from_le_bytes(wav[40..44].try_into().unwrap()) as usize, wav.len() - 44);
        let trace_path = dir.join("test-output.jsonl");
        let rows: Vec<Value> = std::fs::read_to_string(&trace_path).unwrap().lines()
            .map(|line| serde_json::from_str(line).unwrap()).collect();
        assert_eq!(rows.len(), 7);
        assert_eq!(rows[0]["raw_sample_offset"], 0);
        assert_eq!(rows[0]["raw_sample_rate_hz"], 24000);
        assert_eq!(rows[0]["queued_sample_rate_hz"], 16000);
        assert_eq!(rows[1]["source_start_sample"], 0);
        assert_eq!(rows[1]["source_samples"], 960);
        assert_eq!(rows[1]["padded_samples"], 0);
        assert_eq!(rows[1]["queue_before"], 1920);
        assert_eq!(rows[1]["queue_after"], 960);
        assert_eq!(rows[1]["lateness_us"], 8);
        assert_eq!(rows[1]["send_wait_us"], 3);
        assert_eq!(rows[2], json!({"kind":"clear", "at_ms":20,
            "source_start_sample":960, "source_samples":960}));
        assert_eq!(rows[3], json!({"kind":"recovery_audio", "at_ms":20,
            "queued_sample_rate_hz":16000, "queued_sample_offset":1920, "queued_samples":160}));
        assert_eq!(rows[4]["raw_sample_offset"], 2880);
        assert_eq!(rows[4]["queued_sample_offset"], 2080);
        assert_eq!(rows[5]["frame_index"], 1);
        assert_eq!(rows[5]["output_sample_offset"], 960);
        assert_eq!(rows[5]["source_start_sample"], 1920);
        assert_eq!(rows[5]["source_samples"], 480);
        assert_eq!(rows[5]["padded_samples"], 480);
        assert_eq!(rows[5]["padding_reason"], "final_tail");
        assert_eq!(rows[6], json!({"kind":"finished", "complete":true,
            "provider_samples":3360, "queued_samples":2400, "sent_frames":2}));
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        for path in [wav_path, trace_path] {
            assert_eq!(std::fs::metadata(path).unwrap().permissions().mode() & 0o777, 0o600);
        }
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn output_capture_stops_at_the_call_time_bound() {
        let dir = std::env::temp_dir().join(format!("hwv-output-limit-{}", rand::random::<u32>()));
        let mut capture = OutputCapture::create(&dir, "test").unwrap();
        capture.provider(180_001, 320, 200);
        let (frame, drain) = Playout::default().next_frame_with_trace();
        capture.sent(180_002, &frame, drain, Duration::ZERO, Duration::ZERO);
        capture.finish();
        assert_eq!(read_wav(&dir.join("test-output.wav")).unwrap(), (16_000, vec![]));
        let row: Value = serde_json::from_str(
            std::fs::read_to_string(dir.join("test-output.jsonl")).unwrap().trim()).unwrap();
        assert_eq!(row, json!({"kind":"finished", "complete":false,
            "provider_samples":0, "queued_samples":0, "sent_frames":0}));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn output_capture_writer_failure_disables_capture_without_losing_playout() {
        let dir = std::env::temp_dir().join(format!("hwv-output-error-{}", rand::random::<u32>()));
        let mut capture = OutputCapture::create(&dir, "test").unwrap();
        // A read-only descriptor fails on the real first write, not a simulated return value.
        capture.events = BufWriter::with_capacity(1, File::open(dir.join("test-output.jsonl")).unwrap());
        capture.provider(0, 2880, 1920);
        assert!(!capture.enabled);
        let playout = Playout::default();
        playout.push(&[700; 1920]);
        let (frame, drain) = playout.next_frame_with_trace();
        capture.sent(1, &frame, drain, Duration::ZERO, Duration::ZERO);
        assert_eq!(frame, vec![700; FRAME_SAMPLES]);
        assert_eq!(playout.buffered(), FRAME_SAMPLES);
        capture.finish();
        assert_eq!(read_wav(&dir.join("test-output.wav")).unwrap(), (16_000, vec![]));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn pcm_bytes_round_trip() {
        let s = [0i16, 1, -1, i16::MAX, i16::MIN, 12345];
        assert_eq!(le_bytes_to_i16(&i16_to_le_bytes(&s)), s);
    }

    #[test]
    fn downsampler_ratio_across_uneven_chunks() {
        let mut d = Downsampler::new();
        let input = sine(440.0, 24000.0, 24000);
        let out: Vec<i16> = input.chunks(731).flat_map(|c| d.process(c)).collect();
        assert!((15980..=16000).contains(&out.len()), "got {}", out.len());
    }

    #[test]
    fn downsampler_keeps_passband_and_cuts_aliases() {
        let pass = Downsampler::new().process(&sine(1000.0, 24000.0, 24000));
        let r = rms(&pass[1000..]);
        assert!((10900.0..12300.0).contains(&r), "1 kHz rms {r}"); // 16384/sqrt(2) ≈ 11585
        // 11 kHz would alias to 5 kHz at 16 kHz without the low-pass.
        let stop = Downsampler::new().process(&sine(11000.0, 24000.0, 24000));
        let r = rms(&stop[1000..]);
        assert!(r < 400.0, "11 kHz leaked with rms {r}");
    }

    #[test]
    fn playout_pads_and_clears() {
        let p = Playout::default();
        let start = Instant::now();
        assert_eq!(p.next_frame_at(start), vec![0; FRAME_SAMPLES]);
        p.push_at(&vec![1; 1000], start);
        assert_eq!(p.buffered(), 1000);
        assert_eq!(p.next_frame_at(start + FULL_TAIL_WAIT), vec![1; FRAME_SAMPLES]);
        let tail = p.next_frame_at(start + TAIL_WAIT);
        assert_eq!(tail.len(), FRAME_SAMPLES);
        assert_eq!(&tail[..40], &[1; 40]);
        assert!(tail[40..].iter().all(|&s| s == 0));
        p.push(&vec![1; 5000]);
        p.clear();
        assert_eq!(p.buffered(), 0);
    }

    #[test]
    fn playout_prefills_and_rebuffers_without_losing_pcm() {
        let p = Playout::default();
        let start = Instant::now();
        let at = |ms| start + Duration::from_millis(ms);
        let pcm: Vec<i16> = (0..3840).map(|i| ((i % 31) as i16 - 15) * 100).collect();
        p.push_at(&pcm[..320], at(0));
        assert_eq!(p.next_frame_at(at(0)), vec![0; FRAME_SAMPLES]);
        assert_eq!(p.buffered(), 320, "prefill must retain even the zero samples");
        p.push_at(&pcm[320..960], at(20));
        assert_eq!(p.next_frame_at(at(60)), vec![0; FRAME_SAMPLES]);
        assert_eq!(p.buffered(), 960);
        p.push_at(&pcm[960..1920], at(70));
        assert_eq!(p.next_frame_at(at(120)), pcm[..960]);
        assert_eq!(p.next_frame_at(at(180)), pcm[960..1920]);
        p.push_at(&pcm[1920..2240], at(190));
        assert_eq!(p.next_frame_at(at(240)), vec![0; FRAME_SAMPLES]);
        assert_eq!(p.buffered(), 320, "underrun must retain the incomplete frame");
        p.push_at(&pcm[2240..], at(250));
        assert_eq!(p.next_frame_at(at(300)), pcm[1920..2880]);
        assert_eq!(p.next_frame_at(at(360)), pcm[2880..]);
        assert_eq!(p.buffered(), 0);
        let stats = p.snapshot();
        assert_eq!(stats["partial_frames"], 0);
        assert_eq!(stats["prefill_padded_samples"], 1920);
        assert_eq!(stats["rebuffer_padded_samples"], 960);
        assert_eq!(stats["final_tail_padded_samples"], 0);
    }

    #[test]
    fn playout_diagnostics_measure_padding_queue_and_pacer_without_changing_pcm() {
        let p = Playout::default();
        let start = Instant::now();
        let at = |ms| start + Duration::from_millis(ms);
        assert_eq!(p.next_frame_at(at(0)), vec![0; FRAME_SAMPLES]);
        p.push_at(&[7; 320], at(0));
        let partial = p.next_frame_at(at(180));
        assert_eq!(&partial[..320], &[7; 320]);
        assert!(partial[320..].iter().all(|&sample| sample == 0));
        p.push_at(&[3; FRAME_SAMPLES], at(200));
        assert_eq!(p.next_frame_at(at(320)), vec![3; FRAME_SAMPLES]);
        p.push(&[9; 20]);
        p.clear();
        p.observe_pacer(Duration::from_millis(12), Duration::from_millis(80));
        p.observe_pacer(Duration::from_millis(2), Duration::from_millis(3));
        let stats = p.snapshot();
        assert_eq!(stats["queued_samples"], 0);
        assert_eq!(stats["max_queued_samples"], FRAME_SAMPLES);
        assert_eq!(stats["frames"], 3);
        assert_eq!(stats["empty_frames"], 1);
        assert_eq!(stats["partial_frames"], 1);
        assert_eq!(stats["padded_samples"], 1600);
        assert_eq!(stats["partial_padded_samples"], 640);
        assert_eq!(stats["final_tail_padded_samples"], 640);
        assert_eq!(stats["idle_padded_samples"], FRAME_SAMPLES);
        assert_eq!(stats["cleared_samples"], 20);
        assert_eq!(stats["pacer_ticks"], 2);
        assert_eq!(stats["pacer_max_lateness_us"], 12000);
        assert_eq!(stats["mic_send_wait_us"], 83000);
        assert_eq!(stats["mic_send_max_wait_us"], 80000);
    }

    #[test]
    fn short_tails_flush_by_three_ticks_without_losing_or_repeating_pcm() {
        for length in [1, 320, 959, 960, 1280, 1919] {
            let p = Playout::default();
            let start = Instant::now();
            let pcm: Vec<i16> = (0..length).map(|i| (i % 31) as i16 - 15).collect();
            p.push_at(&pcm, start);
            assert_eq!(p.next_frame_at(start), vec![0; FRAME_SAMPLES]);
            assert_eq!(p.next_frame_at(start + Duration::from_millis(59)), vec![0; FRAME_SAMPLES]);
            let full = p.next_frame_at(start + FULL_TAIL_WAIT);
            let consumed = if length >= FRAME_SAMPLES { FRAME_SAMPLES } else { 0 };
            assert_eq!(&full[..consumed], &pcm[..consumed]);
            assert!(full[consumed..].iter().all(|&sample| sample == 0));
            let tail = p.next_frame_at(start + TAIL_WAIT);
            let remaining = length-consumed;
            assert_eq!(&tail[..remaining], &pcm[consumed..]);
            assert!(tail[remaining..].iter().all(|&sample| sample == 0));
            assert_eq!(p.buffered(), 0);
            assert_eq!(p.next_frame_at(start + Duration::from_millis(240)), vec![0; FRAME_SAMPLES]);
            let stats = p.snapshot();
            let played = stats["frames"].as_u64().unwrap() * FRAME_SAMPLES as u64
                - stats["padded_samples"].as_u64().unwrap();
            assert_eq!(played, length as u64, "every input sample must play once");
        }
    }

    #[test]
    fn new_pcm_restarts_tail_deadline_and_clear_starts_a_fresh_prefill() {
        let p = Playout::default();
        let start = Instant::now();
        let at = |ms| start + Duration::from_millis(ms);
        p.push_at(&[1; 320], at(0));
        assert_eq!(p.next_frame_at(at(60)), vec![0; FRAME_SAMPLES]);
        p.push_at(&[2; 320], at(100));
        assert_eq!(p.next_frame_at(at(180)), vec![0; FRAME_SAMPLES]);
        assert_eq!(p.next_frame_at(at(219)), vec![0; FRAME_SAMPLES]);
        let tail = p.next_frame_at(at(220));
        assert_eq!(&tail[..320], &[1; 320]);
        assert_eq!(&tail[320..640], &[2; 320]);
        assert!(tail[640..].iter().all(|&sample| sample == 0));
        p.push_at(&[300; 1920], at(400));
        assert_eq!(p.next_frame_at(at(400)), vec![300; FRAME_SAMPLES]);
        p.clear();
        p.push_at(&[4; 320], at(500));
        assert_eq!(p.next_frame_at(at(560)), vec![0; FRAME_SAMPLES]);
        let fresh = p.next_frame_at(at(680));
        assert_eq!(&fresh[..320], &[4; 320]);
        assert!(fresh[320..].iter().all(|&sample| sample == 0));
        assert_eq!(p.snapshot()["cleared_samples"], 960);
    }

    #[test]
    fn tail_deadline_holds_between_pacer_ticks() {
        for length in [320, 1280] {
            let p = Playout::default();
            let start = Instant::now();
            let pcm = vec![7; length];
            p.push_at(&pcm, start + Duration::from_millis(1));
            let mut played = Vec::new();
            for ms in [60, 120, 180] {
                let before = p.snapshot()["padded_samples"].as_u64().unwrap();
                let frame = p.next_frame_at(start + Duration::from_millis(ms));
                let padding = p.snapshot()["padded_samples"].as_u64().unwrap() - before;
                played.extend_from_slice(&frame[..FRAME_SAMPLES-padding as usize]);
            }
            assert_eq!(p.buffered(), 0, "last input arrived between pacing ticks");
            assert_eq!(played, pcm);
        }
    }

    #[test]
    fn playout_resumes_a_complete_frame_after_underrun_without_repriming() {
        let p = Playout::default();
        let start = Instant::now();
        let at = |ms| start + Duration::from_millis(ms);
        let pcm: Vec<i16> = (0..3200).map(|i| ((i % 53) as i16 + 1) * 100).collect();

        p.push_at(&pcm[..960], at(0));
        assert_eq!(p.next_frame_at(at(0)), vec![0; FRAME_SAMPLES]);
        assert_eq!(p.buffered(), 960, "initial playback still requires its prefill");
        p.push_at(&pcm[960..1920], at(20));
        let mut played = p.next_frame_at(at(60));
        assert_eq!(played, pcm[..960]);
        let second = p.next_frame_at(at(120));
        assert_eq!(second, pcm[960..1920]);
        played.extend(second);

        p.push_at(&pcm[1920..2560], at(160));
        assert_eq!(p.next_frame_at(at(180)), vec![0; FRAME_SAMPLES]);
        assert_eq!(p.buffered(), 640, "an incomplete frame must remain queued");
        p.push_at(&pcm[2560..2880], at(200));
        let resumed = p.next_frame_at(at(240));
        assert_eq!(resumed, pcm[1920..2880], "a complete available frame must resume on this tick");
        played.extend(resumed);
        assert_eq!(p.buffered(), 0);

        p.push_at(&pcm[2880..], at(260));
        for ms in [300, 360] {
            assert_eq!(p.next_frame_at(at(ms)), vec![0; FRAME_SAMPLES]);
            assert_eq!(p.buffered(), 320);
        }
        let tail = p.next_frame_at(at(420));
        assert_eq!(&tail[..320], &pcm[2880..]);
        assert!(tail[320..].iter().all(|&sample| sample == 0));
        assert!(at(420).duration_since(at(260)) <= MAX_TAIL_LATENCY);
        played.extend_from_slice(&tail[..320]);
        assert_eq!(played, pcm, "every original sample must play exactly once");
        assert_eq!(p.buffered(), 0);
        assert_eq!(p.next_frame_at(at(480)), vec![0; FRAME_SAMPLES]);
    }

    #[test]
    fn quiet_refill_holds_a_quiet_frame_and_drains_all_pcm_in_order() {
        let p = Playout::default();
        let start = Instant::now();
        let at = |ms| start + Duration::from_millis(ms);
        p.push_at(&[7; FRAME_SAMPLES], at(0));
        p.push_at(&[100; FRAME_SAMPLES], at(0));
        let (held, trace) = p.drain_at(at(0));
        assert_eq!(trace.padding_reason, "quiet_refill");
        assert_eq!(trace.source_samples, 0);
        assert!(held.iter().all(|&sample| sample == 0));
        assert_eq!(p.buffered(), PREFILL_SAMPLES);

        p.push_at(&[200; FRAME_SAMPLES], at(20));
        let (quiet, trace) = p.drain_at(at(60));
        assert_eq!(quiet, vec![7; FRAME_SAMPLES]);
        assert_eq!(trace.source_start_sample, 0);
        assert_eq!(trace.queue_after, PREFILL_SAMPLES);

        p.push_at(&[400; 320], at(90));
        assert_eq!(p.next_frame_at(at(120)), vec![100; FRAME_SAMPLES]);
        assert_eq!(p.next_frame_at(at(180)), vec![200; FRAME_SAMPLES]);
        let tail = p.next_frame_at(at(240));
        assert_eq!(&tail[..320], &[400; 320]);
        assert!(tail[320..].iter().all(|&sample| sample == 0));
        assert_eq!(p.buffered(), 0);
        let stats = p.snapshot();
        assert_eq!(stats["quiet_refill_padded_samples"], FRAME_SAMPLES);
        assert_eq!(stats["rebuffer_padded_samples"], 0);
        assert_eq!(stats["prefill_padded_samples"], 0);
        assert_eq!(stats["final_tail_padded_samples"], FRAME_SAMPLES - 320);
    }

    #[test]
    fn quiet_refill_releases_stopped_audio_and_clear_resets_priming() {
        let p = Playout::default();
        let start = Instant::now();
        let at = |ms| start + Duration::from_millis(ms);
        p.push_at(&[7; PREFILL_SAMPLES], at(0));
        assert_eq!(p.drain_at(at(0)).1.padding_reason, "quiet_refill");
        assert_eq!(p.clear(), (0, PREFILL_SAMPLES));

        p.push_at(&[11; 1280], at(61));
        assert_eq!(p.drain_at(at(120)).1.padding_reason, "prefill");
        let (full, trace) = p.drain_at(at(180));
        assert_eq!(full, vec![11; FRAME_SAMPLES]);
        assert_eq!(trace.source_start_sample, PREFILL_SAMPLES as u64);
        assert_eq!(trace.padding_reason, "none");
        let (tail, trace) = p.drain_at(at(240));
        assert_eq!(&tail[..320], &[11; 320]);
        assert!(tail[320..].iter().all(|&sample| sample == 0));
        assert_eq!(trace.padding_reason, "final_tail");
        assert_eq!(p.buffered(), 0);
        assert!(at(240).duration_since(at(61)) <= MAX_TAIL_LATENCY);
    }
}
