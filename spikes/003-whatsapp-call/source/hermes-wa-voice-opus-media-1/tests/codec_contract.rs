//! Offline call-site contract, not a phone-call or pause-resolution test.

#[test]
fn every_pcm_call_selects_native_opus() {
    let calls = include_str!("../src/calls.rs");
    let outbound = calls.split("pub async fn run_outbound").nth(1).unwrap()
        .split("pub async fn run_inbound").next().unwrap();
    let inbound = calls.split("pub async fn run_inbound").nth(1).unwrap()
        .split("/// Asks Hermes").next().unwrap();
    for (direction, body) in [("inbound", inbound), ("outbound", outbound)] {
        assert!(body.contains(".audio_opus(mic_rx, spk_tx)"),
            "{direction} still selects the lossy MLow PCM default");
        assert!(!body.contains(".audio(mic_rx, spk_tx)"));
    }
}
