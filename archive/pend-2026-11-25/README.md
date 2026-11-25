# Arsip pengembangan WA Control & Live, 25 November 2026

Kode plugin, aturan pengembangan, rencana, status, dan tes saat proyek dijeda ada di [revisi `66cace7`](https://github.com/dhansxd/wa-control-live/tree/66cace7). Checkout utama, worktree percakapan, dan instalasi plugin menggunakan tree yang sama. Folder ini menyimpan hasil kerja tambahan dari direktori riset dan build.

## Empat kandidat pemasangan

Cabang [`archive/staging-2026-11-25`](https://github.com/dhansxd/wa-control-live/tree/archive/staging-2026-11-25) memuat keempat tree staging. Setiap commit arsip berisi seluruh berkas, mode berkas, dan struktur direktori yang sama dengan commit lokal pasangannya. ID commit arsip berubah karena metadata Git disiapkan untuk repositori publik.

| Commit lokal | Commit arsip | Isi |
| --- | --- | --- |
| `6f966c6` | [`0453aad`](https://github.com/dhansxd/wa-control-live/tree/0453aad) | Kandidat Control dan Live, aturan serta hasil uji awal |
| `335d0a9` | [`55c9885`](https://github.com/dhansxd/wa-control-live/tree/55c9885) | Deteksi akun bot dan pemeriksaan akun Control saat pairing |
| `a1661c0` | [`4a76e0f`](https://github.com/dhansxd/wa-control-live/tree/4a76e0f) | Paket instalasi dan pairing melalui Hermes |
| `a8cfb1f` | [`e75ccce`](https://github.com/dhansxd/wa-control-live/tree/e75ccce) | Pemakaian Node yang disiapkan Hermes |

Untuk membuka kandidat terakhir di clone baru, jalankan `git switch --detach e75ccce`. Ganti ID tersebut untuk melihat kandidat lain. Revisi lokal sebelum amend `c516bae` tersedia pada [tag arsip](https://github.com/dhansxd/wa-control-live/tree/archive/pre-amend-c516bae). Perbedaannya dari `978704f` hanya satu baris hasil tes di STATUS.md. Tree revisi sebelum amend `461d854` identik dengan [`b815440`](https://github.com/dhansxd/wa-control-live/tree/b815440).

## Riset suara dan pengujian

- [Perbandingan jeda suara](research/perbandingan-jeda-suara-2026-11-23.md) memuat cara menghitung waktu, hasil sintetis, dan pengukuran agregat pemutaran ulang. [Riset perilaku Desktop dan Hermes](research/riset-telepon-hermes-dan-chatgpt-desktop.md) mencatat sumber, versi klien, dan batas kesimpulan. [Catatan instalasi](research/instalasi-8ff07d3.md) merekam pemeriksaan pada rilis `8ff07d3`.
- [Probe pengiriman](research/probe_voice_delivery.py) membandingkan hasil transport media dengan laporan tugas dan konteks suara. Gunakan Python 3.12 dengan dependensi uji plugin dan sumber Hermes v0.23.2 pada `PYTHONPATH`. Jalankan `python probe_voice_delivery.py CHECKOUT DIREKTORI_UJI_BARU HTTP_STATUS` dengan checkout `907e804` dan direktori kosong terpisah untuk tiap run. Pada verifikasi arsip, transport tiruan menerima fixture 200 dan menolak fixture 400. Kedua run memakai gambar sintetis dan nomor fiksi.
- [Fixture penerimaan tugas](research/unproposed_voice_task.py) mempertahankan pengujian awal ketika provider memberi jawaban suara langsung. Dengan fixture identik, revisi `cfbb3f1` gagal pada dua urutan transkrip dan lulus pada lima kontrol. Semua tujuh kasus lulus pada `ea75123`. [Draf pengujian Desktop](research/desktop_live_draft.py) memulihkan blob lokal `2c463aa`. Keempat kasusnya lulus pada `66cace7`. Jalankan berkas masing-masing secara eksplisit dengan pytest, dengan direktori checkout, `spikes/003-whatsapp-call`, dan sumber Hermes di `PYTHONPATH`. Pemeriksaan arsip memakai Python 3.12.15 pada macOS arm64.
- [Dependensi audit Hermes](research/runtime-requirements.txt) memuat pin lingkungan audit awal. Seluruh 23.586 entri sumber audit cocok dengan tag [Hermes v0.23.1](https://github.com/NousResearch/hermes-agent/tree/3b421578588243f920886e6fe79a6faa87ea026e) setelah normalisasi akhir baris PowerShell. Paket Python, cache, dan salinan sumber upstream dapat dibuat kembali.

Log regresi sementara dari audit v0.23.1 mencatat satu run 686 lulus dan dua gagal (validasi argumen perintah provider serta status akun saat worker terinterupsi). Run berikutnya mencatat 687 lulus dan satu gagal pada validasi argumen. Kedua run melaporkan 15 subtes lulus. Rangkaian terfokus mencatat 195 lulus dan satu gagal pada validasi yang sama. Satu percobaan lain berhenti saat impor dependensi pytest `py`. Log itu tidak mencatat SHA plugin untuk tiap run. Hasil per revisi yang lebih baru tersedia di [status proyek](../../STATUS.md).

## Build sidecar dan lisensi

Seluruh 32 berkas sumber dalam direktori build sidecar lokal identik dengan [sumber sidecar pada `8ff07d3`](https://github.com/dhansxd/wa-control-live/tree/8ff07d3/spikes/003-whatsapp-call/source/hermes-wa-voice-opus-media-1). Checkout dependensi menggunakan revisi [WhatsApp Rust `9b9fdb7`](https://github.com/oxidezap/whatsapp-rust/tree/9b9fdb7e5c4211cdc7c837abb6a9ae9c597e35c5). Hash sembilan berkas hasil patch lokal cocok dengan pemeriksa pada `prepare_whatsapp_rust.py`. Lisensi MIT sumber dan atribusi binari tersedia pada [pemberitahuan repositori](../../THIRD_PARTY_NOTICES.md).

[Generator pemberitahuan historis](sidecar-licenses/sidecar_licenses.py) memakai [inventaris 310 paket](sidecar-licenses/production-crates.txt), [daftar berkas lisensi](sidecar-licenses/crate-notice-files.txt), dan salinan pemberitahuan dalam `sidecar-licenses/pinned/`. Untuk menjalankannya, gunakan Python 3.11 atau lebih baru. Dari `spikes/003-whatsapp-call` pada checkout `8ff07d3`, jalankan skrip persiapan Rust, lalu `cargo fetch --locked --manifest-path source/hermes-wa-voice-opus-media-1/Cargo.toml`. Tetapkan `CRATE_REGISTRY_SRC` ke direktori registry yang langsung berisi direktori paket Cargo. Jalankan generator dari direktori kerja yang sama menggunakan jalur ke salinan arsip.

Pemberitahuan hasil generator cocok byte demi byte dengan artefak build historis. SHA-256 hasilnya adalah `c57f10c0e3d6a5090de90d8e4f2a68c984e350f9d15ce3a3ff6f050d9b2bfe1e`. Paket sidecar yang sudah dirilis memiliki kumpulan pemberitahuan tersendiri. Log build historis mencatat dua tes konfirmasi pairing lulus, lalu 87 tes lulus dan tujuh dilewati pada rangkaian pemeriksaan owner LID. Rekaman telepon, transkrip atau pesan asli, identitas akun, kunci, profil terpasang, dan log runtime privat berada di luar arsip publik.
