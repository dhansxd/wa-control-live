//! Actual native codec characterization, without WhatsApp sockets or model inference.
use whatsapp_rust::voip::audio::{WaOpusDecoder, WaOpusEncoder};

#[test]
fn native_opus_carries_sixty_ms_pcm_without_silencing_speech_energy() {
    let mut encoder = WaOpusEncoder::new().unwrap();
    let mut decoder = WaOpusDecoder::new().unwrap();
    let mut total_samples = 0;
    let mut square_sum = 0.0;
    for frame in 0..8 {
        let pcm: Vec<i16> = (0..960).map(|i| {
            (12000.0 * (2.0 * std::f64::consts::PI * 440.0
                * (frame * 960 + i) as f64 / 16000.0).sin()) as i16
        }).collect();
        let payload = encoder.encode(&pcm).unwrap();
        assert!(!payload.is_empty());
        let decoded = decoder.decode(&payload).unwrap();
        assert_eq!(decoded.len(), 960, "60ms at 16kHz must remain 960 samples");
        total_samples += decoded.len();
        square_sum += decoded.iter().map(|v| (*v as f64).powi(2)).sum::<f64>();
    }
    let rms = (square_sum / total_samples as f64).sqrt();
    assert!(rms > 5000.0, "Opus roundtrip silenced actual PCM energy: rms={rms}");
}
