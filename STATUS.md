# Status pengembangan

Repositori ini menyimpan snapshot sumber pengembangan WA Control & Live.
Jika memasang dari mesin baru, ikuti [prasyarat, instalasi, dan setup](README.md#setup-plugin).
Untuk melanjutkan pengembangan, lihat [PRD dan checklist](PLAN.md).

## Sudah ada di kode

| Area | Implementasi dan cakupan uji lokal |
| --- | --- |
| Plugin gabungan | Satu manifest Hermes. Koneksi Rust akun bot A melayani Control, DM pemilik, dan telepon. Akun B atau akun tambahan dapat ditautkan untuk Control melalui Baileys. |
| Control | Pesan, file, forwarding, Status, grup, pemilihan akun dan tujuan, serta hasil per tujuan. Uji wildcard mencakup akun yang daftar kontaknya kosong atau gagal dibaca, kelanjutan akun lain, dan retry sesuai daftar penerima awal. |
| Chatbot tamu | Kebijakan DM dan grup per akun, mention, riwayat chat terisolasi, gambar dan voice note sesuai konfigurasi media, dan handoff ke pemilik. Saat model bermasalah, status per akun dan satu pemberitahuan per chat yang memenuhi aturan sudah diuji lokal. |
| Percakapan Live | DM, audio, transkrip, dan pekerjaan mengikuti sesi WhatsApp Hermes yang aktif. Uji simulasi mencakup pergantian sesi, interupsi, dan deduplikasi DM saat audio berpindah provider. |
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

## Masih perlu dibuktikan

- Build sidecar release dari clone bersih, wizard dengan perangkat nyata,
  serta siklus update dan purge dengan layanan tertaut pada kandidat rilis.
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

- Resumption asli melalui handle Gemini dan fork GPT-Live. Pemulihan saat ini
  memakai koneksi audio pengganti dengan konteks Hermes yang sama.
- Rincian penyebab dan waktu retry per alias pada `/walive status`, serta
  penanganan status perangkat bot yang dicabut.
- Cakupan tes klasifikasi kegagalan provider lintas konektor. Usulan untuk
  mengirim DM kegagalan yang dipastikan belum terkirim setelah WhatsApp pulih
  tercatat di PRD dan belum diimplementasikan.

Implementasi lanjutan ditunda pada snapshot awal ini. PRD menyimpan rincian
perilaku sukses dan gagal serta skenario penerimaan yang masih terbuka.
