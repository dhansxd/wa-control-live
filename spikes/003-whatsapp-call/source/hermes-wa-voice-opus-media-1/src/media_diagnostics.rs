//! Read-only PCM/counter snapshots. They do not infer whether the owner spoke.
use serde_json::{Value, json};
use whatsapp_rust::wacore::voip_control::media_stats::CallMediaStats;

#[derive(Default)]
pub struct PcmDiagnostics {
    frames: u64,
    samples: u64,
    all_zero_frames: u64,
    nonzero_samples: u64,
    peak: u16,
}

impl PcmDiagnostics {
    pub fn observe(&mut self, pcm: &[i16]) {
        self.frames += 1;
        self.samples += pcm.len() as u64;
        let nonzero = pcm.iter().filter(|&&sample| sample != 0).count() as u64;
        self.nonzero_samples += nonzero;
        self.all_zero_frames += u64::from(nonzero == 0);
        self.peak = self.peak.max(pcm.iter().map(|s| s.unsigned_abs()).max().unwrap_or(0));
    }

    fn pcm_snapshot(&self) -> Value {
        json!({
            "frames": self.frames,
            "samples": self.samples,
            "all_zero_frames": self.all_zero_frames,
            "nonzero_samples": self.nonzero_samples,
            "peak": self.peak,
        })
    }

    pub fn snapshot(&self, at_ms: u128, native: CallMediaStats) -> Value {
        json!({
            "at_ms": at_ms,
            "pcm": self.pcm_snapshot(),
            "native": {
                "rtp_received": native.rtp_received,
                "rtp_payload_type_unexpected": native.rtp_payload_type_unexpected,
                "srtp_unprotect_failed": native.srtp_unprotect_failed,
                "sframe_decrypt_failed": native.sframe_decrypt_failed,
                "audio_frames_decoded": native.audio_frames_decoded,
                "audio_frames_delivered": native.audio_frames_delivered,
                "foreign_frames_decoded": native.foreign_frames_decoded,
                "audio_frames_concealed": native.audio_frames_concealed,
                "audio_frames_without_decoder": native.audio_frames_without_decoder,
                "mlow_off_point_dropped": native.mlow_off_point_dropped,
                "mlow_inactive_or_sid": native.mlow_inactive_or_sid,
                "playout_trimmed_samples": native.playout_trimmed_samples,
                "inbound_pipe_dropped": native.inbound_pipe_dropped,
                "audio_sink_dropped": native.audio_sink_dropped,
                "relay_packet_unclassified": native.relay_packet_unclassified,
                "forwarding_envelope_rejected": native.forwarding_envelope_rejected,
                "codec_switches": native.codec_switches,
            },
        })
    }
}

/// Provider PCM arriving before downsampling and WhatsApp playout. Energy is not speech detection.
#[derive(Default)]
pub struct OutputDiagnostics {
    pcm: PcmDiagnostics,
    first_at_ms: Option<u128>,
    first_nonzero_at_ms: Option<u128>,
    last_at_ms: Option<u128>,
    last_gap_ms: u128,
    max_gap_ms: u128,
    last_peak: u16,
    last_rms: f64,
}

impl OutputDiagnostics {
    pub fn observe(&mut self, pcm: &[i16], at_ms: u128) {
        self.pcm.observe(pcm);
        self.first_at_ms.get_or_insert(at_ms);
        if let Some(last) = self.last_at_ms {
            self.last_gap_ms = at_ms.saturating_sub(last);
            self.max_gap_ms = self.max_gap_ms.max(self.last_gap_ms);
        }
        self.last_at_ms = Some(at_ms);
        self.last_peak = pcm.iter().map(|sample| sample.unsigned_abs()).max().unwrap_or(0);
        self.last_rms = (pcm.iter().map(|&sample| f64::from(sample).powi(2)).sum::<f64>()
            / pcm.len().max(1) as f64).sqrt();
        if self.last_peak > 0 {
            self.first_nonzero_at_ms.get_or_insert(at_ms);
        }
    }

    pub fn snapshot(&self, playout: Value) -> Value {
        json!({
            "provider_sample_rate_hz": 24000,
            "playout_sample_rate_hz": 16000,
            "provider_pcm": self.pcm.pcm_snapshot(),
            "first_pcm_at_ms": self.first_at_ms,
            "first_nonzero_pcm_at_ms": self.first_nonzero_at_ms,
            "last_pcm_at_ms": self.last_at_ms,
            "last_arrival_gap_ms": self.last_gap_ms,
            "max_arrival_gap_ms": self.max_gap_ms,
            "last_peak": self.last_peak,
            "last_rms": self.last_rms,
            "playout": playout,
        })
    }
}
