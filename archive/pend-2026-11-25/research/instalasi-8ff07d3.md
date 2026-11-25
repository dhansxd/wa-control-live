# Verifikasi instalasi WA Control & Live

23 November 2026, macOS arm64, Hermes v0.23.2 `ae2a81ccc0`. Revisi kode
`ea75123f921f631d5db65e1ccd441199d3398ed5` dan revisi rilis
`8ff07d351b42416ab92b3e5db8a1cf6a0a737280` memuat kode plugin yang sama.

| Pemeriksaan | Hasil |
| --- | --- |
| Asal instalasi | SHA GitHub `main`, checkout lokal dan instalasi Hermes cocok. |
| Profil pemilik | Instalasi ulang melalui Hermes, persetujuan dependensi Node dan Python, satu restart gateway. Adapter yang berjalan berasal dari plugin terpasang. |
| Akun dan sesi | Bot serta akun Control tambahan terhubung kembali. ID percakapan WhatsApp yang diberikan Hermes sebelum dan sesudah restart cocok. |
| Suara | Live aktif, rute `codex-chatgpt-subscription` terpilih. Uji suara sintetis dengan Codex menerima satu jawaban dan satu pengajuan untuk permintaan foto. Pengajuan memakai penerima uji. |
| Profil baru | Instalasi GitHub di profil sementara tanpa plugin, pairing, atau kredensial. CLI resmi menjalankan pemasangan dependensi. Pemeriksaan `plugins info`, pemuatan perintah, dan impor dependensi berhasil. |
| Lifecycle | Saat plugin dinonaktifkan, CLI khusus plugin berhenti tersedia. Setelah diaktifkan kembali, bantuan pairing tersedia. Sesudah dihapus, direktori dan perintah plugin hilang. Seluruh perubahan lifecycle ini dilakukan di profil sementara. |

Percobaan profil baru dengan cache kosong sempat gagal ketika mengunduh
dependensi Hermes dari GitHub. Uji yang berhasil memakai cache dependensi dan
toolchain Hermes di Mac. Pengujian ini tidak mengirim media ke akun pemilik.
Pengukuran jeda audio dan perbandingan dengan ChatGPT Desktop pada perangkat
tetap terbuka. [Regresi dan hasil pembanding](https://github.com/dhansxd/wa-control-live/blob/8ff07d3/STATUS.md#hasil-verifikasi)
