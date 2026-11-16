# Handoff WA Control & Live

Gunakan halaman ini untuk melanjutkan proyek dari clone baru atau sesi Codex
baru. Sumber berada di [branch `main`](https://github.com/dhansxd/wa-control-live/tree/main).

```sh
git clone https://github.com/dhansxd/wa-control-live.git
cd wa-control-live
```

Contoh permintaan dari root repo:

> Lanjutkan WA Control & Live sesuai HANDOFF.md. Cek kode dan tes yang sudah ada, lalu kerjakan satu langkah pertama yang masih terbuka.

## Sebelum mengubah kode

1. Baca [AGENTS.md](AGENTS.md) untuk aturan kerja dan batas proyek,
   [PLAN.md](PLAN.md) untuk spesifikasi serta kriteria penerimaan, dan
   [STATUS.md](STATUS.md) untuk progres serta bukti pengujian.
2. Jalankan `git status --short --branch` dan `git log -5 --oneline`. Cocokkan
   kode serta tes yang relevan dengan checklist, terutama jika `main` sudah
   berubah. Gunakan implementasi yang tersedia dan perbaiki celah yang terbukti.
3. Implementasi lanjutan dijeda saat handoff ini ditulis. Mulai perubahan
   setelah ada permintaan melanjutkan. Pertahankan Control multiakun, Live
   khusus akun bot, dan satu percakapan WhatsApp Hermes yang aktif untuk DM,
   telepon, lampiran, transkrip, serta pekerjaan.

## Baseline yang terakhir diuji

- Snapshot kode publik yang diperiksa adalah
  [`9f9992575468a6935fdf0490209b0836b8313e90`](https://github.com/dhansxd/wa-control-live/commit/9f9992575468a6935fdf0490209b0836b8313e90).
  Regresi kandidat mencatat 680 tes Python, 15 subtes, 92 tes Rust, dan 5 tes
  Baileys lulus. Regresi Python dan Baileys dijalankan ulang pada snapshot
  publik. Rincian hasil dan batas verifikasi tercatat di [STATUS.md](STATUS.md#hasil-verifikasi).
- Instalasi publik, registrasi perintah setup, disable, enable, dan remove
  diverifikasi di profil Hermes v0.23.1 sementara. Build sidecar release dari
  clone bersih, wizard lengkap dengan perangkat tertaut, dan panggilan dengan
  provider nyata masih perlu diuji pada kandidat rilis.
- Tiga perbaikan yang sudah memiliki regresi lokal adalah kelanjutan akun
  sehat saat lookup semua kontak gagal di akun lain, deduplikasi DM saat
  provider berpindah, dan satu klip lokal saat pemulihan audio berlangsung lama.

## Langkah pertama yang siap dilanjutkan

Tampilkan **alasan dan waktu retry per alias akun di `/walive status`**.
Pada snapshot ini, [pool Live](live_routing.py) sudah menyediakan data akun
di `routing.accounts`. [Formatter perintah](live_commands.py) baru menampilkan
ringkasan keseluruhan. Cek keduanya lagi sebelum mengubahnya.

- Gunakan alasan yang sudah dinormalisasi, alias yang aman ditampilkan, dan
  waktu retry jika tersedia. Pertahankan status panggilan dan bukti pengiriman
  notifikasi yang sudah ditampilkan.
- Tambahkan kasus lokal di [tes perintah Live](test_live_commands.py). Uji
  beberapa alias dengan penyebab berbeda, waktu retry yang tersedia, dan
  ketiadaan secret atau detail error mentah pada balasan.
- Catat hasil di PRD dan status. Jika langkah ini sudah selesai di commit
  yang lebih baru, pilih celah berikutnya dari status setelah memeriksa kode
  dan bukti yang ada.

Retry DM setelah WhatsApp pulih tercatat sebagai usulan. Resumption provider
lewat handle Gemini atau fork GPT-Live adalah opsi teknis terbuka. Pertahankan
urutan implementasi dan tes lokal sebelum pengujian perangkat nyata.

## Peta kode dan pengujian setelah reset

| Area | Titik masuk |
| --- | --- |
| Setup dan lifecycle Hermes | [Registrasi plugin](__init__.py), [wizard](setup_wizard.py), [launcher](spikes/003-whatsapp-call/launch.py) |
| Control dan chatbot | [Service Control](whatsapp_control/service.py), [chatbot tamu](whatsapp_control/chatbot.py), [pemeriksaan Control](whatsapp_control/checks/) |
| Sesi WhatsApp aktif | [Gateway](whatsapp_gateway.py), [sesi](whatsapp_session.py), [delegasi dan riwayat Live](live_agent.py) |
| Audio dan provider Live | [Adapter panggilan](spikes/003-whatsapp-call/call_adapter.py), [routing](live_routing.py), [tes pergantian](spikes/003-whatsapp-call/test_live_switch.py), [sumber sidecar Rust](spikes/003-whatsapp-call/source/hermes-wa-voice-opus-media-1/) |

Tes dasar Control dan bridge dapat dijalankan dari root tanpa akun nyata:

```sh
PYTHONDONTWRITEBYTECODE=1 python3.12 -m unittest discover -s whatsapp_control/checks -p 'test_*.py' -q
npm ci
npm run check
node --test whatsapp_control/checks/test_bridge.mjs
```

Untuk tes `test_live_commands.py` dan regresi Python gabungan, siapkan Python
3.12 dengan [lockfile plugin](spikes/003-whatsapp-call/requirements-lock.txt)
serta source dan dependensi runtime Hermes tag `v0.23.1` pada jalur impor.
Pakai `HERMES_HOME` sementara untuk tes. Untuk tes Rust, ikuti persiapan
dependensi yang dipin di [README](README.md#struktur-dan-pengujian).

Repositori publik memulihkan sumber, tes, dan dokumentasi. Instalasi baru,
kredensial provider, serta penautan akun untuk pemakaian nyata mengikuti
[README](README.md#setup-plugin) di profil Hermes masing-masing.
