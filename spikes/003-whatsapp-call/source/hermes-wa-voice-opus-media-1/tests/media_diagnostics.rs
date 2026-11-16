//! Offline diagnostics contract, not proof of phone input continuity.
#[path = "../src/media_diagnostics.rs"]
mod media_diagnostics;

use media_diagnostics::{OutputDiagnostics, PcmDiagnostics};
use whatsapp_rust::wacore::voip_control::media_stats::CallMediaStats;

#[test]
fn output_diagnostics_distinguish_zero_pcm_and_energy_with_arrival_times() {
    let mut output = OutputDiagnostics::default();
    output.observe(&[0; 480], 100);
    let silent = output.snapshot(serde_json::json!({}));
    assert_eq!(silent["first_pcm_at_ms"], 100);
    assert!(silent["first_nonzero_pcm_at_ms"].is_null());
    assert_eq!(silent["last_peak"], 0);
    assert_eq!(silent["last_rms"], 0.0);
    output.observe(&[-32768, 32767, 0, 0], 120);
    let energy = output.snapshot(serde_json::json!({}));
    assert_eq!(energy["last_peak"], 32768);
    assert!(energy["last_rms"].as_f64().unwrap() > 23000.0);
    output.observe(&[0; 480], 190);
    let snapshot = output.snapshot(serde_json::json!({"queued_samples": 320}));
    assert_eq!(snapshot["provider_sample_rate_hz"], 24000);
    assert_eq!(snapshot["playout_sample_rate_hz"], 16000);
    assert_eq!(snapshot["provider_pcm"]["frames"], 3);
    assert_eq!(snapshot["provider_pcm"]["samples"], 964);
    assert_eq!(snapshot["provider_pcm"]["all_zero_frames"], 2);
    assert_eq!(snapshot["provider_pcm"]["nonzero_samples"], 2);
    assert_eq!(snapshot["provider_pcm"]["peak"], 32768);
    assert_eq!(snapshot["first_nonzero_pcm_at_ms"], 120);
    assert_eq!(snapshot["last_pcm_at_ms"], 190);
    assert_eq!(snapshot["max_arrival_gap_ms"], 70);
    assert_eq!(snapshot["last_arrival_gap_ms"], 70);
    assert_eq!(snapshot["last_peak"], 0);
    assert_eq!(snapshot["last_rms"], 0.0);
    assert_eq!(snapshot["playout"]["queued_samples"], 320);
}

#[test]
fn snapshot_counts_actual_pcm_without_mutation_or_overflow() {
    let mut stats = PcmDiagnostics::default();
    let zero = [0i16; 320];
    let loud = [-32768i16, 32767, 0, 0];
    stats.observe(&zero);
    stats.observe(&loud);
    let snapshot = stats.snapshot(1000, CallMediaStats::default());
    assert_eq!(snapshot["at_ms"], 1000);
    assert_eq!(snapshot["pcm"]["frames"], 2);
    assert_eq!(snapshot["pcm"]["samples"], 324);
    assert_eq!(snapshot["pcm"]["all_zero_frames"], 1);
    assert_eq!(snapshot["pcm"]["nonzero_samples"], 2);
    assert_eq!(snapshot["pcm"]["peak"], 32768);
    assert_eq!(zero, [0i16; 320]);
    assert_eq!(loud, [-32768i16, 32767, 0, 0]);
}

#[test]
fn native_snapshot_preserves_current_counters_without_diagnosing_silence() {
    let mut native = CallMediaStats::default();
    native.rtp_received = 11;
    native.foreign_frames_decoded = 8;
    native.audio_frames_concealed = 2;
    native.playout_trimmed_samples = 320;
    native.srtp_unprotect_failed = 1;
    native.audio_sink_dropped = 3;
    let snapshot = PcmDiagnostics::default().snapshot(2000, native);
    assert_eq!(snapshot["native"]["rtp_received"], 11);
    assert_eq!(snapshot["native"]["foreign_frames_decoded"], 8);
    assert_eq!(snapshot["native"]["audio_frames_concealed"], 2);
    assert_eq!(snapshot["native"]["playout_trimmed_samples"], 320);
    assert_eq!(snapshot["native"]["srtp_unprotect_failed"], 1);
    assert_eq!(snapshot["native"]["audio_sink_dropped"], 3);
    assert!(snapshot.get("cause").is_none());
    assert!(snapshot.get("transcript").is_none());
}

#[test]
fn live_call_emits_native_and_pcm_snapshot_without_changing_audio_forwarding() {
    let source = include_str!("../src/calls.rs");
    let converse = source.split("pub(crate) async fn converse").nth(1).unwrap();
    assert!(converse.contains("pcm_diagnostics.observe(&f);"), "Live call does not observe actual spk_rx PCM");
    assert!(converse.contains("pcm_diagnostics.snapshot("), "Live call does not publish the snapshot");
    assert!(converse.contains("line.media_stats()"), "Live call never samples native counters");
    assert!(converse.contains("live.send_audio(&f);"), "Diagnostic removed normal PCM forwarding");
    assert!(converse.contains(".as_millis()"), "Snapshots need a monotonic call-relative clock");
}
