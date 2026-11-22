# Status pengembangan

Repositori ini menyimpan snapshot sumber pengembangan WA Control & Live.
Jika memasang dari mesin baru, ikuti [instalasi dan pairing](README.md#install-dan-pairing).
Untuk melanjutkan pengembangan, buka [handoff](HANDOFF.md),
[PRD dan checklist](PLAN.md), dan [aturan pengembangan](AGENTS.md).

## Sudah ada di kode

| Area | Implementasi dan cakupan uji lokal |
| --- | --- |
| Plugin gabungan | Satu manifest Hermes. Koneksi Rust akun bot A melayani Control, DM pemilik, dan telepon. Wizard mendeteksi A dari pairing Hermes yang ada. Baileys mengenali akun Control tambahan dari QR dan menolak A sebelum menyimpan sesi. |
| Control | Pesan, file, forwarding, Status, grup, pemilihan akun dan tujuan, serta hasil per tujuan. Uji wildcard mencakup akun yang daftar kontaknya kosong atau gagal dibaca, kelanjutan akun lain, dan retry sesuai daftar penerima awal. |
| Chatbot tamu | Kebijakan DM dan grup per akun, mention, riwayat chat terisolasi, gambar dan voice note sesuai konfigurasi media, dan handoff ke pemilik. Saat model bermasalah, status per akun dan satu pemberitahuan per chat yang memenuhi aturan sudah diuji lokal. |
| Percakapan Live | DM, audio, transkrip, dan pekerjaan mengikuti sesi WhatsApp Hermes yang aktif. Penutupan menyimpan ucapan terakhir yang sudah final walaupun belum dijawab. Provider pengganti menerima transkrip yang selesai selama persiapan, dengan deduplikasi terhadap snapshot. |
| Provider Live | Konektor Codex CLI, Gemini Live, GPT-Live, OpenAI Realtime melalui broker Hermes, dan WebSocket kustom yang kompatibel dengan GPT-Live. Alias bisa dipilih lewat terminal, `/walive`, atau pesan pemilik. Pool mendukung rotasi provider dan key, pergantian saat panggilan aktif, serta fallback. |
| Gangguan Live | Jika pemulihan berjeda saat media aktif, satu klip lokal diputar. Jika pilihan audio habis, jalur pemberitahuan akhir memakai klip lokal dan mencoba DM pemilik. |
| Lifecycle | Installer dan pengelolaan kode memakai CLI Hermes. Wizard, stop, dan purge data plugin tersedia. Tes terisolasi mencakup install, enable, disable, remove, pairing sintetis, dan hasil purge per akun. |

## Hasil verifikasi

Regresi kandidat sebelum publikasi lulus **680 tes Python, 15 subtes,
92 tes Rust, dan 5 tes Baileys**. Tes mencakup unit, koneksi loopback, profil
Hermes v0.23.1 terisolasi, dan simulasi telepon. Tujuh tes Rust yang memerlukan
provider nyata dilewati pada putaran itu.

Untuk commit publik awal `9f9992575468a6935fdf0490209b0836b8313e90`,
regresi Python dan Baileys dijalankan ulang. Instalasi dependensi Node dari
lockfile juga lulus pada salinan sumber bersih. Pada profil Hermes v0.23.1
sementara, instalasi dari URL GitHub publik, registrasi perintah setup,
disable, enable kembali, dan remove lulus. Pengujian CLI ini berhenti sebelum
menjalankan wizard atau layanan WhatsApp.

Kandidat perbaikan transkrip di atas `4e8726f` lulus **684 tes Python,
15 subtes, dan 5 tes bridge Baileys** dengan Python 3.12 dan sumber Hermes
v0.23.1 terisolasi. Regresi penutupan mencakup ucapan final, transkrip parsial,
dan tugas yang sudah dicatat Hermes. Simulasi perpindahan mencakup ucapan yang
selesai selama persiapan dan saat sinyal perpindahan dikirim. Jika sinkronisasi
terakhir gagal, koneksi lama tetap menangani audio. Pengujian perangkat nyata
masih terbuka.

Kandidat interupsi berikutnya lulus **688 tes Python, 15 subtes, dan 5 tes
bridge Baileys**. Regresi baru menguji transkrip final berisi teks dan kosong,
kejadian interupsi yang berulang atau terlambat, serta audio baru yang datang
sebelum event giliran asisten. Lingkungan ini belum punya akun WA Control &
Live uji yang tertaut untuk membandingkan jeda di perangkat dengan Desktop.

Kandidat deteksi dan pairing lulus **694 tes Python, 15 subtes, dan 7 tes
bridge Baileys** dengan sumber Hermes v0.23.2 dan Python 3.12 terisolasi.
Tes mencakup deteksi bot tanpa izin DM lama, konfirmasi izin pemilik,
penolakan bot pada QR Control, dan pengulangan pairing tanpa mengganti
identitas tersimpan. Build release arm64 dengan `cargo build --locked`
berhasil dan lolos pemeriksaan versi lokal. Scan QR dan panggilan dengan
perangkat nyata masih terbuka.

Kandidat paket Git memuat sidecar macOS arm64 terverifikasi dan petunjuk
pascainstalasi. Hermes v0.23.2 menyiapkan dependensi Python dan Node melalui
prompt resmi. `wa-control-live pair` membuka alur penautan langsung, dan
gateway Hermes memiliki proses layanan setelah perangkat tertaut. Regresi
lulus **706 tes Python, 17 subtes, dan 7 tes bridge Baileys**. Uji instalasi
lokal memakai snapshot Git kandidat. Pada profil bersih, instalasi,
disable, enable kembali, dan uninstall lewat CLI Hermes lulus. Profil
uji lokal memverifikasi pemuatan Node yang disiapkan Hermes dan deteksi
bot yang sudah tertaut sebelum QR. Scan QR, pergantian rute gateway, dan
panggilan WhatsApp nyata belum dijalankan.

Pada `35da140`, bot berhasil ditautkan di perangkat nyata. QR akun
tambahan masih terblokir saat gateway profil uji dijeda. Perbaikan membuka
koneksi Baileys sementara hanya untuk pairing akun tambahan dan memakai
lock serta validasi identitas akun yang sama dengan layanan Control.
Regresi lulus **710 tes Python, 17 subtes, dan 7 tes bridge Baileys**,
termasuk penolakan nomor bot, duplikat, dan penulisan inventaris akun saat
gateway dijeda. Panggilan WhatsApp pada perangkat nyata masih perlu diuji.

## Audit acuan ChatGPT Live Desktop

Audit 22 November 2026 memakai kandidat lokal di atas `4e8726f`. Kontrak
pengalaman dan skenario pembanding ada di [PRD](PLAN.md#acuan-perilaku-telepon).
Pemeriksaan ulang mencatat **350 tes Python, 15 subtes, dan 5 tes bridge
Baileys lulus**. Satu probe tambahan mereproduksi celah interupsi di bawah.

| Area | Bukti kandidat dan tindak lanjut |
| --- | --- |
| Control atas akun tertaut | Tool pemilik sudah mencakup pesan dan media, forwarding, Status, pencarian pesan atau kontak yang tersedia, serta pembuatan dan administrasi grup. Regresi lokal memeriksa pemilihan akun dan tujuan, audiens Status, dan tanda terima. Uji akun bot dan akun tambahan pada perangkat nyata masih diperlukan. Daftar kemampuan mengikuti operasi dan izin yang telah diuji. |
| Mengobrol sambil bekerja | Adapter mengirim tugas ke Hermes tanpa menunggu hasil. Progres dan arahan mengikuti ID tugas yang sama. Tes memakai gateway dan provider simulasi, sehingga akurasi memahami maksud, kelancaran berbicara, serta hasil yang sampai di DM tetap perlu dibuktikan di perangkat. |
| Interupsi ketika audio masih antre | Bug pengosongan antrean direproduksi lalu diperbaiki di adapter. Awal ucapan pemilik memicu pengosongan meski provider sudah menandai jawaban selesai. Regresi lokal juga mencakup audio terlambat, sinyal lama ber-ID yang datang saat jawaban baru, dan tugas yang tetap memakai ID semula. Jeda di handset dan status penyampaian transkrip masih perlu bukti. |
| Penutupan oleh asisten | Kandidat punya jalur penutupan nyata dengan salam yang bisa disela. Klasifikasi saat ini memilih satu tindakan dan membatasi penutupan dengan permintaan yang jelas tanpa permintaan kerja lain. Uji penutupan selama pekerjaan masih berjalan, termasuk ucapan gabungan yang mempertahankan pekerjaan. Bandingkan pemicunya dengan Desktop sebelum menetapkan perilaku penutupan proaktif. |
| DM selama panggilan | DM dan lampiran masuk ke konteks suara. Pesan biasa lewat DM tidak secara otomatis meminta jawaban lisan di jalur ini. Kalau pembandingan menunjukkan perbedaan yang penting bagi pemilik, tambahkan skenario khusus sebelum mengubahnya. |

[OpenAI Live Console](https://github.com/openai/openai-live-console) (MIT) dan
[panduan delegasi GPT-Live](https://developers.openai.com/api/docs/guides/live-delegation)
memberi contoh pemisahan audio dan pekerjaan. Implementasi plugin saat ini bisa
memakai pola dari [Hermes Desktop v0.23.1](https://github.com/NousResearch/hermes-agent/blob/v0.23.1/apps/desktop/src/lib/voice-live.ts)
dan [contoh Synero yang sudah diadaptasi](THIRD_PARTY_NOTICES.md). Inventaris
lokal menemukan aplikasi ChatGPT Desktop terpasang, tetapi tidak menemukan
source implementasi Desktop Live yang bisa dipakai ulang. Protokol Realtime
Codex yang sudah dipakai plugin ditandai
[eksperimental dalam dokumentasi app-server](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/app-server/README.md#thread-realtime-events-experimental).
Contoh Live Console menghentikan operasi backend yang tersisa ketika sesi
contohnya ditutup. WA Control & Live mempertahankan tugas yang sudah diterima
oleh antrean Hermes ketika media panggilan ditutup.
[Kode penutupan Live Console](https://github.com/openai/openai-live-console/blob/main/server/session.ts#L450-L485)

## Masih perlu dibuktikan

- Wizard dengan perangkat nyata serta siklus update dan purge dengan
  layanan tertaut pada kandidat rilis.
- Control akun A dan akun tambahan yang berjalan bersamaan, visibilitas Status
  serta audiens di perangkat penerima, izin admin grup, dan chatbot dengan
  media serta handoff pada akun nyata.
- Percakapan yang sama sebelum, selama, dan sesudah telepon WhatsApp nyata,
  termasuk interupsi, pekerjaan Hermes, dan jeda pergantian provider. Hasil
  simulasi belum membuktikan kesetaraan pengalaman dengan ChatGPT Desktop.
- Audio dua arah, autentikasi, model dan suara, limit dan fallback pada
  layanan sebenarnya. Uji `hermes-realtime` memakai akun nyata ditunda
  setelah probe pengembangan berhenti pada `auth_required`. Dukungan audio
  gateway tertentu perlu dibuktikan terhadap endpoint dan protokolnya.
- Kompatibilitas versi Hermes lain dan platform selain macOS Apple Silicon.
  Integrasi sesi aktif memakai bagian internal gateway Hermes yang perlu diuji
  ulang saat versi berubah.

## Pekerjaan kode tersisa

- Ukur interupsi setelah provider selesai menghasilkan suara pada sambungan
  WhatsApp nyata. Adapter sudah mengirim perintah pengosongan antrean. Frame
  yang sudah masuk jalur media berada di luar jangkauan pengosongan ini.
  Uji status penyampaian transkrip berdasarkan bukti yang tersedia.
- Resumption asli melalui handle Gemini dan fork GPT-Live adalah pilihan
  teknis yang masih terbuka. Pemulihan saat ini memakai koneksi audio pengganti
  dengan konteks Hermes yang sama.
- Rincian penyebab dan waktu retry per alias pada `/walive status`, serta
  penanganan status perangkat bot yang dicabut.
- Cakupan tes klasifikasi kegagalan provider lintas konektor. Usulan untuk
  mengirim DM kegagalan yang dipastikan belum terkirim setelah WhatsApp pulih
  tercatat di PRD dan belum diimplementasikan.

PRD menyimpan rincian perilaku sukses dan gagal serta skenario penerimaan
yang masih terbuka.
