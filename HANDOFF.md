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
3. Pertahankan Control multiakun, Live khusus akun bot, dan satu percakapan
   WhatsApp Hermes yang aktif untuk DM, telepon, lampiran, transkrip, serta
   pekerjaan. Gunakan [skenario ChatGPT Live Desktop](PLAN.md#acuan-perilaku-telepon)
   sebagai acuan penerimaan telepon.

## Baseline yang terakhir diuji

- Snapshot kode publik yang diperiksa adalah
  [`9f9992575468a6935fdf0490209b0836b8313e90`](https://github.com/dhansxd/wa-control-live/commit/9f9992575468a6935fdf0490209b0836b8313e90).
  Regresi kandidat mencatat 680 tes Python, 15 subtes, 92 tes Rust, dan 5 tes
  Baileys lulus. Regresi Python dan Baileys dijalankan ulang pada snapshot
  publik. Rincian hasil dan batas verifikasi tercatat di [STATUS.md](STATUS.md#hasil-verifikasi).
- Instalasi publik, registrasi perintah setup, disable, enable, dan remove
  diverifikasi di profil Hermes v0.23.1 sementara. Kandidat berikutnya
  berhasil membangun sidecar release arm64 dan menjalankan regresi
  terhadap Hermes v0.23.2. Wizard mendeteksi bot yang sudah dipairing di
  Hermes dan QR Control menolak nomor bot. Scan perangkat nyata dan
  panggilan dengan provider nyata masih perlu diuji.
- Paket kandidat membawa sidecar macOS arm64. Uji instalasi native Hermes
  memakai snapshot Git lokal dengan dependensi dari prompt Hermes. Alur mulai
  dari `wa-control-live pair`. Gateway Hermes memiliki transport setelah
  perangkat tertaut. Lihat [status](STATUS.md#hasil-verifikasi) untuk hasil uji.
- Setelah bot tertaut, `whatsapp-control pair <label>` dapat menautkan akun
  tambahan saat gateway profil uji dijeda. Jalur QR memakai lock worker
  Control dan menutup koneksinya setelah akun tersimpan.
- Tiga perbaikan yang sudah memiliki regresi lokal adalah kelanjutan akun
  sehat saat lookup semua kontak gagal di akun lain, deduplikasi DM saat
  provider berpindah, dan satu klip lokal saat pemulihan audio berlangsung lama.

## Langkah pertama yang siap dilanjutkan

Uji **jeda interupsi pada panggilan nyata**. Regresi lokal [adapter](spikes/003-whatsapp-call/call_adapter.py)
sudah memeriksa pengosongan antrean setelah provider mengirim transkrip final,
kelanjutan input pemilik, dan tugas Hermes dengan ID yang sama. Gunakan
akun serta perangkat uji tertaut untuk mencatat waktu mulai suara pemilik dan
akhir suara lama yang terdengar. Bandingkan skenario yang sama di Desktop.
Periksa juga penutupan oleh asisten dan obrolan sambil kerja.

Alasan dan waktu retry per alias pada `/walive status` tetap tercatat sebagai
pekerjaan diagnostik. Data akun tersedia di `routing.accounts` pada
[pool Live](live_routing.py). Cek kondisi [formatter](live_commands.py) dan
[tes perintah](test_live_commands.py) ketika pekerjaan itu diambil.

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
