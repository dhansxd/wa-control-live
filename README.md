# WA Control & Live

**Status proyek: PENDING sejak 25 November 2026.** Pengembangan dijeda atas
permintaan pemilik. Versi terakhir sebelum jeda adalah [v0.2.1, commit `66cace7`](https://github.com/dhansxd/wa-control-live/commit/66cace7dbb2d17f3eaa92dd66f03ca8df51f65cc).
Untuk melanjutkan, baca [aturan proyek](AGENTS.md), [rencana](PLAN.md),
[catatan serah terima](HANDOFF.md), dan [hasil pengujian](STATUS.md).
Catatan riset, alat audit, dan tautan ke snapshot pengembangan lama ada
di [arsip saat proyek dijeda](archive/pend-2026-11-25/README.md).

Plugin komunitas untuk Hermes Agent yang menggabungkan kontrol multiakun
WhatsApp, chatbot tamu, dan telepon Live. Akun bot memakai satu koneksi untuk
Control, DM pemilik, dan telepon. Akun tambahan bisa ditautkan untuk Control.
DM dan telepon mengikuti satu percakapan aktif di Hermes. Pilihan audio Live
mencakup Codex CLI, Gemini Live, GPT-Live, broker Realtime Hermes, dan gateway
WebSocket yang kompatibel dengan GPT-Live.

## Install dan pairing

Gunakan profil Hermes yang sudah menjalankan bot WhatsApp di macOS Apple
Silicon. Setujui dependensi Python dan Node pada prompt resmi Hermes.
Sidecar macOS arm64 sudah ada di paket Git dan diverifikasi oleh plugin.

```sh
hermes plugins install dhansxd/wa-control-live --enable
hermes wa-control-live pair
```

Plugin mendeteksi bot dari pairing WhatsApp Hermes jika tersedia. Konfirmasi
kontak pemilik dan pindai QR Live dari **Perangkat Tertaut** di WhatsApp bot.
Dalam wizard yang sama, masukkan label seperti `personal` untuk menautkan
akun Control tambahan. Pindai QR kedua dari WhatsApp akun tersebut.
Setelah selesai, jalankan `hermes gateway restart` sekali jika gateway
profil itu sudah berjalan. Plugin akan berjalan bersama gateway Hermes.
[Panduan plugin Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#managing-plugins)

Untuk menambah akun nanti, gunakan `hermes whatsapp-control pair pribadi`.
QR ini juga tersedia saat gateway dijeda. Plugin membaca identitas dari
hasil scan dan mengarahkan akun bot kembali ke QR Live.

Gunakan profil yang sama untuk semua perintah. Jika memakai profil bernama,
tambahkan `-p NAMA_PROFIL` pada perintah Hermes.

`hermes wa-control-live setup` tersedia untuk mengubah provider audio,
mengatur izin pemilik, atau menunda scan. Pairing pertama memakai Codex CLI
jika tersedia dan provider belum dipilih. Opsi Codex memakai login CLI itu.
Opsi broker Realtime memakai login Codex profil Hermes. API key yang dimasukkan
disimpan lewat mekanisme secret Hermes. Lihat kesiapan dengan
`hermes wa-control-live status`.

### Pilih provider Live

Tanpa pengaturan `live`, plugin memakai login Codex yang sudah tersedia.
Telepon memakai aturan percakapan Hermes Desktop Live dan mengikuti bahasa
pemilik, termasuk Indonesia dan Inggris. Pada jalur Codex, begitu ucapanmu
selesai, Hermes memeriksa maksudnya sambil audio tetap aktif. Ini dapat memakai
token tambahan, termasuk saat mengobrol biasa. Permintaan kerja masuk sekali
ke sesi DM WhatsApp yang sedang aktif. Kamu bisa langsung melanjutkan obrolan.
Saat ditanya progres, Hermes memakai hasil alat dan tanda terima pengiriman
dari sesi itu. Ucapkan “tutup telepon” atau “hang up” untuk mengakhiri panggilan.

Untuk memakai provider lain atau pool, tambahkan blok berikut di bawah
`plugins.entries.wa-control-live.settings` pada profil Hermes yang sama.

```yaml
live:
  selected: audio
  providers:
    codex:
      kind: codex
    gemini:
      kind: gemini
      model: gemini-3.8-live
      accounts:
        utama:
          secret_env: GEMINI_API_KEY
        kedua:
          secret_env: GEMINI_LIVE_KEY_2
    gpt-live:
      kind: gpt-live
      model: gpt-live-1
      voice: marin
      accounts:
        utama:
          secret_env: OPENAI_API_KEY
  pools:
    audio: [codex, gemini, gpt-live]
```

Sediakan variabel secret yang dirujuk melalui profil atau lingkungan tempat
`serve` dijalankan. Konfigurasi hanya berisi nama variabel. Jalankan ulang
`serve` setelah mengedit daftar provider, pool, atau secret. Pilihan alias
dapat diubah dari terminal saat layanan berjalan.

```sh
hermes wa-control-live list
hermes wa-control-live use audio
hermes wa-control-live use gemini now
hermes wa-control-live status
```

`use audio` memilih pool untuk koneksi audio berikutnya. `use gemini now`
mencoba menyiapkan koneksi Gemini di panggilan aktif dan menggunakan Gemini
untuk panggilan berikutnya setelah berhasil. Dalam pool, urutan provider
digilir per panggilan dan setiap provider punya giliran akun sendiri. Status
menampilkan alias yang aktif dan alasan jika pool kurang siap. Uji konektor
Gemini, GPT-Live, dan perpindahan provider saat ini menggunakan fixture lokal.
GPT-Live memakai API key proyek. Jika `accounts` dihilangkan, GPT-Live memakai
`OPENAI_API_KEY`. Pakai `/walive use gpt-live now` untuk pindah ke alias pada
contoh di atas saat telepon aktif.

Untuk gateway dengan protokol WebSocket GPT-Live, tambahkan alias berikut di
`live.providers`. Gunakan URL WebSocket lengkap yang diberikan gateway dan
referensi `secret_env` untuk key milik gateway tersebut.

```yaml
gateway:
  kind: gpt-live
  endpoint: wss://gateway.example.com/live/sessions
  model: gpt-live-1
  accounts:
    utama:
      secret_env: GATEWAY_LIVE_KEY
```

Endpoint memakai `wss://` untuk koneksi jaringan atau `ws://` pada loopback.
Plugin mengirim key ke URL yang dikonfigurasi dan menolak redirect. Gateway
perlu mendukung `session.start`, audio dua arah, dan client delegation
mengikuti [protokol GPT-Live](https://developers.openai.com/api/reference/resources/live/primary-websocket).
Pilih dengan `/walive use gateway` atau tambahkan `gateway` ke `live.pools.audio`.

Untuk mencoba OpenAI Realtime dengan login Codex di Hermes, tambahkan ini ke
`live.providers` pada profil Hermes yang menjalankan plugin.

```yaml
hermes-audio:
  kind: hermes-realtime
  model: gpt-realtime-2.1
```

`hermes-realtime` memakai satu login profil dengan model dan suara opsional.
Hermes meminta token sesi sementara lewat broker `openai-codex`, lalu adapter
memakainya untuk menyambungkan audio Realtime lewat WebRTC. Untuk profil yang
meminta login ulang, jalankan `hermes auth add openai-codex` pada profil itu.
Pilih alias dengan `/walive use hermes-audio` atau masukkan ke pool yang sama.

**Status pengujian `hermes-realtime`**. Uji lokal mencakup broker dengan respons
simulasi, audio WebRTC dua arah, dan pemulihan setelah login ulang. Uji dengan
akun nyata ditunda sampai login Codex di Hermes tersedia. Penerbitan token oleh
OpenAI, akses model, audio WebRTC ke OpenAI, dan panggilan WhatsApp melalui
provider ini belum terverifikasi.

Di DM WhatsApp pemilik, gunakan `/walive`, `/walive list`, `/walive use audio`,
atau `/walive use gemini now`. Operator bisa memakai perintah yang sama di DM
Telegram jika kebijakan slash Hermes pada profil tersebut mengizinkannya.
Saat agen sibuk, Hermes menerapkan perilaku input sibuk profil itu.
Pemilik atau operator yang sama juga bisa memilih provider atau pool lewat
pesan biasa, misalnya “Pakai Gemini untuk telepon berikutnya” atau “Ganti ke
Gemini sekarang.” Hermes bisa mengikuti maksud dari teks maupun transkrip
voice note. Pilihan saat panggilan berjalan berlaku setelah audio pengganti
siap. Jika persiapan gagal, audio lama tetap dipakai selama masih sehat.

Saat pemulihan audio menimbulkan jeda, sidecar memutar satu klip lokal.
Jika pilihan audio habis, sidecar mencoba mengirim rincian ke DM pemilik.
Ketika audio telepon masih tersedia, sidecar juga memutar klip kegagalan.
[Klip pemberitahuan suara](spikes/003-whatsapp-call/assets/README.md)
Status menyebut apakah audio diserahkan ke jalur media dan apakah WhatsApp
menerima pesan. Untuk memakai notifikasi suara dan DM, build sidecar gabungan
dari source yang menyertakan fitur ini.

Daftarkan akun tambahan dengan `hermes whatsapp-control pair <label>`
setelah menautkan bot. Gunakan `hermes whatsapp-control status`
untuk melihat label akun yang tersedia. Atur chatbot tiap akun dari percakapan
pemilik dengan Hermes atau melalui `hermes whatsapp-control configure`.
Format konfigurasi dan operasi tersedia di [panduan WhatsApp Control](whatsapp_control/README.md).

Untuk memperbarui sidecar pada profil yang sudah tertaut, hentikan `serve`,
jalankan `setup --sidecar` dengan binary dan SHA-256 baru, lalu mulai `serve`
kembali.
Jika WhatsApp memilih port 3480, sidecar mencoba port 3478 secara
paralel pada host relay yang sama. Mekanisme bawaan memprioritaskan port 3480.
Antrean audio masuk memiliki ruang tambahan saat paket tiba beruntun. Jika
host utama lambat atau gagal saat menerima panggilan pemilik, sidecar mencoba
hingga tiga host lain dari tawaran yang sama, masing-masing dengan token
yang sesuai. Log mencatat jumlah host yang layak dan hasil sambungannya.

Jalankan gateway pada profil Hermes yang sama dan kirim pesan ke bot lewat
WhatsApp untuk memulai sesi DM. Saat panggilan masuk masih berdering, plugin
membuka sesi DM WhatsApp yang aktif untuk nomor pemilik dan menyiapkan koneksi
audio pilihan dengan konteks sesi tersebut. Panggilan diangkat setelah sesi suara siap.
Pesan dan lampiran yang diterima Hermes di DM ikut masuk ke konteks selama
panggilan melalui langganan ke sesi aktif di gateway Hermes. Hasil pekerjaan DM
yang selesai saat telepon bisa disampaikan lewat suara dari hasil Hermes yang
sama. Pertanyaan baru tentang file memakai lampiran
asli jika masih tersedia. Jawaban lisan dari konteks dicatat di sesi yang sama
selama panggilan, sehingga pembicaraan bisa langsung dilanjutkan di DM.
Untuk pekerjaan yang membutuhkan alat atau data baru, Hermes memakai model,
provider, dan izin profil itu. Gateway mengirim hasil lengkap dan lampiran
lewat WhatsApp. Asisten bisa membahas hasilnya di telepon.
Selama kamu mengobrol, Live menerima progres dari tugas yang sama. Kamu
bisa bertanya “sudah sampai mana?” atau “bagian kode mana yang berubah?”.
Rincian mengikuti laporan alat, termasuk kegagalan dan perubahan file yang
sudah terkonfirmasi.
Pekerjaan yang sudah diterima gateway dapat berlanjut setelah telepon ditutup.
Jika sesi DM berganti saat panggilan berlangsung, plugin memulai ulang koneksi
suara dengan konteks sesi yang baru di panggilan yang sama. Persetujuan atau
klarifikasi ditangani dari DM itu di Hermes.

Untuk memulai panggilan dari chat, minta asisten menelepon dengan bahasa
sehari-hari di DM WhatsApp, lewat teks atau pesan suara. Tool
`whatsapp_call_now` memakai nomor pemilik yang sudah terdaftar. Sidecar
gabungan menyiapkan provider audio sebelum mengirim tawaran panggilan
WhatsApp. Jika semua pilihan gagal, penyebabnya dilaporkan ke DM tanpa
membuat dering. Saat kamu mengangkat, koneksi audio yang sudah siap
melanjutkan sesi DM aktif.
Kamu juga bisa menjadwalkan satu panggilan balik dari DM atau saat sedang
menelepon, misalnya “telepon aku besok jam sembilan”. Hermes mengonfirmasi
waktu setelah jadwal tercatat di cron profil yang sama.

## Nonaktifkan, perbarui, dan hapus

`hermes plugins disable wa-control-live` menonaktifkan plugin dan launcher
menghentikan proses yang dimiliki plugin. Ikuti arahan reload gateway dari
Hermes agar registrasi gateway ikut diperbarui.
Untuk menjalankannya lagi, gunakan
`hermes plugins enable wa-control-live` dan ikuti petunjuk gateway Hermes.

Untuk instalasi yang mengikuti cabang, jalankan
`hermes plugins update wa-control-live`. Untuk instalasi yang dipin, pilih SHA
lengkap yang baru dengan
`hermes plugins install dhansxd/wa-control-live --force --ref SHA_LENGKAP --enable`.
Runtime mengikuti revisi plugin yang terpasang dan pairing yang masih valid.

Untuk berhenti sementara dan menyimpan data, jalankan
`hermes wa-control-live cleanup`. Untuk menghapus kode plugin dan
registrasinya, jalankan `hermes plugins remove wa-control-live`. Jika juga
ingin menghapus data plugin dan mencoba melepas perangkat, jalankan kedua
langkah berikut pada profil yang sama.

```sh
hermes wa-control-live cleanup --purge
hermes plugins remove wa-control-live
```

Periksa hasil purge sebelum uninstall. Jalankan purge saat plugin masih aktif.
Jika layanannya juga sedang berjalan, purge mencoba melepas perangkat melalui
transport yang tersedia. Purge tetap bisa dijalankan saat layanan sudah mati.
Laporan mencatat `local_removed`, `remote_unlinked`, dan jika tersedia
`unlink_requested` per akun. Untuk setiap `remote_unlinked: false`, buka
**WhatsApp → Perangkat Tertaut** dan hapus perangkat itu jika masih terdaftar.
Jika launcher dari versi lama belum merespons cleanup, hentikan launcher itu
pada profil yang sama, lalu ulangi cleanup.

Purge mencakup data Control, Live gabungan, runtime Live lama milik instalasi
yang sama, dan alarm telepon yang masih menunggu dan bisa dibuktikan asalnya.
Riwayat Hermes, kredensial bersama, pairing WhatsApp bawaan Hermes, dan instalasi
atau profil lain tetap tersimpan. Jika versi WhatsApp Control standalone juga
terpasang pada profil itu, cleanup menolak menyentuh data Control yang dipakai
bersama. Hermes menghapus kode serta entri konfigurasi plugin saat uninstall.

Pada mode terpisah dengan `unified_gateway: false`, `setup --reuse-pairing-from
/path/to/previous-state` menyalin sesi dari launcher lama yang sudah dihentikan.
Opsi ini memverifikasi identitas akun bot sebelum menyimpan sesi.

## Struktur dan pengujian

Source terbagi dalam tiga tahap pengujian.

- `spikes/001-codex-audio` berisi transport audio langganan Codex dan tesnya.
- `spikes/002-hermes-task` berisi handoff tugas Hermes dengan pencatatan hasil.
- `spikes/003-whatsapp-call` berisi adapter panggilan, launcher, dan tesnya.
- `spikes/003-whatsapp-call/source/hermes-wa-voice-opus-media-1` berisi source
  sidecar WhatsApp Rust yang dipilih launcher.

Plugin mendukung satu pemilik dan satu panggilan aktif. Setelah tersambung,
panggilan berjalan sampai ditutup atau koneksi berakhir,
termasuk saat tidak ada yang bicara. Rekaman WAV diagnostik tetap terbatas
pada 180 detik audio per arah.
`PLAN.md` merangkum arsitektur dan cakupan pengujian.

Repositori ini memuat [WhatsApp Control](whatsapp_control/LICENSE) dari commit
`4ebdd0f` dan telepon Live dalam satu plugin Hermes. Tool `whatsapp_control`
menggunakan service Control untuk pengelolaan akun, operasi WhatsApp, dan
kebijakan chatbot. Mode `unified_gateway` memakai satu koneksi Rust untuk
DM pemilik, Control, dan telepon Live pada nomor bot Hermes. Koneksi Baileys
melayani akun tambahan. Chatbot tamu memakai pengaturan dan riwayat
percakapannya sendiri dengan akses media sesuai kebijakan akun.

Pairing menyimpan konfigurasi akun bot pada profil Hermes aktif. Adapter
gateway menjalankan transport dan service Control untuk profil itu. Perintah
`hermes whatsapp-control` memakai service yang sama untuk pengelolaan akun
dan kebijakan chatbot.

Panggilan Live melanjutkan sesi WhatsApp pemilik yang aktif di Hermes.
Integrasi sesi memakai penyimpanan internal Hermes, termasuk pemetaan rute
pada gateway multiplex. Jalankan kembali tes sesi dan reconnect pada build
Hermes yang akan dipakai sebelum memperbarui instalasi. Konfirmasi server
untuk kirim pesan atau Status tersimpan bersama ID operasinya. Status
`accepted` berarti server mengonfirmasi pesan, sedangkan visibilitas ke
penerima perlu diperiksa pada perangkat penerima.

Git menyimpan source, tes, dependency lock, sidecar rilis, dan dokumentasi.
State autentikasi, identitas nomor, rekaman panggilan, laporan runtime, dan lingkungan lokal
disimpan terpisah melalui `.gitignore`. Source dan fixture audio upstream
tersedia di `spikes/003-whatsapp-call/source/hermes-wa-voice-opus-media-1`.

Jalankan pemeriksaan Control dan Baileys berikut dari root repo. Suite Python
gabungan juga memerlukan dependensi plugin di
`spikes/003-whatsapp-call/requirements-lock.txt`, serta source dan dependensi
runtime Hermes v0.23.1 pada jalur impor Python. Suite memakai Python 3.12 dan
profil Hermes uji terisolasi. [Catatan verifikasi](STATUS.md)

```sh
python3.12 -m unittest discover -s whatsapp_control/checks -p 'test_*.py'
npm ci
npm run check
node --test whatsapp_control/checks/test_bridge.mjs
```

Source sidecar menggunakan
[Hermes WhatsApp Voice](https://github.com/ashwanthbalakrishnan5/hermes-whatsapp-voice) pada revisi
`ef3b8be4a79651b6c11a720f20a95b9e2939c92e`, dengan perubahan Opus dan diagnostik
lokal. `prepare_whatsapp_rust.py` menyiapkan `whatsapp-rust` pada revisi
`9b9fdb7e5c4211cdc7c837abb6a9ae9c597e35c5` dan memverifikasi perubahan
failover, backport antrean audio, dan persiapan model sebelum jawaban final.
[PR upstream #1815](https://github.com/oxidezap/whatsapp-rust/pull/1815)
Lisensi MIT upstream disertakan dalam direktori source sidecar.

Alur `delegation.created` → `prompt.submit` → `delegation.context.append`
diadaptasi dari [Hermes Live Voice](https://github.com/Synero/hermes-live-voice/tree/0578e0cc35022a9dcb469cb9fbfc4d8afc93fc65).
Sesi DM WhatsApp pemilik menggantikan chat yang dipilih di Hermes Desktop.
Status pengerjaan dan hasil diambil dari receipt native Hermes.

Jalankan ini dari direktori source sidecar dengan Python 3 dan Rust 1.94
atau yang lebih baru untuk menguji dan membangun source.

```sh
python3 prepare_whatsapp_rust.py
cargo test --locked
cargo build --locked --release
```

Launcher memeriksa hash binary arm64 yang dipilih. Persiapan binary, verifikasi
hash, dan pairing diperlukan pada mesin baru sebelum menjalankan layanan.

## Lisensi

Kode proyek memakai [lisensi MIT](LICENSE). Kode dan fixture yang diadaptasi
mempertahankan [atribusi sumber masing-masing](THIRD_PARTY_NOTICES.md).
